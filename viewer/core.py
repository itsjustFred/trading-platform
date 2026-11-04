"""
Shared viewer helper logic used by the FastAPI backend in `viewer/main.py`.

This module keeps strategy/backtest/data/task utilities and legacy helper
functions that are consumed by the FastAPI routes.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data_tools import cache_manager
from data_tools.catalog_builder import build_catalog, scan_ohlcv
from runner.auto_agent import (  # noqa: F401 - re-exported for viewer/main.py and the API surface
    DEFAULT_MODEL_NAME,
    SUPPORTED_MODEL_NAMES,
    run_auto_agent,
)
from runner.llm_providers import (  # noqa: F401 - re-exported for viewer/main.py and the API surface
    DEFAULT_LLM_PROVIDER_NAME,
    SUPPORTED_LLM_PROVIDER_NAMES,
)
from runner.optimizer import (
    OptimizationConfig,  # noqa: F401 - re-exported for viewer/main.py and the API surface
)
from runner.optimizer import (
    run_optimization as execute_optimization,  # noqa: F401 - re-exported for viewer/main.py and the API surface
)
from runner.stats import compute_stats, compute_stats_for_result
from shared.constants import (
    DATA_DIR,
    LOG_DIR,
    OHLCV_COLUMNS,
    OPTIMIZATIONS_DIR,
    normalize_timeframe,
)
from shared.logging import get_logger, redact_text, redact_value
from shared.models import (  # noqa: F401 - re-exported for viewer/main.py and the API surface
    BacktestResult,
    EquityPoint,
    PortfolioStats,
    Trade,
    resolve_defined_stat,
)
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    StrategyEntry,
    iter_strategy_files,
    load_strategy_meta,
    matches_strategy_identifier,
    read_strategy_documentation,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
    update_strategy_display_name,  # noqa: F401 - re-exported for viewer/main.py and the API surface
)
from viewer.comparison import (  # noqa: F401 - re-exported for viewer/main.py and the API surface
    compare_backtests,
    compute_drawdown_pct,
)

logger = get_logger("viewer")


def _log_safe(value: object) -> str:
    return "".join(character if character.isprintable() else "?" for character in str(value))


BACKTESTS_DIR = DATA_DIR / "backtests"
REACT_DIST = Path(__file__).parent.parent / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).parent / "static"

STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOGS_DIR / "tasks"
TRADES_SUFFIX = ".trades"
TRADES_FILE_GLOB = "*.trades.json"
JSON_FILE_GLOB = "*.json"
NOT_FOUND_ERROR = "Not found"
JSON_BODY_REQUIRED_ERROR = "JSON body required"
CATALOG_FILE_NAME = "catalog.json"
TASK_NOT_FOUND_ERROR = "Task not found"
JFOREX_SDK_UNAVAILABLE_ERROR = "JForex SDK classes unavailable"
JAVA_RUNTIME_UNAVAILABLE_ERROR = "Java/JVM runtime unavailable"
ALL_LOGS_NAME = "ALL_LOGS"


def _environment_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(value, minimum)


MAX_DATA_QUERY_TIMEOUT_SECONDS = 7200.0
SINGLEFLIGHT_LEASE_SECONDS = max(
    _environment_float(
        "VIEWER_SINGLEFLIGHT_LEASE_SECONDS",
        MAX_DATA_QUERY_TIMEOUT_SECONDS + 60.0,
        1.0,
    ),
    MAX_DATA_QUERY_TIMEOUT_SECONDS + 60.0,
)
SINGLEFLIGHT_HEARTBEAT_SECONDS = _environment_float(
    "VIEWER_SINGLEFLIGHT_HEARTBEAT_SECONDS", 30.0, 0.1
)
SINGLEFLIGHT_BOOT_ID = secrets.token_urlsafe(16)
TASK_LOG_MAX_BYTES = 64 * 1024
GENERAL_LOG_MAX_BYTES = 2 * 1024 * 1024
GENERAL_LOG_MAX_LINES = 2000
MAX_TERMINAL_TASKS = 1024
_running_jobs: dict[str, dict] = {}  # job_id -> {status, result, error}
_JOBS_LOCK = threading.RLock()
_JOBS_TIMESTAMP: dict[str, float] = {}  # job_id -> last lifecycle transition (monotonic)
_JOBS_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def register_job(job_id: str) -> dict:
    """Register a job as `running` and stamp its lifecycle timestamp."""
    job = {"status": "running", "result": None, "error": None}
    with _JOBS_LOCK:
        _running_jobs[job_id] = job
        _JOBS_TIMESTAMP[job_id] = time.monotonic()
    return dict(job)


def set_job_status(job_id: str, status: str) -> dict | None:
    """Transition a job to `status` atomically (single lock-guarded write)."""
    with _JOBS_LOCK:
        job = _running_jobs.get(job_id)
        if job is None:
            return None
        job["status"] = str(status)
        _JOBS_TIMESTAMP[job_id] = time.monotonic()
        return dict(job)


def finish_job(
    job_id: str,
    *,
    status: str,
    result: Any | None = None,
    error: str | None = None,
) -> dict | None:
    """Finalize a job with result/error in one lock-guarded write."""
    with _JOBS_LOCK:
        job = _running_jobs.get(job_id)
        if job is None:
            return None
        job["status"] = str(status)
        job["result"] = result
        job["error"] = error
        _JOBS_TIMESTAMP[job_id] = time.monotonic()
        return dict(job)


def get_job(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _running_jobs.get(job_id)
        return dict(job) if job is not None else None


def evict_jobs(*, max_age_seconds: float = 3600.0) -> int:
    """Drop terminal jobs whose final transition is older than the grace period.

    Returns the number of evicted records. Non-terminal jobs are never evicted.
    Invariant: `_running_jobs` and `_JOBS_TIMESTAMP` share exactly the same key
    set; every writer must touch both under `_JOBS_LOCK`. The divergence guard
    below turns a forgotten timestamp write into a loud warning instead of a
    silently wrong eviction age.
    """
    now = time.monotonic()
    with _JOBS_LOCK:
        orphan_ids = _JOBS_TIMESTAMP.keys() - _running_jobs.keys()
        if orphan_ids:  # pragma: no cover - a writer bypassed the helpers
            for job_id in orphan_ids:
                _JOBS_TIMESTAMP.pop(job_id, None)
            logger.warning(
                "Job registry: dropped %d orphan timestamp(s) with no job record; "
                "a writer is mutating _running_jobs/_JOBS_TIMESTAMP outside the "
                "register/finish/evict helpers",
                len(orphan_ids),
            )
        stale_ids = [
            job_id
            for job_id, job in _running_jobs.items()
            if str(job.get("status") or "").lower() in _JOBS_TERMINAL_STATUSES
            and now - _JOBS_TIMESTAMP.get(job_id, now) >= max_age_seconds
        ]
        for job_id in stale_ids:
            _running_jobs.pop(job_id, None)
            _JOBS_TIMESTAMP.pop(job_id, None)
        return len(stale_ids)


BARS_BEFORE = 60
BARS_AFTER = 25
_OHLCV_COLUMNS = list(OHLCV_COLUMNS)
_OHLCV_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,63}$")
_OHLCV_TIMEFRAME_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_OHLCV_MAX_MATCHES = 32
_OHLCV_MAX_BYTES = 256 * 1024 * 1024
_OHLCV_MAX_ROWS = 2_000_000
_OHLCV_SOURCE_PRIORITY = {
    "dukascopy": 0,
    "yfinance": 1,
    "huggingface": 2,
    "alpaca": 3,
    "dolthub": 4,
}
_OHLCV_DF_CACHE_LOCK = threading.RLock()
_OHLCV_SOURCE_SIGNATURE = tuple[tuple[str, int, int], ...]
_OHLCV_DF_CACHE: dict[
    tuple[str, str],
    tuple[_OHLCV_SOURCE_SIGNATURE, int, pd.DataFrame | None],
] = {}


def _invalidate_ohlcv_cache(cache_key: tuple[str, str]) -> None:
    with _OHLCV_DF_CACHE_LOCK:
        _OHLCV_DF_CACHE.pop(cache_key, None)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _load_result(backtest_id: str) -> BacktestResult | None:
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return None
    return BacktestResult.load(str(path))


def _optimization_path(optimization_id: str) -> Path:
    normalized_id = str(optimization_id or "").strip()
    if normalized_id.endswith(".json"):
        normalized_id = normalized_id[:-5]
    return OPTIMIZATIONS_DIR / f"{normalized_id}.json"


def _load_optimization_payload(optimization_id: str) -> dict[str, Any] | None:
    path = _optimization_path(optimization_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _optimization_summary_payload(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    summary = {
        "id": payload.get("id", path.stem),
        "filename": path.name,
        "created_at": payload.get("created_at"),
        "base_backtest_id": payload.get("base_backtest_id"),
        "target_metric": payload.get("target_metric"),
        "n_trials": payload.get("n_trials"),
        "train_split_pct": payload.get("train_split_pct"),
        "best_value": payload.get("best_value"),
        "best_params": payload.get("best_params", {}),
        "effective_best_parameters": payload.get("effective_best_parameters", {}),
        "symbols": payload.get("symbols", []),
        "best_symbols": payload.get("best_symbols", []),
        "optimization_mode": payload.get("optimization_mode", "parameter"),
        "symbol_universes": payload.get("symbol_universes", []),
        "train_period": payload.get("train_period", {}),
        "test_period": payload.get("test_period", {}),
        "train_stats": payload.get("train_stats", {}),
        "test_stats": payload.get("test_stats", {}),
    }
    for key in (
        "best_metric_key",
        "best_raw_value",
        "best_value_is_risk_adjusted",
        "oos_comparison",
    ):
        if key in payload:
            summary[key] = payload.get(key)
    if "oos_status" in payload:
        summary["oos_status"] = payload.get("oos_status")
    return summary


def _strategy_entries() -> list[StrategyEntry]:
    entries: list[StrategyEntry] = []
    if not STRATEGIES_DIR.exists():
        return entries

    for path in iter_strategy_files(STRATEGIES_DIR):
        try:
            meta = load_strategy_meta(path, STRATEGIES_DIR)
        except Exception as exc:
            logger.warning("Skipping strategy %s due to import error: %s", path, exc)
            continue

        if not meta:
            continue

        entries.append(
            StrategyEntry(
                slug=strategy_slug_for_path(path, STRATEGIES_DIR),
                file_path=path,
                relative_file=strategy_relative_file(path, STRATEGIES_DIR),
                directory=path.parent,
                documentation_path=strategy_documentation_path(path, STRATEGIES_DIR),
                meta=meta,
            )
        )

    return sorted(entries, key=lambda entry: entry.slug)


def _strategy_entry(identifier: str) -> StrategyEntry | None:
    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    for entry in _strategy_entries():
        if matches_strategy_identifier(entry, normalized):
            return entry
    return None


def _strategy_path(identifier: str) -> Path | None:
    """Resolve a strategy identifier to a file inside ``STRATEGIES_DIR``.

    Only registry identifiers (slug, relative file, module name, display name)
    resolve to a path. The fallback branch resolves relative identifiers that
    name an existing regular file inside the strategies tree; absolute client
    paths and traversal escapes such as ``../x.py`` are rejected so the value
    can be handed to a subprocess without letting a request select arbitrary
    code on disk.
    """
    entry = _strategy_entry(identifier)
    if entry is not None:
        return entry.file_path

    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    # Reject anything with path separators or ".." segments before joining:
    # the registry matcher normalizes identifiers aggressively (punctuation
    # is stripped, so "../demo.py" can alias "demo.py"), and pathlib also
    # drops the base when joining an absolute path. Only a bare relative
    # file name inside STRATEGIES_DIR may pass this branch.
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        return None
    candidate = (STRATEGIES_DIR / normalized).resolve()
    if not candidate.is_file():
        return None
    if not candidate.is_relative_to(STRATEGIES_DIR.resolve()):
        return None
    return candidate


def _strategy_labels(identifier: str) -> tuple[str, str, str]:
    entry = _strategy_entry(identifier)
    slug = entry.slug if entry is not None else (identifier or "unknown")
    display_name = entry.display_name if entry is not None else slug
    relative_file = entry.relative_file if entry is not None else ""
    return slug, display_name, relative_file


def _backtest_info_payload(result: BacktestResult) -> dict:
    meta = result.meta
    raw_strategy = meta.strategies[0] if meta.strategies else "unknown"
    strategy_slug, strategy_display_name, strategy_file = _strategy_labels(raw_strategy)
    notes = (meta.notes or "").strip()
    return {
        "id": meta.id,
        "title": notes or strategy_display_name,
        "notes": notes,
        "strategy": raw_strategy,
        "strategy_slug": strategy_slug,
        "strategy_display_name": strategy_display_name,
        "strategy_file": strategy_file,
        "engine": meta.engine,
        "symbols": meta.data.get("symbols", []),
        "timeframe": {"start": meta.backtest_start, "end": meta.backtest_end},
        "data_timeframe": meta.data.get("timeframes", ["1h"])[0]
        if meta.data.get("timeframes")
        else "1h",
        "timeframes": meta.data.get("timeframes", []),
        "data_source": meta.data.get("source", ""),
        "requested_data_source": meta.data.get("requested_data_source", ""),
        "asset_classes": meta.data.get("asset_classes", []),
        "data_files": meta.data.get("data_files", []),
        "pnl_basis": meta.data.get("pnl_basis", {}),
        "risk_config": meta.data.get("risk_config", {}),
        "initial_capital": meta.initial_capital,
        "duration_days": meta.duration_days,
        "total_bars": meta.total_bars,
        "created_at": meta.created_at,
        "trade_count": len(result.trades),
        "parameters": meta.parameters,
        "has_equity_curve": bool(result.equity_curve),
        "has_indicators": bool(result.stats.get("indicators")),
    }


def _iso_to_unix(iso_str: str) -> float:
    """Convert ISO 8601 string to Unix timestamp (seconds)."""
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _normalize_equity_points(points: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for point in points:
        raw_time = point.get("time")
        unix_time = raw_time if isinstance(raw_time, (int, float)) else _iso_to_unix(str(raw_time))
        if unix_time <= 0:
            continue
        equity_value = _safe_float(point.get("equity"), float("nan"))
        if not math.isfinite(equity_value):
            continue
        normalized.append({"time": float(unix_time), "equity": round(equity_value, 2)})

    normalized.sort(key=lambda item: item["time"])
    collapsed: list[dict] = []
    for point in normalized:
        if collapsed and point["time"] == collapsed[-1]["time"]:
            collapsed[-1] = point
        else:
            collapsed.append(point)
    return collapsed


def _trade_net_pnl(trade: Trade) -> float:
    value = getattr(trade, "net_pnl", None)
    if value is not None:
        numeric = _safe_float(value, float("nan"))
        if math.isfinite(numeric):
            return numeric
    gross = _safe_float(getattr(trade, "pnl", 0.0), 0.0)
    fees = _safe_float(getattr(trade, "fees", 0.0), 0.0)
    return gross - fees


def _trade_realized_risk(trade: Trade) -> tuple[float | None, float | None]:
    metadata = trade.meta if isinstance(getattr(trade, "meta", None), dict) else {}
    risk_math = metadata.get("risk_math") if isinstance(metadata.get("risk_math"), dict) else {}
    realized_amount = risk_math.get("realized_risk_amount")
    realized_percent = risk_math.get("realized_risk_percent")
    if realized_amount is None:
        realized_amount = metadata.get("realized_risk_amount")
    if realized_percent is None:
        realized_percent = metadata.get("realized_risk_percent")
    if realized_amount is not None:
        amount = _safe_float(realized_amount, float("nan"))
        if math.isfinite(amount) and amount >= 0:
            percent = realized_percent
            if percent is None:
                equity = _safe_float(risk_math.get("equity_at_entry"), 0.0)
                percent = amount / equity if equity > 0 else None
            else:
                percent = _safe_float(percent, float("nan"))
                if not math.isfinite(percent):
                    percent = None
            return amount, percent
    if realized_percent is not None:
        percent = _safe_float(realized_percent, float("nan"))
        if math.isfinite(percent):
            return None, percent
    if not risk_math:
        amount = _safe_float(getattr(trade, "risk_amount", None), float("nan"))
        if math.isfinite(amount) and amount >= 0:
            percent = _safe_float(getattr(trade, "risk_percent", None), float("nan"))
            return amount, (percent if math.isfinite(percent) else None)
    return None, None


def _equity_curve_from_trades(result: BacktestResult) -> list[dict]:
    if not result.trades:
        return []

    initial_capital = _safe_float(result.meta.initial_capital, 0.0)
    ordered_trades = sorted(
        result.trades,
        key=lambda trade: (
            _iso_to_unix(trade.last_exit_time or trade.entry_time),
            _iso_to_unix(trade.entry_time),
            trade.id,
        ),
    )

    first_trade_time = _iso_to_unix(
        ordered_trades[0].entry_time or ordered_trades[0].last_exit_time
    )
    points: list[dict] = []
    if first_trade_time > 0:
        points.append({"time": first_trade_time, "equity": round(initial_capital, 2)})

    running_equity = initial_capital
    for trade in ordered_trades:
        event_time = _iso_to_unix(trade.last_exit_time or trade.entry_time)
        if event_time <= 0:
            continue
        running_equity += _trade_net_pnl(trade)
        points.append({"time": event_time, "equity": round(running_equity, 2)})

    return _normalize_equity_points(points)


def _trade_to_viewer(trade) -> dict:
    """Convert a Universal Format Trade to viewer-friendly flat format."""
    last_exit = trade.exits[-1] if trade.exits else None
    trade_meta = trade.meta if isinstance(trade.meta, dict) else {}
    risk_math = trade_meta.get("risk_math") if isinstance(trade_meta.get("risk_math"), dict) else {}

    position_size = trade.position_size
    if position_size is None:
        position_size = risk_math.get("position_size")

    risk_amount, risk_percent = _trade_realized_risk(trade)

    exit_reason = trade.exit_reason or (last_exit.reason if last_exit else None)
    if exit_reason is None and isinstance(trade_meta.get("exit_reason"), str):
        exit_reason = str(trade_meta.get("exit_reason"))

    net_pnl = _trade_net_pnl(trade)
    payload = {
        "nr": trade.id,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_time": _iso_to_unix(trade.entry_time),
        "entry_time_iso": trade.entry_time,
        "entry_price": trade.entry_price,
        "exit_time": _iso_to_unix(last_exit.time) if last_exit else 0,
        "exit_time_iso": last_exit.time if last_exit else "",
        "exit_price": last_exit.price if last_exit else 0,
        "quantity": trade.quantity,
        "position_size": position_size,
        "risk_amount": risk_amount,
        "risk_percent": risk_percent,
        "exit_reason": exit_reason,
        "pnl": net_pnl,
        "gross_pnl": trade.pnl,
        "net_pnl": net_pnl,
        "pnl_pct": trade.pnl_pct,
        "r_multiple": trade.r_multiple,
        "holding_bars": trade.holding_bars,
        "fees": trade.fees,
        "tags": trade.tags,
        "exits": [
            {
                "time": _iso_to_unix(e.time),
                "time_iso": e.time,
                "price": e.price,
                "quantity": e.quantity,
                "pnl": e.pnl,
                "reason": e.reason,
            }
            for e in trade.exits
        ],
    }
    if trade.meta:
        payload["meta"] = trade.meta
    return payload


def _ohlcv_candidates(root: Path, symbol: str, timeframe: str) -> list[Path]:
    normalized_symbol = symbol.casefold()
    normalized_timeframe = normalize_timeframe(timeframe).casefold()
    candidates: list[tuple[int, str, Path]] = []
    for path in root.rglob("*.parquet"):
        try:
            relative = path.relative_to(root)
            parts = relative.parts
            if len(parts) < 2 or path.is_symlink() or not path.is_file():
                continue
            if parts[-2].casefold() != normalized_symbol:
                continue
            file_timeframe = path.stem
            if normalize_timeframe(file_timeframe).casefold() != normalized_timeframe:
                continue
            source = parts[0].casefold()
            priority = _OHLCV_SOURCE_PRIORITY.get(source, len(_OHLCV_SOURCE_PRIORITY))
            candidates.append((priority, source, path))
            if len(candidates) > _OHLCV_MAX_MATCHES:
                return []
        except (OSError, ValueError):
            continue
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1], str(item[2])))
    selected_source = candidates[0][1]
    return [item[2] for item in candidates if item[1] == selected_source]


def _parquet_row_count(path: Path) -> int:
    metadata = pq.ParquetFile(path).metadata
    if metadata is None:
        raise ValueError("Parquet metadata is unavailable")
    rows = int(metadata.num_rows)
    if rows < 0:
        raise ValueError("Parquet row count is invalid")
    return rows


def _read_parquet_frame_bounded(path: Path, max_rows: int) -> pd.DataFrame | None:
    row_limit = max(0, int(max_rows))
    parquet_file = pq.ParquetFile(path)
    try:
        schema_names = [str(name) for name in parquet_file.schema.names]
        canonical_names: dict[str, str] = {}
        for schema_name in schema_names:
            canonical_names.setdefault(schema_name.lower(), schema_name)
        missing = [column for column in _OHLCV_COLUMNS if column not in canonical_names]
        if missing:
            raise ValueError(f"OHLCV parquet is missing canonical columns: {missing}")
        columns = [canonical_names[column] for column in _OHLCV_COLUMNS]
        batches = iter(
            parquet_file.iter_batches(
                columns=columns,
                batch_size=min(65_536, max(1, row_limit)),
            )
        )
        frames: list[pd.DataFrame] = []
        rows = 0
        while True:
            if rows >= row_limit:
                extra = next(batches, None)
                if extra is not None and extra.num_rows:
                    return None
                break
            batch = next(batches, None)
            if batch is None:
                break
            if rows + int(batch.num_rows) > row_limit:
                return None
            frame = batch.to_pandas()
            rows += int(batch.num_rows)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        if len(frames) == 1:
            return frames[0]
        return pd.concat(frames, ignore_index=True, sort=False)
    finally:
        parquet_file.close()


def _load_ohlcv_df(symbol: str, timeframe: str) -> pd.DataFrame | None:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    cache_key = (normalized_symbol, normalized_timeframe)
    if (
        _OHLCV_SYMBOL_RE.fullmatch(normalized_symbol) is None
        or _OHLCV_TIMEFRAME_RE.fullmatch(normalized_timeframe) is None
    ):
        _invalidate_ohlcv_cache(cache_key)
        return None

    ohlcv_root = (DATA_DIR / "ohlcv").resolve()
    matches = _ohlcv_candidates(ohlcv_root, normalized_symbol, normalized_timeframe)
    if not matches or len(matches) > _OHLCV_MAX_MATCHES:
        _invalidate_ohlcv_cache(cache_key)
        return None

    signature_parts: list[tuple[str, int, int]] = []
    total_bytes = 0
    for path in matches:
        try:
            stat = path.stat()
        except OSError as exc:
            _invalidate_ohlcv_cache(cache_key)
            logger.warning("Could not stat OHLCV source %s: %s", _log_safe(path), exc)
            return None
        total_bytes += int(stat.st_size)
        if total_bytes > _OHLCV_MAX_BYTES:
            _invalidate_ohlcv_cache(cache_key)
            logger.warning(
                "OHLCV byte budget exceeded for %s %s",
                normalized_symbol,
                normalized_timeframe,
            )
            return None
        signature_parts.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    signature: _OHLCV_SOURCE_SIGNATURE = tuple(signature_parts)

    with _OHLCV_DF_CACHE_LOCK:
        cached_entry = _OHLCV_DF_CACHE.get(cache_key)
    cached = cached_entry if cached_entry is not None and cached_entry[0] == signature else None
    if cached is not None and cached[2] is not None:
        return cached[2].copy(deep=False)

    if cached is not None:
        total_rows = cached[1]
    else:
        total_rows = 0
        for path in matches:
            try:
                total_rows += _parquet_row_count(path)
            except (OSError, ValueError, TypeError, pa.ArrowException) as exc:
                _invalidate_ohlcv_cache(cache_key)
                logger.warning("Could not inspect OHLCV metadata from %s: %s", _log_safe(path), exc)
                return None

    if total_rows > _OHLCV_MAX_ROWS:
        _invalidate_ohlcv_cache(cache_key)
        logger.warning(
            "OHLCV row budget exceeded for %s %s",
            normalized_symbol,
            normalized_timeframe,
        )
        return None

    if cached is None:
        with _OHLCV_DF_CACHE_LOCK:
            _OHLCV_DF_CACHE[cache_key] = (signature, total_rows, None)

    frames: list[pd.DataFrame] = []
    materialized_rows = 0
    for path in matches:
        try:
            frame = _read_parquet_frame_bounded(
                path,
                _OHLCV_MAX_ROWS - materialized_rows,
            )
        except (ValueError, KeyError, OSError, pa.ArrowException) as exc:
            logger.warning("Could not load OHLCV segment from %s: %s", _log_safe(path), exc)
            continue
        if frame is None:
            _invalidate_ohlcv_cache(cache_key)
            logger.warning(
                "OHLCV row budget exceeded while streaming %s %s",
                normalized_symbol,
                normalized_timeframe,
            )
            return None
        materialized_rows += len(frame)
        frame.columns = [str(column).lower() for column in frame.columns]
        if "timestamp" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            frame = frame.dropna(subset=["timestamp"])
        if not frame.empty:
            frames.append(frame)

    if not frames:
        return None
    merged = (
        frames[0].copy()
        if len(frames) == 1
        else pd.concat(frames, ignore_index=True, sort=False)
    )
    if "timestamp" in merged.columns:
        merged = (
            merged.dropna(subset=["timestamp"])
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )
    else:
        merged = merged.drop_duplicates().reset_index(drop=True)

    with _OHLCV_DF_CACHE_LOCK:
        _OHLCV_DF_CACHE[cache_key] = (signature, total_rows, merged)
    return merged.copy(deep=False)


def _infer_symbol_data_category(symbol: str) -> str:
    symbol_upper = symbol.upper()
    entries, used_catalog = _load_catalog_ohlcv_entries()
    if not used_catalog:
        entries = _scan_ohlcv_filesystem()

    for entry in entries:
        entry_symbol = str(entry.get("symbol", "")).upper()
        if entry_symbol != symbol_upper:
            continue
        return _categorize_path(str(entry.get("path", "")))

    if symbol_upper in {"XAUUSD", "XAGUSD"}:
        return "Metals"
    if len(symbol_upper) == 6 and symbol_upper.isalpha():
        return "Forex"
    if 1 <= len(symbol_upper) <= 5 and symbol_upper.isalpha():
        return "Equities"
    if symbol_upper.endswith("USD") and len(symbol_upper) > 6:
        return "Crypto"
    return "Other"


def _missing_ohlcv_details(
    symbol: str,
    timeframe: str,
    *,
    backtest_start: str = "",
    backtest_end: str = "",
) -> dict[str, Any]:
    category = _infer_symbol_data_category(symbol)
    category_lower = category.lower()

    normalized_timeframe = str(timeframe or "").strip().lower()
    intraday_timeframes = {"1m", "5m", "15m", "30m", "1h"}

    if category_lower in {"forex", "metals", "indices", "energy"}:
        suggested_source = "dukascopy"
        alternatives = ["yfinance"]
    elif category_lower in {"equities", "etfs"}:
        if normalized_timeframe in intraday_timeframes:
            suggested_source = "alpaca"
            alternatives = ["huggingface", "yfinance"]
        else:
            suggested_source = "yfinance"
            alternatives = ["dolthub", "alpaca"]
    else:
        suggested_source = "dukascopy"
        alternatives = ["yfinance", "dolthub"]

    return {
        "code": "MISSING_OHLCV",
        "message": f"Missing OHLCV data for {symbol} {timeframe}",
        "symbol": symbol,
        "timeframe": timeframe,
        "category": category,
        "suggested_source": suggested_source,
        "alternative_sources": alternatives,
        "download_request": {
            "source": suggested_source,
            "symbols": symbol,
            "timeframes": timeframe,
            "start": backtest_start,
            "end": backtest_end,
        },
    }


def _preferred_ohlcv_column(df: pd.DataFrame, base_name: str) -> str | None:
    candidates = [base_name, f"bid_{base_name}", f"ask_{base_name}"]
    columns = set(df.columns)
    for candidate in candidates:
        if candidate not in columns:
            continue
        numeric = pd.to_numeric(df[candidate], errors="coerce")
        if np.isfinite(numeric).any():
            return candidate
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _df_to_ohlcv_records(df: pd.DataFrame) -> list[dict]:
    """Convert OHLCV DataFrame to list of dicts with Unix timestamps."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return []

    open_col = _preferred_ohlcv_column(df, "open")
    high_col = _preferred_ohlcv_column(df, "high")
    low_col = _preferred_ohlcv_column(df, "low")
    close_col = _preferred_ohlcv_column(df, "close")
    volume_col = _preferred_ohlcv_column(df, "volume")
    if not open_col or not high_col or not low_col or not close_col:
        return []

    timestamp_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    timestamp_values = timestamp_series.array.asi8.astype("float64", copy=False)
    nat_sentinel = float(np.iinfo(np.int64).min)
    timestamp_values[timestamp_values == nat_sentinel] = np.nan
    unit = str(getattr(timestamp_series.dtype, "unit", "ns") or "ns").lower()
    unit_scale = {
        "s": 1.0,
        "ms": 1_000.0,
        "us": 1_000_000.0,
        "ns": 1_000_000_000.0,
    }.get(unit, 1_000_000_000.0)
    unix_time = timestamp_values / unit_scale

    open_series = pd.to_numeric(df[open_col], errors="coerce")
    high_series = pd.to_numeric(df[high_col], errors="coerce")
    low_series = pd.to_numeric(df[low_col], errors="coerce")
    close_series = pd.to_numeric(df[close_col], errors="coerce")
    if volume_col:
        volume_series = pd.to_numeric(df[volume_col], errors="coerce")
    else:
        volume_series = pd.Series(0.0, index=df.index, dtype="float64")

    valid_mask = (
        np.isfinite(unix_time)
        & (unix_time > 0)
        & np.isfinite(open_series)
        & np.isfinite(high_series)
        & np.isfinite(low_series)
        & np.isfinite(close_series)
    )
    if not valid_mask.any():
        return []

    normalized = pd.DataFrame(
        {
            "time": unix_time[valid_mask],
            "open": open_series[valid_mask].astype(float),
            "high": high_series[valid_mask].astype(float),
            "low": low_series[valid_mask].astype(float),
            "close": close_series[valid_mask].astype(float),
            "volume": volume_series[valid_mask].fillna(0.0).astype(float),
        }
    )
    if normalized.empty:
        return []

    normalized = normalized.drop_duplicates(subset=["time"], keep="last").sort_values("time")
    normalized["volume"] = normalized["volume"].round().astype(int)
    return normalized.to_dict(orient="records")


def _drawing_timestamp_to_unix(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return float(pd.Timestamp(ts).timestamp())


def _coerce_finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _drawing_matches_symbol(drawing: dict[str, Any], symbol: str) -> bool:
    drawing_symbol = drawing.get("symbol")
    if drawing_symbol in (None, ""):
        return True
    return str(drawing_symbol).upper() == symbol.upper()


def _drawing_to_viewer(drawing: dict[str, Any]) -> dict[str, Any] | None:
    drawing_type = str(drawing.get("type", "")).strip()
    payload = dict(drawing)
    if drawing_type == "marker":
        unix_time = _drawing_timestamp_to_unix(drawing.get("time"))
        if unix_time is None:
            logger.warning("Skipping drawing marker with invalid time: %s", drawing)
            return None
        payload["time"] = unix_time
        return payload

    if drawing_type == "line":
        start_time = _drawing_timestamp_to_unix(drawing.get("start_time"))
        end_time = _drawing_timestamp_to_unix(drawing.get("end_time"))
        if start_time is None or end_time is None:
            logger.warning("Skipping drawing line with invalid timestamps: %s", drawing)
            return None
        payload["start_time"] = start_time
        payload["end_time"] = end_time
        start_value = payload.get("start_value", payload.get("val1"))
        end_value = payload.get("end_value", payload.get("val2"))
        start_numeric = _coerce_finite_float(start_value)
        end_numeric = _coerce_finite_float(end_value)
        if start_numeric is None or end_numeric is None:
            logger.warning("Skipping drawing line with invalid values: %s", drawing)
            return None
        payload["start_value"] = start_numeric
        payload["end_value"] = end_numeric
        payload["val1"] = start_numeric
        payload["val2"] = end_numeric
        return payload

    if drawing_type == "line_with_breaks":
        points = drawing.get("data", [])
        if not isinstance(points, list) or not points:
            logger.warning("Skipping line_with_breaks drawing with invalid data: %s", drawing)
            return None
        points_by_time: dict[float, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            unix_time = _drawing_timestamp_to_unix(point.get("time"))
            if unix_time is None:
                continue
            normalized_point = dict(point)
            normalized_point["time"] = unix_time
            raw_value = normalized_point.get("value")
            if raw_value is None:
                normalized_point["value"] = None
            else:
                normalized_point["value"] = _coerce_finite_float(raw_value)
            points_by_time[unix_time] = normalized_point
        normalized_points = [points_by_time[key] for key in sorted(points_by_time.keys())]
        if not normalized_points:
            logger.warning("Skipping line_with_breaks drawing without valid points: %s", drawing)
            return None
        payload["data"] = normalized_points
        payload["start_time"] = normalized_points[0]["time"]
        payload["end_time"] = normalized_points[-1]["time"]
        return payload

    if drawing_type == "box":
        start_time = _drawing_timestamp_to_unix(drawing.get("start_time"))
        end_time = _drawing_timestamp_to_unix(drawing.get("end_time"))
        if start_time is None or end_time is None:
            logger.warning("Skipping drawing box with invalid timestamps: %s", drawing)
            return None
        payload["start_time"] = start_time
        payload["end_time"] = end_time
        return payload

    logger.warning("Skipping unsupported drawing type %r", drawing_type)
    return None


def _parse_symbol_list(raw_symbols: str) -> list[str]:
    return [symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()]


def _parse_timeframe_list(raw_timeframes: str) -> list[str]:
    return [
        timeframe.strip().lower() for timeframe in raw_timeframes.split(",") if timeframe.strip()
    ]


def _normalize_dukascopy_genesis_timeframe(timeframe: str) -> str:
    normalized = timeframe.strip().lower()
    aliases = {
        "sec": "s1",
        "1m": "m1",
        "5m": "m5",
        "15m": "m15",
        "30m": "m30",
        "1h": "h1",
        "4h": "h4",
        "1d": "d1",
    }
    return aliases.get(normalized, normalized)


DUKASCOPY_COVERAGE_TIMEFRAMES: tuple[str, ...] = (
    "tick",
    "s1",
    "m1",
    "m5",
    "m15",
    "m30",
    "h1",
    "h4",
    "d1",
    "mn1",
)


def _load_dukascopy_genesis() -> dict[str, dict[str, str]]:
    from shared.constants import resolve_genesis_path

    genesis_path = resolve_genesis_path(DATA_DIR)
    if not genesis_path.exists():
        return {}

    try:
        data = json.loads(genesis_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read Dukascopy genesis file %s: %s", genesis_path, e)
        return {}

    if not isinstance(data, dict):
        logger.warning("Invalid Dukascopy genesis payload in %s", genesis_path)
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for raw_symbol, raw_value in data.items():
        symbol = str(raw_symbol).upper()
        if isinstance(raw_value, dict):
            timeframe_map = {
                _normalize_dukascopy_genesis_timeframe(str(timeframe)): str(genesis)
                for timeframe, genesis in raw_value.items()
                if str(genesis).strip()
            }
            if timeframe_map:
                normalized[symbol] = dict(sorted(timeframe_map.items()))
        elif str(raw_value).strip():
            normalized[symbol] = {"default": str(raw_value)}
    return normalized


def _collect_dukascopy_genesis(
    genesis_index: dict[str, dict[str, str]],
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, dict[str, dict[str, str | bool | None]]]:
    requested_symbols_raw = symbols or sorted(genesis_index)
    requested_symbols: list[str] = []
    seen_symbols: set[str] = set()
    for symbol in requested_symbols_raw:
        normalized = str(symbol).strip().upper()
        if not normalized or normalized in seen_symbols:
            continue
        seen_symbols.add(normalized)
        requested_symbols.append(normalized)

    requested_timeframes_raw = list(timeframes or DUKASCOPY_COVERAGE_TIMEFRAMES)
    requested_timeframes: list[str] = []
    seen_timeframes: set[str] = set()
    for timeframe in requested_timeframes_raw:
        normalized = _normalize_dukascopy_genesis_timeframe(str(timeframe))
        if not normalized or normalized in seen_timeframes:
            continue
        seen_timeframes.add(normalized)
        requested_timeframes.append(normalized)

    coverage: dict[str, dict[str, dict[str, str | bool | None]]] = {}
    for symbol in requested_symbols:
        symbol_data = genesis_index.get(symbol, {})
        timeframe_map: dict[str, dict[str, str | bool | None]] = {}
        for timeframe in requested_timeframes:
            raw_start = symbol_data.get(timeframe)
            start = str(raw_start).strip() if raw_start is not None else ""
            timeframe_map[timeframe] = {
                "available": bool(start),
                "start": start or None,
            }
        coverage[symbol] = timeframe_map

    return coverage


def _equity_to_viewer(result: BacktestResult) -> list[dict]:
    """Extract equity curve as [{time, equity}] with Unix timestamps.
    If no equity_curve in JSON, compute from trades."""
    if result.equity_curve:
        cached_curve = _normalize_equity_points(
            [{"time": point.time, "equity": point.equity} for point in result.equity_curve]
        )
        if cached_curve:
            return cached_curve
    return _equity_curve_from_trades(result)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_stats_from_result(result: BacktestResult) -> dict:
    """Use persisted runner stats when available, otherwise compute canonical fallbacks."""
    canonical_keys = {"total_pnl", "sharpe_ratio", "cagr", "max_drawdown_pct"}
    cached_stats = dict(result.stats or {})
    if canonical_keys.issubset(cached_stats):
        return cached_stats

    computed = compute_stats_for_result(result)
    return {**cached_stats, **computed}


def _defined_stat_value(
    stats: dict | None,
    key: str,
    *,
    scale: float = 1.0,
    digits: int | None = None,
) -> float | None:
    """A stat the producer actually measured, or ``None``.

    ``dict.get(key, 0)`` cannot tell "computed as zero" from "never computed";
    :func:`resolve_defined_stat` can, because the producer names what it could
    not compute in ``undefined_metrics``. A leftover ``NaN``/``Infinity`` (or a
    non-numeric string) is not a measurement either, so it resolves to ``None``
    as well. The client renders ``None`` as unavailable instead of grading it.
    """
    value = resolve_defined_stat(stats, key, None)
    numeric = _safe_float(value, float("nan"))
    if not math.isfinite(numeric):
        return None
    scaled = numeric * scale
    return round(scaled, digits) if digits is not None else scaled


# ─── API ──────────────────────────────────────────────────────────────────────


def _quick_stats_and_equity(
    trades: list,
    initial_capital: float,
    start_str: str,
    end_str: str,
    equity_curve: list[dict] | None = None,
    cached_stats: dict | None = None,
) -> dict:
    """Build lightweight list-card stats from persisted runner stats when available."""
    if not trades and not equity_curve:
        # No trades means no return series, so Sharpe, CAGR, PSR and p-value were
        # never computed. Reporting 0 / 0 / 1.0 here would present an empty run
        # as a measured result (and 1.0 is the worst attainable p-value). The
        # zeroes that *are* real - no profit, no drawdown - stay zero.
        return {
            "sharpe": None,
            "cagr": None,
            "max_drawdown_pct": 0,
            "net_profit": 0,
            "net_profit_pct": 0,
            "total_return": 0,
            "p_value": None,
            "psr": None,
            "equity_mini": [],
        }

    cached_stats = dict(cached_stats or {})
    equity_points: list[EquityPoint] = []
    if equity_curve:
        try:
            equity_points = [
                point if isinstance(point, EquityPoint) else EquityPoint.from_dict(point)
                for point in equity_curve
            ]
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Could not parse cached equity curve: %s", exc)
            equity_points = []

    required_cached_keys = {
        "total_pnl",
        "sharpe_ratio",
        "cagr",
        "max_drawdown_pct",
        "psr",
        "p_value",
    }
    derived_stats = dict(cached_stats)
    if not required_cached_keys.issubset(derived_stats):
        try:
            trade_models = [
                trade if isinstance(trade, Trade) else Trade.from_dict(trade)
                for trade in trades
            ]
            derived_stats = compute_stats(
                trades=trade_models,
                initial_capital=initial_capital,
                equity_curve=equity_points or None,
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Could not compute canonical quick stats: %s", exc)
            derived_stats = dict(cached_stats)

    if equity_points:
        equity = [point.equity for point in equity_points]
        total_pnl = _safe_float(
            derived_stats.get("total_pnl"),
            equity[-1] - equity[0] if len(equity) > 1 else 0.0,
        )
    else:
        equity = [initial_capital]
        running = initial_capital
        for trade in trades:
            if isinstance(trade, Trade):
                pnl = _trade_net_pnl(trade)
            else:
                pnl = _safe_float(trade.get("net_pnl"), float("nan"))
                if not math.isfinite(pnl):
                    pnl = _safe_float(trade.get("pnl"), 0.0)
                    pnl -= _safe_float(trade.get("fees"), 0.0)
            running += pnl
            equity.append(round(running, 2))
        total_pnl = running - initial_capital

    total_return_fraction = (total_pnl / initial_capital) if initial_capital else 0.0
    net_profit_pct = round(total_return_fraction * 100, 2)

    if "max_drawdown_pct" in derived_stats:
        max_drawdown_pct = round(abs(_safe_float(derived_stats.get("max_drawdown_pct"))) * 100, 2)
    else:
        peak = equity[0]
        max_dd = 0.0
        for value in equity:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        max_drawdown_pct = round(max_dd * 100, 2)

    cagr_value = _defined_stat_value(derived_stats, "cagr")
    cagr = round(cagr_value * 100, 2) if cagr_value is not None else None
    sharpe_value = _defined_stat_value(derived_stats, "sharpe_ratio")
    sharpe = round(sharpe_value, 3) if sharpe_value is not None else None

    psr_val = _defined_stat_value(derived_stats, "psr")
    p_value_value = _defined_stat_value(derived_stats, "p_value")
    p_value = round(p_value_value, 6) if p_value_value is not None else None

    equity_mini = _downsample_numeric_minmax([float(v) for v in equity if isinstance(v, (int, float))], limit=50)

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_drawdown_pct": max_drawdown_pct,
        "net_profit": round(total_pnl, 2),
        "net_profit_pct": net_profit_pct,
        # A fraction, the engine definition (E_end / E_start - 1) that
        # frontend/src/api/metricScale.ts declares for total_return. The
        # net_profit_pct above is this same number in percent points.
        "total_return": round(total_return_fraction, 8),
        "psr": psr_val,
        "p_value": p_value,
        "equity_mini": equity_mini,
    }


def _downsample_numeric_minmax(values: list[float], *, limit: int) -> list[float]:
    if not values:
        return []
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 2000))
    if len(values) <= normalized_limit:
        return list(values)

    bucket_count = max(1, (normalized_limit - 1) // 2)
    n = len(values)
    step = n / bucket_count

    sampled: list[float] = []
    for bucket in range(bucket_count):
        start = int(bucket * step)
        end = int((bucket + 1) * step)
        if bucket == bucket_count - 1:
            end = n
        if start >= n:
            break
        if end <= start:
            end = min(n, start + 1)
        window = values[start:end]
        if not window:
            continue
        min_val = min(window)
        max_val = max(window)
        if min_val == max_val:
            sampled.append(float(min_val))
        else:
            sampled.append(float(min_val))
            sampled.append(float(max_val))

    sampled.append(float(values[-1]))
    if len(sampled) > normalized_limit:
        sampled = sampled[: normalized_limit - 1] + [float(values[-1])]
    return sampled


def _stats_payload_from_result(result: BacktestResult) -> dict:
    """Normalize field names for the frontend stats contract.

    `best_trade`/`worst_trade` are exact per-trade extremes computed from the
    closed trades; `avg_winner`/`avg_loser` stay available under their own
    names. Win/loss counts are whole-backtest values from the canonical stats.
    """
    raw = _canonical_stats_from_result(result)

    net_pnls = [_trade_net_pnl(trade) for trade in (result.trades or [])]
    pnls = [pnl for pnl in net_pnls if not math.isclose(pnl, 0.0, abs_tol=1e-12)]
    best_trade = max(pnls) if pnls else 0
    worst_trade = min(pnls) if pnls else 0
    net_total = sum(net_pnls)
    winners = sum(1 for pnl in net_pnls if pnl > 0)
    losers = sum(1 for pnl in net_pnls if pnl < 0)
    avg_winner = sum(pnl for pnl in net_pnls if pnl > 0) / winners if winners else 0
    avg_loser = sum(pnl for pnl in net_pnls if pnl < 0) / losers if losers else 0
    win_rate = winners / len(net_pnls) if net_pnls else 0.0
    profit_factor = (
        sum(pnl for pnl in net_pnls if pnl > 0) / abs(sum(pnl for pnl in net_pnls if pnl < 0))
        if any(pnl < 0 for pnl in net_pnls)
        else None
    )

    # Persisted runner stats carry total_return as a FRACTION (the engine
    # definition is E_end / E_start - 1, see runner/stats.py) and
    # frontend/src/api/metricScale.ts declares it a "fraction" for this payload,
    # so it is passed through unscaled. Fall back to the net figure when the
    # engine did not record it.
    total_return_fraction = _safe_float(raw.get("total_return"), float("nan"))
    if not math.isfinite(total_return_fraction):
        total_return_fraction = (
            (net_total if result.trades else _safe_float(raw.get("total_pnl"), 0.0))
            / (result.meta.initial_capital or 100000)
        )

    cagr_value = _defined_stat_value(raw, "cagr")
    stats = {
        "net_profit": round(net_total, 2) if result.trades else raw.get("total_pnl", 0),
        "net_profit_pct": round(
            (net_total if result.trades else _safe_float(raw.get("total_pnl"), 0.0))
            / (result.meta.initial_capital or 100000)
            * 100,
            2,
        ),
        # Risk-adjusted ratios are named in `undefined_metrics` when the return
        # series was too short to compute them. `raw.get(key, 0)` published that
        # absence as a measured 0, so the detail card graded an unmeasurable run.
        "sharpe": _defined_stat_value(raw, "sharpe_ratio"),
        "sortino": _defined_stat_value(raw, "sortino_ratio"),
        "calmar": _defined_stat_value(raw, "calmar_ratio"),
        "max_drawdown_pct": round(raw.get("max_drawdown_pct", 0) * 100, 2),
        "win_rate": round(
            (win_rate if net_pnls else _safe_float(raw.get("win_rate"), 0.0)) * 100,
            1,
        ),
        "profit_factor": profit_factor if net_pnls else raw.get("profit_factor", 0),
        "total_trades": len(net_pnls) if net_pnls else raw.get("total_trades", 0),
        "winners": winners if net_pnls else raw.get("winners", 0),
        "losers": losers if net_pnls else raw.get("losers", 0),
        "avg_winner": round(avg_winner, 2) if net_pnls else raw.get("avg_winner", 0),
        "avg_loser": round(avg_loser, 2) if net_pnls else raw.get("avg_loser", 0),
        "avg_trade_pnl": round(
            (net_total if result.trades else _safe_float(raw.get("total_pnl"), 0.0))
            / max(len(net_pnls) if net_pnls else raw.get("total_trades", 1), 1),
            2,
        ),
        "avg_bars_held": raw.get("avg_holding_bars", 0),
        "max_consecutive_losses": raw.get("max_consecutive_losses", 0),
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "cagr": round(cagr_value * 100, 2) if cagr_value is not None else None,
        "total_return": round(total_return_fraction, 6),
        "expectancy": 0,  # computed below
    }
    # Expectancy
    wr = win_rate if net_pnls else _safe_float(raw.get("win_rate"), 0.0)
    winner_average = avg_winner if net_pnls else _safe_float(raw.get("avg_winner"), 0.0)
    loser_average = avg_loser if net_pnls else _safe_float(raw.get("avg_loser"), 0.0)
    stats["expectancy"] = round(
        wr * winner_average + (1 - wr) * loser_average, 2
    )
    # PSR / p-value
    stats["psr"] = _defined_stat_value(raw, "psr")
    stats["p_value"] = _defined_stat_value(raw, "p_value")
    return stats


def _detect_indicators(strategy_file: str) -> list[dict]:
    """Detect indicators used by a strategy from source code with basic parameter extraction."""
    path = _strategy_path(strategy_file)
    if path is None or not path.exists():
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []

    import re

    found: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()

    def _add(kind: str, pane: str, period: int | None = None):
        key = (kind, pane, period)
        if key in seen:
            return
        seen.add(key)
        row = {"kind": kind, "pane": pane}
        if period is not None:
            row["period"] = period
        found.append(row)

    # vectorbt style: vbt.MA.run(..., window=20, ewm=True/False)
    for ma_call in re.finditer(r"vbt\.MA\.run\(([^)]*)\)", source, flags=re.IGNORECASE):
        args = ma_call.group(1)
        w = re.search(r"(?:window|length)\s*=\s*(\d+)", args, flags=re.IGNORECASE)
        period = int(w.group(1)) if w else 20
        if re.search(r"ewm\s*=\s*True", args, flags=re.IGNORECASE):
            _add("EMA", "overlay", period)
        else:
            _add("SMA", "overlay", period)

    # ta.* style period extraction
    for m in re.finditer(r"ta\.sma\([^,]+,\s*(\d+)\)", source, flags=re.IGNORECASE):
        _add("SMA", "overlay", int(m.group(1)))
    for m in re.finditer(r"ta\.ema\([^,]+,\s*(\d+)\)", source, flags=re.IGNORECASE):
        _add("EMA", "overlay", int(m.group(1)))
    for m in re.finditer(
        r"(?:vbt\.RSI\.run\([^)]*window\s*=\s*(\d+)|ta\.rsi\([^,]+,\s*(\d+)\))",
        source,
        flags=re.IGNORECASE,
    ):
        p = m.group(1) or m.group(2)
        _add("RSI", "panel", int(p) if p else 14)
    for m in re.finditer(
        r"(?:vbt\.ATR\.run\([^)]*window\s*=\s*(\d+)|ta\.atr\([^)]*length\s*=\s*(\d+)|ta\.atr\([^,]+,\s*[^,]+,\s*[^,]+,\s*(\d+)\))",
        source,
        flags=re.IGNORECASE,
    ):
        p = m.group(1) or m.group(2) or m.group(3)
        _add("ATR", "panel", int(p) if p else 14)
    for m in re.finditer(r"ta\.bbands\([^)]*length\s*=\s*(\d+)", source, flags=re.IGNORECASE):
        _add("BB", "overlay", int(m.group(1)))

    # Presence-based patterns
    if re.search(r"(?:vbt\.MACD\.run|ta\.macd|macd\()", source, flags=re.IGNORECASE):
        _add("MACD", "panel", None)
    if re.search(r"(?:supertrend|super_trend|ta\.supertrend)", source, flags=re.IGNORECASE):
        _add("SuperTrend", "overlay", None)

    # Fallback defaults if generic mentions exist without explicit params
    if re.search(r"\bsma\b", source, flags=re.IGNORECASE):
        _add("SMA", "overlay", 20)
    if re.search(r"\bema\b", source, flags=re.IGNORECASE):
        _add("EMA", "overlay", 12)
    if re.search(r"\brsi\b", source, flags=re.IGNORECASE):
        _add("RSI", "panel", 14)
    if re.search(r"\batr\b", source, flags=re.IGNORECASE):
        _add("ATR", "panel", 14)
    if re.search(r"(?:bbands|bollinger)", source, flags=re.IGNORECASE):
        _add("BB", "overlay", 20)

    return found


def _indicator_payload(
    name: str, pane: str, data: list[dict], color: str, line_style: int = 0
) -> dict:
    return {
        "name": name,
        "pane": pane,  # backward-compatible field
        "type": pane,  # normalized field requested by frontend/API
        "data": data,
        "color": color,
        "lineStyle": line_style,
        "style": {"color": color, "lineStyle": line_style},
    }


_PANDAS_TA_MISSING_WARNED = False


def _compute_indicators(
    df: pd.DataFrame, indicator_list: list[dict], ohlcv_times: list[float]
) -> list[dict]:
    """Compute indicator values from OHLCV DataFrame for given time range."""
    try:
        import pandas_ta as ta
    except ImportError:
        # Every indicator panel - RSI, ATR, Bollinger, MACD - is produced by this one import, and a
        # missing `pandas_ta` therefore removes all of them at once while leaving the rest of the chart
        # intact. That failure is invisible: the panels are simply absent.
        #
        # `pandas_ta` is declared in no dependency file, so a fresh install lands here, and the bare
        # `return []` said nothing about why. Warned once rather than per call, because this runs per
        # chart request.
        global _PANDAS_TA_MISSING_WARNED
        if not _PANDAS_TA_MISSING_WARNED:
            _PANDAS_TA_MISSING_WARNED = True
            logger.warning(
                "pandas_ta is not installed, so no chart indicators can be computed; "
                "RSI, ATR, Bollinger and MACD panels will be absent rather than wrong"
            )
        return []

    results = []
    seen_names: set[str] = set()
    time_set = set(ohlcv_times)

    for ind in indicator_list:
        kind = (ind.get("kind") or ind.get("name") or "").upper()
        period = int(ind.get("period")) if ind.get("period") else None
        try:
            series_payloads = _compute_indicator_series(
                ta, df, kind, period, time_set
            )
            for label, data, pane, color, line_style in series_payloads:
                if data and label not in seen_names:
                    seen_names.add(label)
                    results.append(_indicator_payload(label, pane, data, color, line_style))
        except (ValueError, KeyError) as e:
            logger.warning("Indicator %s computation failed: %s", kind, e)

    return results


def _compute_indicator_series(
    ta: Any,
    df: pd.DataFrame,
    kind: str,
    period: int | None,
    time_set: set[float],
) -> list[tuple[str, list[dict], str, str, int]]:
    """Compute one indicator kind; returns (label, points, pane, color, line_style)."""
    if kind == "SMA":
        p = period or 20
        vals = ta.sma(df["close"], length=p)
        if vals is None:
            return []
        return [(f"SMA({p})", _indicator_to_points(df, vals, time_set), "overlay", "#d29922", 0)]
    if kind == "EMA":
        p = period or 12
        vals = ta.ema(df["close"], length=p)
        if vals is None:
            return []
        return [(f"EMA({p})", _indicator_to_points(df, vals, time_set), "overlay", "#f97316", 0)]
    if kind == "RSI":
        p = period or 14
        vals = ta.rsi(df["close"], length=p)
        if vals is None:
            return []
        return [(f"RSI({p})", _indicator_to_points(df, vals, time_set), "panel", "#a78bfa", 0)]
    if kind == "ATR":
        p = period or 14
        vals = ta.atr(df["high"], df["low"], df["close"], length=p)
        if vals is None:
            return []
        return [(f"ATR({p})", _indicator_to_points(df, vals, time_set), "panel", "#fb923c", 0)]
    if kind == "BB":
        return _compute_bollinger_series(ta, df, period or 20, time_set)
    if kind == "MACD":
        return _compute_macd_series(ta, df, time_set)
    if kind == "SUPERTREND":
        return _compute_supertrend_series(ta, df, time_set)
    return []


def _compute_bollinger_series(
    ta: Any, df: pd.DataFrame, p: int, time_set: set[float]
) -> list[tuple[str, list[dict], str, str, int]]:
    bb = ta.bbands(df["close"], length=p, std=2)
    if bb is None:
        return []
    payloads = []
    for col, label, color in [
        (f"BBU_{p}_2.0", f"BB Upper ({p})", "#6366f1"),
        (f"BBM_{p}_2.0", f"BB Mid ({p})", "#94a3b8"),
        (f"BBL_{p}_2.0", f"BB Lower ({p})", "#6366f1"),
    ]:
        if col in bb.columns:
            data = _indicator_to_points(df, bb[col], time_set)
            payloads.append(
                (label, data, "overlay", color, 2 if "Mid" not in label else 0)
            )
    return payloads


def _compute_macd_series(
    ta: Any, df: pd.DataFrame, time_set: set[float]
) -> list[tuple[str, list[dict], str, str, int]]:
    macd = ta.macd(df["close"])
    if macd is None:
        return []
    payloads = []
    for col, label, color in [
        ("MACD_12_26_9", "MACD", "#3b82f6"),
        ("MACDs_12_26_9", "Signal", "#f97316"),
    ]:
        if col in macd.columns:
            data = _indicator_to_points(df, macd[col], time_set)
            payloads.append((label, data, "panel", color, 0))
    return payloads


def _compute_supertrend_series(
    ta: Any, df: pd.DataFrame, time_set: set[float]
) -> list[tuple[str, list[dict], str, str, int]]:
    st = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
    if st is None:
        return []
    st_col = next((c for c in st.columns if c.startswith("SUPERT_")), None)
    if not st_col:
        return []
    data = _indicator_to_points(df, st[st_col], time_set)
    return [("SuperTrend", data, "overlay", "#22c55e", 0)]


def _indicator_to_points(df: pd.DataFrame, series: pd.Series, time_set: set[float]) -> list[dict]:
    """Convert a pandas Series to time/value points filtered to the OHLCV window.

    The series may carry non-contiguous labels (e.g. a row slice that keeps its
    original index), so values are paired with timestamps positionally: iterate
    the series values in order and index the frame positions with `.iloc`,
    never treating a label as a position.
    """
    points = []
    values = series.reset_index(drop=True)
    for i in range(len(values)):
        val = values.iloc[i]
        if i >= len(df) or not pd.notna(val):
            continue
        ts = df.iloc[i]["timestamp"]
        unix_ts = ts.timestamp() if hasattr(ts, "timestamp") else pd.Timestamp(ts).timestamp()
        if unix_ts in time_set:
            points.append({"time": unix_ts, "value": round(float(val), 6)})
    return points


def _strategy_backtests(entry: StrategyEntry) -> list[dict]:
    backtests: list[dict] = []
    if not BACKTESTS_DIR.exists():
        return backtests

    for bt_file in sorted(BACKTESTS_DIR.glob(TRADES_FILE_GLOB)):
        try:
            bt_data = json.loads(bt_file.read_text(encoding="utf-8"))
            bt_meta = bt_data.get("meta", {})
            bt_strategies = bt_meta.get("strategies", [])
            if not any(matches_strategy_identifier(entry, strategy_name) for strategy_name in bt_strategies):
                continue

            trades = bt_data.get("trades", [])
            total_pnl = sum(t.get("pnl", 0) for t in trades)
            wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            notes = str(bt_meta.get("notes", "") or "").strip()
            raw_strategy = bt_strategies[0] if bt_strategies else entry.slug
            strategy_slug, strategy_display_name, _ = _strategy_labels(raw_strategy)
            backtests.append(
                {
                    "id": bt_file.stem.replace(TRADES_SUFFIX, ""),
                    "title": notes or strategy_display_name,
                    "notes": notes,
                    "strategy_slug": strategy_slug,
                    "engine": bt_meta.get("engine", ""),
                    "symbols": bt_meta.get("data", {}).get("symbols", []),
                    "timeframes": bt_meta.get("data", {}).get("timeframes", []),
                    "backtest_start": bt_meta.get("backtest_start", ""),
                    "backtest_end": bt_meta.get("backtest_end", ""),
                    "trade_count": len(trades),
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
                    "created_at": bt_meta.get("created_at", ""),
                }
            )
        except (KeyError, ValueError, TypeError, OSError):
            pass
    return backtests


def _strategy_portfolios(entry: StrategyEntry) -> list[dict]:
    portfolios: list[dict] = []
    if not PORTFOLIOS_DIR.exists():
        return portfolios

    for pf_file in PORTFOLIOS_DIR.glob(JSON_FILE_GLOB):
        try:
            pf_data = json.loads(pf_file.read_text(encoding="utf-8"))
            strategies = pf_data.get("strategies", [])
            if not any(
                matches_strategy_identifier(entry, str(strategy.get("strategy_name", "")))
                for strategy in strategies
            ):
                continue
            portfolios.append(
                {
                    "id": pf_data.get("id", pf_file.stem),
                    "name": pf_data.get("name", ""),
                }
            )
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return portfolios


def _extract_ast_parameters(file_path: Path) -> dict:
    """Extract default parameters from a strategy file via AST analysis.

    Priority order (first non-empty wins):
      1. Keyword arguments with defaults from ``generate_signals`` signature
         (forward-looking for ``def generate_signals(df, rsi_window=14)`` style).
      2. ``params.get("key", default)`` calls inside ``generate_signals`` body
         (covers all existing strategies).
      3. Top-level ALL_CAPS scalar constant assignments (legacy fallback).
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError):
        return {}

    gen_sig_func = _find_generate_signals_def(tree)

    kwargs_params = _signature_default_params(gen_sig_func) if gen_sig_func else {}
    if kwargs_params:
        return kwargs_params

    get_params = _params_get_defaults(gen_sig_func) if gen_sig_func else {}
    if get_params:
        return get_params

    return _top_level_caps_constants(tree)


def _find_generate_signals_def(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "generate_signals":
            return node
    return None


def _constant_value(node: Any, *, allow_bool: bool = True) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
        if allow_bool or not isinstance(node.value, bool):
            return node.value
    return None


def _signature_default_params(gen_sig_func: ast.FunctionDef) -> dict:
    """Pattern B: keyword defaults from the generate_signals signature."""
    kwargs_params: dict = {}
    args_obj = gen_sig_func.args
    # defaults align to the LAST n positional args
    defaults = args_obj.defaults
    all_args = args_obj.args
    n_defaults = len(defaults)
    for i, default_node in enumerate(defaults):
        arg = all_args[len(all_args) - n_defaults + i]
        name = arg.arg
        if name in ("df", "data", "params", "self", "cls"):
            continue
        val = _constant_value(default_node)
        if val is not None:
            kwargs_params[name] = val
    # also check kwonly args
    for arg, default_node in zip(args_obj.kwonlyargs, args_obj.kw_defaults):
        if default_node is None:
            continue
        val = _constant_value(default_node)
        if val is not None:
            kwargs_params[arg.arg] = val
    return kwargs_params


def _params_get_defaults(gen_sig_func: ast.FunctionDef) -> dict:
    """Pattern A: ``params.get("key", default)`` calls in the function body."""
    get_params: dict = {}
    # Determine the name of the params variable (second positional arg)
    params_var = "params"
    if len(gen_sig_func.args.args) >= 2:
        params_var = gen_sig_func.args.args[1].arg

    for node in ast.walk(gen_sig_func):
        # Match: <params_var>.get("key", default)
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != params_var:
            continue
        if len(node.args) < 2:
            continue
        key_node, default_node = node.args[0], node.args[1]
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        if isinstance(default_node, ast.Constant):
            val = default_node.value
            if isinstance(val, (int, float, str, bool)):
                get_params[key_node.value] = val
        elif isinstance(default_node, ast.UnaryOp) and isinstance(default_node.op, ast.USub):
            # Handle negative numbers like -0.02
            if isinstance(default_node.operand, ast.Constant):
                val = default_node.operand.value
                if isinstance(val, (int, float)):
                    get_params[key_node.value] = -val
    return get_params


def _top_level_caps_constants(tree: ast.Module) -> dict:
    """Pattern C (legacy fallback): ALL_CAPS top-level scalar constants."""
    caps_params: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not name or not name.replace("_", "").isalpha():
                continue
            if name != name.upper():
                continue
            if name in {"STRATEGY_META", "TRUE", "FALSE", "NONE"}:
                continue
            if isinstance(node.value, ast.Constant):
                val = node.value.value
                if isinstance(val, (int, float, str)) and not isinstance(val, bool):
                    caps_params[name] = val
    return caps_params


def _strategy_payload(entry: StrategyEntry) -> dict:
    tags = entry.meta.get("tags", {})
    backtests = _strategy_backtests(entry)
    parameters = entry.meta.get("parameters", {})
    if not isinstance(parameters, dict) or not parameters:
        parameters = _extract_ast_parameters(entry.file_path)
    return {
        "slug": entry.slug,
        "name": entry.display_name,
        "version": entry.meta.get("version", "1.0.0"),
        "file": entry.relative_file,
        "created_at": entry.created_at,
        "tags": tags,
        "type": tags.get("type", []),
        "source": tags.get("source", ""),
        "link": tags.get("link", ""),
        "source_url": entry.meta.get("source_url", ""),
        "notes": entry.meta.get("notes", ""),
        "asset_class": tags.get("asset_class", []),
        "complexity": tags.get("complexity", ""),
        "engine": entry.engine,
        "parameters": parameters if isinstance(parameters, dict) else {},
        "backtest_count": len(backtests),
        "best_sharpe": None,
        "documentation_markdown": read_strategy_documentation(entry),
        "backtests": backtests,
        "portfolios": _strategy_portfolios(entry),
    }


#: Per-repo cap for `dolt pull`. Four repos at 30s each is two minutes of
#: waiting, and this runs per backtest request, so a slow or unreachable remote
#: used to pile up threads that kept the process alive long after the work was
#: done.
_DOLTHUB_PULL_TIMEOUT_SECONDS = 10


def _dolthub_pull():
    """Best-effort dolthub pull. Logs warnings on failure.

    A pull that misses its deadline is killed and logged rather than retried: the
    data is refreshed on the next request anyway, and blocking here only delays
    the request that triggered it.
    """
    dolthub_repos = DATA_DIR / "dolthub"
    if not dolthub_repos.is_dir():
        return
    if shutil.which("dolt") is None:
        logger.info("Skipping dolthub pull: optional 'dolt' binary not installed")
        return
    for repo in ("stocks", "earnings", "options", "rates"):
        repo_path = dolthub_repos / repo
        if not (repo_path / ".dolt").is_dir():
            continue
        try:
            subprocess.run(
                ["dolt", "pull"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=_DOLTHUB_PULL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "dolt pull %s exceeded %ss and was abandoned",
                repo,
                _DOLTHUB_PULL_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("dolt pull %s failed: %s", repo, exc)


# ─── Backtest Run Endpoints ──────────────────────────────────────────────────


def _available_log_files() -> list[Path]:
    if not LOGS_DIR.exists():
        return []
    return sorted(LOGS_DIR.glob("*.log"))


def _normalize_log_levels(raw_levels: str) -> set[str]:
    aliases = {
        "WARN": "WARNING",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "ERR": "ERROR",
        "INFO": "INFO",
        "DEBUG": "DEBUG",
    }
    normalized: set[str] = set()
    for level in raw_levels.split(","):
        candidate = level.strip().upper()
        if not candidate:
            continue
        normalized.add(aliases.get(candidate, candidate))
    return normalized


def _parse_log_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_log_level(line: str, payload: dict | None = None) -> str | None:
    if isinstance(payload, dict):
        for key in ("level", "severity"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized = _normalize_log_levels(value)
                return next(iter(normalized), value.upper())

    upper_line = line.upper()
    for candidate in ("ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if candidate in upper_line:
            normalized = _normalize_log_levels(candidate)
            return next(iter(normalized), candidate)
    return None


def _read_log_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_log_entry_payload(line: str) -> dict | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return redact_value(parsed)


def _build_log_entry(
    safe_line: str,
    payload: dict | None,
    *,
    path: Path,
    line_number: int,
) -> dict:
    return {
        "line": safe_line,
        "timestamp": _parse_log_timestamp(payload.get("timestamp")) if payload else None,
        "level": _extract_log_level(safe_line, payload),
        "path": path.name,
        "line_number": line_number,
    }


def _collect_log_entries(
    paths: list[Path],
    *,
    max_bytes: int = GENERAL_LOG_MAX_BYTES,
    max_lines: int = GENERAL_LOG_MAX_LINES,
    predicate: Callable[[str, dict | None], bool] | None = None,
) -> list[dict]:
    line_limit = max(0, int(max_lines))
    byte_limit = max(0, int(max_bytes))
    if line_limit <= 0 or byte_limit <= 0:
        return []

    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique_paths.append(path)
    if not unique_paths:
        return []

    base_byte_limit, byte_remainder = divmod(byte_limit, len(unique_paths))
    file_lines: list[list[tuple[int, str]]] = []
    for index, path in enumerate(unique_paths):
        per_file_limit = min(
            256 * 1024,
            base_byte_limit + (1 if index < byte_remainder else 0),
        )
        if per_file_limit <= 0:
            file_lines.append([])
            continue
        text = _read_log_tail(path, per_file_limit)
        file_lines.append(list(enumerate(text.splitlines())) if text else [])

    offsets = [len(lines) - 1 for lines in file_lines]
    selected: list[tuple[int, int, dict]] = []
    while len(selected) < line_limit:
        added = False
        for file_index, lines in enumerate(file_lines):
            line_index = offsets[file_index]
            if line_index < 0:
                continue
            line_number, line = lines[line_index]
            offsets[file_index] -= 1
            added = True
            safe_line = redact_text(line)
            payload = _parse_log_entry_payload(line)
            if predicate is not None and not predicate(safe_line, payload):
                continue
            selected.append(
                (
                    file_index,
                    line_number,
                    _build_log_entry(
                        safe_line,
                        payload,
                        path=unique_paths[file_index],
                        line_number=line_number,
                    ),
                )
            )
            if len(selected) >= line_limit:
                break
        if not added:
            break

    selected.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in selected]


def _warning_message_from_entry(entry: dict) -> str | None:
    messages: list[str] = []
    validation = entry.get("validation")
    if isinstance(validation, dict):
        warnings = validation.get("warnings")
        if isinstance(warnings, list):
            messages.extend(str(item).strip() for item in warnings if str(item).strip())

        gaps = validation.get("gaps")
        if isinstance(gaps, list) and gaps:
            first_gap = gaps[0] if isinstance(gaps[0], dict) else None
            if first_gap and first_gap.get("start") and first_gap.get("end"):
                messages.append(
                    f"{len(gaps)} data gaps detected (first: {first_gap['start']} -> {first_gap['end']})"
                )
            else:
                messages.append(f"{len(gaps)} data gaps detected")
        elif (
            isinstance(gaps, int)
            and gaps > 0
            and not any("gap" in message.lower() for message in messages)
        ):
            messages.append(f"{gaps} data gaps detected")

        errors = validation.get("errors")
        if isinstance(errors, list):
            messages.extend(str(item).strip() for item in errors if str(item).strip())

    if entry.get("error"):
        messages.append(str(entry["error"]).strip())

    seen: set[str] = set()
    unique_messages = []
    for message in messages:
        if message and message not in seen:
            seen.add(message)
            unique_messages.append(message)
    return " | ".join(unique_messages) if unique_messages else None


def _categorize_path(path_str: str) -> str:
    """Derive asset category from file path."""
    p = path_str.replace("\\", "/").lower()
    if "/xauusd/" in p:
        return "Metals"
    if "/metals/" in p:
        return "Metals"
    if "/indices/" in p or "/index/" in p:
        return "Indices"
    if "/energy/" in p:
        return "Energy"
    if "/forex/" in p:
        return "Forex"
    if "/equity/" in p or "/stocks/" in p:
        return "Equities"
    if "/etf/" in p:
        return "ETFs"
    if "/crypto/" in p:
        return "Crypto"
    if "/options/" in p:
        return "Options"
    if "/rates/" in p:
        return "Rates"
    if "/earnings/" in p:
        return "Earnings"
    return "Other"


def _scan_ohlcv_filesystem() -> list[dict]:
    """Scan ~/data/ohlcv/ when no catalog cache is available."""
    return scan_ohlcv(DATA_DIR)


def _load_catalog_ohlcv_entries() -> tuple[list[dict], bool]:
    """Load cached OHLCV catalog entries if catalog.json is available."""
    catalog_path = DATA_DIR / CATALOG_FILE_NAME
    if not catalog_path.exists():
        return [], False

    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], False

    entries = catalog.get("ohlcv", [])
    if not isinstance(entries, list):
        return [], True
    return [dict(entry) for entry in entries if isinstance(entry, dict)], True


def _dir_size_bytes(root: Path) -> int:
    """Compute recursive directory size in bytes."""
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            fp = Path(dirpath) / filename
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _refresh_catalog() -> None:
    """Rebuild the data catalog after file mutations."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog(DATA_DIR)
    (DATA_DIR / CATALOG_FILE_NAME).write_text(json.dumps(catalog, indent=2, default=str))


DATA_TASKS: dict[str, dict[str, Any]] = {}
DATA_TASKS_LOCK = threading.Lock()
TASK_STATUS_VALUES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "partial",
        "skipped",
        "manual_required",
        "cleanup_pending",
        "cleanup_exhausted",
    }
)
ACTIVE_TASK_STATUSES = frozenset({"queued", "starting", "running", "paused"})
FINAL_TASK_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "partial",
        "skipped",
        "manual_required",
        "cleanup_pending",
        "cleanup_exhausted",
    }
)


def _normalized_task_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in TASK_STATUS_VALUES else "unknown"


def _task_status_or_manual(value: object) -> str:
    normalized = _normalized_task_status(value)
    return "manual_required" if normalized == "unknown" else normalized


def _prune_terminal_tasks_locked() -> None:
    terminal = [
        (str(task.get("updated_at") or ""), task_id)
        for task_id, task in DATA_TASKS.items()
        if _task_status_or_manual(task.get("status")) in FINAL_TASK_STATUSES
    ]
    terminal.sort()
    excess = len(terminal) - MAX_TERMINAL_TASKS
    if excess > 0:
        for _, task_id in terminal[:excess]:
            DATA_TASKS.pop(task_id, None)


def _download_request_key(identity: dict[str, Any]) -> str:
    payload = {
        "source": str(identity.get("source") or "").strip().lower(),
        "symbols": tuple(
            sorted(
                {
                    str(symbol).strip().upper()
                    for symbol in identity.get("symbols", [])
                }
            )
        ),
        "timeframes": tuple(
            sorted(
                {
                    str(timeframe).strip().lower()
                    for timeframe in identity.get("timeframes", [])
                }
            )
        ),
        "start": identity.get("start") or None,
        "end": identity.get("end") or None,
        "price_type": str(identity.get("price_type") or "").strip().lower(),
        "merge_tick": bool(identity.get("merge_tick")),
        "merge_s1": bool(identity.get("merge_s1")),
        "batch_size": int(identity.get("batch_size") or 0),
        "pause_ms": int(identity.get("pause_ms") or 0),
        "options": identity.get("options") or {},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


_SINGLEFLIGHT_OWNER_CONTEXT = threading.local()
# The singleflight lease fields, defined once.
#
# Two operations read this list and they are *not* symmetric, which is what makes a
# divergence here dangerous rather than merely untidy:
#
# - `_clear_singleflight_reservation` in this module POPS every key, releasing the lease
#   in memory;
# - `viewer/main.py::_task_snapshot_payload` COPIES every key into the persisted payload,
#   so a held lease survives a restart and is not re-acquired by a second worker.
#
# A key present in the persisted set but missing from the cleared set is therefore the
# bad direction: the field is written to disk and never released, so the reservation is
# never freed and the task is stuck as permanently claimed. The reverse direction leaves a
# stale field on disk, which at worst costs a redundant re-claim after a restart.
#
# These were two identical thirteen-entry tuples, one per module, with nothing relating
# them. Same shape as every other duplicate this audit has found: they agree today, and
# agreement is what hides it.
_SINGLEFLIGHT_RUNTIME_KEYS = (
    "download_request_key",
    "owner_pid",
    "owner_thread_id",
    "owner_token",
    "singleflight_owner_boot_id",
    "singleflight_heartbeat_at",
    "singleflight_lease_until",
    "singleflight_lease_expires_at",
)


def _clear_singleflight_reservation(task: dict[str, Any]) -> None:
    for key in _SINGLEFLIGHT_RUNTIME_KEYS:
        task.pop(key, None)


def _new_singleflight_owner_token() -> str:
    return secrets.token_urlsafe(32)


def _current_singleflight_owner_token() -> str | None:
    token = getattr(_SINGLEFLIGHT_OWNER_CONTEXT, "token", None)
    return str(token) if token else None


def _set_singleflight_owner_token(token: str | None) -> None:
    _SINGLEFLIGHT_OWNER_CONTEXT.token = token


def _singleflight_owner_matches(
    task: dict[str, Any], owner_token: str | None = None
) -> bool:
    expected_token = (
        owner_token if owner_token is not None else _current_singleflight_owner_token()
    )
    if not expected_token:
        return False
    try:
        owner_pid = int(task.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    return owner_pid == os.getpid() and str(task.get("owner_token") or "") == expected_token


def _singleflight_lease_expired(task: dict[str, Any], now: float) -> bool:
    if _normalized_task_status(task.get("status")) not in ACTIVE_TASK_STATUSES:
        return False
    lease_until = task.get("singleflight_lease_until")
    uses_wall_clock = lease_until is None
    lease_clock = time.time if uses_wall_clock else time.monotonic
    if uses_wall_clock:
        lease_until = task.get("singleflight_lease_expires_at")
    if lease_until is None:
        return True
    try:
        lease_until_value = float(lease_until)
        current_value = float(lease_clock())
        if not math.isfinite(lease_until_value) or not math.isfinite(current_value):
            return True
        if not uses_wall_clock:
            if not math.isfinite(float(now)):
                return True
            current_value = float(now)
        return current_value >= lease_until_value
    except (TypeError, ValueError):
        return True


def _singleflight_reservation_is_owned_by_current_process(task: dict[str, Any]) -> bool:
    if str(task.get("singleflight_owner_boot_id") or "") != SINGLEFLIGHT_BOOT_ID:
        return False
    try:
        owner_pid = int(task.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    return owner_pid == os.getpid() and bool(task.get("owner_token"))


def _touch_singleflight_lease(
    task: dict[str, Any],
    now: float | None = None,
    owner_token: str | None = None,
) -> bool:
    has_reservation = bool(task.get("download_request_key") or task.get("owner_token"))
    if has_reservation and not _singleflight_owner_matches(task, owner_token):
        return False
    current = time.monotonic() if now is None else now
    last_heartbeat = task.get("singleflight_heartbeat_at")
    if last_heartbeat is not None:
        try:
            heartbeat_interval = min(
                float(SINGLEFLIGHT_HEARTBEAT_SECONDS), float(SINGLEFLIGHT_LEASE_SECONDS)
            )
            if current < float(last_heartbeat) + max(0.0, heartbeat_interval):
                return False
        except (TypeError, ValueError):
            pass
    task["singleflight_owner_boot_id"] = SINGLEFLIGHT_BOOT_ID
    task["singleflight_heartbeat_at"] = current
    task["singleflight_lease_until"] = current + SINGLEFLIGHT_LEASE_SECONDS
    task["singleflight_lease_expires_at"] = time.time() + SINGLEFLIGHT_LEASE_SECONDS
    return True


def _register_download_task(
    record: dict[str, Any],
    request_key: str | None,
    owner_token: str | None = None,
) -> dict[str, Any] | None:
    normalized_task_id = _normalized_task_id(record.get("id"))
    if not normalized_task_id:
        raise ValueError("Invalid task id")
    record["id"] = normalized_task_id
    now = time.monotonic()
    reservation_token = owner_token or record.get("owner_token") or _new_singleflight_owner_token()
    with DATA_TASKS_LOCK:
        if request_key:
            for task in DATA_TASKS.values():
                if task.get("download_request_key") != request_key:
                    continue
                if _normalized_task_status(task.get("status")) in ACTIVE_TASK_STATUSES:
                    if (
                        _singleflight_reservation_is_owned_by_current_process(task)
                        and not _singleflight_lease_expired(task, now)
                    ):
                        return dict(task)
                    task["status"] = "failed"
                    task["error"] = "singleflight reservation lease expired"
                    task["finished_at"] = _task_now_iso()
                    _clear_singleflight_reservation(task)
                else:
                    _clear_singleflight_reservation(task)
            record["download_request_key"] = request_key
        record["owner_pid"] = os.getpid()
        record["owner_thread_id"] = threading.get_ident()
        record["owner_token"] = str(reservation_token)
        record["singleflight_owner_boot_id"] = SINGLEFLIGHT_BOOT_ID
        _touch_singleflight_lease(record, now, owner_token=reservation_token)
        DATA_TASKS[record["id"]] = record
        _prune_terminal_tasks_locked()
        return None


def _start_singleflight_download(
    task_id: str,
    request_key: str,
    starter: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    normalized_task_id = _normalized_task_id(task_id)
    if not normalized_task_id:
        raise ValueError("Invalid task id")
    task_id = normalized_task_id
    owner_token = _new_singleflight_owner_token()
    reservation = {
        "id": task_id,
        "status": "starting",
        "download_request_key": request_key,
        "owner_pid": os.getpid(),
        "owner_thread_id": threading.get_ident(),
        "owner_token": owner_token,
        "singleflight_owner_boot_id": SINGLEFLIGHT_BOOT_ID,
    }
    existing = _register_download_task(
        reservation,
        request_key,
        owner_token=owner_token,
    )
    if existing is not None:
        return {
            "task_id": existing["id"],
            "status": existing.get("status", "starting"),
            "coalesced": True,
        }

    previous_token = _current_singleflight_owner_token()
    _set_singleflight_owner_token(owner_token)
    try:
        return starter(task_id, *args, **kwargs)
    except Exception as exc:
        with DATA_TASKS_LOCK:
            current = DATA_TASKS.get(task_id)
        if current is not None:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        raise
    finally:
        _set_singleflight_owner_token(previous_token)


def _task_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _task_log_path(task_id: str) -> Path:
    normalized = _normalized_task_id(task_id)
    if not normalized:
        raise ValueError("Invalid task id")
    return TASK_LOGS_DIR / f"task_{normalized}.log"


def _read_task_log(log_file: str | Path, *, max_bytes: int | None = None) -> str:
    path = Path(log_file)
    if not path.exists() or path.is_symlink() or not path.is_file():
        return ""

    limit = TASK_LOG_MAX_BYTES if max_bytes is None else max(0, int(max_bytes))
    if limit <= 0:
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return redact_text(handle.read(limit).decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _append_task_log(log_file: Path, message: str) -> None:
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(redact_text(message))


class _ThreadBoundTaskLogHandler(logging.Handler):
    def __init__(self, *, task_id: str, handle, source: str) -> None:
        super().__init__()
        self.task_id = task_id
        self.log_handle = handle
        self.source = source
        self.thread_id = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if threading.get_ident() != self.thread_id:
            return

        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        _write_task_log_line(
            self.log_handle,
            self.task_id,
            message,
            stream="stdout",
            log_level=record.levelname.lower(),
            source=self.source,
        )


def _write_task_log_line(
    handle,
    task_id: str,
    message: str,
    *,
    stream: str,
    log_level: str = "info",
    source: str = "data-task",
    ingest_task_progress: bool = True,
) -> None:
    message = redact_text(message)
    if not message.endswith("\n"):
        message = f"{message}\n"
    handle.write(message)
    handle.flush()

    text = message.rstrip()
    if not text:
        return

    if ingest_task_progress:
        _ingest_task_log_line(task_id, text)

    log_method = getattr(logger, log_level, logger.info)
    log_method(
        text,
        extra={"data": {"task_id": task_id, "stream": stream, "source": source}},
    )


def _write_task_traceback(
    handle,
    task_id: str,
    traceback_text: str,
    *,
    source: str = "data-task",
    log_level: str = "error",
) -> None:
    content = (traceback_text or "").rstrip()
    if not content:
        return
    _write_task_log_line(
        handle,
        task_id,
        f"[{_task_timestamp()}] Full traceback:",
        stream="stderr",
        log_level=log_level,
        source=source,
    )
    for line in content.splitlines():
        _write_task_log_line(
            handle,
            task_id,
            line,
            stream="stderr",
            log_level=log_level,
            source=source,
        )


def _new_task_progress(total_items: int = 0) -> dict[str, Any]:
    return {
        "completed": 0,
        "failed": 0,
        "total": max(total_items, 0),
        "percent": 0.0,
        "current": "",
    }


def _create_task_record(
    task_id: str,
    *,
    name: str,
    description: str,
    kind: str,
    source: str,
    total_items: int = 0,
    log_file: Path,
    control_mode: str,
    supports_control: bool = True,
) -> dict[str, Any]:
    now = _task_now_iso()
    record = {
        "id": task_id,
        "name": name,
        "description": description,
        "kind": kind,
        "source": source,
        "status": "starting",
        "log_file": str(log_file),
        "total_items": total_items,
        "progress": 0.0,
        "progress_detail": _new_task_progress(total_items),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "supports_control": supports_control,
        "control_mode": control_mode,
        "created_at": now,
        "updated_at": now,
    }
    with DATA_TASKS_LOCK:
        _prune_terminal_tasks_locked()
        reservation = DATA_TASKS.get(task_id)
        if reservation is not None and reservation.get("download_request_key"):
            for key in _SINGLEFLIGHT_RUNTIME_KEYS:
                if key in reservation:
                    record[key] = reservation[key]
    return record


def _get_task(task_id: str) -> dict[str, Any] | None:
    task_id = _normalized_task_id(task_id)
    with DATA_TASKS_LOCK:
        return DATA_TASKS.get(task_id)


def _normalized_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9-]{8,64}", value) is None:
        return ""
    return value


def _snapshot_task(task_id: str) -> dict[str, Any] | None:
    task_id = _normalized_task_id(task_id)
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        return dict(task) if task is not None else None


def _update_task_fields(task_id: str, **updates: Any) -> dict[str, Any] | None:
    normalized_task_id = _normalized_task_id(task_id)
    if not normalized_task_id:
        return None
    task_id = normalized_task_id
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return None
        task.update(updates)
        status = _task_status_or_manual(task.get("status"))
        task["status"] = status
        if status in FINAL_TASK_STATUSES:
            _clear_singleflight_reservation(task)
        elif task.get("download_request_key"):
            _touch_singleflight_lease(task)
        task["updated_at"] = _task_now_iso()
        return dict(task)


def _set_task_status(task_id: str, status: str) -> dict[str, Any] | None:
    return _update_task_fields(task_id, status=status)


def _update_task_progress(
    task_id: str,
    *,
    percent: float | None = None,
    current: str | None = None,
    total: int | None = None,
) -> dict[str, Any] | None:
    normalized_task_id = _normalized_task_id(task_id)
    if not normalized_task_id:
        return None
    task_id = normalized_task_id
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return None

        detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
        if total is not None:
            detail["total"] = max(total, 0)
        elif not detail.get("total"):
            detail["total"] = max(int(task.get("total_items", 0) or 0), 0)

        if current is not None:
            detail["current"] = redact_text(str(current))

        next_percent = float(task.get("progress", 0.0) or 0.0)
        if percent is not None:
            next_percent = max(0.0, min(100.0, round(percent, 2)))

        detail["percent"] = next_percent
        task["progress"] = next_percent
        task["progress_detail"] = detail
        if task.get("download_request_key"):
            _touch_singleflight_lease(task)
        task["updated_at"] = _task_now_iso()
        return dict(task)


def _ingest_task_log_line(task_id: str, text: str) -> None:
    normalized_task_id = _normalized_task_id(task_id)
    if not normalized_task_id:
        return
    task_id = normalized_task_id
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return

        detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
        detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)
        detail["current"] = redact_text(text)

        if text.startswith(("✅", "❌")):
            if text.startswith("✅"):
                detail["completed"] = int(detail.get("completed", 0) or 0) + 1
            else:
                detail["failed"] = int(detail.get("failed", 0) or 0) + 1

        total = detail["total"]
        if total > 0 and text.startswith(("✅", "❌")):
            done = min(int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0), total)
            detail["percent"] = round(min(100.0, done / total * 100.0), 2)
            task["progress"] = detail["percent"]
        else:
            detail["percent"] = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)

        task["progress_detail"] = detail
        if task.get("download_request_key"):
            _touch_singleflight_lease(task)
        task["updated_at"] = _task_now_iso()


def _task_progress_payload(task: dict[str, Any], log_content: str = "") -> dict[str, Any]:
    detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
    detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)
    if detail.get("current"):
        detail["current"] = redact_text(str(detail["current"]))

    if not detail.get("current") and log_content:
        lines = [line.strip() for line in log_content.splitlines() if line.strip()]
        if lines:
            detail["current"] = redact_text(lines[-1])

    if detail["total"] > 0 and detail.get("completed", 0) == 0 and detail.get("failed", 0) == 0 and log_content:
        detail["completed"] = log_content.count("✅")
        detail["failed"] = log_content.count("❌")

    percent = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)
    if detail["total"] > 0 and percent == 0.0:
        done = int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0)
        if done > 0:
            percent = min(100.0, done / detail["total"] * 100.0)

    if _normalized_task_status(task.get("status")) == "completed":
        percent = 100.0

    detail["percent"] = round(percent, 2)
    return detail


def _task_controls_payload(task: dict[str, Any]) -> dict[str, bool]:
    status = _task_status_or_manual(task.get("status"))
    supports_control = bool(task.get("supports_control", False))
    return {
        "can_pause": supports_control and status in {"starting", "running"},
        "can_resume": supports_control and status == "paused",
        "can_cancel": supports_control and status not in FINAL_TASK_STATUSES,
    }


def _serialize_task(
    task_id: str,
    task: dict[str, Any],
    *,
    include_log: bool = False,
    max_log_bytes: int = 2048,
) -> dict[str, Any]:
    log_content = (
        _read_task_log(task.get("log_file", ""), max_bytes=max_log_bytes)
        if include_log and task.get("log_file")
        else ""
    )
    payload = {
        "id": task_id,
        "name": task.get("name"),
        "description": task.get("description"),
        "kind": task.get("kind", "data"),
        "source": task.get("source"),
        "status": _task_status_or_manual(task.get("status")),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "progress": _task_progress_payload(task, log_content),
        "log": log_content,
    }
    payload.update(_task_controls_payload(task))
    safe_payload = redact_value(payload)
    return safe_payload if isinstance(safe_payload, dict) else {}


def _mark_task_finished(
    task_id: str,
    *,
    status: str,
    returncode: int | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    updates: dict[str, Any] = {"status": status, "finished_at": _task_now_iso()}
    if returncode is not None:
        updates["returncode"] = returncode
    if error is not None:
        updates["error"] = redact_text(str(error))
    task = _update_task_fields(task_id, **updates)
    if status == "completed":
        _update_task_progress(task_id, percent=100.0)
        task = _snapshot_task(task_id)
    return task


def _wait_for_task_resume(task_id: str) -> bool:
    task = _get_task(task_id)
    if task is None:
        return False

    pause_event = task.get("pause_event")
    stop_event = task.get("stop_event")
    if pause_event is None or stop_event is None:
        return True

    was_paused = False
    while pause_event.is_set():
        was_paused = True
        _set_task_status(task_id, "paused")
        if stop_event.wait(0.25):
            return False

    if was_paused:
        _set_task_status(task_id, "running")
    return not stop_event.is_set()


def _task_log_message(
    task_id: str,
    message: str,
    *,
    stream: str = "task",
    log_level: str = "info",
    source: str = "data-task",
) -> None:
    task = _get_task(task_id)
    log_file = Path(task.get("log_file", _task_log_path(task_id)) if task else _task_log_path(task_id))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8", buffering=1) as handle:
        _write_task_log_line(
            handle,
            task_id,
            f"[{_task_timestamp()}] {message}",
            stream=stream,
            log_level=log_level,
            source=source,
        )


def _refresh_catalog_for_task(
    task_id: str,
    *,
    source: str = "data-task",
    cache_invalidation_reason: str | None = None,
) -> bool:
    _task_log_message(task_id, "Refreshing catalog...", source=source)
    try:
        if cache_invalidation_reason:
            removed = cache_manager.invalidate_cache(DATA_DIR, reason=cache_invalidation_reason)
            _task_log_message(
                task_id,
                f"Cache invalidated ({cache_invalidation_reason}): {removed} entries removed.",
                source=source,
            )
        _refresh_catalog()
    except (OSError, ValueError, TypeError) as exc:
        _task_log_message(task_id, f"Catalog refresh failed: {exc}", log_level="error", source=source)
        _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        return False
    return True


def _start_data_task(
    task_id: str,
    cmd: list[str],
    *,
    total_items: int = 0,
    name: str = "Background task",
    description: str = "",
    kind: str = "data",
    source: str = "system",
) -> dict:
    """Launch a background data-management subprocess with streamed task logs."""
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind=kind,
        source=source,
        total_items=total_items,
        log_file=log_file,
        control_mode="process",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    owner_token = (
        str(record.get("owner_token") or _current_singleflight_owner_token() or "") or None
    )

    def run_proc() -> None:
        previous_token = _current_singleflight_owner_token()
        _set_singleflight_owner_token(owner_token)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        command_display = redact_text(" ".join(str(part) for part in cmd))
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                        source="data-task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                if pause_event.is_set() and not _wait_for_task_resume(task_id):
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled while paused.",
                        stream="task",
                        source="data-task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=os.getcwd(),
                    env=env,
                    text=True,
                    bufsize=1,
                )
                _update_task_fields(task_id, status="running", pid=proc.pid, process=proc)

                if proc.stdout is not None:
                    for line in proc.stdout:
                        _write_task_log_line(handle, task_id, line.rstrip("\n"), stream="stdout")

                returncode = proc.wait()
                if stop_event.is_set():
                    _mark_task_finished(task_id, status="cancelled", returncode=returncode)
                    return

                status = "completed" if returncode == 0 else "failed"
                if returncode == 0:
                    cache_reason = _task_cache_invalidation_reason(source)
                    if _refresh_catalog_for_task(
                        task_id,
                        cache_invalidation_reason=cache_reason,
                    ):
                        _mark_task_finished(task_id, status="completed", returncode=returncode)
                        return

                _mark_task_finished(task_id, status=status, returncode=returncode)
        except (subprocess.SubprocessError, OSError) as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            _append_task_log(
                log_file,
                f"[{_task_timestamp()}] Task failed: {exc}\n",
            )
            logger.exception(
                "Task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )
        finally:
            _update_task_fields(task_id, process=None)
            _set_singleflight_owner_token(previous_token)

    thread = threading.Thread(target=run_proc, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _start_dukascopy_task(
    task_id: str,
    cmd: list[str],
    *,
    symbols: list[str],
    timeframes: list[str],
    start: str | None,
    end: str | None,
    price_type: str,
    merge_tick: bool,
    merge_s1: bool,
    batch_size: int,
    pause_ms: int,
    total_items: int,
    name: str,
    description: str,
) -> dict[str, Any]:
    from data_tools.downloaders.dukascopy_native import DownloadCancelled, DukascopyNativeDownloader

    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind="data",
        source="dukascopy",
        total_items=total_items,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    owner_token = (
        str(record.get("owner_token") or _current_singleflight_owner_token() or "") or None
    )

    def _run_download() -> None:
        command_display = redact_text(" ".join(str(part) for part in cmd))
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]
        downloader = DukascopyNativeDownloader()
        refresh_needed = False

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                completed_items = 0
                failed_items = 0
                first_error: str | None = None
                total = max(total_items, 1)

                def task_log(message: str, *, level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="stdout",
                        log_level=level,
                        source="data-task",
                    )

                combinations = [(symbol, timeframe) for symbol in symbols for timeframe in timeframes]
                for symbol, timeframe in combinations:
                    if not _wait_for_task_resume(task_id):
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled while paused.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    if stop_event.is_set():
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    _set_task_status(task_id, "running")
                    item_label = f"{symbol} {timeframe}"
                    base_completed = completed_items + failed_items

                    def report_progress(pct: float, message: str) -> None:
                        overall = ((base_completed + (max(0.0, min(pct, 100.0)) / 100.0)) / total) * 100.0
                        _update_task_progress(
                            task_id,
                            percent=overall,
                            current=f"{item_label}: {message}",
                            total=total,
                        )

                    try:
                        result = downloader.download(
                            symbol,
                            timeframe,
                            DATA_DIR,
                            start=start,
                            end=end,
                            price_type=price_type,
                            merge_tick=merge_tick,
                            merge_s1=merge_s1,
                            batch_size=batch_size,
                            pause_ms=pause_ms,
                            pause_event=pause_event,
                            stop_event=stop_event,
                            report_progress=report_progress,
                            log_callback=task_log,
                        )
                        refresh_needed = True
                        completed_items += 1
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"✅ {result.symbol} {result.timeframe}: {result.rows} rows ({result.start} → {result.end}) → {result.path}",
                            stream="stdout",
                            source="data-task",
                        )
                    except DownloadCancelled:
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return
                    except (OSError, RuntimeError, ValueError, KeyError) as exc:
                        failed_items += 1
                        if first_error is None:
                            first_error = f"{symbol} {timeframe}: {exc}"
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"❌ {symbol} {timeframe}: {exc}",
                            stream="stdout",
                            log_level="error",
                            source="data-task",
                        )
                        _write_task_traceback(
                            handle,
                            task_id,
                            traceback.format_exc(),
                            source="data-task",
                        )
                        logger.exception(
                            "Dukascopy download failed for %s %s: %s",
                            symbol,
                            timeframe,
                            exc,
                            extra={
                                "data": {
                                    "task_id": task_id,
                                    "symbol": symbol,
                                    "timeframe": timeframe,
                                    "source": "data-task",
                                    "downloader": "dukascopy-native",
                                }
                            },
                        )

                if refresh_needed and not _refresh_catalog_for_task(
                    task_id,
                    cache_invalidation_reason="post_download",
                ):
                    return

                final_status = "failed" if failed_items > 0 else "completed"
                returncode = 1 if failed_items > 0 else 0
                _write_task_log_line(
                    handle,
                    task_id,
                    f"Done: {completed_items} downloads completed.",
                    stream="stdout",
                    source="data-task",
                )
                _mark_task_finished(
                    task_id,
                    status=final_status,
                    returncode=returncode,
                    error=first_error if failed_items > 0 else None,
                )
        except Exception as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Task failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="data-task",
                )
                _write_task_traceback(
                    handle,
                    task_id,
                    traceback.format_exc(),
                    source="data-task",
                )
            logger.exception(
                "Dukascopy task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )

    def run_download() -> None:
        previous_token = _current_singleflight_owner_token()
        _set_singleflight_owner_token(owner_token)
        try:
            _run_download()
        finally:
            _set_singleflight_owner_token(previous_token)

    thread = threading.Thread(target=run_download, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _ensure_fallback_downloader(
    fallback_downloader: Any,
) -> Any:
    """Create the native fallback downloader lazily."""
    if fallback_downloader is None:
        from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader

        return DukascopyNativeDownloader()
    return fallback_downloader


@dataclass
class _JForexRequest:
    """Parameters for one JForex/native download attempt."""

    symbol: str
    timeframe: str
    start: str | None
    end: str | None
    offer_side: str
    query_timeout_seconds: int
    merge_tick: bool
    merge_s1: bool
    batch_size: int
    pause_ms: int
    pause_event: Any
    stop_event: Any
    report_progress: Callable[[float, str], None]


@dataclass
class _JForexAttempt:
    """Outcome of one JForex (or native-fallback) download attempt."""

    download: Any = None
    error: str | None = None
    cancelled: bool = False
    used_fallback: bool = False

    @property
    def failed(self) -> bool:
        return self.error is not None


def _attempt_jforex_download(
    *,
    jforex_downloader: Any,
    get_fallback_downloader: Callable[[], Any],
    request: _JForexRequest,
    task_log: Callable[..., None],
    fallback_enabled: bool,
) -> _JForexAttempt:
    """Run one JForex download with optional native fallback; never raises."""
    from data_tools.downloaders.dukascopy_native import DownloadCancelled

    try:
        download = jforex_downloader.download(
            request.symbol,
            request.timeframe,
            DATA_DIR,
            start=request.start,
            end=request.end,
            offer_side=request.offer_side,
            query_timeout_seconds=request.query_timeout_seconds,
        )
        return _JForexAttempt(download=download)
    except DownloadCancelled:
        return _JForexAttempt(cancelled=True)
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        fallback_reason = _jforex_fallback_reason(exc) if fallback_enabled else None
        if fallback_reason is None:
            return _JForexAttempt(error=f"{request.symbol} {request.timeframe}: {exc}")
        return _attempt_native_fallback(
            get_fallback_downloader=get_fallback_downloader,
            request=request,
            task_log=task_log,
            jforex_error=exc,
            fallback_reason=fallback_reason,
        )


def _attempt_native_fallback(
    *,
    get_fallback_downloader: Callable[[], Any],
    request: _JForexRequest,
    task_log: Callable[..., None],
    jforex_error: Exception,
    fallback_reason: str,
) -> _JForexAttempt:
    """Run the Dukascopy-native fallback download for a failed JForex request."""
    from data_tools.downloaders.dukascopy_native import DownloadCancelled

    symbol = request.symbol
    timeframe = request.timeframe
    fallback_downloader = get_fallback_downloader()
    task_log(
        (
            f"ℹ️ {symbol} {timeframe}: JForex not available (reason: {fallback_reason}), "
            f"using HTTP downloader. Original error: {jforex_error}"
        ),
        level="info",
    )
    logger.warning(
        "JForex download failed for %s %s; attempting native fallback: %s",
        symbol,
        timeframe,
        jforex_error,
        extra={
            "data": {
                "symbol": symbol,
                "timeframe": timeframe,
                "source": "data-task",
                "downloader": "jforex",
                "fallback": "dukascopy-native",
            }
        },
    )
    try:
        download = fallback_downloader.download(
            symbol,
            timeframe,
            DATA_DIR,
            start=request.start,
            end=request.end,
            offer_side=request.offer_side,
            merge_tick=request.merge_tick,
            merge_s1=request.merge_s1,
            batch_size=request.batch_size,
            pause_ms=request.pause_ms,
            pause_event=request.pause_event,
            stop_event=request.stop_event,
            report_progress=request.report_progress,
            log_callback=task_log,
        )
    except DownloadCancelled:
        return _JForexAttempt(cancelled=True)
    except (OSError, RuntimeError, ValueError, KeyError) as fallback_exc:
        task_log(
            (
                f"❌ {symbol} {timeframe}: JForex failed ({jforex_error}); "
                f"native fallback also failed ({fallback_exc})"
            ),
            level="error",
        )
        logger.exception(
            "Native fallback failed for %s %s after JForex failure",
            symbol,
            timeframe,
            extra={
                "data": {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "source": "data-task",
                    "downloader": "dukascopy-native",
                }
            },
        )
        return _JForexAttempt(
            error=(
                f"{symbol} {timeframe}: JForex failed ({jforex_error}); "
                f"native fallback failed ({fallback_exc})"
            )
        )
    return _JForexAttempt(download=download, used_fallback=True)


def _start_jforex_task(
    task_id: str,
    cmd: list[str],
    *,
    source_label: str,
    allow_native_fallback: bool | None = None,
    symbols: list[str],
    timeframes: list[str],
    start: str | None,
    end: str | None,
    offer_side: str,
    merge_tick: bool,
    merge_s1: bool,
    batch_size: int,
    pause_ms: int,
    query_timeout_seconds: int,
    total_items: int,
    name: str,
    description: str,
) -> dict[str, Any]:
    from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader
    from data_tools.downloaders.jforex_dl import JForexDownloader

    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind="data",
        source=source_label,
        total_items=total_items,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    owner_token = (
        str(record.get("owner_token") or _current_singleflight_owner_token() or "") or None
    )

    def _run_download() -> None:
        command_display = redact_text(" ".join(str(part) for part in cmd))
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]
        jforex_downloader = JForexDownloader()
        fallback_downloader: DukascopyNativeDownloader | None = None
        fallback_enabled = (
            bool(allow_native_fallback)
            if allow_native_fallback is not None
            else str(source_label or "").strip().lower() == "dukascopy"
        )
        refresh_needed = False

        def _get_fallback_downloader() -> DukascopyNativeDownloader:
            nonlocal fallback_downloader
            if fallback_downloader is None:
                fallback_downloader = DukascopyNativeDownloader()
            return fallback_downloader

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                completed_items = 0
                failed_items = 0
                first_error: str | None = None
                total = max(total_items, 1)

                def task_log(message: str, *, level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="stdout",
                        log_level=level,
                        source="data-task",
                    )

                combinations = [(symbol, timeframe) for symbol in symbols for timeframe in timeframes]
                for symbol, timeframe in combinations:
                    if not _wait_for_task_resume(task_id):
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled while paused.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    if stop_event.is_set():
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    _set_task_status(task_id, "running")
                    item_label = f"{symbol} {timeframe}"
                    base_completed = completed_items + failed_items

                    def report_progress(pct: float, message: str) -> None:
                        overall = ((base_completed + (max(0.0, min(pct, 100.0)) / 100.0)) / total) * 100.0
                        _update_task_progress(
                            task_id,
                            percent=overall,
                            current=f"{item_label}: {message}",
                            total=total,
                        )

                    used_native_backend = False

                    outcome = _attempt_jforex_download(
                        jforex_downloader=jforex_downloader,
                        get_fallback_downloader=_get_fallback_downloader,
                        request=_JForexRequest(
                            symbol=symbol,
                            timeframe=timeframe,
                            start=start,
                            end=end,
                            offer_side=offer_side,
                            query_timeout_seconds=query_timeout_seconds,
                            merge_tick=merge_tick,
                            merge_s1=merge_s1,
                            batch_size=batch_size,
                            pause_ms=pause_ms,
                            pause_event=pause_event,
                            stop_event=stop_event,
                            report_progress=report_progress,
                        ),
                        task_log=task_log,
                        fallback_enabled=fallback_enabled,
                    )
                    if outcome.cancelled:
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return
                    if outcome.failed:
                        failed_items += 1
                        if first_error is None:
                            first_error = outcome.error
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"❌ {outcome.error}",
                            stream="stdout",
                            log_level="error",
                            source="data-task",
                        )
                        continue
                    if outcome.used_fallback:
                        used_native_backend = True
                    result = outcome.download

                    refresh_needed = True
                    completed_items += 1
                    backend_suffix = (
                        " (backend:native-http)"
                        if used_native_backend
                        else " (backend:jforex)"
                    )
                    _write_task_log_line(
                        handle,
                        task_id,
                        (
                            f"✅ {result.symbol} {result.timeframe}: {result.rows} rows "
                            f"({result.start} → {result.end}) → {result.path}{backend_suffix}"
                        ),
                        stream="stdout",
                        source="data-task",
                    )

                if refresh_needed and not _refresh_catalog_for_task(
                    task_id,
                    cache_invalidation_reason="post_download",
                ):
                    return

                final_status = "failed" if failed_items > 0 else "completed"
                returncode = 1 if failed_items > 0 else 0
                _write_task_log_line(
                    handle,
                    task_id,
                    f"Done: {completed_items} downloads completed.",
                    stream="stdout",
                    source="data-task",
                )
                _mark_task_finished(
                    task_id,
                    status=final_status,
                    returncode=returncode,
                    error=first_error if failed_items > 0 else None,
                )
        except Exception as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Task failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="data-task",
                )
                _write_task_traceback(
                    handle,
                    task_id,
                    traceback.format_exc(),
                    source="data-task",
                )
            logger.exception(
                "JForex task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )

    def run_download() -> None:
        previous_token = _current_singleflight_owner_token()
        _set_singleflight_owner_token(owner_token)
        try:
            _run_download()
        finally:
            _set_singleflight_owner_token(previous_token)

    thread = threading.Thread(target=run_download, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _queue_agent_task(task_id: str, payload: dict) -> dict:
    """Register the autonomous conversion agent before the worker thread launches."""
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    provider = str(payload.get("provider") or DEFAULT_LLM_PROVIDER_NAME).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    model = str(payload.get("model") or "").strip() or None
    record = _create_task_record(
        task_id,
        name="Strategy conversion",
        description=(
            f"{payload.get('strategy_name') or 'Convert strategy into the repository format'} "
            f"(provider={provider}{f', model={model}' if model else ''})"
        ),
        kind="agent_conversion",
        source="agent",
        total_items=0,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    record["provider"] = provider
    record["model"] = model
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    return {"job_id": task_id, "task_id": task_id, "status": record["status"]}


def _launch_agent_task(task_id: str, payload: dict) -> None:
    task = _get_task(task_id)
    log_file = Path(task.get("log_file", _task_log_path(task_id)) if task else _task_log_path(task_id))
    provider = str(payload.get("provider") or DEFAULT_LLM_PROVIDER_NAME).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    model = str(payload.get("model") or "").strip() or None

    def run_agent() -> None:
        started_at = datetime.now(timezone.utc)
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            # Thread-bound log capture: route root-logger records from this
            # thread into the task log instead of swapping the process-global
            # sys.stdout/sys.stderr (which would swallow concurrent request
            # logging from other threads into this task's file).
            log_handler = _ThreadBoundTaskLogHandler(
                task_id=task_id,
                handle=handle,
                source="agent-task",
            )
            root_logger = logging.getLogger()
            root_logger.addHandler(log_handler)
            try:
                # `finally` below guarantees removal: a BaseException escaping
                # the handler body must not leave this task's handler attached
                # to the process-wide root logger.
                _write_task_log_line(
                    handle,
                    task_id,
                    (
                        f"[{_task_timestamp()}] Starting strategy conversion with "
                        f"provider={provider} model={model or 'default'}."
                    ),
                    stream="task",
                    source="agent-task",
                )
                _set_task_status(task_id, "running")
                _update_task_fields(task_id, provider=provider, model=model, started_at=started_at.isoformat())

                def task_log(message: str, *, stream: str = "task", level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream=stream,
                        log_level=level,
                        source="agent-task",
                    )

                result = run_auto_agent(
                    payload["code"],
                    strategy_name=payload.get("strategy_name"),
                    requested_engine=payload.get("engine"),
                    provider=provider,
                    model=model,
                    symbols=payload.get("symbols"),
                    timeframe=payload.get("timeframe"),
                    start=payload.get("start"),
                    end=payload.get("end"),
                    capital=_safe_float(payload.get("capital"), 100000.0),
                    log=task_log,
                )
                duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                _update_task_fields(
                    task_id,
                    result=result,
                    provider=provider,
                    model=model,
                    duration_seconds=round(duration_seconds, 3),
                )
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Strategy conversion completed successfully.",
                    stream="task",
                    source="agent-task",
                )
                _mark_task_finished(task_id, status="completed", returncode=0)
            except Exception as exc:
                duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                traceback_text = traceback.format_exc()
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] ❌ Strategy conversion failed: {exc}",
                    stream="task",
                    log_level="error",
                    source="agent-task",
                )
                _write_task_traceback(handle, task_id, traceback_text, source="agent-task")
                _update_task_fields(
                    task_id,
                    provider=provider,
                    model=model,
                    duration_seconds=round(duration_seconds, 3),
                )
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
                logger.exception(
                    "Strategy conversion failed: %s",
                    exc,
                    extra={"data": {"task_id": task_id, "stream": "task", "source": "agent-task"}},
                )
            finally:
                root_logger.removeHandler(log_handler)

    thread = threading.Thread(target=run_agent, daemon=True, name=f"agent-convert-{task_id[:8]}")
    _update_task_fields(task_id, thread=thread)
    try:
        thread.start()
    except Exception as exc:
        traceback_text = traceback.format_exc()
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            _write_task_log_line(
                handle,
                task_id,
                f"[{_task_timestamp()}] ❌ Failed to launch strategy conversion worker: {exc}",
                stream="task",
                log_level="error",
                source="agent-task",
            )
            _write_task_traceback(handle, task_id, traceback_text, source="agent-task")
        _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        logger.exception(
            "Failed to launch strategy conversion worker: %s",
            exc,
            extra={"data": {"task_id": task_id, "stream": "task", "source": "agent-task"}},
        )


def _dolthub_repo_status(repo_path: Path) -> tuple[dict, str]:
    if repo_path.exists():
        return {"status": "ok"}, ""
    return {"status": "warning"}, "Repository directory is missing"


def _scan_dolthub_repos() -> list[dict]:
    """Scan ~/data/dolthub repos and return size metadata."""
    repos_root = DATA_DIR / "dolthub"
    if not repos_root.exists():
        return []

    repos: list[dict] = []
    for repo_name in ("stocks", "earnings", "options", "rates"):
        repo_path = repos_root / repo_name
        if not repo_path.exists():
            continue
        size = _dir_size_bytes(repo_path)
        try:
            updated_at = datetime.fromtimestamp(
                repo_path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except (OSError, ValueError):
            updated_at = None
        repos.append(
            {
                "repo": repo_name,
                "symbol": repo_name,
                "timeframe": "repo",
                "path": str(repo_path),
                "source": "dolthub",
                "size_bytes": size,
                "last_updated": updated_at,
                "is_repo": True,
            }
        )
    return repos


def _task_symbol_summary(symbols: list[str]) -> str:
    if not symbols:
        return "all symbols"
    if len(symbols) == 1:
        return symbols[0]
    if len(symbols) <= 3:
        return ", ".join(symbols)
    return f"{symbols[0]} +{len(symbols) - 1}"


def _task_timeframe_summary(timeframes: list[str]) -> str:
    if not timeframes:
        return "default timeframe"
    if len(timeframes) == 1:
        return timeframes[0]
    return ", ".join(timeframes[:3]) + ("…" if len(timeframes) > 3 else "")


def _download_task_labels(source: str, symbols: list[str], timeframes: list[str]) -> tuple[str, str]:
    symbol_label = _task_symbol_summary(symbols)
    timeframe_label = _task_timeframe_summary(timeframes)
    if source == "dukascopy":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"Dukascopy download for {symbol_label} on {timeframe_label}.",
        )
    if source == "jforex":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"JForex download for {symbol_label} on {timeframe_label}.",
        )
    if source == "yfinance":
        return (
            f"Download {symbol_label}",
            f"Yahoo Finance download for {symbol_label}.",
        )
    if source == "huggingface":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"HuggingFace download for {symbol_label} on {timeframe_label}.",
        )
    if source == "alpaca":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"Alpaca download for {symbol_label} on {timeframe_label}.",
        )
    return ("Sync Dolthub repos", "Refresh Dolthub repositories and rebuild the data catalog.")


def _task_cache_invalidation_reason(source: str) -> str | None:
    source_key = str(source or "").strip().lower()
    if source_key == "resample":
        return "post_resample"
    if source_key in {
        "dukascopy",
        "jforex",
        "yfinance",
        "huggingface",
        "alpaca",
        "dolthub",
        "harvest",
        "download",
    }:
        return "post_download"
    return None


def _jforex_fallback_reason(exc: Exception) -> str | None:
    if isinstance(exc, ModuleNotFoundError):
        missing_name = str(getattr(exc, "name", "") or "").lower()
        if "jpype" in missing_name or "dukascopy" in missing_name:
            return f"missing python module: {missing_name or 'unknown'}"

    message = str(exc).lower()
    availability_reason_by_fragment = {
        "jpype is required": "JPype dependency missing",
        "no module named 'jpype'": "JPype dependency missing",
        "failed to import 'com.dukascopy'": JFOREX_SDK_UNAVAILABLE_ERROR,
        "classnotfoundexception": JFOREX_SDK_UNAVAILABLE_ERROR,
        "com.dukascopy": JFOREX_SDK_UNAVAILABLE_ERROR,
        "failed to start jvm": "Java/JVM startup failed",
        "could not resolve jforex sdk classpath": "JForex SDK classpath unavailable",
        "jforex sdk classpath": "JForex SDK classpath unavailable",
        "jvm shared library": JAVA_RUNTIME_UNAVAILABLE_ERROR,
        "jvm dll not found": JAVA_RUNTIME_UNAVAILABLE_ERROR,
        "no jvm shared library file": JAVA_RUNTIME_UNAVAILABLE_ERROR,
        "nullpointerexception": "JForex runtime classloader failure",
        "jforex sdk request execution failed": "JForex request execution failed",
        "jforex connection timed out": "JForex connection timed out",
        "jforex data request timed out": "JForex data request timed out",
        "jforex history query failed": "JForex historical query failed",
        "interval from [": "JForex historical interval invalid",
        "not valid for period": "JForex historical interval invalid",
        "dukascopy credentials": "Dukascopy credentials missing or invalid",
    }
    for fragment, reason in availability_reason_by_fragment.items():
        if fragment in message:
            return reason

    return None


def _signal_process(task: dict[str, Any], sig: int) -> None:
    process = task.get("process")
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        os.kill(process.pid, sig)
    except ProcessLookupError:
        return

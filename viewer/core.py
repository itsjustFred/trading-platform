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
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from data_tools import cache_manager
from data_tools.catalog_builder import build_catalog, scan_ohlcv
from runner.auto_agent import DEFAULT_MODEL_NAME, SUPPORTED_MODEL_NAMES, run_auto_agent
from runner.llm_providers import DEFAULT_LLM_PROVIDER_NAME, SUPPORTED_LLM_PROVIDER_NAMES
from runner.optimizer import OptimizationConfig, run_optimization as execute_optimization
from runner.stats import compute_stats, compute_stats_for_result
from shared.models import BacktestResult, EquityPoint, PortfolioStats, Trade
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    StrategyEntry,
    iter_strategy_files,
    load_strategy_meta,
    matches_strategy_identifier,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
    read_strategy_documentation,
    update_strategy_display_name,
)
from viewer.comparison import compare_backtests, compute_drawdown_pct

from shared.constants import DATA_DIR, LOG_DIR, OPTIMIZATIONS_DIR
from shared.logging import get_logger

logger = get_logger("viewer")

BACKTESTS_DIR = DATA_DIR / "backtests"
REACT_DIST = Path(__file__).parent.parent / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).parent / "static"

STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOGS_DIR / "tasks"
ALL_LOGS_NAME = "ALL_LOGS"
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
    """
    now = time.monotonic()
    with _JOBS_LOCK:
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
    """
    now = time.monotonic()
    with _JOBS_LOCK:
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
    """
    now = time.monotonic()
    with _JOBS_LOCK:
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
_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_OHLCV_DF_CACHE_LOCK = threading.RLock()
_OHLCV_DF_CACHE: dict[
    tuple[str, str],
    tuple[tuple[tuple[str, int, int], ...], pd.DataFrame],
] = {}


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
    return {
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
        "initial_capital": meta.initial_capital,
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

    first_trade_time = _iso_to_unix(ordered_trades[0].entry_time or ordered_trades[0].last_exit_time)
    points: list[dict] = []
    if first_trade_time > 0:
        points.append({"time": first_trade_time, "equity": round(initial_capital, 2)})

    running_equity = initial_capital
    for trade in ordered_trades:
        event_time = _iso_to_unix(trade.last_exit_time or trade.entry_time)
        if event_time <= 0:
            continue
        running_equity += _safe_float(trade.pnl, 0.0)
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

    risk_amount = trade.risk_amount
    if risk_amount is None:
        risk_amount = risk_math.get("risk_target_usd")

    risk_percent = trade.risk_percent
    if risk_percent is None:
        direct_percent = risk_math.get("risk_target_pct")
        if direct_percent is not None:
            risk_percent = direct_percent
        else:
            equity_at_entry = _safe_float(risk_math.get("equity_at_entry"), 0.0)
            target_risk_usd = _safe_float(risk_math.get("risk_target_usd"), 0.0)
            if equity_at_entry > 0 and target_risk_usd > 0:
                risk_percent = target_risk_usd / equity_at_entry

    exit_reason = trade.exit_reason or (last_exit.reason if last_exit else None)
    if exit_reason is None and isinstance(trade_meta.get("exit_reason"), str):
        exit_reason = str(trade_meta.get("exit_reason"))

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
        "pnl": trade.pnl,
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


def _load_ohlcv_df(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Find and load OHLCV parquet for a symbol+timeframe."""
    ohlcv_root = DATA_DIR / "ohlcv"
    normalized_symbol = str(symbol or "").strip()
    normalized_timeframe = str(timeframe or "").strip()
    cache_key = (normalized_symbol.upper(), normalized_timeframe.lower())
    matches = sorted(ohlcv_root.rglob(f"**/{normalized_symbol}/{normalized_timeframe}.parquet"))
    if not matches:
        with _OHLCV_DF_CACHE_LOCK:
            _OHLCV_DF_CACHE.pop(cache_key, None)
        return None
    signature_parts: list[tuple[str, int, int]] = []
    for path in matches:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature_parts.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    signature: tuple[tuple[str, int, int], ...] = tuple(signature_parts)
    with _OHLCV_DF_CACHE_LOCK:
        cached_entry = _OHLCV_DF_CACHE.get(cache_key)
        if cached_entry is not None:
            cached_signature, cached_df = cached_entry
            if cached_signature == signature:
                return cached_df.copy(deep=False)

    frames: list[pd.DataFrame] = []
    for path in matches:
        try:
            try:
                df = pd.read_parquet(path, columns=_OHLCV_COLUMNS)
            except (ValueError, KeyError):
                df = pd.read_parquet(path)
        except (ValueError, KeyError, pd.errors.EmptyDataError, FileNotFoundError, OSError) as exc:
            logger.warning("Could not load OHLCV segment for %s/%s from %s: %s", symbol, timeframe, path, exc)
            continue
        df.columns = [str(column).lower() for column in df.columns]
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.dropna(subset=["timestamp"])
        if not df.empty:
            frames.append(df)

    if not frames:
        return None
    if len(frames) == 1:
        merged = frames[0].copy()
    else:
        merged = pd.concat(frames, ignore_index=True, sort=False)

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
        _OHLCV_DF_CACHE[cache_key] = (signature, merged)
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
        return {
            "sharpe": 0,
            "cagr": 0,
            "max_drawdown_pct": 0,
            "net_profit": 0,
            "net_profit_pct": 0,
            "p_value": 1.0,
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
            pnl = _safe_float(trade.get("pnl", 0))
            running += pnl
            equity.append(round(running, 2))
        total_pnl = running - initial_capital

    net_profit_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0

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

    cagr = round(_safe_float(derived_stats.get("cagr")) * 100, 2)
    sharpe = round(_safe_float(derived_stats.get("sharpe_ratio")), 3)

    psr_val = derived_stats.get("psr")
    p_value = round(_safe_float(derived_stats.get("p_value"), 1.0), 6)

    n = len(equity)
    equity_mini = _downsample_numeric_minmax([float(v) for v in equity if isinstance(v, (int, float))], limit=50)

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_drawdown_pct": max_drawdown_pct,
        "net_profit": round(total_pnl, 2),
        "net_profit_pct": net_profit_pct,
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

    pnls = [
        _safe_float(trade.pnl, 0.0)
        for trade in (result.trades or [])
        if _safe_float(trade.pnl, 0.0) != 0.0
    ]
    best_trade = max(pnls) if pnls else 0
    worst_trade = min(pnls) if pnls else 0

    stats = {
        "net_profit": raw.get("total_pnl", 0),
        "net_profit_pct": round(
            raw.get("total_pnl", 0) / (result.meta.initial_capital or 100000) * 100, 2
        ),
        "sharpe": raw.get("sharpe_ratio", 0),
        "sortino": raw.get("sortino_ratio", 0),
        "calmar": raw.get("calmar_ratio", 0),
        "max_drawdown_pct": round(raw.get("max_drawdown_pct", 0) * 100, 2),
        "win_rate": round(raw.get("win_rate", 0) * 100, 1),
        "profit_factor": raw.get("profit_factor", 0),
        "total_trades": raw.get("total_trades", 0),
        "winners": raw.get("winners", 0),
        "losers": raw.get("losers", 0),
        "avg_winner": raw.get("avg_winner", 0),
        "avg_loser": raw.get("avg_loser", 0),
        "avg_trade_pnl": round(raw.get("total_pnl", 0) / max(raw.get("total_trades", 1), 1), 2),
        "avg_bars_held": raw.get("avg_holding_bars", 0),
        "max_consecutive_losses": raw.get("max_consecutive_losses", 0),
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "cagr": round(_safe_float(raw.get("cagr")) * 100, 2),
        "expectancy": 0,  # computed below
    }
    # Expectancy
    wr = raw.get("win_rate", 0)
    stats["expectancy"] = round(
        wr * raw.get("avg_winner", 0) + (1 - wr) * raw.get("avg_loser", 0), 2
    )
    # PSR / p-value
    stats["psr"] = raw.get("psr")
    stats["p_value"] = raw.get("p_value", 1.0)
    return stats


@app.route("/api/backtest/<backtest_id>/stats")

def _detect_indicators(strategy_file: str) -> list[dict]:
    """Detect indicators used by a strategy from source code with basic parameter extraction."""
    path = _strategy_path(strategy_file)
    if path is None or not path.exists():
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
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


def _compute_indicators(
    df: pd.DataFrame, indicator_list: list[dict], ohlcv_times: list[float]
) -> list[dict]:
    """Compute indicator values from OHLCV DataFrame for given time range."""
    try:
        import pandas_ta as ta
    except ImportError:
        return []

    results = []
    seen_names: set[str] = set()
    time_set = set(ohlcv_times)

    for ind in indicator_list:
        kind = (ind.get("kind") or ind.get("name") or "").upper()
        period = int(ind.get("period")) if ind.get("period") else None
        try:
            if kind == "SMA":
                p = period or 20
                vals = ta.sma(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"SMA({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "overlay", data, "#d29922"))
            elif kind == "EMA":
                p = period or 12
                vals = ta.ema(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"EMA({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "overlay", data, "#f97316"))
            elif kind == "RSI":
                p = period or 14
                vals = ta.rsi(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"RSI({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "panel", data, "#a78bfa"))
            elif kind == "ATR":
                p = period or 14
                vals = ta.atr(df["high"], df["low"], df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"ATR({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "panel", data, "#fb923c"))
            elif kind == "BB":
                p = period or 20
                bb = ta.bbands(df["close"], length=p, std=2)
                if bb is not None:
                    for col, label, color in [
                        (f"BBU_{p}_2.0", f"BB Upper ({p})", "#6366f1"),
                        (f"BBM_{p}_2.0", f"BB Mid ({p})", "#94a3b8"),
                        (f"BBL_{p}_2.0", f"BB Lower ({p})", "#6366f1"),
                    ]:
                        if col in bb.columns:
                            data = _indicator_to_points(df, bb[col], time_set)
                            if data and label not in seen_names:
                                seen_names.add(label)
                                results.append(
                                    _indicator_payload(
                                        label,
                                        "overlay",
                                        data,
                                        color,
                                        2 if "Mid" not in label else 0,
                                    )
                                )
            elif kind == "MACD":
                macd = ta.macd(df["close"])
                if macd is not None:
                    for col, label, color in [
                        ("MACD_12_26_9", "MACD", "#3b82f6"),
                        ("MACDs_12_26_9", "Signal", "#f97316"),
                    ]:
                        if col in macd.columns:
                            data = _indicator_to_points(df, macd[col], time_set)
                            if data and label not in seen_names:
                                seen_names.add(label)
                                results.append(_indicator_payload(label, "panel", data, color))
            elif kind == "SUPERTREND":
                st = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
                if st is not None:
                    st_col = next((c for c in st.columns if c.startswith("SUPERT_")), None)
                    if st_col:
                        data = _indicator_to_points(df, st[st_col], time_set)
                        if data and "SuperTrend" not in seen_names:
                            seen_names.add("SuperTrend")
                            results.append(
                                _indicator_payload("SuperTrend", "overlay", data, "#22c55e")
                            )
        except (ValueError, KeyError, pd.errors.EmptyDataError) as e:
            logger.warning("Indicator %s computation failed: %s", kind, e)

    return results


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

    for bt_file in sorted(BACKTESTS_DIR.glob("*.trades.json")):
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
                    "id": bt_file.stem.replace(".trades", ""),
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
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            pass
    return backtests


def _strategy_portfolios(entry: StrategyEntry) -> list[dict]:
    portfolios: list[dict] = []
    if not PORTFOLIOS_DIR.exists():
        return portfolios

    for pf_file in PORTFOLIOS_DIR.glob("*.json"):
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
    except (FileNotFoundError, OSError, SyntaxError):
        return {}

    # --- locate generate_signals FunctionDef ---
    gen_sig_func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "generate_signals":
            gen_sig_func = node
            break

    # --- Pattern B: kwargs with defaults in signature ---
    kwargs_params: dict = {}
    if gen_sig_func is not None:
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
            if isinstance(default_node, ast.Constant):
                val = default_node.value
                if isinstance(val, (int, float, str, bool)):
                    kwargs_params[name] = val
        # also check kwonly args
        for arg, default_node in zip(args_obj.kwonlyargs, args_obj.kw_defaults):
            if default_node is None:
                continue
            name = arg.arg
            if isinstance(default_node, ast.Constant):
                val = default_node.value
                if isinstance(val, (int, float, str, bool)):
                    kwargs_params[name] = val

    if kwargs_params:
        return kwargs_params

    # --- Pattern A: params.get("key", default) in function body ---
    get_params: dict = {}
    if gen_sig_func is not None:
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

    if get_params:
        return get_params

    # --- Pattern C (legacy fallback): ALL_CAPS top-level constants ---
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


def _dolthub_pull():
    """Non-blocking dolt pull in all Dolthub repos. Logs warnings on failure."""
    if not _DOLTHUB_REPOS.is_dir():
        return
    if shutil.which("dolt") is None:
        logger.info("Skipping dolthub pull: optional 'dolt' binary not installed")
        return
    for repo in ("stocks", "earnings", "options", "rates"):
        repo_path = _DOLTHUB_REPOS / repo
        if not (repo_path / ".dolt").is_dir():
            continue
        try:
            subprocess.run(
                ["dolt", "pull"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
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


def _collect_log_entries(paths: list[Path]) -> list[dict]:
    entries: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines):
            payload: dict | None = None
            timestamp: datetime | None = None
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    payload = parsed
                    timestamp = _parse_log_timestamp(parsed.get("timestamp"))
            except json.JSONDecodeError:
                payload = None
            entries.append(
                {
                    "line": line,
                    "timestamp": timestamp,
                    "level": _extract_log_level(line, payload),
                    "path": path.name,
                    "line_number": line_number,
                }
            )
    return entries


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
    catalog_path = DATA_DIR / "catalog.json"
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
    (DATA_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2, default=str))


DATA_TASKS: dict[str, dict[str, Any]] = {}
DATA_TASKS_LOCK = threading.Lock()
ACTIVE_TASK_STATUSES = {"starting", "running", "paused"}
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


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


def _register_download_task(
    record: dict[str, Any], request_key: str | None
) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        if request_key:
            for task in DATA_TASKS.values():
                if task.get("download_request_key") != request_key:
                    continue
                if task.get("status") in ACTIVE_TASK_STATUSES:
                    return dict(task)
            record["download_request_key"] = request_key
        DATA_TASKS[record["id"]] = record
        return None


def _start_singleflight_download(
    task_id: str,
    request_key: str,
    starter: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    reservation = {
        "id": task_id,
        "status": "starting",
        "download_request_key": request_key,
    }
    existing = _register_download_task(reservation, request_key)
    if existing is not None:
        return {
            "task_id": existing["id"],
            "status": existing.get("status", "starting"),
            "coalesced": True,
        }

    try:
        return starter(task_id, *args, **kwargs)
    except Exception as exc:
        with DATA_TASKS_LOCK:
            current = DATA_TASKS.get(task_id)
        if current is not None:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        raise


def _task_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _task_log_path(task_id: str) -> Path:
    return TASK_LOGS_DIR / f"task_{task_id}.log"


def _read_task_log(log_file: str | Path, *, max_bytes: int | None = None) -> str:
    path = Path(log_file)
    if not path.exists():
        return ""

    if max_bytes is None:
        return path.read_text(encoding="utf-8", errors="replace")

    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        return handle.read().decode("utf-8", errors="replace")


def _append_task_log(log_file: Path, message: str) -> None:
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(message)


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
        reservation = DATA_TASKS.get(task_id)
        if reservation is not None and reservation.get("download_request_key"):
            record["download_request_key"] = reservation["download_request_key"]
    return record


def _get_task(task_id: str) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        return DATA_TASKS.get(task_id)


def _snapshot_task(task_id: str) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        return dict(task) if task is not None else None


def _update_task_fields(task_id: str, **updates: Any) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return None
        task.update(updates)
        if task.get("status") in FINAL_TASK_STATUSES:
            task.pop("download_request_key", None)
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
            detail["current"] = current

        next_percent = float(task.get("progress", 0.0) or 0.0)
        if percent is not None:
            next_percent = max(0.0, min(100.0, round(percent, 2)))

        detail["percent"] = next_percent
        task["progress"] = next_percent
        task["progress_detail"] = detail
        task["updated_at"] = _task_now_iso()
        return dict(task)


def _ingest_task_log_line(task_id: str, text: str) -> None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return

        detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
        detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)
        detail["current"] = text

        if text.startswith("✅"):
            detail["completed"] = int(detail.get("completed", 0) or 0) + 1
        elif text.startswith("❌"):
            detail["failed"] = int(detail.get("failed", 0) or 0) + 1

        total = detail["total"]
        if total > 0 and (text.startswith("✅") or text.startswith("❌")):
            done = min(int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0), total)
            detail["percent"] = round(min(100.0, done / total * 100.0), 2)
            task["progress"] = detail["percent"]
        else:
            detail["percent"] = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)

        task["progress_detail"] = detail
        task["updated_at"] = _task_now_iso()


def _task_progress_payload(task: dict[str, Any], log_content: str = "") -> dict[str, Any]:
    detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
    detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)

    if not detail.get("current") and log_content:
        lines = [line.strip() for line in log_content.splitlines() if line.strip()]
        if lines:
            detail["current"] = lines[-1]

    if detail["total"] > 0 and detail.get("completed", 0) == 0 and detail.get("failed", 0) == 0 and log_content:
        detail["completed"] = log_content.count("✅")
        detail["failed"] = log_content.count("❌")

    percent = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)
    if detail["total"] > 0 and percent == 0.0:
        done = int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0)
        if done > 0:
            percent = min(100.0, done / detail["total"] * 100.0)

    if task.get("status") == "completed":
        percent = 100.0

    detail["percent"] = round(percent, 2)
    return detail


def _task_controls_payload(task: dict[str, Any]) -> dict[str, bool]:
    status = task.get("status", "unknown")
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
        "status": task.get("status", "unknown"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "progress": _task_progress_payload(task, log_content),
        "log": log_content,
    }
    payload.update(_task_controls_payload(task))
    return payload


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
        updates["error"] = error
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
    except (json.JSONDecodeError, FileNotFoundError, OSError) as exc:
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

    def run_proc() -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        command_display = " ".join(str(part) for part in cmd)
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

    def run_download() -> None:
        command_display = " ".join(str(part) for part in cmd)
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
                    used_native_backend = False

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
                    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
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

    thread = threading.Thread(target=run_download, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


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
    from data_tools.downloaders.dukascopy_native import DownloadCancelled, DukascopyNativeDownloader
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

    def run_download() -> None:
        command_display = " ".join(str(part) for part in cmd)
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
                    used_native_backend = False

                    def report_progress(pct: float, message: str) -> None:
                        overall = ((base_completed + (max(0.0, min(pct, 100.0)) / 100.0)) / total) * 100.0
                        _update_task_progress(
                            task_id,
                            percent=overall,
                            current=f"{item_label}: {message}",
                            total=total,
                        )

                    try:
                        result = jforex_downloader.download(
                            symbol,
                            timeframe,
                            DATA_DIR,
                            start=start,
                            end=end,
                            offer_side=offer_side,
                            query_timeout_seconds=query_timeout_seconds,
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
                    except (
                        ModuleNotFoundError,
                        ImportError,
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                        json.JSONDecodeError,
                    ) as exc:
                        jforex_traceback = traceback.format_exc()
                        fallback_reason = (
                            _jforex_fallback_reason(exc)
                            if fallback_enabled
                            else None
                        )
                        if fallback_reason is not None:
                            if fallback_downloader is None:
                                fallback_downloader = DukascopyNativeDownloader()
                            used_native_backend = True
                            _write_task_log_line(
                                handle,
                                task_id,
                                (
                                    f"ℹ️ {symbol} {timeframe}: JForex not available (reason: {fallback_reason}), "
                                    f"using HTTP downloader. Original error: {exc}"
                                ),
                                stream="stdout",
                                log_level="info",
                                source="data-task",
                            )
                            _write_task_traceback(
                                handle,
                                task_id,
                                jforex_traceback,
                                source="data-task",
                                log_level="warning",
                            )
                            logger.warning(
                                "JForex download failed for %s %s; attempting native fallback: %s",
                                symbol,
                                timeframe,
                                exc,
                                extra={
                                    "data": {
                                        "task_id": task_id,
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "source": "data-task",
                                        "downloader": "jforex",
                                        "fallback": "dukascopy-native",
                                    }
                                },
                            )
                            try:
                                result = fallback_downloader.download(
                                    symbol,
                                    timeframe,
                                    DATA_DIR,
                                    start=start,
                                    end=end,
                                    offer_side=offer_side,
                                    merge_tick=merge_tick,
                                    merge_s1=merge_s1,
                                    batch_size=batch_size,
                                    pause_ms=pause_ms,
                                    pause_event=pause_event,
                                    stop_event=stop_event,
                                    report_progress=report_progress,
                                    log_callback=task_log,
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
                            except (
                                OSError,
                                RuntimeError,
                                TimeoutError,
                                ValueError,
                                KeyError,
                                json.JSONDecodeError,
                            ) as fallback_exc:
                                failed_items += 1
                                if first_error is None:
                                    first_error = (
                                        f"{symbol} {timeframe}: JForex failed ({exc}); "
                                        f"native fallback failed ({fallback_exc})"
                                    )
                                _write_task_log_line(
                                    handle,
                                    task_id,
                                    (
                                        f"❌ {symbol} {timeframe}: JForex failed ({exc}); "
                                        f"native fallback also failed ({fallback_exc})"
                                    ),
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
                                    "Native fallback failed for %s %s after JForex failure",
                                    symbol,
                                    timeframe,
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
                                continue
                        else:
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
                                jforex_traceback,
                                source="data-task",
                            )
                            logger.exception(
                                "JForex download failed for %s %s: %s",
                                symbol,
                                timeframe,
                                exc,
                                extra={
                                    "data": {
                                        "task_id": task_id,
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "source": "data-task",
                                        "downloader": "jforex",
                                    }
                                },
                            )
                            continue

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
                root_logger.removeHandler(log_handler)
            except Exception as exc:
                duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                traceback_text = traceback.format_exc()
                root_logger.removeHandler(log_handler)
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
        "failed to import 'com.dukascopy'": "JForex SDK classes unavailable",
        "classnotfoundexception": "JForex SDK classes unavailable",
        "com.dukascopy": "JForex SDK classes unavailable",
        "failed to start jvm": "Java/JVM startup failed",
        "could not resolve jforex sdk classpath": "JForex SDK classpath unavailable",
        "jforex sdk classpath": "JForex SDK classpath unavailable",
        "jvm shared library": "Java/JVM runtime unavailable",
        "jvm dll not found": "Java/JVM runtime unavailable",
        "no jvm shared library file": "Java/JVM runtime unavailable",
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

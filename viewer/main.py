from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlmodel import SQLModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve()
    if _data_dir_env
    else (PROJECT_ROOT / "data").resolve()
)
os.environ.setdefault("DATA_DIR", str(DATA_DIR))
os.environ.setdefault("TRADING_DATA_DIR", str(DATA_DIR))

_log_dir_env = os.environ.get("LOG_DIR")
LOG_DIR = (
    Path(_log_dir_env).expanduser().resolve()
    if _log_dir_env
    else (PROJECT_ROOT / "logs").resolve()
)
os.environ.setdefault("LOG_DIR", str(LOG_DIR))

from shared.logging import get_logger
from shared.models import BacktestResult, EquityPoint
from shared.database import (
    init_db,
    list_backtest_ids,
    load_backtest_result,
    migrate_json_to_sqlite,
    save_result_to_sqlite,
)
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    StrategyEntry,
    iter_strategy_files,
    load_strategy_meta,
    matches_strategy_identifier,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
)
from viewer import core as legacy_viewer

logger = get_logger("viewer")

BACKTESTS_DIR = DATA_DIR / "backtests"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
REACT_DIST = PROJECT_ROOT / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).resolve().parent / "static"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOG_DIR / "tasks"
OPTIMIZATIONS_DIR = legacy_viewer.OPTIMIZATIONS_DIR
ALL_LOGS_NAME = legacy_viewer.ALL_LOGS_NAME
BARS_BEFORE = legacy_viewer.BARS_BEFORE
BARS_AFTER = legacy_viewer.BARS_AFTER

DATA_TASKS = legacy_viewer.DATA_TASKS
DATA_TASKS_LOCK = legacy_viewer.DATA_TASKS_LOCK
ACTIVE_TASK_STATUSES = legacy_viewer.ACTIVE_TASK_STATUSES
FINAL_TASK_STATUSES = legacy_viewer.FINAL_TASK_STATUSES
_running_jobs = legacy_viewer._running_jobs

_refresh_catalog = legacy_viewer._refresh_catalog
_start_data_task = legacy_viewer._start_data_task
_start_dukascopy_task = legacy_viewer._start_dukascopy_task
_create_task_record = legacy_viewer._create_task_record
_set_task_status = legacy_viewer._set_task_status
_update_task_progress = legacy_viewer._update_task_progress
_update_task_fields = legacy_viewer._update_task_fields
_mark_task_finished = legacy_viewer._mark_task_finished
_snapshot_task = legacy_viewer._snapshot_task
_get_task = legacy_viewer._get_task
_task_log_path = legacy_viewer._task_log_path
_read_task_log = legacy_viewer._read_task_log
_task_log_message = legacy_viewer._task_log_message
_task_timestamp = legacy_viewer._task_timestamp
_write_task_log_line = legacy_viewer._write_task_log_line
_write_task_traceback = legacy_viewer._write_task_traceback
_signal_process = legacy_viewer._signal_process
_serialize_task = legacy_viewer._serialize_task
_queue_agent_task = legacy_viewer._queue_agent_task
_launch_agent_task = legacy_viewer._launch_agent_task
_optimization_summary_payload = legacy_viewer._optimization_summary_payload
_load_optimization_payload = legacy_viewer._load_optimization_payload
_strategy_path = legacy_viewer._strategy_path
_ThreadBoundTaskLogHandler = legacy_viewer._ThreadBoundTaskLogHandler
execute_optimization = legacy_viewer.execute_optimization
OptimizationConfig = legacy_viewer.OptimizationConfig
DEFAULT_MODEL_NAME = legacy_viewer.DEFAULT_MODEL_NAME
SUPPORTED_MODEL_NAMES = legacy_viewer.SUPPORTED_MODEL_NAMES
_parse_symbol_list = legacy_viewer._parse_symbol_list
_parse_timeframe_list = legacy_viewer._parse_timeframe_list
_normalize_dukascopy_genesis_timeframe = legacy_viewer._normalize_dukascopy_genesis_timeframe
_load_dukascopy_genesis = legacy_viewer._load_dukascopy_genesis
_collect_dukascopy_genesis = legacy_viewer._collect_dukascopy_genesis
_download_task_labels = legacy_viewer._download_task_labels
_categorize_path = legacy_viewer._categorize_path
_load_catalog_ohlcv_entries = legacy_viewer._load_catalog_ohlcv_entries
_scan_ohlcv_filesystem = legacy_viewer._scan_ohlcv_filesystem
_scan_dolthub_repos = legacy_viewer._scan_dolthub_repos
_warning_message_from_entry = legacy_viewer._warning_message_from_entry
_dolthub_repo_status = legacy_viewer._dolthub_repo_status
_dolthub_pull = legacy_viewer._dolthub_pull
_safe_int = legacy_viewer._safe_int

init_db()
migrate_json_to_sqlite()

app = FastAPI(title="trading-platform viewer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SymbolsResponse(SQLModel):
    symbols: list[str]
    categories: dict[str, list[str]]


class LogFileModel(SQLModel):
    name: str
    size_bytes: int
    modified: str | None = None


class LogContentModel(SQLModel):
    name: str
    lines: list[str]
    total_lines: int


class BenchmarkPoint(SQLModel):
    time: float
    equity: float


class BenchmarkResponse(SQLModel):
    symbol: str
    timeframe: str
    equity: list[BenchmarkPoint]


class NameUpdateRequest(SQLModel):
    name: str


class BacktestRunRequest(SQLModel):
    strategy: str
    engine: str = "native"
    symbols: list[str]
    timeframe: str = "1d"
    start: str = ""
    end: str = ""
    capital: float = 100000.0
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_leverage: float | None = None
    modeling_resolution: str | None = "open_close"
    intrabar_resolution: str | None = None
    parameters: dict[str, Any] = {}


class PortfolioStrategyConfig(SQLModel):
    backtest_id: str = ""
    name: str = ""
    risk_per_trade_pct: float = 1.0
    max_positions: int = 99


class CreatePortfolioRequest(SQLModel):
    name: str = "Untitled"
    account_capital: float = 100000.0
    strategies: list[PortfolioStrategyConfig] = []


class DataDownloadRequest(SQLModel):
    source: str = "dukascopy"
    symbols: str = ""
    timeframes: str = "1m"
    start: str = ""
    end: str = ""
    price_type: str = "bid"
    merge_tick: bool = False
    merge_bid_ask: bool = False
    merge_s1: bool = False
    batch_size: int | None = None
    concurrency: int | None = None
    pause_ms: int = 1000


class DataResampleRequest(SQLModel):
    source_path: str
    target_timeframes: str


class AgentConvertRequest(SQLModel):
    code: str
    strategy_name: str | None = None
    engine: str | None = None
    model: str | None = None
    symbols: list[str] | None = None
    timeframe: str | None = None
    start: str | None = None
    end: str | None = None
    capital: float = 100000.0


class DataValidateRequest(SQLModel):
    symbol: str | None = None
    report: str | None = None


class DataCalibrateRequest(SQLModel):
    symbols: str
    timeframes: str | None = None
    price_type: str = "bid"
    start_year: int = 2020
    min_year: int = 1990
    force: bool = False
    verbose: bool = False


@app.middleware("http")
async def static_spa_middleware(request: Request, call_next):
    path = request.url.path
    excluded_paths = {"/docs", "/redoc", "/openapi.json"}
    if (
        request.method in {"GET", "HEAD"}
        and not path.startswith("/api")
        and not path.startswith("/ws")
        and path not in excluded_paths
    ):
        rel_path = path.strip("/")
        if rel_path:
            candidate = (STATIC_DIR / rel_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
                return FileResponse(candidate)
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
    return await call_next(request)


def _load_result(backtest_id: str) -> BacktestResult | None:
    result = load_backtest_result(backtest_id)
    if result is not None:
        return result

    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return None

    try:
        fallback_result = BacktestResult.load(str(path))
        save_result_to_sqlite(fallback_result)
        return fallback_result
    except Exception as exc:
        logger.warning("Could not load fallback JSON backtest %s: %s", backtest_id, exc)
        return None


def _task_status(task_id: str) -> str | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        return task.get("status") if task else None


def _task_action_payload(task_id: str) -> dict[str, Any]:
    task = _snapshot_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _serialize_task(task_id, task, include_log=True)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _ensure_equity_curve(result: BacktestResult, initial_capital: float) -> list[EquityPoint]:
    if result.equity_curve:
        return result.equity_curve

    start_raw = result.meta.backtest_start or result.meta.created_at
    end_raw = result.meta.backtest_end or result.meta.created_at or start_raw
    if not start_raw:
        return []

    start_ts = pd.to_datetime(start_raw, utc=True, errors="coerce")
    end_ts = pd.to_datetime(end_raw, utc=True, errors="coerce")
    if pd.isna(start_ts):
        return []
    if pd.isna(end_ts):
        end_ts = start_ts

    stats = result.stats or {}
    if "net_profit" in stats:
        ending_equity = initial_capital + _safe_float(stats.get("net_profit"), 0.0)
    else:
        total_return = _safe_float(stats.get("total_return"), 0.0)
        ending_equity = initial_capital * (1.0 + total_return)
    if not math.isfinite(ending_equity):
        ending_equity = initial_capital

    start_iso = pd.Timestamp(start_ts).isoformat()
    end_iso = pd.Timestamp(end_ts).isoformat()
    start_value = round(float(initial_capital), 2)
    end_value = round(float(ending_equity), 2)

    points = [EquityPoint(time=start_iso, equity=start_value)]
    if end_iso != start_iso or end_value != start_value:
        points.append(EquityPoint(time=end_iso, equity=end_value))
    return points


def _safe_strategy_entries() -> list[StrategyEntry]:
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


def _safe_strategy_entry(identifier: str) -> StrategyEntry | None:
    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    for entry in _safe_strategy_entries():
        if matches_strategy_identifier(entry, normalized):
            return entry
    return None


def _infer_symbol_category(path_value: str, fallback_asset_class: str) -> str:
    normalized = path_value.replace("\\", "/").lower()
    path_parts = {part.lower() for part in Path(path_value).parts}

    if "metals" in path_parts or "/metals/" in normalized:
        return "metals"
    if "indices" in path_parts or "/indices/" in normalized:
        return "indices"
    if "energy" in path_parts or "/energy/" in normalized:
        return "energy"
    if "forex" in path_parts or "/forex/" in normalized:
        return "forex"
    if "equity" in path_parts or "stocks" in path_parts or "/equity/" in normalized:
        return "equity"
    if "etf" in path_parts or "/etf/" in normalized:
        return "etf"
    if "crypto" in path_parts or "/crypto/" in normalized:
        return "crypto"
    return (fallback_asset_class or "other").lower()


_TIMEFRAME_PATTERN = re.compile(r"^(?:tick|s1|mn1|\d+[smhdw])$", re.IGNORECASE)


def _looks_like_timeframe(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and bool(_TIMEFRAME_PATTERN.match(normalized))


def _format_catalog_date(value: object) -> str | None:
    if value is None:
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.date().isoformat()


def _catalog_timestamp_to_epoch(value: object) -> float | None:
    if value is None:
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return float(timestamp.timestamp())


def _should_reuse_cached_parquet_entry(cached_entry: dict[str, Any] | None, file_mtime: float) -> bool:
    if not isinstance(cached_entry, dict):
        return False

    cached_last_updated = _catalog_timestamp_to_epoch(cached_entry.get("last_updated"))
    if cached_last_updated is not None:
        return file_mtime <= (cached_last_updated + 1e-6)

    # Backward compatibility for stale catalog payloads without last_updated.
    try:
        return float(file_mtime) <= float(cached_entry.get("mtime_ns")) / 1_000_000_000
    except (TypeError, ValueError):
        return False


def _catalog_metadata_by_path(catalog_entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(entry.get("path")): entry
        for entry in catalog_entries
        if isinstance(entry, dict) and entry.get("path")
    }


def _read_row_group_stat(
    metadata: Any,
    column_index: int,
    row_group_index: int,
    *,
    boundary: str,
) -> object | None:
    stats = metadata.row_group(row_group_index).column(column_index).statistics
    if stats is None or not getattr(stats, "has_min_max", False):
        return None
    return stats.min if boundary == "min" else stats.max


def _read_parquet_summary(parquet_path: Path) -> dict[str, Any]:
    parquet_file = pq.ParquetFile(parquet_path)
    metadata = parquet_file.metadata
    summary: dict[str, Any] = {
        "rows": metadata.num_rows if metadata is not None else None,
        "start": None,
        "end": None,
    }
    if metadata is None or metadata.num_rows == 0 or metadata.num_row_groups == 0:
        return summary

    columns = list(parquet_file.schema_arrow.names)
    lowered = {column.lower(): column for column in columns}
    timestamp_column = None
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in lowered:
            timestamp_column = lowered[candidate]
            break
    if timestamp_column is None:
        return summary

    column_index = columns.index(timestamp_column)
    start_value = _read_row_group_stat(metadata, column_index, 0, boundary="min")
    end_value = _read_row_group_stat(
        metadata,
        column_index,
        metadata.num_row_groups - 1,
        boundary="max",
    )

    if start_value is None:
        first_group = parquet_file.read_row_group(0, columns=[timestamp_column])
        if first_group.num_rows > 0:
            start_value = first_group.column(0)[0].as_py()
    if end_value is None:
        last_group = parquet_file.read_row_group(
            metadata.num_row_groups - 1,
            columns=[timestamp_column],
        )
        if last_group.num_rows > 0:
            end_value = last_group.column(0)[last_group.num_rows - 1].as_py()

    summary["start"] = _format_catalog_date(start_value)
    summary["end"] = _format_catalog_date(end_value)
    return summary


def _parse_ohlcv_path(parquet_path: Path, ohlcv_root: Path) -> dict[str, Any] | None:
    try:
        rel = parquet_path.relative_to(ohlcv_root)
    except ValueError:
        return None

    parts = rel.parts
    if len(parts) < 4:
        return None

    source = parts[0]
    asset_class = parts[1]
    stem = parquet_path.stem
    region: str | None = None

    if len(parts) >= 5:
        region = parts[-3]
        symbol = parts[-2]
        timeframe = stem if _looks_like_timeframe(stem) else "1d"
    else:
        if _looks_like_timeframe(stem):
            symbol = parts[-2]
            timeframe = stem
        else:
            region = parts[-2]
            symbol = stem
            timeframe = "1d"

    entry: dict[str, Any] = {
        "source": source,
        "asset_class": asset_class,
        "symbol": symbol,
        "timeframe": timeframe,
        "path": str(parquet_path),
    }
    if region:
        entry["region"] = region
    return entry


def _scan_single_file_ohlcv_entries(
    metadata_by_path: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ohlcv_root = DATA_DIR / "ohlcv"
    if not ohlcv_root.exists():
        return []

    if metadata_by_path is None:
        catalog_entries, _ = _load_catalog_ohlcv_entries()
        metadata_by_path = _catalog_metadata_by_path(catalog_entries)

    entries: list[dict[str, Any]] = []
    for parquet_path in sorted(ohlcv_root.rglob("*.parquet")):
        parsed = _parse_ohlcv_path(parquet_path, ohlcv_root)
        if parsed is None:
            continue

        path_key = str(parquet_path)
        cached_entry = metadata_by_path.get(path_key)

        try:
            file_mtime = os.path.getmtime(parquet_path)
        except OSError:
            file_mtime = None

        used_cached_summary = False
        if (
            file_mtime is not None
            and _should_reuse_cached_parquet_entry(cached_entry, file_mtime)
            and isinstance(cached_entry, dict)
        ):
            for key in ("rows", "size_bytes", "columns", "start", "end", "last_updated"):
                if cached_entry.get(key) is not None:
                    parsed[key] = cached_entry.get(key)
            used_cached_summary = True

        if not used_cached_summary:
            try:
                parsed.update(_read_parquet_summary(parquet_path))
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("Could not read date range from %s: %s", parquet_path, exc)

        if parsed.get("size_bytes") is None:
            try:
                parsed["size_bytes"] = os.path.getsize(parquet_path)
            except OSError:
                parsed["size_bytes"] = 0

        entries.append(parsed)

    return entries


def _load_data_entries() -> list[dict[str, Any]]:
    catalog_entries, _ = _load_catalog_ohlcv_entries()
    metadata_by_path = _catalog_metadata_by_path(catalog_entries)
    scanned_entries = _scan_single_file_ohlcv_entries(metadata_by_path)

    if scanned_entries:
        for entry in scanned_entries:
            catalog_entry = metadata_by_path.get(entry["path"])
            if not isinstance(catalog_entry, dict):
                continue
            for key in (
                "validation",
                "error",
                "warning_message",
                "validation_message",
                "last_updated",
                "repo",
            ):
                if catalog_entry.get(key) is not None:
                    entry[key] = catalog_entry.get(key)
        return scanned_entries

    fallback_entries: list[dict[str, Any]] = []
    for entry in catalog_entries:
        if not isinstance(entry, dict):
            continue
        path_value = str(entry.get("path", ""))
        if path_value.endswith(".parquet"):
            fallback_entries.append(dict(entry))
    return fallback_entries


@app.get("/api/strategies", response_model=list[dict[str, Any]])
async def list_strategies() -> list[dict[str, Any]]:
    strategies: list[dict[str, Any]] = []
    if not DEFAULT_STRATEGIES_DIR.exists():
        return strategies

    for entry in _safe_strategy_entries():
        try:
            payload = legacy_viewer._strategy_payload(entry)
            payload.pop("documentation_markdown", None)
            payload.pop("backtests", None)
            payload.pop("portfolios", None)
            strategies.append(payload)
        except Exception as exc:
            logger.warning("Could not load strategy %s: %s", entry.relative_file, exc)

    return strategies


@app.get("/api/strategies/{name:path}", response_model=dict[str, Any])
async def get_strategy(name: str) -> dict[str, Any]:
    entry = _safe_strategy_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    try:
        return legacy_viewer._strategy_payload(entry)
    except Exception as exc:
        logger.exception("Failed to build strategy payload for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.patch("/api/strategies/{name:path}", response_model=dict[str, Any])
async def rename_strategy(name: str, payload: NameUpdateRequest) -> dict[str, Any]:
    entry = _safe_strategy_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    updated_name = str(payload.name or "").strip()
    if not updated_name:
        raise HTTPException(status_code=400, detail="Strategy name cannot be empty")

    try:
        legacy_viewer.update_strategy_display_name(entry.file_path, updated_name, STRATEGIES_DIR)
        refreshed = _safe_strategy_entry(entry.slug)
        if refreshed is None:
            raise HTTPException(
                status_code=500, detail=f"Strategy '{name}' could not be reloaded after rename"
            )
        return legacy_viewer._strategy_payload(refreshed)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to rename strategy %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/backtests", response_model=list[dict[str, Any]])
async def list_backtests() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    backtest_ids = list_backtest_ids()
    known_ids = set(backtest_ids)
    if BACKTESTS_DIR.exists():
        for path in sorted(BACKTESTS_DIR.glob("*.trades.json")):
            fallback_id = path.stem.replace(".trades", "")
            if fallback_id not in known_ids:
                backtest_ids.append(fallback_id)
                known_ids.add(fallback_id)

    for backtest_id in backtest_ids:
        result = _load_result(backtest_id)
        if result is None:
            continue

        try:
            data = result.to_dict()
            meta = data.get("meta", {})
            trades = data.get("trades", [])
            initial_capital = meta.get("initial_capital", 100000)
            start = meta.get("backtest_start", "")
            end = meta.get("backtest_end", "")

            quick = legacy_viewer._quick_stats_and_equity(
                trades,
                initial_capital,
                start,
                end,
                equity_curve=data.get("equity_curve", []),
                cached_stats=data.get("stats", {}),
            )
            raw_strategy = (meta.get("strategies") or ["unknown"])[0]
            try:
                strategy_slug, strategy_display_name, strategy_file = legacy_viewer._strategy_labels(
                    raw_strategy
                )
            except Exception:
                strategy_slug = str(raw_strategy or "unknown")
                strategy_display_name = strategy_slug
                strategy_file = ""
            notes = str(meta.get("notes", "") or "").strip()

            results.append(
                {
                    "id": backtest_id,
                    "filename": f"{backtest_id}.trades.json",
                    "title": notes or strategy_display_name,
                    "notes": notes,
                    "strategy": raw_strategy,
                    "strategy_slug": strategy_slug,
                    "strategy_display_name": strategy_display_name,
                    "strategy_file": strategy_file,
                    "engine": meta.get("engine", "unknown"),
                    "symbols": meta.get("data", {}).get("symbols", []),
                    "timeframes": meta.get("data", {}).get("timeframes", []),
                    "trade_count": len(trades),
                    "start": start,
                    "end": end,
                    "created_at": meta.get("created_at", ""),
                    "sharpe": quick["sharpe"],
                    "cagr": quick["cagr"],
                    "max_drawdown_pct": quick["max_drawdown_pct"],
                    "net_profit": quick["net_profit"],
                    "net_profit_pct": quick["net_profit_pct"],
                    "psr": quick.get("psr"),
                    "p_value": quick.get("p_value", 1.0),
                    "equity_mini": quick["equity_mini"],
                }
            )
        except Exception as exc:
            logger.warning("Could not read backtest %s from sqlite: %s", backtest_id, exc)

    return results


@app.get("/api/backtest/{backtest_id}", response_model=dict[str, Any])
async def get_backtest(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    try:
        return result.to_dict()
    except Exception as exc:
        logger.exception("Failed to serialize backtest '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.patch("/api/backtests/{backtest_id}", response_model=dict[str, Any])
async def rename_backtest(backtest_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    updated_name = str(payload.name or "").strip()
    if not updated_name:
        raise HTTPException(status_code=400, detail="Backtest title cannot be empty")

    result.meta.notes = updated_name
    BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    try:
        result.save(output_path)
        migrate_json_to_sqlite()
        return {
            "id": backtest_id,
            "title": updated_name,
            "notes": updated_name,
            "status": "updated",
        }
    except Exception as exc:
        logger.exception("Failed to rename backtest '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/backtest/{backtest_id}/info", response_model=dict[str, Any])
async def get_backtest_info(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return legacy_viewer._backtest_info_payload(result)


@app.get("/api/backtest/{backtest_id}/stats", response_model=dict[str, Any])
async def get_backtest_stats(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    try:
        raw = legacy_viewer._canonical_stats_from_result(result)
        stats = {
            "net_profit": raw.get("total_pnl", 0),
            "net_profit_pct": round(
                raw.get("total_pnl", 0) / (result.meta.initial_capital or 100000) * 100,
                2,
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
            "best_trade": raw.get("avg_winner", 0),
            "worst_trade": raw.get("avg_loser", 0),
            "cagr": round(_safe_float(raw.get("cagr")) * 100, 2),
            "expectancy": 0,
        }
        win_rate = raw.get("win_rate", 0)
        stats["expectancy"] = round(
            win_rate * raw.get("avg_winner", 0) + (1 - win_rate) * raw.get("avg_loser", 0),
            2,
        )
        stats["psr"] = raw.get("psr")
        stats["p_value"] = raw.get("p_value", 1.0)
        return stats
    except Exception as exc:
        logger.exception("Failed to compute stats for '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/backtest/{backtest_id}/equity", response_model=list[dict[str, float]])
async def get_equity_curve(backtest_id: str) -> list[dict[str, float]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return legacy_viewer._equity_to_viewer(result)


@app.get("/api/backtest/{backtest_id}/trades", response_model=list[dict[str, Any]])
async def get_backtest_trades(backtest_id: str) -> list[dict[str, Any]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return [legacy_viewer._trade_to_viewer(trade) for trade in result.trades]


def _normalize_indicator_payloads(indicators: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_indicator in indicators:
        indicator = (
            raw_indicator.to_dict()
            if hasattr(raw_indicator, "to_dict")
            else dict(raw_indicator) if isinstance(raw_indicator, dict) else None
        )
        if not isinstance(indicator, dict):
            continue

        points = indicator.get("data")
        if not isinstance(points, list):
            continue

        normalized_points: list[dict[str, Any]] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            raw_time = point.get("time")
            unix_time = (
                float(raw_time)
                if isinstance(raw_time, (int, float))
                else legacy_viewer._iso_to_unix(str(raw_time))
            )
            if unix_time <= 0:
                continue
            normalized_point = dict(point)
            normalized_point["time"] = unix_time
            normalized_points.append(normalized_point)
        indicator["data"] = normalized_points

        if indicator.get("pane") in (None, ""):
            style = indicator.get("style")
            if isinstance(style, dict):
                pane_value = style.get("pane")
                if isinstance(pane_value, str) and pane_value.strip():
                    indicator["pane"] = pane_value

        normalized.append(indicator)
    return normalized


def _slice_indicators_to_time_range(
    indicators: list[dict[str, Any]],
    *,
    time_start: float,
    time_end: float,
) -> None:
    for indicator in indicators:
        points = indicator.get("data")
        if not isinstance(points, list):
            continue
        indicator["data"] = [
            point
            for point in points
            if time_start <= float(point.get("time", 0) or 0) <= time_end
        ]


def _collect_viewer_drawings(result: BacktestResult, symbol: str | None = None) -> list[dict[str, Any]]:
    drawings: list[dict[str, Any]] = []
    for drawing in result.drawings:
        if symbol and not legacy_viewer._drawing_matches_symbol(drawing, symbol):
            continue
        viewer_drawing = legacy_viewer._drawing_to_viewer(drawing)
        if viewer_drawing is not None:
            drawings.append(viewer_drawing)
    return drawings


@app.get("/api/backtest/{backtest_id}/trade/{trade_nr}", response_model=dict[str, Any])
async def get_trade_with_context(backtest_id: str, trade_nr: int) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")

    trade = next((item for item in result.trades if item.id == trade_nr), None)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade #{trade_nr} not found")

    trade_payload = legacy_viewer._trade_to_viewer(trade)
    timeframe = (
        result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
    )

    ohlcv: list[dict[str, Any]] = []
    df = legacy_viewer._load_ohlcv_df(trade.symbol, timeframe)
    if df is None or len(df) == 0:
        details = legacy_viewer._missing_ohlcv_details(
            trade.symbol,
            timeframe,
            backtest_start=str(result.meta.backtest_start or ""),
            backtest_end=str(result.meta.backtest_end or ""),
        )
        raise HTTPException(status_code=404, detail=details)

    ohlcv_start_idx = 0
    ohlcv_end_idx = 0
    entry_ts = pd.Timestamp(trade.entry_time, tz="UTC") if trade.entry_time else None
    if entry_ts is not None:
        idx = df["timestamp"].searchsorted(entry_ts)
        ohlcv_start_idx = max(0, idx - legacy_viewer.BARS_BEFORE)
        ohlcv_end_idx = min(len(df), idx + legacy_viewer.BARS_AFTER)
        ohlcv = legacy_viewer._df_to_ohlcv_records(df.iloc[ohlcv_start_idx:ohlcv_end_idx])

    indicators = _normalize_indicator_payloads(list(result.indicators))
    drawings = _collect_viewer_drawings(result, trade.symbol)

    if not indicators and ohlcv and df is not None:
        strategy_identifier = result.meta.strategies[0] if result.meta.strategies else ""
        detected = legacy_viewer._detect_indicators(strategy_identifier)
        if detected:
            ohlcv_times = [row["time"] for row in ohlcv]
            warmup_start = max(0, ohlcv_start_idx - 50)
            df_slice = df.iloc[warmup_start:ohlcv_end_idx].reset_index(drop=True)
            indicators = legacy_viewer._compute_indicators(df_slice, detected, ohlcv_times)

    if ohlcv and indicators:
        time_start = ohlcv[0]["time"]
        time_end = ohlcv[-1]["time"]
        _slice_indicators_to_time_range(indicators, time_start=time_start, time_end=time_end)

    return {
        "trade": trade_payload,
        "ohlcv": ohlcv,
        "symbol": trade.symbol,
        "timeframe": timeframe,
        "indicators": indicators,
        "drawings": drawings,
        "total_trades": len(result.trades),
    }


@app.get("/api/backtest/{backtest_id}/replay", response_model=dict[str, Any])
async def get_backtest_replay(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")

    trades = [legacy_viewer._trade_to_viewer(trade) for trade in result.trades]
    timeframe = (
        result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
    )
    symbols = sorted({trade.symbol for trade in result.trades if trade.symbol}) or list(
        result.meta.data.get("symbols", [])
    )

    ohlcv_by_symbol: dict[str, list[dict[str, Any]]] = {}
    missing_ohlcv: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
        if df is None or len(df) == 0:
            missing_ohlcv[symbol] = legacy_viewer._missing_ohlcv_details(
                symbol,
                timeframe,
                backtest_start=str(result.meta.backtest_start or ""),
                backtest_end=str(result.meta.backtest_end or ""),
            )
            continue
        ohlcv_by_symbol[symbol] = legacy_viewer._df_to_ohlcv_records(df)

    if not ohlcv_by_symbol:
        detail = next(iter(missing_ohlcv.values()), {"message": "No OHLCV data available"})
        raise HTTPException(status_code=404, detail=detail)

    primary_symbol = next(iter(ohlcv_by_symbol))
    indicators = _normalize_indicator_payloads(list(result.indicators))

    if not indicators:
        df = legacy_viewer._load_ohlcv_df(primary_symbol, timeframe)
        if df is not None and len(df) > 0:
            strategy_identifier = result.meta.strategies[0] if result.meta.strategies else ""
            detected = legacy_viewer._detect_indicators(strategy_identifier)
            if detected:
                ohlcv_times = [row["time"] for row in ohlcv_by_symbol[primary_symbol]]
                indicators = legacy_viewer._compute_indicators(df, detected, ohlcv_times)

    if indicators and ohlcv_by_symbol.get(primary_symbol):
        symbol_ohlcv = ohlcv_by_symbol[primary_symbol]
        _slice_indicators_to_time_range(
            indicators,
            time_start=symbol_ohlcv[0]["time"],
            time_end=symbol_ohlcv[-1]["time"],
        )

    drawings = _collect_viewer_drawings(result)

    return {
        "trades": trades,
        "ohlcv_by_symbol": ohlcv_by_symbol,
        "timeframe": timeframe,
        "symbols": symbols,
        "primary_symbol": primary_symbol,
        "indicators": indicators,
        "drawings": drawings,
        "missing_ohlcv": missing_ohlcv,
        "total_trades": len(result.trades),
    }


@app.get("/api/backtest/{backtest_id}/ohlcv", response_model=dict[str, Any])
async def get_backtest_ohlcv(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")

    symbols = result.meta.data.get("symbols", [])
    timeframe = (
        result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
    )

    ohlcv_data: dict[str, Any] = {}
    for symbol in symbols[:10]:
        df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
        if df is not None:
            ohlcv_data[symbol] = legacy_viewer._df_to_ohlcv_records(df)
    return ohlcv_data


@app.get("/api/compare", response_model=dict[str, Any])
async def compare_backtests_endpoint(ids: str = Query(default="")) -> dict[str, Any]:
    if not ids:
        raise HTTPException(status_code=400, detail="Provide ?ids=id1,id2,id3")

    backtest_ids = [item.strip() for item in ids.split(",") if item.strip()]
    strategies: list[dict[str, Any]] = []

    for backtest_id in backtest_ids:
        result = _load_result(backtest_id)
        if result is None:
            continue

        try:
            stats = legacy_viewer._canonical_stats_from_result(result)
            equity = legacy_viewer._equity_to_viewer(result)
            eq_pairs = [[point["time"], point["equity"]] for point in equity]
            eq_values = [point["equity"] for point in equity]
            drawdown = legacy_viewer.compute_drawdown_pct(eq_values)
            dd_pairs = [[equity[idx]["time"], round(drawdown[idx], 4)] for idx in range(len(drawdown))]
            strategies.append(
                {
                    "id": backtest_id,
                    "name": (
                        f"{result.meta.strategies[0] if result.meta.strategies else 'unknown'} "
                        f"({result.meta.engine})"
                    ),
                    "equity_curve": eq_pairs,
                    "drawdown_curve": dd_pairs,
                    "stats": stats,
                    "trades": {
                        "total_trades": len(result.trades),
                        "winning_trades": sum(1 for trade in result.trades if trade.pnl > 0),
                        "losing_trades": sum(1 for trade in result.trades if trade.pnl <= 0),
                    },
                }
            )
        except Exception as exc:
            logger.warning("Could not load %s for comparison: %s", backtest_id, exc)

    return legacy_viewer.compare_backtests(strategies)


@app.post("/api/backtests/run", response_model=dict[str, Any])
async def run_backtest(payload: BacktestRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        threading.Thread(target=_dolthub_pull, daemon=True).start()
    except RuntimeError:
        pass

    strategy = str(payload.strategy or "").strip()
    symbols = list(payload.symbols or [])
    if not strategy or not symbols:
        raise HTTPException(status_code=400, detail="strategy and symbols required")

    params = payload.parameters or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="'parameters' must be an object")

    strategy_path = _strategy_path(strategy)
    if strategy_path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy}' not found")

    engine = str(payload.engine or "native")
    timeframe = str(payload.timeframe or "1d")
    start = str(payload.start or "")
    end = str(payload.end or "")
    capital = float(payload.capital or 100000.0)
    risk_pct = payload.risk_per_trade_pct
    max_position_pct = payload.max_position_pct
    max_leverage = payload.max_leverage
    modeling_resolution = str(
        payload.modeling_resolution or payload.intrabar_resolution or "open_close"
    )

    job_id = str(uuid.uuid4())[:8]
    _running_jobs[job_id] = {"status": "running", "result": None, "error": None}

    def _run() -> None:
        try:
            cmd = [
                sys.executable,
                "-m",
                "runner.cli",
                "run",
                "--engine",
                engine,
                "--strategy",
                str(strategy_path),
                "--symbols",
                *symbols,
                "--timeframe",
                timeframe,
                "--capital",
                str(capital),
            ]
            if start:
                cmd.extend(["--start", start])
            if end:
                cmd.extend(["--end", end])
            if params:
                cmd.extend(["--params", json.dumps(params)])
            if risk_pct is not None:
                cmd.extend(["--risk-pct", str(risk_pct)])
            if max_position_pct is not None:
                cmd.extend(["--max-position-pct", str(max_position_pct)])
            if max_leverage is not None:
                cmd.extend(["--max-leverage", str(max_leverage)])
            if modeling_resolution:
                cmd.extend(["--intrabar-resolution", modeling_resolution])

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(PROJECT_ROOT),
            )
            if proc.returncode == 0:
                output = (proc.stdout or "") + (proc.stderr or "")
                _running_jobs[job_id]["status"] = "completed"
                _running_jobs[job_id]["result"] = output
            else:
                _running_jobs[job_id]["status"] = "failed"
                _running_jobs[job_id]["error"] = proc.stderr or proc.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            _running_jobs[job_id]["status"] = "failed"
            _running_jobs[job_id]["error"] = str(exc)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/backtests/run/{job_id}/status", response_model=dict[str, Any])
async def get_backtest_run_status(job_id: str) -> dict[str, Any]:
    job = _running_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(job)


@app.post("/api/optimizations/run", response_model=dict[str, Any], status_code=202)
async def run_optimization_job(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        config = OptimizationConfig.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid optimization payload", "details": exc.errors()},
        ) from exc

    task_id = str(uuid.uuid4())[:8]
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name="Parameter optimization",
        description=(
            f"Optimize {config.base_backtest_id} for {config.target_metric} "
            f"across {config.n_trials} trials."
        ),
        kind="optimization",
        source="optuna",
        total_items=config.n_trials,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    def _run() -> None:
        optuna_logger = logging.getLogger("optuna")
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            log_handler = _ThreadBoundTaskLogHandler(
                task_id=task_id,
                handle=handle,
                source="optimization",
            )
            log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
            optuna_logger.addHandler(log_handler)
            try:
                _set_task_status(task_id, "running")
                _write_task_log_line(
                    handle,
                    task_id,
                    (
                        f"[{_task_timestamp()}] Starting optimization for "
                        f"{config.base_backtest_id} ({config.target_metric})."
                    ),
                    stream="task",
                    source="optimization",
                )

                def report_progress(percent: float, message: str) -> None:
                    _update_task_progress(
                        task_id,
                        percent=percent,
                        current=message,
                        total=config.n_trials,
                    )
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="task",
                        source="optimization",
                    )

                optimization_payload = execute_optimization(config, progress_callback=report_progress)
                _update_task_fields(
                    task_id,
                    optimization_id=optimization_payload.get("id"),
                    optimization_file=optimization_payload.get("file"),
                )
                _write_task_log_line(
                    handle,
                    task_id,
                    (
                        f"[{_task_timestamp()}] Optimization saved to "
                        f"{optimization_payload.get('file', '')}."
                    ),
                    stream="task",
                    source="optimization",
                )
                _mark_task_finished(task_id, status="completed")
            except Exception as exc:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Optimization failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="optimization",
                )
                _write_task_traceback(handle, task_id, traceback.format_exc(), source="optimization")
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            finally:
                optuna_logger.removeHandler(log_handler)

    thread = threading.Thread(target=_run, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"job_id": task_id, "status": "starting"}


@app.get("/api/optimizations", response_model=list[dict[str, Any]])
async def list_optimizations() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not OPTIMIZATIONS_DIR.exists():
        return results

    for path in sorted(OPTIMIZATIONS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results.append(_optimization_summary_payload(payload, path))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("Could not read optimization %s: %s", path, exc)

    results.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    return results


@app.get("/api/optimizations/{optimization_id}", response_model=dict[str, Any])
async def get_optimization(optimization_id: str) -> dict[str, Any]:
    try:
        payload = _load_optimization_payload(optimization_id)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.exception("Failed to load optimization '%s': %s", optimization_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if payload is None:
        raise HTTPException(status_code=404, detail=f"Optimization '{optimization_id}' not found")
    return payload


@app.get("/api/portfolios/compare", response_model=dict[str, Any])
async def compare_portfolios(ids: str = Query(default="")) -> dict[str, Any]:
    if not ids:
        raise HTTPException(status_code=400, detail="Provide ?ids=id1,id2")

    portfolio_ids = [item.strip() for item in ids.split(",") if item.strip()]
    portfolios: list[dict[str, Any]] = []
    for portfolio_id in portfolio_ids:
        path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
        if not path.exists():
            continue
        portfolios.append(json.loads(path.read_text(encoding="utf-8")))
    return {"portfolios": portfolios}


@app.get("/api/portfolios", response_model=list[dict[str, Any]])
async def list_portfolios() -> list[dict[str, Any]]:
    portfolios: list[dict[str, Any]] = []
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    for file_path in sorted(PORTFOLIOS_DIR.glob("*.json")):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            stats = legacy_viewer.PortfolioStats.from_dict(data.get("combined_stats", {})).to_dict()

            equity_full = data.get("combined_equity", [])
            if equity_full:
                values = [
                    point.get("equity", point) if isinstance(point, dict) else point
                    for point in equity_full
                ]
                count = len(values)
                if count <= 50:
                    equity_mini = values
                else:
                    step = count / 50
                    equity_mini = [values[int(index * step)] for index in range(50)]
                    equity_mini.append(values[-1])
            else:
                equity_mini = []

            cagr_raw = stats.get("cagr", 0) or 0
            max_drawdown_raw = stats.get("max_drawdown", 0) or 0
            net_profit = stats.get("net_profit", 0) or 0
            capital = data.get("account_capital", 100000) or 100000

            portfolios.append(
                {
                    "id": data.get("id", file_path.stem),
                    "name": data.get("name", ""),
                    "account_capital": capital,
                    "strategy_count": len(data.get("strategies", [])),
                    "sharpe": round(stats.get("sharpe", 0) or 0, 3),
                    "cagr": round(cagr_raw * 100, 2),
                    "max_drawdown_pct": round(abs(max_drawdown_raw) * 100, 2),
                    "net_profit": round(net_profit, 2),
                    "net_profit_pct": round(net_profit / capital * 100, 2) if capital else 0,
                    "psr": stats.get("psr"),
                    "p_value": round(_safe_float(stats.get("p_value"), 1.0), 6),
                    "equity_mini": equity_mini,
                    "created_at": data.get("created_at", ""),
                }
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError) as exc:
            logger.warning("Could not read portfolio %s: %s", file_path.name, exc)
    return portfolios


@app.post("/api/portfolios", response_model=dict[str, Any], status_code=201)
async def create_portfolio(payload: CreatePortfolioRequest) -> dict[str, Any]:
    name = str(payload.name or "Untitled")
    account_capital = float(payload.account_capital or 100000.0)
    strategy_configs = [item.model_dump() for item in payload.strategies]

    if not strategy_configs:
        raise HTTPException(status_code=400, detail="At least one strategy required")

    strategy_returns = []
    strategy_info: list[dict[str, Any]] = []
    all_trades_timed: list[dict[str, Any]] = []

    for strategy_config in strategy_configs:
        backtest_id = strategy_config.get("backtest_id", "")
        strategy_name_input = strategy_config.get("name", "")
        risk_factor = strategy_config.get("risk_per_trade_pct", 1.0)

        if not backtest_id and strategy_name_input and BACKTESTS_DIR.exists():
            strategy_match = _safe_strategy_entry(strategy_name_input)
            normalized_input = strategy_name_input.lower().replace("_", " ").replace("-", " ")
            candidates: list[tuple[str, str]] = []
            for file_path in BACKTESTS_DIR.glob("*.trades.json"):
                try:
                    payload_data = json.loads(file_path.read_text(encoding="utf-8"))
                    meta = payload_data.get("meta", {})
                    strategies = meta.get("strategies", [])
                    matches_input = any(
                        str(strategy_name).lower().replace("_", " ").replace("-", " ")
                        == normalized_input
                        for strategy_name in strategies
                    )
                    if strategy_match is not None:
                        matches_input = any(
                            matches_strategy_identifier(strategy_match, strategy_name)
                            for strategy_name in strategies
                        )
                    if matches_input:
                        candidates.append((meta.get("created_at", ""), file_path.stem.replace(".trades", "")))
                except (json.JSONDecodeError, KeyError, OSError):
                    continue
            if candidates:
                candidates.sort(reverse=True)
                backtest_id = candidates[0][1]
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"No backtest found for strategy '{strategy_name_input}'",
                )

        result = _load_result(backtest_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

        equity_curve = _ensure_equity_curve(result, account_capital)
        if not equity_curve:
            continue

        strategy_name = result.meta.strategies[0] if result.meta.strategies else backtest_id
        strategy_info.append(
            {
                "backtest_id": backtest_id,
                "strategy_name": strategy_name,
                "risk_per_trade_pct": risk_factor,
                "max_positions": strategy_config.get("max_positions", 99),
            }
        )

        equity_df = pd.DataFrame([point.to_dict() for point in equity_curve])
        equity_df["time"] = pd.to_datetime(equity_df["time"], utc=True)
        equity_df.set_index("time", inplace=True)
        daily_equity = equity_df["equity"].resample("D").last().ffill()
        daily_returns = daily_equity.pct_change().fillna(0.0)

        weighted_returns = daily_returns * risk_factor
        weighted_returns.name = strategy_name
        strategy_returns.append(weighted_returns)

        for trade in result.trades:
            all_trades_timed.append(
                {
                    "time": trade.last_exit_time,
                    "pnl": round(trade.pnl * risk_factor, 2),
                    "strategy": strategy_name,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                }
            )

    if not strategy_returns:
        raise HTTPException(status_code=400, detail="No valid strategies found")

    combined_df = pd.concat(strategy_returns, axis=1).fillna(0.0)
    portfolio_daily_returns = combined_df.sum(axis=1)
    if len(combined_df.columns) > 1:
        correlation_matrix = combined_df.corr().round(4).fillna(0.0).to_dict()
    else:
        correlation_matrix = {column: {column: 1.0} for column in combined_df.columns}

    current_equity = account_capital
    combined_equity_points = []
    for timestamp, daily_return in portfolio_daily_returns.items():
        current_equity *= 1 + daily_return
        combined_equity_points.append(
            legacy_viewer.EquityPoint(time=timestamp.isoformat(), equity=current_equity)
        )

    stats_result = legacy_viewer.compute_stats(
        trades=[],
        initial_capital=account_capital,
        equity_curve=combined_equity_points,
    )

    total_pnl = current_equity - account_capital
    contributions: list[dict[str, Any]] = []
    for column in combined_df.columns:
        strategy_equity = account_capital
        for daily_return in combined_df[column]:
            strategy_equity *= 1 + daily_return
        strategy_pnl = strategy_equity - account_capital
        contributions.append(
            {
                "strategy_name": column,
                "pnl_contribution": round(strategy_pnl, 2),
                "pnl_contribution_pct": 0,
            }
        )

    for contribution in contributions:
        if total_pnl != 0:
            contribution["pnl_contribution_pct"] = round(
                contribution["pnl_contribution"] / total_pnl * 100,
                1,
            )
        else:
            contribution["pnl_contribution_pct"] = 0

    now = datetime.now(timezone.utc)
    portfolio_id = f"{name.lower().replace(' ', '-')}-{now.strftime('%Y%m%d%H%M%S')}"
    combined_equity = [point.to_dict() for point in combined_equity_points]
    all_trades_timed.sort(key=lambda trade: trade.get("time", ""))

    portfolio = {
        "id": portfolio_id,
        "name": name,
        "created_at": now.isoformat(),
        "account_capital": account_capital,
        "strategies": strategy_info,
        "combined_stats": legacy_viewer.PortfolioStats(
            sharpe=stats_result.get("sharpe_ratio", 0),
            sortino=stats_result.get("sortino_ratio", 0),
            cagr=stats_result.get("cagr", 0),
            max_drawdown=stats_result.get("max_drawdown_pct", 0),
            net_profit=stats_result.get("total_pnl", 0),
            win_rate=stats_result.get("win_rate", 0),
            profit_factor=stats_result.get("profit_factor", 0),
            calmar=stats_result.get("calmar_ratio", 0),
            total_return=stats_result.get("total_return", 0),
            max_drawdown_duration=stats_result.get("max_drawdown_duration", 0),
            psr=stats_result.get("psr"),
            p_value=stats_result.get("p_value", 1.0),
            correlation_matrix=correlation_matrix,
        ).to_dict(),
        "combined_equity": combined_equity,
        "strategy_contributions": contributions,
        "trades": all_trades_timed,
    }

    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    output_path.write_text(json.dumps(portfolio, indent=2), encoding="utf-8")
    return portfolio


@app.get("/api/portfolios/{portfolio_id}", response_model=dict[str, Any])
async def get_portfolio(portfolio_id: str) -> dict[str, Any]:
    path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Portfolio not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["combined_stats"] = legacy_viewer.PortfolioStats.from_dict(
        payload.get("combined_stats", {})
    ).to_dict()
    return payload


@app.delete("/api/portfolios/{portfolio_id}", response_model=dict[str, Any])
async def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Portfolio not found")
    path.unlink()
    return {"status": "deleted"}


@app.get("/api/data/overview", response_model=dict[str, Any])
async def data_overview() -> dict[str, Any]:
    all_entries = _load_data_entries()

    categories: dict[str, dict[str, Any]] = {}
    for entry in all_entries:
        category = _categorize_path(str(entry.get("path", "")))
        categories.setdefault(
            category,
            {"count": 0, "size_mb": 0.0, "symbols": set(), "start": None, "end": None},
        )
        categories[category]["count"] += 1

        size = entry.get("size_bytes", 0)
        if size == 0:
            try:
                entry_path = Path(entry.get("path", ""))
                if entry_path.exists():
                    size = entry_path.stat().st_size
            except OSError:
                pass
        categories[category]["size_mb"] += size / (1024 * 1024)
        categories[category]["symbols"].add(entry.get("symbol"))

        start_value = entry.get("start")
        end_value = entry.get("end")
        if start_value and (
            categories[category]["start"] is None or start_value < categories[category]["start"]
        ):
            categories[category]["start"] = start_value
        if end_value and (categories[category]["end"] is None or end_value > categories[category]["end"]):
            categories[category]["end"] = end_value

    dolthub_repos = _scan_dolthub_repos()
    if dolthub_repos:
        categories["Dolthub Repos"] = {
            "count": len(dolthub_repos),
            "size_mb": sum(repo.get("size_bytes", 0) for repo in dolthub_repos) / (1024 * 1024),
            "symbols": {repo.get("repo") for repo in dolthub_repos},
            "start": None,
            "end": None,
        }

    result: list[dict[str, Any]] = []
    for name, data in sorted(categories.items()):
        result.append(
            {
                "name": name,
                "symbol_count": len(data["symbols"]),
                "file_count": data["count"],
                "total_size_mb": round(data["size_mb"], 2),
                "start": data["start"],
                "end": data["end"],
            }
        )
    return {"categories": result}


@app.get("/api/data/category/{category_name}", response_model=dict[str, Any])
async def data_category_details(category_name: str) -> dict[str, Any]:
    target = category_name.lower()
    if target == "dolthub repos":
        repos = []
        for repo in _scan_dolthub_repos():
            validation, validation_message = _dolthub_repo_status(Path(repo.get("path", "")))
            repos.append(
                {
                    "symbol": repo.get("repo"),
                    "timeframe": "repo",
                    "path": repo.get("path"),
                    "size_mb": round(repo.get("size_bytes", 0) / (1024 * 1024), 2),
                    "start": None,
                    "end": None,
                    "source": "dolthub",
                    "repo": True,
                    "last_updated": repo.get("last_updated"),
                    "validation": validation,
                    "warning_message": validation_message,
                    "validation_message": validation_message,
                }
            )
        return {"symbols": sorted(repos, key=lambda item: item["symbol"] or "")}

    all_entries = _load_data_entries()

    symbols: list[dict[str, Any]] = []
    for entry in all_entries:
        category = _categorize_path(str(entry.get("path", ""))).lower()
        if category != target:
            continue

        size = entry.get("size_bytes", 0)
        if size == 0:
            try:
                entry_path = Path(entry.get("path", ""))
                if entry_path.exists():
                    size = entry_path.stat().st_size
            except OSError:
                pass

        symbol_entry: dict[str, Any] = {
            "symbol": entry.get("symbol"),
            "timeframe": entry.get("timeframe"),
            "path": entry.get("path"),
            "size_mb": round(size / (1024 * 1024), 2),
            "start": entry.get("start"),
            "end": entry.get("end"),
            "source": entry.get("source"),
        }
        if entry.get("validation") is not None:
            symbol_entry["validation"] = entry.get("validation")
        if entry.get("error") is not None:
            symbol_entry["error"] = entry.get("error")
        warning_message = _warning_message_from_entry(entry)
        if warning_message is not None:
            symbol_entry["warning_message"] = warning_message
            symbol_entry["validation_message"] = warning_message
        if entry.get("repo"):
            symbol_entry["repo"] = True
        if entry.get("last_updated") is not None:
            symbol_entry["last_updated"] = entry.get("last_updated")
        symbols.append(symbol_entry)

    return {
        "symbols": sorted(
            symbols,
            key=lambda item: (item.get("symbol") or "", item.get("timeframe") or ""),
        )
    }


@app.post("/api/data/download", response_model=dict[str, Any])
async def data_download(payload: DataDownloadRequest) -> dict[str, Any]:
    source = payload.source
    symbols = payload.symbols
    timeframes = payload.timeframes
    start = payload.start
    end = payload.end
    price_type = payload.price_type
    merge_tick = bool(payload.merge_tick or payload.merge_bid_ask)
    merge_s1 = bool(payload.merge_s1 or payload.merge_bid_ask)
    batch_size = _safe_int(payload.batch_size if payload.batch_size is not None else payload.concurrency, 10)
    pause_ms = _safe_int(payload.pause_ms, 1000)
    symbol_list = _parse_symbol_list(symbols)
    timeframe_list = [
        _normalize_dukascopy_genesis_timeframe(timeframe)
        for timeframe in _parse_timeframe_list(timeframes)
    ]

    if batch_size < 1 or batch_size > 50:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 50")
    if pause_ms < 0 or pause_ms > 60000:
        raise HTTPException(status_code=400, detail="pause_ms must be between 0 and 60000")

    if source == "dukascopy" and start:
        genesis_index = _load_dukascopy_genesis()
        too_early: dict[str, str] = {}
        for symbol in symbol_list:
            symbol_data = genesis_index.get(symbol, {})
            for timeframe in timeframe_list:
                genesis = symbol_data.get(timeframe) or symbol_data.get("default")
                if genesis and start < genesis:
                    too_early[f"{symbol}/{timeframe}"] = genesis
        if too_early:
            details = ", ".join(
                f"{symbol_tf} >= {genesis}" for symbol_tf, genesis in sorted(too_early.items())
            )
            raise HTTPException(
                status_code=400,
                detail=f"Start date {start} precedes Dukascopy genesis for: {details}",
            )

    task_id = str(uuid.uuid4())
    name, description = _download_task_labels(source, symbol_list, timeframe_list)

    if source == "dukascopy":
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.dukascopy_native",
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
            "--batch-size",
            str(batch_size),
            "--pause-ms",
            str(pause_ms),
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
        if price_type:
            cmd.extend(["--price-type", str(price_type)])
        if merge_tick:
            cmd.append("--merge-tick")
        if merge_s1:
            cmd.append("--merge-s1")

        total_items = max(len(symbol_list) * len(timeframe_list), 1)
        return _start_dukascopy_task(
            task_id,
            cmd,
            symbols=symbol_list,
            timeframes=timeframe_list,
            start=start or None,
            end=end or None,
            price_type=str(price_type),
            merge_tick=merge_tick,
            merge_s1=merge_s1,
            batch_size=batch_size,
            pause_ms=pause_ms,
            total_items=total_items,
            name=name,
            description=description,
        )

    if source == "yfinance":
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.yfinance_dl",
            "--symbols",
            symbols,
        ]
    elif source == "dolthub":
        cmd = [sys.executable, "-u", "-m", "data_tools.dolthub_manager", "update"]
    else:
        raise HTTPException(status_code=400, detail="Unknown source")

    total_items = max(len(symbol_list) * len(timeframe_list), 1)
    return _start_data_task(
        task_id,
        cmd,
        total_items=total_items,
        name=name,
        description=description,
        source=source,
    )


@app.post("/api/agent/convert", response_model=dict[str, Any], status_code=202)
async def agent_convert(payload: AgentConvertRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    code = str(payload.code or "")
    if not code.strip():
        raise HTTPException(status_code=400, detail="A non-empty 'code' field is required")

    model = str(payload.model or "").strip() or DEFAULT_MODEL_NAME
    if model not in SUPPORTED_MODEL_NAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported model '{model}'")

    task_payload = {
        "code": code,
        "strategy_name": str(payload.strategy_name or "").strip() or None,
        "engine": str(payload.engine or "").strip().lower() or None,
        "model": model,
        "symbols": (
            [str(symbol).strip().upper() for symbol in payload.symbols if str(symbol).strip()]
            if isinstance(payload.symbols, list)
            else None
        ),
        "timeframe": str(payload.timeframe or "").strip().lower() or None,
        "start": str(payload.start or "").strip() or None,
        "end": str(payload.end or "").strip() or None,
        "capital": payload.capital,
    }

    task_id = str(uuid.uuid4())
    queued = _queue_agent_task(task_id, task_payload)
    background_tasks.add_task(_launch_agent_task, task_id, task_payload)
    return queued


@app.get("/api/data/dukascopy/genesis", response_model=dict[str, Any])
async def get_dukascopy_genesis(
    symbols: str = Query(default=""),
    timeframes: str = Query(default=""),
) -> dict[str, Any]:
    requested_symbols = _parse_symbol_list(symbols)
    requested_timeframes = _parse_timeframe_list(timeframes)
    genesis_index = _load_dukascopy_genesis()
    filtered_genesis, lower_bound = _collect_dukascopy_genesis(
        genesis_index,
        symbols=requested_symbols or None,
        timeframes=requested_timeframes or None,
    )
    return {
        "source": "dukascopy",
        "genesis": filtered_genesis,
        "lower_bound": lower_bound,
    }


@app.post("/api/data/resample", response_model=dict[str, Any])
async def data_resample(payload: DataResampleRequest) -> dict[str, Any]:
    source_path = payload.source_path
    target_timeframes = payload.target_timeframes
    task_id = str(uuid.uuid4())
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "data_tools.resample",
        "--source",
        source_path,
        "--timeframes",
        target_timeframes,
    ]
    return _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Resample data",
        description=f"Resample {Path(source_path).name or source_path} into {target_timeframes}.",
        source="resample",
    )


@app.post("/api/data/validate", response_model=dict[str, Any])
async def data_validate(payload: DataValidateRequest) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    cmd = [sys.executable, "-u", "-m", "data_tools.validate", "--data-dir", str(DATA_DIR)]
    if payload.symbol:
        cmd.extend(["--symbol", payload.symbol])
    if payload.report:
        cmd.extend(["--report", payload.report])
    return _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Validate data",
        description="Validate OHLCV data quality and structure.",
        source="validate",
    )


@app.post("/api/data/calibrate", response_model=dict[str, Any])
async def data_calibrate(payload: DataCalibrateRequest) -> dict[str, Any]:
    symbols = str(payload.symbols or "").strip()
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols is required")

    task_id = str(uuid.uuid4())
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "data_tools.dukascopy_discovery",
        "--symbols",
        symbols,
        "--price-type",
        payload.price_type,
        "--start-year",
        str(payload.start_year),
        "--min-year",
        str(payload.min_year),
    ]
    if payload.timeframes:
        cmd.extend(["--timeframes", payload.timeframes])
    if payload.force:
        cmd.append("--force")
    if payload.verbose:
        cmd.append("--verbose")

    return _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Calibrate Dukascopy genesis",
        description=f"Discover genesis boundaries for {symbols}.",
        source="calibrate",
    )


@app.get("/api/data/tasks", response_model=dict[str, Any])
async def list_data_tasks() -> dict[str, Any]:
    with DATA_TASKS_LOCK:
        snapshots = [(task_id, dict(task)) for task_id, task in DATA_TASKS.items()]
    payload = [
        _serialize_task(task_id, task, include_log=True, max_log_bytes=768)
        for task_id, task in snapshots
    ]
    payload.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        )
    )
    return {"tasks": payload}


@app.get("/api/data/tasks/{task_id}", response_model=dict[str, Any])
async def get_data_task(task_id: str) -> dict[str, Any]:
    task = _snapshot_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _serialize_task(task_id, task, include_log=True)


@app.post("/api/data/tasks/{task_id}/pause", response_model=dict[str, Any])
async def pause_data_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be paused")
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_payload(task_id)

    task["pause_event"].set()
    if task.get("control_mode") == "process":
        stop_signal = getattr(signal, "SIGSTOP", None)
        if stop_signal is not None:
            _signal_process(task, stop_signal)
    _set_task_status(task_id, "paused")
    _task_log_message(task_id, "Task paused by user.")
    return _task_action_payload(task_id)


@app.post("/api/data/tasks/{task_id}/resume", response_model=dict[str, Any])
async def resume_data_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be resumed")
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_payload(task_id)

    task["pause_event"].clear()
    if task.get("control_mode") == "process":
        continue_signal = getattr(signal, "SIGCONT", None)
        if continue_signal is not None:
            _signal_process(task, continue_signal)
    _set_task_status(task_id, "running")
    _task_log_message(task_id, "Task resumed by user.")
    return _task_action_payload(task_id)


@app.post("/api/data/tasks/{task_id}/cancel", response_model=dict[str, Any])
async def cancel_data_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be cancelled")
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_payload(task_id)

    task["stop_event"].set()
    task["pause_event"].clear()
    if task.get("control_mode") == "process":
        process = task.get("process")
        if process is not None and process.poll() is None:
            process.kill()
    _set_task_status(task_id, "cancelled")
    _task_log_message(task_id, "Task cancelled by user.")
    return _task_action_payload(task_id)


@app.get("/api/data/tasks/{task_id}/logs", response_model=dict[str, Any])
async def get_data_task_logs(task_id: str) -> dict[str, Any]:
    task = _snapshot_task(task_id)
    log_file = _task_log_path(task_id)
    if task is None and not log_file.exists():
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "logs": _read_task_log(task.get("log_file", log_file) if task else log_file),
        "status": task.get("status", "unknown") if task else "unknown",
    }


@app.delete("/api/data/tasks/{task_id}", response_model=dict[str, Any])
async def delete_data_task(task_id: str) -> dict[str, Any]:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("status") not in FINAL_TASK_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Only completed, failed, or cancelled tasks can be removed",
            )
        del DATA_TASKS[task_id]
    return {"task_id": task_id, "status": "deleted"}


@app.delete("/api/data/symbol", response_model=dict[str, Any])
async def delete_symbol_data(path: str = Query(default="")) -> dict[str, Any]:
    if not path:
        raise HTTPException(status_code=400, detail="Path required")

    file_path = Path(path)
    try:
        file_path = file_path.resolve()
        data_root = DATA_DIR.resolve()
        if not str(file_path).startswith(str(data_root)):
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        _refresh_catalog()
        return {"status": "deleted"}
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/logs", response_model=list[LogFileModel])
async def list_logs() -> list[LogFileModel]:
    log_files = legacy_viewer._available_log_files()
    total_size = sum(path.stat().st_size for path in log_files) if log_files else 0
    latest_modified = (
        max(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) for path in log_files).isoformat()
        if log_files
        else None
    )

    logs: list[LogFileModel] = [
        LogFileModel(name=ALL_LOGS_NAME, size_bytes=total_size, modified=latest_modified)
    ]
    for log_file in log_files:
        logs.append(
            LogFileModel(
                name=log_file.name,
                size_bytes=log_file.stat().st_size,
                modified=datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc).isoformat(),
            )
        )
    return logs


@app.get("/api/logs/{log_name}", response_model=LogContentModel)
async def get_log(
    log_name: str,
    lines: int = Query(default=200, ge=1, le=5000),
    level: str = Query(default=""),
    search: str = Query(default=""),
) -> LogContentModel:
    if log_name == ALL_LOGS_NAME:
        paths = legacy_viewer._available_log_files()
    else:
        try:
            path = (LOG_DIR / log_name).resolve()
            if not path.is_relative_to(LOG_DIR.resolve()):
                raise HTTPException(status_code=403, detail="Access denied")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid log path") from exc
        paths = [path] if path.exists() else []

    if log_name != ALL_LOGS_NAME and not paths:
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        entries = legacy_viewer._collect_log_entries(paths)
        if log_name == ALL_LOGS_NAME:
            entries.sort(
                key=lambda item: (
                    item["timestamp"] is None,
                    item["timestamp"] or datetime.max.replace(tzinfo=timezone.utc),
                    item["path"],
                    item["line_number"],
                )
            )

        level_filter = legacy_viewer._normalize_log_levels(level)
        if level_filter:
            entries = [entry for entry in entries if entry.get("level") in level_filter]

        if search:
            query_lower = search.lower()
            entries = [entry for entry in entries if query_lower in entry["line"].lower()]

        total_lines = len(entries)
        recent_entries = entries[-lines:] if len(entries) > lines else entries
        return LogContentModel(
            name=log_name,
            lines=[entry["line"] for entry in recent_entries],
            total_lines=total_lines,
        )
    except Exception as exc:
        logger.exception("Failed to read log '%s': %s", log_name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/symbols", response_model=SymbolsResponse)
async def list_symbols() -> SymbolsResponse:
    datasets = _load_data_entries()
    if not datasets:
        return SymbolsResponse(symbols=[], categories={})

    categories: dict[str, list[str]] = {}
    seen: set[str] = set()
    for dataset in datasets:
        symbol = str(dataset.get("symbol", "") or "")
        if not symbol or symbol in seen:
            continue

        seen.add(symbol)
        category = _infer_symbol_category(
            str(dataset.get("path", "") or ""), str(dataset.get("asset_class", "other") or "other")
        )
        categories.setdefault(category, []).append(symbol)

    for category in categories:
        categories[category].sort()

    return SymbolsResponse(symbols=sorted(seen), categories=categories)


@app.get("/api/benchmark/{symbol}", response_model=BenchmarkResponse)
async def get_benchmark(
    symbol: str,
    start: str = Query(default=""),
    end: str = Query(default=""),
    timeframe: str = Query(default="1d"),
) -> BenchmarkResponse:
    df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
    if df is None and timeframe == "1d":
        for fallback_tf in ["4h", "1h", "15m"]:
            df = legacy_viewer._load_ohlcv_df(symbol, fallback_tf)
            if df is None:
                continue

            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            df = (
                df.resample("1D")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna()
                .reset_index()
            )
            df.rename(columns={"index": "timestamp"} if "index" in df.columns else {}, inplace=True)
            if "timestamp" not in df.columns and df.index.name == "timestamp":
                df = df.reset_index()
            timeframe = "1d"
            logger.info("Benchmark %s: using %s resampled to 1d", symbol, fallback_tf)
            break

    if df is None:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}/{timeframe}")

    if start and "timestamp" in df.columns:
        df = df[df["timestamp"] >= start]
    if end and "timestamp" in df.columns:
        df = df[df["timestamp"] <= end]
    if df.empty:
        raise HTTPException(status_code=404, detail="No data in range")

    initial = 100000.0
    first_close = float(df.iloc[0]["close"])
    shares = initial / first_close

    equity: list[BenchmarkPoint] = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        unix_ts = ts.timestamp() if hasattr(ts, "timestamp") else pd.Timestamp(ts).timestamp()
        equity.append(BenchmarkPoint(time=float(unix_ts), equity=round(float(row["close"]) * shares, 2)))

    return BenchmarkResponse(symbol=symbol, timeframe=timeframe, equity=equity)


@app.websocket("/ws/tasks/{job_id}")
async def task_log_socket(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()

    log_path = TASK_LOGS_DIR / f"task_{job_id}.log"
    position = 0
    idle_cycles = 0

    try:
        while True:
            if log_path.exists():
                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()

                if chunk:
                    for line in chunk.splitlines():
                        await websocket.send_text(line)
                idle_cycles = 0
            else:
                idle_cycles += 1

            status = _task_status(job_id)
            if status in FINAL_TASK_STATUSES:
                await websocket.send_text(f"[task:{job_id}] status={status}")
                await websocket.close()
                return

            if idle_cycles > 120 and status is None:
                await websocket.send_text(f"[task:{job_id}] no log stream available")
                await websocket.close(code=1000)
                return

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return

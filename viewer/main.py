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
import time
import traceback
import uuid
from contextlib import asynccontextmanager, suppress
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
from starlette.middleware.gzip import GZipMiddleware

from data_tools.catalog_builder import merge_ohlcv_entries
from data_tools import cache_manager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
# Avoid pynecore TOML auto-writes at import-time in the web process.
os.environ.setdefault("PYNE_SAVE_SCRIPT_TOML", "0")

from shared.constants import DATA_DIR as RESOLVED_DATA_DIR, LOG_DIR as RESOLVED_LOG_DIR

DATA_DIR = RESOLVED_DATA_DIR
os.environ.setdefault("DATA_DIR", str(DATA_DIR))
os.environ.setdefault("TRADING_DATA_DIR", str(DATA_DIR))

LOG_DIR = RESOLVED_LOG_DIR
os.environ.setdefault("LOG_DIR", str(LOG_DIR))

from shared.logging import get_logger
from shared.models import BacktestResult, EquityPoint
from shared.database import (
    create_live_deployment,
    delete_background_task_record,
    delete_backtest,
    delete_live_deployment,
    ensure_live_account,
    get_background_task_record,
    get_live_account,
    init_db,
    list_backtest_ids,
    list_backtest_fingerprint_rows,
    list_backtest_metadata_rows,
    list_background_task_records,
    list_live_accounts,
    list_live_balances,
    list_live_deployments,
    list_live_deployments_for_account,
    list_live_signal_executions_for_deployments,
    load_backtest_metadata_row,
    load_backtest_equity_preview_points,
    load_backtest_equity_preview_map,
    load_live_account_stats_inputs,
    load_backtest_result,
    mark_background_tasks_interrupted,
    migrate_json_to_sqlite,
    save_result_to_sqlite,
    upsert_background_task_record,
    update_live_deployment_status,
)
try:
    from live_runner.account_discovery import discover_configured_accounts
except ModuleNotFoundError:  # pragma: no cover - optional dependency for account discovery
    async def discover_configured_accounts() -> list[dict[str, Any]]:
        return []
from shared.models import LiveDeploymentConfig, LiveDeploymentCreateRequest, LiveDeploymentStatusUpdateRequest
from runner.capm import align_return_series, calculate_capm
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
_start_jforex_task = legacy_viewer._start_jforex_task
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

_LEGACY_CREATE_TASK_RECORD = _create_task_record
_LEGACY_SET_TASK_STATUS = _set_task_status
_LEGACY_UPDATE_TASK_PROGRESS = _update_task_progress
_LEGACY_UPDATE_TASK_FIELDS = _update_task_fields
_LEGACY_MARK_TASK_FINISHED = _mark_task_finished
_LEGACY_SNAPSHOT_TASK = _snapshot_task
_LEGACY_GET_TASK = _get_task
_LEGACY_SERIALIZE_TASK = _serialize_task

init_db()
migrate_json_to_sqlite()

async def _prewarm_data_views_background() -> None:
    try:
        await asyncio.to_thread(_prewarm_data_views_once)
    except Exception as exc:
        logger.warning("Data views prewarm failed: %s", exc)


async def _prewarm_live_accounts_background() -> None:
    try:
        await asyncio.to_thread(_cached_live_accounts_payload, force_refresh=True)
    except Exception as exc:
        logger.warning("Live accounts prewarm failed: %s", exc)


async def _prewarm_strategies_background() -> None:
    try:
        await asyncio.to_thread(_cached_strategies_payload, force_refresh=True)
    except Exception as exc:
        logger.warning("Strategies prewarm failed: %s", exc)


async def _prewarm_backtest_metadata_background() -> None:
    try:
        count = await asyncio.to_thread(_refresh_backtest_meta_cache)
        logger.info("Backtest metadata cache warmed (%s items)", count)
    except Exception as exc:
        logger.warning("Backtest metadata prewarm failed: %s", exc)


async def _prewarm_jforex_jvm_background() -> None:
    try:
        from data_tools.downloaders import jforex_dl

        await asyncio.to_thread(jforex_dl.prewarm_jvm)
        logger.info("JVM prewarmed for JForex")
    except ImportError:
        logger.info("JPype not available, JForex disabled")
    except RuntimeError as exc:
        message = str(exc).lower()
        if (
            "jpype is required" in message
            or "failed to import 'com.dukascopy'" in message
            or "jforex sdk classpath" in message
            or "failed to start jvm" in message
        ):
            logger.info("JPype not available, JForex disabled")
            return
        logger.warning("JForex JVM prewarm failed: %s", exc)
    except Exception as exc:
        logger.warning("JForex JVM prewarm failed: %s", exc)


def _resume_interrupted_tasks() -> None:
    records = list_background_task_records(statuses=["interrupted", "queued"])
    for record in records:
        try:
            task_id = str(record.get("id") or "").strip()
            if not task_id:
                continue

            task_type = str(record.get("type") or "")
            raw_config = dict(record.get("config_json") or {})
            restart_config = raw_config.get("restart_config")
            backtest_config = dict(restart_config) if isinstance(restart_config, dict) else dict(raw_config)

            if task_type == "backtest":
                strategy = str(backtest_config.get("strategy") or "").strip()
                symbols = backtest_config.get("symbols")
                if not strategy or not isinstance(symbols, list) or not symbols:
                    logger.warning(
                        "Skipping auto-recovery for task %s: incomplete backtest restart config",
                        task_id,
                    )
                    continue

                logger.info(
                    "Auto-restarting backtest task %s (%s %s %s)",
                    task_id,
                    strategy,
                    ",".join(str(symbol) for symbol in symbols),
                    str(backtest_config.get("timeframe") or "1d"),
                )
                request_payload = BacktestRunRequest(
                    strategy=strategy,
                    engine=str(backtest_config.get("engine") or "native"),
                    symbols=[str(symbol) for symbol in symbols if str(symbol).strip()],
                    timeframe=str(backtest_config.get("timeframe") or "1d"),
                    start=str(backtest_config.get("start") or ""),
                    end=str(backtest_config.get("end") or ""),
                    capital=float(backtest_config.get("capital") or 100000.0),
                    risk_per_trade_pct=backtest_config.get("risk_per_trade_pct"),
                    max_position_pct=backtest_config.get("max_position_pct"),
                    max_leverage=backtest_config.get("max_leverage"),
                    modeling_resolution=(
                        str(backtest_config.get("modeling_resolution"))
                        if backtest_config.get("modeling_resolution") is not None
                        else None
                    ),
                    intrabar_resolution=(
                        str(backtest_config.get("intrabar_resolution"))
                        if backtest_config.get("intrabar_resolution") is not None
                        else None
                    ),
                    parameters=dict(backtest_config.get("parameters") or {}),
                )
                restarted = asyncio.run(run_backtest(request_payload))
                restarted_task_id = restarted.get("task_id") if isinstance(restarted, dict) else None
                upsert_background_task_record(
                    task_id=task_id,
                    task_type=task_type,
                    status="completed",
                    config_json=raw_config,
                    progress_message=(
                        f"Auto-restarted as task {restarted_task_id}"
                        if restarted_task_id
                        else "Auto-restarted as new task"
                    ),
                    name=str(record.get("name") or ""),
                    description=str(record.get("description") or ""),
                    source=str(record.get("source") or ""),
                    created_at=str(record.get("created_at") or ""),
                    updated_at=_now_iso(),
                )
                continue

            if task_type == "data":
                source = str(record.get("source") or raw_config.get("source") or "").strip().lower()
                downloader = str(raw_config.get("downloader") or source or "").strip().lower()
                symbols = [str(item).strip().upper() for item in raw_config.get("symbols", []) if str(item).strip()]
                timeframes = [
                    str(item).strip().lower()
                    for item in raw_config.get("timeframes", [])
                    if str(item).strip()
                ]
                total_items = max(int(raw_config.get("total_items", 0) or 0), 0)
                name = str(record.get("name") or "Recovered data task")
                description = str(record.get("description") or "")
                start = str(raw_config.get("start") or "").strip() or None
                end = str(raw_config.get("end") or "").strip() or None

                if downloader in {"dukascopy", "jforex"} and symbols and timeframes:
                    downloader = "dukascopy" if downloader == "dukascopy" else "jforex"
                    cmd = raw_config.get("command")
                    if not isinstance(cmd, list) or not cmd:
                        logger.warning(
                            "Skipping auto-recovery for data task %s: missing command for %s",
                            task_id,
                            downloader,
                        )
                        upsert_background_task_record(
                            task_id=task_id,
                            task_type=task_type,
                            status="queued",
                            config_json=raw_config,
                            progress_message="Recovery pending manual restart; missing restart command",
                            name=str(record.get("name") or ""),
                            description=str(record.get("description") or ""),
                            source=str(record.get("source") or ""),
                            created_at=str(record.get("created_at") or ""),
                            updated_at=_now_iso(),
                        )
                        continue

                    logger.info("Auto-restarting %s data task %s", downloader, task_id)
                    cmd_list = [str(part) for part in cmd]
                    _start_jforex_task(
                        task_id,
                        cmd_list,
                        source_label=downloader,
                        allow_native_fallback=(downloader == "dukascopy"),
                        symbols=symbols,
                        timeframes=timeframes,
                        start=start,
                        end=end,
                        offer_side=str(raw_config.get("offer_side") or raw_config.get("price_type") or "bid"),
                        merge_tick=bool(raw_config.get("merge_tick", False)),
                        merge_s1=bool(raw_config.get("merge_s1", False)),
                        batch_size=_safe_int(raw_config.get("batch_size"), 10),
                        pause_ms=_safe_int(raw_config.get("pause_ms"), 1000),
                        query_timeout_seconds=_safe_int(raw_config.get("query_timeout_seconds"), 900),
                        total_items=total_items or max(len(symbols) * len(timeframes), 1),
                        name=name,
                        description=description,
                    )
                    _apply_recovery_data_metadata(
                        task_id,
                        command=cmd_list,
                        downloader=downloader,
                        symbols=symbols,
                        timeframes=timeframes,
                        start=start,
                        end=end,
                        offer_side=str(raw_config.get("offer_side") or "bid"),
                        price_type=str(raw_config.get("price_type") or "bid"),
                        merge_tick=bool(raw_config.get("merge_tick", False)),
                        merge_s1=bool(raw_config.get("merge_s1", False)),
                        batch_size=_safe_int(raw_config.get("batch_size"), 10),
                        pause_ms=_safe_int(raw_config.get("pause_ms"), 1000),
                        query_timeout_seconds=_safe_int(raw_config.get("query_timeout_seconds"), 900),
                    )
                    continue

                command = raw_config.get("command")
                if isinstance(command, list) and command:
                    logger.info("Auto-restarting data task %s via persisted command", task_id)
                    _start_data_task(
                        task_id,
                        [str(part) for part in command],
                        total_items=total_items,
                        name=name,
                        description=description,
                        kind="data",
                        source=source or downloader or "data",
                    )
                    _apply_recovery_data_metadata(
                        task_id,
                        command=[str(part) for part in command],
                        downloader=downloader or None,
                        symbols=symbols or None,
                        timeframes=timeframes or None,
                        start=start,
                        end=end,
                        offer_side=str(raw_config.get("offer_side") or "") or None,
                        price_type=str(raw_config.get("price_type") or "") or None,
                        merge_tick=(
                            bool(raw_config.get("merge_tick"))
                            if raw_config.get("merge_tick") is not None
                            else None
                        ),
                        merge_s1=(
                            bool(raw_config.get("merge_s1"))
                            if raw_config.get("merge_s1") is not None
                            else None
                        ),
                        batch_size=(
                            _safe_int(raw_config.get("batch_size"))
                            if raw_config.get("batch_size") is not None
                            else None
                        ),
                        pause_ms=(
                            _safe_int(raw_config.get("pause_ms"))
                            if raw_config.get("pause_ms") is not None
                            else None
                        ),
                        query_timeout_seconds=(
                            _safe_int(raw_config.get("query_timeout_seconds"))
                            if raw_config.get("query_timeout_seconds") is not None
                            else None
                        ),
                    )
                    continue

                upsert_background_task_record(
                    task_id=task_id,
                    task_type=task_type,
                    status="queued",
                    config_json=raw_config,
                    progress_message="Recovery pending manual restart; missing restart metadata",
                    name=str(record.get("name") or ""),
                    description=str(record.get("description") or ""),
                    source=str(record.get("source") or ""),
                    created_at=str(record.get("created_at") or ""),
                    updated_at=_now_iso(),
                )
        except Exception as exc:
            logger.warning("Failed to recover interrupted task %s: %s", record.get("id"), exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_views_prewarm_task = asyncio.create_task(_prewarm_data_views_background())
    live_accounts_prewarm_task = asyncio.create_task(_prewarm_live_accounts_background())
    strategies_prewarm_task = asyncio.create_task(_prewarm_strategies_background())
    backtests_meta_prewarm_task = asyncio.create_task(_prewarm_backtest_metadata_background())
    jforex_jvm_prewarm_task = asyncio.create_task(_prewarm_jforex_jvm_background())
    app.state.data_views_prewarm_task = data_views_prewarm_task
    app.state.live_accounts_prewarm_task = live_accounts_prewarm_task
    app.state.strategies_prewarm_task = strategies_prewarm_task
    app.state.backtests_meta_prewarm_task = backtests_meta_prewarm_task
    app.state.jforex_jvm_prewarm_task = jforex_jvm_prewarm_task
    await asyncio.to_thread(mark_background_tasks_interrupted, from_statuses=["queued", "running"])
    await asyncio.to_thread(_resume_interrupted_tasks)
    try:
        yield
    finally:
        for task in (
            data_views_prewarm_task,
            live_accounts_prewarm_task,
            strategies_prewarm_task,
            backtests_meta_prewarm_task,
            jforex_jvm_prewarm_task,
        ):
            if not task.done():
                task.cancel()
        for task in (
            data_views_prewarm_task,
            live_accounts_prewarm_task,
            strategies_prewarm_task,
            backtests_meta_prewarm_task,
            jforex_jvm_prewarm_task,
        ):
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="trading-platform viewer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)


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


class CapmScatterPoint(SQLModel):
    x: float
    y: float


class CapmRegressionLine(SQLModel):
    slope: float
    intercept: float


class CapmResponse(SQLModel):
    alpha: float
    beta: float
    r_squared: float
    p_value: float
    scatter_data: list[CapmScatterPoint]
    regression_line: CapmRegressionLine
    benchmark_symbol: str


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
    offer_side: str = "bid"
    price_type: str | None = None
    merge_tick: bool = False
    merge_bid_ask: bool = False
    merge_s1: bool = False
    batch_size: int | None = None
    concurrency: int | None = None
    pause_ms: int = 1000
    query_timeout_seconds: int = 900


class DataResampleRequest(SQLModel):
    source_path: str
    target_timeframes: str


class DataHarvestRequest(SQLModel):
    source: str = "dukascopy"
    symbol: str
    asset_class: str | None = None
    start: str | None = None


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


class BacktestsListResponse(SQLModel):
    items: list[dict[str, Any]]
    page: int
    per_page: int
    total: int
    sort: str
    order: str


class BacktestsDuplicatesResponse(SQLModel):
    groups: list[dict[str, Any]]
    total_groups: int
    total_backtests: int


class BacktestsDeduplicateResponse(SQLModel):
    deleted_ids: list[str]
    kept_ids: list[str]
    groups_affected: int


class BacktestsBulkDeleteRequest(SQLModel):
    ids: list[str]


_QUIET_EXACT_PATHS = {
    "/api/data/tasks",
    "/api/live/accounts",
    "/api/live/deployments",
}
_QUIET_PREFIX_PATHS = (
    "/api/data/tasks/",
    "/api/live/balances/",
    "/api/live/stats/",
    "/api/live/trades/",
)
_UVICORN_ACCESS_PATH_RE = re.compile(r'"[A-Z]+ (?P<path>/[^ ]*) HTTP/[^"]+"')


def _is_quiet_request_path(path: str) -> bool:
    normalized = str(path or "").split("?", 1)[0]
    if normalized in _QUIET_EXACT_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in _QUIET_PREFIX_PATHS)


def _is_quiet_access_log_message(message: str) -> bool:
    matched = _UVICORN_ACCESS_PATH_RE.search(str(message or ""))
    if matched is None:
        return False
    return _is_quiet_request_path(matched.group("path"))


class _QuietAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not _is_quiet_access_log_message(message)


def _configure_uvicorn_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    # Access logs are intentionally disabled; request logging is handled by
    # api_request_logging_middleware with structured timestamps.
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True


_configure_uvicorn_access_log_filter()


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


@app.middleware("http")
async def api_request_logging_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method
    started_at = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
        logger.exception(
            "HTTP request failed",
            extra={
                "data": {
                    "method": method,
                    "path": path,
                    "status": 500,
                    "duration_ms": duration_ms,
                }
            },
        )
        raise

    duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    log = logger.debug if _is_quiet_request_path(path) else logger.info
    log(
        "HTTP request completed",
        extra={
            "data": {
                "method": method,
                "path": path,
                "status": int(response.status_code),
                "duration_ms": duration_ms,
            }
        },
    )
    return response


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


def _restart_interrupted_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    current_status = str(task.get("status") or "").lower()
    if current_status not in {"interrupted", "failed"}:
        raise HTTPException(status_code=409, detail="Only interrupted or failed tasks can be restarted")

    record = get_background_task_record(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task persistence record not found")
    config_payload = dict(record.get("config_json") or {})
    restart_config = config_payload.get("restart_config")
    if isinstance(restart_config, dict):
        config_payload = dict(restart_config)
    task_type = str(record.get("type") or "")

    if task_type == "backtest":
        strategy = str(config_payload.get("strategy") or "").strip()
        symbols = config_payload.get("symbols")
        if not strategy or not isinstance(symbols, list) or not symbols:
            raise HTTPException(status_code=409, detail="Backtest restart configuration is incomplete")
        request_payload = BacktestRunRequest(
            strategy=strategy,
            engine=str(config_payload.get("engine") or "native"),
            symbols=[str(symbol) for symbol in symbols if str(symbol).strip()],
            timeframe=str(config_payload.get("timeframe") or "1d"),
            start=str(config_payload.get("start") or ""),
            end=str(config_payload.get("end") or ""),
            capital=float(config_payload.get("capital") or 100000.0),
            risk_per_trade_pct=config_payload.get("risk_per_trade_pct"),
            max_position_pct=config_payload.get("max_position_pct"),
            max_leverage=config_payload.get("max_leverage"),
            modeling_resolution=(
                str(config_payload.get("modeling_resolution"))
                if config_payload.get("modeling_resolution") is not None
                else None
            ),
            intrabar_resolution=(
                str(config_payload.get("intrabar_resolution"))
                if config_payload.get("intrabar_resolution") is not None
                else None
            ),
            parameters=dict(config_payload.get("parameters") or {}),
        )
        restarted = asyncio.run(run_backtest(request_payload))
        _update_task_fields(
            task_id,
            status="completed",
            progress=100.0,
            progress_detail={
                "completed": 1,
                "failed": 0,
                "total": 1,
                "percent": 100.0,
                "current": "Restarted as a new task",
            },
        )
        upsert_background_task_record(
            task_id=task_id,
            task_type=task_type,
            status="completed",
            config_json=config_payload,
            progress_message="Restarted as new task",
            name=str(record.get("name") or ""),
            description=str(record.get("description") or ""),
            source=str(record.get("source") or ""),
            created_at=str(record.get("created_at") or ""),
            updated_at=_now_iso(),
        )
        payload = _task_action_payload(task_id)
        payload["restarted_task_id"] = restarted.get("task_id") if isinstance(restarted, dict) else None
        return payload

    if task_type == "data":
        downloader = str(config_payload.get("downloader") or "").strip().lower()
        if downloader == "jforex":
            _update_task_fields(
                task_id,
                status="interrupted",
                progress=100.0,
                progress_detail={
                    "completed": 0,
                    "failed": 1,
                    "total": max(int(task.get("total_items", 0) or 0), 0),
                    "percent": 100.0,
                    "current": "JForex task can only be restarted from Data page controls",
                },
            )
            refreshed_task = _get_task(task_id)
            refreshed_progress = dict((refreshed_task or {}).get("progress_detail") or {})
            upsert_background_task_record(
                task_id=task_id,
                task_type=task_type,
                status="interrupted",
                config_json=_task_config_payload(refreshed_task) if refreshed_task is not None else config_payload,
                progress_message=str(
                    refreshed_progress.get("current")
                    or "JForex task requires manual restart from Data page"
                ),
                name=str(record.get("name") or ""),
                description=str(record.get("description") or ""),
                source=str(record.get("source") or ""),
                created_at=str(record.get("created_at") or ""),
                updated_at=_now_iso(),
            )
            return _task_action_payload(task_id)
        upsert_background_task_record(
            task_id=task_id,
            task_type=task_type,
            status="queued",
            config_json=config_payload,
            progress_message="Restart requested; re-run manually from Data page",
            name=str(record.get("name") or ""),
            description=str(record.get("description") or ""),
            source=str(record.get("source") or ""),
            created_at=str(record.get("created_at") or ""),
            updated_at=_now_iso(),
        )
        _update_task_fields(
            task_id,
            status="queued",
            progress=5.0,
            progress_detail={
                "completed": 0,
                "failed": 0,
                "total": max(int(task.get("total_items", 0) or 0), 0),
                "percent": 5.0,
                "current": "Restart requested; start a new data task",
            },
        )
        return _task_action_payload(task_id)

    raise HTTPException(status_code=409, detail=f"Task type '{task_type}' does not support restart")


_DATA_VIEWS_CACHE_LOCK = threading.RLock()
_DATA_VIEWS_CACHE: dict[str, Any] = {
    "key": None,
    "overview_categories": [],
    "category_symbols": {},
    "symbols_payload": {"symbols": [], "categories": {}},
}
_DATA_VIEWS_PREWARM_DONE = False
_LIVE_ACCOUNTS_CACHE_LOCK = threading.RLock()
_LIVE_ACCOUNTS_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": [], "provider_marker": None}
_LIVE_ACCOUNTS_CACHE_TTL_SECONDS = 30.0
_STRATEGIES_CACHE_LOCK = threading.RLock()
_STRATEGIES_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": []}
_STRATEGIES_CACHE_TTL_SECONDS = 60.0
_BACKTEST_META_CACHE_LOCK = threading.RLock()
_BACKTEST_META_CACHE: dict[str, dict[str, Any]] = {}
_BACKTEST_EQUITY_PREVIEW_CACHE_LOCK = threading.RLock()
_BACKTEST_EQUITY_PREVIEW_CACHE: dict[tuple[str, int], list[float]] = {}
_TASK_STATUS_WEIGHTS: dict[str, float] = {
    "queued": 5.0,
    "starting": 10.0,
    "running": 50.0,
    "completed": 100.0,
    "failed": 100.0,
    "cancelled": 100.0,
    "interrupted": 100.0,
}

_DATA_SYMBOL_METADATA_KEYS = {
    "symbol",
    "timeframe",
    "path",
    "size_mb",
    "row_count",
    "file_count",
    "files",
    "start",
    "end",
    "source",
    "sources",
    "asset_class",
    "asset_classes",
    "last_updated",
    "validation",
    "warning_message",
    "validation_message",
    "error",
    "repo",
    "segment_count",
    "segments",
}
_DATA_SEGMENT_METADATA_KEYS = {
    "path",
    "start",
    "end",
    "rows",
    "size_bytes",
    "segment",
    "last_updated",
    "validation",
    "error",
}


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _task_category_from_kind_source(kind: Any, source: Any) -> str:
    kind_key = str(kind or "").strip().lower()
    source_key = str(source or "").strip().lower()
    if kind_key == "backtest":
        return "backtest"
    if kind_key in {"optimization", "agent_conversion"}:
        return "optimization"
    if kind_key == "live":
        return "live"
    if source_key == "resample":
        return "resample"
    if source_key == "live":
        return "live"
    return "download"


def _sanitize_progress_history(entries: Any) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return history
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp") or "").strip()
        if not timestamp:
            continue
        percent = _safe_float(entry.get("percent"), default=float("nan"))
        if not math.isfinite(percent):
            continue
        history.append(
            {
                "timestamp": timestamp,
                "percent": max(0.0, min(100.0, round(percent, 2))),
            }
        )
    return history[-120:]


def _ensure_task_runtime_metadata(task: dict[str, Any]) -> None:
    started_at = str(task.get("started_at") or task.get("created_at") or _now_iso())
    task["started_at"] = started_at
    task["category"] = str(
        task.get("category") or _task_category_from_kind_source(task.get("kind"), task.get("source"))
    )
    history = _sanitize_progress_history(task.get("progress_history"))
    if not history:
        history = [
            {
                "timestamp": started_at,
                "percent": max(0.0, min(100.0, _safe_float(task.get("progress"), 0.0))),
            }
        ]
    task["progress_history"] = history


def _append_task_progress_history(task: dict[str, Any], percent: Any) -> None:
    numeric = _safe_float(percent, default=float("nan"))
    if not math.isfinite(numeric):
        return
    _ensure_task_runtime_metadata(task)
    bounded = max(0.0, min(100.0, round(numeric, 2)))
    history = list(task.get("progress_history") or [])
    if history:
        last = history[-1]
        if _safe_float(last.get("percent"), default=-1.0) == bounded:
            return
    history.append({"timestamp": _now_iso(), "percent": bounded})
    task["progress_history"] = history[-120:]


def _task_elapsed_seconds(task: dict[str, Any], status: str) -> int | None:
    started = _parse_iso_datetime(task.get("started_at") or task.get("created_at"))
    if started is None:
        return None
    if status in FINAL_TASK_STATUSES:
        ended = _parse_iso_datetime(task.get("finished_at") or task.get("updated_at")) or datetime.now(
            timezone.utc
        )
    else:
        ended = datetime.now(timezone.utc)
    elapsed = (ended - started).total_seconds()
    if elapsed < 0:
        return 0
    return int(round(elapsed))


def _task_eta_seconds(task: dict[str, Any], status: str, elapsed_seconds: int | None) -> int | None:
    if elapsed_seconds is None or status in FINAL_TASK_STATUSES:
        return None
    progress = _safe_float(task.get("progress"), 0.0)
    if progress <= 0.0 or progress >= 100.0:
        return None
    eta = elapsed_seconds * (100.0 - progress) / progress
    if not math.isfinite(eta):
        return None
    return max(0, int(round(eta)))


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_order(value: str, *, default: str = "desc") -> str:
    normalized = str(value or default).strip().lower()
    return "asc" if normalized == "asc" else "desc"


def _normalize_backtest_sort(value: str) -> str:
    normalized = str(value or "created_at").strip().lower()
    if normalized in {
        "created_at",
        "strategy_name",
        "trade_count",
        "total_return",
        "sharpe_ratio",
        "cagr",
        "psr",
        "p_value",
    }:
        return normalized
    return "created_at"


def _backtest_sort_key(item: dict[str, Any], sort_field: str) -> Any:
    if sort_field in {"trade_count"}:
        return int(item.get(sort_field, 0) or 0)
    if sort_field in {"total_return", "sharpe_ratio", "cagr", "psr", "p_value"}:
        return float(item.get(sort_field, 0.0) or 0.0)
    return str(item.get(sort_field, "") or "")


def _refresh_backtest_meta_cache() -> int:
    rows = list_backtest_metadata_rows()
    next_cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        backtest_id = str(row.get("id") or "").strip()
        if not backtest_id:
            continue
        next_cache[backtest_id] = dict(row)
    backtest_ids = list(next_cache.keys())
    preview_map = load_backtest_equity_preview_map(backtest_ids, limit=50) if backtest_ids else {}
    next_preview_cache = {
        (backtest_id, 50): list(preview_map.get(backtest_id, []))
        for backtest_id in backtest_ids
    }
    with _BACKTEST_META_CACHE_LOCK:
        _BACKTEST_META_CACHE.clear()
        _BACKTEST_META_CACHE.update(next_cache)
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        _BACKTEST_EQUITY_PREVIEW_CACHE.clear()
        _BACKTEST_EQUITY_PREVIEW_CACHE.update(next_preview_cache)
    return len(next_cache)


def _ensure_backtest_meta_cache() -> None:
    with _BACKTEST_META_CACHE_LOCK:
        if _BACKTEST_META_CACHE:
            return
    _refresh_backtest_meta_cache()


def _upsert_backtest_meta_cache(backtest_id: str) -> None:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return

    row = load_backtest_metadata_row(normalized_id)
    preview_values: list[float] = []
    if row is not None:
        preview_points = load_backtest_equity_preview_points(normalized_id, 50)
        for point in preview_points:
            try:
                preview_values.append(float(point.equity))
            except (TypeError, ValueError):
                continue

    with _BACKTEST_META_CACHE_LOCK:
        if row is None:
            _BACKTEST_META_CACHE.pop(normalized_id, None)
        else:
            _BACKTEST_META_CACHE[normalized_id] = dict(row)

    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        stale_keys = [key for key in _BACKTEST_EQUITY_PREVIEW_CACHE if key[0] == normalized_id]
        for key in stale_keys:
            _BACKTEST_EQUITY_PREVIEW_CACHE.pop(key, None)
        if row is not None:
            _BACKTEST_EQUITY_PREVIEW_CACHE[(normalized_id, 50)] = list(preview_values)


def _remove_backtest_meta_cache(backtest_id: str) -> None:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return
    with _BACKTEST_META_CACHE_LOCK:
        _BACKTEST_META_CACHE.pop(normalized_id, None)
    _remove_backtest_equity_preview_cache(normalized_id)


def _snapshot_backtest_meta_cache() -> list[dict[str, Any]]:
    _ensure_backtest_meta_cache()
    with _BACKTEST_META_CACHE_LOCK:
        return [dict(item) for item in _BACKTEST_META_CACHE.values()]


def _remove_backtest_equity_preview_cache(backtest_id: str) -> None:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        stale_keys = [key for key in _BACKTEST_EQUITY_PREVIEW_CACHE if key[0] == normalized_id]
        for key in stale_keys:
            _BACKTEST_EQUITY_PREVIEW_CACHE.pop(key, None)


def _cached_backtest_equity_preview(backtest_id: str, *, limit: int = 80) -> list[float]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return []

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 80
    normalized_limit = max(1, min(normalized_limit, 200))

    cache_key = (normalized_id, normalized_limit)
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        cached = _BACKTEST_EQUITY_PREVIEW_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)

    points = load_backtest_equity_preview_points(normalized_id, normalized_limit)
    values: list[float] = []
    for point in points:
        try:
            values.append(float(point.equity))
        except (TypeError, ValueError):
            continue

    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        _BACKTEST_EQUITY_PREVIEW_CACHE[cache_key] = list(values)
    return values


def _cached_backtest_equity_preview_only(backtest_id: str, *, limit: int = 50) -> list[float]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return []

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 200))

    cache_key = (normalized_id, normalized_limit)
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        cached = _BACKTEST_EQUITY_PREVIEW_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)

        fallback_keys = [
            key for key in _BACKTEST_EQUITY_PREVIEW_CACHE.keys() if key[0] == normalized_id
        ]
        if not fallback_keys:
            return []
        fallback_keys.sort(key=lambda key: key[1], reverse=True)
        return list(_BACKTEST_EQUITY_PREVIEW_CACHE.get(fallback_keys[0], []))


def _bulk_warm_backtest_equity_preview(backtest_ids: list[str], *, limit: int = 50) -> None:
    normalized_ids = [str(backtest_id or "").strip() for backtest_id in backtest_ids if str(backtest_id or "").strip()]
    if not normalized_ids:
        return

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 200))

    missing_ids: list[str] = []
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        for backtest_id in normalized_ids:
            cache_key = (backtest_id, normalized_limit)
            if cache_key not in _BACKTEST_EQUITY_PREVIEW_CACHE:
                missing_ids.append(backtest_id)
    if not missing_ids:
        return

    preview_map = load_backtest_equity_preview_map(missing_ids, limit=normalized_limit)
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        for backtest_id in missing_ids:
            cache_key = (backtest_id, normalized_limit)
            values = preview_map.get(backtest_id, [])
            _BACKTEST_EQUITY_PREVIEW_CACHE[cache_key] = list(values)


def _warm_backtest_equity_preview_cache(limit: int = 50) -> None:
    items = _snapshot_backtest_meta_cache()
    backtest_ids = [str(item.get("id") or "").strip() for item in items if str(item.get("id") or "").strip()]
    _bulk_warm_backtest_equity_preview(backtest_ids, limit=limit)


def _normalize_win_rate(value: Any) -> float | None:
    if value is None:
        return None
    numeric = _safe_float(value, default=0.0)
    if numeric > 1.0 and numeric <= 100.0:
        numeric = numeric / 100.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return numeric


def _backtest_summary_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    strategy_name = str(meta.get("strategy_name") or "unknown")
    symbols = [str(item) for item in meta.get("symbols", []) if str(item).strip()]
    timeframe = str(meta.get("timeframe") or "")
    start = str(meta.get("start_date") or "")
    end = str(meta.get("end_date") or "")
    total_return_decimal = _safe_float(meta.get("total_return"), 0.0)
    cagr_decimal = _safe_float(meta.get("cagr"), total_return_decimal)
    sharpe_ratio = _safe_float(meta.get("sharpe_ratio"), 0.0)
    psr = _safe_float(meta.get("psr"), 0.0)
    p_value = _safe_float(meta.get("p_value"), 1.0)
    max_drawdown_decimal = abs(_safe_float(meta.get("max_drawdown"), 0.0))
    trade_count = max(int(meta.get("trade_count", 0) or 0), 0)
    backtest_id = str(meta.get("id") or "").strip()
    win_rate = _normalize_win_rate(meta.get("win_rate"))

    return {
        "id": backtest_id,
        "filename": f"{meta.get('id', '')}.trades.json",
        "title": strategy_name,
        "notes": "",
        "strategy": strategy_name,
        "strategy_slug": strategy_name,
        "strategy_display_name": strategy_name,
        "strategy_file": "",
        "engine": str(meta.get("engine") or "unknown"),
        "symbols": symbols,
        "timeframes": [timeframe] if timeframe else [],
        "trade_count": trade_count,
        "start": start,
        "end": end,
        "created_at": str(meta.get("created_at") or ""),
        "sharpe": sharpe_ratio,
        "sharpe_ratio": sharpe_ratio,
        "cagr": round(cagr_decimal * 100.0, 2),
        "max_drawdown_pct": round(max_drawdown_decimal * 100.0, 2),
        "net_profit": 0.0,
        "net_profit_pct": round(total_return_decimal * 100.0, 2),
        "psr": round(psr, 4),
        "p_value": round(p_value, 6),
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "equity_mini": [],
    }


def _backtest_all_item_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    win_rate = _normalize_win_rate(meta.get("win_rate"))
    return {
        "id": str(meta.get("id") or "").strip(),
        "strategy_name": str(meta.get("strategy_name") or "unknown"),
        "symbols": [str(item) for item in meta.get("symbols", []) if str(item).strip()],
        "timeframe": str(meta.get("timeframe") or ""),
        "total_return": _safe_float(meta.get("total_return"), 0.0),
        "sharpe_ratio": _safe_float(meta.get("sharpe_ratio"), 0.0),
        "psr": _safe_float(meta.get("psr"), 0.0),
        "p_value": _safe_float(meta.get("p_value"), 1.0),
        "cagr": _safe_float(meta.get("cagr"), 0.0),
        "max_drawdown": abs(_safe_float(meta.get("max_drawdown"), 0.0)),
        "trade_count": max(int(meta.get("trade_count", 0) or 0), 0),
        "win_rate": win_rate,
        "start_date": str(meta.get("start_date") or ""),
        "end_date": str(meta.get("end_date") or ""),
        "created_at": str(meta.get("created_at") or ""),
    }


def _backtest_all_item_with_equity_preview(meta: dict[str, Any], *, preview_limit: int = 50) -> dict[str, Any]:
    payload = _backtest_all_item_from_meta(meta)
    backtest_id = str(payload.get("id") or "").strip()
    if not backtest_id:
        payload["equity_preview"] = []
        return payload
    payload["equity_preview"] = _cached_backtest_equity_preview_only(backtest_id, limit=preview_limit)
    return payload


def _backtest_fingerprint(item: dict[str, Any]) -> str:
    strategy_name = str(item.get("strategy_name") or "").strip().lower()
    symbols = sorted(
        {str(symbol).strip().upper() for symbol in item.get("symbols", []) if str(symbol).strip()}
    )
    timeframe = str(item.get("timeframe") or "").strip().lower()
    start_date = str(item.get("start_date") or "").strip()
    end_date = str(item.get("end_date") or "").strip()
    engine = str(item.get("engine") or "unknown").strip().lower()
    return "|".join([strategy_name, ",".join(symbols), timeframe, start_date, end_date, engine])


def _backtest_meta_list_sorted(sort_field: str, order: str) -> list[dict[str, Any]]:
    items = _snapshot_backtest_meta_cache()
    reverse = order == "desc"
    items.sort(
        key=lambda item: (_backtest_sort_key(item, sort_field), str(item.get("id") or "")),
        reverse=reverse,
    )
    return items


def _apply_backtest_numeric_filters(
    items: list[dict[str, Any]],
    *,
    sharpe_min: float | None,
    sharpe_max: float | None,
    psr_min: float | None,
    psr_max: float | None,
    p_value_min: float | None,
    p_value_max: float | None,
    cagr_min: float | None,
    cagr_max: float | None,
) -> list[dict[str, Any]]:
    filtered = items

    def in_range(value: float, lower: float | None, upper: float | None) -> bool:
        if lower is not None and value < lower:
            return False
        if upper is not None and value > upper:
            return False
        return True

    if sharpe_min is not None or sharpe_max is not None:
        filtered = [
            item
            for item in filtered
            if in_range(_safe_float(item.get("sharpe_ratio"), 0.0), sharpe_min, sharpe_max)
        ]
    if psr_min is not None or psr_max is not None:
        filtered = [
            item for item in filtered if in_range(_safe_float(item.get("psr"), 0.0), psr_min, psr_max)
        ]
    if p_value_min is not None or p_value_max is not None:
        filtered = [
            item
            for item in filtered
            if in_range(_safe_float(item.get("p_value"), 1.0), p_value_min, p_value_max)
        ]
    if cagr_min is not None or cagr_max is not None:
        filtered = [
            item for item in filtered if in_range(_safe_float(item.get("cagr"), 0.0), cagr_min, cagr_max)
        ]
    return filtered


def _group_duplicate_backtests() -> tuple[list[dict[str, Any]], int]:
    groups: dict[str, list[dict[str, Any]]] = {}
    total_items = 0
    for row in list_backtest_fingerprint_rows():
        fingerprint = _backtest_fingerprint(row)
        if not fingerprint:
            continue
        groups.setdefault(fingerprint, []).append(dict(row))

    duplicates: list[dict[str, Any]] = []
    for fingerprint, entries in groups.items():
        if len(entries) < 2:
            continue
        sorted_entries = sorted(
            entries,
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        total_items += len(sorted_entries)
        first = sorted_entries[0]
        duplicates.append(
            {
                "fingerprint": fingerprint,
                "strategy_name": str(first.get("strategy_name") or ""),
                "symbols": [str(symbol) for symbol in first.get("symbols", []) if str(symbol).strip()],
                "timeframe": str(first.get("timeframe") or ""),
                "start_date": str(first.get("start_date") or ""),
                "end_date": str(first.get("end_date") or ""),
                "engine": str(first.get("engine") or "unknown"),
                "backtests": [
                    {
                        "id": str(item.get("id") or ""),
                        "created_at": str(item.get("created_at") or ""),
                    }
                    for item in sorted_entries
                ],
            }
        )

    duplicates.sort(
        key=lambda item: (
            item.get("backtests", [{}])[0].get("created_at", ""),
            item.get("backtests", [{}])[0].get("id", ""),
        ),
        reverse=True,
    )
    return duplicates, total_items


def _delete_backtest_with_artifacts(backtest_id: str) -> bool:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return False

    deleted_in_db = delete_backtest(normalized_id)
    _remove_backtest_meta_cache(normalized_id)
    json_path = BACKTESTS_DIR / f"{normalized_id}.trades.json"
    deleted_json = False
    if json_path.exists():
        try:
            json_path.unlink()
            deleted_json = True
        except OSError as exc:
            logger.warning("Could not delete JSON backtest %s: %s", normalized_id, exc)
    return deleted_in_db or deleted_json


def _task_config_payload(task: dict[str, Any]) -> dict[str, Any]:
    _ensure_task_runtime_metadata(task)
    progress = _json_safe(dict(task.get("progress_detail") or {}))
    payload: dict[str, Any] = {
        "kind": task.get("kind"),
        "source": task.get("source"),
        "category": task.get("category"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "progress_history": _json_safe(_sanitize_progress_history(task.get("progress_history"))),
        "total_items": int(task.get("total_items", 0) or 0),
        "supports_control": bool(task.get("supports_control", False)),
        "control_mode": str(task.get("control_mode", "") or ""),
        "progress_percent": float(task.get("progress", 0.0) or 0.0),
        "progress_detail": progress,
        "error": _json_safe(task.get("error")),
        "result": _json_safe(task.get("result")),
        "restart_config": _json_safe(task.get("restart_config")),
    }
    if task.get("pid") is not None:
        payload["pid"] = task.get("pid")
    if task.get("command") is not None:
        payload["command"] = _json_safe(task.get("command"))
    if task.get("downloader") is not None:
        payload["downloader"] = _json_safe(task.get("downloader"))
    for key in (
        "symbols",
        "timeframes",
        "start",
        "end",
        "offer_side",
        "price_type",
        "merge_tick",
        "merge_s1",
        "batch_size",
        "pause_ms",
        "query_timeout_seconds",
    ):
        if key in task and task.get(key) is not None:
            payload[key] = _json_safe(task.get(key))
    return payload


def _persist_task_snapshot(task_id: str, task: dict[str, Any]) -> None:
    try:
        _ensure_task_runtime_metadata(task)
        if str(task.get("status") or "").lower() in FINAL_TASK_STATUSES and not task.get("finished_at"):
            task["finished_at"] = str(task.get("updated_at") or _now_iso())
        progress_detail = dict(task.get("progress_detail") or {})
        progress_message = str(progress_detail.get("current") or "")
        upsert_background_task_record(
            task_id=task_id,
            task_type=str(task.get("kind") or "unknown"),
            status=str(task.get("status") or "unknown"),
            config_json=_task_config_payload(task),
            progress_message=progress_message,
            name=str(task.get("name") or ""),
            description=str(task.get("description") or ""),
            source=str(task.get("source") or ""),
            created_at=str(task.get("created_at") or ""),
            updated_at=str(task.get("updated_at") or ""),
        )
    except Exception as exc:
        logger.warning("Could not persist task %s: %s", task_id, exc)


def _decorate_task_from_record(task_id: str, task: dict[str, Any]) -> None:
    record = get_background_task_record(task_id)
    if record is None:
        return

    if not task.get("created_at"):
        task["created_at"] = record.get("created_at") or _now_iso()
    if not task.get("updated_at"):
        task["updated_at"] = record.get("updated_at") or task.get("created_at")

    if task.get("name") in (None, ""):
        task["name"] = record.get("name", "")
    if task.get("description") in (None, ""):
        task["description"] = record.get("description", "")
    if task.get("source") in (None, ""):
        task["source"] = record.get("source", "")
    if task.get("kind") in (None, ""):
        task["kind"] = record.get("type", "data")
    if task.get("status") in (None, ""):
        task["status"] = record.get("status", "unknown")

    config_payload = dict(record.get("config_json") or {})
    task["category"] = str(
        config_payload.get("category")
        or task.get("category")
        or _task_category_from_kind_source(task.get("kind"), task.get("source"))
    )
    task["started_at"] = str(
        config_payload.get("started_at") or task.get("started_at") or task.get("created_at") or _now_iso()
    )
    finished_at = str(config_payload.get("finished_at") or task.get("finished_at") or "").strip()
    if finished_at:
        task["finished_at"] = finished_at
    total_items = int(config_payload.get("total_items", task.get("total_items", 0)) or 0)
    task["total_items"] = total_items

    progress_detail = dict(config_payload.get("progress_detail") or {})
    progress_detail.setdefault("completed", 0)
    progress_detail.setdefault("failed", 0)
    progress_detail.setdefault("total", max(total_items, 0))
    progress_detail.setdefault(
        "percent",
        float(config_payload.get("progress_percent", _TASK_STATUS_WEIGHTS.get(str(task.get("status")), 0.0))),
    )
    progress_detail.setdefault("current", str(record.get("progress_message") or ""))
    task["progress"] = float(progress_detail.get("percent", 0.0) or 0.0)
    task["progress_detail"] = progress_detail

    supports_control = bool(config_payload.get("supports_control", task.get("supports_control", True)))
    task["supports_control"] = supports_control
    task["control_mode"] = str(config_payload.get("control_mode", task.get("control_mode", "thread")) or "thread")
    task.setdefault("pause_event", threading.Event())
    task.setdefault("stop_event", threading.Event())
    task.setdefault("log_file", str(_task_log_path(task_id)))
    record_history = _sanitize_progress_history(config_payload.get("progress_history"))
    if record_history:
        task["progress_history"] = record_history
    _ensure_task_runtime_metadata(task)


def _build_task_from_record(record: dict[str, Any]) -> dict[str, Any]:
    task_id = str(record.get("id") or "").strip()
    config_payload = dict(record.get("config_json") or {})
    total_items = int(config_payload.get("total_items", 0) or 0)
    status = str(record.get("status") or "unknown")
    progress_detail = dict(config_payload.get("progress_detail") or {})
    progress_detail.setdefault("completed", 0)
    progress_detail.setdefault("failed", 0)
    progress_detail.setdefault("total", max(total_items, 0))
    progress_detail.setdefault(
        "percent",
        float(config_payload.get("progress_percent", _TASK_STATUS_WEIGHTS.get(status, 0.0))),
    )
    progress_detail.setdefault("current", str(record.get("progress_message") or ""))
    task = {
        "id": task_id,
        "name": str(record.get("name") or ""),
        "description": str(record.get("description") or ""),
        "kind": str(record.get("type") or "data"),
        "source": str(record.get("source") or ""),
        "status": status,
        "log_file": str(_task_log_path(task_id)),
        "total_items": total_items,
        "progress": float(progress_detail.get("percent", 0.0) or 0.0),
        "progress_detail": progress_detail,
        "supports_control": bool(config_payload.get("supports_control", False)),
        "control_mode": str(config_payload.get("control_mode", "thread") or "thread"),
        "created_at": str(record.get("created_at") or _now_iso()),
        "updated_at": str(record.get("updated_at") or _now_iso()),
        "started_at": str(config_payload.get("started_at") or record.get("created_at") or _now_iso()),
        "finished_at": str(config_payload.get("finished_at") or ""),
        "category": str(
            config_payload.get("category")
            or _task_category_from_kind_source(record.get("type"), record.get("source"))
        ),
        "progress_history": _sanitize_progress_history(config_payload.get("progress_history")),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "error": config_payload.get("error"),
        "result": config_payload.get("result"),
    }
    if config_payload.get("pid") is not None:
        task["pid"] = config_payload.get("pid")
    if config_payload.get("command") is not None:
        task["command"] = config_payload.get("command")
    if config_payload.get("downloader") is not None:
        task["downloader"] = config_payload.get("downloader")
    for key in (
        "symbols",
        "timeframes",
        "start",
        "end",
        "offer_side",
        "price_type",
        "merge_tick",
        "merge_s1",
        "batch_size",
        "pause_ms",
        "query_timeout_seconds",
    ):
        if key in config_payload and config_payload.get(key) is not None:
            task[key] = config_payload.get(key)
    _ensure_task_runtime_metadata(task)
    return task


def _apply_recovery_data_metadata(
    task_id: str,
    *,
    command: list[str],
    downloader: str | None = None,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    offer_side: str | None = None,
    price_type: str | None = None,
    merge_tick: bool | None = None,
    merge_s1: bool | None = None,
    batch_size: int | None = None,
    pause_ms: int | None = None,
    query_timeout_seconds: int | None = None,
) -> None:
    task = _get_task(task_id)
    if task is None:
        logger.warning("Cannot apply recovery metadata for unknown task %s", task_id)
        return
    payload_updates: dict[str, Any] = {}
    payload_updates["command"] = list(command)
    if downloader is not None:
        payload_updates["downloader"] = downloader
    if symbols is not None:
        payload_updates["symbols"] = list(symbols)
    if timeframes is not None:
        payload_updates["timeframes"] = list(timeframes)
    if start is not None:
        payload_updates["start"] = start
    if end is not None:
        payload_updates["end"] = end
    if offer_side is not None:
        payload_updates["offer_side"] = offer_side
    if price_type is not None:
        payload_updates["price_type"] = price_type
    if merge_tick is not None:
        payload_updates["merge_tick"] = bool(merge_tick)
    if merge_s1 is not None:
        payload_updates["merge_s1"] = bool(merge_s1)
    if batch_size is not None:
        payload_updates["batch_size"] = int(batch_size)
    if pause_ms is not None:
        payload_updates["pause_ms"] = int(pause_ms)
    if query_timeout_seconds is not None:
        payload_updates["query_timeout_seconds"] = int(query_timeout_seconds)
    _update_task_fields(task_id, **payload_updates)


def _create_task_record(*args: Any, **kwargs: Any) -> dict[str, Any]:
    task = _LEGACY_CREATE_TASK_RECORD(*args, **kwargs)
    _ensure_task_runtime_metadata(task)
    _append_task_progress_history(task, task.get("progress"))
    task_id = str(task.get("id") or "")
    if task_id:
        _persist_task_snapshot(task_id, task)
    return task


def _get_task(task_id: str) -> dict[str, Any] | None:
    task = _LEGACY_GET_TASK(task_id)
    if task is not None:
        _decorate_task_from_record(task_id, task)
        return task

    record = get_background_task_record(task_id)
    if record is None:
        return None
    hydrated = _build_task_from_record(record)
    with DATA_TASKS_LOCK:
        existing = DATA_TASKS.get(task_id)
        if existing is not None:
            _decorate_task_from_record(task_id, existing)
            return existing
        DATA_TASKS[task_id] = hydrated
        return hydrated


def _snapshot_task(task_id: str) -> dict[str, Any] | None:
    snapshot = _LEGACY_SNAPSHOT_TASK(task_id)
    if snapshot is None:
        current = _get_task(task_id)
        return dict(current) if current is not None else None
    _decorate_task_from_record(task_id, snapshot)
    return snapshot


def _update_task_fields(task_id: str, **updates: Any) -> dict[str, Any] | None:
    task = _LEGACY_UPDATE_TASK_FIELDS(task_id, **updates)
    if task is not None:
        _ensure_task_runtime_metadata(task)
        if str(task.get("status") or "").lower() in FINAL_TASK_STATUSES and not task.get("finished_at"):
            task["finished_at"] = str(task.get("updated_at") or _now_iso())
        if "progress" in updates:
            _append_task_progress_history(task, task.get("progress"))
        _persist_task_snapshot(task_id, task)
    return task


def _set_task_status(task_id: str, status: str) -> dict[str, Any] | None:
    task = _LEGACY_SET_TASK_STATUS(task_id, status)
    if task is not None:
        _ensure_task_runtime_metadata(task)
        if str(status).lower() in FINAL_TASK_STATUSES and not task.get("finished_at"):
            task["finished_at"] = str(task.get("updated_at") or _now_iso())
        _append_task_progress_history(task, task.get("progress"))
        _persist_task_snapshot(task_id, task)
    return task


def _update_task_progress(
    task_id: str,
    *,
    percent: float | None = None,
    current: str | None = None,
    total: int | None = None,
) -> dict[str, Any] | None:
    task = _LEGACY_UPDATE_TASK_PROGRESS(
        task_id,
        percent=percent,
        current=current,
        total=total,
    )
    if task is not None:
        _ensure_task_runtime_metadata(task)
        _append_task_progress_history(task, task.get("progress"))
        _persist_task_snapshot(task_id, task)
    return task


def _mark_task_finished(
    task_id: str,
    *,
    status: str,
    returncode: int | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    task = _LEGACY_MARK_TASK_FINISHED(
        task_id,
        status=status,
        returncode=returncode,
        error=error,
    )
    if task is not None:
        _ensure_task_runtime_metadata(task)
        task["finished_at"] = str(task.get("updated_at") or _now_iso())
        _append_task_progress_history(task, task.get("progress"))
        _persist_task_snapshot(task_id, task)
    return task


def _serialize_task(
    task_id: str,
    task: dict[str, Any],
    *,
    include_log: bool = False,
    max_log_bytes: int = 2048,
) -> dict[str, Any]:
    _ensure_task_runtime_metadata(task)
    payload = _LEGACY_SERIALIZE_TASK(
        task_id,
        task,
        include_log=include_log,
        max_log_bytes=max_log_bytes,
    )
    status = str(payload.get("status") or task.get("status") or "").lower()
    elapsed_seconds = _task_elapsed_seconds(task, status)
    eta_seconds = _task_eta_seconds(task, status, elapsed_seconds)
    payload["can_restart"] = status in {"interrupted", "failed"}
    payload["category"] = str(task.get("category") or _task_category_from_kind_source(task.get("kind"), task.get("source")))
    payload["started_at"] = str(task.get("started_at") or task.get("created_at") or "")
    payload["finished_at"] = str(task.get("finished_at") or "")
    payload["elapsed_seconds"] = elapsed_seconds
    payload["eta_seconds"] = eta_seconds
    payload["error"] = task.get("error")
    return payload


# Route legacy core task lifecycle through persistence-aware wrappers.
legacy_viewer._create_task_record = _create_task_record
legacy_viewer._get_task = _get_task
legacy_viewer._snapshot_task = _snapshot_task
legacy_viewer._update_task_fields = _update_task_fields
legacy_viewer._set_task_status = _set_task_status
legacy_viewer._update_task_progress = _update_task_progress
legacy_viewer._mark_task_finished = _mark_task_finished
legacy_viewer._serialize_task = _serialize_task


def _serialize_live_timestamp(value: datetime | str | None) -> tuple[str, float]:
    if isinstance(value, datetime):
        ts = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, str):
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return "", 0.0
        ts = parsed.to_pydatetime()
    else:
        return "", 0.0
    return ts.isoformat(), float(ts.timestamp())


def _serialize_live_account_payload(account: Any) -> dict[str, Any]:
    broker_login = getattr(account, "broker_login", None)
    ctid = getattr(account, "ctid", None)
    normalized_login = str(broker_login).strip() if broker_login else None
    return {
        "id": account.id,
        "broker": account.broker,
        "name": account.name,
        "broker_login": normalized_login,
        "login": normalized_login,
        "ctid": str(ctid).strip() if ctid else None,
    }


def _serialize_live_deployment_payload(deployment: LiveDeploymentConfig) -> dict[str, Any]:
    return {
        "id": str(deployment.id),
        "strategy_name": deployment.strategy_name,
        "account_id": deployment.account_id,
        "symbols": deployment.symbols,
        "timeframe": deployment.timeframe,
        "status": deployment.status,
        "settings": deployment.settings,
    }


def _discover_live_accounts_payload() -> list[dict[str, Any]]:
    discovered = asyncio.run(discover_configured_accounts())
    payload: list[dict[str, Any]] = []
    for account in discovered:
        if isinstance(account, dict):
            account_id = str(account.get("id") or "").strip()
            if not account_id:
                continue
            normalized_login = (
                str(account.get("broker_login") or account.get("login")).strip()
                if account.get("broker_login") or account.get("login")
                else None
            )
            payload.append(
                {
                    "id": account_id,
                    "name": str(account.get("name") or account_id),
                    "broker": str(account.get("broker") or "unknown"),
                    "balance": _safe_float(account.get("balance"), 0.0),
                    "is_demo": bool(account.get("is_demo", False)),
                    "broker_login": normalized_login,
                    "login": normalized_login,
                    "ctid": (
                        str(account.get("ctid")).strip() if account.get("ctid") else None
                    ),
                }
            )
            continue
        normalized_login = (
            str(account.broker_login).strip() if getattr(account, "broker_login", None) else None
        )
        payload.append(
            {
                "id": account.id,
                "name": account.name,
                "broker": account.broker,
                "balance": float(account.balance),
                "is_demo": bool(account.is_demo),
                "broker_login": normalized_login,
                "login": normalized_login,
                "ctid": (
                    str(account.ctid).strip() if getattr(account, "ctid", None) else None
                ),
            }
        )
    return payload


def _cached_live_accounts_payload(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    provider_marker = id(discover_configured_accounts)
    with _LIVE_ACCOUNTS_CACHE_LOCK:
        if (
            not force_refresh
            and now < float(_LIVE_ACCOUNTS_CACHE.get("expires_at", 0.0))
            and _LIVE_ACCOUNTS_CACHE.get("provider_marker") == provider_marker
        ):
            cached = _LIVE_ACCOUNTS_CACHE.get("payload", [])
            if isinstance(cached, list):
                return [dict(item) for item in cached if isinstance(item, dict)]

    payload = _discover_live_accounts_payload()
    with _LIVE_ACCOUNTS_CACHE_LOCK:
        _LIVE_ACCOUNTS_CACHE["payload"] = [dict(item) for item in payload]
        _LIVE_ACCOUNTS_CACHE["expires_at"] = time.monotonic() + _LIVE_ACCOUNTS_CACHE_TTL_SECONDS
        _LIVE_ACCOUNTS_CACHE["provider_marker"] = provider_marker
        return [dict(item) for item in payload]


def _list_strategies_payload() -> list[dict[str, Any]]:
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


def _cached_strategies_payload(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _STRATEGIES_CACHE_LOCK:
        if not force_refresh and now < float(_STRATEGIES_CACHE.get("expires_at", 0.0)):
            cached = _STRATEGIES_CACHE.get("payload", [])
            if isinstance(cached, list):
                return [dict(item) for item in cached if isinstance(item, dict)]

    payload = _list_strategies_payload()
    with _STRATEGIES_CACHE_LOCK:
        _STRATEGIES_CACHE["payload"] = [dict(item) for item in payload]
        _STRATEGIES_CACHE["expires_at"] = time.monotonic() + _STRATEGIES_CACHE_TTL_SECONDS
        return [dict(item) for item in payload]


def _seed_default_live_accounts() -> None:
    return


def _deployment_ids_for_account(
    account_id: str, deployments: list[LiveDeploymentConfig]
) -> list[str]:
    ids = {account_id}
    for deployment in deployments:
        ids.add(str(deployment.id))
        strategy_name = str(deployment.strategy_name or "").strip()
        if strategy_name:
            ids.add(strategy_name)
    return sorted(ids)


def _log_live_endpoint_request(endpoint: str, **params: Any) -> None:
    logger.debug(
        "Live API request params",
        extra={"data": {"endpoint": endpoint, "params": params}},
    )


def _calculate_realized_live_pnl(
    rows: list[dict[str, Any]],
) -> tuple[float, int, int]:
    positions: dict[str, dict[str, float]] = {}
    total_pnl = 0.0
    wins = 0
    losses = 0

    ordered_rows = sorted(
        rows,
        key=lambda row: _serialize_live_timestamp(row.get("execution_timestamp"))[1],
    )
    for row in ordered_rows:
        symbol = str(row.get("symbol") or "UNKNOWN").upper()
        action = str(row.get("action") or "").strip().lower()
        direction = 1.0 if action == "buy" else -1.0 if action == "sell" else 0.0
        if direction == 0.0:
            continue

        quantity = abs(_safe_float(row.get("filled_qty"), 0.0))
        price = _safe_float(row.get("actual_price"), 0.0)
        if quantity <= 0.0 or price <= 0.0:
            continue

        delta = quantity * direction
        state = positions.setdefault(symbol, {"qty": 0.0, "avg": 0.0})
        current_qty = state["qty"]
        current_avg = state["avg"]

        if current_qty == 0.0:
            state["qty"] = delta
            state["avg"] = price
            continue

        if current_qty * delta > 0:
            combined_qty = abs(current_qty) + abs(delta)
            if combined_qty > 0:
                state["avg"] = ((abs(current_qty) * current_avg) + (abs(delta) * price)) / combined_qty
            state["qty"] = current_qty + delta
            continue

        closed_qty = min(abs(current_qty), abs(delta))
        if closed_qty > 0:
            side_sign = 1.0 if current_qty > 0 else -1.0
            realized = (price - current_avg) * closed_qty * side_sign
            total_pnl += realized
            if realized > 0:
                wins += 1
            elif realized < 0:
                losses += 1

        new_qty = current_qty + delta
        if new_qty == 0.0:
            state["qty"] = 0.0
            state["avg"] = 0.0
        elif current_qty * new_qty > 0:
            state["qty"] = new_qty
        else:
            state["qty"] = new_qty
            state["avg"] = price

    return total_pnl, wins, losses


def _live_account_stats_payload(account_id: str) -> dict[str, Any]:
    balances, execution_rows = load_live_account_stats_inputs(account_id)

    total_pnl_realized, wins, losses = _calculate_realized_live_pnl(execution_rows)
    total_trades = len(execution_rows)
    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    peak_balance = 0.0
    current_balance = 0.0
    current_equity = 0.0
    for record in balances:
        balance_value = float(record.balance)
        equity_value = float(record.equity)
        current_balance = balance_value
        current_equity = equity_value
        peak_balance = max(peak_balance, balance_value)
        if peak_balance > 0:
            max_drawdown = max(max_drawdown, peak_balance - balance_value)
            drawdown = ((peak_balance - balance_value) / peak_balance) * 100.0
            max_drawdown_pct = max(max_drawdown_pct, drawdown)

    return {
        "account_id": account_id,
        "total_trades": total_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl_realized, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "current_balance": round(current_balance, 2),
        "current_equity": round(current_equity, 2),
    }


def _ensure_live_account_for_deployment(account_id: str) -> dict[str, Any]:
    existing_accounts = {account.id: account for account in list_live_accounts()}
    current = existing_accounts.get(account_id)
    if current is not None:
        return _serialize_live_account_payload(current)

    try:
        discovered_payload = _cached_live_accounts_payload(force_refresh=True)
    except Exception as exc:
        logger.warning(
            "Could not discover broker accounts while creating deployment",
            extra={"data": {"account_id": account_id, "error": str(exc)}},
        )
        discovered_payload = []

    discovered = next(
        (
            item
            for item in discovered_payload
            if str(item.get("id") or "").strip() == account_id
        ),
        None,
    )
    if isinstance(discovered, dict):
        broker = str(discovered.get("broker") or "").strip().lower() or "unknown"
        name = str(discovered.get("name") or "").strip() or account_id
        ctid = str(discovered.get("ctid") or "").strip() or None
        broker_login = (
            str(discovered.get("broker_login") or discovered.get("login") or "").strip() or None
        )
        account = ensure_live_account(
            account_id,
            broker,
            name=name,
            ctid=ctid,
            broker_login=broker_login,
        )
        return _serialize_live_account_payload(account)

    persisted = get_live_account(account_id)
    if persisted is not None:
        return _serialize_live_account_payload(persisted)

    raise HTTPException(
        status_code=400,
        detail=(
            f"Unknown live account '{account_id}'. Select an account from /api/live/accounts."
        ),
    )


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


def _returns_from_equity_curve(equity_curve: list[EquityPoint]) -> pd.Series:
    if len(equity_curve) < 2:
        raise ValueError("Backtest has insufficient equity history for CAPM")

    timestamps = pd.to_datetime([point.time for point in equity_curve], utc=True, errors="coerce")
    equities = pd.to_numeric([point.equity for point in equity_curve], errors="coerce")
    series = pd.Series(equities, index=timestamps).dropna()
    series = series[~series.index.isna()]
    series = series.sort_index().groupby(level=0).last()
    if len(series) < 2:
        raise ValueError("Backtest has insufficient equity history for CAPM")

    daily_equity = series.resample("1D").last().ffill()
    returns = daily_equity.pct_change().replace([math.inf, -math.inf], pd.NA).dropna()
    if returns.empty:
        raise ValueError("Backtest has no valid daily returns for CAPM")
    return returns


def _close_series_from_df(df: pd.DataFrame, *, symbol: str) -> pd.Series:
    if "close" not in df.columns:
        raise HTTPException(status_code=500, detail=f"Benchmark data for {symbol} has no close column")

    if "timestamp" in df.columns:
        timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        series = pd.Series(close.values, index=timestamps).dropna()
    elif isinstance(df.index, pd.DatetimeIndex):
        timestamps = pd.DatetimeIndex(df.index)
        if timestamps.tz is None:
            timestamps = timestamps.tz_localize("UTC")
        else:
            timestamps = timestamps.tz_convert("UTC")
        close = pd.to_numeric(df["close"], errors="coerce")
        series = pd.Series(close.values, index=timestamps).dropna()
    else:
        raise HTTPException(status_code=500, detail=f"Benchmark data for {symbol} has no timestamp column")

    series = series[~series.index.isna()].sort_index().groupby(level=0).last()
    if series.empty:
        raise HTTPException(status_code=404, detail=f"No benchmark price history for {symbol}")
    return series


def _benchmark_daily_returns(symbol: str, *, start: str = "", end: str = "") -> pd.Series:
    df_1d = legacy_viewer._load_ohlcv_df(symbol, "1d")
    series = _close_series_from_df(df_1d, symbol=symbol) if df_1d is not None else pd.Series(dtype=float)

    if series.empty:
        for fallback_timeframe in ["4h", "1h", "15m"]:
            fallback_df = legacy_viewer._load_ohlcv_df(symbol, fallback_timeframe)
            if fallback_df is None:
                continue
            intraday_close = _close_series_from_df(fallback_df, symbol=symbol)
            series = intraday_close.resample("1D").last().dropna()
            if not series.empty:
                logger.info("CAPM benchmark %s: using %s resampled to 1d", symbol, fallback_timeframe)
                break

    if series.empty:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    start_ts = pd.to_datetime(start, utc=True, errors="coerce") if start else pd.NaT
    end_ts = pd.to_datetime(end, utc=True, errors="coerce") if end else pd.NaT
    if not pd.isna(start_ts):
        series = series[series.index >= start_ts]
    if not pd.isna(end_ts):
        series = series[series.index <= end_ts]

    returns = series.pct_change().replace([math.inf, -math.inf], pd.NA).dropna()
    if returns.empty:
        raise HTTPException(status_code=404, detail=f"No benchmark returns for {symbol} in selected range")
    return returns


def _default_benchmark_symbol(result: BacktestResult) -> str:
    data = result.meta.data if isinstance(result.meta.data, dict) else {}
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if isinstance(symbols, list) and symbols:
        return str(symbols[0]).strip().upper()
    return ""


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


_TIMEFRAME_REGEX = r"(?:tick|mn1|(?:\d+)?(?:s|sec|m|min|h|d|w|mo)|[smhdw]\d+)"
_TIMEFRAME_PATTERN = re.compile(rf"^{_TIMEFRAME_REGEX}$", re.IGNORECASE)
_SEGMENTED_TIMEFRAME_PATTERN = re.compile(
    rf"^(?P<timeframe>{_TIMEFRAME_REGEX})(?:[_-].+)$",
    re.IGNORECASE,
)
_SYMBOL_PREFIXED_TIMEFRAME_PATTERN = re.compile(
    rf"^(?P<symbol>.+?)[_-](?P<timeframe>{_TIMEFRAME_REGEX})(?:[_-](?P<segment>.+))?$",
    re.IGNORECASE,
)


def _looks_like_timeframe(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and bool(_TIMEFRAME_PATTERN.match(normalized))


def _extract_timeframe_and_segment(stem: str) -> tuple[str | None, str | None]:
    normalized = str(stem or "").strip()
    if not normalized:
        return None, None
    if _looks_like_timeframe(normalized):
        return normalized.lower(), None

    segmented_match = _SEGMENTED_TIMEFRAME_PATTERN.match(normalized)
    if segmented_match is None:
        return None, None

    timeframe = str(segmented_match.group("timeframe") or "").lower()
    separator_index = len(timeframe)
    segment = (
        normalized[separator_index + 1 :].strip()
        if len(normalized) > separator_index + 1
        else ""
    )
    return timeframe, (segment or None)


def _extract_symbol_timeframe_and_segment(
    stem: str,
) -> tuple[str | None, str | None, str | None]:
    normalized = str(stem or "").strip()
    if not normalized:
        return None, None, None

    timeframe, segment = _extract_timeframe_and_segment(normalized)
    if timeframe is not None:
        return None, timeframe, segment

    prefixed_match = _SYMBOL_PREFIXED_TIMEFRAME_PATTERN.match(normalized)
    if prefixed_match is None:
        return None, None, None

    symbol = str(prefixed_match.group("symbol") or "").strip()
    timeframe_value = str(prefixed_match.group("timeframe") or "").strip().lower()
    segment_value = str(prefixed_match.group("segment") or "").strip()
    if not symbol or not timeframe_value:
        return None, None, None
    return symbol, timeframe_value, (segment_value or None)


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
    metadata_by_path: dict[str, dict[str, Any]] = {}
    for entry in catalog_entries:
        if not isinstance(entry, dict):
            continue

        path_value = entry.get("path")
        if path_value:
            metadata_by_path[str(path_value)] = entry

        segments = entry.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                segment_path = segment.get("path")
                if not segment_path:
                    continue
                merged_segment_entry = dict(entry)
                merged_segment_entry.update(segment)
                metadata_by_path[str(segment_path)] = merged_segment_entry
    return metadata_by_path


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
    if len(parts) < 3:
        return None

    source = parts[0]
    asset_class = parts[1]
    stem = parquet_path.stem
    stem_symbol, timeframe, segment = _extract_symbol_timeframe_and_segment(stem)
    region: str | None = None

    if len(parts) >= 5:
        region = parts[-3]
        symbol = parts[-2]
        timeframe_value = timeframe or "1d"
    elif len(parts) == 4:
        parent = parts[-2]
        if timeframe:
            timeframe_value = timeframe
            if stem_symbol and stem_symbol.lower() != parent.lower():
                region = parent
                symbol = stem_symbol
            else:
                symbol = parent
        else:
            region = parent
            symbol = stem
            timeframe_value = "1d"
    else:
        if timeframe and stem_symbol:
            symbol = stem_symbol
            timeframe_value = timeframe
        elif timeframe:
            return None
        else:
            symbol = stem
            timeframe_value = "1d"

    entry: dict[str, Any] = {
        "source": source,
        "asset_class": asset_class,
        "symbol": symbol,
        "timeframe": timeframe_value,
        "path": str(parquet_path),
    }
    if region:
        entry["region"] = region
    if segment:
        entry["segment"] = segment
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


def _cached_catalog_ohlcv_entries() -> list[dict[str, Any]]:
    try:
        entries = cache_manager.get_catalog_ohlcv_entries(DATA_DIR, unsafe_no_copy=True)
    except Exception as exc:
        logger.warning("Catalog cache load failed, falling back to catalog/file scan: %s", exc)
        entries = []

    if not isinstance(entries, list):
        entries = []

    if not entries:
        legacy_entries, used_legacy_catalog = _load_catalog_ohlcv_entries()
        if used_legacy_catalog and isinstance(legacy_entries, list):
            return [
                dict(entry)
                for entry in legacy_entries
                if isinstance(entry, dict) and str(entry.get("path", "")).endswith(".parquet")
            ]
        return _scan_single_file_ohlcv_entries({})

    include_last_updated = (DATA_DIR / "catalog.json").exists()
    if include_last_updated:
        normalized_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("path", "")).endswith(".parquet")
        ]
        if normalized_entries:
            return normalized_entries

    normalized_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("path", "")).endswith(".parquet"):
            continue
        normalized = dict(entry)
        normalized.pop("last_updated", None)
        normalized_entries.append(normalized)
    if normalized_entries:
        return normalized_entries
    return _scan_single_file_ohlcv_entries({})


def _data_views_cache_key() -> tuple[Any, ...]:
    catalog_path = DATA_DIR / "catalog.json"
    if catalog_path.exists():
        try:
            stat = catalog_path.stat()
            catalog_stamp: tuple[Any, ...] = (True, int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            catalog_stamp = (True, None, None)
    else:
        catalog_stamp = (False, None, None)

    try:
        cache_stats = cache_manager.get_cache_stats()
        cache_stamp = cache_stats.get("last_invalidation_at")
    except Exception:
        cache_stamp = None

    return str(DATA_DIR), catalog_stamp, cache_stamp


def _metadata_only_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {key: segment[key] for key in _DATA_SEGMENT_METADATA_KEYS if key in segment}


def _metadata_only_data_symbol(entry: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: entry[key] for key in _DATA_SYMBOL_METADATA_KEYS if key in entry}
    segments = sanitized.get("segments")
    if isinstance(segments, list):
        sanitized["segments"] = [
            _metadata_only_segment(segment)
            for segment in segments
            if isinstance(segment, dict)
        ]
    return sanitized


def _build_overview_categories(logical_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for entry in logical_entries:
        category = str(entry.get("category") or "Other")
        bucket = categories.setdefault(
            category,
            {"count": 0, "size_bytes": 0, "symbols": set(), "start": None, "end": None},
        )
        bucket["count"] += 1
        bucket["size_bytes"] += max(int(entry.get("size_bytes", 0) or 0), 0)
        bucket["symbols"].add(entry.get("symbol"))

        start_value = entry.get("start")
        end_value = entry.get("end")
        if start_value and (bucket["start"] is None or start_value < bucket["start"]):
            bucket["start"] = start_value
        if end_value and (bucket["end"] is None or end_value > bucket["end"]):
            bucket["end"] = end_value

    return [
        {
            "name": name,
            "symbol_count": len(data["symbols"]),
            "file_count": data["count"],
            "total_size_mb": round(data["size_bytes"] / (1024 * 1024), 2),
            "start": data["start"],
            "end": data["end"],
        }
        for name, data in sorted(categories.items())
    ]


def _build_symbols_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, list[str]] = {}
    seen: set[str] = set()

    for dataset in entries:
        symbol = str(dataset.get("symbol", "") or "")
        if not symbol or symbol in seen:
            continue

        seen.add(symbol)
        category = _infer_symbol_category(
            str(dataset.get("path", "") or ""),
            str(dataset.get("asset_class", "other") or "other"),
        )
        categories.setdefault(category, []).append(symbol)

    for category in categories:
        categories[category].sort()

    return {"symbols": sorted(seen), "categories": categories}


def _invalidate_data_views_cache() -> None:
    with _DATA_VIEWS_CACHE_LOCK:
        _DATA_VIEWS_CACHE["key"] = None
        _DATA_VIEWS_CACHE["overview_categories"] = []
        _DATA_VIEWS_CACHE["category_symbols"] = {}
        _DATA_VIEWS_CACHE["symbols_payload"] = {"symbols": [], "categories": {}}


def _get_data_views() -> dict[str, Any]:
    key = _data_views_cache_key()
    with _DATA_VIEWS_CACHE_LOCK:
        if _DATA_VIEWS_CACHE.get("key") == key:
            return _DATA_VIEWS_CACHE

    entries = _cached_catalog_ohlcv_entries()
    logical_entries = _aggregate_symbol_timeframe_entries(
        entries,
        include_category=True,
        include_size_bytes=True,
    )
    category_symbols: dict[str, list[dict[str, Any]]] = {}
    for entry in logical_entries:
        category_key = str(entry.get("category") or "Other").lower()
        category_symbols.setdefault(category_key, []).append(
            _metadata_only_data_symbol(
                {k: v for k, v in entry.items() if k not in {"category", "size_bytes"}}
            )
        )

    computed = {
        "key": key,
        "overview_categories": _build_overview_categories(logical_entries),
        "category_symbols": category_symbols,
        "symbols_payload": _build_symbols_payload(logical_entries),
    }

    with _DATA_VIEWS_CACHE_LOCK:
        _DATA_VIEWS_CACHE.update(computed)
        return _DATA_VIEWS_CACHE


def _prewarm_data_views_once() -> None:
    global _DATA_VIEWS_PREWARM_DONE
    with _DATA_VIEWS_CACHE_LOCK:
        if _DATA_VIEWS_PREWARM_DONE:
            return

    try:
        _get_data_views()
    except Exception as exc:
        logger.warning("Data views prewarm failed: %s", exc)
        return

    with _DATA_VIEWS_CACHE_LOCK:
        _DATA_VIEWS_PREWARM_DONE = True


def _needs_data_views_cache(path: str) -> bool:
    if path == "/api/data/overview":
        return True
    if path.startswith("/api/data/category/"):
        return True
    return path == "/api/symbols"


@app.middleware("http")
async def data_views_prewarm_middleware(request: Request, call_next):
    if request.method in {"GET", "HEAD"} and _needs_data_views_cache(request.url.path):
        prewarm_task = getattr(request.app.state, "data_views_prewarm_task", None)
        if isinstance(prewarm_task, asyncio.Task) and not prewarm_task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(prewarm_task)
        elif not _DATA_VIEWS_PREWARM_DONE:
            await asyncio.to_thread(_prewarm_data_views_once)
    return await call_next(request)


def _merge_timeframe_entry_dates(current: str | None, candidate: Any, *, latest: bool) -> str | None:
    value = str(candidate or "").strip()
    if not value:
        return current
    if current is None:
        return value
    if latest:
        return value if value > current else current
    return value if value < current else current


def _entry_size_bytes(entry: dict[str, Any]) -> int:
    raw_size = entry.get("size_bytes", 0)
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        size = 0

    if size > 0:
        return size

    path_value = Path(str(entry.get("path", "")))
    try:
        if path_value.exists():
            return int(path_value.stat().st_size)
    except OSError:
        return 0
    return 0


def _canonical_timeframe_path(paths: set[str], timeframe: str) -> str:
    if not paths:
        return ""
    normalized_tf = str(timeframe or "").strip().lower()
    sorted_paths = sorted(paths)
    for path_value in sorted_paths:
        if Path(path_value).stem.lower() == normalized_tf:
            return path_value
    return sorted_paths[0]


def _aggregate_symbol_timeframe_entries(
    entries: list[dict[str, Any]],
    *,
    include_category: bool = False,
    include_size_bytes: bool = False,
) -> list[dict[str, Any]]:
    logical_entries: list[dict[str, Any]] = []
    merged_entries = merge_ohlcv_entries([entry for entry in entries if isinstance(entry, dict)])
    for entry in merged_entries:
        path_value = str(entry.get("path") or "")
        category = _categorize_path(path_value) if include_category else ""
        start_value = entry.get("start") or entry.get("start_date")
        end_value = entry.get("end") or entry.get("end_date")
        size_bytes = _entry_size_bytes(entry)
        warning_message = _warning_message_from_entry(entry)
        error_message = str(entry.get("error") or "").strip() or None

        row_count_raw = entry.get("row_count", entry.get("rows"))
        try:
            row_count = int(row_count_raw) if row_count_raw is not None else None
        except (TypeError, ValueError):
            row_count = None

        files = [
            str(file_path).strip()
            for file_path in (entry.get("files") if isinstance(entry.get("files"), list) else [])
            if str(file_path).strip()
        ]
        if not files and path_value:
            files = [path_value]

        source_values = [
            str(item).strip()
            for item in (entry.get("sources") if isinstance(entry.get("sources"), list) else [])
            if str(item).strip()
        ]

        output_entry: dict[str, Any] = {
            "symbol": str(entry.get("symbol") or "Unknown"),
            "timeframe": str(entry.get("timeframe") or "unknown"),
            "path": path_value,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "start": start_value,
            "end": end_value,
            "start_date": start_value,
            "end_date": end_value,
            "source": str(entry.get("source") or ""),
            "files": files,
            "file_count": len(files),
        }
        if row_count is not None:
            output_entry["row_count"] = row_count
        if source_values:
            output_entry["sources"] = sorted(set(source_values))
        if include_size_bytes:
            output_entry["size_bytes"] = size_bytes

        if include_category:
            output_entry["category"] = category

        validation_payload = entry.get("validation")
        if isinstance(validation_payload, dict) or warning_message or error_message:
            normalized_validation = dict(validation_payload) if isinstance(validation_payload, dict) else {}
            status = str(normalized_validation.get("status") or "").strip().lower()
            if status not in {"ok", "warning", "error"}:
                status = "ok"
            if status == "ok" and (warning_message or error_message):
                status = "warning"
            normalized_validation["status"] = status
            output_entry["validation"] = normalized_validation
        if warning_message:
            output_entry["warning_message"] = warning_message
            output_entry["validation_message"] = warning_message
        if error_message:
            output_entry["error"] = error_message
        if entry.get("repo"):
            output_entry["repo"] = True
        if entry.get("last_updated") is not None:
            output_entry["last_updated"] = entry.get("last_updated")

        segment_entries = sorted(
            [
                segment
                for segment in (entry.get("segments") if isinstance(entry.get("segments"), list) else [])
                if isinstance(segment, dict)
            ],
            key=lambda segment: str(segment.get("path") or ""),
        )
        include_segments = len(segment_entries) > 1 or any(
            str(segment.get("segment") or "").strip() for segment in segment_entries
        )
        if include_segments:
            output_entry["segments"] = segment_entries
            output_entry["segment_count"] = len(segment_entries)

        logical_entries.append(output_entry)

    logical_entries.sort(
        key=lambda item: (
            str(item.get("category") or ""),
            str(item.get("symbol") or ""),
            str(item.get("timeframe") or ""),
            str(item.get("source") or ""),
            str(item.get("path") or ""),
        )
    )
    return logical_entries


@app.get("/api/strategies", response_model=list[dict[str, Any]])
async def list_strategies() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_cached_strategies_payload)


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


@app.get("/api/backtests", response_model=BacktestsListResponse)
async def list_backtests(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=50),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
    sharpe_min: float | None = Query(default=None),
    sharpe_max: float | None = Query(default=None),
    psr_min: float | None = Query(default=None),
    psr_max: float | None = Query(default=None),
    p_value_min: float | None = Query(default=None),
    p_value_max: float | None = Query(default=None),
    cagr_min: float | None = Query(default=None),
    cagr_max: float | None = Query(default=None),
    ) -> BacktestsListResponse:
    normalized_sort = _normalize_backtest_sort(sort)
    normalized_order = _normalize_order(order, default="desc")
    sorted_items = _backtest_meta_list_sorted(normalized_sort, normalized_order)
    sorted_items = _apply_backtest_numeric_filters(
        sorted_items,
        sharpe_min=sharpe_min,
        sharpe_max=sharpe_max,
        psr_min=psr_min,
        psr_max=psr_max,
        p_value_min=p_value_min,
        p_value_max=p_value_max,
        cagr_min=cagr_min,
        cagr_max=cagr_max,
    )
    total = len(sorted_items)
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]
    summaries = [_backtest_summary_from_meta(item) for item in page_items]
    return BacktestsListResponse(
        items=summaries,
        page=page,
        per_page=per_page,
        total=total,
        sort=normalized_sort,
        order=normalized_order,
    )


@app.get("/api/backtests/all", response_model=list[dict[str, Any]])
async def list_backtests_all(
    include_equity_preview: bool = Query(default=True),
    preview_limit: int = Query(default=50, ge=50, le=100),
) -> list[dict[str, Any]]:
    items = _snapshot_backtest_meta_cache()
    items.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    if include_equity_preview:
        backtest_ids = [str(item.get("id") or "").strip() for item in items]
        _bulk_warm_backtest_equity_preview(backtest_ids, limit=preview_limit)
        return [_backtest_all_item_with_equity_preview(item, preview_limit=preview_limit) for item in items]
    return [_backtest_all_item_from_meta(item) for item in items]


@app.get("/api/backtests/duplicates", response_model=BacktestsDuplicatesResponse)
async def list_backtest_duplicates() -> BacktestsDuplicatesResponse:
    groups, total_backtests = _group_duplicate_backtests()
    return BacktestsDuplicatesResponse(
        groups=groups,
        total_groups=len(groups),
        total_backtests=total_backtests,
    )


@app.post("/api/backtests/deduplicate", response_model=BacktestsDeduplicateResponse)
async def deduplicate_backtests() -> BacktestsDeduplicateResponse:
    groups, _ = _group_duplicate_backtests()
    deleted_ids: list[str] = []
    kept_ids: list[str] = []
    affected = 0

    for group in groups:
        backtests = group.get("backtests", [])
        if len(backtests) < 2:
            continue
        keep_id = str(backtests[0].get("id") or "")
        if keep_id:
            kept_ids.append(keep_id)

        removed_in_group = False
        for entry in backtests[1:]:
            backtest_id = str(entry.get("id") or "")
            if not backtest_id:
                continue
            if _delete_backtest_with_artifacts(backtest_id):
                deleted_ids.append(backtest_id)
                removed_in_group = True
        if removed_in_group:
            affected += 1

    return BacktestsDeduplicateResponse(
        deleted_ids=deleted_ids,
        kept_ids=kept_ids,
        groups_affected=affected,
    )


@app.post("/api/backtests/delete-bulk", response_model=dict[str, int])
async def delete_backtests_bulk(payload: BacktestsBulkDeleteRequest) -> dict[str, int]:
    deleted = 0
    seen: set[str] = set()
    for raw_id in payload.ids:
        backtest_id = str(raw_id or "").strip()
        if not backtest_id or backtest_id in seen:
            continue
        seen.add(backtest_id)
        if _delete_backtest_with_artifacts(backtest_id):
            deleted += 1
    return {"deleted": deleted}


@app.get("/api/backtest/{backtest_id}", response_model=dict[str, Any])
@app.get("/api/backtests/{backtest_id}", response_model=dict[str, Any])
async def get_backtest(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    try:
        return result.to_dict()
    except Exception as exc:
        logger.exception("Failed to serialize backtest '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/backtests/{backtest_id}", response_model=dict[str, Any])
async def delete_single_backtest(backtest_id: str) -> dict[str, Any]:
    if not _delete_backtest_with_artifacts(backtest_id):
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")
    return {"id": backtest_id, "status": "deleted"}


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
        _upsert_backtest_meta_cache(backtest_id)
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
@app.get("/api/backtests/{backtest_id}/info", response_model=dict[str, Any])
async def get_backtest_info(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return legacy_viewer._backtest_info_payload(result)


@app.get("/api/backtest/{backtest_id}/stats", response_model=dict[str, Any])
@app.get("/api/backtests/{backtest_id}/stats", response_model=dict[str, Any])
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
@app.get("/api/backtests/{backtest_id}/equity", response_model=list[dict[str, float]])
async def get_equity_curve(backtest_id: str) -> list[dict[str, float]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return legacy_viewer._equity_to_viewer(result)


@app.get("/api/backtests/{backtest_id}/equity-preview", response_model=list[float])
async def get_backtest_equity_preview(
    backtest_id: str,
    limit: int = Query(default=80, ge=50, le=100),
) -> list[float]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        raise HTTPException(status_code=400, detail="Backtest id is required")

    _ensure_backtest_meta_cache()
    with _BACKTEST_META_CACHE_LOCK:
        exists = normalized_id in _BACKTEST_META_CACHE
    if not exists:
        raise HTTPException(status_code=404, detail=f"Backtest '{normalized_id}' not found")
    return _cached_backtest_equity_preview(normalized_id, limit=limit)


@app.get("/api/backtest/{backtest_id}/capm", response_model=CapmResponse)
async def get_backtest_capm(
    backtest_id: str,
    benchmark: str = Query(default=""),
) -> CapmResponse:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    benchmark_symbol = str(benchmark or "").strip().upper() or _default_benchmark_symbol(result)
    if not benchmark_symbol:
        raise HTTPException(
            status_code=400,
            detail="No benchmark symbol provided and no backtest symbol available",
        )

    initial_capital = _safe_float(result.meta.initial_capital, 100000.0) or 100000.0
    equity_curve = _ensure_equity_curve(result, initial_capital=initial_capital)
    try:
        strategy_returns = _returns_from_equity_curve(equity_curve)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    benchmark_returns = _benchmark_daily_returns(
        benchmark_symbol,
        start=result.meta.backtest_start or "",
        end=result.meta.backtest_end or "",
    )

    try:
        capm = calculate_capm(strategy_returns, benchmark_returns)
        aligned = align_return_series(strategy_returns, benchmark_returns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    scatter = [
        CapmScatterPoint(
            x=float(row.benchmark_return),
            y=float(row.strategy_return),
        )
        for row in aligned.itertuples(index=False)
    ]

    beta = _safe_float(capm.get("beta"), 0.0)
    alpha_annualized = _safe_float(capm.get("alpha"), 0.0)
    return CapmResponse(
        alpha=alpha_annualized,
        beta=beta,
        r_squared=_safe_float(capm.get("r_squared"), 0.0),
        p_value=_safe_float(capm.get("p_value"), 1.0),
        scatter_data=scatter,
        regression_line=CapmRegressionLine(
            slope=beta,
            intercept=alpha_annualized / 252.0,
        ),
        benchmark_symbol=benchmark_symbol,
    )


@app.get("/api/backtest/{backtest_id}/trades", response_model=list[dict[str, Any]])
@app.get("/api/backtests/{backtest_id}/trades", response_model=list[dict[str, Any]])
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

        points_by_time: dict[float, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            raw_time = point.get("time")
            unix_time = (
                float(raw_time)
                if isinstance(raw_time, (int, float))
                else legacy_viewer._iso_to_unix(str(raw_time))
            )
            if not math.isfinite(unix_time) or unix_time <= 0:
                continue
            normalized_point = dict(point)
            normalized_point["time"] = float(unix_time)
            if "value" in normalized_point:
                raw_value = normalized_point.get("value")
                if raw_value is None:
                    normalized_point["value"] = None
                else:
                    try:
                        numeric_value = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                    normalized_point["value"] = (
                        round(numeric_value, 6) if math.isfinite(numeric_value) else None
                    )
            points_by_time[float(unix_time)] = normalized_point
        indicator["data"] = [points_by_time[key] for key in sorted(points_by_time.keys())]

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


@app.post("/api/backtests/run", response_model=dict[str, Any], status_code=202)
async def run_backtest(payload: BacktestRunRequest) -> dict[str, Any]:
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

    engine = str(payload.engine or "native").strip() or "native"
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

    task_id = str(uuid.uuid4())[:8]
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    stage_total = 6
    record = _create_task_record(
        task_id,
        name=f"Backtest {strategy_path.stem}",
        description=(
            f"Run {strategy_path.stem} on {', '.join(symbols)} "
            f"({timeframe}, engine={engine})."
        ),
        kind="backtest",
        source=engine,
        total_items=stage_total,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record
    _set_task_status(task_id, "queued")

    _running_jobs[task_id] = {
        "status": "queued",
        "result": None,
        "error": None,
        "task_id": task_id,
        "job_id": task_id,
    }
    _update_task_fields(
        task_id,
        restart_config={
            "strategy": strategy,
            "engine": engine,
            "symbols": symbols,
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "capital": capital,
            "risk_per_trade_pct": risk_pct,
            "max_position_pct": max_position_pct,
            "max_leverage": max_leverage,
            "modeling_resolution": payload.modeling_resolution,
            "intrabar_resolution": payload.intrabar_resolution,
            "parameters": dict(params),
        },
    )

    def _runner_log_message(raw_line: str) -> str:
        text = str(raw_line or "").strip()
        if not text:
            return ""
        try:
            payload_json = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload_json, dict):
            message = payload_json.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return text

    def _runner_stage(message: str) -> tuple[float, str] | None:
        normalized = message.lower()
        if normalized.startswith("progress:"):
            payload = message.split(":", 2)
            if len(payload) == 3:
                try:
                    percent = float(payload[1])
                except ValueError:
                    return None
                progress_message = payload[2].strip() or "Running backtest..."
                return (percent, progress_message)
            return None
        if "auto-detected date range" in normalized:
            return (15.0, "Loading market data...")
        if "starting command:" in normalized:
            return (20.0, "Loading market data...")
        if "running backtest:" in normalized:
            return (35.0, f"Running strategy {strategy_path.stem}...")
        if "result:" in normalized and "trades" in normalized:
            return (65.0, "Processing trades...")
        if normalized.startswith("result:"):
            return (80.0, "Calculating statistics...")
        if normalized.startswith("saved to:"):
            return (92.0, "Saving results...")
        return None

    def _run() -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        cmd = [
            sys.executable,
            "-u",
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
            "--progress-stdout",
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
        _update_task_fields(task_id, command=list(cmd))

        latest_message = ""
        output_path = ""
        backtest_id = ""
        stage_progress = 0.0

        def report_stage(
            handle,
            *,
            percent: float,
            message: str,
            write_log_line: bool = True,
        ) -> None:
            nonlocal stage_progress
            if percent <= stage_progress:
                return
            stage_progress = percent
            _update_task_progress(
                task_id,
                percent=percent,
                current=message,
                total=stage_total,
            )
            if write_log_line:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] {message}",
                    stream="task",
                    source="backtest",
                )

        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            try:
                _set_task_status(task_id, "running")
                _running_jobs[task_id]["status"] = "running"
                report_stage(handle, percent=10.0, message="Loading market data...")

                command_display = " ".join(str(part) for part in cmd)
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                    source="backtest",
                )

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    text=True,
                    bufsize=1,
                )
                _update_task_fields(task_id, pid=proc.pid, process=proc)

                if proc.stdout is not None:
                    for raw_line in proc.stdout:
                        line = raw_line.rstrip("\n")
                        if line:
                            _write_task_log_line(
                                handle,
                                task_id,
                                line,
                                stream="stdout",
                                source="backtest",
                            )
                        parsed_message = _runner_log_message(line)
                        if not parsed_message:
                            continue
                        latest_message = parsed_message
                        stage = _runner_stage(parsed_message)
                        if stage is not None:
                            percent, message = stage
                            report_stage(handle, percent=percent, message=message)

                        if parsed_message.lower().startswith("saved to:"):
                            output_path = parsed_message.split(":", 1)[1].strip()
                            if output_path:
                                filename = Path(output_path).name
                                if filename.endswith(".trades.json"):
                                    filename_id = filename[: -len(".trades.json")]
                                    backtest_id = filename_id
                                    if filename_id:
                                        loaded_result = _load_result(filename_id)
                                        if loaded_result is not None:
                                            meta_id = str(getattr(loaded_result.meta, "id", "") or "").strip()
                                            if meta_id:
                                                backtest_id = meta_id
                                _update_task_fields(
                                    task_id,
                                    output_path=output_path,
                                    backtest_id=backtest_id or None,
                                )

                try:
                    returncode = proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    if proc.poll() is None:
                        proc.kill()
                    raise subprocess.SubprocessError("Backtest timed out after 300 seconds")
                if returncode == 0:
                    report_stage(handle, percent=92.0, message="Saving results...")
                    result_payload = {
                        "output_path": output_path or None,
                        "backtest_id": backtest_id or None,
                    }
                    compact_result = {key: value for key, value in result_payload.items() if value}
                    final_trade_count = 0
                    if backtest_id:
                        loaded_result = _load_result(backtest_id)
                        if loaded_result is not None:
                            final_trade_count = len(loaded_result.trades)
                    if final_trade_count == 0 and isinstance(latest_message, str):
                        match = re.search(r"result:\s*(\d+)\s+trades", latest_message.lower())
                        if match:
                            try:
                                final_trade_count = int(match.group(1))
                            except ValueError:
                                final_trade_count = 0
                    _upsert_backtest_meta_cache(backtest_id or "")
                    _update_task_fields(task_id, result=compact_result)
                    report_stage(
                        handle,
                        percent=100.0,
                        message=f"Completed - {final_trade_count} trades found",
                    )
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Backtest completed successfully.",
                        stream="task",
                        source="backtest",
                    )
                    _mark_task_finished(task_id, status="completed", returncode=0)
                    _running_jobs[task_id]["status"] = "completed"
                    _running_jobs[task_id]["result"] = compact_result
                    _running_jobs[task_id]["error"] = None
                else:
                    error_text = latest_message or f"Backtest command exited with code {returncode}"
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Backtest failed: {error_text}",
                        stream="stderr",
                        log_level="error",
                        source="backtest",
                    )
                    _mark_task_finished(
                        task_id,
                        status="failed",
                        returncode=returncode,
                        error=error_text,
                    )
                    _running_jobs[task_id]["status"] = "failed"
                    _running_jobs[task_id]["result"] = None
                    _running_jobs[task_id]["error"] = error_text
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Backtest failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="backtest",
                )
                _write_task_traceback(handle, task_id, traceback.format_exc(), source="backtest")
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
                _running_jobs[task_id]["status"] = "failed"
                _running_jobs[task_id]["result"] = None
                _running_jobs[task_id]["error"] = str(exc)
            finally:
                _update_task_fields(task_id, process=None)

    thread = threading.Thread(target=_run, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "job_id": task_id, "status": "queued"}


@app.get("/api/backtests/run/{job_id}/status", response_model=dict[str, Any])
async def get_backtest_run_status(job_id: str) -> dict[str, Any]:
    task = _snapshot_task(job_id)
    if task is not None and task.get("kind") == "backtest":
        progress = dict(task.get("progress_detail") or {})
        progress.setdefault("percent", float(task.get("progress", 0.0) or 0.0))
        return {
            "job_id": job_id,
            "task_id": job_id,
            "status": str(task.get("status", "unknown")),
            "result": task.get("result"),
            "error": task.get("error"),
            "progress": progress,
        }

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
    _set_task_status(task_id, "queued")

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
    return {"task_id": task_id, "job_id": task_id, "status": "queued"}


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


@app.get("/api/live/accounts", response_model=list[dict[str, Any]])
async def get_live_accounts() -> list[dict[str, Any]]:
    _log_live_endpoint_request("/api/live/accounts")
    return await asyncio.to_thread(_cached_live_accounts_payload)


@app.get("/api/live/deployments", response_model=list[dict[str, Any]])
async def get_live_deployments() -> list[dict[str, Any]]:
    _log_live_endpoint_request("/api/live/deployments")
    deployments = await asyncio.to_thread(list_live_deployments)
    return [_serialize_live_deployment_payload(deployment) for deployment in deployments]


@app.post("/api/live/deployments", response_model=dict[str, Any], status_code=201)
async def create_live_deployment_endpoint(
    payload: LiveDeploymentCreateRequest,
) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/deployments",
        strategy_name=payload.strategy_name,
        account_id=payload.account_id,
        symbols=payload.symbols,
        timeframe=payload.timeframe,
    )
    account_payload = await asyncio.to_thread(_ensure_live_account_for_deployment, payload.account_id)
    inferred_broker = str(account_payload.get("broker", "")).strip().lower()
    if inferred_broker not in {"alpaca", "ctrader"}:
        raise HTTPException(status_code=400, detail="Unknown broker for account")
    deployment = LiveDeploymentConfig(
        strategy_name=payload.strategy_name,
        account_id=payload.account_id,
        symbols=payload.symbols,
        timeframe=payload.timeframe,
        status="ACTIVE",
        settings=payload.settings or {},
    )
    try:
        created = await asyncio.to_thread(create_live_deployment, deployment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_live_deployment_payload(created)


@app.put("/api/live/deployments/{deployment_id}/status", response_model=dict[str, Any])
async def update_live_deployment_status_endpoint(
    deployment_id: str, payload: LiveDeploymentStatusUpdateRequest
) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/deployments/{deployment_id}/status",
        deployment_id=deployment_id,
        status=payload.status,
    )
    try:
        updated = await asyncio.to_thread(
            update_live_deployment_status, deployment_id, payload.status
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Live deployment not found")
    return _serialize_live_deployment_payload(updated)


@app.delete("/api/live/deployments/{deployment_id}", response_model=dict[str, Any])
async def delete_live_deployment_endpoint(deployment_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/deployments/{deployment_id}",
        deployment_id=deployment_id,
    )
    deleted = await asyncio.to_thread(delete_live_deployment, deployment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Live deployment not found")
    return {"status": "deleted", "id": deployment_id}


@app.get("/api/live/balances/{account_id}", response_model=dict[str, Any])
async def get_live_balances(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/balances/{account_id}",
        account_id=account_id,
    )
    records = await asyncio.to_thread(list_live_balances, account_id)
    series: list[dict[str, Any]] = []
    for record in records:
        iso_ts, unix_ts = _serialize_live_timestamp(record.timestamp)
        series.append(
            {
                "id": str(record.id),
                "account_id": record.account_id,
                "timestamp": iso_ts,
                "time": unix_ts,
                "balance": float(record.balance),
                "equity": float(record.equity),
            }
        )
    return {"account_id": account_id, "series": series}


@app.get("/api/live/trades/{account_id}", response_model=dict[str, Any])
async def get_live_trades(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/trades/{account_id}",
        account_id=account_id,
    )
    deployments = await asyncio.to_thread(list_live_deployments_for_account, account_id)
    deployment_ids = _deployment_ids_for_account(account_id, deployments)
    rows = await asyncio.to_thread(list_live_signal_executions_for_deployments, deployment_ids)

    trades: list[dict[str, Any]] = []
    for row in rows:
        signal_iso, signal_unix = _serialize_live_timestamp(row.get("signal_timestamp"))
        execution_iso, execution_unix = _serialize_live_timestamp(row.get("execution_timestamp"))
        trades.append(
            {
                "signal_id": str(row.get("signal_id", "")),
                "execution_id": str(row.get("execution_id", "")),
                "deployment_id": row.get("deployment_id", ""),
                "account_id": account_id,
                "symbol": row.get("symbol", ""),
                "action": row.get("action", ""),
                "theoretical_price": float(row.get("theoretical_price", 0.0)),
                "actual_price": float(row.get("actual_price", 0.0)),
                "slippage": float(row.get("slippage", 0.0)),
                "slippage_bps": float(row.get("slippage_bps", 0.0)),
                "filled_qty": float(row.get("filled_qty", 0.0)),
                "commission": float(row.get("commission", 0.0)),
                "signal_timestamp": signal_iso,
                "signal_time": signal_unix,
                "execution_timestamp": execution_iso,
                "execution_time": execution_unix,
                "broker_order_id": row.get("broker_order_id", ""),
                "signal_metadata": row.get("signal_metadata", {}),
            }
        )
    return {"account_id": account_id, "deployment_ids": deployment_ids, "trades": trades}


@app.get("/api/live/stats/{account_id}", response_model=dict[str, Any])
async def get_live_stats(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/stats/{account_id}",
        account_id=account_id,
    )
    return await asyncio.to_thread(_live_account_stats_payload, account_id)


@app.get("/api/data/overview", response_model=dict[str, Any])
async def data_overview() -> dict[str, Any]:
    data_views = _get_data_views()
    result = [dict(item) for item in data_views.get("overview_categories", [])]

    dolthub_repos = _scan_dolthub_repos()
    if dolthub_repos:
        result.append(
            {
                "name": "Dolthub Repos",
                "symbol_count": len({repo.get("repo") for repo in dolthub_repos}),
                "file_count": len(dolthub_repos),
                "total_size_mb": round(
                    sum(repo.get("size_bytes", 0) for repo in dolthub_repos) / (1024 * 1024), 2
                ),
                "start": None,
                "end": None,
            }
        )

    return {"categories": sorted(result, key=lambda item: str(item.get("name") or ""))}


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
        return {
            "symbols": [
                _metadata_only_data_symbol(repo)
                for repo in sorted(repos, key=lambda item: item["symbol"] or "")
            ]
        }

    data_views = _get_data_views()
    symbols = [
        _metadata_only_data_symbol(item)
        for item in data_views.get("category_symbols", {}).get(target, [])
    ]

    return {
        "symbols": sorted(
            symbols,
            key=lambda item: (item.get("symbol") or "", item.get("timeframe") or ""),
        )
    }


@app.post("/api/data/cache/invalidate", response_model=dict[str, Any])
async def invalidate_data_cache(
    reason: str = Query(default="manual"),
    include_validation: bool | None = Query(default=None),
) -> dict[str, Any]:
    return _manual_cache_invalidation_payload(
        reason=reason,
        include_validation=include_validation,
    )


@app.post("/api/data/download", response_model=dict[str, Any])
async def data_download(payload: DataDownloadRequest) -> dict[str, Any]:
    source = str(payload.source or "dukascopy").strip().lower()
    symbols = payload.symbols
    timeframes = payload.timeframes
    start = payload.start
    end = payload.end
    provided_fields = (
        payload.model_fields_set
        if hasattr(payload, "model_fields_set")
        else getattr(payload, "__fields_set__", set())
    )
    offer_side_value = payload.offer_side
    if payload.price_type and "offer_side" not in provided_fields:
        offer_side_value = payload.price_type
    offer_side = str(offer_side_value or "bid").strip().lower()
    if offer_side not in {"bid", "ask", "both"}:
        raise HTTPException(status_code=400, detail="offer_side must be one of: bid, ask, both")
    merge_tick = bool(payload.merge_tick or payload.merge_bid_ask)
    merge_s1 = bool(payload.merge_s1 or payload.merge_bid_ask)
    batch_size = _safe_int(payload.batch_size if payload.batch_size is not None else payload.concurrency, 10)
    pause_ms = _safe_int(payload.pause_ms, 1000)
    query_timeout_seconds = _safe_int(payload.query_timeout_seconds, 900)
    symbol_list = _parse_symbol_list(symbols)
    timeframe_list = [
        _normalize_dukascopy_genesis_timeframe(timeframe)
        for timeframe in _parse_timeframe_list(timeframes)
    ]

    if batch_size < 1 or batch_size > 50:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 50")
    if pause_ms < 0 or pause_ms > 60000:
        raise HTTPException(status_code=400, detail="pause_ms must be between 0 and 60000")
    if source in {"dukascopy", "jforex"} and (
        query_timeout_seconds < 30 or query_timeout_seconds > 7200
    ):
        raise HTTPException(status_code=400, detail="query_timeout_seconds must be between 30 and 7200")

    if source in {"dukascopy", "jforex"} and start:
        genesis_index = _load_dukascopy_genesis()
        too_early: dict[str, str] = {}
        for symbol in symbol_list:
            symbol_data = genesis_index.get(symbol, {})
            for timeframe in timeframe_list:
                genesis = symbol_data.get(timeframe)
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

    if source in {"dukascopy", "jforex"}:
        source_label = "dukascopy" if source == "dukascopy" else "jforex"
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.jforex_dl",
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
            "--query-timeout-seconds",
            str(query_timeout_seconds),
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
        if offer_side:
            cmd.extend(["--price-type", offer_side])

        total_items = max(len(symbol_list) * len(timeframe_list), 1)
        result = _start_jforex_task(
            task_id,
            cmd,
            source_label=source_label,
            allow_native_fallback=(source_label == "dukascopy"),
            symbols=symbol_list,
            timeframes=timeframe_list,
            start=start or None,
            end=end or None,
            offer_side=offer_side,
            merge_tick=merge_tick,
            merge_s1=merge_s1,
            batch_size=batch_size,
            pause_ms=pause_ms,
            query_timeout_seconds=query_timeout_seconds,
            total_items=total_items,
            name=name,
            description=description,
        )
        _apply_recovery_data_metadata(
            task_id,
            command=[str(part) for part in cmd],
            downloader=source_label,
            symbols=symbol_list,
            timeframes=timeframe_list,
            start=start or None,
            end=end or None,
            offer_side=offer_side,
            price_type=offer_side,
            merge_tick=merge_tick,
            merge_s1=merge_s1,
            batch_size=batch_size,
            pause_ms=pause_ms,
            query_timeout_seconds=query_timeout_seconds,
        )
        return result

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
    result = _start_data_task(
        task_id,
        cmd,
        total_items=total_items,
        name=name,
        description=description,
        source=source,
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader=source,
        symbols=symbol_list,
        timeframes=timeframe_list,
        start=start or None,
        end=end or None,
    )
    return result


def _manual_cache_invalidation_payload(
    *,
    reason: str,
    include_validation: bool | None = None,
) -> dict[str, Any]:
    removed_entries = cache_manager.invalidate_cache(
        DATA_DIR,
        include_validation=include_validation,
        reason=reason,
    )
    _invalidate_data_views_cache()
    return {
        "status": "ok",
        "removed_entries": removed_entries,
        "reason": reason,
        "include_validation": include_validation,
        "cache_stats": cache_manager.get_cache_stats(),
    }


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
    coverage = _collect_dukascopy_genesis(
        genesis_index,
        symbols=requested_symbols or None,
        timeframes=requested_timeframes or None,
    )
    return {
        "source": "dukascopy",
        "coverage": coverage,
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
    result = _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Resample data",
        description=f"Resample {Path(source_path).name or source_path} into {target_timeframes}.",
        source="resample",
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader="resample",
    )
    return result


@app.post("/api/data/harvest", response_model=dict[str, Any])
async def data_harvest(payload: DataHarvestRequest) -> dict[str, Any]:
    source = str(payload.source or "dukascopy").strip().lower()
    if source != "dukascopy":
        raise HTTPException(status_code=400, detail="Smart Harvest currently supports only source='dukascopy'")

    symbol = str(payload.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    asset_class = str(payload.asset_class or "").strip().lower()
    start = str(payload.start or "").strip()

    task_id = str(uuid.uuid4())
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "scripts.adaptive_harvester",
        "--symbol",
        symbol,
    ]
    if asset_class:
        cmd.extend(["--asset_class", asset_class])
    if start:
        cmd.extend(["--start", start])

    result = _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name=f"Smart Harvest {symbol}",
        description=f"Adaptive auto-tuned Dukascopy harvest for {symbol}.",
        source="harvest",
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader="harvest",
        symbols=[symbol],
        start=start or None,
    )
    return result


@app.post("/api/data/validate", response_model=dict[str, Any])
async def data_validate(payload: DataValidateRequest) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    cmd = [sys.executable, "-u", "-m", "data_tools.validate", "--data-dir", str(DATA_DIR)]
    if payload.symbol:
        cmd.extend(["--symbol", payload.symbol])
    if payload.report:
        cmd.extend(["--report", payload.report])
    result = _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Validate data",
        description="Validate OHLCV data quality and structure.",
        source="validate",
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader="validate",
        symbols=[str(payload.symbol).strip().upper()] if payload.symbol else None,
    )
    return result


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

    result = _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Calibrate Dukascopy genesis",
        description=f"Discover genesis boundaries for {symbols}.",
        source="calibrate",
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader="calibrate",
        symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()],
        timeframes=(
            [tf.strip().lower() for tf in str(payload.timeframes).split(",") if tf.strip()]
            if payload.timeframes
            else None
        ),
    )
    return result


@app.get("/api/data/tasks", response_model=dict[str, Any])
async def list_data_tasks() -> dict[str, Any]:
    records = await asyncio.to_thread(list_background_task_records)
    with DATA_TASKS_LOCK:
        for record in records:
            task_id = str(record.get("id") or "").strip()
            if not task_id or task_id in DATA_TASKS:
                continue
            DATA_TASKS[task_id] = _build_task_from_record(record)

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


@app.post("/api/data/tasks/{task_id}/restart", response_model=dict[str, Any])
async def restart_data_task(task_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_restart_interrupted_task, task_id)


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
    await asyncio.to_thread(delete_background_task_record, task_id)
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
        cache_manager.invalidate_cache(DATA_DIR, reason="manual")
        _invalidate_data_views_cache()
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
    payload = _get_data_views().get("symbols_payload", {"symbols": [], "categories": {}})
    return SymbolsResponse(
        symbols=list(payload.get("symbols", [])),
        categories=dict(payload.get("categories", {})),
    )


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

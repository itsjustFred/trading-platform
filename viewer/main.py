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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import numpy as np
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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field as PydanticField
from pydantic import ValidationError
from sqlmodel import SQLModel
from starlette.middleware.gzip import GZipMiddleware

from data_tools.catalog_builder import merge_ohlcv_entries
from data_tools import cache_manager
from data_tools.signal_import import load_signals_from_csv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
# Avoid pynecore TOML auto-writes at import-time in the web process.
os.environ.setdefault("PYNE_SAVE_SCRIPT_TOML", "0")

from shared.constants import (
    DATA_DIR as RESOLVED_DATA_DIR,
    LOG_DIR as RESOLVED_LOG_DIR,
    normalize_timeframe,
)

DATA_DIR = RESOLVED_DATA_DIR
os.environ.setdefault("DATA_DIR", str(DATA_DIR))
os.environ.setdefault("TRADING_DATA_DIR", str(DATA_DIR))

LOG_DIR = RESOLVED_LOG_DIR
os.environ.setdefault("LOG_DIR", str(LOG_DIR))

from shared.logging import get_logger
from shared.models import (
    BacktestResult,
    EquityPoint,
    Service,
    ServiceStatus,
    ServiceType,
    VerificationEvidenceRecord,
    VerificationEvidenceSummary,
    Portfolio as PortfolioModel,
    PortfolioComponent as PortfolioComponentModel,
)
from shared.database import (
    DB_PATH as BACKTEST_DB_PATH,
    create_live_deployment,
    delete_background_task_record,
    delete_backtest,
    delete_portfolio as delete_portfolio_record,
    delete_live_deployment,
    ensure_live_account,
    get_background_task_record,
    get_live_account,
    get_service,
    get_latest_live_balance,
    init_db,
    list_backtest_ids,
    list_backtest_fingerprint_rows,
    list_backtest_metadata_rows,
    list_portfolios as list_portfolio_records,
    list_verification_evidence_records,
    list_verification_evidence_summary,
    list_background_task_records,
    list_live_accounts,
    list_live_balances,
    list_live_deployments,
    list_live_deployments_for_account,
    list_services,
    list_live_signals_for_replay,
    list_live_signals_for_deployments,
    list_live_signal_executions_for_deployments,
    load_backtest_metadata_row,
    load_backtest_equity_preview_points,
    load_backtest_equity_preview_map,
    load_live_account_stats_inputs,
    load_backtest_result,
    load_portfolio as load_portfolio_record,
    mark_background_tasks_interrupted,
    migrate_json_to_sqlite,
    save_result_to_sqlite,
    save_portfolio as save_portfolio_record,
    upsert_verification_evidence,
    upsert_background_task_record,
    create_service,
    delete_service,
    update_live_deployment_status,
    update_service_status,
)
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from risk.config import RiskConfig
from signal_bots.registry import get_signal_processor, list_signal_processor_options
try:
    from live_runner.account_discovery import discover_configured_accounts
except ModuleNotFoundError:  # pragma: no cover - optional dependency for account discovery
    async def discover_configured_accounts() -> list[dict[str, Any]]:
        return []
from shared.models import (
    LiveDeploymentConfig,
    LiveDeploymentCreateRequest,
    LiveDeploymentStatusUpdateRequest,
)
from runner.capm import align_return_series, calculate_capm
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    StrategyEntry,
    iter_strategy_files,
    load_strategy_meta,
    matches_strategy_identifier,
    strategy_identifier_aliases,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
)
from live_runner.market_data_stream import LiveMarketDataStreamService
from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from viewer import core as legacy_viewer

logger = get_logger("viewer")
AGENT_CONVERT_PROVIDER_NAMES: tuple[str, ...] = ("nim", "copilot_sdk")

BACKTESTS_DIR = DATA_DIR / "backtests"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
REACT_DIST = PROJECT_ROOT / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).resolve().parent / "static"
_STATIC_ASSET_SUFFIXES = {
    ".js",
    ".mjs",
    ".css",
    ".map",
    ".json",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".txt",
    ".xml",
    ".webmanifest",
}
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOG_DIR / "tasks"
OPTIMIZATIONS_DIR = legacy_viewer.OPTIMIZATIONS_DIR
ALL_LOGS_NAME = legacy_viewer.ALL_LOGS_NAME
BARS_BEFORE = legacy_viewer.BARS_BEFORE
BARS_AFTER = legacy_viewer.BARS_AFTER
REPLAY_MAX_BARS_WITHOUT_TRADES = 15_000
REPLAY_TARGET_BARS = 50_000
REPLAY_MIN_BARS_PER_EVENT = 4
REPLAY_MAX_BARS_PER_EVENT = (BARS_BEFORE * 2) + (BARS_AFTER * 3)

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
DEFAULT_LLM_PROVIDER_NAME = legacy_viewer.DEFAULT_LLM_PROVIDER_NAME
SUPPORTED_LLM_PROVIDER_NAMES = legacy_viewer.SUPPORTED_LLM_PROVIDER_NAMES
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

_JSON_MIGRATION_STATUS_LOCK = threading.Lock()
_JSON_MIGRATION_STATUS = "pending"


def _run_json_to_sqlite_migration() -> None:
    global _JSON_MIGRATION_STATUS
    with _JSON_MIGRATION_STATUS_LOCK:
        if _JSON_MIGRATION_STATUS in {"running", "completed"}:
            return
        _JSON_MIGRATION_STATUS = "running"
    try:
        summary = migrate_json_to_sqlite()
        logger.info(
            "Startup JSON→SQLite migration finished",
            extra={"data": {"summary": summary}},
        )
        with _JSON_MIGRATION_STATUS_LOCK:
            _JSON_MIGRATION_STATUS = "completed"
    except Exception as exc:
        with _JSON_MIGRATION_STATUS_LOCK:
            _JSON_MIGRATION_STATUS = "failed"
        logger.warning("Startup JSON→SQLite migration failed: %s", exc)

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


async def _prewarm_portfolio_metadata_background() -> None:
    try:
        count = await asyncio.to_thread(_refresh_portfolio_meta_cache)
        logger.info("Portfolio metadata cache warmed (%s items)", count)
    except Exception as exc:
        logger.warning("Portfolio metadata prewarm failed: %s", exc)


async def _prewarm_tasks_cache_background() -> None:
    try:
        count = await asyncio.to_thread(_hydrate_task_cache_once, True)
        logger.info("Task cache warmed (%s items)", count)
    except Exception as exc:
        logger.warning("Task cache prewarm failed: %s", exc)


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
    await asyncio.to_thread(init_db)
    json_migration_task = asyncio.create_task(asyncio.to_thread(_run_json_to_sqlite_migration))
    data_views_prewarm_task = asyncio.create_task(_prewarm_data_views_background())
    live_accounts_prewarm_task = asyncio.create_task(_prewarm_live_accounts_background())
    strategies_prewarm_task = asyncio.create_task(_prewarm_strategies_background())
    backtests_meta_prewarm_task = asyncio.create_task(_prewarm_backtest_metadata_background())
    portfolios_meta_prewarm_task = asyncio.create_task(_prewarm_portfolio_metadata_background())
    task_cache_prewarm_task = asyncio.create_task(_prewarm_tasks_cache_background())
    jforex_jvm_prewarm_task = asyncio.create_task(_prewarm_jforex_jvm_background())
    live_market_data_sync_task = asyncio.create_task(_sync_live_market_data_stream_background())
    app.state.json_migration_task = json_migration_task
    app.state.data_views_prewarm_task = data_views_prewarm_task
    app.state.live_accounts_prewarm_task = live_accounts_prewarm_task
    app.state.strategies_prewarm_task = strategies_prewarm_task
    app.state.backtests_meta_prewarm_task = backtests_meta_prewarm_task
    app.state.portfolios_meta_prewarm_task = portfolios_meta_prewarm_task
    app.state.task_cache_prewarm_task = task_cache_prewarm_task
    app.state.jforex_jvm_prewarm_task = jforex_jvm_prewarm_task
    app.state.live_market_data_sync_task = live_market_data_sync_task
    await asyncio.to_thread(mark_background_tasks_interrupted, from_statuses=["queued", "running"])
    await asyncio.to_thread(_resume_interrupted_tasks)
    try:
        yield
    finally:
        for task in (
            json_migration_task,
            data_views_prewarm_task,
            live_accounts_prewarm_task,
            strategies_prewarm_task,
            backtests_meta_prewarm_task,
            portfolios_meta_prewarm_task,
            task_cache_prewarm_task,
            jforex_jvm_prewarm_task,
            live_market_data_sync_task,
        ):
            if not task.done():
                task.cancel()
        for task in (
            json_migration_task,
            data_views_prewarm_task,
            live_accounts_prewarm_task,
            strategies_prewarm_task,
            backtests_meta_prewarm_task,
            portfolios_meta_prewarm_task,
            task_cache_prewarm_task,
            jforex_jvm_prewarm_task,
            live_market_data_sync_task,
        ):
            with suppress(asyncio.CancelledError):
                await task
        with suppress(Exception):
            await _LIVE_STREAM_BROKER.shutdown()
        with suppress(Exception):
            await _live_market_data_service.stop()


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


class ServiceCreateRequest(SQLModel):
    name: str
    type: Literal["discord_bot", "scheduled_job"]
    status: Literal["running", "paused", "failed", "stopped"] = "stopped"
    config: dict[str, Any] = {}


class ServiceStatusUpdateRequest(SQLModel):
    status: Literal["running", "paused", "failed", "stopped"]


class VerificationEvidenceCreateRequest(SQLModel):
    component: str
    flow_name: str
    category: str
    status: Literal["success", "failed", "partial", "skipped"]
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    git_sha: str | None = None
    environment: str | None = None
    mode: str | None = None
    evidence_metadata: dict[str, Any] = PydanticField(default_factory=dict, alias="metadata")
    error_summary: str | None = None
    evidence_ref: str | None = None
    manual_validation_required: bool = False
    source: str = ""
    run_id: str | None = None


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


class SignalReplayInput(SQLModel):
    timestamp: str
    symbol: str
    action: str
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    timeframe: str | None = None
    source: str | None = None
    signal_metadata: dict[str, Any] = PydanticField(default_factory=dict, alias="metadata")


class BacktestRunSignalsRequest(SQLModel):
    signals: list[SignalReplayInput] = []
    signal_source: str | None = None
    signal_processor: str | None = None
    account_id: str | None = None
    start_date: str
    end_date: str
    timeframe: str = "1h"
    capital: float = 100000.0
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_positions: int | None = None
    auto_download: bool = True


class BacktestRunFromSignalsRequest(SQLModel):
    signal_source: str = "csv"
    filepath: str = ""
    signal_processor: str | None = None
    account_id: str | None = None
    start_date: str
    end_date: str
    timeframe: str = ""
    capital: float = 100000.0
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_positions: int | None = None
    auto_download: bool = True


class PortfolioStrategyConfig(SQLModel):
    backtest_id: str = ""
    name: str = ""
    risk_per_trade_pct: float = 1.0
    max_positions: int = 99


class PortfolioComponentConfig(SQLModel):
    strategy_name: str = ""
    symbol: str = ""
    timeframe: str = "1h"
    weight: float = 1.0
    risk_config: dict[str, Any] = PydanticField(default_factory=dict)
    settings: dict[str, Any] = PydanticField(default_factory=dict)
    backtest_id: str | None = None


class CreatePortfolioRequest(SQLModel):
    name: str = "Untitled"
    account_capital: float = 100000.0
    strategies: list[PortfolioStrategyConfig] = []
    components: list[PortfolioComponentConfig] = []


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
    provider: str | None = None
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
    pages: int = 1
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


class PortfoliosDuplicatesResponse(SQLModel):
    groups: list[dict[str, Any]]
    total_groups: int
    total_portfolios: int


class PortfoliosDeduplicateResponse(SQLModel):
    deleted_ids: list[str]
    kept_ids: list[str]
    groups_affected: int


class BacktestsBulkDeleteRequest(SQLModel):
    ids: list[str]


class PortfoliosBulkDeleteRequest(SQLModel):
    ids: list[str]


_QUIET_EXACT_PATHS = {
    "/api/tasks",
    "/api/data/tasks",
    "/api/live/accounts",
    "/api/live/deployments",
}
_QUIET_PREFIX_PATHS = (
    "/api/tasks/",
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


def _configure_third_party_log_filters() -> None:
    # Alpaca stream internals emit noisy plain-text websocket logs that bypass our
    # structured logger; silence them and rely on live_runner structured warnings.
    alpaca_ws_logger = logging.getLogger("alpaca.data.live.websocket")
    alpaca_ws_logger.handlers.clear()
    alpaca_ws_logger.propagate = False
    alpaca_ws_logger.setLevel(logging.CRITICAL)


_configure_uvicorn_access_log_filter()
_configure_third_party_log_filters()

_live_market_data_service = LiveMarketDataStreamService(max_bars_per_symbol=500)


async def _sync_live_market_data_stream_background() -> None:
    while True:
        try:
            deployments = await asyncio.to_thread(list_live_deployments)
            accounts = await asyncio.to_thread(list_live_accounts)
            account_lookup = {account.id: account for account in accounts}
            await _live_market_data_service.refresh_from_deployments(deployments, account_lookup)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Live market-data stream sync failed: %s", exc)
        await asyncio.sleep(10)


def _is_static_asset_request(path: str) -> bool:
    return Path(path).suffix.lower() in _STATIC_ASSET_SUFFIXES


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
            if _is_static_asset_request(rel_path):
                # Missing static asset must not fall back to index.html,
                # otherwise browsers report MIME errors for module scripts.
                return await call_next(request)
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled server exception",
        extra={
            "data": {
                "method": request.method,
                "path": request.url.path,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        },
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


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
_LIVE_WS_QUEUE_MAX_SIZE = 64
_LIVE_WS_HEARTBEAT_SECONDS = 15.0
_LIVE_WS_ACCOUNTS_POLL_SECONDS = 1.0
_LIVE_WS_SIGNALS_POLL_SECONDS = 1.0
_LIVE_WS_EXECUTIONS_POLL_SECONDS = 1.0
_LIVE_WS_POSITIONS_POLL_SECONDS = 1.0
_BACKTEST_META_CACHE_LOCK = threading.RLock()
_BACKTEST_META_CACHE: dict[str, dict[str, Any]] = {}
_BACKTEST_META_CACHE_SOURCE_MTIME_NS = 0
_BACKTEST_EQUITY_PREVIEW_CACHE_LOCK = threading.RLock()
_BACKTEST_EQUITY_PREVIEW_CACHE: dict[tuple[str, int], list[float]] = {}
_PORTFOLIO_META_CACHE_LOCK = threading.RLock()
_PORTFOLIO_META_CACHE: dict[str, dict[str, Any]] = {}
_PORTFOLIO_META_CACHE_WARMED = False
_TASK_CACHE_HYDRATION_LOCK = threading.RLock()
_TASK_CACHE_HYDRATED = False
_TASK_STATUS_WEIGHTS: dict[str, float] = {
    "queued": 5.0,
    "starting": 10.0,
    "running": 50.0,
    "completed": 100.0,
    "failed": 100.0,
    "cancelled": 100.0,
    "interrupted": 100.0,
}


@dataclass
class _LiveStreamTopicState:
    topic: str
    fetch_payload: Callable[[], Awaitable[dict[str, Any]]]
    poll_seconds: float
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    last_signature: str | None = None
    last_payload: dict[str, Any] | None = None
    producer_task: asyncio.Task[None] | None = None


class _LiveStreamBroker:
    def __init__(self) -> None:
        self._topics: dict[str, _LiveStreamTopicState] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        *,
        topic: str,
        fetch_payload: Callable[[], Awaitable[dict[str, Any]]],
        poll_seconds: float,
        seed_payload: dict[str, Any] | None = None,
    ) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_LIVE_WS_QUEUE_MAX_SIZE)
        async with self._lock:
            state = self._topics.get(topic)
            if state is None:
                state = _LiveStreamTopicState(
                    topic=topic,
                    fetch_payload=fetch_payload,
                    poll_seconds=poll_seconds,
                )
                self._topics[topic] = state
            else:
                state.fetch_payload = fetch_payload
                state.poll_seconds = poll_seconds

            if seed_payload is not None and state.last_signature is None:
                state.last_payload = seed_payload
                state.last_signature = _stable_payload_signature(seed_payload)

            state.subscribers.add(queue)
            if state.producer_task is None or state.producer_task.done():
                state.producer_task = asyncio.create_task(
                    self._run_topic(state),
                    name=f"ws-live-topic-{topic}",
                )
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        producer_task: asyncio.Task[None] | None = None
        async with self._lock:
            state = self._topics.get(topic)
            if state is None:
                return
            state.subscribers.discard(queue)
            if state.subscribers:
                return
            producer_task = state.producer_task
            state.producer_task = None
            self._topics.pop(topic, None)
        if producer_task is not None and not producer_task.done():
            producer_task.cancel()
            with suppress(asyncio.CancelledError):
                await producer_task

    async def _broadcast(
        self,
        state: _LiveStreamTopicState,
        payload: dict[str, Any],
    ) -> None:
        subscribers = tuple(state.subscribers)
        for queue in subscribers:
            await _queue_live_ws_message(queue, payload)

    async def _run_topic(self, state: _LiveStreamTopicState) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.1, state.poll_seconds))
                try:
                    payload = await state.fetch_payload()
                except Exception as exc:
                    await self._broadcast(
                        state,
                        {
                            "type": "error",
                            "topic": state.topic,
                            "error": _live_ws_error_detail(exc),
                            "ts": time.time(),
                        },
                    )
                    continue

                signature = _stable_payload_signature(payload)
                is_initial = state.last_signature is None
                if signature == state.last_signature:
                    continue
                state.last_signature = signature
                state.last_payload = payload
                if is_initial:
                    continue
                await self._broadcast(
                    state,
                    {
                        "type": "update",
                        "topic": state.topic,
                        "data": payload,
                        "ts": time.time(),
                    },
                )
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        states: list[_LiveStreamTopicState]
        async with self._lock:
            states = list(self._topics.values())
            self._topics.clear()
        for state in states:
            task = state.producer_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


_LIVE_STREAM_BROKER = _LiveStreamBroker()

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


@lru_cache(maxsize=8192)
def _parse_iso_datetime_cached(text: str) -> datetime | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        fallback = pd.to_datetime(normalized, utc=True, errors="coerce")
        if pd.isna(fallback):
            return None
        return fallback.to_pydatetime()


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _parse_iso_datetime_cached(text)


def _task_category_from_kind_source(kind: Any, source: Any) -> str:
    kind_key = str(kind or "").strip().lower()
    source_key = str(source or "").strip().lower()
    if kind_key == "backtest":
        return "backtest"
    if kind_key in {"optimization", "agent_conversion"}:
        return "optimization"
    if source_key == "resample":
        return "resample"
    return "download"


def _normalize_task_category(value: Any, kind: Any, source: Any) -> str:
    category = str(value or "").strip().lower()
    if category in {"backtest", "download", "optimization", "resample"}:
        return category
    return _task_category_from_kind_source(kind, source)


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
    task["category"] = _normalize_task_category(task.get("category"), task.get("kind"), task.get("source"))
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
        "max_drawdown",
        "win_rate",
    }:
        return normalized
    return "created_at"


def _backtest_sort_key(item: dict[str, Any], sort_field: str) -> Any:
    if sort_field in {"trade_count"}:
        return int(item.get(sort_field, 0) or 0)
    if sort_field == "win_rate":
        return _normalize_win_rate(item.get("win_rate")) or 0.0
    if sort_field in {"total_return", "sharpe_ratio", "cagr", "psr", "p_value", "max_drawdown"}:
        return float(item.get(sort_field, 0.0) or 0.0)
    return str(item.get(sort_field, "") or "")


def _normalize_portfolio_sort(value: str) -> str:
    normalized = str(value or "created_at").strip().lower()
    aliases = {
        "sharpe": "sharpe_ratio",
        "max_drawdown_pct": "max_drawdown",
        "total_trades": "trade_count",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {
        "created_at",
        "total_return",
        "sharpe_ratio",
        "psr",
        "p_value",
        "cagr",
        "max_drawdown",
        "win_rate",
        "trade_count",
    }:
        return normalized
    return "created_at"


def _portfolio_sort_key(item: dict[str, Any], sort_field: str) -> Any:
    if sort_field in {"trade_count"}:
        return int(_safe_float(item.get("trade_count"), 0))
    if sort_field == "created_at":
        return str(item.get("created_at") or "")
    if sort_field in {"total_return", "sharpe_ratio", "psr", "p_value", "cagr", "max_drawdown", "win_rate"}:
        return float(_safe_float(item.get(sort_field), 0.0))
    return str(item.get(sort_field) or "")


def _portfolio_equity_preview(values: Any, *, limit: int = 50) -> list[float]:
    if not isinstance(values, list) or not values:
        return []
    numeric_values = []
    for point in values:
        if isinstance(point, dict):
            numeric_values.append(_safe_float(point.get("equity"), 0.0))
        else:
            numeric_values.append(_safe_float(point, 0.0))
    numeric_values = [float(value) for value in numeric_values if math.isfinite(value)]
    return _downsample_numeric_minmax(numeric_values, limit=limit)


def _downsample_numeric_minmax(values: list[float], *, limit: int) -> list[float]:
    """Downsample a numeric series while preserving extremes.

    Produces up to ~2*bucket_count points (min+max per bucket) plus final point,
    which captures drawdowns/peaks better than uniform stepping.
    """
    if not values:
        return []

    normalized_limit = int(limit) if isinstance(limit, int) else 50
    normalized_limit = max(1, min(normalized_limit, 2000))
    if len(values) <= normalized_limit:
        return [float(v) for v in values]

    # Reserve space for the last point so the series always ends correctly.
    # For min/max sampling we target about half as many buckets as limit.
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

        # Keep a stable order to reduce “zig-zag” artifacts when min/max are equal.
        if min_val == max_val:
            sampled.append(float(min_val))
        else:
            sampled.append(float(min_val))
            sampled.append(float(max_val))

    sampled.append(float(values[-1]))
    # Hard cap in case bucket logic overshoots.
    if len(sampled) > normalized_limit:
        sampled = sampled[: normalized_limit - 1] + [float(values[-1])]
    return sampled


def _portfolio_stats_have_values(stats: dict[str, Any]) -> bool:
    if not isinstance(stats, dict):
        return False
    keys = ("net_profit", "total_return", "sharpe", "cagr", "max_drawdown", "win_rate", "trade_count")
    for key in keys:
        numeric = _safe_float(stats.get(key), 0.0)
        if abs(numeric) > 1e-12:
            return True
    return False


def _portfolio_stats_from_trades(
    trades: list[dict[str, Any]],
    *,
    account_capital: float,
) -> dict[str, Any]:
    if not trades:
        return {}

    ordered_trades = sorted(trades, key=_component_trade_time)
    total_pnl = 0.0
    wins = 0
    equity = account_capital
    peak = account_capital
    max_drawdown = 0.0

    for trade in ordered_trades:
        pnl = _safe_float(trade.get("pnl"), 0.0)
        total_pnl += pnl
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if pnl > 0:
            wins += 1

    trade_count = len(ordered_trades)
    win_rate = (wins / trade_count) if trade_count else 0.0
    total_return = (total_pnl / account_capital) if account_capital else 0.0
    return {
        "trade_count": trade_count,
        "net_profit": round(total_pnl, 6),
        "total_return": round(total_return, 8),
        "win_rate": round(win_rate, 8),
        "max_drawdown": round(max_drawdown, 8),
    }


def _portfolio_summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    account_capital = _safe_float(payload.get("account_capital"), 100000.0) or 100000.0
    stats = legacy_viewer.PortfolioStats.from_dict(payload.get("combined_stats", {})).to_dict()
    trades = payload.get("trades") if isinstance(payload.get("trades"), list) else []
    normalized_trades = [trade for trade in trades if isinstance(trade, dict)]
    if normalized_trades and not _portfolio_stats_have_values(stats):
        stats = {
            **stats,
            **_portfolio_stats_from_trades(
                normalized_trades,
                account_capital=account_capital,
            ),
        }
    cagr = _safe_float(stats.get("cagr"), 0.0)
    max_drawdown = abs(_safe_float(stats.get("max_drawdown"), 0.0))
    net_profit = _safe_float(stats.get("net_profit"), 0.0)
    total_return = _safe_float(stats.get("total_return"), 0.0)
    win_rate = _normalize_win_rate(stats.get("win_rate")) or 0.0
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    return {
        "id": str(payload.get("id") or ""),
        "name": str(payload.get("name") or ""),
        "account_capital": account_capital,
        "strategy_count": len(components),
        "total_return": total_return,
        "sharpe_ratio": round(_safe_float(stats.get("sharpe"), 0.0), 6),
        "sharpe": round(_safe_float(stats.get("sharpe"), 0.0), 3),
        "cagr": round(cagr * 100.0, 2),
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "net_profit": round(net_profit, 2),
        "net_profit_pct": round(net_profit / account_capital * 100, 2) if account_capital else 0.0,
        "psr": stats.get("psr"),
        "p_value": round(_safe_float(stats.get("p_value"), 1.0), 6),
        "win_rate": round(win_rate * 100.0, 2),
        "trade_count": int(_safe_float(stats.get("trade_count"), len(normalized_trades))),
        "equity_mini": _portfolio_equity_preview(payload.get("combined_equity", []), limit=50),
        "created_at": str(payload.get("created_at", "")),
    }


def _refresh_portfolio_meta_cache() -> int:
    payloads = _snapshot_portfolio_payloads()
    next_cache: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        portfolio_id = str(payload.get("id") or "").strip()
        if not portfolio_id:
            continue
        next_cache[portfolio_id] = _portfolio_summary_from_payload(payload)
    global _PORTFOLIO_META_CACHE_WARMED
    with _PORTFOLIO_META_CACHE_LOCK:
        _PORTFOLIO_META_CACHE.clear()
        _PORTFOLIO_META_CACHE.update(next_cache)
        _PORTFOLIO_META_CACHE_WARMED = True
    return len(next_cache)


def _ensure_portfolio_meta_cache() -> None:
    with _PORTFOLIO_META_CACHE_LOCK:
        if _PORTFOLIO_META_CACHE_WARMED:
            return
    _refresh_portfolio_meta_cache()


def _snapshot_portfolio_meta_cache() -> list[dict[str, Any]]:
    _ensure_portfolio_meta_cache()
    with _PORTFOLIO_META_CACHE_LOCK:
        return [dict(item) for item in _PORTFOLIO_META_CACHE.values()]


def _upsert_portfolio_meta_cache_from_payload(payload: dict[str, Any]) -> None:
    portfolio_id = str(payload.get("id") or "").strip()
    if not portfolio_id:
        return
    summary = _portfolio_summary_from_payload(payload)
    global _PORTFOLIO_META_CACHE_WARMED
    with _PORTFOLIO_META_CACHE_LOCK:
        _PORTFOLIO_META_CACHE[portfolio_id] = summary
        _PORTFOLIO_META_CACHE_WARMED = True


def _upsert_portfolio_meta_cache(portfolio_id: str) -> None:
    normalized_id = str(portfolio_id or "").strip()
    if not normalized_id:
        return
    payload = _load_portfolio_payload(normalized_id)
    _upsert_portfolio_meta_cache_from_payload(payload)


def _remove_portfolio_meta_cache(portfolio_id: str) -> None:
    normalized_id = str(portfolio_id or "").strip()
    if not normalized_id:
        return
    with _PORTFOLIO_META_CACHE_LOCK:
        _PORTFOLIO_META_CACHE.pop(normalized_id, None)


def _sanitize_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    raw_components = sanitized.get("components") if isinstance(sanitized.get("components"), list) else []
    components: list[dict[str, Any]] = []
    for raw_component in raw_components:
        if hasattr(raw_component, "model_dump"):
            component = raw_component.model_dump()
        elif isinstance(raw_component, dict):
            component = dict(raw_component)
        else:
            continue
        components.append(component)
    sanitized["components"] = components
    if not isinstance(sanitized.get("combined_stats"), dict):
        sanitized["combined_stats"] = {}
    if not isinstance(sanitized.get("combined_equity"), list):
        sanitized["combined_equity"] = []
    if not isinstance(sanitized.get("trades"), list):
        sanitized["trades"] = []
    return sanitized


def _snapshot_portfolio_payloads() -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)

    for file_path in sorted(PORTFOLIOS_DIR.glob("*.json")):
        try:
            data = _normalize_portfolio_payload(json.loads(file_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("Could not read portfolio %s: %s", file_path.name, exc)
            continue
        portfolio_id = str(data.get("id") or file_path.stem).strip()
        if not portfolio_id:
            continue
        data["id"] = portfolio_id
        by_id[portfolio_id] = data

    try:
        records = list_portfolio_records()
    except Exception as exc:
        logger.warning("Could not read portfolio records from DB: %s", exc)
        records = []
    for record in records:
        if hasattr(record, "model_dump"):
            source = record.model_dump()
        elif isinstance(record, dict):
            source = dict(record)
        else:
            source = {}
        data = _normalize_portfolio_payload(source)
        portfolio_id = str(data.get("id") or "").strip()
        if not portfolio_id or portfolio_id in by_id:
            continue
        by_id[portfolio_id] = data

    payloads = list(by_id.values())
    payloads.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    return payloads


def _filter_portfolios_by_name(items: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    needle = str(name or "").strip().lower()
    if not needle:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        values = [
            str(item.get("name") or ""),
            str(item.get("id") or ""),
        ]
        if any(needle in value.lower() for value in values if value):
            filtered.append(item)
    return filtered


def _portfolio_fingerprint(payload: dict[str, Any]) -> str:
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    component_fingerprints: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        strategy_name = _normalize_strategy_key(component.get("strategy_name"))
        symbol = _normalize_symbol_key(component.get("symbol"))
        timeframe = _normalize_timeframe_key(component.get("timeframe"))
        weight = round(_safe_float(component.get("weight"), 0.0), 8)
        backtest_id = str(component.get("backtest_id") or "").strip()
        component_fingerprints.append(
            f"{strategy_name}|{symbol}|{timeframe}|{weight:.8f}|{backtest_id}"
        )
    component_fingerprints.sort()
    account_capital = round(_safe_float(payload.get("account_capital"), 0.0), 2)
    return f"{account_capital:.2f}|{';'.join(component_fingerprints)}"


def _group_duplicate_portfolios() -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for payload in _snapshot_portfolio_payloads():
        fingerprint = _portfolio_fingerprint(payload)
        if not fingerprint:
            continue
        grouped.setdefault(fingerprint, []).append(payload)

    duplicates: list[dict[str, Any]] = []
    total_items = 0
    for fingerprint, entries in grouped.items():
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
                "name": str(first.get("name") or ""),
                "account_capital": _safe_float(first.get("account_capital"), 0.0),
                "component_count": len(first.get("components", []))
                if isinstance(first.get("components"), list)
                else 0,
                "portfolios": [
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "created_at": str(item.get("created_at") or ""),
                    }
                    for item in sorted_entries
                ],
            }
        )

    duplicates.sort(
        key=lambda item: (
            item.get("portfolios", [{}])[0].get("created_at", ""),
            item.get("portfolios", [{}])[0].get("id", ""),
        ),
        reverse=True,
    )
    return duplicates, total_items


def _delete_portfolio_with_artifacts(portfolio_id: str) -> bool:
    normalized_id = str(portfolio_id or "").strip()
    if not normalized_id:
        return False

    deleted_db = False
    try:
        deleted_db = delete_portfolio_record(normalized_id)
    except Exception as exc:
        logger.warning("Could not delete portfolio %s from DB: %s", normalized_id, exc)

    path = PORTFOLIOS_DIR / f"{normalized_id}.json"
    deleted_file = False
    if path.exists():
        try:
            path.unlink()
            deleted_file = True
        except OSError as exc:
            logger.warning("Could not delete portfolio %s: %s", normalized_id, exc)

    deleted = deleted_db or deleted_file
    if deleted:
        _remove_portfolio_meta_cache(normalized_id)
    return deleted


def _current_backtest_store_mtime_ns() -> int:
    try:
        return int(BACKTEST_DB_PATH.stat().st_mtime_ns)
    except OSError:
        return 0


def _refresh_backtest_meta_cache() -> int:
    source_mtime_ns = _current_backtest_store_mtime_ns()
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
    global _BACKTEST_META_CACHE_SOURCE_MTIME_NS
    with _BACKTEST_META_CACHE_LOCK:
        _BACKTEST_META_CACHE.clear()
        _BACKTEST_META_CACHE.update(next_cache)
        _BACKTEST_META_CACHE_SOURCE_MTIME_NS = source_mtime_ns
    with _BACKTEST_EQUITY_PREVIEW_CACHE_LOCK:
        _BACKTEST_EQUITY_PREVIEW_CACHE.clear()
        _BACKTEST_EQUITY_PREVIEW_CACHE.update(next_preview_cache)
    return len(next_cache)


def _ensure_backtest_meta_cache() -> None:
    source_mtime_ns = _current_backtest_store_mtime_ns()
    with _BACKTEST_META_CACHE_LOCK:
        if (
            _BACKTEST_META_CACHE
            and _BACKTEST_META_CACHE_SOURCE_MTIME_NS >= source_mtime_ns
        ):
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
    display_name = str(
        meta.get("display_name")
        or meta.get("notes")
        or meta.get("strategy_name")
        or "unknown"
    ).strip() or "unknown"
    symbols = [str(item) for item in meta.get("symbols", []) if str(item).strip()]
    timeframe = normalize_timeframe(str(meta.get("timeframe") or ""))
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
        "title": display_name,
        "notes": str(meta.get("notes") or ""),
        "strategy": display_name,
        "strategy_slug": strategy_name,
        "strategy_display_name": display_name,
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
    tags = [
        str(tag).strip()
        for tag in (meta.get("tags") if isinstance(meta.get("tags"), list) else [])
        if str(tag).strip()
    ]
    display_name = str(
        meta.get("display_name")
        or meta.get("notes")
        or meta.get("strategy_name")
        or "unknown"
    ).strip() or "unknown"
    return {
        "id": str(meta.get("id") or "").strip(),
        "strategy_name": display_name,
        "strategy_slug": str(meta.get("strategy_name") or "unknown"),
        "symbols": [str(item) for item in meta.get("symbols", []) if str(item).strip()],
        "timeframe": normalize_timeframe(str(meta.get("timeframe") or "")),
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
        "tags": tags,
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
    timeframe = normalize_timeframe(str(item.get("timeframe") or "").strip())
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


def _filter_backtests_by_name(items: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    needle = str(name or "").strip().lower()
    if not needle:
        return items

    filtered: list[dict[str, Any]] = []
    for item in items:
        values = [
            str(item.get("display_name") or ""),
            str(item.get("notes") or ""),
            str(item.get("strategy_name") or ""),
            str(item.get("id") or ""),
        ]
        if any(needle in value.lower() for value in values if value):
            filtered.append(item)
    return filtered


def _apply_backtest_numeric_filters(
    items: list[dict[str, Any]],
    *,
    total_return_min: float | None,
    total_return_max: float | None,
    sharpe_min: float | None,
    sharpe_max: float | None,
    psr_min: float | None,
    psr_max: float | None,
    p_value_min: float | None,
    p_value_max: float | None,
    cagr_min: float | None,
    cagr_max: float | None,
    win_rate_min: float | None,
    win_rate_max: float | None,
    max_drawdown_min: float | None,
    max_drawdown_max: float | None,
) -> list[dict[str, Any]]:
    filtered = items

    def in_range(value: float, lower: float | None, upper: float | None) -> bool:
        if lower is not None and value < lower:
            return False
        if upper is not None and value > upper:
            return False
        return True

    def maybe_fraction(value: float | None) -> float | None:
        if value is None:
            return None
        numeric = _safe_float(value, value)
        if abs(numeric) > 1.0:
            return numeric / 100.0
        return numeric

    def maybe_win_rate(value: float | None) -> float | None:
        if value is None:
            return None
        numeric = _safe_float(value, value)
        if abs(numeric) > 1.0:
            return numeric / 100.0
        return numeric

    total_return_min = maybe_fraction(total_return_min)
    total_return_max = maybe_fraction(total_return_max)
    max_drawdown_min = maybe_fraction(max_drawdown_min)
    max_drawdown_max = maybe_fraction(max_drawdown_max)
    win_rate_min = maybe_win_rate(win_rate_min)
    win_rate_max = maybe_win_rate(win_rate_max)

    if total_return_min is not None or total_return_max is not None:
        filtered = [
            item
            for item in filtered
            if in_range(_safe_float(item.get("total_return"), 0.0), total_return_min, total_return_max)
        ]
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
    if win_rate_min is not None or win_rate_max is not None:
        filtered = [
            item
            for item in filtered
            if in_range(_normalize_win_rate(item.get("win_rate")) or 0.0, win_rate_min, win_rate_max)
        ]
    if max_drawdown_min is not None or max_drawdown_max is not None:
        filtered = [
            item
            for item in filtered
            if in_range(abs(_safe_float(item.get("max_drawdown"), 0.0)), max_drawdown_min, max_drawdown_max)
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
                "timeframe": normalize_timeframe(str(first.get("timeframe") or "")),
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
        "provider": task.get("provider"),
        "model": task.get("model"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "duration_seconds": task.get("duration_seconds"),
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
    task["category"] = _normalize_task_category(
        config_payload.get("category") or task.get("category"),
        task.get("kind"),
        task.get("source"),
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
        "category": _normalize_task_category(
            config_payload.get("category"),
            record.get("type"),
            record.get("source"),
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


def _hydrate_task_cache_once(force: bool = False) -> int:
    global _TASK_CACHE_HYDRATED
    with _TASK_CACHE_HYDRATION_LOCK:
        if _TASK_CACHE_HYDRATED and not force:
            with DATA_TASKS_LOCK:
                return len(DATA_TASKS)
        records = list_background_task_records()
        with DATA_TASKS_LOCK:
            for record in records:
                task_id = str(record.get("id") or "").strip()
                if not task_id or task_id in DATA_TASKS:
                    continue
                DATA_TASKS[task_id] = _build_task_from_record(record)
            size = len(DATA_TASKS)
        _TASK_CACHE_HYDRATED = True
        return size


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
    progress_payload = dict(payload.get("progress") or {})
    progress_percent = _safe_float(progress_payload.get("percent"), _safe_float(task.get("progress"), 0.0))
    progress_message = str(progress_payload.get("current") or "")
    completed_at = str(task.get("finished_at") or "")
    if not completed_at and status in FINAL_TASK_STATUSES:
        completed_at = str(task.get("updated_at") or "")
    error_message = task.get("error")
    if error_message in (None, ""):
        error_message = None
    elif not isinstance(error_message, str):
        error_message = str(error_message)
    payload["can_restart"] = status in {"interrupted", "failed"}
    payload["category"] = _normalize_task_category(
        task.get("category"),
        task.get("kind"),
        task.get("source"),
    )
    payload["started_at"] = str(task.get("started_at") or task.get("created_at") or "")
    payload["finished_at"] = str(task.get("finished_at") or "")
    payload["completed_at"] = completed_at
    payload["elapsed_seconds"] = elapsed_seconds
    payload["eta_seconds"] = eta_seconds
    payload["duration_seconds"] = _safe_float(task.get("duration_seconds"), elapsed_seconds or 0.0) if status in FINAL_TASK_STATUSES else task.get("duration_seconds")
    payload["progress_percent"] = max(0.0, min(100.0, round(progress_percent, 2)))
    payload["progress_message"] = progress_message
    payload["error"] = error_message
    payload["error_message"] = error_message
    payload["provider"] = task.get("provider")
    payload["model"] = task.get("model")
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


def _stable_payload_signature(payload: Any) -> str:
    try:
        return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return repr(payload)


async def _queue_live_ws_message(
    queue: asyncio.Queue[dict[str, Any]],
    message: dict[str, Any],
) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    await queue.put(message)


def _live_ws_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        try:
            return json.dumps(detail, ensure_ascii=False, default=str)
        except Exception:
            return str(detail)
    return str(exc)


async def _consume_live_ws_messages(
    websocket: WebSocket,
    *,
    queue: asyncio.Queue[dict[str, Any]],
    topic: str,
) -> None:
    try:
        while True:
            raw_payload = await websocket.receive_text()
            if not raw_payload:
                continue
            try:
                inbound = json.loads(raw_payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(inbound, dict):
                continue
            msg_type = str(inbound.get("type") or "").strip().lower()
            if msg_type == "ping":
                await _queue_live_ws_message(
                    queue,
                    {"type": "pong", "topic": topic, "ts": time.time()},
                )
    except WebSocketDisconnect:
        await _queue_live_ws_message(queue, {"type": "_disconnect"})
    except Exception:
        await _queue_live_ws_message(queue, {"type": "_disconnect"})


async def _stream_live_payload(
    websocket: WebSocket,
    *,
    topic: str,
    fetch_payload: Callable[[], Awaitable[dict[str, Any]]],
    poll_seconds: float,
) -> None:
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] | None = None
    consumer_task: asyncio.Task[None] | None = None

    try:
        initial_payload = await fetch_payload()
    except Exception as exc:
        error_message = _live_ws_error_detail(exc)
        await websocket.send_json({"type": "error", "topic": topic, "error": error_message})
        with suppress(Exception):
            await websocket.close(code=1008)
        return

    await websocket.send_json(
        {
            "type": "snapshot",
            "topic": topic,
            "data": initial_payload,
            "ts": time.time(),
        }
    )

    queue = await _LIVE_STREAM_BROKER.subscribe(
        topic=topic,
        fetch_payload=fetch_payload,
        poll_seconds=poll_seconds,
        seed_payload=initial_payload,
    )
    consumer_task = asyncio.create_task(
        _consume_live_ws_messages(websocket, queue=queue, topic=topic),
        name=f"ws-live-consumer-{topic}",
    )
    try:
        while True:
            try:
                outbound = await asyncio.wait_for(queue.get(), timeout=_LIVE_WS_HEARTBEAT_SECONDS)
                if outbound.get("type") == "_disconnect":
                    return
            except asyncio.TimeoutError:
                outbound = {"type": "ping", "topic": topic, "ts": time.time()}
            await websocket.send_json(outbound)
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        if consumer_task is not None:
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
        if queue is not None:
            await _LIVE_STREAM_BROKER.unsubscribe(topic, queue)


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
        "signal_source_type": deployment.signal_source_type,
        "signal_source_id": deployment.signal_source_id,
        "symbol_filter": deployment.symbol_filter,
        "signal_source": deployment.signal_source,
        "signal_config": deployment.signal_config,
        "settings": deployment.settings,
    }


def _serialize_service_payload(service: Service) -> dict[str, Any]:
    return {
        "id": str(service.id),
        "name": service.name,
        "type": service.type.value,
        "status": service.status.value,
        "config": service.config or {},
        "last_activity": service.last_activity.isoformat() if service.last_activity else None,
        "created_at": service.created_at.isoformat(),
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


def _live_accounts_stream_payload() -> dict[str, Any]:
    accounts = _cached_live_accounts_payload(force_refresh=True)
    enriched_accounts: list[dict[str, Any]] = []
    for account in accounts:
        item = dict(account)
        account_id = str(item.get("id") or "").strip()
        latest_balance = get_latest_live_balance(account_id) if account_id else None
        if latest_balance is not None:
            timestamp_iso, timestamp_unix = _serialize_live_timestamp(latest_balance.timestamp)
            item["balance"] = float(latest_balance.balance)
            item["equity"] = float(latest_balance.equity)
            item["balance_timestamp"] = timestamp_iso
            item["balance_time"] = timestamp_unix
        else:
            fallback_balance = _safe_float(item.get("balance"), 0.0)
            item["balance"] = fallback_balance
            item["equity"] = _safe_float(item.get("equity"), fallback_balance)
            item["balance_timestamp"] = ""
            item["balance_time"] = 0.0
        enriched_accounts.append(item)
    return {"accounts": enriched_accounts}


def _list_strategies_payload() -> list[dict[str, Any]]:
    entries = _safe_strategy_entries()
    if not entries:
        return []

    stats_by_slug = _strategy_list_stats_from_backtests(entries)
    payload: list[dict[str, Any]] = []
    for entry in entries:
        strategy_stats = stats_by_slug.get(
            entry.slug,
            {"backtest_count": 0, "best_sharpe": None},
        )
        payload.append(
            _strategy_list_payload(
                entry,
                backtest_count=max(int(strategy_stats.get("backtest_count", 0) or 0), 0),
                best_sharpe=(
                    float(strategy_stats["best_sharpe"])
                    if strategy_stats.get("best_sharpe") is not None
                    else None
                ),
                equity_preview=(
                    list(strategy_stats.get("equity_preview", []))
                    if isinstance(strategy_stats.get("equity_preview"), list)
                    else []
                ),
            )
        )

    return payload


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


def _signal_status_from_row(row: dict[str, Any]) -> str:
    if row.get("execution_id"):
        return "Filled"
    metadata = row.get("signal_metadata")
    signal_metadata = metadata if isinstance(metadata, dict) else {}
    source = str(row.get("source") or signal_metadata.get("source") or "internal").lower()
    wait_enabled = bool(
        signal_metadata.get("wait_for_candle_close")
        or signal_metadata.get("wait_candle_close")
    )
    if source.startswith("discord") and wait_enabled:
        return "Waiting"
    return "Pending"


def _parse_request_date(value: str, *, field_name: str, end_of_day: bool = False) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: {value}")
    timestamp = pd.Timestamp(parsed)
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        timestamp = timestamp + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return timestamp.to_pydatetime()


def _normalize_replay_signal_payload(
    payload: SignalReplayInput,
    *,
    default_timeframe: str,
) -> dict[str, Any]:
    signal_time = _parse_request_date(payload.timestamp, field_name="signals.timestamp")
    symbol = str(payload.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="signals.symbol is required")
    action = str(payload.action or "").strip().lower()
    if action not in {"buy", "sell", "long", "short"}:
        raise HTTPException(status_code=400, detail=f"Unsupported signal action '{payload.action}'")
    entry_price = _safe_float(payload.entry_price, 0.0)
    if entry_price <= 0:
        raise HTTPException(status_code=400, detail="signals.entry_price must be > 0")
    metadata = (
        payload.signal_metadata
        if isinstance(payload.signal_metadata, dict)
        else {}
    )
    return {
        "timestamp": signal_time,
        "symbol": symbol,
        "action": "buy" if action in {"buy", "long"} else "sell",
        "entry_price": entry_price,
        "stop_loss": payload.stop_loss,
        "take_profit": payload.take_profit,
        "timeframe": normalize_timeframe(str(payload.timeframe or metadata.get("timeframe") or default_timeframe))
        or default_timeframe,
        "source": str(payload.source or metadata.get("source") or "manual").lower(),
        "metadata": dict(metadata),
    }


def _signal_has_intraday_timestamp(payload: dict[str, Any]) -> bool:
    raw_timestamp = payload.get("timestamp")
    parsed = pd.to_datetime(raw_timestamp, utc=True, errors="coerce")
    if pd.isna(parsed):
        return False
    ts = pd.Timestamp(parsed)
    return bool(ts.hour or ts.minute or ts.second)


def _resolve_signal_replay_timeframe(
    *,
    requested_timeframe: str | None,
    normalized_signals: list[dict[str, Any]],
    fallback: str = "1h",
) -> str:
    requested = normalize_timeframe(str(requested_timeframe or "").strip())
    if requested:
        return requested

    intraday_timeframes = {"1m", "5m", "15m", "30m", "1h", "4h"}
    has_intraday = False
    for signal in normalized_signals:
        signal_timeframe = normalize_timeframe(str(signal.get("timeframe") or "").strip())
        if signal_timeframe in intraday_timeframes:
            has_intraday = True
            break
        if _signal_has_intraday_timestamp(signal):
            has_intraday = True
            break

    if has_intraday:
        return "1m"
    if normalized_signals:
        return "1d"
    return normalize_timeframe(fallback) or fallback


def _signal_replay_task_name(source_label: str) -> str:
    return f"Signal replay ({source_label})"


def _waiting_countdown(signal_metadata: dict[str, Any], signal_time_unix: float) -> int | None:
    if not (
        signal_metadata.get("wait_for_candle_close")
        or signal_metadata.get("wait_candle_close")
    ):
        return None
    timeframe = normalize_timeframe(str(signal_metadata.get("timeframe") or "")) or ""
    matched = re.match(r"^(\d+)([smhdw])$", timeframe)
    if not matched:
        return None
    count = int(matched.group(1))
    unit = matched.group(2)
    unit_seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
    }
    timeframe_seconds = count * unit_seconds.get(unit, 0)
    if timeframe_seconds <= 0:
        return None
    now = time.time()
    signal_reference = signal_time_unix if signal_time_unix > 0 else now
    close_unix = ((int(signal_reference) // timeframe_seconds) + 1) * timeframe_seconds
    remaining = int(max(0, math.ceil(close_unix - now)))
    return remaining


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


def _normalize_strategy_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def _normalize_symbol_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_timeframe_key(value: Any) -> str:
    return normalize_timeframe(str(value or "").strip() or "1h")


def _resolve_component_backtest_id(
    *,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    explicit_backtest_id: str = "",
) -> str:
    explicit_id = str(explicit_backtest_id or "").strip()
    if explicit_id:
        return explicit_id

    strategy_entry = _safe_strategy_entry(strategy_name)
    normalized_strategy = _normalize_strategy_key(strategy_name)
    normalized_symbol = _normalize_symbol_key(symbol)
    normalized_timeframe = _normalize_timeframe_key(timeframe)

    best_candidate: tuple[str, str] | None = None
    for item in _snapshot_backtest_meta_cache():
        backtest_id = str(item.get("id") or "").strip()
        if not backtest_id:
            continue
        item_strategy = _normalize_strategy_key(item.get("strategy_name"))
        strategy_matches = False
        if strategy_entry is not None:
            strategy_matches = matches_strategy_identifier(strategy_entry, str(item.get("strategy_name") or ""))
        if not strategy_matches:
            strategy_matches = item_strategy == normalized_strategy
        if not strategy_matches:
            continue

        item_symbols = [str(raw).strip().upper() for raw in (item.get("symbols") or []) if str(raw).strip()]
        if normalized_symbol and item_symbols and normalized_symbol not in item_symbols:
            continue
        item_timeframe = _normalize_timeframe_key(item.get("timeframe"))
        if normalized_timeframe and item_timeframe and normalized_timeframe != item_timeframe:
            continue

        created_at = str(item.get("created_at") or "")
        if best_candidate is None or created_at > best_candidate[0]:
            best_candidate = (created_at, backtest_id)

    if best_candidate is not None:
        return best_candidate[1]

    if not BACKTESTS_DIR.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"No backtest found for component '{strategy_name}' "
                f"({normalized_symbol or 'N/A'} {normalized_timeframe})"
            ),
        )

    for file_path in BACKTESTS_DIR.glob("*.trades.json"):
        try:
            payload_data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta = payload_data.get("meta", {})
        raw_strategies = meta.get("strategies", [])
        if not isinstance(raw_strategies, list) or not raw_strategies:
            continue

        strategy_matches = False
        if strategy_entry is not None:
            strategy_matches = any(
                matches_strategy_identifier(strategy_entry, strategy_value)
                for strategy_value in raw_strategies
            )
        if not strategy_matches:
            strategy_matches = any(
                _normalize_strategy_key(strategy_value) == normalized_strategy
                for strategy_value in raw_strategies
            )
        if not strategy_matches:
            continue

        data_payload = meta.get("data", {}) if isinstance(meta.get("data"), dict) else {}
        symbols_payload = [
            _normalize_symbol_key(raw_symbol)
            for raw_symbol in (data_payload.get("symbols") or [])
            if _normalize_symbol_key(raw_symbol)
        ]
        if normalized_symbol and symbols_payload and normalized_symbol not in symbols_payload:
            continue

        timeframes_payload = data_payload.get("timeframes") or []
        timeframe_match = True
        if isinstance(timeframes_payload, list) and timeframes_payload:
            timeframe_match = normalized_timeframe in {
                _normalize_timeframe_key(raw_timeframe) for raw_timeframe in timeframes_payload
            }
        if not timeframe_match:
            continue

        return file_path.stem.replace(".trades", "")

    raise HTTPException(
        status_code=404,
        detail=(
            f"No backtest found for component '{strategy_name}' "
            f"({normalized_symbol or 'N/A'} {normalized_timeframe})"
        ),
    )


def _resolve_strategy_backtest_id(
    strategy_name: str,
    *,
    explicit_backtest_id: str = "",
) -> str:
    explicit_id = str(explicit_backtest_id or "").strip()
    if explicit_id:
        return explicit_id

    strategy_entry = _safe_strategy_entry(strategy_name)
    normalized_strategy = _normalize_strategy_key(strategy_name)

    best_candidate: tuple[str, str] | None = None
    for item in _snapshot_backtest_meta_cache():
        backtest_id = str(item.get("id") or "").strip()
        if not backtest_id:
            continue
        strategy_matches = False
        if strategy_entry is not None:
            strategy_matches = matches_strategy_identifier(strategy_entry, str(item.get("strategy_name") or ""))
        if not strategy_matches:
            strategy_matches = _normalize_strategy_key(item.get("strategy_name")) == normalized_strategy
        if not strategy_matches:
            continue
        created_at = str(item.get("created_at") or "")
        if best_candidate is None or created_at > best_candidate[0]:
            best_candidate = (created_at, backtest_id)

    if best_candidate is not None:
        return best_candidate[1]

    if not BACKTESTS_DIR.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No backtest found for strategy '{strategy_name}'",
        )

    for file_path in BACKTESTS_DIR.glob("*.trades.json"):
        try:
            payload_data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        meta = payload_data.get("meta", {})
        raw_strategies = meta.get("strategies", [])
        if not isinstance(raw_strategies, list) or not raw_strategies:
            continue

        strategy_matches = False
        if strategy_entry is not None:
            strategy_matches = any(
                matches_strategy_identifier(strategy_entry, strategy_value)
                for strategy_value in raw_strategies
            )
        if not strategy_matches:
            strategy_matches = any(
                _normalize_strategy_key(strategy_value) == normalized_strategy
                for strategy_value in raw_strategies
            )
        if not strategy_matches:
            continue

        return file_path.stem.replace(".trades", "")

    raise HTTPException(
        status_code=404,
        detail=f"No backtest found for strategy '{strategy_name}'",
    )


def _component_trade_time(trade: dict[str, Any]) -> str:
    for key in ("exit_time_iso", "entry_time_iso"):
        value = str(trade.get(key) or "").strip()
        if value:
            return value
    raw_exit = trade.get("exit_time")
    raw_entry = trade.get("entry_time")
    for value in (raw_exit, raw_entry):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                continue
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return pd.Timestamp(parsed).isoformat()
    return ""


def _trade_numeric_value(trade: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = trade.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _normalize_portfolio_trade_side_direction(trade: dict[str, Any]) -> tuple[str, str]:
    raw = str(
        trade.get("side")
        or trade.get("direction")
        or trade.get("action")
        or ""
    ).strip().upper()
    if not raw:
        return "BUY", "LONG"
    if raw in {"BUY", "LONG", "B", "+1"} or "BUY" in raw:
        return "BUY", "LONG"
    if raw in {"SELL", "SHORT", "S", "-1"} or "SELL" in raw:
        return "SELL", "SHORT"
    if "LONG" in raw:
        return "BUY", "LONG"
    if "SHORT" in raw:
        return "SELL", "SHORT"
    return "BUY", "LONG"


def _normalize_portfolio_trade_fields(raw_trade: dict[str, Any]) -> dict[str, Any]:
    trade = dict(raw_trade)

    side, direction = _normalize_portfolio_trade_side_direction(trade)
    trade["side"] = side
    trade["direction"] = direction

    entry_price = _trade_numeric_value(trade, "entry_price", "entry", "open_price", "price")
    if entry_price is not None:
        trade["entry_price"] = entry_price

    exits = trade.get("exits")
    if not isinstance(exits, list):
        exits = []

    exit_price = _trade_numeric_value(trade, "exit_price", "close_price")
    if exit_price is None:
        for exit_row in reversed(exits):
            if not isinstance(exit_row, dict):
                continue
            exit_price = _trade_numeric_value(exit_row, "price", "exit_price")
            if exit_price is not None:
                break
    if exit_price is not None:
        trade["exit_price"] = exit_price

    quantity = _trade_numeric_value(
        trade,
        "quantity",
        "qty",
        "filled_qty",
        "filled_quantity",
        "order_qty",
    )
    if quantity is None:
        for exit_row in exits:
            if not isinstance(exit_row, dict):
                continue
            quantity = _trade_numeric_value(exit_row, "quantity", "qty", "filled_qty")
            if quantity is not None:
                break
    if quantity is not None:
        trade["quantity"] = quantity

    position_size = _trade_numeric_value(
        trade,
        "position_size",
        "size",
        "quantity",
        "qty",
        "filled_qty",
        "filled_quantity",
    )
    if position_size is not None:
        trade["position_size"] = position_size

    size = _trade_numeric_value(
        trade,
        "size",
        "position_size",
        "quantity",
        "qty",
        "filled_qty",
        "filled_quantity",
    )
    trade["size"] = _safe_float(size, _safe_float(quantity, 0.0))

    pnl = _trade_numeric_value(trade, "pnl", "realized_pnl", "profit", "net_profit")
    if pnl is not None:
        trade["pnl"] = pnl
    pnl_pct = _trade_numeric_value(trade, "pnl_pct", "profit_pct", "return_pct")
    if pnl_pct is not None:
        trade["pnl_pct"] = pnl_pct

    for iso_key, raw_key in (("entry_time_iso", "entry_time"), ("exit_time_iso", "exit_time")):
        existing = str(trade.get(iso_key) or "").strip()
        if existing:
            continue
        raw_value = trade.get(raw_key)
        if raw_value is None:
            continue
        parsed = pd.to_datetime(raw_value, utc=True, errors="coerce")
        if pd.isna(parsed):
            continue
        trade[iso_key] = pd.Timestamp(parsed).isoformat()

    trade["time"] = _component_trade_time(trade)
    trade["date"] = str(trade.get("time") or "").split("T", 1)[0]
    trade["duration"] = _trade_duration_text(trade)
    return trade


def _normalized_iso_timestamp(value: Any) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).isoformat()


def _portfolio_backtest_trade_lookup(backtest_id: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    normalized_backtest_id = str(backtest_id or "").strip()
    if not normalized_backtest_id:
        return {}

    result = _load_result(normalized_backtest_id)
    if result is None:
        return {}

    lookup: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for trade in result.trades:
        payload = legacy_viewer._trade_to_viewer(trade)
        symbol = _normalize_symbol_key(payload.get("symbol") or trade.symbol)
        direction = str(payload.get("direction") or trade.direction or "").strip().upper() or "LONG"
        trade_time_iso = (
            str(payload.get("exit_time_iso") or "").strip()
            or str(payload.get("entry_time_iso") or "").strip()
        )
        normalized_time = _normalized_iso_timestamp(trade_time_iso)
        if not symbol or not normalized_time:
            continue
        key = (symbol, direction, normalized_time)
        lookup.setdefault(key, []).append(payload)
    return lookup


def _enrich_portfolio_trade_from_backtest(
    trade: dict[str, Any],
    *,
    backtest_id: str,
    lookup_cache: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]],
) -> dict[str, Any]:
    normalized_backtest_id = str(backtest_id or "").strip()
    if not normalized_backtest_id:
        return trade

    lookup = lookup_cache.get(normalized_backtest_id)
    if lookup is None:
        lookup = _portfolio_backtest_trade_lookup(normalized_backtest_id)
        lookup_cache[normalized_backtest_id] = lookup
    if not lookup:
        return trade

    symbol = _normalize_symbol_key(trade.get("symbol"))
    direction = str(trade.get("direction") or "").strip().upper() or "LONG"
    trade_time = _normalized_iso_timestamp(
        trade.get("time") or trade.get("exit_time_iso") or trade.get("entry_time_iso")
    )
    if not symbol or not trade_time:
        return trade

    candidate_keys = [
        (symbol, direction, trade_time),
        (symbol, "LONG", trade_time),
        (symbol, "SHORT", trade_time),
    ]

    candidates: list[dict[str, Any]] = []
    selected_key: tuple[str, str, str] | None = None
    for key in candidate_keys:
        entries = lookup.get(key)
        if entries:
            candidates = entries
            selected_key = key
            break
    if not candidates or selected_key is None:
        return trade

    selected_index = 0
    pnl_value = _trade_numeric_value(trade, "pnl")
    if pnl_value is not None and len(candidates) > 1:
        selected_index = min(
            range(len(candidates)),
            key=lambda idx: abs(_safe_float(candidates[idx].get("pnl"), 0.0) - pnl_value),
        )
    candidate = candidates.pop(selected_index)
    if not candidates:
        lookup.pop(selected_key, None)

    if _trade_numeric_value(trade, "entry_price", "entry") is None:
        candidate_entry = _trade_numeric_value(candidate, "entry_price")
        if candidate_entry is not None:
            trade["entry_price"] = candidate_entry
    if _trade_numeric_value(trade, "exit_price", "close_price") is None:
        candidate_exit = _trade_numeric_value(candidate, "exit_price")
        if candidate_exit is not None:
            trade["exit_price"] = candidate_exit
    if _trade_numeric_value(trade, "quantity", "qty", "filled_qty") is None:
        candidate_qty = _trade_numeric_value(candidate, "quantity")
        if candidate_qty is not None:
            trade["quantity"] = candidate_qty
    if _trade_numeric_value(trade, "position_size", "size") is None:
        candidate_size = _trade_numeric_value(candidate, "position_size", "quantity")
        if candidate_size is not None:
            trade["position_size"] = candidate_size
    if _trade_numeric_value(trade, "pnl_pct", "profit_pct", "return_pct") is None:
        candidate_pnl_pct = _trade_numeric_value(candidate, "pnl_pct")
        if candidate_pnl_pct is not None:
            trade["pnl_pct"] = candidate_pnl_pct

    if not str(trade.get("entry_time_iso") or "").strip():
        trade["entry_time_iso"] = str(candidate.get("entry_time_iso") or "")
    if not str(trade.get("exit_time_iso") or "").strip():
        trade["exit_time_iso"] = str(candidate.get("exit_time_iso") or "")
    if not isinstance(trade.get("exits"), list):
        exits = candidate.get("exits")
        if isinstance(exits, list):
            trade["exits"] = [dict(item) for item in exits if isinstance(item, dict)]
    if not str(trade.get("exit_reason") or "").strip():
        trade["exit_reason"] = str(candidate.get("exit_reason") or "")
    if not str(trade.get("side") or "").strip():
        trade["side"] = "SELL" if direction == "SHORT" else "BUY"

    return trade


def _component_group_key(item: dict[str, Any], group_by: str) -> str:
    if group_by == "strategy":
        return str(item.get("strategy_name") or "unknown")
    if group_by == "symbol":
        return str(item.get("symbol") or "unknown").upper()
    if group_by == "timeframe":
        return _normalize_timeframe_key(item.get("timeframe"))
    return str(item.get("component_id") or "")


def _resolve_component_payloads(payload: CreatePortfolioRequest) -> list[dict[str, Any]]:
    if payload.components:
        resolved: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.components, start=1):
            strategy_name = str(raw.strategy_name or "").strip()
            symbol = _normalize_symbol_key(raw.symbol)
            timeframe = _normalize_timeframe_key(raw.timeframe)
            if not strategy_name:
                raise HTTPException(status_code=400, detail=f"Component #{index} is missing strategy_name")
            if not symbol:
                raise HTTPException(status_code=400, detail=f"Component #{index} is missing symbol")
            weight = _safe_float(raw.weight, 1.0)
            if not math.isfinite(weight) or weight <= 0:
                raise HTTPException(status_code=400, detail=f"Component #{index} has invalid weight")
            risk_config = dict(raw.risk_config or {})
            settings = dict(raw.settings or {})
            backtest_id = _resolve_component_backtest_id(
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
                explicit_backtest_id=str(raw.backtest_id or "").strip(),
            )
            resolved.append(
                {
                    "component_id": f"component-{index:03d}",
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "weight": weight,
                    "risk_config": risk_config,
                    "settings": settings,
                    "backtest_id": backtest_id,
                    "legacy_risk_per_trade_pct": weight,
                    "legacy_max_positions": int(_safe_float(risk_config.get("max_positions"), 99)),
                }
            )
        return resolved

    strategy_configs = [item.model_dump() for item in payload.strategies]
    if not strategy_configs:
        return []

    resolved_legacy: list[dict[str, Any]] = []
    for index, strategy_config in enumerate(strategy_configs, start=1):
        strategy_name = str(strategy_config.get("name") or "").strip()
        if not strategy_name:
            raise HTTPException(status_code=400, detail=f"Strategy row #{index} is missing name")
        weight = _safe_float(strategy_config.get("risk_per_trade_pct"), 1.0)
        if not math.isfinite(weight) or weight <= 0:
            raise HTTPException(status_code=400, detail=f"Strategy row #{index} has invalid risk_per_trade_pct")
        backtest_id = _resolve_strategy_backtest_id(
            strategy_name,
            explicit_backtest_id=str(strategy_config.get("backtest_id") or "").strip(),
        )
        result = _load_result(backtest_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")
        result_symbol = (
            _normalize_symbol_key((result.meta.data or {}).get("symbols", [None])[0])
            if isinstance(result.meta.data, dict)
            else ""
        )
        result_timeframe = (
            _normalize_timeframe_key((result.meta.data or {}).get("timeframes", ["1h"])[0])
            if isinstance(result.meta.data, dict)
            else "1h"
        )
        resolved_legacy.append(
            {
                "component_id": f"component-{index:03d}",
                "strategy_name": strategy_name,
                "symbol": result_symbol,
                "timeframe": result_timeframe,
                "weight": weight,
                "risk_config": {"max_positions": int(strategy_config.get("max_positions", 99))},
                "settings": {},
                "backtest_id": backtest_id,
                "legacy_risk_per_trade_pct": weight,
                "legacy_max_positions": int(strategy_config.get("max_positions", 99)),
            }
        )
    return resolved_legacy


def _compute_portfolio_contributions(
    *,
    portfolio: dict[str, Any],
    group_by: str = "component",
) -> list[dict[str, Any]]:
    components = portfolio.get("components", [])
    if not isinstance(components, list):
        components = []
    trades = portfolio.get("trades", [])
    if not isinstance(trades, list):
        trades = []

    normalized_group = str(group_by or "component").strip().lower()
    if normalized_group not in {"component", "strategy", "symbol", "timeframe"}:
        normalized_group = "component"

    grouped: dict[str, dict[str, Any]] = {}
    total_return = _safe_float((portfolio.get("combined_stats") or {}).get("total_return"), 0.0)
    total_drawdown = abs(_safe_float((portfolio.get("combined_stats") or {}).get("max_drawdown"), 0.0))

    component_metrics: dict[str, dict[str, float]] = {}
    for component in components:
        component_id = str(component.get("component_id") or "").strip()
        if not component_id:
            continue
        raw_win_rate = _normalize_win_rate(component.get("win_rate"))
        metrics = {
            "sharpe_ratio": 0.0,
            "psr": 0.0,
            "p_value": 1.0,
            "max_drawdown": 0.0,
            "win_rate": raw_win_rate if raw_win_rate is not None else 0.0,
            "cagr": 0.0,
            "total_return": 0.0,
        }
        backtest_id = str(component.get("backtest_id") or "").strip()
        if backtest_id:
            result = _load_result(backtest_id)
            if result is not None:
                try:
                    raw = legacy_viewer._canonical_stats_from_result(result)
                    max_drawdown = abs(
                        _safe_float(raw.get("max_drawdown_pct"), _safe_float(raw.get("max_drawdown"), 0.0))
                    )
                    metrics = {
                        "sharpe_ratio": _safe_float(raw.get("sharpe_ratio"), 0.0),
                        "psr": _safe_float(raw.get("psr"), 0.0),
                        "p_value": _safe_float(raw.get("p_value"), 1.0),
                        "max_drawdown": max_drawdown,
                        "win_rate": _normalize_win_rate(raw.get("win_rate")) or 0.0,
                        "cagr": _safe_float(raw.get("cagr"), 0.0),
                        "total_return": _safe_float(raw.get("total_return"), 0.0),
                    }
                except Exception as exc:
                    logger.warning(
                        "Could not compute contribution metrics for component %s (%s): %s",
                        component_id,
                        backtest_id,
                        exc,
                    )
        component_metrics[component_id] = metrics

    for component in components:
        component_id = str(component.get("component_id") or "").strip()
        if not component_id:
            continue
        strategy_name = str(component.get("strategy_name") or "").strip()
        symbol = _normalize_symbol_key(component.get("symbol"))
        timeframe = _normalize_timeframe_key(component.get("timeframe"))
        key = _component_group_key(component, normalized_group)
        bucket = grouped.setdefault(
            key,
            {
                "group": normalized_group,
                "group_value": key,
                "component_id": component_id,
                "component_ids": [],
                "strategy_name": strategy_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "weight": 0.0,
                "return_contribution": 0.0,
                "return_contribution_pct": 0.0,
                "drawdown_contribution": 0.0,
                "drawdown_contribution_pct": 0.0,
                "trade_count": 0,
                "win_rate": 0.0,
                "wins": 0,
                "pnl_total": 0.0,
                "pnl_contribution": 0.0,
                "pnl_contribution_pct": 0.0,
                "metric_weight": 0.0,
                "sharpe_sum": 0.0,
                "psr_sum": 0.0,
                "p_value_sum": 0.0,
                "max_drawdown_sum": 0.0,
                "win_rate_sum": 0.0,
                "cagr_sum": 0.0,
                "total_return_sum": 0.0,
            },
        )
        if not bucket.get("strategy_name"):
            bucket["strategy_name"] = strategy_name
        if not bucket.get("symbol"):
            bucket["symbol"] = symbol
        if not bucket.get("timeframe"):
            bucket["timeframe"] = timeframe
        bucket["component_ids"].append(component_id)
        weight = _safe_float(component.get("weight"), 0.0)
        bucket["weight"] = _safe_float(bucket.get("weight"), 0.0) + weight

        metrics = component_metrics.get(component_id) or {
            "sharpe_ratio": 0.0,
            "psr": 0.0,
            "p_value": 1.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "cagr": 0.0,
            "total_return": 0.0,
        }
        return_contribution = _safe_float(component.get("return_contribution"), float("nan"))
        derived_return_contribution = _safe_float(metrics.get("total_return"), 0.0) * weight
        if (
            not math.isfinite(return_contribution)
            or (abs(return_contribution) <= 1e-12 and abs(derived_return_contribution) > 1e-12)
        ):
            return_contribution = derived_return_contribution

        drawdown_contribution = _safe_float(component.get("drawdown_contribution"), float("nan"))
        derived_drawdown_contribution = abs(_safe_float(metrics.get("max_drawdown"), 0.0)) * max(weight, 0.0)
        if (
            not math.isfinite(drawdown_contribution)
            or (abs(drawdown_contribution) <= 1e-12 and derived_drawdown_contribution > 1e-12)
        ):
            drawdown_contribution = derived_drawdown_contribution

        bucket["return_contribution"] = _safe_float(bucket.get("return_contribution"), 0.0) + return_contribution
        bucket["drawdown_contribution"] = _safe_float(bucket.get("drawdown_contribution"), 0.0) + abs(
            drawdown_contribution
        )

        metric_weight = max(weight, 0.000001)
        bucket["metric_weight"] = _safe_float(bucket.get("metric_weight"), 0.0) + metric_weight
        bucket["sharpe_sum"] = _safe_float(bucket.get("sharpe_sum"), 0.0) + _safe_float(
            metrics.get("sharpe_ratio"), 0.0
        ) * metric_weight
        bucket["psr_sum"] = _safe_float(bucket.get("psr_sum"), 0.0) + _safe_float(
            metrics.get("psr"), 0.0
        ) * metric_weight
        bucket["p_value_sum"] = _safe_float(bucket.get("p_value_sum"), 0.0) + _safe_float(
            metrics.get("p_value"), 1.0
        ) * metric_weight
        bucket["max_drawdown_sum"] = _safe_float(bucket.get("max_drawdown_sum"), 0.0) + abs(
            _safe_float(metrics.get("max_drawdown"), 0.0)
        ) * metric_weight
        bucket["win_rate_sum"] = _safe_float(bucket.get("win_rate_sum"), 0.0) + (
            _normalize_win_rate(metrics.get("win_rate")) or 0.0
        ) * metric_weight
        bucket["cagr_sum"] = _safe_float(bucket.get("cagr_sum"), 0.0) + _safe_float(metrics.get("cagr"), 0.0) * metric_weight
        bucket["total_return_sum"] = _safe_float(bucket.get("total_return_sum"), 0.0) + _safe_float(
            metrics.get("total_return"), 0.0
        ) * metric_weight

    component_ids_by_group = {
        key: {str(component_id) for component_id in value.get("component_ids", [])}
        for key, value in grouped.items()
    }

    for trade in trades:
        component_id = str(trade.get("component_id") or "").strip()
        if not component_id:
            continue
        for key, component_ids in component_ids_by_group.items():
            if component_id not in component_ids:
                continue
            bucket = grouped[key]
            pnl_value = _safe_float(trade.get("pnl"), 0.0)
            bucket["trade_count"] = int(bucket.get("trade_count") or 0) + 1
            if pnl_value > 0:
                bucket["wins"] = int(bucket.get("wins") or 0) + 1
            bucket["pnl_total"] = _safe_float(bucket.get("pnl_total"), 0.0) + pnl_value

    total_pnl = _safe_float((portfolio.get("combined_stats") or {}).get("net_profit"), 0.0)
    contributions: list[dict[str, Any]] = []
    for key in sorted(grouped.keys()):
        bucket = grouped[key]
        trade_count = int(bucket.get("trade_count") or 0)
        wins = int(bucket.get("wins") or 0)
        return_value = _safe_float(bucket.get("return_contribution"), 0.0)
        drawdown_value = abs(_safe_float(bucket.get("drawdown_contribution"), 0.0))
        return_pct = 0.0 if total_return == 0 else (return_value / total_return) * 100.0
        drawdown_pct = 0.0 if total_drawdown == 0 else (drawdown_value / total_drawdown) * 100.0
        pnl_value = _safe_float(bucket.get("pnl_total"), 0.0)
        pnl_pct = 0.0 if total_pnl == 0 else (pnl_value / total_pnl) * 100.0
        metric_weight = _safe_float(bucket.get("metric_weight"), 0.0) or 1.0
        sharpe_ratio = _safe_float(bucket.get("sharpe_sum"), 0.0) / metric_weight
        psr = _safe_float(bucket.get("psr_sum"), 0.0) / metric_weight
        p_value = _safe_float(bucket.get("p_value_sum"), 1.0) / metric_weight
        max_drawdown = _safe_float(bucket.get("max_drawdown_sum"), 0.0) / metric_weight
        metric_win_rate = _safe_float(bucket.get("win_rate_sum"), 0.0) / metric_weight
        cagr = _safe_float(bucket.get("cagr_sum"), 0.0) / metric_weight
        display_value = bucket.get("group_value", key)
        if normalized_group == "component":
            strategy_name = str(bucket.get("strategy_name") or display_value or "").strip()
            symbol = _normalize_symbol_key(bucket.get("symbol"))
            timeframe = _normalize_timeframe_key(bucket.get("timeframe"))
            suffix_values = [value for value in [symbol, timeframe] if value]
            if strategy_name and suffix_values:
                display_value = f"{strategy_name} ({', '.join(suffix_values)})"
            elif strategy_name:
                display_value = strategy_name
        contributions.append(
            {
                "group": normalized_group,
                "group_value": display_value,
                "component_id": str(bucket.get("component_id") or ""),
                "component_ids": bucket.get("component_ids", []),
                "strategy_name": bucket.get("strategy_name", ""),
                "symbol": bucket.get("symbol", ""),
                "timeframe": bucket.get("timeframe", ""),
                "weight": round(_safe_float(bucket.get("weight"), 0.0), 6),
                "return_contribution": round(return_value, 8),
                "return_contribution_pct": round(return_pct, 3),
                "drawdown_contribution": round(drawdown_value, 8),
                "drawdown_contribution_pct": round(drawdown_pct, 3),
                "trade_count": trade_count,
                "win_rate": round(metric_win_rate * 100.0, 2),
                "wins": wins,
                "sharpe_ratio": round(sharpe_ratio, 6),
                "psr": round(psr, 6),
                "p_value": round(p_value, 6),
                "max_drawdown": round(max_drawdown, 6),
                "cagr": round(cagr, 6),
                "pnl_total": round(pnl_value, 6),
                "pnl_contribution": round(pnl_value, 6),
                "pnl_contribution_pct": round(pnl_pct, 3),
            }
        )
    return contributions


def _portfolio_trades_filtered(
    *,
    portfolio: dict[str, Any],
    strategy: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> list[dict[str, Any]]:
    trades = portfolio.get("trades", [])
    if not isinstance(trades, list):
        return []
    components = portfolio.get("components") if isinstance(portfolio.get("components"), list) else []
    component_by_id = {
        str(component.get("component_id") or "").strip(): component
        for component in components
        if isinstance(component, dict) and str(component.get("component_id") or "").strip()
    }

    strategy_filter = _normalize_strategy_key(strategy)
    symbol_filter = _normalize_symbol_key(symbol)
    timeframe_filter = _normalize_timeframe_key(timeframe) if str(timeframe or "").strip() else ""

    filtered: list[dict[str, Any]] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        normalized_trade = dict(trade)
        component_id = str(normalized_trade.get("component_id") or "").strip()
        component = component_by_id.get(component_id)

        strategy_name = str(normalized_trade.get("strategy_name") or normalized_trade.get("strategy") or "").strip()
        if not strategy_name and component is not None:
            strategy_name = str(component.get("strategy_name") or "").strip()
        normalized_symbol = _normalize_symbol_key(normalized_trade.get("symbol"))
        if not normalized_symbol and component is not None:
            normalized_symbol = _normalize_symbol_key(component.get("symbol"))
        normalized_timeframe = _normalize_timeframe_key(normalized_trade.get("timeframe"))
        if not normalized_timeframe and component is not None:
            normalized_timeframe = _normalize_timeframe_key(component.get("timeframe"))
        if not normalized_timeframe:
            normalized_timeframe = "1h"

        if strategy_filter and _normalize_strategy_key(strategy_name) != strategy_filter:
            continue
        if symbol_filter and normalized_symbol != symbol_filter:
            continue
        if timeframe_filter and normalized_timeframe != timeframe_filter:
            continue

        normalized_trade["component_id"] = component_id
        normalized_trade["strategy_name"] = strategy_name
        normalized_trade["strategy"] = strategy_name
        normalized_trade["symbol"] = normalized_symbol
        normalized_trade["timeframe"] = normalized_timeframe
        normalized_trade = _normalize_portfolio_trade_fields(normalized_trade)
        label_parts = [part for part in [normalized_symbol, normalized_timeframe] if part]
        normalized_trade["strategy_label"] = (
            f"{strategy_name} ({', '.join(label_parts)})" if strategy_name and label_parts else strategy_name
        )
        if not str(normalized_trade.get("id") or "").strip():
            nr = normalized_trade.get("nr")
            if nr is not None and component_id:
                normalized_trade["id"] = f"{component_id}-{nr}"
            elif nr is not None:
                normalized_trade["id"] = str(nr)
            else:
                normalized_trade["id"] = uuid.uuid4().hex[:10]
        filtered.append(normalized_trade)

    filtered.sort(key=_component_trade_time)
    return filtered


def _normalize_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)

    raw_components = normalized.get("components")
    components: list[dict[str, Any]] = []
    if isinstance(raw_components, list) and raw_components:
        source_components = [item for item in raw_components if isinstance(item, dict)]
    else:
        source_components = []
        for strategy_row in normalized.get("strategies", []):
            if not isinstance(strategy_row, dict):
                continue
            source_components.append(
                {
                    "strategy_name": strategy_row.get("strategy_name") or strategy_row.get("name") or "",
                    "symbol": strategy_row.get("symbol") or "",
                    "timeframe": strategy_row.get("timeframe") or "1h",
                    "weight": strategy_row.get("risk_per_trade_pct", 1.0),
                    "risk_config": {"max_positions": strategy_row.get("max_positions", 99)},
                    "settings": {},
                    "backtest_id": strategy_row.get("backtest_id", ""),
                }
            )

    for index, raw_component in enumerate(source_components, start=1):
        strategy_name = str(raw_component.get("strategy_name") or raw_component.get("name") or "").strip()
        symbol = _normalize_symbol_key(raw_component.get("symbol"))
        timeframe = _normalize_timeframe_key(raw_component.get("timeframe"))
        weight = _safe_float(
            raw_component.get("weight"),
            _safe_float(raw_component.get("risk_per_trade_pct"), 1.0),
        )
        if not math.isfinite(weight) or weight <= 0:
            weight = 1.0

        raw_risk_config = raw_component.get("risk_config")
        risk_config = dict(raw_risk_config) if isinstance(raw_risk_config, dict) else {}
        if not risk_config and raw_component.get("max_positions") is not None:
            risk_config["max_positions"] = int(_safe_float(raw_component.get("max_positions"), 99))

        raw_settings = raw_component.get("settings")
        settings = dict(raw_settings) if isinstance(raw_settings, dict) else {}
        component_id = str(
            raw_component.get("component_id")
            or settings.get("component_id")
            or f"component-{index:03d}"
        ).strip()
        backtest_id = str(raw_component.get("backtest_id") or "").strip()
        return_contribution = _safe_float(
            raw_component.get("return_contribution"),
            _safe_float(settings.get("return_contribution"), 0.0),
        )
        return_contribution_pct = _safe_float(
            raw_component.get("return_contribution_pct"),
            _safe_float(settings.get("return_contribution_pct"), 0.0),
        )
        drawdown_contribution = _safe_float(
            raw_component.get("drawdown_contribution"),
            _safe_float(settings.get("drawdown_contribution"), 0.0),
        )
        drawdown_contribution_pct = _safe_float(
            raw_component.get("drawdown_contribution_pct"),
            _safe_float(settings.get("drawdown_contribution_pct"), 0.0),
        )
        trade_count = int(
            _safe_float(
                raw_component.get("trade_count"),
                _safe_float(settings.get("trade_count"), 0),
            )
        )
        win_rate = _safe_float(
            raw_component.get("win_rate"),
            _safe_float(settings.get("win_rate"), 0.0),
        )
        wins = int(
            _safe_float(
                raw_component.get("wins"),
                _safe_float(settings.get("wins"), 0),
            )
        )
        legacy_risk_per_trade_pct = _safe_float(
            raw_component.get("legacy_risk_per_trade_pct"),
            _safe_float(settings.get("legacy_risk_per_trade_pct"), weight),
        )
        legacy_max_positions = int(
            _safe_float(
                raw_component.get("legacy_max_positions"),
                _safe_float(
                    settings.get("legacy_max_positions"),
                    _safe_float(risk_config.get("max_positions"), 99),
                ),
            )
        )

        component_payload = PortfolioComponentModel(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            weight=weight,
            risk_config=risk_config,
            settings=settings,
            backtest_id=backtest_id or None,
        ).model_dump()
        component_payload.update(
            {
                "component_id": component_id,
                "backtest_id": backtest_id,
                "return_contribution": return_contribution,
                "return_contribution_pct": return_contribution_pct,
                "drawdown_contribution": drawdown_contribution,
                "drawdown_contribution_pct": drawdown_contribution_pct,
                "trade_count": trade_count,
                "win_rate": win_rate,
                "wins": wins,
                "legacy_risk_per_trade_pct": legacy_risk_per_trade_pct,
                "legacy_max_positions": legacy_max_positions,
            }
        )
        components.append(component_payload)

    normalized["components"] = components
    normalized["strategies"] = [
        {
            "backtest_id": str(component.get("backtest_id") or ""),
            "strategy_name": str(component.get("strategy_name") or ""),
            "risk_per_trade_pct": _safe_float(component.get("legacy_risk_per_trade_pct"), 1.0),
            "max_positions": int(_safe_float(component.get("legacy_max_positions"), 99)),
        }
        for component in components
    ]

    component_by_id = {
        str(component.get("component_id") or ""): component
        for component in components
        if str(component.get("component_id") or "").strip()
    }
    backtest_trade_lookup_cache: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]] = {}
    normalized_trades: list[dict[str, Any]] = []
    for raw_trade in normalized.get("trades", []):
        if not isinstance(raw_trade, dict):
            continue
        trade = dict(raw_trade)
        component_id = str(trade.get("component_id") or "").strip()
        component = component_by_id.get(component_id)

        strategy_name = str(trade.get("strategy_name") or trade.get("strategy") or "").strip()
        symbol = _normalize_symbol_key(trade.get("symbol"))
        timeframe_text = str(trade.get("timeframe") or "").strip()
        timeframe = _normalize_timeframe_key(timeframe_text) if timeframe_text else ""

        if component is None and strategy_name:
            normalized_strategy = _normalize_strategy_key(strategy_name)
            for candidate in components:
                if _normalize_strategy_key(candidate.get("strategy_name")) != normalized_strategy:
                    continue
                candidate_symbol = _normalize_symbol_key(candidate.get("symbol"))
                candidate_timeframe = _normalize_timeframe_key(candidate.get("timeframe"))
                if symbol and candidate_symbol and symbol != candidate_symbol:
                    continue
                if timeframe and candidate_timeframe and timeframe != candidate_timeframe:
                    continue
                component = candidate
                component_id = str(candidate.get("component_id") or "")
                break

        if component is not None:
            if not strategy_name:
                strategy_name = str(component.get("strategy_name") or "")
            if not symbol:
                symbol = _normalize_symbol_key(component.get("symbol"))
            if not timeframe:
                timeframe = _normalize_timeframe_key(component.get("timeframe"))
            if not str(trade.get("backtest_id") or "").strip():
                trade["backtest_id"] = str(component.get("backtest_id") or "")

        trade["component_id"] = component_id
        trade["strategy_name"] = strategy_name
        trade["strategy"] = strategy_name
        trade["symbol"] = symbol
        trade["timeframe"] = timeframe or "1h"
        trade = _enrich_portfolio_trade_from_backtest(
            trade,
            backtest_id=str(trade.get("backtest_id") or ""),
            lookup_cache=backtest_trade_lookup_cache,
        )
        trade = _normalize_portfolio_trade_fields(trade)
        if "id" not in trade:
            nr_value = trade.get("nr")
            if component_id and nr_value is not None:
                trade["id"] = f"{component_id}-{nr_value}"
            elif nr_value is not None:
                trade["id"] = str(nr_value)
            else:
                trade["id"] = uuid.uuid4().hex[:10]
        normalized_trades.append(trade)

    normalized_trades.sort(key=_component_trade_time)
    normalized["trades"] = normalized_trades

    component_contributions = normalized.get("component_contributions")
    if not isinstance(component_contributions, list):
        normalized["component_contributions"] = _compute_portfolio_contributions(
            portfolio=normalized,
            group_by="component",
        )

    if not isinstance(normalized.get("strategy_contributions"), list):
        grouped = _compute_portfolio_contributions(portfolio=normalized, group_by="strategy")
        stats = normalized.get("combined_stats") if isinstance(normalized.get("combined_stats"), dict) else {}
        total_pnl = _safe_float(stats.get("net_profit"), 0.0)
        strategy_contributions: list[dict[str, Any]] = []
        for item in grouped:
            contribution_pnl = _safe_float(item.get("pnl_total"), 0.0)
            pct = 0.0 if total_pnl == 0 else (contribution_pnl / total_pnl) * 100.0
            strategy_contributions.append(
                {
                    "strategy_name": item.get("group_value", ""),
                    "pnl_contribution": round(contribution_pnl, 2),
                    "pnl_contribution_pct": round(pct, 3),
                }
            )
        normalized["strategy_contributions"] = strategy_contributions

    combined_stats = normalized.get("combined_stats") if isinstance(normalized.get("combined_stats"), dict) else {}
    if normalized_trades and not _portfolio_stats_have_values(combined_stats):
        account_capital = _safe_float(normalized.get("account_capital"), 100000.0) or 100000.0
        derived_stats = _portfolio_stats_from_trades(
            normalized_trades,
            account_capital=account_capital,
        )
        normalized["combined_stats"] = {
            **combined_stats,
            **derived_stats,
        }

    return normalized


def _portfolio_identifier(name: str, *, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return f"{slug or 'portfolio'}-{timestamp}"


def _portfolio_record_to_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "model_dump"):
        source = record.model_dump()
    elif isinstance(record, dict):
        source = dict(record)
    else:
        source = {}
    return _normalize_portfolio_payload(source)


def _portfolio_payload_to_model(payload: dict[str, Any]) -> PortfolioModel:
    normalized = _normalize_portfolio_payload(payload)
    components: list[PortfolioComponentModel] = []
    for component in normalized.get("components", []):
        if not isinstance(component, dict):
            continue
        settings = {
            **dict(component.get("settings") or {}),
            "component_id": str(component.get("component_id") or ""),
            "legacy_risk_per_trade_pct": _safe_float(component.get("legacy_risk_per_trade_pct"), 1.0),
            "legacy_max_positions": int(_safe_float(component.get("legacy_max_positions"), 99)),
            "return_contribution": _safe_float(component.get("return_contribution"), 0.0),
            "return_contribution_pct": _safe_float(component.get("return_contribution_pct"), 0.0),
            "drawdown_contribution": _safe_float(component.get("drawdown_contribution"), 0.0),
            "drawdown_contribution_pct": _safe_float(component.get("drawdown_contribution_pct"), 0.0),
            "trade_count": int(_safe_float(component.get("trade_count"), 0)),
            "win_rate": _safe_float(component.get("win_rate"), 0.0),
            "wins": int(_safe_float(component.get("wins"), 0)),
        }
        components.append(
            PortfolioComponentModel(
                strategy_name=str(component.get("strategy_name") or ""),
                symbol=str(component.get("symbol") or ""),
                timeframe=str(component.get("timeframe") or "1h"),
                weight=_safe_float(component.get("weight"), 1.0),
                risk_config=dict(component.get("risk_config") or {}),
                settings=settings,
                backtest_id=str(component.get("backtest_id") or "") or None,
            )
        )

    return PortfolioModel(
        id=str(normalized.get("id") or ""),
        name=str(normalized.get("name") or ""),
        created_at=str(normalized.get("created_at") or ""),
        account_capital=_safe_float(normalized.get("account_capital"), 100000.0),
        components=components,
        combined_stats=dict(normalized.get("combined_stats") or {}),
        combined_equity=list(normalized.get("combined_equity") or []),
        strategy_contributions=list(normalized.get("strategy_contributions") or []),
        component_contributions=list(normalized.get("component_contributions") or []),
        trades=list(normalized.get("trades") or []),
    )


def _trade_duration_text(trade: dict[str, Any]) -> str:
    duration_text = str(trade.get("duration") or "").strip()
    if duration_text:
        return duration_text

    holding_bars = int(_safe_float(trade.get("holding_bars"), 0))
    if holding_bars > 0:
        suffix = "bar" if holding_bars == 1 else "bars"
        return f"{holding_bars} {suffix}"

    entry_time = pd.to_datetime(trade.get("entry_time_iso"), utc=True, errors="coerce")
    exit_time = pd.to_datetime(trade.get("exit_time_iso"), utc=True, errors="coerce")
    if pd.isna(entry_time) or pd.isna(exit_time):
        return ""
    delta = pd.Timestamp(exit_time) - pd.Timestamp(entry_time)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _strategy_identifier_key(identifier: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in str(identifier or ""))
    return " ".join(normalized.split())


def _strategy_list_payload(
    entry: StrategyEntry,
    *,
    backtest_count: int = 0,
    best_sharpe: float | None = None,
    equity_preview: list[float] | None = None,
) -> dict[str, Any]:
    tags = entry.meta.get("tags", {})
    if not isinstance(tags, dict):
        tags = {}

    parameters = entry.meta.get("parameters", {})
    if not isinstance(parameters, dict) or not parameters:
        parameters = legacy_viewer._extract_ast_parameters(entry.file_path)

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
        "backtest_count": max(int(backtest_count or 0), 0),
        "best_sharpe": best_sharpe,
        "equity_preview": list(equity_preview or []),
    }


def _strategy_list_stats_from_backtests(entries: list[StrategyEntry]) -> dict[str, dict[str, Any]]:
    stats_by_slug: dict[str, dict[str, Any]] = {
        entry.slug: {
            "backtest_count": 0,
            "best_sharpe": None,
            "equity_preview": [],
            "_latest_backtest_id": "",
            "_latest_created_at": "",
        }
        for entry in entries
    }
    if not entries:
        return stats_by_slug

    alias_to_slugs: dict[str, set[str]] = {}
    for entry in entries:
        for alias in strategy_identifier_aliases(entry):
            alias_to_slugs.setdefault(alias, set()).add(entry.slug)

    try:
        metadata_rows = _snapshot_backtest_meta_cache()
    except Exception as exc:
        logger.warning("Could not read backtest metadata cache while listing strategies: %s", exc)
        return stats_by_slug

    for row in metadata_rows:
        strategy_key = _strategy_identifier_key(str(row.get("strategy_name") or ""))
        if not strategy_key:
            continue
        matched_slugs = alias_to_slugs.get(strategy_key, set())
        if not matched_slugs:
            continue

        backtest_id = str(row.get("id") or "").strip()
        created_at = str(row.get("created_at") or "")
        sharpe_value = None
        raw_sharpe = row.get("sharpe_ratio")
        if raw_sharpe is not None:
            try:
                parsed_sharpe = float(raw_sharpe)
                if math.isfinite(parsed_sharpe):
                    sharpe_value = parsed_sharpe
            except (TypeError, ValueError):
                sharpe_value = None

        for slug in matched_slugs:
            bucket = stats_by_slug.setdefault(
                slug,
                {
                    "backtest_count": 0,
                    "best_sharpe": None,
                    "equity_preview": [],
                    "_latest_backtest_id": "",
                    "_latest_created_at": "",
                },
            )
            bucket["backtest_count"] = max(int(bucket.get("backtest_count", 0) or 0) + 1, 0)
            if sharpe_value is not None:
                current_best = bucket.get("best_sharpe")
                if current_best is None or sharpe_value > float(current_best):
                    bucket["best_sharpe"] = sharpe_value
            current_latest = str(bucket.get("_latest_created_at") or "")
            if backtest_id and (not current_latest or created_at >= current_latest):
                bucket["_latest_backtest_id"] = backtest_id
                bucket["_latest_created_at"] = created_at

    preview_backtest_ids = [
        str(bucket.get("_latest_backtest_id") or "").strip()
        for bucket in stats_by_slug.values()
        if str(bucket.get("_latest_backtest_id") or "").strip()
    ]
    if preview_backtest_ids:
        _bulk_warm_backtest_equity_preview(preview_backtest_ids, limit=50)

    for bucket in stats_by_slug.values():
        backtest_id = str(bucket.get("_latest_backtest_id") or "").strip()
        bucket["equity_preview"] = (
            _cached_backtest_equity_preview_only(backtest_id, limit=50) if backtest_id else []
        )
        bucket.pop("_latest_backtest_id", None)
        bucket.pop("_latest_created_at", None)

    return stats_by_slug


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
        return normalize_timeframe(normalized), None

    segmented_match = _SEGMENTED_TIMEFRAME_PATTERN.match(normalized)
    if segmented_match is None:
        return None, None

    timeframe = normalize_timeframe(str(segmented_match.group("timeframe") or ""))
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
    timeframe_value = normalize_timeframe(str(prefixed_match.group("timeframe") or "").strip())
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
        "timeframe": normalize_timeframe(timeframe_value),
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


def _normalize_coverage_date(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _build_ohlcv_availability_index() -> dict[tuple[str, str], dict[str, Any]]:
    entries = _aggregate_symbol_timeframe_entries(_cached_catalog_ohlcv_entries())
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        symbol = str(entry.get("symbol") or "").strip().upper()
        timeframe = normalize_timeframe(str(entry.get("timeframe") or "").strip())
        if not symbol or not timeframe:
            continue
        index[(symbol, timeframe)] = entry
    return index


def _check_entry_range_coverage(
    entry: dict[str, Any] | None,
    *,
    requested_start: str,
    requested_end: str,
) -> tuple[bool, dict[str, str] | None]:
    normalized_start = _normalize_coverage_date(requested_start)
    normalized_end = _normalize_coverage_date(requested_end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        normalized_start, normalized_end = normalized_end, normalized_start

    if entry is None:
        return False, {
            "start": normalized_start or "",
            "end": normalized_end or "",
        }

    available_start = _normalize_coverage_date(str(entry.get("start") or ""))
    available_end = _normalize_coverage_date(str(entry.get("end") or ""))

    if normalized_start is None and normalized_end is None:
        return True, None

    if available_start is None or available_end is None:
        return False, {
            "start": normalized_start or "",
            "end": normalized_end or "",
        }

    required_start = normalized_start or available_start
    required_end = normalized_end or available_end
    if required_start >= available_start and required_end <= available_end:
        return True, None

    if required_start > available_end:
        return False, {"start": required_start, "end": required_end}
    if required_end < available_start:
        return False, {"start": required_start, "end": required_end}

    missing_start = required_start if required_start < available_start else None
    missing_end = required_end if required_end > available_end else None
    if missing_start and missing_end:
        return False, {"start": required_start, "end": required_end}
    if missing_start:
        return False, {"start": missing_start, "end": available_start}
    if missing_end:
        return False, {"start": available_end, "end": missing_end}

    return False, {"start": required_start, "end": required_end}


def _allow_small_backtest_boundary_gap(
    *,
    requested_start: str,
    requested_end: str,
    available_start: str | None,
    available_end: str | None,
    tolerance_days: int = 3,
) -> tuple[bool, str | None]:
    normalized_requested_start = _normalize_coverage_date(requested_start)
    normalized_requested_end = _normalize_coverage_date(requested_end)
    normalized_available_start = _normalize_coverage_date(available_start)
    normalized_available_end = _normalize_coverage_date(available_end)
    if normalized_available_start is None or normalized_available_end is None:
        return False, None

    if (
        normalized_requested_start is not None
        and normalized_requested_start < normalized_available_start
        and (
            normalized_requested_end is None
            or normalized_requested_end <= normalized_available_end
        )
    ):
        leading_gap_days = int(
            (pd.Timestamp(normalized_available_start) - pd.Timestamp(normalized_requested_start)).days
        )
        if 0 < leading_gap_days <= max(int(tolerance_days), 0):
            return True, f"leading gap of {leading_gap_days} day(s)"

    if (
        normalized_requested_end is not None
        and normalized_requested_end > normalized_available_end
        and (
            normalized_requested_start is None
            or normalized_requested_start >= normalized_available_start
        )
    ):
        trailing_gap_days = int(
            (pd.Timestamp(normalized_requested_end) - pd.Timestamp(normalized_available_end)).days
        )
        if 0 < trailing_gap_days <= max(int(tolerance_days), 0):
            return True, f"trailing gap of {trailing_gap_days} day(s)"

    return False, None


def _data_check_result(
    *,
    symbol: str,
    timeframe: str,
    start: str = "",
    end: str = "",
    availability_index: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_symbol or not normalized_timeframe:
        return {"available": False, "missing_range": {"start": "", "end": ""}}

    index = availability_index if availability_index is not None else _build_ohlcv_availability_index()
    entry = index.get((normalized_symbol, normalized_timeframe))
    available, missing_range = _check_entry_range_coverage(
        entry,
        requested_start=start,
        requested_end=end,
    )
    return {"available": available, "missing_range": missing_range}


US_INTRADAY_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h"}
HF_INTRADAY_COVERAGE_END = "2025-07-31"
ALPACA_INTRADAY_START = "2025-07-01"
_US_STOCK_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


def _is_forex_symbol(symbol: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    if "/" in normalized_symbol:
        return True
    if len(normalized_symbol) == 6 and normalized_symbol.isalpha():
        return True

    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        known_forex_symbols = {
            key.upper()
            for key, value in SYMBOL_CONFIG.items()
            if str(value.get("asset_class", "")).strip().lower() == "forex"
        }
    except Exception:
        known_forex_symbols = set()

    return normalized_symbol in known_forex_symbols


def _is_us_intraday_stock(symbol: str, timeframe: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if normalized_timeframe not in US_INTRADAY_TIMEFRAMES:
        return False
    if _is_forex_symbol(normalized_symbol):
        return False
    if not _US_STOCK_SYMBOL_PATTERN.fullmatch(normalized_symbol):
        return False
    if normalized_symbol.endswith("USD") and len(normalized_symbol) > 5:
        return False
    return True


def _auto_download_source(symbol: str, timeframe: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if _is_forex_symbol(normalized_symbol):
        return "dukascopy"
    if _is_us_intraday_stock(normalized_symbol, normalized_timeframe):
        return "huggingface"
    return "yfinance"


def _auto_download_source_plan(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
) -> list[dict[str, str | None]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    default_source = _auto_download_source(normalized_symbol, normalized_timeframe)
    if default_source != "huggingface":
        return [{"source": default_source, "start": start or None, "end": end or None}]

    normalized_start = _normalize_coverage_date(start)
    normalized_end = _normalize_coverage_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        normalized_start, normalized_end = normalized_end, normalized_start

    if normalized_start is None and normalized_end is None:
        return [{"source": "alpaca", "start": None, "end": None}]

    plan: list[dict[str, str | None]] = []

    hf_start = normalized_start or normalized_end
    hf_end = normalized_end
    if hf_start is not None and hf_start > HF_INTRADAY_COVERAGE_END:
        hf_start = None
        hf_end = None
    elif hf_end is None or hf_end > HF_INTRADAY_COVERAGE_END:
        hf_end = HF_INTRADAY_COVERAGE_END
    if hf_start and hf_end and hf_start > hf_end:
        hf_start = None
        hf_end = None
    if hf_end is not None:
        plan.append(
            {
                "source": "huggingface",
                "start": hf_start,
                "end": hf_end,
            }
        )

    alpaca_start = normalized_start or normalized_end
    alpaca_end = normalized_end
    if alpaca_end is not None and alpaca_end < ALPACA_INTRADAY_START:
        alpaca_start = None
    else:
        if alpaca_start is None or alpaca_start < ALPACA_INTRADAY_START:
            alpaca_start = ALPACA_INTRADAY_START
        if alpaca_end is not None and alpaca_start > alpaca_end:
            alpaca_start = None
    if alpaca_start is not None:
        plan.append(
            {
                "source": "alpaca",
                "start": alpaca_start,
                "end": alpaca_end or end or None,
            }
        )

    if not plan:
        return [{"source": "alpaca", "start": start or None, "end": end or None}]
    return plan


def _download_missing_ohlcv_data(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    source: str,
) -> None:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(timeframe) or "1d"
    selected_source = str(source or "").strip().lower() or "dukascopy"

    if selected_source == "huggingface":
        from data_tools.downloaders.huggingface_dl import HuggingFaceDownloader

        downloader = HuggingFaceDownloader()
        downloader.download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=DATA_DIR,
            start=start or None,
            end=end or None,
        )
        return

    if selected_source == "alpaca":
        from data_tools.downloaders.alpaca_dl import AlpacaDownloader

        try:
            downloader = AlpacaDownloader()
            downloader.download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=DATA_DIR,
                start=start or None,
                end=end or None,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "Missing Alpaca credentials" not in message:
                raise
            logger.warning(
                "Alpaca credentials missing for auto-download; falling back to yfinance",
                extra={
                    "data": {
                        "symbol": normalized_symbol,
                        "timeframe": normalized_timeframe,
                        "start": start,
                        "end": end,
                    }
                },
            )
            from data_tools.downloaders.yfinance_dl import YFinanceDownloader

            fallback = YFinanceDownloader()
            fallback.download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=DATA_DIR,
                start=start or None,
                end=end or None,
            )
        return

    if selected_source == "yfinance":
        from data_tools.downloaders.yfinance_dl import YFinanceDownloader

        downloader = YFinanceDownloader()
        downloader.download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=DATA_DIR,
            start=start or None,
            end=end or None,
        )
        return

    if selected_source == "dolthub":
        from data_tools.downloaders.dolthub_dl import DolthubDownloader

        downloader = DolthubDownloader()
        downloader.download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=DATA_DIR,
            start=start or None,
            end=end or None,
        )
        return

    if selected_source in {"dukascopy", "jforex"}:
        from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader

        downloader = DukascopyNativeDownloader()
        downloader.download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=DATA_DIR,
            start=start or None,
            end=end or None,
            offer_side="bid",
        )
        return

    raise ValueError(f"Unsupported auto-download source '{selected_source}' for {normalized_symbol}")


def _ensure_backtest_data_availability(
    *,
    symbols: list[str],
    timeframe: str,
    start: str = "",
    end: str = "",
    progress_callback: Callable[[float, str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    normalized_timeframe = normalize_timeframe(timeframe)
    symbol_list = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not symbol_list or not normalized_timeframe:
        return []

    availability_index = _build_ohlcv_availability_index()
    missing_items: list[dict[str, Any]] = []
    for symbol in symbol_list:
        availability = _data_check_result(
            symbol=symbol,
            timeframe=normalized_timeframe,
            start=start,
            end=end,
            availability_index=availability_index,
        )
        if availability.get("available"):
            continue
        missing_items.append(
            {
                "symbol": symbol,
                "timeframe": normalized_timeframe,
                "missing_range": availability.get("missing_range"),
                "source": _auto_download_source(symbol, normalized_timeframe),
            }
        )

    if not missing_items:
        return []

    total_missing = len(missing_items)
    for index, missing in enumerate(missing_items, start=1):
        progress_before = 10.0 + ((index - 1) / total_missing) * 35.0
        if progress_callback is not None:
            progress_callback(progress_before, f"Downloading data... {int(round(progress_before))}%")

        symbol = str(missing["symbol"])
        source = str(missing["source"])
        missing_range = missing.get("missing_range") if isinstance(missing.get("missing_range"), dict) else {}
        download_start = str((missing_range or {}).get("start") or start or "").strip() or None
        download_end = str((missing_range or {}).get("end") or end or "").strip() or None

        source_plan = _auto_download_source_plan(
            symbol=symbol,
            timeframe=normalized_timeframe,
            start=download_start,
            end=download_end,
        )
        for source_index, step in enumerate(source_plan, start=1):
            step_source = str(step.get("source") or source or "").strip().lower()
            step_start = str(step.get("start") or "").strip() or None
            step_end = str(step.get("end") or "").strip() or None
            if log_callback is not None:
                suffix = (
                    f" [{source_index}/{len(source_plan)}]"
                    if len(source_plan) > 1
                    else ""
                )
                log_callback(
                    (
                        f"Auto-download{suffix}: {symbol} {normalized_timeframe} via {step_source} "
                        f"({step_start or 'auto'} → {step_end or 'latest'})"
                    )
                )
            _download_missing_ohlcv_data(
                symbol=symbol,
                timeframe=normalized_timeframe,
                start=step_start,
                end=step_end,
                source=step_source,
            )
        cache_manager.invalidate_cache(DATA_DIR, reason="auto-backtest-download")
        _invalidate_data_views_cache()
        _refresh_catalog()

        availability_index = _build_ohlcv_availability_index()
        verification = _data_check_result(
            symbol=symbol,
            timeframe=normalized_timeframe,
            start=start,
            end=end,
            availability_index=availability_index,
        )
        if not verification.get("available"):
            coverage_entry = availability_index.get((symbol, normalized_timeframe))
            tolerated_gap = False
            tolerated_reason: str | None = None
            if isinstance(coverage_entry, dict):
                tolerated_gap, tolerated_reason = _allow_small_backtest_boundary_gap(
                    requested_start=start,
                    requested_end=end,
                    available_start=str(coverage_entry.get("start") or ""),
                    available_end=str(coverage_entry.get("end") or ""),
                )
            if tolerated_gap:
                if log_callback is not None:
                    available_start = str((coverage_entry or {}).get("start") or "unknown")
                    available_end = str((coverage_entry or {}).get("end") or "unknown")
                    log_callback(
                        (
                            f"Auto-download coverage tolerance for {symbol} {normalized_timeframe}: "
                            f"{tolerated_reason}; available range {available_start} → {available_end}."
                        )
                    )
            else:
                raise RuntimeError(
                    f"Auto-download did not provide required data range for {symbol} {normalized_timeframe}"
                )

        progress_after = 10.0 + (index / total_missing) * 35.0
        if progress_callback is not None:
            progress_callback(progress_after, f"Downloading data... {int(round(progress_after))}%")

    return missing_items


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
    normalized_tf = normalize_timeframe(timeframe)
    sorted_paths = sorted(paths)
    for path_value in sorted_paths:
        if normalize_timeframe(Path(path_value).stem) == normalized_tf:
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
            "timeframe": normalize_timeframe(str(entry.get("timeframe") or "unknown")),
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


@app.get("/api/signal-processors", response_model=list[dict[str, str]])
async def list_signal_processors() -> list[dict[str, str]]:
    return await asyncio.to_thread(list_signal_processor_options)


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
    name: str = Query(default=""),
    total_return_min: float | None = Query(default=None),
    total_return_max: float | None = Query(default=None),
    sharpe_min: float | None = Query(default=None),
    sharpe_max: float | None = Query(default=None),
    psr_min: float | None = Query(default=None),
    psr_max: float | None = Query(default=None),
    p_value_min: float | None = Query(default=None),
    p_value_max: float | None = Query(default=None),
    cagr_min: float | None = Query(default=None),
    cagr_max: float | None = Query(default=None),
    win_rate_min: float | None = Query(default=None),
    win_rate_max: float | None = Query(default=None),
    drawdown_min: float | None = Query(default=None),
    drawdown_max: float | None = Query(default=None),
    max_drawdown_min: float | None = Query(default=None),
    max_drawdown_max: float | None = Query(default=None),
) -> BacktestsListResponse:
    normalized_sort = _normalize_backtest_sort(sort)
    normalized_order = _normalize_order(order, default="desc")
    sorted_items = _backtest_meta_list_sorted(normalized_sort, normalized_order)
    sorted_items = _filter_backtests_by_name(sorted_items, name)
    effective_max_drawdown_min = (
        max_drawdown_min if max_drawdown_min is not None else drawdown_min
    )
    effective_max_drawdown_max = (
        max_drawdown_max if max_drawdown_max is not None else drawdown_max
    )
    sorted_items = _apply_backtest_numeric_filters(
        sorted_items,
        total_return_min=total_return_min,
        total_return_max=total_return_max,
        sharpe_min=sharpe_min,
        sharpe_max=sharpe_max,
        psr_min=psr_min,
        psr_max=psr_max,
        p_value_min=p_value_min,
        p_value_max=p_value_max,
        cagr_min=cagr_min,
        cagr_max=cagr_max,
        win_rate_min=win_rate_min,
        win_rate_max=win_rate_max,
        max_drawdown_min=effective_max_drawdown_min,
        max_drawdown_max=effective_max_drawdown_max,
    )
    total = len(sorted_items)
    pages = max(1, math.ceil(total / per_page)) if per_page > 0 else 1
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]
    summaries = [_backtest_summary_from_meta(item) for item in page_items]
    return BacktestsListResponse(
        items=summaries,
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        sort=normalized_sort,
        order=normalized_order,
    )


@app.get("/api/backtests/all", response_model=list[dict[str, Any]])
async def list_backtests_all(
    include_equity_preview: bool = Query(default=False),
    preview_limit: int = Query(default=50, ge=1, le=100),
    name: str = Query(default=""),
) -> list[dict[str, Any]]:
    items = _snapshot_backtest_meta_cache()
    items = _filter_backtests_by_name(items, name)
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


@app.post("/api/backtests/remove-empty", response_model=dict[str, int])
async def remove_empty_backtests() -> dict[str, int]:
    removed = 0
    for item in _snapshot_backtest_meta_cache():
        backtest_id = str(item.get("id") or "").strip()
        if not backtest_id:
            continue
        trade_count = max(int(_safe_float(item.get("trade_count"), 0.0)), 0)
        if trade_count != 0:
            continue
        if _delete_backtest_with_artifacts(backtest_id):
            removed += 1
    return {"removed": removed}


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


@app.get("/api/backtests/compare", response_model=dict[str, Any])
async def compare_two_backtests(ids: str = Query(default="")) -> dict[str, Any]:
    if not ids:
        raise HTTPException(status_code=400, detail="Provide ?ids=id1,id2")

    requested_ids = [item.strip() for item in ids.split(",") if item.strip()]
    unique_ids: list[str] = []
    seen: set[str] = set()
    for backtest_id in requested_ids:
        if backtest_id in seen:
            continue
        unique_ids.append(backtest_id)
        seen.add(backtest_id)

    if len(unique_ids) != 2:
        raise HTTPException(status_code=400, detail="Provide exactly 2 unique backtest IDs")

    backtest1 = _backtest_compare_payload(unique_ids[0])
    backtest2 = _backtest_compare_payload(unique_ids[1])
    stats1 = backtest1.get("stats", {})
    stats2 = backtest2.get("stats", {})

    def delta(metric: str) -> float:
        return round(_safe_float(stats2.get(metric), 0.0) - _safe_float(stats1.get(metric), 0.0), 6)

    return {
        "ids": unique_ids,
        "backtest1": backtest1,
        "backtest2": backtest2,
        "comparison": {
            "delta_return": delta("total_return"),
            "delta_net_profit": delta("net_profit"),
            "delta_sharpe": delta("sharpe_ratio"),
            "delta_psr": delta("psr"),
            "delta_p_value": delta("p_value"),
            "delta_cagr": delta("cagr"),
            "delta_max_drawdown": delta("max_drawdown"),
            "delta_win_rate": delta("win_rate"),
            "delta_trade_count": delta("trade_count"),
        },
    }


@app.get("/api/backtest/{backtest_id}", response_model=dict[str, Any])
@app.get("/api/backtests/{backtest_id}", response_model=dict[str, Any])
async def get_backtest(
    backtest_id: str,
    include_trades: bool = Query(default=True),
    include_equity: bool = Query(default=False),
    include_indicators: bool = Query(default=False),
    include_drawings: bool = Query(default=False),
) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    try:
        payload = result.to_dict()
        if not include_trades:
            payload.pop("trades", None)
        if not include_equity:
            payload.pop("equity_curve", None)
        if not include_indicators:
            payload.pop("indicators", None)
        if not include_drawings:
            payload.pop("drawings", None)
        return payload
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
        save_result_to_sqlite(result)
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


@app.put("/api/backtests/{backtest_id}/name", response_model=dict[str, Any])
async def rename_backtest_name(backtest_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    return await rename_backtest(backtest_id, payload)


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
async def get_equity_curve(
    backtest_id: str,
    max_points: int = Query(default=0, ge=0, le=20000),
    sampling: str = Query(default="none"),
) -> list[dict[str, float]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    equity_curve = legacy_viewer._equity_to_viewer(result)
    equity_curve.sort(key=lambda point: _safe_float(point.get("time"), 0.0))
    if max_points and max_points > 0 and str(sampling or "").strip().lower() in {"minmax", "min_max"}:
        return _downsample_equity_curve_minmax(equity_curve, max_points=max_points)
    return equity_curve


def _downsample_equity_curve_minmax(
    points: list[dict[str, float]] | list[dict[str, Any]],
    *,
    max_points: int,
) -> list[dict[str, float]]:
    """Downsample equity curve points while preserving drawdowns/peaks.

    Input points are expected to have at least: {time, equity}. Output stays compatible with frontend.
    """
    if not isinstance(points, list) or not points:
        return []
    try:
        limit = int(max_points)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return [dict(p) for p in points if isinstance(p, dict)]
    limit = max(10, min(limit, 20000))

    normalized: list[dict[str, float]] = []
    for raw in points:
        if not isinstance(raw, dict):
            continue
        t = _safe_float(raw.get("time"), None)
        e = _safe_float(raw.get("equity"), None)
        if t is None or e is None:
            continue
        if not math.isfinite(t) or not math.isfinite(e):
            continue
        normalized.append({"time": float(t), "equity": float(e)})

    if len(normalized) <= limit:
        return normalized

    # Keep endpoints stable.
    first = normalized[0]
    last = normalized[-1]

    # Reserve room for first/last.
    remaining = max(2, limit - 2)
    bucket_count = max(1, remaining // 2)

    n = len(normalized)
    step = n / bucket_count
    sampled: list[dict[str, float]] = [first]

    for bucket in range(bucket_count):
        start = int(bucket * step)
        end = int((bucket + 1) * step)
        if bucket == bucket_count - 1:
            end = n
        if start >= n:
            break
        if end <= start:
            end = min(n, start + 1)

        window = normalized[start:end]
        if not window:
            continue

        min_point = min(window, key=lambda p: p["equity"])
        max_point = max(window, key=lambda p: p["equity"])

        if min_point["time"] == max_point["time"] and min_point["equity"] == max_point["equity"]:
            sampled.append(min_point)
        else:
            # Add in chronological order to avoid artificial backtracking.
            if min_point["time"] <= max_point["time"]:
                sampled.extend([min_point, max_point])
            else:
                sampled.extend([max_point, min_point])

    sampled.append(last)

    # De-duplicate same-time points while keeping the later one.
    deduped: list[dict[str, float]] = []
    for p in sampled:
        if deduped and deduped[-1]["time"] == p["time"]:
            deduped[-1] = p
        else:
            deduped.append(p)

    if len(deduped) > limit:
        # If we still exceed, drop internal points evenly while preserving endpoints.
        inner = deduped[1:-1]
        if not inner:
            return [first, last]
        keep = max(0, limit - 2)
        if keep <= 0:
            return [first, last]
        step2 = len(inner) / keep
        reduced_inner = [inner[int(i * step2)] for i in range(keep)]
        return [first, *reduced_inner, last]

    return deduped


@app.get("/api/backtests/{backtest_id}/equity-preview", response_model=list[float])
async def get_backtest_equity_preview(
    backtest_id: str,
    limit: int = Query(default=50, ge=1, le=100),
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


@app.get("/api/backtests/equity-preview/batch", response_model=dict[str, list[float]])
async def get_backtest_equity_preview_batch(
    ids: str = Query(default=""),
    limit: int = Query(default=48, ge=1, le=100),
) -> dict[str, list[float]]:
    requested_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in str(ids or "").split(","):
        backtest_id = raw_id.strip()
        if not backtest_id or backtest_id in seen:
            continue
        requested_ids.append(backtest_id)
        seen.add(backtest_id)
    if not requested_ids:
        return {}
    requested_ids = requested_ids[:100]

    _ensure_backtest_meta_cache()
    with _BACKTEST_META_CACHE_LOCK:
        existing_ids = [
            backtest_id for backtest_id in requested_ids if backtest_id in _BACKTEST_META_CACHE
        ]
    if not existing_ids:
        return {}

    _bulk_warm_backtest_equity_preview(existing_ids, limit=limit)
    return {
        backtest_id: _cached_backtest_equity_preview_only(backtest_id, limit=limit)
        for backtest_id in existing_ids
    }


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
async def get_backtest_trades(
    backtest_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    paged_trades = result.trades[offset : offset + limit]
    return [legacy_viewer._trade_to_viewer(trade) for trade in paged_trades]


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


def _to_unix_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric) and numeric > 0:
            return numeric
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
            if math.isfinite(numeric) and numeric > 0:
                return numeric
        except (TypeError, ValueError):
            pass
        parsed = pd.to_datetime(text, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return float(pd.Timestamp(parsed).timestamp())
    return None


def _trade_event_unix_seconds(trade_payload: dict[str, Any]) -> list[float]:
    timestamps: list[float] = []
    for unix_key, iso_key in (("entry_time", "entry_time_iso"), ("exit_time", "exit_time_iso")):
        unix_time = _to_unix_seconds(trade_payload.get(unix_key))
        if unix_time is None:
            unix_time = _to_unix_seconds(trade_payload.get(iso_key))
        if unix_time is not None:
            timestamps.append(unix_time)

    exits = trade_payload.get("exits")
    if isinstance(exits, list):
        for exit_payload in exits:
            if not isinstance(exit_payload, dict):
                continue
            unix_time = _to_unix_seconds(exit_payload.get("time"))
            if unix_time is None:
                unix_time = _to_unix_seconds(exit_payload.get("time_iso"))
            if unix_time is not None:
                timestamps.append(unix_time)
    return timestamps


def _replay_keep_mask_from_trades(df: pd.DataFrame, trade_payloads: list[dict[str, Any]]) -> np.ndarray:
    keep = np.zeros(len(df), dtype=bool)
    if "timestamp" not in df.columns:
        keep[:] = True
        return keep
    if len(df) <= REPLAY_MAX_BARS_WITHOUT_TRADES:
        keep[:] = True
        return keep

    event_seconds: list[float] = []
    for trade_payload in trade_payloads:
        event_seconds.extend(_trade_event_unix_seconds(trade_payload))
    if not event_seconds:
        keep[max(0, len(df) - REPLAY_MAX_BARS_WITHOUT_TRADES):] = True
        return keep

    event_array = np.array(event_seconds, dtype=np.float64)
    event_array = event_array[np.isfinite(event_array) & (event_array > 0)]
    if event_array.size == 0:
        keep[max(0, len(df) - REPLAY_MAX_BARS_WITHOUT_TRADES):] = True
        return keep
    event_ns = np.unique(np.round(event_array * 1_000_000_000.0).astype(np.int64))

    ts_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    timestamp_values = ts_series.array.asi8.astype(np.int64, copy=False)
    unit = str(getattr(ts_series.dtype, "unit", "ns") or "ns").lower()
    to_ns_factor = {
        "s": 1_000_000_000,
        "ms": 1_000_000,
        "us": 1_000,
        "ns": 1,
    }.get(unit, 1)
    if to_ns_factor != 1:
        timestamp_values = timestamp_values.copy()
        valid_for_scale = timestamp_values != np.iinfo(np.int64).min
        timestamp_values[valid_for_scale] = timestamp_values[valid_for_scale] * to_ns_factor
    valid_ts = timestamp_values != np.iinfo(np.int64).min
    if not np.any(valid_ts):
        keep[max(0, len(df) - REPLAY_MAX_BARS_WITHOUT_TRADES):] = True
        return keep

    event_count = max(1, int(event_ns.size))
    budget_per_event = max(REPLAY_MIN_BARS_PER_EVENT, REPLAY_TARGET_BARS // event_count)
    bars_per_event = min(REPLAY_MAX_BARS_PER_EVENT, budget_per_event)
    window_before = max(1, int(round(bars_per_event * 0.6)))
    window_after = max(1, bars_per_event - window_before)
    insert_idx = np.searchsorted(timestamp_values, event_ns, side="left")
    insert_idx = np.clip(insert_idx, 0, len(df) - 1)
    prev_idx = np.clip(insert_idx - 1, 0, len(df) - 1)
    current_delta = np.abs(timestamp_values[insert_idx] - event_ns)
    prev_delta = np.abs(timestamp_values[prev_idx] - event_ns)
    choose_prev = (insert_idx > 0) & (prev_delta <= current_delta)
    nearest_idx = np.where(choose_prev, prev_idx, insert_idx)

    starts = np.maximum(0, nearest_idx - window_before)
    ends = np.minimum(len(df), nearest_idx + window_after + 1)
    diff = np.zeros(len(df) + 1, dtype=np.int32)
    np.add.at(diff, starts, 1)
    np.add.at(diff, ends, -1)
    keep = np.cumsum(diff[:-1]) > 0
    keep &= valid_ts

    if not np.any(keep):
        keep[max(0, len(df) - REPLAY_MAX_BARS_WITHOUT_TRADES):] = True
    return keep


def _slice_replay_ohlcv(df: pd.DataFrame, trade_payloads: list[dict[str, Any]]) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    keep_mask = _replay_keep_mask_from_trades(df, trade_payloads)
    if keep_mask.size != len(df) or np.all(keep_mask):
        return df
    return df.iloc[np.flatnonzero(keep_mask)]


def _slice_indicators_to_timestamps(indicators: list[dict[str, Any]], *, ohlcv_times: list[float]) -> None:
    if not ohlcv_times:
        for indicator in indicators:
            indicator["data"] = []
        return

    allowed_keys = {round(float(value), 6) for value in ohlcv_times if math.isfinite(float(value))}
    for indicator in indicators:
        points = indicator.get("data")
        if not isinstance(points, list):
            continue
        filtered: list[dict[str, Any]] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                point_time = float(point.get("time", 0) or 0)
            except (TypeError, ValueError):
                continue
            if round(point_time, 6) in allowed_keys:
                filtered.append(point)
        indicator["data"] = filtered


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
    try:
        trade = next((item for item in result.trades if item.id == trade_nr), None)
        if trade is None:
            raise HTTPException(status_code=404, detail=f"Trade #{trade_nr} not found")

        trade_payload = legacy_viewer._trade_to_viewer(trade)
        timeframe = (
            result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
        )
        timeframe = normalize_timeframe(timeframe)
        symbol = str(trade.symbol or "").strip() or "SPY"

        ohlcv: list[dict[str, Any]] = []
        df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
        if df is None or len(df) == 0:
            details = legacy_viewer._missing_ohlcv_details(
                symbol,
                timeframe,
                backtest_start=str(result.meta.backtest_start or ""),
                backtest_end=str(result.meta.backtest_end or ""),
            )
            raise HTTPException(status_code=404, detail=details)

        ohlcv_start_idx = 0
        ohlcv_end_idx = 0
        entry_ts_raw = pd.to_datetime(trade.entry_time, utc=True, errors="coerce")
        entry_ts = pd.Timestamp(entry_ts_raw) if not pd.isna(entry_ts_raw) else None
        if entry_ts is not None:
            idx = df["timestamp"].searchsorted(entry_ts)
            ohlcv_start_idx = max(0, idx - legacy_viewer.BARS_BEFORE)
            ohlcv_end_idx = min(len(df), idx + legacy_viewer.BARS_AFTER)
            ohlcv = legacy_viewer._df_to_ohlcv_records(df.iloc[ohlcv_start_idx:ohlcv_end_idx])

        indicators = _normalize_indicator_payloads(list(result.indicators))
        drawings = _collect_viewer_drawings(result, symbol)

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
            "symbol": symbol,
            "timeframe": timeframe,
            "indicators": indicators,
            "drawings": drawings,
            "total_trades": len(result.trades),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Trade viewer context failed for backtest '%s' trade #%s: %s",
            backtest_id,
            trade_nr,
            exc,
        )
        raise HTTPException(status_code=500, detail="Failed to load trade context") from exc


@app.get("/api/backtest/{backtest_id}/replay", response_model=dict[str, Any])
async def get_backtest_replay(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        trades = [legacy_viewer._trade_to_viewer(trade) for trade in result.trades]
        timeframe = (
            result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
        )
        timeframe = normalize_timeframe(timeframe)
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
            symbol_trades = [trade for trade in trades if str(trade.get("symbol") or "").upper() == symbol.upper()]
            replay_df = _slice_replay_ohlcv(df, symbol_trades)
            ohlcv_by_symbol[symbol] = legacy_viewer._df_to_ohlcv_records(replay_df)

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
                    primary_trades = [
                        trade for trade in trades if str(trade.get("symbol") or "").upper() == primary_symbol.upper()
                    ]
                    replay_df = _slice_replay_ohlcv(df, primary_trades)
                    ohlcv_times = [row["time"] for row in ohlcv_by_symbol[primary_symbol]]
                    indicators = legacy_viewer._compute_indicators(replay_df, detected, ohlcv_times)

        if indicators and ohlcv_by_symbol.get(primary_symbol):
            symbol_ohlcv = ohlcv_by_symbol[primary_symbol]
            _slice_indicators_to_timestamps(
                indicators,
                ohlcv_times=[float(item.get("time", 0) or 0) for item in symbol_ohlcv],
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Trade replay payload failed for backtest '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail="Failed to load trade replay") from exc


@app.get("/api/backtest/{backtest_id}/ohlcv", response_model=dict[str, Any])
async def get_backtest_ohlcv(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")

    symbols = result.meta.data.get("symbols", [])
    timeframe = (
        result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"
    )
    timeframe = normalize_timeframe(timeframe)

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


def _backtest_compare_payload(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    raw = legacy_viewer._canonical_stats_from_result(result)
    equity_curve = legacy_viewer._equity_to_viewer(result)
    display_name = str(
        result.meta.notes
        or (result.meta.strategies[0] if result.meta.strategies else "")
        or backtest_id
    ).strip() or backtest_id
    symbols = [
        str(symbol).strip().upper()
        for symbol in ((result.meta.data or {}).get("symbols", []) if isinstance(result.meta.data, dict) else [])
        if str(symbol).strip()
    ]
    timeframe = normalize_timeframe(
        str(
            ((result.meta.data or {}).get("timeframes", ["1h"])[0])
            if isinstance(result.meta.data, dict) and (result.meta.data or {}).get("timeframes")
            else "1h"
        )
    )
    win_rate = _normalize_win_rate(raw.get("win_rate")) or 0.0
    winning_trades = sum(1 for trade in result.trades if _safe_float(trade.pnl, 0.0) > 0)
    losing_trades = max(len(result.trades) - winning_trades, 0)

    return {
        "id": backtest_id,
        "name": display_name,
        "strategy_name": str(result.meta.strategies[0] if result.meta.strategies else ""),
        "engine": str(result.meta.engine or "unknown"),
        "symbols": symbols,
        "timeframe": timeframe,
        "stats": {
            "total_return": round(_safe_float(raw.get("total_return"), 0.0) * 100.0, 6),
            "net_profit": round(_safe_float(raw.get("total_pnl"), 0.0), 6),
            "sharpe_ratio": round(_safe_float(raw.get("sharpe_ratio"), 0.0), 6),
            "psr": round(_safe_float(raw.get("psr"), 0.0) * 100.0, 6),
            "p_value": round(_safe_float(raw.get("p_value"), 1.0), 6),
            "cagr": round(_safe_float(raw.get("cagr"), 0.0) * 100.0, 6),
            "max_drawdown": round(abs(_safe_float(raw.get("max_drawdown_pct"), 0.0)) * 100.0, 6),
            "win_rate": round(win_rate * 100.0, 6),
            "trade_count": int(_safe_float(raw.get("total_trades"), len(result.trades))),
        },
        "equity_curve": equity_curve,
        "trade_distribution": {
            "total_trades": len(result.trades),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
        },
    }


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
    timeframe = normalize_timeframe(str(payload.timeframe or "1d"))
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
                scaled = 45.0 + (max(0.0, min(percent, 100.0)) * 0.55)
                if progress_message:
                    return (scaled, f"Running backtest... {int(round(scaled))}%")
                return (scaled, "Running backtest...")
            return None
        if "auto-detected date range" in normalized:
            return (50.0, "Running backtest... 50%")
        if "starting command:" in normalized:
            return (52.0, "Running backtest... 52%")
        if "running backtest:" in normalized:
            return (58.0, "Running backtest... 58%")
        if "result:" in normalized and "trades" in normalized:
            return (82.0, "Processing trades...")
        if normalized.startswith("result:"):
            return (90.0, "Calculating statistics...")
        if normalized.startswith("saved to:"):
            return (96.0, "Saving results...")
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
                    ingest_task_progress=False,
                )

        def report_download_phase(percent: float, message: str) -> None:
            report_stage(handle, percent=percent, message=message)

        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            try:
                _set_task_status(task_id, "running")
                _running_jobs[task_id]["status"] = "running"
                report_stage(handle, percent=10.0, message="Loading market data...")

                missing_downloads = _ensure_backtest_data_availability(
                    symbols=symbols,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    progress_callback=report_download_phase,
                    log_callback=lambda message: _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] {message}",
                        stream="task",
                        source="backtest",
                        ingest_task_progress=False,
                    ),
                )
                if missing_downloads:
                    report_stage(handle, percent=45.0, message="Downloading data... 45%")
                report_stage(handle, percent=45.0, message="Running backtest... 45%")

                command_display = " ".join(str(part) for part in cmd)
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                    source="backtest",
                    ingest_task_progress=False,
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
                                ingest_task_progress=False,
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
                            output_path = parsed_message[len("saved to:"):].strip()
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
                    report_stage(handle, percent=96.0, message="Saving results...")
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
                        ingest_task_progress=False,
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
                        ingest_task_progress=False,
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
            except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Backtest failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="backtest",
                    ingest_task_progress=False,
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


@app.post("/api/backtests/run-signals", response_model=dict[str, Any], status_code=202)
async def run_backtest_signals(payload: BacktestRunSignalsRequest) -> dict[str, Any]:
    requested_timeframe = normalize_timeframe(str(payload.timeframe or "").strip()) or ""
    start_dt = _parse_request_date(payload.start_date, field_name="start_date")
    end_dt = _parse_request_date(payload.end_date, field_name="end_date", end_of_day=True)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    direct_signals_payload = list(payload.signals or [])
    source_name = str(payload.signal_source or "").strip().lower()
    valid_sources = {"discord", "internal", "csv", "discord-history", "manual"}
    if source_name and source_name not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail="signal_source must be one of: discord, internal, csv, discord-history, manual",
        )
    normalized_source = source_name if source_name in {"discord", "internal"} else ""
    account_id = str(payload.account_id or "").strip()
    signal_processor_name = str(payload.signal_processor or "").strip().lower()
    if signal_processor_name:
        try:
            get_signal_processor(signal_processor_name, {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not direct_signals_payload and not normalized_source:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'signals' or 'signal_source'",
        )

    risk_per_trade_pct = (
        float(payload.risk_per_trade_pct)
        if payload.risk_per_trade_pct is not None
        else 0.01
    )
    if risk_per_trade_pct <= 0:
        raise HTTPException(status_code=400, detail="risk_per_trade_pct must be > 0")

    max_position_pct = (
        float(payload.max_position_pct)
        if payload.max_position_pct is not None
        else 0.05
    )
    if max_position_pct <= 0:
        raise HTTPException(status_code=400, detail="max_position_pct must be > 0")

    max_positions = int(payload.max_positions or 5)
    if max_positions <= 0:
        raise HTTPException(status_code=400, detail="max_positions must be > 0")

    default_timeframe = requested_timeframe or "1h"
    if direct_signals_payload:
        normalized_signals: list[dict[str, Any]] = [
            _normalize_replay_signal_payload(item, default_timeframe=default_timeframe)
            for item in direct_signals_payload
        ]
        replay_source_label = source_name or str(
            normalized_signals[0].get("source") if normalized_signals else "manual"
        )
        replay_source_label = replay_source_label.strip().lower() or "manual"
    else:
        if normalized_source not in {"discord", "internal"}:
            raise HTTPException(
                status_code=400,
                detail="signal_source must be 'discord' or 'internal' when signals are omitted",
            )
        deployments = await asyncio.to_thread(list_live_deployments)
        candidate_deployment_ids = [
            str(deployment.id)
            for deployment in deployments
            if str(deployment.signal_source or "internal").lower() == normalized_source
            and (not account_id or str(deployment.account_id) == account_id)
        ]
        if not candidate_deployment_ids:
            raise HTTPException(
                status_code=404,
                detail="No deployments found for requested signal_source/account_id",
            )
        db_signals = await asyncio.to_thread(
            list_live_signals_for_replay,
            deployment_ids=candidate_deployment_ids,
            source=normalized_source,
            start=start_dt,
            end=end_dt,
            limit=20000,
        )
        normalized_signals = []
        for row in db_signals:
            signal_time = row.get("timestamp")
            if not isinstance(signal_time, datetime):
                continue
            if signal_time < start_dt or signal_time > end_dt:
                continue
            normalized_signals.append(
                {
                    "timestamp": signal_time,
                    "symbol": str(row.get("symbol") or "").strip().upper(),
                    "action": str(row.get("action") or "").strip().lower(),
                    "entry_price": _safe_float(row.get("entry_price"), 0.0),
                    "stop_loss": row.get("stop_loss"),
                    "take_profit": row.get("take_profit"),
                    "timeframe": normalize_timeframe(str(row.get("timeframe") or default_timeframe))
                    or default_timeframe,
                    "source": str(row.get("source") or normalized_source or "internal").lower(),
                    "metadata": (
                        dict(row.get("metadata"))
                        if isinstance(row.get("metadata"), dict)
                        else {}
                    ),
                }
            )
        replay_source_label = normalized_source

    normalized_signals = [
        item
        for item in normalized_signals
        if str(item.get("symbol") or "").strip()
        and str(item.get("action") or "") in {"buy", "sell"}
        and _safe_float(item.get("entry_price"), 0.0) > 0
    ]
    if not normalized_signals:
        raise HTTPException(status_code=400, detail="No valid signals to replay")

    timeframe = _resolve_signal_replay_timeframe(
        requested_timeframe=requested_timeframe,
        normalized_signals=normalized_signals,
        fallback=default_timeframe,
    )

    symbols = sorted({str(item["symbol"]).strip().upper() for item in normalized_signals})
    start_str = start_dt.date().isoformat()
    end_str = end_dt.date().isoformat()
    task_id = str(uuid.uuid4())[:8]
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    stage_total = 5
    record = _create_task_record(
        task_id,
        name=_signal_replay_task_name(replay_source_label),
        description=(
            f"Replay {len(normalized_signals)} signals "
            f"for {', '.join(symbols)} ({timeframe})."
        ),
        kind="backtest",
        source="signal-replay",
        total_items=stage_total,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record
    _set_task_status(task_id, "queued")
    _update_task_fields(
        task_id,
        restart_config={
            "signal_source": replay_source_label,
            "account_id": account_id,
            "start_date": start_str,
            "end_date": end_str,
            "timeframe": timeframe,
            "capital": float(payload.capital or 100000.0),
            "risk_per_trade_pct": risk_per_trade_pct,
            "max_position_pct": max_position_pct,
            "max_positions": max_positions,
            "signal_processor": signal_processor_name or None,
            "signal_count": len(normalized_signals),
        },
    )
    _running_jobs[task_id] = {
        "status": "queued",
        "result": None,
        "error": None,
        "task_id": task_id,
        "job_id": task_id,
    }

    def _run_signal_replay() -> None:
        stage_progress = 0.0

        def report_stage(
            handle: Any,
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
                    ingest_task_progress=False,
                )

        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            try:
                _set_task_status(task_id, "running")
                _running_jobs[task_id]["status"] = "running"
                report_stage(handle, percent=10.0, message="Preparing signal replay...")

                if bool(payload.auto_download):
                    _ensure_backtest_data_availability(
                        symbols=symbols,
                        timeframe=timeframe,
                        start=start_str,
                        end=end_str,
                        progress_callback=lambda p, m: report_stage(
                            handle, percent=min(45.0, max(12.0, p)), message=m
                        ),
                        log_callback=lambda message: _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] {message}",
                            stream="task",
                            source="backtest",
                            ingest_task_progress=False,
                        ),
                    )
                report_stage(handle, percent=45.0, message="Loading OHLCV data...")

                ohlcv_by_symbol: dict[str, pd.DataFrame] = {}
                missing_symbols: list[str] = []
                for symbol in symbols:
                    frame = legacy_viewer._load_ohlcv_df(symbol, timeframe)
                    if frame is None or frame.empty:
                        missing_symbols.append(symbol)
                        continue
                    frame.columns = [str(col).lower() for col in frame.columns]
                    if "timestamp" not in frame.columns:
                        missing_symbols.append(symbol)
                        continue
                    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
                    frame = frame.dropna(subset=["timestamp"])
                    frame = frame[
                        (frame["timestamp"] >= pd.Timestamp(start_dt))
                        & (frame["timestamp"] <= pd.Timestamp(end_dt))
                    ]
                    if frame.empty:
                        missing_symbols.append(symbol)
                        continue
                    ohlcv_by_symbol[symbol] = frame.reset_index(drop=True)

                if missing_symbols:
                    raise RuntimeError(
                        f"Missing OHLCV data for replay symbols: {', '.join(sorted(missing_symbols))}"
                    )

                replay_signals = [
                    ReplaySignal.from_payload(item, default_timeframe=timeframe)
                    for item in normalized_signals
                ]
                report_stage(handle, percent=60.0, message="Replaying signals...")

                adapter = SignalReplayAdapter(
                    risk_config=RiskConfig(
                        max_risk_per_trade=risk_per_trade_pct,
                        max_total_exposure=max_position_pct,
                        max_positions=max_positions,
                        default_sl_atr_multiplier=2.0,
                    ),
                    initial_capital=float(payload.capital or 100000.0),
                    default_timeframe=timeframe,
                    signal_processor_name=signal_processor_name or None,
                )
                result = adapter.run(
                    signals=replay_signals,
                    ohlcv_by_symbol=ohlcv_by_symbol,
                    timeframe=timeframe,
                    backtest_start=start_str,
                    backtest_end=end_str,
                    metadata={
                        "signal_source": replay_source_label,
                        "signal_processor": signal_processor_name or None,
                        "account_id": account_id or None,
                        "signal_count": len(replay_signals),
                    },
                    progress_callback=lambda percent, _message: report_stage(
                        handle,
                        percent=min(92.0, 60.0 + (percent * 0.32)),
                        message="Replaying signals...",
                        write_log_line=False,
                    ),
                )
                result.meta.tags = sorted(
                    {
                        *(result.meta.tags or []),
                        "signal-replay",
                        "signal_replay",
                    }
                )
                if replay_source_label:
                    result.meta.tags.append(f"source:{replay_source_label}")
                if account_id:
                    result.meta.tags.append(f"account:{account_id}")
                result.meta.tags = sorted({str(tag).strip() for tag in result.meta.tags if str(tag).strip()})

                report_stage(handle, percent=96.0, message="Saving results...")
                BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
                output_path = BACKTESTS_DIR / f"{result.meta.id}.trades.json"
                result.save(output_path)
                save_result_to_sqlite(result)
                _upsert_backtest_meta_cache(result.meta.id)
                compact_result = {
                    "output_path": str(output_path),
                    "backtest_id": result.meta.id,
                    "trade_count": len(result.trades),
                }
                _update_task_fields(task_id, result=compact_result, backtest_id=result.meta.id)
                report_stage(
                    handle,
                    percent=100.0,
                    message=f"Completed - {len(result.trades)} trades found",
                )
                _mark_task_finished(task_id, status="completed", returncode=0)
                _running_jobs[task_id]["status"] = "completed"
                _running_jobs[task_id]["result"] = compact_result
                _running_jobs[task_id]["error"] = None
            except (RuntimeError, ValueError, OSError) as exc:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Signal replay failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="backtest",
                    ingest_task_progress=False,
                )
                _write_task_traceback(handle, task_id, traceback.format_exc(), source="backtest")
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
                _running_jobs[task_id]["status"] = "failed"
                _running_jobs[task_id]["result"] = None
                _running_jobs[task_id]["error"] = str(exc)

    thread = threading.Thread(target=_run_signal_replay, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "job_id": task_id, "status": "queued"}


@app.post("/api/backtests/run-from-signals", response_model=dict[str, Any], status_code=202)
async def run_backtest_from_signals(payload: BacktestRunFromSignalsRequest) -> dict[str, Any]:
    source = str(payload.signal_source or "csv").strip().lower()
    if source in {"discord-history", "discord"}:
        replay_payload = BacktestRunSignalsRequest(
            signal_source="discord",
            signal_processor=payload.signal_processor,
            account_id=payload.account_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            timeframe=normalize_timeframe(str(payload.timeframe or "1m")) or "1m",
            capital=float(payload.capital or 100000.0),
            risk_per_trade_pct=payload.risk_per_trade_pct,
            max_position_pct=payload.max_position_pct,
            max_positions=payload.max_positions,
            auto_download=payload.auto_download,
        )
        return await run_backtest_signals(replay_payload)

    if source != "csv":
        raise HTTPException(status_code=400, detail="signal_source must be 'csv' or 'discord-history'")

    file_path = str(payload.filepath or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="filepath is required for csv signal_source")

    try:
        loaded_signals = await asyncio.to_thread(load_signals_from_csv, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not loaded_signals:
        raise HTTPException(status_code=400, detail="CSV file contains no replayable signals")

    start_dt = _parse_request_date(payload.start_date, field_name="start_date")
    end_dt = _parse_request_date(payload.end_date, field_name="end_date", end_of_day=True)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    source_label = f"csv:{Path(file_path).name}"
    filtered_signals: list[dict[str, Any]] = []
    signal_inputs: list[SignalReplayInput] = []
    for signal in loaded_signals:
        signal_time = pd.to_datetime(signal.get("timestamp"), utc=True, errors="coerce")
        if pd.isna(signal_time):
            continue
        signal_dt = pd.Timestamp(signal_time).to_pydatetime()
        if signal_dt < start_dt or signal_dt > end_dt:
            continue

        action = str(signal.get("action") or "").strip().lower()
        if action not in {"buy", "sell", "long", "short"}:
            continue
        entry_price = _safe_float(signal.get("entry_price"), 0.0)
        if entry_price <= 0:
            continue

        metadata = dict(signal.get("metadata") or {})
        metadata["signal_source_label"] = f"CSV: {Path(file_path).name}"
        metadata["signal_filepath"] = str(Path(file_path).expanduser())
        signal_payload = {
            "timestamp": pd.Timestamp(signal_time).isoformat(),
            "symbol": str(signal.get("symbol") or "").strip().upper(),
            "action": action,
            "entry_price": entry_price,
            "stop_loss": signal.get("stop_loss"),
            "take_profit": signal.get("take_profit"),
            "timeframe": normalize_timeframe(str(signal.get("timeframe") or "")) or "",
            "source": source_label,
            "metadata": metadata,
        }
        filtered_signals.append(signal_payload)
        signal_inputs.append(
            SignalReplayInput(
                timestamp=str(signal_payload["timestamp"]),
                symbol=str(signal_payload["symbol"]),
                action=str(signal_payload["action"]),
                entry_price=float(signal_payload["entry_price"]),
                stop_loss=signal_payload.get("stop_loss"),
                take_profit=signal_payload.get("take_profit"),
                timeframe=str(signal_payload["timeframe"] or ""),
                source=str(signal_payload["source"]),
                metadata=dict(metadata),
            )
        )

    if not signal_inputs:
        raise HTTPException(status_code=400, detail="No replayable CSV signals in selected date range")

    timeframe = _resolve_signal_replay_timeframe(
        requested_timeframe=payload.timeframe,
        normalized_signals=filtered_signals,
        fallback="1h",
    )
    replay_payload = BacktestRunSignalsRequest(
        signals=signal_inputs,
        signal_source=None,
        signal_processor=payload.signal_processor,
        account_id=payload.account_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        timeframe=timeframe,
        capital=float(payload.capital or 100000.0),
        risk_per_trade_pct=payload.risk_per_trade_pct,
        max_position_pct=payload.max_position_pct,
        max_positions=payload.max_positions,
        auto_download=payload.auto_download,
    )
    return await run_backtest_signals(replay_payload)


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
        if path.exists():
            portfolios.append(_normalize_portfolio_payload(json.loads(path.read_text(encoding="utf-8"))))
            continue
        record = load_portfolio_record(portfolio_id)
        if record is not None:
            portfolios.append(_portfolio_record_to_payload(record))
    return {"portfolios": portfolios}


@app.get("/api/portfolios", response_model=list[dict[str, Any]])
async def list_portfolios(
    name: str = Query(default=""),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
) -> list[dict[str, Any]]:
    normalized_sort = _normalize_portfolio_sort(sort)
    normalized_order = _normalize_order(order, default="desc")
    summaries = _snapshot_portfolio_meta_cache()
    summaries = _filter_portfolios_by_name(summaries, name)
    summaries.sort(
        key=lambda item: (_portfolio_sort_key(item, normalized_sort), str(item.get("id") or "")),
        reverse=normalized_order == "desc",
    )
    return summaries


@app.get("/api/portfolios/duplicates", response_model=PortfoliosDuplicatesResponse)
async def list_portfolio_duplicates() -> PortfoliosDuplicatesResponse:
    groups, total_portfolios = _group_duplicate_portfolios()
    return PortfoliosDuplicatesResponse(
        groups=groups,
        total_groups=len(groups),
        total_portfolios=total_portfolios,
    )


@app.post("/api/portfolios/deduplicate", response_model=PortfoliosDeduplicateResponse)
async def deduplicate_portfolios() -> PortfoliosDeduplicateResponse:
    groups, _ = _group_duplicate_portfolios()
    deleted_ids: list[str] = []
    kept_ids: list[str] = []
    affected = 0

    for group in groups:
        portfolios = group.get("portfolios", [])
        if len(portfolios) < 2:
            continue
        keep_id = str(portfolios[0].get("id") or "")
        if keep_id:
            kept_ids.append(keep_id)

        removed_in_group = False
        for entry in portfolios[1:]:
            portfolio_id = str(entry.get("id") or "")
            if not portfolio_id:
                continue
            if _delete_portfolio_with_artifacts(portfolio_id):
                deleted_ids.append(portfolio_id)
                removed_in_group = True
        if removed_in_group:
            affected += 1

    return PortfoliosDeduplicateResponse(
        deleted_ids=deleted_ids,
        kept_ids=kept_ids,
        groups_affected=affected,
    )


@app.post("/api/portfolios/remove-empty", response_model=dict[str, Any])
async def remove_empty_portfolios() -> dict[str, Any]:
    portfolios = _snapshot_portfolio_payloads()
    removed_ids: list[str] = []
    for portfolio in portfolios:
        portfolio_id = str(portfolio.get("id") or "").strip()
        components = portfolio.get("components")
        has_components = isinstance(components, list) and len(components) > 0
        if not portfolio_id or has_components:
            continue
        if _delete_portfolio_with_artifacts(portfolio_id):
            removed_ids.append(portfolio_id)

    remaining = len(_snapshot_portfolio_payloads())
    return {
        "removed": len(removed_ids),
        "remaining": remaining,
        "removed_ids": removed_ids,
    }


@app.post("/api/portfolios", response_model=dict[str, Any], status_code=201)
async def create_portfolio(payload: CreatePortfolioRequest) -> dict[str, Any]:
    name = str(payload.name or "Untitled").strip() or "Untitled"
    account_capital = float(payload.account_capital or 100000.0)
    if not math.isfinite(account_capital) or account_capital <= 0:
        raise HTTPException(status_code=400, detail="account_capital must be positive")

    component_payloads = _resolve_component_payloads(payload)
    allows_empty_portfolio = len(component_payloads) == 0

    component_returns: list[pd.Series] = []
    components: list[dict[str, Any]] = []
    all_trades_timed: list[dict[str, Any]] = []

    for component_payload in component_payloads:
        component_id = str(component_payload.get("component_id") or "").strip()
        strategy_name = str(component_payload.get("strategy_name") or "").strip()
        symbol = _normalize_symbol_key(component_payload.get("symbol"))
        timeframe = _normalize_timeframe_key(component_payload.get("timeframe"))
        weight = _safe_float(component_payload.get("weight"), 1.0)
        risk_config = dict(component_payload.get("risk_config") or {})
        settings = dict(component_payload.get("settings") or {})
        backtest_id = str(component_payload.get("backtest_id") or "").strip()
        if not component_id or not strategy_name or not backtest_id:
            continue

        result = _load_result(backtest_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

        initial_capital = _safe_float(result.meta.initial_capital, account_capital) or account_capital
        equity_curve = _ensure_equity_curve(result, initial_capital=initial_capital)
        if len(equity_curve) < 1:
            continue

        equity_df = pd.DataFrame([point.to_dict() for point in equity_curve])
        if "time" not in equity_df.columns or "equity" not in equity_df.columns:
            continue
        equity_df["time"] = pd.to_datetime(equity_df["time"], utc=True, errors="coerce")
        equity_df["equity"] = pd.to_numeric(equity_df["equity"], errors="coerce")
        equity_df = equity_df.dropna(subset=["time", "equity"])
        if equity_df.empty:
            continue
        equity_df = equity_df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
        equity_df.set_index("time", inplace=True)

        daily_equity = equity_df["equity"].resample("D").last().ffill()
        if daily_equity.empty:
            continue
        daily_returns = daily_equity.pct_change().replace([math.inf, -math.inf], pd.NA).fillna(0.0)
        weighted_returns = daily_returns * weight
        weighted_returns.name = component_id
        component_returns.append(weighted_returns)

        component_equity = account_capital
        for component_return in weighted_returns:
            component_equity *= 1.0 + _safe_float(component_return, 0.0)
        component_return_contribution = (component_equity - account_capital) / account_capital if account_capital else 0.0

        component_max_drawdown = abs(_safe_float(result.stats.get("max_drawdown_pct"), 0.0)) * weight
        component_wins = 0
        component_trade_count = 0

        for trade in result.trades:
            viewer_trade = legacy_viewer._trade_to_viewer(trade)
            trade_payload: dict[str, Any] = dict(viewer_trade)
            trade_payload.update(
                {
                    "component_id": component_id,
                    "strategy_name": strategy_name,
                    "strategy": strategy_name,
                    "symbol": symbol or _normalize_symbol_key(viewer_trade.get("symbol")),
                    "timeframe": timeframe,
                    "backtest_id": backtest_id,
                    "pnl": round(_safe_float(viewer_trade.get("pnl"), 0.0) * weight, 6),
                }
            )
            trade_payload["time"] = _component_trade_time(trade_payload)
            trade_id = f"{component_id}-{trade_payload.get('nr')}"
            trade_payload["id"] = trade_id
            if _safe_float(trade_payload.get("pnl"), 0.0) > 0:
                component_wins += 1
            component_trade_count += 1
            all_trades_timed.append(trade_payload)

        component_win_rate = (component_wins / component_trade_count * 100.0) if component_trade_count else 0.0
        components.append(
            {
                "component_id": component_id,
                "strategy_name": strategy_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "weight": weight,
                "risk_config": risk_config,
                "settings": settings,
                "backtest_id": backtest_id,
                "return_contribution": component_return_contribution,
                "drawdown_contribution": component_max_drawdown,
                "trade_count": component_trade_count,
                "win_rate": component_win_rate,
                "wins": component_wins,
                "legacy_risk_per_trade_pct": _safe_float(
                    component_payload.get("legacy_risk_per_trade_pct"),
                    weight,
                ),
                "legacy_max_positions": int(
                    _safe_float(
                        component_payload.get("legacy_max_positions"),
                        _safe_float(risk_config.get("max_positions"), 99),
                    )
                ),
            }
        )

    if not component_returns and not allows_empty_portfolio:
        raise HTTPException(status_code=400, detail="No valid components found")

    now = datetime.now(timezone.utc)
    if component_returns:
        combined_df = pd.concat(component_returns, axis=1).fillna(0.0)
        portfolio_daily_returns = combined_df.sum(axis=1)
        if len(combined_df.columns) > 1:
            correlation_matrix = combined_df.corr().round(4).fillna(0.0).to_dict()
        else:
            first = combined_df.columns[0]
            correlation_matrix = {first: {first: 1.0}}

        current_equity = account_capital
        combined_equity_points: list[legacy_viewer.EquityPoint] = []
        for timestamp, daily_return in portfolio_daily_returns.items():
            current_equity *= 1.0 + _safe_float(daily_return, 0.0)
            combined_equity_points.append(
                legacy_viewer.EquityPoint(time=pd.Timestamp(timestamp).isoformat(), equity=current_equity)
            )

        stats_result = legacy_viewer.compute_stats(
            trades=[],
            initial_capital=account_capital,
            equity_curve=combined_equity_points,
        )
    else:
        correlation_matrix = {}
        combined_equity_points = [
            legacy_viewer.EquityPoint(time=now.isoformat(), equity=account_capital)
        ]
        stats_result = {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "cagr": 0.0,
            "max_drawdown_pct": 0.0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "calmar_ratio": 0.0,
            "total_return": 0.0,
            "max_drawdown_duration": 0,
            "psr": 0.0,
            "p_value": 1.0,
        }

    portfolio_id = _portfolio_identifier(name, now=now)
    all_trades_timed.sort(key=_component_trade_time)

    portfolio = {
        "id": portfolio_id,
        "name": name,
        "created_at": now.isoformat(),
        "account_capital": account_capital,
        "components": components,
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
        "combined_equity": [point.to_dict() for point in combined_equity_points],
        "trades": all_trades_timed,
    }

    normalized_portfolio = _normalize_portfolio_payload(portfolio)
    db_components: list[PortfolioComponentModel] = []
    for component in normalized_portfolio.get("components", []):
        if not isinstance(component, dict):
            continue
        component_model = PortfolioComponentModel(
            strategy_name=str(component.get("strategy_name") or ""),
            symbol=str(component.get("symbol") or ""),
            timeframe=str(component.get("timeframe") or "1h"),
            weight=_safe_float(component.get("weight"), 1.0),
            risk_config=dict(component.get("risk_config") or {}),
            settings={
                **dict(component.get("settings") or {}),
                "component_id": str(component.get("component_id") or ""),
                "legacy_risk_per_trade_pct": _safe_float(component.get("legacy_risk_per_trade_pct"), 1.0),
                "legacy_max_positions": int(_safe_float(component.get("legacy_max_positions"), 99)),
                "return_contribution": _safe_float(component.get("return_contribution"), 0.0),
                "return_contribution_pct": _safe_float(component.get("return_contribution_pct"), 0.0),
                "drawdown_contribution": _safe_float(component.get("drawdown_contribution"), 0.0),
                "drawdown_contribution_pct": _safe_float(component.get("drawdown_contribution_pct"), 0.0),
                "trade_count": int(_safe_float(component.get("trade_count"), 0)),
                "win_rate": _safe_float(component.get("win_rate"), 0.0),
                "wins": int(_safe_float(component.get("wins"), 0)),
            },
            backtest_id=str(component.get("backtest_id") or "") or None,
        )
        db_components.append(component_model)

    save_portfolio_record(
        portfolio=PortfolioModel(
            id=str(normalized_portfolio.get("id") or portfolio_id),
            name=str(normalized_portfolio.get("name") or name),
            created_at=str(normalized_portfolio.get("created_at") or now.isoformat()),
            account_capital=_safe_float(normalized_portfolio.get("account_capital"), account_capital),
            components=db_components,
            combined_stats=dict(normalized_portfolio.get("combined_stats") or {}),
            combined_equity=list(normalized_portfolio.get("combined_equity") or []),
            strategy_contributions=list(normalized_portfolio.get("strategy_contributions") or []),
            component_contributions=list(normalized_portfolio.get("component_contributions") or []),
            trades=list(normalized_portfolio.get("trades") or []),
        )
    )

    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    output_path.write_text(json.dumps(normalized_portfolio, indent=2), encoding="utf-8")
    _upsert_portfolio_meta_cache_from_payload(normalized_portfolio)
    return normalized_portfolio


def _load_portfolio_payload(portfolio_id: str) -> dict[str, Any]:
    path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    if path.exists():
        return _normalize_portfolio_payload(json.loads(path.read_text(encoding="utf-8")))

    record = load_portfolio_record(portfolio_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return _portfolio_record_to_payload(record)


@app.post("/api/portfolios/delete-bulk", response_model=dict[str, int])
async def delete_portfolios_bulk(payload: PortfoliosBulkDeleteRequest) -> dict[str, int]:
    deleted = 0
    seen: set[str] = set()
    for raw_id in payload.ids:
        portfolio_id = str(raw_id or "").strip()
        if not portfolio_id or portfolio_id in seen:
            continue
        seen.add(portfolio_id)
        if _delete_portfolio_with_artifacts(portfolio_id):
            deleted += 1
    return {"deleted": deleted}


@app.get("/api/portfolios/{portfolio_id}", response_model=dict[str, Any])
async def get_portfolio(portfolio_id: str) -> dict[str, Any]:
    payload = _load_portfolio_payload(portfolio_id)
    payload["combined_stats"] = legacy_viewer.PortfolioStats.from_dict(
        payload.get("combined_stats", {})
    ).to_dict()
    return payload


@app.patch("/api/portfolios/{portfolio_id}", response_model=dict[str, Any])
async def rename_portfolio(portfolio_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    updated_name = str(payload.name or "").strip()
    if not updated_name:
        raise HTTPException(status_code=400, detail="Portfolio name cannot be empty")

    portfolio_payload = _load_portfolio_payload(portfolio_id)
    portfolio_payload["name"] = updated_name
    normalized_portfolio = _normalize_portfolio_payload(portfolio_payload)
    normalized_portfolio["name"] = updated_name

    try:
        save_portfolio_record(_portfolio_payload_to_model(normalized_portfolio))
        PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
        output_path.write_text(json.dumps(normalized_portfolio, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to rename portfolio '%s': %s", portfolio_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _upsert_portfolio_meta_cache_from_payload(normalized_portfolio)
    return {"id": portfolio_id, "name": updated_name, "status": "updated"}


@app.put("/api/portfolios/{portfolio_id}/name", response_model=dict[str, Any])
async def rename_portfolio_name(portfolio_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    return await rename_portfolio(portfolio_id, payload)


@app.get("/api/portfolios/{portfolio_id}/contributions", response_model=list[dict[str, Any]])
async def get_portfolio_contributions(
    portfolio_id: str,
    group_by: str = Query(default="component"),
) -> list[dict[str, Any]]:
    portfolio = _load_portfolio_payload(portfolio_id)
    return _compute_portfolio_contributions(portfolio=portfolio, group_by=group_by)


@app.get("/api/portfolios/{portfolio_id}/trades", response_model=list[dict[str, Any]])
async def get_portfolio_trades(
    portfolio_id: str,
    strategy: str = Query(default=""),
    symbol: str = Query(default=""),
    timeframe: str = Query(default=""),
) -> list[dict[str, Any]]:
    portfolio = _load_portfolio_payload(portfolio_id)
    return _portfolio_trades_filtered(
        portfolio=portfolio,
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
    )


@app.delete("/api/portfolios/{portfolio_id}", response_model=dict[str, Any])
async def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    if not _delete_portfolio_with_artifacts(portfolio_id):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"status": "deleted", "id": portfolio_id}


async def _live_trades_payload(account_id: str) -> dict[str, Any]:
    deployments = await asyncio.to_thread(list_live_deployments_for_account, account_id)
    deployment_ids = _deployment_ids_for_account(account_id, deployments)
    rows = await asyncio.to_thread(list_live_signal_executions_for_deployments, deployment_ids)

    trades: list[dict[str, Any]] = []
    for row in rows:
        signal_metadata = row.get("signal_metadata")
        metadata = signal_metadata if isinstance(signal_metadata, dict) else {}
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
                "position_size": (
                    _safe_float(row.get("position_size"))
                    if row.get("position_size") is not None
                    else (
                        _safe_float(metadata.get("position_size"))
                        if metadata.get("position_size") is not None
                        else None
                    )
                ),
                "risk_amount": (
                    _safe_float(row.get("risk_amount"))
                    if row.get("risk_amount") is not None
                    else (
                        _safe_float(metadata.get("risk_amount"))
                        if metadata.get("risk_amount") is not None
                        else None
                    )
                ),
                "risk_percent": (
                    _safe_float(row.get("risk_percent"))
                    if row.get("risk_percent") is not None
                    else (
                        _safe_float(metadata.get("risk_percent"))
                        if metadata.get("risk_percent") is not None
                        else None
                    )
                ),
                "exit_reason": (
                    str(row.get("exit_reason")).strip()
                    if row.get("exit_reason") not in (None, "")
                    else None
                ),
                "commission": float(row.get("commission", 0.0)),
                "signal_timestamp": signal_iso,
                "signal_time": signal_unix,
                "execution_timestamp": execution_iso,
                "execution_time": execution_unix,
                "broker_order_id": row.get("broker_order_id", ""),
                "signal_metadata": metadata,
            }
        )
    return {"account_id": account_id, "deployment_ids": deployment_ids, "trades": trades}


async def _live_signals_payload(account_id: str, *, limit: int = 200) -> dict[str, Any]:
    deployments = await asyncio.to_thread(list_live_deployments_for_account, account_id)
    deployment_ids = _deployment_ids_for_account(account_id, deployments)
    rows = await asyncio.to_thread(
        list_live_signals_for_deployments,
        deployment_ids,
        limit=limit,
    )

    signals: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("signal_metadata")
        signal_metadata = metadata if isinstance(metadata, dict) else {}
        signal_iso, signal_unix = _serialize_live_timestamp(row.get("signal_timestamp"))
        execution_iso, execution_unix = _serialize_live_timestamp(row.get("execution_timestamp"))
        raw_source = str(row.get("source") or signal_metadata.get("source") or "internal").lower()
        source = "discord" if raw_source.startswith("discord") else "internal"
        status = _signal_status_from_row(row)
        waiting_seconds = (
            _waiting_countdown(signal_metadata, signal_unix)
            if status == "Waiting"
            else None
        )
        if status == "Waiting" and waiting_seconds is not None and waiting_seconds <= 0:
            status = "Pending"
        raw_message = signal_metadata.get("discord_content")
        signals.append(
            {
                "signal_id": str(row.get("signal_id") or ""),
                "deployment_id": str(row.get("deployment_id") or ""),
                "symbol": str(row.get("symbol") or ""),
                "action": str(row.get("action") or ""),
                "status": status,
                "source": source,
                "execution_id": (
                    str(row.get("execution_id") or "")
                    if row.get("execution_id") is not None
                    else None
                ),
                "signal_timestamp": signal_iso,
                "signal_time": signal_unix,
                "execution_timestamp": execution_iso,
                "execution_time": execution_unix,
                "theoretical_price": _safe_float(row.get("theoretical_price"), 0.0),
                "actual_price": (
                    _safe_float(row.get("actual_price"))
                    if row.get("actual_price") is not None
                    else None
                ),
                "filled_qty": (
                    _safe_float(row.get("filled_qty"))
                    if row.get("filled_qty") is not None
                    else None
                ),
                "commission": (
                    _safe_float(row.get("commission"))
                    if row.get("commission") is not None
                    else None
                ),
                "broker_order_id": (
                    str(row.get("broker_order_id") or "")
                    if row.get("broker_order_id") is not None
                    else None
                ),
                "signal_metadata": signal_metadata,
                "raw_message": str(raw_message).strip() if raw_message else None,
                "waiting_for_candle_close": bool(
                    signal_metadata.get("wait_for_candle_close")
                    or signal_metadata.get("wait_candle_close")
                ),
                "waiting_countdown_seconds": waiting_seconds,
            }
        )

    return {
        "account_id": account_id,
        "deployment_ids": deployment_ids,
        "signals": signals,
    }


async def _live_positions_payload(account_id: str) -> dict[str, Any]:
    account = await asyncio.to_thread(get_live_account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Live account not found")
    if account.broker != "alpaca":
        raise HTTPException(
            status_code=400,
            detail="Live positions endpoint currently supports only Alpaca accounts",
        )

    credentials = resolve_alpaca_credentials(
        account_id=account.id,
        broker_login=account.broker_login,
    )
    if credentials is None:
        raise HTTPException(status_code=400, detail="Missing Alpaca credentials for account")
    api_key, secret_key, paper = credentials

    adapter = AlpacaLiveAdapter(
        api_key=api_key,
        secret_key=secret_key,
        paper=paper,
        account_id=account.id,
    )
    rows = await adapter.get_positions()
    positions = [
        {
            "symbol": str(row.get("symbol") or ""),
            "qty": _safe_float(row.get("qty"), 0.0),
            "entry_price": _safe_float(row.get("entry_price"), 0.0),
            "current_price": _safe_float(row.get("current_price"), 0.0),
            "unrealized_pnl": _safe_float(row.get("unrealized_pnl"), 0.0),
        }
        for row in rows
    ]
    return {"account_id": account_id, "positions": positions}


@app.get("/api/live/accounts", response_model=list[dict[str, Any]])
async def get_live_accounts() -> list[dict[str, Any]]:
    _log_live_endpoint_request("/api/live/accounts")
    return await asyncio.to_thread(_cached_live_accounts_payload)


@app.get("/api/services", response_model=list[dict[str, Any]])
async def get_services_endpoint(
    type: str | None = Query(default=None),  # noqa: A002 - follows API contract
) -> list[dict[str, Any]]:
    normalized_type = str(type or "").strip().lower()
    selected_type: ServiceType | None = None
    if normalized_type:
        if normalized_type not in {ServiceType.DISCORD_BOT.value, ServiceType.SCHEDULED_JOB.value}:
            raise HTTPException(status_code=400, detail="type must be 'discord_bot' or 'scheduled_job'")
        selected_type = ServiceType(normalized_type)
    services = await asyncio.to_thread(list_services, service_type=selected_type)
    return [_serialize_service_payload(service) for service in services]


@app.post("/api/services", response_model=dict[str, Any], status_code=201)
async def create_service_endpoint(payload: ServiceCreateRequest) -> dict[str, Any]:
    try:
        service = Service(
            name=str(payload.name or "").strip(),
            type=ServiceType(payload.type),
            status=ServiceStatus(payload.status),
            config=dict(payload.config or {}),
            created_at=datetime.now(timezone.utc),
        )
        created = await asyncio.to_thread(create_service, service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_service_payload(created)


@app.get("/api/services/{service_id}", response_model=dict[str, Any])
async def get_service_endpoint(service_id: str) -> dict[str, Any]:
    service = await asyncio.to_thread(get_service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return _serialize_service_payload(service)


@app.put("/api/services/{service_id}/status", response_model=dict[str, Any])
async def update_service_status_endpoint(
    service_id: str,
    payload: ServiceStatusUpdateRequest,
) -> dict[str, Any]:
    try:
        updated = await asyncio.to_thread(update_service_status, service_id, payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return _serialize_service_payload(updated)


@app.delete("/api/services/{service_id}", response_model=dict[str, Any])
async def delete_service_endpoint(service_id: str) -> dict[str, Any]:
    deleted = await asyncio.to_thread(delete_service, service_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Service not found")
    return {"status": "deleted", "id": service_id}


def _service_log_tokens(service: Service) -> set[str]:
    tokens: set[str] = set()
    for value in (
        str(service.id),
        str(service.name or ""),
        str((service.config or {}).get("channel_id") or ""),
        str((service.config or {}).get("token_env_key") or ""),
    ):
        token = value.strip().lower()
        if token:
            tokens.add(token)
    return tokens


def _entry_matches_service_log(entry: dict[str, Any], *, service: Service, tokens: set[str]) -> bool:
    line = str(entry.get("line") or "")
    line_lower = line.lower()
    if any(token in line_lower for token in tokens):
        return True

    path_name = str(entry.get("path") or "").lower()
    if any(token in path_name for token in tokens):
        return True

    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            payload = parsed
    except json.JSONDecodeError:
        payload = None

    if payload is not None:
        payload_blob = json.dumps(payload, default=str).lower()
        if any(token in payload_blob for token in tokens):
            return True

    if service.type.value == "discord_bot":
        discord_markers = ("discord", "webhook", "signal", "channel")
        if any(marker in line_lower for marker in discord_markers):
            return True
        if payload is not None:
            payload_blob = json.dumps(payload, default=str).lower()
            if any(marker in payload_blob for marker in discord_markers):
                return True

    return False


@app.get("/api/services/{service_id}/logs", response_model=LogContentModel)
async def get_service_logs_endpoint(
    service_id: str,
    lines: int = Query(default=200, ge=1, le=100000),
    level: str = Query(default=""),
    search: str = Query(default=""),
) -> LogContentModel:
    service = await asyncio.to_thread(get_service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    try:
        entries = legacy_viewer._collect_log_entries(legacy_viewer._available_log_files())
        level_filter = legacy_viewer._normalize_log_levels(level)
        if level_filter:
            entries = [entry for entry in entries if entry.get("level") in level_filter]
        service_terms = _service_log_tokens(service)
        entries = [
            entry
            for entry in entries
            if _entry_matches_service_log(entry, service=service, tokens=service_terms)
        ]
        if search:
            query_lower = search.lower()
            entries = [entry for entry in entries if query_lower in entry["line"].lower()]
        total_lines = len(entries)
        recent_entries = entries[-lines:] if len(entries) > lines else entries
        return LogContentModel(
            name=f"service:{service.name}",
            lines=[entry["line"] for entry in recent_entries],
            total_lines=total_lines,
        )
    except Exception as exc:
        logger.exception("Failed to load service logs for %s: %s", service_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/verification/evidence", response_model=list[VerificationEvidenceRecord])
async def get_verification_evidence(
    component: str = Query(default=""),
    category: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[VerificationEvidenceRecord]:
    try:
        return await asyncio.to_thread(
            list_verification_evidence_records,
            component=str(component or "").strip() or None,
            category=str(category or "").strip() or None,
            status=str(status or "").strip() or None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/verification/evidence/summary", response_model=list[VerificationEvidenceSummary])
async def get_verification_evidence_summary(
    component: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[VerificationEvidenceSummary]:
    try:
        return await asyncio.to_thread(
            list_verification_evidence_summary,
            component=str(component or "").strip() or None,
            category=str(category or "").strip() or None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/verification/evidence",
    response_model=VerificationEvidenceRecord,
    status_code=201,
)
async def create_verification_evidence(
    payload: VerificationEvidenceCreateRequest,
) -> VerificationEvidenceRecord:
    try:
        return await asyncio.to_thread(
            upsert_verification_evidence,
            component=str(payload.component or "").strip(),
            flow_name=str(payload.flow_name or "").strip(),
            category=str(payload.category or "").strip(),
            status=payload.status,
            started_at=payload.started_at,
            finished_at=payload.finished_at,
            duration_seconds=payload.duration_seconds,
            git_sha=payload.git_sha,
            environment=payload.environment,
            mode=payload.mode,
            metadata=dict(payload.evidence_metadata or {}),
            error_summary=payload.error_summary,
            evidence_ref=payload.evidence_ref,
            manual_validation_required=payload.manual_validation_required,
            source=payload.source,
            run_id=payload.run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/live/deployments", response_model=list[dict[str, Any]])
async def get_live_deployments() -> list[dict[str, Any]]:
    _log_live_endpoint_request("/api/live/deployments")
    deployments = await asyncio.to_thread(list_live_deployments)
    return [_serialize_live_deployment_payload(deployment) for deployment in deployments]


@app.post("/api/live/deployments", response_model=dict[str, Any], status_code=201)
async def create_live_deployment_endpoint(
    payload: LiveDeploymentCreateRequest,
) -> dict[str, Any]:
    signal_source_type = str(
        payload.signal_source_type or payload.signal_source or "internal"
    ).strip().lower()
    _log_live_endpoint_request(
        "/api/live/deployments",
        strategy_name=payload.strategy_name,
        account_id=payload.account_id,
        symbols=payload.symbols,
        timeframe=payload.timeframe,
        signal_source_type=signal_source_type,
        signal_source_id=payload.signal_source_id,
        symbol_filter=payload.symbol_filter,
    )
    if signal_source_type not in {"internal", "discord"}:
        raise HTTPException(status_code=400, detail="signal_source_type must be 'discord' or 'internal'")

    account_payload = await asyncio.to_thread(_ensure_live_account_for_deployment, payload.account_id)
    inferred_broker = str(account_payload.get("broker", "")).strip().lower()
    if inferred_broker not in {"alpaca", "ctrader"}:
        raise HTTPException(status_code=400, detail="Unknown broker for account")

    strategy_name = str(payload.strategy_name or "").strip()
    symbols = [
        str(symbol).strip().upper()
        for symbol in (payload.symbols or [])
        if str(symbol).strip()
    ]
    symbol_filter = [
        str(symbol).strip().upper()
        for symbol in (payload.symbol_filter or [])
        if str(symbol).strip()
    ]
    signal_source_id = str(payload.signal_source_id or "").strip()
    signal_config_payload = dict(payload.signal_config or {})
    signal_processor_name = str(signal_config_payload.get("signal_processor") or "").strip().lower()
    if signal_processor_name:
        try:
            get_signal_processor(signal_processor_name, {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        signal_config_payload["signal_processor"] = signal_processor_name

    if signal_source_type == "internal":
        if not strategy_name:
            raise HTTPException(status_code=400, detail="strategy_name is required for internal signal source")
        if not symbols:
            raise HTTPException(status_code=400, detail="At least one symbol is required")
        signal_source_id = ""
        symbol_filter = []
    else:
        matched_service: Service | None = None
        if signal_source_id:
            discord_services = await asyncio.to_thread(
                list_services,
                service_type=ServiceType.DISCORD_BOT,
            )
            matched_service = next(
                (
                    service
                    for service in discord_services
                    if str(service.name or "").strip().lower() == signal_source_id.lower()
                ),
                None,
            )
            if matched_service is None:
                raise HTTPException(status_code=404, detail="Discord signal source service not found")
            signal_source_id = matched_service.name
        elif not str(signal_config_payload.get("channel_id") or "").strip():
            raise HTTPException(
                status_code=400,
                detail="signal_source_id is required for discord signal source",
            )
        if not strategy_name:
            strategy_name = (
                f"discord::{signal_source_id.lower()}"
                if signal_source_id
                else "discord::legacy"
            )
        if not symbols and symbol_filter:
            symbols = list(symbol_filter)
        if not symbols:
            symbols = ["*"]
        if signal_source_id:
            signal_config_payload.setdefault("service_name", signal_source_id)
        if matched_service and isinstance(matched_service.config, dict):
            channel_id = str(matched_service.config.get("channel_id") or "").strip()
            if channel_id and not str(signal_config_payload.get("channel_id") or "").strip():
                signal_config_payload["channel_id"] = channel_id

    deployment = LiveDeploymentConfig(
        strategy_name=strategy_name,
        account_id=payload.account_id,
        symbols=symbols,
        timeframe=payload.timeframe,
        status="ACTIVE",
        signal_source_type=signal_source_type,
        signal_source_id=signal_source_id or None,
        symbol_filter=symbol_filter,
        signal_source=signal_source_type,
        signal_config=signal_config_payload,
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
    return await _live_trades_payload(account_id)


@app.get("/api/live/signals/{account_id}", response_model=dict[str, Any])
async def get_live_signals(account_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/signals/{account_id}",
        account_id=account_id,
        limit=limit,
    )
    return await _live_signals_payload(account_id, limit=limit)


@app.get("/api/live/stats/{account_id}", response_model=dict[str, Any])
async def get_live_stats(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/stats/{account_id}",
        account_id=account_id,
    )
    return await asyncio.to_thread(_live_account_stats_payload, account_id)


def _fallback_live_quote_payload(symbol: str, *, limit: int = 500) -> dict[str, Any] | None:
    normalized_symbol = _normalize_symbol_key(symbol)
    raw_symbol = str(symbol or "").strip().upper()
    symbol_candidates = [candidate for candidate in [normalized_symbol, raw_symbol] if candidate]
    if not symbol_candidates:
        return None

    timeframe_candidates = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    for candidate_symbol in symbol_candidates:
        for timeframe in timeframe_candidates:
            df = legacy_viewer._load_ohlcv_df(candidate_symbol, timeframe)
            if df is None or len(df) == 0:
                continue
            bars_df = df.tail(max(1, min(int(limit or 500), 2000)))
            bars: list[dict[str, Any]] = []
            for row in bars_df.itertuples():
                timestamp = pd.to_datetime(getattr(row, "timestamp", None), utc=True, errors="coerce")
                if pd.isna(timestamp):
                    continue
                bars.append(
                    {
                        "symbol": candidate_symbol,
                        "timestamp": pd.Timestamp(timestamp).isoformat(),
                        "time": float(pd.Timestamp(timestamp).timestamp()),
                        "open": _safe_float(getattr(row, "open", None), 0.0),
                        "high": _safe_float(getattr(row, "high", None), 0.0),
                        "low": _safe_float(getattr(row, "low", None), 0.0),
                        "close": _safe_float(getattr(row, "close", None), 0.0),
                        "volume": _safe_float(getattr(row, "volume", None), 0.0),
                    }
                )
            if bars:
                return {"symbol": candidate_symbol, "latest": bars[-1], "bars": bars}
    return None


@app.get("/api/live/quotes/{symbol}", response_model=dict[str, Any])
async def get_live_quotes(symbol: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/quotes/{symbol}",
        symbol=symbol,
    )
    payload = await _live_market_data_service.get_quote_payload(symbol)
    if not payload.get("latest"):
        fallback_payload = await asyncio.to_thread(_fallback_live_quote_payload, symbol, limit=500)
        if fallback_payload is None:
            raise HTTPException(status_code=404, detail=f"No live quote bars available for {symbol.upper()}")
        return fallback_payload
    return payload


@app.get("/api/live/positions/{account_id}", response_model=dict[str, Any])
async def get_live_positions(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/positions/{account_id}",
        account_id=account_id,
    )
    return await _live_positions_payload(account_id)


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
        normalized_symbols = ",".join(symbol_list)
        normalized_timeframes = ",".join(timeframe_list)
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.jforex_dl",
            "--symbols",
            normalized_symbols,
            "--timeframes",
            normalized_timeframes,
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

    if source in {"yfinance", "huggingface", "alpaca"}:
        module_name = {
            "yfinance": "data_tools.downloaders.yfinance_dl",
            "huggingface": "data_tools.downloaders.huggingface_dl",
            "alpaca": "data_tools.downloaders.alpaca_dl",
        }[source]
        cmd = [
            sys.executable,
            "-u",
            "-m",
            module_name,
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
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

    provider = str(payload.provider or "").strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    if provider not in AGENT_CONVERT_PROVIDER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported provider '{provider}'")

    model = str(payload.model or "").strip() or None
    if provider == "nim":
        model = model or DEFAULT_MODEL_NAME
        if model not in SUPPORTED_MODEL_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported model '{model}' for provider '{provider}'",
            )
    elif provider == "copilot_sdk":
        model = (
            model
            or str(os.getenv("COPILOT_SDK_MODEL") or "gpt-5-mini").strip()
            or "gpt-5-mini"
        )

    task_payload = {
        "code": code,
        "strategy_name": str(payload.strategy_name or "").strip() or None,
        "engine": str(payload.engine or "").strip().lower() or None,
        "provider": provider,
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


@app.get("/api/data/check", response_model=dict[str, Any])
async def data_check(
    symbol: str = Query(default=""),
    timeframe: str = Query(default="1d"),
    start: str = Query(default=""),
    end: str = Query(default=""),
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not normalized_timeframe:
        raise HTTPException(status_code=400, detail="timeframe is required")

    result = _data_check_result(
        symbol=normalized_symbol,
        timeframe=normalized_timeframe,
        start=start,
        end=end,
    )
    return {
        "available": bool(result.get("available")),
        "missing_range": result.get("missing_range"),
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
    await asyncio.to_thread(_hydrate_task_cache_once)

    with DATA_TASKS_LOCK:
        snapshots = [(task_id, dict(task)) for task_id, task in DATA_TASKS.items()]
    payload: list[dict[str, Any]] = []
    for task_id, task in snapshots:
        status = str(task.get("status") or "").lower()
        include_log = status not in FINAL_TASK_STATUSES
        item = _serialize_task(task_id, task, include_log=include_log, max_log_bytes=64)
        item.pop("log", None)
        payload.append(item)
    payload.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        )
    )
    return {"tasks": payload}


@app.get("/api/tasks", response_model=dict[str, Any])
async def list_tasks() -> dict[str, Any]:
    return await list_data_tasks()


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
    lines: int = Query(default=200, ge=1, le=100000),
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
    timeframe = normalize_timeframe(timeframe)
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


@app.websocket("/ws/live/accounts")
async def live_accounts_socket(websocket: WebSocket) -> None:
    await _stream_live_payload(
        websocket,
        topic="live.accounts",
        fetch_payload=lambda: asyncio.to_thread(_live_accounts_stream_payload),
        poll_seconds=_LIVE_WS_ACCOUNTS_POLL_SECONDS,
    )


@app.websocket("/ws/live/{account_id}/positions")
async def live_positions_socket(websocket: WebSocket, account_id: str) -> None:
    await _stream_live_payload(
        websocket,
        topic=f"live.positions.{account_id}",
        fetch_payload=lambda: _live_positions_payload(account_id),
        poll_seconds=_LIVE_WS_POSITIONS_POLL_SECONDS,
    )


@app.websocket("/ws/live/{account_id}/signals")
async def live_signals_socket(websocket: WebSocket, account_id: str) -> None:
    await _stream_live_payload(
        websocket,
        topic=f"live.signals.{account_id}",
        fetch_payload=lambda: _live_signals_payload(account_id, limit=200),
        poll_seconds=_LIVE_WS_SIGNALS_POLL_SECONDS,
    )


@app.websocket("/ws/live/{account_id}/executions")
async def live_executions_socket(websocket: WebSocket, account_id: str) -> None:
    await _stream_live_payload(
        websocket,
        topic=f"live.executions.{account_id}",
        fetch_payload=lambda: _live_trades_payload(account_id),
        poll_seconds=_LIVE_WS_EXECUTIONS_POLL_SECONDS,
    )


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

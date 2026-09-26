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
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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
from pydantic import BaseModel, ConfigDict, Field as PydanticField, ValidationError
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sqlmodel import SQLModel
from starlette.middleware.gzip import GZipMiddleware

from data_tools.catalog_builder import merge_ohlcv_entries
from data_tools import cache_manager
from data_tools import auto_download as _auto_download_config
from data_tools.auto_download import reload_coverage_windows
from data_tools.signal_import import load_signals_from_csv
from shared.atomic_io import write_text_atomically

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

from shared.logging import get_logger, redact_text, redact_value
from shared.sentry import initialize_sentry
from shared.models import (
    BacktestResult,
    EquityPoint,
    LiveAccountConfig,
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
    get_live_deployment,
    get_service,
    get_latest_live_balance,
    get_latest_live_balances,
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
    normalize_task_status,
    save_result_to_sqlite,
    save_portfolio as save_portfolio_record,
    upsert_verification_evidence,
    upsert_background_task_record,
    list_live_deployment_submission_claims,
    LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS,
    LiveDeploymentSubmissionClaim,
    utcnow_naive,
    create_service,
    delete_service,
    record_live_account_demo_verification,
    update_live_deployment_account,
    update_live_deployment_status,
    update_service_status,
    upsert_live_accounts,
)
from runner.agent_process import build_backtest_child_env
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from risk.config import RiskConfig
from signal_bots.registry import get_signal_processor, list_signal_processor_options
from live_runner.ctrader_deployment import build_ctrader_deployment_plan
from live_runner.stockkit_routing import validate_stockkit_deployment_route
try:
    from live_runner.account_discovery import discover_configured_accounts
except ModuleNotFoundError:  # pragma: no cover - optional dependency for account discovery
    async def discover_configured_accounts() -> list[dict[str, Any]]:
        return []
from shared.models import (
    LiveDeploymentAccountUpdateRequest,
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

initialize_sentry(
    "viewer",
    integrations=[
        StarletteIntegration(),
        FastApiIntegration(),
    ],
)
logger = get_logger("viewer")
AGENT_CONVERT_PROVIDER_NAMES: tuple[str, ...] = ("nim", "copilot_sdk")

BACKTEST_TRADES_SUFFIX = ".trades.json"
OHLCV_PARQUET_SUFFIX = ".parquet"

BACKTESTS_DIR = DATA_DIR / "backtests"
MATRIX_RESULTS_DIR = DATA_DIR / "backtests" / "matrix_runner"
MATRIX_GRID_OVERVIEW_FILE = "matrix-grid-overview.json"
MATRIX_GRID_OVERVIEW_PATH = PROJECT_ROOT / "frontend" / "public" / MATRIX_GRID_OVERVIEW_FILE
LIVE_DEPLOYMENTS_PATH = "/api/live/deployments"
TASK_NOT_FOUND_DETAIL = "Task not found"
PARQUET_SUFFIX = ".parquet"
NOT_FOUND_DETAIL = "Not found"
SAVED_TO_PREFIX = "saved to:"
SAVING_RESULTS_MESSAGE = "Saving results..."
SERVICE_NOT_FOUND_DETAIL = "Service not found"
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
BACKTEST_WATCHDOG_TIMEOUT_SECONDS = float(os.environ.get("BACKTEST_TIMEOUT_SECONDS", "300"))
BACKTEST_WATCHDOG_POLL_SECONDS = 2.0
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SINGLEFLIGHT_PERSISTED_KEYS = (
    "download_request_key",
    "owner_pid",
    "owner_thread_id",
    "owner_token",
    "singleflight_owner_boot_id",
    "singleflight_heartbeat_at",
    "singleflight_lease_until",
    "singleflight_lease_expires_at",
)
_TASK_LOG_WS_CHUNK_BYTES = 64 * 1024
_TASK_LOG_WS_MAX_BYTES = 2 * 1024 * 1024
_TASK_LOG_RESPONSE_MAX_LINES = 2000


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the child and everything it spawned.

    The runner is started with `start_new_session=True`, so it leads its own
    process group. A bare `proc.kill()` (SIGKILL to the direct child only)
    leaves grandchildren alive holding the inherited stdout pipe, which would
    keep the pump loop hanging and strand the task anyway.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # ProcessLookupError (child gone) and PermissionError both derive
        # from OSError, which covers every killpg precondition failure.
        pgid = None
    if pgid is not None and pgid == proc.pid:
        # Only signal the group when the child actually leads it (it was
        # started with start_new_session=True). Otherwise the pgid is OUR
        # group and killpg would kill this process and its siblings.
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _monitor_backtest_process(
    proc: subprocess.Popen,
    *,
    timeout_seconds: float = BACKTEST_WATCHDOG_TIMEOUT_SECONDS,
    poll_interval: float = BACKTEST_WATCHDOG_POLL_SECONDS,
    timed_out: threading.Event | None = None,
) -> bool:
    """Bound the whole child process lifetime.

    Runs alongside the stdout pump; unlike a `proc.wait(timeout=...)` after
    EOF, this watchdog fires even when a hung child never closes its stdout.
    Kills the child's whole process group when the deadline expires. Returns
    True when the child exited inside the budget, False when it was killed.
    When `timed_out` is given, it is set before the kill so the caller's
    failure path can report the watchdog timeout instead of the kill's
    negative exit code.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if proc.poll() is not None:
            return True
        if time.monotonic() >= deadline:
            logger.warning(
                "Backtest watchdog: child pid %s exceeded %.0fs; terminating",
                proc.pid,
                timeout_seconds,
            )
            if timed_out is not None:
                timed_out.set()
            _kill_process_group(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return False
        time.sleep(max(0.01, float(poll_interval)))


DATA_TASKS = legacy_viewer.DATA_TASKS
DATA_TASKS_LOCK = legacy_viewer.DATA_TASKS_LOCK
ACTIVE_TASK_STATUSES = legacy_viewer.ACTIVE_TASK_STATUSES
FINAL_TASK_STATUSES = legacy_viewer.FINAL_TASK_STATUSES
# A task can only be paused while it is actually pending or executing. This must
# stay in lockstep with `viewer.core._task_controls_payload`'s `can_pause` set:
# the flag is the contract the UI renders from, so the endpoint accepting a
# status the flag hides leaves the operator unable to act, and accepting a status
# the flag offers but should not is a silent no-op reported as success.
PAUSABLE_TASK_STATUSES = frozenset({"running", "starting"})
_running_jobs = legacy_viewer._running_jobs
_JOBS_LOCK = legacy_viewer._JOBS_LOCK
_JOBS_TIMESTAMP = legacy_viewer._JOBS_TIMESTAMP


def _set_job_status(job_id: str, status: str) -> None:
    """Single lock-guarded status write (no-op for an unregistered job)."""
    legacy_viewer.set_job_status(job_id, status)


def _finish_job(
    job_id: str,
    *,
    status: str,
    result: Any | None,
    error: str | None,
) -> None:
    """Finalize status/result/error in one lock-guarded write on core's helper."""
    legacy_viewer.finish_job(job_id, status=status, result=result, error=error)


def _register_job(job_id: str, job: dict[str, Any]) -> None:
    """Register a job payload under the shared lock."""
    with _JOBS_LOCK:
        _running_jobs[job_id] = job
        _JOBS_TIMESTAMP[job_id] = time.monotonic()


_refresh_catalog = legacy_viewer._refresh_catalog
_LEGACY_START_DATA_TASK = legacy_viewer._start_data_task
_LEGACY_START_DUKASCOPY_TASK = legacy_viewer._start_dukascopy_task
_LEGACY_START_JFOREX_TASK = legacy_viewer._start_jforex_task
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


def _run_coroutine_sync(coroutine_factory: Callable[[], Any]) -> Any:
    """Drive an async helper from sync code without `asyncio.run()`.

    The coroutine runs on its own worker thread with a private loop, so this
    stays safe whether the caller is a worker thread or the event loop itself
    (`asyncio.run()` would raise `RuntimeError` in the latter case and each
    call would otherwise spin a throwaway loop).
    """
    result: dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result["payload"] = asyncio.run(coroutine_factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised verbatim on the joining thread
            result["error"] = exc
            raise
        finally:
            done.set()

    thread = threading.Thread(target=_runner, daemon=True, name="coro-sync-dispatch")
    thread.start()
    done.wait()
    if "error" in result:
        raise result["error"]
    return result.get("payload")


def _run_backtest_sync(runner: Callable[[Any], Any], payload: Any) -> Any:
    """Invoke the async `run_backtest` route logic from sync helpers."""
    async def _invoke() -> Any:
        return await runner(payload)

    return _run_coroutine_sync(lambda: _invoke())


_RECOVERY_SPEC_VERSION = 1
_RECOVERY_BACKTEST_ENGINES = {"native", "vectorbt", "lean", "pynecore"}
_RECOVERY_DOWNLOAD_SOURCES = {"yfinance", "huggingface", "alpaca", "dolthub", "dukascopy", "jforex"}
_RECOVERY_OPERATIONS = {"download", "resample", "harvest", "validate", "calibrate"}
_RECOVERY_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,63}$")
_RECOVERY_TIMEFRAME_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_RECOVERY_EXECUTABLE_KEYS = {
    "argv",
    "cmd",
    "command",
    "commandline",
    "entrypoint",
    "executable",
    "shell",
}


def _is_executable_recovery_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _RECOVERY_EXECUTABLE_KEYS


def _contains_executable_command(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_executable_recovery_key(key) or _contains_executable_command(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_executable_command(child) for child in value)
    return False


def _strip_executable_command_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_executable_command_fields(child)
            for key, child in value.items()
            if not _is_executable_recovery_key(key)
        }
    if isinstance(value, list):
        return [_strip_executable_command_fields(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_strip_executable_command_fields(child) for child in value)
    return value


class _BacktestRecoveryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    engine: str = "native"
    symbols: list[str]
    timeframe: str = "1d"
    data_source: str = ""
    start: str = ""
    end: str = ""
    capital: float = 100000.0
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_leverage: float | None = None
    modeling_resolution: str | None = None
    intrabar_resolution: str | None = None
    parameters: dict[str, Any] = PydanticField(default_factory=dict)


class _BacktestRecoverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    kind: Literal["backtest"]
    operation: Literal["run"]
    parameters: _BacktestRecoveryParameters


class _DataRecoverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    kind: Literal["data"]
    operation: Literal["download", "resample", "harvest", "validate", "calibrate"]
    source: str | None = None
    symbols: list[str] | str | None = None
    timeframes: list[str] | str | None = None
    target_timeframes: list[str] | str | None = None
    symbol: str | None = None
    start: str | None = None
    end: str | None = None
    offer_side: str | None = None
    price_type: str | None = None
    merge_tick: bool = False
    merge_s1: bool = False
    batch_size: int | None = None
    pause_ms: int | None = None
    query_timeout_seconds: int | None = None
    source_path: str | None = None
    asset_class: str | None = None
    report: str | None = None
    start_year: int | None = None
    min_year: int | None = None
    force: bool = False
    verbose: bool = False
    parameters: dict[str, Any] = PydanticField(default_factory=dict)


def _parse_recovery_spec(
    raw_config: dict[str, Any], expected_kind: Literal["backtest", "data"]
) -> _BacktestRecoverySpec | _DataRecoverySpec | None:
    if not isinstance(raw_config, dict):
        return None
    candidate = raw_config.get("task_spec")
    if not isinstance(candidate, dict):
        return None
    if _contains_executable_command(candidate):
        return None
    kind = str(candidate.get("kind") or "").strip().lower()
    if kind != expected_kind:
        return None
    if kind == "data":
        model_candidate = dict(candidate)
        nested = model_candidate.pop("parameters", None)
        if isinstance(nested, dict):
            for key, value in nested.items():
                model_candidate.setdefault(key, value)
        try:
            return _DataRecoverySpec.model_validate(model_candidate)
        except ValidationError:
            return None
    try:
        return _BacktestRecoverySpec.model_validate(candidate)
    except ValidationError:
        return None


def _recovery_spec(raw_config: dict[str, Any], kind: str) -> dict[str, Any] | None:
    if kind not in {"backtest", "data"}:
        return None
    spec = _parse_recovery_spec(raw_config, kind)
    return spec.model_dump() if spec is not None else None


def _recovery_backtest_spec(raw_config: dict[str, Any]) -> dict[str, Any] | None:
    spec = _parse_recovery_spec(raw_config, "backtest")
    return spec.model_dump() if spec is not None else None


def _recovery_list(payload: dict[str, Any], key: str, *, required: bool) -> list[str]:
    raw = payload.get(key)
    if raw is None and not required:
        return []
    if isinstance(raw, str):
        raw = [item for item in raw.split(",") if item.strip()]
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    values: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if not value or value in values:
            continue
        values.append(value)
    if required and not values:
        raise ValueError(f"{key} is required")
    if len(values) > 256:
        raise ValueError(f"{key} has too many values")
    return values


def _recovery_symbols(payload: dict[str, Any], *, required: bool) -> list[str]:
    values = _recovery_list(payload, "symbols", required=required)
    for value in values:
        if _RECOVERY_SYMBOL_RE.fullmatch(value) is None:
            raise ValueError("invalid recovery symbol")
    return [value.upper() for value in values]


def _recovery_timeframes(payload: dict[str, Any], *, required: bool) -> list[str]:
    values = _recovery_list(payload, "timeframes", required=required)
    for value in values:
        if _RECOVERY_TIMEFRAME_RE.fullmatch(value) is None:
            raise ValueError("invalid recovery timeframe")
    return [value.lower() for value in values]


def _recovery_date(payload: dict[str, Any], key: str) -> str | None:
    value = str(payload.get(key) or "").strip()
    if not value:
        return None
    if len(value) > 64 or pd.isna(pd.to_datetime(value, utc=True, errors="coerce")):
        raise ValueError(f"invalid recovery {key}")
    return value


def _recovery_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"invalid recovery {key}")
    return value


def _recovery_int(payload: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = payload.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError(f"invalid recovery {key}")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid recovery {key}") from exc
    if normalized < low or normalized > high:
        raise ValueError(f"recovery {key} is out of range")
    return normalized


def _backtest_request_from_recovery_spec(
    spec: _BacktestRecoverySpec | dict[str, Any],
) -> BacktestRunRequest:
    if isinstance(spec, BaseModel):
        spec = spec.model_dump()
    nested_parameters = spec.get("parameters")
    if isinstance(nested_parameters, dict) and "strategy" in nested_parameters:
        payload = nested_parameters
    else:
        payload = spec
    strategy = str(payload.get("strategy") or "").strip()
    engine = str(payload.get("engine") or "native").strip().lower()
    symbols = _recovery_symbols(payload, required=True)
    timeframe = str(payload.get("timeframe") or "1d").strip().lower()
    if not strategy or engine not in _RECOVERY_BACKTEST_ENGINES:
        raise ValueError("invalid recovery backtest configuration")
    if _RECOVERY_TIMEFRAME_RE.fullmatch(timeframe) is None:
        raise ValueError("invalid recovery timeframe")
    capital = _safe_float(payload.get("capital"), 100000.0)
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("invalid recovery capital")
    parameters = payload.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("invalid recovery parameters")
    return BacktestRunRequest(
        strategy=strategy,
        engine=engine,
        symbols=symbols,
        timeframe=timeframe,
        data_source=str(payload.get("data_source") or ""),
        start=str(payload.get("start") or ""),
        end=str(payload.get("end") or ""),
        capital=capital,
        risk_per_trade_pct=payload.get("risk_per_trade_pct"),
        max_position_pct=payload.get("max_position_pct"),
        max_leverage=payload.get("max_leverage"),
        modeling_resolution=(
            str(payload["modeling_resolution"])
            if payload.get("modeling_resolution") is not None
            else None
        ),
        intrabar_resolution=(
            str(payload["intrabar_resolution"])
            if payload.get("intrabar_resolution") is not None
            else None
        ),
        parameters=dict(parameters or {}),
    )


def _download_request_from_recovery_spec(
    spec: _DataRecoverySpec | dict[str, Any],
) -> _PreparedDownloadRequest:
    if isinstance(spec, BaseModel):
        spec = spec.model_dump()
    source = str(spec.get("source") or "").strip().lower()
    if source not in _RECOVERY_DOWNLOAD_SOURCES:
        raise ValueError("unsupported recovery download source")
    symbols = _recovery_symbols(spec, required=source != "dolthub")
    timeframes = _recovery_timeframes(spec, required=source != "dolthub")
    offer_side = str(spec.get("offer_side") or spec.get("price_type") or "bid").strip().lower()
    if offer_side not in {"bid", "ask", "both"}:
        raise ValueError("invalid recovery offer side")
    request = _PreparedDownloadRequest(
        source=source,
        symbols=",".join(symbols),
        timeframes=",".join(timeframes),
        start=_recovery_date(spec, "start") or "",
        end=_recovery_date(spec, "end") or "",
        offer_side=offer_side,
        merge_tick=_recovery_bool(spec, "merge_tick"),
        merge_s1=_recovery_bool(spec, "merge_s1"),
        batch_size=_recovery_int(spec, "batch_size", 10, 1, 50),
        pause_ms=_recovery_int(spec, "pause_ms", 1000, 0, 60000),
        query_timeout_seconds=_recovery_int(
            spec,
            "query_timeout_seconds",
            900,
            30,
            int(legacy_viewer.MAX_DATA_QUERY_TIMEOUT_SECONDS),
        ),
        symbol_list=symbols,
        timeframe_list=timeframes,
    )
    _validate_download_limits(request)
    return request


def _mark_task_manual_required(
    record: dict[str, Any], task_id: str, raw_config: dict[str, Any], reason: str
) -> None:
    config = dict(raw_config)
    for key in _RECOVERY_EXECUTABLE_KEYS:
        config.pop(key, None)
    config.pop("restart_config", None)
    config["recovery_rejected"] = redact_text(str(reason))
    config["recovery_mode"] = "manual"
    config = _strip_executable_command_fields(config)
    task = None
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is not None:
            was_failed = str(task.get("status") or "").lower() == "failed"
            for key in _RECOVERY_EXECUTABLE_KEYS:
                task.pop(key, None)
            task.pop("restart_config", None)
            task["status"] = "manual_required"
            task["error"] = config["recovery_rejected"]
            task["recovery_rejected"] = config["recovery_rejected"]
            task["recovery_mode"] = "manual"
            if was_failed:
                progress_detail = dict(task.get("progress_detail") or {})
                progress_detail["failed"] = max(1, int(progress_detail.get("failed", 0) or 0))
                progress_detail["percent"] = 100.0
                progress_detail["current"] = "Manual recovery required"
                task["progress_detail"] = progress_detail
                task["progress"] = 100.0
            task["finished_at"] = _now_iso()
            task["updated_at"] = task["finished_at"]
            task_snapshot = dict(task)
        else:
            task_snapshot = None
    if task_snapshot is not None:
        _persist_task_snapshot(task_id, task_snapshot)
        config["progress_detail"] = _json_safe(task_snapshot.get("progress_detail"))
        config["progress_percent"] = float(task_snapshot.get("progress", 100.0) or 100.0)
        config["finished_at"] = task_snapshot.get("finished_at")
        config["error"] = config["recovery_rejected"]
    upsert_background_task_record(
        task_id=task_id,
        task_type=str(record.get("type") or "data"),
        status="manual_required",
        config_json=config,
        progress_message="Manual recovery required; typed operation was not executed",
        name=str(record.get("name") or ""),
        description=str(record.get("description") or ""),
        source=str(record.get("source") or ""),
        created_at=str(record.get("created_at") or ""),
        updated_at=_now_iso(),
    )


def _queue_unrecoverable_task(
    record: dict[str, Any], task_id: str, raw_config: dict[str, Any], reason: str
) -> None:
    _mark_task_manual_required(record, task_id, raw_config, reason)


def _dispatch_typed_data_recovery(
    record: dict[str, Any], task_id: str, raw_config: dict[str, Any], spec: _DataRecoverySpec
) -> bool:
    payload = spec.model_dump()
    operation = spec.operation
    if operation not in {"download", "resample"}:
        _mark_task_manual_required(
            record,
            task_id,
            raw_config,
            f"typed data operation '{operation}' is unsupported for automatic recovery",
        )
        return False
    try:
        if operation == "download":
            request = _download_request_from_recovery_spec(spec)
            name = str(record.get("name") or "Recovered data task")
            description = str(record.get("description") or "")
            if request.source in {"dukascopy", "jforex"}:
                _start_jforex_download(task_id, request, name=name, description=description)
            else:
                _start_generic_download(task_id, request, name=name, description=description)
            return True
        source_path = _contained_client_data_file(
            str(payload.get("source_path") or ""), allowed_suffix=OHLCV_PARQUET_SUFFIX
        )
        target_timeframes = _recovery_timeframes(
            {"timeframes": payload.get("target_timeframes")}, required=True
        )
        if source_path is None or not target_timeframes:
            raise ValueError("invalid recovery resample input")
        command = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.resample",
            "--source",
            str(source_path),
            "--timeframes",
            ",".join(target_timeframes),
        ]
        _start_data_task(
            task_id,
            command,
            total_items=1,
            name=str(record.get("name") or "Recovered resample task"),
            description=str(record.get("description") or ""),
            source="resample",
        )
        _apply_recovery_data_metadata(
            task_id,
            command=command,
            downloader="resample",
            task_spec=payload,
        )
        return True
    except Exception as exc:
        _mark_task_manual_required(record, task_id, raw_config, str(exc))
        return False


def _resume_interrupted_tasks() -> None:
    if not _LEGACY_HOOKS_INSTALLED:
        raise RuntimeError(
            "install_legacy_task_hooks() must run before _resume_interrupted_tasks: "
            "the resume path writes task records through the persistence-aware "
            "implementations, which only exist once the hooks are installed."
        )
    records = list_background_task_records(statuses=["interrupted", "queued"])
    for record in records:
        try:
            task_id = str(record.get("id") or "").strip()
            if not task_id or _TASK_ID_RE.fullmatch(task_id) is None:
                continue
            task_type = str(record.get("type") or "").strip().lower()
            raw_config = dict(record.get("config_json") or {})
            if task_type == "backtest":
                _resume_interrupted_backtest_task(record, task_id, raw_config)
            elif task_type == "data":
                _resume_interrupted_data_task(record, task_id, raw_config)
        except Exception as exc:
            logger.warning("Failed to recover interrupted task %s: %s", record.get("id"), exc)


def _resume_interrupted_backtest_task(
    record: dict[str, Any], task_id: str, raw_config: dict[str, Any]
) -> None:
    spec = _parse_recovery_spec(raw_config, "backtest")
    if spec is None:
        _mark_task_manual_required(record, task_id, raw_config, "missing typed backtest spec")
        return
    try:
        request_payload = _backtest_request_from_recovery_spec(spec)
    except (TypeError, ValueError) as exc:
        _mark_task_manual_required(record, task_id, raw_config, str(exc))
        return
    logger.info(
        "Auto-restarting backtest task %s (%s %s %s)",
        task_id,
        request_payload.strategy,
        ",".join(request_payload.symbols),
        request_payload.timeframe,
    )
    try:
        restarted = _run_backtest_sync(run_backtest, request_payload)
    except Exception as exc:
        _mark_task_manual_required(
            record,
            task_id,
            raw_config,
            f"backtest recovery failed: {exc}",
        )
        return
    restarted_task_id = restarted.get("task_id") if isinstance(restarted, dict) else None
    upsert_background_task_record(
        task_id=task_id,
        task_type=str(record.get("type") or "backtest"),
        status="completed",
        config_json=_strip_executable_command_fields(raw_config),
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


def _resume_interrupted_data_task(
    record: dict[str, Any], task_id: str, raw_config: dict[str, Any]
) -> None:
    spec = _parse_recovery_spec(raw_config, "data")
    if spec is None:
        _mark_task_manual_required(record, task_id, raw_config, "missing typed data spec")
        return
    _dispatch_typed_data_recovery(record, task_id, raw_config, spec)


def _resume_interrupted_downloader_task(
    *,
    record: dict[str, Any],
    task_id: str,
    raw_config: dict[str, Any],
    downloader: str,
    symbols: list[str],
    timeframes: list[str],
    total_items: int,
    name: str,
    description: str,
    start: str | None,
    end: str | None,
) -> None:
    spec = _parse_recovery_spec(raw_config, "data")
    if spec is None or spec.operation != "download":
        _mark_task_manual_required(record, task_id, raw_config, "missing typed downloader spec")
        return
    requested_source = str(spec.source or "").strip().lower()
    if requested_source not in {"dukascopy", "jforex"}:
        _mark_task_manual_required(
            record, task_id, raw_config, "downloader source is not allowlisted"
        )
        return
    if requested_source != downloader:
        _mark_task_manual_required(record, task_id, raw_config, "downloader source mismatch")
        return
    if not _dispatch_typed_data_recovery(record, task_id, raw_config, spec):
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(_recover_maintenance_deletes)
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
    await asyncio.to_thread(
        mark_background_tasks_interrupted,
        from_statuses=["queued", "starting", "running", "paused"],
    )
    # The hooks swap the persistence-aware task implementations onto the legacy
    # module; they MUST be installed before any helper that creates or updates
    # task records (resume below), or those writes land on the in-memory
    # originals and never persist.
    install_legacy_task_hooks()
    await asyncio.to_thread(_resume_interrupted_tasks)
    await asyncio.to_thread(legacy_viewer.evict_jobs)
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
# The viewer serves its own SPA from the same origin (and the Vite dev server
# proxies /api), so no cross-origin browser access is needed. A wildcard
# origin combined with allow_credentials=True would reflect any Origin header
# and made every mutating endpoint drive-by reachable; credentials are now
# explicitly disabled and no wildcard origin is advertised.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
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


class CTraderTopDeployRequest(SQLModel):
    limit: int = 5
    activate: bool = False
    dry_run: bool = True


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
    data_source: str = ""
    start: str = ""
    end: str = ""
    capital: float = 100000.0
    risk_per_trade_pct: float | None = PydanticField(default=None, ge=0.0, lt=1.0)
    max_position_pct: float | None = PydanticField(default=None, gt=0.0, le=1.0)
    max_leverage: float | None = PydanticField(default=None, gt=0.0)
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
    items: list[dict[str, Any]] = PydanticField(default_factory=list)


class PortfoliosDuplicatesResponse(SQLModel):
    groups: list[dict[str, Any]]
    total_groups: int
    total_portfolios: int


class PortfoliosDeduplicateResponse(SQLModel):
    deleted_ids: list[str]
    kept_ids: list[str]
    groups_affected: int
    items: list[dict[str, Any]] = PydanticField(default_factory=list)


class BacktestsBulkDeleteRequest(SQLModel):
    ids: list[str]


class PortfoliosBulkDeleteRequest(SQLModel):
    ids: list[str]


_SAFE_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_asset_id(raw_id: object) -> str:
    """Normalize a client-supplied asset id for file-backed operations.

    Returns an empty string for ids that are blank or that could participate
    in a path traversal (path separators, ``..`` segments). Ids are only ever
    joined onto a fixed directory below, and the join is containment-checked
    again by :func:`_contained_asset_path`.
    """
    normalized = str(raw_id or "").strip()
    if not normalized:
        return ""
    if normalized in {".", ".."}:
        return ""
    if not _SAFE_ASSET_ID_RE.match(normalized):
        return ""
    return normalized


def _contained_asset_path(base_dir: Path, asset_id: str, suffix: str) -> Path | None:
    """Join ``asset_id`` onto ``base_dir`` if the result stays inside it.

    The resolved candidate must be contained in the resolved base directory,
    otherwise ``None`` is returned so the caller never builds an unlink path
    from raw input. See :func:`_contained_client_data_file` for the
    client-path variant of the same containment contract.
    """
    normalized = _safe_asset_id(asset_id)
    if not normalized:
        return None
    base = base_dir.resolve()
    raw_candidate = base_dir / f"{normalized}{suffix}"
    if raw_candidate.is_symlink():
        return None
    candidate = raw_candidate.resolve()
    if candidate.parent != base:
        return None
    return candidate


def _contained_client_data_file(
    raw_path: str, *, allowed_suffix: str, base_dir: Path | None = None
) -> Path | None:
    """Validate a client-supplied data file path for reading or subprocess use.

    The resolved path must stay inside the client data tree (``DATA_DIR`` by
    default — signal CSVs live under ``DATA_DIR / "signals"`` and OHLCV
    parquet under ``DATA_DIR / "ohlcv"``) and must carry the expected suffix,
    so requests can neither read arbitrary files nor point a subprocess at
    code outside the data directory. ``base_dir`` narrows the tree for
    callers that manage a single subtree.
    """
    normalized = str(raw_path or "").strip()
    if not normalized:
        return None
    base = (base_dir or DATA_DIR).resolve()
    try:
        candidate = Path(normalized).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(base):
        return None
    if candidate.suffix.lower() != allowed_suffix.lower():
        return None
    return candidate


_QUIET_EXACT_PATHS = {
    "/api/tasks",
    "/api/data/tasks",
    "/api/live/accounts",
    LIVE_DEPLOYMENTS_PATH,
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
            # C1: quotes resolve credentials from the same file-first source
            # as execution, never bare process env.
            from live_runner.credential_env import load_accounts_env_values
            from live_runner.service_discovery import DEFAULT_ACCOUNTS_ENV

            env_values = await asyncio.to_thread(
                load_accounts_env_values, DEFAULT_ACCOUNTS_ENV
            )
            await _live_market_data_service.refresh_from_deployments(
                deployments, account_lookup, env_values=env_values
            )
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
    error_id = uuid.uuid4().hex
    logger.exception(
        "Unhandled server exception",
        extra={
            "data": {
                "method": request.method,
                "path": request.url.path,
                "error_id": error_id,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error (error_id={error_id})"},
    )


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


def _validated_task_id(task_id: str) -> str:
    normalized = str(task_id or "").strip()
    if _TASK_ID_RE.fullmatch(normalized) is None:
        raise HTTPException(status_code=400, detail="Invalid task id")
    return normalized


def _validated_task_log_path(task_id: str) -> Path:
    normalized = _validated_task_id(task_id)
    if TASK_LOGS_DIR.is_symlink():
        raise HTTPException(status_code=400, detail="Invalid task log path")
    root = TASK_LOGS_DIR.resolve()
    raw_candidate = root / f"task_{normalized}.log"
    if raw_candidate.is_symlink():
        raise HTTPException(status_code=400, detail="Invalid task log path")
    candidate = raw_candidate.resolve()
    if candidate.parent != root or candidate.is_symlink():
        raise HTTPException(status_code=400, detail="Invalid task log path")
    return candidate


def _task_for_log_access(task_id: str) -> tuple[str, dict[str, Any], Path]:
    normalized = _validated_task_id(task_id)
    record = get_background_task_record(normalized)
    if record is None or str(record.get("id") or "") != normalized:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    task = _get_task(normalized)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    log_file = _validated_task_log_path(normalized)
    configured_log = task.get("log_file")
    if configured_log:
        try:
            configured_raw = Path(str(configured_log))
            configured_path = configured_raw.resolve()
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid task log path") from exc
        if (
            configured_raw.is_symlink()
            or configured_path != log_file
            or configured_path.is_symlink()
        ):
            raise HTTPException(status_code=400, detail="Invalid task log path")
    return normalized, task, log_file


def _task_status(task_id: str) -> str | None:
    normalized = str(task_id or "").strip()
    if _TASK_ID_RE.fullmatch(normalized) is None:
        return None
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(normalized)
        return _task_status_or_manual(task.get("status")) if task else None


def _task_action_payload(task_id: str) -> dict[str, Any]:
    task = _snapshot_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    return _serialize_task(task_id, task, include_log=True)


def _restart_interrupted_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    current_status = _task_status_or_manual(task.get("status"))
    if current_status not in {"interrupted", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="Only interrupted or failed tasks can be restarted",
        )

    record = get_background_task_record(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task persistence record not found")
    raw_config = dict(record.get("config_json") or {})
    task_type = str(record.get("type") or "").strip().lower()

    if task_type == "backtest":
        spec = _parse_recovery_spec(raw_config, "backtest")
        if spec is None:
            _mark_task_manual_required(
                record, task_id, raw_config, "missing typed backtest spec"
            )
            return _task_action_payload(task_id)
        try:
            request_payload = _backtest_request_from_recovery_spec(spec)
        except (HTTPException, TypeError, ValueError) as exc:
            _mark_task_manual_required(record, task_id, raw_config, str(exc))
            return _task_action_payload(task_id)
        try:
            restarted = _run_backtest_sync(run_backtest, request_payload)
        except Exception as exc:
            _mark_task_manual_required(
                record,
                task_id,
                raw_config,
                f"backtest restart failed: {exc}",
            )
            return _task_action_payload(task_id)
        completed_config = _strip_executable_command_fields(raw_config)
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
            config_json=completed_config,
            progress_message="Restarted as new task",
            name=str(record.get("name") or ""),
            description=str(record.get("description") or ""),
            source=str(record.get("source") or ""),
            created_at=str(record.get("created_at") or ""),
            updated_at=_now_iso(),
        )
        payload = _task_action_payload(task_id)
        payload["restarted_task_id"] = (
            restarted.get("task_id") if isinstance(restarted, dict) else None
        )
        return payload

    if task_type == "data":
        spec = _parse_recovery_spec(raw_config, "data")
        if spec is None:
            _mark_task_manual_required(record, task_id, raw_config, "missing typed data spec")
            return _task_action_payload(task_id)
        if not _dispatch_typed_data_recovery(record, task_id, raw_config, spec):
            return _task_action_payload(task_id)
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
# Live streams refresh every few seconds (not 1 Hz): each poll fans out DB
# and broker requests, and the previous 1 Hz default rate-limited brokers.
_LIVE_WS_ACCOUNTS_POLL_SECONDS = 5.0
_LIVE_WS_SIGNALS_POLL_SECONDS = 3.0
_LIVE_WS_EXECUTIONS_POLL_SECONDS = 3.0
_LIVE_WS_POSITIONS_POLL_SECONDS = 3.0
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
    "paused": 50.0,
    "completed": 100.0,
    "failed": 100.0,
    "cancelled": 100.0,
    "interrupted": 100.0,
    "partial": 100.0,
    "skipped": 100.0,
    "manual_required": 100.0,
    "cleanup_pending": 100.0,
    "cleanup_exhausted": 100.0,
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


# Residual position size below this threshold is treated as flat. Broker
# fills and aggregated quantities are floats; exact-zero equality (S1244)
# leaves dust positions open and pollutes the average price of the next fill.
# 1e-6 also absorbs the float error of closing fills that overshoot by dust.
LIVE_POSITION_FLAT_THRESHOLD = 1e-6


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


def _task_status_or_manual(value: object) -> str:
    try:
        return normalize_task_status(value)
    except ValueError:
        return "manual_required"


def _task_status_weight(value: object) -> float:
    return _TASK_STATUS_WEIGHTS.get(_task_status_or_manual(value), 100.0)


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
    status = _task_status_or_manual(status)
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
    status = _task_status_or_manual(status)
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
        pnl_value = trade.get("net_pnl")
        if pnl_value is None:
            pnl_value = _safe_float(trade.get("pnl"), 0.0)
            pnl_value -= _safe_float(trade.get("fees"), 0.0)
        pnl = _safe_float(pnl_value, 0.0)
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
        except (OSError, TypeError, ValueError) as exc:
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


def _maintenance_journal_path(artifact_path: Path) -> Path:
    return artifact_path.with_name(f".{artifact_path.name}.delete-journal.json")


def _maintenance_staged_path(artifact_path: Path) -> Path:
    return artifact_path.with_name(f".{artifact_path.name}.delete-staged")


def _write_maintenance_journal(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomically(path, json.dumps(payload, sort_keys=True))


def _remove_maintenance_journal(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _recover_maintenance_delete_journal(
    journal_path: Path,
    *,
    kind: str,
    expected_path: Path,
    db_delete: Callable[[str], bool],
) -> bool:
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    task_id = _safe_asset_id(payload.get("asset_id"))
    if not task_id or str(payload.get("kind") or "") != kind:
        return False
    original = expected_path.resolve()
    staged = _maintenance_staged_path(original)
    if Path(str(payload.get("original") or "")).resolve() != original:
        return False
    if Path(str(payload.get("staged") or "")).resolve() != staged:
        return False
    if staged.is_symlink():
        return False
    phase = str(payload.get("phase") or "prepared")
    if phase == "rollback_failed":
        return False
    if staged.exists():
        if not staged.is_file():
            return False
        if phase != "db_deleted":
            try:
                db_delete(task_id)
            except Exception:
                return False
        try:
            _write_maintenance_journal(
                journal_path,
                {**payload, "phase": "db_deleted"},
            )
        except OSError:
            return False
        try:
            staged.unlink()
        except OSError:
            return False
        return _remove_maintenance_journal(journal_path)
    if original.exists():
        _remove_maintenance_journal(journal_path)
        return False
    if phase == "db_deleted":
        return _remove_maintenance_journal(journal_path)

    return False


def _recover_maintenance_deletes() -> None:
    targets = (
        ("backtest", BACKTESTS_DIR, BACKTEST_TRADES_SUFFIX, lambda value: delete_backtest(value)),
        (
            "portfolio",
            PORTFOLIOS_DIR,
            ".json",
            lambda value: delete_portfolio_record(value),
        ),
    )
    for kind, base_dir, suffix, db_delete in targets:
        if base_dir.is_symlink() or not base_dir.exists():
            continue
        try:
            journals = list(base_dir.glob(".*.delete-journal.json"))
        except OSError:
            continue
        for journal_path in journals:
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            asset_id = _safe_asset_id(payload.get("asset_id") if isinstance(payload, dict) else "")
            if not asset_id:
                continue
            expected_path = _contained_asset_path(base_dir, asset_id, suffix)
            if expected_path is None:
                continue
            _recover_maintenance_delete_journal(
                journal_path,
                kind=kind,
                expected_path=expected_path,
                db_delete=db_delete,
            )


def _delete_asset_with_artifacts(
    asset_id: str,
    *,
    kind: str,
    artifact_path: Path | None,
    db_delete: Callable[[str], bool],
) -> dict[str, Any]:
    normalized_id = _safe_asset_id(asset_id)
    result: dict[str, Any] = {
        "id": normalized_id,
        "status": "failed",
        "database": "not_attempted",
        "artifact": "not_attempted",
    }
    if not normalized_id or artifact_path is None:
        result["error"] = "invalid asset"
        return result
    raw_path = artifact_path
    if raw_path.is_symlink() or raw_path.parent.is_symlink():
        result["error"] = "invalid artifact path"
        return result
    if raw_path.exists() and not raw_path.is_file():
        result["artifact"] = "failed"
        result["error"] = "artifact is not a regular file"
        return result

    journal_path = _maintenance_journal_path(raw_path)
    staged_path = _maintenance_staged_path(raw_path)
    if journal_path.exists():
        recovered = _recover_maintenance_delete_journal(
            journal_path,
            kind=kind,
            expected_path=raw_path,
            db_delete=db_delete,
        )
        if (
            recovered
            and not journal_path.exists()
            and not staged_path.exists()
            and not raw_path.exists()
        ):
            result.update(
                status="deleted",
                database="recovered",
                artifact="deleted",
            )
            return result
    if staged_path.exists():
        result.update(
            status="cleanup_pending",
            database="unknown",
            artifact="staged",
            recovery_required=True,
            manual_required=True,
            error="previous delete is awaiting cleanup",
        )
        return result

    staged = False
    if raw_path.exists():
        payload = {
            "version": 1,
            "kind": kind,
            "asset_id": normalized_id,
            "original": str(raw_path.resolve()),
            "staged": str(staged_path.resolve()),
            "phase": "prepared",
        }
        try:
            _write_maintenance_journal(journal_path, payload)
            os.replace(raw_path, staged_path)
            staged = True
            _write_maintenance_journal(journal_path, {**payload, "phase": "staged"})
        except OSError as exc:
            rollback_error: OSError | None = None
            if staged and staged_path.exists() and not raw_path.exists():
                try:
                    os.replace(staged_path, raw_path)
                except OSError as restore_exc:
                    rollback_error = restore_exc
            if rollback_error is not None:
                try:
                    _write_maintenance_journal(
                        journal_path,
                        {**payload, "phase": "rollback_failed"},
                    )
                except OSError:
                    pass
                result.update(
                    status="cleanup_pending",
                    artifact="staged",
                    recovery_required=True,
                    manual_required=True,
                    journal_pending=True,
                    error=redact_text(str(rollback_error))[:300],
                )
                return result
            if not _remove_maintenance_journal(journal_path):
                result.update(
                    status="cleanup_pending",
                    artifact="restored",
                    recovery_required=True,
                    manual_required=True,
                    journal_pending=True,
                    error=redact_text(str(exc))[:300],
                )
                return result
            result["artifact"] = "failed"
            result["error"] = redact_text(str(exc))[:300]
            return result

    try:
        database_deleted = bool(db_delete(normalized_id))
    except Exception as exc:
        if staged:
            try:
                os.replace(staged_path, raw_path)
                if not _remove_maintenance_journal(journal_path):
                    result.update(
                        status="cleanup_pending",
                        artifact="restored",
                        recovery_required=True,
                        manual_required=True,
                        journal_pending=True,
                        error=redact_text(str(exc))[:300],
                    )
                else:
                    result["artifact"] = "restored"
            except OSError as restore_exc:
                try:
                    _write_maintenance_journal(
                        journal_path,
                        {**payload, "phase": "rollback_failed"},
                    )
                except OSError:
                    pass
                result.update(
                    status="cleanup_pending",
                    artifact="staged",
                    recovery_required=True,
                    manual_required=True,
                    journal_pending=True,
                    error=redact_text(str(restore_exc))[:300],
                )
        else:
            result["artifact"] = "absent"
            result["error"] = redact_text(str(exc))[:300]
        result["database"] = "failed"
        return result

    result["database"] = "deleted" if database_deleted else "absent"
    if staged:
        try:
            _write_maintenance_journal(
                journal_path,
                {
                    "version": 1,
                    "kind": kind,
                    "asset_id": normalized_id,
                    "original": str(raw_path.resolve()),
                    "staged": str(staged_path.resolve()),
                    "phase": "db_deleted",
                },
            )
        except OSError as exc:
            result.update(
                status="cleanup_pending",
                artifact="staged",
                recovery_required=True,
                manual_required=True,
                journal_pending=True,
                error=redact_text(str(exc))[:300],
            )
            return result
        try:
            staged_path.unlink()
        except OSError as exc:
            result.update(
                status="cleanup_pending",
                artifact="staged",
                recovery_required=True,
                manual_required=True,
                journal_pending=True,
                error=redact_text(str(exc))[:300],
            )
            return result
        result["artifact"] = "deleted"
        result["status"] = "deleted"
        if not _remove_maintenance_journal(journal_path):
            result.update(
                status="cleanup_pending",
                recovery_required=True,
                manual_required=True,
                journal_pending=True,
            )
        return result

    result["artifact"] = "absent"
    result["status"] = "deleted" if database_deleted else "not_found"
    return result


def _delete_portfolio_with_artifacts(portfolio_id: str) -> dict[str, Any]:
    normalized_id = _safe_asset_id(portfolio_id)
    if not normalized_id:
        return {
            "id": str(portfolio_id or ""),
            "status": "not_found",
            "database": "absent",
            "artifact": "absent",
        }
    path = _contained_asset_path(PORTFOLIOS_DIR, normalized_id, ".json")
    result = _delete_asset_with_artifacts(
        normalized_id,
        kind="portfolio",
        artifact_path=path,
        db_delete=delete_portfolio_record,
    )
    if result.get("status") in {"deleted", "cleanup_pending"}:
        _remove_portfolio_meta_cache(normalized_id)
    return result


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


def _resolve_backtest_output_id(output_path: str) -> tuple[str, str]:
    """Map a 'saved to' path to (output_path, backtest_id) for a trades result file."""
    if not output_path:
        return "", ""
    filename = Path(output_path).name
    if not filename.endswith(".trades.json"):
        return output_path, ""
    backtest_id = filename[: -len(".trades.json")]
    if backtest_id:
        loaded_result = _load_result(backtest_id)
        if loaded_result is not None:
            meta_id = str(getattr(loaded_result.meta, "id", "") or "").strip()
            if meta_id:
                backtest_id = meta_id
    return output_path, backtest_id


def _update_saved_backtest(
    output_path: str, prev_output_path: str, prev_backtest_id: str
) -> tuple[str, str]:
    """Merge a new 'saved to' path into the tracked output path / backtest id."""
    new_path, new_id = _resolve_backtest_output_id(output_path)
    return new_path or prev_output_path, new_id or prev_backtest_id


def _count_backtest_trades(backtest_id: str, latest_message: str) -> int:
    """Count trades of the finished backtest, falling back to the runner log line."""
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
    return final_trade_count


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
    total_pnl = _safe_float(meta.get("total_pnl"), 0.0)
    initial_capital = _safe_float(meta.get("initial_capital"), 0.0)
    net_profit_pct = (
        total_pnl / initial_capital * 100.0
        if initial_capital > 0
        else total_return_decimal * 100.0
    )
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
        "net_profit": round(total_pnl, 6),
        "net_profit_pct": round(net_profit_pct, 2),
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


def _delete_backtest_with_artifacts(backtest_id: str) -> dict[str, Any]:
    normalized_id = _safe_asset_id(backtest_id)
    if not normalized_id:
        return {
            "id": str(backtest_id or ""),
            "status": "not_found",
            "database": "absent",
            "artifact": "absent",
        }
    json_path = _contained_asset_path(BACKTESTS_DIR, normalized_id, BACKTEST_TRADES_SUFFIX)
    result = _delete_asset_with_artifacts(
        normalized_id,
        kind="backtest",
        artifact_path=json_path,
        db_delete=delete_backtest,
    )
    if result.get("status") in {"deleted", "cleanup_pending"}:
        _remove_backtest_meta_cache(normalized_id)
    return result


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
        "task_spec": _json_safe(task.get("task_spec")),
        "recovery_rejected": _json_safe(task.get("recovery_rejected")),
        "recovery_mode": _json_safe(task.get("recovery_mode")),
    }
    if task.get("pid") is not None:
        payload["pid"] = task.get("pid")
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
    for key in _SINGLEFLIGHT_PERSISTED_KEYS:
        if key in task and task.get(key) is not None:
            payload[key] = _json_safe(task.get(key))
    return _strip_executable_command_fields(payload)


def _persist_task_snapshot(task_id: str, task: dict[str, Any]) -> None:
    try:
        _ensure_task_runtime_metadata(task)
        status = _task_status_or_manual(task.get("status"))
        task["status"] = status
        if status in FINAL_TASK_STATUSES and not task.get("finished_at"):
            task["finished_at"] = str(task.get("updated_at") or _now_iso())
        progress_detail = dict(task.get("progress_detail") or {})
        progress_message = str(progress_detail.get("current") or "")
        upsert_background_task_record(
            task_id=task_id,
            task_type=str(task.get("kind") or "unknown"),
            status=status,
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
        task["status"] = _task_status_or_manual(record.get("status"))
    else:
        task["status"] = _task_status_or_manual(task.get("status"))

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
        float(config_payload.get("progress_percent", _task_status_weight(task.get("status")))),
    )
    progress_detail.setdefault(
        "current",
        redact_text(str(record.get("progress_message") or "")),
    )
    if progress_detail.get("current"):
        progress_detail["current"] = redact_text(str(progress_detail["current"]))
    task["progress"] = float(progress_detail.get("percent", 0.0) or 0.0)
    task["progress_detail"] = progress_detail

    supports_control = bool(config_payload.get("supports_control", task.get("supports_control", True)))
    task["supports_control"] = supports_control
    task["control_mode"] = str(config_payload.get("control_mode", task.get("control_mode", "thread")) or "thread")
    task.setdefault("pause_event", threading.Event())
    task.setdefault("stop_event", threading.Event())
    task.setdefault("log_file", str(_task_log_path(task_id)))
    if config_payload.get("task_spec") is not None:
        task["task_spec"] = config_payload.get("task_spec")
    for key in ("recovery_rejected", "recovery_mode"):
        if config_payload.get(key) is not None:
            task[key] = config_payload[key]
    for key in _SINGLEFLIGHT_PERSISTED_KEYS:
        if key in config_payload and key not in task:
            task[key] = config_payload[key]
    record_history = _sanitize_progress_history(config_payload.get("progress_history"))
    if record_history:
        task["progress_history"] = record_history
    _ensure_task_runtime_metadata(task)


def _singleflight_reservation_is_stale(
    record: dict[str, Any], config_payload: dict[str, Any]
) -> bool:
    status = _task_status_or_manual(record.get("status"))
    if status not in ACTIVE_TASK_STATUSES or not config_payload.get("download_request_key"):
        return False
    if str(config_payload.get("singleflight_owner_boot_id") or "") != str(
        getattr(legacy_viewer, "SINGLEFLIGHT_BOOT_ID", "")
    ):
        return True
    try:
        return int(config_payload.get("owner_pid")) != os.getpid()
    except (TypeError, ValueError):
        return True


def _build_task_from_record(record: dict[str, Any]) -> dict[str, Any]:
    task_id = str(record.get("id") or "").strip()
    config_payload = dict(record.get("config_json") or {})
    total_items = int(config_payload.get("total_items", 0) or 0)
    raw_status = str(record.get("status") or "").strip().lower()
    stale_singleflight = _singleflight_reservation_is_stale(record, config_payload)
    if stale_singleflight:
        raw_status = "interrupted"
    status = _task_status_or_manual(raw_status)
    status_error = None
    if stale_singleflight:
        status_error = "Singleflight reservation invalidated by server restart"
        for key in _SINGLEFLIGHT_PERSISTED_KEYS:
            config_payload.pop(key, None)
    elif status == "manual_required" and raw_status != "manual_required":
        status_error = "Unknown persisted task status; manual review required"
    if status not in ACTIVE_TASK_STATUSES and config_payload.get("download_request_key"):
        for key in _SINGLEFLIGHT_PERSISTED_KEYS:
            config_payload.pop(key, None)
    progress_detail = dict(config_payload.get("progress_detail") or {})
    progress_detail.setdefault("completed", 0)
    progress_detail.setdefault("failed", 0)
    progress_detail.setdefault("total", max(total_items, 0))
    progress_detail.setdefault(
        "percent",
        float(config_payload.get("progress_percent", _task_status_weight(status))),
    )
    progress_detail.setdefault(
        "current",
        redact_text(str(record.get("progress_message") or "")),
    )
    raw_error = config_payload.get("error")
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
        "started_at": str(
            config_payload.get("started_at") or record.get("created_at") or _now_iso()
        ),
        "finished_at": str(config_payload.get("finished_at") or ""),
        "category": _normalize_task_category(
            config_payload.get("category"),
            record.get("type"),
            record.get("source"),
        ),
        "progress_history": _sanitize_progress_history(config_payload.get("progress_history")),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "error": redact_text(str(raw_error)) if raw_error is not None else status_error,
        "result": config_payload.get("result"),
    }
    if config_payload.get("pid") is not None:
        task["pid"] = config_payload.get("pid")
    if config_payload.get("downloader") is not None:
        task["downloader"] = config_payload.get("downloader")
    for key in _SINGLEFLIGHT_PERSISTED_KEYS:
        if key in config_payload and key not in task:
            task[key] = config_payload[key]
    if config_payload.get("task_spec") is not None:
        task["task_spec"] = config_payload["task_spec"]
    for key in ("recovery_rejected", "recovery_mode"):
        if config_payload.get(key) is not None:
            task[key] = config_payload[key]
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
            task[key] = config_payload[key]
    if status_error:
        task["recovery_mode"] = "manual"
        task["recovery_rejected"] = status_error
    _ensure_task_runtime_metadata(task)
    return task


def _apply_recovery_data_metadata(
    task_id: str,
    *,
    command: list[str],
    downloader: str | None = None,
    task_spec: dict[str, Any] | None = None,
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
    with DATA_TASKS_LOCK:
        task["command"] = [str(part) for part in command]
    payload_updates: dict[str, Any] = {}
    if task_spec is not None:
        payload_updates["task_spec"] = _json_safe(task_spec)
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


def _coalesced_cleanup_state_path() -> Path:
    return DATA_DIR / ".coalesced-cleanup-state.json"


def _load_coalesced_cleanup_state() -> dict[str, dict[str, Any]]:
    with _COALESCED_CLEANUP_STATE_LOCK:
        try:
            payload = json.loads(_coalesced_cleanup_state_path().read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        state: dict[str, dict[str, Any]] = {}
        for task_id, entry in payload.items():
            normalized = str(task_id or "").strip()
            if _TASK_ID_RE.fullmatch(normalized) is None or not isinstance(entry, dict):
                continue
            try:
                attempts = max(0, int(entry.get("attempts", 0)))
            except (TypeError, ValueError):
                attempts = 0
            state[normalized] = {
                "attempts": attempts,
                "exhausted": bool(entry.get("exhausted")),
            }
            if len(state) >= _COALESCED_CLEANUP_MAX_ENTRIES:
                break
        return state


def _write_coalesced_cleanup_state(state: dict[str, dict[str, Any]]) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        path = _coalesced_cleanup_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path, json.dumps(state, sort_keys=True))


def _update_coalesced_cleanup_state(
    task_id: str, *, attempts: int, exhausted: bool
) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        state = _load_coalesced_cleanup_state()
        state[task_id] = {
            "attempts": max(0, int(attempts)),
            "exhausted": bool(exhausted),
        }
        while len(state) > _COALESCED_CLEANUP_MAX_ENTRIES:
            state.pop(next(iter(state)))
        _write_coalesced_cleanup_state(state)


def _clear_coalesced_cleanup_state(task_id: str) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        state = _load_coalesced_cleanup_state()
        if task_id not in state:
            return
        state.pop(task_id, None)
        if state:
            _write_coalesced_cleanup_state(state)
            return
        try:
            _coalesced_cleanup_state_path().unlink(missing_ok=True)
        except OSError:
            pass


def _is_coalesced_cleanup_marker(record: dict[str, Any]) -> bool:
    config = dict(record.get("config_json") or {})
    return bool(config.get("coalesced_cleanup_pending"))


def _is_coalesced_cleanup_pending(record: dict[str, Any]) -> bool:
    status = _task_status_or_manual(record.get("status"))
    if status == "cleanup_exhausted":
        return False
    if status == "cleanup_pending" or _is_coalesced_cleanup_marker(record):
        return True
    if status not in {"queued", "starting", "running", "paused"}:
        return False
    task_id = str(record.get("id") or "").strip()
    state = _load_coalesced_cleanup_state().get(task_id, {})
    if state.get("exhausted"):
        return False
    return _coalesced_cleanup_attempts(task_id, record) > 0


def _is_coalesced_cleanup_exhausted(record: dict[str, Any]) -> bool:
    config = dict(record.get("config_json") or {})
    status = _task_status_or_manual(record.get("status"))
    if status == "cleanup_exhausted" or config.get("coalesced_cleanup_exhausted"):
        return True
    if status == "cleanup_pending":
        return False
    task_id = str(record.get("id") or "").strip()
    state = _load_coalesced_cleanup_state().get(task_id, {})
    return bool(state.get("exhausted"))


def _coalesced_cleanup_attempts(
    task_id: str, record: dict[str, Any] | None = None
) -> int:
    state = _load_coalesced_cleanup_state().get(task_id, {})
    try:
        persisted = int(state.get("attempts", 0))
    except (TypeError, ValueError):
        persisted = 0
    if record is None:
        try:
            record = get_background_task_record(task_id)
        except Exception:
            record = None
    config_attempts = 0
    if isinstance(record, dict):
        try:
            config_attempts = int(
                dict(record.get("config_json") or {}).get("coalesced_cleanup_attempts", 0)
            )
        except (TypeError, ValueError):
            config_attempts = 0
    return max(persisted, config_attempts, _COALESCED_CLEANUP_ATTEMPTS.get(task_id, 0))


_COALESCED_CLEANUP_PENDING: set[str] = set()
_COALESCED_CLEANUP_ORDER: list[str] = []
_COALESCED_CLEANUP_ATTEMPTS: dict[str, int] = {}
_COALESCED_CLEANUP_EXHAUSTED: set[str] = set()
_COALESCED_CLEANUP_MAX_ENTRIES = 1024
_COALESCED_CLEANUP_MAX_ATTEMPTS = 5
_COALESCED_CLEANUP_STATE_LOCK = threading.RLock()


def _remember_coalesced_cleanup(task_id: str) -> tuple[str, ...]:
    evicted: list[str] = []
    with _COALESCED_CLEANUP_STATE_LOCK:
        if task_id not in _COALESCED_CLEANUP_PENDING:
            _COALESCED_CLEANUP_PENDING.add(task_id)
            while task_id in _COALESCED_CLEANUP_ORDER:
                _COALESCED_CLEANUP_ORDER.remove(task_id)
            _COALESCED_CLEANUP_ORDER.append(task_id)
        while len(_COALESCED_CLEANUP_ORDER) > _COALESCED_CLEANUP_MAX_ENTRIES:
            oldest = _COALESCED_CLEANUP_ORDER.pop(0)
            _COALESCED_CLEANUP_PENDING.discard(oldest)
            _COALESCED_CLEANUP_ATTEMPTS.pop(oldest, None)
            evicted.append(oldest)
    return tuple(evicted)


def _forget_coalesced_cleanup(task_id: str) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        _COALESCED_CLEANUP_PENDING.discard(task_id)
        _COALESCED_CLEANUP_ATTEMPTS.pop(task_id, None)
        _COALESCED_CLEANUP_EXHAUSTED.discard(task_id)
        try:
            _COALESCED_CLEANUP_ORDER.remove(task_id)
        except ValueError:
            pass


def _persist_coalesced_cleanup_state(
    task_id: str,
    *,
    attempts: int,
    exhausted: bool,
    fallback: dict[str, Any] | None = None,
) -> bool:
    try:
        _update_coalesced_cleanup_state(
            task_id, attempts=attempts, exhausted=exhausted
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not persist local coalesced cleanup state: %s", exc)

    try:
        record = get_background_task_record(task_id) or {}
    except Exception:
        record = {}
    if not record and fallback is not None:
        record = dict(fallback)
    config = dict(record.get("config_json") or {})
    config.pop("coalesced_cleanup_pending", None)
    if exhausted:
        config["coalesced_cleanup_exhausted"] = True
    else:
        config["coalesced_cleanup_pending"] = True
        config["coalesced_cleanup_attempts"] = max(0, int(attempts))
    record_values: dict[str, Any] = {
        "task_id": task_id,
        "task_type": str(record.get("type") or "data"),
        "status": "cleanup_exhausted" if exhausted else "cleanup_pending",
        "config_json": config,
        "progress_message": (
            "Cleanup exhausted; manual removal required"
            if exhausted
            else "Pending cleanup of coalesced provisional task"
        ),
    }
    for key in ("name", "description", "source", "created_at"):
        if record.get(key):
            record_values[key] = str(record[key])
    try:
        upsert_background_task_record(**record_values)
    except Exception as exc:
        logger.warning("Could not persist coalesced cleanup marker: %s", exc)
        return False
    return True


def _remember_coalesced_cleanup_exhausted(task_id: str) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        _COALESCED_CLEANUP_EXHAUSTED.add(task_id)
        while len(_COALESCED_CLEANUP_EXHAUSTED) > _COALESCED_CLEANUP_MAX_ENTRIES:
            _COALESCED_CLEANUP_EXHAUSTED.pop()


def _exhaust_coalesced_cleanup(task_id: str, attempts: int) -> None:
    with _COALESCED_CLEANUP_STATE_LOCK:
        _forget_coalesced_cleanup(task_id)
    _remember_coalesced_cleanup_exhausted(task_id)
    _persist_coalesced_cleanup_state(task_id, attempts=attempts, exhausted=True)


def _retry_coalesced_cleanup(task_id: str) -> bool:
    try:
        delete_background_task_record(task_id)
    except Exception as exc:
        attempts = _coalesced_cleanup_attempts(task_id) + 1
        with _COALESCED_CLEANUP_STATE_LOCK:
            _COALESCED_CLEANUP_ATTEMPTS[task_id] = attempts
            _remember_coalesced_cleanup(task_id)
        if attempts >= _COALESCED_CLEANUP_MAX_ATTEMPTS:
            _exhaust_coalesced_cleanup(task_id, attempts)
        else:
            _persist_coalesced_cleanup_state(
                task_id, attempts=attempts, exhausted=False
            )
        logger.warning(
            "Could not retry provisional coalesced task cleanup %s: %s",
            task_id,
            exc,
        )
        return False
    with _COALESCED_CLEANUP_STATE_LOCK:
        _forget_coalesced_cleanup(task_id)
    try:
        _clear_coalesced_cleanup_state(task_id)
    except OSError:
        pass
    return True


def _get_task(task_id: str) -> dict[str, Any] | None:
    normalized = str(task_id or "").strip()
    if _TASK_ID_RE.fullmatch(normalized) is None:
        return None
    task_id = normalized
    with DATA_TASKS_LOCK:
        cleanup_pending = task_id in _COALESCED_CLEANUP_PENDING
        cleanup_exhausted = task_id in _COALESCED_CLEANUP_EXHAUSTED
        if cleanup_pending or cleanup_exhausted:
            DATA_TASKS.pop(task_id, None)
    if cleanup_exhausted:
        return None
    if cleanup_pending:
        _retry_coalesced_cleanup(task_id)
        return None

    task = _LEGACY_GET_TASK(task_id)
    if task is not None:
        record = get_background_task_record(task_id)
        if record is not None and _is_coalesced_cleanup_exhausted(record):
            with DATA_TASKS_LOCK:
                DATA_TASKS.pop(task_id, None)
            _forget_coalesced_cleanup(task_id)
            _remember_coalesced_cleanup_exhausted(task_id)
            return None
        if record is not None and _is_coalesced_cleanup_pending(record):
            with DATA_TASKS_LOCK:
                DATA_TASKS.pop(task_id, None)
            _retry_coalesced_cleanup(task_id)
            return None
        _decorate_task_from_record(task_id, task)
        return task

    record = get_background_task_record(task_id)
    if record is None:
        return None
    if _is_coalesced_cleanup_exhausted(record):
        _forget_coalesced_cleanup(task_id)
        _remember_coalesced_cleanup_exhausted(task_id)
        return None
    if _is_coalesced_cleanup_pending(record):
        _retry_coalesced_cleanup(task_id)
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
    current = _get_task(task_id)
    return dict(current) if current is not None else None


def _evict_coalesced_cleanup_tasks() -> tuple[set[str], int]:
    with DATA_TASKS_LOCK:
        cleanup_ids = {
            task_id
            for task_id in _COALESCED_CLEANUP_PENDING
            if task_id not in _COALESCED_CLEANUP_EXHAUSTED
        }
        for task_id in set(_COALESCED_CLEANUP_PENDING):
            DATA_TASKS.pop(task_id, None)
        return cleanup_ids, len(DATA_TASKS)


def _hydrate_background_task_records(
    records: list[dict[str, Any]],
) -> tuple[set[str], int]:
    cleanup_ids: set[str] = {
        task_id
        for task_id in _COALESCED_CLEANUP_PENDING
        if task_id not in _COALESCED_CLEANUP_EXHAUSTED
    }
    exhausted_ids: set[str] = set()
    with DATA_TASKS_LOCK:
        for task_id in set(_COALESCED_CLEANUP_PENDING) | _COALESCED_CLEANUP_EXHAUSTED:
            DATA_TASKS.pop(task_id, None)
        for record in records:
            task_id = str(record.get("id") or "").strip()
            if not task_id or _TASK_ID_RE.fullmatch(task_id) is None:
                continue
            if _is_coalesced_cleanup_exhausted(record):
                DATA_TASKS.pop(task_id, None)
                exhausted_ids.add(task_id)
                continue
            if _is_coalesced_cleanup_pending(record):
                DATA_TASKS.pop(task_id, None)
                if _coalesced_cleanup_attempts(task_id, record) >= _COALESCED_CLEANUP_MAX_ATTEMPTS:
                    exhausted_ids.add(task_id)
                else:
                    _remember_coalesced_cleanup(task_id)
                    cleanup_ids.add(task_id)
                continue
            if task_id in cleanup_ids or task_id in DATA_TASKS:
                continue
            DATA_TASKS[task_id] = _build_task_from_record(record)
        terminal = sorted(
            (
                str(task.get("updated_at") or ""),
                task_id,
            )
            for task_id, task in DATA_TASKS.items()
            if _task_status_or_manual(task.get("status")) in FINAL_TASK_STATUSES
        )
        max_terminal_tasks = int(getattr(legacy_viewer, "MAX_TERMINAL_TASKS", 1024))
        excess = len(terminal) - max_terminal_tasks
        if excess > 0:
            for _, task_id in terminal[:excess]:
                DATA_TASKS.pop(task_id, None)
    for task_id in exhausted_ids:
        _exhaust_coalesced_cleanup(
            task_id, max(_COALESCED_CLEANUP_MAX_ATTEMPTS, _coalesced_cleanup_attempts(task_id))
        )
    return cleanup_ids, len(DATA_TASKS)


def _hydrate_task_cache_once(force: bool = False) -> int:
    global _TASK_CACHE_HYDRATED
    with _TASK_CACHE_HYDRATION_LOCK:
        if _TASK_CACHE_HYDRATED and not force:
            cleanup_ids, size = _evict_coalesced_cleanup_tasks()
            for task_id in cleanup_ids:
                _retry_coalesced_cleanup(task_id)
            return size
        records = list_background_task_records()
        cleanup_ids, size = _hydrate_background_task_records(records)
        for task_id in cleanup_ids:
            _retry_coalesced_cleanup(task_id)
        _TASK_CACHE_HYDRATED = True
        return size


def _update_task_fields(task_id: str, **updates: Any) -> dict[str, Any] | None:
    task = _LEGACY_UPDATE_TASK_FIELDS(task_id, **updates)
    if task is not None:
        _ensure_task_runtime_metadata(task)
        if (
            _task_status_or_manual(task.get("status")) in FINAL_TASK_STATUSES
            and not task.get("finished_at")
        ):
            task["finished_at"] = str(task.get("updated_at") or _now_iso())
        if "progress" in updates:
            _append_task_progress_history(task, task.get("progress"))
        _persist_task_snapshot(task_id, task)
    return task


def _set_task_status(task_id: str, status: str) -> dict[str, Any] | None:
    task = _LEGACY_SET_TASK_STATUS(task_id, status)
    if task is not None:
        _ensure_task_runtime_metadata(task)
        if _task_status_or_manual(status) in FINAL_TASK_STATUSES and not task.get("finished_at"):
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


def _discard_coalesced_download_registration(task_id: str) -> None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.pop(task_id, None)
    try:
        delete_background_task_record(task_id)
    except Exception as exc:
        attempts = 1
        persisted = _persist_coalesced_cleanup_state(
            task_id,
            attempts=attempts,
            exhausted=False,
            fallback={
                "id": task_id,
                "type": str((task or {}).get("kind") or "data"),
                "status": "cleanup_pending",
                "source": str((task or {}).get("source") or ""),
                "name": str((task or {}).get("name") or ""),
                "description": str((task or {}).get("description") or ""),
            },
        )
        if not persisted:
            _remember_coalesced_cleanup(task_id)
        logger.warning(
            "Could not remove provisional coalesced task %s; persisted cleanup marker: %s",
            task_id,
            exc,
        )


def _download_request_key_for_source(identity: dict[str, Any]) -> str:
    source = str(identity.get("source") or "")
    request_identity = {
        "source": source,
        "symbols": identity.get("symbols", []),
        "timeframes": identity.get("timeframes", []),
        "start": identity.get("start") or None,
        "end": identity.get("end") or None,
        "price_type": "",
        "merge_tick": False,
        "merge_s1": False,
        "batch_size": 0,
        "pause_ms": 0,
        "options": {},
    }
    if source == "dolthub":
        request_identity.update(symbols=[], timeframes=[], start=None, end=None)
    if source in {"dukascopy", "jforex"}:
        request_identity["price_type"] = identity.get("offer_side", "")
        request_identity["options"] = {
            "query_timeout_seconds": identity.get("query_timeout_seconds", 900)
        }
    if source in {"dukascopy", "jforex"}:
        request_identity.update(
            merge_tick=bool(identity.get("merge_tick")),
            merge_s1=bool(identity.get("merge_s1")),
            batch_size=identity.get("batch_size", 0),
            pause_ms=identity.get("pause_ms", 0),
        )
    return legacy_viewer._download_request_key(request_identity)


def _command_argument(cmd: list[str], flag: str) -> str:
    try:
        return str(cmd[cmd.index(flag) + 1])
    except (ValueError, IndexError):
        return ""


def _download_command_timeframes(cmd: list[str]) -> list[str]:
    return [
        _normalize_dukascopy_genesis_timeframe(timeframe)
        for timeframe in _parse_timeframe_list(_command_argument(cmd, "--timeframes"))
    ]


def _start_singleflight_download(
    task_id: str,
    request_key: str,
    starter: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        result = legacy_viewer._start_singleflight_download(
            task_id, request_key, starter, *args, **kwargs
        )
    except Exception:
        with DATA_TASKS_LOCK:
            registration_exists = task_id in DATA_TASKS
        if not registration_exists:
            _discard_coalesced_download_registration(task_id)
        raise
    if result.get("coalesced"):
        _discard_coalesced_download_registration(task_id)
    return result


def _start_data_task(
    task_id: str,
    cmd: list[str],
    **kwargs: Any,
) -> dict[str, Any]:
    source = str(kwargs.get("source") or "system")
    if source not in {"yfinance", "huggingface", "alpaca", "dolthub"}:
        return _LEGACY_START_DATA_TASK(task_id, cmd, **kwargs)
    request_key = _download_request_key_for_source(
        {
            "source": source,
            "symbols": _command_argument(cmd, "--symbols").split(","),
            "timeframes": _download_command_timeframes(cmd),
            "start": _command_argument(cmd, "--start"),
            "end": _command_argument(cmd, "--end"),
        }
    )
    return _start_singleflight_download(
        task_id,
        request_key,
        _LEGACY_START_DATA_TASK,
        cmd,
        **kwargs,
    )


def _start_dukascopy_task(
    task_id: str, cmd: list[str], **kwargs: Any
) -> dict[str, Any]:
    request_key = _download_request_key_for_source(
        {
            "source": "dukascopy",
            "symbols": kwargs.get("symbols", []),
            "timeframes": kwargs.get("timeframes", []),
            "start": kwargs.get("start"),
            "end": kwargs.get("end"),
            "offer_side": kwargs.get("price_type", "bid"),
            "merge_tick": kwargs.get("merge_tick", False),
            "merge_s1": kwargs.get("merge_s1", False),
            "batch_size": kwargs.get("batch_size", 10),
            "pause_ms": kwargs.get("pause_ms", 1000),
            "query_timeout_seconds": kwargs.get("query_timeout_seconds", 900),
        }
    )
    return _start_singleflight_download(
        task_id, request_key, _LEGACY_START_DUKASCOPY_TASK, cmd, **kwargs
    )


def _start_jforex_task(
    task_id: str, cmd: list[str], **kwargs: Any
) -> dict[str, Any]:
    request_key = _download_request_key_for_source(
        {
            "source": kwargs.get("source_label", "jforex"),
            "symbols": kwargs.get("symbols", []),
            "timeframes": kwargs.get("timeframes", []),
            "start": kwargs.get("start"),
            "end": kwargs.get("end"),
            "offer_side": kwargs.get("offer_side", "bid"),
            "merge_tick": kwargs.get("merge_tick", False),
            "merge_s1": kwargs.get("merge_s1", False),
            "batch_size": kwargs.get("batch_size", 10),
            "pause_ms": kwargs.get("pause_ms", 1000),
            "query_timeout_seconds": kwargs.get("query_timeout_seconds", 900),
        }
    )
    return _start_singleflight_download(
        task_id, request_key, _LEGACY_START_JFOREX_TASK, cmd, **kwargs
    )


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
    status = _task_status_or_manual(payload.get("status") or task.get("status"))
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
    if error_message is not None:
        error_message = redact_text(error_message)
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
    safe_payload = redact_value(payload)
    return safe_payload if isinstance(safe_payload, dict) else {}


_LEGACY_HOOKS_INSTALLED = False


def install_legacy_task_hooks() -> None:
    """Wire the persistence-aware task wrappers into the legacy core module.

    Explicit injection (called from lifespan startup) instead of import-time
    attribute reassignment: behavior no longer depends on which module was
    imported first and the wrapper chain is visible to static analysis.
    """
    global _LEGACY_HOOKS_INSTALLED
    if _LEGACY_HOOKS_INSTALLED:
        return
    legacy_viewer._create_task_record = _create_task_record
    legacy_viewer._get_task = _get_task
    legacy_viewer._snapshot_task = _snapshot_task
    legacy_viewer._update_task_fields = _update_task_fields
    legacy_viewer._set_task_status = _set_task_status
    legacy_viewer._update_task_progress = _update_task_progress
    legacy_viewer._mark_task_finished = _mark_task_finished
    legacy_viewer._serialize_task = _serialize_task
    _LEGACY_HOOKS_INSTALLED = True


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
        safe_payload = redact_value(payload)
        return json.dumps(safe_payload, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return redact_text(repr(payload))


def _redact_ws_message(message: Any) -> dict[str, Any]:
    safe_message = redact_value(message)
    if isinstance(safe_message, dict):
        return safe_message
    return {"type": "error", "error": "Live stream unavailable"}


async def _queue_live_ws_message(
    queue: asyncio.Queue[dict[str, Any]],
    message: dict[str, Any],
) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    await queue.put(_redact_ws_message(message))


def _live_ws_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return redact_text(detail)
        try:
            return redact_text(
                json.dumps(redact_value(detail), ensure_ascii=False, default=str)
            )
        except Exception:
            return "Live stream unavailable"
    return "Live stream unavailable"


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
        await websocket.send_json(
            _redact_ws_message({"type": "error", "topic": topic, "error": error_message})
        )

        with suppress(Exception):
            await websocket.close(code=1008)
        return

    await websocket.send_json(
        _redact_ws_message(
            {
                "type": "snapshot",
                "topic": topic,
                "data": initial_payload,
                "ts": time.time(),
            }
        )
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
            await websocket.send_json(_redact_ws_message(outbound))
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
    payload = {
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
    safe_payload = redact_value(payload)
    return safe_payload if isinstance(safe_payload, dict) else {}


def _serialize_service_payload(service: Service) -> dict[str, Any]:
    payload = {
        "id": str(service.id),
        "name": service.name,
        "type": service.type.value,
        "status": service.status.value,
        "config": service.config or {},
        "last_activity": service.last_activity.isoformat() if service.last_activity else None,
        "created_at": service.created_at.isoformat(),
    }
    safe_payload = redact_value(payload)
    return safe_payload if isinstance(safe_payload, dict) else {}


def _record_discovered_account_verification(
    account_id: str,
    *,
    is_demo: bool | None,
    ctid: str | None,
    broker_login: str | None,
) -> None:
    """Persist a broker demo verification for a freshly discovered account.

    Discovery is the only place that has just spoken to the broker, so it is the
    only legitimate writer of the verification stamp that ACTIVE transitions
    require. Failure is logged and swallowed: discovery must still return the
    account list even when the write fails, and the account simply stays
    unverified (fail-closed) until the next successful discovery.

    ``is_demo`` is tri-state. ``None`` means the source did not report the flag
    at all, and then there is no verification to record: defaulting it to False
    would invent a "the broker says this is live" confirmation out of an absent
    field, and would stamp a live-looking verification onto the account besides.
    """
    if is_demo is None:
        logger.warning(
            "Discovery reported no is_demo flag; recording no demo verification",
            extra={"data": {"account_id": account_id}},
        )
        return
    try:
        record_live_account_demo_verification(
            account_id,
            is_demo=is_demo,
            ctid=ctid,
            broker_login=broker_login,
        )
    except Exception as exc:
        logger.warning(
            "Could not persist broker demo verification for account",
            extra={"data": {"account_id": account_id, "error": repr(exc)}},
        )


def _discover_live_accounts_payload() -> list[dict[str, Any]]:
    discovered = _run_coroutine_sync(discover_configured_accounts)
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
            normalized_ctid = (
                str(account.get("ctid")).strip() if account.get("ctid") else None
            )
            # Only a real boolean counts as the source asserting demo-ness. A
            # missing or non-boolean value leaves the stored flag alone and is
            # reported to the client as unasserted, instead of being invented
            # into a False that would demote a verified demo account.
            raw_is_demo = account.get("is_demo")
            is_demo = raw_is_demo if isinstance(raw_is_demo, bool) else None
            _record_discovered_account_verification(
                account_id,
                is_demo=is_demo,
                ctid=normalized_ctid,
                broker_login=normalized_login,
            )
            payload.append(
                {
                    "id": account_id,
                    "name": str(account.get("name") or account_id),
                    "broker": str(account.get("broker") or "unknown"),
                    "balance": _safe_float(account.get("balance"), 0.0),
                    "is_demo": bool(is_demo),
                    "is_demo_asserted": is_demo is not None,
                    "broker_login": normalized_login,
                    "login": normalized_login,
                    "ctid": normalized_ctid,
                }
            )
            continue
        normalized_login = (
            str(account.broker_login).strip() if getattr(account, "broker_login", None) else None
        )
        normalized_ctid = (
            str(account.ctid).strip() if getattr(account, "ctid", None) else None
        )
        is_demo = bool(account.is_demo)
        _record_discovered_account_verification(
            account.id,
            is_demo=is_demo,
            ctid=normalized_ctid,
            broker_login=normalized_login,
        )
        payload.append(
            {
                "id": account.id,
                "name": account.name,
                "broker": account.broker,
                "balance": float(account.balance),
                "is_demo": is_demo,
                # A LiveAccount always carries a real boolean, so the source did
                # assert the flag. Spelled out so the persistence path below has
                # one rule to read instead of inferring it per branch.
                "is_demo_asserted": True,
                "broker_login": normalized_login,
                "login": normalized_login,
                "ctid": normalized_ctid,
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
    # One batched query for all accounts instead of get_latest_live_balance
    # per account (N+1) on every stream tick.
    balances_by_account = get_latest_live_balances(
        [str(item.get("id") or "").strip() for item in accounts]
    )
    for account in accounts:
        item = dict(account)
        account_id = str(item.get("id") or "").strip()
        latest_balance = balances_by_account.get(account_id) if account_id else None
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
        if action == "buy":
            direction = 1.0
        elif action == "sell":
            direction = -1.0
        else:
            continue

        quantity = abs(_safe_float(row.get("filled_qty"), 0.0))
        price = _safe_float(row.get("actual_price"), 0.0)
        if quantity <= 0.0 or price <= 0.0:
            continue

        delta = quantity * direction
        state = positions.setdefault(symbol, {"qty": 0.0, "avg": 0.0})
        current_qty = state["qty"]
        current_avg = state["avg"]

        if abs(current_qty) < LIVE_POSITION_FLAT_THRESHOLD:
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
        if abs(new_qty) < LIVE_POSITION_FLAT_THRESHOLD:
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
    # Win rate denominators are closed round trips (a win or a loss), never raw
    # execution rows: partial fills would otherwise deflate the ratio.
    total_trades = wins + losses
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
                logger.info("CAPM benchmark: using %s resampled to 1d", _log_safe(fallback_timeframe))
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
    symbols = data.get("symbols") or []
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


def _cached_backtest_strategy_matches(
    item: dict[str, Any],
    *,
    strategy_entry: Any,
    normalized_strategy: str,
) -> bool:
    item_strategy = _normalize_strategy_key(item.get("strategy_name"))
    if strategy_entry is not None and matches_strategy_identifier(
        strategy_entry, str(item.get("strategy_name") or "")
    ):
        return True
    return item_strategy == normalized_strategy


def _latest_matching_cached_backtest(
    *,
    strategy_entry: Any,
    normalized_strategy: str,
    normalized_symbol: str,
    normalized_timeframe: str,
) -> tuple[str, str] | None:
    """Newest backtest from the meta cache matching strategy/symbol/timeframe."""
    best_candidate: tuple[str, str] | None = None
    for item in _snapshot_backtest_meta_cache():
        backtest_id = str(item.get("id") or "").strip()
        if not backtest_id:
            continue
        if not _cached_backtest_strategy_matches(
            item,
            strategy_entry=strategy_entry,
            normalized_strategy=normalized_strategy,
        ):
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
    return best_candidate


def _file_backtest_strategy_matches(
    raw_strategies: list[Any],
    *,
    strategy_entry: Any,
    normalized_strategy: str,
) -> bool:
    if strategy_entry is not None and any(
        matches_strategy_identifier(strategy_entry, strategy_value)
        for strategy_value in raw_strategies
    ):
        return True
    return any(
        _normalize_strategy_key(strategy_value) == normalized_strategy
        for strategy_value in raw_strategies
    )


def _latest_matching_file_backtest(
    *,
    strategy_name: str,
    strategy_entry: Any,
    normalized_strategy: str,
    normalized_symbol: str,
    normalized_timeframe: str,
) -> str:
    """Newest trade file on disk matching strategy/symbol/timeframe (meta cache miss)."""
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

        if not _file_backtest_strategy_matches(
            raw_strategies,
            strategy_entry=strategy_entry,
            normalized_strategy=normalized_strategy,
        ):
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
        if (
            isinstance(timeframes_payload, list)
            and timeframes_payload
            and normalized_timeframe
            not in {_normalize_timeframe_key(raw) for raw in timeframes_payload}
        ):
            continue

        return file_path.stem.replace(".trades", "")

    raise HTTPException(
        status_code=404,
        detail=(
            f"No backtest found for component '{strategy_name}' "
            f"({normalized_symbol or 'N/A'} {normalized_timeframe})"
        ),
    )


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

    best_candidate = _latest_matching_cached_backtest(
        strategy_entry=strategy_entry,
        normalized_strategy=normalized_strategy,
        normalized_symbol=normalized_symbol,
        normalized_timeframe=normalized_timeframe,
    )

    if best_candidate is not None:
        return best_candidate[1]

    file_candidate = _latest_matching_file_backtest(
        strategy_name=strategy_name,
        strategy_entry=strategy_entry,
        normalized_strategy=normalized_strategy,
        normalized_symbol=normalized_symbol,
        normalized_timeframe=normalized_timeframe,
    )
    if file_candidate:
        return file_candidate

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

    gross_pnl = _trade_numeric_value(trade, "gross_pnl", "pnl", "realized_pnl", "profit")
    net_pnl = _trade_numeric_value(trade, "net_pnl", "net_profit")
    fees = _trade_numeric_value(trade, "fees", "commission")
    if net_pnl is None and gross_pnl is not None:
        net_pnl = gross_pnl - (fees or 0.0)
    if net_pnl is not None:
        trade["net_pnl"] = net_pnl
        trade["pnl"] = net_pnl
    if gross_pnl is not None:
        trade["gross_pnl"] = gross_pnl
    if fees is not None:
        trade["fees"] = fees
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

    component_metrics = _collect_component_contribution_metrics(components)

    grouped = _accumulate_portfolio_buckets(
        components=components,
        component_metrics=component_metrics,
        grouped=grouped,
        normalized_group=normalized_group,
    )

    component_ids_by_group = {
        key: {str(component_id) for component_id in value.get("component_ids", [])}
        for key, value in grouped.items()
    }

    _accumulate_trade_pnls(
        trades=trades,
        component_ids_by_group=component_ids_by_group,
        grouped=grouped,
    )

    total_pnl = _safe_float((portfolio.get("combined_stats") or {}).get("net_profit"), 0.0)
    contributions = [
        _portfolio_contribution_payload(
            grouped[key],
            group=normalized_group,
            total_return=total_return,
            total_drawdown=total_drawdown,
            total_pnl=total_pnl,
        )
        for key in sorted(grouped.keys())
    ]
    return contributions


def _empty_contribution_metrics() -> dict[str, float]:
    return {
        "sharpe_ratio": 0.0,
        "psr": 0.0,
        "p_value": 1.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "cagr": 0.0,
        "total_return": 0.0,
    }


def _collect_component_contribution_metrics(
    components: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Load per-component stats from the linked backtest results."""
    component_metrics: dict[str, dict[str, float]] = {}
    for component in components:
        component_id = str(component.get("component_id") or "").strip()
        if not component_id:
            continue
        raw_win_rate = _normalize_win_rate(component.get("win_rate"))
        metrics = _empty_contribution_metrics()
        metrics["win_rate"] = raw_win_rate if raw_win_rate is not None else 0.0
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
    return component_metrics


def _new_contribution_bucket(
    *,
    normalized_group: str,
    key: str,
    component_id: str,
    strategy_name: str,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    return {
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
    }


def _accumulate_portfolio_buckets(
    *,
    components: list[dict[str, Any]],
    component_metrics: dict[str, dict[str, float]],
    grouped: dict[str, dict[str, Any]],
    normalized_group: str,
) -> dict[str, dict[str, Any]]:
    """Fold every component's weight/contributions/metrics into its group bucket."""
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
            _new_contribution_bucket(
                normalized_group=normalized_group,
                key=key,
                component_id=component_id,
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
            ),
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

        metrics = component_metrics.get(component_id) or _empty_contribution_metrics()
        _fold_contribution_values(bucket, component, metrics, weight)
        _fold_metric_sums(bucket, metrics, weight)
    return grouped


def _derived_contribution(
    reported: Any, derived: float
) -> float:
    """Use the derived value when the reported one is missing or dust."""
    value = _safe_float(reported, float("nan"))
    if (
        not math.isfinite(value)
        or (abs(value) <= 1e-12 and abs(derived) > 1e-12)
    ):
        return derived
    return value


def _fold_contribution_values(
    bucket: dict[str, Any], component: dict[str, Any], metrics: dict[str, float], weight: float
) -> None:
    return_contribution = _derived_contribution(
        component.get("return_contribution"),
        _safe_float(metrics.get("total_return"), 0.0) * weight,
    )
    drawdown_contribution = _derived_contribution(
        component.get("drawdown_contribution"),
        abs(_safe_float(metrics.get("max_drawdown"), 0.0)) * max(weight, 0.0),
    )

    bucket["return_contribution"] = _safe_float(bucket.get("return_contribution"), 0.0) + return_contribution
    bucket["drawdown_contribution"] = _safe_float(bucket.get("drawdown_contribution"), 0.0) + abs(
        drawdown_contribution
    )


def _fold_metric_sums(
    bucket: dict[str, Any], metrics: dict[str, float], weight: float
) -> None:
    """Accumulate weighted metric sums into the group bucket."""
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


def _accumulate_trade_pnls(
    *,
    trades: list[dict[str, Any]],
    component_ids_by_group: dict[str, set[str]],
    grouped: dict[str, dict[str, Any]],
) -> None:
    """Add per-trade counts and PnL to the groups owning their component."""
    for trade in trades:
        component_id = str(trade.get("component_id") or "").strip()
        if not component_id:
            continue
        for key, component_ids in component_ids_by_group.items():
            if component_id not in component_ids:
                continue
            bucket = grouped[key]
            pnl_value = trade.get("net_pnl")
            if pnl_value is None:
                pnl_value = _safe_float(trade.get("pnl"), 0.0)
                pnl_value -= _safe_float(trade.get("fees"), 0.0)
            pnl_value = _safe_float(pnl_value, 0.0)
            bucket["trade_count"] = int(bucket.get("trade_count") or 0) + 1
            if pnl_value > 0:
                bucket["wins"] = int(bucket.get("wins") or 0) + 1
            bucket["pnl_total"] = _safe_float(bucket.get("pnl_total"), 0.0) + pnl_value


def _portfolio_contribution_display_value(
    bucket: dict[str, Any], group: str, default_value: str
) -> str:
    if group != "component":
        return default_value
    strategy_name = str(bucket.get("strategy_name") or default_value or "").strip()
    symbol = _normalize_symbol_key(bucket.get("symbol"))
    timeframe = _normalize_timeframe_key(bucket.get("timeframe"))
    suffix_values = [value for value in [symbol, timeframe] if value]
    if strategy_name and suffix_values:
        return f"{strategy_name} ({', '.join(suffix_values)})"
    if strategy_name:
        return strategy_name
    return default_value


def _portfolio_contribution_payload(
    bucket: dict[str, Any],
    *,
    group: str,
    total_return: float,
    total_drawdown: float,
    total_pnl: float,
) -> dict[str, Any]:
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
    display_value = _portfolio_contribution_display_value(
        bucket, group, str(bucket.get("group_value") or "")
    )
    return {
        "group": group,
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


def _source_components_from_strategies(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map legacy 'strategies' rows into component-shaped dicts."""
    source_components: list[dict[str, Any]] = []
    for strategy_row in strategies:
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
    return source_components


def _build_portfolio_components(source_components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize every raw component dict into a portfolio component payload."""
    components: list[dict[str, Any]] = []
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
    return components


def _match_component_for_trade(
    trade: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    strategy_name: str,
    symbol: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """Find a component matching a trade by strategy/symbol/timeframe."""
    if not strategy_name:
        return None
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
        return candidate
    return None


def _portfolio_legacy_strategy_rows(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map components back into the legacy 'strategies' row shape."""
    return [
        {
            "backtest_id": str(component.get("backtest_id") or ""),
            "strategy_name": str(component.get("strategy_name") or ""),
            "risk_per_trade_pct": _safe_float(component.get("legacy_risk_per_trade_pct"), 1.0),
            "max_positions": int(_safe_float(component.get("legacy_max_positions"), 99)),
        }
        for component in components
    ]


def _normalize_portfolio_trades(
    raw_trades: list[Any], *, components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalize portfolio trades and enrich them from their source backtests."""
    component_by_id = {
        str(component.get("component_id") or ""): component
        for component in components
        if str(component.get("component_id") or "").strip()
    }
    backtest_trade_lookup_cache: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]] = {}
    normalized_trades: list[dict[str, Any]] = []
    for raw_trade in raw_trades:
        if not isinstance(raw_trade, dict):
            continue
        trade = dict(raw_trade)
        component_id = str(trade.get("component_id") or "").strip()
        component = component_by_id.get(component_id)

        strategy_name = str(trade.get("strategy_name") or trade.get("strategy") or "").strip()
        symbol = _normalize_symbol_key(trade.get("symbol"))
        timeframe_text = str(trade.get("timeframe") or "").strip()
        timeframe = _normalize_timeframe_key(timeframe_text) if timeframe_text else ""

        if component is None:
            component = _match_component_for_trade(
                trade,
                components,
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
            )
            if component is not None:
                component_id = str(component.get("component_id") or "")

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
    return normalized_trades


def _compute_strategy_contributions(
    grouped: list[dict[str, Any]], *, combined_stats: dict[str, Any]
) -> list[dict[str, Any]]:
    """Derive per-strategy PnL contribution percentages from grouped stats."""
    total_pnl = _safe_float(combined_stats.get("net_profit"), 0.0)
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
    return strategy_contributions


def _normalize_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)

    raw_components = normalized.get("components")
    if isinstance(raw_components, list) and raw_components:
        source_components = [item for item in raw_components if isinstance(item, dict)]
    else:
        source_components = _source_components_from_strategies(normalized.get("strategies", []))

    components = _build_portfolio_components(source_components)
    normalized["components"] = components
    normalized["strategies"] = _portfolio_legacy_strategy_rows(components)

    normalized_trades = _normalize_portfolio_trades(
        normalized.get("trades", []),
        components=components,
    )

    normalized_trades.sort(key=_component_trade_time)
    normalized["trades"] = normalized_trades

    component_contributions = normalized.get("component_contributions")
    if not isinstance(component_contributions, list):
        normalized["component_contributions"] = _compute_portfolio_contributions(
            portfolio=normalized,
            group_by="component",
        )

    if not isinstance(normalized.get("strategy_contributions"), list):
        normalized["strategy_contributions"] = _compute_strategy_contributions(
            _compute_portfolio_contributions(portfolio=normalized, group_by="strategy"),
            combined_stats=normalized.get("combined_stats")
            if isinstance(normalized.get("combined_stats"), dict)
            else {},
        )

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
                if isinstance(entry, dict) and str(entry.get("path", "")).endswith(OHLCV_PARQUET_SUFFIX)
            ]
        return _scan_single_file_ohlcv_entries({})

    include_last_updated = (DATA_DIR / "catalog.json").exists()
    if include_last_updated:
        normalized_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("path", "")).endswith(OHLCV_PARQUET_SUFFIX)
        ]
        if normalized_entries:
            return normalized_entries

    normalized_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("path", "")).endswith(OHLCV_PARQUET_SUFFIX):
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
# Vendor intraday coverage windows live in data_tools.auto_download and are
# configurable via HF_INTRADAY_COVERAGE_END / ALPACA_INTRADAY_START env vars;
# _auto_download_source_plan re-reads them, so no module-level mirror here.
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


def _is_dukascopy_symbol(symbol: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        return normalized_symbol in {key.upper() for key in SYMBOL_CONFIG}
    except Exception:
        return False


def _is_us_intraday_stock(symbol: str, timeframe: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if normalized_timeframe not in US_INTRADAY_TIMEFRAMES:
        return False
    if _is_forex_symbol(normalized_symbol) or _is_dukascopy_symbol(normalized_symbol):
        return False
    if not _US_STOCK_SYMBOL_PATTERN.fullmatch(normalized_symbol):
        return False
    if normalized_symbol.endswith("USD") and len(normalized_symbol) > 5:
        return False
    return True


def _auto_download_source(symbol: str, timeframe: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if _is_dukascopy_symbol(normalized_symbol) or _is_forex_symbol(normalized_symbol):
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
    """Shared vendor plan from data_tools.auto_download (env-configurable)."""
    reload_coverage_windows()
    return _auto_download_config._auto_download_source_plan(
        symbol=symbol,
        timeframe=timeframe,
        start=start or None,
        end=end or None,
    )


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


def _collect_missing_backtest_data(
    *,
    symbol_list: list[str],
    timeframe: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Return the per-symbol missing-range entries for the requested data."""
    availability_index = _build_ohlcv_availability_index()
    missing_items: list[dict[str, Any]] = []
    for symbol in symbol_list:
        availability = _data_check_result(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            availability_index=availability_index,
        )
        if availability.get("available"):
            continue
        missing_items.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "missing_range": availability.get("missing_range"),
                "source": _auto_download_source(symbol, timeframe),
            }
        )
    return missing_items


def _verify_missing_download_coverage(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    log_callback: Callable[[str], None] | None = None,
) -> None:
    """Re-check coverage after an auto-download; tolerate only small boundary gaps."""
    availability_index = _build_ohlcv_availability_index()
    verification = _data_check_result(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        availability_index=availability_index,
    )
    if verification.get("available"):
        return
    coverage_entry = availability_index.get((symbol, timeframe))
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
                    f"Auto-download coverage tolerance for {symbol} {timeframe}: "
                    f"{tolerated_reason}; available range {available_start} → {available_end}."
                )
            )
    else:
        raise RuntimeError(
            f"Auto-download did not provide required data range for {symbol} {timeframe}"
        )


def _download_and_verify_missing_data(
    missing_items: list[dict[str, Any]],
    *,
    normalized_timeframe: str,
    start: str,
    end: str,
    progress_callback: Callable[[float, str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> None:
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
                coverage_note = (
                    " [coverage unknown: no configured vendor window covers this range]"
                    if str(step.get("coverage") or "") == "unknown"
                    else ""
                )
                log_callback(
                    (
                        f"Auto-download{suffix}: {symbol} {normalized_timeframe} via {step_source} "
                        f"({step_start or 'auto'} → {step_end or 'latest'}){coverage_note}"
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

        _verify_missing_download_coverage(
            symbol=symbol,
            timeframe=normalized_timeframe,
            start=start,
            end=end,
            log_callback=log_callback,
        )

        progress_after = 10.0 + (index / total_missing) * 35.0
        if progress_callback is not None:
            progress_callback(progress_after, f"Downloading data... {int(round(progress_after))}%")


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

    missing_items = _collect_missing_backtest_data(
        symbol_list=symbol_list,
        timeframe=normalized_timeframe,
        start=start,
        end=end,
    )

    if not missing_items:
        return []

    _download_and_verify_missing_data(
        missing_items,
        normalized_timeframe=normalized_timeframe,
        start=start,
        end=end,
        progress_callback=progress_callback,
        log_callback=log_callback,
    )

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


def _normalize_ohlcv_validation(
    validation_payload: dict[str, Any],
    warning_message: str | None,
    error_message: str | None,
) -> dict[str, Any]:
    """Normalize a catalog validation payload; an empty dict means 'omit'."""
    if not validation_payload and not warning_message and not error_message:
        return {}
    normalized_validation = dict(validation_payload)
    status = str(normalized_validation.get("status") or "").strip().lower()
    if status not in {"ok", "warning", "error"}:
        status = "ok"
    if status == "ok" and (warning_message or error_message):
        status = "warning"
    normalized_validation["status"] = status
    return normalized_validation


def _aggregate_ohlcv_entry_payload(
    entry: dict[str, Any],
    *,
    include_category: bool = False,
    include_size_bytes: bool = False,
) -> dict[str, Any]:
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
    normalized_validation = _normalize_ohlcv_validation(
        validation_payload if isinstance(validation_payload, dict) else {},
        warning_message,
        error_message,
    )
    if normalized_validation:
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

    return output_entry


def _aggregate_symbol_timeframe_entries(
    entries: list[dict[str, Any]],
    *,
    include_category: bool = False,
    include_size_bytes: bool = False,
) -> list[dict[str, Any]]:
    logical_entries: list[dict[str, Any]] = []
    merged_entries = merge_ohlcv_entries([entry for entry in entries if isinstance(entry, dict)])
    for entry in merged_entries:
        logical_entries.append(
            _aggregate_ohlcv_entry_payload(
                entry,
                include_category=include_category,
                include_size_bytes=include_size_bytes,
            )
        )

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


@app.get("/api/strategies")
async def list_strategies() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_cached_strategies_payload)


@app.get("/api/signal-processors")
async def list_signal_processors() -> list[dict[str, str]]:
    return await asyncio.to_thread(list_signal_processor_options)


def _codegen_gate_strategy_path(relative_file: str) -> Path:
    """Resolve a strategy relative file inside the strategies dir (traversal-safe)."""
    normalized = str(relative_file or "").strip()
    if not normalized or ".." in normalized or normalized.startswith("/"):
        raise _invalid_strategy_file_path_error()
    candidate = (STRATEGIES_DIR / normalized).resolve()
    try:
        candidate.relative_to(STRATEGIES_DIR.resolve())
    except ValueError:
        raise _invalid_strategy_file_path_error() from None
    return candidate


def _invalid_strategy_file_path_error() -> HTTPException:
    """Build the 400 raised when a strategy path fails traversal checks."""
    return HTTPException(status_code=400, detail="Invalid strategy file path")


def _require_existing_strategy(relative_file: str) -> Path:
    """Traversal-safe resolve plus 404 when the file does not exist."""
    path = _codegen_gate_strategy_path(relative_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Strategy '{relative_file}' not found")
    return path


@app.get(
    "/api/strategies/{relative_file:path}/approval",
    responses={
        400: {"description": "Invalid strategy file path"},
        404: {"description": "Strategy not found"},
    },
)
async def get_strategy_approval(relative_file: str) -> dict[str, Any]:
    """Report whether a strategy file's current content is covered by a human approval."""
    from runner.approval_gate import is_strategy_approved

    path = _require_existing_strategy(relative_file)
    approved = await asyncio.to_thread(
        is_strategy_approved, path, strategies_root=STRATEGIES_DIR
    )
    return {"approved": approved}


@app.post(
    "/api/strategies/{relative_file:path}/approve",
    responses={
        400: {"description": "Invalid strategy file path"},
        404: {"description": "Strategy not found"},
        500: {"description": "Could not record the approval"},
    },
)
async def approve_strategy(relative_file: str) -> dict[str, Any]:
    """Record a SHA256-bound human approval for a generated strategy file."""
    from runner.approval_gate import approve_strategy as record_approval

    path = _require_existing_strategy(relative_file)
    try:
        record = await asyncio.to_thread(
            record_approval, path, approved_by="human", strategies_root=STRATEGIES_DIR
        )
    except OSError:
        logger.exception("Failed to record approval for the requested strategy file")
        raise HTTPException(
            status_code=500,
            detail="Could not record the strategy approval; see server logs.",
        ) from None
    return {
        "approved": True,
        "file": record["file"],
        "sha256": record["sha256"],
        "approved_by": record["approved_by"],
        "approved_at": record["approved_at"],
    }


@app.get("/api/strategies/{name:path}", response_model=dict[str, Any])
async def get_strategy(name: str) -> dict[str, Any]:
    entry = _safe_strategy_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")
    try:
        return legacy_viewer._strategy_payload(entry)
    except Exception as exc:
        logger.exception("Failed to build strategy payload for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "strategy payload")) from exc


@app.patch("/api/strategies/{name:path}")
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
        logger.exception("Failed to rename strategy %s: %s", _log_safe(name), exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "strategy rename")) from exc


def _matrix_result_path(filename: str) -> Path:
    normalized = str(filename or "").strip()
    if not normalized or ".." in normalized or normalized.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid matrix filename")
    return MATRIX_RESULTS_DIR / normalized


def _matrix_summary_payload() -> dict[str, Any]:
    summary_path = MATRIX_RESULTS_DIR / "matrix_summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="No matrix summary found. Run `python -m runner.cli matrix` first.")
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "matrix summary")) from exc
    payload["matrix_csv"] = str(_matrix_result_path("matrix_matrix.csv"))
    payload["daily_equity_csv"] = str(_matrix_result_path("matrix_daily_equity.csv"))
    return payload


def _matrix_grid_overview_path() -> Path:
    candidates = [
        MATRIX_GRID_OVERVIEW_PATH,
        STATIC_DIR / MATRIX_GRID_OVERVIEW_FILE,
        PROJECT_ROOT / "frontend" / "public" / MATRIX_GRID_OVERVIEW_FILE,
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _load_matrix_grid_overview_payload() -> dict[str, Any]:
    path = _matrix_grid_overview_path()
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No matrix grid overview found. Run `python scripts/generate_matrix_grid_overview.py` first.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "matrix grid overview")) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Invalid matrix grid overview payload")
    payload["api_source"] = str(path)
    return payload


def _matrix_row_backtest_id(row: dict[str, Any]) -> str:
    backtest_id = str(row.get("backtest_id") or "").strip()
    if backtest_id:
        return backtest_id
    run_id = str(row.get("run_id") or "").strip()
    return run_id


def _matrix_grid_row_with_trades(row: dict[str, Any], *, trades_limit: int) -> dict[str, Any]:
    enriched = dict(row)
    backtest_id = _matrix_row_backtest_id(row)
    if backtest_id:
        enriched["backtest_id"] = backtest_id
        enriched["backtest_url"] = f"/backtests/{backtest_id}"
        enriched["trades_url"] = f"/api/backtest/{backtest_id}/trades"

    if not backtest_id or trades_limit <= 0:
        enriched.setdefault("trades", [])
        enriched["trade_sample_count"] = 0
        return enriched

    result = load_backtest_result(backtest_id)
    if result is None:
        enriched.setdefault("trades", [])
        enriched["trade_sample_count"] = 0
        enriched["trade_data_available"] = False
        return enriched

    trades = [legacy_viewer._trade_to_viewer(trade) for trade in result.trades[:trades_limit]]
    enriched["trades"] = trades
    enriched["trade_sample_count"] = len(trades)
    enriched["trade_data_available"] = bool(result.trades)
    enriched["total_trades"] = len(result.trades)
    return enriched


@app.get("/api/backtests/matrix/summary")
async def get_backtest_matrix_summary() -> dict[str, Any]:
    return _matrix_summary_payload()


@app.get("/api/backtests/matrix/matrix.csv", response_class=FileResponse)
async def get_backtest_matrix_csv() -> FileResponse:
    path = _matrix_result_path("matrix_matrix.csv")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Matrix CSV not found")
    return FileResponse(path, media_type="text/csv", filename=path.name)


@app.get("/api/backtests/matrix/daily-equity.csv", response_class=FileResponse)
async def get_backtest_matrix_daily_equity_csv() -> FileResponse:
    path = _matrix_result_path("matrix_daily_equity.csv")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Matrix daily equity CSV not found")
    return FileResponse(path, media_type="text/csv", filename=path.name)


@app.get("/api/grid/{strategy_id:path}")
async def get_grid_strategy_detail(
    strategy_id: str,
    trades_limit: int = Query(default=3, ge=0, le=20),
) -> dict[str, Any]:
    payload = _load_matrix_grid_overview_payload()
    normalized_strategy = str(strategy_id or "").strip()
    include_all = normalized_strategy.lower() in {"", "all", "*"}

    rows = [row for row in payload.get("rows", []) if isinstance(row, dict)]
    if not include_all:
        rows = [row for row in rows if str(row.get("strategy") or "") == normalized_strategy]
        if not rows:
            raise HTTPException(status_code=404, detail=f"Grid strategy '{strategy_id}' not found")

    payload = dict(payload)
    payload["rows"] = [
        _matrix_grid_row_with_trades(row, trades_limit=trades_limit)
        for row in rows
    ]
    if include_all:
        summaries = payload.get("strategy_summaries", [])
        payload["strategy_summaries"] = [item for item in summaries if isinstance(item, dict)]
    else:
        summaries = payload.get("strategy_summaries", [])
        payload["strategy_summaries"] = [
            item for item in summaries
            if isinstance(item, dict) and str(item.get("strategy") or "") == normalized_strategy
        ]
        payload["blocked_strategies"] = [
            item for item in payload.get("blocked_strategies", [])
            if isinstance(item, dict) and str(item.get("strategy") or "") == normalized_strategy
        ]
    payload["selected_strategy"] = None if include_all else normalized_strategy
    payload["counts"] = {
        **dict(payload.get("counts") or {}),
        "rows": len(payload["rows"]),
        "ok_rows": sum(1 for row in payload["rows"] if row.get("status") == "ok"),
        "failed_rows": sum(1 for row in payload["rows"] if row.get("status") == "failed"),
        "symbols": sorted({str(row.get("symbol") or "") for row in payload["rows"] if row.get("symbol")}),
    }
    return payload


@app.get("/api/backtests")
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


@app.get("/api/backtests/all")
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


@app.get("/api/backtests/duplicates")
async def list_backtest_duplicates() -> BacktestsDuplicatesResponse:
    groups, total_backtests = _group_duplicate_backtests()
    return BacktestsDuplicatesResponse(
        groups=groups,
        total_groups=len(groups),
        total_backtests=total_backtests,
    )


@app.post("/api/backtests/deduplicate")
async def deduplicate_backtests() -> BacktestsDeduplicateResponse:
    groups, _ = _group_duplicate_backtests()
    deleted_ids: list[str] = []
    kept_ids: list[str] = []
    items: list[dict[str, Any]] = []
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
            result = _delete_backtest_with_artifacts(backtest_id)
            items.append(result)
            if result.get("status") == "deleted":
                deleted_ids.append(backtest_id)
                removed_in_group = True
        if removed_in_group:
            affected += 1

    return BacktestsDeduplicateResponse(
        deleted_ids=deleted_ids,
        kept_ids=kept_ids,
        groups_affected=affected,
        items=items,
    )


@app.post("/api/backtests/remove-empty")
async def remove_empty_backtests() -> dict[str, Any]:
    removed = 0
    items: list[dict[str, Any]] = []
    for item in _snapshot_backtest_meta_cache():
        backtest_id = str(item.get("id") or "").strip()
        if not backtest_id:
            continue
        trade_count = max(int(_safe_float(item.get("trade_count"), 0.0)), 0)
        if trade_count != 0:
            continue
        result = _delete_backtest_with_artifacts(backtest_id)
        items.append(result)
        if result.get("status") == "deleted":
            removed += 1
    return {"removed": removed, "items": items}


@app.post("/api/backtests/delete-bulk")
async def delete_backtests_bulk(payload: BacktestsBulkDeleteRequest) -> dict[str, Any]:
    deleted = 0
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for raw_id in payload.ids:
        backtest_id = str(raw_id or "").strip()
        if not backtest_id or backtest_id in seen:
            continue
        seen.add(backtest_id)
        result = _delete_backtest_with_artifacts(backtest_id)
        items.append(result)
        if result.get("status") == "deleted":
            deleted += 1
    return {"deleted": deleted, "items": items}


@app.get("/api/backtests/compare")
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


@app.get("/api/backtest/{backtest_id}")
@app.get("/api/backtests/{backtest_id}")
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
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "backtest serialization")) from exc


@app.delete("/api/backtests/{backtest_id}")
async def delete_single_backtest(backtest_id: str) -> dict[str, Any]:
    result = _delete_backtest_with_artifacts(backtest_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")
    if result.get("status") == "cleanup_pending":
        raise HTTPException(
            status_code=503,
            detail={"error": "Backtest deletion is pending cleanup", "result": result},
        )
    if result.get("status") != "deleted":
        raise HTTPException(
            status_code=500,
            detail={"error": "Backtest deletion failed", "result": result},
        )
    return result


@app.patch("/api/backtests/{backtest_id}")
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
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "backtest rename")) from exc


@app.put("/api/backtests/{backtest_id}/name")
async def rename_backtest_name(backtest_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    return await rename_backtest(backtest_id, payload)


@app.get("/api/backtest/{backtest_id}/info")
@app.get("/api/backtests/{backtest_id}/info")
async def get_backtest_info(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return legacy_viewer._backtest_info_payload(result)


@app.get("/api/backtest/{backtest_id}/stats")
@app.get("/api/backtests/{backtest_id}/stats")
async def get_backtest_stats(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Backtest '{backtest_id}' not found")

    try:
        return await asyncio.to_thread(legacy_viewer._stats_payload_from_result, result)
    except Exception as exc:
        logger.exception("Failed to compute stats for '%s': %s", backtest_id, exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "backtest stats")) from exc


@app.get("/api/backtest/{backtest_id}/equity")
@app.get("/api/backtests/{backtest_id}/equity")
async def get_equity_curve(
    backtest_id: str,
    max_points: int = Query(default=0, ge=0, le=20000),
    sampling: str = Query(default="none"),
) -> list[dict[str, float]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
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


@app.get("/api/backtests/{backtest_id}/equity-preview")
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


@app.get("/api/backtests/equity-preview/batch")
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


@app.get("/api/backtest/{backtest_id}/capm")
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
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc

    benchmark_returns = _benchmark_daily_returns(
        benchmark_symbol,
        start=result.meta.backtest_start or "",
        end=result.meta.backtest_end or "",
    )

    try:
        capm = calculate_capm(strategy_returns, benchmark_returns)
        aligned = align_return_series(strategy_returns, benchmark_returns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc

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


@app.get("/api/backtest/{backtest_id}/trades")
@app.get("/api/backtests/{backtest_id}/trades")
async def get_backtest_trades(
    backtest_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    paged_trades = result.trades[offset : offset + limit]
    return [legacy_viewer._trade_to_viewer(trade) for trade in paged_trades]


def _normalize_indicator_payloads(indicators: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_indicator in indicators:
        if hasattr(raw_indicator, "to_dict"):
            indicator = raw_indicator.to_dict()
        elif isinstance(raw_indicator, dict):
            indicator = dict(raw_indicator)
        else:
            indicator = None
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


def _replay_timeframe(result: BacktestResult) -> str:
    """First configured timeframe for a result, normalized (default "1h")."""
    configured = result.meta.data.get("timeframes")
    raw = configured[0] if configured else "1h"
    return normalize_timeframe(str(raw or "1h"))


def _load_symbol_ohlcv_with_details(
    symbol: str, timeframe: str, result: BacktestResult
) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
    """Load a symbol frame, or the user-facing missing-data details for it."""
    df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
    if df is None or len(df) == 0:
        return None, legacy_viewer._missing_ohlcv_details(
            symbol,
            timeframe,
            backtest_start=str(result.meta.backtest_start or ""),
            backtest_end=str(result.meta.backtest_end or ""),
        )
    return df, None


def _detect_indicators_for_result(result: BacktestResult) -> dict[str, Any]:
    """Indicator specs detected from the result's first strategy, if any."""
    strategy_identifier = result.meta.strategies[0] if result.meta.strategies else ""
    return legacy_viewer._detect_indicators(strategy_identifier)


def _slice_replay_ohlcv(df: pd.DataFrame, trade_payloads: list[dict[str, Any]]) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    keep_mask = _replay_keep_mask_from_trades(df, trade_payloads)
    if keep_mask.size != len(df) or np.all(keep_mask):
        return df
    # reset_index so downstream consumers (indicator computation) never
    # confuse the preserved original labels with positional row indexes.
    return df.iloc[np.flatnonzero(keep_mask)].reset_index(drop=True)


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


@app.get("/api/backtest/{backtest_id}/trade/{trade_nr}")
async def get_trade_with_context(backtest_id: str, trade_nr: int) -> dict[str, Any]:
    return await asyncio.to_thread(_trade_context_payload, backtest_id, trade_nr)


def _resolve_trade(result: BacktestResult, trade_nr: int) -> tuple[Any, dict[str, Any]]:
    """Locate a trade on the result and build its viewer payload."""
    trade = next((item for item in result.trades if item.id == trade_nr), None)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade #{trade_nr} not found")
    return trade, legacy_viewer._trade_to_viewer(trade)


@dataclass(frozen=True)
class _TradeWindow:
    """OHLCV window around a trade's entry bar plus its source-frame bounds."""

    ohlcv: list[dict[str, Any]]
    df: pd.DataFrame
    start_idx: int
    end_idx: int


def _trade_window_ohlcv(result: BacktestResult, trade: Any) -> _TradeWindow:
    """OHLCV window around a trade's entry bar, plus the source frame and bounds."""
    timeframe = _replay_timeframe(result)
    symbol = str(trade.symbol or "").strip() or "SPY"
    df, details = _load_symbol_ohlcv_with_details(symbol, timeframe, result)
    if df is None:
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
    else:
        ohlcv = []
    return _TradeWindow(ohlcv=ohlcv, df=df, start_idx=ohlcv_start_idx, end_idx=ohlcv_end_idx)


def _trade_context_indicators(
    result: BacktestResult, window: _TradeWindow
) -> list[dict[str, Any]]:
    """Indicators for the trade window: payload ones, else auto-detected ones."""
    indicators = _normalize_indicator_payloads(list(result.indicators))
    if indicators or not window.ohlcv:
        return indicators

    detected = _detect_indicators_for_result(result)
    if not detected:
        return indicators

    ohlcv_times = [row["time"] for row in window.ohlcv]
    warmup_start = max(0, window.start_idx - 50)
    df_slice = window.df.iloc[warmup_start:window.end_idx].reset_index(drop=True)
    return legacy_viewer._compute_indicators(df_slice, detected, ohlcv_times)


def _trade_context_payload(backtest_id: str, trade_nr: int) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    try:
        trade, trade_payload = _resolve_trade(result, trade_nr)
        symbol = str(trade.symbol or "").strip() or "SPY"
        window = _trade_window_ohlcv(result, trade)
        indicators = _trade_context_indicators(result, window)
        drawings = _collect_viewer_drawings(result, symbol)

        if window.ohlcv and indicators:
            time_start = window.ohlcv[0]["time"]
            time_end = window.ohlcv[-1]["time"]
            _slice_indicators_to_time_range(indicators, time_start=time_start, time_end=time_end)

        return {
            "trade": trade_payload,
            "ohlcv": window.ohlcv,
            "symbol": symbol,
            "timeframe": _replay_timeframe(result),
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


@app.get("/api/backtest/{backtest_id}/replay")
async def get_backtest_replay(backtest_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_replay_payload, backtest_id)


def _replay_symbols(result: BacktestResult) -> list[str]:
    """Symbols to load for a replay: from trades, else the configured ones."""
    from_trades = sorted({trade.symbol for trade in result.trades if trade.symbol})
    return from_trades or list(result.meta.data.get("symbols", []))


def _replay_ohlcv_by_symbol(
    result: BacktestResult, timeframe: str, trades: list[dict[str, Any]], symbols: list[str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Per-symbol replay OHLCV; symbols without data are reported separately."""
    ohlcv_by_symbol: dict[str, list[dict[str, Any]]] = {}
    missing_ohlcv: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        df, details = _load_symbol_ohlcv_with_details(symbol, timeframe, result)
        if df is None:
            missing_ohlcv[symbol] = details
            continue
        symbol_trades = [
            trade for trade in trades if str(trade.get("symbol") or "").upper() == symbol.upper()
        ]
        replay_df = _slice_replay_ohlcv(df, symbol_trades)
        ohlcv_by_symbol[symbol] = legacy_viewer._df_to_ohlcv_records(replay_df)
    return ohlcv_by_symbol, missing_ohlcv


def _replay_indicators(
    result: BacktestResult,
    timeframe: str,
    trades: list[dict[str, Any]],
    ohlcv_by_symbol: dict[str, list[dict[str, Any]]],
    primary_symbol: str,
) -> list[dict[str, Any]]:
    """Indicators for the replay: payload ones, else auto-detected on the primary symbol."""
    indicators = _normalize_indicator_payloads(list(result.indicators))
    if indicators:
        return indicators

    detected = _detect_indicators_for_result(result)
    if not detected:
        return indicators

    df = legacy_viewer._load_ohlcv_df(primary_symbol, timeframe)
    if df is None or len(df) == 0:
        return indicators

    primary_trades = [
        trade for trade in trades if str(trade.get("symbol") or "").upper() == primary_symbol.upper()
    ]
    replay_df = _slice_replay_ohlcv(df, primary_trades)
    ohlcv_times = [row["time"] for row in ohlcv_by_symbol[primary_symbol]]
    return legacy_viewer._compute_indicators(replay_df, detected, ohlcv_times)


def _replay_payload(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    try:
        trades = [legacy_viewer._trade_to_viewer(trade) for trade in result.trades]
        timeframe = _replay_timeframe(result)
        symbols = _replay_symbols(result)

        ohlcv_by_symbol, missing_ohlcv = _replay_ohlcv_by_symbol(
            result, timeframe, trades, symbols
        )
        if not ohlcv_by_symbol:
            detail = next(iter(missing_ohlcv.values()), {"message": "No OHLCV data available"})
            raise HTTPException(status_code=404, detail=detail)

        primary_symbol = next(iter(ohlcv_by_symbol))
        indicators = _replay_indicators(result, timeframe, trades, ohlcv_by_symbol, primary_symbol)

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
        logger.exception("Trade replay payload failed for backtest '%s': %s", _log_safe(backtest_id), exc)
        raise HTTPException(status_code=500, detail="Failed to load trade replay") from exc


@app.get("/api/backtest/{backtest_id}/ohlcv")
async def get_backtest_ohlcv(backtest_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_backtest_ohlcv_payload, backtest_id)


def _backtest_ohlcv_payload(backtest_id: str) -> dict[str, Any]:
    result = _load_result(backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)

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


@app.get("/api/compare")
async def compare_backtests_endpoint(ids: str = Query(default="")) -> dict[str, Any]:
    return await asyncio.to_thread(_compare_backtests_payload, ids)


def _compare_backtests_payload(ids: str) -> dict[str, Any]:
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
                        "winning_trades": sum(
                            1 for trade in result.trades if legacy_viewer._trade_net_pnl(trade) > 0
                        ),
                        "losing_trades": sum(
                            1 for trade in result.trades if legacy_viewer._trade_net_pnl(trade) <= 0
                        ),
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
    winning_trades = sum(
        1 for trade in result.trades if legacy_viewer._trade_net_pnl(trade) > 0
    )
    losing_trades = max(len(result.trades) - winning_trades, 0)

    return {
        "id": backtest_id,
        "name": display_name,
        "strategy_name": str(result.meta.strategies[0] if result.meta.strategies else ""),
        "engine": str(result.meta.engine or "unknown"),
        "symbols": symbols,
        "timeframe": timeframe,
        "stats": {
            "total_return": round(_safe_float(raw.get("total_return"), 0.0), 6),
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


def _run_backtest_process(
    *,
    task_id: str,
    handle: Any,
    cmd: list[str],
    env: dict[str, str],
    output_path: str,
    backtest_id: str,
    report_stage: Callable[..., None],
    watchdog_timed_out: threading.Event,
) -> tuple[Any, str, str, str]:
    """Launch the runner process and drain its stdout.

    Returns ``(proc, latest_message, output_path, backtest_id)``.
    """
    command_display = redact_text(" ".join(str(part) for part in cmd))
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
        # Leads its own process group so _kill_process_group can signal the
        # whole tree without touching this process (see its docstring).
        start_new_session=True,
    )
    _update_task_fields(task_id, pid=proc.pid, process=proc)

    # Watchdog bounds the whole child lifetime: it fires even if a hung child
    # never closes stdout (a post-EOF proc.wait() timeout would never cover
    # that case). It must start BEFORE the stdout pump below — the pump only
    # returns at EOF, so starting it after would never watch a hung child.
    watchdog = threading.Thread(
        target=_monitor_backtest_process,
        args=(proc,),
        kwargs={
            "timeout_seconds": BACKTEST_WATCHDOG_TIMEOUT_SECONDS,
            "timed_out": watchdog_timed_out,
        },
        daemon=True,
        name=f"backtest-watchdog-{task_id}",
    )
    watchdog.start()

    latest_message = ""
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
            outcome = _consume_backtest_line(
                line,
                output_path=output_path,
                backtest_id=backtest_id,
            )
            if outcome is None:
                continue
            parsed_message, stage, output_path, backtest_id = outcome
            latest_message = parsed_message
            if stage is not None:
                percent, message = stage
                report_stage(handle, percent=percent, message=message)
            if output_path or backtest_id:
                _update_task_fields(
                    task_id,
                    output_path=output_path or None,
                    backtest_id=backtest_id or None,
                )
    return proc, latest_message, output_path, backtest_id


def _prepare_backtest_runner_data(
    *,
    task_id: str,
    handle: Any,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    report_stage: Callable[..., None],
    report_download_phase: Callable[[float, str], None],
) -> None:
    """Ensure OHLCV availability, then report the running-backtest stage."""
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


def _finalize_backtest_run(
    *,
    task_id: str,
    handle: Any,
    returncode: int,
    output_path: str,
    backtest_id: str,
    latest_message: str,
    report_stage: Callable[..., None],
) -> None:
    """Record completion or failure of a backtest runner process."""
    if returncode != 0:
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
        return

    report_stage(handle, percent=96.0, message=SAVING_RESULTS_MESSAGE)
    result_payload = {
        "output_path": output_path or None,
        "backtest_id": backtest_id or None,
    }
    compact_result = {key: value for key, value in result_payload.items() if value}
    final_trade_count = _count_backtest_trades(backtest_id, latest_message)
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


def _consume_backtest_line(
    line: str,
    *,
    output_path: str,
    backtest_id: str,
) -> tuple[str, tuple[float, str] | None, str, str] | None:
    """Parse one runner stdout line.

    Returns ``(message, stage, output_path, backtest_id)`` or ``None`` when the
    line carries no usable message.
    """
    parsed_message = _backtest_runner_log_message(line)
    if not parsed_message:
        return None
    stage = _backtest_runner_stage(parsed_message)
    if parsed_message.lower().startswith("saved to:"):
        output_path, backtest_id = _update_saved_backtest(
            parsed_message[len(SAVED_TO_PREFIX):].strip(),
            output_path,
            backtest_id,
        )
    return parsed_message, stage, output_path, backtest_id


def _backtest_runner_log_message(raw_line: str) -> str:
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


def _backtest_runner_stage(message: str) -> tuple[float, str] | None:
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
    if normalized.startswith(SAVED_TO_PREFIX):
        return (96.0, SAVING_RESULTS_MESSAGE)
    return None


@app.post("/api/backtests/run", status_code=202)
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
    data_source = str(payload.data_source or "").strip()
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

    _register_job(
        task_id,
        {
            "status": "queued",
            "result": None,
            "error": None,
            "task_id": task_id,
            "job_id": task_id,
        },
    )
    restart_config = {
        "strategy": strategy,
        "engine": engine,
        "symbols": symbols,
        "timeframe": timeframe,
        "data_source": data_source,
        "start": start,
        "end": end,
        "capital": capital,
        "risk_per_trade_pct": risk_pct,
        "max_position_pct": max_position_pct,
        "max_leverage": max_leverage,
        "modeling_resolution": payload.modeling_resolution,
        "intrabar_resolution": payload.intrabar_resolution,
        "parameters": dict(params),
    }
    _update_task_fields(
        task_id,
        restart_config=restart_config,
        task_spec={
            "version": _RECOVERY_SPEC_VERSION,
            "kind": "backtest",
            "operation": "run",
            "parameters": restart_config,
        },
    )



    def _build_command(env: dict[str, str]) -> list[str]:
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
            *([] if not data_source else ["--data-source", data_source]),
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
        return cmd

    def _run() -> None:
        env = build_backtest_child_env()
        cmd = _build_command(env)

        latest_message = ""
        output_path = ""
        backtest_id = ""
        stage_progress = 0.0
        watchdog_timed_out = threading.Event()

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
                _set_job_status(task_id, "running")
                report_stage(handle, percent=10.0, message="Loading market data...")

                _prepare_backtest_runner_data(
                    task_id=task_id,
                    handle=handle,
                    symbols=symbols,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    report_stage=report_stage,
                    report_download_phase=report_download_phase,
                )

                proc, latest_message, output_path, backtest_id = _run_backtest_process(
                    task_id=task_id,
                    handle=handle,
                    cmd=cmd,
                    env=env,
                    output_path=output_path,
                    backtest_id=backtest_id,
                    report_stage=report_stage,
                    watchdog_timed_out=watchdog_timed_out,
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
                        outcome = _consume_backtest_line(
                            line,
                            output_path=output_path,
                            backtest_id=backtest_id,
                        )
                        if outcome is None:
                            continue
                        parsed_message, stage, output_path, backtest_id = outcome
                        latest_message = parsed_message
                        if stage is not None:
                            percent, message = stage
                            report_stage(handle, percent=percent, message=message)

                        if parsed_message.lower().startswith("saved to:"):
                            output_path = parsed_message[len(SAVED_TO_PREFIX):].strip()
                            if output_path:
                                filename = Path(output_path).name
                                if filename.endswith(BACKTEST_TRADES_SUFFIX):
                                    filename_id = filename[: -len(BACKTEST_TRADES_SUFFIX)]
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

                # The watchdog owns the child-lifetime deadline; this wait only
                # collects the exit status after EOF (or after the kill).
                try:
                    returncode = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    if proc.poll() is None:
                        watchdog_timed_out.set()
                        _kill_process_group(proc)
                    raise subprocess.SubprocessError(
                        f"Backtest timed out after {BACKTEST_WATCHDOG_TIMEOUT_SECONDS:.0f} seconds"
                    )
                if watchdog_timed_out.is_set():
                    # The watchdog killed the child, so its returncode is the
                    # signal that terminated it (-9), not a real exit status.
                    raise subprocess.SubprocessError(
                        f"Backtest timed out after {BACKTEST_WATCHDOG_TIMEOUT_SECONDS:.0f} seconds"
                    )
                if returncode == 0:
                    report_stage(handle, percent=96.0, message=SAVING_RESULTS_MESSAGE)
                    result_payload = {
                        "output_path": output_path or None,
                        "backtest_id": backtest_id or None,
                    }
                    compact_result = {key: value for key, value in result_payload.items() if value}
                    final_trade_count = _count_backtest_trades(backtest_id, latest_message)
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
                    _finish_job(task_id, status="completed", result=compact_result, error=None)
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
                    _finish_job(task_id, status="failed", result=None, error=error_text)
            except Exception as exc:
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
                _finish_job(task_id, status="failed", result=None, error=str(exc))
            finally:
                _update_task_fields(task_id, process=None)

    thread = threading.Thread(target=_run, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "job_id": task_id, "status": "queued"}


def _validate_signal_processor_name(signal_processor: Any) -> str:
    """Normalize the requested signal processor; raises 400 when unknown."""
    signal_processor_name = str(signal_processor or "").strip().lower()
    if signal_processor_name:
        try:
            get_signal_processor(signal_processor_name, {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    return signal_processor_name


def _validate_positive_payload_float(value: Any, *, default: float, field: str) -> float:
    """Normalize an optional positive float payload field; raises 400 when invalid."""
    normalized = float(value) if value is not None else default
    if normalized <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be > 0")
    return normalized


async def _collect_source_signals(
    *,
    normalized_source: str,
    account_id: str,
    start_dt: datetime,
    end_dt: datetime,
    default_timeframe: str,
) -> tuple[list[dict[str, Any]], str]:
    """Load replay signals from a live signal source (discord/internal)."""
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
                    dict(row.get("metadata")) if isinstance(row.get("metadata"), dict) else {}
                ),
            }
        )
    return normalized_signals, normalized_source


def _log_safe(value: object) -> str:
    """Sanitize user-controlled values before writing them to logs (S5145)."""
    text = str(value)
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _internal_error_detail(exc: Exception, operation: str) -> str:
    error_id = uuid.uuid4().hex
    logger.exception(
        "%s failed",
        operation,
        extra={"data": {"operation": operation, "error_id": error_id, "error": str(exc)}},
    )
    return f"Internal server error (error_id={error_id})"


def _public_value_error_detail(exc: Exception) -> str:
    return redact_text(str(exc))[:300]


def _validated_signal_replay_request(
    payload: BacktestRunSignalsRequest,
) -> tuple[
    list[dict[str, Any]], str, str, str, str, float, float, int
]:
    """Validate a run-signals request; returns the normalized request fields."""
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
    signal_processor_name = _validate_signal_processor_name(payload.signal_processor)
    if not direct_signals_payload and not normalized_source:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'signals' or 'signal_source'",
        )

    risk_per_trade_pct = _validate_positive_payload_float(
        payload.risk_per_trade_pct, default=0.01, field="risk_per_trade_pct"
    )
    max_position_pct = _validate_positive_payload_float(
        payload.max_position_pct, default=0.05, field="max_position_pct"
    )
    max_positions = int(payload.max_positions or 5)
    if max_positions <= 0:
        raise HTTPException(status_code=400, detail="max_positions must be > 0")
    return (
        direct_signals_payload,
        source_name,
        normalized_source,
        account_id,
        signal_processor_name,
        risk_per_trade_pct,
        max_position_pct,
        max_positions,
    )


def _register_signal_replay_task(
    *,
    payload: BacktestRunSignalsRequest,
    symbols: list[str],
    normalized_signals: list[dict[str, Any]],
    replay_source_label: str,
    account_id: str,
    signal_processor_name: str,
    risk_per_trade_pct: float,
    max_position_pct: float,
    max_positions: int,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[str, Any, int]:
    """Create the replay task record, restart config and job placeholder."""
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
            "start_date": start_dt.date().isoformat(),
            "end_date": end_dt.date().isoformat(),
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
    return task_id, log_file, stage_total


async def _gather_replay_signals(
    *,
    payload_signals: list[dict[str, Any]],
    source_name: str,
    normalized_source: str,
    account_id: str,
    start_dt: datetime,
    end_dt: datetime,
    default_timeframe: str,
) -> tuple[list[dict[str, Any]], str]:
    """Collect, normalize and validate the signals to replay; returns (signals, source)."""
    if payload_signals:
        normalized_signals: list[dict[str, Any]] = [
            _normalize_replay_signal_payload(item, default_timeframe=default_timeframe)
            for item in payload_signals
        ]
        replay_source_label = source_name or str(
            normalized_signals[0].get("source") if normalized_signals else "manual"
        )
        replay_source_label = replay_source_label.strip().lower() or "manual"
    else:
        normalized_signals, replay_source_label = await _collect_source_signals(
            normalized_source=normalized_source,
            account_id=account_id,
            start_dt=start_dt,
            end_dt=end_dt,
            default_timeframe=default_timeframe,
        )
        replay_source_label = replay_source_label or normalized_source

    normalized_signals = [
        item
        for item in normalized_signals
        if str(item.get("symbol") or "").strip()
        and str(item.get("action") or "") in {"buy", "sell"}
        and _safe_float(item.get("entry_price"), 0.0) > 0
    ]
    if not normalized_signals:
        raise HTTPException(status_code=400, detail="No valid signals to replay")
    return normalized_signals, replay_source_label


@app.post("/api/backtests/run-signals", status_code=202)
async def run_backtest_signals(payload: BacktestRunSignalsRequest) -> dict[str, Any]:
    requested_timeframe = normalize_timeframe(str(payload.timeframe or "").strip()) or ""
    start_dt = _parse_request_date(payload.start_date, field_name="start_date")
    end_dt = _parse_request_date(payload.end_date, field_name="end_date", end_of_day=True)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    (
        direct_signals_payload,
        source_name,
        normalized_source,
        account_id,
        signal_processor_name,
        risk_per_trade_pct,
        max_position_pct,
        max_positions,
    ) = _validated_signal_replay_request(payload)

    default_timeframe = requested_timeframe or "1h"
    normalized_signals, replay_source_label = await _gather_replay_signals(
        payload_signals=direct_signals_payload,
        source_name=source_name,
        normalized_source=normalized_source,
        account_id=account_id,
        start_dt=start_dt,
        end_dt=end_dt,
        default_timeframe=default_timeframe,
    )
    timeframe = _resolve_signal_replay_timeframe(
        requested_timeframe=requested_timeframe,
        normalized_signals=normalized_signals,
        fallback=default_timeframe,
    )

    symbols = sorted({str(item["symbol"]).strip().upper() for item in normalized_signals})
    start_str = start_dt.date().isoformat()
    end_str = end_dt.date().isoformat()
    symbols = sorted({str(item["symbol"]).strip().upper() for item in normalized_signals})
    task_id, log_file, stage_total = _register_signal_replay_task(
        payload=payload,
        symbols=symbols,
        normalized_signals=normalized_signals,
        replay_source_label=replay_source_label,
        account_id=account_id,
        signal_processor_name=signal_processor_name,
        risk_per_trade_pct=risk_per_trade_pct,
        max_position_pct=max_position_pct,
        max_positions=max_positions,
        timeframe=timeframe,
        start_dt=start_dt,
        end_dt=end_dt,
    )
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
    _register_job(
        task_id,
        {
            "status": "queued",
            "result": None,
            "error": None,
            "task_id": task_id,
            "job_id": task_id,
        },
    )

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
                _set_job_status(task_id, "running")
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

                report_stage(handle, percent=96.0, message=SAVING_RESULTS_MESSAGE)
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
                _finish_job(task_id, status="completed", result=compact_result, error=None)
            except Exception as exc:
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
                _finish_job(task_id, status="failed", result=None, error=str(exc))

    thread = threading.Thread(target=_run_signal_replay, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "job_id": task_id, "status": "queued"}


def _replay_signal_payload(
    signal: dict[str, Any], signal_time: pd.Timestamp, source_label: str, file_path: str
) -> dict[str, Any]:
    """Normalise one raw CSV signal row into the replay payload shape."""
    action = str(signal.get("action") or "").strip().lower()
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    metadata = dict(signal.get("metadata") or {})
    metadata["signal_source_label"] = f"CSV: {Path(file_path).name}"
    metadata["signal_filepath"] = str(Path(file_path).expanduser())
    return {
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


async def _load_and_filter_csv_signals(
    file_path: str, start_dt: datetime, end_dt: datetime
) -> tuple[list[dict[str, Any]], list[SignalReplayInput]]:
    """Load a contained signal CSV and keep only replayable rows in range."""
    try:
        loaded_signals = await asyncio.to_thread(load_signals_from_csv, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Signal CSV not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc

    if not loaded_signals:
        raise HTTPException(status_code=400, detail="CSV file contains no replayable signals")

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
        if _safe_float(signal.get("entry_price"), 0.0) <= 0:
            continue

        signal_payload = _replay_signal_payload(signal, signal_time, source_label, file_path)
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
                metadata=dict(signal_payload["metadata"]),
            )
        )
    return filtered_signals, signal_inputs


@app.post(
    "/api/backtests/run-from-signals",
    status_code=202,
    responses={
        400: {"description": "Invalid signal_source, filepath, date range or no replayable signals"},
        403: {"description": "Signal CSV is outside the OHLCV data directory"},
    },
)
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

    csv_path = _contained_client_data_file(file_path, allowed_suffix=".csv")
    if csv_path is None:
        raise HTTPException(
            status_code=403,
            detail="Signal CSV must be inside the OHLCV data directory",
        )
    file_path = str(csv_path)

    start_dt = _parse_request_date(payload.start_date, field_name="start_date")
    end_dt = _parse_request_date(payload.end_date, field_name="end_date", end_of_day=True)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    filtered_signals, signal_inputs = await _load_and_filter_csv_signals(
        file_path, start_dt, end_dt
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


@app.get("/api/backtests/run/{job_id}/status")
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

    legacy_viewer.evict_jobs()
    job = legacy_viewer.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/optimizations/run", status_code=202)
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


@app.get("/api/optimizations")
async def list_optimizations() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not OPTIMIZATIONS_DIR.exists():
        return results

    for path in sorted(OPTIMIZATIONS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results.append(_optimization_summary_payload(payload, path))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not read optimization %s: %s", path, exc)

    results.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    return results


@app.get("/api/optimizations/{optimization_id}")
async def get_optimization(optimization_id: str) -> dict[str, Any]:
    try:
        payload = _load_optimization_payload(optimization_id)
    except (OSError, TypeError, ValueError) as exc:
        logger.exception("Failed to load optimization '%s': %s", _log_safe(optimization_id), exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "optimization load")) from exc

    if payload is None:
        raise HTTPException(status_code=404, detail=f"Optimization '{optimization_id}' not found")
    return payload


@app.get("/api/portfolios/compare")
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


@app.get("/api/portfolios")
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


@app.get("/api/portfolios/duplicates")
async def list_portfolio_duplicates() -> PortfoliosDuplicatesResponse:
    groups, total_portfolios = _group_duplicate_portfolios()
    return PortfoliosDuplicatesResponse(
        groups=groups,
        total_groups=len(groups),
        total_portfolios=total_portfolios,
    )


@app.post("/api/portfolios/deduplicate")
async def deduplicate_portfolios() -> PortfoliosDeduplicateResponse:
    groups, _ = _group_duplicate_portfolios()
    deleted_ids: list[str] = []
    kept_ids: list[str] = []
    items: list[dict[str, Any]] = []
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
            result = _delete_portfolio_with_artifacts(portfolio_id)
            items.append(result)
            if result.get("status") == "deleted":
                deleted_ids.append(portfolio_id)
                removed_in_group = True
        if removed_in_group:
            affected += 1

    return PortfoliosDeduplicateResponse(
        deleted_ids=deleted_ids,
        kept_ids=kept_ids,
        groups_affected=affected,
        items=items,
    )


@app.post("/api/portfolios/remove-empty")
async def remove_empty_portfolios() -> dict[str, Any]:
    portfolios = _snapshot_portfolio_payloads()
    removed_ids: list[str] = []
    items: list[dict[str, Any]] = []
    for portfolio in portfolios:
        portfolio_id = str(portfolio.get("id") or "").strip()
        components = portfolio.get("components")
        has_components = isinstance(components, list) and len(components) > 0
        if not portfolio_id or has_components:
            continue
        result = _delete_portfolio_with_artifacts(portfolio_id)
        items.append(result)
        if result.get("status") == "deleted":
            removed_ids.append(portfolio_id)

    remaining = len(_snapshot_portfolio_payloads())
    return {
        "removed": len(removed_ids),
        "remaining": remaining,
        "removed_ids": removed_ids,
        "items": items,
    }


def _weighted_trade_value(value: Any, weight: float) -> float | None:
    if value is None:
        return None
    return round(_safe_float(value, 0.0) * weight, 6)


def _build_portfolio_components_from_backtests(
    component_payloads: list[dict[str, Any]],
    *,
    account_capital: float,
) -> tuple[list[dict[str, Any]], list[pd.Series], list[dict[str, Any]]]:
    """Load each component backtest and derive its returns, trades and stats."""
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
                    "net_pnl": _weighted_trade_value(viewer_trade.get("net_pnl"), weight),
                    "gross_pnl": _weighted_trade_value(viewer_trade.get("gross_pnl"), weight),
                    "fees": _weighted_trade_value(viewer_trade.get("fees"), weight),
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
    return components, component_returns, all_trades_timed


def _portfolio_combined_metrics(
    component_returns: list[pd.Series],
    *,
    account_capital: float,
    now: datetime,
) -> tuple[dict[str, Any], list[legacy_viewer.EquityPoint], dict[str, Any]]:
    """Combine component return streams into portfolio metrics and equity curve."""
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

    return correlation_matrix, combined_equity_points, stats_result


@app.post("/api/portfolios", status_code=201)
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

    components, component_returns, all_trades_timed = _build_portfolio_components_from_backtests(
        component_payloads,
        account_capital=account_capital,
    )

    if not component_returns and not allows_empty_portfolio:
        raise HTTPException(status_code=400, detail="No valid components found")

    now = datetime.now(timezone.utc)
    correlation_matrix, combined_equity_points, stats_result = _portfolio_combined_metrics(
        component_returns,
        account_capital=account_capital,
        now=now,
    )

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


@app.post("/api/portfolios/delete-bulk")
async def delete_portfolios_bulk(payload: PortfoliosBulkDeleteRequest) -> dict[str, Any]:
    deleted = 0
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for raw_id in payload.ids:
        portfolio_id = str(raw_id or "").strip()
        if not portfolio_id or portfolio_id in seen:
            continue
        seen.add(portfolio_id)
        result = _delete_portfolio_with_artifacts(portfolio_id)
        items.append(result)
        if result.get("status") == "deleted":
            deleted += 1
    return {"deleted": deleted, "items": items}


@app.get("/api/portfolios/{portfolio_id}")
async def get_portfolio(portfolio_id: str) -> dict[str, Any]:
    payload = _load_portfolio_payload(portfolio_id)
    payload["combined_stats"] = legacy_viewer.PortfolioStats.from_dict(
        payload.get("combined_stats", {})
    ).to_dict()
    return payload


@app.patch("/api/portfolios/{portfolio_id}")
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
        logger.exception("Failed to rename portfolio '%s': %s", _log_safe(portfolio_id), exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "portfolio rename")) from exc

    _upsert_portfolio_meta_cache_from_payload(normalized_portfolio)
    return {"id": portfolio_id, "name": updated_name, "status": "updated"}


@app.put("/api/portfolios/{portfolio_id}/name")
async def rename_portfolio_name(portfolio_id: str, payload: NameUpdateRequest) -> dict[str, Any]:
    return await rename_portfolio(portfolio_id, payload)


@app.get("/api/portfolios/{portfolio_id}/contributions")
async def get_portfolio_contributions(
    portfolio_id: str,
    group_by: str = Query(default="component"),
) -> list[dict[str, Any]]:
    portfolio = _load_portfolio_payload(portfolio_id)
    return _compute_portfolio_contributions(portfolio=portfolio, group_by=group_by)


@app.get("/api/portfolios/{portfolio_id}/trades")
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


@app.delete("/api/portfolios/{portfolio_id}")
async def delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    result = _delete_portfolio_with_artifacts(portfolio_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if result.get("status") == "cleanup_pending":
        raise HTTPException(
            status_code=503,
            detail={"error": "Portfolio deletion is pending cleanup", "result": result},
        )
    if result.get("status") != "deleted":
        raise HTTPException(
            status_code=500,
            detail={"error": "Portfolio deletion failed", "result": result},
        )
    return result


def _execution_risk_value(
    row: dict[str, Any], metadata: dict[str, Any], field: str
) -> float | None:
    """Prefer the execution row's risk value; fall back to signal metadata."""
    row_value = row.get(field)
    if row_value is not None:
        return _safe_float(row_value)
    metadata_value = metadata.get(field)
    if metadata_value is not None:
        return _safe_float(metadata_value)
    return None


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
                "position_size": _execution_risk_value(row, metadata, "position_size"),
                "risk_amount": _execution_risk_value(row, metadata, "risk_amount"),
                "risk_percent": _execution_risk_value(row, metadata, "risk_percent"),
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


@app.get("/api/live/accounts")
async def get_live_accounts() -> list[dict[str, Any]]:
    _log_live_endpoint_request("/api/live/accounts")
    discovered = await asyncio.to_thread(_cached_live_accounts_payload)
    # Persist what discovery just learned. The guarded account re-point and the
    # ACTIVE verification-freshness gate both operate on stored account rows, so
    # an account that was only ever seen in a discovery response cannot be a
    # valid deployment target.
    #
    # A read must never demote a safety flag. `is_demo` is only forwarded when
    # discovery positively asserted it; an unasserted flag becomes None, which
    # `upsert_live_accounts` treats as "say nothing". Coercing a missing field
    # to False here is what silently disarmed accounts: the stored flag flipped
    # to non-demo while `demo_verified_at` stayed fresh, the adapter was torn
    # down, and no later read could ever put it back.
    persisted: list[LiveAccountConfig] = []
    for item in discovered:
        broker = str(item.get("broker") or "").strip().lower()
        account_id = str(item.get("id") or "").strip()
        if broker not in {"alpaca", "ctrader"} or not account_id:
            continue
        raw_is_demo = item.get("is_demo")
        asserted = bool(item.get("is_demo_asserted", isinstance(raw_is_demo, bool)))
        persisted.append(
            LiveAccountConfig(
                id=account_id,
                broker=broker,
                name=str(item.get("name") or account_id),
                broker_login=str(item.get("broker_login") or item.get("login") or "").strip()
                or None,
                ctid=str(item.get("ctid") or "").strip() or None,
                is_demo=bool(raw_is_demo) if asserted and isinstance(raw_is_demo, bool) else None,
            )
        )
    if persisted:
        await asyncio.to_thread(upsert_live_accounts, persisted)
    return discovered


@app.get("/api/services")
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


@app.post("/api/services", status_code=201)
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
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    return _serialize_service_payload(created)


@app.get("/api/services/{service_id}")
async def get_service_endpoint(service_id: str) -> dict[str, Any]:
    service = await asyncio.to_thread(get_service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail=SERVICE_NOT_FOUND_DETAIL)
    return _serialize_service_payload(service)


@app.put("/api/services/{service_id}/status")
async def update_service_status_endpoint(
    service_id: str,
    payload: ServiceStatusUpdateRequest,
) -> dict[str, Any]:
    try:
        updated = await asyncio.to_thread(update_service_status, service_id, payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=SERVICE_NOT_FOUND_DETAIL)
    return _serialize_service_payload(updated)


@app.delete("/api/services/{service_id}")
async def delete_service_endpoint(service_id: str) -> dict[str, Any]:
    deleted = await asyncio.to_thread(delete_service, service_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=SERVICE_NOT_FOUND_DETAIL)
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


def _log_entry_level_search_predicate(
    level: str,
    search: str,
) -> Callable[[str, dict[str, Any] | None], bool] | None:
    level_filter = legacy_viewer._normalize_log_levels(level)
    query = search.lower() if search else ""
    if not level_filter and not query:
        return None

    def matches(safe_line: str, payload: dict[str, Any] | None) -> bool:
        if query and query not in safe_line.lower():
            return False
        if not level_filter:
            return True
        entry_level = legacy_viewer._extract_log_level(safe_line, payload)
        return entry_level in level_filter

    return matches


@app.get("/api/services/{service_id}/logs")
async def get_service_logs_endpoint(
    service_id: str,
    lines: int = Query(default=200, ge=1, le=100000),
    level: str = Query(default=""),
    search: str = Query(default=""),
) -> LogContentModel:
    service = await asyncio.to_thread(get_service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail=SERVICE_NOT_FOUND_DETAIL)
    try:
        entries = legacy_viewer._collect_log_entries(
            legacy_viewer._available_log_files(),
            predicate=_log_entry_level_search_predicate(level, search),
        )
        service_terms = _service_log_tokens(service)
        entries = [
            entry
            for entry in entries
            if _entry_matches_service_log(entry, service=service, tokens=service_terms)
        ]
        total_lines = len(entries)
        response_limit = min(lines, _TASK_LOG_RESPONSE_MAX_LINES)
        recent_entries = entries[-response_limit:] if len(entries) > response_limit else entries
        return LogContentModel(
            name=f"service:{service.name}",
            lines=[entry["line"] for entry in recent_entries],
            total_lines=total_lines,
        )
    except Exception as exc:
        logger.exception("Failed to load service logs for %s: %s", service_id, exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "service logs")) from exc


@app.get("/api/verification/evidence")
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
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc


@app.get("/api/verification/evidence/summary")
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
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc


@app.post(
    "/api/verification/evidence",

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
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc


async def _resolve_live_deployment_signal_source(
    *,
    signal_source_type: str,
    strategy_name: str,
    symbols: list[str],
    symbol_filter: list[str],
    signal_source_id: str,
    signal_config_payload: dict[str, Any],
) -> tuple[str, list[str], str, list[str], dict[str, Any]]:
    """Validate/normalize the signal-source inputs for a live deployment."""
    if signal_source_type == "internal":
        if not strategy_name:
            raise HTTPException(status_code=400, detail="strategy_name is required for internal signal source")
        if not symbols:
            raise HTTPException(status_code=400, detail="At least one symbol is required")
        return "", [], strategy_name, symbols, signal_config_payload

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
    return signal_source_id, symbol_filter, strategy_name, symbols, signal_config_payload


@app.get(LIVE_DEPLOYMENTS_PATH)
async def get_live_deployments() -> list[dict[str, Any]]:
    _log_live_endpoint_request(LIVE_DEPLOYMENTS_PATH)
    deployments = await asyncio.to_thread(list_live_deployments)
    return [_serialize_live_deployment_payload(deployment) for deployment in deployments]


@app.get("/api/live/dashboard")
async def get_live_dashboard() -> dict[str, Any]:
    _log_live_endpoint_request("/api/live/dashboard")
    accounts, deployments = await asyncio.gather(
        asyncio.to_thread(list_live_accounts),
        asyncio.to_thread(list_live_deployments),
    )
    account_payloads: list[dict[str, Any]] = []
    active_strategies: list[dict[str, Any]] = []
    recent_fills: list[dict[str, Any]] = []
    total_equity = 0.0
    active_deployments = [
        deployment for deployment in deployments if str(deployment.status).upper() == "ACTIVE"
    ]

    # Fetch stats and trades for all accounts concurrently instead of
    # serially awaiting a full stats+trades scan per account.
    stats_list = await asyncio.gather(
        *(
            asyncio.to_thread(_live_account_stats_payload, account.id)
            for account in accounts
        )
    )
    trades_payloads = await asyncio.gather(
        *(_live_trades_payload(account.id) for account in accounts)
    )

    for account, stats, trades_payload in zip(accounts, stats_list, trades_payloads):
        equity = _safe_float(stats.get("current_equity"), 0.0) or 0.0
        total_equity += equity
        account_payloads.append(
            {
                **_serialize_live_account_payload(account),
                "current_equity": equity,
                "current_balance": _safe_float(stats.get("current_balance"), 0.0),
                "total_trades": stats.get("total_trades", 0),
                "total_pnl": stats.get("total_pnl", 0.0),
            }
        )
        recent_fills.extend(trades_payload.get("trades", [])[:10])

    for deployment in active_deployments:
        account_equity = next(
            (
                float(item.get("current_equity") or 0.0)
                for item in account_payloads
                if item.get("id") == deployment.account_id
            ),
            0.0,
        )
        allocation = (
            account_equity / total_equity
            if total_equity > 0 and account_equity > 0
            else 0.0
        )
        active_strategies.append(
            {
                **_serialize_live_deployment_payload(deployment),
                "capital_allocation": round(allocation, 6),
                "account_equity": round(account_equity, 2),
            }
        )

    recent_fills.sort(key=lambda item: str(item.get("execution_timestamp") or ""), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": account_payloads,
        "active_strategies": active_strategies,
        "capital": {
            "total_equity": round(total_equity, 2),
            "active_account_count": len(account_payloads),
            "active_strategy_count": len(active_strategies),
        },
        "recent_fills": recent_fills[:25],
    }


@app.post("/api/live/deployments/ctrader/top5")
async def deploy_top_ctrader_strategies(payload: CTraderTopDeployRequest) -> dict[str, Any]:
    limit = max(1, min(int(payload.limit or 5), 20))
    status = "ACTIVE" if payload.activate else "PAUSED"
    _log_live_endpoint_request(
        "/api/live/deployments/ctrader/top5",
        limit=limit,
        status=status,
        dry_run=payload.dry_run,
    )
    backtests, accounts = await asyncio.gather(
        asyncio.to_thread(list_backtest_metadata_rows),
        asyncio.to_thread(list_live_accounts),
    )
    plan = build_ctrader_deployment_plan(
        backtests=backtests,
        accounts=accounts,
        limit=limit,
        status=status,
    )
    if not plan:
        raise HTTPException(
            status_code=400,
            detail="No eligible cTrader account or backtest candidates found",
        )

    if payload.dry_run:
        return {
            "status": "planned",
            "dry_run": True,
            "deployments": [_serialize_live_deployment_payload(item) for item in plan],
        }

    created: list[dict[str, Any]] = []
    for deployment in plan:
        try:
            created_deployment = await asyncio.to_thread(create_live_deployment, deployment)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
        created.append(_serialize_live_deployment_payload(created_deployment))
    return {
        "status": "created",
        "dry_run": False,
        "deployments": created,
    }


@app.post(LIVE_DEPLOYMENTS_PATH, status_code=201)
async def create_live_deployment_endpoint(
    payload: LiveDeploymentCreateRequest,
) -> dict[str, Any]:
    signal_source_type = str(
        payload.signal_source_type or payload.signal_source or "internal"
    ).strip().lower()
    _log_live_endpoint_request(
        LIVE_DEPLOYMENTS_PATH,
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
            raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
        signal_config_payload["signal_processor"] = signal_processor_name

    (
        signal_source_id,
        symbol_filter,
        strategy_name,
        symbols,
        signal_config_payload,
    ) = await _resolve_live_deployment_signal_source(
        signal_source_type=signal_source_type,
        strategy_name=strategy_name,
        symbols=symbols,
        symbol_filter=symbol_filter,
        signal_source_id=signal_source_id,
        signal_config_payload=signal_config_payload,
    )

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
        validate_stockkit_deployment_route(
            deployment,
            # Routing compares BROKER identity, not the local account id. Without
            # this the validator has nothing to compare against and fails closed,
            # so every Discord create was rejected.
            account_broker_login=str(
                account_payload.get("broker_login")
                or account_payload.get("ctid")
                or ""
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    try:
        created = await asyncio.to_thread(create_live_deployment, deployment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    return _serialize_live_deployment_payload(created)


class LiveSubmissionDrainOutcome(str, Enum):
    """What a PAUSE can honestly claim about orders that were already at the broker."""

    #: Every lease held when the status changed was released before its TTL.
    DRAINED = "DRAINED"
    #: A dispatcher still holds a live lease, so an order may still be in flight.
    IN_FLIGHT = "IN_FLIGHT"
    #: A lease expired without being released, so whether its order landed is
    #: unknown. Never reported as a clean drain.
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class LiveSubmissionDrainResult:
    outcome: LiveSubmissionDrainOutcome
    in_flight: int
    expired_unreleased: int

    @property
    def complete(self) -> bool:
        return self.outcome is LiveSubmissionDrainOutcome.DRAINED


async def _drain_live_deployment_submissions(
    deployment_id: str,
    *,
    timeout_seconds: float = LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS,
    initial_claims: Sequence[LiveDeploymentSubmissionClaim] | None = None,
) -> LiveSubmissionDrainResult:
    """Wait out the submission leases held when a status change was applied.

    The budget defaults to :data:`LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS`, which is
    derived from the claim TTL so the drain can always outlast the lease it is
    waiting on. A drain that gave up before the lease expired would report "in
    flight" for perfectly healthy submissions.

    ``initial_claims`` is the inventory taken *before* the status write and seeds
    the ledger. Without it there is a window in which a lease could expire and be
    reaped by a later claim attempt before the first poll ever saw it, and the
    drain would then report a clean stop for a submission nobody observed. Seeds
    and observations together cover every lease: a lease taken after the snapshot
    is seen by the poll loop, and one that existed at the snapshot is accounted
    for by the ledger.

    The verdict is deliberately one-directional. ``DRAINED`` is returned only
    when every lease seen during the drain was still within its TTL at the
    moment it disappeared, i.e. it was released by a live claimor. A lease that
    was ever observed expired, or that vanished only after its TTL had passed, is
    reported as ``INDETERMINATE`` instead: the row disappearing proves nothing,
    because the reaper in ``claim_live_deployment_submission`` deletes exactly the
    same rows. Under-reporting progress is safe here; under-reporting the doubt
    is not, so an ambiguous end state can never be reported as a clean stop.
    Exactly one lease can exist per deployment (``UNIQUE(deployment_id)`` on the
    claim table), so a lease that is still live is the only thing worth waiting
    for.
    """
    deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.0)
    expired_seen: set[str] = set()
    leases: dict[str, datetime] = {}
    for claim in initial_claims or ():
        leases[claim.token] = claim.expires_at
        if claim.expired:
            expired_seen.add(claim.token)
    while True:
        observed = await asyncio.to_thread(
            list_live_deployment_submission_claims, deployment_id
        )
        present: dict[str, datetime] = {}
        for claim in observed:
            present[claim.token] = claim.expires_at
            if claim.expired:
                expired_seen.add(claim.token)
        # A lease that vanished after its own TTL lapsed was either reaped or
        # released too late to tell apart, so it can no longer vouch for a stop.
        for token, expires_at in leases.items():
            if token not in present and utcnow_naive() >= expires_at:
                expired_seen.add(token)
        leases = present

        if not present:
            if expired_seen:
                return LiveSubmissionDrainResult(
                    outcome=LiveSubmissionDrainOutcome.INDETERMINATE,
                    in_flight=0,
                    expired_unreleased=len(expired_seen),
                )
            return LiveSubmissionDrainResult(
                outcome=LiveSubmissionDrainOutcome.DRAINED,
                in_flight=0,
                expired_unreleased=0,
            )
        # A lease that has already outlived its TTL settles the verdict on its
        # own: waiting cannot help, because a claimor that releases it later is
        # still indistinguishable from the reaper deleting it. Say so now rather
        # than holding the operator's request open for the whole budget.
        if all(token in expired_seen for token in present):
            return LiveSubmissionDrainResult(
                outcome=LiveSubmissionDrainOutcome.INDETERMINATE,
                in_flight=0,
                expired_unreleased=len(expired_seen),
            )
        if asyncio.get_running_loop().time() >= deadline:
            if expired_seen:
                return LiveSubmissionDrainResult(
                    outcome=LiveSubmissionDrainOutcome.INDETERMINATE,
                    in_flight=len([t for t in present if t not in expired_seen]),
                    expired_unreleased=len(expired_seen),
                )
            return LiveSubmissionDrainResult(
                outcome=LiveSubmissionDrainOutcome.IN_FLIGHT,
                in_flight=len(present),
                expired_unreleased=0,
            )
        await asyncio.sleep(0.05)


async def _reverify_account_against_broker(account_id: str) -> bool:
    """Ask the broker whether ``account_id`` is still a demo account.

    Returns True only when discovery positively reports the account as demo and
    the fresh verification is persisted. Any error, missing account, or non-demo
    answer returns False so the caller fails closed.
    """
    try:
        discovered = await asyncio.to_thread(_discover_live_accounts_payload)
    except Exception as exc:
        logger.error(
            "Broker re-verification failed; refusing activation",
            extra={"data": {"account_id": account_id, "error": repr(exc)}},
        )
        return False

    for account in discovered:
        if str(account.get("id") or "") != str(account_id):
            continue
        if not bool(account.get("is_demo")):
            logger.error(
                "Broker reports account is not a demo account; refusing activation",
                extra={"data": {"account_id": account_id}},
            )
            return False
        return True

    logger.error(
        "Broker discovery did not return the account; refusing activation",
        extra={"data": {"account_id": account_id}},
    )
    return False


@app.put("/api/live/deployments/{deployment_id}/status")
async def update_live_deployment_status_endpoint(
    deployment_id: str, payload: LiveDeploymentStatusUpdateRequest
) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/deployments/{deployment_id}/status",
        deployment_id=deployment_id,
        status=payload.status,
    )
    requested_status = str(payload.status or "").strip().upper()
    pending_claims: list[LiveDeploymentSubmissionClaim] = []

    if requested_status == "ACTIVE":
        # Re-verify the account against the broker immediately before arming.
        # The DB gate also enforces verification freshness, so a stale
        # confirmation can never be reused to arm a deployment.
        deployment = await asyncio.to_thread(get_live_deployment, deployment_id)
        if deployment is None:
            raise HTTPException(status_code=404, detail="Live deployment not found")
        if not await _reverify_account_against_broker(deployment.account_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Broker could not confirm the account is a demo account; "
                    "refusing to activate"
                ),
            )

    # Inventory the leases to account for *before* the write, so a lease that is
    # reaped in the window between the commit and the drain's first poll is still
    # part of the ledger and cannot be mistaken for "nothing was in flight".
    if requested_status != "ACTIVE":
        pending_claims = await asyncio.to_thread(
            list_live_deployment_submission_claims, deployment_id
        )

    try:
        updated = await asyncio.to_thread(
            update_live_deployment_status, deployment_id, payload.status
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Live deployment not found")
    payload = _serialize_live_deployment_payload(updated)
    if updated.status != "ACTIVE":
        # The status write above has already committed, and it is what stops new
        # claims: `claim_live_deployment_submission` only inserts while the row
        # reads ACTIVE. The pause therefore *has* taken effect by the time we get
        # here, and the drain is an observation of orders that were already at
        # the broker, not a precondition for the change. Reporting a failure
        # here would tell an operator that a stop which succeeded did not happen,
        # and their obvious response - clicking ACTIVE - re-arms the bot through
        # a fresh broker verification. So the response states what is true: the
        # new status is in effect, plus an explicit, non-clean outcome for the
        # submissions that were already in flight.
        drain = await _drain_live_deployment_submissions(
            deployment_id, initial_claims=pending_claims
        )
        payload["submission_drain_outcome"] = drain.outcome.value
        payload["submission_drain_complete"] = drain.complete
        payload["in_flight_submissions"] = drain.in_flight
        payload["expired_unreleased_submissions"] = drain.expired_unreleased
        payload["status_change_applied"] = True
        if drain.outcome is LiveSubmissionDrainOutcome.IN_FLIGHT:
            payload["warning"] = (
                f"Status is now {updated.status}, but {drain.in_flight} "
                "submission(s) were already in flight when the change was applied. "
                "The new status blocks any further orders; the in-flight "
                "submission(s) may still reach the broker. Verify the broker "
                "before resuming."
            )
        elif drain.outcome is LiveSubmissionDrainOutcome.INDETERMINATE:
            payload["warning"] = (
                f"Status is now {updated.status}, but {drain.expired_unreleased} "
                "submission lease(s) expired without being released, so whether "
                "those orders reached the broker is unknown. Verify the broker "
                "before resuming."
            )
        if not drain.complete:
            logger.error(
                "Live deployment status persisted; submission drain did not "
                "complete cleanly",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "status": updated.status,
                        "submission_drain_outcome": drain.outcome.value,
                        "in_flight_submissions": drain.in_flight,
                        "expired_unreleased_submissions": drain.expired_unreleased,
                    }
                },
            )
    return payload


@app.put("/api/live/deployments/{deployment_id}/account")
async def update_live_deployment_account_endpoint(
    deployment_id: str, payload: LiveDeploymentAccountUpdateRequest
) -> dict[str, Any]:
    """Re-point a deployment at a different broker account.

    Guarded on the DB side: the target must be a known, verified demo account,
    the deployment must not be ACTIVE unless explicitly overridden, and any
    in-flight submissions must have drained first.
    """
    _log_live_endpoint_request(
        "/api/live/deployments/{deployment_id}/account",
        deployment_id=deployment_id,
    )
    try:
        updated = await asyncio.to_thread(
            update_live_deployment_account,
            deployment_id,
            payload.account_id,
            allow_active=bool(payload.allow_active),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_public_value_error_detail(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Live deployment not found")
    return _serialize_live_deployment_payload(updated)


@app.delete("/api/live/deployments/{deployment_id}")
async def delete_live_deployment_endpoint(deployment_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/deployments/{deployment_id}",
        deployment_id=deployment_id,
    )
    deleted = await asyncio.to_thread(delete_live_deployment, deployment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Live deployment not found")
    return {"status": "deleted", "id": deployment_id}


@app.get("/api/live/balances/{account_id}")
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


@app.get("/api/live/trades/{account_id}")
async def get_live_trades(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/trades/{account_id}",
        account_id=account_id,
    )
    return await _live_trades_payload(account_id)


@app.get("/api/live/signals/{account_id}")
async def get_live_signals(account_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/signals/{account_id}",
        account_id=account_id,
        limit=limit,
    )
    return await _live_signals_payload(account_id, limit=limit)


@app.get("/api/live/stats/{account_id}")
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


@app.get("/api/live/quotes/{symbol}")
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


@app.get("/api/live/positions/{account_id}")
async def get_live_positions(account_id: str) -> dict[str, Any]:
    _log_live_endpoint_request(
        "/api/live/positions/{account_id}",
        account_id=account_id,
    )
    return await _live_positions_payload(account_id)


def _data_overview_payload() -> dict[str, Any]:
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


@app.get("/api/data/overview")
async def data_overview() -> dict[str, Any]:
    return await asyncio.to_thread(_data_overview_payload)


def _data_category_payload(category_name: str) -> dict[str, Any]:
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


@app.get("/api/data/category/{category_name}")
async def data_category_details(category_name: str) -> dict[str, Any]:
    return await asyncio.to_thread(_data_category_payload, category_name)


@app.post("/api/data/cache/invalidate", response_model=dict[str, Any])
async def invalidate_data_cache(
    reason: str = Query(default="manual"),
    include_validation: bool | None = Query(default=None),
) -> dict[str, Any]:
    return _manual_cache_invalidation_payload(
        reason=reason,
        include_validation=include_validation,
    )


@dataclass(frozen=True)
class _PreparedDownloadRequest:
    source: str
    symbols: str
    timeframes: str
    start: str
    end: str
    offer_side: str
    merge_tick: bool
    merge_s1: bool
    batch_size: int
    pause_ms: int
    query_timeout_seconds: int
    symbol_list: list[str]
    timeframe_list: list[str]


def _resolve_download_offer_side(payload: DataDownloadRequest) -> str:
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
    return offer_side


def _validate_download_limits(request: _PreparedDownloadRequest) -> None:
    if request.batch_size < 1 or request.batch_size > 50:
        raise HTTPException(status_code=400, detail="batch_size must be between 1 and 50")
    if request.pause_ms < 0 or request.pause_ms > 60000:
        raise HTTPException(status_code=400, detail="pause_ms must be between 0 and 60000")
    if request.source in {"dukascopy", "jforex"} and (
        request.query_timeout_seconds < 30
        or request.query_timeout_seconds > legacy_viewer.MAX_DATA_QUERY_TIMEOUT_SECONDS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "query_timeout_seconds must be between 30 and "
                f"{int(legacy_viewer.MAX_DATA_QUERY_TIMEOUT_SECONDS)}"
            ),
        )


def _validate_download_genesis(request: _PreparedDownloadRequest) -> None:
    if request.source not in {"dukascopy", "jforex"} or not request.start:
        return

    genesis_index = _load_dukascopy_genesis()
    too_early: dict[str, str] = {}
    for symbol in request.symbol_list:
        symbol_data = genesis_index.get(symbol, {})
        for timeframe in request.timeframe_list:
            genesis = symbol_data.get(timeframe)
            if genesis and request.start < genesis:
                too_early[f"{symbol}/{timeframe}"] = genesis
    if not too_early:
        return

    details = ", ".join(
        f"{symbol_tf} >= {genesis}" for symbol_tf, genesis in sorted(too_early.items())
    )
    raise HTTPException(
        status_code=400,
        detail=f"Start date {request.start} precedes Dukascopy genesis for: {details}",
    )


def _prepare_download_request(payload: DataDownloadRequest) -> _PreparedDownloadRequest:
    request = _PreparedDownloadRequest(
        source=str(payload.source or "dukascopy").strip().lower(),
        symbols=payload.symbols,
        timeframes=payload.timeframes,
        start=payload.start,
        end=payload.end,
        offer_side=_resolve_download_offer_side(payload),
        merge_tick=bool(payload.merge_tick or payload.merge_bid_ask),
        merge_s1=bool(payload.merge_s1 or payload.merge_bid_ask),
        batch_size=_safe_int(
            payload.batch_size if payload.batch_size is not None else payload.concurrency, 10
        ),
        pause_ms=_safe_int(payload.pause_ms, 1000),
        query_timeout_seconds=_safe_int(payload.query_timeout_seconds, 900),
        symbol_list=_parse_symbol_list(payload.symbols),
        timeframe_list=[
            _normalize_dukascopy_genesis_timeframe(timeframe)
            for timeframe in _parse_timeframe_list(payload.timeframes)
        ],
    )
    _validate_download_limits(request)
    _validate_download_genesis(request)
    return request


def _download_task_spec(
    request: _PreparedDownloadRequest, *, source: str | None = None
) -> dict[str, Any]:
    return {
        "version": _RECOVERY_SPEC_VERSION,
        "kind": "data",
        "operation": "download",
        "source": str(source or request.source).strip().lower(),
        "symbols": list(request.symbol_list),
        "timeframes": list(request.timeframe_list),
        "start": request.start or None,
        "end": request.end or None,
        "offer_side": request.offer_side,
        "merge_tick": request.merge_tick,
        "merge_s1": request.merge_s1,
        "batch_size": request.batch_size,
        "pause_ms": request.pause_ms,
        "query_timeout_seconds": request.query_timeout_seconds,
    }


def _jforex_download_command(request: _PreparedDownloadRequest) -> list[str]:
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "data_tools.downloaders.jforex_dl",
        "--symbols",
        ",".join(request.symbol_list),
        "--timeframes",
        ",".join(request.timeframe_list),
        "--query-timeout-seconds",
        str(request.query_timeout_seconds),
    ]
    if request.start:
        cmd.extend(["--start", request.start])
    if request.end:
        cmd.extend(["--end", request.end])
    if request.offer_side:
        cmd.extend(["--price-type", request.offer_side])
    return cmd


def _start_jforex_download(
    task_id: str,
    request: _PreparedDownloadRequest,
    *,
    name: str,
    description: str,
) -> dict[str, Any]:
    source_label = "dukascopy" if request.source == "dukascopy" else "jforex"
    cmd = _jforex_download_command(request)
    result = _start_jforex_task(
        task_id,
        cmd,
        source_label=source_label,
        allow_native_fallback=(source_label == "dukascopy"),
        symbols=request.symbol_list,
        timeframes=request.timeframe_list,
        start=request.start or None,
        end=request.end or None,
        offer_side=request.offer_side,
        merge_tick=request.merge_tick,
        merge_s1=request.merge_s1,
        batch_size=request.batch_size,
        pause_ms=request.pause_ms,
        query_timeout_seconds=request.query_timeout_seconds,
        total_items=max(len(request.symbol_list) * len(request.timeframe_list), 1),
        name=name,
        description=description,
    )
    if not result.get("coalesced"):
        _apply_recovery_data_metadata(
            task_id,
            command=[str(part) for part in cmd],
            downloader=source_label,
            task_spec=_download_task_spec(request, source=source_label),
            symbols=request.symbol_list,
            timeframes=request.timeframe_list,
            start=request.start or None,
            end=request.end or None,
            offer_side=request.offer_side,
            price_type=request.offer_side,
            merge_tick=request.merge_tick,
            merge_s1=request.merge_s1,
            batch_size=request.batch_size,
            pause_ms=request.pause_ms,
            query_timeout_seconds=request.query_timeout_seconds,
        )
    return result


def _generic_download_command(request: _PreparedDownloadRequest) -> list[str]:
    module_names = {
        "yfinance": "data_tools.downloaders.yfinance_dl",
        "huggingface": "data_tools.downloaders.huggingface_dl",
        "alpaca": "data_tools.downloaders.alpaca_dl",
    }
    if request.source == "dolthub":
        return [sys.executable, "-u", "-m", "data_tools.dolthub_manager", "update"]
    if request.source not in module_names:
        raise HTTPException(status_code=400, detail="Unknown source")

    cmd = [
        sys.executable,
        "-u",
        "-m",
        module_names[request.source],
        "--symbols",
        request.symbols,
        "--timeframes",
        request.timeframes,
    ]
    if request.start:
        cmd.extend(["--start", request.start])
    if request.end:
        cmd.extend(["--end", request.end])
    return cmd


def _start_generic_download(
    task_id: str,
    request: _PreparedDownloadRequest,
    *,
    name: str,
    description: str,
) -> dict[str, Any]:
    cmd = _generic_download_command(request)
    result = _start_data_task(
        task_id,
        cmd,
        total_items=max(len(request.symbol_list) * len(request.timeframe_list), 1),
        name=name,
        description=description,
        source=request.source,
    )
    if not result.get("coalesced"):
        _apply_recovery_data_metadata(
            task_id,
            command=[str(part) for part in cmd],
            downloader=request.source,
            task_spec=_download_task_spec(request),
            symbols=request.symbol_list,
            timeframes=request.timeframe_list,
            start=request.start or None,
            end=request.end or None,
        )
    return result


@app.post(
    "/api/data/download",
    responses={400: {"description": "Invalid download request"}},
)
async def data_download(payload: DataDownloadRequest) -> dict[str, Any]:
    request = _prepare_download_request(payload)
    task_id = str(uuid.uuid4())
    name, description = _download_task_labels(
        request.source, request.symbol_list, request.timeframe_list
    )
    if request.source in {"dukascopy", "jforex"}:
        return _start_jforex_download(task_id, request, name=name, description=description)
    return _start_generic_download(task_id, request, name=name, description=description)


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


@app.post("/api/agent/convert", status_code=202)
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


@app.get("/api/data/dukascopy/genesis")
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


@app.get("/api/data/check")
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


@app.post(
    "/api/data/resample",
    responses={400: {"description": "source_path is not a parquet file in the data directory"}},
)
async def data_resample(payload: DataResampleRequest) -> dict[str, Any]:
    target_timeframes = payload.target_timeframes
    source_path = _contained_client_data_file(
        payload.source_path, allowed_suffix=OHLCV_PARQUET_SUFFIX
    )
    if source_path is None:
        raise HTTPException(
            status_code=400,
            detail="source_path must be a parquet file inside the OHLCV data directory",
        )
    task_id = str(uuid.uuid4())
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "data_tools.resample",
        "--source",
        str(source_path),
        "--timeframes",
        target_timeframes,
    ]
    result = _start_data_task(
        task_id,
        cmd,
        total_items=1,
        name="Resample data",
        description=f"Resample {source_path.name or source_path} into {target_timeframes}.",
        source="resample",
    )
    _apply_recovery_data_metadata(
        task_id,
        command=[str(part) for part in cmd],
        downloader="resample",
        task_spec={
            "version": _RECOVERY_SPEC_VERSION,
            "kind": "data",
            "operation": "resample",
            "source_path": str(source_path),
            "target_timeframes": [
                item.strip().lower()
                for item in str(target_timeframes).split(",")
                if item.strip()
            ],
        },
    )
    return result


@app.post("/api/data/harvest")
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
        task_spec={
            "version": _RECOVERY_SPEC_VERSION,
            "kind": "data",
            "operation": "harvest",
            "source": "dukascopy",
            "symbols": [symbol],
            "asset_class": asset_class or None,
            "start": start or None,
        },
        symbols=[symbol],
        start=start or None,
    )
    return result


@app.post("/api/data/validate")
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
        task_spec={
            "version": _RECOVERY_SPEC_VERSION,
            "kind": "data",
            "operation": "validate",
            "symbol": str(payload.symbol).strip().upper() if payload.symbol else None,
            "report": str(payload.report).strip() if payload.report else None,
        },
        symbols=[str(payload.symbol).strip().upper()] if payload.symbol else None,
    )
    return result


@app.post("/api/data/calibrate")
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
        task_spec={
            "version": _RECOVERY_SPEC_VERSION,
            "kind": "data",
            "operation": "calibrate",
            "source": "dukascopy",
            "symbols": [s.strip().upper() for s in symbols.split(",") if s.strip()],
            "timeframes": (
                [tf.strip().lower() for tf in str(payload.timeframes).split(",") if tf.strip()]
                if payload.timeframes
                else []
            ),
            "price_type": str(payload.price_type or "bid").strip().lower(),
            "start_year": int(payload.start_year),
            "min_year": int(payload.min_year),
            "force": bool(payload.force),
            "verbose": bool(payload.verbose),
        },
        symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()],
        timeframes=(
            [tf.strip().lower() for tf in str(payload.timeframes).split(",") if tf.strip()]
            if payload.timeframes
            else None
        ),
    )
    return result


@app.get("/api/data/tasks")
async def list_data_tasks() -> dict[str, Any]:
    await asyncio.to_thread(_hydrate_task_cache_once)

    with DATA_TASKS_LOCK:
        snapshots = [(task_id, dict(task)) for task_id, task in DATA_TASKS.items()]
    payload: list[dict[str, Any]] = []
    for task_id, task in snapshots:
        status = _task_status_or_manual(task.get("status"))
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


@app.get("/api/tasks")
async def list_tasks() -> dict[str, Any]:
    return await list_data_tasks()


@app.get("/api/data/tasks/{task_id}")
async def get_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    task = _snapshot_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    return _serialize_task(task_id, task, include_log=True)


@app.post("/api/data/tasks/{task_id}/restart")
async def restart_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    return await asyncio.to_thread(_restart_interrupted_task, task_id)


@app.post("/api/data/tasks/{task_id}/pause")
async def pause_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be paused")
    if _task_status_or_manual(task.get("status")) in FINAL_TASK_STATUSES:
        # Answer honestly: a finished task cannot be paused. Returning 200 here
        # reported success for a no-op while `can_pause` was already false.
        raise HTTPException(
            status_code=409,
            detail=f"Task already finished (status={task.get('status')!r})",
        )
    # Only a task that is actually pending or executing can be paused. Without
    # this guard an already paused task was silently "paused" again, so the UI
    # advertised can_pause for a task the endpoint would not act on.
    if _task_status_or_manual(task.get("status")) not in PAUSABLE_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task cannot be paused from status {task.get('status')!r}",
        )

    task["pause_event"].set()
    if task.get("control_mode") == "process":
        stop_signal = getattr(signal, "SIGSTOP", None)
        if stop_signal is not None:
            _signal_process(task, stop_signal)
    _set_task_status(task_id, "paused")
    _task_log_message(task_id, "Task paused by user.")
    return _task_action_payload(task_id)


@app.post("/api/data/tasks/{task_id}/resume")
async def resume_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be resumed")
    # Only a paused task can be resumed. Previously the endpoint only
    # short-circuited on final statuses, so POSTing resume against a `starting`
    # or `queued` task promoted it to `running`: the task was reported as
    # executing when it had not begun, and its elapsed/ETA figures were wrong.
    # A finished task cannot be resumed either, so it gets the same honest 409
    # rather than a 200 for a no-op that `can_resume` already hid.
    if _task_status_or_manual(task.get("status")) != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"Task is not paused (status={task.get('status')!r})",
        )

    task["pause_event"].clear()
    if task.get("control_mode") == "process":
        continue_signal = getattr(signal, "SIGCONT", None)
        if continue_signal is not None:
            _signal_process(task, continue_signal)
    _set_task_status(task_id, "running")
    _task_log_message(task_id, "Task resumed by user.")
    return _task_action_payload(task_id)


@app.post("/api/data/tasks/{task_id}/cancel")
async def cancel_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    task = _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
    if not task.get("supports_control", False):
        raise HTTPException(status_code=409, detail="Task cannot be cancelled")
    if _task_status_or_manual(task.get("status")) in FINAL_TASK_STATUSES:
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


@app.get("/api/data/tasks/{task_id}/logs")
async def get_data_task_logs(task_id: str) -> dict[str, Any]:
    task_id, task, log_file = _task_for_log_access(task_id)
    return {
        "logs": _read_task_log(log_file, max_bytes=64 * 1024),
        "status": _task_status_or_manual(task.get("status")),
    }


@app.delete("/api/data/tasks/{task_id}")
async def delete_data_task(task_id: str) -> dict[str, Any]:
    task_id = _validated_task_id(task_id)
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=TASK_NOT_FOUND_DETAIL)
        if _task_status_or_manual(task.get("status")) not in FINAL_TASK_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Only completed, failed, or cancelled tasks can be removed",
            )
        del DATA_TASKS[task_id]
    await asyncio.to_thread(delete_background_task_record, task_id)
    return {"task_id": task_id, "status": "deleted"}


@app.delete(
    "/api/data/symbol",
    responses={
        400: {"description": "Path query parameter is missing"},
        403: {"description": "Path is outside the OHLCV data directory or is not a parquet file"},
    },
)
async def delete_symbol_data(path: str = Query(default="")) -> dict[str, Any]:
    if not path:
        raise HTTPException(status_code=400, detail="Path required")

    # Containment must be checked on resolved paths (prefix strings can be
    # spoofed with ".." components) and deletion is restricted to the parquet
    # files the catalog manages.
    file_path = _contained_client_data_file(path, allowed_suffix=OHLCV_PARQUET_SUFFIX)
    if file_path is None:
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        cache_manager.invalidate_cache(DATA_DIR, reason="manual")
        _invalidate_data_views_cache()
        _refresh_catalog()
        return {"status": "deleted"}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "data deletion")) from exc


@app.get("/api/logs")
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


@app.get("/api/logs/{log_name}")
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
        entries = legacy_viewer._collect_log_entries(
            paths,
            predicate=_log_entry_level_search_predicate(level, search),
        )
        if log_name == ALL_LOGS_NAME:
            entries.sort(
                key=lambda item: (
                    item["timestamp"] is None,
                    item["timestamp"] or datetime.max.replace(tzinfo=timezone.utc),
                    item["path"],
                    item["line_number"],
                )
            )

        total_lines = len(entries)
        response_limit = min(lines, _TASK_LOG_RESPONSE_MAX_LINES)
        recent_entries = entries[-response_limit:] if len(entries) > response_limit else entries
        return LogContentModel(
            name=log_name,
            lines=[entry["line"] for entry in recent_entries],
            total_lines=total_lines,
        )
    except Exception as exc:
        logger.exception("Failed to read log '%s': %s", log_name, exc)
        raise HTTPException(status_code=500, detail=_internal_error_detail(exc, "log read")) from exc


def _symbols_payload() -> dict[str, Any]:
    return _get_data_views().get("symbols_payload", {"symbols": [], "categories": {}})


@app.get("/api/symbols", response_model=SymbolsResponse)
async def list_symbols() -> SymbolsResponse:
    payload = await asyncio.to_thread(_symbols_payload)
    return SymbolsResponse(
        symbols=list(payload.get("symbols", [])),
        categories=dict(payload.get("categories", {})),
    )


_BENCHMARK_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def _resample_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Resample an intraday frame to daily bars keyed by a `timestamp` column."""
    daily = (
        df.set_index("timestamp")
        .resample("1D")
        .agg(_BENCHMARK_AGG)
        .dropna()
        .reset_index()
    )
    return daily.rename(columns={"index": "timestamp"})


def _load_benchmark_daily_df(symbol: str, timeframe: str) -> tuple[pd.DataFrame | None, str]:
    """Frame for a benchmark request; intraday sources are resampled to 1d."""
    df = legacy_viewer._load_ohlcv_df(symbol, timeframe)
    if df is not None:
        return df, timeframe

    if timeframe != "1d":
        return None, timeframe

    for fallback_tf in ["4h", "1h", "15m"]:
        intraday = legacy_viewer._load_ohlcv_df(symbol, fallback_tf)
        if intraday is None:
            continue
        logger.info("Benchmark %s: using %s resampled to 1d", symbol, fallback_tf)
        return _resample_to_daily(intraday), "1d"
    return None, timeframe


def _filter_benchmark_df(
    df: pd.DataFrame, start: str, end: str
) -> pd.DataFrame:
    """Restrict a benchmark frame to the requested window."""
    if start and "timestamp" in df.columns:
        df = df[df["timestamp"] >= start]
    if end and "timestamp" in df.columns:
        df = df[df["timestamp"] <= end]
    return df


def _benchmark_equity_points(df: pd.DataFrame, initial: float = 100000.0) -> list[BenchmarkPoint]:
    """Buy-and-hold equity curve over the frame's closes."""
    first_close = float(df.iloc[0]["close"])
    shares = initial / first_close

    equity: list[BenchmarkPoint] = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        unix_ts = ts.timestamp() if hasattr(ts, "timestamp") else pd.Timestamp(ts).timestamp()
        equity.append(BenchmarkPoint(time=float(unix_ts), equity=round(float(row["close"]) * shares, 2)))
    return equity


def _benchmark_payload(
    symbol: str,
    start: str,
    end: str,
    timeframe: str,
) -> BenchmarkResponse:
    timeframe = normalize_timeframe(timeframe)
    df, timeframe = _load_benchmark_daily_df(symbol, timeframe)
    if df is None:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}/{timeframe}")

    df = _filter_benchmark_df(df, start, end)
    if df.empty:
        raise HTTPException(status_code=404, detail="No data in range")

    return BenchmarkResponse(
        symbol=symbol, timeframe=timeframe, equity=_benchmark_equity_points(df)
    )


@app.get("/api/benchmark/{symbol}")
async def get_benchmark(
    symbol: str,
    start: str = Query(default=""),
    end: str = Query(default=""),
    timeframe: str = Query(default="1d"),
) -> BenchmarkResponse:
    return await asyncio.to_thread(_benchmark_payload, symbol, start, end, timeframe)


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
    try:
        task_id, _task, log_path = _task_for_log_access(job_id)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    position = 0
    total_sent = 0
    pending = ""
    idle_cycles = 0

    try:
        while True:
            if log_path.is_file() and not log_path.is_symlink():
                try:
                    size = log_path.stat().st_size
                    if size < position:
                        position = 0
                    with log_path.open("rb") as handle:
                        handle.seek(position)
                        remaining = min(_TASK_LOG_WS_CHUNK_BYTES, _TASK_LOG_WS_MAX_BYTES - total_sent)
                        chunk = handle.read(max(0, remaining))
                        position = handle.tell()
                except OSError:
                    chunk = b""
                if chunk:
                    total_sent += len(chunk)
                    pending += chunk.decode("utf-8", errors="replace")
                    complete_lines = pending.splitlines(keepends=True)
                    if complete_lines and not complete_lines[-1].endswith(("\n", "\r")):
                        pending = complete_lines.pop()
                    else:
                        pending = ""
                    for raw_line in complete_lines:
                        await websocket.send_text(redact_text(raw_line.rstrip("\r\n")))
                idle_cycles = 0
            else:
                idle_cycles += 1

            status = _task_status(task_id)
            if status in FINAL_TASK_STATUSES:
                if pending:
                    await websocket.send_text(redact_text(pending))
                await websocket.send_text(f"[task:{task_id}] status={status}")
                await websocket.close()
                return

            if total_sent >= _TASK_LOG_WS_MAX_BYTES:
                await websocket.send_text("[task:log] output limit reached")
                await websocket.close(code=1009)
                return

            if idle_cycles > 120 and status is None:
                await websocket.send_text(f"[task:{task_id}] no log stream available")
                await websocket.close(code=1000)
                return

            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, OSError, ValueError):
        return




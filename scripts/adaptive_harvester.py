from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import lzma
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import aiohttp
import pandas as pd
from dotenv import load_dotenv

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism -
# and the reasoning - live in scripts/_import_origin.py; only the invocation is per
# script. Running this file directly puts scripts/ on sys.path[0], not the repo root,
# so `shared` never finds this checkout and the guard refuses (exit 2) rather than
# mutating the wrong tree. See tests/test_script_import_origin.py; run as:
#     python -m scripts.adaptive_harvester
if __package__ in (None, ""):
    # `python scripts/<name>.py`, or a loader that reads this file by path (e.g.
    # importlib's spec_from_file_location, which tests/test_script_hygiene.py uses): scripts/ is
    # not necessarily on sys.path, so make this file's own directory importable. scripts/ only -
    # never the repo root, which would repair `import shared` and defeat the guard.
    import sys as _sys
    from pathlib import Path as _path

    _scripts_dir = str(_path(__file__).resolve().parent)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    import _import_origin as _guard
else:  # `python -m scripts.<name>`: the repo root is on sys.path
    from scripts import _import_origin as _guard

if not _guard.is_own_checkout_file(_guard):
    _guard = _guard.load_sibling(__file__)
_guard.ensure_own_checkout("adaptive_harvester")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from data_tools.downloaders.dukascopy_native import (
    SYMBOL_CONFIG,
    _get_decimal_factor,
    decompress_bi5,
    generate_tick_urls,
    parse_ticks,
)
from shared.constants import resolve_genesis_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve()
    if _data_dir_env
    else (PROJECT_ROOT / "data").resolve()
)

_log_dir_env = os.environ.get("LOG_DIR")
LOG_DIR = Path(_log_dir_env).expanduser().resolve() if _log_dir_env else (Path.home() / "logs")
TASK_LOG_PATH = LOG_DIR / "tasks" / "task_harvester.log"

STATE_PATH = DATA_DIR / "harvester_state.json"
PROFILE_PATH = PROJECT_ROOT / "data_tools" / "config" / "downloader_profiles.json"

DEFAULT_BATCH_SIZE = 5
DEFAULT_DELAY_MS = 1000
MIN_BATCH_SIZE = 2
MAX_BATCH_SIZE = 20
MIN_DELAY_MS = 100
MAX_DELAY_MS = 5000
SUCCESS_STREAK_TARGET = 5
FAST_LATENCY_THRESHOLD_S = 2.0


@dataclass
class AdaptiveConfig:
    batch_size: int = DEFAULT_BATCH_SIZE
    delay_ms: int = DEFAULT_DELAY_MS
    success_streak: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "batch_size": self.batch_size,
            "delay_ms": self.delay_ms,
            "success_streak": self.success_streak,
        }


@dataclass
class FetchOutcome:
    dt: datetime
    data: bytes
    latency_s: float
    success: bool
    penalty: bool
    reason: str


@dataclass
class MonthResult:
    month_key: str
    rows: int
    file_path: Path
    files_per_sec: float
    duration_s: float
    successful_files: int
    total_files: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Expected YYYY-MM-DD.") from exc


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _month_ranges(start_inclusive: date, end_exclusive: date) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cursor = date(start_inclusive.year, start_inclusive.month, 1)
    while cursor < end_exclusive:
        nxt = _next_month(cursor)
        window_start = max(cursor, start_inclusive)
        window_end = min(nxt, end_exclusive)
        if window_start < window_end:
            ranges.append((window_start, window_end))
        cursor = nxt
    return ranges


def _month_key(symbol: str, month_start: date) -> str:
    return f"{symbol.upper()}_{month_start.year:04d}_{month_start.month:02d}"


def _month_label(month_start: date) -> str:
    return f"{month_start.year:04d}-{month_start.month:02d}"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("adaptive_harvester")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    TASK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(TASK_LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def _resolve_symbol_and_instrument(symbol: str, asset_class: str | None) -> tuple[str, str, str]:
    normalized_symbol = symbol.strip().upper()
    if normalized_symbol in SYMBOL_CONFIG:
        cfg = SYMBOL_CONFIG[normalized_symbol]
        resolved_asset_class = asset_class or cfg["asset_class"]
        return normalized_symbol, cfg["instrument"], resolved_asset_class

    cleaned = "".join(ch for ch in symbol.strip().lower() if ch.isalnum())
    if not cleaned:
        raise ValueError(f"Invalid symbol '{symbol}'")
    if not asset_class:
        raise ValueError(f"Unknown symbol '{symbol}'. Please pass --asset_class explicitly.")
    return normalized_symbol, cleaned, asset_class


def _resolve_start_date(symbol: str, explicit_start: str | None) -> date:
    if explicit_start:
        return _parse_iso_date(explicit_start)

    genesis_path = resolve_genesis_path(DATA_DIR)
    genesis = _load_json(genesis_path)
    symbol_entry = genesis.get(symbol.upper(), {})
    tick_start = symbol_entry.get("tick") or symbol_entry.get("default")
    if not tick_start:
        raise ValueError(
            f"No genesis tick date for {symbol}. "
            f"Provide --start explicitly or update {genesis_path}."
        )
    return _parse_iso_date(tick_start)


def _resolve_end_date_exclusive(explicit_end: str | None) -> date:
    if explicit_end:
        return _parse_iso_date(explicit_end)
    return datetime.now(timezone.utc).date() + timedelta(days=1)


def _load_state() -> dict[str, Any]:
    state = _load_json(STATE_PATH)
    if not state:
        return {"version": 1, "jobs": {}}
    jobs = state.get("jobs")
    if not isinstance(jobs, dict):
        raise RuntimeError(f"Invalid state format in {STATE_PATH}: missing 'jobs' dictionary.")
    return state


def _save_state(state: dict[str, Any]) -> None:
    _write_json_atomic(STATE_PATH, state)


def _ensure_job_state(
    state: dict[str, Any],
    symbol: str,
    asset_class: str,
    month_keys: list[str],
) -> tuple[str, dict[str, Any]]:
    jobs = state.setdefault("jobs", {})
    job_id = f"dukascopy:{asset_class}:{symbol}:tick"
    job = jobs.setdefault(
        job_id,
        {
            "symbol": symbol,
            "asset_class": asset_class,
            "timeframe": "tick",
            "months": {},
            "tuning": AdaptiveConfig().as_dict(),
            "month_stats": {},
            "updated_at": _utc_now(),
        },
    )

    months = job.setdefault("months", {})
    for key in month_keys:
        if key not in months:
            months[key] = "pending"

    for key, status in list(months.items()):
        if key in month_keys and status in {"in_progress", "failed"}:
            months[key] = "pending"

    tuning = job.setdefault("tuning", AdaptiveConfig().as_dict())
    tuning.setdefault("batch_size", DEFAULT_BATCH_SIZE)
    tuning.setdefault("delay_ms", DEFAULT_DELAY_MS)
    tuning.setdefault("success_streak", 0)
    job["updated_at"] = _utc_now()
    return job_id, job


def _looks_like_html(data: bytes) -> bool:
    if len(data) < 20:
        return False
    head = data[:100].lower()
    return b"<html" in head or b"<!doctype" in head


def _looks_invalid_bi5(data: bytes) -> bool:
    if not data or _looks_like_html(data):
        return True
    try:
        lzma.decompress(data)
    except lzma.LZMAError:
        return True
    return False


def _ram_gb() -> float:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return usage / (1024**3)
        return usage / (1024**2)
    except (ImportError, AttributeError, OSError, ValueError):
        return 0.0


def _load_profiles() -> dict[str, Any]:
    profiles = _load_json(PROFILE_PATH)
    if not profiles:
        return {}
    if not isinstance(profiles, dict):
        raise RuntimeError(f"Invalid profile format in {PROFILE_PATH}.")
    return profiles


def _save_profiles(profiles: dict[str, Any]) -> None:
    _write_json_atomic(PROFILE_PATH, profiles)


def _update_profile_if_better(
    symbol: str,
    asset_class: str,
    tuning: AdaptiveConfig,
    result: MonthResult,
    logger: logging.Logger,
) -> None:
    profiles = _load_profiles()
    profile_key = f"dukascopy:{asset_class}:{symbol}:tick"
    previous = profiles.get(profile_key, {})
    previous_fps = float(previous.get("files_per_sec", 0.0))
    if result.files_per_sec <= previous_fps:
        return

    profiles[profile_key] = {
        "symbol": symbol,
        "asset_class": asset_class,
        "timeframe": "tick",
        "batch_size": tuning.batch_size,
        "delay_ms": tuning.delay_ms,
        "files_per_sec": round(result.files_per_sec, 4),
        "sample_month": result.month_key,
        "updated_at": _utc_now(),
    }
    _save_profiles(profiles)
    logger.info(
        "Updated downloader profile %s: batch_size=%d delay_ms=%d files_per_sec=%.3f",
        profile_key,
        tuning.batch_size,
        tuning.delay_ms,
        result.files_per_sec,
    )


async def _fetch_url(
    session: aiohttp.ClientSession,
    dt: datetime,
    url: str,
    max_retries: int,
) -> FetchOutcome:
    for attempt in range(max_retries + 1):
        started = time.perf_counter()
        try:
            async with session.get(url) as response:
                data = await response.read()
                latency_s = time.perf_counter() - started
                status = response.status

                if status == 200:
                    if _looks_invalid_bi5(data):
                        weekday_penalty = dt.weekday() < 5
                        if weekday_penalty and attempt < max_retries:
                            await asyncio.sleep(2**attempt)
                            continue
                        reason = "empty_weekday" if weekday_penalty else "empty_weekend"
                        return FetchOutcome(
                            dt=dt,
                            data=b"",
                            latency_s=latency_s,
                            success=False,
                            penalty=weekday_penalty,
                            reason=reason,
                        )
                    return FetchOutcome(
                        dt=dt,
                        data=data,
                        latency_s=latency_s,
                        success=True,
                        penalty=False,
                        reason="ok",
                    )

                if status == 404:
                    weekday_penalty = dt.weekday() < 5
                    if weekday_penalty and attempt < max_retries:
                        await asyncio.sleep(2**attempt)
                        continue
                    reason = "not_found_weekday" if weekday_penalty else "not_found_weekend"
                    return FetchOutcome(
                        dt=dt,
                        data=b"",
                        latency_s=latency_s,
                        success=False,
                        penalty=weekday_penalty,
                        reason=reason,
                    )

                penalty = status == 429 or status == 403 or status >= 500
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                return FetchOutcome(
                    dt=dt,
                    data=b"",
                    latency_s=latency_s,
                    success=False,
                    penalty=penalty,
                    reason=f"http_{status}",
                )
        except asyncio.TimeoutError:
            latency_s = time.perf_counter() - started
            if attempt < max_retries:
                await asyncio.sleep(2**attempt)
                continue
            return FetchOutcome(
                dt=dt,
                data=b"",
                latency_s=latency_s,
                success=False,
                penalty=True,
                reason="timeout",
            )
        except aiohttp.ClientError:
            latency_s = time.perf_counter() - started
            if attempt < max_retries:
                await asyncio.sleep(2**attempt)
                continue
            return FetchOutcome(
                dt=dt,
                data=b"",
                latency_s=latency_s,
                success=False,
                penalty=True,
                reason="connection_drop",
            )

    return FetchOutcome(
        dt=dt,
        data=b"",
        latency_s=0.0,
        success=False,
        penalty=True,
        reason="unknown_fetch_failure",
    )


def _batch_all_successful(outcomes: list[FetchOutcome]) -> bool:
    acceptable = {"not_found_weekend", "empty_weekend"}
    return all(item.success or item.reason in acceptable for item in outcomes)


def _assemble_tick_dataframe(
    successful_results: list[tuple[datetime, bytes]],
    instrument: str,
) -> pd.DataFrame:
    decimal_factor = int(_get_decimal_factor(instrument))
    all_ticks: list[tuple[datetime, float, float, float, float]] = []
    for hour_start, data in sorted(successful_results, key=lambda item: item[0]):
        raw = decompress_bi5(data)
        if not raw:
            continue
        all_ticks.extend(parse_ticks(raw, hour_start, decimal_factor))

    if not all_ticks:
        return pd.DataFrame(columns=["timestamp", "bid", "ask", "bid_vol", "ask_vol"])

    frame = pd.DataFrame(
        all_ticks,
        columns=["timestamp", "ask", "bid", "ask_vol", "bid_vol"],
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return frame[["timestamp", "bid", "ask", "bid_vol", "ask_vol"]]


async def _download_month(
    symbol: str,
    asset_class: str,
    instrument: str,
    month_start: date,
    month_end_exclusive: date,
    tuning: AdaptiveConfig,
    logger: logging.Logger,
    max_retries: int,
    on_batch_complete: Callable[[AdaptiveConfig], None] | None = None,
) -> MonthResult:
    range_start = datetime(
        month_start.year,
        month_start.month,
        month_start.day,
        tzinfo=timezone.utc,
    )
    range_end = datetime(
        month_end_exclusive.year,
        month_end_exclusive.month,
        month_end_exclusive.day,
        tzinfo=timezone.utc,
    )
    urls = generate_tick_urls(instrument, range_start, range_end)
    if not urls:
        raise RuntimeError(f"No URLs generated for {_month_label(month_start)}.")

    total_files = len(urls)
    expected_hours_by_day: dict[date, int] = defaultdict(int)
    for dt, _ in urls:
        expected_hours_by_day[dt.date()] += 1
    processed_hours_by_day: dict[date, int] = defaultdict(int)
    total_days = len(expected_hours_by_day)

    logger.info(
        "[%s] Starting download: %d hourly files (%d days).",
        _month_label(month_start),
        total_files,
        total_days,
    )

    timeout = aiohttp.ClientTimeout(total=45, connect=15)
    connector = aiohttp.TCPConnector(limit=MAX_BATCH_SIZE * 2, force_close=False)
    successful_results: list[tuple[datetime, bytes]] = []
    successful_files = 0
    month_started = time.perf_counter()
    penalty_count = 0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        offset = 0
        while offset < total_files:
            batch = urls[offset : offset + tuning.batch_size]
            tasks = [
                asyncio.create_task(_fetch_url(session, dt, url, max_retries=max_retries))
                for dt, url in batch
            ]
            outcomes = await asyncio.gather(*tasks)

            penalties_this_batch = any(item.penalty for item in outcomes)
            batch_all_successful = _batch_all_successful(outcomes)
            avg_latency_s = sum(item.latency_s for item in outcomes) / len(outcomes)
            if not outcomes:
                avg_latency_s = float("inf")

            for item in outcomes:
                processed_hours_by_day[item.dt.date()] += 1
                if item.success:
                    successful_results.append((item.dt, item.data))
                    successful_files += 1

            if penalties_this_batch:
                penalty_count += 1
                logger.warning("Rate limit hit! Backing off.")
                cooldown_seconds = random.randint(15, 30)
                await asyncio.sleep(cooldown_seconds)
                tuning.batch_size = max(MIN_BATCH_SIZE, tuning.batch_size // 2)
                tuning.delay_ms = min(MAX_DELAY_MS, tuning.delay_ms * 2)
                tuning.success_streak = 0
            else:
                if batch_all_successful and avg_latency_s < FAST_LATENCY_THRESHOLD_S:
                    tuning.success_streak += 1
                else:
                    tuning.success_streak = 0

                if tuning.success_streak >= SUCCESS_STREAK_TARGET:
                    tuning.batch_size = min(MAX_BATCH_SIZE, tuning.batch_size + 1)
                    tuned_delay = int(tuning.delay_ms * 0.9)
                    tuning.delay_ms = max(MIN_DELAY_MS, tuned_delay)
                    tuning.success_streak = 0
                    logger.info(
                        "AIMD increase applied: batch_size=%d delay_ms=%d",
                        tuning.batch_size,
                        tuning.delay_ms,
                    )

            offset += len(batch)
            days_completed = sum(
                1 for day_key, expected in expected_hours_by_day.items()
                if processed_hours_by_day[day_key] >= expected
            )

            elapsed = max(time.perf_counter() - month_started, 1e-6)
            speed = successful_files / elapsed
            logger.info(
                "[%s] Downloaded %d/%d days. Speed: %.2f files/sec. Current config: "
                "Batch=%d, Delay=%dms. RAM: %.2fGB.",
                _month_label(month_start),
                days_completed,
                total_days,
                speed,
                tuning.batch_size,
                tuning.delay_ms,
                _ram_gb(),
            )

            if on_batch_complete is not None:
                on_batch_complete(tuning)

            if offset < total_files:
                await asyncio.sleep(tuning.delay_ms / 1000.0)

    duration_s = max(time.perf_counter() - month_started, 1e-6)
    if successful_files == 0:
        raise RuntimeError(
            f"[{_month_label(month_start)}] No successful files downloaded. "
            f"penalties={penalty_count}"
        )

    frame = _assemble_tick_dataframe(successful_results, instrument)
    if frame.empty:
        raise RuntimeError(f"[{_month_label(month_start)}] Parsed tick dataframe is empty.")

    out_path = (
        DATA_DIR
        / "ohlcv"
        / "dukascopy"
        / asset_class
        / symbol.upper()
        / f"tick_{month_start.year:04d}_{month_start.month:02d}.parquet"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out_path, index=False)
    rows = len(frame)
    del frame
    del successful_results
    gc.collect()

    files_per_sec = successful_files / duration_s
    return MonthResult(
        month_key=_month_key(symbol, month_start),
        rows=rows,
        file_path=out_path,
        files_per_sec=files_per_sec,
        duration_s=duration_s,
        successful_files=successful_files,
        total_files=total_files,
    )


def _pending_months(
    month_ranges: list[tuple[date, date]],
    months_state: dict[str, str],
    symbol: str,
) -> list[tuple[str, date, date]]:
    pending: list[tuple[str, date, date]] = []
    for month_start, month_end in month_ranges:
        key = _month_key(symbol, month_start)
        status = months_state.get(key, "pending")
        if status == "done":
            continue
        pending.append((key, month_start, month_end))
    return pending


def _run(
    *,
    symbol: str,
    asset_class: str,
    start: date,
    end_exclusive: date,
    max_retries: int,
    logger: logging.Logger,
) -> int:
    month_ranges = _month_ranges(start, end_exclusive)
    if not month_ranges:
        raise ValueError("No months in selected range.")

    state = _load_state()
    month_keys = [_month_key(symbol, month_start) for month_start, _ in month_ranges]
    _, job = _ensure_job_state(state, symbol, asset_class, month_keys)
    job["last_requested_start"] = start.isoformat()
    job["last_requested_end_exclusive"] = end_exclusive.isoformat()
    _save_state(state)

    months_state: dict[str, str] = job["months"]
    pending = _pending_months(month_ranges, months_state, symbol)
    if not pending:
        logger.info("All requested months already done. Nothing to do.")
        return 0

    logger.info(
        "Adaptive Tick Harvester started: symbol=%s asset_class=%s months_pending=%d state=%s",
        symbol,
        asset_class,
        len(pending),
        STATE_PATH,
    )

    tuning_payload = job.get("tuning", {})
    tuning = AdaptiveConfig(
        batch_size=max(MIN_BATCH_SIZE, int(tuning_payload.get("batch_size", DEFAULT_BATCH_SIZE))),
        delay_ms=max(MIN_DELAY_MS, int(tuning_payload.get("delay_ms", DEFAULT_DELAY_MS))),
        success_streak=max(0, int(tuning_payload.get("success_streak", 0))),
    )
    tuning.batch_size = min(MAX_BATCH_SIZE, tuning.batch_size)
    tuning.delay_ms = min(MAX_DELAY_MS, tuning.delay_ms)

    _, instrument, _ = _resolve_symbol_and_instrument(symbol, asset_class)

    for month_key, month_start, month_end in pending:
        months_state[month_key] = "in_progress"
        job["updated_at"] = _utc_now()
        job["tuning"] = tuning.as_dict()
        _save_state(state)

        def on_batch_complete(updated_tuning: AdaptiveConfig) -> None:
            job["tuning"] = updated_tuning.as_dict()
            job["updated_at"] = _utc_now()
            _save_state(state)

        try:
            result = asyncio.run(
                _download_month(
                    symbol=symbol,
                    asset_class=asset_class,
                    instrument=instrument,
                    month_start=month_start,
                    month_end_exclusive=month_end,
                    tuning=tuning,
                    logger=logger,
                    max_retries=max_retries,
                    on_batch_complete=on_batch_complete,
                )
            )
        except KeyboardInterrupt:
            months_state[month_key] = "pending"
            job["updated_at"] = _utc_now()
            job["tuning"] = tuning.as_dict()
            _save_state(state)
            logger.warning(
                "Interrupted by user. Progress saved. "
                "Restart to continue pending months."
            )
            raise
        except Exception as exc:
            months_state[month_key] = "failed"
            job["last_error"] = str(exc)
            job["updated_at"] = _utc_now()
            job["tuning"] = tuning.as_dict()
            _save_state(state)
            raise
        else:
            months_state[month_key] = "done"
            stats = job.setdefault("month_stats", {})
            stats[month_key] = {
                "rows": result.rows,
                "file_path": str(result.file_path),
                "files_per_sec": round(result.files_per_sec, 4),
                "successful_files": result.successful_files,
                "total_files": result.total_files,
                "duration_s": round(result.duration_s, 2),
                "finished_at": _utc_now(),
            }
            job["updated_at"] = _utc_now()
            job["tuning"] = tuning.as_dict()
            _save_state(state)

            _update_profile_if_better(symbol, asset_class, tuning, result, logger)
            logger.info(
                "[%s] Saved %d rows to %s (successful_files=%d/%d, speed=%.2f files/sec)",
                _month_label(month_start),
                result.rows,
                result.file_path,
                result.successful_files,
                result.total_files,
                result.files_per_sec,
            )

    logger.info("Adaptive Tick Harvester finished successfully.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crash-resilient adaptive Dukascopy tick harvester with monthly chunking."
    )
    parser.add_argument("--symbol", required=True, help="Instrument symbol (e.g. XAUUSD).")
    parser.add_argument(
        "--asset_class",
        default=None,
        help="Asset class path segment (e.g. metals). Defaults from known symbol mapping.",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Start date (YYYY-MM-DD). Defaults to dukascopy_genesis tick date.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End date exclusive (YYYY-MM-DD). Example: 2023-03-01 downloads up to Feb.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retry attempts per URL on transient errors (default: 2).",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    logger = _configure_logger()
    logger.info("Log file: %s", TASK_LOG_PATH)
    logger.info("State file: %s", STATE_PATH)
    logger.info("Data dir: %s", DATA_DIR)

    symbol, _, resolved_asset = _resolve_symbol_and_instrument(args.symbol, args.asset_class)
    start_date = _resolve_start_date(symbol, args.start)
    end_exclusive = _resolve_end_date_exclusive(args.end)
    if end_exclusive <= start_date:
        raise ValueError("--end must be after --start.")

    return _run(
        symbol=symbol,
        asset_class=resolved_asset,
        start=start_date,
        end_exclusive=end_exclusive,
        max_retries=max(0, args.max_retries),
        logger=logger,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

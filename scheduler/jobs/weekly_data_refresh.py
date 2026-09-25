"""Weekly data refresh scheduled job.

Refreshes the core OHLCV grid for the last 7 days. The job only reports a
completed refresh for symbols whose data it actually checked and refreshed;
missing or undownloadable data is reported as a skip/failure instead of
being recorded as successful work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from data_tools.auto_download import ensure_backtest_data, has_ohlcv_bars_in_range
from data_tools.path_safety import validate_dataset_component
from shared.constants import normalize_timeframe
from shared.database import upsert_background_task_record
from shared.logging import get_logger

logger = get_logger("scheduler")

REFRESH_SYMBOLS = ["EURUSD", "AAPL", "TSLA", "SPY", "QQQ"]
REFRESH_TIMEFRAMES = ["1m", "5m", "1h", "4h", "1d"]

_DESCRIPTION = "Refreshes core symbols for the last 7 days across standard timeframes."
_ALLOWED_REFRESH_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"})


def _validated_refresh_config(
    config: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    source: Mapping[str, Any] = {} if config is None else config
    if not isinstance(source, Mapping):
        raise ValueError("weekly refresh config must be a mapping")
    nested = source.get("refresh_config", source.get("refresh"))
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ValueError("weekly refresh config section must be a mapping")
        source = nested

    raw_symbols = source.get("symbols", REFRESH_SYMBOLS)
    raw_timeframes = source.get("timeframes", REFRESH_TIMEFRAMES)
    if isinstance(raw_symbols, str) or not isinstance(raw_symbols, (list, tuple, set)):
        raise ValueError("weekly refresh symbols must be a non-empty sequence")
    if isinstance(raw_timeframes, str) or not isinstance(raw_timeframes, (list, tuple, set)):
        raise ValueError("weekly refresh timeframes must be a non-empty sequence")

    symbols: list[str] = []
    for raw_symbol in raw_symbols:
        symbol = str(raw_symbol or "").strip().upper()
        validate_dataset_component(symbol, name="weekly refresh symbol")
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("weekly refresh symbols must not be empty")

    timeframes: list[str] = []
    for raw_timeframe in raw_timeframes:
        timeframe = normalize_timeframe(str(raw_timeframe or "").strip())
        if timeframe not in _ALLOWED_REFRESH_TIMEFRAMES:
            raise ValueError(f"unsupported weekly refresh timeframe: {raw_timeframe!r}")
        validate_dataset_component(timeframe, name="weekly refresh timeframe")
        if timeframe not in timeframes:
            timeframes.append(timeframe)
    if not timeframes:
        raise ValueError("weekly refresh timeframes must not be empty")
    return symbols, timeframes


def _symbol_is_fresh(
    symbol: str,
    *,
    start: str,
    end: str,
    timeframes: list[str] | None = None,
) -> bool:
    """A symbol counts as fresh when every requested timeframe has a bar in the window.

    Freshness is checked against the advertised 7-day window via
    ``has_ohlcv_bars_in_range`` (which reads the parquet timestamps), not merely
    file existence — a months-old file does not count as a refreshed week.
    """
    return all(
        has_ohlcv_bars_in_range(
            symbol=symbol, timeframe=timeframe, start=start, end=end
        )
        for timeframe in (timeframes or REFRESH_TIMEFRAMES)
    )


def _covered_symbols(
    *,
    start: str,
    end: str,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> list[str]:
    """Return the refresh symbols whose local data already covers [start, end].

    Blocking (reads parquet timestamps) — callers must run it via
    ``asyncio.to_thread``.
    """
    covered: list[str] = []
    for symbol in symbols or REFRESH_SYMBOLS:
        try:
            if _symbol_is_fresh(symbol, start=start, end=end, timeframes=timeframes):
                covered.append(symbol)
        except Exception as exc:
            logger.warning(
                "Weekly data refresh freshness check failed",
                extra={"data": {"symbol": symbol, "error": str(exc)}},
            )
    return covered


async def run(
    *,
    task_id: str,
    service_id: str,
    service_name: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh the weekly data window and report the truthful outcome.

    Returns a result dict with ``status`` (completed/skipped/failed),
    ``refreshed`` and ``missing`` symbol lists plus the refresh ``window``.
    The scheduler records this outcome verbatim.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    window = {"start": start.isoformat(), "end": end.isoformat()}
    try:
        symbols, timeframes = _validated_refresh_config(config)
    except ValueError as exc:
        result = {
            "status": "failed",
            "refreshed": [],
            "missing": [],
            "errors": [str(exc)],
            "already_fresh": [],
            "window": window,
        }
        await asyncio.to_thread(
            upsert_background_task_record,
            task_id=task_id,
            task_type="service",
            status="failed",
            config_json={
                "service_id": service_id,
                "service_name": service_name,
                "window": window,
                "result": result,
                "error": str(exc),
            },
            progress_message=f"Weekly data refresh failed: {exc}",
            name=f"{service_name} · Weekly Data Refresh",
            description=_DESCRIPTION,
            source="scheduler",
            updated_at=end.isoformat(),
        )
        return result

    logger.info(
        "Weekly data refresh started",
        extra={
            "data": {
                "service_id": service_id,
                "service_name": service_name,
                "symbols": symbols,
                "timeframes": timeframes,
                "start": window["start"],
                "end": window["end"],
            }
        },
    )

    base_config = {
        "service_id": service_id,
        "service_name": service_name,
        "symbols": symbols,
        "timeframes": timeframes,
        "window": window,
        "total_items": len(symbols),
        "progress_percent": 5.0,
        "progress_detail": {
            "completed": 0,
            "failed": 0,
            "total": len(symbols),
            "percent": 5.0,
            "current": "Checking symbol coverage",
        },
        "supports_control": False,
        "control_mode": "thread",
        "category": "service",
    }
    await asyncio.to_thread(
        upsert_background_task_record,
        task_id=task_id,
        task_type="service",
        status="running",
        config_json=base_config,
        progress_message="Checking symbol coverage",
        name=f"{service_name} · Weekly Data Refresh",
        description=_DESCRIPTION,
        source="scheduler",
        updated_at=end.isoformat(),
    )

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    # The freshness scan reads up to len(REFRESH_SYMBOLS) * len(REFRESH_TIMEFRAMES)
    # parquet files; run it off the event loop like every other blocking call.
    covered = list(
        await asyncio.to_thread(
            _covered_symbols,
            start=start_str,
            end=end_str,
            symbols=symbols,
            timeframes=timeframes,
        )
    )
    missing = [symbol for symbol in symbols if symbol not in covered]

    refreshed: list[str] = []
    errors: list[str] = []
    if missing:
        # Only the missing symbols are requested for download; covered symbols
        # are left untouched so the refresh stays a cheap incremental no-op.
        refreshed, errors = await _download_missing_symbols(
            missing,
            start=start,
            end=end,
            task_id=task_id,
            service_id=service_id,
            service_name=service_name,
            base_config=base_config,
            timeframes=timeframes,
        )

    finished = datetime.now(timezone.utc)
    if not missing:
        # Every symbol already has data covering the advertised window: nothing
        # was downloaded, so the job reports a no-op ("skipped"), never a
        # completed refresh.
        status = "skipped"
        progress_message = (
            "Weekly data refresh skipped: all "
            f"{len(symbols)} symbols already cover the window "
            f"{start_str}..{end_str}; nothing to download"
        )
        current = f"Skipped: {len(covered)}/{len(symbols)} symbols already fresh"
    elif errors:
        status = "failed"
        progress_message = f"Weekly data refresh failed: {'; '.join(errors)}"
        current = errors[0]
    elif missing and not refreshed:
        status = "skipped"
        progress_message = (
            "Weekly data refresh skipped: no data downloaded for "
            f"{', '.join(missing)}"
        )
        current = "Skipped: no data downloaded"
    else:
        # `refreshed` is the subset of `missing` whose download produced real
        # window coverage; `covered` are the symbols that were already fresh
        # before the run, so "completed" requires both groups to cover every
        # symbol.
        fully_covered = len(covered) + len(refreshed)
        status = "completed" if fully_covered == len(symbols) else "partial"
        progress_message = (
            f"Weekly data refresh {status}: {fully_covered} of "
            f"{len(symbols)} symbols cover the window"
        )
        current = f"{status.capitalize()}: {fully_covered}/{len(symbols)} refreshed"

    completed_count = len(refreshed) if status in {"completed", "partial"} else 0
    failed_count = len(errors) if errors else 0
    result_config = _progress_config(
        base_config,
        total=len(symbols),
        completed=completed_count,
        failed=failed_count,
        current=current,
        percent=100.0,
    )
    result_config.update(
        {
            "finished_at": finished.isoformat(),
            "result": {
                "status": status,
                "refreshed": refreshed,
                "missing": missing,
                "errors": errors,
                "already_fresh": covered,
                "window": window,
            },
        }
    )
    if errors:
        result_config["error"] = "; ".join(errors)

    await asyncio.to_thread(
        upsert_background_task_record,
        task_id=task_id,
        task_type="service",
        status=status,
        config_json=result_config,
        progress_message=progress_message,
        name=f"{service_name} · Weekly Data Refresh",
        description=_DESCRIPTION,
        source="scheduler",
        updated_at=finished.isoformat(),
    )

    logger.info(
        "Weekly data refresh finished",
        extra={
            "data": {
                "service_id": service_id,
                "service_name": service_name,
                "status": status,
                "refreshed": refreshed,
                "missing": missing,
                "already_fresh": covered,
            }
        },
    )

    return {
        "status": status,
        "refreshed": refreshed,
        "missing": missing,
        "errors": errors,
        "already_fresh": covered,
        "window": window,
    }


def _progress_config(
    base_config: dict[str, Any],
    *,
    total: int,
    completed: int,
    failed: int,
    current: str,
    percent: float,
) -> dict[str, Any]:
    """Build the task-record config for one progress point.

    Single source of the progress_detail shape used by every progress write in
    this job (pre-download, post-download and the final record).
    """
    config = dict(base_config)
    config.update(
        {
            "progress_percent": percent,
            "progress_detail": {
                "completed": completed,
                "failed": failed,
                "total": total,
                "percent": percent,
                "current": current,
            },
        }
    )
    return config


def _write_progress_sync(
    *,
    task_id: str,
    service_id: str,
    service_name: str,
    config: dict[str, Any],
    progress_message: str,
) -> None:
    upsert_background_task_record(
        task_id=task_id,
        task_type="service",
        status="running",
        config_json=config,
        progress_message=progress_message,
        name=f"{service_name} · Weekly Data Refresh",
        description=_DESCRIPTION,
        source="scheduler",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


async def _download_missing_symbols(
    missing: list[str],
    *,
    start: datetime,
    end: datetime,
    task_id: str,
    service_id: str,
    service_name: str,
    base_config: dict[str, Any],
    timeframes: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Download every missing symbol and report per-symbol progress.

    Returns ``(refreshed, errors)``: symbols whose download produced real OHLCV
    coverage, and the per-symbol failure messages for everything else.
    """
    refreshed: list[str] = []
    errors: list[str] = []
    total_missing = max(len(missing), 1)
    effective_timeframes = list(timeframes or REFRESH_TIMEFRAMES)
    # One window definition for the download request and the coverage check:
    # both must describe the exact same date range.
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    for index, symbol in enumerate(missing, start=1):
        # The pre-download record reports the symbols finished so far
        # (index - 1); the post-download record below reports index.
        try:
            progress_percent = round(((index - 1) / total_missing) * 95.0, 2)
            progress_config = _progress_config(
                base_config,
                total=len(missing),
                completed=index - 1,
                failed=0,
                current=f"Downloading {symbol}",
                percent=progress_percent,
            )
            await asyncio.to_thread(
                _write_progress_sync,
                task_id=task_id,
                service_id=service_id,
                service_name=service_name,
                config=progress_config,
                progress_message=(
                    f"Downloading {symbol} for {', '.join(effective_timeframes)}"
                ),
            )
            for timeframe in effective_timeframes:
                await asyncio.to_thread(
                    ensure_backtest_data,
                    symbols=[symbol],
                    timeframe=timeframe,
                    start=start_date,
                    end=end_date,
                    modeling_resolution="main_timeframe",
                )
            if await asyncio.to_thread(
                _symbol_is_fresh,
                symbol,
                start=start_date,
                end=end_date,
                timeframes=effective_timeframes,
            ):
                # Only count the symbol as finished (and record the progress)
                # once the coverage check actually passed.
                refreshed.append(symbol)
                await asyncio.to_thread(
                    _record_progress_sync,
                    task_id=task_id,
                    service_id=service_id,
                    service_name=service_name,
                    base_config=base_config,
                    completed=index,
                    total_missing=total_missing,
                    message=f"Downloaded {symbol}",
                )
            else:
                errors.append(
                    f"{symbol}: download completed but no OHLCV data covering "
                    f"{start_date}..{end_date} for {', '.join(effective_timeframes)}"
                )
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            logger.warning(
                "Weekly data refresh download failed",
                extra={
                    "data": {
                        "service_id": service_id,
                        "service_name": service_name,
                        "symbol": symbol,
                        "error": str(exc),
                    }
                },
            )
    return refreshed, errors


def _record_progress_sync(
    *,
    task_id: str,
    service_id: str,
    service_name: str,
    base_config: dict[str, Any],
    completed: int,
    total_missing: int,
    message: str,
) -> None:
    """Write a running record reporting ``completed`` finished symbols."""
    percent = round((completed / max(total_missing, 1)) * 95.0, 2)
    config = _progress_config(
        base_config,
        total=total_missing,
        completed=completed,
        failed=0,
        current=message,
        percent=percent,
    )
    _write_progress_sync(
        task_id=task_id,
        service_id=service_id,
        service_name=service_name,
        config=config,
        progress_message=message,
    )

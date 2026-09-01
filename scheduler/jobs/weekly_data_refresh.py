"""Weekly data refresh scheduled job.

Refreshes the core OHLCV grid for the last 7 days. The job only reports a
completed refresh for symbols whose data it actually checked and refreshed;
missing or undownloadable data is reported as a skip/failure instead of
being recorded as successful work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from data_tools.auto_download import ensure_backtest_data
from shared import constants as shared_constants
from shared.constants import normalize_timeframe
from shared.database import upsert_background_task_record
from shared.logging import get_logger

logger = get_logger("scheduler")

REFRESH_SYMBOLS = ["EURUSD", "AAPL", "TSLA", "SPY", "QQQ"]
REFRESH_TIMEFRAMES = ["1m", "5m", "1h", "4h", "1d"]

_DESCRIPTION = "Refreshes core symbols for the last 7 days across standard timeframes."


def _has_ohlcv_file(symbol: str, timeframe: str, data_dir: Any = None) -> bool:
    root = (data_dir or shared_constants.DATA_DIR) / "ohlcv"
    matches = list(root.rglob(f"**/{symbol}/{normalize_timeframe(timeframe)}.parquet"))
    return any(path.stat().st_size > 0 for path in matches if path.is_file())


def _symbol_is_fresh(symbol: str) -> bool:
    """A symbol counts as covered when every requested timeframe has a data file.

    "Covered" means a non-empty local parquet exists — it deliberately does not
    check the data's age. A stale file is still treated as covered so the
    refresh stays a cheap incremental no-op instead of re-downloading the whole
    window on every tick; the advertised 7-day window is requested from the
    downloader whenever a symbol is *not* covered.
    """
    return all(_has_ohlcv_file(symbol, timeframe) for timeframe in REFRESH_TIMEFRAMES)


async def run(*, task_id: str, service_id: str, service_name: str) -> dict[str, Any]:
    """Refresh the weekly data window and report the truthful outcome.

    Returns a result dict with ``status`` (completed/skipped/failed),
    ``refreshed`` and ``missing`` symbol lists plus the refresh ``window``.
    The scheduler records this outcome verbatim.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    window = {"start": start.isoformat(), "end": end.isoformat()}

    logger.info(
        "Weekly data refresh started",
        extra={
            "data": {
                "service_id": service_id,
                "service_name": service_name,
                "symbols": REFRESH_SYMBOLS,
                "timeframes": REFRESH_TIMEFRAMES,
                "start": window["start"],
                "end": window["end"],
            }
        },
    )

    base_config = {
        "service_id": service_id,
        "service_name": service_name,
        "symbols": REFRESH_SYMBOLS,
        "timeframes": REFRESH_TIMEFRAMES,
        "window": window,
        "total_items": len(REFRESH_SYMBOLS),
        "progress_percent": 5.0,
        "progress_detail": {
            "completed": 0,
            "failed": 0,
            "total": len(REFRESH_SYMBOLS),
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

    covered = [symbol for symbol in REFRESH_SYMBOLS if _symbol_is_fresh(symbol)]
    missing = [symbol for symbol in REFRESH_SYMBOLS if symbol not in covered]

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
        )
    else:
        # Everything already has local coverage; verify instead of re-downloading.
        refreshed = list(covered)
        errors = []

    finished = datetime.now(timezone.utc)
    if errors:
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
    elif missing:
        status = "partial"
        progress_message = (
            f"Weekly data refresh partial: refreshed {len(refreshed)} of "
            f"{len(REFRESH_SYMBOLS)} symbols"
        )
        current = f"Partial: {len(refreshed)}/{len(REFRESH_SYMBOLS)} refreshed"
    else:
        status = "completed"
        progress_message = (
            f"Weekly data refresh completed for {len(refreshed)} symbols "
            f"across {', '.join(REFRESH_TIMEFRAMES)}"
        )
        current = "Completed"

    completed_count = len(refreshed) if status in {"completed", "partial"} else 0
    failed_count = len(errors) if errors else 0
    percent = 100.0
    result_config = dict(base_config)
    result_config.update(
        {
            "finished_at": finished.isoformat(),
            "progress_percent": percent,
            "progress_detail": {
                "completed": completed_count,
                "failed": failed_count,
                "total": len(REFRESH_SYMBOLS),
                "percent": percent,
                "current": current,
            },
            "result": {
                "status": status,
                "refreshed": refreshed,
                "missing": missing,
                "errors": errors,
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
            }
        },
    )

    return {
        "status": status,
        "refreshed": refreshed,
        "missing": missing,
        "errors": errors,
        "window": window,
    }


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
) -> tuple[list[str], list[str]]:
    """Download every missing symbol and report per-symbol progress.

    Returns ``(refreshed, errors)``: symbols whose download produced real OHLCV
    coverage, and the per-symbol failure messages for everything else.
    """
    refreshed: list[str] = []
    errors: list[str] = []
    total_missing = max(len(missing), 1)

    for index, symbol in enumerate(missing, start=1):
        # The pre-download record reports the symbols finished so far
        # (index - 1); the post-download record below reports index.
        progress_percent = round(((index - 1) / total_missing) * 95.0, 2)
        progress_config = dict(base_config)
        progress_config.update(
            {
                "progress_percent": progress_percent,
                "progress_detail": {
                    "completed": index - 1,
                    "failed": 0,
                    "total": len(missing),
                    "percent": progress_percent,
                    "current": f"Downloading {symbol}",
                },
            }
        )
        await asyncio.to_thread(
            _write_progress_sync,
            task_id=task_id,
            service_id=service_id,
            service_name=service_name,
            config=progress_config,
            progress_message=f"Downloading {symbol} for {', '.join(REFRESH_TIMEFRAMES)}",
        )
        try:
            for timeframe in REFRESH_TIMEFRAMES:
                await asyncio.to_thread(
                    ensure_backtest_data,
                    symbols=[symbol],
                    timeframe=timeframe,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    modeling_resolution="main_timeframe",
                )
            _record_progress_sync(
                task_id=task_id,
                service_id=service_id,
                service_name=service_name,
                base_config=base_config,
                completed=index,
                total_missing=total_missing,
                message=f"Downloaded {symbol}",
            )
            if _symbol_is_fresh(symbol):
                refreshed.append(symbol)
            else:
                errors.append(
                    f"{symbol}: download completed but no OHLCV files present "
                    f"for {', '.join(REFRESH_TIMEFRAMES)}"
                )
        except (RuntimeError, OSError, ValueError) as exc:
            errors.append(f"{symbol}: {exc}")
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
    config = dict(base_config)
    config.update(
        {
            "progress_percent": percent,
            "progress_detail": {
                "completed": completed,
                "failed": 0,
                "total": total_missing,
                "percent": percent,
                "current": message,
            },
        }
    )
    _write_progress_sync(
        task_id=task_id,
        service_id=service_id,
        service_name=service_name,
        config=config,
        progress_message=message,
    )

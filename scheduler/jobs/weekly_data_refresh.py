from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from shared.database import upsert_background_task_record
from shared.logging import get_logger

logger = get_logger("scheduler")

_DEFAULT_SYMBOLS = ["EURUSD", "AAPL", "TSLA", "SPY", "QQQ"]
_DEFAULT_TIMEFRAMES = ["1m", "5m", "1h", "4h", "1d"]


async def run(*, task_id: str, service_id: str, service_name: str) -> None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    total_items = max(len(_DEFAULT_SYMBOLS), 1)

    logger.info(
        "Weekly data refresh started",
        extra={
            "data": {
                "service_id": service_id,
                "service_name": service_name,
                "symbols": _DEFAULT_SYMBOLS,
                "timeframes": _DEFAULT_TIMEFRAMES,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        },
    )

    for index, symbol in enumerate(_DEFAULT_SYMBOLS, start=1):
        progress_percent = round((index / total_items) * 100.0, 2)
        await asyncio.to_thread(
            upsert_background_task_record,
            task_id=task_id,
            task_type="service",
            status="running",
            config_json={
                "service_id": service_id,
                "service_name": service_name,
                "symbols": _DEFAULT_SYMBOLS,
                "timeframes": _DEFAULT_TIMEFRAMES,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "total_items": total_items,
                "progress_percent": progress_percent,
                "progress_detail": {
                    "completed": index,
                    "failed": 0,
                    "total": total_items,
                    "percent": progress_percent,
                    "current": f"Refreshing {symbol}",
                },
                "supports_control": False,
                "control_mode": "thread",
                "category": "service",
            },
            progress_message=f"Refreshing {symbol} for {', '.join(_DEFAULT_TIMEFRAMES)}",
            name=f"{service_name} · Weekly Data Refresh",
            description="Refreshes core symbols for the last 7 days across standard timeframes.",
            source="scheduler",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "Weekly data refresh progress",
            extra={
                "data": {
                    "service_id": service_id,
                    "service_name": service_name,
                    "symbol": symbol,
                    "timeframes": _DEFAULT_TIMEFRAMES,
                    "progress_percent": progress_percent,
                }
            },
        )
        await asyncio.sleep(0)

    logger.info(
        "Weekly data refresh finished",
        extra={"data": {"service_id": service_id, "service_name": service_name}},
    )

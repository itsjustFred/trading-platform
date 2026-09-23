from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from risk.models import TradeSignal
from shared.constants import normalize_timeframe
from shared.logging import get_logger

logger = get_logger("live-runner")

_TIMEFRAME_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[smhdw])$")


def _parse_signal_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _timeframe_seconds(timeframe: str) -> int | None:
    normalized = normalize_timeframe(timeframe)
    if normalized in {"", "tick"}:
        return None
    if normalized == "s1":
        return 1
    matched = _TIMEFRAME_RE.match(normalized)
    if matched is None:
        return None
    count = int(matched.group("count"))
    unit = matched.group("unit")
    if count <= 0:
        return None
    if unit == "s":
        return count
    if unit == "m":
        return count * 60
    if unit == "h":
        return count * 3600
    if unit == "d":
        return count * 86400
    if unit == "w":
        return count * 7 * 86400
    return None


def _next_candle_close(
    reference: datetime, timeframe_seconds: int, *, timeframe: str = ""
) -> datetime:
    # B2: weekly candles anchor on Monday 00:00 UTC, not the Unix epoch
    # (which anchors weeks on Thursday). All other granularities divide
    # days evenly from midnight UTC already.
    if timeframe.strip().lower().endswith("w"):
        weeks = max(timeframe_seconds // (7 * 86400), 1)
        monday = (reference - timedelta(days=reference.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        candidate = monday
        while candidate <= reference:
            candidate += timedelta(days=7 * weeks)
        return candidate
    epoch_seconds = int(reference.timestamp())
    next_epoch = ((epoch_seconds // timeframe_seconds) + 1) * timeframe_seconds
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)


def max_signal_age_seconds(timeframe: str) -> int | None:
    """Maximum live-signal age: one candle of the deployment timeframe (B2).

    A 1h signal older than 60 minutes is discarded instead of trading
    immediately on stale information.
    """
    return _timeframe_seconds(timeframe)


def signal_is_fresh(
    signal: TradeSignal, timeframe: str, *, now: datetime | None = None
) -> bool:
    """Whether a live signal is young enough to trade (B2).

    Signals without a parseable `received_at` are treated as fresh — age
    cannot be judged, and replay/internal paths (which this gate does not
    cover) must not break on missing stamps. The daemon applies this gate
    on the Discord dispatch path only.
    """
    max_age = max_signal_age_seconds(timeframe)
    if max_age is None:
        return True
    received_at = _parse_signal_timestamp(signal.metadata.get("received_at"))
    if received_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    return (current - received_at).total_seconds() <= max_age


async def wait_for_candle_close(signal: TradeSignal, timeframe: str) -> None:
    """Wait until the next candle close for the given timeframe."""
    timeframe_seconds = _timeframe_seconds(timeframe)
    if timeframe_seconds is None:
        return

    received_at = _parse_signal_timestamp(signal.metadata.get("received_at"))
    reference = received_at or datetime.now(timezone.utc)
    close_at = _next_candle_close(reference, timeframe_seconds, timeframe=timeframe)
    now = datetime.now(timezone.utc)
    delay = (close_at - now).total_seconds()
    if delay <= 0:
        return

    logger.info(
        "Signal received, waiting for candle close",
        extra={
            "data": {
                "symbol": signal.symbol,
                "action": signal.action,
                "timeframe": timeframe,
                "candle_close_at": close_at.strftime("%H:%M:%S"),
                "wait_seconds": round(delay, 3),
            }
        },
    )
    await asyncio.sleep(delay)

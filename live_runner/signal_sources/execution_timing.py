from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from risk.models import TradeSignal
from shared.constants import normalize_timeframe
from shared.logging import get_logger
from shared.models import VALID_TIMEFRAMES

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


def _require_recognised_timeframe(timeframe: str, caller: str) -> None:
    """Refuse a timeframe label the repo does not define.

    Separated from the budget lookup because "no budget" and "no such timeframe"
    are different answers to different questions, and collapsing them is what let
    a broker-style label (`M15`, `H1`, `D`) disable both the staleness gate and the
    candle-close wait without a word. `tick` resolves to no budget and is fine;
    `M15` resolves to nothing at all and is a configuration error.
    """
    normalized = normalize_timeframe(timeframe)
    if normalized in VALID_TIMEFRAMES:
        return
    raise ValueError(
        f"{caller}: unrecognised live timeframe {timeframe!r} "
        f"(normalised {normalized!r}); expected one of "
        f"{', '.join(VALID_TIMEFRAMES)}. An unknown label cannot be given a "
        f"staleness budget or a candle close, so treating it as 'no limit' would "
        f"silently trade on old information and skip the requested wait."
    )


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

    Fail-closed: signals without a parseable `received_at` are treated as
    STALE. Age cannot be judged, and an unstamped signal must never trade
    on possibly ancient information. The Discord dispatch path always
    stamps `received_at` at receipt, so this only rejects signals that
    bypassed stamping.
    """
    max_age = max_signal_age_seconds(timeframe)
    if max_age is None:
        # Deliberately permissive, and NOT changed here. This gate is secondary:
        # `_validate_signal_age_before_wait` in the daemon is the primary one, it
        # raises for a missing, invalid or stale stamp, and for a timeframe it
        # cannot size it falls back to `max(300.0, 2 * timeframe_budget)`. That
        # compensating gate is documented in `_validate_signal_age_before_wait`'s
        # own docstring, which names this exact fail-open by name.
        #
        # An audit proposed refusing here too. It was rejected after reading that
        # gate: it would add a second, weaker refusal in front of a stronger one,
        # change a documented decision, and break a test that pins the behaviour on
        # purpose. `wait_for_candle_close` below has no such compensating gate -
        # skipping the wait silently changes what the strategy does - so that one is
        # fixed and this one is left as designed.
        return True
    received_at = _parse_signal_timestamp(signal.metadata.get("received_at"))
    if received_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - received_at).total_seconds() <= max_age


async def wait_for_candle_close(signal: TradeSignal, timeframe: str) -> None:
    """Wait until the next candle close for the given timeframe."""
    timeframe_seconds = _timeframe_seconds(timeframe)
    if timeframe_seconds is None:
        # See `signal_is_fresh`. Returning here for an unrecognised label means
        # "no wait", which turns the operator's requested candle-close entry into
        # an immediate market order at a different price than the strategy was
        # validated against - a silent change in what the strategy does.
        _require_recognised_timeframe(timeframe, "wait_for_candle_close")
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

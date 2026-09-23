"""Tests for live signal execution timing (B2: max age, weekly anchor)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from live_runner.signal_sources.execution_timing import (
    _next_candle_close,
    max_signal_age_seconds,
    signal_is_fresh,
)
from risk.models import TradeSignal


def _signal(received_at: str | None) -> TradeSignal:
    metadata = {}
    if received_at is not None:
        metadata["received_at"] = received_at
    return TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        timeframe="1h",
        metadata=metadata,
    )


class TestMaxSignalAge:
    def test_one_candle_is_the_limit(self) -> None:
        assert max_signal_age_seconds("1h") == 3600
        assert max_signal_age_seconds("15m") == 900
        assert max_signal_age_seconds("1d") == 86400

    def test_stale_signal_is_not_fresh(self) -> None:
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(minutes=61)).isoformat()
        assert signal_is_fresh(_signal(stale), "1h", now=now) is False

    def test_fresh_signal_passes(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(minutes=5)).isoformat()
        assert signal_is_fresh(_signal(fresh), "1h", now=now) is True

    def test_missing_timestamp_is_fresh(self) -> None:
        # Age cannot be judged — replay/internal paths must not break.
        assert signal_is_fresh(_signal(None), "1h") is True


class TestWeeklyAnchor:
    def test_weekly_candle_closes_monday_midnight(self) -> None:
        # Sunday 2024-01-07 12:00 UTC → next close Monday 2024-01-08 00:00
        # (Unix-epoch math would anchor Thursday instead).
        sunday = datetime(2024, 1, 7, 12, 0, tzinfo=timezone.utc)
        close = _next_candle_close(sunday, 7 * 86400, timeframe="1w")
        assert close == datetime(2024, 1, 8, 0, 0, tzinfo=timezone.utc)
        assert close.weekday() == 0

    def test_daily_anchor_unchanged(self) -> None:
        ref = datetime(2024, 1, 7, 12, 0, tzinfo=timezone.utc)
        close = _next_candle_close(ref, 86400, timeframe="1d")
        assert close == datetime(2024, 1, 8, 0, 0, tzinfo=timezone.utc)

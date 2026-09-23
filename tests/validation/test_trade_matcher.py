"""Tests for validation.comparison.matcher.TradeMatcher."""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.comparison.matcher import MatchKey, MatchResult, TradeMatcher


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    pnl: float = 50.0,
) -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
    )


class TestPerfectMatch:
    def test_single_trade_matches(self):
        ref = [_make_trade()]
        act = [_make_trade()]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        assert result.missing == []
        assert result.extra == []

    def test_multiple_trades_match(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z", exit_time="2024-01-01T11:00:00Z"),
            _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z", exit_time="2024-01-02T11:00:00Z"),
        ]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z", exit_time="2024-01-01T11:00:00Z"),
            _make_trade(trade_id=20, entry_time="2024-01-02T10:00:00Z", exit_time="2024-01-02T11:00:00Z"),
        ]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 2
        assert result.missing == []
        assert result.extra == []


class TestMissingTrades:
    def test_one_trade_missing_in_actual(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z"),
        ]
        act = [_make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z")]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        assert len(result.missing) == 1
        assert result.missing[0].id == 2
        assert result.extra == []

    def test_all_trades_missing(self):
        ref = [_make_trade(trade_id=1)]
        act = []
        result = TradeMatcher.match(ref, act)
        assert result.matched == []
        assert len(result.missing) == 1
        assert result.extra == []


class TestExtraTrades:
    def test_one_extra_trade_in_actual(self):
        ref = [_make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z")]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=20, entry_time="2024-01-02T10:00:00Z"),
        ]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        assert result.missing == []
        assert len(result.extra) == 1
        assert result.extra[0].id == 20

    def test_all_trades_extra(self):
        ref = []
        act = [_make_trade(trade_id=10)]
        result = TradeMatcher.match(ref, act)
        assert result.matched == []
        assert result.missing == []
        assert len(result.extra) == 1


class TestTemporalTolerance:
    def test_entry_time_within_tolerance(self):
        ref = [_make_trade(entry_time="2024-01-01T10:00:00Z")]
        act = [_make_trade(entry_time="2024-01-01T10:00:05Z")]
        # exact match should fail
        exact = TradeMatcher.match(ref, act, time_tolerance_seconds=0)
        assert exact.matched == []
        assert len(exact.missing) == 1
        assert len(exact.extra) == 1

        # 5-second tolerance should succeed
        tol = TradeMatcher.match(ref, act, time_tolerance_seconds=5)
        assert len(tol.matched) == 1
        assert tol.missing == []
        assert tol.extra == []

    def test_exit_time_within_tolerance(self):
        ref = [_make_trade(exit_time="2024-01-01T11:00:00Z")]
        act = [_make_trade(exit_time="2024-01-01T11:00:03Z")]
        exact = TradeMatcher.match(ref, act, time_tolerance_seconds=0)
        assert exact.matched == []

        tol = TradeMatcher.match(ref, act, time_tolerance_seconds=5)
        assert len(tol.matched) == 1

    def test_both_times_outside_tolerance(self):
        ref = [_make_trade(entry_time="2024-01-01T10:00:00Z", exit_time="2024-01-01T11:00:00Z")]
        act = [_make_trade(entry_time="2024-01-01T10:00:06Z", exit_time="2024-01-01T11:00:06Z")]
        tol = TradeMatcher.match(ref, act, time_tolerance_seconds=5)
        assert tol.matched == []
        assert len(tol.missing) == 1
        assert len(tol.extra) == 1


class TestSymbolMatching:
    def test_symbol_match_required_by_default(self):
        ref = [_make_trade(symbol="EURUSD")]
        act = [_make_trade(symbol="GBPUSD")]
        result = TradeMatcher.match(ref, act)
        assert result.matched == []
        assert len(result.missing) == 1
        assert len(result.extra) == 1

    def test_symbol_mismatch_ignored_when_disabled(self):
        ref = [_make_trade(symbol="EURUSD")]
        act = [_make_trade(symbol="GBPUSD")]
        result = TradeMatcher.match(ref, act, match_symbol=False)
        assert len(result.matched) == 1
        assert result.missing == []
        assert result.extra == []

    def test_symbol_match_explicitly_enabled(self):
        ref = [_make_trade(symbol="EURUSD")]
        act = [_make_trade(symbol="EURUSD")]
        result = TradeMatcher.match(ref, act, match_symbol=True)
        assert len(result.matched) == 1


class TestDirectionMismatch:
    def test_different_direction_becomes_missing_and_extra(self):
        ref = [_make_trade(direction="LONG")]
        act = [_make_trade(direction="SHORT")]
        result = TradeMatcher.match(ref, act)
        assert result.matched == []
        assert len(result.missing) == 1
        assert len(result.extra) == 1

    def test_short_matches_short(self):
        ref = [_make_trade(direction="SHORT")]
        act = [_make_trade(direction="SHORT")]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1


class TestEmptyInputs:
    def test_both_empty(self):
        result = TradeMatcher.match([], [])
        assert result.matched == []
        assert result.missing == []
        assert result.extra == []

    def test_only_reference_empty(self):
        act = [_make_trade()]
        result = TradeMatcher.match([], act)
        assert result.matched == []
        assert result.missing == []
        assert len(result.extra) == 1

    def test_only_actual_empty(self):
        ref = [_make_trade()]
        result = TradeMatcher.match(ref, [])
        assert result.matched == []
        assert len(result.missing) == 1
        assert result.extra == []


class TestIdenticalTimestamps:
    def test_pairs_in_input_order(self):
        """When several trades share the same timestamps they should be paired
        according to their position in the input arrays."""
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z", pnl=10),
            _make_trade(trade_id=2, entry_time="2024-01-01T10:00:00Z", pnl=20),
            _make_trade(trade_id=3, entry_time="2024-01-01T10:00:00Z", pnl=30),
        ]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z", pnl=10),
            _make_trade(trade_id=20, entry_time="2024-01-01T10:00:00Z", pnl=20),
            _make_trade(trade_id=30, entry_time="2024-01-01T10:00:00Z", pnl=30),
        ]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 3
        assert result.missing == []
        assert result.extra == []
        # Verify order preservation
        assert result.matched[0][0].id == 1
        assert result.matched[0][1].id == 10
        assert result.matched[1][0].id == 2
        assert result.matched[1][1].id == 20
        assert result.matched[2][0].id == 3
        assert result.matched[2][1].id == 30

    def test_identical_timestamps_with_missing(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z", pnl=10),
            _make_trade(trade_id=2, entry_time="2024-01-01T10:00:00Z", pnl=20),
        ]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z", pnl=10),
        ]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        assert result.matched[0][0].id == 1
        assert result.matched[0][1].id == 10
        assert len(result.missing) == 1
        assert result.missing[0].id == 2


class TestTimezoneNormalization:
    def test_different_timezone_offsets_match(self):
        """Timestamps with different but equivalent UTC offsets should match."""
        ref = [_make_trade(entry_time="2024-01-01T12:00:00+02:00")]
        act = [_make_trade(entry_time="2024-01-01T10:00:00Z")]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        assert result.missing == []
        assert result.extra == []

    def test_naive_timestamp_treated_as_utc(self):
        ref = [_make_trade(entry_time="2024-01-01T10:00:00")]
        act = [_make_trade(entry_time="2024-01-01T10:00:00Z")]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1


class TestMatchResultStructure:
    def test_match_result_is_pydantic(self):
        result = TradeMatcher.match([], [])
        assert isinstance(result, MatchResult)

    def test_match_key_in_result(self):
        ref = [_make_trade(symbol="EURUSD", direction="LONG")]
        act = [_make_trade(symbol="EURUSD", direction="LONG")]
        result = TradeMatcher.match(ref, act)
        assert len(result.matched) == 1
        _, _, key = result.matched[0]
        assert isinstance(key, MatchKey)
        assert key.symbol == "EURUSD"
        assert key.direction == "LONG"
        assert "2024-01-01T10:00:00" in key.entry_time
        assert "2024-01-01T11:00:00" in key.exit_time

    def test_match_key_uses_utc(self):
        ref = [_make_trade(entry_time="2024-01-01T12:00:00+02:00")]
        act = [_make_trade(entry_time="2024-01-01T10:00:00Z")]
        result = TradeMatcher.match(ref, act)
        _, _, key = result.matched[0]
        assert key.entry_time == "2024-01-01T10:00:00Z"


class TestCostBasedTieBreak:
    """C4: minimum cost wins ties — array order no longer decides."""

    def test_closer_price_wins_over_earlier_index(self) -> None:
        from validation.comparison.matcher import TradeMatcher

        ref = [_make_trade(trade_id=1, entry_price=1.1000)]
        # First actual is eligible but far in price; second is close.
        act = [
            _make_trade(trade_id=2, entry_price=1.1500),
            _make_trade(trade_id=3, entry_price=1.1001),
        ]
        result = TradeMatcher.match(ref, act, time_tolerance_seconds=60)
        assert len(result.matched) == 1
        assert result.matched[0][1].id == 3

    def test_exact_tie_keeps_lowest_index(self) -> None:
        from validation.comparison.matcher import TradeMatcher

        ref = [_make_trade(trade_id=1)]
        act = [_make_trade(trade_id=2), _make_trade(trade_id=3)]
        result = TradeMatcher.match(ref, act, time_tolerance_seconds=60)
        assert len(result.matched) == 1
        assert result.matched[0][1].id == 2

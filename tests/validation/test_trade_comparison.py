"""Tests for validation.comparison.comparison.TradeComparator."""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.comparison.comparison import (
    FieldComparisonResult,
    MatchedTrade,
    TradeComparator,
    TradeComparisonConfig,
    TradeComparisonResult,
)
from validation.comparison.matcher import MatchKey


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    position_size: float | None = None,
    pnl: float = 50.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> Trade:
    meta: dict[str, object] = {}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    if take_profit is not None:
        meta["take_profit"] = take_profit
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
        position_size=position_size,
        meta=meta,
    )


def _make_matched(
    ref: Trade | None = None,
    actual: Trade | None = None,
) -> MatchedTrade:
    r = ref or _make_trade()
    a = actual or _make_trade()
    key = MatchKey(
        entry_time=r.entry_time,
        exit_time=r.last_exit_time,
        symbol=r.symbol,
        direction=r.direction,
    )
    return MatchedTrade(reference=r, actual=a, key=key)


def _assert_field_passed(result: TradeComparisonResult, field: str) -> None:
    assert result.field_results[field].passed, result.field_results[field].message


def _assert_field_failed(result: TradeComparisonResult, field: str) -> None:
    assert not result.field_results[field].passed, result.field_results[field].message


class TestExactMatch:
    def test_identical_trades_match(self):
        matched = _make_matched()
        comp = TradeComparator()
        result = comp.compare(matched)
        assert result.is_match is True
        assert result.mismatches == []
        assert len(result.field_results) == 10
        for name, field_result in result.field_results.items():
            assert field_result.passed, f"Field {name} should pass"

    def test_all_defaults_produce_match(self):
        ref = _make_trade()
        act = _make_trade()
        matched = _make_matched(ref, act)
        result = TradeComparator().compare(matched)
        assert result.is_match
        assert result.mismatches == []


class TestDirectionMismatch:
    def test_long_vs_short_fails(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        result = TradeComparator().compare(_make_matched(ref, act))
        assert result.is_match is False
        _assert_field_failed(result, "direction")
        assert "mismatch" in result.field_results["direction"].message.lower()

    def test_short_vs_long_fails(self):
        ref = _make_trade(direction="SHORT")
        act = _make_trade(direction="LONG")
        result = TradeComparator().compare(_make_matched(ref, act))
        assert result.is_match is False
        _assert_field_failed(result, "direction")


class TestEntryTime:
    def test_entry_time_within_default_tolerance(self):
        ref = _make_trade(entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(entry_time="2024-01-01T10:00:30Z")
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "entry_time")
        assert result.is_match

    def test_entry_time_outside_default_tolerance(self):
        ref = _make_trade(entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(entry_time="2024-01-01T10:01:30Z")
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "entry_time")
        assert not result.is_match
        assert "entry_time" in result.mismatches

    def test_entry_time_custom_tolerance(self):
        ref = _make_trade(entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(entry_time="2024-01-01T10:01:30Z")
        config = TradeComparisonConfig(entry_time_seconds=120.0)
        result = TradeComparator(config).compare(_make_matched(ref, act))
        _assert_field_passed(result, "entry_time")
        assert result.is_match


class TestExitTime:
    def test_exit_time_within_default_tolerance(self):
        ref = _make_trade(exit_time="2024-01-01T11:00:00Z")
        act = _make_trade(exit_time="2024-01-01T11:00:30Z")
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "exit_time")
        assert result.is_match

    def test_exit_time_outside_default_tolerance(self):
        ref = _make_trade(exit_time="2024-01-01T11:00:00Z")
        act = _make_trade(exit_time="2024-01-01T11:01:30Z")
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "exit_time")
        assert not result.is_match
        assert "exit_time" in result.mismatches

    def test_exit_time_custom_tolerance(self):
        ref = _make_trade(exit_time="2024-01-01T11:00:00Z")
        act = _make_trade(exit_time="2024-01-01T11:01:30Z")
        config = TradeComparisonConfig(exit_time_seconds=120.0)
        result = TradeComparator(config).compare(_make_matched(ref, act))
        _assert_field_passed(result, "exit_time")
        assert result.is_match


class TestEntryPrice:
    def test_entry_price_within_tolerance(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.10005)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "entry_price")
        assert result.is_match

    def test_entry_price_outside_tolerance(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1005)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "entry_price")
        assert not result.is_match
        assert "entry_price" in result.mismatches


class TestExitPrice:
    def test_exit_price_within_tolerance(self):
        ref = _make_trade(exit_price=1.1050)
        act = _make_trade(exit_price=1.10505)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "exit_price")
        assert result.is_match

    def test_exit_price_outside_tolerance(self):
        ref = _make_trade(exit_price=1.1050)
        act = _make_trade(exit_price=1.1055)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "exit_price")
        assert not result.is_match
        assert "exit_price" in result.mismatches


class TestVolume:
    def test_volume_within_tolerance(self):
        ref = _make_trade(quantity=1000.0)
        act = _make_trade(quantity=1005.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "volume")
        assert result.is_match

    def test_volume_outside_tolerance(self):
        ref = _make_trade(quantity=1000.0)
        act = _make_trade(quantity=1050.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "volume")
        assert not result.is_match
        assert "volume" in result.mismatches


class TestPositionSize:
    def test_position_size_within_tolerance(self):
        ref = _make_trade(position_size=10000.0)
        act = _make_trade(position_size=10005.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "position_size")
        assert result.is_match

    def test_position_size_outside_tolerance(self):
        ref = _make_trade(position_size=10000.0)
        act = _make_trade(position_size=10500.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "position_size")
        assert not result.is_match
        assert "position_size" in result.mismatches

    def test_position_size_both_none_matches(self):
        ref = _make_trade(position_size=None)
        act = _make_trade(position_size=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "position_size")
        assert result.is_match

    def test_position_size_none_vs_float_mismatch(self):
        ref = _make_trade(position_size=10000.0)
        act = _make_trade(position_size=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "position_size")
        assert not result.is_match


class TestPnL:
    def test_pnl_within_tolerance(self):
        ref = _make_trade(pnl=50.0)
        act = _make_trade(pnl=50.005)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "pnl")
        assert result.is_match

    def test_pnl_outside_pct_tolerance(self):
        # pct diff ~4%, outside 0.1%, abs diff 2.0, outside 0.01
        ref = _make_trade(pnl=50.0)
        act = _make_trade(pnl=52.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "pnl")
        assert not result.is_match
        assert "pnl" in result.mismatches

    def test_pnl_drift_inside_rel_band_but_outside_abs_band(self):
        # ref=100000, act=100000.1: rel diff 1e-6 (inside 0.001), abs diff 0.1
        # (outside the 0.01 abs band). DV-B35: both bands must hold, so this
        # fails - loosen the abs band explicitly when drift of this size is
        # acceptable.
        ref = _make_trade(pnl=100000.0)
        act = _make_trade(pnl=100000.1)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "pnl")
        assert not result.is_match

        config = TradeComparisonConfig(pnl_abs=1.0)
        loosened = TradeComparator(config).compare(_make_matched(ref, act))
        _assert_field_passed(loosened, "pnl")

    def test_zero_pnl_exact_match(self):
        ref = _make_trade(pnl=0.0)
        act = _make_trade(pnl=0.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "pnl")
        assert result.is_match

    def test_zero_vs_nonzero_pnl_mismatch(self):
        # ref=0, act=0.001: pct diff=1.0 (100%), abs diff=0.001.
        # DV-B35: a completely wrong PnL is not rescued by the abs band.
        ref = _make_trade(pnl=0.0)
        act = _make_trade(pnl=0.001)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "pnl")
        assert not result.is_match


class TestStopLoss:
    def test_sl_both_none_matches(self):
        ref = _make_trade(stop_loss=None)
        act = _make_trade(stop_loss=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "stop_loss")
        assert result.is_match

    def test_sl_none_vs_float_mismatch(self):
        ref = _make_trade(stop_loss=1.0900)
        act = _make_trade(stop_loss=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "stop_loss")
        assert not result.is_match
        assert "stop_loss" in result.mismatches

    def test_sl_float_vs_float_within_tolerance(self):
        ref = _make_trade(stop_loss=1.0900)
        act = _make_trade(stop_loss=1.09005)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "stop_loss")
        assert result.is_match

    def test_sl_float_vs_float_outside_tolerance(self):
        ref = _make_trade(stop_loss=1.0900)
        act = _make_trade(stop_loss=1.0910)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "stop_loss")
        assert not result.is_match


class TestTakeProfit:
    def test_tp_both_none_matches(self):
        ref = _make_trade(take_profit=None)
        act = _make_trade(take_profit=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "take_profit")
        assert result.is_match

    def test_tp_none_vs_float_mismatch(self):
        ref = _make_trade(take_profit=1.1100)
        act = _make_trade(take_profit=None)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "take_profit")
        assert not result.is_match
        assert "take_profit" in result.mismatches

    def test_tp_float_vs_float_within_tolerance(self):
        ref = _make_trade(take_profit=1.1100)
        act = _make_trade(take_profit=1.11005)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "take_profit")
        assert result.is_match

    def test_tp_float_vs_float_outside_tolerance(self):
        ref = _make_trade(take_profit=1.1100)
        act = _make_trade(take_profit=1.1110)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "take_profit")
        assert not result.is_match


class TestStopLevelResolution:
    """#85: SL/TP must be resolved from Position-level fields with meta as
    fallback — a meta-only read saw None on BOTH sides for native trades and
    certified "both None (match)" without ever comparing a stop."""

    def test_sl_resolved_from_meta_sl_price_fallback(self):
        """A trade carrying its stop under the engine's ``sl_price`` meta key
        must be compared, not skipped as both-None."""
        ref = _make_trade(stop_loss=None)
        act = _make_trade(stop_loss=None)
        object.__setattr__(act, "meta", {"sl_price": 1.0900})
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "stop_loss")
        assert not result.is_match

    def test_sl_position_attribute_wins_over_meta(self):
        """A Position-level stop_loss attribute takes precedence over meta."""
        ref = _make_trade(stop_loss=1.0900, take_profit=1.1100)
        act = _make_trade(stop_loss=None, take_profit=1.1100)
        object.__setattr__(act, "stop_loss", 1.0900)
        # NOTE: preserve take_profit in meta — overwriting meta without it
        # would create an unrelated TP mismatch and mask the SL assertion.
        object.__setattr__(act, "meta", {"stop_loss": 1.9999, "take_profit": 1.1100})
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "stop_loss")
        assert result.is_match

    def test_tp_meta_none_vs_real_value_is_mismatch(self):
        """None vs a real stop is a mismatch, never 'both None (match)'."""
        ref = _make_trade(take_profit=None)
        act = _make_trade(take_profit=None)
        object.__setattr__(act, "meta", {"tp_price": 1.1100})
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "take_profit")
        assert not result.is_match


class TestCombinedMismatches:
    def test_multiple_mismatches_reported(self):
        ref = _make_trade(
            direction="LONG",
            entry_price=1.1000,
            quantity=1000.0,
            stop_loss=1.0900,
        )
        act = _make_trade(
            direction="SHORT",
            entry_price=1.1050,
            quantity=500.0,
            stop_loss=None,
        )
        result = TradeComparator().compare(_make_matched(ref, act))
        assert result.is_match is False
        assert "direction" in result.mismatches
        assert "entry_price" in result.mismatches
        assert "volume" in result.mismatches
        assert "stop_loss" in result.mismatches
        # Fields that should still match
        _assert_field_passed(result, "entry_time")
        _assert_field_passed(result, "exit_time")


class TestConfigCustomization:
    def test_custom_tolerance_allows_larger_deviation(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1100)
        default = TradeComparator().compare(_make_matched(ref, act))
        assert not default.is_match

        config = TradeComparisonConfig(entry_price_pct=0.02)
        custom = TradeComparator(config).compare(_make_matched(ref, act))
        assert custom.is_match

    def test_custom_pnl_tolerance(self):
        ref = _make_trade(pnl=100.0)
        act = _make_trade(pnl=100.2)
        default = TradeComparator().compare(_make_matched(ref, act))
        # default abs tol 0.01, diff 0.2 -> fail (both bands must hold)
        assert not default.is_match

        config = TradeComparisonConfig(pnl_pct=0.01, pnl_abs=1.0)
        custom = TradeComparator(config).compare(_make_matched(ref, act))
        assert custom.is_match


class TestFiniteTolerances:
    def test_nonfinite_trade_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            TradeComparisonConfig(pnl_abs=float("nan"))
        with pytest.raises(ValueError, match="finite"):
            TradeComparisonConfig(entry_price_pct=float("inf"))

    def test_negative_trade_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            TradeComparisonConfig(volume_pct=-0.1)


class TestStructure:
    def test_result_types(self):
        matched = _make_matched()
        result = TradeComparator().compare(matched)
        assert isinstance(result, TradeComparisonResult)
        assert isinstance(result.matched_trade, MatchedTrade)
        for field_result in result.field_results.values():
            assert isinstance(field_result, FieldComparisonResult)

    def test_mismatches_list_only_failed_fields(self):
        ref = _make_trade(direction="LONG", entry_price=1.1000)
        act = _make_trade(direction="SHORT", entry_price=1.1000)
        result = TradeComparator().compare(_make_matched(ref, act))
        assert result.mismatches == ["direction"]


class TestPnLAndGate:
    """DV-B35: the PnL gate must require both tolerances to hold (AND)."""

    def test_zero_vs_tiny_pnl_fails_under_default_bands(self):
        # abs diff 0.001 <= 0.01 but relative diff is 1.0 (one side exactly 0)
        ref = _make_trade(pnl=0.0)
        act = _make_trade(pnl=0.001)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_failed(result, "pnl")
        assert not result.is_match

    def test_large_pnl_drift_inside_abs_band_fails(self):
        # abs diff 5.0 <= 10.0 abs band, but rel diff ~10% > 0.001
        config = TradeComparisonConfig(pnl_abs=10.0)
        ref = _make_trade(pnl=50.0)
        act = _make_trade(pnl=55.0)
        result = TradeComparator(config).compare(_make_matched(ref, act))
        _assert_field_failed(result, "pnl")

    def test_both_bands_satisfied_passes(self):
        config = TradeComparisonConfig(pnl_pct=0.01, pnl_abs=1.0)
        ref = _make_trade(pnl=100.0)
        act = _make_trade(pnl=100.2)
        result = TradeComparator(config).compare(_make_matched(ref, act))
        _assert_field_passed(result, "pnl")
        assert result.is_match

    def test_zero_pnl_exact_match_still_passes(self):
        ref = _make_trade(pnl=0.0)
        act = _make_trade(pnl=0.0)
        result = TradeComparator().compare(_make_matched(ref, act))
        _assert_field_passed(result, "pnl")

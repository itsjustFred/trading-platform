"""Tests for the dedicated FixedRiskModel.

Covers:
- Basic fixed-risk calculation
- LONG / SHORT directions
- Negative stop-loss values
- Stop-loss equal to entry -> error
- Rounding on lots
- min/max position-size clamping
- Risk-per-trade calculation
- Validation of missing fields
- Meta extraction of SL from trade.meta
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.models import Trade, TradeExit
from validation.risk.fixed_risk import FixedRiskConfig, FixedRiskModel
from validation.risk.models import RiskSizingInput

# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_trade(
    trade_id: int = 1,
    direction: str = "LONG",
    entry_price: float = 100.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    meta: dict[str, Any] | None = None,
) -> Trade:
    merged_meta: dict[str, Any] = dict(meta) if meta else {}
    if stop_loss is not None:
        merged_meta["stop_loss"] = stop_loss
    if take_profit is not None:
        merged_meta["take_profit"] = take_profit
    return Trade(
        id=trade_id,
        symbol="EURUSD",
        direction=direction,
        entry_time="2024-01-01T10:00:00Z",
        entry_price=entry_price,
        quantity=1.0,
        exits=[TradeExit(time="2024-01-01T11:00:00Z", price=entry_price, quantity=1.0)],
        pnl=0.0,
        meta=merged_meta,
    )


def _make_input(
    entry_price: float = 100.0,
    stop_loss: float | None = 99.0,
    take_profit: float | None = None,
    direction: str = "LONG",
    trade: Trade | None = None,
    account_balance: float | None = None,
) -> RiskSizingInput:
    return RiskSizingInput(
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        direction=direction,
        trade=trade,
        account_balance=account_balance,
    )


# ─── Basic calculation ──────────────────────────────────────────────────────


def test_basic_fixed_risk_calculation() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0)
    result = model.size(inp)
    assert len(result.profiles) == 1
    assert result.profiles[0].position_size == pytest.approx(100.0)
    assert result.profiles[0].risk_amount == pytest.approx(100.0)


def test_lot_rounding_never_exceeds_risk_budget() -> None:
    # raw size = 100/0.4 = 250 ... use a value that rounds UP under round() but
    # would breach the risk budget: 100 / (1/2.6) -> 260 lots? Instead pick a
    # fractional lot count and assert realized risk stays within budget.
    config = FixedRiskConfig(risk_amount=100.0, round_lots=True, lot_size=1.0)
    model = FixedRiskModel(config)
    # stop_distance = 0.385 -> raw size = 259.74 ; round() -> 260 (risk 100.1),
    # floor -> 259 (risk 99.7), which must not exceed the 100.0 budget.
    inp = _make_input(entry_price=100.0, stop_loss=99.615)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(259.0)
    assert result.profiles[0].risk_amount <= 100.0 + 1e-9


def test_long_direction_preserved() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, direction="LONG")
    result = model.size(inp)
    assert result.profiles[0].direction == "LONG"


def test_short_direction_preserved() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=101.0, direction="SHORT")
    result = model.size(inp)
    assert result.profiles[0].direction == "SHORT"


# ─── Edge cases / errors ────────────────────────────────────────────────────


def test_negative_stop_loss() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=50.0, stop_loss=-50.0)
    with pytest.raises(ValueError, match="stop_loss must be finite and > 0"):
        model.size(inp)


def test_stop_equals_entry_raises() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=100.0)
    with pytest.raises(ValueError, match="stop_distance must be > 0"):
        model.size(inp)


def test_zero_stop_loss_with_nonzero_entry() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=0.0)
    with pytest.raises(ValueError, match="stop_loss must be finite and > 0"):
        model.size(inp)


# ─── Lot rounding ───────────────────────────────────────────────────────────


def test_round_lots_enabled() -> None:
    config = FixedRiskConfig(risk_amount=100.0, round_lots=True, lot_size=10.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=98.0)
    result = model.size(inp)
    # raw = 100 / 2 = 50 -> already a multiple of 10
    assert result.profiles[0].position_size == pytest.approx(50.0)


def test_round_lots_rounds_up() -> None:
    config = FixedRiskConfig(risk_amount=100.0, round_lots=True, lot_size=10.0)
    model = FixedRiskModel(config)
    # distance = 3 -> raw = 33.33 -> rounds to 30 (nearest multiple of 10)
    inp = _make_input(entry_price=100.0, stop_loss=97.0)
    result = model.size(inp)
    # 100 / 3 = 33.33 -> round(33.33/10) = round(3.333) = 3 -> 3*10 = 30
    assert result.profiles[0].position_size == pytest.approx(30.0)


def test_round_lots_disabled() -> None:
    config = FixedRiskConfig(risk_amount=100.0, round_lots=False, lot_size=10.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0)
    result = model.size(inp)
    # 100 / 3 = 33.333...
    assert result.profiles[0].position_size == pytest.approx(100.0 / 3.0, rel=1e-6)


def test_lot_size_zero_disables_rounding() -> None:
    config = FixedRiskConfig(risk_amount=100.0, round_lots=True, lot_size=0.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0 / 3.0, rel=1e-6)


# ─── Min / max clamping ─────────────────────────────────────────────────────


def test_min_position_size_clamping_zero() -> None:
    """DV-B48: a size below min_position_size fails closed (raises)."""
    config = FixedRiskConfig(risk_amount=100.0, min_position_size=50.0)
    model = FixedRiskModel(config)
    # distance = 10 -> size = 10, below min 50 -> raises instead of 0
    inp = _make_input(entry_price=100.0, stop_loss=90.0)
    with pytest.raises(ValueError, match="minimum position size"):
        model.size(inp)


def test_min_position_size_not_clamped_when_above() -> None:
    config = FixedRiskConfig(risk_amount=100.0, min_position_size=5.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_max_position_size_clamping() -> None:
    config = FixedRiskConfig(risk_amount=100.0, max_position_size=20.0)
    model = FixedRiskModel(config)
    # distance = 1 -> size = 100, max = 20 -> clamped
    inp = _make_input(entry_price=100.0, stop_loss=99.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(20.0)
    assert result.profiles[0].risk_amount == pytest.approx(20.0)


def test_max_position_size_none_no_clamping() -> None:
    config = FixedRiskConfig(risk_amount=100.0, max_position_size=None)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0)


# ─── Risk per trade & derived metrics ───────────────────────────────────────


def test_risk_per_trade_calculated() -> None:
    config = FixedRiskConfig(risk_amount=200.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=200.0, stop_loss=198.0)
    result = model.size(inp)
    # size = 200 / 2 = 100, risk = 100 * 2 = 200
    assert result.profiles[0].risk_amount == pytest.approx(200.0)
    assert result.total_risk_amount == pytest.approx(200.0)


def test_stop_distance_pct_when_entry_nonzero() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0)
    result = model.size(inp)
    # stop_distance = 1.0 -> 1% of 100
    # Note: TradeRiskProfile doesn't have stop_distance_pct field.
    # We verify stop_distance instead.
    assert result.profiles[0].stop_distance == pytest.approx(1.0)


def test_account_balance_risk_percent() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].risk_percent == pytest.approx(0.01)
    assert result.total_risk_percent == pytest.approx(0.01)


def test_account_balance_none_risk_percent_zero() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=None)
    result = model.size(inp)
    assert result.profiles[0].risk_percent == 0.0


# ─── Validation ─────────────────────────────────────────────────────────────


def test_validate_missing_stop_loss_raises() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    inp = _make_input(entry_price=100.0, stop_loss=None)
    with pytest.raises(ValueError, match="stop_loss is required"):
        model.validate(inp)


def test_validate_missing_entry_price_finite() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    inp = _make_input(entry_price=float("nan"), stop_loss=99.0)
    with pytest.raises(ValueError, match="entry_price is required"):
        model.validate(inp)


def test_validate_infinite_stop_loss_raises() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    inp = _make_input(entry_price=100.0, stop_loss=float("inf"))
    with pytest.raises(ValueError, match="stop_loss must be finite"):
        model.validate(inp)


# ─── Meta extraction ────────────────────────────────────────────────────────


def test_validate_extracts_stop_loss_from_trade_meta() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=None, trade=trade)
    model.validate(inp)
    assert inp.stop_loss == pytest.approx(98.0)


def test_size_uses_stop_loss_from_trade_meta() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=None, trade=trade)
    result = model.size(inp)
    # distance = 2 -> size = 50
    assert result.profiles[0].position_size == pytest.approx(50.0)
    assert result.profiles[0].stop_loss == pytest.approx(98.0)


def test_validate_prefers_direct_stop_loss_over_meta() -> None:
    model = FixedRiskModel(FixedRiskConfig(risk_amount=100.0))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=97.0, trade=trade)
    model.validate(inp)
    assert inp.stop_loss == pytest.approx(97.0)


# ─── Risk-reward ratio ──────────────────────────────────────────────────────


def test_risk_reward_ratio_with_take_profit() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, take_profit=102.0)
    result = model.size(inp)
    # risk = 1, reward = 2 -> RR = 2.0
    assert result.profiles[0].risk_reward_ratio == pytest.approx(2.0)


def test_risk_reward_ratio_none_without_take_profit() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, take_profit=None)
    result = model.size(inp)
    assert result.profiles[0].risk_reward_ratio is None


# ─── Trade identity ─────────────────────────────────────────────────────────


def test_trade_id_extracted_from_input_trade() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    trade = _make_trade(trade_id=42)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, trade=trade)
    result = model.size(inp)
    assert result.profiles[0].trade_id == 42


def test_trade_id_zero_when_no_trade() -> None:
    config = FixedRiskConfig(risk_amount=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, trade=None)
    result = model.size(inp)
    assert result.profiles[0].trade_id == 0


# ─── Config defaults ────────────────────────────────────────────────────────


def test_config_defaults() -> None:
    config = FixedRiskConfig(risk_amount=50.0)
    assert config.min_position_size == 0.0
    assert config.max_position_size is None
    assert config.round_lots is True
    assert config.lot_size == 1.0


# ─── Min-size fail-closed (DV-B48) ─────────────────────────────────────────


def test_sub_minimum_size_raises_instead_of_silent_zero() -> None:
    """DV-B48: a computed size below min_position_size must raise, not zero."""
    config = FixedRiskConfig(risk_amount=100.0, min_position_size=50.0)
    model = FixedRiskModel(config)
    # distance = 10 -> raw size 10 < min 50
    inp = _make_input(entry_price=100.0, stop_loss=90.0)
    with pytest.raises(ValueError, match="minimum position size"):
        model.size(inp)


def test_lot_rounded_to_zero_below_minimum_raises() -> None:
    """A lot-floored zero size must fail closed (not positive)."""
    # raw = 100/3 ≈ 33.3 -> floored to a multiple of lot 100 -> 0 < min 1
    config = FixedRiskConfig(
        risk_amount=100.0, round_lots=True, lot_size=100.0, min_position_size=1.0
    )
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0)
    with pytest.raises(ValueError, match="not positive"):
        model.size(inp)


def test_lot_floor_to_zero_raises_even_without_min_size() -> None:
    """A lot-floored 0.0 size is an empty position, never a valid profile."""
    # raw = 100/3 ≈ 33.3 -> floored to a multiple of lot 100 -> 0
    config = FixedRiskConfig(risk_amount=100.0, round_lots=True, lot_size=100.0)
    model = FixedRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0)
    with pytest.raises(ValueError, match="not positive"):
        model.size(inp)

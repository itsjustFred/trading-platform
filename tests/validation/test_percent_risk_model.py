"""Tests for the dedicated PercentRiskModel.

Covers:
- Basic percent-risk calculation
- LONG / SHORT directions
- Account balance <= 0 -> error
- risk_percent <= 0 -> error
- Stop-loss equal to entry -> error
- Rounding on lots
- min/max position-size clamping
- Meta extraction of SL/TP from trade.meta
- Risk-reward ratio
- Integration via RiskAdapter
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.models import Trade, TradeExit
from validation.risk.adapter import RiskAdapter
from validation.risk.models import RiskSizingInput, SizingModel
from validation.risk.percent_risk import PercentRiskConfig, PercentRiskModel

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
    account_balance: float | None = 10_000.0,
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


def test_basic_percent_risk_calculation() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    assert len(result.profiles) == 1
    # risk_amount = 10_000 * 0.01 = 100, stop_distance = 1 -> size = 100
    assert result.profiles[0].position_size == pytest.approx(100.0)
    assert result.profiles[0].risk_amount == pytest.approx(100.0)
    assert result.profiles[0].risk_percent == pytest.approx(0.01)


def test_percent_risk_2_percent() -> None:
    config = PercentRiskConfig(risk_percent=0.02)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    # risk_amount = 200, distance = 1 -> size = 200
    assert result.profiles[0].position_size == pytest.approx(200.0)
    assert result.profiles[0].risk_amount == pytest.approx(200.0)


def test_percent_risk_with_10_pip_stop() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    # entry 1.1000, stop 1.0990 -> distance 0.0010
    inp = _make_input(entry_price=1.1000, stop_loss=1.0990, account_balance=10_000.0)
    result = model.size(inp)
    # risk_amount = 100, distance = 0.001 -> size = 100_000
    # Use rel=1e-5 to account for floating point precision in subtraction
    assert result.profiles[0].position_size == pytest.approx(100_000.0, rel=1e-5)
    assert result.profiles[0].risk_amount == pytest.approx(100.0, rel=1e-4)


# ─── LONG / SHORT ───────────────────────────────────────────────────────────


def test_long_direction_preserved() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, direction="LONG")
    result = model.size(inp)
    assert result.profiles[0].direction == "LONG"


def test_short_direction_preserved() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=101.0, direction="SHORT")
    result = model.size(inp)
    assert result.profiles[0].direction == "SHORT"


def test_short_stop_distance_positive() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=102.0, direction="SHORT")
    result = model.size(inp)
    assert result.profiles[0].stop_distance == pytest.approx(2.0)
    assert result.profiles[0].position_size == pytest.approx(50.0)


# ─── Account balance validation ─────────────────────────────────────────────


def test_account_balance_zero_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=0.0)
    with pytest.raises(ValueError, match="account_balance must be > 0"):
        model.size(inp)


def test_account_balance_negative_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=-1000.0)
    with pytest.raises(ValueError, match="account_balance must be > 0"):
        model.size(inp)


def test_account_balance_none_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=None)
    with pytest.raises(ValueError, match="account_balance is required"):
        model.size(inp)


def test_account_balance_infinite_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=float("inf"))
    with pytest.raises(ValueError, match="account_balance must be > 0"):
        model.size(inp)


# ─── risk_percent validation ───────────────────────────────────────────────


def test_risk_percent_zero_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="risk_percent must be > 0"):
        model.size(inp)


def test_risk_percent_negative_raises() -> None:
    with pytest.raises(ValueError, match="risk_percent must be between 0 and 1"):
        PercentRiskConfig(risk_percent=-0.01)


def test_risk_percent_upper_boundary_is_accepted() -> None:
    config = PercentRiskConfig(risk_percent=1.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].risk_percent == pytest.approx(1.0)


def test_risk_percent_above_one_raises() -> None:
    with pytest.raises(ValueError, match="risk_percent must be between 0 and 1"):
        PercentRiskConfig(risk_percent=1.01)


# ─── Stop-loss equals entry ─────────────────────────────────────────────────


def test_stop_equals_entry_raises() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=100.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="stop_distance must be > 0"):
        model.size(inp)


# ─── Lot rounding ───────────────────────────────────────────────────────────


def test_round_lots_enabled() -> None:
    config = PercentRiskConfig(risk_percent=0.01, round_lots=True, lot_size=10.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=98.0, account_balance=10_000.0)
    result = model.size(inp)
    # risk = 100, distance = 2 -> raw = 50 -> already multiple of 10
    assert result.profiles[0].position_size == pytest.approx(50.0)


def test_round_lots_rounds_down() -> None:
    config = PercentRiskConfig(risk_percent=0.01, round_lots=True, lot_size=10.0)
    model = PercentRiskModel(config)
    # distance = 3 -> raw = 100/3 = 33.33 -> round(33.33/10)=round(3.33)=3 -> 30
    inp = _make_input(entry_price=100.0, stop_loss=97.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(30.0)


def test_round_lots_disabled() -> None:
    config = PercentRiskConfig(risk_percent=0.01, round_lots=False, lot_size=10.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0 / 3.0, rel=1e-6)


def test_lot_size_zero_disables_rounding() -> None:
    config = PercentRiskConfig(risk_percent=0.01, round_lots=True, lot_size=0.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0 / 3.0, rel=1e-6)


# ─── Min / max clamping ─────────────────────────────────────────────────────


def test_min_position_size_clamping_zero() -> None:
    """DV-B48: a size below min_position_size fails closed (raises)."""
    config = PercentRiskConfig(risk_percent=0.01, min_position_size=200.0)
    model = PercentRiskModel(config)
    # distance = 10 -> size = 1, below min 200 -> raises instead of 0
    inp = _make_input(entry_price=100.0, stop_loss=90.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="minimum position size"):
        model.size(inp)


def test_min_position_size_not_clamped_when_above() -> None:
    config = PercentRiskConfig(risk_percent=0.01, min_position_size=50.0)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_max_position_size_clamping() -> None:
    config = PercentRiskConfig(risk_percent=0.01, max_position_size=20.0)
    model = PercentRiskModel(config)
    # distance = 0.5 -> size = 200, max = 20 -> clamped
    inp = _make_input(entry_price=100.0, stop_loss=99.5, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(20.0)
    assert result.profiles[0].risk_amount == pytest.approx(10.0)


def test_max_position_size_none_no_clamping() -> None:
    config = PercentRiskConfig(risk_percent=0.01, max_position_size=None)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].position_size == pytest.approx(100.0)


# ─── Meta extraction ────────────────────────────────────────────────────────


def test_validate_extracts_stop_loss_from_trade_meta() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=None, trade=trade, account_balance=10_000.0)
    model.validate(inp)
    assert inp.stop_loss == pytest.approx(98.0)


def test_size_uses_stop_loss_from_trade_meta() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=None, trade=trade, account_balance=10_000.0)
    result = model.size(inp)
    # distance = 2 -> size = 100/2 = 50
    assert result.profiles[0].position_size == pytest.approx(50.0)
    assert result.profiles[0].stop_loss == pytest.approx(98.0)


def test_validate_prefers_direct_stop_loss_over_meta() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    trade = _make_trade(entry_price=100.0, stop_loss=98.0)
    inp = RiskSizingInput(entry_price=100.0, stop_loss=97.0, trade=trade, account_balance=10_000.0)
    model.validate(inp)
    assert inp.stop_loss == pytest.approx(97.0)


def test_validate_extracts_take_profit_for_risk_reward() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    trade = _make_trade(entry_price=100.0, stop_loss=99.0, take_profit=102.0)
    # The model uses input.take_profit directly; meta extraction is for stop_loss only.
    # But if we pass take_profit=None and want it from meta, that's adapter-level.
    # Here we verify that when take_profit is provided directly, RR is calculated.
    inp2 = RiskSizingInput(
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=trade.meta.get("take_profit"),
        trade=trade,
        account_balance=10_000.0,
    )
    result = model.size(inp2)
    assert result.profiles[0].risk_reward_ratio == pytest.approx(2.0)


# ─── Risk-reward ratio ──────────────────────────────────────────────────────


def test_risk_reward_ratio_with_take_profit() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, take_profit=102.0, account_balance=10_000.0)
    result = model.size(inp)
    # risk = 1, reward = 2 -> RR = 2.0
    assert result.profiles[0].risk_reward_ratio == pytest.approx(2.0)


def test_risk_reward_ratio_none_without_take_profit() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, take_profit=None, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].risk_reward_ratio is None


def test_risk_reward_ratio_with_zero_reward() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, take_profit=100.0, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].risk_reward_ratio is None


# ─── Trade identity ─────────────────────────────────────────────────────────


def test_trade_id_extracted_from_input_trade() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    trade = _make_trade(trade_id=42)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, trade=trade, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].trade_id == 42


def test_trade_id_zero_when_no_trade() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=99.0, trade=None, account_balance=10_000.0)
    result = model.size(inp)
    assert result.profiles[0].trade_id == 0


# ─── Config defaults ────────────────────────────────────────────────────────


def test_config_defaults() -> None:
    config = PercentRiskConfig(risk_percent=0.01)
    assert config.min_position_size == 0.0
    assert config.max_position_size is None
    assert config.round_lots is True
    assert config.lot_size == 1.0


# ─── Validation edge cases ──────────────────────────────────────────────────


def test_validate_missing_entry_price_finite() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    inp = _make_input(entry_price=float("nan"), stop_loss=99.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="entry_price is required"):
        model.validate(inp)


def test_validate_infinite_stop_loss_raises() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    inp = _make_input(entry_price=100.0, stop_loss=float("inf"), account_balance=10_000.0)
    with pytest.raises(ValueError, match="stop_loss must be finite"):
        model.validate(inp)


def test_validate_missing_stop_loss_raises() -> None:
    model = PercentRiskModel(PercentRiskConfig(risk_percent=0.01))
    inp = _make_input(entry_price=100.0, stop_loss=None, account_balance=10_000.0)
    with pytest.raises(ValueError, match="stop_loss is required"):
        model.validate(inp)


# ─── Integration via RiskAdapter ────────────────────────────────────────────


def test_adapter_size_percent_risk_model() -> None:
    adapter = RiskAdapter()
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    config = PercentRiskConfig(risk_percent=0.01)
    result = adapter.size(inp, model=SizingModel.PERCENT_RISK, config=config)
    assert len(result.profiles) == 1
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_adapter_size_fixed_risk_model() -> None:
    from validation.risk.fixed_risk import FixedRiskConfig
    adapter = RiskAdapter()
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    config = FixedRiskConfig(risk_amount=100.0)
    result = adapter.size(inp, model=SizingModel.FIXED_RISK, config=config)
    assert len(result.profiles) == 1
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_adapter_size_percent_risk_default_config() -> None:
    adapter = RiskAdapter()
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = adapter.size(inp, model=SizingModel.PERCENT_RISK)
    # default risk_percent = 0.01 -> size = 100
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_adapter_size_fixed_risk_default_config() -> None:
    adapter = RiskAdapter()
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    result = adapter.size(inp, model=SizingModel.FIXED_RISK)
    # default risk_amount = 100.0 -> size = 100
    assert result.profiles[0].position_size == pytest.approx(100.0)


def test_adapter_size_wrong_config_type_raises() -> None:
    from validation.risk.fixed_risk import FixedRiskConfig
    adapter = RiskAdapter()
    inp = _make_input(entry_price=100.0, stop_loss=99.0, account_balance=10_000.0)
    config = FixedRiskConfig(risk_amount=100.0)
    with pytest.raises(TypeError, match="config must be PercentRiskConfig"):
        adapter.size(inp, model=SizingModel.PERCENT_RISK, config=config)


# ─── Min-size fail-closed (DV-B48) ─────────────────────────────────────────


def test_sub_minimum_size_raises_instead_of_silent_zero() -> None:
    """DV-B48: a computed size below min_position_size must raise, not zero."""
    config = PercentRiskConfig(risk_percent=0.01, min_position_size=200.0)
    model = PercentRiskModel(config)
    # distance = 10 -> raw size 1 < min 200
    inp = _make_input(entry_price=100.0, stop_loss=90.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="minimum position size"):
        model.size(inp)


def test_lot_rounded_to_zero_below_minimum_raises() -> None:
    """A lot-floored zero size must fail closed (not positive)."""
    config = PercentRiskConfig(
        risk_percent=0.01, round_lots=True, lot_size=10_000.0, min_position_size=1.0
    )
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="not positive"):
        model.size(inp)


def test_lot_floor_to_zero_raises_even_without_min_size() -> None:
    """A lot-floored 0.0 size is an empty position, never a valid profile."""
    config = PercentRiskConfig(
        risk_percent=0.01, round_lots=True, lot_size=10_000.0
    )
    model = PercentRiskModel(config)
    inp = _make_input(entry_price=100.0, stop_loss=97.0, account_balance=10_000.0)
    with pytest.raises(ValueError, match="not positive"):
        model.size(inp)

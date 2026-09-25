"""Tests for validation.risk adapter and models.

Covers:
- Trade -> TradeSignal conversion
- RiskAdapter single-trade and aggregate risk calculations
- Fallback behaviour when stop-loss is missing
- LONG / SHORT parity
- Integration with ValidationReference
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from risk.config import RiskConfig
from risk.engine import RiskEngine
from shared.models import Trade, TradeExit
from validation.reference import ValidationReference
from validation.risk.adapter import RiskAdapter, _trade_to_signal
from validation.risk.models import RiskSizingResult, TradeRiskProfile


# ─── Helpers ────────────────────────────────────────────────────────────────


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
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
        meta=merged_meta,
    )


def _make_validation_reference(
    trades: list[Trade],
    symbol: str = "EURUSD",
    timeframe: str = "1h",
    settings: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    raw_metadata: dict[str, Any] | None = None,
    allow_empty: bool = False,
) -> ValidationReference:
    return ValidationReference(
        strategy_name="test_strategy",
        source_framework="unknown",
        symbol=symbol,
        timeframe=timeframe,
        settings=settings or {},
        trades=trades,
        stats=stats or {},
        raw_metadata=raw_metadata or {},
        allow_empty=allow_empty,
    )


# ─── _trade_to_signal ───────────────────────────────────────────────────────


def test_trade_to_signal_long_direction() -> None:
    trade = _make_trade(direction="LONG")
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.action == "buy"


def test_trade_to_signal_short_direction() -> None:
    trade = _make_trade(direction="SHORT")
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.action == "sell"


def test_trade_to_signal_symbol_and_timeframe() -> None:
    trade = _make_trade()
    signal = _trade_to_signal(trade, "GBPUSD", "4h")
    assert signal.symbol == "GBPUSD"
    assert signal.timeframe == "4h"


def test_trade_to_signal_stop_loss_from_meta() -> None:
    trade = _make_trade(stop_loss=1.0900)
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.stop_loss == pytest.approx(1.0900)


def test_trade_to_signal_take_profit_from_meta() -> None:
    trade = _make_trade(take_profit=1.1200)
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.take_profit == pytest.approx(1.1200)


def test_trade_to_signal_account_balance_in_metadata() -> None:
    trade = _make_trade()
    signal = _trade_to_signal(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert signal.metadata.get("account_balance") == pytest.approx(10_000.0)


def test_trade_to_signal_none_stop_loss() -> None:
    trade = _make_trade(stop_loss=None)
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.stop_loss is None


# ─── RiskAdapter single trade ───────────────────────────────────────────────


def test_calculate_trade_risk_long_valid_stop() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900, direction="LONG")
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.direction == "LONG"
    assert profile.stop_loss == pytest.approx(1.0900)
    assert profile.position_size > 0.0


def test_calculate_trade_risk_short_valid_stop() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.1100, direction="SHORT")
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.direction == "SHORT"
    assert profile.stop_loss == pytest.approx(1.1100)
    assert profile.position_size > 0.0


def test_calculate_trade_risk_risk_amount() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900)
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # 1% of 10_000 = 100
    assert profile.risk_amount == pytest.approx(100.0)


def test_calculate_trade_risk_stop_distance() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900)
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.stop_distance == pytest.approx(0.0100)


def test_calculate_trade_risk_risk_reward_ratio() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200)
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # Risk distance = 0.01, Reward distance = 0.02 -> R:R = 2.0
    assert profile.risk_reward_ratio == pytest.approx(2.0)


def test_calculate_trade_risk_none_take_profit_rr_is_none() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900, take_profit=None)
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.risk_reward_ratio is None


def test_calculate_trade_risk_none_stop_fallback() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=None)
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # Engine applies 2% fallback stop -> 1.1000 * 0.02 = 0.022 distance
    assert profile.stop_loss is not None
    assert profile.stop_distance == pytest.approx(1.1000 * 0.02)


def test_calculate_trade_risk_none_stop_fallback_uses_meta_atr() -> None:
    config = RiskConfig(fallback_stop_pct=0.02, default_sl_atr_multiplier=3.0)
    engine = RiskEngine(config=config)
    adapter = RiskAdapter(engine=engine, pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=None, meta={"atr": 0.005})
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # ATR fallback: 0.005 * 3.0 = 0.015
    assert profile.stop_loss is not None
    assert profile.stop_distance == pytest.approx(0.015)


def test_calculate_trade_risk_enforce_stop_direction_rejects_wrong_side() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    # LONG trade but stop ABOVE entry -> invalid
    trade = _make_trade(entry_price=1.1000, stop_loss=1.1200, direction="LONG", meta={"enforce_stop_direction": True})
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.position_size == 0.0
    assert profile.risk_amount == 0.0
    assert profile.risk_percent == 0.0


def test_calculate_trade_risk_zero_account_balance_raises() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900)
    with pytest.raises(ValueError, match="account_balance must be > 0"):
        adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=0.0)


def test_calculate_trade_risk_max_positions_zero_size() -> None:
    config = RiskConfig(max_positions=2)
    engine = RiskEngine(config=config)
    adapter = RiskAdapter(engine=engine, pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900, meta={"current_positions": 2})
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    assert profile.position_size == 0.0


def test_calculate_trade_risk_total_exposure_zero_size() -> None:
    config = RiskConfig(max_total_exposure=0.01)
    engine = RiskEngine(config=config)
    adapter = RiskAdapter(engine=engine, pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0900)
    # current_exposure at max_total_exposure -> no room left
    profile = adapter.calculate_trade_risk(
        trade, "EURUSD", "1h", account_balance=10_000.0, current_exposure=0.01
    )
    assert profile.position_size == 0.0


def test_adapter_custom_pip_value_changes_position_size() -> None:
    trade = _make_trade(entry_price=1.1000, stop_loss=1.0000)
    engine = RiskEngine(RiskConfig())
    adapter1 = RiskAdapter(engine=engine, pip_value=1.0)
    adapter2 = RiskAdapter(engine=engine, pip_value=2.0)
    profile1 = adapter1.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    profile2 = adapter2.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # Same risk amount, double pip value -> half the position size
    assert profile1.risk_amount == pytest.approx(profile2.risk_amount)
    assert profile2.position_size == pytest.approx(profile1.position_size / 2.0)


def test_adapter_default_engine_instantiation() -> None:
    adapter = RiskAdapter()
    assert adapter.engine is not None
    assert adapter.pip_value == 1.0


# ─── RiskSizingResult / aggregates ──────────────────────────────────────────


def test_calculate_validation_reference_risk_aggregation() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trades = [
        _make_trade(trade_id=1, entry_price=1.1000, stop_loss=1.0900),
        _make_trade(trade_id=2, entry_price=1.2000, stop_loss=1.1900),
    ]
    ref = _make_validation_reference(trades=trades)
    result = adapter.calculate_validation_reference_risk(ref, account_balance=10_000.0)
    assert len(result.profiles) == 2
    assert result.total_risk_amount == pytest.approx(200.0)  # 100 + 100
    assert result.total_risk_percent == pytest.approx(0.02)  # 0.01 + 0.01


def test_calculate_validation_reference_risk_profiles_are_correct_type() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trades = [_make_trade(trade_id=1, entry_price=1.1000, stop_loss=1.0900)]
    ref = _make_validation_reference(trades=trades)
    result = adapter.calculate_validation_reference_risk(ref, account_balance=10_000.0)
    for p in result.profiles:
        assert isinstance(p, TradeRiskProfile)


def test_calculate_validation_reference_risk_empty_trades() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    ref = _make_validation_reference(trades=[], allow_empty=True)
    result = adapter.calculate_validation_reference_risk(ref, account_balance=10_000.0)
    assert result.profiles == []
    assert result.total_risk_amount == 0.0
    assert result.total_risk_percent == 0.0


# ─── TradeRiskProfile model ─────────────────────────────────────────────────


def test_trade_risk_profile_model_creation() -> None:
    profile = TradeRiskProfile(
        trade_id=1,
        entry_price=1.1000,
        stop_loss=1.0900,
        take_profit=1.1200,
        direction="LONG",
        position_size=10_000.0,
        risk_amount=100.0,
        risk_percent=0.01,
        stop_distance=0.01,
        risk_reward_ratio=2.0,
    )
    assert profile.trade_id == 1
    assert profile.risk_reward_ratio == pytest.approx(2.0)


def test_risk_sizing_result_allows_aggregate_percent_above_one() -> None:
    result = RiskSizingResult(total_risk_percent=1.5)
    assert result.total_risk_percent == pytest.approx(1.5)


def test_risk_sizing_result_model_creation() -> None:
    profile = TradeRiskProfile(
        trade_id=1,
        entry_price=1.1000,
        stop_loss=1.0900,
        take_profit=None,
        direction="LONG",
        position_size=10_000.0,
        risk_amount=100.0,
        risk_percent=0.01,
        stop_distance=0.01,
        risk_reward_ratio=None,
    )
    result = RiskSizingResult(profiles=[profile], total_risk_amount=100.0, total_risk_percent=0.01)
    assert len(result.profiles) == 1
    assert result.total_risk_amount == pytest.approx(100.0)


# ─── Edge cases ─────────────────────────────────────────────────────────────


def test_calculate_trade_risk_short_with_fallback_stop() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=None, direction="SHORT")
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # For SHORT fallback stop must be ABOVE entry
    assert profile.stop_loss is not None
    assert profile.stop_loss > profile.entry_price


def test_calculate_trade_risk_long_with_fallback_stop() -> None:
    adapter = RiskAdapter(pip_value=1.0)
    trade = _make_trade(entry_price=1.1000, stop_loss=None, direction="LONG")
    profile = adapter.calculate_trade_risk(trade, "EURUSD", "1h", account_balance=10_000.0)
    # For LONG fallback stop must be BELOW entry
    assert profile.stop_loss is not None
    assert profile.stop_loss < profile.entry_price


def test_trade_to_signal_none_take_profit() -> None:
    trade = _make_trade(take_profit=None)
    signal = _trade_to_signal(trade, "EURUSD", "1h")
    assert signal.take_profit is None

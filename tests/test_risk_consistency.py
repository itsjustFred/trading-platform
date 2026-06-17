"""Tests for risk consistency between backtest and live engines."""
import pytest

from datetime import datetime

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.live import size_live_signal
from risk.models import TradeSignal
from shared.models import LiveSignal


def test_risk_engine_and_live_use_same_defaults() -> None:
    """Test that both engines use same RiskConfig defaults."""
    engine = RiskEngine()
    live_config = RiskConfig()
    
    assert engine.config.max_risk_per_trade == live_config.max_risk_per_trade
    assert engine.config.max_total_exposure == live_config.max_total_exposure
    assert engine.config.max_positions == live_config.max_positions
    assert engine.config.default_sl_atr_multiplier == live_config.default_sl_atr_multiplier
    assert engine.config.fallback_stop_pct == live_config.fallback_stop_pct


def test_live_size_rejects_wrong_stop_direction() -> None:
    """Test that live sizing rejects wrong stop direction."""
    signal = TradeSignal(
        action="buy",
        entry_price=100.0,
        stop_loss=105.0,  # Wrong direction for LONG
        take_profit=110.0,
        symbol="EURUSD",
        timeframe="1h",
    )
    live_signal = LiveSignal(
        deployment_id="test-deployment",
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(),
        metadata={
            "enforce_stop_direction": True,
            "entry_price": 100.0,
            "stop_loss": 105.0,  # Wrong direction for LONG
            "take_profit": 110.0,
        },
    )
    
    result = size_live_signal(live_signal, account_balance=100000.0)
    
    assert result.position_size == 0
    assert result.risk_amount == 0
    assert result.risk_percent == 0


def test_backtest_engine_allows_wrong_stop_direction() -> None:
    """Test that backtest engine allows wrong stop direction by default."""
    engine = RiskEngine()
    signal = TradeSignal(
        action="buy",
        entry_price=100.0,
        stop_loss=105.0,  # Wrong direction for LONG
        take_profit=110.0,
        symbol="EURUSD",
        timeframe="1h",
    )
    
    result = engine.calculate_position_size(
        signal=signal,
        account_balance=100000.0,
        pip_value=10.0,
        current_exposure=0.0,
    )
    
    assert result.position_size > 0
    assert result.risk_amount > 0
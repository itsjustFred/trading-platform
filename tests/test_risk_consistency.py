"""Tests for live risk consistency between risk/engine.py and risk/live.py."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.live import size_live_signal
from risk.models import TradeSignal
from shared.models import LiveSignal


class TestRiskConsistency:
    """Parametrized tests to ensure identical sizing logic between engine and live."""

    def test_identical_inputs_identical_outputs(self) -> None:
        """Test that identical inputs produce identical outputs in both paths."""
        config = RiskConfig(
            max_risk_per_trade=0.02,
            max_total_exposure=0.10,
            max_positions=10,
            default_sl_atr_multiplier=2.0,
            fallback_stop_pct=0.02,
        )
        engine = RiskEngine(config)
        
        # Shared inputs
        account_balance = 100000.0
        pip_value = 1.0
        current_exposure = 0.0  # No existing exposure
        
        # Create a test signal
        signal = TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=1.1000,
            stop_loss=1.0950,  # 50 pips below entry
            take_profit=1.1100,
            timeframe="1h",
            metadata={},
        )
        
        # Engine path
        sized_engine = engine.calculate_position_size(
            signal=signal,
            account_balance=account_balance,
            pip_value=pip_value,
            current_exposure=current_exposure,
        )
        
        # Live path (convert TradeSignal to LiveSignal)
        live_signal = LiveSignal(
            deployment_id="test_deployment",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1000,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "stop_loss": 1.0950,
                "take_profit": 1.1100,
                "pip_value": pip_value,
                "current_exposure": current_exposure,
                "max_risk_per_trade": 0.02,
                "max_total_exposure": 0.10,
                "max_positions": 10,
            },
        )
        sized_live = size_live_signal(live_signal, account_balance=account_balance)
        
        # Verify identical sizing
        assert sized_engine.position_size == pytest.approx(sized_live.position_size, rel=1e-6)
        assert sized_engine.risk_amount == pytest.approx(sized_live.risk_amount, rel=1e-6)
        assert sized_engine.risk_percent == pytest.approx(sized_live.risk_percent, rel=1e-6)

    def test_stop_direction_enforcement_difference(self) -> None:
        """Test that live enforces stop direction while backtest does not by default."""
        config = RiskConfig()
        engine = RiskEngine(config)
        
        account_balance = 100000.0
        
        # Signal with "wrong" stop (above entry for buy)
        signal_wrong_stop = TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=1.1000,
            stop_loss=1.1050,  # Above entry - wrong direction!
            take_profit=1.1100,
            timeframe="1h",
            metadata={},  # No enforce_stop_direction set
        )
        
        # Backtest should accept it (enforce_stop_direction defaults to False)
        sized_backtest = engine.calculate_position_size(
            signal=signal_wrong_stop,
            account_balance=account_balance,
            pip_value=1.0,
            current_exposure=0.0,
        )
        assert sized_backtest.position_size > 0
        
        # Live should reject it (enforce_stop_direction is hardcoded True)
        live_signal = LiveSignal(
            deployment_id="test_deployment",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1000,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "stop_loss": 1.1050,
                "take_profit": 1.1100,
                "enforce_stop_direction": True,  # Live forces this
            },
        )
        sized_live = size_live_signal(live_signal, account_balance=account_balance)
        assert sized_live.position_size == 0.0

    def test_atr_fallback_calculation_consistency(self) -> None:
        """Test that ATR-based fallback is calculated identically when stop_loss is None."""
        config = RiskConfig(default_sl_atr_multiplier=2.0)
        engine = RiskEngine(config)
        
        atr = 0.0010  # 10 pips ATR
        
        # Signal with no stop_loss - should use ATR fallback
        signal = TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=1.1000,
            stop_loss=None,  # No stop - should trigger ATR fallback
            take_profit=1.1100,
            timeframe="1h",
            metadata={"atr": atr},
        )
        
        sized = engine.calculate_position_size(
            signal=signal,
            account_balance=100000.0,
            pip_value=10.0,  # Standard pip value for EURUSD
            current_exposure=0.0,
        )
        
        # Risk distance should be ATR * multiplier
        expected_distance = abs(atr * config.default_sl_atr_multiplier)
        # For buy: stop = entry - distance
        expected_stop = signal.entry_price - expected_distance
        
        assert signal.stop_loss == expected_stop
        assert sized.position_size > 0

    def test_fallback_stop_pct_for_high_priced_assets(self) -> None:
        """Test that high-priced assets use fallback_stop_pct correctly when no ATR."""
        config = RiskConfig(fallback_stop_pct=0.02)
        engine = RiskEngine(config)
        
        # High-priced symbol like US30
        signal = TradeSignal(
            action="buy",
            symbol="US30",
            entry_price=4500.0,
            stop_loss=None,  # No stop_loss - should trigger fallback
            take_profit=4600.0,
            timeframe="1h",
            metadata={},  # No ATR, no stop_loss - should use fallback_stop_pct
        )
        
        sized = engine.calculate_position_size(
            signal=signal,
            account_balance=100000.0,
            pip_value=0.1,  # Pip value for index
            current_exposure=0.0,
        )
        
        # Stop should be based on fallback_stop_pct
        expected_distance = signal.entry_price * config.fallback_stop_pct
        expected_stop = signal.entry_price - expected_distance
        
        assert signal.stop_loss == expected_stop
        assert sized.position_size > 0
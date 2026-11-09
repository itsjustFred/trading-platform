"""Tests for risk consistency between backtest and live engines."""
from datetime import datetime

import pytest

from live_runner.daemon import size_live_signal_for_submission
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal


def _live_deployment(**settings: object) -> LiveDeploymentConfig:
    """The deployment the daemon's live sizing reads its RiskConfig from."""
    return LiveDeploymentConfig(
        strategy_name="parity",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        settings=dict(settings),
    )


def test_risk_engine_and_live_use_same_defaults() -> None:
    """Test that both engines use same RiskConfig defaults."""
    engine = RiskEngine()
    live_config = RiskConfig()

    assert engine.config.max_risk_per_trade == live_config.max_risk_per_trade
    assert engine.config.max_total_exposure == live_config.max_total_exposure
    assert engine.config.max_positions == live_config.max_positions
    assert engine.config.default_sl_atr_multiplier == live_config.default_sl_atr_multiplier
    assert engine.config.fallback_stop_pct == live_config.fallback_stop_pct


@pytest.mark.parametrize(
    ("action", "entry_price", "stop_loss", "take_profit", "pip_value", "expected_size"),
    [
        # risk 1% of 100000 = 1000; buy: 1000/(2*10)=50 shares, notional 5000
        # < cap -> uncapped; sell: 1000/(5*10)=20 shares.
        ("buy", 100.0, 98.0, 110.0, 10.0, 50.0),
        ("sell", 100.0, 105.0, 90.0, 10.0, 20.0),
    ],
)
def test_risk_engine_and_live_size_match_for_valid_signals(
    action: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    pip_value: float,
    expected_size: float,
) -> None:
    """Parametrized parity test for identical valid inputs."""
    engine = RiskEngine(RiskConfig(max_notional_per_trade=1.0))
    backtest_signal = TradeSignal(
        action=action,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        symbol="EURUSD",
        timeframe="1h",
    )
    live_signal = LiveSignal(
        deployment_id="test-deployment",
        symbol="EURUSD",
        action=action,
        theoretical_price=entry_price,
        timestamp=datetime.now(),
        metadata={
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
    )

    backtest_result = engine.calculate_position_size(
        signal=backtest_signal,
        account_balance=100000.0,
        pip_value=pip_value,
        current_exposure=0.0,
    )
    # The daemon's live sizing path (the only live sizing entry point, #125).
    live_result = size_live_signal_for_submission(
        live_signal,
        _live_deployment(max_notional_per_trade=1.0),
        account_balance=100000.0,
        pip_value=pip_value,
    )

    assert backtest_result.position_size == pytest.approx(expected_size)
    assert live_result.metadata["qty"] == pytest.approx(expected_size)
    # Uncapped, so the realised risk (#84) equals the 1 % budget.
    assert live_result.metadata["risk_amount"] == pytest.approx(1000.0)
    assert live_result.metadata["risk_percent"] == pytest.approx(0.01)


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

    result = size_live_signal_for_submission(
        live_signal, _live_deployment(), account_balance=100000.0
    )

    assert result.metadata["qty"] == 0
    assert result.metadata["risk_amount"] == 0
    assert result.metadata["risk_percent"] == 0


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

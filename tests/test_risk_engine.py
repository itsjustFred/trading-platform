import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from live_runner.base_adapter import BaseLiveAdapter
from live_runner.daemon import submit_order_with_risk
from risk.engine import RiskEngine
from risk.config import RiskConfig
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal


def test_calculate_position_size_normal_case():
    engine = RiskEngine(
        RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        )
    )
    signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        timeframe="1h",
        metadata={"current_positions": 1},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.risk_percent == 0.01
    assert sized.risk_amount == 100.0
    assert sized.position_size == 50.0
    assert sized.risk_reward_ratio == 2.0


def test_calculate_position_size_exposure_limit_hit():
    engine = RiskEngine(
        RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        )
    )
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1h",
        metadata={"current_positions": 1},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10000.0,
        pip_value=1.0,
        current_exposure=0.05,
    )

    assert sized.position_size == 0.0
    assert sized.risk_amount == 0.0
    assert sized.risk_percent == 0.0
    assert sized.risk_reward_ratio is None


def test_calculate_position_size_missing_stop_loss_uses_fallback():
    engine = RiskEngine(
        RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        )
    )
    signal = TradeSignal(
        action="buy",
        symbol="BTCUSD",
        entry_price=100.0,
        stop_loss=None,
        take_profit=105.0,
        timeframe="1h",
        metadata={"current_positions": 1, "atr": 1.5},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert signal.stop_loss is not None
    assert signal.metadata.get("resolved_stop_loss") == signal.stop_loss
    assert sized.position_size > 0.0
    assert sized.risk_amount == 100.0


@dataclass(slots=True)
class _DummyAdapter(BaseLiveAdapter):
    submitted_signal: LiveSignal | None = None

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal) -> str:
        self.submitted_signal = signal
        return "risk-sized-order"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0


def test_submit_order_with_risk_applies_qty_before_submit():
    adapter = _DummyAdapter()
    deployment = LiveDeploymentConfig(
        id="11111111-1111-1111-1111-111111111111",
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={"risk_per_trade_pct": 0.01, "max_total_exposure": 0.05},
    )
    signal = LiveSignal(
        deployment_id="11111111-1111-1111-1111-111111111111",
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0},
    )

    order_id = asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert order_id == "risk-sized-order"
    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 200.0

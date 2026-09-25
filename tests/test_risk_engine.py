import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from live_runner.base_adapter import BaseLiveAdapter
from live_runner.daemon import submit_order_with_risk
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import RiskDecision, TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal


@pytest.mark.parametrize(
    "field_name",
    ["max_risk_per_trade", "max_total_exposure", "max_notional_per_trade"],
)
@pytest.mark.parametrize("value", [0.0, -0.01, 1.01, math.nan, math.inf])
def test_risk_config_rejects_exposure_outside_fraction_range(field_name, value):
    with pytest.raises(ValueError):
        RiskConfig(**{field_name: value})


@pytest.mark.parametrize(
    "field_name",
    ["max_risk_per_trade", "max_total_exposure", "max_notional_per_trade"],
)
def test_risk_config_accepts_upper_fraction_boundary(field_name):
    config = RiskConfig(**{field_name: 1.0})
    assert getattr(config, field_name) == 1.0


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
    # Notional cap: 50 shares * 100 = 5000 > 0.2 * 10000 -> capped at 20 shares.
    assert sized.position_size == 20.0
    assert sized.decision.metadata["capped_by_notional"] is True
    assert sized.risk_reward_ratio == 2.0
    assert sized.decision is not None
    assert sized.decision.accepted is True
    assert sized.decision.signal_id.startswith("legacy:EURUSD:1h:buy:")
    assert sized.decision.quantity == pytest.approx(20.0)
    assert sized.decision.risk_distance == pytest.approx(2.0)
    assert sized.decision.stop_price_used == pytest.approx(98.0)
    assert sized.decision.take_profit_used == pytest.approx(104.0)
    assert sized.decision.rejection_reason is None
    assert sized.decision.metadata["stop_source"] == "signal"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("account_balance", math.nan),
        ("pip_value", math.nan),
        ("current_exposure", math.nan),
    ],
)
def test_calculate_position_size_rejects_non_finite_inputs(
    field_name: str, value: float
) -> None:
    sizing_inputs = {
        "account_balance": 10_000.0,
        "pip_value": 1.0,
        "current_exposure": 0.0,
    }
    sizing_inputs[field_name] = value
    signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=None,
        timeframe="1h",
    )

    with pytest.raises(ValueError, match=f"{field_name} must be finite"):
        RiskEngine().calculate_position_size(signal=signal, **sizing_inputs)


def test_calculate_position_size_notional_cap_respects_entry_price():
    """The cap converts notional into quantity at the signal's own entry price."""
    engine = RiskEngine(RiskConfig(max_risk_per_trade=0.01, max_total_exposure=0.05))
    sized_high = engine.calculate_position_size(
        signal=TradeSignal(
            action="buy", symbol="HIGH", entry_price=1000.0, stop_loss=980.0,
            take_profit=None, timeframe="1h", metadata={},
        ),
        account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    )
    sized_low = engine.calculate_position_size(
        signal=TradeSignal(
            action="buy", symbol="LOW", entry_price=100.0, stop_loss=98.0,
            take_profit=None, timeframe="1h", metadata={},
        ),
        account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    )

    # Same notional ceiling (0.2 * 10k = 2000) expressed in each asset's units.
    assert sized_high.position_size == pytest.approx(2.0)
    assert sized_low.position_size == pytest.approx(20.0)
    assert sized_high.position_size * 1000.0 == pytest.approx(
        sized_low.position_size * 100.0
    )


def test_calculate_position_size_notional_cap_converts_at_sub_unit_entry_price():
    """Sub-1.0 entry prices must not shortcut the notional conversion: the cap
    divides by the signal's own entry price, whatever its magnitude."""
    engine = RiskEngine(
        RiskConfig(max_risk_per_trade=0.01, max_total_exposure=0.05,
                   max_notional_per_trade=0.5)
    )
    signal = TradeSignal(
        action="buy", symbol="PENNY", entry_price=0.5, stop_loss=0.4999,
        take_profit=None, timeframe="1h", metadata={},
    )

    sized = engine.calculate_position_size(
        signal=signal, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    )

    # Ceiling 0.5 * 10k = 5000 notional -> 5000 / 0.5 entry = 10000 units.
    assert sized.position_size == pytest.approx(10_000.0)
    assert sized.position_size * 0.5 == pytest.approx(5000.0)
    assert sized.decision.metadata["capped_by_notional"] is True


def test_calculate_position_size_reports_realized_risk_when_capped():
    """risk_amount/risk_percent stay the policy figures; the realized stop-loss
    amount of the capped position is reported via metadata."""
    engine = RiskEngine(RiskConfig(max_risk_per_trade=0.01, max_total_exposure=0.05))
    signal = TradeSignal(
        action="buy", symbol="CAPPED", entry_price=100.0, stop_loss=98.0,
        take_profit=None, timeframe="1h", metadata={},
    )

    sized = engine.calculate_position_size(
        signal=signal, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    )

    md = sized.decision.metadata
    assert md["capped_by_notional"] is True
    assert sized.risk_amount == pytest.approx(100.0)  # policy figure
    # Realized: 20 shares * 2.0 distance * 1.0 pip = 40 account currency.
    assert md["realized_risk_amount"] == pytest.approx(40.0)


def test_calculate_position_size_notional_cap_edge_is_inclusive():
    """A position exactly at the ceiling is accepted unchanged (>= would
    spuriously re-write the same quantity and flag the cap)."""
    engine = RiskEngine(
        RiskConfig(max_risk_per_trade=0.01, max_total_exposure=0.05,
                   max_notional_per_trade=1.0)
    )
    signal = TradeSignal(
        action="buy", symbol="EDGE", entry_price=100.0, stop_loss=99.0,
        take_profit=None, timeframe="1h", metadata={},
    )

    sized = engine.calculate_position_size(
        signal=signal, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    )

    # Uncapped risk sizing: 100 / 1 = 100 shares, exactly at the 10_000
    # notional ceiling (max_notional_per_trade=1.0) but not beyond it.
    assert sized.position_size == pytest.approx(100.0)
    assert sized.decision.metadata["capped_by_notional"] is False


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
    assert sized.decision is not None
    assert sized.decision.accepted is False
    assert sized.decision.rejection_reason == "max_total_exposure"


def test_calculate_position_size_blocks_stop_on_wrong_side():
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
        stop_loss=102.0,
        take_profit=110.0,
        timeframe="1h",
        metadata={"current_positions": 0, "enforce_stop_direction": True},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.position_size == 0.0
    assert sized.risk_amount == 0.0
    assert sized.risk_percent == 0.0
    assert sized.decision is not None
    assert sized.decision.accepted is False
    assert sized.decision.rejection_reason == "bad_stop_direction"
    assert sized.decision.stop_price_used == pytest.approx(102.0)


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
    assert sized.decision is not None
    assert sized.decision.accepted is True
    assert sized.decision.stop_price_used == signal.stop_loss
    assert sized.decision.metadata["stop_source"] == "fallback_atr"
    assert ":none:" in sized.decision.signal_id


def test_calculate_position_size_rejects_missing_stop_when_fallback_disabled():
    engine = RiskEngine(RiskConfig(reject_missing_stop=True))
    signal = TradeSignal(
        action="buy",
        symbol="BTCUSD",
        entry_price=100.0,
        stop_loss=None,
        take_profit=105.0,
        timeframe="1h",
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.position_size == 0.0
    assert sized.decision is not None
    assert sized.decision.accepted is False
    assert sized.decision.rejection_reason == "missing_stop"
    assert sized.decision.metadata["fallback_stop_allowed"] is False


def test_legacy_signal_ids_are_unique_and_stable_per_signal():
    engine = RiskEngine()
    first = TradeSignal(
        action="buy", symbol="X", entry_price=100.0, stop_loss=99.0,
        take_profit=None, timeframe="1h",
    )
    second = TradeSignal(
        action="buy", symbol="X", entry_price=100.0, stop_loss=99.0,
        take_profit=None, timeframe="1h",
    )

    first_id = engine.calculate_position_size(
        signal=first, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    ).decision.signal_id
    first_id_again = engine.calculate_position_size(
        signal=first, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    ).decision.signal_id
    second_id = engine.calculate_position_size(
        signal=second, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    ).decision.signal_id

    assert first_id == first_id_again
    assert first_id != second_id

    fallback = TradeSignal(
        action="buy", symbol="X", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h",
    )
    fallback_id = engine.calculate_position_size(
        signal=fallback, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    ).decision.signal_id
    fallback_id_again = engine.calculate_position_size(
        signal=fallback, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0,
    ).decision.signal_id
    assert fallback_id == fallback_id_again


    engine = RiskEngine()
    signal = TradeSignal(
        action="sell",
        symbol="ES",
        entry_price=5000.0,
        stop_loss=5010.0,
        take_profit=None,
        timeframe="1m",
        metadata={"signal_id": "intraday-es-001"},
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=10000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.decision is not None
    assert sized.decision.signal_id == "intraday-es-001"


def test_risk_decision_rejects_malformed_wire_values():
    with pytest.raises(ValueError, match="signal_id"):
        RiskDecision(
            signal_id="",
            accepted=True,
            quantity=0.0,
            risk_amount=0.0,
            risk_percent=0.0,
            risk_distance=None,
            stop_price_used=None,
            take_profit_used=None,
        )

    with pytest.raises(ValueError, match="accepted"):
        RiskDecision(
            signal_id="risk-1",
            accepted="maybe",  # type: ignore[arg-type]
            quantity=0.0,
            risk_amount=0.0,
            risk_percent=0.0,
            risk_distance=None,
            stop_price_used=None,
            take_profit_used=None,
        )

    with pytest.raises(ValueError, match="quantity"):
        RiskDecision(
            signal_id="risk-1",
            accepted=True,
            quantity=math.inf,
            risk_amount=0.0,
            risk_percent=0.0,
            risk_distance=None,
            stop_price_used=None,
            take_profit_used=None,
        )


@dataclass(slots=True)
class _DummyAdapter(BaseLiveAdapter):
    submitted_signal: LiveSignal | None = None

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted_signal = signal
        self.submitted_qty = qty
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
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )

    order_id = asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert order_id == "risk-sized-order"
    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 200.0
    assert adapter.submitted_signal.metadata["explicit_qty"] == 200.0


class TestIssueC1FallbackGuards:
    """C1: no silent healing of bad ATR; unsupported trailing warns."""

    def _engine(self) -> RiskEngine:
        return RiskEngine(
            RiskConfig(
                max_risk_per_trade=0.01,
                max_total_exposure=0.05,
                max_positions=5,
                default_sl_atr_multiplier=2.0,
            )
        )

    def _signal(self, **metadata) -> TradeSignal:
        return TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=100.0,
            stop_loss=None,
            take_profit=105.0,
            timeframe="1h",
            metadata={"current_positions": 1, **metadata},
        )

    @pytest.mark.parametrize("bad_atr", [float("nan"), float("inf"), -1.5, 0.0, "nonsense"])
    def test_non_finite_atr_rejected(self, bad_atr) -> None:
        with pytest.raises(ValueError, match="[Aa][Tt][Rr]"):
            self._engine().calculate_position_size(
                signal=self._signal(atr=bad_atr),
                account_balance=10000.0,
                pip_value=1.0,
                current_exposure=0.0,
            )

    def test_unsupported_trailing_mode_warns_and_leaves_stop(self, caplog) -> None:
        import logging

        signal = self._signal(
            trailing_sl_config={"mode": "supertrend_flip"},
        )
        signal.stop_loss = 98.0
        with caplog.at_level(logging.WARNING, logger="risk.engine"):
            result = self._engine().apply_trailing_stop(signal, market_price=101.0)
        assert result == pytest.approx(98.0)
        assert any("supertrend_flip" in record.message for record in caplog.records)

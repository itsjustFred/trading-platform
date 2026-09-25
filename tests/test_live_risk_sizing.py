"""Live sizing bridge tests: trust boundary and unit contract.

Signal metadata is untrusted publisher payload: it must never set risk limits,
precomputed quantities, exposure or position counts. Policy comes from the
RiskConfig passed by the caller (deployment/runtime config).
"""

from datetime import datetime, timezone

import pytest

from risk.config import RiskConfig
from risk.live import size_live_signal
from shared.models import LiveSignal


def _signal(metadata: dict):
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def test_size_live_signal_sets_qty_for_valid_risk():
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "timeframe": "1m",
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    assert sized.position_size > 0
    assert signal.metadata["qty"] == sized.position_size
    assert signal.metadata["risk_amount"] == sized.risk_amount
    assert signal.metadata["risk_rejected"] is False
    assert signal.metadata["risk_config_source"] == "deployment_runtime"


def test_size_live_signal_blocks_when_max_positions_reached():
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "timeframe": "1m",
        }
    )

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        config=RiskConfig(max_positions=1),
        current_positions=1,
    )

    assert sized.position_size == 0.0
    assert signal.metadata["qty"] == 0.0
    assert signal.metadata["risk_rejected"] is True
    assert signal.metadata["risk_rejection_reason"] == "max_positions"


def test_size_live_signal_ignores_metadata_risk_limits():
    """Metadata cannot raise risk limits (Decision Record: config from
    deployment/runtime only, never the signal payload)."""
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "timeframe": "1m",
            "max_risk_per_trade": 1.0,
            "max_total_exposure": 1.0,
            "max_positions": 999,
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    # Defaults still govern: 1% of 10000 = 100 risk; notional cap applies.
    assert sized.risk_percent == pytest.approx(0.01)
    assert sized.risk_amount == pytest.approx(100.0)
    assert sized.decision is not None
    assert sized.decision.metadata["max_risk_per_trade"] == pytest.approx(0.01)
    assert sized.decision.metadata["max_total_exposure"] == pytest.approx(0.05)


def test_size_live_signal_ignores_metadata_qty_override():
    """A publisher-supplied qty must not bypass the risk engine."""
    signal = _signal(
        {
            "qty": 500,
            "explicit_qty": 500,
            "risk_amount": 0.0,
            "risk_percent": 0.0,
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    # Sized from the fallback stop (2% of entry), not the metadata qty.
    # risk 1% of 10000 = 100 -> 100/(2*1) = 50 shares, but the 20% notional
    # cap (2000/100) binds at 20 shares — the override cannot escape it.
    assert sized.decision is not None
    assert sized.decision.accepted is True
    assert sized.position_size == pytest.approx(20.0)
    assert sized.decision.metadata["capped_by_notional"] is True
    assert signal.metadata["qty"] == pytest.approx(20.0)
    assert "explicit_qty" not in signal.metadata


def test_size_live_signal_ignores_metadata_exposure_state():
    """Exposure/position state is caller-supplied truth, not payload state."""
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "timeframe": "1m",
            "current_positions": 0,
            "current_exposure": 0.0,
        }
    )

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        config=RiskConfig(max_positions=1),
        current_positions=1,
    )

    # The broker truth (1 open position, max 1) binds even though the payload
    # claims a flat account.
    assert sized.position_size == 0.0


def test_size_live_signal_trusted_exposure_parameter_wins_over_metadata():
    """Only the caller-supplied (broker-truth) exposure feeds the cap; payload
    values can neither raise nor lower it (mutation guard)."""
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "timeframe": "1m",
            # Payload claims a nearly-exhausted budget ...
            "current_exposure": 0.049,
        }
    )

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        config=RiskConfig(max_total_exposure=0.05),
        # ... but broker truth says the account is flat: full budget available.
        current_exposure=0.0,
    )

    assert sized.position_size > 0
    assert sized.decision.metadata["current_exposure"] == pytest.approx(0.0)


def test_size_live_signal_blocks_when_exposure_cap_reached():
    signal = _signal({"entry_price": 100.0, "stop_loss": 98.0, "timeframe": "1m"})

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        config=RiskConfig(max_total_exposure=0.02),
        current_exposure=0.05,
    )

    assert sized.position_size == 0.0
    assert signal.metadata["risk_rejection_reason"] == "max_total_exposure"


def test_size_live_signal_enforces_stop_direction():
    signal = _signal(
        {
            "entry_price": 100.0,
            "stop_loss": 105.0,
            "timeframe": "1m",
        }
    )

    sized = size_live_signal(signal, account_balance=10000.0)

    assert sized.position_size == 0.0
    assert signal.metadata["risk_rejection_reason"] == "bad_stop_direction"


def test_size_live_signal_uses_fallback_stop_when_missing():
    signal = _signal({"entry_price": 100.0, "timeframe": "1m"})

    sized = size_live_signal(signal, account_balance=10000.0)

    # fallback_stop_pct 0.02 -> stop at 98; risk 1% -> 100 / (2 * 1) = 50 shares,
    # capped by the 20% notional bound (2000 / 100 = 20 shares).
    assert sized.decision is not None
    assert sized.decision.metadata["stop_source"] == "fallback_percent"
    assert sized.position_size == pytest.approx(20.0)
    assert signal.metadata["resolved_stop_loss"] == pytest.approx(98.0)


def test_size_live_signal_reduce_only_flattens_broker_qty_ignoring_metadata():
    """The trusted reduce-only path takes its quantity from the caller's broker
    position list — payload qty/explicit_qty are ignored and stripped."""
    signal = _signal(
        {
            "entry_price": 100.0,
            "timeframe": "1m",
            "qty": 500,
            "explicit_qty": 500,
            "risk_sizing_mode": "flatten",
        }
    )

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        reduce_only=True,
        reduce_only_qty=7.0,
    )

    assert sized.position_size == pytest.approx(7.0)
    assert signal.metadata["qty"] == pytest.approx(7.0)
    assert "explicit_qty" not in signal.metadata
    assert signal.metadata["risk_sizing_mode"] == "reduce_only"
    assert signal.metadata["risk_rejected"] is False
    assert signal.metadata["risk_amount"] == 0.0


def test_size_live_signal_reduce_only_clamps_to_notional_ceiling():
    signal = _signal({"entry_price": 100.0, "timeframe": "1m"})

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        reduce_only=True,
        reduce_only_qty=1000.0,
    )

    # Even the internal flatten cannot exceed 0.2 * 10k = 2000 notional.
    assert sized.position_size == pytest.approx(20.0)
    assert signal.metadata["qty"] == pytest.approx(20.0)


def test_size_live_signal_reduce_only_zero_qty_is_rejected():
    signal = _signal({"entry_price": 100.0, "timeframe": "1m"})

    sized = size_live_signal(
        signal,
        account_balance=10000.0,
        reduce_only=True,
        reduce_only_qty=0.0,
    )

    assert sized.position_size == 0.0
    assert signal.metadata["risk_rejected"] is True
    assert signal.metadata["risk_rejection_reason"] == "reduce_only_zero_qty"
    assert signal.metadata["risk_sizing_mode"] == "reduce_only_blocked"


def test_risk_config_rejects_non_positive_notional_cap():
    """The notional bound is a hard policy knob, not an optional hint."""
    with pytest.raises(ValueError, match="max_notional_per_trade"):
        RiskConfig(max_notional_per_trade=0.0)
    with pytest.raises(ValueError, match="max_notional_per_trade"):
        RiskConfig(max_notional_per_trade=-0.1)


def test_size_live_signal_rejects_malformed_present_metadata():
    signal = _signal(
        {
            "entry_price": "not-a-number",
            "stop_loss": {"bad": "type"},
            "current_exposure": "oops",
            "current_positions": None,
            "max_risk_per_trade": 0,
        }
    )

    with pytest.raises(ValueError, match="entry_price"):
        size_live_signal(signal, account_balance=10000.0)

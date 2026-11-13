"""The daemon's live sizing entry point and its trust boundary (#125 cleanup).

``live_runner.daemon.size_live_signal_for_submission`` (via
``prepare_order_with_risk``) is the only live sizing function. The former
second entry point ``risk.live.size_live_signal`` had no production caller;
its test guarantees from ``tests/test_live_risk_sizing.py`` live here (or in
the files named next to each test) on the daemon path.

Signal metadata is untrusted publisher payload: it never sets risk limits,
quantities, exposure, position counts or the pip value.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import BaseLiveAdapter, LiveQuote
from risk.config import RiskConfig
from shared.models import LiveDeploymentConfig, LiveSignal


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings=settings,
    )


def _signal(metadata: dict, *, action: str = "buy", price: float = 100.0) -> LiveSignal:
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action=action,
        theoretical_price=price,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def _size(signal: LiveSignal, deployment: LiveDeploymentConfig, **kwargs: Any) -> LiveSignal:
    kwargs.setdefault("account_balance", 10_000.0)
    return daemon.size_live_signal_for_submission(signal, deployment, **kwargs)


@dataclass
class _Broker(BaseLiveAdapter):
    positions: list[dict[str, Any]] = field(default_factory=list)
    submitted: list[tuple[LiveSignal, float | None]] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 10_000.0, 10_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def get_quote(self, symbol: str) -> LiveQuote:
        return LiveQuote(bid=100.0, ask=100.0, timestamp=datetime.now(timezone.utc))


# ── one live sizing entry point ───────────────────────────────────────────────


def test_there_is_no_second_live_sizing_entry_point():
    """risk.live keeps the shared resolver; sizing lives in the daemon only."""
    import risk
    import risk.live

    assert not hasattr(risk.live, "size_live_signal")
    assert "size_live_signal" not in risk.__all__
    assert not hasattr(risk, "size_live_signal")
    assert callable(risk.live.resolve_live_levels)


# ── moved from test_live_risk_sizing.py ───────────────────────────────────────


def test_sizing_sets_qty_and_risk_figures_for_valid_risk():
    """Was test_size_live_signal_sets_qty_for_valid_risk."""
    deployment = _deployment()
    signal = _signal({"entry_price": 100.0, "stop_loss": 98.0, "take_profit": 104.0})

    sized = _size(signal, deployment)

    # 1 % of 10k = 100 risk / distance 2 = 50, capped by the 20 % notional
    # bound (2000 / 100 = 20).
    assert sized.metadata["qty"] == pytest.approx(20.0)
    assert sized.metadata["explicit_qty"] == pytest.approx(20.0)
    # #84: realised risk at the stop, not the policy budget.
    assert sized.metadata["risk_amount"] == pytest.approx(40.0)
    assert sized.metadata["risk_percent"] == pytest.approx(0.004)
    assert sized.metadata["risk_reward_ratio"] == pytest.approx(2.0)
    assert sized.metadata["risk_timeframe"] == "1h"
    assert sized.metadata["resolved_stop_loss"] == pytest.approx(98.0)


def test_sizing_blocks_when_max_positions_reached():
    """Was test_size_live_signal_blocks_when_max_positions_reached."""
    deployment = _deployment(max_positions=1)
    signal = _signal({"stop_loss": 98.0})

    sized = _size(signal, deployment, current_positions=1)

    assert sized.metadata["qty"] == 0.0


def test_sizing_ignores_payload_position_count_in_favour_of_broker_truth():
    """Was test_size_live_signal_ignores_metadata_exposure_state."""
    deployment = _deployment(max_positions=1)
    signal = _signal({"stop_loss": 98.0, "current_positions": 0, "current_exposure": 0.0})

    sized = _size(signal, deployment, current_positions=1)

    assert sized.metadata["qty"] == 0.0
    assert sized.metadata["current_positions"] == 1


def test_trusted_exposure_parameter_wins_over_payload_exposure():
    """Was test_size_live_signal_trusted_exposure_parameter_wins_over_metadata:
    a payload claiming a nearly exhausted budget cannot shrink the size."""
    deployment = _deployment(max_total_exposure=0.05, max_notional_per_trade=1.0)
    signal = _signal({"stop_loss": 98.0, "current_exposure": 0.049})

    sized = _size(signal, deployment, current_exposure=0.0)

    # Full 1 % budget: 100 / 2 = 50 (with 0.049 used it would be 0.001 -> 5).
    assert sized.metadata["qty"] == pytest.approx(50.0)


def test_sizing_blocks_when_exposure_cap_reached():
    """Was test_size_live_signal_blocks_when_exposure_cap_reached."""
    deployment = _deployment(max_total_exposure=0.02)
    signal = _signal({"stop_loss": 98.0})

    sized = _size(signal, deployment, current_exposure=0.05)

    assert sized.metadata["qty"] == 0.0


@pytest.mark.parametrize("alias", ["stop_loss", "sl_price", "stop_price", "resolved_stop_loss"])
def test_sizing_names_the_malformed_stop_alias(alias):
    """Was test_size_live_signal_rejects_malformed_stop_alias."""
    deployment = _deployment()
    signal = _signal({alias: "not-a-price", "take_profit": 104.0})

    with pytest.raises(ValueError, match=alias):
        _size(signal, deployment)


def test_sizing_rejects_malformed_entry_before_other_fields():
    """Was test_size_live_signal_rejects_malformed_present_metadata."""
    deployment = _deployment()
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
        _size(signal, deployment)


def test_deployment_risk_config_rejects_non_positive_notional_cap():
    """Was test_risk_config_rejects_non_positive_notional_cap; also pinned for
    the daemon's deployment config, which is what live sizing uses."""
    with pytest.raises(ValueError, match="max_notional_per_trade"):
        RiskConfig(max_notional_per_trade=0.0)
    with pytest.raises(ValueError, match="max_notional_per_trade"):
        RiskConfig(max_notional_per_trade=-0.1)
    with pytest.raises(ValueError, match="max_notional_per_trade"):
        daemon._risk_config_for_deployment(_deployment(max_notional_per_trade=0.0))


# ── reduce-only guarantees on the real exit path ──────────────────────────────


def test_exit_only_close_takes_broker_qty_and_ignores_payload_quantities():
    """Was test_size_live_signal_reduce_only_flattens_broker_qty_ignoring_metadata.
    The daemon's exit-only path is the live reduce path."""
    deployment = _deployment()
    broker = _Broker(positions=[{"symbol": "AAPL", "qty": 7.0}])
    signal = _signal(
        {
            "exit_only": True,
            "qty": 500,
            "explicit_qty": 500,
            "risk_sizing_mode": "flatten",
        },
        action="sell",
    )

    asyncio.run(daemon.submit_order_with_risk(broker, deployment, signal))

    submitted, qty = broker.submitted[-1]
    assert qty == pytest.approx(7.0)
    assert submitted.metadata["qty"] == pytest.approx(7.0)
    assert submitted.metadata["explicit_qty"] == pytest.approx(7.0)
    assert submitted.metadata["risk_sizing_mode"] == "exit_only"


def test_exit_only_without_a_position_sends_nothing():
    """Was test_size_live_signal_reduce_only_zero_qty_is_rejected: with no
    broker position there is nothing to close, and no order may be sent
    (whether the daemon raises or skips is the exit path's own policy)."""
    deployment = _deployment()
    broker = _Broker(positions=[])
    signal = _signal({"exit_only": True, "qty": 5}, action="sell")

    with contextlib.suppress(ValueError):
        asyncio.run(daemon.submit_order_with_risk(broker, deployment, signal))

    assert broker.submitted == []

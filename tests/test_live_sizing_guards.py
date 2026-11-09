"""Implementation guards behind the #125 acceptance tests.

The acceptance behaviour is pinned in ``test_live_sizing_single_resolver.py``
and ``test_live_entry_quotes.py``; these cover the defensive branches that
only a regression (or a misbehaving broker) can reach.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import LiveQuote
from risk.live import entry_deviation_sl_fraction
from shared.models import LiveDeploymentConfig, LiveSignal


def _deployment() -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={"max_notional_per_trade": 1.0},
    )


def _signal(deployment: LiveDeploymentConfig, metadata: dict) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


class _Adapter:
    def __init__(self, quote=None) -> None:
        self.quote = quote or LiveQuote(bid=100.0, ask=100.0)
        self.submitted: list = []

    async def submit_order(self, signal, *, qty=None):
        self.submitted.append(signal)
        return "order-1"

    async def get_balance(self):
        return 100_000.0, 100_000.0

    async def get_positions(self):
        return []

    async def get_quote(self, symbol):
        return self.quote


# ── sizing / submission agreement ─────────────────────────────────────────────


def test_submission_refuses_a_sized_stop_that_differs_from_the_signal_stop(monkeypatch):
    """If sizing ever reports a different stop than the resolver's signal stop,
    the bracket would not match the size: refuse rather than pick one."""
    real = daemon.size_live_signal_for_submission

    def _drifting(signal, deployment, **kwargs):
        sized = real(signal, deployment, **kwargs)
        return sized.model_copy(
            update={"metadata": {**sized.metadata, "resolved_stop_loss": 90.0}}
        )

    monkeypatch.setattr(daemon, "size_live_signal_for_submission", _drifting)
    deployment = _deployment()
    adapter = _Adapter()
    signal = _signal(deployment, {"stop_loss": 95.0, "take_profit": 110.0})

    with pytest.raises(ValueError, match="differs from the resolved signal stop"):
        asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))
    assert adapter.submitted == []


def test_sizing_overwrites_a_payload_resolved_stop_with_the_engine_stop():
    deployment = _deployment()
    signal = _signal(deployment, {"stop_loss": 95.0, "resolved_stop_loss": 80.0})

    sized = daemon.size_live_signal_for_submission(
        signal, deployment, account_balance=100_000.0
    )

    assert sized.metadata["resolved_stop_loss"] == pytest.approx(95.0)


def test_sizing_reports_no_resolved_stop_when_the_engine_sized_none():
    """No signal stop and fallback disabled: the engine rejects, and sizing
    must not report a stop it never used."""
    deployment = _deployment().model_copy(
        update={"settings": {"allow_fallback_stop": False, "reject_missing_stop": True}}
    )
    signal = _signal(deployment, {"take_profit": 110.0})

    sized = daemon.size_live_signal_for_submission(
        signal, deployment, account_balance=100_000.0
    )

    assert sized.metadata["qty"] == 0.0
    assert "resolved_stop_loss" not in sized.metadata


def test_sizing_rejects_a_negative_trusted_position_count():
    deployment = _deployment()
    signal = _signal(deployment, {"stop_loss": 95.0})

    with pytest.raises(ValueError, match="current_positions"):
        daemon.size_live_signal_for_submission(
            signal, deployment, account_balance=100_000.0, current_positions=-1
        )


def test_gate_rejects_a_quote_that_is_not_a_live_quote():
    deployment = _deployment()
    adapter = _Adapter(quote=SimpleNamespace(bid=100.0, ask=100.0))
    signal = _signal(deployment, {"stop_loss": 95.0, "take_profit": 110.0})

    with pytest.raises(ValueError, match="no usable quote"):
        asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))
    assert adapter.submitted == []


@pytest.mark.parametrize("bad", ["x", None, True])
def test_gate_rejects_non_numeric_quote_sides(bad):
    deployment = _deployment()
    adapter = _Adapter(quote=LiveQuote(bid=bad, ask=100.0))  # type: ignore[arg-type]
    signal = _signal(deployment, {"stop_loss": 95.0, "take_profit": 110.0})

    with pytest.raises(ValueError, match="unusable"):
        asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))


# ── deviation helper ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "entry", "stop", "quote", "expected"),
    [
        ("buy", 100.0, 96.0, 99.0, 0.25),
        ("buy", 100.0, 96.0, 102.0, -0.5),
        ("sell", 100.0, 104.0, 101.0, 0.25),
        ("sell", 100.0, 104.0, 98.0, -0.5),
    ],
)
def test_entry_deviation_sign_convention(action, entry, stop, quote, expected):
    assert entry_deviation_sl_fraction(
        action, entry_price=entry, stop_loss=stop, quote_price=quote
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("action", "entry", "stop", "quote"),
    [
        ("hold", 100.0, 96.0, 99.0),
        ("buy", 100.0, 100.0, 99.0),  # zero stop distance
        ("buy", 100.0, 104.0, 99.0),  # stop above a buy
        ("sell", 100.0, 96.0, 99.0),  # stop below a sell
        ("buy", 0.0, 96.0, 99.0),
        ("buy", 100.0, 96.0, 0.0),
        ("buy", 100.0, -1.0, 99.0),
        ("buy", float("nan"), 96.0, 99.0),
    ],
)
def test_entry_deviation_rejects_invalid_geometry(action, entry, stop, quote):
    with pytest.raises(ValueError):
        entry_deviation_sl_fraction(action, entry_price=entry, stop_loss=stop, quote_price=quote)


# ── cTrader spot handling ─────────────────────────────────────────────────────


def test_ctrader_unreadable_spot_event_fails_the_pending_quote():
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )

    async def _run():
        waiter = module._SpotWaiter(future=asyncio.get_running_loop().create_future())
        adapter._spot_waiters[1] = waiter
        adapter._handle_spot_event(SimpleNamespace(symbolId=1, bid="garbage", ask=1))
        with pytest.raises(ValueError, match="unreadable"):
            await waiter.future

    asyncio.run(_run())


def test_ctrader_spot_event_respects_has_field_presence():
    """A proto2 optional field that was not sent must not be read as 0."""
    from live_runner import ctrader_adapter as module

    class _Proto:
        symbolId = 1
        bid = 0
        ask = 108_520

        def HasField(self, name):  # noqa: N802 - protobuf API
            return name == "ask"

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )

    async def _run():
        waiter = module._SpotWaiter(future=asyncio.get_running_loop().create_future())
        adapter._spot_waiters[1] = waiter
        adapter._handle_spot_event(_Proto())
        assert waiter.bid is None
        assert waiter.ask == pytest.approx(1.0852)
        assert not waiter.future.done()

    asyncio.run(_run())


def test_ctrader_get_quote_without_loaded_spot_messages_raises():
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )
    adapter._connected = True
    adapter._authenticated = True

    with pytest.raises(RuntimeError, match="spot subscription"):
        asyncio.run(adapter.get_quote("EURUSD"))

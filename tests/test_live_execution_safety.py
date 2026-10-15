"""Safety-net tests for live execution, risk sizing boundaries, persistence and recovery.

These pin the behaviors that mutation testing showed were unprotected:
pip scaling, fallback-stop geometry, submit guards, fill persistence/idempotency,
and fail-closed recovery decisions.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pandas as pd
import pytest

from tests.conftest import seed_synthetic_deployment_parents

from live_runner.base_adapter import BaseLiveAdapter
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal


def _alpaca_order_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"alpaca-test:{label}"))


def _make_ctrader_adapter():
    """Bootstrap a detached cTrader adapter for unit-scoped tests."""
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
    )
    return module, adapter


# ── Risk engine boundaries ─────────────────────────────────────────────────────


def _engine(**overrides: Any) -> RiskEngine:
    config = RiskConfig(
        max_risk_per_trade=overrides.pop("max_risk_per_trade", 0.01),
        max_total_exposure=overrides.pop("max_total_exposure", 0.05),
        max_positions=overrides.pop("max_positions", 5),
        default_sl_atr_multiplier=overrides.pop("default_sl_atr_multiplier", 2.0),
        **overrides,
    )
    return RiskEngine(config)


def test_position_size_scales_inversely_with_pip_value():
    """Kills mutant: dropping `* pip` from the sizing formula."""
    signal_kwargs = dict(
        action="buy",
        symbol="EURUSD",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=None,
        timeframe="1h",
    )
    sized_pip_1 = _engine().calculate_position_size(
        signal=TradeSignal(**signal_kwargs, metadata={}),
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    sized_pip_10 = _engine().calculate_position_size(
        signal=TradeSignal(**signal_kwargs, metadata={}),
        account_balance=10_000.0,
        pip_value=10.0,
        current_exposure=0.0,
    )

    # Notional cap binds: 0.2 * 10k = 2000 / entry 100 = 20 shares.
    assert sized_pip_1.position_size == pytest.approx(20.0)
    assert sized_pip_10.position_size == pytest.approx(5.0)


def test_fallback_stop_is_below_entry_for_buys_and_above_for_sells():
    """Kills mutant: fallback stop placed on the wrong side of entry."""
    buy = TradeSignal(
        action="buy", symbol="AAPL", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={"atr": 1.5},
    )
    sell = TradeSignal(
        action="sell", symbol="AAPL", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={"atr": 1.5},
    )
    _engine().calculate_position_size(
        signal=buy, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )
    _engine().calculate_position_size(
        signal=sell, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )

    assert buy.stop_loss is not None, "the long leg must carry a stop loss"
    assert buy.stop_loss < buy.entry_price, "a long stop must sit below entry"
    assert sell.stop_loss is not None, "the short leg must carry a stop loss"
    assert sell.stop_loss > sell.entry_price, "a short stop must sit above entry"


def test_fallback_stop_without_atr_is_percent_of_price():
    """No-ATR fallback must scale with price, never an absolute pip distance.

    With the old absolute $2 fallback, a 100k asset got a stop 0.002% away and an
    absurd position size. Percent-based: distance == entry * fallback_stop_pct.
    """
    signal = TradeSignal(
        action="buy", symbol="BTCUSD", entry_price=100_000.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={},
    )
    sized = _engine().calculate_position_size(
        signal=signal, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )

    expected_distance = 100_000.0 * 0.02
    assert signal.stop_loss == pytest.approx(100_000.0 - expected_distance)
    # risk_amount (1% of 10k) = 100; the 20% notional cap (2000 / 100k entry)
    # binds at 0.02 units — far below the uncapped 0.05, not hundreds of BTC.
    assert sized.position_size == pytest.approx(0.02)
    assert sized.decision.metadata["capped_by_notional"] is True


def test_unknown_deployment_status_is_fail_closed():
    """Kills mutant: unknown status normalized to ACTIVE (trading a corrupt row)."""
    from live_runner.daemon import _normalize_deployment_status

    assert _normalize_deployment_status("ACTIVE") == "ACTIVE"
    assert _normalize_deployment_status("paused") == "PAUSED"
    assert _normalize_deployment_status("DISABLED") == "UNKNOWN"
    assert _normalize_deployment_status("") == "UNKNOWN"
    assert _normalize_deployment_status(None) == "UNKNOWN"

    # The allow-list is exactly the three deployment statuses, and nothing else. Adding a
    # synonym to it turns a fail-closed read into a fail-open one: the value stops being
    # UNKNOWN, and UNKNOWN is what every trading gate compares against.
    #
    # The dangerous additions are not nonsense strings - `DISABLED` already covers those -
    # but plausible operator vocabulary that means the opposite of ACTIVE. `RUNNING` and
    # `RESUMED` are the ones that matter: this repo already uses `running` as a *service*
    # status (ServiceStatus.RUNNING) and `resume` as an operator action, so either can
    # reach a deployment row. Adding either to the allow-list would make a row that does
    # not say ACTIVE trade.
    for near_miss in (
        "RUNNING", "RESUMED", "ENABLED", "ON", "LIVE", "ACTIVE_PENDING",
        "ACTIVE ", " ACTIVE", "ACTIVE	", "ACTIVA", "ACTI",
    ):
        assert _normalize_deployment_status(near_miss) in {"UNKNOWN", "ACTIVE"}, (
            f"{near_miss!r} normalised to something other than UNKNOWN/ACTIVE"
        )
        if near_miss.strip() != near_miss or near_miss != near_miss.strip():
            # Whitespace-padded ACTIVE is not ACTIVE: the comparison is on the exact
            # token, so a padded value must not trade.
            assert _normalize_deployment_status(near_miss) == "UNKNOWN", (
                f"{near_miss!r} traded as if it were ACTIVE"
            )

    for synonym in ("RUNNING", "RESUMED", "ENABLED", "ON", "LIVE"):
        assert _normalize_deployment_status(synonym) == "UNKNOWN", (
            f"{synonym!r} is treated as a tradeable status; only ACTIVE may trade"
        )


# ── Live submission guards ─────────────────────────────────────────────────────


@dataclass
class _PositionsAdapter(BaseLiveAdapter):
    positions: list[dict[str, Any]] = field(default_factory=list)
    submitted_signal: LiveSignal | None = None
    submitted_qty: float | None = None

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted_signal = signal
        self.submitted_qty = qty
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)


class _NoPositionsAdapter(_PositionsAdapter):
    """Adapter whose broker does not expose open positions (B-10 seam default).

    ``get_positions`` support is probed with ``getattr(..., None)`` and a
    ``callable`` check, so overriding it with ``None`` models an adapter (or
    broker API) that cannot report open positions at all.
    """

    get_positions = None  # type: ignore[assignment]


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    # A Discord source, deliberately. These tests cover execution mechanics -
    # fills, reconciliation, risk caps - and all of them presuppose a deployment
    # that can actually trade. `internal` is the model default and used to be
    # passed here as filler, but it has no live evaluator, so an ACTIVE internal
    # deployment is refused at the activation gate. Tests that are about the
    # internal source itself say so explicitly.
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        # A named Discord source would drag in the StockKit routing rules, which
        # are a separate concern with their own tests. What these tests need is
        # only a source that is not `internal`, i.e. one that can actually
        # trade; the specific channel is irrelevant to fills and reconciliation.
        signal_source="discord",
        signal_source_type="discord",
        signal_config={},
        settings={"max_risk_per_trade": 0.01, "max_total_exposure": 0.05, **settings},
    )


def _verified_demo_account(
    database: Any, account_id: str, broker: str = "alpaca"
) -> None:
    """Register a demo account the way production does.

    ACTIVATION requires a fresh broker verification in addition to ``is_demo``,
    so tests that arm a deployment must perform the same two steps the discovery
    endpoint performs: register the account, then record its verification.
    """
    database.ensure_live_account(account_id, broker, is_demo=True)
    database.record_live_account_demo_verification(account_id, is_demo=True)


def _live_signal(deployment: LiveDeploymentConfig) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )


def test_submit_order_with_risk_blocks_when_broker_positions_fill_max_positions():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_positions=2)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    with pytest.raises(ValueError, match="zero-size"):
        asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is None


def test_submit_order_with_risk_blocks_when_broker_exposure_consumes_cap():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_total_exposure=0.02, max_positions=10)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    with pytest.raises(ValueError, match="zero-size"):
        asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is None


def test_submit_order_with_risk_sizes_normally_when_flat():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == pytest.approx(200.0)
    assert adapter.submitted_qty == pytest.approx(200.0)
    # Trusted quantity channel: the sized qty is handed to the adapter
    # explicitly, so the broker request cannot be driven by payload state.


def test_submit_order_with_risk_fails_closed_without_resolved_stop(monkeypatch):
    """A position sized against a computed stop must not open naked at the broker.

    The risk engine resolves a fallback stop (ATR/percent) when the signal carries
    none. The fail-closed guard fires when sizing produced no resolvable stop/TP —
    simulated here by a sizing regression — so a future contract change cannot
    silently reopen unbounded positions.
    """
    import live_runner.daemon as daemon_module

    def _sizing_without_stop(signal, deployment, **_kwargs):
        return signal.model_copy(
            update={"metadata": {**signal.metadata, "qty": 1.0, "explicit_qty": 1.0}}
        )

    monkeypatch.setattr(
        daemon_module, "size_live_signal_for_submission", _sizing_without_stop
    )
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    sized_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )

    submit = submit_order_with_risk(adapter, deployment, sized_signal)
    with pytest.raises(ValueError, match="stop loss"):
        asyncio.run(submit)

    assert adapter.submitted_signal is None


def test_submit_order_with_risk_does_not_let_incoming_qty_override_sizing():
    """A self-declared qty in the signal metadata must not bypass the risk
    sizing: the sized qty/explicit_qty win, unrelated keys are preserved."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])

    asyncio.run(
        submit_order_with_risk(
            adapter,
            deployment,
            _live_signal(deployment).model_copy(
                update={
                    "metadata": {
                        "qty": 999_999.0,
                        "note": "hello",
                        "stop_loss": 95.0,
                        "take_profit": 110.0,
                    }
                }
            ),
        )
    )

    submitted = adapter.submitted_signal
    assert submitted is not None
    assert submitted.metadata["qty"] == pytest.approx(200.0)
    assert submitted.metadata["explicit_qty"] == pytest.approx(200.0)
    assert submitted.metadata["note"] == "hello"
    assert submitted.metadata["stop_loss"] == pytest.approx(95.0)
    assert submitted.metadata["take_profit"] == pytest.approx(110.0)


def test_submit_order_with_risk_resolves_stop_for_stopless_signals():
    """A stopless signal is sized on the engine's fallback stop; the TP must
    be explicit (C1: no invented 1:1 bracket) and the sized qty is binding."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"take_profit": 110.0},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))

    assert adapter.submitted_signal is not None
    metadata = adapter.submitted_signal.metadata
    # Sizing: risk budget 1000 (1% of 100k) / distance 2 = 500; the 20%
    # notional cap (20000 / entry 100) binds and reduces the position to 200.
    assert metadata["qty"] == pytest.approx(200.0)
    assert metadata["stop_loss"] == pytest.approx(98.0)
    assert metadata["take_profit"] == pytest.approx(110.0)
    assert adapter.submitted_qty == pytest.approx(200.0)


def test_submit_order_with_risk_fails_closed_without_take_profit():
    """C1 (owner decision): a missing TP fails — no invented bracket."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )

    with pytest.raises(ValueError, match="take profit"):
        asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))

    assert adapter.submitted_signal is None


def test_submit_order_with_risk_forwards_sl_tp_to_broker_metadata():
    """The broker adapter must receive the computed SL/TP, not just the risk stats."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])

    asyncio.run(
        submit_order_with_risk(
            adapter,
            deployment,
            _live_signal(deployment),
            pip_value=1.0,
        )
    )

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["stop_loss"] == pytest.approx(95.0)
    assert adapter.submitted_signal.metadata["take_profit"] == pytest.approx(110.0)


def test_alpaca_submit_order_blocks_non_positive_quantity(monkeypatch):
    """C1/B2: qty is binding and finite — missing, non-positive and NaN
    quantities never reach the broker (no default-config sizing)."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: pytest.fail("order must not reach the broker"),
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "entry_price": 100.0,
            "stop_loss": 95.0,
        },
    )

    with pytest.raises(ValueError, match="explicit risk-sized qty"):
        asyncio.run(adapter.submit_order(signal))
    for invalid_qty in (0.0, -1.0, float("nan"), float("inf"), False, "not-a-number"):
        with pytest.raises(ValueError, match="finite and positive"):
            asyncio.run(adapter.submit_order(signal, qty=invalid_qty))


def test_alpaca_extended_hours_entries_fail_closed():
    """The broker rejects bracket orders with extended_hours enabled ("bracket
    order has non-zero extended_hours value", HTTP 422), and dropping the
    SL/TP to work around it would open an unbounded position. Extended-hours
    entries must therefore fail closed at the request builder."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)

    def _fail_request_cls(**_kwargs):
        pytest.fail("no broker request may be constructed for extended-hours entries")

    base_kwargs = dict(
        qty=1.0,
        order_side=SimpleNamespace(BUY="buy", SELL="sell"),
        time_in_force=SimpleNamespace(DAY="day"),
        market_request_cls=_fail_request_cls,
        client_order_id="cid-eh",
    )
    with_limit = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "qty": 1.0,
            "extended_hours": True,
            "limit_price": 101.5,
            "stop_loss": 95.0,
            "take_profit": 110.0,
        },
    )

    builder = adapter._build_order_request
    with pytest.raises(ValueError, match="extended_hours"):
        builder(with_limit, **base_kwargs)


def test_alpaca_regular_market_orders_carry_bracket_legs(monkeypatch):
    """Regular-session signals always build market orders with extended_hours
    disabled and the mandatory bracket legs attached."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    order_side = SimpleNamespace(BUY="buy", SELL="sell")
    time_in_force = SimpleNamespace(DAY="day")

    def _market_request_cls(**kwargs):
        return ("market", kwargs)

    def _build(signal, client_order_id):
        return adapter._build_order_request(
            signal,
            qty=2.0,
            order_side=order_side,
            time_in_force=time_in_force,
            market_request_cls=_market_request_cls,
            client_order_id=client_order_id,
        )

    regular = LiveSignal(
        deployment_id="dep-1",
        symbol="MSFT",
        action="sell",
        theoretical_price=300.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0, "stop_loss": 310.0, "take_profit": 280.0},
    )
    market_kind, market_kwargs = _build(regular, "cid-regular")
    assert market_kind == "market"
    assert market_kwargs["symbol"] == "MSFT"
    assert market_kwargs["qty"] == 2.0
    assert market_kwargs["side"] == "sell"
    assert market_kwargs["time_in_force"] == "day"
    assert market_kwargs["extended_hours"] is False
    assert market_kwargs["client_order_id"] == "cid-regular"
    assert market_kwargs["order_class"] is module.OrderClass.BRACKET


def test_alpaca_discord_redelivery_submits_once_with_canonical_signal_id(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    canonical_signal_id = str(uuid4())
    submitted_requests: list[Any] = []

    def _submit_order(*, order_data):
        if submitted_requests:
            pytest.fail(
                "Discord redelivery submitted a second Alpaca order with "
                f"client_order_id={order_data.client_order_id}"
            )
        submitted_requests.append(order_data)
        return SimpleNamespace(id=_alpaca_order_id("order-1"))

    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=_submit_order,
    )
    save_results = iter(
        [(canonical_signal_id, True), (canonical_signal_id, False)]
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: next(save_results))
    persisted_order_ids: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted_order_ids.append((sid, oid)) or True,
    )

    first = LiveSignal(
        id=UUID(canonical_signal_id),
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={
            "qty": 1.0,
            "discord_message_id": "discord-123",
            "stop_loss": 95.0,
            "take_profit": 110.0,
        },
    )
    redelivery = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 1, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "discord_message_id": "discord-123"},
    )

    assert asyncio.run(adapter.submit_order(first, qty=1.0)) == _alpaca_order_id("order-1")
    with pytest.raises(RuntimeError, match="canonical signal already exists"):
        asyncio.run(adapter.submit_order(redelivery, qty=1.0))

    assert len(submitted_requests) == 1
    assert submitted_requests[0].client_order_id == canonical_signal_id
    assert persisted_order_ids == [(canonical_signal_id, _alpaca_order_id("order-1"))]


def test_alpaca_redelivery_recovers_accepted_order_without_resubmitting(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """A timeout after broker acceptance must recover by canonical client order id."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    canonical_signal_id = str(uuid4())
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 1, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "discord_message_id": "discord-timeout"},
    )
    submit_calls: list[Any] = []
    lookup_calls: list[str] = []

    def _lookup(client_order_id: str):
        lookup_calls.append(client_order_id)
        return SimpleNamespace(id=_alpaca_order_id("accepted-before-timeout"))

    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        get_order_by_client_id=_lookup,
        submit_order=lambda *, order_data: submit_calls.append(order_data),
    )
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (canonical_signal_id, False)
    )
    persisted_order_ids: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted_order_ids.append((sid, oid)) or True,
    )

    assert asyncio.run(adapter.submit_order(signal, qty=1.0)) == _alpaca_order_id(
        "accepted-before-timeout"
    )
    assert lookup_calls == [canonical_signal_id]
    assert submit_calls == []
    assert persisted_order_ids == [
        (canonical_signal_id, _alpaca_order_id("accepted-before-timeout"))
    ]


def test_alpaca_redelivery_uses_persisted_order_id_without_broker_call(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    canonical_signal_id = str(uuid4())
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 2, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "discord_message_id": "discord-persisted"},
    )

    def _forbidden(*_args, **_kwargs):
        pytest.fail("Persisted Alpaca order id must avoid broker lookup and resubmit")

    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        get_order_by_client_id=_forbidden,
        submit_order=_forbidden,
    )
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (canonical_signal_id, False)
    )
    monkeypatch.setattr(
        module,
        "get_live_signal_broker_order_id",
        lambda signal_id: _alpaca_order_id("already-persisted")
        if signal_id == canonical_signal_id
        else None,
    )

    assert asyncio.run(adapter.submit_order(signal, qty=1.0)) == _alpaca_order_id(
        "already-persisted"
    )


def test_alpaca_rejects_invalid_persisted_order_id_on_redelivery(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        get_order_by_client_id=lambda _cid: pytest.fail("invalid persisted id must stop recovery"),
        submit_order=lambda **_kwargs: pytest.fail("duplicate order must not be submitted"),
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(uuid4()), False))
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "0")

    with pytest.raises(RuntimeError, match="persisted broker order id is invalid"):
        asyncio.run(
            adapter.submit_order(
                LiveSignal(
                    deployment_id="dep-1",
                    symbol="AAPL",
                    action="buy",
                    theoretical_price=100.0,
                    timestamp=datetime.now(timezone.utc),
                    metadata={"stop_loss": 95.0, "take_profit": 110.0},
                ),
                qty=1.0,
            )
        )


def test_alpaca_broker_order_id_persistence_failure_is_not_success(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    canonical_signal_id = str(uuid4())
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={
            "qty": 1.0,
            "discord_message_id": "discord-456",
            "stop_loss": 95.0,
            "take_profit": 110.0,
        },
    )
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda *, order_data: SimpleNamespace(id=_alpaca_order_id("order-2")),
    )
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (canonical_signal_id, True)
    )
    persistence_calls: list[tuple[str, str]] = []

    def _fail_persistence(signal_id: str, broker_order_id: str) -> bool:
        assert signal_id == canonical_signal_id, (
            "Alpaca broker order id was persisted against the noncanonical signal row"
        )
        persistence_calls.append((signal_id, broker_order_id))
        return False

    monkeypatch.setattr(module, "set_live_signal_broker_order_id", _fail_persistence)

    with pytest.raises(RuntimeError, match="Failed to persist Alpaca broker order id"):
        asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert persistence_calls == [(canonical_signal_id, _alpaca_order_id("order-2"))]






@pytest.mark.parametrize(
    "broker_order_id", [None, "", "   ", "None", 0, -1, "-2.5", "not-a-uuid"]
)
def test_alpaca_rejects_missing_or_nonpositive_order_id(monkeypatch, broker_order_id) -> None:
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: SimpleNamespace(id=broker_order_id),
    )
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(uuid4()), True))
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda *_args: pytest.fail("invalid broker order id must not be persisted"),
    )

    with pytest.raises(RuntimeError, match="valid positive broker order id"):
        asyncio.run(adapter.submit_order(signal, qty=1.0))


def test_ctrader_volume_rejects_missing_and_below_minimum_qty():
    """Volume floor must not silently override risk sizing; no default size."""
    _module, adapter = _make_ctrader_adapter()

    def _signal(metadata: dict[str, Any]) -> LiveSignal:
        return LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1,
            timestamp=datetime.now(timezone.utc),
            metadata=metadata,
        )

    empty = _signal({})
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(empty)
    with pytest.raises(ValueError, match="below the broker minimum"):
        adapter._resolve_order_volume(_signal({"qty": 0.07}))
    assert adapter._resolve_order_volume(_signal({"qty": 5.0})) == 5000


# ── B-6: exit-only close sums all matching positions ──────────────────────────

def test_exit_only_target_qty_rejects_multiple_matching_positions():
    from live_runner.daemon import _resolve_exit_only_target_qty

    positions = [
        {"symbol": "EURUSD", "qty": 2.0},
        {"symbol": "eurusd", "qty": 3.5},
    ]

    with pytest.raises(ValueError, match="multiple broker positions"):
        _resolve_exit_only_target_qty(iter(positions), "EURUSD")


def test_exit_only_target_qty_preserves_single_short_position():
    from live_runner.daemon import _resolve_exit_only_target_qty

    assert _resolve_exit_only_target_qty(
        iter([{"symbol": "EURUSD", "qty": -0.5}]), "EURUSD"
    ) == pytest.approx(0.5)


def test_exit_only_close_action_rejects_mixed_books():
    from live_runner.daemon import _exit_only_close_action

    assert _exit_only_close_action(iter([{"symbol": "EURUSD", "qty": -2.0}]), "EURUSD") == "buy"
    assert _exit_only_close_action(iter([{"symbol": "EURUSD", "qty": 2.0}]), "EURUSD") == "sell"
    with pytest.raises(ValueError, match="multiple broker positions"):
        _exit_only_close_action(
            iter([{"symbol": "EURUSD", "qty": 2.0}, {"symbol": "EURUSD", "qty": -1.0}]),
            "EURUSD",
        )


def test_exit_only_metadata_restamps_action_for_short_book():
    from live_runner.daemon import _metadata_for_exit_only_order

    class _Adapter:
        async def get_positions(self):
            return [{"symbol": "EURUSD", "qty": -2.0}]

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )

    metadata = asyncio.run(_metadata_for_exit_only_order(signal, _Adapter(), {}))

    assert metadata["explicit_qty"] == pytest.approx(2.0)
    assert metadata["risk_sizing_mode"] == "exit_only"
    assert metadata["action"] == "buy"
    assert metadata["signal_action"] == "sell"


def test_exit_only_metadata_rejects_multiple_positions():
    from live_runner.daemon import _metadata_for_exit_only_order

    class _Adapter:
        async def get_positions(self):
            return [
                {"symbol": "EURUSD", "qty": 1.0, "position_id": 10},
                {"symbol": "EURUSD", "qty": 2.0, "position_id": 11},
            ]

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )

    with pytest.raises(ValueError, match="multiple broker positions"):
        asyncio.run(_metadata_for_exit_only_order(signal, _Adapter(), {}))


def test_exit_only_target_qty_rejects_unparseable_rows():
    from live_runner.daemon import _resolve_exit_only_target_qty

    with pytest.raises(ValueError, match="Invalid qty"):
        _resolve_exit_only_target_qty(
            iter([{"symbol": "EURUSD", "qty": "not-a-number"}]), "EURUSD"
        )


def test_exit_only_target_qty_returns_zero_when_no_match():
    from live_runner.daemon import _resolve_exit_only_target_qty

    positions = [{"symbol": "AAPL", "qty": 10.0}]

    assert _resolve_exit_only_target_qty(iter(positions), "EURUSD") == 0.0


# ── B-10: exposure fed to the risk engine is risk-at-stake in balance units ───

def test_broker_exposure_estimate_is_risk_at_stake_in_balance_fraction_units():
    """Each open position contributes at most its per-trade risk budget, never
    a quantity-like number, so max_total_exposure (fraction of balance) and the
    exposure estimate share one unit."""
    from live_runner.daemon import _broker_exposure_estimate

    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.05)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 100.0},
            {"symbol": "MSFT", "qty": 300.0},
        ]
    )

    count, exposure = asyncio.run(_broker_exposure_estimate(adapter, deployment))

    assert count == 2
    assert exposure == pytest.approx(0.02)


def test_broker_exposure_estimate_returns_zero_without_position_support():
    from live_runner.daemon import _broker_exposure_estimate

    deployment = _deployment()
    adapter = _NoPositionsAdapter()

    count, exposure = asyncio.run(_broker_exposure_estimate(adapter, deployment))

    assert count is None
    assert exposure == 0.0


def test_submit_order_with_risk_clamps_exposure_to_total_cap():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.05, max_positions=100)
    adapter = _PositionsAdapter(
        positions=[{"symbol": f"S{i}", "qty": 1.0} for i in range(9)]
    )

    with pytest.raises(ValueError, match="zero-size"):
        asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is None


def test_submit_order_with_risk_exposure_binds_before_max_positions():
    """Exposure must bind in risk units, not position rows: with the budget
    consumed by one fewer position than max_positions allows, the next signal
    is sized from the remainder, not from the full per-trade cap."""
    from live_runner.daemon import submit_order_with_risk

    # 3 open positions x 1% == 3% consumed of the 5% budget; 2% remain, so the
    # next trade is sized from the remainder (2% < per-trade cap is false here,
    # but the point is the estimate stays in risk units, not quantities).
    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.05, max_positions=4)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 50_000.0},
            {"symbol": "MSFT", "qty": 50_000.0},
            {"symbol": "TSLA", "qty": 20.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    # Sizing must see 3 open positions and a risk-unit exposure of 3% — the
    # position rows' quantities must not leak into the exposure estimate.
    assert adapter.submitted_signal.metadata["current_positions"] == 3
    assert adapter.submitted_signal.metadata["qty"] == pytest.approx(200.0)


def test_submit_order_with_risk_rejects_zero_size_when_budget_exhausted():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.02, max_positions=10)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 1.0},
            {"symbol": "MSFT", "qty": 1.0},
        ]
    )

    with pytest.raises(ValueError, match="zero-size"):
        asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is None


# ── B-7: one cTrader volume contract ──────────────────────────────────────────

def test_ctrader_volume_contract_uses_qty_units_only():
    """The one contract: signal metadata 'qty' is in broker volume / 1000
    (account-currency lots for FX). Raw 'ctrader_volume' and 'volume' keys are
    rejected for entries and exit-only closes; the daemon-bound 'qty' is the
    only traded-size authority.

    Merge resolution: the other side of the merge stated the same rule in one
    line ("Only the daemon-sized quantity may determine broker volume."), which
    is folded into this docstring rather than left as a second string literal.
    """
    _module, adapter = _make_ctrader_adapter()

    def _signal(metadata: dict[str, Any]) -> LiveSignal:
        return LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1,
            timestamp=datetime.now(timezone.utc),
            metadata=metadata,
        )

    assert adapter._resolve_order_volume(_signal({"qty": 5.0})) == 5000
    # Entry + explicit volume: rejected, never honored.
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_order_volume(_signal({"ctrader_volume": 100000}))
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_order_volume(_signal({"volume": 100000}))
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_order_volume(
            _signal({"ctrader_volume": 100000, "risk_sizing_mode": "exit_only"})
        )
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_order_volume(
            _signal({"volume": 100000, "risk_sizing_mode": "exit_only"})
        )
    empty = _signal({})
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(empty)
    # Merge resolution: the other side of the merge looped the same three
    # metadata shapes under one `match="no positive quantity"` expectation. The
    # resolved adapter distinguishes them — a raw `ctrader_volume`/`volume`
    # override is an "ambiguous" refusal, only a missing `qty` is "no positive
    # quantity" — so that loop could not hold for the two raw-volume shapes.
    # The stronger per-shape assertions above replace it; the empty case it also
    # covered is asserted immediately above.


def test_ctrader_exit_only_rejects_ambiguous_raw_metadata_volume() -> None:
    _module, adapter = _make_ctrader_adapter()
    signal = _ctrader_signal(
        "EURUSD",
        {
            "qty": 3.0,
            "ctrader_volume": 999_999,
            "volume": 888_888,
            "risk_sizing_mode": "exit_only",
        },
    )

    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_order_volume(signal)


def test_ctrader_rejects_raw_volume_before_signal_persistence(monkeypatch):
    module, adapter = _make_ctrader_adapter()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "qty": 3.0,
            "volume": 999_999,
            "stop_loss": 1.05,
            "take_profit": 1.2,
        },
    )
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(
        module,
        "save_live_signal",
        lambda _signal: pytest.fail("raw volume must be rejected before persistence"),
    )

    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(adapter.submit_order(signal, qty=3.0))


def test_ctrader_volume_rejects_non_finite_qty():
    """Non-finite or non-positive qty must fail closed, never int()-truncate
    NaN/inf into a broker volume."""
    _module, adapter = _make_ctrader_adapter()

    def _signal(qty: Any) -> LiveSignal:
        return LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1,
            timestamp=datetime.now(timezone.utc),
            metadata={"qty": qty},
        )

    bad_signal = _signal(float("nan"))
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(bad_signal)
    bad_signal = _signal(float("inf"))
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(bad_signal)
    bad_signal = _signal(-2.0)
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(bad_signal)
    for invalid_qty in ("not-a-number", True):
        bad_signal = _signal(invalid_qty)
        with pytest.raises(ValueError, match="no positive quantity"):
            adapter._resolve_order_volume(bad_signal)


def test_ctrader_direct_persistence_strips_raw_broker_volume_keys(monkeypatch):
    module, adapter = _make_ctrader_adapter()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "volume": 999_999,
            "ctrader_volume": 888_888,
            "note": "keep",
        },
    )
    persisted: list[LiveSignal] = []

    class _PersistenceCaptured(Exception):
        pass

    def _capture(persisted_signal):
        persisted.append(persisted_signal)
        raise _PersistenceCaptured

    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: None)
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(module, "save_live_signal", _capture)
    monkeypatch.setattr(adapter, "_resolve_order_volume", lambda _signal: 5_000)

    with pytest.raises(_PersistenceCaptured):
        asyncio.run(adapter.submit_order(signal, qty=3.0))

    assert len(persisted) == 1
    metadata = persisted[0].metadata
    assert metadata["qty"] == pytest.approx(3.0)
    assert metadata["explicit_qty"] == pytest.approx(3.0)
    assert metadata["note"] == "keep"
    assert "volume" not in metadata
    assert "ctrader_volume" not in metadata


def test_ctrader_fill_quantities_are_persisted_in_qty_units(monkeypatch):
    """Live fill events and reconcile backfills must persist filled_qty in the
    same unit as signal metadata qty (broker volume / 1000), so the viewer's
    position reconstruction and the cumulative cap compare like with like."""
    module, adapter = _make_ctrader_adapter()
    adapter.account_config_id = "local-account-123"
    # #80: fills scale per symbol — the fake deal carries its symbolId.
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    monkeypatch.setattr(
        module,
        "find_live_signal_id_by_broker_order_id",
        lambda account_id, order_id: str(uuid4()),
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    event = SimpleNamespace(
        executionType=1,
        order=None,
        deal=SimpleNamespace(
            dealId=777,
            orderId=45678,
            symbolId=1,
            filledVolume=5_000_000,  # raw broker units
            executionPrice=1.2345,
            executionTimestamp=1700000000000,
            commission=0.0,
        ),
        HasField=lambda field: field == "deal",
    )

    asyncio.run(adapter._persist_execution_event(event))

    assert len(saved) == 1
    assert saved[0].filled_qty == pytest.approx(5000.0)  # 5_000_000 / 1000
    assert saved[0].dedupe_key == "ctrader:deal:777"


def test_ctrader_sparse_deal_uses_order_executed_volume(monkeypatch):
    module, adapter = _make_ctrader_adapter()
    adapter._order_id_to_symbol[45678] = "EURUSD"
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    saved: list[LiveExecution] = []
    monkeypatch.setattr(
        module,
        "save_live_execution",
        lambda execution: saved.append(execution) or True,
    )

    event = SimpleNamespace(
        executionType=1,
        order=SimpleNamespace(
            orderId=45678,
            clientOrderId=str(uuid4()),
            executedVolume=5_000_000,
            utcLastUpdateTimestamp=1700000000000,
        ),
        deal=SimpleNamespace(
            dealId=778,
            orderId=45678,
            filledVolume=0,
            volume=0,
            executionPrice=1.2345,
            executionTimestamp=1700000000000,
            commission=0.0,
        ),
        HasField=lambda field: field == "order",
    )

    asyncio.run(adapter._persist_execution_event(event))

    assert len(saved) == 1
    assert saved[0].filled_qty == pytest.approx(5000.0)
    assert saved[0].dedupe_key == "ctrader:deal:778"


def test_ctrader_reconciled_deals_are_persisted_in_qty_units(monkeypatch):
    """The reconcile backfill path uses the same unit contract."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_id = str(uuid4())
    monkeypatch.setattr(
        module,
        "list_live_signal_ids_for_reconciliation",
        lambda account_id, limit: [signal_id],
    )
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "45678")
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    # #80: fills scale per symbol — the fake deal carries its symbolId.
    adapter._symbol_name_to_id = {"EURUSD": 1}
    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,  # FILLED
        symbolId=1,
        filledVolume=2_500_000,
        executionPrice=1.2345,
        executionTimestamp=1700000000000,
        commission=0.0,
    )
    adapter._ProtoOAOrderDetailsRes = type("ProtoOAOrderDetailsRes", (), {"deal": [deal]})
    order_details_res = adapter._ProtoOAOrderDetailsRes()

    async def _mock_send_request(req, *, timeout_seconds=15):
        return order_details_res

    adapter._send_request = _mock_send_request
    adapter._ProtoOAOrderDetailsReq = SimpleNamespace
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=1, PARTIALLY_FILLED=2)

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 1
    assert saved[0].filled_qty == pytest.approx(2500.0)  # 2_500_000 / 1000


def test_ctrader_reconcile_sparse_deal_uses_order_executed_volume():
    _module, adapter = _make_ctrader_adapter()
    adapter._order_id_to_symbol[45678] = "EURUSD"
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=1, PARTIALLY_FILLED=2)
    deal = SimpleNamespace(
        dealId=779,
        dealStatus=1,
        filledVolume=0,
        volume=0,
        executionPrice=1.2345,
        executionTimestamp=1700000000000,
        commission=0.0,
    )
    order = SimpleNamespace(executedVolume=5_000_000)

    execution = adapter._build_reconciled_execution(
        deal,
        signal_uuid=UUID("00000000-0000-0000-0000-000000000001"),
        broker_order_id="45678",
        order=order,
    )

    assert execution is not None
    assert execution.filled_qty == pytest.approx(5000.0)


def test_ctrader_get_positions_qty_matches_volume_contract():
    """get_positions qty must use the same unit as signal metadata qty."""
    module, adapter = _make_ctrader_adapter()
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}

    buy_position = SimpleNamespace(
        positionStatus=1,
        tradeData=SimpleNamespace(symbolId=1, volume=2_500_000, tradeSide=1),
        price=1.1,
    )
    sell_position = SimpleNamespace(
        positionStatus=1,
        tradeData=SimpleNamespace(symbolId=1, volume=1_000_000, tradeSide=2),
        price=1.1,
    )
    other_status = SimpleNamespace(positionStatus=2)

    class _ReconcileRes:
        position = [buy_position, sell_position, other_status]

    async def _send(req, *, timeout_seconds=15):
        return _ReconcileRes()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOAReconcileReq = SimpleNamespace
    adapter._ProtoOAReconcileRes = _ReconcileRes
    adapter._ProtoOAPositionStatus = SimpleNamespace(POSITION_STATUS_OPEN=1)
    adapter._ProtoOATradeSide = SimpleNamespace(SELL=2)
    adapter._raise_if_terminal_auth_failed = lambda: None

    async def _noop_connect() -> None:
        return None

    adapter.connect = _noop_connect

    positions = asyncio.run(adapter.get_positions())

    assert positions == [
        {"symbol": "EURUSD", "qty": pytest.approx(2500.0), "entry_price": pytest.approx(1.1),
         "current_price": 0.0, "unrealized_pnl": 0.0},
        {"symbol": "EURUSD", "qty": pytest.approx(-1000.0), "entry_price": pytest.approx(1.1),
         "current_price": 0.0, "unrealized_pnl": 0.0},
    ]


# ── B-8: validate symbol/action BEFORE the first persistence ──────────────────

def test_both_broker_adapters_refuse_a_missing_qty(monkeypatch):
    """The one rule, asserted on both copies at once.

    `AlpacaLiveAdapter` and `CTraderLiveAdapter` each carry their own
    "qty is binding, no default-config sizing here" guard. The Alpaca one was pinned;
    cTrader's was not, which is why removing it left the suite green.

    Asserting the pair together is deliberate. Pinning only one broker's copy is what
    let this through in the first place, and the two messages differ, so a test on either
    in isolation documents one implementation rather than the rule.

    The two refusals are separate calls, not a shared helper, which is precisely why both
    need pinning: nothing relates them, so a fix to one leaves the other on the old
    behaviour. That is the same hazard `parse_bool_env`'s docstring records for four
    private copies of a boolean parser - two of which became live bugs.
    """
    from live_runner import alpaca_adapter as alpaca_module

    alpaca = alpaca_module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    alpaca._connected = True
    alpaca._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: pytest.fail("order must not reach the broker"),
    )
    monkeypatch.setattr(alpaca_module, "save_live_signal", lambda _signal: None)

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"entry_price": 100.0, "stop_loss": 95.0},
    )

    with pytest.raises(ValueError, match="explicit risk-sized qty"):
        asyncio.run(alpaca.submit_order(signal))

    module, ctrader = _make_ctrader_adapter()
    ctrader._symbol_name_to_id = {"EURUSD": 1}
    ctrader._connected = True
    ctrader._authenticated = True
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: None
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)

    # A separate signal, because this adapter's symbol map only knows EURUSD - and its
    # pre-persistence symbol check correctly fires first for anything else. Asserting the
    # qty guard therefore needs a symbol that broker would accept.
    ctrader_signal = signal.model_copy(update={"symbol": "EURUSD"})

    with pytest.raises(ValueError, match="explicit risk-sized qty"):
        asyncio.run(ctrader.submit_order(ctrader_signal))

    # And each broker refuses a non-positive qty too, on its own copy of that guard.
    for target, sig in ((alpaca, signal), (ctrader, ctrader_signal)):
        for invalid in (0.0, -1.0, float("nan"), False):
            with pytest.raises(ValueError, match="finite and positive"):
                asyncio.run(target.submit_order(sig, qty=invalid))


def test_ctrader_submit_order_requires_an_explicit_risk_sized_qty(monkeypatch):
    """cTrader's own qty guards, which the Alpaca test does not reach.

    `test_alpaca_submit_order_blocks_non_positive_quantity` covers exactly this contract -
    missing qty refused, non-positive and NaN refused - but for `AlpacaLiveAdapter`.
    `CTraderLiveAdapter.submit_order` carries the *same two guards* independently, and
    neither had a test: removing both leaves the whole suite green.

    **The seam lesson again, and now the third distinct instance in this sweep:** a rule
    pinned on one broker's adapter while the other broker's copy of it is untested. This
    is exactly what `parse_bool_env`'s docstring records - four private copies of one
    boolean parser, two of which produced live bugs - except here the copies still agree
    and nothing relates them, so a fix to one would leave the other behind.

    Two facets, both asserted:

    - **A missing qty is refused.** Sizing happens once upstream in `RiskConfig`; a
      default here would be a second, unconfigured sizing decision made at the last
      possible moment, on a real order.
    - **A non-positive or non-finite qty is refused**, including `False` - which is
      `0.0`, and therefore a sell of nothing that some brokers accept as valid.

    The mutation is killed by the *pair*: a `qty=None` call must raise before any broker
    interaction, so `submit_order` is stubbed to fail loudly if it gets that far.
    """
    module, adapter = _make_ctrader_adapter()
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._connected = True
    adapter._authenticated = True

    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: None
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)

    def _fail_if_reached(*_args, **_kwargs):
        pytest.fail("the order must be refused before any broker interaction")

    monkeypatch.setattr(adapter, "_submit_to_broker", _fail_if_reached, raising=False)

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0},
    )

    # Facet one: no qty at all. Sizing belongs upstream, not here.
    with pytest.raises(ValueError, match="explicit risk-sized qty"):
        asyncio.run(adapter.submit_order(signal))

    # Facet two: a qty that is not finite and positive. `False` is 0.0.
    for invalid_qty in (0.0, -1.0, float("nan"), float("inf"), False, "not-a-number"):
        with pytest.raises(ValueError, match="finite and positive"):
            asyncio.run(adapter.submit_order(signal, qty=invalid_qty))


def test_ctrader_invalid_symbol_never_persists_signal(monkeypatch):
    """An unknown symbol must be rejected before save_live_signal: otherwise the
    signal row exists with no broker order and every redelivery wedges on the
    duplicate-persisted recovery path."""
    module, adapter = _make_ctrader_adapter()
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._connected = True
    adapter._authenticated = True

    def _fail_save(signal):
        pytest.fail("save_live_signal must not run before symbol/action validation")

    monkeypatch.setattr(module, "save_live_signal", _fail_save)
    monkeypatch.setattr(
        module, "get_live_signal_timestamp", lambda _sid: None
    )
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: None
    )

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="NOSUCHSYMBOL",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 5.0},
    )

    submission = adapter.submit_order(signal)
    with pytest.raises(ValueError, match="Unknown cTrader symbol"):
        asyncio.run(submission)


def test_ctrader_invalid_action_never_persists_signal(monkeypatch):
    """Same wedge, action facet: an unsupported action must be refused before
    the first persistence, not after it."""
    module, adapter = _make_ctrader_adapter()
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._connected = True
    adapter._authenticated = True

    def _fail_save(signal):
        pytest.fail("save_live_signal must not run before symbol/action validation")

    monkeypatch.setattr(module, "save_live_signal", _fail_save)
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: None
    )

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 5.0},
    )
    # LiveSignal restricts action to buy/sell; the guard also protects the
    # broker request builder, so force an out-of-domain action through the model.
    object.__setattr__(signal, "action", "hold")

    submission = adapter.submit_order(signal)
    with pytest.raises(ValueError, match=r"Unsupported (signal )?action"):
        asyncio.run(submission)


def test_alpaca_invalid_action_never_persists_signal(monkeypatch):
    """Alpaca facet of the B-8 wedge: validate before the first persistence."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: pytest.fail("order must not reach the broker"),
    )
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: pytest.fail("persisted before validation")
    )

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0},
    )
    object.__setattr__(signal, "action", "hold")

    submission = adapter.submit_order(signal)
    with pytest.raises(ValueError, match=r"Unsupported (signal )?action"):
        asyncio.run(submission)


def test_processor_market_data_refuses_synthetic_fabrication():
    from live_runner.daemon import _build_processor_market_data

    signal = TradeSignal(
        action="buy", symbol="AAPL", entry_price=100.0, stop_loss=98.0,
        take_profit=None, timeframe="5m", metadata={},
    )

    with pytest.raises(ValueError, match="refusing to fabricate"):
        asyncio.run(
            _build_processor_market_data(
                _NoMarketDataAdapter(),
                signal,
                timeframe="5m",
                deployment=_deployment(),
            )
        )


class _NoMarketDataAdapter:
    async def get_market_data(self, *_args, **_kwargs):
        raise NotImplementedError


def test_the_real_ctrader_adapter_cannot_feed_a_signal_processor():
    """The refusal above uses a stub, so it proves the daemon's handling but not
    that the production adapter actually triggers it.

    ``CTraderLiveAdapter`` does not implement ``get_market_data`` at all - broker
    OHLCV is fetched from Alpaca for the Discord processors, and cTrader was
    never in scope. That is a deliberate limitation rather than an oversight, but
    it is a real capability boundary: a cTrader deployment cannot be given a
    signal processor, ever, because ATR cannot be computed without bars.

    Worth pinning for two reasons. It guards the fail-closed behaviour against a
    future ``get_market_data`` landing here without a staleness check - the Alpaca
    path has a calendar-aware one, cTrader would not - and it records the
    boundary as intentional so it is not mistaken for a bug. The refusal must
    name the reason, so an operator reading the log knows the deployment cannot
    trade rather than assuming the broker is down.
    """
    from live_runner.ctrader_adapter import CTraderLiveAdapter
    from live_runner.daemon import _build_processor_market_data

    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="access",
        refresh_token="refresh",
        account_id="46736434",
        demo=True,
    )
    signal = TradeSignal(
        action="buy",
        symbol="EURUSD",
        entry_price=1.0850,
        stop_loss=None,
        take_profit=None,
        timeframe="5m",
        metadata={},
    )

    with pytest.raises(ValueError, match="refusing to fabricate"):
        asyncio.run(
            _build_processor_market_data(
                adapter, signal, timeframe="5m", deployment=_deployment()
            )
        )


def test_signal_age_validation_rejects_stale_discord_signal():
    from live_runner.daemon import _validate_signal_age_before_wait

    deployment = _deployment()
    deployment.signal_config = {"max_signal_age_seconds": 60}
    stale = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        timeframe="5m",
        metadata={
            "received_at": (
                datetime.now(timezone.utc) - timedelta(seconds=120)
            ).isoformat()
        },
    )

    with pytest.raises(ValueError, match="stale"):
        _validate_signal_age_before_wait(
            stale, deployment, timeframe="5m", require_producer_timestamp=True
        )


def test_signal_age_budget_scales_with_signal_timeframe():
    from live_runner.daemon import _validate_signal_age_before_wait

    deployment = _deployment()
    deployment.signal_config = {}
    recent = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        timeframe="1h",
        metadata={
            "received_at": (
                datetime.now(timezone.utc) - timedelta(minutes=30)
            ).isoformat()
        },
    )

    _validate_signal_age_before_wait(
        recent, deployment, timeframe="1h", require_producer_timestamp=True
    )

    # REWRITTEN. This used to set `metadata["source"] = "discord"` and then drop
    # `received_at`, pinning the premise that the literal string "discord"
    # reaches this gate. It never did: `DiscordSignalSource` overwrites that
    # value with `f"discord_{bot_name.lower()}"` on every polled message, so
    # the exact-match comparison the test appeared to cover was dead code. The
    # refusal is now unconditional on the Discord path and the source value does
    # not enter the decision, so the assertion below is strictly stronger: it
    # refuses for the value production actually writes, and it refuses even with
    # no `source` key at all.
    recent.metadata.pop("received_at")
    for source in ("discord_nitro", "discord", None):
        if source is None:
            recent.metadata.pop("source", None)
        else:
            recent.metadata["source"] = source
        with pytest.raises(ValueError, match="missing received_at"):
            _validate_signal_age_before_wait(
                recent, deployment, timeframe="1h", require_producer_timestamp=True
            )


def test_signal_processor_uses_broker_market_data():
    from live_runner.daemon import _apply_signal_processor

    class _MarketDataAdapter:
        async def get_market_data(self, symbol, *, timeframe, limit):
            assert symbol == "AAPL"
            assert timeframe == "5m"
            assert limit == 200
            index = pd.date_range("2026-01-01", periods=80, freq="5min", tz="UTC")
            close = pd.Series(range(100, 180), index=index, dtype=float)
            return pd.DataFrame(
                {
                    "timestamp": index,
                    "open": close - 0.2,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 10000,
                }
            )

    deployment = LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="ramjet",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="5m",
        status="ACTIVE",
        signal_source="discord",
        signal_source_id="ramjet",
        signal_config={"signal_processor": "ramjet"},
        settings={},
    )
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=179.0,
        stop_loss=None,
        take_profit=None,
        timeframe="5m",
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
    )

    processed = asyncio.run(_apply_signal_processor(signal, deployment, _MarketDataAdapter()))

    assert processed.stop_loss is not None
    assert processed.take_profit is not None
    assert processed.metadata["signal_processor"] == "ramjet"
    assert processed.metadata["source"] == "ramjet"
    assert "market_data" not in processed.metadata


# ── Persistence durability / idempotency / recovery reads ─────────────────────


@pytest.fixture
def isolated_db(monkeypatch, tmp_path: Path, request):
    import shared.database as database

    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    # These tests persist signals that point at synthetic parents such as "dep-1". Tests marked
    # `owns_legacy_schema` hand-build the very tables this would create first, so they opt out.
    if not request.node.get_closest_marker("owns_legacy_schema"):
        seed_synthetic_deployment_parents(database)
    yield database
    engine.dispose()


def _persist_signal(database: Any) -> LiveSignal:
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)
    return signal


def test_save_live_signal_returns_canonical_discord_claim(isolated_db):
    database = isolated_db
    discord_message_id = "discord-123"
    first = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"discord_message_id": discord_message_id},
    )
    second = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 1, tzinfo=timezone.utc),
        metadata={"discord_message_id": discord_message_id},
    )

    assert database.save_live_signal(first) == (str(first.id), True)
    assert database.save_live_signal(second) == (str(first.id), False)

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveSignalModel)).all()
    assert [row.id for row in rows] == [str(first.id)]


def test_save_live_signal_scopes_discord_claim_by_deployment(isolated_db):
    database = isolated_db
    discord_message_id = "discord-fanout-123"
    first = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"discord_message_id": discord_message_id},
    )
    second = LiveSignal(
        deployment_id="dep-2",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"discord_message_id": discord_message_id},
    )

    assert database.save_live_signal(first) == (str(first.id), True)
    assert database.save_live_signal(second) == (str(second.id), True)

    with database.get_session() as session:
        rows = session.exec(
            database.select(database.LiveSignalModel).where(
                database.LiveSignalModel.discord_message_id == discord_message_id
            )
        ).all()
    assert {(row.deployment_id, row.id) for row in rows} == {
        ("dep-1", str(first.id)),
        ("dep-2", str(second.id)),
    }


def _legacy_parent_deployments(database: Any) -> None:
    """Give the legacy fixtures the parent deployments their signals point at.

    These tests build a hand-written legacy `live_signals` table. Once the
    `live_signals.deployment_id` foreign key is part of the migration set, rows in that table whose
    `deployment_id` names nothing are orphans, and the migration refuses to run - which blocks every
    other migration too, not just this one. Production genuinely had such orphans, and refusing is the
    intended fail-closed behaviour, but that is its own explicitly tested case rather than something
    every unrelated legacy test should be tripping over.

    So the legacy rows here model legacy data that was internally consistent, which is the case these
    tests are about: the discord index scope, the fan-out rule, the unique constraint, and the
    `live_executions` foreign key.
    """
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text(
                "CREATE TABLE IF NOT EXISTS live_deployments ("
                "id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, account_id TEXT,"
                " symbols TEXT NOT NULL, timeframe TEXT NOT NULL, status TEXT NOT NULL,"
                " signal_source TEXT NOT NULL, signal_source_type TEXT NOT NULL,"
                " symbol_filter TEXT NOT NULL, signal_config TEXT NOT NULL,"
                " settings TEXT NOT NULL)"
            )
        )
        for deployment_id in ("dep-1", "dep-2"):
            connection.execute(
                database.text(
                    "INSERT OR IGNORE INTO live_deployments (id, strategy_name, symbols, timeframe,"
                    " status, signal_source, signal_source_type, symbol_filter, signal_config,"
                    " settings) VALUES (:id, 'legacy', :symbols, '1h', 'PAUSED', 'discord',"
                    " 'discord', '[]', '{}', '{}')"
                ),
                {"id": deployment_id, "symbols": json.dumps(["EURUSD"])},
            )


def _legacy_live_signal_table(database: Any, *, table_unique: bool = False) -> None:
    unique_clause = ", UNIQUE(discord_message_id)" if table_unique else ""
    _legacy_parent_deployments(database)
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text(
                "CREATE TABLE live_signals ("
                "id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, symbol TEXT NOT NULL, "
                "action TEXT NOT NULL, theoretical_price FLOAT NOT NULL, timestamp TEXT NOT NULL, "
                "source TEXT NOT NULL DEFAULT 'internal', "
                "broker_order_id TEXT NOT NULL DEFAULT '', "
                f"discord_message_id TEXT, metadata TEXT NOT NULL{unique_clause})"
            )
        )
        if not table_unique:
            connection.execute(
                database.text(
                    "CREATE UNIQUE INDEX uq_live_signals_discord_msg_id "
                    "ON live_signals(discord_message_id) WHERE discord_message_id IS NOT NULL"
                )
            )


def _insert_legacy_live_signal(
    database: Any,
    *,
    row_id: str,
    deployment_id: str,
    discord_message_id: str | None,
) -> None:
    metadata = {"discord_message_id": discord_message_id} if discord_message_id else {}
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text(
                "INSERT INTO live_signals (id, deployment_id, symbol, action, theoretical_price, "
                "timestamp, source, broker_order_id, discord_message_id, metadata) "
                "VALUES (:id, :deployment_id, 'EURUSD', 'buy', 1.1, :timestamp, "
                "'discord', '', :discord_message_id, :metadata)"
            ),
            {
                "id": row_id,
                "deployment_id": deployment_id,
                "timestamp": "2026-07-15T10:00:00+00:00",
                "discord_message_id": discord_message_id,
                "metadata": json.dumps(metadata),
            },
        )


def _run_legacy_live_signal_migration(database: Any) -> None:
    database.SQLModel.metadata.create_all(database.ENGINE)
    database._migrate_schema()


def _discord_unique_indexes(database: Any) -> dict[str, list[str]]:
    with database.ENGINE.connect() as connection:
        indexes = connection.execute(database.text("PRAGMA index_list('live_signals')")).fetchall()
        result = {}
        for row in indexes:
            if not bool(row[2]):
                continue
            name = str(row[1])
            columns = connection.execute(
                database.text(f'PRAGMA index_info("{name}")')
            ).fetchall()
            result[name] = [str(column[2]) for column in columns]
        return {
            name: columns
            for name, columns in result.items()
            if "discord_message_id" in columns
        }


@pytest.mark.owns_legacy_schema
def test_migration_replaces_global_discord_index_with_deployment_scope(isolated_db):
    database = isolated_db
    _legacy_live_signal_table(database)
    _insert_legacy_live_signal(
        database,
        row_id="dep-1-signal",
        deployment_id="dep-1",
        discord_message_id="fanout-message",
    )

    _run_legacy_live_signal_migration(database)
    _run_legacy_live_signal_migration(database)

    indexes = _discord_unique_indexes(database)
    assert indexes == {
        "uq_live_signals_deployment_discord_msg": [
            "deployment_id",
            "discord_message_id",
        ]
    }
    with database.ENGINE.connect() as connection:
        sql = connection.execute(
            database.text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_live_signals_deployment_discord_msg'"
            )
        ).scalar_one()
    assert "WHERE discord_message_id IS NOT NULL" in sql


@pytest.mark.owns_legacy_schema
def test_migration_allows_legacy_fanout_across_deployments(isolated_db):
    database = isolated_db
    _legacy_live_signal_table(database)
    _insert_legacy_live_signal(
        database,
        row_id="dep-1-signal",
        deployment_id="dep-1",
        discord_message_id="fanout-message",
    )
    # Simulate the second deployment arriving through legacy metadata after the
    # global index was created; migration must backfill it after dropping that index.
    _insert_legacy_live_signal(
        database,
        row_id="dep-2-signal",
        deployment_id="dep-2",
        discord_message_id=None,
    )
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text("UPDATE live_signals SET metadata = :metadata WHERE id = 'dep-2-signal'"),
            {"metadata": json.dumps({"discord_message_id": "fanout-message"})},
        )

    _run_legacy_live_signal_migration(database)

    with database.ENGINE.connect() as connection:
        rows = connection.execute(
            database.text(
                "SELECT deployment_id, discord_message_id FROM live_signals ORDER BY deployment_id"
            )
        ).fetchall()
    assert rows == [("dep-1", "fanout-message"), ("dep-2", "fanout-message")]


@pytest.mark.owns_legacy_schema
def test_migration_fails_closed_for_duplicate_composite_claim(isolated_db):
    database = isolated_db
    _legacy_live_signal_table(database)
    _insert_legacy_live_signal(
        database,
        row_id="duplicate-a",
        deployment_id="dep-1",
        discord_message_id="duplicate-message",
    )
    _insert_legacy_live_signal(
        database,
        row_id="duplicate-b",
        deployment_id="dep-1",
        discord_message_id=None,
    )
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text("UPDATE live_signals SET metadata = :metadata WHERE id = 'duplicate-b'"),
            {"metadata": json.dumps({"discord_message_id": "duplicate-message"})},
        )

    with pytest.raises(RuntimeError, match="No rows were deleted"):
        _run_legacy_live_signal_migration(database)

    with database.ENGINE.connect() as connection:
        foreign_keys_enabled = int(
            connection.execute(database.text("PRAGMA foreign_keys")).scalar_one()
        )
        row_count = connection.execute(
            database.text("SELECT COUNT(*) FROM live_signals")
        ).scalar_one()
        rolled_back_claim = connection.execute(
            database.text(
                "SELECT discord_message_id FROM live_signals WHERE id = 'duplicate-b'"
            )
        ).scalar_one()
        legacy_table_count = connection.execute(
            database.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'live_signals__discord_legacy'"
            )
        ).scalar_one()
    assert foreign_keys_enabled == 1
    assert row_count == 2
    assert rolled_back_claim is None
    assert legacy_table_count == 0
    assert _discord_unique_indexes(database) == {
        "uq_live_signals_discord_msg_id": ["discord_message_id"]
    }


@pytest.mark.owns_legacy_schema
def test_migration_rebuilds_table_backed_global_unique_constraint(isolated_db):
    database = isolated_db
    _legacy_live_signal_table(database, table_unique=True)
    _insert_legacy_live_signal(
        database,
        row_id="dep-1-signal",
        deployment_id="dep-1",
        discord_message_id="table-unique",
    )

    _run_legacy_live_signal_migration(database)
    _insert_legacy_live_signal(
        database,
        row_id="dep-2-signal",
        deployment_id="dep-2",
        discord_message_id="table-unique",
    )

    assert _discord_unique_indexes(database) == {
        "uq_live_signals_deployment_discord_msg": [
            "deployment_id",
            "discord_message_id",
        ]
    }
    with database.ENGINE.connect() as connection:
        rows = connection.execute(
            database.text(
                "SELECT id, deployment_id, discord_message_id FROM live_signals "
                "ORDER BY deployment_id"
            )
        ).fetchall()
    assert rows == [
        ("dep-1-signal", "dep-1", "table-unique"),
        ("dep-2-signal", "dep-2", "table-unique"),
    ]


@pytest.mark.owns_legacy_schema
def test_migration_rebuild_preserves_live_execution_foreign_key(isolated_db):
    database = isolated_db
    _legacy_live_signal_table(database, table_unique=True)
    _insert_legacy_live_signal(
        database,
        row_id="signal-with-execution",
        deployment_id="dep-1",
        discord_message_id="fk-message",
    )
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text(
                "CREATE TABLE live_executions ("
                "id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, "
                "FOREIGN KEY(signal_id) REFERENCES live_signals(id))"
            )
        )
        connection.execute(
            database.text(
                "INSERT INTO live_executions (id, signal_id) "
                "VALUES ('execution-1', 'signal-with-execution')"
            )
        )

    with database.ENGINE.connect() as connection:
        foreign_keys_before = int(
            connection.execute(database.text("PRAGMA foreign_keys")).scalar_one()
        )

    _run_legacy_live_signal_migration(database)
    _run_legacy_live_signal_migration(database)

    with database.ENGINE.connect() as connection:
        foreign_keys_after = int(
            connection.execute(database.text("PRAGMA foreign_keys")).scalar_one()
        )
        foreign_keys = connection.execute(
            database.text("PRAGMA foreign_key_list('live_executions')")
        ).fetchall()
        violations = connection.execute(database.text("PRAGMA foreign_key_check")).fetchall()
        execution = connection.execute(
            database.text("SELECT id, signal_id FROM live_executions")
        ).one()
    assert foreign_keys_before == 1
    assert foreign_keys_after == foreign_keys_before
    assert {str(row[2]) for row in foreign_keys} == {"live_signals"}
    assert violations == []
    assert execution == ("execution-1", "signal-with-execution")


def _concurrent_discord_claims(database: Any, deployments: list[str]) -> list[tuple[str, bool]]:
    def persist(deployment_id: str) -> tuple[str, bool]:
        signal = LiveSignal(
            deployment_id=deployment_id,
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1,
            timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            metadata={"discord_message_id": "concurrent-message"},
        )
        return database.save_live_signal(signal)

    database.init_db()
    with ThreadPoolExecutor(max_workers=len(deployments)) as executor:
        return list(executor.map(persist, deployments))


def test_concurrent_discord_claim_is_atomic_within_deployment(isolated_db):
    database = isolated_db
    results = _concurrent_discord_claims(database, ["dep-1", "dep-1"])

    assert sorted(created for _signal_id, created in results) == [False, True]
    assert len({signal_id for signal_id, _created in results}) == 1


def test_concurrent_discord_claims_are_independent_across_deployments(isolated_db):
    database = isolated_db
    results = _concurrent_discord_claims(database, ["dep-1", "dep-2"])

    assert [created for _signal_id, created in results] == [True, True]
    assert len({signal_id for signal_id, _created in results}) == 2


def test_non_demo_account_cannot_activate_deployment(isolated_db):
    database = isolated_db
    database.ensure_live_account("acc-live", "alpaca", is_demo=False)
    deployment = _deployment()
    deployment.account_id = "acc-live"
    deployment.status = "PAUSED"

    active_deployment = deployment.model_copy(update={"status": "ACTIVE"})
    with pytest.raises(ValueError, match="non-demo"):
        database.create_live_deployment(active_deployment)

    created = database.create_live_deployment(deployment)
    assert created.status == "PAUSED"
    with pytest.raises(ValueError, match="non-demo"):
        database.update_live_deployment_status(created.id, "ACTIVE")

    # is_demo alone is no longer sufficient: activation additionally requires a
    # fresh broker verification, so the flag cannot be trusted indefinitely.
    database.ensure_live_account("acc-live", "alpaca", is_demo=True)
    with pytest.raises(ValueError, match="no broker demo verification"):
        database.update_live_deployment_status(created.id, "ACTIVE")

    database.record_live_account_demo_verification("acc-live", is_demo=True)
    assert database.update_live_deployment_status(created.id, "ACTIVE") is not None


def test_activation_requires_verification_freshness(isolated_db):
    """A demo flag with no verification, or a verification that has aged out,
    must both block ACTIVE. This is what stops a re-pointed credential from
    inheriting an old, trusted confirmation."""
    database = isolated_db
    database.ensure_live_account("acc-demo", "alpaca", is_demo=True)
    deployment = _deployment()
    deployment.account_id = "acc-demo"
    deployment.status = "PAUSED"
    created = database.create_live_deployment(deployment)

    # 1. is_demo set, but never verified against the broker.
    with pytest.raises(ValueError, match="no broker demo verification"):
        database.update_live_deployment_status(created.id, "ACTIVE")

    # 2. Verified, then aged past the TTL.
    database.record_live_account_demo_verification("acc-demo", is_demo=True)
    assert database.update_live_deployment_status(created.id, "ACTIVE") is not None
    database.update_live_deployment_status(created.id, "PAUSED")

    with database.get_session() as session:
        account = session.get(database.LiveAccountModel, "acc-demo")
        account.demo_verified_at = account.demo_verified_at - timedelta(
            seconds=database.DEMO_VERIFICATION_TTL_SECONDS + 60
        )
        session.add(account)
        session.commit()

    with pytest.raises(ValueError, match="older than"):
        database.update_live_deployment_status(created.id, "ACTIVE")

    # 3. A fresh re-verification restores the ability to arm.
    database.record_live_account_demo_verification("acc-demo", is_demo=True)
    assert database.update_live_deployment_status(created.id, "ACTIVE") is not None


def test_identity_fingerprint_changes_when_broker_identity_changes(isolated_db):
    database = isolated_db
    _verified_demo_account(database, "acc-1", "alpaca")
    first = database.record_live_account_demo_verification(
        "acc-1", is_demo=True, ctid="ctid-a", broker_login="login-a"
    )
    second = database.record_live_account_demo_verification(
        "acc-1", is_demo=True, ctid="ctid-b", broker_login="login-a"
    )
    assert first.is_demo and second.is_demo

    def _fingerprint(account_id: str) -> str | None:
        with database.get_session() as session:
            return session.get(database.LiveAccountModel, account_id).identity_fingerprint

    assert _fingerprint("acc-1") not in (None, "")

    # The actual property: a different broker-reported identity must produce a
    # different fingerprint. This used to be asserted only as `is not None`, and
    # the two verifications above were captured but never compared, so the test
    # passed whether or not the fingerprint depended on the identity at all. It
    # is the whole basis of the silent re-point check in
    # _live_account_fingerprint_mismatch_reason, so it has to be compared.
    assert second.identity_fingerprint is not None
    assert first.identity_fingerprint != second.identity_fingerprint, (
        "ctid changed from ctid-a to ctid-b, so the fingerprint must change"
    )

    # Re-verifying the SAME identity must not change it, otherwise every
    # re-verification would look like a re-point and block activation forever.
    same_again = database.record_live_account_demo_verification(
        "acc-1", is_demo=True, ctid="ctid-b", broker_login="login-a"
    )
    assert same_again.identity_fingerprint == second.identity_fingerprint

    # broker_login is part of the identity too, so changing only that must also
    # change the fingerprint.
    re_logged_in = database.record_live_account_demo_verification(
        "acc-1", is_demo=True, ctid="ctid-b", broker_login="login-b"
    )
    assert re_logged_in.identity_fingerprint != second.identity_fingerprint


def test_repoint_deployment_enforces_safety_guards(isolated_db):
    database = isolated_db
    _verified_demo_account(database, "acc-old", "alpaca")
    database.record_live_account_demo_verification("acc-old", is_demo=True)
    _verified_demo_account(database, "acc-new", "alpaca")
    database.record_live_account_demo_verification("acc-new", is_demo=True)
    database.ensure_live_account("acc-live", "alpaca", is_demo=False)

    deployment = _deployment()
    deployment.account_id = "acc-old"
    deployment.status = "PAUSED"
    created = database.create_live_deployment(deployment)

    # Unknown target is refused.
    with pytest.raises(ValueError, match="unknown account"):
        database.update_live_deployment_account(created.id, "acc-missing")

    # Non-demo target is refused.
    with pytest.raises(ValueError, match="not verified as demo"):
        database.update_live_deployment_account(created.id, "acc-live")

    # ACTIVE deployment is refused without an explicit override.
    database.update_live_deployment_status(created.id, "ACTIVE")
    with pytest.raises(ValueError, match="ACTIVE deployment"):
        database.update_live_deployment_account(created.id, "acc-new")

    # Even with the override, in-flight submissions block the re-point, so a
    # submission that is already at the broker cannot land on the old account
    # after we moved the deployment elsewhere.
    assert database.claim_live_deployment_submission(created.id, "tok-inflight") is True
    with pytest.raises(ValueError, match="in-flight submissions"):
        database.update_live_deployment_account(created.id, "acc-new", allow_active=True)

    # Once the submission has drained, the re-point succeeds and persists.
    assert database.release_live_deployment_submission("tok-inflight") is True

    # An EXPIRED lease is not in flight either: its TTL elapsed, so there is
    # nothing left to drain, and refusing with "drain them first" strands the
    # operator on an error they cannot act on. The count used to include every
    # claim regardless of expiry, so a stale row blocked the re-point until some
    # later claim happened to purge it.
    assert (
        database.claim_live_deployment_submission(
            created.id, "tok-expired", ttl_seconds=0.01
        )
        is True
    )
    time.sleep(0.05)
    moved = database.update_live_deployment_account(
        created.id, "acc-new", allow_active=True
    )
    assert moved is not None
    assert moved.account_id == "acc-new"


def test_repoint_rejects_blank_identifiers(isolated_db):
    database = isolated_db
    with pytest.raises(ValueError, match="required"):
        database.update_live_deployment_account("", "acc-1")
    with pytest.raises(ValueError, match="required"):
        database.update_live_deployment_account("dep-1", "")


def test_ensure_live_account_without_demo_flag_preserves_verified_value(isolated_db):
    database = isolated_db
    _verified_demo_account(database, "acc-demo", "alpaca")

    refreshed = database.ensure_live_account("acc-demo", "alpaca")

    assert refreshed.is_demo is True


def test_submission_claim_is_atomic_with_deployment_status(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)

    deployment_id = str(deployment.id)
    assert database.claim_live_deployment_submission(deployment_id, "token-1") is True
    assert len(database.list_live_deployment_submission_claims(deployment_id)) == 1

    database.update_live_deployment_status(deployment_id, "PAUSED")

    assert database.claim_live_deployment_submission(deployment_id, "token-2") is False
    assert len(database.list_live_deployment_submission_claims(deployment_id)) == 1
    assert database.release_live_deployment_submission("token-1") is True
    assert database.list_live_deployment_submission_claims(deployment_id) == []


def test_expired_submission_claim_is_reaped_but_never_hidden(isolated_db):
    """A dead claimor must not wedge a deployment, and must not be papered over.

    This test used to assert that counting submissions returned 0 once a lease
    expired, because the count purged expired rows before counting. That made an
    expired-but-never-released claim indistinguishable from "nothing was in
    flight", so a PAUSE could be reported as a clean stop for an order whose
    outcome nobody had observed. The anti-wedge requirement is still real and is
    asserted here, but it belongs to the claim path: a fresh claim reaps the
    dead lease. The observer only reports, so the ambiguity stays visible.
    """
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)

    deployment_id = str(deployment.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "expired-token", ttl_seconds=0.01
    ) is True
    time.sleep(0.05)

    # The observer reports the dead lease as expired rather than deleting it.
    observed = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in observed] == ["expired-token"]
    assert observed[0].expired is True

    # Reading it is side-effect free: the row is still there afterwards, because
    # an observer that deletes evidence cannot be used to decide whether a stop
    # reached the broker.
    from sqlmodel import select

    with database.get_session() as session:
        rows = session.exec(select(database.LiveDeploymentSubmissionClaimModel)).all()
    assert [row.token for row in rows] == ["expired-token"]

    # A fresh claim still reaps it, so a crashed claimor cannot wedge the
    # deployment forever.
    assert database.claim_live_deployment_submission(
        deployment_id, "after-ttl", ttl_seconds=30.0
    ) is True
    reaped = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in reaped] == ["after-ttl"]
    assert reaped[0].expired is False


def test_a_lease_is_reaped_at_the_exact_ttl_instant_not_after_it(isolated_db, monkeypatch):
    """The reaping predicate is `<=`, not `<`: a lease dies *at* its TTL, not after.

    The existing reaping test sleeps 50 ms past a 10 ms TTL, so it cannot tell
    `expires_at <= now` from `expires_at < now`. That distinction is the whole
    anti-wedge guarantee at its boundary: with `<`, a lease whose TTL expires in
    the same instant the next claim arrives is not reaped, the INSERT hits
    UNIQUE(deployment_id), and the claim is refused - wedging the deployment for a
    claimor that died at the exact moment its lease lapsed, which is precisely the
    case the TTL exists to recover from.

    The clock is frozen so the lease is due at *exactly* the instant the claim
    reads. Rewinding the stored expiry instead only makes it strictly earlier,
    which both predicates accept, so the mutant survives; equality is the only
    input that separates `<=` from `<`.
    """
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)

    deployment_id = str(deployment.id)
    frozen_now = datetime(2026, 9, 30, 12, 0, 0)
    monkeypatch.setattr(database, "_utcnow_naive", lambda: frozen_now)
    assert database.claim_live_deployment_submission(
        deployment_id, "due-now", ttl_seconds=30.0
    ) is True

    # Move the expiry onto the very instant the next claim will read as "now",
    # which is what a lease expiring between two dispatch ticks looks like.
    with database.get_session() as session:
        row = session.get(database.LiveDeploymentSubmissionClaimModel, "due-now")
        assert row is not None
        row.expires_at = frozen_now
        session.commit()

    assert database.claim_live_deployment_submission(
        deployment_id, "after-exact-ttl", ttl_seconds=30.0
    ) is True, "a lease due at exactly `now` was not reaped, so the claim was refused"
    reaped = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in reaped] == ["after-exact-ttl"]


def test_a_blank_claim_token_is_refused(isolated_db):
    """An empty or whitespace-only token must not take the lease.

    `claim_live_deployment_submission` normalises both arguments and refuses
    blank ones. Dropping that guard lets a blank token insert a real lease row,
    and that row is then unreleasable: `release_live_deployment_submission`
    applies the same normalisation and refuses a blank token too, so the caller
    can never give the lease back. The deployment is wedged for the whole TTL.

    The guard is what keeps a caller bug (an unset token) a visible refusal
    instead of a silent 30-second outage.
    """
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)

    deployment_id = str(deployment.id)

    assert database.claim_live_deployment_submission(deployment_id, "") is False
    assert database.claim_live_deployment_submission(deployment_id, "   ") is False
    assert database.claim_live_deployment_submission("", "tok") is False
    assert database.claim_live_deployment_submission("   ", "tok") is False

    # No lease was taken, and the deployment is still claimable by a real token.
    assert database.list_live_deployment_submission_claims(deployment_id) == []
    assert database.claim_live_deployment_submission(deployment_id, "real-token") is True


def test_an_unrecognised_stockkit_route_resolves_to_no_account():
    """A route nobody recognises must resolve to nothing, not to a fallback number.

    `expected_account_number_for_route` returns `""` for an unknown route. Removing that
    early return leaves every routing test green, because they all use known aliases -
    so the question "which Alpaca account does this route trade on?" was only ever asked
    for routes that had an answer.

    The empty string is the only safe answer here. A fallback table lookup would trade
    an unrecognised bot's signals on somebody's account: the failure is silent, the order
    is real, and the account number in the log looks entirely legitimate.

    Removing the guard is an **equivalent mutant** through the static table, since
    `STOCKKIT_ACCOUNT_ROUTES.get("", "")` is also `""`. The one observable difference is
    the env lookup, whose key becomes the *unrecognised route itself* rather than
    nothing - so `STOCKKIT_ACCOUNT_NUMBER_` with an empty suffix resolves an unknown bot
    onto a real account number. That case is asserted below, with a note that it is the
    only path that distinguishes the mutant.
    """
    from live_runner.stockkit_routing import (
        expected_account_number_for_route,
        normalize_stockkit_route,
    )

    for route in (
        "unknown-bot",
        "NITRO_PLUS",
        "ramjets",
        "",
        None,
        "   ",
        "12345",
        "!!!",
    ):
        assert normalize_stockkit_route(route) == "", (
            f"{route!r} was resolved to a route it does not name"
        )
        assert expected_account_number_for_route(route) == "", (
            f"{route!r} resolved to an Alpaca account number; an unrecognised route "
            "must trade on nothing"
        )

    # With env present, an unknown route still refuses rather than matching a block.
    assert (
        expected_account_number_for_route(
            "unknown-bot",
            {
                "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
                "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
                "STOCKKIT_ACCOUNT_NUMBER_NITRO": "PA3T0MQAGQ51",
            },
        )
        == ""
    ), "an unknown route matched a configured account block"

    # The only input that distinguishes the mutant: with the early return gone the env
    # key is looked up with an empty route suffix, so a stray entry resolves the unknown
    # route onto a real account number. Verified by direct probe, not inference.
    assert (
        expected_account_number_for_route(
            "unknown-bot", {"STOCKKIT_ACCOUNT_NUMBER_": "PA_REAL_ACCOUNT"}
        )
        == ""
    ), (
        "an unrecognised route resolved through an env key with an empty route suffix"
    )


def test_a_known_route_still_resolves_with_the_same_env_present():
    """The guard above is not satisfied by refusing everything.

    A test that only asserts `""` for everything passes against a function that never
    resolves anything, so the known aliases are checked against the same env used
    above - which is the shape that actually selects the wrong account when the
    resolution order is wrong.
    """
    from live_runner.stockkit_routing import expected_account_number_for_route

    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
        "STOCKKIT_ACCOUNT_NUMBER_NITRO": "PA3T0MQAGQ51",
    }
    # Step 1: an explicit entry beats the indexed block for the same route.
    assert expected_account_number_for_route("nitro", env) == "PA3T0MQAGQ51"

    # Step 2: with no explicit entry, the account whose label matches resolves it.
    # This is the step that survives a paper account being re-created, which
    # silently invalidates any hardcoded number.
    assert (
        expected_account_number_for_route(
            "nitro",
            {
                "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
                "ALPACA_ACCOUNT_NUMBER_ACC1": "PA3E767TJ91D",
            },
        )
        == "PA3E767TJ91D"
    )

    # A route with no label and no explicit entry falls through to the static table.
    assert expected_account_number_for_route("ramjet", env) == "PA3T0MQAGQ51"

    # Step 3: nothing in env at all falls back to the same table.
    assert expected_account_number_for_route("rocketfuel", {}) == "PA3EFBL4P7BU"


def test_ctrader_reconciliation_candidates_include_partially_executed_signal(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"broker_order_id": "45678"},
    )
    database.save_live_signal(signal)
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=1.1,
            filled_qty=0.5,
            commission=0.0,
            timestamp=datetime(2026, 7, 15, 10, 1, tzinfo=timezone.utc),
            broker_order_id="45678",
            dedupe_key="ctrader:deal:1",
        )
    )

    assert str(signal.id) in database.list_live_signal_ids_for_reconciliation("acc-1")


def test_reconciliation_candidates_are_scoped_to_account(isolated_db):
    database = isolated_db
    deployment_a = _deployment()
    deployment_b = LiveDeploymentConfig(
        **{**deployment_a.model_dump(), "id": str(uuid4()), "account_id": "acc-2"}
    )
    _verified_demo_account(database, "acc-1", "ctrader")
    _verified_demo_account(database, "acc-2", "ctrader")
    database.create_live_deployment(deployment_a)
    database.create_live_deployment(deployment_b)

    signal_a = LiveSignal(
        deployment_id=str(deployment_a.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
    )
    signal_b = LiveSignal(
        deployment_id=str(deployment_b.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
    )
    database.save_live_signal(signal_a)
    database.save_live_signal(signal_b)

    assert database.list_live_signal_ids_for_reconciliation("acc-1") == [str(signal_a.id)]
    assert database.list_live_signal_ids_for_reconciliation("acc-2") == [str(signal_b.id)]
    assert database.list_live_signal_ids_for_reconciliation("") == []


def test_reconciliation_candidates_support_keyset_pagination(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "alpaca")
    database.create_live_deployment(deployment)
    timestamps = [
        datetime(2026, 7, 15, 10, minute, tzinfo=timezone.utc)
        for minute in range(5)
    ]
    signals = []
    for timestamp in timestamps:
        signal = LiveSignal(
            deployment_id=str(deployment.id),
            symbol="AAPL",
            action="buy",
            theoretical_price=200.0,
            timestamp=timestamp,
        )
        database.save_live_signal(signal)
        signals.append(signal)

    first_page = database.list_live_signal_ids_for_reconciliation("acc-1", limit=2)
    first_cursor = database.get_live_signal_reconciliation_cursor(first_page[-1])
    second_page = database.list_live_signal_ids_for_reconciliation(
        "acc-1", limit=2, before=first_cursor
    )
    second_cursor = database.get_live_signal_reconciliation_cursor(second_page[-1])
    third_page = database.list_live_signal_ids_for_reconciliation(
        "acc-1", limit=2, before=second_cursor
    )

    assert first_page == [str(signals[4].id), str(signals[3].id)]
    assert second_page == [str(signals[2].id), str(signals[1].id)]
    assert third_page == [str(signals[0].id)]
    assert len(set(first_page + second_page + third_page)) == 5


def test_reconciliation_keyset_is_stable_when_newer_signal_arrives(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "alpaca")
    database.create_live_deployment(deployment)
    shared_timestamp = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    original_ids = []
    for _ in range(4):
        signal = LiveSignal(
            deployment_id=str(deployment.id),
            symbol="AAPL",
            action="buy",
            theoretical_price=200.0,
            timestamp=shared_timestamp,
        )
        database.save_live_signal(signal)
        original_ids.append(str(signal.id))

    first_page = database.list_live_signal_ids_for_reconciliation("acc-1", limit=2)
    first_cursor = database.get_live_signal_reconciliation_cursor(first_page[-1])
    newer_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=201.0,
        timestamp=shared_timestamp + timedelta(minutes=1),
    )
    database.save_live_signal(newer_signal)
    second_page = database.list_live_signal_ids_for_reconciliation(
        "acc-1", limit=2, before=first_cursor
    )

    assert first_page + second_page == sorted(original_ids, reverse=True)
    assert str(newer_signal.id) not in second_page
    assert len(set(first_page + second_page)) == 4


def test_save_live_execution_is_durable(isolated_db):
    """Kills mutant: removing the commit (fills silently rolled back)."""
    database = isolated_db
    signal = _persist_signal(database)
    execution = LiveExecution(
        signal_id=signal.id,
        actual_price=100.5,
        filled_qty=10.0,
        commission=0.1,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-1",
        dedupe_key="alpaca:exec:e-1",
    )

    assert database.save_live_execution(execution) is True

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 1
    assert rows[0].filled_qty == 10.0
    assert rows[0].dedupe_key == "alpaca:exec:e-1"


def test_save_live_execution_skips_duplicate_dedupe_key(isolated_db):
    database = isolated_db
    signal = _persist_signal(database)

    def _execution() -> LiveExecution:
        return LiveExecution(
            signal_id=signal.id,
            actual_price=100.5,
            filled_qty=10.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id="bo-1",
            dedupe_key="alpaca:exec:dup",
        )

    assert database.save_live_execution(_execution()) is True
    assert database.save_live_execution(_execution()) is False

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 1


def test_live_signal_timestamp_lookup_normalizes_to_utc(isolated_db):
    database = isolated_db

    # `dep-naive` and `dep-aware` are this test's own signals, so they need parents too.
    from tests.conftest import seed_synthetic_deployment_parents

    seed_synthetic_deployment_parents(database, ("dep-naive", "dep-aware"))

    assert database.get_live_signal_timestamp("") is None
    assert database.get_live_signal_timestamp(str(uuid4())) is None

    naive_timestamp = datetime(2026, 7, 15, 10, 30)
    naive_signal = LiveSignal(
        deployment_id="dep-naive",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=naive_timestamp,
        metadata={},
    )
    database.save_live_signal(naive_signal)
    assert database.get_live_signal_timestamp(str(naive_signal.id)) == naive_timestamp.replace(
        tzinfo=timezone.utc
    )

    plus_two = timezone(timedelta(hours=2))
    aware_signal = LiveSignal(
        deployment_id="dep-aware",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 12, 30, tzinfo=plus_two),
        metadata={},
    )
    database.save_live_signal(aware_signal)
    assert database.get_live_signal_timestamp(str(aware_signal.id)) == datetime(
        2026, 7, 15, 10, 30, tzinfo=timezone.utc
    )


def test_broker_order_id_roundtrip_and_missing_execution_lookup(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="buy",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc), metadata={},
    )
    database.save_live_signal(signal)

    assert database.set_live_signal_broker_order_id(str(signal.id), "bo-42") is True
    assert database.set_live_signal_broker_order_id(str(signal.id), "bo-42") is True
    assert database.set_live_signal_broker_order_id(str(signal.id), "bo-43") is False
    assert database.get_live_signal_broker_order_id(str(signal.id)) == "bo-42"
    assert database.find_live_signal_id_by_broker_order_id("acc-1", "bo-42") == str(signal.id)
    assert database.find_live_signal_id_by_broker_order_id("acc-1", "missing") is None

    # Signal has no execution yet -> reported for reconciliation.
    assert database.list_live_signal_ids_missing_executions() == [str(signal.id)]

    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=1.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id="bo-42",
            dedupe_key=None,
        )
    )
    assert database.list_live_signal_ids_missing_executions() == []


def test_live_execution_filled_qty_sum_tracks_partial_events(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "alpaca")
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="buy",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc), metadata={},
    )
    database.save_live_signal(signal)
    for index, qty in enumerate((1.0, 1.5), start=1):
        database.save_live_execution(
            LiveExecution(
                signal_id=signal.id,
                actual_price=100.0 + index,
                filled_qty=qty,
                commission=0.0,
                timestamp=datetime.now(timezone.utc),
                broker_order_id="bo-partial",
                dedupe_key=f"alpaca:exec:partial-{index}",
            )
        )

    assert database.get_live_signal_filled_qty(str(signal.id)) == pytest.approx(2.5)
    assert database.get_live_signal_filled_qty(str(uuid4())) == 0.0


def test_live_execution_cumulative_cap_rejects_delayed_duplicate_fill(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "alpaca")
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="buy",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc), metadata={},
    )
    database.save_live_signal(signal)
    reconciled = LiveExecution(
        signal_id=signal.id,
        actual_price=101.0,
        filled_qty=3.0,
        commission=0.0,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-capped",
        dedupe_key="alpaca:order:bo-capped:reconciled:3.0",
    )
    delayed_stream = LiveExecution(
        signal_id=signal.id,
        actual_price=100.0,
        filled_qty=1.0,
        commission=0.0,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-capped",
        dedupe_key="alpaca:exec:delayed-first-fill",
    )

    assert database.save_live_execution_up_to_cumulative(reconciled, 3.0) == pytest.approx(3.0)
    assert database.save_live_execution_up_to_cumulative(delayed_stream, 1.0) == 0.0
    assert database.save_live_execution_up_to_cumulative(delayed_stream, 3.0) == 0.0
    assert database.get_live_signal_filled_qty(str(signal.id)) == pytest.approx(3.0)


def test_live_execution_cumulative_propagates_unrelated_integrity_error(isolated_db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    database = isolated_db
    signal = _persist_signal(database)
    execution = LiveExecution(
        signal_id=signal.id,
        actual_price=100.0,
        filled_qty=1.0,
        commission=0.0,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-integrity",
        dedupe_key="alpaca:exec:integrity",
    )

    def _raise(*_args, **_kwargs):
        raise IntegrityError("INSERT", {}, Exception("CHECK failed"))

    monkeypatch.setattr(database, "_insert_live_execution_locked", _raise)
    with pytest.raises(IntegrityError):
        database.save_live_execution_up_to_cumulative(execution, 1.0)


def test_live_execution_cumulative_cap_serializes_concurrent_writers(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "alpaca")
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="buy",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc), metadata={},
    )
    database.save_live_signal(signal)

    def _persist(index: int) -> float:
        return database.save_live_execution_up_to_cumulative(
            LiveExecution(
                signal_id=signal.id,
                actual_price=100.0 + index,
                filled_qty=3.0,
                commission=0.0,
                timestamp=datetime.now(timezone.utc),
                broker_order_id="bo-concurrent-cap",
                dedupe_key=f"alpaca:concurrent:{index}",
            ),
            3.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_persist, (1, 2)))

    assert sorted(results) == [0.0, 3.0]
    assert database.get_live_signal_filled_qty(str(signal.id)) == pytest.approx(3.0)


def test_broker_order_id_lookup_is_scoped_to_account(isolated_db):
    database = isolated_db
    deployment_a = _deployment()
    deployment_b = LiveDeploymentConfig(
        **{**deployment_a.model_dump(), "id": str(uuid4()), "account_id": "acc-2"}
    )
    _verified_demo_account(database, "acc-1", "ctrader")
    _verified_demo_account(database, "acc-2", "ctrader")
    database.create_live_deployment(deployment_a)
    database.create_live_deployment(deployment_b)
    signal_a = LiveSignal(
        deployment_id=str(deployment_a.id), symbol="EURUSD", action="buy",
        theoretical_price=1.1, timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={},
    )
    signal_b = LiveSignal(
        deployment_id=str(deployment_b.id), symbol="EURUSD", action="sell",
        theoretical_price=1.2, timestamp=datetime(2026, 7, 15, 10, 1, tzinfo=timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal_a)
    database.save_live_signal(signal_b)
    assert database.set_live_signal_broker_order_id(str(signal_a.id), "45678") is True
    assert database.set_live_signal_broker_order_id(str(signal_b.id), "45678") is True

    assert database.find_live_signal_id_by_broker_order_id("acc-1", "45678") == str(signal_a.id)
    assert database.find_live_signal_id_by_broker_order_id("acc-2", "45678") == str(signal_b.id)
    assert database.find_live_signal_id_by_broker_order_id("", "45678") is None


def test_alpaca_partial_fill_persists_event_qty_not_cumulative(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    saved: list[tuple[LiveExecution, float]] = []

    def _capture(execution: LiveExecution, cumulative_qty: float) -> float:
        saved.append((execution, cumulative_qty))
        return execution.filled_qty

    monkeypatch.setattr(module, "save_live_execution_up_to_cumulative", _capture)

    signal_id = str(uuid4())
    update = SimpleNamespace(
        event="partial_fill",
        qty="100",  # per-event quantity
        price="50.0",
        timestamp=datetime.now(timezone.utc),
        execution_id="exec-9",
        order=SimpleNamespace(
            client_order_id=signal_id,
            id=_alpaca_order_id("partial-fill"),
            filled_qty="3500",  # cumulative - must NOT be persisted
            filled_avg_price="49.9",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0][0].filled_qty == 100.0
    assert saved[0][0].dedupe_key == "alpaca:exec:exec-9"
    assert saved[0][0].broker_order_id == _alpaca_order_id("partial-fill")
    assert saved[0][1] == 3500.0


@pytest.mark.parametrize("broker_order_id", [None, "", "   ", 0, "0", False])
def test_alpaca_stream_rejects_missing_or_nonpositive_order_id(
    monkeypatch, broker_order_id
) -> None:
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    update = SimpleNamespace(
        event="fill",
        qty="1",
        price="50.0",
        timestamp=datetime.now(timezone.utc),
        execution_id="exec-invalid-id",
        order=SimpleNamespace(
            client_order_id=str(uuid4()),
            id=broker_order_id,
            filled_qty="1",
            filled_avg_price="50.0",
            commission=None,
        ),
    )
    monkeypatch.setattr(
        module,
        "save_live_execution",
        lambda _execution: pytest.fail("invalid stream order id must not be persisted"),
    )
    monkeypatch.setattr(
        module,
        "save_live_execution_up_to_cumulative",
        lambda _execution, _cumulative: pytest.fail(
            "invalid stream order id must not be persisted"
        ),
    )

    asyncio.run(adapter._on_trade_update(update))


def test_alpaca_delayed_stream_fill_is_reported_as_duplicate(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    output = io.StringIO()
    stdout_handler = next(
        handler
        for handler in module.logger.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    )
    monkeypatch.setattr(stdout_handler, "stream", output)
    monkeypatch.setattr(
        module, "save_live_execution_up_to_cumulative", lambda _execution, _cumulative: 0.0
    )
    update = SimpleNamespace(
        event="fill",
        qty="1",
        price="50.0",
        timestamp=datetime.now(timezone.utc),
        execution_id="exec-delayed",
        order=SimpleNamespace(
            client_order_id=str(uuid4()),
            id=_alpaca_order_id("delayed"),
            filled_qty="3",
            filled_avg_price="50.0",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    records = [
        json.loads(line)
        for line in output.getvalue().splitlines()
        if line.strip()
    ]
    assert any(
        record.get("message") == "Skipped duplicate Alpaca execution fill"
        for record in records
    )


def test_alpaca_partial_fill_without_event_qty_is_skipped(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    update = SimpleNamespace(
        event="partial_fill",
        timestamp=datetime.now(timezone.utc),
        order=SimpleNamespace(
            client_order_id=str(uuid4()),
            id=_alpaca_order_id("no-event-qty"),
            filled_qty="3500",
            filled_avg_price="49.9",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert saved == []


def test_alpaca_reconcile_backfills_missing_fill(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(
        api_key="k", secret_key="s", paper=True, account_id="alpaca-acc-1"
    )
    signal_id = str(uuid4())
    queried: list[tuple[str, int]] = []
    monkeypatch.setattr(
        module,
        "list_live_signal_ids_for_reconciliation",
        lambda account_id, limit: queried.append((account_id, limit)) or [signal_id],
    )
    monkeypatch.setattr(module, "get_live_signal_filled_qty", lambda _signal_id: 0.0)
    saved: list[tuple[LiveExecution, float]] = []
    monkeypatch.setattr(
        module,
        "save_live_execution_up_to_cumulative",
        lambda e, cumulative: saved.append((e, cumulative)) or e.filled_qty,
    )

    _client = SimpleNamespace(
        get_order_by_client_id=lambda _cid: SimpleNamespace(
            id=_alpaca_order_id("reconcile-fill"),
            filled_qty="25",
            filled_avg_price="101.5",
            filled_at=datetime.now(timezone.utc),
        )
    )
    adapter._trading_client = _client

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 1
    assert queried == [("alpaca-acc-1", 200)]
    assert saved[0][0].filled_qty == 25.0
    assert saved[0][0].broker_order_id == _alpaca_order_id("reconcile-fill")
    assert saved[0][0].dedupe_key == (
        f"alpaca:order:{_alpaca_order_id('reconcile-fill')}:reconciled:25.0"
    )
    assert str(saved[0][0].signal_id) == signal_id
    assert saved[0][1] == 25.0


def test_alpaca_reconcile_pages_past_candidate_limit(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(
        api_key="k", secret_key="s", paper=True, account_id="alpaca-acc-1"
    )
    signal_ids = [str(uuid4()) for _ in range(5)]
    cursors = {
        signal_id: (datetime(2026, 7, 15, 10, minute, tzinfo=timezone.utc), signal_id)
        for minute, signal_id in enumerate(signal_ids)
    }
    queried: list[tuple[str, int, tuple[datetime, str] | None]] = []

    def list_page(account_id, limit, *, before=None):
        queried.append((account_id, limit, before))
        if before is None:
            return signal_ids[:2]
        if before == cursors[signal_ids[1]]:
            return signal_ids[2:4]
        if before == cursors[signal_ids[3]]:
            return signal_ids[4:]
        return []

    monkeypatch.setattr(module, "list_live_signal_ids_for_reconciliation", list_page)
    monkeypatch.setattr(
        module,
        "get_live_signal_reconciliation_cursor",
        lambda signal_id: cursors[signal_id],
        raising=False,
    )
    monkeypatch.setattr(module, "get_live_signal_filled_qty", lambda _signal_id: 0.0)
    monkeypatch.setattr(
        module, "save_live_execution_up_to_cumulative", lambda execution, _cap: execution.filled_qty
    )
    broker_queries: list[str] = []
    adapter._trading_client = SimpleNamespace(
        get_order_by_client_id=lambda signal_id: broker_queries.append(signal_id)
        or SimpleNamespace(
            id=_alpaca_order_id(signal_id),
            filled_qty="1",
            filled_avg_price="200",
            filled_at=datetime.now(timezone.utc),
        )
    )

    recovered = asyncio.run(adapter.reconcile_executions(max_signals=2))

    assert recovered == 5
    assert broker_queries == signal_ids
    assert queried == [
        ("alpaca-acc-1", 2, None),
        ("alpaca-acc-1", 2, cursors[signal_ids[1]]),
        ("alpaca-acc-1", 2, cursors[signal_ids[3]]),
    ]


def test_alpaca_reconcile_backfills_only_missing_partial_fill_delta(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(
        api_key="k", secret_key="s", paper=True, account_id="alpaca-acc-1"
    )
    signal_id = str(uuid4())
    queried: list[tuple[str, int]] = []
    monkeypatch.setattr(
        module,
        "list_live_signal_ids_for_reconciliation",
        lambda account_id, limit: queried.append((account_id, limit)) or [signal_id],
        raising=False,
    )
    monkeypatch.setattr(
        module, "get_live_signal_filled_qty", lambda _signal_id: 1.0, raising=False
    )
    saved: list[tuple[LiveExecution, float]] = []
    monkeypatch.setattr(
        module,
        "save_live_execution_up_to_cumulative",
        lambda e, cumulative: saved.append((e, cumulative)) or e.filled_qty,
    )
    adapter._trading_client = SimpleNamespace(
        get_order_by_client_id=lambda _cid: SimpleNamespace(
            id=_alpaca_order_id("partial-recovery"),
            filled_qty="3",
            filled_avg_price="101.5",
            filled_at=datetime.now(timezone.utc),
        )
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert queried == [("alpaca-acc-1", 200)]
    assert recovered == 1
    assert len(saved) == 1
    assert saved[0][0].filled_qty == pytest.approx(2.0)
    assert saved[0][0].dedupe_key == (
        f"alpaca:order:{_alpaca_order_id('partial-recovery')}:reconciled:3.0"
    )
    assert saved[0][1] == 3.0


@pytest.mark.parametrize("local_filled_qty", [3.0, 4.0])
def test_alpaca_reconcile_skips_non_positive_partial_fill_delta(
    monkeypatch, local_filled_qty
):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(
        api_key="k", secret_key="s", paper=True, account_id="alpaca-acc-1"
    )
    signal_id = str(uuid4())
    monkeypatch.setattr(
        module,
        "list_live_signal_ids_for_reconciliation",
        lambda _account_id, _limit: [signal_id],
    )
    monkeypatch.setattr(
        module, "get_live_signal_filled_qty", lambda _signal_id: local_filled_qty
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)
    adapter._trading_client = SimpleNamespace(
        get_order_by_client_id=lambda _cid: SimpleNamespace(
            id=_alpaca_order_id("no-delta"),
            filled_qty="3",
            filled_avg_price="101.5",
            filled_at=datetime.now(timezone.utc),
        )
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 0
    assert saved == []


# ── cTrader adapter specific tests ──────────────────────────────────────────────


def test_ctrader_fill_fallback_lookup_uses_canonical_account_scope(monkeypatch):
    module, adapter = _make_ctrader_adapter()
    adapter.account_config_id = "local-account-123"
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    queried: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "find_live_signal_id_by_broker_order_id",
        lambda account_id, order_id: queried.append((account_id, order_id)) or None,
    )
    event = SimpleNamespace(
        executionType=1,
        order=SimpleNamespace(orderId=45678, clientOrderId=""),
        deal=None,
        HasField=lambda field: field == "order",
    )

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(event))

    assert queried == [("local-account-123", "45678")]


def test_ctrader_reconcile_backfills_missing_fill(monkeypatch):
    """cTrader reconcile_executions backfills fills for signals with broker_order_id."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_id = str(uuid4())

    queried_scopes: list[tuple[str, int]] = []
    monkeypatch.setattr(
        module,
        "list_live_signal_ids_for_reconciliation",
        lambda account_id, limit: queried_scopes.append((account_id, limit)) or [signal_id],
    )
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: "45678"
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    # Mock _send_request to return a reconciled order with deals
    # #80: fills scale per symbol — the fake deal carries its symbolId.
    adapter._symbol_name_to_id = {"EURUSD": 1}
    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,  # FILLED
        symbolId=1,
        filledVolume=5_000_000,
        executionPrice=1.2345,
        executionTimestamp=1700000000000,
        commission=0.0,
    )
    # Create the response object using the adapter's response class so isinstance() works
    # We need to mock the Protobuf types that are loaded by _load_openapi_types
    # Since _load_openapi_types requires a running event loop and Twisted reactor,
    # we mock the specific types needed for the reconcile logic.
    adapter._ProtoOAOrderDetailsRes = type("ProtoOAOrderDetailsRes", (), {"deal": [deal]})
    # Mock _send_request to return the details response
    order_details_res = adapter._ProtoOAOrderDetailsRes()

    async def _mock_send_request(req, *, timeout_seconds=15):
        return order_details_res

    adapter._send_request = _mock_send_request
    adapter._ProtoOAOrderDetailsReq = SimpleNamespace
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=1, PARTIALLY_FILLED=2)

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 1
    assert len(saved) == 1
    # Unit contract: filled_qty is persisted in qty units (broker volume / 1000).
    assert saved[0].filled_qty == pytest.approx(5000.0)
    assert saved[0].broker_order_id == "45678"
    assert saved[0].dedupe_key == "ctrader:deal:999"
    assert queried_scopes == [("123", 200)]


def test_ctrader_reconcile_pages_past_candidate_limit(monkeypatch):
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_ids = [str(uuid4()) for _ in range(5)]
    cursors = {
        signal_id: (datetime(2026, 7, 15, 10, minute, tzinfo=timezone.utc), signal_id)
        for minute, signal_id in enumerate(signal_ids)
    }
    queried: list[tuple[str, int, tuple[datetime, str] | None]] = []

    def list_page(account_id, limit, *, before=None):
        queried.append((account_id, limit, before))
        if before is None:
            return signal_ids[:2]
        if before == cursors[signal_ids[1]]:
            return signal_ids[2:4]
        if before == cursors[signal_ids[3]]:
            return signal_ids[4:]
        return []

    monkeypatch.setattr(module, "list_live_signal_ids_for_reconciliation", list_page)
    monkeypatch.setattr(
        module,
        "get_live_signal_reconciliation_cursor",
        lambda signal_id: cursors[signal_id],
        raising=False,
    )
    order_ids = {signal_id: str(45000 + index) for index, signal_id in enumerate(signal_ids)}
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda signal_id: order_ids[signal_id]
    )

    class _OrderDetailsReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.orderId = 0

    class _OrderDetailsRes:
        deal = []

    requested_order_ids: list[int] = []

    async def _send(request, *, timeout_seconds=15):
        requested_order_ids.append(request.orderId)
        return _OrderDetailsRes()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOAOrderDetailsReq = _OrderDetailsReq
    adapter._ProtoOAOrderDetailsRes = _OrderDetailsRes
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=1, PARTIALLY_FILLED=2)

    recovered = asyncio.run(adapter.reconcile_executions(max_signals=2))

    assert recovered == 0
    assert requested_order_ids == [int(order_ids[signal_id]) for signal_id in signal_ids]
    assert queried == [
        ("123", 2, None),
        ("123", 2, cursors[signal_ids[1]]),
        ("123", 2, cursors[signal_ids[3]]),
    ]


def test_ctrader_reconcile_recovers_accepted_order_before_local_id_persistence(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """Restart recovery finds an accepted order by the stable signal client id."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_id = str(uuid4())
    signal_timestamp = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        module, "list_live_signal_ids_for_reconciliation", lambda _account_id, _limit: [signal_id]
    )
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(
        module,
        "get_live_signal_timestamp",
        lambda _sid: signal_timestamp,
        raising=False,
    )
    persisted_order_ids: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted_order_ids.append((sid, oid)) or True,
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    class _OrderListReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.fromTimestamp = 0
            self.toTimestamp = 0

    class _OrderDetailsReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.orderId = 0

    class _OrderListRes:
        def __init__(self) -> None:
            self.order = [SimpleNamespace(orderId=45678, clientOrderId=signal_id)]
            self.hasMore = False

    # #80: fills scale per symbol — the fake deal carries its symbolId.
    adapter._symbol_name_to_id = {"EURUSD": 1}
    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,
        symbolId=1,
        filledVolume=5000,
        executionPrice=1.2345,
        executionTimestamp=1700000000000,
        commission=0.0,
    )

    class _OrderDetailsRes:
        def __init__(self) -> None:
            self.deal = [deal]

    requests: list[object] = []

    async def _send(request, *, timeout_seconds=15):
        requests.append(request)
        if isinstance(request, _OrderListReq):
            return _OrderListRes()
        if isinstance(request, _OrderDetailsReq):
            return _OrderDetailsRes()
        raise AssertionError(f"Unexpected request: {type(request).__name__}")

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOAOrderListReq = _OrderListReq
    adapter._ProtoOAOrderListRes = _OrderListRes
    adapter._ProtoOAOrderDetailsReq = _OrderDetailsReq
    adapter._ProtoOAOrderDetailsRes = _OrderDetailsRes
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=1, PARTIALLY_FILLED=2)

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 1
    assert persisted_order_ids == [(signal_id, "45678")]
    assert adapter._order_id_to_signal_id == {45678: signal_id}
    assert len(requests) == 2
    lookup = requests[0]
    assert isinstance(lookup, _OrderListReq)
    assert lookup.ctidTraderAccountId == 123
    assert lookup.fromTimestamp < int(signal_timestamp.timestamp() * 1000)
    assert lookup.toTimestamp > int(signal_timestamp.timestamp() * 1000)
    assert saved[0].signal_id == UUID(signal_id)
    assert saved[0].broker_order_id == "45678"
    assert saved[0].dedupe_key == "ctrader:deal:999"


def test_ctrader_discord_redelivery_uses_canonical_signal_without_resubmitting(monkeypatch):
    """A repeated Discord message with a new UUID must reuse the canonical order."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    canonical_signal_id = str(uuid4())
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "discord_message_id": "discord-123"},
    )

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id",
        lambda sid: "45678" if sid == canonical_signal_id else "",
    )
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (canonical_signal_id, False)
    )

    async def _unexpected_send(*_args, **_kwargs):
        raise AssertionError("Discord redelivery submitted a second broker order")

    adapter._send_request_with_refresh_retry = _unexpected_send

    assert asyncio.run(adapter.submit_order(signal, qty=1.0)) == "45678"
    assert adapter._order_id_to_signal_id == {45678: canonical_signal_id}


def test_ctrader_redelivery_with_persisted_order_id_needs_no_broker_connection(
    monkeypatch,
):
    """Local broker confirmation must short-circuit even while broker is unavailable."""
    module, adapter = _make_ctrader_adapter()
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0},
    )
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: "45678"
    )

    async def _unexpected_connect():
        raise AssertionError("persisted order redelivery attempted broker connection")

    adapter.connect = _unexpected_connect

    broker_order_id = asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert broker_order_id == "45678"
    assert adapter._order_id_to_signal_id == {45678: str(signal.id)}


def test_ctrader_redelivery_recovers_accepted_order_without_resubmitting(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """A persisted signal with an accepted broker order must not submit twice."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0},
    )

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(
        module,
        "get_live_signal_timestamp",
        lambda _sid: signal.timestamp,
        raising=False,
    )
    persisted_order_ids: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted_order_ids.append((sid, oid)) or True,
    )

    class _OrderListReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.fromTimestamp = 0
            self.toTimestamp = 0

    class _OrderListRes:
        def __init__(self) -> None:
            self.order = [
                SimpleNamespace(orderId=45678, clientOrderId=str(signal.id))
            ]
            self.hasMore = False

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""

    async def _send(request, *, timeout_seconds=15):
        if isinstance(request, _OrderListReq):
            return _OrderListRes()
        if isinstance(request, _NewOrderReq):
            raise AssertionError("redelivery submitted a duplicate broker order")
        raise AssertionError(f"Unexpected request: {type(request).__name__}")

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOAOrderListReq = _OrderListReq
    adapter._ProtoOAOrderListRes = _OrderListRes
    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAOrderErrorEvent = type("_OrderError", (), {})
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    broker_order_id = asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert broker_order_id == "45678"
    assert persisted_order_ids == [(str(signal.id), "45678")]
    assert adapter._order_id_to_signal_id == {45678: str(signal.id)}


def test_ctrader_submit_order_fails_closed_when_broker_id_is_missing(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """#77: an order response without a usable order id must raise, not
    return the success-shaped "" that downstream treats as accepted."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 1.05, "take_profit": 1.15},
    )

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(signal.id), True))
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

    class _IdLessResponse:
        orderId = 0  # absent/unusable id

    async def _send(request, *, timeout_seconds=20, _depth=0):
        assert isinstance(request, _NewOrderReq)
        return _IdLessResponse()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAExecutionEvent = type("_ExecutionEvent", (), {})
    adapter._ProtoOAOrderErrorEvent = type("_OrderError", (), {})
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    with pytest.raises(RuntimeError, match="no usable order id"):
        asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert adapter._order_id_to_signal_id == {}


def test_ctrader_submit_order_with_id_persists_and_returns_id(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """#77 companion: an order WITH an id still persists and returns it."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 1.05, "take_profit": 1.15},
    )

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(signal.id), True))
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    persisted_order_ids: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted_order_ids.append((sid, oid)) or True,
    )

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

    class _AcceptedOrderRes:
        orderId = 45678

    async def _send(request, *, timeout_seconds=20, _depth=0):
        assert isinstance(request, _NewOrderReq)
        return _AcceptedOrderRes()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAExecutionEvent = type("_ExecutionEvent", (), {})
    adapter._ProtoOAOrderErrorEvent = type("_OrderError", (), {})
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    broker_order_id = asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert broker_order_id == "45678"
    assert persisted_order_ids == [(str(signal.id), "45678")]
    assert adapter._order_id_to_signal_id == {45678: str(signal.id)}


def test_ctrader_accepted_order_fails_closed_when_local_id_persistence_fails(monkeypatch):
    """Broker acceptance must not be reported as durable when DB confirmation fails."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 1.05, "take_profit": 1.2},
    )

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(signal.id), True))
    monkeypatch.setattr(
        module, "set_live_signal_broker_order_id", lambda _sid, _order_id: False
    )

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

    class _AcceptedOrderRes:
        orderId = 45678

    async def _send(request, *, timeout_seconds=20, _depth=0):
        assert isinstance(request, _NewOrderReq)
        return _AcceptedOrderRes()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAExecutionEvent = type("_ExecutionEvent", (), {})
    adapter._ProtoOAOrderErrorEvent = type("_OrderError", (), {})
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    with pytest.raises(RuntimeError, match="persist cTrader broker order id"):
        asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert adapter._order_id_to_signal_id == {}


@pytest.mark.parametrize(
    ("orders", "has_more"),
    [
        ([], False),
        (
            [
                SimpleNamespace(orderId=45678, clientOrderId="signal-1"),
                SimpleNamespace(orderId=45679, clientOrderId="signal-1"),
            ],
            False,
        ),
        ([SimpleNamespace(orderId=45678, clientOrderId="signal-1")], True),
    ],
)
def test_ctrader_order_recovery_fails_closed_when_match_is_not_unique(
    orders, has_more
):
    """Zero, duplicate, or incomplete broker results must never select an order."""
    module, adapter = _make_ctrader_adapter()

    class _OrderListReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.fromTimestamp = 0
            self.toTimestamp = 0

    class _OrderListRes:
        def __init__(self) -> None:
            self.order = orders
            self.hasMore = has_more

    async def _send(request, *, timeout_seconds=15):
        assert isinstance(request, _OrderListReq)
        return _OrderListRes()

    adapter._send_request_with_refresh_retry = _send
    adapter._ProtoOAOrderListReq = _OrderListReq
    adapter._ProtoOAOrderListRes = _OrderListRes

    recovered = asyncio.run(
        adapter._recover_broker_order_id(
            "signal-1", datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
        )
    )

    assert recovered is None


def test_ctrader_reconcile_skips_signals_without_broker_order_id(monkeypatch, isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    """cTrader reconcile must skip signals that have no broker_order_id persisted."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_id = str(uuid4())

    monkeypatch.setattr(
        module, "list_live_signal_ids_for_reconciliation", lambda _account_id, _limit: [signal_id]
    )
    # Return empty string for broker_order_id -> signal never reached broker
    monkeypatch.setattr(
        module, "get_live_signal_broker_order_id", lambda _sid: ""
    )
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 0
    assert saved == []


def test_ctrader_reconcile_skips_non_numeric_broker_order_ids(monkeypatch):
    """cTrader reconcile must skip Alpaca-style UUID broker_order_ids (non-numeric)."""
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._authenticated = True
    signal_id = str(uuid4())

    monkeypatch.setattr(
        module, "list_live_signal_ids_for_reconciliation", lambda _account_id, _limit: [signal_id]
    )
    # Alpaca UUID format - not numeric, should be skipped
    monkeypatch.setattr(
        module,
        "get_live_signal_broker_order_id",
        lambda _sid: "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 0
    assert saved == []


# ── Service restart semantics ──────────────────────────────────────────────────


def test_recover_failed_services_respects_paused_and_stopped(monkeypatch):
    """Restart must not resurrect services an operator paused or stopped."""
    from live_runner import daemon as module
    from shared.models import Service, ServiceStatus, ServiceType

    def _service(name: str, status: ServiceStatus) -> Service:
        return Service(
            name=name,
            type=ServiceType.DISCORD_BOT,
            status=status,
            config={},
            created_at=datetime.now(timezone.utc),
        )

    services = [
        _service("paused-bot", ServiceStatus.PAUSED),
        _service("stopped-bot", ServiceStatus.STOPPED),
        _service("failed-bot", ServiceStatus.FAILED),
        _service("running-bot", ServiceStatus.RUNNING),
    ]
    updated: list[tuple[str, ServiceStatus]] = []

    monkeypatch.setattr(module, "list_services", lambda: services)
    monkeypatch.setattr(
        module,
        "update_service_status",
        lambda service_id, status: updated.append((service_id, status)),
    )

    module._recover_failed_services()

    assert len(updated) == 1
    assert updated[0][0] == str(services[2].id)
    assert updated[0][1] == ServiceStatus.RUNNING


# ── Cleanup 15-A: unknown signal actions must never become SELL ────────────────


def _live_signal_with_action(action: str) -> LiveSignal:
    """Build a signal whose action bypasses the pydantic Literal gate.

    The model already rejects unknown actions at construction time; the adapter
    guard exists for defense in depth (e.g. signals loaded from persisted
    metadata, future model changes, or in-memory copies with relaxed typing).
    """
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 95.0, "take_profit": 110.0},
    )
    object.__setattr__(signal, "__dict__", {**signal.__dict__, "action": action})
    return signal


def _seed_ctrader_fixture_scale(adapter, symbol: str = "AAPL") -> None:
    """Seed an FX-equivalent fixture scale for synthetic test symbols (#80).

    Fixture symbols like AAPL match no fallback class; seeding declares the
    test scale explicitly instead of relying on a guessed default.
    """
    from live_runner import ctrader_adapter as module

    adapter._symbol_props[str(symbol).upper()] = module.SymbolScale(
        volume_per_qty=1000.0,
        price_digits=5,
        min_volume=1000,
        lot_size=100_000.0,
        source="test",
    )


def _make_ctrader_request_builder_adapter():
    """Return (module, adapter) with cTrader proto stubs for _build_new_order_request."""
    module, adapter = _make_ctrader_adapter()
    _seed_ctrader_fixture_scale(adapter)

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)
    return module, adapter


def test_alpaca_rejects_unknown_signal_action_instead_of_selling():
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True

    def _forbidden(*_args, **_kwargs):
        pytest.fail("unknown action must not reach the broker as a SELL order")

    adapter.get_balance = lambda: asyncio.sleep(0, (100_000.0, 100_000.0))
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=_forbidden,
    )

    bad_signal = _live_signal_with_action("close")
    submission = adapter.submit_order(bad_signal)
    with pytest.raises(ValueError, match=r"[Uu]nsupported .*action 'close'"):
        asyncio.run(submission)


def test_alpaca_build_order_request_rejects_unknown_signal_action():
    """The request-builder guard must hold even when submit_order is bypassed."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    bad_signal = _live_signal_with_action("close")

    builder = adapter._build_order_request
    kwargs = dict(
        qty=1.0,
        order_side=SimpleNamespace(BUY="buy", SELL="sell"),
        time_in_force=SimpleNamespace(DAY="day"),
        market_request_cls=lambda **kw: ("market", kw),
        client_order_id="cid-unknown-action",
    )
    with pytest.raises(ValueError, match=r"[Uu]nsupported .*action: close"):
        builder(bad_signal, **kwargs)


def test_ctrader_rejects_unknown_signal_action_instead_of_selling(isolated_db):
    # Isolated: without a database fixture this reached the configured database.
    module, adapter = _make_ctrader_adapter()
    adapter._client = object()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"AAPL": 1}

    class _NewOrderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.symbolId = 0
            self.orderType = 0
            self.tradeSide = 0
            self.volume = 0
            self.timeInForce = 0
            self.clientOrderId = ""
            self.stopLoss = 0
            self.takeProfit = 0

    async def _forbidden_send(request, *, timeout_seconds=20, _depth=0):
        raise AssertionError(
            f"unknown action submitted a broker order: {type(request).__name__}"
        )

    adapter._send_request_with_refresh_retry = _forbidden_send

    async def _balance() -> tuple[float, float]:
        return 10_000.0, 10_000.0

    adapter.get_balance = _balance
    adapter._ProtoOANewOrderReq = _NewOrderReq
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    bad_signal = _live_signal_with_action("close")
    submission = adapter.submit_order(bad_signal)
    with pytest.raises(ValueError, match=r"[Uu]nsupported .*action 'close'"):
        asyncio.run(submission)


def test_ctrader_new_order_request_carries_sl_tp_prices():
    """The computed SL/TP must land on the broker request, not just metadata."""
    module, adapter = _make_ctrader_request_builder_adapter()

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 1.05, "take_profit": 1.2},
    )

    request = adapter._build_new_order_request(signal, symbol_id=7)

    assert request.symbolId == 7
    # MARKET orders ignore the absolute stopLoss/takeProfit fields; the
    # distances must arrive via the relative int64 fields (1/100000 units):
    # BUY stop = entry - relativeStopLoss, take = entry + relativeTakeProfit.
    assert request.relativeStopLoss == int(round(abs(1.1 - 1.05) * 100_000))
    assert request.relativeTakeProfit == int(round(abs(1.2 - 1.1) * 100_000))


@pytest.mark.parametrize(
    "alias", ["stop_loss", "sl_price", "stop_price", "resolved_stop_loss"]
)
def test_ctrader_rejects_malformed_stop_alias(alias: str) -> None:
    _module, adapter = _make_ctrader_request_builder_adapter()
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, alias: "not-a-price", "take_profit": 1.2},
    )

    with pytest.raises(ValueError, match=f"Invalid {alias}"):
        adapter._build_new_order_request(signal, symbol_id=7)


def test_ctrader_relative_sl_tp_direction_and_rejections():
    """Relative distances are signed by trade side at the protocol level; the
    sent magnitude must be the positive distance in 1/100000 price units, and
    wrong-side/non-finite/zero-distance prices must fail closed."""
    _module, adapter = _make_ctrader_request_builder_adapter()
    module, adapter = _make_ctrader_request_builder_adapter()

    def _signal(action, stop_loss, take_profit, theoretical_price=1.1):
        return LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action=action,
            theoretical_price=theoretical_price,
            timestamp=datetime.now(timezone.utc),
            metadata={"qty": 1.0, "stop_loss": stop_loss, "take_profit": take_profit},
        )

    buy_request = adapter._build_new_order_request(_signal("buy", 1.05, 1.2), symbol_id=7)
    assert buy_request.relativeStopLoss == int(round(0.05 * 100_000))
    assert buy_request.relativeTakeProfit == int(round(0.1 * 100_000))

    sell_request = adapter._build_new_order_request(_signal("sell", 1.2, 1.0), symbol_id=7)
    assert sell_request.relativeStopLoss == int(round(0.1 * 100_000))
    assert sell_request.relativeTakeProfit == int(round(0.1 * 100_000))

    wrong_side = adapter._build_new_order_request
    wrong_side_signal = _signal("buy", 1.2, 1.05)
    with pytest.raises(ValueError, match="wrong side of the reference price"):
        wrong_side(wrong_side_signal, symbol_id=7)
    nan_signal = _signal("buy", float("nan"), 1.2)
    with pytest.raises(ValueError, match="Invalid SL/TP price"):
        wrong_side(nan_signal, symbol_id=7)
    zero_distance_signal = _signal("buy", 1.1, 1.2)
    with pytest.raises(ValueError, match="wrong side of the reference price"):
        wrong_side(zero_distance_signal, symbol_id=7)
    sub_unit_signal = _signal("buy", 1.099995, 1.2)
    with pytest.raises(ValueError, match="below one 1/100000 price unit"):
        wrong_side(sub_unit_signal, symbol_id=7)


def test_ctrader_new_order_request_fails_closed_without_sl_tp():
    module, adapter = _make_ctrader_request_builder_adapter()

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0},
    )

    request = adapter._build_new_order_request
    with pytest.raises(ValueError, match="without stop_loss"):
        request(signal, symbol_id=7)


def test_ctrader_build_new_order_request_rejects_unknown_signal_action():
    """The request-builder guard must hold even when submit_order is bypassed."""
    module, adapter = _make_ctrader_request_builder_adapter()
    bad_signal = _live_signal_with_action("close")

    with pytest.raises(ValueError, match=r"[Uu]nsupported .*action: close"):
        adapter._build_new_order_request(bad_signal, symbol_id=7)


def test_alpaca_rejects_non_finite_sl_tp_prices():
    """NaN/inf prices must fail closed like missing ones — a NaN stop_price
    reaches the broker as an unvalidatable order and blocks the position."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    for bad in (float("nan"), float("inf"), float("-inf")):
        signal = LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"stop_loss": bad, "take_profit": 110.0},
        )
        with pytest.raises(ValueError, match="Invalid stop_loss price"):
            adapter._resolve_broker_sl_tp(signal)


@pytest.mark.parametrize(
    "alias", ["stop_loss", "sl_price", "stop_price", "resolved_stop_loss"]
)
def test_alpaca_rejects_malformed_stop_alias(alias: str) -> None:
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={alias: "not-a-price", "take_profit": 110.0},
    )

    with pytest.raises(ValueError, match=f"Invalid {alias}"):
        adapter._resolve_broker_sl_tp(signal)


def test_alpaca_build_order_request_carries_bracket_legs():
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0, "stop_loss": 95.0, "take_profit": 110.0},
    )

    request = adapter._build_order_request(
        signal,
        qty=2.0,
        order_side=SimpleNamespace(BUY="buy", SELL="sell"),
        time_in_force=SimpleNamespace(DAY="day"),
        market_request_cls=lambda **kwargs: ("market", kwargs),
        client_order_id="cid-bracket",
    )

    kind, kwargs = request
    assert kind == "market"
    assert kwargs["side"] == "buy"
    assert kwargs["order_class"] is module.OrderClass.BRACKET
    assert kwargs["take_profit"].limit_price == pytest.approx(110.0)
    assert kwargs["stop_loss"].stop_price == pytest.approx(95.0)


def test_alpaca_build_order_request_fails_closed_without_sl_tp():
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    naked_signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0},
    )

    resolve = adapter._resolve_broker_sl_tp
    with pytest.raises(ValueError, match="without stop_loss"):
        resolve(naked_signal)


def test_exit_only_orders_still_bypass_sizing_but_close_exact_position():
    """Exit-only must keep its exact-qty path (no risk sizing) and must not be
    blocked by the entry SL/TP guard."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(
        positions=[{"symbol": "EURUSD", "qty": 3.0}]
    )
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["explicit_qty"] == pytest.approx(3.0)
    assert adapter.submitted_signal.metadata["risk_sizing_mode"] == "exit_only"
    # Net-long book: the close stays a SELL.
    assert adapter.submitted_signal.action == "sell"
    # Union (branch side): the exact close quantity is what reaches the adapter.
    assert adapter.submitted_qty == pytest.approx(3.0)


def test_exit_only_metadata_discards_untrusted_close_volume() -> None:
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[{"symbol": "EURUSD", "qty": 3.0}])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True, "ctrader_volume": 999_999, "volume": 888_888},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_qty == pytest.approx(3.0)
    assert adapter.submitted_signal.metadata["explicit_qty"] == pytest.approx(3.0)
    assert "ctrader_volume" not in adapter.submitted_signal.metadata
    assert "volume" not in adapter.submitted_signal.metadata


def test_entry_metadata_discards_raw_broker_volume_but_keeps_risk_qty() -> None:
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = _live_signal(deployment)
    signal = signal.model_copy(
        update={
            "metadata": {
                **signal.metadata,
                "volume": 999_999,
                "ctrader_volume": 888_888,
            }
        }
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_qty == pytest.approx(200.0)
    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == pytest.approx(200.0)
    assert "volume" not in adapter.submitted_signal.metadata
    assert "ctrader_volume" not in adapter.submitted_signal.metadata


def test_exit_only_close_action_derives_the_close_side_from_the_book():
    """Pins `short -> "buy"`, the fact an action-based exit-only rule inverts.

    The close side is a CONSEQUENCE of the position side: a negative broker
    quantity is a short, and flattening a short means buying it back. If this
    ever returned "sell" for a short, every stop-loss and take-profit on a short
    would be submitted as a sell that doubles the position instead of closing
    it. Kept as a direct pin so the mapping cannot be "simplified" away.
    """
    from live_runner.daemon import _exit_only_close_action

    assert _exit_only_close_action([{"symbol": "EURUSD", "qty": -3.0}], "EURUSD") == "buy"
    assert _exit_only_close_action([{"symbol": "EURUSD", "qty": 3.0}], "EURUSD") == "sell"
    # Symbol matching is case-insensitive and other symbols are ignored.
    assert _exit_only_close_action(
        [{"symbol": "eurusd", "qty": -1.0}, {"symbol": "GBPUSD", "qty": 5.0}],
        "EURUSD",
    ) == "buy"
    # A flat book has no side to derive; the default is a long close, and the
    # no-position refusal upstream means this is never reached in practice.
    assert _exit_only_close_action([], "EURUSD") == "sell"

    # The daemon must stamp that derived side onto the metadata and onto the
    # signal, overriding the side the producer asked for.
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[{"symbol": "EURUSD", "qty": -3.0}])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )
    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.action == "buy"
    assert adapter.submitted_signal.metadata["action"] == "buy"
    assert adapter.submitted_signal.metadata["signal_action"] == "sell"


def test_exit_only_orders_close_short_positions_with_a_buy():
    """#76: an exit-only signal on a net-short book must submit a BUY that
    flattens the short magnitude instead of raising 'no long position'."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(
        positions=[{"symbol": "EURUSD", "qty": -3.0}]
    )
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.action == "buy"
    assert adapter.submitted_signal.metadata["explicit_qty"] == pytest.approx(3.0)
    assert adapter.submitted_signal.metadata["risk_sizing_mode"] == "exit_only"


def test_exit_only_metadata_cannot_open_a_long():
    """Exit-only metadata with no broker position would OPEN a position, so it
    is refused. The refusal is broker-derived (`no position is open`), not a
    string match on the action: the action is a consequence of the position
    side, and closing a short legitimately arrives as a buy."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"risk_sizing_mode": "exit_only"},
    )

    with pytest.raises(ValueError, match="no position is open"):
        asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is None
    assert adapter.submitted_qty is None


def test_risk_sized_entry_cannot_inherit_exit_only_metadata():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        # Merge resolution: the fixture predates the C1 owner decision that a
        # missing take profit FAILS the entry guard instead of being invented at
        # 1:1 risk-reward. Give it a bounded bracket so the test reaches the
        # metadata-inheritance assertion it is actually about.
        metadata={
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "exit_only": False,
            "risk_sizing_mode": "untrusted_entry_mode",
        },
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["exit_only"] is False
    assert adapter.submitted_signal.metadata["risk_sizing_mode"] == "risk_sized"


def _alpaca_exit_only_adapter(module, monkeypatch, positions):
    """Alpaca adapter wired to a fake broker holding `positions`.

    `submitted` collects every request that reached the broker, so a refusal is
    observable as "nothing was sent" rather than inferred from a raised error.
    """
    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    submitted: list[object] = []

    class _Client:
        def submit_order(self, order_data=None, **_kwargs):
            submitted.append(order_data)
            return SimpleNamespace(id=_alpaca_order_id("alpaca-exit-only"))

        def get_all_positions(self):
            return [
                SimpleNamespace(
                    symbol=position["symbol"],
                    qty=str(position["qty"]),
                    avg_entry_price="100.0",
                    current_price="100.0",
                    unrealized_pl="0.0",
                )
                for position in positions
            ]

    adapter._trading_client = _Client()
    monkeypatch.setattr(
        module, "save_live_signal", lambda signal: (str(signal.id), True)
    )
    monkeypatch.setattr(module, "set_live_signal_broker_order_id", lambda *_: True)
    return adapter, submitted, _alpaca_order_id("alpaca-exit-only")


def test_alpaca_exit_only_is_decided_by_metadata_not_by_the_order_side(monkeypatch):
    """The exit-only contract, asserted through the real submit path.

    A close's side is DERIVED from the broker book: flattening a SHORT is a
    BUY. An action-based rule therefore refuses exactly the exits that matter
    -- every stop-loss and take-profit on a short -- while letting long exits
    through. The decision must rest on the system-stamped metadata; what must
    still be refused is exit-only metadata with no broker position, which would
    OPEN an unbounded position instead of closing one.
    """
    from alpaca.trading.enums import OrderClass, OrderSide

    from live_runner import alpaca_adapter as module

    # (a) closing a short: action "buy" + exit-only metadata is a legitimate
    # exit, is routed down the close path, and the order is actually sent.
    adapter, submitted, broker_order_id = _alpaca_exit_only_adapter(
        module, monkeypatch, [{"symbol": "AAPL", "qty": -4.0}]
    )
    short_close = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 4.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )

    assert asyncio.run(adapter.submit_order(short_close, qty=4.0)) == broker_order_id

    assert len(submitted) == 1, "a short close must actually reach the broker"
    request = submitted[0]
    assert request.side == OrderSide.BUY, "a short is closed by buying it back"
    assert request.qty == pytest.approx(4.0)
    # The close path sheds the entry bracket, which is exactly what an
    # action-based guard wrongly protected against.
    assert request.order_class is OrderClass.SIMPLE
    assert request.stop_loss is None
    assert request.take_profit is None

    # (b) exit-only metadata with nothing to close would OPEN a position
    # instead, unbounded, because this path drops the mandatory SL/TP. Refused
    # for BOTH sides, with no order of any kind sent.
    empty_adapter, empty_submitted, _ = _alpaca_exit_only_adapter(
        module, monkeypatch, []
    )
    for action in ("buy", "sell"):
        unbacked = LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action=action,
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"qty": 2.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
        )
        with pytest.raises(ValueError, match="no live broker position to close"):
            asyncio.run(empty_adapter.submit_order(unbacked, qty=2.0))
    assert empty_submitted == [], "an unbacked exit-only order must never be sent"


def test_trade_signal_conversion_rejects_unknown_action_instead_of_selling():
    """The daemon-side conversion site must whitelist, not default: an action
    outside {buy, long, sell, short} is rejected rather than mapped to SELL."""
    from live_runner.daemon import _trade_signal_to_live_signal

    deployment = _deployment()
    base = TradeSignal(
        action="sell", symbol="AAPL", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={},
    )
    close = object.__new__(TradeSignal)
    for field_name, value in (
        ("action", "close"),
        ("symbol", "AAPL"),
        ("entry_price", 100.0),
        ("stop_loss", None),
        ("take_profit", None),
        ("timeframe", "1h"),
        ("metadata", {}),
    ):
        object.__setattr__(close, field_name, value)

    with pytest.raises(ValueError, match=r"Unsupported (signal )?action"):
        _trade_signal_to_live_signal(close, deployment)

    converted = _trade_signal_to_live_signal(base, deployment)
    assert converted.action == "sell"


def test_conversion_mints_key_only_with_producer_stamp():
    """CodeRabbit round: timestamp-less signals must not share a minted key."""
    from live_runner.daemon import _trade_signal_to_live_signal

    deployment = _deployment()

    def _signal(metadata: dict) -> TradeSignal:
        return TradeSignal(
            action="buy",
            symbol="AAPL",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=104.0,
            timeframe="1h",
            metadata=metadata,
        )

    naked = _trade_signal_to_live_signal(_signal({}), deployment)
    assert "idempotency_key" not in naked.metadata

    stamped = _trade_signal_to_live_signal(
        _signal({"signal_timestamp": "2026-07-15T10:00:00+00:00"}), deployment
    )
    restamped = _trade_signal_to_live_signal(
        _signal({"signal_timestamp": "2026-07-15T10:00:00+00:00"}), deployment
    )
    assert stamped.metadata["idempotency_key"]
    assert restamped.metadata["idempotency_key"] == stamped.metadata["idempotency_key"]


def test_alpaca_exit_only_orders_bypass_entry_sl_tp_guard():
    """Exit-only closes reduce exposure and carry no SL/TP; the adapter's
    entry guard must not block them, and the order must go out as a plain
    market order without bracket legs."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )

    request = adapter._build_order_request(
        signal,
        qty=3.0,
        order_side=SimpleNamespace(BUY="buy", SELL="sell"),
        time_in_force=SimpleNamespace(DAY="day"),
        market_request_cls=lambda **kwargs: ("market", kwargs),
        client_order_id="cid-exit",
    )

    kind, kwargs = request
    assert kind == "market"
    assert kwargs["qty"] == pytest.approx(3.0)
    assert kwargs["order_class"] is module.OrderClass.SIMPLE
    assert kwargs["take_profit"] is None
    assert kwargs["stop_loss"] is None


def test_alpaca_market_data_normalizes_broker_bars():
    from live_runner import alpaca_adapter as module

    timestamps = pd.Timestamp.now(tz="UTC").floor("min") - pd.to_timedelta(
        [3, 2, 1], unit="m"
    )
    index = pd.MultiIndex.from_product(
        [["AAPL"], timestamps], names=["symbol", "timestamp"]
    )
    bars = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10, 20, 30],
            "trade_count": [1, 2, 3],
        },
        index=index,
    )
    captured: dict[str, object] = {}
    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True

    def _get_stock_bars(request):
        captured["request"] = request
        return SimpleNamespace(df=bars)

    adapter._historical_data_client = SimpleNamespace(get_stock_bars=_get_stock_bars)

    frame = asyncio.run(adapter.get_market_data("aapl", timeframe="5m", limit=3))

    assert list(frame["symbol"]) == ["AAPL", "AAPL", "AAPL"]
    assert list(frame["close"]) == [100.5, 101.5, 102.5]
    assert isinstance(frame["timestamp"].dtype, pd.DatetimeTZDtype)
    assert len(frame) == 3
    assert captured["request"].limit == 1000


def test_alpaca_market_data_rejects_stale_bars():
    from live_runner import alpaca_adapter as module

    stale_index = pd.MultiIndex.from_product(
        [
            ["AAPL"],
            pd.date_range("2020-01-01", periods=3, freq="5min", tz="UTC"),
        ],
        names=["symbol", "timestamp"],
    )
    stale_bars = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10, 20, 30],
            "trade_count": [1, 2, 3],
        },
        index=stale_index,
    )
    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = SimpleNamespace(
        get_stock_bars=lambda _request: SimpleNamespace(df=stale_bars)
    )

    with pytest.raises(ValueError, match="stale"):
        asyncio.run(adapter.get_market_data("AAPL", timeframe="5m", limit=3))


def test_alpaca_entry_guard_still_blocks_naked_non_exit_orders():
    """The exit-only bypass must not open a hole: a plain entry without SL/TP
    is still refused."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0},
    )

    resolve = adapter._resolve_broker_sl_tp
    with pytest.raises(ValueError, match="without stop_loss"):
        resolve(signal)


def test_alpaca_unbounded_tp_rejects_invalid_stop() -> None:
    """CodeRabbit round: the unbounded early-return must validate the stop."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)

    def _signal(stop: object) -> LiveSignal:
        return LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "stop_loss": stop,
                "take_profit_policy": "unbounded_exit_on_opposite_signal",
            },
        )

    stop, _ = adapter._resolve_broker_sl_tp(_signal(95.0))
    assert stop == pytest.approx(95.0)
    for bad in (float("nan"), float("inf"), -5.0, 0.0):
        with pytest.raises(ValueError, match="Invalid stop_loss"):
            adapter._resolve_broker_sl_tp(_signal(bad))


def test_ctrader_exit_only_uses_position_addressed_close():
    module, adapter = _make_ctrader_request_builder_adapter()

    class _CloseReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.positionId = 0
            self.volume = 0

    adapter._ProtoOAClosePositionReq = _CloseReq
    request = adapter._build_close_position_request(position_id=77, volume=3000)

    assert request.positionId == 77
    assert request.volume == 3000
    for invalid_position_id in (0, -1, False):
        with pytest.raises(ValueError, match="position id must be positive"):
            adapter._build_close_position_request(
                position_id=invalid_position_id, volume=3000
            )
    for invalid_volume in (0, -1, False, float("nan")):
        with pytest.raises(ValueError, match="close volume"):
            adapter._build_close_position_request(
                position_id=77, volume=invalid_volume
            )
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )
    with pytest.raises(ValueError, match="position-addressed"):
        adapter._build_new_order_request(signal, symbol_id=7)


def test_ctrader_submit_exit_only_uses_broker_position_request(monkeypatch) -> None:
    module, adapter = _make_ctrader_adapter()
    adapter._connected = True
    adapter._authenticated = True
    adapter._ProtoOAExecutionEvent = type("ProtoOAExecutionEvent", (), {})
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._position_ids_by_symbol["EURUSD"] = (77,)
    signal_id = str(uuid4())
    signal = LiveSignal(
        id=UUID(signal_id),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "risk_sizing_mode": "exit_only"},
    )

    class _CloseReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.positionId = 0
            self.volume = 0

    adapter._ProtoOAClosePositionReq = _CloseReq
    requests: list[Any] = []
    saved_signals: list[LiveSignal] = []

    async def _submit(request):
        requests.append(request)
        return SimpleNamespace(orderId=901)

    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module,
        "save_live_signal",
        lambda saved_signal: (
            saved_signals.append(saved_signal) or (signal_id, True)
        ),
    )
    monkeypatch.setattr(module, "set_live_signal_broker_order_id", lambda _sid, _oid: True)
    adapter._submit_new_order_request = _submit

    assert asyncio.run(adapter.submit_order(signal, qty=3.0)) == "901"
    assert len(requests) == 1
    assert requests[0].positionId == 77
    assert requests[0].volume == 3000
    assert saved_signals[0].metadata["position_id"] == 77
    assert not hasattr(requests[0], "relativeStopLoss")


def test_ctrader_persisted_close_recovers_by_position_before_broker_requery(
    monkeypatch,
) -> None:
    module, adapter = _make_ctrader_adapter()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal_id = str(uuid4())
    timestamp = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    signal = LiveSignal(
        id=UUID(signal_id),
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=timestamp,
        metadata={"qty": 3.0, "risk_sizing_mode": "exit_only"},
    )
    recovery_calls: list[tuple[str, int | None]] = []

    async def _recover(recovery_signal_id, _timestamp, *, position_id):
        recovery_calls.append((recovery_signal_id, position_id))
        return "901"

    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: timestamp)
    monkeypatch.setattr(
        module,
        "get_live_signal_metadata",
        lambda _sid: {"risk_sizing_mode": "exit_only", "position_id": 77},
    )
    monkeypatch.setattr(module, "set_live_signal_broker_order_id", lambda _sid, _oid: True)
    monkeypatch.setattr(
        module,
        "save_live_signal",
        lambda _signal: pytest.fail("persisted close must not be resubmitted"),
    )
    monkeypatch.setattr(adapter, "_recover_broker_order_id", _recover)

    assert asyncio.run(adapter.submit_order(signal, qty=3.0)) == "901"
    assert recovery_calls == [(signal_id, 77)]
    assert adapter._position_ids_by_symbol == {}


def test_ctrader_exit_only_without_position_id_fails_closed() -> None:
    _module, adapter = _make_ctrader_adapter()
    with pytest.raises(ValueError, match="no broker position id"):
        adapter._position_id_for_exit("EURUSD")
    with pytest.raises(ValueError, match="no broker position id"):
        adapter._position_id_for_exit("EURUSD", 77)


def test_ctrader_exit_only_with_multiple_position_ids_fails_closed() -> None:
    _module, adapter = _make_ctrader_adapter()
    adapter._position_ids_by_symbol["EURUSD"] = (77, 78)
    with pytest.raises(ValueError, match="multiple broker positions"):
        adapter._position_id_for_exit("EURUSD")


def test_ctrader_entry_guard_still_blocks_naked_non_exit_orders():
    """The exit-only bypass must not open a hole on cTrader either."""
    module, adapter = _make_ctrader_request_builder_adapter()
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0},
    )

    builder = adapter._build_new_order_request
    with pytest.raises(ValueError, match="without stop_loss"):
        builder(signal, symbol_id=7)


def test_ctrader_exit_only_is_decided_by_metadata_not_by_the_order_side():
    """The exit-only contract, asserted where cTrader builds requests.

    A close's side is DERIVED from the broker book: flattening a SHORT is a
    BUY. An action-based rule therefore refuses exactly the exits that matter
    -- every stop-loss and take-profit on a short -- while letting long exits
    through. What the new-order path must refuse is exit-only metadata itself,
    for BOTH sides: an exit-only order is always a close addressed at a broker
    position id, never a new order.
    """
    from live_runner.daemon import submit_order_with_risk

    _module, adapter = _make_ctrader_request_builder_adapter()
    for action in ("buy", "sell"):
        exit_only = LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action=action,
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"qty": 2.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
        )
        with pytest.raises(ValueError, match="position-addressed close request"):
            adapter._build_new_order_request(exit_only, symbol_id=7)

    # (a) the proof that a short close is action "buy": the daemon derives the
    # side from the book, so the exit reaches the adapter as a BUY.
    deployment = _deployment()
    positions_adapter = _PositionsAdapter(
        positions=[{"symbol": "EURUSD", "qty": -2.0}]
    )
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )
    asyncio.run(submit_order_with_risk(positions_adapter, deployment, signal))

    assert positions_adapter.submitted_signal is not None
    assert positions_adapter.submitted_signal.action == "buy", (
        "closing a short is a buy; an action-based exit-only rule would have "
        "refused this order"
    )
    assert positions_adapter.submitted_signal.metadata["risk_sizing_mode"] == "exit_only"

    # (b) exit-only metadata with no broker position would OPEN a position
    # instead of closing one. Refused for BOTH sides, before anything is sent.
    for action in ("buy", "sell"):
        unbacked_adapter = _PositionsAdapter(positions=[])
        unbacked = LiveSignal(
            deployment_id=str(deployment.id),
            symbol="EURUSD",
            action=action,
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"risk_sizing_mode": "exit_only"},
        )
        with pytest.raises(ValueError, match="no position is open"):
            asyncio.run(
                submit_order_with_risk(unbacked_adapter, deployment, unbacked)
            )
        assert unbacked_adapter.submitted_signal is None


def test_submit_order_with_risk_resolves_fallback_sl_tp_into_metadata():
    """A signal without explicit SL gets the risk engine's resolved stop; the
    TP must be explicit (C1: the old 1:1-derived TP is removed)."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"take_profit": 112.0},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))

    assert adapter.submitted_signal is not None
    metadata = adapter.submitted_signal.metadata
    stop_loss = float(metadata["stop_loss"])
    take_profit = float(metadata["take_profit"])
    assert stop_loss == pytest.approx(98.0)  # fallback: entry * (1 - 0.02)
    assert take_profit == pytest.approx(112.0)
    assert adapter.submitted_qty == pytest.approx(float(metadata["qty"]))


# ── #78: Alpaca stream subscribe-once + no reconnect spin ─────────────────────

class _FakeAlpacaStream:
    """Minimal TradingStream double: records subscriptions, scripted run."""

    instances: list["_FakeAlpacaStream"] = []

    def __init__(self, behaviours: list[str | BaseException | None]):
        self.behaviours = list(behaviours)
        self.subscribe_calls = 0
        self.stop_calls = 0
        self.handler = None
        _FakeAlpacaStream.instances.append(self)

    def subscribe_trade_updates(self, handler):
        self.subscribe_calls += 1
        self.handler = handler

    def run(self):
        if not self.behaviours:
            raise asyncio.CancelledError()
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    def stop_ws(self):
        self.stop_calls += 1


def _patch_alpaca_listen_env(monkeypatch, module):
    """Make listen_executions deterministic: inline to_thread, recording sleep."""
    real_sleep = module.asyncio.sleep
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(float(seconds))
        await real_sleep(0)

    async def _inline_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    return sleeps


def test_alpaca_listen_subscribes_once_across_reconnects(monkeypatch):
    """#78: reconnect must not duplicate subscribe_trade_updates callbacks."""
    import alpaca.trading.stream as alpaca_stream_module

    from live_runner import alpaca_adapter as module

    _FakeAlpacaStream.instances.clear()
    first = _FakeAlpacaStream([RuntimeError("boom")])
    second = _FakeAlpacaStream([asyncio.CancelledError()])
    monkeypatch.setattr(alpaca_stream_module, "TradingStream", lambda *a, **k: second)

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_stream = first
    sleeps = _patch_alpaca_listen_env(monkeypatch, module)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert first.subscribe_calls == 1
    assert first.stop_calls == 1
    assert second.subscribe_calls == 1
    # First retry backs off 2s; the CancelledError propagates without sleep.
    assert sleeps == [2]


def test_alpaca_listen_clean_return_backs_off_instead_of_spinning(monkeypatch):
    """#78: a clean run() return must sleep before re-subscribing."""
    from live_runner import alpaca_adapter as module

    _FakeAlpacaStream.instances.clear()
    stream = _FakeAlpacaStream([None, asyncio.CancelledError()])

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_stream = stream
    sleeps = _patch_alpaca_listen_env(monkeypatch, module)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert stream.subscribe_calls == 1
    assert sleeps and sleeps[0] == 2


# ── #80: per-symbol cTrader volume + SL/TP scales ─────────────────────────────

def _ctrader_signal(symbol: str, metadata: dict) -> "LiveSignal":
    return LiveSignal(
        deployment_id="dep-1",
        symbol=symbol,
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def test_ctrader_fx_and_jpy_volume_scales_match_lots() -> None:
    """FX/JPY: 1 lot = 100_000 broker units = 100 qty units (0.01 lots)."""
    from live_runner import ctrader_adapter as module

    assert module._fallback_symbol_scale("EURUSD").volume_per_qty == 1000.0
    assert module._fallback_symbol_scale("USDJPY").volume_per_qty == 1000.0
    assert module._fallback_symbol_scale("EURUSD").price_digits == 5
    assert module._fallback_symbol_scale("USDJPY").price_digits == 3


def test_ctrader_xau_volume_scale_is_ounces() -> None:
    """XAU: 1 lot = 100 oz; one qty unit (0.01 lots) = 1 broker unit."""
    from live_runner import ctrader_adapter as module

    scale = module._fallback_symbol_scale("XAUUSD")
    assert scale.volume_per_qty == 1.0
    assert scale.price_digits == 2


def test_ctrader_index_fallback_class() -> None:
    from live_runner import ctrader_adapter as module

    scale = module._fallback_symbol_scale("US30")
    assert scale.price_digits == 2
    assert scale.source == "fallback"


def test_ctrader_unknown_symbol_fails_closed() -> None:
    """No broker props + no fallback class = no trade, never a guessed size."""
    _module, adapter = _make_ctrader_adapter()
    with pytest.raises(ValueError, match="[Uu]nknown cTrader symbol"):
        adapter._resolve_order_volume(_ctrader_signal("NOTASYMBOLXYZ", {"qty": 5.0}))


def test_ctrader_broker_props_cache_symbol_without_static_fallback() -> None:
    protobuf = pytest.importorskip("ctrader_open_api.messages.OpenApiMessages_pb2")
    model = pytest.importorskip("ctrader_open_api.messages.OpenApiModelMessages_pb2")
    response = protobuf.ProtoOASymbolByIdRes()
    symbol = model.ProtoOASymbol()
    symbol.symbolId = 42
    symbol.digits = 2
    symbol.lotSize = 100
    symbol.minVolume = 1
    response.symbol.append(symbol)

    _module, adapter = _make_ctrader_adapter()

    async def _send_request(_request, *, timeout_seconds=15):
        return response

    adapter._send_request_with_refresh_retry = _send_request
    asyncio.run(adapter._load_symbol_props_into_cache({"WTIUSD": 42}))

    assert adapter._scale_for("WTIUSD").source == "broker"
    assert adapter._resolve_order_volume(_ctrader_signal("WTIUSD", {"qty": 5.0})) == 5


def test_ctrader_broker_props_win_over_fallback() -> None:
    """Broker digits/lotSize are the source of truth when cached."""
    from live_runner import ctrader_adapter as module

    _module, adapter = _make_ctrader_adapter()
    adapter._symbol_props["XAUUSD"] = module.SymbolScale(
        volume_per_qty=2.0,
        price_digits=3,
        min_volume=2,
        lot_size=200.0,
        source="broker",
    )
    assert adapter._scale_for("XAUUSD").source == "broker"
    assert adapter._resolve_order_volume(_ctrader_signal("XAUUSD", {"qty": 5.0})) == 10


def test_ctrader_jpy_relative_sl_tp_uses_fixed_protocol_divisor() -> None:
    """cTrader relative distances always use 1/100000 price units."""
    _module, adapter = _make_ctrader_request_builder_adapter()
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="USDJPY",
        action="buy",
        theoretical_price=150.00,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, "stop_loss": 149.95, "take_profit": 150.10},
    )
    adapter._symbol_name_to_id = {"USDJPY": 9}
    request = adapter._build_new_order_request(signal, symbol_id=9)
    assert request.relativeStopLoss == 5000
    assert request.relativeTakeProfit == 10000


def test_ctrader_xau_order_volume_uses_ounce_scale() -> None:
    """XAU qty 5.0 (0.05 lots = 5 oz) resolves to volume 5, not 5000."""
    _module, adapter = _make_ctrader_adapter()
    assert adapter._resolve_order_volume(_ctrader_signal("XAUUSD", {"qty": 5.0})) == 5


@pytest.mark.parametrize("symbol", ["WTIUSD", "XPTUSD", "XPDUSD", "BRENTUSD", "ABCDEF"])
def test_ctrader_non_fx_six_letter_symbols_fail_closed(symbol: str) -> None:
    from live_runner import ctrader_adapter as module

    assert module._fallback_scale_class(symbol) is None
    with pytest.raises(ValueError, match="[Uu]nknown cTrader symbol"):
        module._fallback_symbol_scale(symbol)


def test_ctrader_crypto_symbols_fail_closed_not_fx() -> None:
    """Six-letter crypto pairs must not fall into the FX branch."""
    from live_runner import ctrader_adapter as module

    assert module._fallback_scale_class("BTCUSD") is None
    assert module._fallback_scale_class("ETHUSD") is None
    with pytest.raises(ValueError, match="[Uu]nknown cTrader symbol"):
        module._fallback_symbol_scale("BTCUSD")


def test_ctrader_disconnect_clears_symbol_session_state() -> None:
    """Reconnects re-fetch broker props; order-symbol map stays bounded."""
    _module, adapter = _make_ctrader_adapter()
    adapter._symbol_props["EURUSD"] = adapter._scale_for("EURUSD")
    adapter._order_id_to_symbol[123] = "EURUSD"
    asyncio.run(adapter.disconnect())
    assert adapter._symbol_props == {}
    assert adapter._order_id_to_symbol == {}


def test_exit_only_metadata_flows_through_adapter_volume_gate() -> None:
    """Pre-mortem PM1: daemon exit-only metadata must satisfy the adapter's
    volume gate — producer and consumer share one constant, not two
    spellings."""
    import asyncio

    from live_runner.base_adapter import EXIT_ONLY_RISK_SIZING_MODE
    from live_runner.daemon import _metadata_for_exit_only_order

    assert EXIT_ONLY_RISK_SIZING_MODE == "exit_only"

    class _Adapter:
        async def get_positions(self):
            return [{"symbol": "EURUSD", "qty": 2.0}]

    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )
    metadata = asyncio.run(_metadata_for_exit_only_order(signal, _Adapter(), {}))
    assert metadata["risk_sizing_mode"] == EXIT_ONLY_RISK_SIZING_MODE
    assert metadata["qty"] == pytest.approx(2.0)
    assert metadata["explicit_qty"] == pytest.approx(2.0)

    _module, adapter = _make_ctrader_adapter()
    gated = signal.model_copy(update={"metadata": metadata})
    assert adapter._resolve_order_volume(gated) == 2000


def test_unbounded_tp_policy_passes_daemon_guard_with_warning(caplog) -> None:
    """C1: explicit unbounded flag (no invented bracket) passes the daemon."""
    import logging

    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "stop_loss": 95.0,
            "take_profit_policy": "unbounded_exit_on_opposite_signal",
        },
    )
    with caplog.at_level(logging.WARNING, logger="live_runner.daemon"):
        asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))
    assert adapter.submitted_signal is not None
    assert "take_profit" not in adapter.submitted_signal.metadata
    assert any("unbounded" in record.message for record in caplog.records)


def test_unbounded_tp_policy_builds_stop_only_requests() -> None:
    """C1: stop-only orders carry a stop leg and no TP leg on both brokers."""
    from live_runner import alpaca_adapter as alpaca_module

    alpaca = alpaca_module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "stop_loss": 95.0,
            "take_profit_policy": "unbounded_exit_on_opposite_signal",
        },
    )
    captured: dict[str, Any] = {}

    def _market_request_cls(**kwargs):
        captured.update(kwargs)
        return ("market", kwargs)

    alpaca._build_order_request(
        signal,
        qty=1.0,
        order_side=SimpleNamespace(BUY="buy", SELL="sell"),
        time_in_force=SimpleNamespace(DAY="day"),
        market_request_cls=_market_request_cls,
        client_order_id="cid-unbounded",
    )
    assert captured["take_profit"] is None
    assert captured["stop_loss"] is not None
    assert str(captured["order_class"]).endswith("OTO")

    _module, cadapter = _make_ctrader_request_builder_adapter()
    csignal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "qty": 1.0,
            "stop_loss": 1.05,
            "take_profit_policy": "unbounded_exit_on_opposite_signal",
        },
    )
    request = cadapter._build_new_order_request(csignal, symbol_id=7)
    assert request.relativeStopLoss == 5000
    assert request.relativeTakeProfit == 0


def test_is_unbounded_tp_rejects_non_dict_metadata() -> None:
    """C1: the unbounded-TP guard never blows up on malformed metadata."""
    from live_runner.base_adapter import UNBOUNDED_TP_POLICY, is_unbounded_tp

    assert is_unbounded_tp(None) is False
    assert is_unbounded_tp("unbounded_exit_on_opposite_signal") is False
    assert is_unbounded_tp({"take_profit_policy": UNBOUNDED_TP_POLICY}) is True
    assert is_unbounded_tp({"take_profit_policy": "bounded"}) is False


def test_non_duplicate_integrity_error_propagates(isolated_db) -> None:
    """C1/CodeRabbit: only a real dedupe_key conflict counts as duplicate."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    import shared.database as database

    signal = _persist_signal(database)
    execution = LiveExecution(
        signal_id=signal.id,
        actual_price=100.5,
        filled_qty=10.0,
        commission=0.0,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-1",
        dedupe_key="alpaca:exec:conflict-check",
    )
    record = database.LiveExecutionModel.from_live_execution(execution)

    class _ExplodingSession:
        def exec(self, *args, **kwargs):
            class _Empty:
                def first(self):
                    return None

            return _Empty()

        def add(self, *args, **kwargs):
            return None

        def commit(self):
            raise IntegrityError("INSERT", {}, Exception("CHECK failed"))

        def rollback(self):
            return None

    with pytest.raises(IntegrityError):
        database._insert_live_execution_locked(_ExplodingSession(), record)


def test_processor_signal_price_attribution_is_explicit() -> None:
    """C1: processors always stamp signal_price + visible slippage."""
    import pandas as pd

    from signal_bots.ramjet.processor import RamJetSignalProcessor
    from signal_bots.rocketfuel.processor import RocketFuelSignalProcessor

    frame = pd.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1000.0] * 60,
        }
    )
    raw = {"symbol": "AAPL", "action": "buy", "signal_price": 99.5}
    ramjet = RamJetSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}}).process_signal(
        raw, frame
    )
    assert ramjet.metadata["signal_price"] == pytest.approx(99.5)
    assert ramjet.metadata["slippage_vs_signal"] == pytest.approx(0.5)

    rocket = RocketFuelSignalProcessor(
        {"rocket_fuel": {"candle_timeframe": "5Min", "variant": "longsonly"}}
    ).process_signal(raw, frame)
    assert rocket.metadata["signal_price"] == pytest.approx(99.5)
    assert rocket.metadata["slippage_vs_signal"] == pytest.approx(0.5)
    assert rocket.take_profit is None
    assert (
        rocket.metadata["take_profit_policy"] == "unbounded_exit_on_opposite_signal"
    )


def test_save_live_signal_dedups_non_discord_redelivery_by_idempotency_key(isolated_db):
    """C1: internal/replay redeliveries with a stable key return canonical."""
    database = isolated_db

    def _signal():
        return LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            metadata={"idempotency_key": "replay-abc-123"},
        )

    first_id, first_created = database.save_live_signal(_signal())
    second_id, second_created = database.save_live_signal(_signal())

    assert first_created is True
    assert second_created is False
    assert second_id == first_id


def test_save_live_signal_distinct_keys_persist_separately(isolated_db):
    """Different idempotency keys must not collapse distinct signals."""
    database = isolated_db

    def _signal(key: str):
        return LiveSignal(
            deployment_id="dep-1",
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            metadata={"idempotency_key": key},
        )

    assert database.save_live_signal(_signal("k-1"))[1] is True
    assert database.save_live_signal(_signal("k-2"))[1] is True


def test_save_live_execution_derives_stable_key_without_dedupe_key(isolated_db):
    """C1: key-less fills get a deterministic key — redelivery skips."""
    database = isolated_db
    parent = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        metadata={},
    )
    database.save_live_signal(parent)
    signal_id = parent.id
    execution = LiveExecution(
        signal_id=signal_id,
        actual_price=50.0,
        filled_qty=10.0,
        commission=0.0,
        timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        broker_order_id="bo-1",
        dedupe_key=None,
    )
    assert database.save_live_execution(execution) is True
    assert database.save_live_execution(execution) is False


def test_ctrader_adapter_treats_incoming_qty_as_binding(monkeypatch) -> None:
    """B2/C1: size exactly once upstream — submit trades the passed qty,
    never a metadata self-declaration."""
    module, adapter = _make_ctrader_adapter()
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "qty": 999_999.0,
            "stop_loss": 1.05,
            "take_profit": 1.2,
        },
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: (str(signal.id), True))
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "set_live_signal_broker_order_id", lambda _sid, _oid: True
    )
    volumes: list[int] = []

    class _Req:
        def __init__(self) -> None:
            self.volume = 0

    adapter._ProtoOANewOrderReq = _Req
    adapter._ProtoOAOrderType = SimpleNamespace(MARKET=1)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOATimeInForce = SimpleNamespace(IMMEDIATE_OR_CANCEL=1)

    async def _send(request, **_kwargs):
        volumes.append(request.volume)
        return SimpleNamespace(orderId=777)

    adapter._submit_new_order_request = _send
    adapter._extract_order_id_from_payload = lambda _payload: 777

    assert asyncio.run(adapter.submit_order(signal, qty=2.0)) == "777"
    assert volumes == [2000]


def test_recover_failed_services_arms_reconnect_floor(monkeypatch) -> None:
    """B2: resuscitation is bounded — a flipped service backs off first."""
    from live_runner import daemon as module
    from shared.models import Service, ServiceStatus, ServiceType

    service = Service(
        name="failed-bot",
        type=ServiceType.DISCORD_BOT,
        status=ServiceStatus.FAILED,
        config={},
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(module, "list_services", lambda: [service])
    monkeypatch.setattr(module, "update_service_status", lambda *_args: None)

    module._recover_failed_services()

    assert str(service.id) in module._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE


# ─── Discord deployment config sanity (silent-failure guard) ─────────────────


def _discord_deployment(channel_id: object) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="discord::rocketfuel",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_config={"service_name": "ROCKETFUEL", "channel_id": channel_id},
    )


@pytest.mark.parametrize(
    "channel_id",
    ["987654321", "123456789", "0", "1", "111111111111111111", ""],
)
def test_discord_deployment_rejects_placeholder_channel_id(channel_id):
    from live_runner.stockkit_routing import validate_stockkit_deployment_route

    with pytest.raises(ValueError, match="placeholder|empty channel_id"):
        validate_stockkit_deployment_route(_discord_deployment(channel_id))


def test_discord_deployment_rejects_non_numeric_channel_id():
    from live_runner.stockkit_routing import validate_stockkit_deployment_route

    with pytest.raises(ValueError, match="not numeric"):
        validate_stockkit_deployment_route(_discord_deployment("not-a-number"))


def test_discord_deployment_accepts_plausible_snowflake_channel_id():
    from live_runner.stockkit_routing import validate_discord_deployment_signal_config

    # The real RocketFuel channel shape must pass.
    # no-raise: test_discord_deployment_rejects_non_numeric_channel_id
    validate_discord_deployment_signal_config(
        _discord_deployment("1469353015738630206")
    )


def test_non_discord_deployment_ignores_channel_config():
    # no-raise: test_discord_deployment_rejects_non_numeric_channel_id
    from live_runner.stockkit_routing import validate_discord_deployment_signal_config

    internal = LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="internal",
        signal_config={"channel_id": "987654321"},
    )
    validate_discord_deployment_signal_config(internal)


# ─── StockKit route resolution: broker identity, not the local account id ────


def test_expected_account_number_prefers_configured_label_over_stale_table():
    """Paper accounts get re-created, which silently invalidates hardcoded
    account numbers. Resolution must follow the configured label."""
    from live_runner.stockkit_routing import expected_account_number_for_route

    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-NEW-NITRO",
        "ALPACA_ACCOUNT_NAME_ACC2": "RocketFuel",
        "ALPACA_ACCOUNT_NUMBER_ACC2": "PA-NEW-ROCKET",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-NEW-NITRO"
    assert expected_account_number_for_route("rocketfuel", env) == "PA-NEW-ROCKET"
    # Unlabelled route falls back to the static table.
    assert expected_account_number_for_route("ramjet", env) == "PA3T0MQAGQ51"


def test_expected_account_number_honours_explicit_override():
    from live_runner.stockkit_routing import expected_account_number_for_route

    env = {
        "STOCKKIT_ACCOUNT_NUMBER_NITRO": "PA-EXPLICIT",
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-LABELLED",
    }
    assert expected_account_number_for_route("nitro", env) == "PA-EXPLICIT"


def test_route_validation_compares_broker_identity_not_local_account_id():
    """Regression: the check used to compare the local account UUID against an
    Alpaca account number, two different identifier spaces, so every StockKit
    deployment was rejected including correctly routed ones."""
    from live_runner.stockkit_routing import validate_stockkit_deployment_route

    env = {
        "ALPACA_ACCOUNT_NAME_ACC1": "Nitro",
        "ALPACA_ACCOUNT_NUMBER_ACC1": "PA-NITRO-1",
    }
    deployment = LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="discord::nitro",
        account_id="c419ca33-a759-4e6b-8bb1-299d13da27c3",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_config={"service_name": "nitro", "channel_id": "1469353015738630206"},
    )

    # Correct broker identity passes even though the local id is unrelated.
    validate_stockkit_deployment_route(
        deployment, account_broker_login="PA-NITRO-1", accounts_env_values=env
    )

    # A different broker account is rejected.
    with pytest.raises(ValueError, match="must route to Alpaca account PA-NITRO-1"):
        validate_stockkit_deployment_route(
            deployment, account_broker_login="PA-SOMEONE-ELSE", accounts_env_values=env
        )

    # An unknown account (no reported number) is rejected rather than waved through.
    with pytest.raises(ValueError, match="no reported number"):
        validate_stockkit_deployment_route(
            deployment, account_broker_login="", accounts_env_values=env
        )


def test_route_validation_fails_closed_when_no_account_number_configured(monkeypatch):
    """With neither config nor a fallback entry, the check must refuse rather
    than silently allow an unroutable StockKit deployment."""
    import live_runner.stockkit_routing as routing

    monkeypatch.setattr(routing, "STOCKKIT_ACCOUNT_ROUTES", {})

    deployment = LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="discord::nitro",
        account_id="c419ca33-a759-4e6b-8bb1-299d13da27c3",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_config={"service_name": "nitro", "channel_id": "1469353015738630206"},
    )
    with pytest.raises(ValueError, match="No Alpaca account number is configured"):
        routing.validate_stockkit_deployment_route(
            deployment,
            account_broker_login="PA-NITRO-1",
            accounts_env_values={},
        )


def test_route_validation_uses_static_fallback_when_env_is_empty():
    """Without a labelled config block the static table is still consulted, so
    the result is a mismatch refusal rather than an accidental pass."""
    from live_runner.stockkit_routing import validate_stockkit_deployment_route

    deployment = LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="discord::nitro",
        account_id="c419ca33-a759-4e6b-8bb1-299d13da27c3",
        symbols=["EURUSD"],
        timeframe="1h",
        status="PAUSED",
        signal_source="discord",
        signal_config={"service_name": "nitro", "channel_id": "1469353015738630206"},
    )
    with pytest.raises(ValueError, match="must route to Alpaca account"):
        validate_stockkit_deployment_route(
            deployment, account_broker_login="PA-SOMETHING-ELSE", accounts_env_values={}
        )


# ─── market data lookback must survive closed-market time ───────────────────


def test_market_data_start_scales_past_weekends_for_intraday_bars():
    """Weekends and holidays are closed market time, not missing data.

    A flat 3x calendar multiplier under-requests intraday history: 500 hourly
    bars over 62 calendar days yields only ~434 trading hours, so the adapter
    silently shipped a shorter series than requested.
    """
    from datetime import datetime, timezone

    from live_runner.alpaca_adapter import _alpaca_market_data_start

    start = _alpaca_market_data_start("1h", 500)
    span_hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    trading_hours = span_hours * (5 / 7)  # weekends closed
    assert trading_hours >= 500, (
        f"lookback must cover {500} trading hours, only ~{trading_hours:.0f} available"
    )


def test_market_data_start_scales_past_holidays_for_daily_bars():
    from datetime import datetime, timezone

    from live_runner.alpaca_adapter import _alpaca_market_data_start

    start = _alpaca_market_data_start("1d", 300)
    span_days = (datetime.now(timezone.utc) - start).days
    trading_days = span_days * (5 / 7)
    assert trading_days >= 300, f"only ~{trading_days:.0f} trading days covered"


def test_market_data_start_honours_a_widening_multiplier():
    from live_runner.alpaca_adapter import _alpaca_market_data_start

    narrow = _alpaca_market_data_start("1h", 200, slack_multiplier=1.0)
    wide = _alpaca_market_data_start("1h", 200, slack_multiplier=4.0)
    assert wide < narrow, "a larger multiplier must reach further back"


def test_short_market_data_history_is_refused_not_truncated():
    """A short history silently shifts every lookback-dependent indicator."""
    import pandas as pd

    from live_runner import alpaca_adapter as mod

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC"),
            "open": [1.0] * 5, "high": [1.0] * 5, "low": [1.0] * 5, "close": [1.0] * 5,
        }
    ).set_index("timestamp")

    class _Client:
        def __init__(self):
            self.starts = []

        def get_stock_bars(self, request):
            self.starts.append(request.start)
            return type("R", (), {"df": frame})()

    adapter = mod.AlpacaLiveAdapter.__new__(mod.AlpacaLiveAdapter)
    adapter._connected = True
    adapter._historical_data_client = _Client()
    adapter.credentials = None

    with pytest.raises(ValueError, match="refusing to compute indicators on a shorter history"):
        asyncio.run(adapter.get_market_data("AAPL", timeframe="1h", limit=100))
    # It must have tried to widen before giving up.
    assert len(adapter._historical_data_client.starts) == 2


# ─── Alpaca stop controls must not count failures as success ────────────────


def test_cancel_open_orders_raises_when_the_broker_reports_a_failure():
    """A rejected cancel must not be counted as cancelled.

    The daemon marks a STOPPED deployment enforced from this count, so counting
    a failure would report the deployment as stopped while its order is live.
    """
    import asyncio

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Resp:
        cancelled_order_ids = ["o1", "o2"]

    class _Order:
        def __init__(self, oid, status):
            self.id = oid
            self.status = status

    class _Client:
        def cancel_orders(self):
            return type(
                "R",
                (),
                {"cancelled_orders": [_Order("o1", 200), _Order("o2", 422)]},
            )()

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()

    with pytest.raises(RuntimeError, match="did not succeed"):
        asyncio.run(adapter.cancel_open_orders())


def test_close_all_positions_raises_when_the_broker_reports_a_failure():
    import asyncio

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Order:
        def __init__(self, oid, status):
            self.id = oid
            self.status = status

    class _Client:
        def close_all_positions(self):
            return type("R", (), {"orders": [_Order("p1", 500)]})()

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()

    with pytest.raises(RuntimeError, match="did not succeed"):
        asyncio.run(adapter.close_all_positions())


def test_successful_operations_still_return_their_count():
    import asyncio

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Order:
        def __init__(self, oid, status):
            self.id = oid
            self.status = status

    class _Client:
        def cancel_orders(self):
            return type("R", (), {"cancelled_orders": [_Order("o1", 200)]})()

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()
    assert asyncio.run(adapter.cancel_open_orders()) == 1


def test_legacy_response_without_a_status_is_not_treated_as_a_failure():
    """The raw-dict response shape carries no status; it must still pass."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    assert AlpacaLiveAdapter._response_count({"cancelled_order_ids": ["a", "b"]}) == 2
    AlpacaLiveAdapter._raise_on_failed_operations(
        {"cancelled_order_ids": ["a", "b"]}, operation="cancel_orders"
    )


def _alpaca_cancel_adapter_with_status(status: int):
    """A cancel adapter whose broker answers with one order in `status`."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Order:
        def __init__(self, oid, status):
            self.id = oid
            self.status = status

    class _Client:
        def cancel_orders(self):
            return type("R", (), {"cancelled_orders": [_Order("o1", status)]})()

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()
    return adapter


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_alpaca_two_hundred_status_is_counted_as_a_successful_cancel(status):
    """The whole 2xx band is success: that is the band the broker documents."""
    import asyncio

    adapter = _alpaca_cancel_adapter_with_status(status)

    assert asyncio.run(adapter.cancel_open_orders()) == 1


@pytest.mark.parametrize("status", [300, 301, 302, 399, 400, 403, 404, 422, 500])
def test_alpaca_status_outside_two_hundred_is_reported_as_a_failed_cancel(status):
    """Everything outside 2xx means "not done as asked", 3xx included.

    A 3xx is not a redirect to follow here: the order was not cancelled. The
    daemon marks a STOPPED deployment enforced from this count, so accepting a
    3xx would report the deployment as flattened while its orders and
    positions are still live.
    """
    import asyncio

    adapter = _alpaca_cancel_adapter_with_status(status)

    with pytest.raises(RuntimeError, match=r"o1=" + str(status)):
        asyncio.run(adapter.cancel_open_orders())


def test_alpaca_close_all_positions_also_refuses_a_redirect_status():
    """The same 2xx-only rule guards the flatten count, not just cancels."""
    import asyncio

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Position:
        def __init__(self, pid, status):
            self.id = pid
            self.status = status

    class _Client:
        def close_all_positions(self):
            return type("R", (), {"orders": [_Position("p1", 302)]})()

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()

    with pytest.raises(RuntimeError, match="did not succeed"):
        asyncio.run(adapter.close_all_positions())


def _alpaca_bulk_adapter_with_result(result, method: str = "cancel_orders"):
    """A bulk-operation adapter whose broker answers with `result`, verbatim."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    class _Client:
        pass

    setattr(_Client, method, staticmethod(lambda: result))
    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()
    return adapter


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param(None, id="no-response"),
        pytest.param(object(), id="unknown-object"),
        pytest.param({"unexpected_key": ["a"]}, id="dict-without-known-key"),
        pytest.param(type("R", (), {"unexpected_key": ["a"]})(), id="model-without-known-key"),
    ],
)
def test_alpaca_unreadable_bulk_response_raises_instead_of_reporting_zero(unreadable):
    """"I could not parse the response" is INDETERMINATE, not a broker zero.

    Collapsing the two made `cancel_open_orders()` return 0 for a response we
    could not read. The daemon's ledger check is `is not None`, so 0 satisfied
    it and recorded the STOPPED deployment as enforced while its orders and
    positions were still live -- exactly what the enforcement exists to
    prevent. It must raise instead.
    """
    import asyncio

    adapter = _alpaca_bulk_adapter_with_result(unreadable)

    with pytest.raises(RuntimeError, match="unrecognised response shape"):
        asyncio.run(adapter.cancel_open_orders())

    # The count helper must refuse too, so no caller can reintroduce the zero.
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    with pytest.raises(RuntimeError, match="unrecognised response shape"):
        AlpacaLiveAdapter._response_count(unreadable)


def test_alpaca_genuine_zero_is_still_reported_as_zero():
    """The fail-closed fix must not invent a failure: a readable response with
    no operations IS a real zero, and reporting it keeps the daemon's ledger
    honest in the other direction."""
    import asyncio

    adapter = _alpaca_bulk_adapter_with_result({"cancelled_orders": []})

    assert asyncio.run(adapter.cancel_open_orders()) == 0


def test_alpaca_flatten_also_refuses_an_unreadable_response():
    """Same rule for close_all_positions: it is the other half of the flatten."""
    import asyncio

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = _alpaca_bulk_adapter_with_result(object(), method="close_all_positions")

    with pytest.raises(RuntimeError, match="unrecognised response shape"):
        asyncio.run(adapter.close_all_positions())

    with pytest.raises(RuntimeError, match="unrecognised response shape"):
        AlpacaLiveAdapter._raise_on_failed_operations(
            object(), operation="close_all_positions"
        )


@pytest.mark.parametrize("status", ["rejected", "CANCELLED", True, object()])
def test_alpaca_non_numeric_status_is_a_failed_operation_not_a_skipped_one(status):
    """A status we cannot read is a FAILED operation, never a silent success.

    It used to be skipped by a bare `continue`, so a broker that reported the
    item as anything other than a number counted as cancelled. The daemon
    marks a STOPPED deployment enforced from that count.
    """
    import asyncio

    class _Order:
        def __init__(self, oid, status):
            self.id = oid
            self.status = status

    class _Client:
        def cancel_orders(self):
            return type("R", (), {"cancelled_orders": [_Order("o1", status)]})()

    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = _Client()

    with pytest.raises(RuntimeError, match="did not succeed"):
        asyncio.run(adapter.cancel_open_orders())


def test_widened_market_data_request_asks_for_the_newest_bars():
    """The response is capped at `limit`, so ordering decides which bars arrive.

    An ascending request over a widened window returns the oldest bars in it, so
    the widening retry meant to recover a short history would have returned
    staler data than the first attempt.
    """
    import asyncio

    from alpaca.common.enums import Sort

    from live_runner import alpaca_adapter as mod

    seen_sorts = []
    # Recent timestamps: the adapter rejects stale data, which is itself correct.
    recent = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=100, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": recent,
            "open": [1.0] * 100, "high": [1.0] * 100,
            "low": [1.0] * 100, "close": [1.0] * 100,
        }
    ).set_index("timestamp")

    class _Client:
        def get_stock_bars(self, request):
            seen_sorts.append(request.sort)
            return type("R", (), {"df": frame})()

    adapter = mod.AlpacaLiveAdapter.__new__(mod.AlpacaLiveAdapter)
    adapter._connected = True
    adapter._historical_data_client = _Client()

    asyncio.run(adapter.get_market_data("AAPL", timeframe="1h", limit=50))

    assert seen_sorts, "the client must have been called"
    assert all(s is Sort.DESC for s in seen_sorts), (
        f"every request must ask for the newest bars first, got {seen_sorts}"
    )


def test_returned_bars_are_sorted_ascending_regardless_of_request_order():
    """Callers index chronologically, so the descending response is re-sorted."""
    import asyncio

    from live_runner import alpaca_adapter as mod

    recent = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=10, freq="1h", tz="UTC")
    descending = pd.DataFrame(
        {
            "timestamp": recent[::-1],
            "open": [1.0] * 10, "high": [1.0] * 10,
            "low": [1.0] * 10, "close": [1.0] * 10,
        }
    ).set_index("timestamp")

    class _Client:
        def get_stock_bars(self, request):
            return type("R", (), {"df": descending})()

    adapter = mod.AlpacaLiveAdapter.__new__(mod.AlpacaLiveAdapter)
    adapter._connected = True
    adapter._historical_data_client = _Client()

    out = asyncio.run(adapter.get_market_data("AAPL", timeframe="1h", limit=5))
    timestamps = pd.to_datetime(out["timestamp"], utc=True)
    assert timestamps.is_monotonic_increasing, "bars must be returned oldest first"


# ─── The broker identity behind a demo verification ────────────────────────────
#
# `identity_fingerprint` records which broker account a demo verification was
# taken from. Without comparing it back, a credential that is re-pointed at a
# live account keeps the old verification and the platform places a real order on
# a real account. These tests pin the comparison on both gates, and the pin is
# the comparison itself: a mutation that stops comparing is caught here.


def test_repointed_credential_cannot_keep_its_demo_verification(isolated_db):
    """The proven sequence, end to end: verify, re-point, and be refused.

    `ensure_live_account(..., is_demo=None)` is a local configuration change: it
    overwrites the broker identity without talking to the broker. It must not
    leave the previous identity's verification attached to the new identity.
    """
    database = isolated_db
    database.ensure_live_account("acc-1", "alpaca", is_demo=True, broker_login="PA-DEMO-1")
    database.record_live_account_demo_verification(
        "acc-1", is_demo=True, broker_login="PA-DEMO-1"
    )

    deployment = _deployment()
    created = database.create_live_deployment(deployment)
    database.update_live_deployment_status(created.id, "PAUSED")
    assert database.live_account_submission_block_reason("acc-1") is None

    # Re-point at a live-money account, the way a rotated credential would be.
    database.ensure_live_account("acc-1", "alpaca", broker_login="PA-LIVE-9", is_demo=None)

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-1")
        # The demo flag itself is not this gate's business, but the verification
        # behind it must not survive the identity change.
        assert row.demo_verified_at is None, (
            "a re-pointed credential must not keep the old identity's verification"
        )
        assert row.identity_fingerprint is None

    # Refused on the arm path...
    with pytest.raises(ValueError, match="Refusing to activate"):
        database.update_live_deployment_status(created.id, "ACTIVE")

    # ...and on the per-dispatch path, which is the one that reaches a broker.
    assert database.live_account_submission_block_reason("acc-1") is not None

    # Re-verifying against the broker is what makes it tradeable again.
    database.record_live_account_demo_verification(
        "acc-1", is_demo=True, broker_login="PA-LIVE-9"
    )
    assert database.live_account_submission_block_reason("acc-1") is None
    assert database.update_live_deployment_status(created.id, "ACTIVE") is not None


def test_identity_change_outside_the_account_writers_is_still_caught(isolated_db):
    """The fingerprint comparison is the gate; the writers are only a shortcut.

    A stored fingerprint that no longer matches the row's identity must be
    refused even when nothing went through `ensure_live_account` — a direct
    write, a restored backup, or a future writer must not be able to move the
    identity out from under the verification.
    """
    database = isolated_db
    database.ensure_live_account("acc-1", "alpaca", is_demo=True, broker_login="PA-DEMO-1")
    database.record_live_account_demo_verification(
        "acc-1", is_demo=True, broker_login="PA-DEMO-1"
    )
    created = database.create_live_deployment(_deployment())
    assert database.live_account_submission_block_reason("acc-1") is None

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-1")
        row.broker_login = "PA-LIVE-9"
        session.add(row)
        session.commit()

    reason = database.live_account_submission_block_reason("acc-1")
    assert reason is not None, "a changed identity must be refused"
    assert "identity changed" in reason

    database.update_live_deployment_status(created.id, "PAUSED")
    with pytest.raises(ValueError, match="identity changed"):
        database.update_live_deployment_status(created.id, "ACTIVE")


def test_verification_without_a_fingerprint_is_refused(isolated_db):
    """A fresh stamp with no fingerprint is an unverifiable verification.

    Fail closed: if the fingerprint that ties "verified demo" to one broker
    account cannot be read, the account is not demonstrably a demo.
    """
    database = isolated_db
    database.ensure_live_account("acc-1", "alpaca", is_demo=True)
    database.record_live_account_demo_verification("acc-1", is_demo=True)

    with database.get_session() as session:
        row = session.get(database.LiveAccountModel, "acc-1")
        row.identity_fingerprint = None
        session.add(row)
        session.commit()

    reason = database.live_account_submission_block_reason("acc-1")
    assert reason is not None
    assert "no broker identity fingerprint" in reason


def test_unreadable_verification_timestamp_is_refused(isolated_db):
    """A stamp that cannot be parsed is not a fresh stamp.

    SQLite will happily hold text in a DATETIME column, so a stamp written by
    some other tool can come back as a string. That must be reported as "no
    verification" and must never raise on the way out of the DB layer, because
    the callers are safety gates, not validators.
    """
    database = isolated_db
    row = database.LiveAccountModel(
        id="acc-1",
        broker="alpaca",
        name="acc-1",
        is_demo=True,
        demo_verified_at="not-a-timestamp",
        identity_fingerprint=database.live_account_identity_fingerprint(
            "alpaca", ctid=None, broker_login=None
        ),
    )
    assert row.to_live_account().demo_verified_at is None
    reason = database.live_account_verification_block_reason(row)
    assert reason is not None
    assert "unreadable" in reason


def test_dispatch_refuses_once_the_verification_ages_out(isolated_db):
    """The TTL is enforced on the dispatch path, not only on the arm path.

    Arming is a one-off decision; a deployment that trades for longer than the
    TTL must stop submitting even though nothing about it changed.
    """
    database = isolated_db
    _verified_demo_account(database, "acc-1", "alpaca")
    # Armed while the verification is fresh, which is the only way this state
    # can ever be reached.
    database.create_live_deployment(_deployment())
    assert database.live_account_submission_block_reason("acc-1") is None

    with database.get_session() as session:
        account = session.get(database.LiveAccountModel, "acc-1")
        account.demo_verified_at = account.demo_verified_at - timedelta(
            seconds=database.DEMO_VERIFICATION_TTL_SECONDS + 60
        )
        session.add(account)
        session.commit()

    reason = database.live_account_submission_block_reason("acc-1")
    assert reason is not None, "an expired verification must stop submissions"
    assert "older than" in reason


# ─── Mutual exclusion for the live submission claim ────────────────────────────


def test_submission_claim_is_mutually_exclusive_per_deployment(isolated_db):
    """Two dispatchers, one deployment, one order.

    The claim is the only thing that turns "still ACTIVE" into an atomic fact
    taken immediately before the broker call, so a second claimer for the same
    ACTIVE deployment has to be refused. Every caller mints a fresh token, so
    uniqueness has to come from the deployment, not the token.
    """
    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    assert database.claim_live_deployment_submission(deployment_id, "token-1") is True
    assert database.claim_live_deployment_submission(deployment_id, "token-2") is False
    assert len(database.list_live_deployment_submission_claims(deployment_id)) == 1

    # A different deployment is unaffected: the exclusion is per deployment.
    other = database.create_live_deployment(_deployment())
    assert database.claim_live_deployment_submission(str(other.id), "token-3") is True

    # Releasing hands the deployment over cleanly.
    assert database.release_live_deployment_submission("token-1") is True
    assert database.claim_live_deployment_submission(deployment_id, "token-4") is True


def test_a_dead_claimor_does_not_wedge_a_deployment(isolated_db):
    """Mutual exclusion must not become mutual deadlock.

    A claimor that dies mid-submission leaves its lease behind. The lease has a
    TTL, and the next claimer reaps it — otherwise a crashed process would stop
    that deployment from ever trading again.
    """
    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    assert database.claim_live_deployment_submission(
        deployment_id, "crashed-token", ttl_seconds=0.01
    ) is True
    # Still inside the lease: refused, because the other claimor may be live.
    assert database.claim_live_deployment_submission(deployment_id, "too-soon") is False
    time.sleep(0.05)
    # Past the TTL the stale lease is reaped and the deployment trades again.
    assert database.claim_live_deployment_submission(deployment_id, "after-ttl") is True
    remaining = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in remaining] == ["after-ttl"]
    assert database.release_live_deployment_submission("crashed-token") is False


# ─── What an operator is told when a PAUSE cannot reach a clean stop ──────────


def test_pause_hands_the_drain_the_leases_it_inventoried_before_the_write(
    isolated_db, viewer_client, monkeypatch
):
    """The pre-write inventory is what closes the reaped-before-first-poll window.

    `claim_live_deployment_submission` reaps expired leases, and the drain's own
    poll loop cannot see a lease that was already reaped - a lease taken while the
    deployment was ACTIVE, expired in the window between the status commit and the
    drain's first poll, and was deleted by a later claim attempt. The drain would
    then see an empty ledger and report `DRAINED`: a clean stop for a submission
    nobody observed, which is exactly the false "nothing was in flight" this
    mechanism exists to prevent.

    `initial_claims` seeds the ledger from the inventory taken *before* the write.
    Dropping that inventory left all 774 collected tests green, because the
    existing pause test replaces the drain with a wrapper that accepts and
    discards `initial_claims` - so the seed was never observed by anything.

    The wrapper here records what it was handed instead.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-inventoried", ttl_seconds=30.0
    ) is True

    seen: list[object] = []

    async def _capturing_drain(target_deployment_id, *, initial_claims=None, **_kwargs):
        seen.append(initial_claims)
        return viewer_app_module.LiveSubmissionDrainResult(
            outcome=viewer_app_module.LiveSubmissionDrainOutcome.IN_FLIGHT,
            in_flight=1,
            expired_unreleased=0,
        )

    monkeypatch.setattr(
        viewer_app_module, "_drain_live_deployment_submissions", _capturing_drain
    )

    resp = viewer_client.put(
        f"/api/live/deployments/{deployment_id}/status",
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )

    assert resp.status_code == 200
    assert seen, "the drain was never called, so this test proves nothing"
    inventoried = [claim.token for claim in (seen[0] or ())]
    assert inventoried == ["tok-inventoried"], (
        "the drain was not seeded with the leases held before the pause commit, so a "
        f"lease reaped before the first poll would be invisible: {inventoried}"
    )


def test_a_lease_reaped_before_the_first_poll_is_reported_indeterminate(
    isolated_db,
):
    """The seed's purpose, asserted on the drain itself: vanished + expired = doubt.

    The lease was present at the snapshot and gone by the first poll, so the drain
    cannot tell the reaper from a late release. It reports `INDETERMINATE` rather
    than `DRAINED`, because a row disappearing proves nothing - and under-reporting
    doubt is the one thing this verdict must never do.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    # The reaper only deletes leases whose TTL has already lapsed, so the window is
    # an *already-expired* lease: expired while ACTIVE, then deleted by a later claim
    # attempt between the status commit and the drain's first poll.
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-vanished", ttl_seconds=0.01
    ) is True
    time.sleep(0.05)

    seed = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in seed] == ["tok-vanished"]
    assert seed[0].expired is True

    # The reaper runs and removes it before the drain ever polls.
    with database.get_session() as session:
        session.exec(
            database.delete(database.LiveDeploymentSubmissionClaimModel).where(
                database.LiveDeploymentSubmissionClaimModel.token == "tok-vanished"
            )
        )
        session.commit()
    assert database.list_live_deployment_submission_claims(deployment_id) == []

    # With the seed, the ledger still knows an expired lease existed here.
    result = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.2, initial_claims=seed
        )
    )
    assert result.outcome is viewer_app_module.LiveSubmissionDrainOutcome.INDETERMINATE
    assert result.expired_unreleased == 1

    # Without the seed, the drain sees an empty deployment and reports a clean stop.
    unseeded = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.2
        )
    )
    assert unseeded.outcome is viewer_app_module.LiveSubmissionDrainOutcome.DRAINED


def test_pause_with_an_in_flight_submission_is_applied_and_reported_as_applied(
    isolated_db, viewer_client, monkeypatch
):
    """The status change and the leftover submissions are two separate facts.

    The status write commits before the drain runs, and it is that write which
    stops new claims, so the operator's stop has already taken effect. The old
    code answered with 409 *after* the committed write: the operator saw a red
    failure for a stop that had worked, and the obvious response to a red error
    on a stop is to click ACTIVE, which re-arms the bot through a fresh broker
    verification. The response has to be a 200 that says the status is in
    effect *and* that N submissions were still in flight.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-in-flight", ttl_seconds=30.0
    ) is True

    # Keep the real drain logic, but with a budget the test can afford.
    real_drain = viewer_app_module._drain_live_deployment_submissions

    async def _budgeted_drain(target_deployment_id, **_kwargs):
        return await real_drain(target_deployment_id, timeout_seconds=0.2)

    monkeypatch.setattr(
        viewer_app_module, "_drain_live_deployment_submissions", _budgeted_drain
    )

    resp = viewer_client.put(
        f"/api/live/deployments/{deployment_id}/status",
        # Confirmed as the real client is; the refusal path has its own tests.
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )

    assert resp.status_code == 200, "a committed status change must not report failure"
    body = resp.json()
    assert body["status"] == "PAUSED"
    assert body["status_change_applied"] is True
    assert body["submission_drain_outcome"] == "IN_FLIGHT"
    assert body["submission_drain_complete"] is False
    assert body["in_flight_submissions"] == 1
    assert "Verify the broker before resuming" in body["warning"]

    # The stop really is in effect, which is what a failure code would deny.
    assert database.get_live_deployment(deployment_id).status == "PAUSED"
    assert (
        database.claim_live_deployment_submission(deployment_id, "tok-after-pause")
        is False
    )


def test_pause_drain_budget_outlives_the_claim_lease_it_waits_on(isolated_db):
    """The drain budget and the lease TTL are one decision, not two numbers.

    They used to be 10s and 30s, so a submission slower than the drain was
    reported as in flight while the order was perfectly healthy - and the
    endpoint turned that into a 409. The relationship has to be derivable and
    the drain has to be able to outlast the lease it is waiting on.
    """
    import inspect

    import viewer.main as viewer_app_module
    from shared import database as database_module

    database = isolated_db

    # The relationship itself: the budget must exceed the lease, or the drain
    # gives up on a healthy submission.
    assert (
        database_module.LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS
        > database_module.LIVE_SUBMISSION_CLAIM_TTL_SECONDS
    )
    # And both sides default to those constants, so there is no second literal
    # left to drift.
    assert (
        inspect.signature(database_module.claim_live_deployment_submission)
        .parameters["ttl_seconds"]
        .default
        == database_module.LIVE_SUBMISSION_CLAIM_TTL_SECONDS
    )
    assert (
        inspect.signature(viewer_app_module._drain_live_deployment_submissions)
        .parameters["timeout_seconds"]
        .default
        == database_module.LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS
    )

    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    # A budget shorter than the lease it is waiting on - the shape that made a
    # 30s-lease submission fail against a 10s drain - gives up while a perfectly
    # healthy lease is still held, and says so rather than inventing a stop.
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-slow", ttl_seconds=30.0
    ) is True
    too_short = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.2
        )
    )
    assert too_short.outcome is viewer_app_module.LiveSubmissionDrainOutcome.IN_FLIGHT
    assert too_short.in_flight == 1
    assert too_short.complete is False

    # The same submission, released while its lease is still valid, drains
    # cleanly - but only for a budget that outlasts the lease, which is what the
    # derived relationship guarantees.
    assert database.release_live_deployment_submission("tok-slow") is True
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-within-lease", ttl_seconds=5.0
    ) is True
    releaser = ThreadPoolExecutor(max_workers=1)
    try:
        releaser.submit(
            lambda: (
                time.sleep(0.3),
                database.release_live_deployment_submission("tok-within-lease"),
            )
        )
        outlasted = asyncio.run(
            viewer_app_module._drain_live_deployment_submissions(
                deployment_id, timeout_seconds=1.0
            )
        )
    finally:
        releaser.shutdown(wait=True)
    assert outlasted.outcome is viewer_app_module.LiveSubmissionDrainOutcome.DRAINED
    assert outlasted.complete is True

    # A lease released after its TTL is not a clean stop: a late release and the
    # reaper deleting the row look identical from here.
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-overran", ttl_seconds=0.2
    ) is True
    time.sleep(0.3)
    overran = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.6
        )
    )
    assert overran.outcome is viewer_app_module.LiveSubmissionDrainOutcome.INDETERMINATE
    assert overran.complete is False
    assert overran.expired_unreleased == 1


def test_pause_reports_an_indeterminate_outcome_not_a_clean_stop(
    isolated_db, viewer_client
):
    """A lease that expired without being released is never "drained".

    The old count purged expired leases before counting, so a claimor that
    blew past its TTL and never released read as zero in-flight submissions and
    the stop reported 200 OK for an order whose outcome nobody had observed.
    An unknown outcome must be reported as unknown - in the response body the
    operator's UI actually reads.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-dead", ttl_seconds=0.2
    ) is True
    time.sleep(0.3)

    # The observer reports the dead lease rather than deleting it, and marks it
    # expired, so the ambiguity stays visible.
    observed = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in observed] == ["tok-dead"]
    assert observed[0].expired is True

    result = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.2
        )
    )
    assert result.outcome is viewer_app_module.LiveSubmissionDrainOutcome.INDETERMINATE
    assert result.complete is False
    assert result.expired_unreleased == 1

    # Reading the drain state never reaps: the reaper is the claim path alone,
    # which is what stops a dead claimor from wedging the deployment.
    assert [
        claim.token
        for claim in database.list_live_deployment_submission_claims(deployment_id)
    ] == ["tok-dead"]

    # And the endpoint says so, instead of a clean stop.
    resp = viewer_client.put(
        f"/api/live/deployments/{deployment_id}/status",
        # Confirmed as the real client is; the refusal path has its own tests.
        json={
            "status": "PAUSED",
            "confirmation": {"confirmed_deployment_id": deployment_id},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAUSED"
    assert body["status_change_applied"] is True
    assert body["submission_drain_outcome"] == "INDETERMINATE"
    assert body["submission_drain_complete"] is False
    assert body["expired_unreleased_submissions"] == 1
    assert "is unknown" in body["warning"]
    assert database.get_live_deployment(deployment_id).status == "PAUSED"


def test_pause_stays_indeterminate_when_a_dead_lease_is_reaped_before_the_drain(
    isolated_db,
):
    """A reaped lease is indistinguishable from a late release, so it is not clean.

    ``claim_live_deployment_submission`` deletes exactly the same expired rows
    that a late release would remove, so once a dead lease is gone there is no
    evidence left to distinguish "the claimor finished" from "the reaper cleaned
    up after a claimor that may still be at the broker". The ledger therefore
    starts at the moment the stop is requested, before the status write, so the
    reaper cannot erase the evidence from under the drain.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-dead", ttl_seconds=0.2
    ) is True
    time.sleep(0.3)

    # The inventory the endpoint takes before the status write.
    pending = database.list_live_deployment_submission_claims(deployment_id)
    assert [claim.token for claim in pending] == ["tok-dead"]

    # A later claim attempt reaps the dead lease, and the row is gone.
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-next", ttl_seconds=30.0
    ) is True
    assert [
        claim.token
        for claim in database.list_live_deployment_submission_claims(deployment_id)
    ] == ["tok-next"]
    assert database.release_live_deployment_submission("tok-next") is True

    result = asyncio.run(
        viewer_app_module._drain_live_deployment_submissions(
            deployment_id, timeout_seconds=0.2, initial_claims=pending
        )
    )
    assert result.outcome is viewer_app_module.LiveSubmissionDrainOutcome.INDETERMINATE
    assert result.complete is False
    assert result.expired_unreleased == 1


class TestInternalSignalSourceCannotArm:
    """An ``internal`` deployment can never signal: there is no live evaluator.

    The daemon only turns Discord messages into signals, so an armed internal
    deployment is not idle, it is permanently incapable of trading while
    reporting itself healthy - ACTIVE status, a verified demo account, and no
    possible order. That combination is the worst kind of failure for an
    operator to discover, because every visible field says healthy.
    """

    @staticmethod
    def _internal_deployment(**overrides: Any) -> LiveDeploymentConfig:
        payload: dict[str, Any] = {
            "id": uuid4(),
            "strategy_name": "trend_following/trend_following.py",
            "account_id": "acc-demo",
            "symbols": ["EURUSD"],
            "timeframe": "M15",
            "status": "PAUSED",
            "signal_source_type": "discord",
            "signal_source_id": "rocketfuel",
        }
        payload.update(overrides)
        return LiveDeploymentConfig(**payload)

    def _demo_account(self, database, account_id: str = "acc-demo") -> None:
        database.ensure_live_account(account_id, "ctrader", is_demo=True)
        database.record_live_account_demo_verification(
            account_id, is_demo=True, ctid="46736452"
        )

    def test_create_refuses_an_active_internal_deployment(self, isolated_db):
        database = isolated_db
        self._demo_account(database)

        with pytest.raises(ValueError, match="no live evaluator"):
            database.create_live_deployment(
                self._internal_deployment(
                    status="ACTIVE", signal_source_type="internal"
                )
            )

    def test_arming_an_existing_internal_deployment_is_refused(self, isolated_db):
        """The row already exists (created while PAUSED), so the gate has to
        hold on the arming path too - that is the path the UI actually uses."""
        database = isolated_db
        self._demo_account(database)
        created = database.create_live_deployment(
            self._internal_deployment(signal_source_type="internal")
        )

        with pytest.raises(ValueError, match="no live evaluator"):
            database.update_live_deployment_status(created.id, "ACTIVE")

        # And the refusal is durable: it did not arm on the way through.
        assert database.get_live_deployment(str(created.id)).status == "PAUSED"

    def test_discord_source_still_arms_normally(self, isolated_db):
        """The gate must not be a blanket refusal on the activation path."""
        database = isolated_db
        self._demo_account(database)
        created = database.create_live_deployment(
            self._internal_deployment()
        )

        armed = database.update_live_deployment_status(created.id, "ACTIVE")

        assert armed.status == "ACTIVE"

    def test_paused_internal_deployment_can_still_be_created_and_removed(
        self, isolated_db
    ):
        """Only arming is refused. An operator must be able to stage and clean
        up rows without the gate blocking ordinary management."""
        database = isolated_db
        self._demo_account(database)
        created = database.create_live_deployment(
            self._internal_deployment(signal_source_type="internal")
        )
        assert created.status == "PAUSED"


class TestPreflightSymbolCoverage:
    """Arming honesty: a deployment configured with instruments the account
    cannot trade reports nothing until a signal arrives, and then every order
    for that symbol is rejected with no explanation.

    This is a warning, not an error. cTrader resolves symbol ids per account,
    so the deployment is legitimate - it is the *pairing* that is wrong, and
    the operator can only discover that from a rejection at submit time.
    """

    @staticmethod
    def _account():
        from shared.models import LiveAccountConfig

        return LiveAccountConfig(
            id="acc-1", broker="ctrader", name="cTrader 46736434"
        )

    @staticmethod
    def _deployment(*symbols: str) -> LiveDeploymentConfig:
        return LiveDeploymentConfig(
            id=uuid4(),
            strategy_name="trend_following",
            account_id="acc-1",
            symbols=list(symbols),
            timeframe="M15",
            status="PAUSED",
            signal_source_type="discord",
            signal_config={"channel_id": "123"},
        )

    def test_warns_when_the_account_cannot_trade_a_configured_symbol(self):
        from live_runner.preflight import (
            LivePreflightAccountResult,
            _validate_deployments,
        )

        result = LivePreflightAccountResult(
            account_id="acc-1",
            broker="ctrader",
            ok=True,
            tradeable_symbols={"EURUSD", "GBPUSD"},
        )

        errors, warnings = _validate_deployments(
            [self._account()], [self._deployment("EURUSD", "XAUUSD")], [result]
        )

        assert errors == []
        assert len(warnings) == 1
        assert "XAUUSD" in warnings[0]
        # It names the consequence, so the operator knows this is not cosmetic.
        assert "refused at submit" in warnings[0]

    def test_quiet_when_every_symbol_is_tradeable(self):
        from live_runner.preflight import (
            LivePreflightAccountResult,
            _validate_deployments,
        )

        result = LivePreflightAccountResult(
            account_id="acc-1", broker="ctrader", ok=True,
            tradeable_symbols={"EURUSD", "GBPUSD"},
        )

        _errors, warnings = _validate_deployments(
            [self._account()], [self._deployment("EURUSD", "GBPUSD")], [result]
        )

        assert warnings == []

    def test_claims_nothing_when_the_broker_was_not_asked(self):
        """"Not checked" is not "everything is tradeable". Without a broker
        answer the check must stay silent rather than invent a warning - or,
        worse, invent a clean bill of health."""
        from live_runner.preflight import (
            LivePreflightAccountResult,
            _validate_deployments,
        )

        unchecked = LivePreflightAccountResult(
            account_id="acc-1", broker="ctrader", ok=True, tradeable_symbols=None
        )

        _errors, warnings = _validate_deployments(
            [self._account()], [self._deployment("EURUSD", "XAUUSD")], [unchecked]
        )

        assert warnings == []

    def test_symbol_matching_is_case_insensitive(self):
        from live_runner.preflight import (
            LivePreflightAccountResult,
            _validate_deployments,
        )

        result = LivePreflightAccountResult(
            account_id="acc-1", broker="ctrader", ok=True,
            tradeable_symbols={"EURUSD"},
        )

        _errors, warnings = _validate_deployments(
            [self._account()], [self._deployment("eurusd")], [result]
        )

        assert warnings == []

    def test_back_compatible_when_no_account_results_are_supplied(self):
        from live_runner.preflight import _validate_deployments

        errors, warnings = _validate_deployments(
            [self._account()], [self._deployment("EURUSD", "XAUUSD")]
        )

        assert errors == []
        assert warnings == []


def test_deleting_a_deployment_with_an_in_flight_submission_says_so(
    viewer_client, isolated_db, monkeypatch
):
    """DELETE is a live control too, and it was the only one with no drain.

    PAUSE and STOP snapshot the leases, apply the change, drain, and return a
    verdict that distinguishes "nothing was in flight" from "N orders may still
    reach the broker". DELETE did none of that: it removed every claim and the row
    and answered `{"status": "deleted"}`. An operator who deleted an ACTIVE
    deployment with a live lease was told it was deleted while the dispatcher
    still held its order at the broker - and because the reap later removes the
    claim row, no code path could ever classify the outcome. The row's
    disappearance proves nothing, which is exactly what the drain's own docstring
    says.

    The drain has to run *before* the delete, because `delete_live_deployment`
    removes the claim rows as part of it. Draining afterwards would find nothing
    left to account for and report a clean stop for submissions it never saw.

    Deliberately no `status` key here, unlike the status path: the deployment is
    gone and there is no status to report. The verdict fields are the whole point.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "tok-delete-in-flight", ttl_seconds=30.0
    ) is True

    real_drain = viewer_app_module._drain_live_deployment_submissions

    async def _budgeted_drain(target_deployment_id, **_kwargs):
        return await real_drain(target_deployment_id, timeout_seconds=0.2)

    monkeypatch.setattr(
        viewer_app_module, "_drain_live_deployment_submissions", _budgeted_drain
    )

    resp = viewer_client.delete(
        f"/api/live/deployments/{deployment_id}"
        f"?confirm_deployment_id={deployment_id}"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["submission_drain_outcome"] == "IN_FLIGHT", (
        f"a delete with a live lease reported a clean outcome: {body}"
    )
    assert body["submission_drain_complete"] is False
    assert body["in_flight_submissions"] == 1
    assert "may still reach the broker" in body["warning"], (
        f"an in-flight order was deleted without saying so: {body}"
    )
    # And the deployment really is gone, so the warning is not an excuse.
    assert database.get_live_deployment(deployment_id) is None


def test_deleting_a_deployment_with_nothing_in_flight_reports_a_clean_drain(
    viewer_client, isolated_db, monkeypatch
):
    """The other direction: the verdict must not be theatre that always warns.

    A control that always says "verify the broker" teaches operators to ignore
    it. Nothing in flight is the overwhelmingly common case and has to read as
    DRAINED with no warning.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    real_drain = viewer_app_module._drain_live_deployment_submissions

    async def _budgeted_drain(target_deployment_id, **_kwargs):
        return await real_drain(target_deployment_id, timeout_seconds=0.2)

    monkeypatch.setattr(
        viewer_app_module, "_drain_live_deployment_submissions", _budgeted_drain
    )

    resp = viewer_client.delete(
        f"/api/live/deployments/{deployment_id}"
        f"?confirm_deployment_id={deployment_id}"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["submission_drain_outcome"] == "DRAINED", body
    assert body["submission_drain_complete"] is True
    assert body["in_flight_submissions"] == 0
    assert "warning" not in body, (
        f"a clean delete warned anyway, which trains operators to ignore the "
        f"field that matters: {body}"
    )


def test_deleting_a_deployment_closes_the_claim_gate_before_draining(
    viewer_client, isolated_db, monkeypatch
):
    """A drain with the gate still open can report a clean bill of health.

    `claim_live_deployment_submission` only takes a lease while the row reads
    ACTIVE. PAUSE and STOP commit their status write *first* and drain second, so
    no new claim can appear during the drain. DELETE went straight from ACTIVE to
    deleted, leaving the gate open for the whole drain: a dispatcher could claim,
    submit and release inside that window, and the response then read DRAINED,
    complete, zero in flight, with no warning key at all - the exact false
    assurance the drain was added to remove.

    Asserted by having a claim taken *during* the drain, which is only possible
    while the row still reads ACTIVE.
    """
    import viewer.main as viewer_app_module

    database = isolated_db
    _verified_demo_account(database, "acc-1", "ctrader")
    created = database.create_live_deployment(_deployment())
    deployment_id = str(created.id)

    claims_taken_during_drain: list[str] = []
    real_drain = viewer_app_module._drain_live_deployment_submissions

    async def _drain_and_try_to_claim(target_deployment_id, **_kwargs):
        # Exactly what a dispatcher does while the endpoint waits: take a lease
        # and submit. If the row is still ACTIVE this succeeds.
        took = database.claim_live_deployment_submission(
            target_deployment_id, "tok-during-drain", ttl_seconds=30.0
        )
        claims_taken_during_drain.append(str(took))
        return await real_drain(target_deployment_id, timeout_seconds=0.2)

    monkeypatch.setattr(
        viewer_app_module, "_drain_live_deployment_submissions", _drain_and_try_to_claim
    )

    resp = viewer_client.delete(
        f"/api/live/deployments/{deployment_id}"
        f"?confirm_deployment_id={deployment_id}"
    )

    assert resp.status_code == 200, resp.text
    assert claims_taken_during_drain == ["False"], (
        "a new submission claim was taken while the delete was draining. The row "
        "must be made non-ACTIVE before the drain, or an order can be submitted "
        "after the operator was told the deployment was deleted."
    )
    assert database.get_live_deployment(deployment_id) is None


def test_alpaca_reconcile_tells_a_missing_order_from_a_failed_lookup():
    """A broker that cannot answer is not the same as a broker saying "no order".

    `_reconcile_signal_fill` caught every exception from the order lookup and
    returned False, so a 429, a 5xx or a dropped connection read exactly like a
    genuine "no such order". The local filled quantity never advances, `recovered`
    stays put, and `reconcile_executions` returns normally - so the caller's
    `except` in `connect()` never fires and nothing anywhere says a fill is
    unrecorded. A missed fill stays missed, quietly, on every connect.

    The contrast is in the same module: `_recover_existing_signal_order` makes the
    identical call and deliberately re-raises "so a transient failure cannot be
    mistaken for absence", and cTrader's equivalent logs before returning None.

    Both directions are pinned: 404 is genuine absence and stays quiet, anything
    else must be visible. The handler is attached to the logger directly because
    `get_logger` sets `propagate = False`, so `caplog` - which hangs off the root -
    sees nothing at all. Using caplog here would have asserted against an empty
    list and passed for the wrong reason, which is how this file's first attempt
    behaved.
    """
    import asyncio
    import logging

    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(
        api_key="k", secret_key="s", paper=True, account_id="alpaca-acc-1"
    )
    signal_id = str(uuid4())

    class _Boom(Exception):
        def __init__(self, status):
            super().__init__("broker said no")
            self.status_code = status

    def _install(exception):
        adapter._trading_client = SimpleNamespace(
            get_order_by_client_id=lambda _cid: (_ for _ in ()).throw(exception)
        )

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    capture = _Capture(level=logging.WARNING)
    source_logger = logging.getLogger("trading.live-runner")
    source_logger.addHandler(capture)
    try:
        # A genuine 404 is a real answer: no such order, so no fill. Silent.
        _install(_Boom(404))
        assert asyncio.run(adapter._reconcile_signal_fill(signal_id)) is False
        assert not [
            r for r in records if "Could not read order state" in r.getMessage()
        ], "a 404 is genuine absence and must not be reported as a failure"

        # Anything else is the broker failing to answer, and must say so.
        for status in (429, 500, 502):
            records.clear()
            _install(_Boom(status))
            assert asyncio.run(adapter._reconcile_signal_fill(signal_id)) is False
            messages = [r.getMessage() for r in records]
            assert any(
                "Could not read order state" in m for m in messages
            ), f"a {status} during reconciliation was silent: {messages}"
            assert any(
                getattr(r, "data", {}).get("signal_id") == signal_id for r in records
            ), f"the warning did not name the signal it could not reconcile: {messages}"
    finally:
        source_logger.removeHandler(capture)


def test_a_lookup_error_with_no_status_code_is_not_treated_as_absence():
    """An exception carrying no status is still a failed lookup, not a 404.

    The predicate reads the status code structurally, so an error with none - a
    transport failure, or an SDK that moved the field - must fall on the "could not
    answer" side. Defaulting it to "not found" would reintroduce the original bug
    for exactly the case we cannot inspect.
    """
    from live_runner.alpaca_adapter import _is_order_not_found

    assert _is_order_not_found(Exception("connection reset")) is False
    assert _is_order_not_found(ValueError("bad request")) is False
    # And a 404 is recognised whether the SDK hung the code on the exception or on
    # the response object.
    assert _is_order_not_found(SimpleNamespace(status_code=404)) is True
    assert _is_order_not_found(
        SimpleNamespace(response=SimpleNamespace(status_code=404))
    ) is True


class TestSignalIdempotencyKeyAndStalenessBudget:
    """Signal identity, the staleness budget, and int coercion in the daemon.

    These three are the same family of decision - *is this signal allowed through, and is it
    the same signal as last time* - and all were unmeasured.

    **The idempotency key is the closest thing to a primary key for a live signal.** A
    redelivery returns the canonical row instead of placing a second broker order, so the key
    has two properties that pull against each other: the *same* logical signal must produce
    the *same* key, and two *different* signals must not. Both directions are asserted, and
    the components are varied one at a time - symbol case, action case, entry price, levels,
    deployment - because a key that ignores any of them will happily dedupe a distinct signal.

    Two escape hatches matter and are asserted separately:

    - **A signal with no producer timestamp gets no key at all.** Minting one from an empty
      timestamp slot would make distinct timestamp-less signals share a key and *wrongly
      dedupe* - trading one signal twice is worse than losing the dedupe protection.
    - **An existing key or a Discord redelivery is left alone.** A Discord redelivery keys on
      its `discord_message_id`, which is the strongest identity available; replacing it with a
      minted key would trade redelivery protection for nothing.

    **The staleness budget is a fail-closed default in both directions.** An unknown timeframe
    gets 300s rather than infinity - trading a signal you could not age-check is worse than
    refusing one you could. And `' 1D '` must resolve, because a timeframe arriving with
    padding is normal and a 300s budget on a daily strategy refuses every valid signal.

    That normalisation has a consequence worth recording: **lowercasing collapses `1M` (month)
    into `1m` (minute)**, so `1M` gets a 60-second budget. Traded platforms use `1M` for
    monthly, and the budget that comes out is 60 seconds. The failure direction is refusing
    valid signals rather than trading stale ones, which is the safe direction - but it is
    still a wrong answer to a plausible input. Recorded alongside the existing `1w` decision
    rather than merged into it.
    """

    @staticmethod
    def _deployment(**settings):
        from tests.test_live_execution_safety import _deployment

        return _deployment(**settings)

    @staticmethod
    def _signal(**kwargs):
        from risk.models import TradeSignal

        base = dict(
            action="buy",
            symbol="EURUSD",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=104.0,
            timeframe="1h",
        )
        base.update(kwargs)
        return TradeSignal(**base)

    def _key(self, metadata, *, signal=None, deployment=None):
        from live_runner.daemon import _trade_signal_to_live_signal

        signal = signal or self._signal(metadata=metadata)
        converted = _trade_signal_to_live_signal(signal, deployment or self._deployment())
        return converted.metadata.get("idempotency_key")

    # ------------------------------------------------------------ idempotency key

    def test_the_same_signal_mints_the_same_key(self) -> None:
        """The redelivery case, which is the whole reason the key exists.

        One deployment object is shared across both calls, because the key includes
        `deployment.id` and the shared `_deployment()` helper mints a fresh `uuid4` on
        every call. Two deployments are two different signals by definition - which is what
        `test_every_key_component_actually_changes_the_key` asserts separately.
        """
        deployment = self._deployment()
        stamp = {"signal_timestamp": "2026-07-15T10:00:00+00:00"}
        first = self._key(dict(stamp), deployment=deployment)
        second = self._key(dict(stamp), deployment=deployment)
        assert first, "the baseline produced no key, so stability proves nothing"
        assert first == second

    def test_a_timestamp_less_signal_gets_no_key_at_all(self) -> None:
        """No key, rather than a key shared by every timestamp-less signal.

        The failure is a *wrong* dedupe: two distinct signals would resolve to one row and
        only one would trade. Losing the protection is recoverable; trading one signal twice
        is not.
        """
        assert self._key({}) is None
        assert self._key({"signal_timestamp": ""}) is None
        assert self._key({"signal_timestamp": None}) is None

    def test_an_existing_idempotency_key_is_never_overwritten(self) -> None:
        """The caller already decided this signal's identity; that wins."""
        key = self._key(
            {"signal_timestamp": "2026-07-15T10:00:00+00:00",
             "idempotency_key": "chosen-by-the-caller"}
        )
        assert key == "chosen-by-the-caller", (
            f"a minted key replaced the caller's: {key!r}"
        )

    def test_a_discord_redelivery_keeps_its_message_id_identity(self) -> None:
        """A Discord redelivery keys on its message id, the strongest identity available."""
        from live_runner.daemon import _mint_signal_idempotency_key

        metadata = {
            "discord_message_id": "12345",
            "signal_timestamp": "2026-07-15T10:00:00+00:00",
        }
        deployment = self._deployment()
        _mint_signal_idempotency_key(self._signal(metadata=metadata), deployment, metadata)
        assert "idempotency_key" not in metadata, (
            "a Discord redelivery was given a minted key, which trades redelivery "
            "protection for nothing"
        )

    def test_every_key_component_actually_changes_the_key(self) -> None:
        """Each component, varied one at a time.

        A key that ignores any single component will happily dedupe a distinct signal - and
        that is the expensive direction, because the second order is never placed and nothing
        reports a miss. So all six are asserted rather than only the stability case.
        """
        stamp = {"signal_timestamp": "2026-07-15T10:00:00+00:00"}
        base_deployment = self._deployment()
        base = self._key(dict(stamp), deployment=base_deployment)
        assert base, "the baseline produced no key, so nothing below is meaningful"

        # Case is deliberately NOT in this list: case is *normalised*, so it must produce
        # the same key. Asserting it here would contradict
        # `test_case_is_normalised_so_case_alone_is_not_a_new_signal` - and an early version
        # of this test did exactly that, so the two tests fought and the wrong one lost.
        other_deployment = self._deployment()
        object.__setattr__(other_deployment, "id", "a-different-deployment")
        variants = {
            "entry price": (self._signal(entry_price=100.5, metadata=dict(stamp)), base_deployment),
            "stop loss": (self._signal(stop_loss=97.0, metadata=dict(stamp)), base_deployment),
            "take profit": (self._signal(take_profit=105.0, metadata=dict(stamp)), base_deployment),
            "symbol": (self._signal(symbol="GBPUSD", metadata=dict(stamp)), base_deployment),
            "action": (self._signal(action="sell", metadata=dict(stamp)), base_deployment),
            "deployment": (self._signal(metadata=dict(stamp)), other_deployment),
            "timestamp": (self._signal(metadata={"signal_timestamp": "2026-07-15T10:01:00+00:00"}), base_deployment),
        }
        for label, (signal, deployment) in variants.items():
            other = self._key(dict(stamp), signal=signal, deployment=deployment)
            assert other != base, (
                f"a different {label} produced the same idempotency key, so a distinct "
                "signal would be silently deduped and never traded"
            )

    def test_the_keys_case_normalisation_is_redundant_because_the_model_already_did_it(self) -> None:
        """**Two verified equivalent mutants**, recorded with the structural reason.

        `_mint_signal_idempotency_key` normalises the symbol with `.upper()` and the action
        with `.lower()`. Removing either survives, because `TradeSignal` **already normalises
        both in its own validator** - verified by constructing signals directly:

        | input | stored |
        |---|---|
        | `symbol="eurusd"` | `"EURUSD"` |
        | `symbol=" gbpusd "` | `"GBPUSD"` |
        | `action="BUY"` | `"buy"` |
        | `action="Sell"` | `"sell"` |

        So for every input the model accepts, the key's normalisation is a no-op. It is
        defence in depth rather than dead code: it means the key stays correct if the key
        material is ever built from something other than a `TradeSignal`, and it does not
        depend on that model continuing to normalise.

        **The reason this is worth writing down rather than deleting:** the property being
        protected - *case must not split one signal into two keys* - is asserted by the
        neighbouring test against the model, not against this function. So the guarantee
        holds at two layers, and it is the model layer that would fail first. If `TradeSignal`
        ever stops normalising, that test catches it immediately and this note becomes the
        thing to revisit.
        """
        # Asserted rather than described, because the claim is about the *model*, and the
        # model can change without anything else in this file changing.
        cases = [
            ("eurusd", "EURUSD"),
            (" gbpusd ", "GBPUSD"),
            ("xauusd", "XAUUSD"),
        ]
        for supplied, expected in cases:
            signal = TradeSignal(
                action="buy", symbol=supplied, entry_price=1.0, stop_loss=0.9,
                take_profit=1.1, timeframe="1h", metadata={},
            )
            assert signal.symbol == expected, (
                f"TradeSignal stored {signal.symbol!r} for {supplied!r}; this note's whole "
                "argument is that the model normalises, so if it stops, the key function's "
                "normalisation stops being redundant and this needs revisiting"
            )

        for supplied, expected in (("BUY", "buy"), ("Sell", "sell")):
            signal = TradeSignal(
                action=supplied, symbol="EURUSD", entry_price=1.0, stop_loss=0.9,
                take_profit=1.1, timeframe="1h", metadata={},
            )
            assert signal.action == expected, (supplied, signal.action)

    def test_case_is_normalised_so_case_alone_is_not_a_new_signal(self) -> None:
        """The other half of normalisation: case must *not* split one signal in two.

        `EURUSD` and `eurusd` are the same instrument and `buy`/`BUY` the same action, so
        they must agree - otherwise a redelivery whose producer differed in case loses its
        dedupe and places a second order.
        """
        stamp = {"signal_timestamp": "2026-07-15T10:00:00+00:00"}
        deployment = self._deployment()
        lower = self._key(dict(stamp), signal=self._signal(symbol="eurusd", action="buy", metadata=dict(stamp)), deployment=deployment)
        upper = self._key(dict(stamp), signal=self._signal(symbol="EURUSD", action="BUY", metadata=dict(stamp)), deployment=deployment)
        assert lower == upper, (
            "case differences split one logical signal into two keys, so a redelivery "
            "would place a second order"
        )

    # --------------------------------------------------------- staleness budget

    def test_the_budget_table_is_seconds_for_every_timeframe(self) -> None:
        """Stated rather than assumed, because a minutes-for-seconds slip is invisible."""
        from live_runner.daemon import _timeframe_age_budget_seconds

        expected = {
            "1m": 60.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
            "1h": 3600.0, "4h": 14400.0, "1d": 86400.0, "1w": 604800.0,
        }
        for timeframe, seconds in expected.items():
            assert _timeframe_age_budget_seconds(timeframe) == seconds, (
                f"{timeframe} budget is {_timeframe_age_budget_seconds(timeframe)}, "
                f"not {seconds}"
            )

    def test_a_padded_and_uppercase_timeframe_still_resolves(self) -> None:
        """Padding and case are normal in config, and a miss here refuses valid signals."""
        from live_runner.daemon import _timeframe_age_budget_seconds

        for spelling in ("1d", " 1D ", "\t1D\n", "1D"):
            assert _timeframe_age_budget_seconds(spelling) == 86400.0, (
                f"{spelling!r} did not resolve to a daily budget, so a daily strategy's "
                "signals would be refused as stale"
            )

    def test_a_blank_terminal_error_message_needs_no_explicit_guard(self) -> None:
        """**A verified equivalent mutant**, with the reason in one line.

        `_is_terminal_ctrader_runtime_error` guards with `if not message: return False` before
        the marker scan. Removing that guard changes nothing, because `any(marker in message
        for marker in ...)` is `False` for an empty string - a substring of `""` is never
        found.

        Verified directly rather than assumed. The guard is a cheap short-circuit and reads
        clearly, so it stays; the note is here so nobody re-runs the mutation and re-derives
        this. The behaviour that *is* load-bearing is asserted by the marker tests: a
        non-terminal message like `"timeout"` must not match, and a blank one must not either.
        """
        from live_runner.daemon import _is_terminal_ctrader_runtime_error

        assert not any(marker in "" for marker in ("not authorized", "unauthorized")), (
            "a substring search over an empty string started matching, so the guard removal "
            "would no longer be equivalent"
        )
        assert _is_terminal_ctrader_runtime_error(RuntimeError("")) is False
        assert _is_terminal_ctrader_runtime_error(RuntimeError("   ")) is False

    def test_an_unknown_timeframe_fails_closed_rather_than_never_expiring(self) -> None:
        """300 seconds, not infinity.

        An infinite budget would let a signal of unknown age through regardless of how stale
        it is. The 300s default is the conservative choice: it refuses some signals a longer
        timeframe would have allowed, rather than trading information that is no longer valid.
        """
        from live_runner.daemon import _timeframe_age_budget_seconds

        for unknown in ("2h", "3d", "banana", "", None, "  "):
            budget = _timeframe_age_budget_seconds(unknown)
            assert budget == 300.0, f"{unknown!r} got a budget of {budget}, not the 300s default"
            assert budget != float("inf"), "an unknown timeframe must not get an infinite budget"

    def test_a_monthly_spelling_collides_with_a_minute_one_KNOWN_AMBIGUITY(self) -> None:
        """`1M` means month on most platforms and minute here. Recorded, not changed.

        **The cause is not this function.** `_timeframe_age_budget_seconds` lowercases before
        its own lookup, so `1M` and `1m` collide here - but the *root* cause is
        `shared.constants.normalize_timeframe`, which lowercases and then consults
        `TIMEFRAME_ALIASES`. That table holds exactly `m1`, `h1` and `d1`: with no monthly
        entry, `1M` lowercases to `1m` and passes through as already-valid.

        I first attributed this to the daemon's own lowercasing, which is the surface where the
        60 seconds became visible but not where a fix would go. Corrected after tracing it, and
        the authoritative version - with the decision and the options - is recorded in
        `tests/live_runner/test_execution_timing.py` next to the code that causes it.

        Either way a monthly strategy gets a one-minute staleness budget and its signals are
        refused almost immediately. The failure direction is the safe one - refusing valid
        signals rather than trading stale ones - which is why it can sit unnoticed.
        """
        from live_runner.daemon import _timeframe_age_budget_seconds

        assert _timeframe_age_budget_seconds("1M") == 60.0, (
            "1M no longer collides with 1m; if monthly was added deliberately, drop the "
            "KNOWN_AMBIGUITY marker and record the decision"
        )
        assert _timeframe_age_budget_seconds("1m") == 60.0

    # ---------------------------------------------------------- int coercion

    def test_a_bool_is_refused_rather_than_read_as_one_or_zero(self) -> None:
        """`True` is an `int` in Python, so the guard is what stops `True` becoming 1."""
        from live_runner.daemon import _coerce_live_int

        for value in (True, False):
            with pytest.raises(ValueError, match="Invalid"):
                _coerce_live_int(value, field_name="qty")

    def test_a_fractional_value_is_refused_rather_than_truncated(self) -> None:
        """`int(7.5)` is 7 silently; a half-size order is a real mistake, not a rounding."""
        from live_runner.daemon import _coerce_live_int

        for value in (7.5, "7.5"):
            with pytest.raises(ValueError, match="must be an integer"):
                _coerce_live_int(value, field_name="qty")

    def test_a_non_finite_value_is_refused(self) -> None:
        from live_runner.daemon import _coerce_live_int

        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="must be an integer"):
                _coerce_live_int(value, field_name="qty")

    def test_the_minimum_is_enforced_and_is_inclusive(self) -> None:
        """`minimum` is a floor, so the value *at* it is allowed."""
        from live_runner.daemon import _coerce_live_int

        assert _coerce_live_int(5, field_name="qty", minimum=5) == 5
        assert _coerce_live_int(0, field_name="qty", minimum=0) == 0
        for value in (4, -1, 0):
            with pytest.raises(ValueError, match="must be >= 5"):
                _coerce_live_int(value, field_name="qty", minimum=5)

    def test_an_accepted_integer_shape_is_pinned(self) -> None:
        """int, float-with-zero-fraction, and numeric string all reach the same value.

        Config arrives as YAML, JSON or env text, so all three spellings are normal input -
        and the acceptance is asserted so the refusals above are not just always-firing.
        """
        from live_runner.daemon import _coerce_live_int

        for value in (7, 7.0, "7"):
            assert _coerce_live_int(value, field_name="qty") == 7
        for value in (None, "abc", ""):
            with pytest.raises(ValueError, match="Invalid"):
                _coerce_live_int(value, field_name="qty")


class TestExitOnlyAndUnboundedTpMarkers:
    """The two single-definition metadata markers every broker order is routed by.

    `base_adapter.py` holds these as two module constants and two functions, and the docstrings
    say why they are the *single* definition - a pre-mortem finding that producer and consumers
    had been comparing divergent spellings. So the property that matters is that they stay
    **exact-match, dict-only, and case-sensitive**, and that is what these assert.

    **`is_exit_only_metadata` decides whether the entry SL/TP guard is bypassed at all.** Its
    docstring explains that the close side is derived from the broker book rather than from
    `signal.action`, because a SHORT closes with a BUY - so an action test would invert the
    meaning on exactly the orders that matter, refusing every stop-loss and take-profit on a
    short. That reasoning is why this function is marker-only, and it is why the negative cases
    below are the load-bearing half.

    **The `isinstance(metadata, dict)` guard is what keeps a non-dict from crashing.** Without
    it, `metadata.get(...)` raises `AttributeError` on `None`, a string or an int - and this is
    called on the order path, so the failure would be an exception in routing rather than a
    refused order. A non-dict must therefore be *not exit-only*, and that is asserted for
    several types rather than one.

    **The comparison is exact, not case-insensitive and not trimmed.** `"EXIT_ONLY"` and
    `" exit_only "` are both refused. That is deliberate - the producer writes one spelling, and
    a tolerant comparison would let a near-miss pass as the real marker - and it is asserted
    because "should it be tolerant?" is exactly the question a future edit would ask.

    **The unbounded-TP marker is the same shape, and the reason the class covers both** is the
    finding: B9 (a non-dict accepted for the unbounded check) was *already killed* while B1 (the
    identical change to the exit-only check) survived. Two functions written the same way, one
    covered and one not - which is a stronger argument for asserting both than any amount of
    reading either.

    A note on my own mutation writing: my first attempt at B1 used
    `getattr(metadata, "get", lambda _k: "")`, which is behaviourally *identical* to the real
    code for every input - so it was a no-op dressed as a mutation. Rewritten to flip the guard
    to `return True`, which is the dangerous direction and is actually observable.
    """

    @staticmethod
    def _signal(metadata):
        from risk.models import TradeSignal

        return TradeSignal(
            action="buy",
            symbol="EURUSD",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=104.0,
            timeframe="1h",
            metadata=metadata,
        )

    def test_the_exit_only_marker_is_an_exact_match(self) -> None:
        """One spelling, and it is the one the producer writes."""
        from live_runner.base_adapter import (
            EXIT_ONLY_RISK_SIZING_MODE,
            is_exit_only_metadata,
        )

        assert EXIT_ONLY_RISK_SIZING_MODE == "exit_only"
        assert is_exit_only_metadata({"risk_sizing_mode": "exit_only"}) is True

    def test_a_near_miss_marker_is_not_exit_only(self) -> None:
        """Case, padding and a wrong mode are all refused.

        This is the half that carries the safety weight. A tolerant comparison would let a
        near-miss pass as the real marker, and the marker is what bypasses the entry SL/TP guard -
        so a near-miss that slipped through would be an order submitted without its stop.
        """
        from live_runner.base_adapter import is_exit_only_metadata

        for metadata in (
            {"risk_sizing_mode": "EXIT_ONLY"},
            {"risk_sizing_mode": "Exit_Only"},
            {"risk_sizing_mode": " exit_only "},
            {"risk_sizing_mode": "exit-only"},
            {"risk_sizing_mode": "exit_only_v2"},
            {"risk_sizing_mode": "not_exit_only"},
            {"risk_sizing_mode": ""},
            {"risk_sizing_mode": None},
            {"risk_sizing_mode": 0},
            {"risk_sizing_mode": True},
            {},
            {"some_other_key": "exit_only"},
        ):
            assert is_exit_only_metadata(metadata) is False, (
                f"{metadata!r} was treated as an exit-only order, which would bypass the "
                "entry stop-loss and take-profit guard"
            )

    @pytest.mark.parametrize(
        "metadata", [None, "exit_only", 42, 0, ["exit_only"], ("exit_only",), object()]
    )
    def test_a_non_dict_is_never_exit_only(self, metadata) -> None:
        """**The guard is what stops an `AttributeError` on the order path.**

        Without the `isinstance` check, `metadata.get(...)` raises on `None`, a string and an
        int alike - and this runs while routing an order, so the failure would be an exception
        rather than a refused order. Asserted over several types because a guard that only
        handles `None` is a weaker guard than it looks.
        """
        from live_runner.base_adapter import is_exit_only_metadata

        try:
            result = is_exit_only_metadata(metadata)
        except AttributeError as exc:  # pragma: no cover - the failure this guards
            raise AssertionError(
                f"{metadata!r} raised instead of being refused: {exc}"
            ) from exc
        assert result is False, f"{metadata!r} was treated as an exit-only order"

    def test_the_exit_only_marker_never_consults_the_signal_action(self) -> None:
        """Documented as a deliberate design, so it is pinned rather than left implied.

        The close side comes from the broker book: closing a SHORT is a BUY. So an action test
        would invert the meaning on exactly the orders that matter - every stop-loss and
        take-profit on a short would be refused while long exits passed. Asserted by showing
        that the *same* action yields different results depending only on the marker.
        """
        from live_runner.base_adapter import is_exit_only_metadata

        buy_without = self._signal({"risk_sizing_mode": None})
        buy_with = self._signal({"risk_sizing_mode": "exit_only"})
        assert buy_without.action == buy_with.action == "buy"
        assert is_exit_only_metadata(buy_without.metadata) is False
        assert is_exit_only_metadata(buy_with.metadata) is True

    def test_the_unbounded_tp_marker_has_the_same_exactness(self) -> None:
        """The twin function, asserted identically - which is the point of pairing them.

        The pre-mortem that produced these two constants was about *divergent spellings*
        between producer and consumers, so the two functions carrying the same contract should
        carry the same tests. That reasoning is what caught the gap: the non-dict mutation on
        this one was already killed while the identical change to `is_exit_only_metadata`
        survived.
        """
        from live_runner.base_adapter import UNBOUNDED_TP_POLICY, is_unbounded_tp

        assert UNBOUNDED_TP_POLICY == "unbounded_exit_on_opposite_signal"
        assert is_unbounded_tp({"take_profit_policy": UNBOUNDED_TP_POLICY}) is True
        for metadata in (
            {"take_profit_policy": "UNBOUNDED_EXIT_ON_OPPOSITE_SIGNAL"},
            {"take_profit_policy": " unbounded_exit_on_opposite_signal "},
            {"take_profit_policy": "unbounded"},
            {"take_profit_policy": ""},
            {"take_profit_policy": None},
            {},
            {"risk_sizing_mode": "exit_only"},
        ):
            assert is_unbounded_tp(metadata) is False, (
                f"{metadata!r} declared the unbounded-TP policy"
            )

    @pytest.mark.parametrize(
        "metadata", [None, "unbounded", 42, ["unbounded"], object()]
    )
    def test_a_non_dict_declares_neither_marker(self, metadata) -> None:
        """Neither marker can come from a non-dict, and neither may raise."""
        from live_runner.base_adapter import is_exit_only_metadata, is_unbounded_tp

        assert is_exit_only_metadata(metadata) is False
        assert is_unbounded_tp(metadata) is False

    def test_the_marker_stringification_is_a_verified_equivalent_mutation_target(self) -> None:
        """**A verified equivalent mutant**, recorded with the reason rather than dropped.

        `is_exit_only_metadata` reads:

            str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE

        Removing the `str()` changes nothing, because `EXIT_ONLY_RISK_SIZING_MODE` is the string
        constant `"exit_only"`. A `str()` is the identity on a `str`, and the only way a
        non-string could reach the comparison and stringify *to* `"exit_only"` is a custom
        object whose `__str__` returns exactly that - at which point the stringification would
        be doing something surprising rather than normalising.

        So the `str()` is defence against a caller passing a non-string, not a conversion that
        any current input needs. It stays for the same reason the `isinstance` guard stays: both
        cost nothing, and both keep the function total for inputs nobody has written yet.

        Recorded because the reasoning is not obvious from the line, and because "is that
        `str()` needed?" is a reasonable question that would otherwise be re-derived from
        scratch - or, worse, answered by deleting something load-bearing in a sibling
        function.
        """
        from live_runner.base_adapter import EXIT_ONLY_RISK_SIZING_MODE

        assert isinstance(EXIT_ONLY_RISK_SIZING_MODE, str)
        assert str(EXIT_ONLY_RISK_SIZING_MODE) == EXIT_ONLY_RISK_SIZING_MODE
        # And the only way the str() could matter is a non-string, which the type guard in
        # the same line sequence already excludes from reaching a comparison as a match.
        for non_string in (1, 1.0, True, None, object(), ["exit_only"]):
            assert str(non_string) != EXIT_ONLY_RISK_SIZING_MODE, (
                f"{non_string!r} stringifies to the marker, so the str() is load-bearing "
                "and this note is wrong"
            )

    def test_a_marker_does_not_imply_the_other(self) -> None:
        """They are independent contracts, and conflating them would be a silent behaviour change.

        The exit-only marker routes a *flatten* order; the unbounded-TP policy is about how a
        strategy takes profit. A signal carrying one must not acquire the other, because the
        two have different consequences in the adapters.
        """
        from live_runner.base_adapter import is_exit_only_metadata, is_unbounded_tp

        exit_only = {"risk_sizing_mode": "exit_only"}
        unbounded = {"take_profit_policy": "unbounded_exit_on_opposite_signal"}
        assert is_exit_only_metadata(exit_only) is True
        assert is_unbounded_tp(exit_only) is False
        assert is_unbounded_tp(unbounded) is True
        assert is_exit_only_metadata(unbounded) is False


class TestAMonthlySpellingIsRefusedAtTheConfigBoundary:
    """Item 8: `1M` is a month nearly everywhere, and here it was being traded as a minute.

    `normalize_timeframe` lowercases, so `1M` and `1m` become the same string and
    `_timeframe_seconds` returns 60 for both - which means `_require_recognised_timeframe` is
    never reached and the guard that exists has nothing to catch.

    The two measured consequences were **not** in the safe direction. `_next_candle_close("1M", 60)`
    returns *the next minute*, so a "monthly" candle closes 30 seconds later and the strategy
    enters at a price it was never validated against. `_alpaca_timeframe("1M")` returns `1m`, so
    minute bars reach the signal processor and ATR/stops are computed from minute-scale data.
    (cTrader is immune: no `get_market_data`, so it fails closed.)

    **For `<n>M` this is not a new policy.** `BacktestConfig` has always required the canonical,
    case-sensitive spelling - `runner/engines/base.py:190` - so the backtester refused `1M` while
    the live path accepted it, and refusing it at the live boundary makes those two agree.

    **That parity argument covers `<n>M` only.** `BacktestConfig` also refuses `1D`, `1W`, `1H`,
    `5M`, `M15` and `m5`; this guard does not, so the live path remains more permissive for those.
    The divergence is deliberate and recorded - see `_MONTH_TIMEFRAME_PATTERN`'s comment - and a
    test below pins it so it stays visible rather than being closed by accident.

    Measured exposure was zero - no config row, no database row, no file on disk - so this closes
    a latent path with nothing to migrate.
    """

    @staticmethod
    def _config(timeframe: str):
        from shared.models import LiveDeploymentConfig

        return LiveDeploymentConfig(
            strategy_name="s", account_id="a", symbols=["AAPL"], timeframe=timeframe
        )

    @pytest.mark.parametrize("spelling", ["1M", "5M", "15M", "30M", " 1M ", "1M\n"])
    def test_a_monthly_spelling_is_refused(self, spelling) -> None:
        with pytest.raises(ValueError, match="monthly candle"):
            self._config(spelling)

    def test_the_error_explains_the_collision_rather_than_just_refusing(self) -> None:
        """The person who typed this is the one who has to understand the refusal.

        A bare "invalid timeframe" would send them looking for a spelling table. What they need
        to know is that `1M` means something *else* where they came from, and what to write here.
        """
        with pytest.raises(ValueError) as excinfo:
            self._config("1M")
        message = str(excinfo.value)
        assert "1m" in message, f"the error does not give the spelling to use instead: {message}"
        assert "minutes" in message, (
            f"the error does not say what it would have been read as: {message}"
        )

    @pytest.mark.parametrize(
        "spelling",
        ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "s1", "tick"],
    )
    def test_every_canonical_timeframe_still_works(self, spelling) -> None:
        """The guard refuses one spelling. It must not quietly narrow the accepted set."""
        assert self._config(spelling).timeframe == spelling

    @pytest.mark.parametrize(
        "alias", ["m1", "min1", "m5", "m15", "m30", "h1", "d1", "daily", "hour1"]
    )
    def test_minute_aliases_are_unaffected(self, alias) -> None:
        """Letter-first aliases carry no casing meaning, so they must keep working.

        `m1` is OANDA's minute spelling and `min1` is the grid downloader's. Both mean one
        minute here, exactly as the uppercase `1M` did - but the *intent* is unambiguous, which is
        what separates them from `<n>M`.
        """
        assert self._config(alias).timeframe == alias

    @pytest.mark.parametrize("spelling", ["1D", "1W", "1H"])
    def test_uppercase_day_and_hour_are_left_alone(self, spelling) -> None:
        """Only `M` is dangerous, and only after a digit - and this is a *known divergence*.

        `1D` is *day* on every platform that distinguishes it from a month, so refusing it would
        break a legitimate spelling in order to fix an illegitimate one.

        This test deliberately freezes a live/backtest **disagreement**, so it is worth being precise
        about that: `BacktestConfig` refuses all three of these, because it accepts only canonical
        spellings. This guard accepts them. The disagreement is recorded rather than closed, because
        widening the guard to match would reject `1D` - a legitimate spelling - on the strength of a
        change made about months. If a future change does widen it, this test is what should fail
        loudly and say why.
        """
        assert self._config(spelling).timeframe == spelling

    @pytest.mark.parametrize("spelling", ["1D", "1W", "1H", "M15", "m5"])
    def test_the_live_path_is_still_more_permissive_than_the_backtester_for_these(self, spelling) -> None:
        """The parity claim is scoped to `<n>M`; this is the test that keeps it scoped.

        `BacktestConfig` accepts only canonical, case-sensitive spellings, so it refuses every one of
        these. The live guard refuses `<n>M` and nothing else on the list. Anyone widening the guard
        to match the backtester will break this test, which is the intended moment to decide whether
        rejecting `1D` is really wanted.
        """
        from runner.engines.base import BacktestConfig

        with pytest.raises(ValueError, match="Invalid timeframe"):
            BacktestConfig(
                strategy_name="s", symbols=["AAPL"], timeframe=spelling,
                start_date="2021-01-01", end_date="2025-06-30",
            )
        assert self._config(spelling).timeframe == spelling, (
            f"{spelling!r} is no longer accepted live; if the guard was widened to match "
            "BacktestConfig, update the reasoning in _MONTH_TIMEFRAME_PATTERN rather than only "
            "this assertion"
        )

    def test_the_api_payload_is_guarded_too(self) -> None:
        """The UI is a `<select>`, so a typo arrives via the API or via YAML.

        The exposure analysis named exactly those two routes, so guarding only the persisted
        model would leave the API as a way in.
        """
        from shared.models import LiveDeploymentCreateRequest

        with pytest.raises(ValueError, match="monthly candle"):
            LiveDeploymentCreateRequest(
                account_id="a", symbols=["AAPL"], timeframe="1M"
            )

    def test_the_backtester_already_refused_the_same_spelling(self) -> None:
        """If `BacktestConfig` ever accepts `1M`, this pin is what says the two have diverged."""
        from runner.engines.base import BacktestConfig

        with pytest.raises(ValueError, match="Invalid timeframe"):
            BacktestConfig(
                strategy_name="s", symbols=["AAPL"], timeframe="1M",
                start_date="2021-01-01", end_date="2025-06-30",
            )

    def test_the_helpers_below_the_boundary_still_measure_as_documented(self) -> None:
        """These are unchanged, and that is the point.

        `_next_candle_close` and `_alpaca_timeframe` still treat `1M` as a minute. They are not
        fixed here because the right place to refuse it is the boundary, where a person finds out.
        This test records that the refusal is *upstream* of them, so nobody later "fixes" these
        two and thinks the boundary has been handled.
        """
        from live_runner.alpaca_adapter import _alpaca_timeframe
        from live_runner.signal_sources.execution_timing import _next_candle_close

        _, normalized = _alpaca_timeframe("1M")
        assert normalized == "1m", (
            f"_alpaca_timeframe now normalizes 1M to {normalized!r}; the boundary refusal is "
            "where this is meant to be handled, and if this changed it needs re-measuring"
        )
        # The reference comes first; `timeframe` is keyword-only and is what carries the spelling,
        # since `timeframe_seconds` is already the normalised 60 for both "1m" and "1M".
        from datetime import datetime, timezone

        moment = datetime(2024, 1, 3, 12, 0, 30, tzinfo=timezone.utc)
        assert _next_candle_close(moment, 60, timeframe="1M") == datetime(
            2024, 1, 3, 12, 1, 0, tzinfo=timezone.utc
        ), (
            "_next_candle_close no longer resolves 1M to the next minute; the documented "
            "consequence of the collision has changed"
        )


# --- size_live_signal_for_submission is executed by no test --------------------
#
# The one test in this file that mentions `size_live_signal_for_submission` *replaces* it with a stub
# (`_sizing_without_stop`, above) so a case about a missing stop-loss can run. Nothing executed the real
# function, which the reach measurement confirmed independently: it is reached transitively from
# `submit_order_with_risk`, but no test names it.
#
# Two mutations were applied to prove the gap rather than infer it from a name list:
#
#   M1  "enforce_stop_direction": True -> False          SURVIVED, 4969 passed, exit 0
#   M2  drop the broker position count before sizing      killed, 2 failed
#
# M1 is the dangerous one. `enforce_stop_direction` is what makes the risk engine refuse a stop on the
# wrong side of the entry, so setting it False allows a live order to be sized against a stop that would
# not protect the position - and the entire suite stayed green. M2 being killed shows the harness works
# and the gap is specific, not "this function is untested".
#
# Both tests wrap the real RiskEngine rather than substituting one. An earlier attempt returned a
# `SimpleNamespace`, which the `realized_risk_amount` helper then rejected for missing fields - a stub
# that has to imitate more of the engine than the engine itself is worth. Delegating to `super()` keeps
# the assertions on real sizing output.
def _spy_risk_engine(captured: dict[str, Any]) -> type:
    from live_runner import daemon as daemon_module

    real = daemon_module.RiskEngine

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, config: Any) -> None:
            super().__init__(config)
            captured["config"] = config

        def calculate_position_size(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return super().calculate_position_size(**kwargs)

    return _Spy


def _long_signal(**metadata: Any) -> LiveSignal:
    return LiveSignal(
        deployment_id="d1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1000,
        timestamp=datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
        metadata=metadata,
    )


def test_live_sizing_enforces_stop_direction() -> None:
    from live_runner.daemon import size_live_signal_for_submission

    captured: dict[str, Any] = {}
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("live_runner.daemon.RiskEngine", _spy_risk_engine(captured))
        size_live_signal_for_submission(
            _long_signal(stop_loss=1.0950, take_profit=1.1100),
            _deployment(),
            account_balance=10_000.0,
            current_exposure=0.0,
            pip_value=10.0,
            current_positions=0,
        )
    finally:
        monkey.undo()

    # M1 dies here. With the flag flipped to False the sizing call still succeeds, so nothing else in
    # the suite notices; only this assertion sees the difference.
    assert captured["signal"].metadata["enforce_stop_direction"] is True
    assert captured["signal"].stop_loss == pytest.approx(1.0950)
    assert captured["signal"].take_profit == pytest.approx(1.1100)
    assert captured["account_balance"] == pytest.approx(10_000.0)
    assert captured["pip_value"] == pytest.approx(10.0)


def test_live_sizing_carries_broker_position_count_into_the_signal() -> None:
    from live_runner.daemon import size_live_signal_for_submission

    captured: dict[str, Any] = {}
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("live_runner.daemon.RiskEngine", _spy_risk_engine(captured))
        sized = size_live_signal_for_submission(
            _long_signal(stop_loss=1.0950),
            _deployment(),
            account_balance=10_000.0,
            current_positions=2,
        )
    finally:
        monkey.undo()

    # M2 dies here. Without the count, max_positions and max_total_exposure bind as if the account
    # were flat, which is exactly what the call site warns about.
    assert sized.metadata["current_positions"] == 2
    assert sized.metadata["qty"] == pytest.approx(sized.metadata["explicit_qty"])
    assert sized.metadata["qty"] > 0


# --- _resolve_account_balance: cash vs equity ---------------------------------
#
# No test named this function. It decides which broker figure sizes a live order, and the distinction
# is not cosmetic: `get_balance()` returns `(balance, equity)` where balance is cash and equity
# includes floating P&L. Sizing on cash instead of equity under-sizes every order whenever a position
# is open.
#
# Mutating the unpacking - `_, equity` to `equity, _` - SURVIVED the full suite: 4,971 passed, exit 0.
# The test below is the one that should have existed.
def test_resolve_account_balance_prefers_equity_over_cash() -> None:
    from live_runner.daemon import _resolve_account_balance

    class _BalanceAdapter:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balance(self) -> tuple[float, float]:
            self.calls += 1
            # Deliberately different values. A test returning the same number twice would agree with
            # either unpacking, which is how this gap survived in the first place.
            return 12_000.0, 20_000.0  # cash, equity

    adapter = _BalanceAdapter()
    resolved = asyncio.run(_resolve_account_balance(adapter, None))
    assert resolved == pytest.approx(20_000.0)
    assert adapter.calls == 1


def test_resolve_account_balance_uses_the_supplied_balance_without_asking_the_broker() -> None:
    from live_runner.daemon import _resolve_account_balance

    class _MustNotBeCalled:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balance(self) -> tuple[float, float]:
            self.calls += 1
            return 1.0, 1.0

    adapter = _MustNotBeCalled()
    resolved = asyncio.run(_resolve_account_balance(adapter, 5_000.0))
    assert resolved == pytest.approx(5_000.0)
    assert adapter.calls == 0, "a supplied balance must not trigger a broker round-trip"


# --- the balance poller, and what it does when a broker credential dies --------
#
# Neither function was named by a test, and both mutations survived the full suite (4,973 passed,
# exit 0):
#
#   M3  drop _block_ctrader_account() from the terminal-auth path
#   M4  record `balance` in the equity column
#
# These two belong together because they describe how an account goes quiet. On a terminal
# authorization failure the adapter is removed from the pool; blocking the account is what stops it
# being retried forever. Removing the adapter without blocking it leaves an account that has stopped
# polling with nothing recording why - which is exactly the shape of the two ACTIVE deployments found
# silent in production on 2026-10-15 (79h and ~50d). This suite cannot say what happened there; it can
# only make sure the code that would leave a trace is exercised.
def test_poll_adapter_balance_records_balance_and_equity_separately() -> None:
    from live_runner import daemon as daemon_module

    saved: list[Any] = []

    class _BalanceAdapter:
        async def get_balance(self) -> tuple[float, float]:
            # Different values on purpose: a record built from either column looks right if both
            # columns hold the same number.
            return 3_000.0, 4_500.0

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            daemon_module,
            "save_live_balance",
            lambda record: saved.append(record),
        )
        adapters = {"acc-1": _BalanceAdapter()}
        asyncio.run(
            daemon_module._poll_adapter_balance(
                adapters,
                "acc-1",
                adapters["acc-1"],
                datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
            )
        )
    finally:
        monkey.undo()

    assert len(saved) == 1
    # M4 dies here.
    assert saved[0].balance == pytest.approx(3_000.0)
    assert saved[0].equity == pytest.approx(4_500.0)
    assert saved[0].account_id == "acc-1"


def test_terminal_auth_failure_blocks_the_account_before_dropping_the_adapter() -> None:
    from live_runner import daemon as daemon_module
    from live_runner.ctrader_adapter import CTraderAuthTerminalError

    disconnected: list[str] = []

    class _FailingAdapter:
        async def get_balance(self) -> tuple[float, float]:
            raise CTraderAuthTerminalError("token revoked")

    class _SpyAdapter:
        pass

    adapters = {"acc-1": _SpyAdapter()}

    async def _fake_disconnect(adapter_obj: Any, *, account_id: str, reason: str) -> None:
        disconnected.append(f"{account_id}:{reason}")

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(daemon_module, "_disconnect_adapter", _fake_disconnect)
        # `_block_ctrader_account` is deliberately NOT stubbed. An earlier version stubbed it and then
        # asserted the real block set was populated, which is a test that contradicts itself: the stub
        # guarantees the set stays empty. Asserting the real side effect is both coherent and stronger.
        daemon_module._CTRADER_AUTH_BLOCKED_ACCOUNTS.discard("acc-1")
        daemon_module._CTRADER_AUTH_BLOCKED_REASONS.pop("acc-1", None)
        asyncio.run(
            daemon_module._poll_adapter_balance(
                adapters,
                "acc-1",
                _FailingAdapter(),
                datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
            )
        )
        was_blocked = "acc-1" in daemon_module._CTRADER_AUTH_BLOCKED_ACCOUNTS
    finally:
        monkey.undo()
        daemon_module._CTRADER_AUTH_BLOCKED_ACCOUNTS.discard("acc-1")
        daemon_module._CTRADER_AUTH_BLOCKED_REASONS.pop("acc-1", None)

    # M3 dies here. Without the block the account is simply dropped from the pool: polling stops and
    # nothing records that a terminal authorization failure stopped it.
    assert was_blocked, "a terminal auth failure must block the account"
    assert "acc-1" not in adapters, "the dead adapter must not stay in the pool"
    assert disconnected == ["acc-1:terminal_auth_failure"]

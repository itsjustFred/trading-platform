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
from uuid import UUID, uuid4

import pandas as pd
import pytest

from live_runner.base_adapter import BaseLiveAdapter
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal


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
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
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
    """max_positions must bind on broker truth, not caller-supplied metadata."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_positions=2)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 0.0


def test_submit_order_with_risk_blocks_when_broker_exposure_consumes_cap():
    """max_total_exposure must bind using estimated open risk per broker position."""
    from live_runner.daemon import submit_order_with_risk

    # 2 open positions * 1% risk == 2% == max_total_exposure -> nothing left.
    deployment = _deployment(max_total_exposure=0.02, max_positions=10)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 0.0


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
    """A stopless signal is sized on the engine's fallback stop and the broker
    order carries that resolved stop plus a 1:1 take profit."""
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

    asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))

    assert adapter.submitted_signal is not None
    metadata = adapter.submitted_signal.metadata
    # Sizing: risk budget 1000 (1% of 100k) / distance 2 = 500; the 20%
    # notional cap (20000 / entry 100) binds and reduces the position to 200.
    assert metadata["qty"] == pytest.approx(200.0)
    assert metadata["stop_loss"] == pytest.approx(98.0)
    assert metadata["take_profit"] == pytest.approx(102.0)


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
    """Kills mutant: relaxing the qty <= 0 guard to qty < 0."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: pytest.fail("order must not reach the broker"),
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)

    # A stop on the wrong side of entry makes the live sizing engine return
    # qty 0 (bad_stop_direction) -> submit must refuse before the broker.
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "entry_price": 100.0,
            "stop_loss": 105.0,
        },
    )

    with pytest.raises(ValueError, match="non-positive quantity"):
        asyncio.run(adapter.submit_order(signal))


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
        metadata={"qty": 1.0, "extended_hours": True, "limit_price": 101.5, "stop_loss": 95.0, "take_profit": 110.0},
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


def test_alpaca_discord_redelivery_submits_once_with_canonical_signal_id(monkeypatch):
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
        return SimpleNamespace(id="alpaca-order-1")

    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=_submit_order,
    )
    monkeypatch.setattr(
        module,
        "size_live_signal",
        lambda _signal, *, account_balance: SimpleNamespace(position_size=1.0, risk_percent=0.01),
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
        metadata={"qty": 1.0, "discord_message_id": "discord-123", "stop_loss": 95.0, "take_profit": 110.0},
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

    assert asyncio.run(adapter.submit_order(first)) == "alpaca-order-1"
    with pytest.raises(RuntimeError, match="canonical signal already exists"):
        asyncio.run(adapter.submit_order(redelivery))

    assert len(submitted_requests) == 1
    assert submitted_requests[0].client_order_id == canonical_signal_id
    assert persisted_order_ids == [(canonical_signal_id, "alpaca-order-1")]


def test_alpaca_redelivery_recovers_accepted_order_without_resubmitting(monkeypatch):
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
        return SimpleNamespace(id="alpaca-accepted-before-timeout")

    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        get_order_by_client_id=_lookup,
        submit_order=lambda *, order_data: submit_calls.append(order_data),
    )
    monkeypatch.setattr(
        module,
        "size_live_signal",
        lambda _signal, *, account_balance: SimpleNamespace(position_size=1.0, risk_percent=0.01),
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

    assert asyncio.run(adapter.submit_order(signal)) == "alpaca-accepted-before-timeout"
    assert lookup_calls == [canonical_signal_id]
    assert submit_calls == []
    assert persisted_order_ids == [
        (canonical_signal_id, "alpaca-accepted-before-timeout")
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
        module,
        "size_live_signal",
        lambda _signal, *, account_balance: SimpleNamespace(position_size=1.0, risk_percent=0.01),
    )
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (canonical_signal_id, False)
    )
    monkeypatch.setattr(
        module,
        "get_live_signal_broker_order_id",
        lambda signal_id: "alpaca-already-persisted"
        if signal_id == canonical_signal_id
        else None,
    )

    assert asyncio.run(adapter.submit_order(signal)) == "alpaca-already-persisted"


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
        metadata={"qty": 1.0, "discord_message_id": "discord-456", "stop_loss": 95.0, "take_profit": 110.0},
    )
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda *, order_data: SimpleNamespace(id="alpaca-order-2"),
    )
    monkeypatch.setattr(
        module,
        "size_live_signal",
        lambda _signal, *, account_balance: SimpleNamespace(position_size=1.0, risk_percent=0.01),
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
        asyncio.run(adapter.submit_order(signal))

    assert persistence_calls == [(canonical_signal_id, "alpaca-order-2")]






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

def test_exit_only_target_qty_sums_all_matching_positions():
    """Hedging accounts can hold several positions per symbol; the close must
    flatten all of them, not just the first match."""
    from live_runner.daemon import _resolve_exit_only_target_qty

    positions = [
        {"symbol": "EURUSD", "qty": 2.0},
        {"symbol": "eurusd", "qty": 3.5},
        {"symbol": "AAPL", "qty": 10.0},
        {"symbol": "EURUSD", "qty": 0.5},
    ]

    assert _resolve_exit_only_target_qty(iter(positions), "EURUSD") == pytest.approx(6.0)


def test_exit_only_target_qty_ignores_opposite_side_residual():
    """Signed quantities: a residual short lot is not part of the long exposure
    the exit-only close is meant to flatten."""
    from live_runner.daemon import _resolve_exit_only_target_qty

    positions = [
        {"symbol": "EURUSD", "qty": 2.0},
        {"symbol": "EURUSD", "qty": -0.5},
    ]

    assert _resolve_exit_only_target_qty(iter(positions), "EURUSD") == pytest.approx(2.0)


def test_exit_only_target_qty_skips_unparseable_rows():
    from live_runner.daemon import _resolve_exit_only_target_qty

    positions = [
        {"symbol": "EURUSD", "qty": "not-a-number"},
        {"symbol": "EURUSD", "qty": 1.5},
    ]

    assert _resolve_exit_only_target_qty(iter(positions), "EURUSD") == pytest.approx(1.5)


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
    """A positions flood between snapshots must not push the estimate past the
    aggregate cap; the cap is the binding constraint, not position count."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.05, max_positions=100)
    adapter = _PositionsAdapter(
        positions=[{"symbol": f"S{i}", "qty": 1.0} for i in range(9)]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    # 9 positions x 1% == 9% > 5% cap; clamped to the cap leaves zero budget.
    assert adapter.submitted_signal.metadata["qty"] == 0.0


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


def test_submit_order_with_risk_zeroes_size_when_budget_exhausted_by_positions():
    """Positions at the cap consume the whole budget: the next signal must be
    sized to zero via the exposure path, not blocked only by max_positions."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_risk_per_trade=0.01, max_total_exposure=0.02, max_positions=10)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 1.0},
            {"symbol": "MSFT", "qty": 1.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 0.0


# ── B-7: one cTrader volume contract ──────────────────────────────────────────

def test_ctrader_volume_contract_uses_qty_units_only():
    """Only the daemon-sized quantity may determine broker volume."""
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
    for metadata in ({"ctrader_volume": 100000}, {"volume": 100000}, {}):
        with pytest.raises(ValueError, match="no positive quantity"):
            adapter._resolve_order_volume(_signal(metadata))


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
    bad_signal = _signal("not-a-number")
    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(bad_signal)


def test_ctrader_fill_quantities_are_persisted_in_qty_units(monkeypatch):
    """Live fill events and reconcile backfills must persist filled_qty in the
    same unit as signal metadata qty (broker volume / 1000), so the viewer's
    position reconstruction and the cumulative cap compare like with like."""
    module, adapter = _make_ctrader_adapter()
    adapter.account_config_id = "local-account-123"
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

    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,  # FILLED
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
        _validate_signal_age_before_wait(stale, deployment, timeframe="5m")


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

    _validate_signal_age_before_wait(recent, deployment, timeframe="1h")

    recent.metadata["source"] = "discord"
    recent.metadata.pop("received_at")
    with pytest.raises(ValueError, match="missing received_at"):
        _validate_signal_age_before_wait(recent, deployment, timeframe="1h")


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
def isolated_db(monkeypatch, tmp_path: Path):
    import shared.database as database

    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
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


def _legacy_live_signal_table(database: Any, *, table_unique: bool = False) -> None:
    unique_clause = ", UNIQUE(discord_message_id)" if table_unique else ""
    with database.ENGINE.begin() as connection:
        connection.execute(
            database.text(
                "CREATE TABLE live_signals ("
                "id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, symbol TEXT NOT NULL, "
                "action TEXT NOT NULL, theoretical_price FLOAT NOT NULL, timestamp TEXT NOT NULL, "
                "source TEXT NOT NULL DEFAULT 'internal', broker_order_id TEXT NOT NULL DEFAULT '', "
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
        row_count = connection.execute(database.text("SELECT COUNT(*) FROM live_signals")).scalar_one()
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
    # A different broker-reported identity must produce a different fingerprint,
    # so the persisted value can be used to detect a silent re-point.
    database.record_live_account_demo_verification(
        "acc-1", is_demo=True, ctid="ctid-b", broker_login="login-a"
    )
    assert _fingerprint("acc-1") is not None


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
    assert database.count_live_deployment_submissions(deployment_id) == 1

    database.update_live_deployment_status(deployment_id, "PAUSED")

    assert database.claim_live_deployment_submission(deployment_id, "token-2") is False
    assert database.count_live_deployment_submissions(deployment_id) == 1
    assert database.release_live_deployment_submission("token-1") is True
    assert database.count_live_deployment_submissions(deployment_id) == 0


def test_expired_submission_claim_does_not_block_pause(isolated_db):
    database = isolated_db
    deployment = _deployment()
    _verified_demo_account(database, "acc-1", "ctrader")
    database.create_live_deployment(deployment)

    deployment_id = str(deployment.id)
    assert database.claim_live_deployment_submission(
        deployment_id, "expired-token", ttl_seconds=0.01
    ) is True
    time.sleep(0.05)

    assert database.count_live_deployment_submissions(deployment_id) == 0


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
            id="bo-9",
            filled_qty="3500",  # cumulative - must NOT be persisted
            filled_avg_price="49.9",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0][0].filled_qty == 100.0
    assert saved[0][0].dedupe_key == "alpaca:exec:exec-9"
    assert saved[0][0].broker_order_id == "bo-9"
    assert saved[0][1] == 3500.0


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
            id="bo-delayed",
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
            id="bo-10",
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
            id="bo-77",
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
    assert saved[0][0].broker_order_id == "bo-77"
    assert saved[0][0].dedupe_key == "alpaca:order:bo-77:reconciled:25.0"
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
            id=f"order-{signal_id}",
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
            id="bo-partial-recovery",
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
    assert saved[0][0].dedupe_key == "alpaca:order:bo-partial-recovery:reconciled:3.0"
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
            id="bo-no-delta",
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
    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,  # FILLED
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


def test_ctrader_reconcile_recovers_accepted_order_before_local_id_persistence(monkeypatch):
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

    deal = SimpleNamespace(
        dealId=999,
        dealStatus=1,
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


def test_ctrader_redelivery_recovers_accepted_order_without_resubmitting(monkeypatch):
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


def test_ctrader_reconcile_skips_signals_without_broker_order_id(monkeypatch):
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
        module, "get_live_signal_broker_order_id", lambda _sid: "f47ac10b-58cc-4372-a567-0e02b2c3d479"
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


def _make_ctrader_request_builder_adapter():
    """Return (module, adapter) with cTrader proto stubs for _build_new_order_request."""
    module, adapter = _make_ctrader_adapter()

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


def test_ctrader_rejects_unknown_signal_action_instead_of_selling():
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


def test_ctrader_relative_sl_tp_direction_and_rejections():
    """Relative distances are signed by trade side at the protocol level; the
    sent magnitude must be the positive distance in 1/100000 price units, and
    wrong-side/non-finite/zero-distance prices must fail closed."""
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
    assert adapter.submitted_qty == pytest.approx(3.0)


def test_exit_only_metadata_cannot_open_a_long():
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

    with pytest.raises(ValueError, match="non-sell"):
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
        metadata={"exit_only": False, "risk_sizing_mode": "untrusted_entry_mode"},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["exit_only"] is False
    assert adapter.submitted_signal.metadata["risk_sizing_mode"] == "risk_sized"


def test_alpaca_buy_cannot_use_exit_only_mode_to_bypass_entry_guards():
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )

    with pytest.raises(ValueError, match="without stop_loss"):
        adapter._resolve_broker_sl_tp(signal)


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


def test_ctrader_exit_only_orders_bypass_entry_sl_tp_guard():
    """Exit-only closes carry no SL/TP and must not set relative distances."""
    module, adapter = _make_ctrader_request_builder_adapter()
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )

    request = adapter._build_new_order_request(signal, symbol_id=7)

    assert not hasattr(request, "relativeStopLoss") or request.relativeStopLoss == 0
    assert not hasattr(request, "relativeTakeProfit") or request.relativeTakeProfit == 0


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


def test_ctrader_buy_cannot_use_exit_only_mode_to_bypass_entry_guards():
    _module, adapter = _make_ctrader_request_builder_adapter()
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 2.0, "exit_only": True, "risk_sizing_mode": "exit_only"},
    )

    with pytest.raises(ValueError, match="without stop_loss"):
        adapter._build_new_order_request(signal, symbol_id=7)


def test_submit_order_with_risk_resolves_fallback_sl_tp_into_metadata():
    """A signal without explicit SL/TP gets the risk engine's resolved stop and a
    TP derived from the engine's risk-reward geometry, so the broker always
    receives bounded prices."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, signal, pip_value=1.0))

    assert adapter.submitted_signal is not None
    metadata = adapter.submitted_signal.metadata
    stop_loss = float(metadata["stop_loss"])
    take_profit = float(metadata["take_profit"])
    assert stop_loss == pytest.approx(98.0)  # fallback: entry * (1 - 0.02)
    # risk_reward_ratio of the fallback geometry is 1.0 -> TP mirrors the stop distance
    assert take_profit == pytest.approx(102.0)


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
    validate_discord_deployment_signal_config(
        _discord_deployment("1469353015738630206")
    )


def test_non_discord_deployment_ignores_channel_config():
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
    from live_runner.alpaca_adapter import _alpaca_market_data_start
    from datetime import datetime, timezone

    start = _alpaca_market_data_start("1h", 500)
    span_hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    trading_hours = span_hours * (5 / 7)  # weekends closed
    assert trading_hours >= 500, (
        f"lookback must cover {500} trading hours, only ~{trading_hours:.0f} available"
    )


def test_market_data_start_scales_past_holidays_for_daily_bars():
    from live_runner.alpaca_adapter import _alpaca_market_data_start
    from datetime import datetime, timezone

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


def test_widened_market_data_request_asks_for_the_newest_bars():
    """The response is capped at `limit`, so ordering decides which bars arrive.

    An ascending request over a widened window returns the oldest bars in it, so
    the widening retry meant to recover a short history would have returned
    staler data than the first attempt.
    """
    import asyncio
    from live_runner import alpaca_adapter as mod
    from alpaca.common.enums import Sort

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

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

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
    only traded-size authority."""
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
        _build_processor_market_data(signal, timeframe="5m")


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


def test_ctrader_reconciliation_candidates_include_partially_executed_signal(isolated_db):
    database = isolated_db
    deployment = _deployment()
    database.ensure_live_account("acc-1", "ctrader")
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
    database.ensure_live_account("acc-1", "ctrader")
    database.ensure_live_account("acc-2", "ctrader")
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
    database.ensure_live_account("acc-1", "alpaca")
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
    database.ensure_live_account("acc-1", "alpaca")
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
    database.ensure_live_account("acc-1", "ctrader")
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
    database.ensure_live_account("acc-1", "alpaca")
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
    database.ensure_live_account("acc-1", "alpaca")
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
    database.ensure_live_account("acc-1", "alpaca")
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
    database.ensure_live_account("acc-1", "ctrader")
    database.ensure_live_account("acc-2", "ctrader")
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


def test_ctrader_submit_order_fails_closed_when_broker_id_is_missing(monkeypatch):
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


def test_ctrader_submit_order_with_id_persists_and_returns_id(monkeypatch):
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
        metadata={"qty": 3.0, "risk_sizing_mode": "exit_only"},
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
        metadata={"ctrader_volume": 3000, "risk_sizing_mode": "exit_only"},
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

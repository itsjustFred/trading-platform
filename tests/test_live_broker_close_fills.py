"""Issue #144: a broker-side close must reduce the ledger an exit is sized against.

The ledger behind exit sizing (``get_live_deployment_net_filled_qty``, #142 for #121) sums the
fills recorded against a deployment's own signals. A broker-side close - an Alpaca bracket's
stop/target leg, a cTrader SL/TP close, a flatten, a manual close - is an order this platform
never submitted, so its fill names no signal of ours and stays out of the ledger. The broker
position shrinks while every ledger still counts the shares, and #142's guard then reports a
mismatch on *every* later exit for that account and symbol: the exit is capped at the broker
position, which on a netted account is another deployment's share (``_alert_ledger_mismatch``
says exactly this).

These tests pin the fix for the fill kinds this PR covers: the shared insert path that books a
broker-side close against the parent signal's *deployment* (the ledger signs come from the
parent signal's action, so the close is carried by a signal of the reducing side), and the
Alpaca bracket-leg path - the trade-update stream, a redelivery, and the reconciliation backfill
that runs on connect.

Every test marked SAFE passes on the unmodified code and pins behaviour that must survive the
change; the rest reproduce the defect.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from live_runner.alpaca_adapter import AlpacaLiveAdapter
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

ACCOUNT = "fixture-account"
ACCOUNT_ID = "alpaca-144"
# Deterministic uuids: the close signal's id is derived from the parent signal's id, so a test
# that pins it stays readable.
DEP_ENTRY = "11111111-1111-4111-8111-aaaaaaaaaaaa"
DEP_OTHER = "22222222-2222-4222-8222-bbbbbbbbbbbb"
PARENT_ORDER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
STOP_LEG_ID = "11111111-2222-4333-8444-555555555555"
T0 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(isolated_db):
    db = isolated_db[0]
    seed_live_deployments(db, [DEP_ENTRY, DEP_OTHER])
    return db


# ─── fixtures / helpers ───────────────────────────────────────────────────────────────────────


def _persist_entry(
    database,
    *,
    deployment_id: str = DEP_ENTRY,
    symbol: str = "AAPL",
    action: str = "buy",
    qty: float = 10.0,
    broker_order_id: str = PARENT_ORDER_ID,
) -> str:
    """One entry signal of one deployment, already filled at the broker and recorded."""
    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        theoretical_price=100.0,
        timestamp=T0,
        # The broker order id lives in the metadata: `LiveSignalModel` reads it from there.
        metadata={"broker_order_id": broker_order_id},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    assert database.save_live_execution(
        LiveExecution(
            signal_id=UUID(signal_id),
            actual_price=100.0,
            actual_price_observed="observed",
            filled_qty=qty,
            commission=0.0,
            timestamp=T0,
            broker_order_id=broker_order_id,
            dedupe_key=f"alpaca:order:{broker_order_id}:fill:cum:{qty:.8f}",
        )
    )
    return signal_id


def _leg(*, order_id: str = STOP_LEG_ID, side: str = "sell", order_type: str = "stop",
         filled_qty: float = 10.0, status: str = "filled") -> SimpleNamespace:
    """A bracket leg as Alpaca reports it nested in the parent order."""
    return SimpleNamespace(
        id=order_id,
        client_order_id=f"leg-client-{order_id}",
        symbol="AAPL",
        side=side,
        order_type=order_type,
        qty=filled_qty,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=95.0,
        filled_at=T1,
        updated_at=T1,
    )


def _parent_order(*, legs: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=PARENT_ORDER_ID,
        client_order_id="",  # reconciled by client id, so the field is not echoed back
        symbol="AAPL",
        side="buy",
        order_type="market",
        qty=10.0,
        status="filled",
        filled_qty=10.0,
        filled_avg_price=100.0,
        filled_at=T0,
        updated_at=T0,
        legs=list(legs or []),
    )


class _FakeTradingClient:
    """The order lookups reconciliation uses, recording how the parent was asked for.

    ``orders_by_client_id`` maps a signal id (the order's ``client_order_id``) to the
    order object; ``get_order_by_id`` resolves the same orders by their broker ``id``
    and records the filter it was given, so a test can assert the nested-legs request.
    """

    def __init__(self, orders_by_client_id: dict[str, Any]) -> None:
        self._orders = dict(orders_by_client_id)
        self.get_order_by_id_calls: list[tuple[str, Any]] = []
        self.get_order_by_client_id_calls: list[str] = []

    def _by_broker_id(self, order_id: str) -> Any:
        for order in self._orders.values():
            if str(getattr(order, "id", "")) == str(order_id):
                return order
        raise KeyError(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Any:
        self.get_order_by_client_id_calls.append(client_order_id)
        return self._orders[client_order_id]

    def get_order_by_id(self, order_id: Any, filter: Any = None) -> Any:
        self.get_order_by_id_calls.append((str(order_id), filter))
        return self._by_broker_id(str(order_id))


def _alpaca_adapter(
    orders_by_client_id: dict[str, Any] | None = None, *, client: Any | None = None
) -> AlpacaLiveAdapter:
    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id=ACCOUNT)
    adapter._connected = True
    adapter._trading_client = (
        client
        if client is not None
        else _FakeTradingClient(orders_by_client_id or {})
    )
    return adapter


def _leg_fill_update(
    *,
    leg_id: str = STOP_LEG_ID,
    qty: float = 10.0,
    cumulative: float | None = None,
    event: str = "fill",
    execution_id: str = "exec-leg-1",
    client_order_id: str | None = None,
    side: str = "sell",
    price: float = 95.0,
) -> SimpleNamespace:
    """A trade update for a bracket leg's fill.

    ``client_order_id`` is what the broker puts on the leg order: Alpaca gives a child order its
    own id, and a leg that inherits the parent's client order id names the parent signal - both
    shapes reach this path, and neither may be booked against the parent signal itself.
    ``cumulative`` is the leg order's ``filled_qty`` at the event - the broker's running total,
    which differs from the event's own ``qty`` on every partial fill.
    """
    cumulative = qty if cumulative is None else cumulative
    return SimpleNamespace(
        event=event,
        qty=qty,
        price=price,
        fill_price=price,
        timestamp=T1,
        execution_id=execution_id,
        order=SimpleNamespace(
            id=leg_id,
            client_order_id=client_order_id if client_order_id is not None else f"leg-{leg_id}",
            symbol="AAPL",
            side=side,
            order_type="stop",
            qty=qty,
            status="filled",
            filled_qty=cumulative,
            filled_avg_price=price,
            filled_at=T1,
            updated_at=T1,
            commission=None,
        ),
    )


def _close_fill_kwargs(
    database,
    parent_signal_id: str,
    *,
    qty: float = 10.0,
    dedupe_key: str = "alpaca:order:fill:1",
    close_side: str = "sell",
    reason: str = "alpaca_bracket_stop",
    broker_order_id: str = STOP_LEG_ID,
    timestamp: datetime | None = T1,
) -> dict[str, Any]:
    return dict(
        parent_signal_id=parent_signal_id,
        close_reason=reason,
        filled_qty=qty,
        actual_price=95.0,
        actual_price_observed="observed",
        commission=0.0,
        timestamp=timestamp,
        broker_order_id=broker_order_id,
        dedupe_key=dedupe_key,
        close_side=close_side,
    )


def _ledger(database, deployment_id: str, symbol: str = "AAPL") -> float:
    return database.get_live_deployment_net_filled_qty(deployment_id, symbol)


def _entry_signal() -> LiveSignal:
    """A bracket entry signal of the kind every Alpaca entry carries."""
    return LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="buy",
        theoretical_price=200.0,
        timestamp=T0,
        metadata={"stop_loss": 195.0, "take_profit": 210.0},
    )


def _executions(database) -> list[tuple[str, str | None, float]]:
    import sqlite3

    connection = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        return list(
            connection.execute(
                "SELECT signal_id, dedupe_key, filled_qty FROM live_executions ORDER BY rowid"
            )
        )
    finally:
        connection.close()


# ─── the shared insert path ───────────────────────────────────────────────────────────────────


def test_a_bracket_stop_fill_reduces_the_deployment_ledger_to_zero(database) -> None:
    """The acceptance criterion of #144, at the ledger.

    A bought 10 and Alpaca's stop leg closed them. The deployment's ledger must read 0, not 10,
    and the close must be attributed to the deployment that owned the position."""
    parent = _persist_entry(database)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent)
    )

    assert persisted == pytest.approx(10.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    account_ledgers = database.get_account_net_filled_qty_by_deployment(ACCOUNT, "AAPL")
    assert account_ledgers == {DEP_ENTRY: pytest.approx(0.0)}


def test_the_close_signal_is_the_parents_own_deployment_and_reducing_side(database) -> None:
    """The close is carried by a signal of the parent's deployment with the reducing action.

    The ledger signs fills by the parent signal's action, so a close booked against the entry
    signal itself would *add* to the long side. It also must never reach the replay path, which
    only replays internal/discord signals - a close that already happened cannot be reordered.
    """
    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(**_close_fill_kwargs(database, parent))

    close_signal_id = database.broker_close_signal_id(parent)
    metadata = database.get_live_signal_metadata(close_signal_id)
    assert metadata is not None
    assert metadata["broker_side_close"] is True
    assert metadata["broker_close_reason"] == "alpaca_bracket_stop"
    assert metadata["parent_signal_id"] == parent
    with database.get_session() as session:
        row = session.get(database.LiveSignalModel, close_signal_id)
    assert row is not None
    assert row.deployment_id == DEP_ENTRY
    assert row.action == "sell"
    assert row.symbol == "AAPL"
    assert row.source == "broker_close"
    assert [signal["signal_id"] for signal in database.list_live_signals_for_replay(
        deployment_ids=[DEP_ENTRY], source="internal"
    )] == [parent]


def test_a_redelivered_close_fill_is_stored_once(database) -> None:
    """The stream redelivers, and reconciliation re-reads the leg after a reconnect.

    Same broker fill, same broker-native dedupe key: one row, and the ledger must not fall twice.
    """
    parent = _persist_entry(database)
    kwargs = _close_fill_kwargs(database, parent)

    first = database.record_live_broker_side_close_fill(**kwargs)
    second = database.record_live_broker_side_close_fill(**kwargs)

    assert first == pytest.approx(10.0)
    assert second == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert len(_executions(database)) == 2  # the entry fill plus the one close fill


def test_two_partial_closes_of_the_same_leg_both_count(database) -> None:
    """A leg filled in two slices: both slices reduce the ledger, once."""
    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=4.0, dedupe_key="alpaca:order:leg:cum:4")
    )
    database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=6.0, dedupe_key="alpaca:order:leg:cum:10")
    )

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)


def test_a_close_of_a_short_entry_is_a_buy(database) -> None:
    """The reducing side follows the parent's action, not the close's own order."""
    parent = _persist_entry(database, action="sell", qty=8.0, broker_order_id="order-short")
    assert _ledger(database, DEP_ENTRY) == pytest.approx(-8.0)

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(
            database,
            parent,
            qty=8.0,
            close_side="buy",
            reason="alpaca_bracket_stop",
            broker_order_id="order-short",
        )
    )

    assert persisted == pytest.approx(8.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)


def test_a_close_that_would_extend_the_position_is_refused(database) -> None:
    """A fill on the wrong side of the parent must never be booked as a close.

    Recording it would move the ledger the wrong way and let a later exit size against exposure
    that does not exist."""
    parent = _persist_entry(database)

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, close_side="buy")
    )

    assert persisted == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)


def test_a_close_of_an_unknown_parent_signal_is_refused(database) -> None:
    """An unattributable fill is never invented an owner: it is logged and dropped."""
    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, str(uuid4()))
    )

    assert persisted == pytest.approx(0.0)
    assert _executions(database) == []


def test_a_close_without_a_timestamp_is_refused(database) -> None:
    """The execution ledger is ordered by time; a fabricated time could not be corrected."""
    parent = _persist_entry(database)

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, timestamp=None)
    )

    assert persisted == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)


@pytest.mark.parametrize("qty", [0.0, -3.0, float("nan"), float("inf")])
def test_a_close_without_a_usable_quantity_is_refused(database, qty: float) -> None:
    parent = _persist_entry(database)

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=qty)
    )

    assert persisted == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)


def test_a_close_dedupe_key_owned_by_another_signal_is_never_stolen(database, caplog) -> None:
    """The unique key is the table-wide identity of a fill (#127): a close may not annex it."""
    parent = _persist_entry(database)
    other = _persist_entry(database, deployment_id=DEP_OTHER, broker_order_id="order-other")
    database.save_live_execution(
        LiveExecution(
            signal_id=UUID(other),
            actual_price=95.0,
            actual_price_observed="observed",
            filled_qty=1.0,
            commission=0.0,
            timestamp=T1,
            broker_order_id="order-other",
            dedupe_key="alpaca:order:fill:1",
        )
    )

    with caplog.at_level(logging.ERROR):
        persisted = database.record_live_broker_side_close_fill(
            **_close_fill_kwargs(database, parent)
        )

    assert persisted == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)
    # The other deployment's own fills are untouched: its entry 10 plus the row that owns the key.
    assert _ledger(database, DEP_OTHER) == pytest.approx(11.0)
    close_signal_id = database.broker_close_signal_id(parent)
    assert [row for row in _executions(database) if row[0] == close_signal_id] == []
    assert any(
        "Dedupe key collision across signals" in record.getMessage() for record in caplog.records
    )


def test_the_entry_fills_own_signal_is_untouched_by_a_close(database) -> None:
    """SAFE: the entry fill and its signal row survive the close booking unchanged."""
    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(**_close_fill_kwargs(database, parent))

    assert database.get_live_signal_filled_qty(parent) == pytest.approx(10.0)
    entry_rows = [row for row in _executions(database) if row[0] == parent]
    assert entry_rows == [(parent, f"alpaca:order:{PARENT_ORDER_ID}:fill:cum:10.00000000", 10.0)]


# ─── the Alpaca bracket-leg path ──────────────────────────────────────────────────────────────


def test_a_leg_fill_on_the_stream_reduces_the_deployment_ledger(database, monkeypatch) -> None:
    """The bracket's stop leg filled: the fill arrives on the trade-update stream.

    Before #144 the leg's fill named no signal of ours, so it was dropped
    ("Trade update client_order_id is not a UUID") and the ledger kept the shares.
    """
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    asyncio.run(adapter.reconcile_executions())  # learns the leg -> parent mapping on connect

    asyncio.run(adapter._on_trade_update(_leg_fill_update()))

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    close_rows = [
        row for row in _executions(database) if row[0] == database.broker_close_signal_id(parent)
    ]
    # Keyed on the leg's own broker order id and its cumulative quantity: the reconciliation
    # pass derives the same key for the same fill, so a redelivery is a skip there too.
    assert [(row[1], row[2]) for row in close_rows] == [
        (f"alpaca:order:{STOP_LEG_ID}:leg:cum:10.00000000", 10.0)
    ]


def test_a_submitted_bracket_teaches_the_adapter_its_legs(database, monkeypatch) -> None:
    """The submit response carries the bracket's legs, so a leg's fill is attributed from then on.

    Without this the mapping exists only after the first reconciliation pass, and a leg that fills
    before then is dropped as unattributable.
    """
    from live_runner import alpaca_adapter as alpaca_module

    canonical_signal_id = str(uuid4())
    monkeypatch.setattr(
        alpaca_module, "save_live_signal", lambda _signal: (canonical_signal_id, True)
    )
    monkeypatch.setattr(
        alpaca_module, "set_live_signal_broker_order_id", lambda *_a, **_k: True
    )
    adapter = _alpaca_adapter(
        client=SimpleNamespace(
            submit_order=lambda order_data: _parent_order(legs=[_leg(), _leg(
                order_id="99999999-8888-4777-8666-555555555555", order_type="limit"
            )])
        )
    )

    assert asyncio.run(adapter.submit_order(_entry_signal(), qty=1.0)) == PARENT_ORDER_ID

    assert adapter._bracket_leg_order_id_to_signal_id == {
        STOP_LEG_ID: canonical_signal_id,
        "99999999-8888-4777-8666-555555555555": canonical_signal_id,
    }


def test_a_leg_inheriting_the_parents_client_order_id_is_not_a_second_entry_fill(
    database,
) -> None:
    """A leg may carry the parent's client order id.

    Booking that against the parent signal would ADD the leg's quantity to the long side (the
    ledger signs by the parent signal's action) and leave the deployment 20 long.
    """
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    asyncio.run(adapter.reconcile_executions())

    asyncio.run(
        adapter._on_trade_update(_leg_fill_update(client_order_id=parent, execution_id="exec-2"))
    )

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert database.get_live_signal_filled_qty(parent) == pytest.approx(10.0)


def test_a_redelivered_leg_fill_is_stored_once(database) -> None:
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    asyncio.run(adapter.reconcile_executions())
    update = _leg_fill_update()

    asyncio.run(adapter._on_trade_update(update))
    asyncio.run(adapter._on_trade_update(update))
    asyncio.run(adapter.reconcile_executions())  # and again after a reconnect

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    close_rows = [
        row for row in _executions(database) if row[0] == database.broker_close_signal_id(parent)
    ]
    assert len(close_rows) == 1


def test_reconciliation_backfills_the_leg_fill_the_stream_missed(database) -> None:
    """The daemon was down when the stop filled: the next connect backfills it.

    The leg's broker cumulative is the authority; only the slice still missing is persisted, so a
    second pass adds nothing.
    """
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})

    assert asyncio.run(adapter.reconcile_executions()) >= 1
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)

    assert asyncio.run(adapter.reconcile_executions()) == 0
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    close_rows = [
        row for row in _executions(database) if row[0] == database.broker_close_signal_id(parent)
    ]
    assert [row[2] for row in close_rows] == [10.0]


def test_a_partial_leg_fill_is_backfilled_up_to_the_leg_cumulative(database) -> None:
    """The stream stored the first slice only; reconciliation adds exactly the rest."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    # Learned at submit (and at every reconcile), so a fill of the leg can reach the stream first.
    adapter._register_bracket_legs(parent, _parent_order(legs=[_leg()]))
    asyncio.run(adapter._on_trade_update(_leg_fill_update(qty=4.0, execution_id="exec-partial")))
    assert _ledger(database, DEP_ENTRY) == pytest.approx(6.0)

    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    close_rows = [
        row for row in _executions(database) if row[0] == database.broker_close_signal_id(parent)
    ]
    assert sorted(row[2] for row in close_rows) == [4.0, 6.0]


def test_a_leg_on_the_extending_side_is_not_booked_as_a_close(database, caplog) -> None:
    """A leg that would extend the position is refused, never recorded."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg(side="buy")])})
    asyncio.run(adapter.reconcile_executions())

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._on_trade_update(_leg_fill_update(side="buy")))
        asyncio.run(adapter.reconcile_executions())

    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)
    assert [
        record for record in caplog.records if "reduces it" in record.getMessage()
    ]


def test_a_leg_of_an_order_that_is_not_ours_is_dropped(database, caplog) -> None:
    """SAFE: a fill for an order this deployment never submitted still changes nothing."""
    parent = _persist_entry(database)
    # The parent's stop leg is still working: nothing of this signal has closed yet.
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg(filled_qty=0.0, status="held")])})
    asyncio.run(adapter.reconcile_executions())
    # Another account's order fills: its client order id is not a signal and its order id was
    # never registered as a leg of one of ours.
    foreign = _leg_fill_update(leg_id=str(uuid4()), client_order_id="someone-elses-order")

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._on_trade_update(foreign))

    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)
    assert any("not a UUID" in record.getMessage() for record in caplog.records)
    close_signal_id = database.broker_close_signal_id(parent)
    assert [row for row in _executions(database) if row[0] == close_signal_id] == []


def test_a_still_open_leg_is_not_backfilled(database) -> None:
    """SAFE: a leg that has not filled closes nothing yet."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter(
        {parent: _parent_order(legs=[_leg(filled_qty=0.0, status="held")])}
    )

    assert asyncio.run(adapter.reconcile_executions()) == 0
    assert _ledger(database, DEP_ENTRY) == pytest.approx(10.0)


# ─── what the exit does afterwards (the issue's acceptance criterion) ─────────────────────────


def _deployment(deployment_id: str) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=UUID(deployment_id),
        broker="alpaca",
        strategy_name="fixture",
        account_id=ACCOUNT,
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
    )


class _ExitAdapter:
    """The minimum the exit sizing reads from a broker adapter."""

    def __init__(self, positions: list[dict[str, float]]) -> None:
        self.positions = positions

    async def get_positions(self) -> list[dict[str, float]]:
        return list(self.positions)

    def position_qty_tolerance(self, symbol: str) -> float:
        return 0.0

    def exit_qty_step(self, symbol: str) -> float:
        return 1e-9


def _exit_signal() -> LiveSignal:
    from live_runner.exit_signals import ExitInstruction, exit_intent_metadata

    instruction = ExitInstruction(fraction=1.0, position_side="long")
    return LiveSignal(
        deployment_id=DEP_OTHER,
        symbol="AAPL",
        action="sell",
        theoretical_price=95.0,
        timestamp=T1,
        metadata=exit_intent_metadata(instruction),
    )


def _prepare_exit(adapter: _ExitAdapter, deployment_id: str) -> tuple[LiveSignal, float]:
    import live_runner.daemon as daemon

    prepared, qty = asyncio.run(
        daemon.prepare_exit_intent_order(
            adapter, _deployment(deployment_id), _exit_signal()
        )
    )
    return prepared, qty


def test_after_a_bracket_stop_fill_no_exit_is_mis_sized_and_none_alerts(
    database, caplog
) -> None:
    """The acceptance criterion of #144, end to end.

    A and B each hold 10 shares of one netted broker position (20). A's bracket stop closed
    A's 10, so the broker holds only B's 10. An exit message fans out to both deployments:

    * for A there is nothing of its own to close - ignored, and B's shares stay open. On the
      unfixed code A's ledger still says 10, so the exit is capped at the broker position and
      closes 10 shares that belong to B;
    * for B the ledgers add up to the broker position again, so its exit is sized to its own 10
      with no `exit_ledger_mismatch` alert.
    """
    parent_a = _persist_entry(database, deployment_id=DEP_ENTRY, qty=10.0)
    _persist_entry(
        database, deployment_id=DEP_OTHER, qty=10.0, broker_order_id="order-b"
    )
    adapter = _alpaca_adapter({parent_a: _parent_order(legs=[_leg()])})
    assert asyncio.run(adapter.reconcile_executions()) >= 1
    assert database.get_account_net_filled_qty_by_deployment(ACCOUNT, "AAPL") == {
        DEP_ENTRY: pytest.approx(0.0),
        DEP_OTHER: pytest.approx(10.0),
    }

    exit_adapter = _ExitAdapter([{"symbol": "AAPL", "qty": 10.0}])
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception) as own_exit:
            _prepare_exit(exit_adapter, DEP_ENTRY)
        prepared, qty = _prepare_exit(exit_adapter, DEP_OTHER)

    assert type(own_exit.value).__name__ == "NothingToExitError"
    assert "no own quantity" in str(own_exit.value)
    assert prepared.action == "sell"
    assert qty == pytest.approx(10.0)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert prepared.metadata["exit_ledger_qty"] == pytest.approx(10.0)


def test_without_the_close_booking_the_same_exit_alerts_and_takes_the_other_share(
    database, caplog
) -> None:
    """SAFE on the unfixed code: pins the defect the fix removes (stale ledger, netted account).

    Same book, but the stop leg's fill was never recorded: the ledgers add up to 20 while the
    broker holds 10, so the alert fires and A's exit closes B's shares.
    """
    _persist_entry(database, deployment_id=DEP_ENTRY, qty=10.0)
    _persist_entry(
        database, deployment_id=DEP_OTHER, qty=10.0, broker_order_id="order-b"
    )

    exit_adapter = _ExitAdapter([{"symbol": "AAPL", "qty": 10.0}])
    with caplog.at_level(logging.ERROR):
        prepared, qty = _prepare_exit(exit_adapter, DEP_ENTRY)

    assert qty == pytest.approx(10.0)
    assert prepared.metadata["exit_ledger_qty"] == pytest.approx(10.0)
    alerts = [
        record
        for record in caplog.records
        if "exit_ledger_mismatch" in str(getattr(record, "data", {}))
    ]
    assert len(alerts) == 1


def test_a_ledger_that_matches_the_broker_again_raises_no_alert(database, caplog) -> None:
    """SAFE: with the close booked the guard sees ledgers == broker position."""
    parent_a = _persist_entry(database, deployment_id=DEP_ENTRY, qty=10.0)
    _persist_entry(
        database, deployment_id=DEP_OTHER, qty=10.0, broker_order_id="order-b"
    )
    asyncio.run(
        _alpaca_adapter({parent_a: _parent_order(legs=[_leg()])}).reconcile_executions()
    )

    exit_adapter = _ExitAdapter([{"symbol": "AAPL", "qty": 10.0}])
    with caplog.at_level(logging.ERROR):
        _prepared, qty = _prepare_exit(exit_adapter, DEP_OTHER)

    assert qty == pytest.approx(10.0)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


# ─── review round: restart, trailing, caps and prices ─────────────────────────────────────────

TRAIL_LEG_ID = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
TARGET_LEG_ID = "88888888-9999-4aaa-8bbb-cccccccccccc"


def _close_execution_rows(database, parent_signal_id: str) -> list[tuple]:
    """``(filled_qty, actual_price, actual_price_observed)`` of the close signal's fills."""
    import sqlite3

    connection = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        return list(
            connection.execute(
                "SELECT filled_qty, actual_price, actual_price_observed "
                "FROM live_executions WHERE signal_id = ? ORDER BY rowid",
                (database.broker_close_signal_id(parent_signal_id),),
            )
        )
    finally:
        connection.close()


def test_reconcile_fetches_the_parent_with_nested_legs(database) -> None:
    """The restart path must ask for the legs: by-client-id returns none (#144 review).

    alpaca-py 0.44.0's ``get_order_by_client_id`` takes no ``nested`` option and the runbook
    records that a bracket's legs are only reported nested in the parent - so without the
    by-id nested fetch the leg -> signal map is never re-learned after a restart and leg
    fills are dropped again.
    """
    parent = _persist_entry(database)
    client = _FakeTradingClient({parent: _parent_order(legs=[_leg()])})
    adapter = _alpaca_adapter(client=client)

    assert asyncio.run(adapter.reconcile_executions()) >= 1

    (order_id, request), = client.get_order_by_id_calls
    assert order_id == PARENT_ORDER_ID
    assert request.nested is True
    assert client.get_order_by_client_id_calls == []
    assert adapter._bracket_leg_order_id_to_signal_id[STOP_LEG_ID] == parent
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)


def test_reconcile_falls_back_to_the_client_id_fetch_without_a_stored_order_id(
    database,
) -> None:
    """A signal persisted before its broker order id was stored still reconciles."""
    signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=T0,
        metadata={},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    client = _FakeTradingClient({signal_id: _parent_order()})
    adapter = _alpaca_adapter(client=client)

    assert asyncio.run(adapter.reconcile_executions()) >= 1

    assert client.get_order_by_id_calls == []
    assert client.get_order_by_client_id_calls == [signal_id]


def test_a_trailed_stop_fill_reduces_the_ledger(database) -> None:
    """A trailed stop's replacement is a NEW leg order id; its fill must still book (#144).

    ``amend_protective_stop`` replaces the order, so the leg id learned at submit is stale
    from that moment. The adapter re-learns the new id against the same parent signal at
    once rather than waiting for the next connect.
    """
    from live_runner.base_adapter import ProtectiveStop

    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    asyncio.run(adapter.reconcile_executions())
    assert adapter._bracket_leg_order_id_to_signal_id[STOP_LEG_ID] == parent

    replaced = SimpleNamespace(id=TRAIL_LEG_ID, stop_price=96.5)
    adapter._trading_client.replace_order_by_id = (
        lambda order_id, order_data: replaced
    )
    stop = ProtectiveStop(
        order_id=STOP_LEG_ID, symbol="AAPL", side="sell", stop_price=95.0, qty=10.0
    )
    amended = asyncio.run(adapter.amend_protective_stop(stop, 96.5))

    assert amended.order_id == TRAIL_LEG_ID
    assert STOP_LEG_ID not in adapter._bracket_leg_order_id_to_signal_id
    assert adapter._bracket_leg_order_id_to_signal_id[TRAIL_LEG_ID] == parent

    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(leg_id=TRAIL_LEG_ID, execution_id="exec-trailed")
        )
    )

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)


def test_a_leg_still_open_at_connect_is_learned_and_its_later_fill_books(database) -> None:
    """The restart case: the leg was working when the adapter connected, fills later."""
    parent = _persist_entry(database)
    client = _FakeTradingClient(
        {parent: _parent_order(legs=[_leg(filled_qty=0.0, status="held")])}
    )
    adapter = _alpaca_adapter(client=client)

    assert asyncio.run(adapter.reconcile_executions()) == 0
    assert adapter._bracket_leg_order_id_to_signal_id[STOP_LEG_ID] == parent

    asyncio.run(adapter._on_trade_update(_leg_fill_update()))

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)


def test_sibling_legs_book_against_their_own_cumulative(database) -> None:
    """Stop and target legs share one close signal; each leg's cumulative is its own (#144)."""
    parent = _persist_entry(database)
    legs = [
        _leg(filled_qty=4.0),
        _leg(order_id=TARGET_LEG_ID, order_type="limit", filled_qty=6.0),
    ]
    adapter = _alpaca_adapter({parent: _parent_order(legs=legs)})

    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert sorted(row[0] for row in _close_execution_rows(database, parent)) == [4.0, 6.0]


def test_a_second_partial_leg_event_books_only_its_own_slice(database) -> None:
    """Two partial fills of one leg: the event quantity, never the cumulative, is booked."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    adapter._register_bracket_legs(parent, _parent_order(legs=[_leg()]))

    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(
                qty=4.0, cumulative=4.0, event="partial_fill", execution_id=""
            )
        )
    )
    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(qty=6.0, cumulative=10.0, event="fill", execution_id="")
        )
    )
    # A redelivery of the first slice is a skip, not a third row.
    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(
                qty=4.0, cumulative=4.0, event="partial_fill", execution_id=""
            )
        )
    )

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert [row[0] for row in _close_execution_rows(database, parent)] == [4.0, 6.0]


def test_a_partially_filled_leg_is_backfilled_up_to_its_cumulative(database) -> None:
    """A leg caught mid-fill reconciles its slice, then the rest once it completes."""
    parent = _persist_entry(database)
    partial = _alpaca_adapter(
        {parent: _parent_order(legs=[_leg(filled_qty=4.0, status="partially_filled")])}
    )
    assert asyncio.run(partial.reconcile_executions()) >= 1
    assert _ledger(database, DEP_ENTRY) == pytest.approx(6.0)

    complete = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    assert asyncio.run(complete.reconcile_executions()) >= 1

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert [row[0] for row in _close_execution_rows(database, parent)] == [4.0, 6.0]


def test_a_reconciled_leg_close_records_a_usable_price(database) -> None:
    """The fill price of a reconciled close is derived, never stored as 0.0 (#144)."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})

    asyncio.run(adapter.reconcile_executions())

    (qty, price, observed), = _close_execution_rows(database, parent)
    assert qty == pytest.approx(10.0)
    assert price == pytest.approx(95.0)
    assert observed == "derived"


def test_a_partial_leg_backfill_derives_the_remainder_price(database) -> None:
    """A backfill covering only part of the leg prices the missing slice, not 0.0.

    The leg's ``filled_avg_price`` is the average over its whole cumulative: the first
    slice was already recorded at 90, the leg's average is 95 over 10, so the missing 6
    derive to (95*10 - 90*4) / 6.
    """
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    adapter._register_bracket_legs(parent, _parent_order(legs=[_leg()]))
    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(
                qty=4.0, cumulative=4.0, event="partial_fill", price=90.0,
                execution_id="",
            )
        )
    )
    assert _ledger(database, DEP_ENTRY) == pytest.approx(6.0)

    asyncio.run(adapter.reconcile_executions())

    rows = _close_execution_rows(database, parent)
    assert [row[0] for row in rows] == [4.0, pytest.approx(6.0)]
    assert rows[0][1:] == (90.0, "observed")
    assert rows[1][1] == pytest.approx((95.0 * 10.0 - 90.0 * 4.0) / 6.0)
    assert rows[1][2] == "derived"


def test_a_delayed_leg_event_after_reconcile_is_not_double_counted(database) -> None:
    """A stale stream event whose slice reconcile already covered is skipped by the cap."""
    parent = _persist_entry(database)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[_leg()])})
    asyncio.run(adapter.reconcile_executions())
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)

    # The event snapshot predates the reconcile: its cumulative (4) is below what is
    # already recorded for the leg (10), so nothing may be booked.
    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(
                qty=4.0, cumulative=4.0, event="partial_fill", execution_id=""
            )
        )
    )

    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert [row[0] for row in _close_execution_rows(database, parent)] == [10.0]


def test_reconcile_books_no_leg_while_the_parent_fill_is_unrecoverable(database) -> None:
    """A leg closes the position the parent's fill opened: with the parent's own
    fill unbookable (no usable ``filled_avg_price``), no leg close may be booked
    on that pass - the ledger would go below zero (#144 review survivor)."""
    parent = _persist_entry(database, qty=5.0)  # only half the fill is recorded
    order = _parent_order(legs=[_leg(filled_qty=10.0)])
    order.filled_qty = 10.0
    order.filled_avg_price = None
    adapter = _alpaca_adapter({parent: order})

    assert asyncio.run(adapter.reconcile_executions()) == 0

    assert _close_execution_rows(database, parent) == []
    assert _ledger(database, DEP_ENTRY) == pytest.approx(5.0)


def test_a_leg_stream_event_is_capped_at_the_leg_cumulative(database) -> None:
    """The event's own quantity is not the leg's remaining quantity (#144 review
    survivor): 4 of the leg's 6 already recorded, an event claiming 6 books 2."""
    parent = _persist_entry(database)
    leg_order = _leg(filled_qty=6.0)
    adapter = _alpaca_adapter({parent: _parent_order(legs=[leg_order])})
    adapter._register_bracket_legs(parent, _parent_order(legs=[leg_order]))

    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(
                qty=4.0, cumulative=4.0, event="partial_fill", execution_id=""
            )
        )
    )
    asyncio.run(
        adapter._on_trade_update(
            _leg_fill_update(qty=6.0, cumulative=6.0, event="fill", execution_id="")
        )
    )

    # The leg filled 6 in total: 4 + the capped 2, never 4 + 6.
    assert _ledger(database, DEP_ENTRY) == pytest.approx(4.0)
    assert [row[0] for row in _close_execution_rows(database, parent)] == [4.0, 2.0]


def test_a_close_is_capped_at_the_parent_signal_s_unclosed_quantity(database) -> None:
    """No double-booked fill may drive the ledger below zero (#144 review).

    Two distinct dedupe keys for closes of one 10-share parent: the second is capped at
    what remains unclosed, so the ledger ends at exactly 0 rather than -3.
    """
    parent = _persist_entry(database)

    first = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=7.0, dedupe_key="k:leg-a:7")
    )
    second = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=5.0, dedupe_key="k:leg-b:5")
    )
    third = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent, qty=1.0, dedupe_key="k:leg-c:1")
    )

    assert first == pytest.approx(7.0)
    assert second == pytest.approx(3.0)
    assert third == pytest.approx(0.0)
    assert _ledger(database, DEP_ENTRY) == pytest.approx(0.0)
    assert [row[0] for row in _close_execution_rows(database, parent)] == [7.0, 3.0]


def test_a_close_of_a_parent_with_no_position_sign_is_refused(database) -> None:
    """A parent whose action is neither buy nor sell cannot be closed against.

    ``LiveSignal`` validates action to buy/sell, so the row is written directly - the
    refusal exists for rows that predate or bypass that validation.
    """
    parent = str(uuid4())
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=parent,
                deployment_id=DEP_ENTRY,
                symbol="AAPL",
                action="hold",
                theoretical_price=100.0,
                timestamp=T0,
                source="internal",
            )
        )
        session.commit()

    persisted = database.record_live_broker_side_close_fill(
        **_close_fill_kwargs(database, parent)
    )

    assert persisted == pytest.approx(0.0)
    assert database.get_live_signal_metadata(database.broker_close_signal_id(parent)) is None


def test_a_broker_close_signal_does_not_displace_the_entry_for_trailing(database) -> None:
    """The close row is bookkeeping, not an entry: trailing must still find the entry (#144)."""
    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(**_close_fill_kwargs(database, parent))

    entries = database.find_latest_filled_live_entry_signals([DEP_ENTRY], ["AAPL"])

    assert entries["AAPL"]["signal_id"] == parent


def test_broker_close_fills_stay_out_of_the_trades_payload(database, monkeypatch) -> None:
    """The live page must not price a broker close against a 0.0 theoretical (#144).

    A 10-share stop at 95 would otherwise render as a -950 loss and pollute slippage.
    """
    import viewer.main as viewer_main

    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(**_close_fill_kwargs(database, parent))
    monkeypatch.setattr(
        viewer_main,
        "list_live_deployments_for_account",
        lambda _a: [SimpleNamespace(id=DEP_ENTRY, strategy_name="", account_id=ACCOUNT)],
    )

    payload = asyncio.run(viewer_main._live_trades_payload(ACCOUNT))

    assert [row["signal_id"] for row in payload["trades"]] == [parent]
    # The serializer still serves the close row (stats price it from actual prices) but
    # never a slippage against a theoretical price the close does not have.
    rows = database.list_live_signal_executions_for_deployments([DEP_ENTRY])
    close_rows = [row for row in rows if row["source"] == "broker_close"]
    assert len(close_rows) == 1
    assert close_rows[0]["slippage"] is None
    assert close_rows[0]["slippage_bps"] is None


def test_broker_close_rows_carry_their_own_source_and_fill_price(
    database, monkeypatch
) -> None:
    """The signals payload must not pass a broker close off as an internal
    signal at price 0.0: it gets its own source label and keeps the fill's
    ``actual_price`` for the chart marker (#144 review)."""
    import viewer.main as viewer_main

    parent = _persist_entry(database)
    database.record_live_broker_side_close_fill(**_close_fill_kwargs(database, parent))
    monkeypatch.setattr(
        viewer_main,
        "list_live_deployments_for_account",
        lambda _a: [SimpleNamespace(id=DEP_ENTRY, strategy_name="", account_id=ACCOUNT)],
    )

    payload = asyncio.run(viewer_main._live_signals_payload(ACCOUNT))

    by_id = {row["signal_id"]: row for row in payload["signals"]}
    close = by_id[database.broker_close_signal_id(parent)]
    assert close["source"] == "broker_close"
    assert close["theoretical_price"] == pytest.approx(0.0)
    assert close["actual_price"] == pytest.approx(95.0)
    assert by_id[parent]["source"] == "internal"

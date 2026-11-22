"""Issue #144 / PR #277 decision: book a cTrader broker-side position close.

A cTrader SL/TP fill - and, through the same chain, a flatten or a manual close -
is a deal this platform never submitted, so its execution event names no signal
of ours and used to be retained forever: the ledger kept counting shares the
broker position no longer held, and #142's guard refused every later exit.

cTrader gives an exact chain the Alpaca bracket does not need: ``deal ->
positionId -> ProtoOAOrderListByPositionId -> the opening order's
clientOrderId/orderId -> the persisted signal``. The close is booked against the
parent signal's deployment ledger through the same
``record_live_broker_side_close_fill`` path, but ONLY when the chain resolves to
exactly one signal on this account. Anything else - no opening order, several
opening orders (a netted book), an unresolvable fill - is NOT guessed into a
ledger: the account+symbol is flagged ``ledger_reconciliation_required`` in the
control audit trail and the event is retained for reconciliation, per the
maintainer decision on #277.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from live_runner.ctrader_adapter import CTraderLiveAdapter, SymbolScale
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

ACCOUNT = "ctrader-account-144"
DEP_ENTRY = "33333333-3333-4333-8333-cccccccccccc"
DEP_OTHER = "44444444-4444-4444-8444-dddddddddddd"
SYMBOL_ID = 101
POSITION_ID = 4242
ENTRY_ORDER_ID = 555
CLOSE_ORDER_ID = 777
PROTECTION_ORDER_ID = 888
T0 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)
T1 = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone.utc)
T1_MS = int(T1.timestamp() * 1000)


@pytest.fixture
def database(isolated_db):
    db = isolated_db[0]
    seed_live_deployments(db, [DEP_ENTRY, DEP_OTHER], account_id=ACCOUNT)
    return db


# ─── fixtures / helpers ───────────────────────────────────────────────────────────────────────


def _persist_entry(
    database,
    *,
    deployment_id: str = DEP_ENTRY,
    symbol: str = "AAPL",
    action: str = "buy",
    qty: float = 10.0,
    order_id: int = ENTRY_ORDER_ID,
) -> str:
    """One entry signal of one deployment, filled at the broker and recorded."""
    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        theoretical_price=100.0,
        timestamp=T0,
        metadata={"broker_order_id": str(order_id)},
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
            broker_order_id=str(order_id),
            dedupe_key=f"ctrader:test:entry:{signal_id}",
        )
    )
    return signal_id


def _entry_order(signal_id: str, *, order_id: int = ENTRY_ORDER_ID) -> SimpleNamespace:
    """The position's opening order as OrderListByPositionId reports it."""
    return SimpleNamespace(
        orderId=order_id,
        clientOrderId=str(signal_id),
        positionId=POSITION_ID,
        closingOrder=False,
        tradeSide=1,  # BUY
        orderType=1,
        orderStatus=2,
        symbolId=SYMBOL_ID,
        executedVolume=10.0,
        executionPrice=100.0,
        utcLastUpdateTimestamp=T0_MS,
    )


def _protection_order() -> SimpleNamespace:
    """The attached SL/TP order: on the position, but it names no signal."""
    return SimpleNamespace(
        orderId=PROTECTION_ORDER_ID,
        clientOrderId="",
        positionId=POSITION_ID,
        closingOrder=False,
        tradeSide=2,
        orderType=4,  # STOP_LOSS_TAKE_PROFIT
        orderStatus=1,
        symbolId=SYMBOL_ID,
        executedVolume=0.0,
        utcLastUpdateTimestamp=T0_MS,
    )


def _closing_order(*, order_id: int = CLOSE_ORDER_ID, volume: float = 10.0) -> SimpleNamespace:
    return SimpleNamespace(
        orderId=order_id,
        clientOrderId="",
        positionId=POSITION_ID,
        closingOrder=True,
        tradeSide=2,  # SELL closes the long
        orderType=1,
        orderStatus=2,
        symbolId=SYMBOL_ID,
        executedVolume=volume,
        executionPrice=95.0,
        utcLastUpdateTimestamp=T1_MS,
    )


def _close_deal(
    *,
    deal_id: int = 9001,
    order_id: int = CLOSE_ORDER_ID,
    volume: float = 10.0,
    timestamp_ms: int = T1_MS,
) -> SimpleNamespace:
    return SimpleNamespace(
        dealId=deal_id,
        orderId=order_id,
        positionId=POSITION_ID,
        symbolId=SYMBOL_ID,
        volume=volume,
        filledVolume=volume,
        dealStatus=2,  # FILLED
        tradeSide=2,  # SELL
        executionPrice=95.0,
        executionTimestamp=timestamp_ms,
        utcLastUpdateTimestamp=0,
        commission=0,
        closePositionDetail=SimpleNamespace(),
    )


def _event(order: Any, deal: Any, *, execution_type: int = 1) -> SimpleNamespace:
    return SimpleNamespace(executionType=execution_type, order=order, deal=deal)


class _OrderListReq:
    pass


class _OrderListRes:
    def __init__(self, orders: list[Any]) -> None:
        self.order = list(orders)


class _DealListReq:
    pass


class _DealListRes:
    def __init__(self, deals: list[Any]) -> None:
        self.deal = list(deals)
        self.hasMore = False


class _OrderDetailsReq:
    pass


class _OrderDetailsRes:
    def __init__(self, order: Any = None, deals: list[Any] | None = None) -> None:
        self.order = order
        self.deal = list(deals or [])


def _adapter(
    database,
    *,
    position_orders: dict[int, list[Any]] | None = None,
    deal_list: list[Any] | None = None,
    deal_list_has_more: bool = False,
    order_details: dict[int, Any] | None = None,
) -> CTraderLiveAdapter:
    """A cTrader adapter whose broker reads are faked, on the isolated DB."""
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="12345",
        account_config_id=ACCOUNT,
        demo=True,
    )
    adapter._symbol_name_to_id = {"AAPL": SYMBOL_ID}
    # volume_per_qty=1.0: broker volume units ARE qty units, so test quantities
    # read the same on both sides of the conversion.
    adapter._symbol_props["AAPL"] = SymbolScale(
        volume_per_qty=1.0, price_digits=2, min_volume=1, lot_size=100.0, source="test"
    )
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=2, PARTIALLY_FILLED=3)
    adapter._ProtoOATradeSide = SimpleNamespace(BUY=1, SELL=2)
    adapter._ProtoOAOrderListByPositionIdReq = _OrderListReq
    adapter._ProtoOAOrderListByPositionIdRes = _OrderListRes
    adapter._ProtoOADealListReq = _DealListReq
    adapter._ProtoOADealListRes = _DealListRes
    adapter._ProtoOAOrderDetailsReq = _OrderDetailsReq
    adapter._ProtoOAOrderDetailsRes = _OrderDetailsRes

    orders_by_position = dict(position_orders or {})
    details_by_order = dict(order_details or {})

    async def fake_send(request: Any, *, timeout_seconds: int = 10, _depth: int = 0) -> Any:
        if isinstance(request, _OrderListReq):
            return _OrderListRes(orders_by_position.get(int(request.positionId), []))
        if isinstance(request, _DealListReq):
            response = _DealListRes(list(deal_list or []))
            response.hasMore = deal_list_has_more
            return response
        if isinstance(request, _OrderDetailsReq):
            order = details_by_order.get(int(request.orderId))
            return _OrderDetailsRes(order=order, deals=[])
        raise AssertionError(f"unexpected broker request {type(request).__name__}")

    adapter._send_request_with_refresh_retry = fake_send  # type: ignore[assignment]
    return adapter


def _ledger(database, deployment_id: str = DEP_ENTRY, symbol: str = "AAPL") -> float:
    return float(database.get_live_deployment_net_filled_qty(deployment_id, symbol))


def _reconciliation_flags(database) -> list[dict[str, Any]]:
    return [
        row
        for row in database.list_live_control_audit(limit=200)
        if row["action"] == database.LEDGER_RECONCILIATION_AUDIT_ACTION
    ]


# ─── the stream path ──────────────────────────────────────────────────────────────────────────


def test_a_position_close_books_against_the_parent_deployment_ledger(database) -> None:
    """The issue's cTrader case: an SL/TP fill reduces the ledger to zero."""
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id), _protection_order()]},
    )

    asyncio.run(
        adapter._persist_execution_event(_event(_closing_order(), _close_deal()))
    )

    assert _ledger(database) == pytest.approx(0.0)
    assert _ledger(database, DEP_OTHER) == pytest.approx(0.0)


def test_the_close_lands_on_a_broker_close_signal_of_the_parents_deployment(database) -> None:
    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})

    asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    close_signal_id = db.broker_close_signal_id(signal_id)
    with db.get_session() as session:
        close_signal = session.get(db.LiveSignalModel, close_signal_id)
        executions = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == close_signal_id
            )
        ).all()
    assert close_signal is not None
    assert close_signal.deployment_id == DEP_ENTRY
    assert close_signal.action == "sell"  # the reducing side of a buy entry
    assert close_signal.source == db.BROKER_CLOSE_SIGNAL_SOURCE
    assert float(executions[0].filled_qty) == pytest.approx(10.0)
    assert float(executions[0].actual_price) == pytest.approx(95.0)


def test_a_sell_entry_is_closed_by_a_buy_deal(database) -> None:
    signal_id = _persist_entry(database, action="sell", qty=4.0)
    closing = _closing_order(volume=4.0)
    closing.tradeSide = 1  # BUY closes the short
    deal = _close_deal(volume=4.0)
    deal.tradeSide = 1
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})

    asyncio.run(adapter._persist_execution_event(_event(closing, deal)))

    assert _ledger(database) == pytest.approx(0.0)


def test_a_partial_close_leaves_the_remainder_in_the_ledger(database) -> None:
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})

    asyncio.run(
        adapter._persist_execution_event(
            _event(_closing_order(volume=4.0), _close_deal(deal_id=9001, volume=4.0))
        )
    )
    assert _ledger(database) == pytest.approx(6.0)

    asyncio.run(
        adapter._persist_execution_event(
            _event(
                _closing_order(order_id=778, volume=6.0),
                _close_deal(deal_id=9002, order_id=778, volume=6.0),
            )
        )
    )
    assert _ledger(database) == pytest.approx(0.0)


def test_a_redelivered_close_is_a_skip_not_a_second_close(database) -> None:
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})
    event = _event(_closing_order(), _close_deal())

    asyncio.run(adapter._persist_execution_event(event))
    asyncio.run(adapter._persist_execution_event(event))

    assert _ledger(database) == pytest.approx(0.0)
    import shared.database as db

    with db.get_session() as session:
        rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == db.broker_close_signal_id(signal_id)
            )
        ).all()
    assert len(rows) == 1


def test_a_close_resolved_by_broker_order_id_alone_books(database) -> None:
    """After a restart the in-memory maps are empty: the opening order's id is
    recovered through ``find_live_signal_id_by_broker_order_id``."""
    _persist_entry(database, qty=10.0)
    entry = _entry_order("")  # no clientOrderId echoed
    entry.clientOrderId = ""
    adapter = _adapter(database, position_orders={POSITION_ID: [entry]})

    asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _ledger(database) == pytest.approx(0.0)


# ─── flatten / manual / ambiguous: flag, never guess ──────────────────────────────────────────


def test_a_position_with_two_opening_orders_is_flagged_not_guessed(database) -> None:
    """A netted book: two opening orders name two signals, so no attribution."""
    first = _persist_entry(database, qty=6.0)
    second = _persist_entry(database, qty=4.0)
    adapter = _adapter(
        database,
        position_orders={
            POSITION_ID: [_entry_order(first), _entry_order(second, order_id=556)]
        },
    )

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _ledger(database) == pytest.approx(10.0)
    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["target_id"] == f"{ACCOUNT}:AAPL"
    assert flags[0]["detail"]["cause"] == "ambiguous_parent"


def test_a_close_of_a_position_no_signal_opened_is_flagged(database) -> None:
    """Manual position, or one opened before the platform: nothing to book to."""
    adapter = _adapter(database, position_orders={POSITION_ID: []})

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _reconciliation_flags(database)[0]["detail"]["cause"] == "no_opening_order"


def test_a_close_with_no_position_id_is_flagged(database) -> None:
    deal = _close_deal()
    deal.positionId = 0
    order = _closing_order()
    order.positionId = 0
    adapter = _adapter(database)

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(_event(order, deal)))

    assert _reconciliation_flags(database)[0]["detail"]["cause"] == "no_position_id"


def test_an_own_exit_fill_is_never_taken_for_a_broker_close(database) -> None:
    """Our ClosePositionReq orders persist their broker order id on the exit
    signal, so the normal path resolves them before the close path is tried."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="sell",
        theoretical_price=95.0,
        timestamp=T1,
        metadata={
            "broker_order_id": str(CLOSE_ORDER_ID),
            "signal_intent": "exit",
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
        },
    )
    exit_id, created = db.save_live_signal(exit_signal)
    assert created
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})

    asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    # The fill landed on the EXIT signal (sell, -10), not on a broker_close row:
    # net ledger is entry(+10) + exit(-10) = 0 either way, so count the rows.
    with db.get_session() as session:
        exit_rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == exit_id
            )
        ).all()
        close_rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == db.broker_close_signal_id(entry_id)
            )
        ).all()
    assert len(exit_rows) == 1
    assert exit_rows[0].dedupe_key == "ctrader:deal:9001"
    assert close_rows == []
    assert _reconciliation_flags(database) == []


# ─── the reconciliation backfill ──────────────────────────────────────────────────────────────


def _reconcilable_adapter(database, **kwargs) -> CTraderLiveAdapter:
    adapter = _adapter(database, **kwargs)
    adapter._client = object()
    adapter._authenticated = True
    return adapter


def test_reconcile_backfills_a_position_close_missed_while_down(database) -> None:
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[_close_deal()],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_skips_a_close_our_own_exit_order_made(database) -> None:
    """A deal whose order id is a persisted signal's broker order is ours:
    the per-signal reconcile books it, the broker-close pass must not."""
    import shared.database as db

    _persist_entry(database, qty=10.0)
    exit_signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="sell",
        theoretical_price=95.0,
        timestamp=T1,
        metadata={"broker_order_id": str(CLOSE_ORDER_ID)},
    )
    db.save_live_signal(exit_signal)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[_close_deal()],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    asyncio.run(adapter.reconcile_executions())

    # Nothing booked as a broker close; no flag either - the order is ours.
    assert _reconciliation_flags(database) == []
    entry_id = _entry_row_id(database)
    with db.get_session() as session:
        close_rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == db.broker_close_signal_id(entry_id)
            )
        ).all()
    assert close_rows == []
    assert _ledger(database) == pytest.approx(10.0)


def _entry_row_id(database) -> str:
    import shared.database as db

    with db.get_session() as session:
        row = session.exec(
            db.select(db.LiveSignalModel).where(db.LiveSignalModel.action == "buy")
        ).first()
    return str(row.id)


def test_reconcile_flags_an_unattributable_close_once_per_pass(database) -> None:
    _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[_close_deal()],
    )

    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)
    assert _reconciliation_flags(database)[0]["detail"]["cause"] == "no_opening_order"


def test_reconcile_flags_each_unattributable_deal_only_once(database) -> None:
    """The reconcile pass runs on every connect and every pending retry: one
    audit row and one alert per deal, not one per pass (#144 review)."""
    _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[_close_deal(deal_id=9001), _close_deal(deal_id=9002, order_id=778)],
    )

    for _ in range(3):
        asyncio.run(adapter.reconcile_executions())

    flags = _reconciliation_flags(database)
    assert sorted(flag["detail"]["deal_id"] for flag in flags) == [9001, 9002]


def test_a_closing_order_on_the_position_is_never_an_opener(database) -> None:
    """``closingOrder`` marks the reduce side: it must not count as a second
    opening signal, or every scaled position would read ambiguous (#144)."""
    first = _persist_entry(database, qty=10.0)
    second = _persist_entry(database, qty=5.0, order_id=556)
    closer = _entry_order(second, order_id=557)
    closer.closingOrder = True
    adapter = _adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(first), closer]},
    )

    asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _ledger(database) == pytest.approx(5.0)
    assert _reconciliation_flags(database) == []


def test_reconcile_ignores_a_deal_that_is_not_filled(database) -> None:
    """Only FILLED/PARTIALLY_FILLED deals carry volume worth booking (#144)."""
    signal_id = _persist_entry(database, qty=10.0)
    deal = _close_deal()
    deal.dealStatus = 1  # neither FILLED (2) nor PARTIALLY_FILLED (3)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal],
    )

    assert asyncio.run(adapter.reconcile_executions()) == 0

    assert _ledger(database) == pytest.approx(10.0)
    assert _reconciliation_flags(database) == []


def test_reconcile_warns_when_the_deal_list_reports_more_rows(database, caplog) -> None:
    """``maxRows`` truncates silently at the broker: a busy account's older
    closes are missed, so the truncation has to be loud (#144 review)."""
    import logging

    signal_id = _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[_close_deal()],
        deal_list_has_more=True,
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter.reconcile_executions())

    assert any("hasMore" in record.getMessage() for record in caplog.records)


def test_a_close_naming_another_accounts_signal_is_flagged_not_booked(database) -> None:
    """``clientOrderId`` is a bare UUID: it only counts when the signal it names
    lives on THIS account, or a close would reduce another ledger (#144)."""
    foreign_account = "ctrader-account-other"
    foreign_dep = "55555555-5555-4555-8555-eeeeeeeeeeee"
    seed_live_deployments(database, [foreign_dep], account_id=foreign_account)
    foreign_signal = _persist_entry(
        database, deployment_id=foreign_dep, qty=10.0, order_id=999
    )
    adapter = _adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(foreign_signal, order_id=999)]},
    )

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _reconciliation_flags(database)[0]["detail"]["cause"] == "no_opening_order"
    assert _ledger(database) == pytest.approx(0.0)
    assert _ledger(database, foreign_dep) == pytest.approx(10.0)


def test_a_refused_close_on_a_fresh_dedupe_key_is_flagged_and_retained(database) -> None:
    """``record_live_broker_side_close_fill`` returns 0.0 for a refusal exactly
    like a duplicate; only a stored dedupe key makes it "handled" (#144)."""
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})
    order = _closing_order()
    order.tradeSide = 1  # BUY extends the long the parent opened: refused as a close
    deal = _close_deal()
    deal.tradeSide = 1

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(_event(order, deal)))

    assert _ledger(database) == pytest.approx(10.0)
    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["detail"]["cause"] == "record_refused"


def test_one_deal_reaching_both_fill_paths_is_booked_once(database) -> None:
    """The stream's normal path and the broker-close path must share ONE dedupe
    key per deal: ``ctrader:deal:{dealId}``, the key ``_build_dedupe_key``
    already derives (#144). A deal booked as a broker close and then resolved
    to a signal (the exit's broker order id persisted after a crash) is a skip,
    never a second row."""
    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(signal_id)]})

    # First the broker-close path books the deal (its order names no signal yet).
    asyncio.run(
        adapter._persist_execution_event(
            _event(_closing_order(order_id=999), _close_deal(deal_id=9001, order_id=999))
        )
    )
    assert _ledger(database) == pytest.approx(0.0)

    # Then the same deal arrives resolved to the entry signal itself.
    asyncio.run(
        adapter._persist_execution_event(
            _event(_entry_order(signal_id), _close_deal(deal_id=9001, order_id=ENTRY_ORDER_ID))
        )
    )

    with db.get_session() as session:
        rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.dedupe_key == "ctrader:deal:9001"
            )
        ).all()
    assert len(rows) == 1
    # 10 entry - 10 broker close; the re-resolved deal added nothing.
    assert _ledger(database) == pytest.approx(0.0)


# ─── the flag itself ──────────────────────────────────────────────────────────────────────────


def test_flag_live_ledger_reconciliation_writes_a_durable_audit_row(database) -> None:
    import shared.database as db

    db.flag_live_ledger_reconciliation(
        account_id=ACCOUNT,
        symbol="aapl",
        cause="test_cause",
        detail={"deal_id": 1},
    )

    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["target_type"] == "account_symbol"
    assert flags[0]["target_id"] == f"{ACCOUNT}:AAPL"
    assert flags[0]["succeeded"] is True
    assert flags[0]["detail"]["cause"] == "test_cause"
    assert flags[0]["detail"]["symbol"] == "AAPL"


def test_the_exit_ledger_mismatch_alert_also_flags_reconciliation(database) -> None:
    """Flatten/manual closes the adapters cannot attribute still surface: the
    exit-time mismatch alert writes the same durable flag (#144 decision)."""
    from live_runner.daemon import _alert_ledger_mismatch
    from live_runner.exit_signals import LedgerMismatchError

    deployment = LiveDeploymentConfig(
        id=DEP_ENTRY,
        strategy_name="fixture",
        account_id=ACCOUNT,
        symbols=["AAPL"],
        timeframe="1m",
        status="ACTIVE",
    )
    exc = LedgerMismatchError(
        "ledgers do not match",
        ledgers={DEP_ENTRY: 10.0},
        broker_qty=4.0,
        tolerance=1e-9,
    )

    asyncio.run(_alert_ledger_mismatch(deployment, "AAPL", exc, own_qty=10.0, exit_qty=4.0))

    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["target_id"] == f"{ACCOUNT}:AAPL"
    assert flags[0]["detail"]["cause"] == "exit_ledger_mismatch"

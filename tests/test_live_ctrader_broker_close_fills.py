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
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from live_runner.ctrader_adapter import (
    BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS,
    BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS,
    BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS,
    BROKER_CLOSE_RECONCILE_WINDOW_MS,
    CTraderApiError,
    CTraderLiveAdapter,
    SymbolScale,
)
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
    deal_list_has_more: Any = False,
    deal_list_fail_predicate: Any = None,
    order_details: dict[int, Any] | None = None,
    order_details_deals: dict[int, list[Any]] | None = None,
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
    requests_log: list[Any] = []

    async def fake_send(request: Any, *, timeout_seconds: int = 10, _depth: int = 0) -> Any:
        requests_log.append(request)
        if isinstance(request, _OrderListReq):
            return _OrderListRes(orders_by_position.get(int(request.positionId), []))
        if isinstance(request, _DealListReq):
            # Honour the request's from/to window like the broker does: a deal
            # outside it is not returned, which is what made the flat 7-day
            # scan miss closes older than a week.
            from_ms = int(getattr(request, "fromTimestamp", 0) or 0)
            to_ms = int(getattr(request, "toTimestamp", 0) or 0)
            failure = (
                deal_list_fail_predicate(from_ms, to_ms)
                if callable(deal_list_fail_predicate)
                else None
            )
            if failure:
                if isinstance(failure, BaseException):
                    raise failure
                raise CTraderApiError("TEST_FAILURE", "deal list window failed")

            def _deal_ts(deal: Any) -> int:
                for attr in ("executionTimestamp", "utcLastUpdateTimestamp", "createTimestamp"):
                    value = int(getattr(deal, attr, 0) or 0)
                    if value:
                        return value
                return 0

            response = _DealListRes(
                [
                    deal
                    for deal in (deal_list or [])
                    if not _deal_ts(deal)
                    or from_ms <= _deal_ts(deal) < to_ms
                ]
            )
            response.hasMore = (
                deal_list_has_more(from_ms, to_ms)
                if callable(deal_list_has_more)
                else deal_list_has_more
            )
            return response
        if isinstance(request, _OrderDetailsReq):
            order = details_by_order.get(int(request.orderId))
            deals = list((order_details_deals or {}).get(int(request.orderId), []))
            return _OrderDetailsRes(order=order, deals=deals)
        raise AssertionError(f"unexpected broker request {type(request).__name__}")

    adapter._send_request_with_refresh_retry = fake_send  # type: ignore[assignment]
    adapter._test_requests = requests_log
    adapter._broker_close_deal_list_min_interval_seconds = 0.0
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


# ─── own close fills whose clientOrderId is not ours (#144 follow-up) ──────────────────────────
#
# ``ProtoOAClosePositionReq`` has no ``clientOrderId`` field (payloadType,
# ctidTraderAccountId, positionId, volume), so the execution event for one of
# our exits carries an SDK/broker-assigned id - never the signal UUID. The
# attribution is the broker order id the submit persisted instead; without it
# the fill was booked as a synthetic broker-side close and every later
# reconcile of the exit signal hit the dedupe-key collision.

SDK_CLOSE_CLIENT_ORDER_ID = "12345-0f1e2d3c-ProtoOAClosePositionReq-3"


def _persist_exit_signal(
    database,
    *,
    deployment_id: str = DEP_ENTRY,
    order_id: int | None = CLOSE_ORDER_ID,
    action: str = "sell",
    metadata_extra: dict[str, Any] | None = None,
) -> str:
    """An exit-only signal as the internal runner persists it.

    ``order_id=None`` leaves ``broker_order_id`` unset, the state between
    ``save_live_signal`` and ``set_live_signal_broker_order_id``.
    """
    import shared.database as db

    metadata: dict[str, Any] = {
        "signal_intent": "exit",
        "exit_only": True,
        "risk_sizing_mode": "exit_only",
        **(metadata_extra or {}),
    }
    if order_id is not None:
        metadata["broker_order_id"] = str(order_id)
    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol="AAPL",
        action=action,
        theoretical_price=95.0,
        timestamp=T1,
        metadata=metadata,
    )
    signal_id, created = db.save_live_signal(signal)
    assert created
    return signal_id


def _sdk_closing_order(*, order_id: int = CLOSE_ORDER_ID, volume: float = 10.0) -> Any:
    """The closing order as the broker reports it for a ClosePositionReq:
    an SDK-assigned ``clientOrderId`` that is not our signal UUID."""
    order = _closing_order(order_id=order_id, volume=volume)
    order.clientOrderId = SDK_CLOSE_CLIENT_ORDER_ID
    return order


def _signal_execution_rows(database, signal_id: str) -> list[Any]:
    import shared.database as db

    with db.get_session() as session:
        return session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == signal_id
            )
        ).all()


def test_own_close_fill_with_sdk_client_order_id_attributes_by_broker_order_id(
    database,
) -> None:
    """The soak bug: the fill's clientOrderId parses as no signal, but its
    order id is persisted on the exit signal - attribute there, book no
    synthetic broker-side close."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(database)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})

    asyncio.run(adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal())))

    exit_rows = _signal_execution_rows(database, exit_id)
    assert len(exit_rows) == 1
    assert exit_rows[0].dedupe_key == "ctrader:deal:9001"
    assert exit_rows[0].broker_order_id == str(CLOSE_ORDER_ID)
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []
    with db.get_session() as session:
        assert (
            session.get(db.LiveSignalModel, db.broker_close_signal_id(entry_id)) is None
        )
    assert _ledger(database) == pytest.approx(0.0)
    assert _reconciliation_flags(database) == []


def test_own_close_fill_attributes_via_the_in_memory_order_map(database) -> None:
    """Same attribution while the process lives: ``_order_id_to_signal_id``
    resolves the order id even before ``broker_order_id`` is persisted."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(database, order_id=None)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})
    adapter._order_id_to_signal_id[CLOSE_ORDER_ID] = exit_id

    asyncio.run(adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal())))

    assert len(_signal_execution_rows(database, exit_id)) == 1
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []
    assert _ledger(database) == pytest.approx(0.0)


def test_an_ambiguous_order_id_attributes_to_the_single_exit_owner(database) -> None:
    """Pre-mortem: ``broker_order_id`` is not unique, so a recovered duplicate
    can leave an entry and an exit signal sharing one order id. Only an
    exit-only signal can claim a position-close fill this way - when exactly
    one match is an exit it is the plausible owner; booking the fill as a
    broker-side close would leave the real exit looking unfilled."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(
        database,
        order_id=ENTRY_ORDER_ID,
        metadata_extra={"position_id": POSITION_ID},
    )
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})

    asyncio.run(
        adapter._persist_execution_event(
            _event(
                _sdk_closing_order(order_id=ENTRY_ORDER_ID),
                _close_deal(order_id=ENTRY_ORDER_ID),
            )
        )
    )

    assert len(_signal_execution_rows(database, exit_id)) == 1
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []
    assert _ledger(database) == pytest.approx(0.0)


def test_a_broker_side_close_with_an_sdk_style_client_order_id_still_books(
    database,
) -> None:
    """An order id no signal owns is still a genuine broker-side close: the
    non-UUID clientOrderId changes nothing for it."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    unknown_order_id = 999
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})

    asyncio.run(
        adapter._persist_execution_event(
            _event(
                _sdk_closing_order(order_id=unknown_order_id),
                _close_deal(order_id=unknown_order_id),
            )
        )
    )

    close_rows = _signal_execution_rows(database, db.broker_close_signal_id(entry_id))
    assert len(close_rows) == 1
    assert _ledger(database) == pytest.approx(0.0)


def test_a_close_fill_during_an_in_flight_submit_is_retained_not_booked(
    database,
) -> None:
    """The fill can reach the listener before ``submit_order`` has the broker
    order id: while the close submit for the position is in flight the event
    is retained for reconciliation, never booked as a broker-side close."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(database, order_id=None)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    with pytest.raises(ValueError, match="in flight"):
        asyncio.run(
            adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal()))
        )

    # Nothing was booked while the submit was in flight.
    assert _ledger(database) == pytest.approx(10.0)
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []

    # The submit completes: broker order id persisted, pending mark cleared.
    database.set_live_signal_broker_order_id(exit_id, str(CLOSE_ORDER_ID))
    adapter._pending_close_positions.clear()
    asyncio.run(adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal())))

    assert len(_signal_execution_rows(database, exit_id)) == 1
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []
    assert _ledger(database) == pytest.approx(0.0)


def test_an_expired_pending_mark_does_not_hold_a_broker_close(database) -> None:
    """A pending mark only covers the submit window: once it expires a close
    on the position takes the ordinary broker-close path again."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})
    adapter._pending_close_positions[POSITION_ID] = {
        "00000000-0000-4000-8000-000000000000": time.monotonic() - 7200.0
    }

    asyncio.run(adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal())))

    assert len(_signal_execution_rows(database, db.broker_close_signal_id(entry_id))) == 1
    assert _ledger(database) == pytest.approx(0.0)


def test_restart_reconcile_of_an_attributed_close_fill_has_no_dedupe_collision(
    database, caplog
) -> None:
    """Regression for the 2026-11-26 ERROR: with the fill owned by the exit
    signal, the per-signal reconcile finds the dedupe key already stored
    under the SAME signal and skips it - no cross-signal collision."""
    import logging

    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(database)
    live_adapter = _adapter(
        database, position_orders={POSITION_ID: [_entry_order(entry_id)]}
    )
    asyncio.run(
        live_adapter._persist_execution_event(_event(_sdk_closing_order(), _close_deal()))
    )

    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(entry_id)]},
        deal_list=[_close_deal()],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        order_details_deals={CLOSE_ORDER_ID: [_close_deal()]},
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(adapter.reconcile_executions())

    assert not [
        record
        for record in caplog.records
        if "Dedupe key collision" in record.getMessage()
    ]
    with db.get_session() as session:
        deal_rows = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.dedupe_key == "ctrader:deal:9001"
            )
        ).all()
    assert len(deal_rows) == 1
    assert deal_rows[0].signal_id == exit_id
    assert _ledger(database) == pytest.approx(0.0)


# ─── pending-mark lifecycle and the deal-list reconcile ───────────────────────
#
# ``submit_order`` marks a position while its ``ClosePositionReq`` is in
# flight; the mark must (a) keep BOTH fill paths - the stream listener and
# the deal-list reconcile - from booking our own close as a broker-side
# close, (b) lift as soon as the submit's outcome is known one way or the
# other, and (c) never spend the listener's retry budget while it lives.


def test_deal_list_reconcile_defers_a_close_while_its_submit_is_pending(
    database,
) -> None:
    """Blocker: the deal-list pass must honour the pending mark too.

    A retained stream fill sets ``_reconciliation_required`` with no backoff,
    so the very next listener loop runs ``reconcile_executions`` - while the
    close submit may still be in flight. Booking the deal there would
    mis-book our own exit exactly like the stream path used to.
    """
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    exit_id = _persist_exit_signal(database, order_id=None)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(entry_id)]},
        deal_list=[_close_deal()],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    recovered = asyncio.run(adapter.reconcile_executions())

    # Nothing booked, nothing flagged: the deal's window went back on the
    # scan cursor instead.
    assert recovered == 0
    assert _ledger(database) == pytest.approx(10.0)
    assert _signal_execution_rows(database, db.broker_close_signal_id(entry_id)) == []
    assert _reconciliation_flags(database) == []
    scan_state = database.get_live_adapter_state(ACCOUNT, "ctrader_broker_close_scan")
    assert scan_state is not None
    pending = scan_state.get("pending") or []
    assert any(
        int(start) <= T1_MS < int(end) for start, end in pending
    ), f"deferred deal window missing from pending ranges: {pending}"

    # Once the submit's outcome is known (mark lifted), the next pass books
    # the deferred deal through the ordinary broker-close path.
    adapter._pending_close_positions.clear()
    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)
    assert len(_signal_execution_rows(database, db.broker_close_signal_id(entry_id))) == 1


def test_a_close_fill_for_an_entry_owned_order_id_takes_the_broker_close_path(
    database,
) -> None:
    """M2 pin: the broker-order-id fallback only attributes to an *exit*.

    A position-close fill whose order id resolves to a signal the ledger
    does not count as reducing must still take the broker-close path and
    book against the position's real opener - never onto the entry signal,
    where the fill would open quantity instead of closing it.
    """
    import shared.database as db

    # The entry signal owns CLOSE_ORDER_ID: an order id a non-exit signal
    # carries must not claim the close fill.
    entry_id = _persist_entry(database, qty=10.0, order_id=CLOSE_ORDER_ID)
    adapter = _adapter(database, position_orders={POSITION_ID: [_entry_order(entry_id)]})

    asyncio.run(
        adapter._persist_execution_event(
            _event(_sdk_closing_order(), _close_deal())
        )
    )

    # The fill booked as a broker-side close against the opener's ledger,
    # not as another fill on the entry signal.
    assert len(_signal_execution_rows(database, db.broker_close_signal_id(entry_id))) == 1
    assert len(_signal_execution_rows(database, entry_id)) == 1
    assert _ledger(database) == pytest.approx(0.0)
    assert _reconciliation_flags(database) == []


class _ClosePositionReq:
    pass


class _ExecutionEventPayload:
    pass


def _exit_submit_signal() -> LiveSignal:
    return LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="sell",
        theoretical_price=95.0,
        timestamp=T1,
        metadata={
            "signal_intent": "exit",
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            "position_id": POSITION_ID,
        },
    )


def _close_submit_adapter(database, position_orders: dict[int, list[Any]] | None = None):
    """An adapter wired for ``submit_order`` on a ClosePositionReq."""
    adapter = _adapter(
        database,
        position_orders=position_orders or {POSITION_ID: []},
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._position_ids_by_symbol["AAPL"] = (POSITION_ID,)
    adapter._ProtoOAClosePositionReq = _ClosePositionReq
    adapter._ProtoOAExecutionEvent = _ExecutionEventPayload
    return adapter


def test_a_rejected_close_submit_clears_the_pending_mark(database) -> None:
    """M3 pin: a definitive broker refusal created no order, so nothing can
    fill - the pending mark lifts and a later broker-side close on the
    position books normally."""
    import shared.database as db
    from live_runner.base_adapter import BrokerOrderRejectedError

    entry_id = _persist_entry(database, qty=10.0)
    adapter = _close_submit_adapter(
        database, position_orders={POSITION_ID: [_entry_order(entry_id)]}
    )

    async def refuse(request: Any) -> Any:
        raise BrokerOrderRejectedError("Order rejected", error_code="TEST")

    adapter._submit_new_order_request = refuse  # type: ignore[assignment]
    with pytest.raises(BrokerOrderRejectedError):
        asyncio.run(adapter.submit_order(_exit_submit_signal(), qty=10.0))

    assert not adapter._pending_close_position_active(POSITION_ID)
    assert POSITION_ID not in adapter._pending_close_positions

    # A later genuine broker-side close on the position is booked, not
    # retained: the dead submit's mark no longer holds the guard.
    asyncio.run(
        adapter._persist_execution_event(
            _event(_closing_order(), _close_deal())
        )
    )
    assert len(_signal_execution_rows(database, db.broker_close_signal_id(entry_id))) == 1
    assert _ledger(database) == pytest.approx(0.0)


def test_a_successful_close_submit_clears_the_pending_mark(database) -> None:
    """M5 pin: once the broker order id is persisted the mark has done its
    job - a later genuine broker close on the same position books, it is
    not retained as if a submit were still in flight."""
    import shared.database as db

    entry_id = _persist_entry(database, qty=10.0)
    adapter = _close_submit_adapter(
        database, position_orders={POSITION_ID: [_entry_order(entry_id)]}
    )

    async def accept(request: Any) -> Any:
        return SimpleNamespace(orderId=CLOSE_ORDER_ID)

    adapter._submit_new_order_request = accept  # type: ignore[assignment]
    order_id = asyncio.run(adapter.submit_order(_exit_submit_signal(), qty=10.0))
    assert order_id == str(CLOSE_ORDER_ID)

    assert not adapter._pending_close_position_active(POSITION_ID)
    assert POSITION_ID not in adapter._pending_close_positions

    # A genuine broker-side close (an order id no signal owns) on the same
    # position takes the ordinary broker-close path.
    broker_close_order_id = 999
    asyncio.run(
        adapter._persist_execution_event(
            _event(
                _closing_order(order_id=broker_close_order_id),
                _close_deal(order_id=broker_close_order_id, deal_id=9002),
            )
        )
    )
    close_rows = _signal_execution_rows(database, db.broker_close_signal_id(entry_id))
    assert [row.dedupe_key for row in close_rows] == ["ctrader:deal:9002"]
    assert _ledger(database) == pytest.approx(0.0)


def test_a_failed_order_id_persist_rolls_back_the_order_map(
    database, monkeypatch
) -> None:
    """M6 pin: when persisting the broker order id fails, the in-memory
    ownership maps roll back with it - a fill must not resolve to a signal
    whose order id was never durably recorded."""
    import live_runner.ctrader_adapter as adapter_module

    entry_id = _persist_entry(database, qty=10.0)
    adapter = _close_submit_adapter(
        database, position_orders={POSITION_ID: [_entry_order(entry_id)]}
    )

    async def accept(request: Any) -> Any:
        return SimpleNamespace(orderId=CLOSE_ORDER_ID)

    adapter._submit_new_order_request = accept  # type: ignore[assignment]
    monkeypatch.setattr(
        adapter_module, "set_live_signal_broker_order_id", lambda *a, **k: False
    )

    with pytest.raises(RuntimeError, match="Failed to persist"):
        asyncio.run(adapter.submit_order(_exit_submit_signal(), qty=10.0))

    assert CLOSE_ORDER_ID not in adapter._order_id_to_signal_id
    assert CLOSE_ORDER_ID not in adapter._order_id_to_symbol
    # The pending mark stays: the submit's outcome is unknown, so fills on
    # the position keep being retained until reconciliation recovers the id.
    assert adapter._pending_close_position_active(POSITION_ID)


def test_an_unknown_outcome_close_submit_keeps_the_mark_and_parks_budget_free(
    database,
) -> None:
    """Should-fix: an UNKNOWN submit outcome (transport failure, not a
    refusal) keeps the mark for its TTL - but the retained fill parks in
    ``_unresolved_execution_events`` WITHOUT spending the retry budget, so
    the listener cannot crash-loop for the mark's whole lifetime."""
    from live_runner.ctrader_adapter import _PendingCloseFillRetentionError

    entry_id = _persist_entry(database, qty=10.0)
    adapter = _close_submit_adapter(
        database, position_orders={POSITION_ID: [_entry_order(entry_id)]}
    )

    async def lose_connection(request: Any) -> Any:
        raise RuntimeError("connection lost before the broker answered")

    adapter._submit_new_order_request = lose_connection  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(adapter.submit_order(_exit_submit_signal(), qty=10.0))

    # Unknown outcome: the mark stays (bounded by the TTL).
    assert adapter._pending_close_position_active(POSITION_ID)

    event = _event(_sdk_closing_order(), _close_deal())
    with pytest.raises(_PendingCloseFillRetentionError):
        asyncio.run(adapter._persist_execution_event(event))

    # What listen_executions does with that error: park, budget-free.
    adapter._park_pending_close_fill(event)
    assert event in adapter._unresolved_execution_events
    assert id(event) in adapter._pending_close_fill_event_ids
    assert adapter._reconciliation_required is True
    assert id(event) not in adapter._execution_retry_counts

    # A reconciliation pass requeues the parked fill without spending budget.
    async def no_op_reconcile() -> int:
        return 0

    adapter.reconcile_executions = no_op_reconcile  # type: ignore[assignment]
    adapter._reconciliation_retry_at = 0.0
    asyncio.run(adapter._run_pending_reconciliation(2))

    assert adapter._execution_queue.get_nowait() is event
    assert id(event) not in adapter._execution_retry_counts
    assert adapter._pending_close_fill_event_ids == set()
    assert adapter._reconciliation_required is False


def test_a_recovered_close_order_id_clears_the_pending_mark(database) -> None:
    """Should-fix: when reconciliation recovers and persists the close's
    broker order id, the pending mark lifts - parked fills can attribute as
    soon as the order id is known instead of waiting out the TTL."""
    exit_id = _persist_exit_signal(
        database, order_id=None, metadata_extra={"position_id": POSITION_ID}
    )
    adapter = _adapter(database)
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    async def recovered_order_id(*_args: Any, **_kwargs: Any) -> str:
        return str(CLOSE_ORDER_ID)

    adapter._recover_broker_order_id = recovered_order_id  # type: ignore[assignment]
    recovered = asyncio.run(adapter._recover_and_persist_broker_order_id(exit_id))

    assert recovered == str(CLOSE_ORDER_ID)
    assert POSITION_ID not in adapter._pending_close_positions
    assert adapter._order_id_to_signal_id[CLOSE_ORDER_ID] == exit_id
    assert database.get_live_signal_broker_order_id(exit_id) == str(CLOSE_ORDER_ID)


def test_a_recovered_persisted_signal_order_id_clears_the_pending_mark(
    database,
) -> None:
    """Same lift on the duplicate-signal recovery path."""
    exit_id = _persist_exit_signal(
        database, order_id=None, metadata_extra={"position_id": POSITION_ID}
    )
    adapter = _adapter(database)
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    async def recovered_order_id(*_args: Any, **_kwargs: Any) -> str:
        return str(CLOSE_ORDER_ID)

    adapter._recover_broker_order_id = recovered_order_id  # type: ignore[assignment]
    recovered = asyncio.run(
        adapter._recover_persisted_signal_order_id(
            exit_id,
            signal_timestamp=T1,
            unrecovered_error="unrecovered",
            unpersisted_error="unpersisted",
        )
    )

    assert recovered == str(CLOSE_ORDER_ID)
    assert POSITION_ID not in adapter._pending_close_positions


def test_a_duplicate_signal_with_persisted_order_id_clears_the_pending_mark(
    database,
) -> None:
    """Pre-mortem: the duplicate early-return must lift the mark too - the
    close's broker order id is already known, so a genuine broker-side close
    on the position must not stay parked until the TTL."""
    exit_id = _persist_exit_signal(
        database,
        order_id=CLOSE_ORDER_ID,
        metadata_extra={"position_id": POSITION_ID},
    )
    adapter = _adapter(database)
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    recovered = asyncio.run(adapter._recover_duplicate_signal_order_id(exit_id))

    assert recovered == str(CLOSE_ORDER_ID)
    assert POSITION_ID not in adapter._pending_close_positions
    assert adapter._order_id_to_signal_id[CLOSE_ORDER_ID] == exit_id


def test_an_exit_only_signal_without_risk_sizing_mode_still_lifts_the_mark(
    database,
) -> None:
    """Pre-mortem: ``_persisted_exit_position_id`` must use the ledger's exit
    predicate, not only ``risk_sizing_mode=exit_only`` - an ``exit_only``
    signal with different sizing metadata is still a close the ledger
    counts, so its pending mark must lift on order-id recovery."""
    exit_id = _persist_exit_signal(
        database,
        order_id=CLOSE_ORDER_ID,
        metadata_extra={
            "risk_sizing_mode": "sized",
            "position_id": POSITION_ID,
        },
    )
    adapter = _adapter(database)
    adapter._pending_close_positions[POSITION_ID] = {exit_id: time.monotonic()}

    recovered = asyncio.run(adapter._recover_duplicate_signal_order_id(exit_id))

    assert recovered == str(CLOSE_ORDER_ID)
    assert POSITION_ID not in adapter._pending_close_positions


def test_deal_list_reconcile_flags_an_ambiguous_broker_order_id(
    database,
) -> None:
    """Pre-mortem BLOCK: ``broker_order_id`` is indexed, not unique - two
    signals on the account can share one. The deal may be ours or a
    broker-side close, so the pass must flag it for an operator rather than
    book it against the wrong ledger side (or silently skip it as "ours")."""
    entry_id = _persist_entry(database, qty=10.0)
    first_exit = _persist_exit_signal(
        database,
        order_id=CLOSE_ORDER_ID,
        metadata_extra={"position_id": POSITION_ID},
    )
    second_exit = _persist_exit_signal(
        database,
        order_id=CLOSE_ORDER_ID,
        metadata_extra={"position_id": POSITION_ID},
    )
    assert first_exit != second_exit
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(entry_id)]},
        deal_list=[_close_deal()],
    )

    asyncio.run(adapter.reconcile_executions())

    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["detail"]["cause"] == "ambiguous_broker_order_id"
    # The close was NOT booked: the entry stays open on the ledger.
    assert _ledger(database) == pytest.approx(10.0)


# ─── the reconciliation backfill ──────────────────────────────────────────────────────────────


def _reconcilable_adapter(database, **kwargs) -> CTraderLiveAdapter:
    adapter = _adapter(database, **kwargs)
    adapter._client = object()
    adapter._authenticated = True
    return adapter


def _deal_list_requests(adapter: CTraderLiveAdapter) -> list[Any]:
    return [r for r in adapter._test_requests if isinstance(r, _DealListReq)]


def _order_list_requests(adapter: CTraderLiveAdapter) -> list[Any]:
    return [r for r in adapter._test_requests if isinstance(r, _OrderListReq)]


def _deal_windows(adapter: CTraderLiveAdapter) -> list[tuple[int, int]]:
    return [
        (int(r.fromTimestamp), int(r.toTimestamp)) for r in _deal_list_requests(adapter)
    ]


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


def test_reconcile_books_a_close_older_than_the_flat_week_window(database) -> None:
    """The 2026-11-24 soak regression: a position closed at the broker while
    the daemon was down for weeks kept its ledger quantity forever, because
    the deal-list pass only scanned the last 7 days. The scan now pages back
    to the oldest persisted signal's timestamp."""
    signal_id = _persist_entry(database, qty=10.0)
    # The entry signal is timestamped T0 (months before "now"); the close deal
    # sits one hour later - far outside any 7-day window ending today.
    old_close = _close_deal(deal_id=9001, timestamp_ms=T0_MS + 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[old_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_books_a_recent_close_when_an_older_window_fails(database) -> None:
    """Newest-first coverage: a failed OLD window must not drop the recent
    close the old forward scan would have reached only after paging from the
    floor (#415 B1)."""
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    recent_close = _close_deal(
        deal_id=9100,
        timestamp_ms=now_ms - (24 * 60 * 60 * 1000),
    )
    cutoff_ms = now_ms - (30 * 24 * 60 * 60 * 1000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[recent_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_fail_predicate=lambda from_ms, _to_ms: from_ms < cutoff_ms,
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)
    first_window = _deal_windows(adapter)[0]
    assert first_window[0] >= cutoff_ms


def test_reconcile_scans_back_only_to_the_oldest_open_exposure(database) -> None:
    """Flat history is not open exposure: an old closed signal must not drag
    the scan floor back months when the only live lot is newer (#415 B5)."""
    import shared.database as db

    old_signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=T0,
        metadata={"broker_order_id": str(ENTRY_ORDER_ID)},
    )
    old_signal_id, created = db.save_live_signal(old_signal)
    assert created
    assert db.save_live_execution(
        LiveExecution(
            signal_id=UUID(old_signal_id),
            actual_price=100.0,
            actual_price_observed="observed",
            filled_qty=10.0,
            commission=0.0,
            timestamp=T0,
            broker_order_id=str(ENTRY_ORDER_ID),
            dedupe_key=f"ctrader:test:entry:{old_signal_id}",
        )
    )
    old_exit = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="sell",
        theoretical_price=95.0,
        timestamp=T0 + timedelta(hours=1),
        metadata={
            "broker_order_id": str(ENTRY_ORDER_ID + 1),
            "risk_sizing_mode": "exit_only",
        },
    )
    old_exit_id, created = db.save_live_signal(old_exit)
    assert created
    assert db.save_live_execution(
        LiveExecution(
            signal_id=UUID(old_exit_id),
            actual_price=95.0,
            actual_price_observed="observed",
            filled_qty=10.0,
            commission=0.0,
            timestamp=old_exit.timestamp,
            broker_order_id=str(ENTRY_ORDER_ID + 1),
            dedupe_key=f"ctrader:test:exit:{old_exit_id}",
        )
    )

    open_timestamp = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    open_signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=open_timestamp,
        metadata={"broker_order_id": str(ENTRY_ORDER_ID + 10)},
    )
    open_signal_id, created = db.save_live_signal(open_signal)
    assert created
    assert db.save_live_execution(
        LiveExecution(
            signal_id=UUID(open_signal_id),
            actual_price=100.0,
            actual_price_observed="observed",
            filled_qty=10.0,
            commission=0.0,
            timestamp=open_timestamp,
            broker_order_id=str(ENTRY_ORDER_ID + 10),
            dedupe_key=f"ctrader:test:entry:{open_signal_id}",
        )
    )
    adapter = _reconcilable_adapter(database, deal_list=[])

    asyncio.run(adapter.reconcile_executions())

    expected_floor_ms = int(open_timestamp.timestamp() * 1000) - (
        BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
    )
    windows = _deal_windows(adapter)
    assert windows
    assert min(from_ms for from_ms, _to_ms in windows) == expected_floor_ms
    assert expected_floor_ms > T0_MS


def test_reconcile_caps_deal_list_requests_when_has_more_persists(
    database, caplog
) -> None:
    """A pathological ``hasMore`` account must not turn one pass into
    thousands of deal-list requests (#415 B2)."""
    import logging

    signal_id = _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[_close_deal()],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_has_more=lambda _from_ms, _to_ms: True,
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter.reconcile_executions())

    deal_requests = _deal_list_requests(adapter)
    assert len(deal_requests) == BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS
    has_more_warnings = [
        record for record in caplog.records if "hasMore" in record.getMessage()
    ]
    assert len(has_more_warnings) == 1
    assert "uncovered" in has_more_warnings[0].getMessage()


def test_reconcile_processes_gathered_deals_when_a_window_times_out(database) -> None:
    """A Twisted/asyncio timeout is a per-window failure, not a reason to
    discard every newer window already gathered (#415 should-fix 6)."""
    signal_id = _persist_entry(database, qty=10.0)
    recent_close = _close_deal(
        deal_id=9400,
        timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000)
        - (24 * 60 * 60 * 1000),
    )
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - (
        30 * 24 * 60 * 60 * 1000
    )
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[recent_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_fail_predicate=lambda from_ms, _to_ms: (
            TimeoutError("deal list timed out") if from_ms < cutoff_ms else None
        ),
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_does_not_reflag_a_resolved_close_after_restart(database) -> None:
    """The durable flag set survives a new adapter on the same DB: an
    unattributable close older than the old 7-day window must not be flagged
    (and re-break a resolution) on every process start (#415 B3). The set
    suppresses re-FLAGGING only - the booking attempt is still retried, so
    the order list IS read again; it just must not write a second flag."""
    import shared.database as db

    _persist_entry(database, qty=10.0)
    old_close = _close_deal(deal_id=9501, timestamp_ms=T0_MS + 3_600_000)
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[old_close],
    )
    asyncio.run(first.reconcile_executions())
    assert len(_reconciliation_flags(database)) == 1

    db.resolve_ledger_reconciliation_flag(
        account_id=ACCOUNT,
        symbol="AAPL",
        actor="operator",
        note="reconciled by hand",
    )
    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[old_close],
    )
    asyncio.run(second.reconcile_executions())

    assert len(_reconciliation_flags(database)) == 1
    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == []
    # The retry happened (the flagged deal's window was re-scanned) but the
    # durable flag set kept it from writing a second audit row.
    assert _order_list_requests(second)


def test_reconcile_skips_an_already_booked_close_before_broker_reads(database) -> None:
    """Deal-id dedupe runs before position-order/order-detail reads: a close
    the ledger already booked costs no extra broker requests on reconnect
    (#415 B4)."""
    signal_id = _persist_entry(database, qty=10.0)
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[_close_deal(deal_id=9601)],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(first.reconcile_executions())
    assert _ledger(database) == pytest.approx(0.0)

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[_close_deal(deal_id=9601)],
        order_details={},
    )
    asyncio.run(second.reconcile_executions())

    assert _order_list_requests(second) == []
    assert _reconciliation_flags(database) == []
    assert _ledger(database) == pytest.approx(0.0)


def test_fetch_broker_close_window_halves_has_more_and_keeps_second_half(
    database,
) -> None:
    """The ``hasMore`` recursion must actually fetch both halves: a close in
    the newer half of a truncated window is still booked (#415 should-fix 8)."""
    start_ms = 1_700_000_000_000
    end_ms = start_ms + (7 * 24 * 60 * 60 * 1000)
    mid_ms = start_ms + (end_ms - start_ms) // 2
    first_half_deal = _close_deal(
        deal_id=9701, timestamp_ms=start_ms + (end_ms - start_ms) // 4
    )
    second_half_deal = _close_deal(
        deal_id=9702, timestamp_ms=mid_ms + (end_ms - mid_ms) // 2
    )
    adapter = _adapter(
        database,
        deal_list=[first_half_deal, second_half_deal],
        deal_list_has_more=lambda from_ms, to_ms: (from_ms, to_ms)
        == (start_ms, end_ms),
    )
    budget = {
        "remaining": 10,
        "last_request_at": 0.0,
        "exhausted": False,
        "truncated_ranges": [],
        "failed_ranges": [],
    }

    deals = asyncio.run(
        adapter._fetch_broker_close_deal_window(start_ms, end_ms, budget=budget)
    )

    assert deals is not None
    assert {deal.dealId for deal in deals} == {9701, 9702}
    assert len(_deal_list_requests(adapter)) == 3


def test_reconcile_processes_a_timestamp_less_deal_once_per_pass(database) -> None:
    """Boundary/duplicate deals returned by every window are processed once:
    ``seen_deal_ids`` is the only guard that runs before broker reads (#415
    should-fix 8)."""
    _persist_entry(database, qty=10.0)
    deal = _close_deal(deal_id=9801)
    deal.executionTimestamp = 0
    deal.utcLastUpdateTimestamp = 0
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: []},
        deal_list=[deal],
    )
    calls = 0
    original = adapter._reconcile_position_close_deal

    async def counting(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    adapter._reconcile_position_close_deal = counting  # type: ignore[assignment]
    asyncio.run(adapter.reconcile_executions())

    assert calls == 1
    assert len(_reconciliation_flags(database)) == 1


def test_reconcile_without_open_exposure_keeps_the_recent_window_floor(
    database,
) -> None:
    """``not_before_ms=None`` still means the old flat 7-day floor, not an
    unbounded scan (#415 should-fix 8)."""
    adapter = _reconcilable_adapter(database, deal_list=[])
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    asyncio.run(adapter.reconcile_executions())

    windows = _deal_windows(adapter)
    assert len(windows) == 1
    assert windows[0][1] - windows[0][0] <= BROKER_CLOSE_RECONCILE_WINDOW_MS
    assert windows[0][0] >= now_ms - BROKER_CLOSE_RECONCILE_WINDOW_MS - 60_000


def test_reconcile_position_order_lookup_uses_the_open_exposure_window(
    database,
) -> None:
    """``OrderListByPositionId`` gets an explicit window reaching the opening
    signal, so a ~60-day-old opener is not missed by a default recent lookup
    (#415 should-fix 7)."""
    signal_id = _persist_entry(database, qty=10.0)
    old_close = _close_deal(deal_id=9901, timestamp_ms=T0_MS + 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[old_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    asyncio.run(adapter.reconcile_executions())

    order_requests = _order_list_requests(adapter)
    assert order_requests
    request = order_requests[0]
    assert int(request.fromTimestamp) == T0_MS - BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
    assert int(request.toTimestamp) > T0_MS


def test_reconcile_close_beyond_max_lookback_is_not_booked(database, caplog) -> None:
    """The paged scan is still bounded: a signal (and so any position it
    opened) older than ``BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS`` is outside
    the pass, and the truncation is logged rather than silent."""
    import logging

    ancient = T0.timestamp() - (401 * 24 * 60 * 60)
    signal = LiveSignal(
        deployment_id=DEP_ENTRY,
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.fromtimestamp(ancient, tz=timezone.utc),
        metadata={"broker_order_id": str(ENTRY_ORDER_ID)},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    assert database.save_live_execution(
        LiveExecution(
            signal_id=UUID(signal_id),
            actual_price=100.0,
            actual_price_observed="observed",
            filled_qty=10.0,
            commission=0.0,
            timestamp=signal.timestamp,
            broker_order_id=str(ENTRY_ORDER_ID),
            dedupe_key=f"ctrader:test:entry:{signal_id}",
        )
    )
    ancient_close = _close_deal(
        deal_id=9001, timestamp_ms=int(ancient * 1000) + 3_600_000
    )
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[ancient_close],
    )

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)
    assert any("maximum lookback" in record.getMessage() for record in caplog.records)
    windows = _deal_windows(adapter)
    assert windows
    oldest_from_ms = min(from_ms for from_ms, _to_ms in windows)
    assert oldest_from_ms >= now_ms - BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS - 60_000
    ancient_close_ms = int(ancient * 1000) + 3_600_000
    assert all(
        not (from_ms <= ancient_close_ms < to_ms) for from_ms, to_ms in windows
    )


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


# ─── flip first-sight provenance read (#168) ─────────────────────────────────


def test_opening_signal_id_for_position_proves_the_single_opener(
    database,
) -> None:
    """One persisted opening order -> its signal id; protection legs and
    closing orders never count."""
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(
        database,
        position_orders={
            POSITION_ID: [
                _entry_order(signal_id),
                _protection_order(),
                _closing_order(),
            ]
        },
    )

    assert (
        asyncio.run(adapter.opening_signal_id_for_position(POSITION_ID))
        == signal_id
    )


def test_opening_signal_id_for_position_manual_opener_proves_nothing(
    database,
) -> None:
    """A manual/foreign opener (no persisted signal) returns ``None`` -
    proven foreign, which the flip manager treats as fail-closed."""
    adapter = _adapter(
        database,
        position_orders={
            POSITION_ID: [_entry_order("not-a-uuid", order_id=999)]
        },
    )

    assert asyncio.run(adapter.opening_signal_id_for_position(POSITION_ID)) is None


def test_opening_signal_id_for_position_ambiguous_openers_prove_nothing(
    database,
) -> None:
    """Two distinct persisted openers -> ``None``: the book cannot say which
    signal owns the position."""
    first = _persist_entry(database, qty=10.0, order_id=555)
    second = _persist_entry(
        database, deployment_id=DEP_OTHER, qty=4.0, order_id=556
    )
    adapter = _adapter(
        database,
        position_orders={
            POSITION_ID: [
                _entry_order(first, order_id=555),
                _entry_order(second, order_id=556),
            ]
        },
    )

    assert asyncio.run(adapter.opening_signal_id_for_position(POSITION_ID)) is None


def test_opening_signal_id_for_position_closing_orders_are_skipped(
    database,
) -> None:
    """Only closingOrder=False rows can open a position: a position whose
    order list holds just a close proves nothing."""
    adapter = _adapter(
        database, position_orders={POSITION_ID: [_closing_order()]}
    )

    assert asyncio.run(adapter.opening_signal_id_for_position(POSITION_ID)) is None


def test_opening_signal_id_for_position_unreadable_raises(
    database,
) -> None:
    """An unreadable order list RAISES (transient, retried by the caller)
    instead of returning ``None`` (proven foreign)."""
    from live_runner.ctrader_adapter import CTraderApiError

    adapter = _adapter(database)

    async def _wrong_response(request: Any, **_kwargs: Any) -> Any:
        return object()

    adapter._send_request_with_refresh_retry = _wrong_response  # type: ignore[method-assign]

    with pytest.raises(CTraderApiError, match="unreadable"):
        asyncio.run(adapter.opening_signal_id_for_position(POSITION_ID))


# ─── bundle-74 findings: flags retry, stream window, scan cursor ────────────────────────────────


def test_reconcile_retries_a_flagged_close_once_the_broker_recovers(database) -> None:
    """N1: the durable flag set suppresses re-FLAGGING, never the booking attempt.

    A close flagged for a transient cause (here an unreadable order list) is
    retried on a later pass; once the broker answers, the fill is booked and
    the flag is superseded as resolved-by-booking - no second audit row, no
    permanent never-book."""
    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    old_close = _close_deal(deal_id=9101, timestamp_ms=T0_MS + 3_600_000)
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[old_close],
    )
    inner_send = first._send_request_with_refresh_retry

    async def unreadable_order_list(request: Any, **kwargs: Any) -> Any:
        if isinstance(request, _OrderListReq):
            return object()
        return await inner_send(request, **kwargs)

    first._send_request_with_refresh_retry = unreadable_order_list  # type: ignore[assignment]
    asyncio.run(first.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)
    flags = _reconciliation_flags(database)
    assert len(flags) == 1
    assert flags[0]["detail"]["cause"] == "position_orders_unreadable"

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[old_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(second.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)
    # No second flag row for the same deal, and the booking superseded the flag.
    assert len(_reconciliation_flags(database)) == 1
    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == []
    resolved = [
        row
        for row in database.list_live_control_audit(limit=200)
        if row["action"] == db.LEDGER_RECONCILIATION_RESOLVED_AUDIT_ACTION
    ]
    assert resolved
    # The retry happened through the order-list read, not a re-flag.
    assert _order_list_requests(second)


def test_stream_close_order_lookup_uses_the_open_exposure_window(database) -> None:
    """N2: the stream path bounds ``OrderListByPositionId`` exactly like the
    reconcile pass, so a months-old opener is not missed by a default recent
    lookup and flagged ``no_opening_order`` forever."""
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _adapter(
        database, position_orders={POSITION_ID: [_entry_order(signal_id)]}
    )

    asyncio.run(adapter._persist_execution_event(_event(_closing_order(), _close_deal())))

    assert _ledger(database) == pytest.approx(0.0)
    order_requests = _order_list_requests(adapter)
    assert order_requests
    request = order_requests[0]
    assert int(getattr(request, "fromTimestamp", 0) or 0) == (
        T0_MS - BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
    )
    assert int(getattr(request, "toTimestamp", 0) or 0) > T0_MS


def test_reconcile_keeps_window_rows_when_the_subwindow_budget_runs_out(
    database, monkeypatch
) -> None:
    """S1: rows the broker already returned survive a budget-starved
    subdivision - the parent's deals are the only coverage of the half that
    could not be fetched."""
    monkeypatch.setattr(
        "live_runner.ctrader_adapter.BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS",
        2,
    )
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = BROKER_CLOSE_RECONCILE_WINDOW_MS
    # Second half of the newest window: reachable only through the parent rows
    # once the request budget is gone.
    late_close = _close_deal(deal_id=9201, timestamp_ms=now_ms - window_ms // 4)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[late_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_has_more=lambda from_ms, to_ms: (to_ms - from_ms) == window_ms,
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_keeps_the_newest_window_when_a_subwindow_times_out(
    database,
) -> None:
    """S2: a failed sub-window must not discard its sibling half or the parent
    rows - the newest window's recent close still books."""
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = BROKER_CLOSE_RECONCILE_WINDOW_MS
    recent_close = _close_deal(deal_id=9202, timestamp_ms=now_ms - 3_600_000)
    mid_ms = now_ms - window_ms // 2
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[recent_close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_has_more=lambda from_ms, to_ms: (to_ms - from_ms) == window_ms,
        deal_list_fail_predicate=lambda from_ms, to_ms: (
            TimeoutError("sub-window timed out")
            if (to_ms - from_ms) < window_ms and from_ms >= mid_ms - 60_000
            else None
        ),
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered >= 1
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_cursor_carries_coverage_forward_instead_of_rescanning(
    database, monkeypatch
) -> None:
    """The persisted scan cursor lets a later pass continue further back
    rather than restarting at ``now`` and starving older windows behind the
    request cap."""
    monkeypatch.setattr(
        "live_runner.ctrader_adapter.BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS",
        3,
    )
    _persist_entry(database, qty=10.0)
    first = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(first.reconcile_executions())
    first_windows = _deal_windows(first)
    assert first_windows

    second = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(second.reconcile_executions())
    second_windows = _deal_windows(second)

    assert second_windows
    assert min(from_ms for from_ms, _ in second_windows) < min(
        from_ms for from_ms, _ in first_windows
    )


def test_reconcile_after_a_complete_pass_only_scans_the_fresh_gap(
    database,
) -> None:
    """Once a pass covered ``floor..now`` with nothing left pending, the next
    pass only pays for the deals that arrived since - not the whole history
    again on every reconnect."""
    _persist_entry(database, qty=10.0)
    first = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(first.reconcile_executions())
    covered_top_ms = max(to_ms for _, to_ms in _deal_windows(first))

    second = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(second.reconcile_executions())
    windows = _deal_windows(second)

    assert len(windows) <= 2
    assert min(from_ms for from_ms, _ in windows) > (
        covered_top_ms - BROKER_CLOSE_RECONCILE_WINDOW_MS
    )


def test_reconcile_incomplete_coverage_alerts_once_per_process(
    database, monkeypatch, caplog
) -> None:
    """A cap-truncated pass that may hide an open lot's close is an ERROR
    alert - once, not on every pass."""
    import logging

    monkeypatch.setattr(
        "live_runner.ctrader_adapter.BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS",
        1,
    )
    _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(database, deal_list=[])

    with caplog.at_level(logging.ERROR):
        asyncio.run(adapter.reconcile_executions())
        asyncio.run(adapter.reconcile_executions())

    alerts = [
        record
        for record in caplog.records
        if "incomplete deal coverage" in record.getMessage()
    ]
    assert len(alerts) == 1


def test_reconcile_paces_order_reads_like_deal_list_reads(database) -> None:
    """S4: ``OrderListByPositionId``/``OrderDetails`` reads during the pass
    share the same pacing clock as deal-list reads."""
    signal_id = _persist_entry(database, qty=10.0)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[_close_deal(deal_id=9301, timestamp_ms=T1_MS)],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    adapter._broker_close_deal_list_min_interval_seconds = 0.01
    paced_calls = 0
    original_pace = adapter._pace_broker_read

    async def counting_pace(pacer: dict[str, Any]) -> None:
        nonlocal paced_calls
        paced_calls += 1
        await original_pace(pacer)

    adapter._pace_broker_read = counting_pace  # type: ignore[assignment]

    asyncio.run(adapter.reconcile_executions())

    # The per-signal pass also reads OrderDetails for the entry order; only
    # the broker-close pass's reads share the pace clock.
    close_order_details = [
        r
        for r in adapter._test_requests
        if isinstance(r, _OrderDetailsReq) and int(r.orderId) == CLOSE_ORDER_ID
    ]
    broker_close_reads = (
        len(_deal_list_requests(adapter))
        + len(_order_list_requests(adapter))
        + len(close_order_details)
    )
    assert broker_close_reads > 0
    assert paced_calls == broker_close_reads


# ─── bundle-78 findings: per-deal flag resolution, cursor coverage, retry ─────────────────────


def _resolved_flag_rows(database) -> list[dict[str, Any]]:
    import shared.database as db

    return [
        row
        for row in database.list_live_control_audit(limit=200)
        if row["action"] == db.LEDGER_RECONCILIATION_RESOLVED_AUDIT_ACTION
    ]


def test_a_booked_close_does_not_resolve_a_sibling_unresolved_flag(database) -> None:
    """B1: flag resolution is per ``account:symbol``, so booking deal A must
    not clear deal B's still-outstanding flag - the loss-breaker halt has to
    stay while the ledger is still wrong."""
    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    deal_a = _close_deal(
        deal_id=9301, order_id=CLOSE_ORDER_ID, timestamp_ms=now_ms - 26 * 3_600_000
    )
    deal_b = _close_deal(
        deal_id=9302, order_id=779, volume=5.0, timestamp_ms=now_ms - 2 * 3_600_000
    )
    deal_b.positionId = POSITION_ID + 1  # a different position, no opener

    first = _reconcilable_adapter(
        database, position_orders={}, deal_list=[deal_a, deal_b]
    )
    asyncio.run(first.reconcile_executions())
    assert len(_reconciliation_flags(database)) == 2

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal_a, deal_b],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(second.reconcile_executions())

    # Deal A booked; deal B is still unattributable, so the target stays
    # unresolved and no resolution row may be written.
    assert _ledger(database) == pytest.approx(0.0)
    assert _resolved_flag_rows(database) == []
    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == [
        f"{ACCOUNT}:AAPL"
    ]


def test_a_later_deterministic_flag_survives_a_reconnect_pass(database) -> None:
    """B1: a booked-and-resolved deal must not be re-fetched (and re-resolved)
    on reconnect, and a later deterministic flag on the same symbol must not
    be cleared by it."""
    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    # A PARTIAL close: 6 units stay open, so the scan floor keeps reaching
    # back to the deal's era and its flag retry window is not clamped away.
    deal = _close_deal(
        deal_id=9401, volume=4.0, timestamp_ms=T0_MS + 3_600_000
    )
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal],
    )
    inner_send = first._send_request_with_refresh_retry

    async def unreadable_order_list(request: Any, **kwargs: Any) -> Any:
        if isinstance(request, _OrderListReq):
            return object()
        return await inner_send(request, **kwargs)

    first._send_request_with_refresh_retry = unreadable_order_list  # type: ignore[assignment]
    asyncio.run(first.reconcile_executions())
    assert len(_reconciliation_flags(database)) == 1

    # The broker recovers: the flagged deal books and - being the only
    # outstanding flag on the target - the flag resolves once.
    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal],
        order_details={
            CLOSE_ORDER_ID: _closing_order(order_id=CLOSE_ORDER_ID, volume=4.0)
        },
    )
    asyncio.run(second.reconcile_executions())
    assert _ledger(database) == pytest.approx(6.0)
    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == []
    assert len(_resolved_flag_rows(database)) == 1

    # The stream then flags a genuine deterministic cause on the same symbol.
    db.flag_live_ledger_reconciliation(
        account_id=ACCOUNT,
        symbol="AAPL",
        cause="ambiguous_parent",
        detail={"deal_id": 9555, "position_id": POSITION_ID + 9},
    )
    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == [
        f"{ACCOUNT}:AAPL"
    ]

    # A reconnect pass must not re-fetch the booked deal's retry window, and
    # even a dedupe hit may not resolve over the deterministic flag.
    third = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal],
        order_details={
            CLOSE_ORDER_ID: _closing_order(order_id=CLOSE_ORDER_ID, volume=4.0)
        },
    )
    asyncio.run(third.reconcile_executions())

    assert db.list_unresolved_ledger_reconciliation_flags(ACCOUNT) == [
        f"{ACCOUNT}:AAPL"
    ]
    assert len(_resolved_flag_rows(database)) == 1
    # The booked deal's window was not re-queued: the only deal-list request
    # is the fresh gap, which does not reach back to the old deal.
    deal_ts = T0_MS + 3_600_000
    assert all(
        not (from_ms <= deal_ts < to_ms)
        for from_ms, to_ms in _deal_windows(third)
    )


def _strip_hasmore_parent_rows(adapter: CTraderLiveAdapter) -> None:
    """Make ``hasMore`` windows return no rows, like a >maxRows account."""
    inner_send = adapter._send_request_with_refresh_retry

    async def stripped(request: Any, **kwargs: Any) -> Any:
        response = await inner_send(request, **kwargs)
        if isinstance(request, _DealListReq) and getattr(
            response, "hasMore", False
        ):
            response.deal = []
        return response

    adapter._send_request_with_refresh_retry = stripped  # type: ignore[assignment]


def test_reconcile_requeues_a_failed_hasmore_subwindow(database, caplog) -> None:
    """B2: a ``hasMore`` sub-window that fails is uncovered, not covered - it
    must go back on the cursor and raise the coverage alert."""
    import logging

    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = BROKER_CLOSE_RECONCILE_WINDOW_MS
    close = _close_deal(deal_id=9601, timestamp_ms=now_ms - 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_has_more=lambda from_ms, to_ms: (
            to_ms - from_ms == window_ms and to_ms > now_ms - 86_400_000
        ),
        deal_list_fail_predicate=lambda from_ms, _to_ms: (
            TimeoutError("sub-window timed out")
            if from_ms > now_ms - 4 * 86_400_000
            else None
        ),
    )
    _strip_hasmore_parent_rows(adapter)

    with caplog.at_level(logging.ERROR):
        asyncio.run(adapter.reconcile_executions())

    # The close sat in the failed half: nothing was booked, the range is
    # pending, and the coverage gap alerts.
    assert _ledger(database) == pytest.approx(10.0)
    assert any(
        "incomplete deal coverage" in record.getMessage()
        for record in caplog.records
    )

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(second.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_requeues_a_budget_cut_hasmore_subwindow(
    database, monkeypatch, caplog
) -> None:
    """B2/P3: a request budget that runs out mid-subdivision leaves the
    unfetched half pending, not silently covered."""
    import logging

    monkeypatch.setattr(
        "live_runner.ctrader_adapter.BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS",
        2,
    )
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = BROKER_CLOSE_RECONCILE_WINDOW_MS
    close = _close_deal(deal_id=9602, timestamp_ms=now_ms - 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
        deal_list_has_more=lambda from_ms, to_ms: (
            to_ms - from_ms == window_ms and to_ms > now_ms - 86_400_000
        ),
    )
    _strip_hasmore_parent_rows(adapter)

    with caplog.at_level(logging.ERROR):
        asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)
    assert any(
        "incomplete deal coverage" in record.getMessage()
        for record in caplog.records
    )

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(second.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_retries_a_deal_whose_processing_timed_out(database) -> None:
    """B3: a deal skipped by a transient processing error keeps its window
    pending - the cursor may not advance past an unbooked deal."""
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    close = _close_deal(deal_id=9701, timestamp_ms=now_ms - 2 * 86_400_000)
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    inner_send = first._send_request_with_refresh_retry

    async def timeout_order_list(request: Any, **kwargs: Any) -> Any:
        if isinstance(request, _OrderListReq):
            raise TimeoutError("order list timed out")
        return await inner_send(request, **kwargs)

    first._send_request_with_refresh_retry = timeout_order_list  # type: ignore[assignment]
    asyncio.run(first.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)
    assert _reconciliation_flags(database) == []

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    asyncio.run(second.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_retries_unprocessed_deals_after_an_order_list_api_error(
    database,
) -> None:
    """B3: a ``CTraderApiError`` aborts the pass, but the cursor persisted
    afterwards still carries the failed deal AND every later deal."""
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    deal_old = _close_deal(
        deal_id=9801, order_id=CLOSE_ORDER_ID, volume=4.0,
        timestamp_ms=now_ms - 2 * 86_400_000,
    )
    deal_new = _close_deal(
        deal_id=9802, order_id=779, volume=6.0,
        timestamp_ms=now_ms - 86_400_000,
    )
    order_details = {
        CLOSE_ORDER_ID: _closing_order(order_id=CLOSE_ORDER_ID, volume=4.0),
        779: _closing_order(order_id=779, volume=6.0),
    }
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal_old, deal_new],
        order_details=order_details,
    )
    inner_send = first._send_request_with_refresh_retry
    failed_once = {"done": False}

    async def fail_order_list_once(request: Any, **kwargs: Any) -> Any:
        if isinstance(request, _OrderListReq) and not failed_once["done"]:
            failed_once["done"] = True
            raise CTraderApiError("TEST_FAILURE", "order list failed")
        return await inner_send(request, **kwargs)

    first._send_request_with_refresh_retry = fail_order_list_once  # type: ignore[assignment]
    with pytest.raises(CTraderApiError):
        asyncio.run(first.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal_old, deal_new],
        order_details=order_details,
    )
    asyncio.run(second.reconcile_executions())

    # Both the failed deal and the one never reached are booked on pass 2.
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_retries_unprocessed_deals_after_a_cancellation(
    database,
) -> None:
    """C1: ``asyncio.CancelledError`` is a ``BaseException`` - the ``finally``
    used to persist the cursor as fully covered anyway, and every deal not
    yet processed was lost forever. The interrupted deal AND the one never
    reached must go back on the cursor so the next healthy pass books them."""
    signal_id = _persist_entry(database, qty=10.0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    deal_old = _close_deal(
        deal_id=9811, order_id=CLOSE_ORDER_ID, volume=4.0,
        timestamp_ms=now_ms - 2 * 86_400_000,
    )
    deal_new = _close_deal(
        deal_id=9812, order_id=779, volume=6.0,
        timestamp_ms=now_ms - 86_400_000,
    )
    order_details = {
        CLOSE_ORDER_ID: _closing_order(order_id=CLOSE_ORDER_ID, volume=4.0),
        779: _closing_order(order_id=779, volume=6.0),
    }
    first = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal_old, deal_new],
        order_details=order_details,
    )
    inner_send = first._send_request_with_refresh_retry
    cancelled_once = {"done": False}

    async def cancel_order_list_once(request: Any, **kwargs: Any) -> Any:
        if isinstance(request, _OrderListReq) and not cancelled_once["done"]:
            cancelled_once["done"] = True
            raise asyncio.CancelledError()
        return await inner_send(request, **kwargs)

    first._send_request_with_refresh_retry = cancel_order_list_once  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(first.reconcile_executions())

    assert _ledger(database) == pytest.approx(10.0)

    second = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[deal_old, deal_new],
        order_details=order_details,
    )
    asyncio.run(second.reconcile_executions())

    # The cancelled deal and the one never reached are both booked on pass 2.
    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_persists_the_scan_cursor_off_the_event_loop(
    database, monkeypatch
) -> None:
    """S-new: the cursor write runs through ``asyncio.to_thread`` like every
    other DB call in the pass - a locked SQLite file must not stall the event
    loop (and every cTrader connection's heartbeats) for the 30 s busy
    timeout."""
    import threading

    import shared.database as db

    signal_id = _persist_entry(database, qty=10.0)
    close = _close_deal(deal_id=9903, timestamp_ms=T0_MS + 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )
    write_threads: list[threading.Thread] = []
    real_set_state = db.set_live_adapter_state

    def recording_set_state(*args: Any, **kwargs: Any) -> Any:
        write_threads.append(threading.current_thread())
        return real_set_state(*args, **kwargs)

    monkeypatch.setattr(
        "live_runner.ctrader_adapter.set_live_adapter_state",
        recording_set_state,
    )
    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)
    assert write_threads
    main_thread = threading.main_thread()
    assert all(thread is not main_thread for thread in write_threads)


def test_reconcile_scan_gap_overlaps_the_previous_covered_top(database) -> None:
    """S1: the fresh gap starts a margin BELOW the last covered top, so a
    deal the deal list had not yet shown - or one stamped before it by clock
    skew - is re-read instead of skipped forever."""
    from live_runner.ctrader_adapter import BROKER_CLOSE_SCAN_CURSOR_OVERLAP_MS

    _persist_entry(database, qty=10.0)
    first = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(first.reconcile_executions())
    covered_top_ms = max(to_ms for _, to_ms in _deal_windows(first))

    second = _reconcilable_adapter(database, deal_list=[])
    asyncio.run(second.reconcile_executions())
    windows = _deal_windows(second)

    assert windows
    min_from_ms = min(from_ms for from_ms, _ in windows)
    expected_from_ms = covered_top_ms - BROKER_CLOSE_SCAN_CURSOR_OVERLAP_MS
    assert abs(min_from_ms - expected_from_ms) < 60_000


def test_reconcile_state_read_failure_still_books_the_close(
    database, monkeypatch
) -> None:
    """S2: an unreadable scan cursor degrades to the full floor..now scan -
    it must not abort the pass."""
    signal_id = _persist_entry(database, qty=10.0)
    close = _close_deal(deal_id=9901, timestamp_ms=T0_MS + 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    def unreadable_state(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        "live_runner.ctrader_adapter.get_live_adapter_state", unreadable_state
    )
    asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)


def test_reconcile_state_write_failure_still_books_the_close(
    database, monkeypatch, caplog
) -> None:
    """S2: a cursor that cannot be persisted is an ERROR, not an abort - the
    gathered deals are still processed and the next pass rescans."""
    import logging

    signal_id = _persist_entry(database, qty=10.0)
    close = _close_deal(deal_id=9902, timestamp_ms=T0_MS + 3_600_000)
    adapter = _reconcilable_adapter(
        database,
        position_orders={POSITION_ID: [_entry_order(signal_id)]},
        deal_list=[close],
        order_details={CLOSE_ORDER_ID: _closing_order()},
    )

    def unwritable_state(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        "live_runner.ctrader_adapter.set_live_adapter_state", unwritable_state
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(adapter.reconcile_executions())

    assert _ledger(database) == pytest.approx(0.0)
    assert any(
        "scan cursor could not be persisted" in record.getMessage()
        for record in caplog.records
    )


def test_a_flag_without_deal_timestamp_retries_from_its_audit_time(database) -> None:
    """S3: flags written before ``deal_timestamp_ms`` existed still get a
    retry window, derived from the flag row's own timestamp."""
    import shared.database as db

    flag_at = datetime.now(timezone.utc) - timedelta(hours=2)
    with db.get_session() as session:
        session.add(
            db.LiveControlAuditModel(
                id="legacy-flag-1",
                created_at=flag_at.replace(tzinfo=None),
                actor="live-runner",
                action=db.LEDGER_RECONCILIATION_AUDIT_ACTION,
                target_type="account_symbol",
                target_id=f"{ACCOUNT}:AAPL",
                succeeded=True,
                detail_json={
                    "account_id": ACCOUNT,
                    "symbol": "AAPL",
                    "cause": "position_orders_unreadable",
                    "deal_id": 424242,
                    "source": "reconcile",
                },
            )
        )
        session.commit()
    # A completed cursor so the only pending ranges are the fresh gap and
    # the flag retry window.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    floor_ms = now_ms - BROKER_CLOSE_RECONCILE_WINDOW_MS
    db.set_live_adapter_state(
        ACCOUNT,
        "ctrader_broker_close_scan",
        {"pending": [], "floor_ms": floor_ms, "top_ms": now_ms},
    )
    adapter = _reconcilable_adapter(database, deal_list=[])

    pending = asyncio.run(
        adapter._broker_close_scan_pending_ranges(floor_ms=floor_ms, now_ms=now_ms)
    )

    flag_ms = int(flag_at.timestamp() * 1000)
    assert any(
        start <= flag_ms <= end and end - start <= 700_000
        for start, end in pending
    )


def test_adapter_state_keys_from_two_brokers_coexist_on_one_account(database) -> None:
    """The shared ``live_adapter_state`` table namespaces keys per adapter:
    a cTrader scan cursor and an Alpaca watermark on one account id must not
    collide (#350/#415 reconciliation)."""
    import shared.database as db

    db.set_live_adapter_state(ACCOUNT, "ctrader_broker_close_scan", {"top_ms": 1})
    db.set_live_adapter_state(
        ACCOUNT, "alpaca_reprotect_reconcile_after", {"mark": 2}
    )

    assert db.get_live_adapter_state(ACCOUNT, "ctrader_broker_close_scan") == {
        "top_ms": 1
    }
    assert db.get_live_adapter_state(
        ACCOUNT, "alpaca_reprotect_reconcile_after"
    ) == {"mark": 2}

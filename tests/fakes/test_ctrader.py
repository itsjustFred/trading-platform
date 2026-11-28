"""Contract tests: the cTrader fake behaves like the broker.

These pin the *fake's* broker behaviour, not any adapter. A test module that
migrates onto :class:`FakeCTraderBroker`/:class:`FakeCTraderClient` relies on
every one of these, so a fake that silently stopped modelling cTrader breaks
here first.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.fakes import ctrader as fake_module
from tests.fakes.ctrader import (
    FakeCTraderBroker,
    FakeCTraderClient,
    FakeCTraderSymbol,
)

ACCOUNT = 46736452
SYMBOL_ID = 1


def _types():
    """The real protobuf classes, loaded through the fake's reactor-safe path."""
    fake_module._ensure_types()
    return fake_module._m, fake_module._mm


def _broker(**kwargs) -> FakeCTraderBroker:
    kwargs.setdefault("account_id", ACCOUNT)
    kwargs.setdefault(
        "symbols",
        [FakeCTraderSymbol.fx("EURUSD", SYMBOL_ID), FakeCTraderSymbol.fx("XAUUSD", 2)],
    )
    kwargs.setdefault("mark_prices", {"EURUSD": 1.10000, "XAUUSD": 2000.0})
    return FakeCTraderBroker(**kwargs)


def _new_order(m, **overrides):
    req = m.ProtoOANewOrderReq()
    req.ctidTraderAccountId = ACCOUNT
    req.symbolId = overrides.pop("symbolId", SYMBOL_ID)
    req.orderType = overrides.pop("orderType", 1)  # MARKET
    req.tradeSide = overrides.pop("tradeSide", 1)  # BUY
    req.volume = overrides.pop("volume", 100_000)
    req.timeInForce = overrides.pop("timeInForce", 3)  # IMMEDIATE_OR_CANCEL
    for name, value in overrides.items():
        setattr(req, name, value)
    return req


def _execution_type(event) -> int:
    return int(event.executionType)


# ── session setup requests ───────────────────────────────────────────────────


def test_application_and_account_auth_answer_with_the_configured_account() -> None:
    m, _mm = _types()
    broker = _broker()

    app_res = broker.handle(m.ProtoOAApplicationAuthReq(clientId="app", clientSecret="s"))
    assert type(app_res).__name__ == "ProtoOAApplicationAuthRes"

    auth_res = broker.handle(
        m.ProtoOAAccountAuthReq(ctidTraderAccountId=ACCOUNT, accessToken="tok")
    )
    assert type(auth_res).__name__ == "ProtoOAAccountAuthRes"
    assert auth_res.ctidTraderAccountId == ACCOUNT


def test_symbols_list_and_symbol_by_id_carry_the_broker_properties() -> None:
    m, _mm = _types()
    broker = _broker()

    listing = broker.handle(
        m.ProtoOASymbolsListReq(ctidTraderAccountId=ACCOUNT, includeArchivedSymbols=False)
    )
    assert [s.symbolName for s in listing.symbol] == ["EURUSD", "XAUUSD"]

    detail_req = m.ProtoOASymbolByIdReq(ctidTraderAccountId=ACCOUNT)
    detail_req.symbolId.extend([SYMBOL_ID, 999])
    detail = broker.handle(detail_req)
    assert len(detail.symbol) == 1, "an unknown symbol id is skipped, not invented"
    symbol = detail.symbol[0]
    assert symbol.symbolId == SYMBOL_ID
    assert symbol.digits == 5
    assert symbol.lotSize == 100_000
    assert symbol.minVolume == 1_000


def test_trader_reports_balance_digits_leverage_and_account_type() -> None:
    m, _mm = _types()
    broker = _broker(account_type="hedged", balance=25_000.0, money_digits=2)

    res = broker.handle(m.ProtoOATraderReq(ctidTraderAccountId=ACCOUNT))

    trader = res.trader
    assert res.ctidTraderAccountId == ACCOUNT
    assert trader.balance == 2_500_000
    assert trader.moneyDigits == 2
    # proto2-optional: the field must be PRESENT or the adapter reads "unknown".
    assert trader.HasField("accountType")
    assert int(trader.accountType) == 0  # HEDGED


# ── acknowledgement before the fill ──────────────────────────────────────────


def test_a_submission_is_acknowledged_and_is_not_a_fill() -> None:
    m, _mm = _types()
    broker = _broker()

    event = broker.handle(_new_order(m))

    assert type(event).__name__ == "ProtoOAExecutionEvent"
    assert _execution_type(event) == 2  # ORDER_ACCEPTED
    assert int(event.order.orderStatus) == 1  # ACCEPTED
    assert broker.positions == {}, "nothing is on the book until advance() fills"
    assert len(broker.orders) == 1


def test_the_fill_lands_on_advance_and_opens_a_position_and_a_deal() -> None:
    m, _mm = _types()
    broker = _broker()
    accepted = broker.handle(_new_order(m))
    order_id = int(accepted.order.orderId)

    events = broker.advance(1.0)

    assert len(events) == 1
    event = events[0]
    assert _execution_type(event) == 3  # ORDER_FILLED
    assert int(event.order.orderStatus) == 2  # FILLED
    assert int(event.order.executedVolume) == 100_000
    assert event.HasField("position")
    position = event.position
    assert int(position.positionId) > 0
    assert int(position.tradeData.volume) == 100_000
    assert int(position.tradeData.tradeSide) == 1

    assert len(broker.deals) == 1
    deal = broker.deals[0]
    assert int(deal.orderId) == order_id
    assert int(deal.positionId) == int(position.positionId)
    assert int(deal.volume) == 100_000
    assert int(deal.tradeSide) == 1
    assert int(deal.dealStatus) == 2  # FILLED
    assert int(deal.commission) == -1, "cTrader reports a charge as negative cash flow"
    assert not deal.HasField("closePositionDetail"), "an entry deal has no close detail"


def test_a_sell_entry_opens_a_short_side_position() -> None:
    m, _mm = _types()
    broker = _broker()

    broker.handle(_new_order(m, tradeSide=2))
    events = broker.advance(1.0)

    assert int(events[0].position.tradeData.tradeSide) == 2
    assert int(events[0].position.tradeData.volume) == 100_000


# ── netting vs hedging ───────────────────────────────────────────────────────


def _entry(broker, m, *, side=1, volume=100_000):
    broker.handle(_new_order(m, tradeSide=side, volume=volume))
    return broker.advance(1.0)[0]


def test_a_netted_account_merges_opposite_fills_on_one_symbol() -> None:
    m, _mm = _types()
    broker = _broker(account_type="netted")
    first = _entry(broker, m)
    second = _entry(broker, m, side=2, volume=40_000)

    assert int(first.position.positionId) == int(second.position.positionId), (
        "a netted account reuses the symbol's open position"
    )
    position = broker.positions[int(first.position.positionId)]
    assert int(position.proto.tradeData.volume) == 60_000
    assert int(position.proto.tradeData.tradeSide) == 1


def test_a_netted_account_flips_side_when_a_fill_overshoots() -> None:
    m, _mm = _types()
    broker = _broker(account_type="netted")
    _entry(broker, m, volume=100_000)
    flipped = _entry(broker, m, side=2, volume=150_000)

    position = broker.positions[int(flipped.position.positionId)]
    assert int(position.proto.tradeData.volume) == 50_000
    assert int(position.proto.tradeData.tradeSide) == 2, "the overshoot reopens short"


def test_a_hedged_account_opens_a_fresh_position_per_entry() -> None:
    m, _mm = _types()
    broker = _broker(account_type="hedged")
    first = _entry(broker, m)
    second = _entry(broker, m, side=2)

    assert int(first.position.positionId) != int(second.position.positionId)
    assert len(broker.positions) == 2


# ── partial fills ────────────────────────────────────────────────────────────


def test_a_partially_filled_order_rests_for_the_remainder() -> None:
    m, _mm = _types()
    broker = _broker(partial_fill_ratio=0.5)
    broker.handle(_new_order(m, volume=100_000, timeInForce=2))  # GTC: rests

    first = broker.advance(1.0)
    assert _execution_type(first[0]) == 11  # ORDER_PARTIAL_FILL
    record = next(iter(broker.orders.values()))
    assert int(record.proto.orderStatus) == 1  # still ACCEPTED
    assert record.executed_volume == 50_000

    for _ in range(25):
        broker.advance(1.0)
        if int(record.proto.orderStatus) == 2:
            break
    assert int(record.proto.orderStatus) == 2  # FILLED
    assert record.executed_volume == 100_000
    position = next(iter(broker.positions.values()))
    assert int(position.proto.tradeData.volume) == 100_000


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_submission_the_broker_refuses_answers_an_order_error_event() -> None:
    m, _mm = _types()
    broker = _broker(reject_submissions=lambda req: "insufficient margin")

    res = broker.handle(_new_order(m))

    assert type(res).__name__ == "ProtoOAOrderErrorEvent"
    assert res.errorCode == "ORDER_REJECTED"
    assert "insufficient margin" in res.description
    assert broker.orders == {}


def test_an_unknown_symbol_and_a_bad_volume_are_refused() -> None:
    m, _mm = _types()
    broker = _broker()

    missing = broker.handle(_new_order(m, symbolId=999))
    assert type(missing).__name__ == "ProtoOAOrderErrorEvent"
    assert missing.errorCode == "SYMBOL_NOT_FOUND"

    tiny = broker.handle(_new_order(m, volume=1))
    assert type(tiny).__name__ == "ProtoOAOrderErrorEvent"
    assert tiny.errorCode == "TRADING_BAD_VOLUME"


def test_a_rejection_after_acceptance_lands_on_a_later_advance() -> None:
    """The #428 shape: ORDER_ACCEPTED on submit, ORDER_REJECTED on the next tick."""
    m, _mm = _types()
    broker = _broker(reject_after_acceptance=lambda req: "rejected downstream")
    accepted = broker.handle(_new_order(m))
    assert _execution_type(accepted) == 2

    events = broker.advance(1.0)

    assert len(events) == 1
    assert _execution_type(events[0]) == 7  # ORDER_REJECTED
    assert events[0].errorCode == "ORDER_REJECTED"
    assert int(events[0].order.orderStatus) == 3  # REJECTED
    assert broker.positions == {}


# ── cancels, closes, SL/TP amendments ────────────────────────────────────────


def test_a_cancel_settles_the_working_order() -> None:
    m, _mm = _types()
    broker = _broker()
    accepted = broker.handle(_new_order(m, orderType=2, limitPrice=0.5))  # LIMIT, unfilled
    order_id = int(accepted.order.orderId)

    res = broker.handle(m.ProtoOACancelOrderReq(ctidTraderAccountId=ACCOUNT, orderId=order_id))

    assert _execution_type(res) == 5  # ORDER_CANCELLED
    assert int(res.order.orderStatus) == 5
    assert broker.orders[order_id].proto.orderStatus == 5

    again = broker.handle(
        m.ProtoOACancelOrderReq(ctidTraderAccountId=ACCOUNT, orderId=order_id)
    )
    assert type(again).__name__ == "ProtoOAOrderErrorEvent"
    assert again.errorCode == "ORDER_NOT_FOUND"


def test_close_position_fills_a_closing_order_and_records_close_detail() -> None:
    m, _mm = _types()
    broker = _broker()
    entry = _entry(broker, m)
    position_id = int(entry.position.positionId)

    res = broker.handle(
        m.ProtoOAClosePositionReq(ctidTraderAccountId=ACCOUNT, positionId=position_id, volume=0)
    )

    assert _execution_type(res) == 3  # ORDER_FILLED
    assert int(res.order.closingOrder) == 1 if res.order.HasField("closingOrder") else True
    assert int(res.deal.positionId) == position_id
    assert res.deal.HasField("closePositionDetail")
    assert int(res.deal.closePositionDetail.closedVolume) == 100_000
    assert position_id not in broker.positions, "a full close drops the position"


def test_close_position_partial_volume_leaves_the_remainder() -> None:
    m, _mm = _types()
    broker = _broker()
    entry = _entry(broker, m, volume=100_000)
    position_id = int(entry.position.positionId)

    broker.handle(
        m.ProtoOAClosePositionReq(
            ctidTraderAccountId=ACCOUNT, positionId=position_id, volume=40_000
        )
    )

    position = broker.positions[position_id]
    assert int(position.proto.tradeData.volume) == 60_000


def test_amend_position_sltp_sets_and_clears_levels() -> None:
    m, _mm = _types()
    broker = _broker()
    entry = _entry(broker, m)
    position_id = int(entry.position.positionId)

    res = broker.handle(
        m.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=ACCOUNT, positionId=position_id, stopLoss=1.05, takeProfit=1.20
        )
    )
    position = broker.positions[position_id]
    assert position.proto.stopLoss == pytest.approx(1.05)
    assert position.proto.takeProfit == pytest.approx(1.20)
    assert _execution_type(res) == 2  # ORDER_ACCEPTED

    broker.handle(
        m.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=ACCOUNT, positionId=position_id, stopLoss=0.0
        )
    )
    assert not position.proto.HasField("stopLoss"), "an explicit 0 clears the stop"
    assert position.proto.takeProfit == pytest.approx(1.20), "an absent TP is untouched"


def test_amend_position_sltp_on_an_unknown_position_is_an_order_error() -> None:
    m, _mm = _types()
    broker = _broker()

    res = broker.handle(
        m.ProtoOAAmendPositionSLTPReq(ctidTraderAccountId=ACCOUNT, positionId=42, stopLoss=1.0)
    )

    assert type(res).__name__ == "ProtoOAOrderErrorEvent"
    assert res.errorCode == "POSITION_NOT_FOUND"


# ── protection triggers ──────────────────────────────────────────────────────


def test_a_stop_loss_closes_the_position_when_the_mark_crosses() -> None:
    m, _mm = _types()
    broker = _broker()
    entry = _entry(broker, m)
    position_id = int(entry.position.positionId)
    broker.handle(
        m.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=ACCOUNT, positionId=position_id, stopLoss=1.09
        )
    )

    broker.set_mark_price("EURUSD", 1.08)
    events = broker.advance(1.0)

    assert len(events) == 1
    assert _execution_type(events[0]) == 3  # ORDER_FILLED (the closing order)
    assert position_id not in broker.positions
    closing = events[0].order
    assert int(closing.tradeData.tradeSide) == 2  # BUY position closes with SELL
    assert events[0].deal.HasField("closePositionDetail")


def test_a_take_profit_closes_the_position_when_the_mark_reaches_it() -> None:
    m, _mm = _types()
    broker = _broker()
    entry = _entry(broker, m)
    position_id = int(entry.position.positionId)
    broker.handle(
        m.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=ACCOUNT, positionId=position_id, takeProfit=1.12
        )
    )

    broker.set_mark_price("EURUSD", 1.13)
    events = broker.advance(1.0)

    assert len(events) == 1
    assert position_id not in broker.positions


def test_entry_relative_sltp_becomes_absolute_position_levels() -> None:
    """``relativeStopLoss``/``relativeTakeProfit`` land as price distances."""
    m, _mm = _types()
    broker = _broker()
    # mark 1.10; relative distances in 1/100000 units: 0.05 -> 5000, 0.10 -> 10000.
    broker.handle(_new_order(m, relativeStopLoss=5000, relativeTakeProfit=10000))
    events = broker.advance(1.0)

    position = events[0].position
    assert position.stopLoss == pytest.approx(1.05)
    assert position.takeProfit == pytest.approx(1.20)


# ── time in force ────────────────────────────────────────────────────────────


def test_a_good_till_date_order_expires_at_its_timestamp() -> None:
    m, _mm = _types()
    broker = _broker()
    expiry = broker.now_ms + 60_000
    accepted = broker.handle(
        _new_order(
            m,
            orderType=2,  # LIMIT
            limitPrice=0.5,
            timeInForce=1,  # GOOD_TILL_DATE
            expirationTimestamp=expiry,
        )
    )
    order_id = int(accepted.order.orderId)

    broker.advance(30.0)
    assert broker.orders[order_id].proto.orderStatus == 1, "still working before expiry"

    broker.advance(60.0)
    record = broker.orders[order_id]
    assert int(record.proto.orderStatus) == 4  # EXPIRED
    expired = [e for e in broker.events if _execution_type(e) == 6]
    assert len(expired) == 1
    assert expired[0].errorCode == "ORDER_EXPIRED"


def test_a_good_till_cancel_order_survives_past_a_day_order_expiry() -> None:
    m, _mm = _types()
    broker = _broker()
    accepted = broker.handle(
        _new_order(m, orderType=2, limitPrice=0.5, timeInForce=2)  # GTC
    )
    order_id = int(accepted.order.orderId)

    broker.advance(48 * 3600)

    assert broker.orders[order_id].proto.orderStatus == 1


def test_an_ioc_order_that_cannot_fill_is_cancelled_not_left_working() -> None:
    m, _mm = _types()
    broker = _broker()
    accepted = broker.handle(
        _new_order(m, orderType=2, limitPrice=0.5, timeInForce=3)  # IOC, mark 1.10
    )
    order_id = int(accepted.order.orderId)

    events = broker.advance(1.0)

    assert int(broker.orders[order_id].proto.orderStatus) == 5  # CANCELLED
    assert _execution_type(events[-1]) == 5


# ── listings ─────────────────────────────────────────────────────────────────


def _list_orders(broker, m, **kwargs):
    req = m.ProtoOAOrderListReq(ctidTraderAccountId=ACCOUNT)
    for name, value in kwargs.items():
        setattr(req, name, value)
    return broker.handle(req)


def test_order_details_return_the_order_and_its_deals() -> None:
    m, _mm = _types()
    broker = _broker()
    accepted = broker.handle(_new_order(m))
    order_id = int(accepted.order.orderId)
    broker.advance(1.0)

    res = broker.handle(m.ProtoOAOrderDetailsReq(ctidTraderAccountId=ACCOUNT, orderId=order_id))

    assert type(res).__name__ == "ProtoOAOrderDetailsRes"
    assert int(res.order.orderId) == order_id
    assert int(res.order.orderStatus) == 2
    assert len(res.deal) == 1


def test_order_details_for_an_unknown_id_is_a_proto_error_res() -> None:
    m, _mm = _types()
    broker = _broker()

    res = broker.handle(m.ProtoOAOrderDetailsReq(ctidTraderAccountId=ACCOUNT, orderId=424242))

    assert type(res).__name__ == "ProtoOAErrorRes"
    assert res.errorCode == "ORDER_NOT_FOUND"


def test_order_lists_page_at_page_size_and_report_has_more() -> None:
    m, _mm = _types()
    broker = _broker(page_size=2)
    for index in range(5):
        broker.handle(_new_order(m, clientOrderId=f"c-{index}"))

    res = _list_orders(broker, m)
    assert len(res.order) == 2
    assert res.hasMore is True


def test_deal_list_honours_max_rows_and_reports_has_more() -> None:
    m, _mm = _types()
    broker = _broker()
    for _ in range(5):
        _entry(broker, m, side=1 if len(broker.deals) % 2 == 0 else 2, volume=10_000)

    req = m.ProtoOADealListReq(ctidTraderAccountId=ACCOUNT, maxRows=2)
    first = broker.handle(req)
    assert len(first.deal) == 2
    assert first.hasMore is True

    # Narrowing the window under the cap clears hasMore - the adapter's
    # subdivision strategy relies on exactly this.
    req2 = m.ProtoOADealListReq(ctidTraderAccountId=ACCOUNT, maxRows=50)
    full = broker.handle(req2)
    assert len(full.deal) == len(broker.deals)
    assert full.hasMore is False


def test_reconcile_lists_open_positions_and_working_orders() -> None:
    m, _mm = _types()
    broker = _broker()
    _entry(broker, m)
    broker.handle(_new_order(m, orderType=2, limitPrice=0.5, clientOrderId="resting"))

    res = broker.handle(m.ProtoOAReconcileReq(ctidTraderAccountId=ACCOUNT))

    assert len(res.position) == 1
    assert len(res.order) == 1
    assert res.order[0].clientOrderId == "resting"


# ── the client seam ──────────────────────────────────────────────────────────


def test_client_send_resolves_the_deferred_and_routes_by_client_msg_id() -> None:
    broker = _broker()
    client = FakeCTraderClient(broker)
    client.startService()
    _m, _mm = _types()

    seen: list[object] = []
    client.setMessageReceivedCallback(lambda _c, msg: seen.append(msg))

    deferred = client.send(
        _new_order(_m), clientMsgId="acct-1-ProtoOANewOrderReq-1", responseTimeoutInSeconds=5
    )

    assert deferred.called, "the fake answers synchronously, like a fast socket"
    message = deferred.result
    assert message.clientMsgId == "acct-1-ProtoOANewOrderReq-1"
    payload = fake_module._Protobuf.extract(message)
    assert type(payload).__name__ == "ProtoOAExecutionEvent"
    # The response went through the message-received callback first, exactly
    # like upstream Client._received.
    assert seen
    assert seen[0] is message


def test_client_disconnect_clears_pending_deferreds_without_errbacking() -> None:
    """Upstream ``_disconnected`` clears ``_responseDeferreds`` silently; the
    adapter's own in-flight set does the errbacking. The fake mirrors that."""
    broker = _broker()
    client = FakeCTraderClient(broker)
    client.startService()
    _m, _mm = _types()

    deferred = client.send(_new_order(_m), clientMsgId="x")
    assert deferred.called

    client.disconnect("gone")
    assert client.isConnected is False
    assert client.running is False
    assert client._responseDeferreds == {}


# ─-) the adapter seam (subprocess: the reactor must be clean) ────────────────


def test_fake_ctrader_transport_drives_a_real_adapter_connect() -> None:
    """``connect`` must run auth -> symbols -> symbol props -> reconcile on the fake.

    Runs in a subprocess for the same reason the account-discovery reactor
    tests do: ``ctrader_open_api`` binds ``twisted.internet.reactor`` at
    import, and only a fresh interpreter guarantees the asyncio reactor the
    adapter installs is the one the client package binds.
    """
    repo = Path(__file__).resolve().parents[2]
    code = textwrap.dedent(
        """
        import asyncio
        import sys

        sys.path.insert(0, {repo!r})
        from tests.fakes.ctrader import (
            FakeCTraderBroker,
            FakeCTraderSymbol,
            fake_ctrader_transport,
        )
        from live_runner.ctrader_adapter import CTraderLiveAdapter

        broker = FakeCTraderBroker(
            account_id=46736452,
            symbols=[FakeCTraderSymbol.fx("EURUSD", 1)],
            mark_prices={{"EURUSD": 1.1}},
        )
        with fake_ctrader_transport(broker) as (b, client):
            adapter = CTraderLiveAdapter(
                app_id="app",
                app_secret="secret",
                access_token="token",
                account_id="46736452",
                demo=True,
                connect_max_attempts=1,
                connect_timeout_seconds=10,
            )
            asyncio.run(adapter.connect())
            assert adapter._authenticated is True
            assert adapter._symbol_name_to_id.get("EURUSD") == 1
            assert adapter._client is client
            asyncio.run(adapter.disconnect())
        print("ok")
        """
    ).format(repo=str(repo))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(repo),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

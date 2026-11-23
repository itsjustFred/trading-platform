"""cTrader protective-stop listing and in-place SL/TP amendment tests (#168).

Positions are real ``ProtoOAPosition`` messages; the amend request is the real
``ProtoOAAmendPositionSLTPReq``. ``_fetch_open_positions`` is stubbed to a
mutable broker snapshot so tests control exactly what the broker reports
before and after the amend - there is no dedicated amend response message,
so broker state is the only confirmation.
"""
from __future__ import annotations

import asyncio

import pytest

from live_runner.base_adapter import ProtectiveStop
from live_runner.ctrader_adapter import CTraderApiError, CTraderLiveAdapter


def _adapter() -> CTraderLiveAdapter:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._symbol_props = {}
    return adapter


def _position(
    *,
    position_id: int = 7,
    symbol_id: int = 1,
    volume: int = 5000,
    trade_side: int = 1,
    stop_loss: float | None = 1.08000,
    take_profit: float | None = None,
    trailing: bool = False,
):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPosition,
        ProtoOATradeData,
        ProtoOATradeSide,
    )

    position = ProtoOAPosition()
    position.positionId = position_id
    position.positionStatus = 1  # POSITION_STATUS_OPEN
    trade_data = ProtoOATradeData()
    trade_data.symbolId = symbol_id
    trade_data.volume = volume
    trade_data.tradeSide = (
        ProtoOATradeSide.BUY if trade_side == 1 else ProtoOATradeSide.SELL
    )
    position.tradeData.CopyFrom(trade_data)
    if stop_loss is not None:
        position.stopLoss = stop_loss
    if take_profit is not None:
        position.takeProfit = take_profit
    if trailing:
        position.trailingStopLoss = True
    return position


def _wire_positions(adapter: CTraderLiveAdapter, state: dict) -> None:
    async def _fetch_open_positions():
        return list(state["positions"])

    adapter._fetch_open_positions = _fetch_open_positions


def _wire_send(adapter: CTraderLiveAdapter, state: dict) -> None:
    sent = state.setdefault("sent", [])

    async def _send(request, **_kwargs):
        sent.append(request)
        handler = state.get("on_send")
        if handler is not None:
            return handler(request)
        return request

    adapter._send_request_with_refresh_retry = _send


def _wire_amend_types(adapter: CTraderLiveAdapter) -> None:
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAmendPositionSLTPReq,
        ProtoOAOrderErrorEvent,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATradeSide

    adapter._ProtoOAAmendPositionSLTPReq = ProtoOAAmendPositionSLTPReq
    adapter._ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
    adapter._ProtoOATradeSide = ProtoOATradeSide


def test_supports_stop_amendment_is_declared() -> None:
    assert _adapter().supports_stop_amendment is True


def test_list_protective_stops_synthesizes_from_position_stop() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    state = {"positions": [_position(position_id=7, stop_loss=1.08000)]}
    _wire_positions(adapter, state)

    stops = asyncio.run(adapter.list_protective_stops())

    assert len(stops) == 1
    stop = stops[0]
    assert isinstance(stop, ProtectiveStop)
    assert stop.order_id == "7"
    assert stop.symbol == "EURUSD"
    # Long position -> the reducing order is a sell stop.
    assert stop.side == "sell"
    assert stop.stop_price == pytest.approx(1.08000)
    assert stop.qty == pytest.approx(5.0)  # 5000 broker units / 1000 per qty


def test_list_protective_stops_short_position_is_a_buy_stop() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    state = {
        "positions": [
            _position(position_id=8, trade_side=2, stop_loss=1.09000)
        ]
    }
    _wire_positions(adapter, state)

    stops = asyncio.run(adapter.list_protective_stops())

    assert len(stops) == 1
    assert stops[0].side == "buy"
    assert stops[0].order_id == "8"


def test_list_protective_stops_skips_positions_without_a_stop() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    state = {"positions": [_position(position_id=7, stop_loss=None)]}
    _wire_positions(adapter, state)

    assert asyncio.run(adapter.list_protective_stops()) == []


def test_list_protective_stops_skips_broker_trailing_positions() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    state = {"positions": [_position(position_id=7, trailing=True)]}
    _wire_positions(adapter, state)

    assert asyncio.run(adapter.list_protective_stops()) == []


def test_list_protective_stops_fails_closed_on_unknown_symbol() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    state = {"positions": [_position(position_id=7, symbol_id=999)]}
    _wire_positions(adapter, state)

    with pytest.raises(ValueError, match="not in the symbols cache"):
        asyncio.run(adapter.list_protective_stops())


def test_list_protective_stops_fails_closed_on_unreadable_side() -> None:
    """A tradeSide outside BUY/SELL cannot name the reducing side.

    A namespace double because the proto enum is closed and cannot carry an
    out-of-range value; every read in the listing is ``getattr``-based.
    """
    from types import SimpleNamespace

    adapter = _adapter()
    _wire_amend_types(adapter)
    position = SimpleNamespace(
        positionId=7,
        tradeData=SimpleNamespace(symbolId=1, volume=5000, tradeSide=99),
        stopLoss=1.08,
        trailingStopLoss=False,
    )
    state = {"positions": [position]}
    _wire_positions(adapter, state)

    with pytest.raises(ValueError, match="tradeSide"):
        asyncio.run(adapter.list_protective_stops())


def test_round_stop_price_floors_sell_and_ceilings_buy() -> None:
    adapter = _adapter()
    sell = ProtectiveStop(order_id="7", symbol="EURUSD", side="sell", stop_price=1.0)
    buy = ProtectiveStop(order_id="8", symbol="EURUSD", side="buy", stop_price=1.0)

    # EURUSD fallback scale: 5 digits -> 0.00001 tick.
    assert adapter.round_stop_price(sell, 1.08500999) == pytest.approx(1.08500)
    assert adapter.round_stop_price(buy, 1.08500101) == pytest.approx(1.08501)
    with pytest.raises(ValueError, match="Invalid stop price"):
        adapter.round_stop_price(sell, -1.0)
    with pytest.raises(ValueError, match="Invalid stop price"):
        adapter.round_stop_price(sell, float("nan"))


def _amend_state(adapter: CTraderLiveAdapter, positions) -> dict:
    state = {"positions": list(positions)}
    _wire_positions(adapter, state)
    _wire_send(adapter, state)
    _wire_amend_types(adapter)
    return state


def test_amend_sends_absolute_stop_and_preserves_take_profit() -> None:
    adapter = _adapter()
    position = _position(stop_loss=1.08000, take_profit=1.10000)
    state = _amend_state(adapter, [position])

    # The broker applies the amend: the snapshot now reports the new stop.
    position.stopLoss = 1.08500
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )
    result = asyncio.run(adapter.amend_protective_stop(stop, 1.085004))

    assert len(state["sent"]) == 1
    request = state["sent"][0]
    assert int(request.positionId) == 7
    assert request.stopLoss == pytest.approx(1.08500)  # floored to the 5-digit grid
    assert request.takeProfit == pytest.approx(1.10000)  # echoed unchanged
    assert result.order_id == "7"
    assert result.stop_price == pytest.approx(1.08500)


def test_amend_without_position_tp_leaves_take_profit_unset() -> None:
    adapter = _adapter()
    position = _position(stop_loss=1.08000, take_profit=None)
    state = _amend_state(adapter, [position])
    position.stopLoss = 1.08500
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    asyncio.run(adapter.amend_protective_stop(stop, 1.08500))

    request = state["sent"][0]
    assert not request.HasField("takeProfit")


def test_amend_refusal_raises_and_never_confirms() -> None:
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    state = _amend_state(adapter, [position])

    def _refuse(_request):
        raise CTraderApiError("ORDER_REJECTED", "stop on wrong side")

    state["on_send"] = _refuse
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    with pytest.raises(CTraderApiError):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))
    # The broker snapshot never changed: the previous stop stays in force.
    assert position.stopLoss == pytest.approx(1.08000)


def test_amend_order_error_event_is_a_refusal() -> None:
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    state = _amend_state(adapter, [position])

    def _reject(_request):
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAOrderErrorEvent

        return ProtoOAOrderErrorEvent(
            ctidTraderAccountId=46736452,
            errorCode="TRADING_BAD_STOPS",
            description="invalid stop",
        )

    state["on_send"] = _reject
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    with pytest.raises(CTraderApiError, match="TRADING_BAD_STOPS"):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))


def test_amend_timeout_with_confirmed_state_still_succeeds() -> None:
    """An ambiguous send resolves through broker state, not the response."""
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    state = _amend_state(adapter, [position])

    def _applied_then_timeout(_request):
        # The request landed; only the response was lost.
        position.stopLoss = 1.08500
        raise TimeoutError("no response")

    state["on_send"] = _applied_then_timeout
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    result = asyncio.run(adapter.amend_protective_stop(stop, 1.08500))
    assert result.stop_price == pytest.approx(1.08500)


def test_amend_timeout_without_confirmation_fails_closed(monkeypatch) -> None:
    import live_runner.ctrader_adapter as ctrader_adapter

    monkeypatch.setattr(ctrader_adapter, "_AMEND_CONFIRM_INTERVAL_SECONDS", 0.001)
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    state = _amend_state(adapter, [position])

    def _timeout(_request):
        raise TimeoutError("no response")

    state["on_send"] = _timeout
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    with pytest.raises(RuntimeError, match="did not confirm"):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))
    assert position.stopLoss == pytest.approx(1.08000)


def test_amend_position_closed_during_amend_fails_closed(monkeypatch) -> None:
    import live_runner.ctrader_adapter as ctrader_adapter

    monkeypatch.setattr(ctrader_adapter, "_AMEND_CONFIRM_INTERVAL_SECONDS", 0.001)
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    state = _amend_state(adapter, [position])

    def _close_then_timeout(_request):
        state["positions"] = []
        raise TimeoutError("no response")

    state["on_send"] = _close_then_timeout
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    with pytest.raises(RuntimeError, match="closed while"):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))


def test_amend_missing_position_raises_before_sending() -> None:
    adapter = _adapter()
    state = _amend_state(adapter, [])
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    with pytest.raises(RuntimeError, match="no longer open"):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))
    assert state["sent"] == []


def test_amend_rejects_a_stop_without_a_position_id() -> None:
    adapter = _adapter()
    _wire_amend_types(adapter)
    stop = ProtectiveStop(
        order_id="not-a-position", symbol="EURUSD", side="sell", stop_price=1.0
    )

    with pytest.raises(ValueError, match="position id"):
        asyncio.run(adapter.amend_protective_stop(stop, 1.08500))


def test_amend_preserves_gsl_and_trigger_flags() -> None:
    adapter = _adapter()
    position = _position(stop_loss=1.08000)
    position.guaranteedStopLoss = True
    position.stopLossTriggerMethod = 1
    state = _amend_state(adapter, [position])
    position.stopLoss = 1.08500
    stop = ProtectiveStop(
        order_id="7", symbol="EURUSD", side="sell", stop_price=1.08000
    )

    asyncio.run(adapter.amend_protective_stop(stop, 1.08500))

    request = state["sent"][0]
    assert request.guaranteedStopLoss is True
    assert request.HasField("stopLossTriggerMethod")

"""cTrader trendbar ingestion tests (#168).

Decoder tests build real ``ProtoOATrendbar``/``ProtoOAGetTrendbarsRes``
messages from the installed ``ctrader_open_api`` package — no network, no
fake proto shapes. ``get_market_data`` tests stub the request transport
(``_send_request_with_refresh_retry``) and the auth/symbol caches.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_runner.ctrader_adapter import (
    CTraderLiveAdapter,
    decode_trendbar,
    decode_trendbars_response,
)


def _trendbar(
    *,
    low: int,
    delta_open: int = 0,
    delta_high: int = 0,
    delta_close: int = 0,
    volume: int = 0,
    utc_minutes: int | None = None,
):
    """A real ProtoOATrendbar: prices are raw 1/100000 units."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbar

    bar = ProtoOATrendbar()
    bar.low = low
    bar.deltaOpen = delta_open
    bar.deltaHigh = delta_high
    bar.deltaClose = delta_close
    bar.volume = volume
    if utc_minutes is not None:
        bar.utcTimestampInMinutes = utc_minutes
    return bar


def _minutes(dt: datetime) -> int:
    return int(dt.timestamp() // 60)


def test_decode_trendbar_prices_and_timestamp() -> None:
    # EURUSD-style 5-digit symbol: low 1.10000 -> 110000 raw units.
    open_time = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    bar = _trendbar(
        low=110_000,
        delta_open=50,   # open  = 1.10050
        delta_high=120,  # high  = 1.10120
        delta_close=80,  # close = 1.10080
        volume=7,
        utc_minutes=_minutes(open_time),
    )
    row = decode_trendbar(bar, price_digits=5)
    assert row["timestamp"] == open_time
    assert row["open"] == pytest.approx(1.10050)
    assert row["high"] == pytest.approx(1.10120)
    assert row["low"] == pytest.approx(1.10000)
    assert row["close"] == pytest.approx(1.10080)
    assert row["volume"] == 7.0


def test_decode_trendbar_uses_deprecated_timestamp_fallback() -> None:
    open_time = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    bar = _trendbar(low=200_000, delta_high=20, delta_close=10)  # no utcTimestampInMinutes
    row = decode_trendbar(
        bar,
        price_digits=5,
        timestamp_ms_fallback=int(open_time.timestamp() * 1000),
    )
    assert row["timestamp"] == open_time
    assert row["close"] == pytest.approx(2.00010)


def test_decode_trendbar_missing_low_fails_closed() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbar

    bar = ProtoOATrendbar()
    bar.deltaOpen = 1
    bar.deltaHigh = 2
    bar.deltaClose = 3
    bar.utcTimestampInMinutes = 1
    with pytest.raises(ValueError, match="missing required field 'low'"):
        decode_trendbar(bar, price_digits=5)


def test_decode_trendbar_missing_timestamp_fails_closed() -> None:
    bar = _trendbar(low=100_000, delta_high=10, delta_close=5)  # no timestamp at all
    with pytest.raises(ValueError, match="no usable open timestamp"):
        decode_trendbar(bar, price_digits=5)


def test_decode_trendbar_inconsistent_ohlc_fails_closed() -> None:
    # Hand-built inconsistent deltas: high below the open.
    bar = _trendbar(
        low=100_000,
        delta_open=500,
        delta_high=100,   # high = 1.00100 < open = 1.00500
        delta_close=200,
        utc_minutes=1,
    )
    with pytest.raises(ValueError, match="OHLC is inconsistent"):
        decode_trendbar(bar, price_digits=5)


def test_decode_trendbars_response_sorts_oldest_first() -> None:
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes

    t1 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    t0 = t1 - timedelta(minutes=5)
    res = ProtoOAGetTrendbarsRes()
    res.trendbar.append(
        _trendbar(low=100_000, delta_high=10, delta_close=5, utc_minutes=_minutes(t1))
    )
    res.trendbar.append(
        _trendbar(low=100_000, delta_high=10, delta_close=5, utc_minutes=_minutes(t0))
    )
    rows = decode_trendbars_response(res, symbol="EURUSD", price_digits=5)
    assert [row["timestamp"] for row in rows] == [t0, t1]


def _adapter_with_fakes(*, response=None, send_error=None):
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        demo=True,
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 42}
    adapter._symbol_props = {}
    # The fake transport bypasses _load_openapi_types; wire the real message
    # classes the market-data path isinstance-checks and builds.
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetTrendbarsReq,
        ProtoOAGetTrendbarsRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOATrendbarPeriod,
    )

    adapter._ProtoOAGetTrendbarsReq = ProtoOAGetTrendbarsReq
    adapter._ProtoOAGetTrendbarsRes = ProtoOAGetTrendbarsRes
    adapter._ProtoOATrendbarPeriod = ProtoOATrendbarPeriod
    sent = []

    async def _send(request, **_kwargs):
        sent.append(request)
        if send_error is not None:
            raise send_error
        return response

    adapter._send_request_with_refresh_retry = _send  # type: ignore[assignment]
    return adapter, sent


def _bars_response(bars_spec: list[tuple[datetime, float]]) -> object:
    """A real ProtoOAGetTrendbarsRes with flat OHLC bars (delta-encoded)."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes

    res = ProtoOAGetTrendbarsRes()
    for open_time, price in bars_spec:
        raw = round(price * 100_000)
        bar = res.trendbar.add()
        bar.low = raw
        bar.deltaOpen = 0
        bar.deltaHigh = 0
        bar.deltaClose = 0
        bar.volume = 1
        bar.utcTimestampInMinutes = _minutes(open_time)
    return res


def test_get_market_data_decodes_and_drops_forming_bar() -> None:
    now = datetime.now(timezone.utc)
    closed_open = now - timedelta(minutes=10)
    forming_open = now - timedelta(minutes=2)  # 5m bar still forming
    response = _bars_response(
        [
            (closed_open - timedelta(minutes=5), 1.1000),
            (closed_open, 1.1005),
            (forming_open, 1.1010),
        ]
    )
    adapter, sent = _adapter_with_fakes(response=response)
    frame = asyncio.run(
        adapter.get_market_data("eurusd", timeframe="5m", limit=50)
    )
    assert len(sent) == 1
    request = sent[0]
    assert request.symbolId == 42
    assert request.count == 50
    assert list(frame.columns[:6]) == [
        "timestamp", "open", "high", "low", "close", "volume",
    ]
    opens = list(frame["timestamp"])
    assert opens == sorted(opens)
    # The still-forming bar is gone; the two closed bars remain.
    assert len(frame) == 2
    assert frame["close"].iloc[-1] == pytest.approx(1.1005)


def test_get_market_data_rejects_unsupported_timeframe() -> None:
    adapter, sent = _adapter_with_fakes(response=None)
    fetch = adapter.get_market_data("EURUSD", timeframe="2h", limit=10)
    with pytest.raises(ValueError, match="cannot serve '2h'"):
        asyncio.run(fetch)
    assert sent == []


def test_get_market_data_rejects_empty_response() -> None:
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes

    adapter, _ = _adapter_with_fakes(response=ProtoOAGetTrendbarsRes())
    fetch = adapter.get_market_data("EURUSD", timeframe="5m", limit=10)
    with pytest.raises(ValueError, match="no 5m trendbars"):
        asyncio.run(fetch)


def test_get_market_data_rejects_unexpected_response_type() -> None:
    adapter, _ = _adapter_with_fakes(response=SimpleNamespace())
    fetch = adapter.get_market_data("EURUSD", timeframe="5m", limit=10)
    with pytest.raises(RuntimeError, match="Unexpected cTrader trendbars response"):
        asyncio.run(fetch)


def test_get_market_data_connects_when_needed() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        demo=True,
    )
    adapter._connected = False
    adapter._authenticated = False
    connected = []

    async def _connect() -> None:
        connected.append(True)
        adapter._connected = True
        adapter._authenticated = True
        adapter._symbol_name_to_id = {"EURUSD": 42}

    adapter.connect = _connect  # type: ignore[assignment]
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetTrendbarsReq,
        ProtoOAGetTrendbarsRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOATrendbarPeriod,
    )

    adapter._ProtoOAGetTrendbarsReq = ProtoOAGetTrendbarsReq
    adapter._ProtoOAGetTrendbarsRes = ProtoOAGetTrendbarsRes
    adapter._ProtoOATrendbarPeriod = ProtoOATrendbarPeriod
    now = datetime.now(timezone.utc)
    closed_open = now - timedelta(minutes=70)
    response = _bars_response([(closed_open, 1.1)])

    async def _send(request, **_kwargs):
        return response

    adapter._send_request_with_refresh_retry = _send  # type: ignore[assignment]
    frame = asyncio.run(adapter.get_market_data("EURUSD", timeframe="1h", limit=5))
    assert connected == [True]
    assert len(frame) == 1

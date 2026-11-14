"""Asset-class routing for market data (issue #122)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

from live_runner import market_data_stream as mds
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.symbol_class import (
    UnclassifiableSymbolError,
    canonical_crypto_symbol,
    classify_symbol,
)
from shared.models import LiveAccountConfig, LiveDeploymentConfig


@pytest.mark.parametrize(
    "symbol",
    ["BTCUSD", "ETHUSD", "BTC/USD", "BTC-USD", "btcusd", " eth/usd ", "ETHUSDT", "SOLUSDC"],
)
def test_crypto_forms(symbol: str) -> None:
    assert classify_symbol(symbol) == "crypto"
    assert mds._is_crypto_symbol(symbol) is True


@pytest.mark.parametrize("symbol", ["AAPL", "SPY", "BRK.B", "ABUSD", "TUSD", "MUSD"])
def test_equities_including_usd_suffix_stay_stock(symbol: str) -> None:
    assert classify_symbol(symbol) == "stock"
    assert mds._is_crypto_symbol(symbol) is False


@pytest.mark.parametrize("symbol", ["", "  ", "FOOBARUSD", "FOO/USD", "XYZ-USD", "AAPL!"])
def test_unclassifiable_fails_closed(symbol: str) -> None:
    with pytest.raises(UnclassifiableSymbolError):
        classify_symbol(symbol)
    with pytest.raises(UnclassifiableSymbolError):
        mds._is_crypto_symbol(symbol)


def test_canonical_crypto_symbol() -> None:
    assert {canonical_crypto_symbol(s) for s in ("BTCUSD", "btc-usd", "BTC/USD")} == {"BTC/USD"}
    with pytest.raises(UnclassifiableSymbolError):
        canonical_crypto_symbol("AAPL")


def test_refresh_routes_each_symbol_to_its_feed_and_skips_unclassifiable(monkeypatch) -> None:
    started: dict[str, set[str]] = {}

    class _Worker:
        def __init__(self, *, kind, **_kw) -> None:
            self.kind = kind

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def set_symbols(self, symbols) -> None:
            started[self.kind] = set(symbols)

    monkeypatch.setattr(mds, "_StreamWorker", _Worker)
    monkeypatch.setattr(
        mds, "resolve_alpaca_credentials", lambda **_kw: ("key", "secret", True)
    )
    service = mds.LiveMarketDataStreamService()
    deployment = LiveDeploymentConfig(
        strategy_name="s",
        account_id="acc",
        symbols=["BTCUSD", "ETH-USD", "AAPL", "FOOBARUSD"],
        timeframe="1h",
    )
    account = LiveAccountConfig(id="acc", broker="alpaca", broker_login="x")

    asyncio.run(service.refresh_from_deployments([deployment], {"acc": account}))

    assert started["crypto"] == {"BTC/USD", "ETH/USD"}
    assert started["stock"] == {"AAPL"}


def test_bars_for_unslashed_and_slashed_crypto_share_one_key() -> None:
    service = mds.LiveMarketDataStreamService()
    asyncio.run(service.ingest_manual_bars([{"symbol": "BTC/USD", "close": 1.0}]))
    payload = asyncio.run(service.get_quote_payload("BTCUSD"))
    assert payload["latest"]["close"] == 1.0


def test_get_market_data_uses_crypto_client_for_crypto_symbols() -> None:
    calls: dict[str, object] = {}

    class _Crypto:
        def get_crypto_bars(self, request):
            calls["symbols"] = request.symbol_or_symbols
            idx = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")], name="timestamp")
            frame = pd.DataFrame(
                {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}, index=idx
            )
            return SimpleNamespace(df=frame)

    class _Stock:
        def get_stock_bars(self, request):  # pragma: no cover - must not be used
            raise AssertionError("crypto symbol routed to the stock client")

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = _Stock()
    adapter._crypto_data_client = _Crypto()

    frame = asyncio.run(adapter.get_market_data("BTCUSD", timeframe="1h", limit=1))

    assert calls["symbols"] == ["BTC/USD"]
    assert len(frame.index) == 1


def test_get_market_data_rejects_unclassifiable_symbol() -> None:
    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = object()
    with pytest.raises(UnclassifiableSymbolError):
        asyncio.run(adapter.get_market_data("FOOBARUSD", timeframe="1h", limit=1))

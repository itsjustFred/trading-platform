"""Asset-class routing for market data (issue #122)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from live_runner import market_data_stream as mds
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.symbol_class import (
    CRYPTO_QUOTES,
    UnclassifiableSymbolError,
    canonical_crypto_symbol,
    classify_symbol,
)
from shared.models import LiveAccountConfig, LiveDeploymentConfig


@pytest.fixture
def live_runner_logs(caplog):
    """The ``live-runner`` logger does not propagate, so attach caplog's handler."""
    target = logging.getLogger("trading.live-runner")
    target.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="trading.live-runner")
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)


@pytest.mark.parametrize(
    "symbol",
    ["BTCUSD", "ETHUSD", "BTC/USD", "BTC-USD", "btcusd", " eth/usd ", "ETHUSDT", "SOLUSDC"],
)
def test_crypto_forms(symbol: str) -> None:
    assert classify_symbol(symbol) == "crypto"
    assert mds._is_crypto_symbol(symbol) is True


def test_crypto_quotes_come_from_the_shared_alpaca_symbol_module() -> None:
    """One quote list: fee backfill (#124) and market-data routing (#122) must not drift.

    The import already makes this true; the assertion is the drift guard for
    whoever reintroduces a local copy of the tuple.
    """
    from shared import alpaca_symbols

    assert CRYPTO_QUOTES == alpaca_symbols.CRYPTO_QUOTES


@pytest.mark.parametrize("symbol", ["BTCUSD", "BTCUSDT", "ETHUSDC", "SOLBTC", "LINKBTC"])
def test_quote_parsing_matches_shared_alpaca_symbol_module(symbol: str) -> None:
    """Both modules must read the same quote out of the same spelling.

    Scoped to pairs both recognise: the base allowlist here is deliberately
    stricter (that strictness is what makes an unknown base fail closed), so
    ``FOO/USD`` is crypto for the fee backfill and refused for market data.
    """
    from shared.alpaca_symbols import split_crypto_pair

    base, quote = split_crypto_pair(symbol)

    assert canonical_crypto_symbol(symbol) == f"{base}/{quote}"


@pytest.mark.parametrize(
    "symbol",
    # BRK-B / BF-B are real NYSE share-class forms (shared/alpaca_symbols.py accepts
    # them as us_equity); refusing them here would take a valid holding dark.
    ["AAPL", "SPY", "BRK.B", "BRK-B", "BF-B", "ABUSD", "TUSD", "MUSD"],
)
def test_equities_including_usd_suffix_stay_stock(symbol: str) -> None:
    assert classify_symbol(symbol) == "stock"
    assert mds._is_crypto_symbol(symbol) is False


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "  ",
        "FOOBARUSD",
        "FOO/USD",
        "XYZ-USD",
        "AAPL!",
        # No US equity ticker is six letters: an unknown one must be refused, not
        # routed to the stock feed by a widened bound.
        "ZZZZZZ",
        # A base equal to its quote is not a pair Alpaca lists.
        "BTCBTC",
        "BTC/BTC",
    ],
)
def test_unclassifiable_fails_closed(symbol: str) -> None:
    with pytest.raises(UnclassifiableSymbolError):
        classify_symbol(symbol)
    with pytest.raises(UnclassifiableSymbolError):
        mds._is_crypto_symbol(symbol)


def test_equity_ticker_length_boundary() -> None:
    """The stock branch is bounded at five letters, so a longer ticker is refused."""
    assert classify_symbol("ABCDE") == "stock"
    with pytest.raises(UnclassifiableSymbolError):
        classify_symbol("ABCDEF")


def test_same_base_and_quote_is_not_a_crypto_pair() -> None:
    """``BTCBTC`` / ``BTC/BTC`` must not canonicalize to a pair Alpaca never lists."""
    with pytest.raises(UnclassifiableSymbolError):
        canonical_crypto_symbol("BTCBTC")
    with pytest.raises(UnclassifiableSymbolError):
        canonical_crypto_symbol("BTC/BTC")


def test_bars_stored_under_an_unclassified_symbol_are_logged(live_runner_logs) -> None:
    """Storing under the raw key must be visible, not silent (#122)."""
    service = mds.LiveMarketDataStreamService()
    asyncio.run(service.ingest_manual_bars([{"symbol": "FOOBARUSD", "close": 1.0}]))
    assert service._bars_for_symbol("FOOBARUSD")  # kept, not dropped

    records = [record for record in live_runner_logs.records if record.levelno >= logging.WARNING]
    assert len(records) == 1
    assert (
        records[0].getMessage() == "Storing market-data bars under an unclassified symbol key"
    )
    assert records[0].data["symbol"] == "FOOBARUSD"


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
    """Every form of the pair lands on one key, whichever side reads or writes it."""
    service = mds.LiveMarketDataStreamService()
    asyncio.run(service.ingest_manual_bars([{"symbol": "BTC/USD", "close": 1.0}]))
    # A manual/backfill bar can arrive in the unslashed spelling too.
    asyncio.run(service.ingest_manual_bars([{"symbol": "BTCUSD", "close": 2.0}]))
    for requested in ("BTC/USD", "BTCUSD", "btc-usd"):
        payload = asyncio.run(service.get_quote_payload(requested))
        assert payload["latest"] is not None
        assert [bar["close"] for bar in payload["bars"]] == [1.0, 2.0]
    # Nothing may be stored under a second, raw key: a quote read for the
    # canonical form would then silently miss bars.
    assert service._bars_for_symbol("BTCUSD") == []


def test_on_bar_keys_stream_bars_by_canonical_crypto_symbol() -> None:
    """The live callback can deliver ``BTCUSD``; it belongs in the ``BTC/USD`` queue."""
    service = mds.LiveMarketDataStreamService()
    asyncio.run(
        service._on_bar(
            SimpleNamespace(
                symbol="BTCUSD",
                timestamp=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc),
                open=1.0,
                high=1.0,
                low=1.0,
                close=7.0,
                volume=1.0,
            )
        )
    )
    bars = service._bars_for_symbol("BTC/USD")
    assert len(bars) == 1
    assert bars[0]["close"] == 7.0
    assert bars[0]["symbol"] == "BTCUSD"
    assert service._bars_for_symbol("BTCUSD") == []
    payload = asyncio.run(service.get_quote_payload("BTC/USD"))
    assert payload["latest"] is not None
    assert payload["latest"]["close"] == 7.0


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


def test_get_market_data_uses_stock_client_for_share_class_equity() -> None:
    """``BF-B`` is a us_equity, so it must reach the stock client unmodified."""
    calls: dict[str, object] = {}

    class _Stock:
        def get_stock_bars(self, request):
            calls["symbols"] = request.symbol_or_symbols
            idx = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")], name="timestamp")
            frame = pd.DataFrame(
                {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}, index=idx
            )
            return SimpleNamespace(df=frame)

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = _Stock()

    frame = asyncio.run(adapter.get_market_data("BF-B", timeframe="1h", limit=1))

    assert calls["symbols"] == ["BF-B"]
    assert len(frame.index) == 1


def test_get_market_data_rejects_unclassifiable_symbol() -> None:
    class _Stock:
        def get_stock_bars(self, request):  # pragma: no cover - must not be used
            raise AssertionError("unclassifiable symbol routed to the stock client")

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._historical_data_client = _Stock()
    with pytest.raises(UnclassifiableSymbolError):
        asyncio.run(adapter.get_market_data("FOOBARUSD", timeframe="1h", limit=1))

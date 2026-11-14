"""#146: live entries are sized with the broker's pip value in account currency.

``pip_value`` (the risk engine's name) is the account-currency P&L of ONE
adapter qty unit per 1.0 price move. Before #146 production passed none, so it
was an implicit 1.0. cTrader qty is 0.01 lot (EURUSD: 1,000 EUR), so the real
value there is 1000. On the demo a 0.036 USD budget became a 36.18 USD loss at
the stop, and the notional cap was 1000x too loose as well.

Every sizing test below goes through the production path
(``daemon.prepare_order_with_risk`` -> ``CTraderLiveAdapter.get_pip_value`` ->
``RiskEngine`` -> ``CTraderLiveAdapter._resolve_order_volume``). It then
recomputes the loss at the stop from the BROKER VOLUME, which is what the
broker charges:

    loss = volume / 100 (base units; volume is in cents) * |entry - SL|
           * (quote -> account rate)

and checks it equals the configured risk amount. The symbol properties are the
ones the cTrader demo returned on 2026-11-13 (lotSize/minVolume/stepVolume, in
cents).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import BaseLiveAdapter, LivePipValue, LiveQuote
from risk import RiskConfig, RiskEngine, TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal

pb = pytest.importorskip("ctrader_open_api.messages.OpenApiMessages_pb2")
model = pytest.importorskip("ctrader_open_api.messages.OpenApiModelMessages_pb2")

from live_runner import ctrader_adapter as ctrader  # noqa: E402

# Asset ids as on the demo (USD is 15 there); the others are arbitrary.
USD, EUR, JPY, XAU, GER40_ASSET, GBP = 15, 1, 2, 3, 4, 5
ASSET_NAMES = {USD: "USD", EUR: "EUR", JPY: "JPY", XAU: "XAU", GER40_ASSET: "GER40", GBP: "GBP"}

#: name -> (symbolId, base asset, quote asset, lotSize, minVolume) from the demo.
SYMBOLS = {
    "EURUSD": (1, EUR, USD, 10_000_000, 100_000),
    "USDJPY": (2, USD, JPY, 10_000_000, 100_000),
    "XAUUSD": (3, XAU, USD, 10_000, 100),
    "GER40": (4, GER40_ASSET, EUR, 100, 1),
    "EURGBP": (5, EUR, GBP, 10_000_000, 100_000),
}

BALANCE = 100_000.0
#: 0.1 % of 100,000 USD = 100 USD at risk on every entry below.
RISK_FRACTION = 0.001
RISK_USD = 100.0


def _quote(bid: float, ask: float | None = None, *, age: float = 0.0, **kwargs: Any) -> LiveQuote:
    kwargs.setdefault("timestamp", datetime.now(timezone.utc) - timedelta(seconds=age))
    return LiveQuote(bid=bid, ask=bid if ask is None else ask, source="test", **kwargs)


def _light(name: str) -> Any:
    symbol_id, base, quote, _lot, _min = SYMBOLS[name]
    light = model.ProtoOALightSymbol()
    light.symbolId = symbol_id
    light.symbolName = name
    light.baseAssetId = base
    light.quoteAssetId = quote
    return light


def _conversion_response(*names: str) -> Any:
    response = pb.ProtoOASymbolsForConversionRes()
    for name in names:
        response.symbol.append(_light(name))
    return response


class _CTrader:
    """A real CTraderLiveAdapter whose broker I/O is replaced by fixtures."""

    def __init__(
        self,
        quotes: dict[str, LiveQuote],
        *,
        conversions: dict[tuple[int, int], Any] | None = None,
        balance: float = BALANCE,
    ) -> None:
        adapter = ctrader.CTraderLiveAdapter(
            app_id="app", app_secret="secret", access_token="token", account_id="123"
        )
        adapter._connected = True
        adapter._authenticated = True
        adapter._deposit_asset_id = USD
        adapter._asset_names = dict(ASSET_NAMES)
        for name, (symbol_id, base, quote, lot_size, min_volume) in SYMBOLS.items():
            adapter._symbol_name_to_id[name] = symbol_id
            adapter._symbol_assets[name] = (base, quote)
            scale = ctrader._symbol_scale_from_broker_props(
                digits=2, lot_size=lot_size, min_volume=min_volume, fallback=None
            )
            assert scale is not None
            adapter._symbol_props[name] = scale
        self.adapter = adapter
        self.quotes = quotes
        self.quote_calls: list[str] = []
        self.conversion_requests: list[tuple[int, int]] = []
        self.conversions = conversions or {}

        async def get_quote(symbol: str) -> LiveQuote:
            self.quote_calls.append(symbol)
            return self.quotes[symbol]

        async def send(request: Any, *, timeout_seconds: float = 15) -> Any:
            assert isinstance(request, pb.ProtoOASymbolsForConversionReq)
            key = (int(request.firstAssetId), int(request.lastAssetId))
            self.conversion_requests.append(key)
            return self.conversions[key]

        async def get_balance() -> tuple[float, float]:
            return balance, balance

        async def get_positions() -> list[dict[str, Any]]:
            return []

        adapter.get_quote = get_quote
        adapter._send_request_with_refresh_retry = send
        adapter.get_balance = get_balance
        adapter.get_positions = get_positions


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    base = {
        "max_risk_per_trade": RISK_FRACTION,
        "max_total_exposure": 0.05,
        # 100 % of balance: every position below stays under it (asserted).
        "max_notional_per_trade": 1.0,
    }
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="pip-value",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={**base, **settings},
    )


def _signal(
    deployment: LiveDeploymentConfig,
    symbol: str,
    *,
    entry: float,
    stop: float,
    take: float,
    action: str = "buy",
    extra: dict[str, Any] | None = None,
) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol=symbol,
        action=action,
        theoretical_price=entry,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": stop, "take_profit": take, **(extra or {})},
    )


def _prepare(adapter: Any, deployment: LiveDeploymentConfig, signal: LiveSignal, **kwargs: Any):
    return asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal, **kwargs))


def _loss_at_stop(volume: int, entry: float, stop: float, rate: float) -> float:
    """Broker-side loss in account currency: volume is in cents of base units."""
    return volume / 100.0 * abs(entry - stop) * rate


# ── sizing to the risk budget, per instrument class ───────────────────────────


def test_eurusd_on_a_usd_account_is_sized_to_the_risk_amount():
    # pip value = lotSize 10,000,000 / 100 / 100 = 1,000 EUR per qty, quote USD
    # = account, rate 1 -> 1000 USD per 1.0 move per qty.
    # qty = 100 USD / (0.0020 * 1000) = 50 -> volume 50 * 100,000 = 5,000,000
    # (0.5 lot = 50,000 EUR). Loss at SL = 50,000 * 0.0020 = 100.00 USD.
    # Notional = 50 * 1.1 * 1000 = 55,000 USD <= cap 100,000.
    fixture = _CTrader({"EURUSD": _quote(1.10000)})
    deployment = _deployment()
    signal = _signal(deployment, "EURUSD", entry=1.10000, stop=1.09800, take=1.10400)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    assert qty == pytest.approx(50.0)
    volume = fixture.adapter._resolve_order_volume(prepared)
    assert volume == 5_000_000
    assert _loss_at_stop(volume, 1.10000, 1.09800, 1.0) == pytest.approx(RISK_USD)
    metadata = prepared.metadata
    assert metadata["pip_value"] == pytest.approx(1000.0)
    assert metadata["pip_value_units_per_qty"] == pytest.approx(1000.0)
    assert metadata["pip_value_conversion_rate"] == 1.0
    assert metadata["pip_value_quote_asset"] == "USD"
    assert metadata["pip_value_account_asset"] == "USD"
    assert metadata["pip_value_source"] == "ctrader_symbol_props"
    assert metadata["pip_value_conversion_symbols"] == []
    assert metadata["risk_amount"] == pytest.approx(RISK_USD)
    # No conversion needed: no conversion request, only the gate's quote.
    assert fixture.conversion_requests == []
    assert fixture.quote_calls == ["EURUSD"]


def test_usdjpy_converts_the_jpy_pnl_into_usd():
    # Quote JPY, account USD: the broker's chain is USDJPY (base USD, quote
    # JPY). Holding JPY = the quote side, so divide by the BID: 1 / 150.00.
    # pip value = 1,000 USD-units per qty / 150 = 6.6667 USD per 1.0 JPY move.
    # qty = 100 / (0.30 * 6.6667) = 50 -> volume 5,000,000 (50,000 USD).
    # Loss at SL = 50,000 * 0.30 JPY = 15,000 JPY = 15,000 / 150 = 100.00 USD.
    # Notional = 50 * 150 * 6.6667 = 50,000 USD <= cap 100,000.
    fixture = _CTrader(
        {"USDJPY": _quote(150.00, 150.02)},
        conversions={(JPY, USD): _conversion_response("USDJPY")},
    )
    deployment = _deployment()
    signal = _signal(deployment, "USDJPY", entry=150.02, stop=149.72, take=150.62)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    rate = 1.0 / 150.00
    assert prepared.metadata["pip_value_conversion_rate"] == pytest.approx(rate)
    assert prepared.metadata["pip_value"] == pytest.approx(1000.0 * rate)
    assert prepared.metadata["pip_value_quote_asset"] == "JPY"
    assert prepared.metadata["pip_value_conversion_symbols"] == ["USDJPY"]
    assert qty == pytest.approx(50.0)
    volume = fixture.adapter._resolve_order_volume(prepared)
    assert volume == 5_000_000
    assert _loss_at_stop(volume, 150.02, 149.72, rate) == pytest.approx(RISK_USD)
    assert fixture.conversion_requests == [(JPY, USD)]


def test_the_conversion_takes_the_side_that_sizes_smaller():
    # Dividing by the bid (150.00) gives a higher rate than the ask (150.02):
    # the spread can only shrink the loss at the stop below the budget.
    fixture = _CTrader(
        {"USDJPY": _quote(150.00, 150.02)},
        conversions={(JPY, USD): _conversion_response("USDJPY")},
    )

    value = asyncio.run(fixture.adapter.get_pip_value("USDJPY"))

    assert value.conversion_rate == pytest.approx(1.0 / 150.00)
    assert value.conversion_rate > 1.0 / 150.02


def test_xauusd_is_sized_in_ounces():
    # lotSize 10,000 (cents) = 100 oz; qty = 0.01 lot = 1 oz -> pip value 1 USD.
    # qty = 100 / (10.0 * 1) = 10 -> volume 10 * 100 = 1,000 (10 oz).
    # Loss at SL = 10 oz * 10 USD = 100.00 USD. Notional 10 * 2000 = 20,000.
    fixture = _CTrader({"XAUUSD": _quote(2000.0)})
    deployment = _deployment()
    signal = _signal(deployment, "XAUUSD", entry=2000.0, stop=1990.0, take=2020.0)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    assert prepared.metadata["pip_value"] == pytest.approx(1.0)
    assert qty == pytest.approx(10.0)
    volume = fixture.adapter._resolve_order_volume(prepared)
    assert volume == 1_000
    assert _loss_at_stop(volume, 2000.0, 1990.0, 1.0) == pytest.approx(RISK_USD)


def test_eur_quoted_index_cfd_is_converted_through_eurusd():
    # GER40: lotSize 100 (cents) = 1 index unit; qty = 0.01 unit. Quote EUR,
    # account USD via EURUSD: holding EUR = the base side, so multiply by the
    # ASK 1.25. pip value = 0.01 * 1.25 = 0.0125 USD per 1.0 index point.
    # qty = 100 / (50 * 0.0125) = 160 -> volume 160 (1.6 index units).
    # Loss at SL = 1.6 * 50 EUR = 80 EUR * 1.25 = 100.00 USD.
    # Notional = 160 * 18,000 * 0.0125 = 36,000 USD <= cap 100,000.
    fixture = _CTrader(
        {"GER40": _quote(18_000.0), "EURUSD": _quote(1.2498, 1.25)},
        conversions={(EUR, USD): _conversion_response("EURUSD")},
    )
    deployment = _deployment()
    signal = _signal(deployment, "GER40", entry=18_000.0, stop=17_950.0, take=18_100.0)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    assert prepared.metadata["pip_value_conversion_rate"] == pytest.approx(1.25)
    assert prepared.metadata["pip_value"] == pytest.approx(0.0125)
    assert qty == pytest.approx(160.0)
    volume = fixture.adapter._resolve_order_volume(prepared)
    assert volume == 160
    assert _loss_at_stop(volume, 18_000.0, 17_950.0, 1.25) == pytest.approx(RISK_USD)
    # The conversion quote is fetched before sizing, the gate quote after.
    assert fixture.quote_calls == ["EURUSD", "GER40"]


def test_short_entries_use_the_same_pip_value():
    # Sell EURUSD, SL 20 pips above: same 50 qty / 100 USD as the buy.
    fixture = _CTrader({"EURUSD": _quote(1.10000)})
    deployment = _deployment()
    signal = _signal(
        deployment, "EURUSD", entry=1.10000, stop=1.10200, take=1.09600, action="sell"
    )

    _prepared, qty = _prepare(fixture.adapter, deployment.model_copy(
        update={"settings": {**deployment.settings, "allow_short": True}}
    ), signal)

    assert qty == pytest.approx(50.0)


def test_demo_repro_reports_the_risk_the_broker_volume_actually_carries():
    """The #146 demo numbers: 99.94 USD balance, 20-pip EURUSD stop, defaults.

    Before: pip value 1.0, qty 18.09 (the 1000x-loose cap), volume 1,808,803,
    reported risk 0.036 USD, real loss at the stop 36.18 USD.

    Now: uncapped qty = 0.9994 / (0.0020 * 1000) = 0.4997, notional
    0.4997 * 1.10504 * 1000 = 552 USD > cap 0.20 * 99.94 = 19.988 USD, so
    qty = 19.988 / 1105.04 = 0.018088, volume 1,809 (18 EUR). The reported
    risk 0.018088 * 0.0020 * 1000 = 0.0362 USD is what that volume loses at
    the stop, and the broker minimum (100,000) refuses the order.
    """
    fixture = _CTrader({"EURUSD": _quote(1.10504)}, balance=99.94)
    deployment = LiveDeploymentConfig(
        id=str(uuid4()), strategy_name="repro", account_id="acc-1", symbols=["EURUSD"],
        timeframe="1h", status="ACTIVE", signal_source="internal", signal_config={},
        settings={},
    )
    signal = _signal(deployment, "EURUSD", entry=1.10504, stop=1.10304, take=1.10904)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    assert qty == pytest.approx(0.20 * 99.94 / (1.10504 * 1000))
    volume = round(qty * 100_000)
    assert volume == 1_809
    assert prepared.metadata["risk_amount"] == pytest.approx(
        _loss_at_stop(volume, 1.10504, 1.10304, 1.0), rel=1e-3
    )
    assert prepared.metadata["risk_amount"] < 0.01 * 99.94
    with pytest.raises(ValueError, match="below"):
        fixture.adapter._resolve_order_volume(prepared)


# ── notional cap ──────────────────────────────────────────────────────────────


def test_notional_cap_counts_the_contract_value_of_a_qty_unit():
    # EURUSD, pip 1000, balance 10,000, cap 20 % = 2,000 USD notional.
    # Uncapped qty = 100 / (0.0020 * 1000) = 50 = 55,000 USD notional.
    # Capped qty = 2,000 / (1.10 * 1000) = 1.81818 (2,000 USD = 1,818 EUR).
    # Realized risk = 1.81818 * 0.0020 * 1000 = 3.636 USD.
    engine = RiskEngine(RiskConfig(max_risk_per_trade=0.01, max_notional_per_trade=0.20))
    sized = engine.calculate_position_size(
        signal=TradeSignal(
            action="buy", symbol="EURUSD", entry_price=1.10, stop_loss=1.098,
            take_profit=1.104, timeframe="1h", metadata={},
        ),
        account_balance=10_000.0,
        pip_value=1000.0,
        current_exposure=0.0,
    )

    assert sized.decision.metadata["capped_by_notional"] is True
    assert sized.position_size == pytest.approx(2_000.0 / 1_100.0)
    assert sized.position_size * 1.10 * 1000.0 == pytest.approx(2_000.0)
    assert sized.decision.metadata["realized_risk_amount"] == pytest.approx(
        2_000.0 / 1_100.0 * 0.002 * 1000.0
    )


def test_notional_cap_does_not_bind_below_the_limit_with_a_pip_value():
    # pip 1000, uncapped qty 50 = 55,000 USD notional; cap 100 % of 100,000.
    engine = RiskEngine(RiskConfig(max_risk_per_trade=0.001, max_notional_per_trade=1.0))
    sized = engine.calculate_position_size(
        signal=TradeSignal(
            action="buy", symbol="EURUSD", entry_price=1.10, stop_loss=1.098,
            take_profit=1.104, timeframe="1h", metadata={},
        ),
        account_balance=100_000.0,
        pip_value=1000.0,
        current_exposure=0.0,
    )

    assert sized.decision.metadata["capped_by_notional"] is False
    assert sized.position_size == pytest.approx(50.0)


def test_notional_cap_for_shares_is_unchanged():
    # pip 1.0: cap 20 % of 10,000 = 2,000 / 100 = 20 shares, as before #146.
    engine = RiskEngine(RiskConfig(max_risk_per_trade=0.01, max_notional_per_trade=0.20))
    sized = engine.calculate_position_size(
        signal=TradeSignal(
            action="buy", symbol="AAPL", entry_price=100.0, stop_loss=98.0,
            take_profit=104.0, timeframe="1h", metadata={},
        ),
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )

    assert sized.position_size == pytest.approx(20.0)


def test_live_notional_cap_binds_in_account_currency_through_the_daemon():
    # EURUSD, balance 100,000, risk 1 % = 1,000 USD -> uncapped qty 500
    # (550,000 USD notional). Cap 20 % = 20,000 USD -> qty 20,000 / 1,100 =
    # 18.1818 -> volume 1,818,182 = 18,181.82 EUR = 20,000 USD notional.
    fixture = _CTrader({"EURUSD": _quote(1.10000)})
    deployment = _deployment(max_risk_per_trade=0.01, max_notional_per_trade=0.20)
    signal = _signal(deployment, "EURUSD", entry=1.10000, stop=1.09800, take=1.10400)

    prepared, qty = _prepare(fixture.adapter, deployment, signal)

    assert qty == pytest.approx(20_000.0 / 1_100.0)
    volume = fixture.adapter._resolve_order_volume(prepared)
    assert volume / 100.0 * 1.10 == pytest.approx(20_000.0, abs=0.01)


# ── fail closed: cTrader adapter ──────────────────────────────────────────────


def test_ctrader_refuses_a_symbol_with_only_fallback_scales():
    fixture = _CTrader({})
    fixture.adapter._symbol_props.pop("EURUSD")

    with pytest.raises(ValueError, match="No broker lotSize"):
        asyncio.run(fixture.adapter.get_pip_value("EURUSD"))


def test_ctrader_refuses_a_broker_scale_whose_lot_size_came_from_the_fallback():
    # Broker sent digits/minVolume but no lotSize: the scale borrows the
    # fallback table's 100,000 (base units, 100x off the broker's cents).
    fixture = _CTrader({})
    scale = ctrader._symbol_scale_from_broker_props(
        digits=5, lot_size=None, min_volume=100_000,
        fallback=ctrader._fallback_symbol_scale("EURUSD"),
    )
    assert scale is not None and scale.source == "broker"
    assert scale.lot_size_from_broker is False
    fixture.adapter._symbol_props["EURUSD"] = scale

    with pytest.raises(ValueError, match="No broker lotSize"):
        asyncio.run(fixture.adapter.get_pip_value("EURUSD"))


def test_broker_props_cache_marks_a_broker_sent_lot_size():
    response = pb.ProtoOASymbolByIdRes()
    with_lot = model.ProtoOASymbol(symbolId=1, digits=5, pipPosition=4, lotSize=10_000_000)
    with_lot.minVolume = 100_000
    without_lot = model.ProtoOASymbol(symbolId=2, digits=3, pipPosition=2)
    without_lot.minVolume = 100_000
    response.symbol.extend([with_lot, without_lot])
    adapter = ctrader.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )

    async def send(_request: Any, *, timeout_seconds: float = 15) -> Any:
        return response

    adapter._send_request_with_refresh_retry = send
    asyncio.run(adapter._load_symbol_props_into_cache({"EURUSD": 1, "USDJPY": 2}))

    assert adapter._symbol_props["EURUSD"].lot_size_from_broker is True
    assert adapter._symbol_props["EURUSD"].volume_per_qty == 100_000
    assert adapter._symbol_props["USDJPY"].lot_size_from_broker is False


def test_symbols_list_caches_base_and_quote_assets():
    response = pb.ProtoOASymbolsListRes()
    response.symbol.append(_light("USDJPY"))
    incomplete = model.ProtoOALightSymbol(symbolId=9, symbolName="NOASSETS")
    response.symbol.append(incomplete)
    adapter = ctrader.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )
    adapter._ProtoOASymbolsListReq = pb.ProtoOASymbolsListReq
    adapter._ProtoOASymbolsListRes = pb.ProtoOASymbolsListRes

    async def send(_request: Any, *, timeout_seconds: float = 15) -> Any:
        return response

    async def no_props(_mapping: dict[str, int]) -> None:
        return None

    adapter._send_request_with_refresh_retry = send
    adapter._load_symbol_props_into_cache = no_props
    asyncio.run(adapter._load_symbols_into_cache())

    assert adapter._symbol_assets == {"USDJPY": (USD, JPY)}


def test_ctrader_refuses_a_symbol_without_asset_ids():
    fixture = _CTrader({})
    fixture.adapter._symbol_assets.pop("EURUSD")

    with pytest.raises(ValueError, match="no base/quote asset ids"):
        asyncio.run(fixture.adapter.get_pip_value("EURUSD"))


def test_ctrader_refuses_an_unknown_account_currency():
    fixture = _CTrader({})
    fixture.adapter._deposit_asset_id = None

    async def fetch_trader() -> Any:
        return SimpleNamespace(depositAssetId=0)

    fixture.adapter._fetch_trader = fetch_trader

    with pytest.raises(ValueError, match="deposit asset"):
        asyncio.run(fixture.adapter.get_pip_value("EURUSD"))


def test_ctrader_reads_and_caches_the_deposit_asset():
    fixture = _CTrader({"EURUSD": _quote(1.1)})
    fixture.adapter._deposit_asset_id = None
    calls: list[int] = []

    async def fetch_trader() -> Any:
        calls.append(1)
        return SimpleNamespace(depositAssetId=USD)

    fixture.adapter._fetch_trader = fetch_trader
    asyncio.run(fixture.adapter.get_pip_value("EURUSD"))
    asyncio.run(fixture.adapter.get_pip_value("EURUSD"))

    assert calls == [1]


@pytest.mark.parametrize(
    ("chain", "match"),
    [
        ((), "no conversion chain"),
        (("EURGBP",), "does not connect"),
        (("USDJPY", "EURUSD", "EURGBP", "USDJPY"), "hops"),
    ],
)
def test_ctrader_refuses_a_bad_conversion_chain(chain, match):
    fixture = _CTrader(
        {name: _quote(1.0) for name in SYMBOLS},
        conversions={(JPY, USD): _conversion_response(*chain)},
    )

    with pytest.raises(ValueError, match=match):
        asyncio.run(fixture.adapter.get_pip_value("USDJPY"))


def test_ctrader_refuses_a_chain_that_does_not_end_in_the_account_currency():
    # JPY -> USD via USDJPY lands in USD; continuing through EURUSD ends in EUR.
    fixture = _CTrader(
        {"USDJPY": _quote(150.0), "EURUSD": _quote(1.1)},
        conversions={(JPY, USD): _conversion_response("USDJPY", "EURUSD")},
    )

    with pytest.raises(ValueError, match="does not end in the account currency"):
        asyncio.run(fixture.adapter.get_pip_value("USDJPY"))


@pytest.mark.parametrize(
    ("bid", "ask", "match"),
    [
        (0.0, 150.0, "positive"),
        (150.0, float("nan"), "positive"),
        (150.1, 150.0, "crossed"),
        (None, 150.0, "missing"),
    ],
)
def test_ctrader_refuses_an_unusable_conversion_quote(bid, ask, match):
    fixture = _CTrader(
        {"USDJPY": LiveQuote(bid=bid, ask=ask, timestamp=datetime.now(timezone.utc))},
        conversions={(JPY, USD): _conversion_response("USDJPY")},
    )

    with pytest.raises(ValueError, match=match):
        asyncio.run(fixture.adapter.get_pip_value("USDJPY"))


def test_ctrader_refuses_an_unexpected_conversion_response():
    fixture = _CTrader({}, conversions={(JPY, USD): pb.ProtoOATraderRes()})

    with pytest.raises(RuntimeError, match="Unexpected cTrader conversion response"):
        asyncio.run(fixture.adapter.get_pip_value("USDJPY"))


def test_ctrader_submit_refuses_a_qty_sized_for_a_different_scale():
    # Sized at 1,000 units per qty (broker props). A reconnect whose props
    # fetch failed would fall back to 1,000 volume per qty = 10 units: the
    # same qty would be a 100x smaller (or, the other way round, larger) trade.
    fixture = _CTrader({"EURUSD": _quote(1.10000)})
    deployment = _deployment()
    signal = _signal(deployment, "EURUSD", entry=1.10000, stop=1.09800, take=1.10400)
    prepared, _qty = _prepare(fixture.adapter, deployment, signal)
    fixture.adapter._symbol_props.pop("EURUSD")

    with pytest.raises(ValueError, match="changed since sizing"):
        fixture.adapter._resolve_order_volume(prepared)


def test_ctrader_submit_refuses_a_malformed_sized_scale():
    fixture = _CTrader({})
    signal = LiveSignal(
        deployment_id="d", symbol="EURUSD", action="buy", theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, "pip_value_units_per_qty": "abc"},
    )

    with pytest.raises(ValueError, match="Invalid pip_value_units_per_qty"):
        fixture.adapter._resolve_order_volume(signal)


# ── fail closed: daemon ───────────────────────────────────────────────────────


class _Adapter(BaseLiveAdapter):
    """Generic adapter whose pip value a test sets; quotes are at 100."""

    def __init__(self, pip: Any = None, error: BaseException | None = None) -> None:
        self.pip = pip
        self.error = error
        self.submitted: list[Any] = []

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return BALANCE, BALANCE

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_quote(self, symbol: str) -> LiveQuote:
        return _quote(100.0)

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        if self.error is not None:
            raise self.error
        if self.pip is None:
            return await super().get_pip_value(symbol)
        return self.pip


def _pip(**overrides: Any) -> LivePipValue:
    values: dict[str, Any] = {
        "value": 1.0, "units_per_qty": 1.0, "conversion_rate": 1.0,
        "quote_asset": "USD", "account_asset": "USD", "source": "test",
    }
    values.update(overrides)
    return LivePipValue(**values)


def _equity_signal(deployment: LiveDeploymentConfig, **extra: Any) -> LiveSignal:
    return _signal(deployment, "AAPL", entry=100.0, stop=95.0, take=110.0, extra=extra)


def test_daemon_refuses_an_entry_when_the_adapter_has_no_pip_value(caplog):
    adapter = _Adapter()
    deployment = _deployment()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="no trusted pip value"):
            asyncio.run(
                daemon.submit_order_with_risk(adapter, deployment, _equity_signal(deployment))
            )

    assert adapter.submitted == []
    assert "Pip value unavailable" in caplog.text


def test_daemon_refuses_an_adapter_without_the_method():
    class _NoMethod:
        async def get_balance(self) -> tuple[float, float]:
            return BALANCE, BALANCE

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

    deployment = _deployment()

    with pytest.raises(ValueError, match="has no get_pip_value"):
        _prepare(_NoMethod(), deployment, _equity_signal(deployment))


def test_daemon_refuses_when_the_pip_value_lookup_fails():
    adapter = _Adapter(error=RuntimeError("broker down"))
    deployment = _deployment()

    with pytest.raises(ValueError, match="broker down"):
        _prepare(adapter, deployment, _equity_signal(deployment))


def test_daemon_bounds_the_pip_value_lookup(monkeypatch):
    class _Hanging(_Adapter):
        async def get_pip_value(self, symbol: str) -> LivePipValue:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    monkeypatch.setattr(daemon, "PIP_VALUE_FETCH_TIMEOUT_SECONDS", 0.05)
    deployment = _deployment()

    with pytest.raises(ValueError, match="TimeoutError"):
        _prepare(_Hanging(), deployment, _equity_signal(deployment))


@pytest.mark.parametrize(
    ("pip", "match"),
    [
        ("1.0", "not LivePipValue"),
        (_pip(value=0.0, units_per_qty=0.0), "must all be > 0"),
        (_pip(value=-1.0, units_per_qty=-1.0), "must all be > 0"),
        (_pip(value=float("nan")), "finite"),
        (_pip(value=float("inf")), "finite"),
        (_pip(value=True), "pip value"),
        (_pip(value=1000.0), r"!= units per qty"),
        (_pip(value=0.5, conversion_rate=0.5), "without the quotes"),
    ],
)
def test_daemon_refuses_an_unusable_pip_value(pip, match):
    adapter = _Adapter(pip=pip)
    deployment = _deployment()

    with pytest.raises(ValueError, match=match):
        _prepare(adapter, deployment, _equity_signal(deployment))

    assert adapter.submitted == []


@pytest.mark.parametrize(
    ("make_quote", "rule"),
    [
        # Built inside the test: a quote timed at collection would age while
        # the rest of the suite runs.
        (lambda: _quote(1.25, age=11.0), "quote_stale"),
        (lambda: _quote(1.25, timestamp=None), "quote_time_missing"),
        (lambda: _quote(1.25, age=-5.0), "quote_time_in_future"),
        (lambda: _quote(1.25, timestamp=datetime(2026, 1, 1)), "quote_time_invalid"),
    ],
)
def test_daemon_refuses_a_stale_or_untimed_conversion_rate(make_quote, rule):
    quote = make_quote()
    pip = _pip(
        value=1.25, conversion_rate=1.25, quote_asset="EUR",
        conversion_symbols=("EURUSD",), conversion_quotes=(quote,),
    )
    adapter = _Adapter(pip=pip)
    deployment = _deployment()

    with pytest.raises(ValueError, match=rf"conversion quote EURUSD rejected \({rule}\)"):
        _prepare(adapter, deployment, _equity_signal(deployment))


def test_conversion_rate_age_limit_follows_the_deployment_setting():
    pip = _pip(
        value=1.25, conversion_rate=1.25, quote_asset="EUR",
        conversion_symbols=("EURUSD",), conversion_quotes=(_quote(1.25, age=11.0),),
    )
    deployment = _deployment(max_entry_quote_age_seconds=30)

    prepared, _qty = _prepare(_Adapter(pip=pip), deployment, _equity_signal(deployment))

    assert prepared.metadata["pip_value_conversion_quote_ages_seconds"][0] == pytest.approx(
        11.0, abs=1.0
    )


def test_ctrader_stale_conversion_quote_refuses_the_entry():
    fixture = _CTrader(
        {"USDJPY": _quote(150.0, age=60.0)},
        conversions={(JPY, USD): _conversion_response("USDJPY")},
    )
    deployment = _deployment()
    signal = _signal(deployment, "USDJPY", entry=150.0, stop=149.7, take=150.6)

    with pytest.raises(ValueError, match="quote_stale"):
        _prepare(fixture.adapter, deployment, signal)


def test_payload_pip_value_fields_never_survive():
    # The record is broker truth; a payload copy under the same names is
    # replaced, including the key the cTrader adapter checks at submission.
    adapter = _Adapter(pip=_pip())
    deployment = _deployment()
    signal = _equity_signal(
        deployment,
        pip_value=1000.0,
        pip_value_source="payload",
        pip_value_units_per_qty=5.0,
        pip_value_conversion_rate=9.0,
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    # 100 USD / (5 * 1.0) = 20 shares.
    assert qty == pytest.approx(20.0)
    assert prepared.metadata["pip_value"] == 1.0
    assert prepared.metadata["pip_value_source"] == "test"
    assert prepared.metadata["pip_value_units_per_qty"] == 1.0
    assert prepared.metadata["pip_value_conversion_rate"] == 1.0


def test_a_trusted_caller_pip_value_skips_the_lookup():
    adapter = _Adapter(error=AssertionError("must not be called"))
    deployment = _deployment()
    signal = _equity_signal(deployment, pip_value_units_per_qty=5.0)

    prepared, qty = _prepare(adapter, deployment, signal, pip_value=2.0)

    # 100 / (5 * 2) = 10.
    assert qty == pytest.approx(10.0)
    assert prepared.metadata["pip_value"] == 2.0
    assert prepared.metadata["pip_value_source"] == "caller"
    assert prepared.metadata["pip_value_units_per_qty"] is None


def test_exit_only_orders_need_no_pip_value():
    class _Book(_Adapter):
        async def get_positions(self) -> list[dict[str, Any]]:
            return [{"symbol": "AAPL", "qty": 3.0}]

    adapter = _Book(error=AssertionError("must not be called"))
    deployment = _deployment()
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="sell",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True},
    )

    _prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(3.0)


def test_exit_only_close_ignores_payload_pip_value_keys():
    # A payload copy of the pip-value record must not reach the adapter on a close:
    # cTrader's submit-time scale guard would otherwise refuse the close (#146).
    class _Book(_Adapter):
        async def get_positions(self) -> list[dict[str, Any]]:
            return [{"symbol": "AAPL", "qty": 3.0}]

    adapter = _Book(error=AssertionError("must not be called"))
    deployment = _deployment()
    signal = LiveSignal(
        deployment_id=str(deployment.id), symbol="AAPL", action="sell",
        theoretical_price=100.0, timestamp=datetime.now(timezone.utc),
        metadata={
            "exit_only": True,
            "pip_value": 9.0,
            "pip_value_units_per_qty": "abc",
            "pip_value_conversion_rate": 5.0,
            "bid": 1.0,
        },
    )

    prepared, qty = _prepare(adapter, deployment, signal)

    assert qty == pytest.approx(3.0)
    for key in (*daemon._PIP_VALUE_METADATA_KEYS, *daemon._ENTRY_QUOTE_METADATA_KEYS):
        assert key not in prepared.metadata


@pytest.mark.parametrize(
    "quote,match",
    [
        (None, "no usable quote"),
        (LiveQuote(bid=None, ask=1.0), "missing bid"),
        (LiveQuote(bid=1.0, ask="x"), "not numeric"),
        (LiveQuote(bid=float("nan"), ask=1.0), "positive and finite"),
        (LiveQuote(bid=0.0, ask=1.0), "positive and finite"),
        (LiveQuote(bid=2.0, ask=1.0), "crossed"),
    ],
)
def test_entry_gate_and_conversion_chain_refuse_the_same_quotes(quote, match):
    # One validator behind both layers: a quote one refuses, the other refuses too.
    with pytest.raises(ValueError, match=match):
        daemon._validated_quote_prices(quote, symbol="X")
    with pytest.raises(ValueError, match=match):
        ctrader._conversion_quote_prices(quote, symbol="X")


# ── Alpaca ────────────────────────────────────────────────────────────────────


def _alpaca(currency: str = "USD") -> Any:
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = AlpacaLiveAdapter.__new__(AlpacaLiveAdapter)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(currency=currency)
    )
    return adapter


@pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "BTC/USD", "BTCUSD"])
def test_alpaca_usd_symbols_in_a_usd_account_are_one_per_unit(symbol):
    value = asyncio.run(_alpaca().get_pip_value(symbol))

    assert value.value == 1.0
    assert value.units_per_qty == 1.0
    assert value.conversion_rate == 1.0
    assert value.source == "alpaca_usd_unit"


@pytest.mark.parametrize("symbol", ["ETH/BTC", "BTC/USDT", "ETHBTC", "SOLUSDC"])
def test_alpaca_refuses_pairs_not_quoted_in_usd(symbol):
    with pytest.raises(ValueError, match="not USD"):
        asyncio.run(_alpaca().get_pip_value(symbol))


def test_alpaca_refuses_a_non_usd_account():
    with pytest.raises(ValueError, match="account currency"):
        asyncio.run(_alpaca(currency="EUR").get_pip_value("AAPL"))


def test_alpaca_refuses_an_empty_symbol():
    with pytest.raises(ValueError, match="symbol is required"):
        asyncio.run(_alpaca().get_pip_value(" "))


def test_base_adapter_default_fails_closed():
    with pytest.raises(NotImplementedError, match="cannot provide a pip value"):
        asyncio.run(_Adapter().get_pip_value("EURUSD"))


def test_ctrader_refuses_a_caller_pip_value_whose_scale_cannot_be_verified():
    # cubic #187: a caller override carries no units-per-qty, so the submit-time
    # scale guard cannot run; converting qty with whatever scale is current
    # could be a different position size. Fail closed.
    fixture = _CTrader({})
    signal = LiveSignal(
        deployment_id="d", symbol="EURUSD", action="buy", theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 1.0, "pip_value": 2.0, "pip_value_source": "caller"},
    )

    with pytest.raises(ValueError, match="cannot verify the volume scale"):
        fixture.adapter._resolve_order_volume(signal)


def test_ctrader_pip_value_does_not_wait_for_the_asset_name_lookup():
    # cubic #187: asset names are labels only; a slow asset list must not hold
    # entry sizing.
    fixture = _CTrader({"EURUSD": _quote(1.1)})
    fixture.adapter._asset_names = {}

    async def run() -> Any:
        async def hang(request: Any, *, timeout_seconds: float = 15) -> Any:
            await asyncio.sleep(3600)

        fixture.adapter._send_request_with_refresh_retry = hang  # type: ignore[method-assign]
        result = await asyncio.wait_for(fixture.adapter.get_pip_value("EURUSD"), timeout=2)
        task = fixture.adapter._asset_names_task
        assert task is not None and not task.done()
        task.cancel()
        return result

    result = asyncio.run(run())
    assert result.quote_asset == str(USD)

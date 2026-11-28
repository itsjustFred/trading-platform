"""Bid/ask fill and trigger semantics on two-sided (Dukascopy-style) data (#472).

The canonical ``open/high/low/close`` series is the BID book. A frame that also
carries ``ask_*`` columns is a two-sided feed, and the native engine must then
fill and trigger on the side of the book a real order would hit:

| event | trigger | fill |
|---|---|---|
| market buy (LONG entry, SHORT exit) | - | ask (next bar's ask open) |
| market sell (SHORT entry, LONG exit) | - | bid |
| LONG SL / TP | bid low / high | bid level; gap -> bid open |
| SHORT SL / TP | ask high / low | ask level; gap -> ask open |
| buy stop / buy limit entry | ask | ask |
| sell stop / sell limit entry | bid | bid |
| signals / indicators | bid bars | - |

Frames without ``ask_*`` columns (yfinance, Alpaca, ...) are unchanged: ask is
the bid, so triggers stay on the bid and the only cost is the configured
slippage model.

Every test here asserts exact prices on a hand-built two-sided frame with a
constant 0.5 spread; nothing depends on a generated golden.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from runner.engine import Order, OrderType, TradeEngine
from runner.engines.base import BacktestConfig
from tests.helpers import with_zero_fees

SPREAD = 0.5

FLAT_BID = (100.0, 100.2, 99.8, 100.0)


def _config(parameters: dict[str, Any] | None = None, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "bid_ask",
        "symbols": ["EURUSD"],
        "timeframe": "1m",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "parameters": dict(parameters or {}),
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


def _engine(parameters: dict[str, Any] | None = None, **overrides: Any) -> TradeEngine:
    return TradeEngine(_config(parameters, **overrides), symbol="EURUSD")


def _frame(bars: list[tuple[float, float, float, float]], *, two_sided: bool = True) -> pd.DataFrame:
    """A bid-bar frame, plus ``ask_*`` = bid + 0.5 when ``two_sided``."""
    timestamps = pd.date_range("2024-01-01", periods=len(bars), freq="min", tz="UTC")
    data: dict[str, Any] = {
        "timestamp": timestamps,
        "open": [bar[0] for bar in bars],
        "high": [bar[1] for bar in bars],
        "low": [bar[2] for bar in bars],
        "close": [bar[3] for bar in bars],
        "volume": [1000.0] * len(bars),
    }
    if two_sided:
        for index, name in enumerate(("open", "high", "low", "close")):
            data[f"ask_{name}"] = [bar[index] + SPREAD for bar in bars]
    return pd.DataFrame(data)


def _strategy(
    n: int,
    *,
    direction: str = "LONG",
    entry_bar: int = 0,
    exit_bar: int | None = None,
    stop: float | None = None,
    tp: float | None = None,
):
    def generate(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series([False] * n, index=df.index)
        exits = pd.Series([False] * n, index=df.index)
        entries.iloc[entry_bar] = True
        if exit_bar is not None:
            exits.iloc[exit_bar] = True
        directions = pd.Series(
            [direction if i == entry_bar else None for i in range(n)], dtype=object
        )
        indicators: dict[str, Any] = {"direction": directions}
        if stop is not None:
            indicators["stop_loss"] = pd.Series(
                [stop if i == entry_bar else float("nan") for i in range(n)]
            )
        if tp is not None:
            indicators["take_profit"] = pd.Series(
                [tp if i == entry_bar else float("nan") for i in range(n)]
            )
        return entries, exits, indicators, []

    return generate


def _run(frame: pd.DataFrame, strategy, parameters=None) -> Any:
    return _engine(parameters).run(frame, strategy)


def _single_trade(result):
    (trade,) = result.trades
    return trade


# ─── market orders ──────────────────────────────────────────────────────────


def test_market_buy_fills_at_the_next_bar_ask_open():
    # LONG entry signalled on bar 0 fills at bar 1's open: the ask open, not
    # the bid open.
    frame = _frame([FLAT_BID] * 4)
    trade = _single_trade(_run(frame, _strategy(4, direction="LONG")))

    assert trade.entry_price == pytest.approx(FLAT_BID[0] + SPREAD)


def test_market_sell_fills_at_the_next_bar_bid_open():
    frame = _frame([FLAT_BID] * 4)
    trade = _single_trade(_run(frame, _strategy(4, direction="SHORT")))

    assert trade.entry_price == pytest.approx(FLAT_BID[0])


# ─── LONG protective levels: bid trigger, bid fill ──────────────────────────


def test_long_stop_triggers_on_bid_low_and_fills_at_the_level():
    # The ask low (95.5) never reaches the 95 stop; the bid low (95.0) does.
    bars = [FLAT_BID, (100.0, 100.2, 95.0, 96.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, stop=95.0)))

    stop_leg = trade.exits[0]
    assert stop_leg.reason == "stop_loss"
    assert stop_leg.price == pytest.approx(95.0)


def test_long_stop_gap_fills_at_the_bid_open():
    bars = [FLAT_BID, FLAT_BID, (94.5, 95.0, 94.0, 94.8), FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, stop=95.0)))

    stop_leg = trade.exits[0]
    assert stop_leg.reason == "stop_loss"
    assert stop_leg.price == pytest.approx(94.5)


def test_long_take_profit_triggers_on_bid_high_and_fills_at_the_level():
    bars = [FLAT_BID, (100.0, 105.0, 100.0, 104.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, tp=105.0)))

    tp_leg = trade.exits[0]
    assert tp_leg.reason == "tp"
    assert tp_leg.price == pytest.approx(105.0)


# ─── SHORT protective levels: ask trigger, ask fill ─────────────────────────


def test_short_stop_triggers_on_ask_high_and_fills_at_the_level():
    # Bid high 100.7 is below the 101 stop; ask high 101.2 reaches it.
    bars = [FLAT_BID, (100.0, 100.7, 99.8, 100.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, direction="SHORT", stop=101.0)))

    stop_leg = trade.exits[0]
    assert stop_leg.reason == "stop_loss"
    # The level is ask-denominated: the fill is the level, never level + spread.
    assert stop_leg.price == pytest.approx(101.0)


def test_short_stop_gap_fills_at_the_ask_open():
    # Bid open 104.7 is short of the 105 stop; ask open 105.2 gaps through it.
    bars = [FLAT_BID, FLAT_BID, (104.7, 105.3, 104.5, 105.0), FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, direction="SHORT", stop=105.0)))

    stop_leg = trade.exits[0]
    assert stop_leg.reason == "stop_loss"
    assert stop_leg.price == pytest.approx(105.2)


def test_short_take_profit_triggers_on_ask_low_and_fills_at_the_level():
    # The 99 target is reached when the ask falls to it: bid low 98.4, ask low
    # 98.9.
    bars = [FLAT_BID, (100.0, 100.2, 98.4, 99.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, direction="SHORT", tp=99.0)))

    tp_leg = trade.exits[0]
    assert tp_leg.reason == "tp"
    assert tp_leg.price == pytest.approx(99.0)


def test_short_take_profit_does_not_fill_on_a_bid_only_touch():
    # Bid low 98.9 touches the 99 target but the ask low (99.4) never does:
    # a short buys back at the ask, so the target was not reached.
    bars = [FLAT_BID, (100.0, 100.2, 98.9, 99.5), FLAT_BID, FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, direction="SHORT", tp=99.0)))

    assert trade.exits[0].reason == "end_of_data"


def test_short_take_profit_gap_fills_at_the_ask_open():
    # Ask open 98.6 gaps through the 99 target; bid open 98.1 is further away.
    bars = [FLAT_BID, FLAT_BID, (98.1, 98.6, 98.0, 98.4), FLAT_BID]
    trade = _single_trade(_run(_frame(bars), _strategy(4, direction="SHORT", tp=99.0)))

    tp_leg = trade.exits[0]
    assert tp_leg.reason == "tp"
    assert tp_leg.price == pytest.approx(98.6)


# ─── pending entry orders ───────────────────────────────────────────────────


def _pending_order_engine() -> TradeEngine:
    return _engine()


def _order(order_type: OrderType, direction: str, **kwargs: Any) -> Order:
    return Order(
        symbol="EURUSD",
        direction=direction,
        order_type=order_type,
        created_index=0,
        created_time="2024-01-01T00:00:00+00:00",
        quantity=1.0,
        **kwargs,
    )


def test_buy_stop_entry_triggers_and_fills_on_the_ask():
    # Bid high 100.8 never reaches the 101 buy stop; ask high 101.3 does.
    engine = _pending_order_engine()
    engine.pending_orders.append(
        _order(OrderType.STOP, "LONG", stop_price=101.0, stop_loss=100.0)
    )
    frame = _frame([FLAT_BID, (100.0, 100.8, 99.8, 100.0), FLAT_BID])

    engine._fill_pending_orders(frame.iloc[1], 1)

    (position,) = engine.positions
    assert position.entry_price == pytest.approx(101.0)


def test_buy_limit_entry_triggers_and_fills_on_the_ask():
    # The ask low (99.9) reaches the 100 buy limit.
    engine = _pending_order_engine()
    engine.pending_orders.append(
        _order(OrderType.LIMIT, "LONG", limit_price=100.0, stop_loss=99.0)
    )
    frame = _frame([FLAT_BID, (100.2, 100.4, 99.4, 99.6), FLAT_BID])

    engine._fill_pending_orders(frame.iloc[1], 1)

    (position,) = engine.positions
    assert position.entry_price == pytest.approx(100.0)


def test_buy_limit_entry_does_not_fill_on_a_bid_only_touch():
    # Bid low 99.8 touches the 100 buy limit but the ask low (100.3) does not:
    # a buy fills at the ask, so the order stays resting.
    engine = _pending_order_engine()
    order = _order(OrderType.LIMIT, "LONG", limit_price=100.0, stop_loss=99.0)
    engine.pending_orders.append(order)
    frame = _frame([FLAT_BID, (100.4, 100.5, 99.8, 100.3), FLAT_BID])

    engine._fill_pending_orders(frame.iloc[1], 1)

    assert engine.positions == []
    assert engine.pending_orders == [order]


def test_sell_stop_entry_triggers_and_fills_on_the_bid():
    # Ask low 99.2 stays above the 99 sell stop; bid low 98.7 reaches it.
    engine = _pending_order_engine()
    engine.pending_orders.append(
        _order(OrderType.STOP, "SHORT", stop_price=99.0, stop_loss=100.0)
    )
    frame = _frame([FLAT_BID, (99.0, 99.4, 98.7, 98.9), FLAT_BID])

    engine._fill_pending_orders(frame.iloc[1], 1)

    (position,) = engine.positions
    assert position.entry_price == pytest.approx(99.0)


def test_sell_limit_entry_triggers_and_fills_on_the_bid():
    # Ask high 100.9 stays below the 101 sell limit; bid high 101.4 reaches it.
    engine = _pending_order_engine()
    engine.pending_orders.append(
        _order(OrderType.LIMIT, "SHORT", limit_price=101.0, stop_loss=102.0)
    )
    frame = _frame([FLAT_BID, (100.5, 101.4, 100.5, 101.0), FLAT_BID])

    engine._fill_pending_orders(frame.iloc[1], 1)

    (position,) = engine.positions
    assert position.entry_price == pytest.approx(101.0)


# ─── signals, market exits ──────────────────────────────────────────────────


def test_signals_still_read_the_bid_bars():
    frame = _frame([FLAT_BID] * 4)
    seen: list[float] = []

    def generate(df: pd.DataFrame, params: dict[str, Any]):
        seen.extend(float(value) for value in df["close"])
        return _strategy(4)(df, params)

    _run(frame, generate)

    assert seen == [FLAT_BID[3]] * 4


def test_short_signal_exit_fills_at_the_next_bar_ask_open():
    # Exit signalled on bar 1 fills at bar 2's open: the ask open.
    frame = _frame([FLAT_BID] * 4)
    trade = _single_trade(
        _run(frame, _strategy(4, direction="SHORT", exit_bar=1))
    )

    exit_leg = trade.exits[0]
    assert exit_leg.reason == "signal"
    assert exit_leg.price == pytest.approx(FLAT_BID[0] + SPREAD)


def test_short_end_of_data_exit_fills_at_the_ask_close():
    frame = _frame([FLAT_BID] * 4)
    trade = _single_trade(_run(frame, _strategy(4, direction="SHORT")))

    exit_leg = trade.exits[0]
    assert exit_leg.reason == "end_of_data"
    assert exit_leg.price == pytest.approx(FLAT_BID[3] + SPREAD)


# ─── the spread is paid exactly once per side ───────────────────────────────


def test_spread_is_paid_exactly_once_per_side():
    # With the bar-spread slippage model on two-sided data, the fill prices are
    # the ask/bid themselves: a LONG round trip pays the spread once, at entry.
    frame = _frame([FLAT_BID] * 4)
    trade = _single_trade(
        _run(
            frame,
            _strategy(4, direction="LONG"),
            parameters={"slippage": "spread", "slippage_bps": 1.0},
        )
    )

    assert trade.entry_price == pytest.approx(FLAT_BID[0] + SPREAD)
    assert trade.exits[0].price == pytest.approx(FLAT_BID[3])
    assert trade.entry_price - trade.exits[0].price == pytest.approx(SPREAD)


def test_short_stop_is_not_charged_the_spread_twice():
    # The stop triggers on the ask and fills at the ask level: the bar-spread
    # model must not add the quoted spread on top of that fill.
    bars = [FLAT_BID, (100.0, 100.7, 99.8, 100.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(
        _run(
            _frame(bars),
            _strategy(4, direction="SHORT", stop=101.0),
            parameters={"slippage": "spread", "slippage_bps": 1.0},
        )
    )

    assert trade.exits[0].price == pytest.approx(101.0)


# ─── no-ask fallback is explicit ────────────────────────────────────────────


def test_frames_without_ask_columns_keep_bid_fills_and_fixed_bps_slippage():
    # No ask columns: ask is the bid, so the market buy fills at the bid open
    # and the configured fixed-bps slippage is the only cost.
    frame = _frame([FLAT_BID] * 4, two_sided=False)
    trade = _single_trade(
        _run(
            frame,
            _strategy(4, direction="LONG"),
            parameters={"slippage": "spread", "slippage_bps": 2.0},
        )
    )

    assert trade.entry_price == pytest.approx(FLAT_BID[0] * (1 + 2.0 / 10_000.0))
    assert trade.exits[0].price == pytest.approx(
        FLAT_BID[3] * (1 - 2.0 / 10_000.0)
    )


def test_frames_without_ask_columns_trigger_short_stops_on_the_bid():
    # Without ask data the ask IS the bid, so a 101 stop needs the bid high.
    bars = [FLAT_BID, (100.0, 100.2, 99.8, 100.0), FLAT_BID, FLAT_BID]
    trade = _single_trade(
        _run(_frame(bars, two_sided=False), _strategy(4, direction="SHORT", stop=101.0))
    )

    assert trade.exits[0].reason == "end_of_data"


# ─── the old opt-in parameter is gone ───────────────────────────────────────


def test_ask_ohlc_parameter_no_longer_changes_anything():
    bars = [FLAT_BID, (100.0, 100.2, 99.8, 100.0), FLAT_BID, FLAT_BID]
    strategy = _strategy(4, direction="SHORT", stop=101.0)

    default = _single_trade(_run(_frame(bars), strategy))
    opted_in = _single_trade(_run(_frame(bars), strategy, {"ask_ohlc": True}))

    assert default.exits[0].price == opted_in.exits[0].price
    assert default.exits[0].reason == opted_in.exits[0].reason

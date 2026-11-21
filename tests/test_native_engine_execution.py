import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig


def _engine(params=None):
    # #84: explicit no-cost declaration for engine fixtures.
    merged_params = {"fees": "zero"}
    merged_params.update(params or {})
    return TradeEngine(
        BacktestConfig(
            strategy_name="test",
            symbols=["EURUSD"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            parameters=merged_params,
        ),
        symbol="EURUSD",
    )


def test_native_engine_reads_native_stop_and_take_profit_aliases():
    engine = _engine()
    row = pd.Series({"sl_price": 1.0950, "tp_price": 1.1100})

    stop_loss, take_profit_levels = engine._extract_levels(row)

    assert stop_loss == 1.0950
    assert take_profit_levels == [{"name": "tp", "price": 1.1100, "fraction": 1.0, "filled": False}]


def test_native_engine_fixed_risk_amount_sizes_from_actual_stop():
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 100.0

def test_native_engine_fixed_risk_amount_honors_mt5_tick_metadata():
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
            "cap_fixed_risk_by_leverage": False,
        }
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 1.0


def test_fixed_risk_prefers_explicit_stop_distance():
    # MT5 parity (strategy-test-audit): lots derive from the strategy's stop
    # distance (e.g. ATR-based), not from |entry - stop|, so fill-price noise
    # never leaks into sizing.
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.7,  # noisy fill, must not matter
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=5.0,
    )

    assert quantity == 100.0


def test_fixed_risk_falls_back_to_entry_minus_stop_without_distance():
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=None,
    )

    assert quantity == 100.0


def test_bar_spread_defaults_to_zero_without_column():
    engine = _engine()
    row = pd.Series({"open": 100.0})

    assert engine._bar_spread(row) == 0.0


def test_bar_spread_reads_configured_price_column():
    engine = _engine({"spread_price_col": "spread_price"})
    row = pd.Series({"open": 100.0, "spread_price": 0.25})

    assert engine._bar_spread(row) == 0.25


def test_bar_spread_treats_nan_as_zero():
    import math

    engine = _engine({"spread_price_col": "spread_price"})
    row = pd.Series({"open": 100.0, "spread_price": float("nan")})

    assert engine._bar_spread(row) == 0.0
    assert not math.isnan(engine._bar_spread(row))


def _spread_frame():
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
            "spread_price": [0.2, 0.2, 0.2],
        }
    )


def _spread_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["LONG", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


def test_market_long_pays_spread_short_hits_bid():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # LONG market order created on bar 0 fills at bar 1 open + spread.
    assert trade.entry_price == 101.0 + 0.2


def test_signal_exit_fill_open_uses_bar_open():
    from runner.engine import TradeEngine as _TE

    engine = _TE(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="open",
            parameters={"fees": "zero"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    # Signal exit on bar 2 fills at bar 2 open (not close 102.2).
    assert result.trades[0].exits[0].price == 102.0


def test_signal_exit_fill_close_is_default():
    from runner.engine import TradeEngine as _TE

    engine = _TE(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].price == 102.2


def _short_spread_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


def test_signal_exit_fill_open_short_pays_spread():
    # MT5 parity: closing a SHORT buys the ask (open + spread).
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="open",
            parameters={"fees": "zero", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _short_spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # SHORT entry hits the bid; signal exit fills at open + spread (ask).
    assert trade.entry_price == 101.0
    assert trade.exits[0].price == 102.0 + 0.2


def test_warmup_bars_feed_indicators_but_create_no_trades():
    # Warmup (strategy-test-audit): bars before start_date warm indicators;
    # entries AND exits on them are ignored, the equity curve starts at start.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [100.5, 101.5, 102.5, 103.5],
            "low": [99.5, 100.5, 101.5, 102.5],
            "close": [100.2, 101.2, 102.2, 103.2],
        }
    )

    def warmup_strategy(df, params):
        entries = pd.Series([True, False, True, False], index=df.index)
        exits = pd.Series([False, True, False, True], index=df.index)
        return entries, exits, {}, []

    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01 00:02",
            end_date="2024-01-01 00:03",
            initial_capital=100_000.0,
            warmup_start="2024-01-01 00:00",
            parameters={"fees": "zero"},
        ),
        symbol="T",
    )
    result = engine.run(frame, warmup_strategy)
    # Only the 00:02 entry -> 00:03 exit survives (market fills next-open);
    # warmup bars trade nothing.
    assert len(result.trades) == 1
    assert result.trades[0].entry_time.startswith("2024-01-01T00:03")


def _ask_stop_frame():
    # SHORT with stop at 101. Bid high never breaches; ask high does on bar 2.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "ask_high": [100.5, 100.5, 101.5],
            "ask_low": [99.5, 99.5, 99.5],
        }
    )


def _ask_stop_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, False], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    sl = pd.Series([101.0, float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "sl_price": sl}, []


def test_short_stop_uses_ask_high_when_enabled():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "ask_ohlc": True},
        ),
        symbol="T",
    )
    result = engine.run(_ask_stop_frame(), _ask_stop_strategy)
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].reason == "stop_loss"
    assert result.trades[0].exits[0].price == 101.0


def test_short_stop_stays_bid_based_by_default():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero"},
        ),
        symbol="T",
    )
    result = engine.run(_ask_stop_frame(), _ask_stop_strategy)
    # Bid high (100.5) never reaches the 101 stop -> no exit, position open
    # until end_of_data.
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].reason == "end_of_data"


def _fill_bar_stop_frame():
    # LONG stop 95: bar 1 (the fill bar) already trades through it.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                ],
                utc=True,
            ),
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0, 101.0],
            "low": [99.0, 90.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0, 100.0],
            "volume": [1000.0, 1000.0, 1000.0, 1000.0],
        }
    )


def _fill_bar_stop_strategy(df, params):
    entries = pd.Series([True, False, False, False], index=df.index)
    exits = pd.Series([False, False, False, False], index=df.index)
    direction = pd.Series(["LONG", None, None, None], index=df.index, dtype=object)
    stop = pd.Series([95.0, float("nan"), float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "stop_loss": stop}, []


def test_fill_bar_stop_exits_on_fill_bar():
    # A-H1: the market order fills at bar 1 open and bar 1 already trades
    # through the fresh stop — the scoped fill-bar recheck must exit on bar
    # 1 itself instead of leaking a full extra bar of phantom exposure.
    engine = _engine()
    result = engine.run(_fill_bar_stop_frame(), _fill_bar_stop_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.holding_bars == 0
    assert trade.exits[0].reason == "stop_loss"
    assert trade.exits[0].price == 95.0
    assert trade.exits[0].time.startswith("2024-01-01T00:01")


def _short_spread_stop_frame():
    # SHORT filled at bar 1 open (100); bar 1 high touches stop 105.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 106.0, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "spread_price": [0.5, 0.5, 0.5],
        }
    )


def _short_spread_stop_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, False], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    stop = pd.Series([105.0, float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "stop_loss": stop}, []


def test_short_stop_fill_adds_spread():
    # A-H7: SHORT stop fills buy the ask (stop level + spread), LONG stops
    # sell the bid (level as-is).
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_short_spread_stop_frame(), _short_spread_stop_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 100.0
    assert trade.exits[0].reason == "stop_loss"
    assert trade.exits[0].price == 105.0 + 0.5


def test_explicit_fees_reduce_net_pnl():
    # A-L1: fee_rate (fraction of notional per side) + fee_flat_per_trade
    # must come out of net_pnl instead of silently staying 0.0.
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fee_rate": 0.01, "fee_flat_per_trade": 3.0},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    qty = trade.quantity
    expected_fees = (trade.entry_price * qty + trade.exits[0].price * qty) * 0.01 + 3.0
    assert trade.fees > 0
    assert trade.fees == pytest.approx(expected_fees)
    assert trade.net_pnl == trade.pnl - trade.fees


def test_native_engine_orders_take_profit_levels_numerically():
    # Regression (#154): string sorting put tp10/tp11 before tp2.
    engine = _engine()
    # Insert out of order so the result cannot come from column order.
    columns = ["tp10", "tp2", "tp11", "tp1", "tp9", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8"]
    row = pd.Series({name: 100.0 + int(name[2:]) for name in columns})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == [f"tp{i}" for i in range(1, 12)]
    assert [level["price"] for level in take_profit_levels] == [100.0 + i for i in range(1, 12)]


def test_native_engine_maps_pct_fractions_to_numerically_ordered_levels():
    engine = _engine()
    data = {f"tp{i}": 100.0 + i for i in range(1, 12)}
    # Fractions are keyed by level name, so they must follow their level
    # through the numeric re-ordering (tp2_pct -> tp2, tp10_pct -> tp10).
    data["tp2_pct"] = 0.5
    data["tp10_pct"] = 0.1
    row = pd.Series(data)

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == [f"tp{i}" for i in range(1, 12)]
    fractions = {level["name"]: level["fraction"] for level in take_profit_levels}
    default_fraction = 1.0 / 11
    assert fractions["tp2"] == pytest.approx(0.5)
    assert fractions["tp10"] == pytest.approx(0.1)
    for name in ("tp1", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8", "tp9", "tp11"):
        assert fractions[name] == pytest.approx(default_fraction)


def test_native_engine_puts_plain_take_profit_before_numbered_levels():
    engine = _engine()
    row = pd.Series(
        {"tp2": 102.0, "tp10": 110.0, "tp_pct": 0.25, "tp1": 101.0, "take_profit": 99.0}
    )

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp", "tp1", "tp2", "tp10"]
    assert take_profit_levels[0]["price"] == 99.0
    assert take_profit_levels[0]["fraction"] == pytest.approx(0.25)


def test_native_engine_ignores_non_decimal_tp_suffix_columns():
    # Regression guard: str.isdigit() accepts superscripts such as "²", which
    # int() rejects. Such columns must be ignored, not crash the sort key.
    engine = _engine()
    row = pd.Series({"tp1": 101.0, "tp²": 102.0, "tp2": 103.0, "tp²_pct": 0.9})

    stop_loss, take_profit_levels = engine._extract_levels(row)

    assert stop_loss is None
    assert [level["name"] for level in take_profit_levels] == ["tp1", "tp2"]
    assert [level["price"] for level in take_profit_levels] == [101.0, 103.0]
    assert [level["fraction"] for level in take_profit_levels] == [
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]


# ─── #237: take_profit / tp_price are ONE level, never two ─────────────────────


def test_native_engine_collapses_take_profit_alias_into_a_single_level():
    """A row carrying both spellings of the plain TP must yield one level (#237).

    Two "tp" levels would both read the ``tp_pct`` fraction, so a 50% exit
    fraction closed the whole position at the take-profit level.
    """
    engine = _engine()
    row = pd.Series(
        {"take_profit": 110.0, "tp_price": 110.0, "sl_price": 90.0, "tp_pct": 0.5}
    )

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp"]
    assert take_profit_levels[0]["price"] == pytest.approx(110.0)
    assert take_profit_levels[0]["fraction"] == pytest.approx(0.5)


def test_native_engine_take_profit_alias_prefers_the_canonical_column():
    """Canonical ``take_profit`` wins over ``tp_price`` (#237).

    The precedence is the one every other resolver in the repo uses
    (``risk.live._LIVE_TP_KEYS``, the comparison/extraction helpers), so a
    conflicting row resolves the same level here and in live.
    """
    engine = _engine()
    row = pd.Series({"take_profit": 110.0, "tp_price": 115.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp"]
    assert take_profit_levels[0]["price"] == pytest.approx(110.0)


def test_native_engine_take_profit_alias_resolution_ignores_column_order():
    """The winner must not depend on the signal frame's column order (#237)."""
    engine = _engine()
    forward = pd.Series({"take_profit": 110.0, "tp_price": 115.0})
    reversed_row = pd.Series({"tp_price": 115.0, "take_profit": 110.0})

    _, forward_levels = engine._extract_levels(forward)
    _, reversed_levels = engine._extract_levels(reversed_row)

    assert forward_levels == reversed_levels
    assert forward_levels[0]["price"] == pytest.approx(110.0)


def test_native_engine_take_profit_alias_falls_back_to_tp_price_when_null():
    """``tp_price`` is still honoured when ``take_profit`` is absent/null (#237)."""
    engine = _engine()
    row = pd.Series({"take_profit": float("nan"), "tp_price": 115.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp"]
    assert take_profit_levels[0]["price"] == pytest.approx(115.0)


def test_native_engine_take_profit_alias_does_not_consume_a_fraction_share():
    """The duplicated alias must not inflate the default-fraction denominator (#237).

    With both columns present the split is over the real levels (plain TP plus
    ``tp1``/``tp2``), not over a phantom fourth one.
    """
    engine = _engine()
    row = pd.Series(
        {"take_profit": 110.0, "tp_price": 115.0, "tp1": 105.0, "tp2": 120.0}
    )

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp", "tp1", "tp2"]
    assert [level["price"] for level in take_profit_levels] == [110.0, 105.0, 120.0]
    assert [level["fraction"] for level in take_profit_levels] == [
        pytest.approx(1.0 / 3.0)
    ] * 3


def test_native_engine_take_profit_alias_fills_one_take_profit_level():
    """End to end (#237): the aliased pair is one take-profit level.

    The strategy publishes its single take-profit level under both spellings,
    with conflicting prices (``take_profit`` 105, ``tp_price`` 112). The
    duplicate made the engine arm two levels, so bar 2 closed half the
    position at 105 and the rest at 112 — two take-profit legs for one
    intended level.
    """
    engine = _engine()
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                    "2024-01-01 00:04",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 105.0, 105.0, 105.0],
            "high": [100.5, 101.5, 112.5, 105.5, 105.5],
            "low": [99.5, 100.5, 104.5, 104.5, 104.5],
            "close": [100.0, 101.0, 105.0, 105.0, 105.0],
        }
    )

    def strategy(df, params):
        index = df.index
        entries = pd.Series([True, False, False, False, False], index=index)
        exits = pd.Series([False, False, False, False, True], index=index)
        indicators = {
            "direction": pd.Series(["LONG", None, None, None, None], index=index, dtype=object),
            # Both spellings of the SAME level, with conflicting prices.
            "take_profit": pd.Series([105.0, None, None, None, None], index=index, dtype=object),
            "tp_price": pd.Series([112.0, None, None, None, None], index=index, dtype=object),
        }
        return entries, exits, indicators, []

    result = engine.run(frame, strategy)

    assert len(result.trades) == 1
    trade = result.trades[0]
    take_profit_exits = [leg for leg in trade.exits if leg.reason == "tp"]
    assert len(take_profit_exits) == 1, (
        "one aliased level must fill once, not once per spelling"
    )
    assert take_profit_exits[0].price == pytest.approx(105.0)
    assert take_profit_exits[0].quantity == pytest.approx(trade.quantity)


# ─── #237 follow-up: a level price that cannot fire arms no level ─────────────


def test_native_engine_take_profit_alias_falls_back_when_the_canonical_is_not_usable():
    """A non-positive canonical price is not a level (live parity, #237).

    ``risk.live.resolve_live_levels`` raises ``take_profit must be > 0``
    (``risk/live.py``), so a row whose canonical spelling is ``0`` must never
    win over a usable alias: a LONG take-profit of 0 is reached by every bar
    (``bar_high >= 0``), and arming it closed the whole position at price 0.
    """
    engine = _engine()
    row = pd.Series({"take_profit": 0.0, "tp_price": 115.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp"]
    assert take_profit_levels[0]["price"] == pytest.approx(115.0)


def test_native_engine_take_profit_alias_ignores_a_non_finite_canonical_price():
    engine = _engine()
    row = pd.Series({"take_profit": float("inf"), "tp_price": 115.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["price"] for level in take_profit_levels] == [115.0]


def test_native_engine_does_not_arm_a_non_positive_take_profit_level():
    """With no usable alias the row arms no take-profit level at all."""
    engine = _engine()
    row = pd.Series({"take_profit": 0.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert take_profit_levels == []


def test_native_engine_does_not_arm_a_non_positive_numbered_take_profit_level():
    """The same rule for numbered levels: ``tp1: 0`` is not a level."""
    engine = _engine()
    row = pd.Series({"tp1": 0.0, "tp2": 120.0})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp2"]
    assert [level["price"] for level in take_profit_levels] == [120.0]
    assert take_profit_levels[0]["fraction"] == pytest.approx(1.0)


def test_native_engine_non_positive_take_profit_does_not_close_the_position_at_zero():
    """End to end (#237): a ``take_profit`` of 0 must not book a fantasy exit.

    A LONG take-profit of 0 is reached by every bar, so arming it closed the
    whole position at price 0 on the first monitored bar. The alias price is
    the usable one, and that is the level the exit must fill at.
    """
    engine = _engine()
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                    "2024-01-01 00:04",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 105.0, 105.0, 105.0],
            "high": [100.5, 101.5, 112.5, 105.5, 105.5],
            "low": [99.5, 100.5, 104.5, 104.5, 104.5],
            "close": [100.0, 101.0, 105.0, 105.0, 105.0],
        }
    )

    def strategy(df, params):
        index = df.index
        entries = pd.Series([True, False, False, False, False], index=index)
        exits = pd.Series([False, False, False, False, True], index=index)
        indicators = {
            "direction": pd.Series(["LONG", None, None, None, None], index=index, dtype=object),
            "stop_loss": pd.Series([95.0, None, None, None, None], index=index, dtype=object),
            # The unusable canonical price plus the usable alias price.
            "take_profit": pd.Series([0.0, None, None, None, None], index=index, dtype=object),
            "tp_price": pd.Series([112.0, None, None, None, None], index=index, dtype=object),
        }
        return entries, exits, indicators, []

    result = engine.run(frame, strategy)

    assert len(result.trades) == 1
    trade = result.trades[0]
    take_profit_exits = [leg for leg in trade.exits if leg.reason == "tp"]
    assert len(take_profit_exits) == 1
    assert take_profit_exits[0].price == pytest.approx(112.0)
    assert take_profit_exits[0].quantity == pytest.approx(trade.quantity)
    assert all(leg.price > 0 for leg in trade.exits), "no leg may fill at price 0"


def test_native_engine_fixed_risk_amount_never_arms_a_take_profit_at_zero():
    """End to end (#237): the fixed-amount sizing path books no price-0 exit.

    ``fixed_risk_amount`` sizing returns before the ``TradeSignal`` that
    validates the level, so the unusable price reached ``_create_order`` and
    armed a take-profit at 0 — which every bar reaches, closing the whole
    position at price 0 on the first monitored bar. The alias price is the
    usable one, so that is the level the exit fills at.
    """
    engine = _engine({"fixed_risk_amount": 500.0})
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                    "2024-01-01 00:04",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 105.0, 105.0, 105.0],
            "high": [100.5, 101.5, 112.5, 105.5, 105.5],
            "low": [99.5, 100.5, 104.5, 104.5, 104.5],
            "close": [100.0, 101.0, 105.0, 105.0, 105.0],
        }
    )

    def strategy(df, params):
        index = df.index
        entries = pd.Series([True, False, False, False, False], index=index)
        exits = pd.Series([False, False, False, False, True], index=index)
        indicators = {
            "direction": pd.Series(["LONG", None, None, None, None], index=index, dtype=object),
            "stop_loss": pd.Series([95.0, None, None, None, None], index=index, dtype=object),
            "take_profit": pd.Series([0.0, None, None, None, None], index=index, dtype=object),
            "tp_price": pd.Series([112.0, None, None, None, None], index=index, dtype=object),
        }
        return entries, exits, indicators, []

    result = engine.run(frame, strategy)

    assert len(result.trades) == 1
    trade = result.trades[0]
    take_profit_exits = [leg for leg in trade.exits if leg.reason == "tp"]
    assert len(take_profit_exits) == 1
    assert take_profit_exits[0].price == pytest.approx(112.0)
    assert all(leg.price > 0 for leg in trade.exits), "no leg may fill at price 0"

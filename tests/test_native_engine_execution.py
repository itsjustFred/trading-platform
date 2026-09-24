from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
import pandas as pd
import pytest


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

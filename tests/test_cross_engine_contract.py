"""Cross-engine contract tests (A-L9): native vs vectorbt parity surface.

Pins the parity regressions from A-H4 (warmup_start, signal_exit_fill,
spread/ask) and A-H2 (entry_stop), plus the unified D4 exit convention
(Fill-zum-Trigger-Close, A-M1) and the D5 shared-equity replay (A-M4):

- signal exits fill at the trigger bar's close in BOTH engines;
- LONG market entries pay the spread column in BOTH engines;
- warmup bars create no trades in BOTH engines;
- inverted-stop drops are counted and reported (A-M7);
- multi-symbol runs share one portfolio equity with summed exposure (A-M4).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from shared.models import Trade, TradeExit
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_ohlcv


def _frame(*, spread: float = 0.0) -> pd.DataFrame:
    closes = [100.0 + i * 1.0 for i in range(12)]
    data: dict[str, list] = {
        "timestamp": pd.date_range("2024-01-01", periods=12, freq="h", tz="UTC"),
        "open": list(closes),
        "high": [c + 0.25 for c in closes],
        "low": [c - 0.25 for c in closes],
        "close": closes,
        "volume": [1000.0] * 12,
    }
    if spread:
        data["spread_price"] = [spread] * 12
    return pd.DataFrame(data)


def _strategy_text(*, entry_bar: int = 2, exit_bar: int = 7, spread_col: bool = False) -> str:
    return f'''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry_bar}] = True
    exits.iloc[{exit_bar}] = True
    indicators = {{"direction": pd.Series(["LONG"] * n, index=df.index)}}
    indicators["exit_reason"] = pd.Series([None] * n, index=df.index, dtype=object)
    indicators["exit_reason"].iloc[{exit_bar}] = "signal"
    return entries, exits, indicators, []
'''


def _write_strategy(tmp_path: Path, name: str, text: str) -> str:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(exist_ok=True)
    path = strategy_dir / f"{name}.py"
    path.write_text(text)
    return str(path)


def _native_result(frame: pd.DataFrame, strategy_fn, **config_kwargs):
    payload = {
        "strategy_name": "contract",
        "symbols": ["CEC"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
    }
    payload.update(config_kwargs)
    config = BacktestConfig(**with_zero_fees(payload))
    engine = TradeEngine(config, symbol="CEC")
    return engine.run(frame, strategy_fn)


def _vectorbt_result(tmp_path: Path, frame: pd.DataFrame, strategy_text: str, **config_kwargs):
    _write_ohlcv(tmp_path, "CEC", frame)
    strategy_path = _write_strategy(tmp_path, "contract_strat", strategy_text)
    payload = {
        "strategy_name": "contract",
        "symbols": ["CEC"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(config_kwargs)
    return VectorbtAdapter().run(BacktestConfig(**with_zero_fees(payload)), strategy_path)


def _load_strategy_fn(strategy_text: str, tmp_path: Path, name: str):
    import importlib.util

    path = _write_strategy(tmp_path, name, strategy_text)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"no import spec for {name!r}"
    assert spec.loader is not None, f"import spec for {name!r} has no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_signals


class TestSignalExitConvention:
    """D4 (A-M1): signal exits fill at the trigger bar's close, both engines."""

    def test_exit_price_equals_trigger_close_on_both_engines(self, tmp_path: Path):
        frame = _frame()
        text = _strategy_text(entry_bar=2, exit_bar=7)
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_exit")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        assert len(native.trades) == 1
        assert len(vectorbt.trades) == 1
        expected_entry = float(frame["open"].iloc[3])  # next-open after bar 2
        expected_exit = float(frame["close"].iloc[7])  # trigger close
        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.entry_price == pytest.approx(expected_entry)
            assert trade.exits[0].price == pytest.approx(expected_exit)
            assert trade.exits[0].reason == "signal"
        # Exit timestamps agree (same trigger bar, format-insensitive).
        native_ts = pd.to_datetime(native.trades[0].exits[0].time, utc=True)
        vbt_ts = pd.to_datetime(vectorbt.trades[0].exits[0].time, utc=True)
        assert native_ts == vbt_ts

    def test_long_market_entry_pays_spread_on_both_engines(self, tmp_path: Path):
        frame = _frame(spread=0.5)
        text = _strategy_text(entry_bar=2, exit_bar=7)
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_spread")
        params = {"spread_price_col": "spread_price"}

        native = _native_result(frame, strategy_fn, parameters=params)
        vectorbt = _vectorbt_result(tmp_path, frame, text, parameters=params)

        expected_entry = float(frame["open"].iloc[3]) + 0.5
        assert native.trades[0].entry_price == pytest.approx(expected_entry)
        assert vectorbt.trades[0].entry_price == pytest.approx(expected_entry)

    def test_warmup_bars_create_no_trades_on_both_engines(self, tmp_path: Path):
        # Entry/exit on bars 1->2 (warmup) must be ignored; bars 6->8 trade.
        text = """
import pandas as pd


def generate_signals(df, params):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[2] = True
    entries.iloc[6] = True
    exits.iloc[8] = True
    indicators = {"direction": pd.Series(["LONG"] * len(df), index=df.index)}
    return entries, exits, indicators, []
"""
        frame = _frame()
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_warmup")
        warmup = {
            "warmup_start": "2024-01-01 00:00",
            "start_date": "2024-01-01 05:00",
            "end_date": "2024-01-02",
        }
        native = _native_result(frame, strategy_fn, **warmup)
        vectorbt = _vectorbt_result(tmp_path, frame, text, **warmup)

        assert len(native.trades) == 1
        assert len(vectorbt.trades) == 1
        # Entry signal bar 6 fills at bar 7 open; exit signal bar 8 at close.
        assert native.trades[0].entry_price == pytest.approx(float(frame["open"].iloc[7]))
        assert vectorbt.trades[0].entry_price == pytest.approx(float(frame["open"].iloc[7]))
        assert native.trades[0].exits[0].price == pytest.approx(float(frame["close"].iloc[8]))
        assert vectorbt.trades[0].exits[0].price == pytest.approx(float(frame["close"].iloc[8]))

    def test_warmup_without_start_date_fails_loudly(self, tmp_path: Path):
        text = _strategy_text()
        frame = _frame()
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_warmup_fail")
        with pytest.raises(ValueError, match="warmup_start"):
            _native_result(
                frame, strategy_fn, warmup_start="2024-01-01 00:00", start_date=""
            )

    def test_signal_exit_fill_rejects_unknown_values(self, tmp_path: Path):
        """D4: the fill-convention switch is load-bearing — a typo must fail
        loudly instead of silently falling through to close-fill."""
        with pytest.raises(ValueError, match="signal_exit_fill"):
            BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero", "slippage": "none"},
                strategy_name="contract",
                symbols=["CEC"],
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-01-02",
                initial_capital=100_000.0,
                signal_exit_fill="clos",
            )

    def test_signal_exit_fill_normalizes_case(self, tmp_path: Path):
        config = BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero", "slippage": "none"},
            strategy_name="contract",
            symbols=["CEC"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="Close",
        )
        assert config.signal_exit_fill == "close"


def _exit_order_type_strategy_text(
    *,
    order_type: str | None,
    exit_price: float | None,
    direction: str = "LONG",
    entry_bar: int = 2,
    exit_bar: int = 7,
) -> str:
    """Entry on ``entry_bar``, signal exit on ``exit_bar`` with explicit exit order type."""
    order_expr = "None" if order_type is None else repr(order_type)
    price_expr = "None" if exit_price is None else repr(exit_price)
    return f'''
import pandas as pd


ORDER_TYPE = {order_expr}
EXIT_PRICE = {price_expr}


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry_bar}] = True
    exits.iloc[{exit_bar}] = True
    indicators = {{"direction": pd.Series(["{direction}"] * n, index=df.index)}}
    indicators["exit_reason"] = pd.Series([None] * n, index=df.index, dtype=object)
    indicators["exit_reason"].iloc[{exit_bar}] = "signal"
    indicators["exit_order_type"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_order_type"].iloc[{exit_bar}] = ORDER_TYPE
    indicators["exit_price"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_price"].iloc[{exit_bar}] = EXIT_PRICE
    return entries, exits, indicators, []
'''


class TestExitOrderType:
    """Explicit ``exit_order_type`` on signal exits (#243).

    ``market`` fills at the signal-exit convention price and ignores
    ``exit_price``; ``level`` fills at ``exit_price``; absent keeps the
    documented legacy rule per engine.
    """

    def test_market_ignores_exit_price_on_both_engines(self, tmp_path: Path):
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type="market", exit_price=105.5)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_market")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        # Bar 7 close is 107.0; the level 105.5 must be ignored.
        for result in (native, vectorbt):
            assert result.trades[0].exits[0].price == pytest.approx(107.0)

    def test_level_fills_at_exit_price_on_both_engines(self, tmp_path: Path):
        frame = _frame()
        # 107.1 sits inside bar 7's range (106.75..107.25).
        text = _exit_order_type_strategy_text(order_type="Level", exit_price=107.1)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_level")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            assert result.trades[0].exits[0].price == pytest.approx(107.1)

    def test_level_without_usable_price_falls_back_flagged(self, tmp_path: Path):
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type="level", exit_price=None)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_noprice")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_missing_price") is True

    def test_unspecified_keeps_legacy_rule_per_engine(self, tmp_path: Path):
        # No exit_order_type: vectorbt honors a usable exit_price as a level
        # (legacy), native fills at the convention close (legacy).
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type=None, exit_price=107.1)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_legacy")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        assert native.trades[0].exits[0].price == pytest.approx(107.0)
        assert vectorbt.trades[0].exits[0].price == pytest.approx(107.1)

    def test_level_via_dataframe_columns(self, tmp_path: Path):
        # DataFrame-output strategies carry exit_order_type/exit_price as
        # signal columns rather than indicators.
        frame = _frame()
        text = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = [False] * n
    exits = [False] * n
    entries[2] = True
    exits[7] = True
    exit_price = [float("nan")] * n
    exit_price[7] = 107.1
    exit_order_type = [None] * n
    exit_order_type[7] = "level"
    return pd.DataFrame(
        {
            "entries": entries,
            "exits": exits,
            "exit_price": exit_price,
            "exit_order_type": exit_order_type,
        },
        index=df.index,
    )
'''
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_df")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            assert result.trades[0].exits[0].price == pytest.approx(107.1)

    def test_unrecognized_order_type_falls_back_to_legacy(self, tmp_path: Path):
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type="bogus", exit_price=107.1)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_bogus")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        assert native.trades[0].exits[0].price == pytest.approx(107.0)
        assert vectorbt.trades[0].exits[0].price == pytest.approx(107.1)

    def test_level_whitespace_spelling_fills_at_price(self, tmp_path: Path):
        frame = _frame()
        text = _exit_order_type_strategy_text(
            order_type=" level ", exit_price=107.1
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_ws")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.1)
            # Canonical spelling, not the raw " level " cell.
            assert trade.meta.get("exit_order_type") == "level"

    def test_short_level_fills_at_price_on_both_engines(self, tmp_path: Path):
        # Bar 7 trades 106.75..107.25; a SHORT exit is a buy limit that
        # fills when the bar's low reaches the level — 107.1 is inside.
        frame = _frame()
        text = _exit_order_type_strategy_text(
            order_type="level", exit_price=107.1, direction="SHORT"
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_type_short")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            assert result.trades[0].direction == "SHORT"
            assert result.trades[0].exits[0].price == pytest.approx(107.1)


class TestExitLevelReachability:
    """A ``level`` the exit bar never traded through cannot have filled.

    Both engines fall back to the market fill, warn once and flag the trade
    ``meta["exit_level_unreachable"]`` instead of booking a fantasy price.
    """

    def test_unreachable_long_level_falls_back_flagged(self, tmp_path: Path):
        # Bar 7 trades 106.75..107.25; a LONG exit level at 500 never printed.
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type="level", exit_price=500.0)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_unreach_long")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_unreachable") is True
            assert trade.meta.get("exit_order_type") == "level"

    def test_unreachable_short_level_falls_back_flagged(self, tmp_path: Path):
        # A SHORT exit is a buy limit: it fills only when the bar's low
        # reaches the level. 50 sits below bar 7's low (106.75).
        frame = _frame()
        text = _exit_order_type_strategy_text(
            order_type="level", exit_price=50.0, direction="SHORT"
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_unreach_short")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_unreachable") is True

    def test_implicit_exit_price_unreachable_falls_back_vectorbt(self, tmp_path: Path):
        # vectorbt's legacy rule (usable exit_price = level) gets the same
        # reachability guard; native never reads exit_price implicitly.
        frame = _frame()
        text = _exit_order_type_strategy_text(order_type=None, exit_price=500.0)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_implicit")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        assert native.trades[0].exits[0].price == pytest.approx(107.0)
        trade = vectorbt.trades[0]
        assert trade.exits[0].price == pytest.approx(107.0)
        assert trade.meta.get("exit_level_unreachable") is True

    def test_terminal_bar_close_ignores_exit_columns(self, tmp_path: Path):
        # exit_order_type/exit_price on every bar but NO exit signal: the
        # forced end-of-data close must use the convention fill in both
        # engines (native's end_of_data close reads neither column).
        frame = _frame()
        text = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    indicators = {"direction": pd.Series(["LONG"] * n, index=df.index)}
    indicators["exit_order_type"] = pd.Series(
        ["level"] * n, index=df.index, dtype=object
    )
    indicators["exit_price"] = pd.Series(
        [105.5] * n, index=df.index, dtype=float
    )
    return entries, exits, indicators, []
'''
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_terminal")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        expected = float(frame["close"].iloc[-1])
        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(expected)
            assert "exit_level_unreachable" not in trade.meta

    def test_missing_level_superseded_by_stop_is_not_flagged(self, tmp_path: Path):
        # vectorbt only: with intrabar data a stop/TP touched on the trigger
        # bar supersedes the signal exit, so a missing level must NOT warn
        # or flag — the trade never left on the signal exit.
        frame = _frame()
        intrabar = pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    "2024-01-01 07:00", periods=12, freq="5min", tz="UTC"
                ),
                "open": [107.0] * 12,
                "high": [107.2] * 12,
                "low": [106.9] * 11 + [99.5],
                "close": [107.0] * 11 + [99.6],
                "volume": [100.0] * 12,
            }
        )
        _write_ohlcv(tmp_path, "CEC", frame)
        _write_ohlcv(tmp_path, "CEC", intrabar, timeframe="5m")
        text = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    exits.iloc[7] = True
    indicators = {"direction": pd.Series(["LONG"] * n, index=df.index)}
    indicators["stop_loss"] = pd.Series([float("nan")] * n, index=df.index)
    indicators["stop_loss"].iloc[2] = 100.0
    indicators["exit_order_type"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_order_type"].iloc[7] = "level"
    indicators["exit_price"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    return entries, exits, indicators, []
'''
        strategy_path = _write_strategy(tmp_path, "exit_lvl_stop", text)
        payload = {
            "strategy_name": "exit_lvl_stop",
            "symbols": ["CEC"],
            "timeframe": "1h",
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
            "initial_capital": 100_000.0,
            "risk_per_trade_pct": 0.01,
            "modeling_resolution": "5m",
            "data_dir": str(tmp_path),
            "parameters": {},
        }
        result = VectorbtAdapter().run(
            BacktestConfig(**with_zero_fees(payload)), strategy_path
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exits[0].reason == "stop_loss"
        assert trade.exits[0].price == pytest.approx(100.0)
        assert "exit_level_missing_price" not in trade.meta
        assert "exit_level_unreachable" not in trade.meta


def _gap_frame(*, spread: float = 0.0) -> pd.DataFrame:
    """12 hourly bars whose exit bar (index 7) opens away from its close.

    Bar 7: open 108.0, high 108.25, low 106.75, close 107.0 — so a
    gap-through-level fill at the OPEN (108.0) is distinguishable from the
    market-fallback CLOSE (107.0).
    """
    closes = [100.0 + i for i in range(12)]
    opens = list(closes)
    opens[7] = 108.0
    data: dict[str, list] = {
        "timestamp": pd.date_range("2024-01-01", periods=12, freq="h", tz="UTC"),
        "open": opens,
        "high": [max(o, c) + 0.25 for o, c in zip(opens, closes, strict=False)],
        "low": [min(o, c) - 0.25 for o, c in zip(opens, closes, strict=False)],
        "close": closes,
        "volume": [1000.0] * 12,
    }
    if spread:
        data["spread_price"] = [spread] * 12
    return pd.DataFrame(data)


_UNREACHABLE_TWO_TRADE_TEXT = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    exits.iloc[5] = True
    entries.iloc[8] = True
    exits.iloc[10] = True
    indicators = {"direction": pd.Series(["LONG"] * n, index=df.index)}
    indicators["exit_reason"] = pd.Series([None] * n, index=df.index, dtype=object)
    indicators["exit_reason"].iloc[5] = "signal"
    indicators["exit_reason"].iloc[10] = "signal"
    indicators["exit_order_type"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_order_type"].iloc[5] = "level"
    indicators["exit_order_type"].iloc[10] = "level"
    indicators["exit_price"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_price"].iloc[5] = 500.0
    indicators["exit_price"].iloc[10] = 500.0
    return entries, exits, indicators, []
'''


_PYRAMID_TEXT = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    entries.iloc[3] = True
    exits.iloc[7] = True
    indicators = {"direction": pd.Series(["LONG"] * n, index=df.index)}
    indicators["exit_reason"] = pd.Series([None] * n, index=df.index, dtype=object)
    indicators["exit_reason"].iloc[7] = "signal"
    indicators["exit_order_type"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_order_type"].iloc[7] = "level"
    indicators["exit_price"] = pd.Series(
        [None] * n, index=df.index, dtype=object
    )
    indicators["exit_price"].iloc[7] = 500.0
    return entries, exits, indicators, []
'''


class TestExitLevelGapped:
    """A ``level`` beyond the exit bar on the marketable side gapped the
    open: both engines fill at the bar's OPEN (gap-through-level
    convention) and flag ``meta["exit_level_gapped"]`` — never at the
    level, which the bar never traded.
    """

    def test_gapped_long_level_fills_at_open_flagged(self, tmp_path: Path):
        # Bar 7: low 106.75 — a LONG sell limit at 106.0 was marketable at
        # the open (108.0), not a fill at 106.0.
        frame = _gap_frame()
        text = _exit_order_type_strategy_text(order_type="level", exit_price=106.0)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_gapped_long")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(108.0)
            assert trade.meta.get("exit_level_gapped") is True
            assert "exit_level_unreachable" not in trade.meta

    def test_gapped_short_level_fills_at_open_flagged(self, tmp_path: Path):
        # A SHORT buy limit at 109.0 sat above bar 7's high (108.25):
        # marketable at the open -> fill at the open.
        frame = _gap_frame()
        text = _exit_order_type_strategy_text(
            order_type="level", exit_price=109.0, direction="SHORT"
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_gapped_short")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(108.0)
            assert trade.meta.get("exit_level_gapped") is True

    def test_gapped_short_level_pays_spread_on_open_fill(self, tmp_path: Path):
        # A gapped SHORT exit buys the ask: open + spread, like every other
        # SHORT open-side fill.
        frame = _gap_frame(spread=0.5)
        params = {"spread_price_col": "spread_price"}
        text = _exit_order_type_strategy_text(
            order_type="level", exit_price=109.0, direction="SHORT"
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_gapped_sprd")

        native = _native_result(frame, strategy_fn, parameters=params)
        vectorbt = _vectorbt_result(tmp_path, frame, text, parameters=params)

        for result in (native, vectorbt):
            assert result.trades[0].exits[0].price == pytest.approx(108.5)

    def test_gapped_level_with_nan_open_falls_back_to_close(self, tmp_path: Path):
        # The gap fill prefers the bar's open; a missing open degrades to
        # the close rather than booking NaN or the unreachable level.
        frame = _gap_frame()
        frame.loc[7, "open"] = float("nan")
        text = _exit_order_type_strategy_text(order_type="level", exit_price=106.0)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_gapped_nan")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_gapped") is True

    def test_gapped_level_via_native_adapter(self, tmp_path: Path):
        # End-to-end through NativeAdapter (OHLCV loaded from data_dir).
        frame = _gap_frame()
        _write_ohlcv(tmp_path, "CEC", frame)
        text = _exit_order_type_strategy_text(order_type="level", exit_price=106.0)
        strategy_path = _write_strategy(tmp_path, "exit_lvl_gapped_ad", text)
        config = BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="contract",
            symbols=["CEC"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero", "slippage": "none"},
        )
        result = NativeAdapter().run(config, strategy_path)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exits[0].price == pytest.approx(108.0)
        assert trade.meta.get("exit_level_gapped") is True

    def test_short_level_reachability_uses_ask_range(self, tmp_path: Path):
        # With ask_ohlc enabled a SHORT exit is judged on the ASK range:
        # level 106.9 sits inside the BID range (106.75..107.25) but below
        # the ask low (107.0), so the buy limit could never have filled.
        frame = _frame()
        frame["ask_high"] = frame["high"] + 0.5
        frame["ask_low"] = frame["low"] + 0.25
        params = {"ask_ohlc": True}
        text = _exit_order_type_strategy_text(
            order_type="level", exit_price=106.9, direction="SHORT"
        )
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_ask")

        native = _native_result(frame, strategy_fn, parameters=params)
        vectorbt = _vectorbt_result(tmp_path, frame, text, parameters=params)

        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_unreachable") is True

        # Without ask_ohlc the same level is inside the bid range and fills.
        native_bid = _native_result(frame, strategy_fn)
        vectorbt_bid = _vectorbt_result(tmp_path, frame, text)
        for result in (native_bid, vectorbt_bid):
            assert result.trades[0].exits[0].price == pytest.approx(106.9)
            assert "exit_level_unreachable" not in result.trades[0].meta

    def test_unreachable_level_warns_once_per_run(self, tmp_path: Path, caplog):
        # Two trades with the same unreachable level: warn-once must fire
        # exactly once per engine run, not once per trade.
        frame = _frame()
        strategy_fn = _load_strategy_fn(
            _UNREACHABLE_TWO_TRADE_TEXT, tmp_path, "exit_lvl_warn_once"
        )

        with caplog.at_level(logging.WARNING):
            native = _native_result(frame, strategy_fn)
        native_msgs = [
            r.getMessage() for r in caplog.records if "traded range" in r.getMessage()
        ]
        assert len(native.trades) == 2
        assert len(native_msgs) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            vectorbt = _vectorbt_result(tmp_path, frame, _UNREACHABLE_TWO_TRADE_TEXT)
        vbt_msgs = [
            r.getMessage() for r in caplog.records if "traded range" in r.getMessage()
        ]
        assert len(vectorbt.trades) == 2
        assert len(vbt_msgs) == 1

    def test_level_with_signal_exit_fill_open_and_spread(self, tmp_path: Path):
        # signal_exit_fill="open" + spread must not leak into a level fill:
        # an in-range level still fills AT the level on both engines.
        frame = _frame(spread=0.5)
        params = {"spread_price_col": "spread_price"}
        text = _exit_order_type_strategy_text(order_type="level", exit_price=107.1)
        strategy_fn = _load_strategy_fn(text, tmp_path, "exit_lvl_openfill")

        native = _native_result(
            frame, strategy_fn, signal_exit_fill="open", parameters=params
        )
        vectorbt = _vectorbt_result(
            tmp_path, frame, text, signal_exit_fill="open", parameters=params
        )

        for result in (native, vectorbt):
            assert result.trades[0].exits[0].price == pytest.approx(107.1)

    def test_native_pyramiding_positions_flagged(self, tmp_path: Path):
        # Native holds several concurrent positions: one exit signal with
        # an unreachable level must fall back and flag EVERY open position.
        frame = _frame()
        strategy_fn = _load_strategy_fn(_PYRAMID_TEXT, tmp_path, "exit_lvl_pyramid")

        native = _native_result(frame, strategy_fn)

        assert len(native.trades) == 2
        for trade in native.trades:
            assert trade.exits[0].price == pytest.approx(107.0)
            assert trade.meta.get("exit_level_unreachable") is True


class TestNativeWarmupLoad:
    """A-H4: NativeAdapter loads from warmup_start so indicators are warm."""

    def test_warmup_start_extends_loaded_window(self, tmp_path: Path):
        _write_ohlcv(tmp_path, "CEC", _frame())
        strategy_path = _write_strategy(tmp_path, "contract_warmup_load", _strategy_text())
        config = BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero", "slippage": "none"},
            strategy_name="contract",
            symbols=["CEC"],
            timeframe="1h",
            start_date="2024-01-01 05:00",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            warmup_start="2024-01-01 00:00",
            data_dir=str(tmp_path),
        )
        result = NativeAdapter().run(config, strategy_path)
        # 12 bars loaded (00:00..11:00), not just the 7 since start_date.
        assert result.meta.total_bars == 12


_INVERTED_TEXT = """
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    exits.iloc[7] = True
    indicators = {
        "direction": pd.Series(["LONG"] * n, index=df.index),
        "sl_price": pd.Series([float("nan")] * n, index=df.index),
    }
    indicators["sl_price"].iloc[2] = 500.0  # wrong side of a ~102 entry
    return entries, exits, indicators, []
"""


class TestInvertedStopReport:
    """A-M7: drops are counted and reported, not just warn-once logged."""

    def test_inverted_stop_drop_count_reported_in_meta(self, tmp_path: Path):
        result = _vectorbt_result(
            tmp_path,
            _frame(),
            _INVERTED_TEXT,
            parameters={},
        )
        assert result.trades == []
        assert result.meta.data["inverted_stop_drops"] == 1


class TestSharedEquity:
    """A-M4 (D5): multi-symbol runs share one portfolio equity."""

    def _two_symbol_env(self, tmp_path: Path):
        for symbol in ("SH1", "SH2"):
            _write_ohlcv(tmp_path, symbol, _frame())
        return _write_strategy(tmp_path, "shared_strat", _strategy_text())

    def test_vectorbt_multi_symbol_replays_shared_equity(self, tmp_path: Path):
        strategy_path = self._two_symbol_env(tmp_path)
        config = BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="contract",
            symbols=["SH1", "SH2"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero", "slippage": "none"},
        )
        result = VectorbtAdapter().run(config, strategy_path)
        assert len(result.trades) == 2
        summary = result.meta.data["shared_equity"]
        assert summary["shared_equity"] is True
        assert summary["symbols_traded"] == ["SH1", "SH2"]
        for trade in result.trades:
            assert "portfolio" in trade.meta
            # Simultaneous entries: nothing exited before either entry, so
            # both size from the full starting capital (no phantom scaling).
            assert trade.meta["portfolio"]["equity_at_entry"] == pytest.approx(100_000.0)
        # Exposure is SUMMED across symbols while both are open (D5) — the
        # peak exceeds either trade's notional alone.
        notionals = [t.entry_price * t.quantity for t in result.trades]
        assert summary["peak_open_notional_usd"] == pytest.approx(
            sum(notionals), rel=1e-4
        )
        final = result.equity_curve[-1].equity
        expected = 100_000.0 + sum(t.net_pnl if t.net_pnl is not None else t.pnl for t in result.trades)
        assert final == pytest.approx(expected, abs=0.02)

    def test_native_multi_symbol_replays_shared_equity(self, tmp_path: Path):
        strategy_path = self._two_symbol_env(tmp_path)
        config = BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="contract",
            symbols=["SH1", "SH2"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero", "slippage": "none"},
        )
        result = NativeAdapter().run(config, strategy_path)
        assert len(result.trades) == 2
        assert result.meta.data["shared_equity"]["shared_equity"] is True
        for trade in result.trades:
            assert "portfolio" in trade.meta

    def test_single_symbol_runs_skip_replay(self, tmp_path: Path):
        result = _vectorbt_result(tmp_path, _frame(), _strategy_text())
        assert result.meta.data["shared_equity"] == {}
        assert "portfolio" not in result.trades[0].meta


def _replay_trade(trade_id: int, symbol: str, entry: str, exit: str,
                  entry_price: float, exit_price: float, qty: float,
                  equity_at_entry: float) -> "Trade":
    direction = "LONG"
    pnl = (exit_price - entry_price) * qty
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry,
        entry_price=entry_price,
        quantity=qty,
        exits=[TradeExit(time=exit, price=exit_price, quantity=qty, pnl=pnl)],
        pnl=pnl,
        meta={"risk_math": {"equity_at_entry": equity_at_entry}},
    )


class TestSharedEquityReplayMath:
    """Unit tests for the D5 replay: chronological rescaling + exposure sum."""

    def test_sequential_trades_compound_on_shared_equity(self):
        from runner.engines._shared import replay_shared_equity

        # Both sized from 100k full capital: 100 @ 100 -> 110 (+1000).
        t1 = _replay_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
                           100.0, 110.0, 100.0, 100_000.0)
        # Second trade sized from 100k as well: 50 @ 200 -> 204 (+200).
        t2 = _replay_trade(2, "B", "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z",
                           200.0, 204.0, 50.0, 100_000.0)
        replayed, curve, summary = replay_shared_equity([t1, t2], 100_000.0)
        # t1 unchanged (first on shared equity); t2 rescales by 101/100.
        assert replayed[0].quantity == pytest.approx(100.0)
        assert replayed[1].quantity == pytest.approx(50.0 * 1.01)
        assert replayed[1].pnl == pytest.approx(200.0 * 1.01)
        assert curve[-1].equity == pytest.approx(100_000.0 + 1000.0 + 202.0)
        assert summary["shared_equity"] is True

    def test_overlapping_trades_sum_open_exposure(self):
        from runner.engines._shared import replay_shared_equity

        t1 = _replay_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-05T00:00:00Z",
                           100.0, 100.0, 100.0, 100_000.0)
        t2 = _replay_trade(2, "B", "2024-01-02T00:00:00Z", "2024-01-06T00:00:00Z",
                           100.0, 100.0, 100.0, 100_000.0)
        replayed, _, summary = replay_shared_equity([t1, t2], 100_000.0)
        # t2 enters while t1 is still open: one open position recorded.
        by_id = {t.id: t for t in replayed}
        assert by_id[2].meta["portfolio"]["open_positions"] == 1
        assert summary["peak_open_notional_usd"] == pytest.approx(20_000.0)

    def test_replay_scales_fees_with_quantity(self):
        from runner.engines._shared import replay_shared_equity
        from shared.models import Trade, TradeExit

        def _fee_trade(trade_id, symbol, entry, exit, qty, fees):
            pnl = (110.0 - 100.0) * qty
            return Trade(
                id=trade_id,
                symbol=symbol,
                direction="LONG",
                entry_time=entry,
                entry_price=100.0,
                quantity=qty,
                exits=[TradeExit(time=exit, price=110.0, quantity=qty, pnl=pnl)],
                pnl=pnl,
                fees=fees,
                net_pnl=pnl - fees,
                meta={"risk_math": {"equity_at_entry": 100_000.0}},
            )

        # Sequential: t2 resizes by 101/100 on shared equity — fees are
        # per-share costs and must scale with the quantity.
        t1 = _fee_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
                        100.0, 50.0)
        t2 = _fee_trade(2, "B", "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z",
                        100.0, 50.0)
        replayed, _, _ = replay_shared_equity([t1, t2], 100_000.0)
        by_id = {t.id: t for t in replayed}
        assert by_id[1].fees == pytest.approx(50.0)
        # t1 nets +950, so t2 rescales by 100950/100000 — fees scale with it.
        assert by_id[2].quantity == pytest.approx(100.0 * 1.0095)
        assert by_id[2].fees == pytest.approx(50.0 * 1.0095, abs=0.01)
        assert by_id[2].net_pnl == pytest.approx(by_id[2].pnl - by_id[2].fees)

    def test_replay_processes_entries_in_entry_order(self):
        from runner.engines._shared import replay_shared_equity

        # D exits (t1.5, +1000) before B enters (t2) but AFTER A enters
        # (t1): entry-order replay sizes A from the still-unreleased shared
        # equity. Exit-order processing would release D's profit first and
        # over-size A.
        d = _replay_trade(3, "D", "2024-01-01T00:30:00Z", "2024-01-01T01:30:00Z",
                          100.0, 101.0, 1000.0, 100_000.0)
        a = _replay_trade(1, "A", "2024-01-01T01:00:00Z", "2024-01-05T00:00:00Z",
                          100.0, 100.0, 100.0, 100_000.0)
        b = _replay_trade(2, "B", "2024-01-01T02:00:00Z", "2024-01-03T00:00:00Z",
                          100.0, 100.0, 100.0, 100_000.0)
        replayed, _, _ = replay_shared_equity([a, b, d], 100_000.0)
        by_id = {t.id: t for t in replayed}
        assert by_id[1].quantity == pytest.approx(100.0)


class TestExitOrderContractHelpers:
    """Unit tests for the shared ``exit_order_type`` parsing helpers."""

    def test_usable_exit_price_rejects_overflow(self):
        from runner.engines import contract

        assert contract.usable_exit_price(10**400) is None
        assert contract.usable_exit_price(float("inf")) is None
        assert contract.usable_exit_price("105.5") == pytest.approx(105.5)
        assert contract.usable_exit_price(-1.0) is None
        assert contract.usable_exit_price(None) is None

    def test_exit_level_disposition_classifies_range(self):
        from runner.engines import contract

        high, low = 107.25, 106.75
        c = contract.exit_level_disposition

        def d(direction: str, level: float) -> str:
            return c(direction=direction, level=level, high=high, low=low)

        # Inside the traded range: the resting order fills at the level.
        assert d("LONG", 107.0) == contract.EXIT_LEVEL_IN_RANGE
        assert d("SHORT", 107.0) == contract.EXIT_LEVEL_IN_RANGE
        # Marketable side (LONG below the low, SHORT above the high): the
        # order was marketable at the open -> gap-through-level open fill.
        assert d("LONG", 105.0) == contract.EXIT_LEVEL_GAPPED
        assert d("SHORT", 500.0) == contract.EXIT_LEVEL_GAPPED
        # Untraded side (LONG above the high, SHORT below the low): the
        # order could never have filled -> market fallback, flagged.
        assert d("LONG", 500.0) == contract.EXIT_LEVEL_UNREACHABLE
        assert d("SHORT", 50.0) == contract.EXIT_LEVEL_UNREACHABLE
        # Range edges are in-range, not gapped.
        assert d("LONG", low) == contract.EXIT_LEVEL_IN_RANGE
        assert d("SHORT", high) == contract.EXIT_LEVEL_IN_RANGE
        # Missing/non-finite extremes fail closed.
        assert (
            c(direction="LONG", level=107.0, high=None, low=low)
            == contract.EXIT_LEVEL_UNREACHABLE
        )
        assert (
            c(direction="SHORT", level=107.0, high=high, low=float("nan"))
            == contract.EXIT_LEVEL_UNREACHABLE
        )
        # An inverted range is not a traded range: fail closed too, never
        # classify it as a gap (which would fill at the open).
        assert (
            c(direction="LONG", level=107.0, high=low, low=high)
            == contract.EXIT_LEVEL_UNREACHABLE
        )
        assert (
            c(direction="SHORT", level=500.0, high=low, low=high)
            == contract.EXIT_LEVEL_UNREACHABLE
        )
        with pytest.raises(ValueError, match="direction"):
            c(direction="SIDEWAYS", level=107.0, high=high, low=low)

"""Vectorbt ↔ native fill-parity regressions (P2 must-fix A-H4, D3).

Mirrors the native semantics pinned in
``tests/test_native_engine_execution.py`` through the VectorbtAdapter:

- spread-aware market fills (LONG lifts the ask, SHORT hits the bid),
- ``signal_exit_fill`` open vs close (SHORT open-exits pay the spread),
- ``warmup_start`` bars feed indicators but create no trades,
- ask-side SHORT stop/TP detection in the intrabar walk (unit seam).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import (
    VectorbtAdapter,
    _match_intrabar_exit_event,
    _resolve_intrabar_exit,
    _vbt_ask_bar,
    _vbt_bar_spread,
)
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_shared_ohlcv


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 01:00", "2024-01-01 02:00"],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
            "volume": [1000.0, 1000.0, 1000.0],
            "spread_price": [0.2, 0.2, 0.2],
        }
    )


def _strategy_text(direction: str) -> str:
    return (
        "import pandas as pd\n"
        "STRATEGY_META = {'name': 'vbt_parity', 'version': '1.0'}\n"
        "def generate_signals(df, params):\n"
        "    n = len(df)\n"
        "    entries = pd.Series([i == 0 for i in range(n)], index=df.index)\n"
        "    exits = pd.Series([i == n - 1 for i in range(n)], index=df.index)\n"
        f"    direction = pd.Series(['{direction}'] + [None]*(n-1), index=df.index, dtype=object)\n"
        "    return entries, exits, {'direction': direction}\n"
    )


def _strategy_with_stop(direction: str, stop: float) -> str:
    return (
        "import pandas as pd\n"
        "STRATEGY_META = {'name': 'vbt_parity_stop', 'version': '1.0'}\n"
        "def generate_signals(df, params):\n"
        "    n = len(df)\n"
        "    entries = pd.Series([i == 0 for i in range(n)], index=df.index)\n"
        "    exits = pd.Series([i == n - 1 for i in range(n)], index=df.index)\n"
        f"    direction = pd.Series(['{direction}'] + [None]*(n-1), index=df.index, dtype=object)\n"
        f"    sl = pd.Series([{stop}, float('nan'), float('nan')], index=df.index)\n"
        "    return entries, exits, {'direction': direction, 'sl_price': sl}\n"
    )


@pytest.fixture
def parity_env(tmp_path: Path):
    _write_shared_ohlcv(tmp_path, "VBP", _frame())
    return tmp_path


def _config(tmp_path: Path, strategy_file: str, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "vbt_parity",
        "symbols": ["VBP"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000,
        # C1: engines size zero times without explicit sizing.
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {"spread_price_col": "spread_price"},
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


def _write_strategy(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / f"{name}.py"
    path.write_text(text)
    return str(path)


class TestSpreadAwareFills:
    def test_market_long_pays_spread(self, parity_env):
        path = _write_strategy(parity_env, "s_long", _strategy_text("LONG"))
        result = VectorbtAdapter().run(_config(parity_env, path), path)
        assert len(result.trades) == 1
        # Entry signal bar 0 executes next-open (bar 1 open + spread).
        assert result.trades[0].entry_price == pytest.approx(101.0 + 0.2)

    def test_market_short_hits_bid(self, parity_env):
        path = _write_strategy(parity_env, "s_short", _strategy_text("SHORT"))
        result = VectorbtAdapter().run(_config(parity_env, path), path)
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(101.0)
        # SHORT signal exit fills at the (next) bar open + spread (ask).
        assert trade.exits[0].price == pytest.approx(102.0 + 0.2)


class TestSignalExitFill:
    def test_signal_exit_fill_close_is_default(self, parity_env):
        path = _write_strategy(parity_env, "s_c", _strategy_text("LONG"))
        result = VectorbtAdapter().run(_config(parity_env, path), path)
        assert result.trades[0].exits[0].price == pytest.approx(102.2)

    def test_signal_exit_fill_open_is_rejected(self, parity_env):
        # Issue #153: the lookahead-prone "open" mode is removed.
        path = _write_strategy(parity_env, "s_o", _strategy_text("LONG"))
        with pytest.raises(ValueError, match="removed.*next_open"):
            _config(parity_env, path, signal_exit_fill="open")

    def test_vectorbt_engine_rejects_open_even_if_config_bypassed_validation(
        self, parity_env
    ):
        path = _write_strategy(parity_env, "s_o2", _strategy_text("LONG"))
        config = _config(parity_env, path)
        object.__setattr__(config, "signal_exit_fill", "open")
        # The adapter wraps per-symbol failures in a RuntimeError; the cause text stays.
        with pytest.raises((ValueError, RuntimeError), match="removed.*next_open"):
            VectorbtAdapter().run(config, path)


class TestWarmupGate:
    def test_warmup_bars_create_no_trades(self, parity_env):
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2024-01-01 00:00",
                        "2024-01-01 01:00",
                        "2024-01-01 02:00",
                        "2024-01-01 03:00",
                        "2024-01-01 04:00",
                    ],
                    utc=True,
                ),
                "open": [100.0, 101.0, 102.0, 103.0, 104.0],
                "high": [100.5, 101.5, 102.5, 103.5, 104.5],
                "low": [99.5, 100.5, 101.5, 102.5, 103.5],
                "close": [100.2, 101.2, 102.2, 103.2, 104.2],
                "volume": [1000.0] * 5,
            }
        )
        _write_shared_ohlcv(parity_env, "VBW", frame)
        # Signal entries [T,F,T,F,F] execute next-open at bars 1,3; the
        # bar-1 execution falls in warmup and is gated. Signal exit on the
        # last bar stays on the last bar, closing the bar-3 execution entry.
        text = (
            "import pandas as pd\n"
            "STRATEGY_META = {'name': 'vbt_warmup', 'version': '1.0'}\n"
            "def generate_signals(df, params):\n"
            "    entries = pd.Series([True, False, True, False, False], index=df.index)\n"
            "    exits = pd.Series([False, False, False, False, True], index=df.index)\n"
            "    return entries, exits, {'direction': pd.Series('LONG', index=df.index)}\n"
        )
        path = parity_env / "warmup_strat.py"
        path.write_text(text)
        config = BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero"},
            strategy_name="vbt_warmup",
            symbols=["VBW"],
            timeframe="1h",
            start_date="2024-01-01 02:00",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            warmup_start="2024-01-01 00:00",
            data_dir=str(parity_env),
        )
        result = VectorbtAdapter().run(config, str(path))
        # Only the post-warmup entry/exit pair survives (entry-execution
        # bar 3 -> exit bar 4); the warmup execution entry at bar 1 is
        # gated, so without the gate the trade would start at 01:00.
        assert len(result.trades) == 1
        assert result.trades[0].entry_time.startswith("2024-01-01 03:00")


class TestAskSideStops:
    def test_ask_bar_disabled_by_default(self):
        row = pd.Series({"high": 100.5, "low": 99.5})
        assert _vbt_ask_bar(row, {}) == (None, None)
        assert _vbt_bar_spread(row, {}) == 0.0

    def test_ask_bar_reads_columns(self):
        row = pd.Series(
            {"high": 100.5, "low": 99.5, "ask_high": 101.5, "ask_low": 99.5}
        )
        assert _vbt_ask_bar(row, {"ask_ohlc": True}) == (101.5, 99.5)

    def test_short_stop_triggers_on_ask_in_intrabar_walk(self):
        dates = pd.date_range("2024-01-01", periods=4, freq="30min", tz="UTC")
        intrabar = pd.DataFrame(
            {
                "timestamp": dates,
                "open": [100.0] * 4,
                "high": [100.5] * 4,
                "low": [99.5] * 4,
                "close": [100.0] * 4,
                "ask_high": [100.5, 100.5, 101.5, 101.5],
                "ask_low": [99.5] * 4,
            }
        )
        # Bid-only: no touch at stop 101.0.
        assert (
            _resolve_intrabar_exit(
                intrabar_df=intrabar,
                intrabar_timeframe="30m",
                base_timeframe="1h",
                direction="SHORT",
                entry_time="2024-01-01T00:00:00+00:00",
                scheduled_exit_time="2024-01-01T02:00:00+00:00",
                stop_price=101.0,
                take_profit_price=None,
                parameters={},
            )
            is None
        )
        # Ask-enabled: ask-high 101.5 breaches the SHORT stop.
        hit = _resolve_intrabar_exit(
            intrabar_df=intrabar,
            intrabar_timeframe="30m",
            base_timeframe="1h",
            direction="SHORT",
            entry_time="2024-01-01T00:00:00+00:00",
            scheduled_exit_time="2024-01-01T02:00:00+00:00",
            stop_price=101.0,
            take_profit_price=None,
            parameters={"ask_ohlc": True},
        )
        assert hit is not None
        assert hit["reason"] == "stop_loss"
        assert hit["price"] == pytest.approx(101.0)

    def test_match_event_stop_first_tiebreak(self):
        event = _match_intrabar_exit_event(
            direction="LONG", high=110.0, low=90.0, stop_price=95.0,
            take_profit_price=105.0,
        )
        assert event is not None
        assert event["reason"] == "stop_loss"

    def test_trigger_bar_intrabar_stop_is_inside_window(self):
        """D4 (A-M1): the exit leg fires on the trigger bar, so its own
        sub-bars sit inside the intrabar window — a stop touched intrabar on
        the trigger bar fills at the level (native parity), not at the
        trigger close."""
        # Hourly frame: entry bar 08:00, trigger bar 09:00. The stop is hit
        # ONLY inside the trigger bar's 09:15 sub-bar.
        sub = pd.date_range("2024-01-01 08:00", periods=8, freq="15min", tz="UTC")
        intrabar = pd.DataFrame(
            {
                "timestamp": sub,
                "open": [100.0] * 8,
                "high": [100.5] * 8,
                "low": [100.0, 100.0, 100.0, 100.0, 100.0, 98.8, 100.0, 100.0],
                "close": [100.0] * 8,
            }
        )
        hit = _resolve_intrabar_exit(
            intrabar_df=intrabar,
            intrabar_timeframe="15min",
            base_timeframe="1h",
            direction="LONG",
            entry_time="2024-01-01T08:00:00+00:00",
            scheduled_exit_time="2024-01-01T09:00:00+00:00",
            stop_price=99.0,
            take_profit_price=None,
        )
        assert hit is not None, (
            "Trigger-bar intrabar stop must fill at the level (native parity)"
        )
        assert hit["reason"] == "stop_loss"
        assert hit["price"] == pytest.approx(99.0)
        assert hit["time"] == "2024-01-01T09:15:00+00:00"

    def test_next_bar_intrabar_stop_stays_outside_window(self):
        """The window ends at the trigger bar's close: a stop touched only in
        the NEXT bar's sub-bars must NOT fire (the scheduled exit already
        ended at the trigger bar)."""
        sub = pd.date_range("2024-01-01 08:00", periods=8, freq="15min", tz="UTC")
        intrabar = pd.DataFrame(
            {
                "timestamp": sub,
                "open": [100.0] * 8,
                "high": [100.5] * 8,
                "low": [100.0] * 7 + [98.8],  # only the 10:45 sub-bar? no: 8 bars end 09:45
                "close": [100.0] * 8,
            }
        )
        # 8 x 15min from 08:00 ends at 09:45 — inside the trigger bar. For a
        # next-bar-only hit, extend past the trigger bar's close (10:00+).
        sub2 = pd.date_range("2024-01-01 08:00", periods=12, freq="15min", tz="UTC")
        intrabar2 = pd.DataFrame(
            {
                "timestamp": sub2,
                "open": [100.0] * 12,
                "high": [100.5] * 12,
                "low": [100.0] * 11 + [98.8],  # only 10:45 sub-bar (next bar)
                "close": [100.0] * 12,
            }
        )
        hit = _resolve_intrabar_exit(
            intrabar_df=intrabar2,
            intrabar_timeframe="15min",
            base_timeframe="1h",
            direction="LONG",
            entry_time="2024-01-01T08:00:00+00:00",
            scheduled_exit_time="2024-01-01T09:00:00+00:00",
            stop_price=99.0,
            take_profit_price=None,
        )
        assert hit is None, "Next-bar intrabar touch must stay outside the window"
        assert intrabar2["low"].min() < 99.0  # sanity: the fixture does contain a touch


class TestFeeModelParity:
    def test_explicit_fees_reduce_net_pnl_on_both_engines(self, parity_env):
        # A-L1: fee_rate (fraction of notional per side) + fee_flat_per_trade
        # must come out of net_pnl in BOTH engines, not stay implicit 0.0.
        fee_params = {
            "spread_price_col": "spread_price",
            "fee_rate": 0.01,
            "fee_flat_per_trade": 3.0,
        }
        path = _write_strategy(parity_env, "s_fee", _strategy_text("LONG"))
        config = _config(parity_env, path, parameters=fee_params)
        for adapter in (VectorbtAdapter(), NativeAdapter()):
            result = adapter.run(config, path)
            assert len(result.trades) == 1
            trade = result.trades[0]
            qty = trade.quantity
            expected = (
                trade.entry_price * qty + trade.exits[0].price * qty
            ) * 0.01 + 3.0
            assert trade.fees > 0
            assert trade.fees == pytest.approx(expected, abs=0.02)
            assert trade.net_pnl == pytest.approx(trade.pnl - trade.fees, abs=0.02)


class TestExplicitColumnRoles:
    """C2: strategy-declared column roles beat exit_* name guessing."""

    def test_exit_named_entry_column_shifts_when_declared(self) -> None:
        import pandas as pd

        from runner.engines.vectorbt_adapter import _prepare_next_open_execution_data

        index = pd.RangeIndex(4)
        signals = pd.DataFrame(
            {
                "entries": [True, False, False, False],
                "exit_signal": [10.0, 11.0, 12.0, 13.0],
            },
            index=index,
        )
        # Legacy same-bar exit leg ("close"): the exit_* name guess stays
        # unshifted. (Owner decision 2026-11-13: the default is next_open, where
        # exit-role columns shift like the entry leg.)
        exec_default, _ = _prepare_next_open_execution_data(
            signals, {}, signal_exit_fill="close"
        )
        assert exec_default["exit_signal"].tolist()[1:2] == [11.0]
        exec_next_open, _ = _prepare_next_open_execution_data(signals, {})
        assert exec_next_open["exit_signal"].tolist()[1:2] == [10.0]
        exec_declared, _ = _prepare_next_open_execution_data(
            signals, {}, {"exit_signal": "entry"}
        )
        # Declared entry: shifted like the entry leg.
        assert pd.isna(exec_declared["exit_signal"].iloc[0])
        assert exec_declared["exit_signal"].tolist()[1:2] == [10.0]

    def test_explicit_indicator_when_wins_over_name(self) -> None:
        from runner.engines.vectorbt_adapter import _infer_indicator_when

        assert _infer_indicator_when("exit_trigger", "entry") == "entry"
        assert _infer_indicator_when("plain", "exit") == "exit"
        assert _infer_indicator_when("exit_trigger", "") == "exit"
        assert _infer_indicator_when("plain", "") == "entry"

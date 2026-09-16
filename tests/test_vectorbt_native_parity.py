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
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {"spread_price_col": "spread_price"},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


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
        result = VectorbtAdapter().run(
            _config(parity_env, path, signal_exit_fill="open"), path
        )
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(101.0)
        # SHORT signal exit fills at open + spread (ask).
        assert trade.exits[0].price == pytest.approx(102.0 + 0.2)


class TestSignalExitFill:
    def test_signal_exit_fill_close_is_default(self, parity_env):
        path = _write_strategy(parity_env, "s_c", _strategy_text("LONG"))
        result = VectorbtAdapter().run(_config(parity_env, path), path)
        assert result.trades[0].exits[0].price == pytest.approx(102.2)

    def test_signal_exit_fill_open_uses_bar_open(self, parity_env):
        path = _write_strategy(parity_env, "s_o", _strategy_text("LONG"))
        result = VectorbtAdapter().run(
            _config(parity_env, path, signal_exit_fill="open"), path
        )
        assert result.trades[0].exits[0].price == pytest.approx(102.0)


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
        config = BacktestConfig(
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

"""Adapter-level curve semantics regressions (Cleanup 16-D1 hardening).

- native single-symbol curves are per-bar mark-to-market and must reconcile
  final equity with realized net PnL.
- multi-symbol curves replay portfolio PnL at exit time.
- ``meta.total_bars`` carries the loaded bar count, not the trade count.
- trade renumbering stays deterministic (entry-time sort before ids).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from tests.helpers import deterministic_ohlcv_frame, sma_crossover_strategy_text

STRATEGY = sma_crossover_strategy_text("curve_sma")


def _frame(*, periods: int = 300) -> pd.DataFrame:
    return deterministic_ohlcv_frame(periods=periods)


@pytest.fixture
def curve_env(tmp_path: Path):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    strategy_path = strategy_dir / "curve_sma.py"
    strategy_path.write_text(STRATEGY)
    symbol_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "CRV"
    symbol_dir.mkdir(parents=True)
    _frame().to_parquet(symbol_dir / "1h.parquet", index=False)
    config = BacktestConfig(
        strategy_name="curve_sma",
        symbols=["CRV"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000,
        modeling_resolution="main_timeframe",
        data_dir=str(tmp_path),
        parameters={"fast_length": 3, "slow_length": 10},
    )
    return config, strategy_path


class TestNativeCurveSemantics:
    def test_curve_is_mark_to_market_and_reconciles_final_pnl(self, curve_env):
        config, strategy_path = curve_env
        result = NativeAdapter().run(config, str(strategy_path))
        assert result.trades, "the fixture must produce trades"
        assert result.equity_curve, "the fixture must produce an equity curve"

        expected_final = config.initial_capital + sum(
            t.net_pnl if t.net_pnl is not None else t.pnl for t in result.trades
        )
        assert len(result.equity_curve) >= result.meta.total_bars
        assert result.equity_curve[0].equity == pytest.approx(config.initial_capital)
        assert result.equity_curve[-1].equity == pytest.approx(round(expected_final, 2))

        timestamps = [pd.Timestamp(point.time) for point in result.equity_curve]
        assert timestamps == sorted(timestamps)
        assert set(timestamps) != {pd.Timestamp(t.last_exit_time) for t in result.trades}

    def test_meta_total_bars_is_bar_count(self, curve_env):
        config, strategy_path = curve_env
        result = NativeAdapter().run(config, str(strategy_path))
        assert result.meta.total_bars == 300
        assert result.meta.total_bars != len(result.trades)

    def test_trade_ids_renumbered_after_entry_time_sort(self, curve_env):
        config, strategy_path = curve_env
        result = NativeAdapter().run(config, str(strategy_path))
        ids = [trade.id for trade in result.trades]
        assert ids == sorted(ids)
        assert ids == list(range(1, len(result.trades) + 1))
        entries = [trade.entry_time for trade in result.trades]
        assert entries == sorted(entries)


def _overlap_frame() -> pd.DataFrame:
    # Rising series. Entries on bars 0+1 fill bars 1+2; A carries a far TP
    # (110, never hit), B a near TP (103, hit on bar 3). A exits on the bar
    # 5 signal. Entry order is A,B but exit order is B,A.
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
            "low": [99.5, 100.5, 101.5, 102.5, 103.5, 104.5],
            "close": [100.2, 101.2, 102.2, 103.2, 104.2, 105.2],
            "volume": [1000.0] * 6,
        }
    )


_OVERLAP_STRATEGY = """
import pandas as pd

STRATEGY_META = {"name": "overlap_curve", "version": "1.0"}

def generate_signals(df, params):
    n = len(df)
    entries = pd.Series([True, True, False, False, False, False], index=df.index)
    exits = pd.Series([False, False, False, False, False, True], index=df.index)
    indicators = {
        "direction": pd.Series("LONG", index=df.index),
        "take_profit": pd.Series([110.0, 103.0, float("nan"), float("nan"),
                                  float("nan"), float("nan")], index=df.index),
    }
    return entries, exits, indicators, []
"""


class TestNativeCurveExitOrder:
    def test_overlapping_trades_keep_mark_to_market_curve(self, tmp_path: Path):
        (tmp_path / "strategies").mkdir()
        strategy_path = tmp_path / "strategies" / "overlap_curve.py"
        strategy_path.write_text(_OVERLAP_STRATEGY)
        symbol_dir = tmp_path / "ohlcv" / "test_source" / "equities" / "OVL"
        symbol_dir.mkdir(parents=True)
        _overlap_frame().to_parquet(symbol_dir / "1h.parquet", index=False)
        config = BacktestConfig(
            strategy_name="overlap_curve",
            symbols=["OVL"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
        )
        result = NativeAdapter().run(config, str(strategy_path))
        assert len(result.trades) == 2
        assert len(result.equity_curve) > len(result.trades)
        stamps = [pd.Timestamp(p.time) for p in result.equity_curve]
        assert stamps == sorted(stamps)
        expected = config.initial_capital + sum(t.pnl for t in result.trades)
        assert result.equity_curve[-1].equity == pytest.approx(round(expected, 2))

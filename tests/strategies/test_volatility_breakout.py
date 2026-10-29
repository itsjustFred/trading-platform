"""
Regression-Test fuer Strategie: Volatility Breakout

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-03-08
Symbol/Timeframe: US500 / 1h
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "volatility_breakout" / "volatility_breakout.py")
# Window retargeted: the old window (2025-01-01..2025-03-31) predates the local
# US500/1h dataset (starts 2025-04-01) and is uncovered everywhere locally.
# Start 2025-04-02 avoids the data-gate 4h edge on 04-01 (dataset starts 04:00).
TEST_START = "2025-04-02"
TEST_END = "2025-06-30"
TEST_SYMBOL = "US500"
TEST_TIMEFRAME = "1h"
# Re-baselined 2026-09-09 for the source-faithful rewrite (daily ATR(5)[1],
# 0.3 offset, stop at session open, 8:30-15:00 NY + 60-min force-close, no
# regime filter). Old values (9 / -6122.70) encoded the invented
# ATR-regime filter and MT5-style 16:30/18:05/22:55 clock.
EXPECTED_TRADE_COUNT = 46
EXPECTED_NET_PROFIT = 38.85


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="volatility_breakout",
        symbols=[TEST_SYMBOL],
        timeframe=TEST_TIMEFRAME,
        start_date=TEST_START,
        end_date=TEST_END,
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
    )
    return engine.run(config, STRATEGY_PATH)


class TestStrategyRegression:
    def test_trade_count(self, backtest_result):
        assert len(backtest_result.trades) == EXPECTED_TRADE_COUNT, (
            f"Erwartet {EXPECTED_TRADE_COUNT} Trades, erhalten {len(backtest_result.trades)}"
        )

    def test_net_profit(self, backtest_result):
        actual = sum(t.pnl for t in backtest_result.trades)
        assert math.isclose(actual, EXPECTED_NET_PROFIT, abs_tol=0.50), (
            f"Net Profit: erwartet {EXPECTED_NET_PROFIT:.2f}, erhalten {actual:.2f}"
        )

    def test_all_trades_have_sl_in_meta(self, backtest_result):
        for trade in backtest_result.trades:
            assert "sl_price" in trade.meta, (
                f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            )

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        equity = 100_000.0
        for trade in backtest_result.trades:
            sl = trade.meta.get("sl_price")
            assert sl is not None, f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            stop_dist = abs(trade.entry_price - sl)
            risk_usd = stop_dist * trade.quantity
            risk_pct = risk_usd / equity
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct*100:.2f}% ueberschreitet Limit von 1.5%"
            )
            equity += trade.pnl


def test_warmup_is_day_based_not_bar_counted():
    """B-M4: kein zusaetzliches i<20-Bar-Gate auf Intraday-Daten.

    10 Tages-Bars (15:00 UTC = 10:00 ET, in Session): der Entry auf
    Bar 8 (Tages-Index 8 >= 5, aber Bar-Index 8 < 20) muss feuern —
    das alte Doppel-Gate hat ihn geschluckt.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "volatility_breakout_unit", STRATEGY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    stamps = pd.date_range("2024-01-08", periods=10, freq="D", tz="UTC") + pd.Timedelta(
        hours=15
    )
    base = 100.0 + np.arange(10) * 0.3
    close = base.copy()
    close[-2] = base[-2] + 3.0
    close[-1] = base[-1] + 3.5
    df = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": base,
            "high": np.maximum(base, close) + 0.5,
            "low": np.minimum(base, close) - 0.5,
            "close": close,
            "volume": 100.0,
        }
    )
    entries, _, indicators, _ = module.generate_signals(df, {})
    entry_idx = entries[entries].index.tolist()
    assert len(entry_idx) >= 1, "Day-Warmup (5 Tage) reicht, kein Bar-Count-Gate"
    assert all(i < 20 for i in entry_idx)
    assert set(indicators["direction"].loc[entry_idx]) <= {"LONG", "SHORT"}

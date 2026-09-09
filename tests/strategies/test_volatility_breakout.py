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
    config = BacktestConfig(
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

"""
Regression-Test fuer Strategie: Short Term Reversal

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-09-09
Symbol/Timeframe: AAPL / 1d

Historie: Erwartungen am 2026-09-09 neu kalibriert (alter Profit 2441.70
mit erfundenem RSI14<35-Filter, ATR-Stop und 10-Bar-Hold; Quelle: kein
RSI, fixer 8-%-Stop, 5-Bar-Hold, +5-%-Target).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "short_term_reversal" / "short_term_reversal.py")
TEST_START = "2025-07-01"
TEST_END = "2025-09-30"
TEST_SYMBOL = "AAPL"
TEST_TIMEFRAME = "1d"
EXPECTED_TRADE_COUNT = 1
EXPECTED_NET_PROFIT = 477.06


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(
        strategy_name="short_term_reversal",
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


def _load_strategy():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "str", str(Path(STRATEGY_PATH))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSourceFidelity:
    def test_no_rsi_no_atr_fixed_stop_and_hold_5(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        # Konstruierte Bahn: Crash an Tag 50 (-6 %), dann Seitwaertsdrift.
        n = 120
        close = pd.Series(np.linspace(100, 101, n))
        close.iloc[50] = 94.0
        df = pd.DataFrame(
            {
                "close": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1_000_000.0,
            }
        )
        entries, exits, ind, _ = mod.generate_signals(df, {})
        assert "RSI" not in ind, "Quelle hat keinen RSI"
        assert "ATR" not in ind, "Quelle hat keinen ATR"
        entry_idx = entries[entries].index
        assert len(entry_idx) >= 1
        first = entry_idx[0]
        # Fixer 8-%-Stop ab Entry-Close.
        assert ind["sl_price"].iloc[first] == (
            __import__("pytest").approx(close.iloc[first] * 0.92)
        )
        # Time-Exit spaetestens 5 Bars nach Entry (kein Profit/Stop davor:
        # Bahn driftet nur +~0.01/Bar).
        exit_idx = exits[exits].index
        assert len(exit_idx) >= 1
        assert exit_idx[0] - first <= 5
        assert ind["bars_held_at_exit"].iloc[exit_idx[0]] <= 5

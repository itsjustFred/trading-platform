"""
Regression-Test fuer Strategie: Statistical Arbitrage Pairs

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen Zeitraum mit genuegend Warmup.

Validiert am: 2026-09-09
Symbol/Timeframe: AAPL / 1d

Historie: Erwartungen am 2026-09-09 neu kalibriert (11 Trades / 4006.10
mit erfundenem ATR-Stop erzeugt, der 4 Trades frueh stoppte; Quelle:
nur z-Score-Exits, kein ATR-Stop — jetzt 7 Trades, alle
mean_reversion).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "stat_arb_pairs" / "stat_arb_pairs.py")
TEST_START = "2021-01-01"
TEST_END = "2024-12-31"
TEST_SYMBOL = "AAPL"
TEST_TIMEFRAME = "1d"
EXPECTED_TRADE_COUNT = 7
EXPECTED_NET_PROFIT = 12224.60


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="stat_arb_pairs",
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

    def test_trade_meta_contains_core_indicator(self, backtest_result):
        for trade in backtest_result.trades:
            assert "ZScore" in trade.meta, f"Trade {trade.id}: 'ZScore' fehlt in trade.meta"
            # sl_price gibt es im Standard NICHT mehr (erfundener ATR-Stop
            # entfernt; nur mit use_atr_stop=true vorhanden).

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        equity = 100_000.0
        for trade in backtest_result.trades:
            risk_math = trade.meta.get("risk_math", {})
            stop_distance = risk_math.get("stop_distance")
            assert stop_distance is not None and stop_distance > 0, (
                f"Trade {trade.id}: keine Stop-Distanz in risk_math"
            )
            risk_pct = (stop_distance * trade.quantity) / risk_math.get(
                "equity_at_entry", equity
            )
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct*100:.2f}% ueberschreitet Limit von 1.5%"
            )
            equity += trade.pnl


def _load_strategy():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "statarb", str(Path(STRATEGY_PATH))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSourceFidelity:
    def test_no_atr_stop_by_default_opt_in_emits_stop(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        rng = np.random.default_rng(5)
        close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500))))
        df = pd.DataFrame(
            {
                "close": close,
                "high": close * 1.005,
                "low": close * 0.995,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1_000_000.0,
            }
        )
        _, _, ind_default, _ = mod.generate_signals(df, {})
        assert "ATR" not in ind_default
        assert ind_default["sl_price"].dropna().empty
        _, _, ind_stop, _ = mod.generate_signals(df, {"use_atr_stop": True})
        assert "ATR" in ind_stop
        assert not ind_stop["sl_price"].dropna().empty

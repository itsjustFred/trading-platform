"""
Regression-Test fuer Strategie: Sector Rotation

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen Zeitraum mit genuegend Warmup.

Validiert am: 2026-09-09
Symbol/Timeframe: AAPL / 1d

Historie: Erwartungen am 2026-09-09 neu kalibriert (17 Trades / -1576.84
waren mit erfundenem ATR-Stop, Mom63<0-Exit und 10d-Schwaeche-Exit
erzeugt; Standard ist jetzt nur der Trendbruch-Exit, die drei erfundenen
Exits sind Opt-in default-OFF).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "sector_rotation" / "sector_rotation.py")
TEST_START = "2023-01-01"
TEST_END = "2024-12-31"
TEST_SYMBOL = "AAPL"
TEST_TIMEFRAME = "1d"
EXPECTED_TRADE_COUNT = 1
EXPECTED_NET_PROFIT = -11.84


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="sector_rotation",
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
            assert "Momentum_63" in trade.meta, f"Trade {trade.id}: 'Momentum_63' fehlt in trade.meta"
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
        "sector", str(Path(STRATEGY_PATH))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synthetic_df(n=400, start=100.0, drift=0.002):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(11)
    rets = drift + rng.normal(0, 0.01, n)
    close = pd.Series(start * np.exp(np.cumsum(rets)))
    return pd.DataFrame(
        {
            "close": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "open": close.shift(1).fillna(start),
            "volume": 1_000_000.0,
        }
    )


class TestSourceFidelity:
    def test_invented_exits_absent_by_default(self):
        mod = _load_strategy()
        _, _, ind, _ = mod.generate_signals(_synthetic_df(), {})
        reasons = set(ind["exit_reason"].dropna().unique())
        assert reasons <= {"trend_break"}, reasons
        assert ind["sl_price"].dropna().empty

    def test_opt_in_exits_trigger(self):
        mod = _load_strategy()
        df = _synthetic_df()
        _, _, ind, _ = mod.generate_signals(
            df,
            {
                "use_atr_stop": True,
                "use_momentum_exit": True,
                "use_weakness_exit": True,
            },
        )
        assert not ind["sl_price"].dropna().empty
        reasons = set(ind["exit_reason"].dropna().unique())
        assert reasons <= {
            "stop_loss",
            "trend_break",
            "momentum_break",
            "short_weakness",
        }, reasons

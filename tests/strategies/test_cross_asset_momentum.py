"""
Regression-Test fuer Strategie: Cross-Asset Momentum

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen Zeitraum mit genuegend Warmup.

Validiert am: 2026-09-09
Symbol/Timeframe: AAPL / 1d

Historie: Erwartungen am 2026-09-09 neu kalibriert (21 Trades / 1830.69
waren mit erfundenem ATR-Stop, Momentum-Bruch-Exit und Drawdown-Exit
sowie falschen Gewichten [0.4,0.3,0.2,0.1] erzeugt; Quelle: [0.35,0.30,
0.20,0.15], kein ATR-Stop, kein Drawdown-Exit).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "cross_asset_momentum" / "cross_asset_momentum.py")
TEST_START = "2024-01-01"
TEST_END = "2025-12-31"
TEST_SYMBOL = "AAPL"
TEST_TIMEFRAME = "1d"
EXPECTED_TRADE_COUNT = 5
EXPECTED_NET_PROFIT = 230.60


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="cross_asset_momentum",
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
            assert "Composite_Momentum" in trade.meta, (
                f"Trade {trade.id}: 'Composite_Momentum' fehlt in trade.meta"
            )
            # sl_price gibt es im Standard NICHT mehr (erfundener ATR-Stop
            # entfernt; nur mit use_atr_stop=true vorhanden).

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        equity = 100_000.0
        for trade in backtest_result.trades:
            risk_math = trade.meta.get("risk_math", {})
            stop_distance = risk_math.get("stop_distance")
            assert stop_distance is not None, (
                f"Trade {trade.id}: keine Stop-Distanz in risk_math"
            )
            assert stop_distance > 0, (
                f"Trade {trade.id}: Stop-Distanz nicht positiv: {stop_distance}"
            )
            risk_pct = (stop_distance * trade.quantity) / risk_math.get(
                "equity_at_entry", equity
            )
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct*100:.2f}% ueberschreitet Limit von 1.5%"
            )
            equity += trade.pnl


def _synthetic_df(n=400, start=100.0, drift=0.002):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(42)
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
    def test_default_weights_match_source(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "xam", str(Path(STRATEGY_PATH))
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        df = _synthetic_df()
        _, _, ind, _ = mod.generate_signals(df, {})
        comp = ind["Composite_Momentum"]
        i = 350
        expected = (
            0.35 * (df["close"].iloc[i] / df["close"].iloc[i - 21] - 1)
            + 0.30 * (df["close"].iloc[i] / df["close"].iloc[i - 63] - 1)
            + 0.20 * (df["close"].iloc[i] / df["close"].iloc[i - 126] - 1)
            + 0.15 * (df["close"].iloc[i] / df["close"].iloc[i - 252] - 1)
        )
        assert comp.iloc[i] == (
            __import__("pytest").approx(expected, rel=1e-9)
        )

    def test_no_stop_by_default_opt_in_emits_stop(self):
        import importlib.util
        from pathlib import Path

        import pandas as pd

        spec = importlib.util.spec_from_file_location(
            "xam", str(Path(STRATEGY_PATH))
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        df = _synthetic_df()
        _, _, ind_default, _ = mod.generate_signals(df, {})
        assert ind_default["sl_price"].dropna().empty
        _, _, ind_stop, _ = mod.generate_signals(df, {"use_atr_stop": True})
        assert not ind_stop["sl_price"].dropna().empty

    def test_invented_exits_absent_by_default_opt_in_triggers_them(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "xam", str(Path(STRATEGY_PATH))
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        df = _synthetic_df()
        _, _, ind_default, _ = mod.generate_signals(df, {})
        default_reasons = set(ind_default["exit_reason"].dropna().unique())
        assert default_reasons <= {"signal_break"}, default_reasons
        # Opt-in mit unerreichbar hoher Schwelle: jeder Exit ist ein
        # Momentum-Bruch.
        _, _, ind_opt, _ = mod.generate_signals(
            df, {"use_momentum_exit": True, "momentum_exit": 999.0}
        )
        opt_reasons = set(ind_opt["exit_reason"].dropna().unique())
        assert "momentum_break" in opt_reasons, opt_reasons

"""
Regression-Test fuer Strategie: Gold Momentum

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-09-09
Symbol/Timeframe: XAUUSD / 1h (GC=F-Futures als XAUUSD-Proxy)

Historie: Erwartungen am 2026-09-09 neu kalibriert (29 Trades / 15439.32
waren mit erfundenem ATR-Stop, diskreter MACD-Cross/RSI-50/40-Logik und
SMA-geglaettetem RSI/ATR erzeugt; Quelle: kontinuierlicher Score,
Wilder-RSI14, Wilder-ATR14, kein ATR-Stop).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "gold_momentum" / "gold_momentum.py")
TEST_START = "2025-10-01"
TEST_END = "2025-12-31"
TEST_SYMBOL = "XAUUSD"
TEST_TIMEFRAME = "1h"
EXPECTED_TRADE_COUNT = 36
EXPECTED_NET_PROFIT = 2503.90


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(
        strategy_name="gold_momentum",
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
            assert "Momentum_Score" in trade.meta, (
                f"Trade {trade.id}: 'Momentum_Score' fehlt in trade.meta"
            )
            assert "MACD_Line" in trade.meta, (
                f"Trade {trade.id}: 'MACD_Line' fehlt in trade.meta"
            )
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
            # XAUUSD wird teils gecappt (max_position_exposure) -> Risiko
            # darf unter, aber nie ueber dem Limit liegen.
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct*100:.2f}% ueberschreitet Limit von 1.5%"
            )
            equity += trade.pnl


def _load_strategy():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "gold", str(Path(STRATEGY_PATH))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSourceFidelity:
    def test_provenance_is_desenyon(self):
        mod = _load_strategy()
        assert mod.STRATEGY_META["tags"]["source"] == "desenyon-github"
        assert "desenyon" in mod.STRATEGY_META["source_url"]

    def test_rsi_is_wilder_not_sma(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        rng = np.random.default_rng(7)
        close = pd.Series(2000 + np.cumsum(rng.normal(0.5, 8, 300)))
        df = pd.DataFrame(
            {
                "close": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1000.0,
            }
        )
        _, _, ind, _ = mod.generate_signals(df, {})
        rsi = ind["RSI"].dropna()
        # Wilder-Referenz direkt nachgerechnet.
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        ag = gain.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
        al = loss.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
        expected = 100 - (100 / (1 + ag / al.replace(0, np.nan)))
        pd.testing.assert_series_equal(
            rsi, expected.dropna(), check_names=False, rtol=1e-9
        )

    def test_no_stop_by_default_opt_in_emits_stop(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        rng = np.random.default_rng(7)
        close = pd.Series(2000 + np.cumsum(rng.normal(0.5, 8, 300)))
        df = pd.DataFrame(
            {
                "close": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1000.0,
            }
        )
        _, _, ind_default, _ = mod.generate_signals(df, {})
        assert ind_default["sl_price"].dropna().empty
        _, _, ind_stop, _ = mod.generate_signals(df, {"use_atr_stop": True})
        assert not ind_stop["sl_price"].dropna().empty

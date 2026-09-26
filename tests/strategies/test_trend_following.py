"""
Regression-Test fuer Strategie: Trend Following

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-09-09
Symbol/Timeframe: US500 / 1h (ES=F-Futures als US500-Proxy)

Historie: Erwartungen am 2026-09-09 neu kalibriert (2 Trades / 2261.02
mit SMA20/50/200, ATR14 und sofortigem 2.0x-Trailing; Quelle: EMA,
ATR20, 2.5x-Stop mit Trailing erst nach 2.0 ATR Profit).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "trend_following" / "trend_following.py")
TEST_START = "2025-07-01"
TEST_END = "2025-09-30"
TEST_SYMBOL = "US500"
TEST_TIMEFRAME = "1h"
EXPECTED_TRADE_COUNT = 4
# Re-baselined 2026-09-15 for P3a A-M2 (close-triggered trailing exits fill
# at the close, not at the stop level) + D4 (signal exits at trigger close):
# same 4 triggers, fills move from 256.87 to 143.81.
EXPECTED_NET_PROFIT = 143.81


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"}, 
        strategy_name="trend_following",
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
        "tf", str(Path(STRATEGY_PATH))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSourceFidelity:
    def test_ema_not_sma_atr20_wilder_stop_2_5(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        rng = np.random.default_rng(3)
        close = pd.Series(6000 + np.cumsum(rng.normal(0.5, 12, 600)))
        df = pd.DataFrame(
            {
                "close": close,
                "high": close + 6,
                "low": close - 6,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1000.0,
            }
        )
        _, _, ind, _ = mod.generate_signals(df, {})
        assert "EMA_Fast" in ind and "SMA_Fast" not in ind
        pd.testing.assert_series_equal(
            ind["EMA_Fast"].dropna(),
            close.ewm(span=20, adjust=False).mean().dropna(),
            check_names=False,
            rtol=1e-9,
        )
        # ATR20 Wilder-Referenz.
        prev = close.shift(1)
        tr = pd.concat(
            [
                (close + 6) - (close - 6),
                ((close + 6) - prev).abs(),
                ((close - 6) - prev).abs(),
            ],
            axis=1,
        ).max(axis=1)
        expected_atr = tr.ewm(alpha=1.0 / 20, min_periods=20, adjust=False).mean()
        pd.testing.assert_series_equal(
            ind["ATR"].dropna(), expected_atr.dropna(), check_names=False, rtol=1e-9
        )

    def test_trailing_gated_until_2atr_profit(self):
        import numpy as np
        import pandas as pd

        mod = _load_strategy()
        # Etablierter Aufwaertstrend, kurzer Dip (Cross nach unten), dann
        # moderate Rampe (< 2 ATR Profit): Cross-Entry danach, Stop darf
        # sich bis zur 2-ATR-Aktivierung nicht bewegen.
        trend = np.linspace(6000, 6300, 250)
        dip = np.linspace(6300, 6220, 15)
        ramp = np.linspace(6220, 6400, 300)
        close = pd.Series(np.concatenate([trend, dip, ramp]))
        df = pd.DataFrame(
            {
                "close": close,
                "high": close + 5,
                "low": close - 5,
                "open": close.shift(1).fillna(close.iloc[0]),
                "volume": 1000.0,
            }
        )
        entries, _, ind, _ = mod.generate_signals(df, {})
        entry_idx = entries[entries].index
        assert len(entry_idx) >= 1
        first = int(entry_idx[0])
        sl0 = float(ind["sl_price"].iloc[first])
        trail = ind["trailing_stop_at_exit"]
        atr = ind["ATR"]
        entry_px = float(close.iloc[first])
        # Nur echte Erhoehungen pruefen (die Serie wird jeden Bar
        # fortgeschrieben, auch ohne Bewegung).
        prev = sl0
        for idx in range(first + 1, len(df)):
            stop = trail.iloc[idx]
            if pd.isna(stop):
                break  # Position beendet
            assert float(stop) >= prev - 1e-9  # nie nach unten
            if float(stop) > prev + 1e-9:
                profit_atr = (float(close.iloc[idx]) - entry_px) / float(atr.iloc[idx])
                assert profit_atr > 2.0, (idx, profit_atr)
            prev = float(stop)

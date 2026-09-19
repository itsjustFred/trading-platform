"""
Regression-Test fuer Strategie: Post VIX Dip Buy

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-03-08
Symbol/Timeframe: SPY / 1d
"""

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "post_vix_dip_buy" / "post_vix_dip_buy.py")
TEST_START = "2025-01-01"
TEST_END = "2025-03-31"
TEST_SYMBOL = "SPY"
TEST_TIMEFRAME = "1d"
# Re-baselined 2026-09-09 for the source-faithful rewrite (VIX>=19 & chg%>=10 &
# SPX chg%<=-1% panic gate, 1-5 day window, SMA200 bull filter, dip 9:30-10:59
# close<VWAP RSI14<40 Wilder near-prior-low, ORB 9:30-10:00/10:00-14:00,
# TP+2%/SL-0.3%, scale>=14:00, EOD>=15:50). Old values (3 / -1160.43) encoded
# the invented RSI35/SMA20 logic. Without a local VIX/1d.parquet the regime
# gate is FAIL-CLOSED (0 trades); daily bars additionally cannot trigger the
# intraday dip/ORB legs.
EXPECTED_TRADE_COUNT = 0
EXPECTED_NET_PROFIT = 0.00


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(
        strategy_name="post_vix_dip_buy",
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


class TestPanicFixtureProducesTrades:
    """B-L1: der Golden-Test darf nicht vakant sein.

    Der Engine-Golden oben erwartet 0 Trades / 0.00 (VIX-Dataset fehlt
    lokal -> FAIL-CLOSED) und passt damit zu jeder nicht-crashenden
    Implementierung. Diese Klasse baut ein synthetisches Panik-Fenster
    (VIX-Spike + SPX-Drop + Intraday-ORB) und verlangt echte Signale —
    mit kaputter Panik-/Dip-/ORB-Logik faellt sie rot aus.
    """

    N_DAYS = 205
    START = "2023-12-25"  # Montag: Panik-Tag 200 = Fr 2024-07-12
    PANIC_IDX = 200
    INTRADAY_IDX = 203  # Mo 2024-07-15, days_since_panic = 3

    @staticmethod
    def _frame() -> pd.DataFrame:
        n = TestPanicFixtureProducesTrades.N_DAYS
        start = pd.Timestamp(TestPanicFixtureProducesTrades.START, tz="UTC")
        dates = [(start + pd.Timedelta(days=i)).date() for i in range(n)]
        trend = [100.0 + 0.05 * i for i in range(n)]
        # Panik-Freitag: SPX-Tages-Close -2 % (Gate: <= -1 %).
        trend[TestPanicFixtureProducesTrades.PANIC_IDX] = (
            trend[TestPanicFixtureProducesTrades.PANIC_IDX - 1] * 0.98
        )
        rows = []
        for i in range(n):
            if i == TestPanicFixtureProducesTrades.INTRADAY_IDX:
                continue  # Intraday-Bars statt Tages-Bar
            ts = pd.Timestamp(dates[i]) + pd.Timedelta(hours=21)
            rows.append(
                (ts.tz_localize("UTC"), trend[i], trend[i] + 0.5,
                 trend[i] - 0.5, trend[i], 1_000_000.0)
            )
        # Intraday 30m: OR 9:30 (High 110.2), Cross drueber 10:30 -> ORB-Long,
        # danach moderater Drift (kein TP/SL), Exit via Power-Hour-Scale.
        ny = pd.date_range(
            pd.Timestamp("2024-07-15 09:30", tz="America/New_York"),
            periods=13,
            freq="30min",
        )
        closes = [110.0, 110.1, 110.5, 110.6, 110.7, 110.8, 110.9,
                  111.0, 111.1, 111.2, 111.3, 111.4, 111.5]
        for ts, c in zip(ny, closes):
            if ts.hour == 9:
                o, h, low = 110.0, 110.2, 109.8
            else:
                o, h, low = c - 0.05, c + 0.1, c - 0.1
            rows.append((ts.tz_convert("UTC"), o, h, low, c, 1000.0))
        df = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        return df.sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _vix() -> pd.Series:
        n = TestPanicFixtureProducesTrades.N_DAYS
        start = pd.Timestamp(TestPanicFixtureProducesTrades.START, tz="UTC")
        dates = [(start + pd.Timedelta(days=i)).date() for i in range(n)]
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates])
        vix = pd.Series(15.0, index=idx)
        # Panik-Freitag: +66 % auf 25 (Gate: chg% >= 10 & close >= 19).
        vix.iloc[TestPanicFixtureProducesTrades.PANIC_IDX] = 25.0
        vix.iloc[TestPanicFixtureProducesTrades.PANIC_IDX + 1:] = 20.0
        return vix

    @staticmethod
    def _module():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "post_vix_dip_buy_fixture", STRATEGY_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_panic_window_produces_entry_and_exit(self, monkeypatch):
        module = self._module()
        monkeypatch.setattr(
            module, "_load_vix_data", lambda start=None, end=None: self._vix()
        )
        df = self._frame()
        entries, exits, indicators, _ = module.generate_signals(df, {})
        assert int(entries.sum()) >= 1, "Panik-Fixture muss Entries liefern"
        assert int(exits.sum()) >= 1, "Entry muss einen Exit (Scale/EOD) haben"
        entry_idx = entries[entries].index.tolist()
        assert indicators["sl_price"].loc[entry_idx].notna().all()
        reasons = set(indicators["exit_reason"][exits].dropna().tolist())
        assert reasons <= {
            "stop_loss",
            "take_profit",
            "scale_50pct_power_hour",
            "eod_flatten",
        }, f"unerwartete Exit-Reasons: {reasons}"

    def test_fail_closed_without_vix(self, monkeypatch):
        module = self._module()
        monkeypatch.setattr(
            module, "_load_vix_data", lambda start=None, end=None: None
        )
        entries, _, _, _ = module.generate_signals(self._frame(), {})
        assert int(entries.sum()) == 0, "ohne VIX-Daten keine Entries"


class TestBullFilterNoLookahead:
    """A-C1: the SMA200 bull filter uses the PRIOR day's close/SMA200.

    daily_close holds each date's FINAL close (last intraday close wins);
    gating an intraday bar on the same date's close/SMA200 is lookahead.
    """

    def test_sma200_indicator_excludes_current_day_close(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "post_vix_dip_buy_under_test", STRATEGY_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        stamps = pd.date_range("2024-01-01", periods=205, freq="D", tz="UTC")
        rows = []
        for day in stamps:
            for ts in pd.date_range(day, periods=8, freq="h", tz="UTC"):
                rows.append((ts, 100.0))
        # Spike only the final bar: the day's FINAL close jumps to 200.
        rows[-1] = (rows[-1][0], 200.0)
        frame = pd.DataFrame(rows, columns=["timestamp", "close"])
        frame["open"] = 100.0
        frame["high"] = frame["close"].clip(lower=100.0) + 0.1
        frame["low"] = 99.9
        frame["volume"] = 1000.0

        _, _, indicators, _ = module.generate_signals(frame, {})
        sma_last = float(indicators["SMA200"].iloc[-1])
        # Prior-day SMA200 over all-100 closes; with lookahead it would be
        # (199*100+200)/200 = 100.5.
        assert sma_last == pytest.approx(100.0)

    def test_bull_filter_uses_prior_day_close(self, monkeypatch):
        """A-C1 (close leg): prior-day close <= SMA200-prev blocks entries
        even when the traded day's FINAL close rallies above it."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "post_vix_dip_buy_close_leg", STRATEGY_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        days = pd.date_range("2023-12-08", "2024-07-15", freq="D", tz="UTC")
        closes = {d.date(): 100.0 for d in days}
        panic_day = pd.Timestamp("2024-07-12", tz="UTC").date()
        # Panic Friday -2 %, weekend stays weak: prior-day close (Sun)
        # is below SMA200-prev -> bear market, no entries allowed.
        closes[panic_day] = 98.0
        closes[pd.Timestamp("2024-07-13", tz="UTC").date()] = 98.0
        closes[pd.Timestamp("2024-07-14", tz="UTC").date()] = 98.0
        rows = []
        for d in days:
            if d.date() == pd.Timestamp("2024-07-15", tz="UTC").date():
                continue  # intraday bars instead of the daily bar
            ts = pd.Timestamp(d.date()) + pd.Timedelta(hours=21)
            c = closes[d.date()]
            rows.append(
                (ts.tz_localize("UTC"), c, c + 0.5, c - 0.5, c, 1_000_000.0)
            )
        # Mon 2024-07-15 intraday (EDT=UTC-4): OR 100.4/100.2, cross
        # above at 10:30 NY -> ORB trigger; FINAL close 101.4 rallies
        # above SMA200-prev (~99.97), so a same-day-close mutant passes
        # the bull filter while the prior-day close (98.0) must block it.
        ny = pd.date_range(
            pd.Timestamp("2024-07-15 09:30", tz="America/New_York"),
            periods=5,
            freq="30min",
        )
        bars = [
            (100.4, 100.6, 100.2, 100.5),
            (100.5, 100.7, 100.3, 100.5),
            (100.6, 101.5, 100.6, 101.4),
            (101.4, 101.6, 101.2, 101.4),
            (101.4, 101.6, 101.2, 101.4),
        ]
        for ts, (o, h, low, c) in zip(ny, bars):
            rows.append((ts.tz_convert("UTC"), o, h, low, c, 1000.0))
        df = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
        ).sort_values("timestamp").reset_index(drop=True)

        idx = days
        vix = pd.Series(15.0, index=idx)
        vix.loc[pd.Timestamp("2024-07-12", tz="UTC")] = 25.0
        vix.iloc[idx.get_loc(pd.Timestamp("2024-07-12", tz="UTC")) + 1:] = 20.0
        monkeypatch.setattr(
            module, "_load_vix_data", lambda start=None, end=None: vix
        )
        entries, _, _, _ = module.generate_signals(df, {})
        assert int(entries.sum()) == 0, (
            "prior-day close below SMA200-prev must block entries despite "
            "the same-day rally"
        )

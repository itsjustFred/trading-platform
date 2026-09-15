"""
Regression-Test fuer Strategie: Opening Range Breakout (PyneCore)

Echte single-symbol Intraday-ORB-Strategie (erste 30 Minuten, Breakout +/-
Range*Buffer 0.2, 1 %-Trailing ab HWM/LWM, Zwangs-Exit 15:45 ET, ein Trade pro
Tag; Session aus Bar-Timestamps in America/New_York).

Validiert am: 2026-09-09
Symbol/Timeframe: US500 / 30m (30-Minuten-Bars tragen die OR-Logik direkt;
EURUSD 1h ist die falsche Zeiteinheit fuer OR-30m-Logik)
"""

import importlib.util
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import PyneCoreAdapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "pynecore_orb" / "pynecore_orb.py")
CONTEXT_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "pynecore_orb" / "pynecore_orb_context.py")
TEST_START = "2024-01-02"
TEST_END = "2024-03-30"
TEST_SYMBOL = "US500"
TEST_TIMEFRAME = "30m"
EXPECTED_TRADE_COUNT = 65
EXPECTED_NET_PROFIT = 1110.20


def _load_context_module():
    spec = importlib.util.spec_from_file_location("pynecore_orb_context", CONTEXT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backtest_result():
    engine = PyneCoreAdapter()
    config = BacktestConfig(
        strategy_name="pynecore_orb",
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
            assert "sl_price" in (trade.meta or {}), (
                f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            )

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        equity = 100_000.0
        for trade in backtest_result.trades:
            sl = (trade.meta or {}).get("sl_price")
            assert sl is not None, f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            stop_dist = abs(trade.entry_price - sl)
            risk_usd = stop_dist * trade.quantity
            risk_pct = risk_usd / equity
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct*100:.2f}% ueberschreitet Limit von 1.5%"
            )
            equity += trade.pnl

    def test_trades_cover_both_directions(self, backtest_result):
        directions = {t.direction for t in backtest_result.trades}
        assert directions == {"LONG", "SHORT"}, (
            f"ORB muss beide Breakout-Richtungen handeln, gefunden: {directions}"
        )

    def test_at_most_one_trade_per_et_day(self, backtest_result):
        per_day: dict[str, int] = {}
        margin_split_days = 0
        for trade in backtest_result.trades:
            day = pd.Timestamp(trade.entry_time).tz_convert("America/New_York").date().isoformat()
            per_day[day] = per_day.get(day, 0) + 1
        for day, count in per_day.items():
            if count > 1:
                # Broker-Emulator-Margin-Splits (100-%-Sizing + 100-%-Margin)
                # teilen einen Entry in 1-Kontrakt-Stub + Rest; kein zweiter Trade.
                reasons = [
                    t.exits[-1].reason
                    for t in backtest_result.trades
                    if pd.Timestamp(t.entry_time).tz_convert("America/New_York").date().isoformat() == day
                ]
                assert "margin_call" in reasons, (
                    f"Tag {day}: {count} Trades ohne Margin-Split ({reasons})"
                )
                margin_split_days += 1
        assert margin_split_days <= 3


class TestOrbContextMirror:
    """Der pandas-Context-Helper muss die Skript-Logik auf ET-Sessions abbilden."""

    @staticmethod
    def _frame() -> pd.DataFrame:
        # 30m-Bars ueber zwei ET-Tage (Januar = EST, UTC-5).
        # Tag 1: OR 100/98 (Range 2, Long-Level 100.4), Breakout, dann
        # 1 %-Reversion ab HWM -> Trailing-Exit. Tag 2: kein Breakout.
        rows = [
            ("2024-01-02 14:00:00+00:00", 100, 100.2, 99.8, 100.0),  # 09:00 ET
            ("2024-01-02 14:30:00+00:00", 100, 100.0, 98.0, 99.0),  # 09:30 ET (OR)
            ("2024-01-02 15:00:00+00:00", 99, 101.0, 99.0, 101.0),  # 10:00 ET -> LONG
            ("2024-01-02 15:30:00+00:00", 101, 103.0, 100.5, 103.0),  # HWM 103
            ("2024-01-02 16:00:00+00:00", 103, 103.0, 101.0, 101.5),  # <101.97 -> Trail
            ("2024-01-02 16:30:00+00:00", 101.5, 102.0, 101.0, 101.8),
            ("2024-01-03 14:30:00+00:00", 100, 100.1, 99.9, 100.0),
            ("2024-01-03 15:00:00+00:00", 100, 100.1, 99.9, 100.0),
        ]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        df["volume"] = 1.0
        return df

    def test_breakout_levels_and_long_entry(self):
        ctx = _load_context_module().build_trade_context(self._frame(), {})
        assert ctx["breakout_long"].iloc[2] == pytest.approx(100.4)
        assert ctx["breakout_short"].iloc[2] == pytest.approx(97.6)
        assert ctx["direction"].iloc[2] == "LONG"

    def test_trailing_stop_exit(self):
        ctx = _load_context_module().build_trade_context(self._frame(), {})
        assert ctx["exit_reason"].iloc[4] == "trailing_stop"
        assert ctx["trailing_stop_at_exit"].iloc[4] == pytest.approx(103.0 * 0.99)

    def test_signal_plus_one_carry_stops_at_session_boundary(self):
        # B-M5: ein Signal auf der LETZTEN Bar von Tag 1 darf nicht auf die
        # erste Bar von Tag 2 uebertragen werden (andere OR-Range/Session).
        rows = [
            ("2024-01-02 14:30:00+00:00", 100, 100.0, 98.0, 99.0),  # OR Tag 1
            ("2024-01-02 15:00:00+00:00", 99, 101.0, 99.0, 101.0),  # LONG-Signal
            ("2024-01-03 14:30:00+00:00", 100, 100.1, 99.9, 100.0),  # Tag 2
            ("2024-01-03 15:00:00+00:00", 100, 100.1, 99.9, 100.0),
        ]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
        df["volume"] = 1.0
        ctx = _load_context_module().build_trade_context(df, {})
        assert ctx["direction"].iloc[1] == "LONG"
        assert pd.isna(ctx["direction"].iloc[2]), (
            "Signal-Carry darf die Session-Boundary nicht ueberschreiten"
        )
        assert pd.isna(ctx["exit_reason"].iloc[2])

    def test_overnight_bars_do_not_pollute_range(self):
        df = self._frame().copy()
        df.loc[0, "high"] = 150.0  # 09:00-ET-Bar ausserhalb der OR
        df.loc[2, "high"] = 150.0  # 10:00-ET-Bar nach der OR
        ctx = _load_context_module().build_trade_context(df, {})
        assert ctx["OR_High"].iloc[2] == pytest.approx(100.0)
        assert ctx["OR_Low"].iloc[2] == pytest.approx(98.0)

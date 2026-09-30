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
    config = BacktestConfig(parameters={"fees": "zero"},
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
                # Ein Split-Tag muss genau EINE logische Entry-Identitaet haben
                # (gleicher Entry-Timestamp + Richtung) und deren Legs muessen
                # den margin_call tragen — sonst waere es ein echter zweiter Trade.
                day_trades = [
                    t
                    for t in backtest_result.trades
                    if pd.Timestamp(t.entry_time).tz_convert("America/New_York").date().isoformat() == day
                ]
                identities = {
                    (t.entry_time, t.direction) for t in day_trades
                }
                assert len(identities) == 1, (
                    f"Tag {day}: {count} Trades ueber {len(identities)} "
                    f"Entry-Identitaeten ohne Margin-Split ({identities})"
                )
                reasons = [t.exits[-1].reason for t in day_trades]
                assert "margin_call" in reasons, (
                    f"Tag {day}: {count} Trades ohne Margin-Split ({reasons})"
                )
                margin_split_days += 1
        assert margin_split_days <= 3



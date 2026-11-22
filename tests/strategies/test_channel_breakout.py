"""
Regression-Test fuer Strategie: Channel Breakout

Smoke-Test: stellt sicher, dass zukuenftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-03-08
Symbol/Timeframe: US500 / 1h
"""

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "channel_breakout" / "channel_breakout.py")
# Window start moved 2025-04-01 -> 2025-04-02: the local US500/1h dataset starts
# 2025-04-01 04:00 UTC, so the data gate rejects a 00:00 start bound.
TEST_START = "2025-04-02"
TEST_END = "2025-06-30"
TEST_SYMBOL = "US500"
TEST_TIMEFRAME = "1h"
# Re-baselined 2026-09-09 for the source-faithful fix (shorts restored, gap
# filter incl. gap==0 block, opposite-side channel exit, session-close exit,
# EMA/fixed-stop opt-ins). Old values (8 trades / +2886.67) encoded the buggy
# long-only port without gap/channel/session exits.
EXPECTED_TRADE_COUNT = 33
# Re-baselined 2026-09-15 for P3a A-M2 (close-triggered trailing exits fill
# at the close, not at the stop level) + D4 (signal exits at trigger close):
# same 33 triggers, fills move from -417.31 to -688.18.
EXPECTED_NET_PROFIT = -688.18


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="channel_breakout",
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


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "channel_breakout_unit", STRATEGY_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _two_day_frame() -> pd.DataFrame:
    """Zwei NY-Tage 30m-Bars; Tag 2 mit Breakout + steady Rise (kein Trail-Exit)."""
    rows = []
    for day in ("2024-01-02", "2024-01-03"):
        base = pd.Timestamp(day + " 09:30", tz="America/New_York")
        rows.extend((base + pd.Timedelta(minutes=30 * k)).tz_convert("UTC") for k in range(14))
    stamps = pd.DatetimeIndex(rows)
    n = len(stamps)
    close = [100.0] * n
    close[15] = 102.0
    for k in range(16, n):
        close[k] = 102.0 + 0.1 * (k - 15)
    close_s = pd.Series(close)
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close_s,
            "high": close_s + 0.05,
            "low": close_s - 0.05,
            "close": close_s,
            "volume": 1.0,
        }
    )


_SMALL = dict(
    channel_length=3,
    ema_period=5,
    atr_period=5,
    use_ema_filter=False,
    use_channel_exit=False,
    use_ema_exit=False,
)


def test_fixed_stop_is_dollar_risk_via_point_value():
    """B-H2/D6: stop_loss_amount ist $-Risiko, Distanz = amount/point_value.

    Default $100 bei $20/Punkt (NQ) -> 5 Punkte, nicht 100 Punkte
    (~$2000/Kontrakt, ~20x mis-sized).
    """
    module = _load_module()
    df = _two_day_frame()
    params = dict(
        _SMALL, use_fixed_stop_loss=True, stop_loss_amount=100.0, use_gap_filter=False
    )
    entries, _, indicators, _ = module.generate_signals(df, params)
    assert int(entries.sum()) >= 1
    entry_idx = entries[entries].index.tolist()
    for i in entry_idx:
        dist = abs(float(df.loc[i, "close"]) - float(indicators["sl_price"].loc[i]))
        assert dist == pytest.approx(5.0), (
            f"$100/$20pt muss 5 Punkte Distanz geben, gefunden {dist}"
        )
    # Custom point value skaliert die Distanz.
    params_ep = dict(params, point_value=10.0)
    _, _, indicators_ep, _ = module.generate_signals(df, params_ep)
    for i in entry_idx:
        dist = abs(float(df.loc[i, "close"]) - float(indicators_ep["sl_price"].loc[i]))
        assert dist == pytest.approx(10.0)


def test_session_close_exit_fires_without_gap_filter():
    """B-M2: exit_on_session_close gilt auch bei use_gap_filter=False."""
    module = _load_module()
    df = _two_day_frame()
    params = dict(
        _SMALL, use_gap_filter=False, exit_on_session_close=True
    )
    entries, exits, indicators, _ = module.generate_signals(df, params)
    assert int(entries.sum()) >= 1
    exit_idx = exits[exits].index.tolist()
    assert len(exit_idx) >= 1, "Session-Close muss flatten"
    last = exit_idx[-1]
    assert indicators["exit_reason"].loc[last] == "session_close"
    assert df.loc[last, "timestamp"] == df["timestamp"].iloc[-1]

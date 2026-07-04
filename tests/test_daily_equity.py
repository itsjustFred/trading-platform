"""Tests for runner.daily_equity finite-value and frequency-metadata handling."""
from __future__ import annotations

import math

from runner.daily_equity import attach_daily_equity, build_daily_equity_curve
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit


def _make_result(trades: list[Trade], *, start: str, end: str) -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="t",
            created_at="2024-01-01T00:00:00+00:00",
            engine="native",
            strategies=["s"],
            backtest_start=start,
            backtest_end=end,
            initial_capital=100000.0,
            data={"symbols": ["SPY"], "timeframes": ["1d"]},
        ),
        trades=trades,
        equity_curve=[],
        stats={},
    )


def _trade(exit_time: str, pnl: float) -> Trade:
    return Trade(
        id=1,
        symbol="SPY",
        direction="LONG",
        entry_time="2024-01-01T00:00:00+00:00",
        entry_price=100.0,
        quantity=1.0,
        exits=[TradeExit(time=exit_time, price=101.0, quantity=1.0, pnl=pnl)],
        pnl=pnl,
    )


def test_non_finite_pnl_is_skipped():
    result = _make_result(
        [
            _trade("2024-01-02T00:00:00+00:00", float("nan")),
            _trade("2024-01-02T00:00:00+00:00", float("inf")),
            _trade("2024-01-02T00:00:00+00:00", 500.0),
        ],
        start="2024-01-01",
        end="2024-01-03",
    )
    points, returns = build_daily_equity_curve(result)

    # All equity points stay finite, and only the finite 500.0 PnL is applied.
    assert all(math.isfinite(p.equity) for p in points)
    assert all(math.isfinite(r["equity"]) and math.isfinite(r["return"]) for r in returns)
    assert points[-1].equity == 100500.0


def test_attach_marks_frequency_daily_for_valid_window():
    result = _make_result(
        [_trade("2024-01-02T00:00:00+00:00", 250.0)],
        start="2024-01-01",
        end="2024-01-03",
    )
    attach_daily_equity(result)
    assert result.meta.data["equity_frequency"] == "daily"
    assert result.stats["daily_return_count"] > 0


def test_attach_does_not_mislabel_fallback_as_daily():
    # No usable window (no dates, no trades, no equity) -> fallback path must not
    # claim the curve is daily.
    result = BacktestResult(
        meta=BacktestMeta(
            id="t",
            created_at="2024-01-01T00:00:00+00:00",
            engine="native",
            strategies=["s"],
            backtest_start="",
            backtest_end="",
            initial_capital=100000.0,
            data={"symbols": ["SPY"], "timeframes": ["1d"]},
        ),
        trades=[],
        equity_curve=[],
        stats={},
    )
    attach_daily_equity(result)
    assert result.meta.data["equity_frequency"] == "unknown"
    assert result.stats["daily_return_count"] == 0

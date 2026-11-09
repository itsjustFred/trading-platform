"""#123: callers that can legitimately see a trade without exits must not crash.

``Trade.last_exit_time`` raises for an exit-less trade instead of faking a
zero-duration closed trade; stored/viewer/statistics paths handle it explicitly.
"""

from __future__ import annotations

import pandas as pd

from runner.stats import _resolve_backtest_period_bounds
from shared.models import Trade, TradeExit
from validation.stats import calculator
from viewer import core


def _trade(trade_id: int, entry: str, exit_time: str | None, pnl: float = 5.0) -> Trade:
    exits = [TradeExit(time=exit_time, price=101.0, quantity=1.0, pnl=pnl)] if exit_time else []
    return Trade(
        id=trade_id,
        symbol="AAPL",
        direction="LONG",
        entry_time=entry,
        entry_price=100.0,
        quantity=1.0,
        exits=exits,
        pnl=pnl if exit_time else 0.0,
    )


def test_viewer_event_time_falls_back_to_entry_only_without_exits() -> None:
    closed = _trade(1, "2024-01-02T10:00:00Z", "2024-01-03T10:00:00Z")
    open_ = _trade(2, "2024-01-04T10:00:00Z", None)
    assert core._trade_event_time(closed) == "2024-01-03T10:00:00Z"
    assert core._trade_event_time(open_) == "2024-01-04T10:00:00Z"


def test_runner_stats_window_ignores_exit_of_open_trade() -> None:
    trades = [
        _trade(1, "2024-01-02T10:00:00Z", "2024-01-03T10:00:00Z"),
        _trade(2, "2024-01-10T10:00:00Z", None),
    ]
    start, end = _resolve_backtest_period_bounds(equity_curve=[], trades=trades)
    assert start == pd.Timestamp("2024-01-02T10:00:00Z")
    assert end == pd.Timestamp("2024-01-03T10:00:00Z")


def test_validation_stats_helpers_tolerate_trade_without_exits() -> None:
    trades = [
        _trade(1, "2024-01-02T10:00:00Z", "2024-01-03T10:00:00Z"),
        _trade(2, "2024-01-05T10:00:00Z", "2024-01-06T10:00:00Z"),
        _trade(3, "2024-02-01T10:00:00Z", None),
    ]
    assert calculator._trade_span_years(trades) is not None
    series = calculator._daily_net_series(trades)
    assert series is not None
    # Only the two closed trades book PnL; the open one adds nothing.
    assert sum(series) == 10.0

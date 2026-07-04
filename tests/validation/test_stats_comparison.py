"""Framework statistics comparison acceptance tests."""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.regression.runner import RegressionConfig, _compare_stats
from validation.stats import TradeStatistics, compute_trade_statistics


def _trade(trade_id: int, pnl: float) -> Trade:
    return Trade(
        id=trade_id,
        symbol="EURUSD",
        direction="LONG" if pnl >= 0 else "SHORT",
        entry_time=f"2024-01-{trade_id:02d}T10:00:00Z",
        entry_price=1.1,
        quantity=1.0,
        exits=[
            TradeExit(
                time=f"2024-01-{trade_id:02d}T11:00:00Z",
                price=1.2,
                quantity=1.0,
                pnl=pnl,
            )
        ],
        pnl=pnl,
    )


def test_framework_statistics_comparison_passes_identical_trade_stats() -> None:
    stats = compute_trade_statistics([_trade(1, 100.0), _trade(2, -40.0), _trade(3, 20.0)])

    deltas = _compare_stats(stats, stats, RegressionConfig())

    assert {delta.field for delta in deltas} == {
        "win_rate",
        "profit_factor",
        "net_profit",
        "gross_profit",
        "gross_loss",
        "max_drawdown",
        "max_drawdown_pct",
        "sharpe_ratio",
        "sortino_ratio",
        "recovery_factor",
    }
    assert all(delta.passed for delta in deltas)


def test_framework_statistics_comparison_flags_out_of_tolerance_stats() -> None:
    expected = compute_trade_statistics([_trade(1, 100.0), _trade(2, -40.0)])
    actual = compute_trade_statistics([_trade(1, 100.0), _trade(2, -80.0)])

    deltas = _compare_stats(expected, actual, RegressionConfig())

    failed_fields = {delta.field for delta in deltas if not delta.passed}
    assert "net_profit" in failed_fields
    net_profit = next(delta for delta in deltas if delta.field == "net_profit")
    assert net_profit.expected == pytest.approx(60.0)
    assert net_profit.actual == pytest.approx(20.0)
    assert net_profit.absolute_diff == pytest.approx(40.0)
    assert net_profit.relative_diff == pytest.approx(40.0 / 60.0)


def test_framework_statistics_comparison_respects_absolute_tolerance() -> None:
    expected = TradeStatistics(net_profit=100.0)
    actual = TradeStatistics(net_profit=100.5)
    config = RegressionConfig(
        default_stats_absolute_tolerance=1.0,
        default_stats_relative_tolerance=0.0,
    )

    deltas = _compare_stats(expected, actual, config)

    assert next(delta for delta in deltas if delta.field == "net_profit").passed is True


def test_framework_statistics_comparison_handles_infinite_profit_factor() -> None:
    expected = TradeStatistics(profit_factor=float("inf"))
    matching = TradeStatistics(profit_factor=float("inf"))
    mismatching = TradeStatistics(profit_factor=2.0)

    matching_delta = next(
        delta for delta in _compare_stats(expected, matching, RegressionConfig())
        if delta.field == "profit_factor"
    )
    mismatching_delta = next(
        delta for delta in _compare_stats(expected, mismatching, RegressionConfig())
        if delta.field == "profit_factor"
    )

    assert matching_delta.passed is True
    assert mismatching_delta.passed is False

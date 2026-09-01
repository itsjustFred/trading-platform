"""Cleanup 18-C slice 5 — comparison helpers validate their inputs.

`compare_backtests` promised "2+ backtests" but silently blended only the
first two; `pearson_correlation` is public module API without a
length/finiteness guard.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _strategy(name: str, curve: list[list]) -> dict:
    return {
        "name": name,
        "equity_curve": curve,
        "drawdown_curve": [],
        "stats": {},
        "trades": {},
    }


class TestPearsonCorrelation:
    def test_perfect_correlation(self):
        from viewer.comparison import pearson_correlation

        assert pearson_correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_mismatched_lengths_return_zero_not_wrong_value(self):
        from viewer.comparison import pearson_correlation

        assert pearson_correlation([1.0, 2.0, 3.0], [1.0, 2.0]) == 0.0

    def test_non_finite_inputs_return_zero(self):
        from viewer.comparison import pearson_correlation

        assert pearson_correlation([1.0, float("nan"), 3.0], [1.0, 2.0, 3.0]) == 0.0
        assert pearson_correlation([1.0, 2.0, 3.0], [1.0, float("inf"), 3.0]) == 0.0

    def test_empty_inputs_return_zero_without_raising(self):
        from viewer.comparison import pearson_correlation

        assert pearson_correlation([], []) == 0.0

    def test_zero_variance_returns_zero(self):
        from viewer.comparison import pearson_correlation

        assert pearson_correlation([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) == 0.0


class TestCompareBacktestsArity:
    def test_requires_two_strategies(self):
        from viewer.comparison import compare_backtests

        curve = [["2024-01-01", 100000.0], ["2024-01-02", 101000.0]]
        result = compare_backtests([_strategy("only", curve)])
        assert any("exactly 2" in error.lower() for error in result["combined_view"]["errors"])

    def test_rejects_more_than_two_with_explicit_error(self):
        from viewer.comparison import compare_backtests

        curve = [["2024-01-01", 100000.0], ["2024-01-02", 101000.0]]
        result = compare_backtests(
            [
                _strategy("a", curve),
                _strategy("b", curve),
                _strategy("c", curve),
            ]
        )
        # The third strategy must not be silently dropped.
        assert any(
            "exactly 2" in error.lower() or "3" in error
            for error in result["combined_view"]["errors"]
        )
        # All strategies are still reported for the per-strategy table.
        assert [s["name"] for s in result["strategies"]] == ["a", "b", "c"]

    def test_two_strategies_still_produce_combined_view(self):
        from viewer.comparison import compare_backtests

        curve_a = [["2024-01-01", 100000.0], ["2024-01-02", 101000.0]]
        curve_b = [["2024-01-01", 100000.0], ["2024-01-02", 99000.0]]
        result = compare_backtests(
            [_strategy("a", curve_a), _strategy("b", curve_b)]
        )
        assert len(result["combined_view"]["combined_equity_curve"]) == 2
        assert result["combined_view"]["errors"] == []

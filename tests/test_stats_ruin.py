"""Acceptance tests for #132: a ruined equity curve is flagged, never summarised.

Returns after a non-positive equity point used to be dropped silently, so
Sharpe/Sortino/PSR/p-value were computed from the pre-ruin subset and a blown
up strategy could show a respectable Sharpe. Maintainer decision (option C):
truncate the curve at the first non-positive equity point, set ``ruined`` and
``ruin_time``, and publish Sharpe, Sortino, PSR and p-value as ``None``,
listed in ``undefined_metrics``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import runner.stats as stats_module
from runner.stats import compute_stats
from shared.models import EquityPoint

RETURN_METRICS = ("sharpe_ratio", "sortino_ratio", "psr", "p_value")
START = datetime(2021, 1, 4, tzinfo=timezone.utc)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [
        EquityPoint(time=(START + timedelta(days=i)).isoformat(), equity=float(eq))
        for i, eq in enumerate(equities)
    ]


# Strong, steady gains, then a single wipe-out, then a "recovery" that must
# not count: the account is gone at index 6.
PRE_RUIN = [100_000, 101_000, 102_500, 103_000, 104_200, 105_000]
RUINED = PRE_RUIN + [0.0, 50_000, 150_000, 200_000]
RUIN_INDEX = len(PRE_RUIN)


@pytest.fixture(params=[True, False], ids=["quantstats", "manual"])
def quantstats_flag(request, monkeypatch):
    if request.param and not stats_module.HAS_QUANTSTATS:
        pytest.skip("quantstats not installed")
    monkeypatch.setattr(stats_module, "HAS_QUANTSTATS", request.param)
    return request.param


def test_ruined_curve_sets_flag_and_ruin_time(quantstats_flag) -> None:
    curve = _curve(RUINED)
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=curve)

    assert stats["ruined"] is True
    assert stats["ruin_time"] == curve[RUIN_INDEX].time


def test_ruined_curve_return_metrics_are_null_and_undefined(quantstats_flag) -> None:
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(RUINED))

    for metric in RETURN_METRICS:
        assert stats[metric] is None, metric
        assert metric in stats["undefined_metrics"], metric


def test_ruined_curve_cagr_and_calmar_are_null_and_undefined(quantstats_flag) -> None:
    """A liquidated run has no annualised growth rate; 0.0 would beat honest losers."""
    for equities in (RUINED, [100_000.0, 0.0]):
        stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(equities))

        for metric in ("cagr", "calmar_ratio"):
            assert stats[metric] is None, (metric, equities)
            assert metric in stats["undefined_metrics"], (metric, equities)


def test_healthy_curve_keeps_cagr_and_calmar(quantstats_flag) -> None:
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(PRE_RUIN))

    assert isinstance(stats["cagr"], float)
    assert isinstance(stats["calmar_ratio"], float)
    assert "cagr" not in stats["undefined_metrics"]
    assert "calmar_ratio" not in stats["undefined_metrics"]


def test_unsorted_curve_with_two_zero_points_picks_earliest_in_time(quantstats_flag) -> None:
    curve = _curve([100_000, 101_000, 102_000, 0.0, 50_000, 80_000, 0.0, 90_000])
    # The later-time zero (index 6) comes first in list order.
    shuffled = [curve[0], curve[6], curve[1], curve[2], curve[3], curve[7], curve[4], curve[5]]
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=shuffled)

    assert stats["ruined"] is True
    assert stats["ruin_time"] == curve[3].time
    assert stats["total_return"] == pytest.approx(-1.0)


def test_ruined_curve_is_truncated_at_ruin(quantstats_flag) -> None:
    """The post-ruin 'recovery' to 200k must not reach any equity metric."""
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(RUINED))

    assert stats["total_return"] == pytest.approx(-1.0)
    # equity peaked at 105k and ended at 0 -> a full drawdown (negative convention)
    assert stats["max_drawdown_pct"] == pytest.approx(-1.0)


def test_negative_equity_point_counts_as_ruin(quantstats_flag) -> None:
    curve = _curve(PRE_RUIN + [-5_000.0, 120_000])
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=curve)

    assert stats["ruined"] is True
    assert stats["ruin_time"] == curve[RUIN_INDEX].time
    assert stats["total_return"] == pytest.approx(-1.05)
    for metric in RETURN_METRICS:
        assert stats[metric] is None, metric


def test_unsorted_curve_uses_first_ruin_in_time_order(quantstats_flag) -> None:
    curve = _curve(RUINED)
    shuffled = [curve[3], curve[0], curve[RUIN_INDEX + 2], curve[RUIN_INDEX], curve[1], curve[2],
                curve[4], curve[5], curve[RUIN_INDEX + 1], curve[RUIN_INDEX + 3]]
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=shuffled)

    assert stats["ruined"] is True
    assert stats["ruin_time"] == curve[RUIN_INDEX].time


def test_healthy_curve_is_not_ruined_and_keeps_metrics(quantstats_flag) -> None:
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(PRE_RUIN))

    assert stats["ruined"] is False
    assert stats["ruin_time"] is None
    assert stats["sharpe_ratio"] is not None
    assert "sharpe_ratio" not in stats["undefined_metrics"]


def test_no_curve_is_not_ruined() -> None:
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=[])

    assert stats["ruined"] is False
    assert stats["ruin_time"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=str)
def test_non_finite_equity_is_ruin_and_fails_closed(quantstats_flag, bad) -> None:
    """NaN/inf equity is never a tradeable account: treat it as ruin, never skip it."""
    curve = _curve(PRE_RUIN + [bad, 120_000.0])
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=curve)

    assert stats["ruined"] is True
    assert stats["ruin_time"] == curve[len(PRE_RUIN)].time
    for metric in RETURN_METRICS + ("cagr", "calmar_ratio"):
        assert stats[metric] is None, metric
        assert metric in stats["undefined_metrics"], metric


def test_degenerate_curves_name_return_and_drawdown_as_undefined() -> None:
    """Consumers resolve these through `undefined_metrics`, not the published 0.0."""
    for equities in ([], [100_000.0], [0.0]):
        stats = compute_stats([], initial_capital=100_000.0, equity_curve=_curve(equities))

        for metric in ("total_return", "max_drawdown_pct"):
            assert metric in stats["undefined_metrics"], (metric, equities)

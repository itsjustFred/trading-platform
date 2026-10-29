"""A single-point engine curve is never a curve, at any window length.

`_preserves_engine_curve` keeps an engine-provided equity curve only when it describes the same
window as the daily aggregation. The first guard rejects a curve with fewer than two points, and the
reason is not cosmetic: a one-point curve has no returns, so `_curve_frequency` reports `"unknown"`
while `daily_return_count` is taken from the aggregation - a payload that describes itself two
different ways.

The existing suite covers the single-point case against a 32-day window, where the day-coverage
check would reject it anyway. That makes the case pass for the wrong reason: with the coverage check
satisfied - a one-day window, where the preserved point and the daily point cover the same day - the
outcome is decided only by the empty-gap guard, and dropping that guard is invisible to every other
test. Measured: removing `gaps.empty` from the gap check left all 23 daily-equity tests green.

This file pins the invariant directly, at the one window length where nothing else catches it.
"""

from __future__ import annotations

from runner.daily_equity import _preserves_engine_curve
from shared.models import EquityPoint


def test_a_single_point_curve_is_not_preserved_for_a_one_day_window() -> None:
    """The point covers the whole window, so only the "no returns" rule can reject it."""
    preserved = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0)]
    daily = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0)]

    assert not _preserves_engine_curve(preserved, daily)


def test_a_single_point_curve_is_not_preserved_even_with_matching_endpoints() -> None:
    """Same shape, and the endpoint tolerance would otherwise accept it.

    The endpoint check is the last one in the function and is satisfied here, so a curve that
    reaches it is preserved. This asserts the earlier guards do their job before that.
    """
    preserved = [EquityPoint(time="2024-01-01T12:00:00+00:00", equity=100_000.0)]
    daily = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0)]

    assert not _preserves_engine_curve(preserved, daily)

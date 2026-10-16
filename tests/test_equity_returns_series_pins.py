"""`_equity_to_returns_series`: an absent series and an empty series are different answers.

    Why this file exists

    2026-10-15, from the `runner/` mutation queue. Two mutations, and they came out differently:

        M49  `previous <= 0` weakened to `previous < 0`   equivalent - NOT a defect
        M50  `len(equity_curve) < 2` weakened to `< 1`    SURVIVED - 5,076 passed

    **M49 is an equivalent mutant, and recording that is the point.** My reasoning was that
    `previous <= 0` was a divide-by-zero guard, and the mutation looked obviously wrong. In fact:

        equity 100 -> 0    pct_change = -1.0    (a total loss - a real, meaningful return)
        equity   0 -> 50   pct_change =  inf    (undefined - division by zero)

    So the `<= 0` guard is not what catches the infinity; the very next line does:

        return returns.replace([math.inf, -math.inf], pd.NA).dropna()

    Both paths drop the same entries, so the mutation changes nothing observable. **This is redundant
    code, not a bug**, and it is worth recording precisely because the default assumption when a mutant
    survives is "a gap in the tests" — here the honest conclusion was "two guards for one condition,
    and removing one is safe". No test was added, because a test that pins redundant code would itself be
    asserting an implementation detail.

    **M50 is a real difference.** A one-point equity curve returns `None` today. Under the mutation it
    returns an *empty Series*, which is not `None`: a caller guarding with `if returns is None` takes a
    different branch, and any aggregation over the empty series yields `nan` instead of short-circuiting.
    One point of equity is not "zero returns", it is "no interval", and the function is right to say so.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from runner.stats import _equity_to_returns_series
from shared.models import EquityPoint


def _curve(*values: float) -> list[EquityPoint]:
    return [
        EquityPoint(time=f"2024-01-{day:02d}T00:00:00Z", equity=value)
        for day, value in enumerate(values, start=1)
    ]


# --- M50: fewer than two points is "no interval", not "no returns" --------------


@pytest.mark.parametrize("length", [0, 1])
def test_a_curve_shorter_than_two_points_has_no_returns_series(length: int) -> None:
    """None, not an empty Series. The two are different answers to different questions."""
    assert _equity_to_returns_series(_curve(*([100.0] * length))) is None


def test_an_empty_series_would_not_survive_being_aggregated() -> None:
    """The reason the distinction matters, stated rather than asserted about a caller."""
    empty = pd.Series([], dtype=float)
    assert empty is not None
    assert math.isnan(float(empty.mean()))


def test_two_points_produce_exactly_one_return() -> None:
    series = _equity_to_returns_series(_curve(100.0, 110.0))
    assert series is not None
    assert len(series) == 1
    assert float(series.iloc[0]) == pytest.approx(0.10)


def test_a_drop_to_zero_is_a_return_of_minus_one_not_an_error() -> None:
    """100 -> 0 is a total loss: -1.0 is the honest answer, and it must survive the series."""
    series = _equity_to_returns_series(_curve(100.0, 0.0))
    assert series is not None
    assert len(series) == 1
    assert float(series.iloc[0]) == pytest.approx(-1.0)


def test_a_rise_from_zero_is_undefined_and_is_dropped() -> None:
    """0 -> 50 divides by zero. The return is undefined, so it is absent rather than infinite."""
    series = _equity_to_returns_series(_curve(100.0, 0.0, 50.0))
    assert series is not None
    # -1.0 for the drop; the rise from a zero base is undefined and does not appear.
    assert len(series) == 1
    assert float(series.iloc[0]) == pytest.approx(-1.0)
    assert not math.isinf(float(series.iloc[0]))


def test_the_series_is_ordered_by_time_regardless_of_input_order() -> None:
    forward = _equity_to_returns_series(_curve(100.0, 110.0, 121.0))
    reverse = _equity_to_returns_series(list(reversed(_curve(100.0, 110.0, 121.0))))
    assert forward is not None and reverse is not None
    assert list(forward.index) == list(reverse.index)
    assert [round(float(v), 6) for v in forward] == [round(float(v), 6) for v in reverse]

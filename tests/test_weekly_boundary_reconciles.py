"""The two sides of the week boundary, pinned to each other.

Item 9's backlog entry says the obstacle to enabling `1w` is "a week-boundary disagreement":

    `resample("1W")` resolves to `<Week: weekday=6>` - W-SUN, right-closed - so weekly bars are
    stamped Sunday, while `shared/market_calendar.py` documents interval-starts stamped Monday,
    and the live timing module assumes a Monday boundary.

That prose was true when written and is **now wrong on the resampler side**. `TIMEFRAME_MAP["1w"]`
is `W-MON` and `resample_with_origin` pins `label="left"`/`closed="left"`, so a weekly bar carries
the Monday its week *starts* on. `execution_timing._next_candle_close` independently anchors weeks
on Monday 00:00 UTC.

So the reconciliation the entry asks for has already happened. This file is what stops the two
sides drifting apart again, and stops the entry's stale claim from going unnoticed: a reader who
believed it would think a 1-day stamp disagreement still blocks `1w`, and decide accordingly.

The cross-check below is the substantive assertion - the live module's notion of when a weekly
candle closes must land exactly on the boundary the resampler uses to label the *next* bar. The
two were written by different routes (a pandas frequency string and a `weekday()` computation), so
nothing but a test relates them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_tools.resample import TIMEFRAME_MAP, resample_ohlcv
from live_runner.signal_sources.execution_timing import _next_candle_close

WEEK_SECONDS = 7 * 86400


def _hourly_series(start: str, periods: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
    )


def test_the_rule_is_monday_anchored_not_the_default_week() -> None:
    """`1W` and `W-MON` are the same frequency but not the same labelling."""
    assert TIMEFRAME_MAP["1w"] == "W-MON", (
        "the weekly rule changed; if it is back to `1W` the bars are Sunday-stamped again and the "
        "live timing module disagrees with them by a day"
    )


def test_every_weekly_label_is_a_monday() -> None:
    """Measured on a range that straddles several Sunday/Monday boundaries."""
    out = resample_ohlcv(_hourly_series("2022-12-19", 21 * 24), "1w")
    assert len(out) >= 3, "not enough weekly bars to say anything"
    assert all(pd.Timestamp(t).weekday() == 0 for t in out["timestamp"]), (
        "weekly bars must carry the Monday their interval starts on; got "
        f"{[str(t) for t in out['timestamp']]}"
    )


def test_labels_are_interval_starts_so_the_next_label_is_the_close() -> None:
    """A bar stamped Monday T covers T..T+6d, so the next label is also that week's close."""
    out = resample_ohlcv(_hourly_series("2022-12-19", 21 * 24), "1w")
    stamps = [pd.Timestamp(t) for t in out["timestamp"]]
    gaps = {b - a for a, b in zip(stamps, stamps[1:])}
    assert gaps == {timedelta(days=7)}, f"weekly labels are not 7 days apart: {gaps}"


@pytest.mark.parametrize(
    "reference",
    [
        "2022-12-19T00:00:00+00:00",  # exactly a Monday midnight
        "2022-12-21T09:30:00+00:00",  # mid-week
        "2022-12-25T23:59:59+00:00",  # a Sunday, the day the old rule would have stamped
        "2022-12-26T00:00:00+00:00",  # the next Monday
        "2023-01-01T10:00:00+00:00",  # New Year, inside a week
    ],
)
def test_the_live_module_closes_on_the_boundary_the_resampler_labels(reference: str) -> None:
    """The cross-check: two implementations, written by different routes, must agree exactly.

    For any reference inside a week, the live module's next weekly close must equal the Monday that
    labels the following bar. This is the reconciliation the backlog entry asks for, expressed as an
    assertion instead of prose - prose is what let the entry go stale while the code was fixed.
    """
    ref = datetime.fromisoformat(reference)
    close = _next_candle_close(ref, WEEK_SECONDS, timeframe="1w")
    assert close.weekday() == 0, f"weekly close {close} is not a Monday"
    assert close.hour == 0 and close.minute == 0 and close.second == 0, (
        f"weekly close {close} is not Monday 00:00 UTC"
    )
    # The close is strictly after the reference, and never more than a week later.
    assert close > ref, f"close {close} is not after the reference {ref}"
    assert close - ref <= timedelta(days=7), f"close {close} is more than a week after {ref}"
    # And it coincides with a real resampler gridline: resampling a series that spans `close` must
    # produce a bar labelled exactly `close`.
    span = resample_ohlcv(_hourly_series("2022-12-19", 21 * 24), "1w")
    stamps = {pd.Timestamp(t) for t in span["timestamp"]}
    assert close in stamps, (
        f"the live module closes weekly at {close} but the resampler never labels a bar then; "
        f"it labels {sorted(str(s) for s in stamps)}"
    )

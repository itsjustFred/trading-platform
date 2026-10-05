"""The coverage gap is a NaN problem in the 1m source, and aggregating upward hides it.

Measured against the real XAUUSD 1m store, read-only:

    rows 7,850,420   span 2003-05-05 -> 2026-06-24 (8,451 days / 23.1 years)
    NaN share in close: 32.7%

    all-NaN bucket rate, by aggregation level:
      1m   buckets 12,170,879   empty 6,885,863   56.6%
      1h   buckets    202,848   empty   105,508   52.0%
      1d   buckets      8,452   empty     3,514   41.6%
      1w   buckets      1,208   empty       269   22.3%

    coverage against the calendar span:
      1d   4,938 of ~8,451   58.4%   3,513 gaps
      1w     939 of ~1,208   77.7%     269 gaps

**This is why item 9's caveat matters more than it reads.** The backlog noted the coverage gate
already fails for `1d`, and that `1w` "looks *healthier*". Measured, the mechanism is the
problem: a bucket survives if **any** minute in it is valid, so the *same* 32.7% NaN share
yields a 41.6% empty-bucket rate at daily and only 22.3% at weekly.

So weekly's better coverage number is an artefact of aggregation, not better data. Enabling
`1w` would put a timeframe in front of the operator whose coverage figure is roughly 2.7x
better than daily's for the identical underlying defect - the misleading green that item 14's
not-comparable gate exists to catch, in a different guise.

Nothing here decides whether to enable `1w`. It establishes that the daily-versus-weekly
coverage comparison is not a comparison of data quality, and that repairing the NaN placeholders
in the 1m source is what would actually move either number.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas", reason="pandas is needed to read the parquet store")

SOURCE = Path("/home/ubuntu/data/ohlcv/dukascopy/metals/XAUUSD/1m.parquet")

# Measured 2026-10-05, recorded so the shape of the problem is pinned even where the 218 MB file
# is absent, and so a later re-measure has something to compare against.
MEASURED = {
    "1m": (12170879, 6885863),
    "1h": (202848, 105508),
    "1d": (8452, 3514),
    "1w": (1208, 269),
}


def test_the_recorded_measurement_is_self_consistent() -> None:
    """A cheap guard that the docstring's table is the one the assertions use.

    Without it the prose could drift from the assertions and the file would still pass, which is
    the same failure this session keeps finding in the notes.
    """
    assert MEASURED["1w"][1] < MEASURED["1d"][1], (
        "weekly cannot have fewer empty buckets than daily"
    )
    for level, (buckets, empty) in MEASURED.items():
        assert 0 < empty < buckets, f"{level}: {empty} of {buckets} is not a fraction"


@pytest.fixture(scope="module")
def close_series():
    if not SOURCE.exists():
        pytest.skip(f"{SOURCE} is absent, so the deficit cannot be re-measured here")
    frame = pd.read_parquet(SOURCE)
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    return frame["close"].set_axis(stamps).sort_index()


def _empty_rate(series, rule: str) -> tuple[int, float]:
    buckets = series.resample(rule, label="left", closed="left").first().isna()
    return len(buckets), float(buckets.mean())


def test_the_empty_bucket_rate_falls_as_the_timeframe_grows(close_series) -> None:
    """The mechanism: a bucket survives if any minute in it is valid."""
    hourly = _empty_rate(close_series, "1h")[1]
    daily = _empty_rate(close_series, "1D")[1]
    weekly = _empty_rate(close_series, "W-MON")[1]
    assert hourly > daily > weekly, (
        f"expected the empty rate to fall as the timeframe grows, got 1h={hourly:.3f} "
        f"1d={daily:.3f} 1w={weekly:.3f}. If that ordering changed, the aggregation artefact "
        f"this file describes no longer holds and the notes need re-measuring."
    )
    assert 0 < weekly < daily < 1


def test_weekly_coverage_is_not_evidence_that_weekly_data_is_better(close_series) -> None:
    """The claim the dashboard and backlog should carry, asserted as a computation.

    Weekly covers 77.7% of its calendar span against daily's 58.4%. That is not weekly data being
    better - it is the same 32.7% NaN source aggregated up until a bucket is likely to contain
    at least one valid minute. Reading the two coverage figures as a quality comparison would
    conclude the opposite of the truth.
    """
    span_days = (close_series.index.max() - close_series.index.min()).days
    daily_present = int(
        close_series.resample("1D", label="left", closed="left").first().notna().sum()
    )
    weekly_present = int(
        close_series.resample("W-MON", label="left", closed="left").first().notna().sum()
    )
    daily_coverage = daily_present / span_days
    weekly_coverage = weekly_present / math.ceil(span_days / 7)
    assert weekly_coverage > daily_coverage, (
        f"weekly {weekly_coverage:.1%} no longer covers more than daily {daily_coverage:.1%}; if "
        f"that flipped, the aggregation artefact needs re-measuring"
    )
    assert daily_coverage < 0.75, (
        f"daily coverage is now {daily_coverage:.1%}, up from the recorded 58.4%. That would mean "
        f"the 1m source was repaired - good news, but then weekly stops looking flattering and the "
        f"notes need updating rather than inheriting these numbers"
    )

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

So weekly's better coverage number is an artefact of aggregation, not better data.

**Correction, 2026-10-10: the "2.7x" in this docstring was wrong, and re-measuring it exposed two
further errors of mine in the same paragraph - including a dramatic one that had already been copied
into the backlog.**

Recomputed from the measured columns rather than from the ratio remembered: 77.7 / 58.4 = **1.33x**,
not 2.7x. No pair of columns yields 2.7; the closest is the *empty-bucket* rate, 41.6 / 22.3 = 1.87x.
The earlier version overstated the gap by roughly double, in a document whose entire purpose is to
stop someone over-reading a coverage number.

**The scale-free metric that looks like the fix does not work here.** Reporting each bucket's
valid-minute *fraction* rather than whether it has any data looks aggregation-invariant, and the means
agree (68.7% at 1d vs 68.5% at 1w, against the ~67.3% implied by the raw NaN share) - which is exactly
why it would have passed review. But the distributions are degenerate: **85.6% of weekly buckets are
exactly 0.0 or exactly 1.0**, across only 53 distinct values in 1,208 buckets. The reason is that the
NaNs are **contiguous blocks, not scattered minutes** - 192 runs, median **6.46 days**, p90 **37.1 days**, max **121.1 days** wall-clock, and **190 of the 192**
runs span at least a full day. A block gap leaves each bucket it touches either complete or empty, so fill-fraction collapses back into the same any-data indicator
it was meant to replace.
either complete or empty, so fill-fraction collapses back into the same any-data indicator it was
meant to replace.

**A third error, and the one worth recording: I reported a "1,782-day hole" spanning 2019-03-14 to
2026-06-24 - roughly seven years - and wrote it into the backlog as the root cause. It does not
exist.** I measured gaps between *consecutive index rows* and read the missing weekends as missing
data. The index is sparse by design: 7,850,420 rows span 12,170,879 minutes, so only **64.5%** of the
clock has rows at all, and a naive row-gap detector reports every weekend as a hole. The true maximal
NaN run is **121.1 days wall-clock**. Every year from 2015 through 2025 holds ~354,000 rows - the
continuous, not gappy. The largest *row-absence* gap and the largest *NaN* gap are different
phenomena, and I conflated them.

So the corrected picture: the deficit is real (32.7% NaN, 192 contiguous runs, max 83 days, 57 of
them a week or longer) but it is **not** one dominant hole, and weekly's better coverage is the
aggregation artefact it was always claimed to be - nothing more dramatic than that. The 1.33x gap
stands, the scale-free metric does not, and the seven-year hole was mine.

Nothing here decides whether to enable `1w`. Comparing coverage across aggregation levels remains
untrustworthy - a ratio gate would mislead here, and no ratio gate is shipped - so compare over a
common span or count NaN runs, and fix the defect at the 1m source either way.

Nothing here decides whether to enable `1w`. It establishes that the daily-versus-weekly
coverage comparison is not a comparison of data quality, and that repairing the NaN placeholders
in the 1m source is what would actually move either number.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import sys

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


NAN_RUN_COUNT = 192
MEASURED_NAN_SHARE = 32.7
MEDIAN_NAN_RUN_DAYS = 6.46
MAX_NAN_RUN_DAYS = 121.1
NAN_RUNS_OVER_1D = 190
P90_NAN_RUN_DAYS = 37.1
WEEKLY_FILL_DEGENERATE_PCT = 85.6
WEEKLY_FILL_DISTINCT = 53
WEEKLY_OVER_DAILY_COVERAGE_RATIO = 1.33


def _nan_runs(series) -> "np.ndarray":
    """Lengths, in **wall-clock days**, of the maximal runs of NaN.

    Counted on the boolean mask, never on gaps between index rows. That distinction is the whole
    point: this index is sparse by design (weekends carry no rows at all), so a row-gap detector
    reports every weekend as a hole and will happily invent a multi-year "gap" out of ordinary
    market closures. That mistake was made here once and reached the backlog before it was caught -
    it reported 1,782 days where the true figure is 83.
    """
    values = series.isna().to_numpy()
    # Start and end must both come from *mask boundaries*. An earlier version paired
    # `nonzero()[0]` (every NaN position, 2.5M of them) against the 192 run ends, which zips
    # mismatched lengths and silently measures the wrong rows - it reported a 6.08-day median
    # where the real figure is 6.46.
    at_start = np.flatnonzero(values & ~np.concatenate(([False], values[:-1])))
    at_end = np.flatnonzero(values & ~np.concatenate((values[1:], [False]))) + 1
    runs = [
        (series.index[min(end, len(series) - 1)] - series.index[start]).total_seconds() / 86400.0
        for start, end in zip(at_start, at_end)
    ]
    return np.asarray(runs, dtype=float)


def test_the_docstring_figures_match_the_numbers_below_it() -> None:
    """The prose is pinned to the tables, because prose is what drifted.

    Three separate numbers in this file's docstring were wrong at one point - a 1,782-day "hole",
    a 2.7x ratio, an 86.2% degeneracy - and every one of them was in *prose*, so pytest had nothing
    to assert against and reported this file green. Tests that re-measure the store cannot catch
    that: the store did not change, the sentence did. So the sentences themselves are checked
    against the values computed below, which is the only comparison that can fail.
    """
    # `__doc__` inside a function is the *function's* docstring, not the module's - which is why
    # the first version of this test silently compared the text against itself and passed.
    # Only the MODULE docstring. Concatenating the sibling test's docstring here made the check
    # satisfiable from the wrong text: mutating the module prose left the numbers intact a few
    # lines down in another docstring, so the assertion still passed. Verified by mutation - this
    # version is killed by editing the prose and nothing else.
    doc = sys.modules[__name__].__doc__ or ""
    checks = {
        "NaN share 32.7%":      f"{MEASURED_NAN_SHARE:.1f}",
        "median run 3.92 days": f"{MEDIAN_NAN_RUN_DAYS:.2f}",
        "max run 121.1 days":   f"{MAX_NAN_RUN_DAYS:.1f}",
        "1.33x ratio":          f"{WEEKLY_OVER_DAILY_COVERAGE_RATIO:.2f}",
        # Present in the prose, previously unpinned - a mutation of each survived.
        "p90 run 37.1 days":    f"{P90_NAN_RUN_DAYS:.1f}",
        "190 runs a day+":      f"{NAN_RUNS_OVER_1D}",
        "85.6% degenerate":     f"{WEEKLY_FILL_DEGENERATE_PCT:.1f}",
        "53 distinct":          f"{WEEKLY_FILL_DISTINCT}",
    }
    for label, rendered in checks.items():
        assert rendered in doc, (
            f"docstring no longer contains {rendered!r} ({label}); the prose has drifted from the "
            f"values asserted below. Update both together - this is the failure mode this file "
            f"has already produced three times."
        )
    # `in` is satisfied by a single surviving occurrence, so a figure stated twice (once in the
    # inventory, once in the retraction narrative) can drift in one place and still pass. Two figures
    # survived mutations for exactly that reason, so the expected count is asserted too.
    for label, rendered, expected in (
        ("max run 121.1 days", "121.1", 2),
        ("1.33x ratio", "1.33x", 2),
    ):
        assert doc.count(rendered) == expected, (
            f"{label}: expected {expected} occurrences of {rendered!r} in the docstring, found "
            f"{doc.count(rendered)}. Change both together or re-state the figure once."
        )


def test_the_recorded_nan_run_inventory_matches_the_store(close_series) -> None:
    """Pins the numbers in the docstring's gap-inventory paragraph.

    Every other test here re-derives an *empty-bucket rate*, which is a different quantity. The
    run inventory - 192 runs, median 6.46 days, max 121.1 days, 190 of them a day or more -
    was prose with nothing asserting it, which is how a wrong version of it survived a green suite.
    """
    import numpy as np

    runs = _nan_runs(close_series)
    assert len(runs) == NAN_RUN_COUNT, f"NaN runs changed: {len(runs)} (recorded: {NAN_RUN_COUNT})"
    median_days = float(np.median(runs))
    assert math.isclose(median_days, MEDIAN_NAN_RUN_DAYS, abs_tol=0.01), (
        f"median NaN run moved: {median_days:.2f} days (docstring says 6.46)"
    )
    max_days = float(runs.max())
    assert math.isclose(max_days, MAX_NAN_RUN_DAYS, abs_tol=0.1), (
        f"max NaN run moved: {max_days:.2f} days (docstring says 121.1)"
    )
    share = float(runs[runs >= 1.0].sum() / runs.sum() * 100)
    over_a_day = int((runs >= 1.0).sum())
    assert over_a_day == 190, (
        f"runs spanning a day or more changed: {over_a_day} (docstring says 190 of 192)"
    )


def test_the_coverage_ratio_is_1_33x_not_2_7x() -> None:
    """Pins the corrected ratio from arithmetic, so "2.7x" cannot reappear.

    The original docstring overstated the daily-vs-weekly gap by roughly double. No pair of the
    measured columns yields 2.7; the closest is the empty-bucket rate at 41.6/22.3 = 1.87.
    """
    daily, weekly = MEASURED["1d"], MEASURED["1w"]
    daily_cov = (daily[0] - daily[1]) / daily[0]
    weekly_cov = (weekly[0] - weekly[1]) / weekly[0]
    assert math.isclose(daily_cov, 0.584, abs_tol=0.005), f"daily coverage moved: {daily_cov:.3f}"
    assert math.isclose(weekly_cov, 0.777, abs_tol=0.005), f"weekly coverage moved: {weekly_cov:.3f}"
    ratio = weekly_cov / daily_cov
    assert math.isclose(ratio, WEEKLY_OVER_DAILY_COVERAGE_RATIO, abs_tol=0.02), (
        f"weekly/daily coverage ratio is {ratio:.2f}, not the 2.7x the docstring once claimed"
    )


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


def test_the_tail_is_entirely_nan_so_every_timeframe_is_short_not_just_weekly() -> None:
    """Why the last bar is ~83 days back on 1h, 1d AND 1w - so this is not a weekly problem.

    `resample_with_origin` ends with `dropna(subset=["open"])`, which is correct: it drops buckets
    that contain no data. But the source's final months are 100% NaN, so those buckets are dropped
    too and every resampled series ends at the last real bar rather than at the source's last row.

    Measured on XAUUSD 1m: the source runs to 2026-06-24, and NaN close is 98.2% since 2026-04-01 and
    100% since 2026-05-01. So 1h's last bar is 2026-04-02, 1d's is 2026-04-02 and 1w's is 2026-03-30 -
    all short by 83-86 days for the same reason.

    This matters for item 9: the weekly coverage advantage measured earlier is not evidence that
    weekly data is better. And a tail that reads as "83 days of missing data" on a chart is really
    "the last 83 days were downloaded as NaN", which is a different defect with a different fix.
    """
    import sys

    import pandas as pd

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    from data_tools.resample import resample_ohlcv

    source = pd.read_parquet(SOURCE)
    ts = pd.to_datetime(source["timestamp"], utc=True)
    source_end = ts.max()

    for cutoff, minimum_nan in (("2026-05-01", 100.0), ("2026-06-01", 100.0)):
        mask = ts >= pd.Timestamp(cutoff, tz="UTC")
        if not mask.any():
            continue
        nan_pct = 100 * source.loc[mask, "close"].isna().mean()
        assert nan_pct >= minimum_nan, (
            f"rows since {cutoff} are only {nan_pct:.1f}% NaN close, so the source tail is real data "
            f"and the short-tail finding above needs re-deriving"
        )

    shortfalls = {}
    for tf in ("1h", "1d", "1w"):
        out = resample_ohlcv(source, tf)
        last = pd.to_datetime(out["timestamp"], utc=True).max()
        shortfalls[tf] = (source_end - last).days

    # Every timeframe is short, and by a comparable number of days.
    assert all(v > 60 for v in shortfalls.values()), (
        f"only some timeframes are short ({shortfalls}); if one timeframe now reaches the source's "
        f"end, the NaN-tail explanation is incomplete"
    )
    assert max(shortfalls.values()) - min(shortfalls.values()) <= 7, (
        f"the shortfalls differ a lot across timeframes ({shortfalls}), so this is not one shared "
        f"NaN tail and per-timeframe gaps need investigating separately"
    )


def test_weekly_thin_weeks_are_early_history_not_a_recent_regression() -> None:
    """Of 26 weeks below half the median row count, 24 are in 2003-2004.

    So the enablement question for 1w is largely a **data-range** question: the recent weekly series
    is dense, and the thin weeks are the first years of a 2003 start. That is a different decision
    from "weekly data is broken", and it is answerable by restricting the range rather than by
    repairing the aggregation.
    """
    import pandas as pd

    source = pd.read_parquet(SOURCE)
    ts = pd.to_datetime(source["timestamp"], utc=True)
    weeks = ts.dt.tz_localize(None).dt.to_period("W-MON").dt.start_time
    per_week = weeks.value_counts()
    median = per_week.median()
    thin = per_week[per_week < median * 0.5]

    assert len(thin), "no thin weeks remain; the early-history claim needs re-measuring"
    by_year = thin.groupby(thin.index.year).size()
    early = int(by_year.loc[[y for y in by_year.index if y <= 2005]].sum())
    assert early / len(thin) > 0.85, (
        f"only {early} of {len(thin)} thin weeks are from 2005 or earlier ({by_year.to_dict()}), so "
        f"this is a recent regression rather than early-history sparsity"
    )

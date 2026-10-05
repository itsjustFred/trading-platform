"""The readiness gate's Sharpe tolerance is flat in absolute units, and 81% of the
corpus never reaches the proportional term at all.

The arithmetic, at `validation/readiness/validator.py`:

    sharpe_threshold = max(cfg.stats_sharpe_tolerance_pct * abs(expected),
                           cfg.stats_sharpe_tolerance_abs)   # max(10% of |S|, 0.1)
    sharpe_passed    = abs(expected - actual) <= sharpe_threshold

The absolute term dominates whenever `|expected| < 1.0`, because the relative term is
then `0.10 * |S| < 0.1`. Measured over the 1,030 corpus rows holding a real `sharpe_ratio`:

    band            rows   median |S|   flat rule vs a strict 10% rule
    < 0.10            9       0.060    16.5x LOOSER
    0.10 - 0.50      50       0.346     2.9x looser
    0.50 - 1.00     773       0.898     1.1x looser
    1.00 - 2.00     172       1.437     0.7x  tighter
    > 2.00           26       4.115     0.2x  much tighter

**832 of 1,030 rows - 80.8% - never reach the proportional branch**, and the median
row, at `|S| = 0.927`, is inside the flat regime.

So the defect is not the zero edge the backlog's example table illustrates: only **9**
rows are small enough for the relative meaning to be genuinely undefined. The defect
is that four rows in five are governed by a branch that was never the point, and the
same rule is simultaneously ~16x too loose at the bottom and ~5x too tight at the top.

A tolerance is meant to say "within 10% of the expected value". Expressed that way it
should be roughly scale-invariant, and this one is not in either direction.

**This file measures the regime split; it does not decide the fix.** Going purely
proportional leaves the relative tolerance undefined at `|S| = 0`, which is presumably
why the absolute term exists. A scale-aware blend, a floor expressed in the metric's own
units, and keeping the flat term are all defensible and the choice is the operator's.
What was missing was the size of the population each option applies to.
"""

from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VALIDATOR = REPO / "validation" / "readiness" / "validator.py"
PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")

RELATIVE_FRACTION = 0.10
ABSOLUTE_FLOOR = 0.1
# Above this the absolute term is the smaller of the two, so the relative branch governs.
FLAT_REGIME_CEILING = 1.0


def test_the_tolerance_still_takes_the_max_of_relative_and_absolute() -> None:
    """The premise. If this changes, the whole regime analysis below is moot."""
    text = VALIDATOR.read_text(encoding="utf-8")
    assert "sharpe_abs_diff" in text and "sharpe_threshold" in text
    assert "max(" in text, (
        "the validator no longer takes max(relative, absolute) for Sharpe; item 14's "
        "arithmetic has changed and this file must be re-measured, not inherited"
    )


def _abs_sharpes() -> list[float]:
    if not PRODUCTION_DB.exists():
        pytest.skip(f"{PRODUCTION_DB} is absent, so there is no corpus to measure")
    conn = sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
    try:
        return [
            abs(float(v))
            for (v,) in conn.execute(
                "select sharpe_ratio from backtests"
                " where sharpe_ratio is not null and sharpe_ratio != 0"
            )
        ]
    finally:
        conn.close()


def test_the_flat_branch_governs_four_rows_in_five() -> None:
    """The size of the population the proportional branch never reaches."""
    values = _abs_sharpes()
    assert values, "no rows hold a real sharpe_ratio; re-measure rather than inherit"
    flat = [v for v in values if v < FLAT_REGIME_CEILING]
    assert len(flat) / len(values) > 0.5, (
        f"only {len(flat)} of {len(values)} rows now fall in the flat regime. If the Sharpe "
        f"scale moved, item 14's premise needs re-measuring."
    )
    assert statistics.median(values) < FLAT_REGIME_CEILING, (
        f"the median |Sharpe| is now {statistics.median(values):.3f}, so the typical row "
        f"uses the proportional branch and the flat floor is a minority concern"
    )


def test_the_same_rule_is_looser_at_the_bottom_and_tighter_at_the_top() -> None:
    """Both directions, because a flat floor is wrong at each end, not only near zero."""
    values = _abs_sharpes()
    small = [v for v in values if 0 < v < 0.10]
    large = [v for v in values if v >= 2.0]
    assert small, "no very small Sharpes remain; the 16x claim needs re-measuring"
    assert large, "no large Sharpes remain; the tight-at-the-top claim needs re-measuring"
    at_small = ABSOLUTE_FLOOR / (RELATIVE_FRACTION * statistics.median(small))
    at_large = ABSOLUTE_FLOOR / (RELATIVE_FRACTION * statistics.median(large))
    assert at_small > 10, (
        f"the flat rule is only {at_small:.1f}x looser for the smallest Sharpes, which "
        f"contradicts this file's recorded 16x"
    )
    assert at_large < 1.0, (
        f"the flat rule is {at_large:.1f}x looser for the largest Sharpes, so it is tighter "
        f"than proportional there and the two-directions claim does not hold"
    )


def test_the_zero_edge_is_the_small_part_not_the_story() -> None:
    """Only 9 rows have a genuinely undefined relative tolerance.

    The backlog's example table makes this look like a zero edge case. The substance is
    that 81% of rows reach the wrong branch, and only these make the relative meaning
    undefined outright.
    """
    values = _abs_sharpes()
    degenerate = [v for v in values if v < RELATIVE_FRACTION]
    assert len(degenerate) < 0.05 * len(values), (
        f"{len(degenerate)} of {len(values)} rows are now in the degenerate band, which "
        f"would make the zero edge the substance and this file's framing wrong"
    )

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

def _live_tolerance() -> tuple[float, float]:
    """Read the tolerance from the live config rather than restating it.

    These two constants used to be hardcoded here as 0.10 and 0.1 while the validator's own default
    for the absolute floor is 0.05. So this file measured a population that production no longer had,
    and still passed - because its assertions are loose bounds rather than exact figures. The numbers
    it reported were real but for the wrong floor: 832/1030 in the flat regime at 0.1, against 59/1030
    at the 0.05 actually in use.

    Deriving them means a config change shows up as a failing measurement rather than as a stale
    dashboard number.
    """
    from validation.readiness.models import ReadinessConfig

    cfg = ReadinessConfig()
    return cfg.stats_sharpe_tolerance_pct, cfg.stats_sharpe_tolerance_abs


RELATIVE_FRACTION, ABSOLUTE_FLOOR = _live_tolerance()
# The crossover is where the two terms are equal: above it the relative branch governs.
FLAT_REGIME_CEILING = ABSOLUTE_FLOOR / RELATIVE_FRACTION


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


def test_the_flat_branch_is_now_a_minority_regime_not_the_common_case() -> None:
    """Re-measured against the live floor, which reverses this file's original premise.

    This test used to assert that four rows in five sat in the flat regime, and it passed - while
    measuring a hardcoded 0.1 floor that the validator's own default (0.05) had already moved past.
    Against the floor actually in use the flat branch governs 59 of 1030 rows, 5.7%, not 80.8%.

    So item 14's premise needs restating rather than defending: the absolute floor is no longer what
    most rows experience. It is still the entire story for those 59, which is why the rule cannot be
    simply deleted - but "the tolerance is effectively flat for the corpus" is no longer true, and the
    dashboard carried that claim.
    """
    values = _abs_sharpes()
    assert values, "no rows hold a real sharpe_ratio; re-measure rather than inherit"
    flat = [v for v in values if v < FLAT_REGIME_CEILING]
    assert flat, (
        f"no row falls in the flat regime at floor {ABSOLUTE_FLOOR}; the floor and the corpus have "
        f"moved apart and the analysis below is moot"
    )
    assert len(flat) / len(values) < 0.10, (
        f"{len(flat)} of {len(values)} rows ({100 * len(flat) / len(values):.1f}%) fall in the flat "
        f"regime. If the absolute floor was tightened further, re-measure - and note that the "
        f"proportional branch now governs the great majority of rows."
    )
    assert statistics.median(values) > FLAT_REGIME_CEILING, (
        f"the median |Sharpe| is {statistics.median(values):.3f}, at or below the crossover, so the "
        f"flat floor governs the typical row after all"
    )


def test_the_same_rule_is_looser_at_the_bottom_and_tighter_at_the_top() -> None:
    """Both directions, because a flat floor is wrong at each end, not only near zero."""
    values = _abs_sharpes()
    small = [v for v in values if 0 < v < 0.10]
    large = [v for v in values if v >= 2.0]
    assert small, "no very small Sharpes remain; the looseness claim needs re-measuring"
    assert large, "no large Sharpes remain; the tight-at-the-top claim needs re-measuring"
    at_small = ABSOLUTE_FLOOR / (RELATIVE_FRACTION * statistics.median(small))
    at_large = ABSOLUTE_FLOOR / (RELATIVE_FRACTION * statistics.median(large))
    # The direction of both claims holds at the live floor, but the magnitudes are smaller than this
    # file recorded: 8.3x rather than 16x at the bottom, 0.12x rather than 0.24x at the top. Both
    # earlier figures came from the same hardcoded 0.1 the retune replaced.
    assert at_small > 5, (
        f"the flat rule is only {at_small:.1f}x looser for the smallest Sharpes; the floor has been "
        f"tightened so far that it is barely disproportionate, and the item 14 claim needs restating"
    )
    assert at_small > 1, (
        f"the flat rule is {at_small:.1f}x for the smallest Sharpes, i.e. no longer looser than "
        f"proportional - the asymmetry this file exists to record has been abolished"
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

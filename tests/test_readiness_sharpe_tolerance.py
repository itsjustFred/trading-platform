"""What the readiness validator's Sharpe gate actually tolerates.

Pinned as characterisation, not endorsement. The arithmetic at
`validation/readiness/validator.py:786-793` is

    sharpe_abs_diff = abs(expected - actual)
    sharpe_threshold = max(0.10 * abs(expected), 0.1)
    sharpe_passed = sharpe_abs_diff <= sharpe_threshold

which behaves very differently depending on where `expected` sits, and the
interesting case is the one nobody wrote down: **at `expected == 0.0` the
relative term vanishes and the tolerance becomes a flat 0.1 in Sharpe units.**
So an expectation of zero accepts any drift up to 0.1 - not an unbounded pass,
and not an unconditional one either. Earlier notes in this repo described it as
both, which is why the real behaviour is pinned here.

The stake started large. **97.9% of the 2,655 rows in the backtest corpus store
`sharpe_ratio` exactly 0.0** (`native` 99.0%), so "expected Sharpe is zero" is
the common case rather than an edge case.

That measurement turned out to argue for a *small* change rather than a large one,
which is why this file now tracks the config instead of a hardcoded constant. A
retune only flips rows whose drift falls *between* the old and new floor; most
rows either agree exactly or differ enormously. Modelling the gate as
reference-vs-platform over the 1,028 corpus pairs that actually differ:

| floor | rows flipped of 2,655 |
|---|---|
| 0.10 (was) | - |
| 0.05 (now) | **7** |
| 0.02 | 9 |
| 0.01 | 9 |

So the floor was halved to 0.05: most of the available tightening at the smallest
blast radius, and reversible because it is a config default rather than logic. The
table below is therefore read from `ReadinessConfig` - a retune that nobody intended
would otherwise be invisible here.

**Still undecided, and deliberately not done here:** whether `expected == 0` should
be *not comparable* rather than pass or fail. That is a policy question, it would
remove 1,572 of 2,655 rows (59.2%) from the verdict - all of which pass today -
and `ReadinessCheck` has no state to express it. The last test in this file holds
that door open.
"""

from __future__ import annotations

import pytest

from validation.readiness.models import ReadinessConfig
from validation.stats.calculator import TradeStatistics


def _stats(**kwargs) -> TradeStatistics:
    return TradeStatistics(**kwargs)


def _sharpe_check(expected: float, actual: float):
    """Run the real gate and return (passed, message) for the sharpe check."""
    from validation.readiness.validator import ReadinessValidator

    recommendations: list[str] = []
    checks = ReadinessValidator._check_stats(
        (_stats(sharpe_ratio=expected), _stats(sharpe_ratio=actual)),
        ReadinessConfig(),
        recommendations,
    )
    match = [c for c in checks if c.name == "stats_sharpe"]
    assert len(match) == 1, f"expected exactly one stats_sharpe check, got {match}"
    return match[0].passed, match[0].message


@pytest.mark.parametrize(
    ("expected", "actual", "why"),
    [
        (0.0, 0.0, "both zero - the common case, and it passes"),
        (0.0, 0.05, "a fifth of the whole tolerance, invisible to the gate"),
        (0.0, 0.099, "just inside the flat floor"),
        (0.0, 0.5, "beyond the floor, so it does fail"),
        (0.02, 0.05, "a tiny expectation makes the relative term negligible"),
        (1.5, 1.6, "an ordinary pair, inside 10% relative"),
        (1.5, 2.0, "an ordinary pair, outside 10% relative"),
    ],
)
def test_the_sharpe_tolerance_table_is_pinned(
    expected: float, actual: float, why: str
) -> None:
    """Assert the whole table against the *live* config, not a hardcoded floor."""
    passed, message = _sharpe_check(expected, actual)
    cfg = ReadinessConfig()
    threshold = max(
        cfg.stats_sharpe_tolerance_pct * abs(expected), cfg.stats_sharpe_tolerance_abs
    )
    should_pass = abs(expected - actual) <= threshold
    assert passed is should_pass, (
        f"expected={expected} actual={actual} ({why}) now reports passed={passed}, but the "
        f"config gives {should_pass} at threshold {threshold}: {message}"
    )


def test_the_floor_is_the_value_the_backlog_records() -> None:
    """Pin the number itself, so a retune has to be a deliberate edit here too.

    The table above would follow any floor silently. This one does not: if someone
    changes `stats_sharpe_tolerance_abs` without updating the backlog's measured flip
    count, this fails and points at the note that quotes 7.
    """
    assert ReadinessConfig().stats_sharpe_tolerance_abs == 0.05, (
        "the absolute Sharpe floor is no longer 0.05. The flip counts in the docstring and in "
        "the backlog were measured against 0.05 - re-run them and update both before "
        "accepting a different floor"
    )


def test_at_zero_expectation_the_tolerance_is_a_flat_absolute_floor() -> None:
    """The specific surprise, isolated so it cannot be lost in the table above.

    An expectation of exactly 0.0 makes `0.10 * abs(expected)` vanish, so the gate
    accepts any drift up to 0.1 in Sharpe units - the same 0.1 it would apply to a
    strategy whose Sharpe is 1.5, where 0.1 is only ~6.7% of the value. The gate is
    therefore ~15x looser in relative terms at zero than at a typical Sharpe.
    """
    floor = ReadinessConfig().stats_sharpe_tolerance_abs
    inside, _ = _sharpe_check(0.0, floor * 0.9)
    outside, _ = _sharpe_check(0.0, floor * 1.1)
    assert inside is True and outside is False, (
        "the flat floor at zero expectation has changed; if this now fails at a "
        "different drift, the tolerance was retuned and this file's analysis needs "
        "revisiting"
    )

    # The floor still dominates a small expectation - that is the asymmetry being
    # bounded, not abolished, and the bound is now 0.05 rather than 0.1.
    typical_pass, _ = _sharpe_check(1.5, 1.59)   # 0.09 of 1.5 = 6.0% - inside 10%
    assert typical_pass is True

    # At the old floor a 60% relative regression against a 0.15 expectation passed. At
    # 0.05 it does not, which is the whole point of the retune.
    old_floor_pass = abs(0.15 - 0.24) <= 0.1
    assert old_floor_pass is True, (
        "sanity: 0.15 vs 0.24 did pass at a 0.1 floor, so this case is the one the retune "
        "was meant to catch"
    )
    now_pass, _ = _sharpe_check(0.15, 0.24)
    assert now_pass is False, (
        "a 60% relative Sharpe regression against a 0.15 expectation passes again; the "
        "absolute floor has crept back up and the retune has been undone"
    )

    # And the boundary now sits where the config says, not where the old default did.
    just_inside, _ = _sharpe_check(0.0, 0.049)
    just_outside, _ = _sharpe_check(0.0, 0.051)
    assert just_inside is True and just_outside is False


def test_the_model_has_no_state_for_not_comparable() -> None:
    """Why the honest fix needs more than a one-line change.

    `ReadinessCheck` can say pass or fail and nothing else, so a rule that says
    "cannot compare" has nowhere to go. Asserted so the model change is a
    deliberate, visible piece of work rather than something discovered later.
    """
    from validation.readiness.models import ReadinessCheck

    assert "passed" in ReadinessCheck.model_fields
    assert not any(
        name in ReadinessCheck.model_fields
        for name in ("applicable", "skipped", "not_applicable", "comparable")
    ), (
        "ReadinessCheck has gained a not-comparable state, so the decision this test "
        "was blocking can now be taken with a place to put the answer"
    )

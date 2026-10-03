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

The stake is large. **97.9% of the 2,655 rows in the backtest corpus store
`sharpe_ratio` exactly 0.0** (`native` 99.0%), so "expected Sharpe is zero" is
the common case rather than an edge case, and any change to how it is treated
rewrites the readiness verdict for almost every row. That is why this is filed as
a decision rather than fixed: `ReadinessCheck` has only `passed: bool` and
`severity`, with no state for "cannot compare", so the honest answer needs a model
change and a policy for what it should say.
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
    """Assert the whole table, so a change to either tolerance shows up here."""
    passed, message = _sharpe_check(expected, actual)
    threshold = max(0.10 * abs(expected), 0.1)
    should_pass = abs(expected - actual) <= threshold
    assert passed is should_pass, (
        f"expected={expected} actual={actual} ({why}) now reports passed={passed}, but the "
        f"documented arithmetic gives {should_pass} at threshold {threshold}: {message}"
    )


def test_at_zero_expectation_the_tolerance_is_a_flat_absolute_floor() -> None:
    """The specific surprise, isolated so it cannot be lost in the table above.

    An expectation of exactly 0.0 makes `0.10 * abs(expected)` vanish, so the gate
    accepts any drift up to 0.1 in Sharpe units - the same 0.1 it would apply to a
    strategy whose Sharpe is 1.5, where 0.1 is only ~6.7% of the value. The gate is
    therefore ~15x looser in relative terms at zero than at a typical Sharpe.
    """
    inside, _ = _sharpe_check(0.0, 0.09)
    outside, _ = _sharpe_check(0.0, 0.11)
    assert inside is True and outside is False, (
        "the flat floor at zero expectation has changed; if this now fails at a "
        "different drift, the tolerance was retuned and this file's analysis needs "
        "revisiting"
    )

    # Same absolute drift, wildly different relative meaning.
    typical_pass, _ = _sharpe_check(1.5, 1.59)   # 0.09 of 1.5 = 6.0% - inside 10%
    assert typical_pass is True
    # 0.09 against an expectation of 0.15 is 60% relative, yet still inside the floor.
    small_exp_pass, _ = _sharpe_check(0.15, 0.24)
    assert small_exp_pass is True, (
        "a 60% relative Sharpe regression against a 0.15 expectation still passes, "
        "because the 0.1 absolute floor dominates. If that has become a failure the "
        "relative/absolute balance changed and the decision this pins has moved."
    )


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

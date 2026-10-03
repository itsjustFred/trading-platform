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


def test_the_not_comparable_state_exists_but_is_off_by_default() -> None:
    """Was `test_the_model_has_no_state_for_not_comparable`, and it did its job.

    That test asserted `ReadinessCheck` had nowhere to put "cannot compare", and it failed the
    moment the state landed - which is the point of writing it that way. The state is now
    `applicable: bool = True`, so it is inert until configured.

    What is pinned here is the *default*, because that is the part that changes behaviour if it
    moves: every existing check must remain applicable, so no verdict shifts by adding a field.
    """
    from validation.readiness.models import ReadinessCheck, ReadinessConfig

    assert "applicable" in ReadinessCheck.model_fields, (
        "ReadinessCheck has lost its not-comparable state; whatever decided to keep it has "
        "changed and the backlog entry needs revisiting"
    )
    assert ReadinessCheck(name="x", passed=True).applicable is True, (
        "a check is not applicable by default, which would silently change every verdict"
    )
    assert (
        ReadinessConfig().stats_sharpe_zero_expectation_is_not_comparable is False
    ), (
        "the zero-expectation policy is now ON by default. That would change what the platform "
        "certifies for 1,572 of 2,655 rows and is a decision, not a default - set it explicitly."
    )


def _sharpe_check_with(expected: float, actual: float, **cfg_kwargs):
    """The stats_sharpe check under an explicit config."""
    from validation.readiness.validator import ReadinessValidator

    checks = ReadinessValidator._check_stats(
        (_stats(sharpe_ratio=expected), _stats(sharpe_ratio=actual)),
        ReadinessConfig(**cfg_kwargs),
        [],
    )
    match = [c for c in checks if c.name == "stats_sharpe"]
    assert len(match) == 1
    return match[0]


def test_the_zero_expectation_policy_is_a_switch_and_defaults_to_todays_behaviour() -> None:
    """The open policy question, exposed as config rather than left in the arithmetic.

    Two things are being asserted, and the second is the important one. First, that the default
    leaves every verdict exactly as it was - this must be a no-op until someone decides. Second,
    that turning it on does what it says: the check stops being applicable at a zero expectation,
    so it is counted neither as a pass nor as a failure.

    Not enabled, deliberately. It would remove 1,572 of 2,655 rows (59.2%) from the verdict - all
    of which pass today - while 1,019 of the 1,083 genuinely comparable rows already fail. That is
    a change to what the platform certifies, not a bug fix.
    """
    # Default: unchanged, and applicable everywhere.
    for expected, actual, was_passing in ((0.0, 0.0, True), (0.0, 0.06, False), (1.5, 1.59, True)):
        check = _sharpe_check_with(expected, actual)
        assert check.passed is was_passing, (
            f"the default behaviour changed at expected={expected} actual={actual}"
        )
        assert check.applicable is True, (
            "a check is not applicable by default; that would silently change verdicts"
        )

    # Opt-in: not comparable at zero expectation, and untouched elsewhere.
    zero = _sharpe_check_with(0.0, 0.0, stats_sharpe_zero_expectation_is_not_comparable=True)
    assert zero.applicable is False, (
        "the opt-in policy is enabled but a zero expectation is still being compared"
    )
    assert zero.severity == "info", (
        f"a not-complicable check has severity {zero.severity!r}; it must not be an error, or "
        "opting in would fail rows rather than decline to judge them"
    )
    assert "not comparable" in zero.message.lower(), (
        f"the message does not say the comparison was declined: {zero.message!r}"
    )

    # Even a gross drift is declined rather than failed, when the expectation carries no scale.
    gross = _sharpe_check_with(0.0, 0.5, stats_sharpe_zero_expectation_is_not_comparable=True)
    assert gross.applicable is False, (
        "a 0.5 drift against a zero expectation was judged rather than declined"
    )

    # And a non-zero expectation is completely unaffected by the switch.
    for expected, actual, want in ((0.15, 0.20, False), (1.5, 1.59, True), (1.5, 2.0, False)):
        on = _sharpe_check_with(expected, actual, stats_sharpe_zero_expectation_is_not_comparable=True)
        off = _sharpe_check_with(expected, actual)
        assert on.passed is want and on.applicable is True, (
            f"the switch changed a verdict at expected={expected} where it must not apply"
        )
        assert (on.passed, on.severity) == (off.passed, off.severity), (
            f"the switch altered the comparison at expected={expected}"
        )


def test_a_not_applicable_check_is_counted_neither_as_error_nor_warning() -> None:
    """The aggregation has to honour `applicable`, or the state is decorative.

    A check that is not applicable has no answer, so counting it as a failure would fail rows the
    policy was written to decline to judge - the opposite of the intent. Counting it as a warning
    would report a problem where there is only an absence.

    Driven through `ReadinessValidator.validate` rather than a local re-implementation of the
    counting. An earlier version of this test rebuilt the sums itself, and a mutation that removed
    `applicable` from the real aggregation survived it - the copy was not the thing under test.
    """
    from validation.readiness.validator import ReadinessValidator

    def errors_when(expected: float, actual: float, **cfg_kwargs) -> tuple[int, int, bool]:
        report = ReadinessValidator.validate(
            stats=(_stats(sharpe_ratio=expected), _stats(sharpe_ratio=actual)),
            config=ReadinessConfig(**cfg_kwargs),
        )
        sharpe = next(c for c in report.checks if c.name == "stats_sharpe")
        return report.errors, report.warnings, sharpe.applicable

    # `validate()` also emits errors for the artifacts this test does not supply, so the counts
    # are compared as deltas rather than absolutes - what is under test is the Sharpe check's
    # contribution, not the whole report.
    baseline, _, _ = errors_when(0.0, 0.0)          # Sharpe passes, so no contribution
    assert baseline > 0, (
        "expected the unsupplied-artifact checks to produce errors; getting 0 means this test "
        "would pass for the wrong reason"
    )

    # Policy off, a 0.5 drift against a zero expectation is a real failure: exactly one more
    # error than the passing baseline.
    errors_off, _, applicable_off = errors_when(0.0, 0.5)
    assert applicable_off is True, "the check stopped being applicable with the policy off"
    assert errors_off == baseline + 1, (
        f"with the policy off, a 0.5 drift contributed {errors_off - baseline} errors, expected 1. "
        "The default behaviour must be untouched by the switch."
    )

    # Policy on: declined rather than judged, so it contributes nothing at all.
    errors_on, _, applicable_on = errors_when(
        0.0, 0.5, stats_sharpe_zero_expectation_is_not_comparable=True
    )
    assert applicable_on is False, "the check claims to be applicable while declining to judge"
    assert errors_on == baseline, (
        f"opting in left {errors_on - baseline} errors from a row the policy declines to judge, "
        "expected 0 - opting in must stop the check counting, not turn it into a warning"
    )

    # A non-zero expectation is an ordinary failure either way.
    errors_real, _, applicable_real = errors_when(
        1.5, 2.0, stats_sharpe_zero_expectation_is_not_comparable=True
    )
    assert applicable_real is True, (
        "the switch marked a comparable row not-applicable; it must only apply at zero"
    )
    assert errors_real == baseline + 1, (
        "an applicable failure stopped being counted once the switch existed"
    )

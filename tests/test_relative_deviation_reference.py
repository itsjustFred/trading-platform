"""Acceptance tests for #128: relative deviation is measured against the reference.

``max(|a|, |b|)`` as the denominator halved real drift (100 -> 200 read as
50%). The tolerance-gate sites (risk comparison, readiness validation and the
cross-platform metric deltas) must use the reference (ground-truth / expected)
magnitude, the same rule ``validation/readiness/validator.py`` already uses.
``validation/comparison/matcher.py`` keeps a symmetric denominator on purpose:
it ranks candidate pairings instead of gating a tolerance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from validation.comparison.tolerances import relative_difference
from validation.risk.comparison import _pct_diff
from validation.statistics.cross_platform import _metric_delta

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── validation/risk/comparison.py::_pct_diff(expected, actual) ──────────────


def test_pct_diff_double_drift_is_100_percent() -> None:
    assert _pct_diff(100.0, 200.0) == pytest.approx(1.0)


def test_pct_diff_uses_expected_as_base_not_the_larger_value() -> None:
    # expected 200, actual 100 -> 50% of the expected value
    assert _pct_diff(200.0, 100.0) == pytest.approx(0.5)


def test_pct_diff_sign_flip_is_200_percent() -> None:
    assert _pct_diff(-100.0, 100.0) == pytest.approx(2.0)
    assert _pct_diff(100.0, -100.0) == pytest.approx(2.0)


def test_pct_diff_zero_expected_fails_closed() -> None:
    assert _pct_diff(0.0, 0.0) == 0.0
    # No proportion exists for a zero base; any non-zero actual must not pass
    # a sub-100% tolerance.
    assert _pct_diff(0.0, 1e-9) >= 1.0
    assert _pct_diff(0.0, -5.0) >= 1.0


def test_pct_diff_matches_readiness_validator() -> None:
    from validation.readiness.validator import _abs_pct_diff

    for expected, actual in [(100.0, 200.0), (-100.0, 100.0), (0.0, 0.0), (0.0, 3.0), (50.0, 49.0)]:
        assert _pct_diff(expected, actual) == pytest.approx(_abs_pct_diff(expected, actual))


# ── validation/statistics/cross_platform.py::_metric_delta ──────────────────


def test_metric_delta_double_drift_is_100_percent() -> None:
    delta = _metric_delta("sharpe_ratio", 100.0, 200.0)
    assert delta.absolute_delta == pytest.approx(100.0)
    assert delta.relative_delta == pytest.approx(1.0)


def test_metric_delta_relative_is_signed_against_reference() -> None:
    delta = _metric_delta("sharpe_ratio", 200.0, 100.0)
    assert delta.absolute_delta == pytest.approx(-100.0)
    assert delta.relative_delta == pytest.approx(-0.5)


def test_metric_delta_negative_reference_keeps_zero_below_delta_positive() -> None:
    # Both values negative: -50 is above -100, so the drift away from the
    # reference is +0.5 of the reference magnitude, not -0.5.
    delta = _metric_delta("total_return", -100.0, -50.0)
    assert delta.absolute_delta == pytest.approx(50.0)
    assert delta.relative_delta == pytest.approx(0.5)


def test_metric_delta_negative_reference_keeps_zero_above_delta_negative() -> None:
    delta = _metric_delta("total_return", -100.0, -150.0)
    assert delta.absolute_delta == pytest.approx(-50.0)
    assert delta.relative_delta == pytest.approx(-0.5)


def test_metric_delta_sign_flip_is_200_percent() -> None:
    delta = _metric_delta("total_return", -100.0, 100.0)
    assert delta.absolute_delta == pytest.approx(200.0)
    assert abs(delta.relative_delta) == pytest.approx(2.0)


def test_metric_delta_zero_reference_has_undefined_relative() -> None:
    delta = _metric_delta("total_return", 0.0, 5.0)
    assert delta.absolute_delta == pytest.approx(5.0)
    assert delta.relative_delta is None
    assert delta.note  # explains why the relative value is undefined


def test_metric_delta_zero_reference_and_zero_platform_is_exact_match() -> None:
    delta = _metric_delta("total_return", 0.0, 0.0)
    assert delta.absolute_delta == 0.0
    assert delta.relative_delta == 0.0


def test_metric_delta_missing_value_is_unchanged() -> None:
    delta = _metric_delta("sharpe_ratio", None, 1.0)
    assert delta.absolute_delta is None and delta.relative_delta is None


# ── no remaining max(|a|,|b|) tolerance-gate denominators ────────────────────

# This is a spelling guard, not a semantic proof: it catches the literal
# ``max(abs(a), abs(b))`` denominator in any layout, including a call continued
# across lines. A rewritten denominator such as ``max(ref, act, 1e-12)`` -- the
# intentional symmetric match cost in ``validation/comparison/matcher.py``, which
# ranks pairings instead of gating a tolerance -- does not match, and cleaning it
# up is not what this test claims.
_MAX_ABS_DENOMINATOR = re.compile(r"max\(\s*abs\([^)]*\)\s*,\s*abs\(")


def _max_abs_denominator_lines(text: str) -> list[int]:
    """1-based line numbers of literal ``max(abs(a), abs(b))`` denominators."""
    return [
        text.count("\n", 0, match.start()) + 1
        for match in _MAX_ABS_DENOMINATOR.finditer(text)
    ]


def test_denominator_scan_flags_single_line_spelling() -> None:
    assert _max_abs_denominator_lines("denom = max(abs(a), abs(b))\n") == [1]


def test_denominator_scan_flags_multi_line_spelling() -> None:
    text = "result = (\n    max(\n        abs(reference),\n        abs(platform),\n    )\n)\n"
    assert _max_abs_denominator_lines(text) == [2]


def test_no_max_abs_relative_denominator_left_in_validation() -> None:
    offenders = []
    for path in (REPO_ROOT / "validation").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno in _max_abs_denominator_lines(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "tolerance-gate relative deviations must use the reference magnitude; "
        "max(abs(a), abs(b)) denominators remain at:\n" + "\n".join(offenders)
    )


def test_relative_difference_uses_reference_base() -> None:
    """`relative_difference(expected, actual)` divides by the expected value."""
    assert relative_difference(100.0, 200.0) == pytest.approx(1.0)
    assert relative_difference(10.0, 50.0) == pytest.approx(4.0)

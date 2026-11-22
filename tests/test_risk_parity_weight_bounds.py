"""Acceptance tests for #129: risk-parity weights always respect their bounds.

The SLSQP-failure fallback clipped inverse-vol weights and renormalised them,
which pushed weights back above ``max_weight``; an infeasible ``max_weight``
was silently relaxed to 1.0. Returned weights must sum to 1 and lie inside
``[min_weight, max_weight]`` on every path, and infeasible bounds raise.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import runner.risk_parity_optimizer as rpo
from runner.risk_parity_optimizer import (
    _project_to_capped_simplex,
    solve_risk_parity_weights,
)

TOL = 1e-9


def _assert_feasible(weights: np.ndarray, n: int, lower: float, upper: float) -> None:
    assert weights.shape == (n,)
    assert np.all(np.isfinite(weights))
    assert float(np.sum(weights)) == pytest.approx(1.0, abs=1e-9)
    assert np.all(weights >= lower - TOL), weights
    assert np.all(weights <= upper + TOL), weights


def _force_solver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_minimize(*_args, **_kwargs):
        return SimpleNamespace(success=False, x=None, message="forced failure")

    monkeypatch.setattr(rpo, "minimize", failing_minimize)


def _random_covariance(rng: np.random.Generator, n: int) -> np.ndarray:
    vols = rng.uniform(0.001, 0.2, size=n) * rng.choice([1.0, 10.0, 100.0], size=n)
    corr_root = rng.normal(size=(n, n))
    corr = corr_root @ corr_root.T
    d = np.sqrt(np.diag(corr))
    corr = corr / np.outer(d, d)
    return np.outer(vols, vols) * corr + np.eye(n) * 1e-10


def test_fallback_example_from_issue_respects_max_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    # inverse-vol weights would be [0.8, 0.1, 0.1]; cap is 0.4
    cov = np.diag([1.0 / 64.0, 1.0, 1.0])
    _force_solver_failure(monkeypatch)

    weights = solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.4)

    _assert_feasible(weights, 3, 0.0, 0.4)
    # Capped-simplex projection of [0.8, 0.1, 0.1]: the excess above the cap
    # goes to the uncapped assets (Euclidean and proportional agree here).
    np.testing.assert_allclose(weights, [0.4, 0.3, 0.3], atol=1e-9)


def test_fallback_respects_min_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    cov = np.diag([1.0 / 10_000.0, 1.0, 1.0, 1.0])  # inv-vol heavily on asset 0
    _force_solver_failure(monkeypatch)

    weights = solve_risk_parity_weights(cov, min_weight=0.15, max_weight=0.5)

    _assert_feasible(weights, 4, 0.15, 0.5)


@pytest.mark.parametrize("seed", range(40))
def test_property_bounds_hold_on_forced_fallback(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(2, 9))
    lower = float(rng.uniform(0.0, 1.0 / n))
    upper = float(rng.uniform(max(lower, 1.0 / n), 1.0))
    cov = _random_covariance(rng, n)
    _force_solver_failure(monkeypatch)

    weights = solve_risk_parity_weights(cov, min_weight=lower, max_weight=upper)

    _assert_feasible(weights, n, lower, upper)


@pytest.mark.parametrize("seed", range(40))
def test_property_bounds_hold_on_solver_path(seed: int) -> None:
    rng = np.random.default_rng(1000 + seed)
    n = int(rng.integers(2, 9))
    lower = float(rng.uniform(0.0, 1.0 / n))
    upper = float(rng.uniform(max(lower, 1.0 / n), 1.0))
    cov = _random_covariance(rng, n)

    weights = solve_risk_parity_weights(cov, min_weight=lower, max_weight=upper)

    _assert_feasible(weights, n, lower, upper)


def test_infeasible_max_weight_raises() -> None:
    cov = np.eye(3) * 0.01
    with pytest.raises(ValueError, match="max_weight"):
        solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.3)  # 3 * 0.3 < 1


def test_max_weight_exactly_feasible_gives_equal_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    cov = np.diag([1.0 / 64.0, 1.0, 1.0, 1.0])
    _force_solver_failure(monkeypatch)

    weights = solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.25)

    np.testing.assert_allclose(weights, [0.25] * 4, atol=1e-9)


def test_infeasible_min_weight_still_raises() -> None:
    cov = np.eye(3) * 0.01
    with pytest.raises(ValueError, match="min_weight"):
        solve_risk_parity_weights(cov, min_weight=0.4, max_weight=0.6)


@pytest.mark.parametrize("seed", range(40))
def test_projection_returns_a_point_of_the_capped_simplex(seed: int) -> None:
    """The projection itself: sum == 1 inside [lower, upper] on its own.

    The fallback example only showed the projection broke (a sum of 0), which
    does not point at the bisection. Assert the invariant directly.
    """
    rng = np.random.default_rng(50_000 + seed)
    n = int(rng.integers(2, 9))
    lower = float(rng.uniform(0.0, 1.0 / n))
    upper = float(rng.uniform(max(lower, 1.0 / n), 1.0))
    values = rng.normal(0.0, 5.0, size=n)

    projected = _project_to_capped_simplex(values, lower=lower, upper=upper)

    _assert_feasible(projected, n, lower, upper)


def test_bounds_that_cross_within_tolerance_raise() -> None:
    """min_weight above max_weight leaves the capped simplex empty.

    Both bound checks pass inside _FEASIBILITY_TOL (n=2, min=0.5+e,
    max=0.5-e), and np.clip with a_min > a_max returns a_max everywhere, so the
    projection would return weights summing to 1 - 2e: the documented
    "weights always sum to 1" invariant would be silently broken. Refuse the
    bounds instead of returning a portfolio that does not sum to 1.
    """
    cov = np.eye(2) * 0.01

    with pytest.raises(ValueError, match="min_weight"):
        solve_risk_parity_weights(
            cov, min_weight=0.5 + 1e-13, max_weight=0.5 - 1e-13
        )


@pytest.mark.parametrize(
    ("min_weight", "max_weight"),
    [
        (0.0, float("nan")),
        (float("nan"), 0.6),
        (0.0, float("inf")),
        (float("-inf"), 0.6),
    ],
)
def test_non_finite_bounds_are_refused_not_clamped(min_weight: float, max_weight: float) -> None:
    """A NaN cap must not become "no cap" (max(0.0, nan) -> 0.0, min(1.0, nan) -> 1.0).

    Fail closed with a clear error instead of solving with silently rewritten bounds.
    """
    cov = np.eye(3) * 0.01

    with pytest.raises(ValueError, match="finite"):
        solve_risk_parity_weights(cov, min_weight=min_weight, max_weight=max_weight)


def test_non_finite_covariance_is_refused() -> None:
    cov = np.eye(3) * 0.01
    cov[1, 1] = np.nan

    with pytest.raises(ValueError, match="finite"):
        solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.6)


def test_projection_refuses_non_finite_weights() -> None:
    """Bisection on NaN never converges: the projection must refuse, not return NaN."""
    values = np.array([0.5, np.nan, 0.5])

    # Match the input guard's own message: without the guard the post-projection
    # backstop also refuses, but with a different message, so a bare
    # match="finite" would pass on the wrong check.
    with pytest.raises(ValueError, match="finite before projection"):
        _project_to_capped_simplex(values, lower=0.0, upper=1.0)


def test_projection_refuses_a_result_that_does_not_sum_to_one() -> None:
    """Output backstop: a projection that cannot reach sum 1 must refuse.

    Bounds that cross (``lower > upper``) leave the capped simplex empty and
    ``np.clip`` returns ``a_max`` everywhere, so the projection here returns
    0.8 - weights that do not sum to 1. ``resolve_weight_bounds`` rejects such
    bounds before any real caller gets here; this pins the last line of defence,
    including the ``abs()`` that makes the check catch an undershoot as well as
    an overshoot.
    """
    with pytest.raises(ValueError, match="summing to 1"):
        _project_to_capped_simplex(np.array([0.5, 0.5]), lower=0.5, upper=0.4)


def test_projection_refuses_non_finite_bounds() -> None:
    """A NaN bound makes ``np.clip`` return NaN, and ``abs(nan - 1) > tol`` is False.

    The sum-to-1 comparison alone cannot see a NaN projection, which is why the
    finiteness half of the backstop stays even though callers validate bounds.
    """
    with pytest.raises(ValueError, match="summing to 1"):
        _project_to_capped_simplex(np.array([0.5, 0.5]), lower=0.0, upper=float("nan"))


@pytest.mark.parametrize("variance", [0.0, -1.0], ids=["zero", "negative"])
def test_module_fallback_refuses_non_positive_variance(
    monkeypatch: pytest.MonkeyPatch, variance: float
) -> None:
    """The runner's inverse-vol fallback must refuse what the script refuses.

    ``1.0 / sqrt(0)`` is inf and ``sqrt(-1)`` is NaN, so flooring instead of
    refusing returned inf/NaN weights (or a single asset pinned at
    ``max_weight``) with no error. Both backends now share one policy, so the
    same covariance cannot give one a refusal and the other a portfolio.
    """
    _force_solver_failure(monkeypatch)
    cov = np.diag([variance, 1.0, 1.0])

    with pytest.raises(ValueError, match="volatility"):
        solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.6)

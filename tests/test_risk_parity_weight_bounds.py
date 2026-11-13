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
from runner.risk_parity_optimizer import solve_risk_parity_weights

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
def test_property_bounds_hold_on_forced_fallback(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
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

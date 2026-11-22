"""#129: universe search skips universes whose bounds are infeasible for their size."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

import runner.risk_parity_optimizer as rpo
from runner.risk_parity_optimizer import select_best_risk_parity_universe


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "SPY": [0.01, 0.02, -0.01, 0.015, 0.004, 0.003],
            "TLT": [0.002, -0.001, 0.002, 0.001, 0.003, -0.002],
            "GLD": [0.004, 0.003, 0.001, -0.002, 0.002, 0.001],
        }
    )


def test_infeasible_universes_are_skipped_with_log(caplog: pytest.LogCaptureFixture) -> None:
    # max_weight=0.4: 2 assets (0.8 < 1) are infeasible, 3 assets are fine.
    with caplog.at_level(logging.WARNING, logger="runner.risk_parity_optimizer"):
        result = select_best_risk_parity_universe(
            _frame(),
            [["SPY", "TLT"], ["SPY", "TLT", "GLD"]],
            max_weight=0.4,
        )

    assert result["best_symbols"] == ["SPY", "TLT", "GLD"]
    assert len(result["evaluated_universes"]) == 1
    assert all(w <= 0.4 + 1e-9 for w in result["best_weights"].values())
    assert any("Skipping risk parity universe" in r.message for r in caplog.records)


def test_infeasible_universe_is_never_scored_and_is_reported(monkeypatch) -> None:
    """The skip itself: an infeasible universe must not reach the scorer."""
    calls: list[list[str]] = []
    real_score = rpo.score_risk_parity_universe

    def spy(symbols, returns, **kwargs):
        calls.append(list(symbols))
        return real_score(symbols, returns, **kwargs)

    monkeypatch.setattr(rpo, "score_risk_parity_universe", spy)
    result = select_best_risk_parity_universe(
        _frame(),
        [["SPY", "TLT"], ["SPY", "TLT", "GLD"]],
        max_weight=0.4,
    )

    assert calls == [["SPY", "TLT", "GLD"]]
    assert result["best_symbols"] == ["SPY", "TLT", "GLD"]
    # Skipped universes are reported, not silently absent from the payload.
    assert result["infeasible_skipped"] == 1
    assert len(result["evaluated_universes"]) == 1


def test_bounds_that_cross_are_skipped_not_solved(monkeypatch) -> None:
    """min_weight above max_weight (inside _FEASIBILITY_TOL) is infeasible on its own.

    Both bound counts pass for n=2 at min=0.5+e / max=0.5-e, so only the
    crossing makes the capped simplex empty. The universe is skipped like any
    other infeasible one rather than reaching the solver and failing the whole
    search from inside scipy.
    """
    calls: list[list[str]] = []
    real_score = rpo.score_risk_parity_universe

    def spy(symbols, returns, **kwargs):
        calls.append(list(symbols))
        return real_score(symbols, returns, **kwargs)

    monkeypatch.setattr(rpo, "score_risk_parity_universe", spy)

    with pytest.raises(ValueError, match="satisfies min_weight/max_weight"):
        select_best_risk_parity_universe(
            _frame(),
            [["SPY", "TLT"]],
            min_weight=0.5 + 1e-13,
            max_weight=0.5 - 1e-13,
        )

    assert calls == []


def test_all_universes_infeasible_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="satisfies min_weight/max_weight"):
        select_best_risk_parity_universe(_frame(), [["SPY", "TLT"]], max_weight=0.4)


def test_backtest_script_fallback_respects_max_weight(monkeypatch) -> None:
    """#129: the script's own solver had the same unbounded inverse-vol fallback."""
    from types import SimpleNamespace

    import numpy as np

    import scripts.run_risk_parity_backtest as script

    monkeypatch.setattr(
        script, "minimize", lambda *_a, **_k: SimpleNamespace(success=False, x=None)
    )
    cov = np.diag([1.0 / 64.0, 1.0, 1.0])
    weights = script.calculate_risk_parity_weights(cov, 0.0, 0.4, ["A", "B", "C"])

    assert sum(weights.values()) == __import__("pytest").approx(1.0)
    assert max(weights.values()) <= 0.4 + 1e-9


def test_backtest_script_rejects_infeasible_max_weight() -> None:
    import numpy as np
    import pytest

    import scripts.run_risk_parity_backtest as script

    with pytest.raises(ValueError, match="max_weight"):
        script.calculate_risk_parity_weights(np.eye(4), 0.0, 0.2, ["A", "B", "C", "D"])


def test_backtest_script_rejects_infeasible_min_weight() -> None:
    """The sibling guard: min_weight * n > 1 cannot satisfy sum(w) == 1.

    Without it SLSQP fails, the unbounded inverse-vol fallback runs and the
    projection silently returns a non-summing "weights" dict - a wrong-weight
    portfolio reported as computed rather than refused.
    """
    import numpy as np
    import pytest

    import scripts.run_risk_parity_backtest as script

    with pytest.raises(ValueError, match="min_weight"):
        script.calculate_risk_parity_weights(np.eye(4), 0.4, 0.6, ["A", "B", "C", "D"])


def test_backtest_script_clamps_bounds_before_the_solver(monkeypatch) -> None:
    """A negative min_weight / max_weight > 1 must not reach SLSQP or the projection.

    The module's solver clamps to the long-only simplex; the script's copy of
    the check did not, so out-of-range bounds entered the solver and the
    projection raw (the in-repo callers pass 0.02/0.30, so it stayed latent).
    """
    from types import SimpleNamespace

    import numpy as np

    import scripts.run_risk_parity_backtest as script

    solver_bounds: list[tuple[float, float]] = []
    projections: list[tuple[float, float]] = []
    real_projection = script._project_to_capped_simplex

    def fake_minimize(*_args, **kwargs):
        solver_bounds.extend(kwargs["bounds"])
        return SimpleNamespace(success=False, x=None)

    def spy_projection(values, *, lower, upper):
        projections.append((lower, upper))
        return real_projection(values, lower=lower, upper=upper)

    monkeypatch.setattr(script, "minimize", fake_minimize)
    monkeypatch.setattr(script, "_project_to_capped_simplex", spy_projection)

    weights = script.calculate_risk_parity_weights(np.eye(3), -0.25, 1.25, ["A", "B", "C"])

    assert solver_bounds == [(0.0, 1.0)] * 3
    assert projections == [(0.0, 1.0)]
    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(0.0 <= weight <= 1.0 for weight in weights.values())


def test_backtest_script_fallback_refuses_zero_volatility(monkeypatch) -> None:
    """Zero variance + solver failure: 1.0 / 0 = inf, inf / inf = NaN weights.

    The script must refuse with a clear error instead of returning NaN weights
    that do not sum to 1.
    """
    from types import SimpleNamespace

    import numpy as np
    import pytest

    import scripts.run_risk_parity_backtest as script

    monkeypatch.setattr(
        script, "minimize", lambda *_a, **_k: SimpleNamespace(success=False, x=None)
    )
    cov = np.diag([0.0, 1.0, 1.0])

    with pytest.raises(ValueError, match="volatility"):
        script.calculate_risk_parity_weights(cov, 0.0, 0.6, ["A", "B", "C"])


def test_backtest_script_fallback_refuses_non_finite_volatility(monkeypatch) -> None:
    """An inf variance is corrupt input, not a wide portfolio.

    ``1.0 / inf`` is 0.0, which silently drops the asset from a solver-failure
    fallback instead of surfacing the bad covariance; a NaN variance gives NaN
    weights. Refuse both with the volatility message rather than leaking them
    into the projection.
    """
    from types import SimpleNamespace

    import numpy as np
    import pytest

    import scripts.run_risk_parity_backtest as script

    monkeypatch.setattr(
        script, "minimize", lambda *_a, **_k: SimpleNamespace(success=False, x=None)
    )
    cov = np.diag([np.inf, 1.0, 1.0])

    with pytest.raises(ValueError, match="volatility"):
        script.calculate_risk_parity_weights(cov, 0.0, 0.6, ["A", "B", "C"])


def test_runner_and_backtest_script_agree_on_the_fallback(monkeypatch) -> None:
    """One inverse-vol policy for both backends (#129).

    The script refused a zero-variance asset while the runner floored the same
    variance at 1e-12 and returned capped weights, so identical input produced a
    hard error in one backend and a portfolio in the other. Both now raise.
    """
    from types import SimpleNamespace

    import numpy as np
    import pytest

    import runner.risk_parity_optimizer as optimizer
    import scripts.run_risk_parity_backtest as script

    failing = lambda *_a, **_k: SimpleNamespace(success=False, x=None)  # noqa: E731
    monkeypatch.setattr(script, "minimize", failing)
    monkeypatch.setattr(optimizer, "minimize", failing)
    cov = np.diag([0.0, 1.0, 1.0])

    with pytest.raises(ValueError, match="volatility"):
        script.calculate_risk_parity_weights(cov, 0.0, 0.6, ["A", "B", "C"])
    with pytest.raises(ValueError, match="volatility"):
        optimizer.solve_risk_parity_weights(cov, min_weight=0.0, max_weight=0.6)

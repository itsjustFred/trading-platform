import numpy as np
import pandas as pd
import pytest

from runner.risk_parity_optimizer import (
    candidate_universes,
    risk_contributions,
    select_best_risk_parity_universe,
    solve_risk_parity_weights,
)


def test_solve_risk_parity_weights_equalizes_risk_contributions() -> None:
    covariance = np.diag([0.04, 0.01, 0.0025])
    weights = solve_risk_parity_weights(covariance)
    contributions = risk_contributions(weights, covariance)

    assert weights.sum() == pytest.approx(1.0)
    assert np.all(weights >= 0.0)
    assert contributions.sum() == pytest.approx(1.0)
    assert np.max(contributions) - np.min(contributions) < 0.01


def test_select_best_risk_parity_universe_returns_diagnostics() -> None:
    frame = pd.DataFrame(
        {
            "SPY": [0.01, 0.02, -0.01, 0.015, 0.004],
            "TLT": [0.002, -0.001, 0.002, 0.001, 0.003],
            "GLD": [0.004, 0.003, 0.001, -0.002, 0.002],
        }
    )

    result = select_best_risk_parity_universe(frame, [["SPY", "TLT"], ["SPY", "GLD"]])

    assert set(result["best_symbols"]) in [{"SPY", "TLT"}, {"SPY", "GLD"}]
    assert sum(result["best_weights"].values()) == pytest.approx(1.0)
    assert result["evaluated_universes"]
    assert "best_risk_contributions" in result


def test_candidate_universes_are_deterministic_and_deduped() -> None:
    assert candidate_universes(["spy", "SPY", "tlt", "gld"], max_assets=2) == [
        ["SPY", "TLT"],
        ["SPY", "GLD"],
        ["TLT", "GLD"],
    ]

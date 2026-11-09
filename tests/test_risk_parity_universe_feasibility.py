"""#129: universe search skips universes whose bounds are infeasible for their size."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

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

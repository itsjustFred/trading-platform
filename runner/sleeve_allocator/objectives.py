"""Allocation objectives for the sleeve allocator (issue #307, phase 1).

Phase 1 ships the two REQUIRED baselines only: ``equal_weight`` (1/N) and
``inverse_vol``. ERC/HRP/CDaR land in later phases behind the same
``solve_weights(objective, sleeve_ids, window)`` interface.

All weights are fractions of ONE common risk budget: they are non-negative
and sum to 1 across the eligible sleeves (fully invested, no cash sleeve).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "BASELINE_OBJECTIVES",
    "SUPPORTED_OBJECTIVES",
    "solve_weights",
]

#: Objectives every report must contain, whatever else is requested.
BASELINE_OBJECTIVES: tuple[str, ...] = ("equal_weight", "inverse_vol")

#: Everything phase 1 can solve. Later phases extend this tuple.
SUPPORTED_OBJECTIVES: tuple[str, ...] = BASELINE_OBJECTIVES


def _inverse_volatility_weights(
    sleeve_ids: list[str],
    window: pd.DataFrame,
    *,
    min_window_observations: int,
) -> np.ndarray | None:
    """Weights proportional to 1/volatility over sleeves with enough data.

    A sleeve needs ``min_window_observations`` non-NaN daily returns inside the
    estimation window and a finite, positive sample std or it receives weight
    0 — a flat or unobserved sleeve must not soak up risk budget. Returns
    ``None`` when NO sleeve is estimable, so the caller can carry previous
    weights instead of publishing a meaningless allocation.
    """
    weights = np.zeros(len(sleeve_ids), dtype=float)
    for i, sleeve_id in enumerate(sleeve_ids):
        if sleeve_id not in window.columns:
            continue
        observed = window[sleeve_id].dropna().to_numpy(dtype=float)
        observed = observed[np.isfinite(observed)]
        if len(observed) < min_window_observations:
            continue
        vol = float(np.std(observed, ddof=1))
        if not np.isfinite(vol) or vol <= 0.0:
            continue
        weights[i] = 1.0 / vol
    total = float(weights.sum())
    if total <= 0.0 or not np.isfinite(total):
        return None
    return weights / total


def solve_weights(
    objective: str,
    sleeve_ids: list[str],
    window: pd.DataFrame,
    *,
    min_window_observations: int = 30,
) -> np.ndarray | None:
    """Solve portfolio weights for one objective over ``sleeve_ids``.

    ``window`` is the estimation sample (business-day returns, ``NaN`` =
    unobserved). Returns an array aligned with ``sleeve_ids`` summing to 1,
    or ``None`` when the objective cannot produce weights (the harness then
    carries the previous allocation forward). Raises ``ValueError`` for an
    unknown objective — a typo must not silently become a baseline.
    """
    ids = [str(s) for s in sleeve_ids]
    if not ids:
        return None
    if objective == "equal_weight":
        return np.full(len(ids), 1.0 / len(ids), dtype=float)
    if objective == "inverse_vol":
        return _inverse_volatility_weights(
            ids, window, min_window_observations=min_window_observations
        )
    raise ValueError(
        f"unknown sleeve allocator objective {objective!r}; "
        f"supported: {', '.join(SUPPORTED_OBJECTIVES)}"
    )

"""Risk-parity optimization primitives.

This module keeps covariance/risk-budget math separate from the generic
parameter optimizer. Risk parity is not a cartesian parameter grid: the useful
search surface is the asset universe plus portfolio risk contributions.
"""
from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

# #129: float slack for the bound-feasibility checks (n * bound vs 1.0).
_FEASIBILITY_TOL = 1e-12
# #129 H8: a resampled series whose median observation spacing is wider than this
# is not a daily series (weekly bars resample to one row per week), so the
# sqrt(252) annualisation does not apply to it.
_MAX_DAILY_SPACING_SECONDS = 1.5 * 24 * 60 * 60


@dataclass(frozen=True)
class RiskParitySelection:
    symbols: list[str]
    weights: dict[str, float]
    score: float
    # None when no daily annualisation applies: the observation frequency is
    # unknown (no datetime index) or coarser than daily (weekly bars). The
    # Sharpe is then undefined and the score excludes the Sharpe term.
    sharpe: float | None
    volatility: float
    diversification_ratio: float
    avg_abs_correlation: float
    risk_contributions: dict[str, float]


def returns_from_equity_matrix(equity_matrix: np.ndarray) -> np.ndarray:
    """Convert aligned equity curves into percentage returns."""
    if equity_matrix.size == 0 or equity_matrix.shape[0] < 2:
        columns = equity_matrix.shape[1] if equity_matrix.ndim == 2 else 0
        return np.array([]).reshape(0, columns)
    previous = np.maximum(equity_matrix[:-1], 1e-12)
    returns = np.diff(equity_matrix, axis=0) / previous
    return np.nan_to_num(returns, nan=0.0, posinf=0.0, neginf=0.0)


def _is_daily_spaced(index: pd.DatetimeIndex) -> bool:
    """Whether the median observation spacing is about one day.

    Median spacing (the same inference ``portfolio_optimizer`` uses for its
    annualisation factor) rather than a label, because only the spacing says
    what the annualisation factor should be. A trading-day series has mostly
    one-day gaps with occasional weekend/holiday gaps, so its median is one
    day; weekly bars give ~7 days and a monthly series ~30.
    """
    if len(index) < 2:
        # A single observation has no spacing to infer from.
        return False
    spacings = index.to_series().diff().dt.total_seconds().dropna()
    if spacings.empty:
        return False
    spacing = float(spacings.median())
    if not math.isfinite(spacing) or spacing <= 0:
        return False
    return spacing <= _MAX_DAILY_SPACING_SECONDS


def daily_returns_frame(returns_frame: pd.DataFrame) -> pd.DataFrame | None:
    """Resample per-bar returns to daily close-to-close returns.

    Sharpe is annualised with sqrt(252) across the platform
    (``runner/stats.py``, ``runner/portfolio_optimizer.py``), which is only
    valid for daily observations (#87, #129 H8). Per-bar returns inside a UTC
    day are compounded (``prod(1 + r) - 1``), which for an interior day is
    exactly the day's close-to-close return, because each bar return already
    spans the gap to the previous bar; the window's first day compounds from
    the first close inside the window (the preceding close is not in the
    frame). Days without any bar (weekends, holidays) are dropped, not
    zero-filled.

    Returns ``None`` when the caller must not apply the daily annualisation:
    the frame has no ``DatetimeIndex`` (frequency unknown), or the observations
    are spaced wider than a day - weekly bars resample to one row per week, and
    sqrt(252) would overstate their Sharpe by sqrt(252/52) ~ 2.2x. The caller
    then keeps its own frame and reports the Sharpe as undefined.
    """
    if not isinstance(returns_frame.index, pd.DatetimeIndex):
        return None
    growth = (1.0 + returns_frame.sort_index()).resample("1D")
    daily = growth.prod() - 1.0
    daily = daily[growth.size() > 0]
    if not _is_daily_spaced(daily.index):
        return None
    return daily


def shrink_covariance(returns: np.ndarray, *, shrinkage: float = 0.2) -> np.ndarray:
    """Return a positive semi-definite covariance estimate with diagonal shrinkage."""
    if returns.size == 0:
        return np.array([]).reshape(0, 0)
    matrix = np.asarray(returns, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("returns must be a 2D matrix")
    if matrix.shape[1] == 0:
        return np.array([]).reshape(0, 0)
    if matrix.shape[0] < 2:
        variances = np.var(matrix, axis=0) + 1e-10
        return np.diag(variances)

    cov = np.cov(matrix, rowvar=False)
    cov = np.atleast_2d(np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0))
    diag = np.diag(np.diag(cov))
    shrunk = ((1.0 - shrinkage) * cov) + (shrinkage * diag)
    jitter = np.eye(shrunk.shape[0]) * 1e-10
    return shrunk + jitter


def risk_contributions(weights: np.ndarray, covariance: np.ndarray) -> np.ndarray:
    """Return fractional portfolio variance contribution for each weight."""
    portfolio_variance = float(weights.T @ covariance @ weights)
    if portfolio_variance <= 0 or not math.isfinite(portfolio_variance):
        return np.full(len(weights), 1.0 / max(len(weights), 1))
    marginal = covariance @ weights
    contributions = weights * marginal / portfolio_variance
    contributions = np.nan_to_num(contributions, nan=0.0, posinf=0.0, neginf=0.0)
    if np.any(contributions < 0):
        contributions = np.clip(contributions, 0.0, None)
        total = float(np.sum(contributions))
        if total > 0:
            contributions = contributions / total
    return contributions


def clamp_weight_bounds(min_weight: float, max_weight: float) -> tuple[float, float]:
    """Clamp raw weight bounds onto the long-only simplex ``[0.0, 1.0]``.

    Risk parity is long-only, so a negative ``min_weight`` or a ``max_weight``
    above 1 is a caller mistake rather than a wider search: clamp it here so no
    caller can hand out-of-range bounds to SLSQP or to the simplex projection.
    """
    return max(0.0, float(min_weight)), min(1.0, float(max_weight))


def weight_bounds_feasible(lower: float, upper: float, n_assets: int) -> bool:
    """Whether a weight vector with ``sum(w) == 1`` exists inside the bounds.

    ``lower`` and ``upper`` must already be clamped (see
    :func:`clamp_weight_bounds`). ``upper >= lower`` is part of the condition:
    bounds that cross leave the capped simplex empty, and numpy's clip with
    ``a_min > a_max`` returns ``a_max`` everywhere - weights that sum to
    ``1 - (upper - lower) * n`` while looking bounded.
    """
    if n_assets < 1:
        return False
    return (
        lower * n_assets <= 1.0 + _FEASIBILITY_TOL
        and upper * n_assets >= 1.0 - _FEASIBILITY_TOL
        and upper >= lower
    )


def resolve_weight_bounds(
    min_weight: float, max_weight: float, n_assets: int
) -> tuple[float, float]:
    """Clamp the bounds onto the simplex and return them, or fail closed.

    The single place that decides whether a weight-bound pair can be solved at
    all, so the runner and the standalone backtest script cannot disagree
    about it (#129).
    """
    lower, upper = clamp_weight_bounds(min_weight, max_weight)
    if not weight_bounds_feasible(lower, upper, n_assets):
        if lower * n_assets > 1.0 + _FEASIBILITY_TOL:
            raise ValueError("min_weight is infeasible for the number of assets")
        if upper < lower:
            raise ValueError(
                "min_weight must not exceed max_weight: the bounds leave the "
                "capped simplex empty"
            )
        raise ValueError("max_weight is infeasible for the number of assets")
    return lower, upper


def _project_to_capped_simplex(values: np.ndarray, *, lower: float, upper: float) -> np.ndarray:
    """Euclidean projection onto {w : sum(w) == 1, lower <= w_i <= upper}.

    #129: clipping then renormalising can push weights back outside the cap
    (inverse-vol [0.8, 0.1, 0.1] capped at 0.4 renormalises to [0.667, ...]).
    The projection has the form clip(v_i - tau, lower, upper): its sum is
    continuous and non-increasing in tau, so bisection finds the unique tau
    whose sum is 1. The caller must have verified that the set is non-empty.
    """
    values = np.asarray(values, dtype=float)
    low = float(np.min(values)) - upper
    high = float(np.max(values)) - lower
    for _ in range(200):
        tau = 0.5 * (low + high)
        if tau == low or tau == high:
            break
        if float(np.sum(np.clip(values - tau, lower, upper))) > 1.0:
            low = tau
        else:
            high = tau
    return np.clip(values - 0.5 * (low + high), lower, upper)


def solve_risk_parity_weights(
    covariance: np.ndarray,
    *,
    min_weight: float = 0.0,
    max_weight: float = 0.6,
) -> np.ndarray:
    """Solve long-only weights whose risk contributions are as equal as possible.

    Raises ``ValueError`` when the bounds are infeasible for the asset count.
    """
    cov = np.asarray(covariance, dtype=float)
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1] or cov.shape[0] == 0:
        raise ValueError("covariance must be a non-empty square matrix")
    n_assets = cov.shape[0]
    # #129: fail closed on infeasible bounds instead of silently relaxing them.
    lower, upper = resolve_weight_bounds(min_weight, max_weight, n_assets)

    target = np.full(n_assets, 1.0 / n_assets)
    initial = np.full(n_assets, 1.0 / n_assets)

    def objective(weights: np.ndarray) -> float:
        rc = risk_contributions(weights, cov)
        return float(np.sum((rc - target) ** 2))

    result = minimize(
        objective,
        initial,
        method="SLSQP",
        bounds=[(lower, upper)] * n_assets,
        constraints=[{"type": "eq", "fun": lambda weights: np.sum(weights) - 1.0}],
        options={"maxiter": 500, "ftol": 1e-12, "disp": False},
    )
    if not result.success or result.x is None:
        inv_vol = 1.0 / np.sqrt(np.maximum(np.diag(cov), 1e-12))
        weights = inv_vol / np.sum(inv_vol)
    else:
        weights = np.asarray(result.x, dtype=float)

    # #129: project onto the capped simplex on every path so the returned
    # weights always sum to 1 inside [lower, upper].
    return _project_to_capped_simplex(weights, lower=lower, upper=upper)


def score_risk_parity_universe(
    symbols: list[str],
    returns: np.ndarray,
    *,
    min_weight: float = 0.0,
    max_weight: float = 0.6,
    daily: bool = False,
) -> RiskParitySelection:
    """Score one asset universe using covariance risk parity diagnostics.

    ``returns`` must already be DAILY close-to-close returns when ``daily`` is
    true (see :func:`daily_returns_frame`); only then is Sharpe annualised with
    sqrt(252). With ``daily=False`` no daily annualisation applies - the
    observation frequency is unknown (no datetime index) or coarser than daily
    (weekly bars) - so Sharpe is left undefined (``None``) and the score uses
    only the scale-invariant structure terms. Weights, diversification ratio,
    correlation and risk contributions do not depend on the return scale.
    """
    if len(symbols) < 2:
        raise ValueError("Risk parity requires at least two symbols")
    cov = shrink_covariance(returns)
    weights_array = solve_risk_parity_weights(cov, min_weight=min_weight, max_weight=max_weight)
    portfolio_returns = returns @ weights_array
    mean_return = float(np.mean(portfolio_returns)) if len(portfolio_returns) else 0.0
    volatility = float(np.std(portfolio_returns) + 1e-12)
    # #87 / #129 H8: annualized Sharpe on the daily convention (sqrt(252)),
    # consistent with runner/stats.py and runner/portfolio_optimizer.py. That
    # factor is only correct for daily returns, so it is applied only when the
    # caller says the input is daily; otherwise the Sharpe is undefined rather
    # than guessed (e.g. per-trade rows have no fixed observation frequency).
    sharpe: float | None
    if daily:
        sharpe = float(mean_return / volatility * math.sqrt(252)) if volatility > 0 else 0.0
    else:
        sharpe = None

    asset_vols = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    weighted_asset_vol = float(weights_array @ asset_vols)
    diversification_ratio = weighted_asset_vol / volatility if volatility > 0 else 0.0

    corr = np.corrcoef(returns, rowvar=False) if returns.shape[0] > 1 else np.eye(len(symbols))
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    if len(symbols) > 1:
        off_diagonal = (len(symbols) ** 2 - len(symbols))
        avg_abs_corr = float((np.sum(np.abs(corr)) - len(symbols)) / off_diagonal)
    else:
        avg_abs_corr = 0.0
    rc = risk_contributions(weights_array, cov)
    rc_error = float(np.sum((rc - (1.0 / len(symbols))) ** 2))
    # #87: heuristic universe-score weights (documented, not normalized):
    # 0.35 rewards diversification (ratio is ~1-2 scale), 0.5 penalizes
    # average absolute correlation (0-1 scale), 2.0 penalizes squared
    # risk-budget deviation (small scale, needs the larger multiplier to
    # bind). Tune explicitly if the selection over/under-weights one axis.
    score = (
        (sharpe or 0.0)
        + (0.35 * diversification_ratio)
        - (0.5 * avg_abs_corr)
        - (2.0 * rc_error)
    )
    weights = {symbol: float(weight) for symbol, weight in zip(symbols, weights_array)}
    risk = {symbol: float(value) for symbol, value in zip(symbols, rc)}
    return RiskParitySelection(
        symbols=list(symbols),
        weights=weights,
        score=float(score),
        sharpe=sharpe,
        volatility=volatility,
        diversification_ratio=float(diversification_ratio),
        avg_abs_correlation=avg_abs_corr,
        risk_contributions=risk,
    )


def candidate_universes(
    symbols: list[str],
    *,
    min_assets: int = 2,
    max_assets: int | None = None,
) -> list[list[str]]:
    """Generate deterministic symbol combinations for automated universe search."""
    normalized: list[str] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    if len(normalized) < min_assets:
        return []
    upper = min(len(normalized), int(max_assets or len(normalized)))
    universes: list[list[str]] = []
    for size in range(max(2, min_assets), upper + 1):
        universes.extend([list(combo) for combo in itertools.combinations(normalized, size)])
    return universes


def select_best_risk_parity_universe(
    returns_frame: pd.DataFrame,
    universes: list[list[str]] | None = None,
    *,
    min_assets: int = 2,
    max_assets: int | None = None,
    min_weight: float = 0.0,
    max_weight: float = 0.6,
) -> dict[str, Any]:
    """Select the highest scoring universe and return full diagnostics."""
    if returns_frame.empty or len(returns_frame.columns) < 2:
        raise ValueError("Need at least two return series for risk parity selection")
    configured = universes or candidate_universes(
        [str(column) for column in returns_frame.columns],
        min_assets=min_assets,
        max_assets=max_assets,
    )
    if not configured:
        raise ValueError("No valid risk parity universes were generated")

    selections: list[RiskParitySelection] = []
    infeasible_skipped = 0
    for universe in configured:
        columns = [symbol for symbol in universe if symbol in returns_frame.columns]
        if len(columns) < 2:
            continue
        # C3: intersection, not zero-fill — missing bars are not 0% returns.
        subset = returns_frame[columns].dropna()
        # #129 H8: covariance, Sharpe and score all use daily close-to-close
        # returns when the timestamps say the bars are daily (None => no
        # datetime index, or observations spaced wider than a day).
        daily_subset = daily_returns_frame(subset)
        if daily_subset is not None:
            subset = daily_subset
        if len(subset) < 2:
            continue
        # #129: bounds that are infeasible for this universe size (e.g.
        # max_weight=0.4 with 2 assets) must not abort the whole search; skip
        # that universe explicitly and say so (also in the returned payload).
        lower, upper = clamp_weight_bounds(min_weight, max_weight)
        if not weight_bounds_feasible(lower, upper, len(columns)):
            logger.warning(
                "Skipping risk parity universe %s: min_weight=%s / max_weight=%s "
                "are infeasible for %d assets",
                columns,
                min_weight,
                max_weight,
                len(columns),
            )
            infeasible_skipped += 1
            continue
        selections.append(
            score_risk_parity_universe(
                columns,
                subset.to_numpy(dtype=float),
                min_weight=min_weight,
                max_weight=max_weight,
                daily=daily_subset is not None,
            )
        )
    if not selections and infeasible_skipped:
        raise ValueError(
            "No risk parity universe satisfies min_weight/max_weight "
            f"({infeasible_skipped} skipped as infeasible for their asset count)"
        )
    if not selections:
        raise ValueError("No risk parity universe had enough return history")

    selections.sort(
        key=lambda item: (
            item.score,
            item.sharpe if item.sharpe is not None else 0.0,
            len(item.symbols),
        ),
        reverse=True,
    )
    best = selections[0]
    return {
        # "structure_only": no datetime index, or observations spaced wider
        # than a day (weekly bars) - in both cases the Sharpe is undefined and
        # excluded from the score (not comparable with Sharpe-based scores).
        "score_basis": (
            "sharpe_and_structure" if best.sharpe is not None else "structure_only"
        ),
        "best_symbols": best.symbols,
        "best_weights": best.weights,
        "best_score": best.score,
        "best_sharpe": best.sharpe,
        "best_volatility": best.volatility,
        "best_diversification_ratio": best.diversification_ratio,
        "best_avg_abs_correlation": best.avg_abs_correlation,
        "best_risk_contributions": best.risk_contributions,
        # Universes the bounds made infeasible for their asset count: they were
        # considered and skipped, so evaluated_universes alone under-reports the
        # search.
        "infeasible_skipped": infeasible_skipped,
        "evaluated_universes": [
            {
                "symbols": item.symbols,
                "score": item.score,
                "sharpe": item.sharpe,
                "volatility": item.volatility,
                "diversification_ratio": item.diversification_ratio,
                "avg_abs_correlation": item.avg_abs_correlation,
                "weights": item.weights,
                "risk_contributions": item.risk_contributions,
            }
            for item in selections
        ],
    }

"""Risk-parity optimization primitives.

This module keeps covariance/risk-budget math separate from the generic
parameter optimizer. Risk parity is not a cartesian parameter grid: the useful
search surface is the asset universe plus portfolio risk contributions.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize


@dataclass(frozen=True)
class RiskParitySelection:
    symbols: list[str]
    weights: dict[str, float]
    score: float
    sharpe: float
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


def solve_risk_parity_weights(
    covariance: np.ndarray,
    *,
    min_weight: float = 0.0,
    max_weight: float = 0.6,
) -> np.ndarray:
    """Solve long-only weights whose risk contributions are as equal as possible."""
    cov = np.asarray(covariance, dtype=float)
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1] or cov.shape[0] == 0:
        raise ValueError("covariance must be a non-empty square matrix")
    n_assets = cov.shape[0]
    lower = max(0.0, float(min_weight))
    upper = min(1.0, max(lower, float(max_weight)))
    if lower * n_assets > 1.0:
        raise ValueError("min_weight is infeasible for the number of assets")
    if upper * n_assets < 1.0:
        upper = 1.0

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

    weights = np.clip(weights, lower, upper)
    total = float(np.sum(weights))
    if total <= 0:
        return initial
    return weights / total


def score_risk_parity_universe(
    symbols: list[str],
    returns: np.ndarray,
    *,
    min_weight: float = 0.0,
    max_weight: float = 0.6,
) -> RiskParitySelection:
    """Score one asset universe using covariance risk parity diagnostics."""
    if len(symbols) < 2:
        raise ValueError("Risk parity requires at least two symbols")
    cov = shrink_covariance(returns)
    weights_array = solve_risk_parity_weights(cov, min_weight=min_weight, max_weight=max_weight)
    portfolio_returns = returns @ weights_array
    mean_return = float(np.mean(portfolio_returns)) if len(portfolio_returns) else 0.0
    volatility = float(np.std(portfolio_returns) + 1e-12)
    sharpe = float(mean_return / volatility) if volatility > 0 else 0.0

    asset_vols = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    weighted_asset_vol = float(weights_array @ asset_vols)
    diversification_ratio = weighted_asset_vol / volatility if volatility > 0 else 0.0

    corr = np.corrcoef(returns, rowvar=False) if returns.shape[0] > 1 else np.eye(len(symbols))
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    if len(symbols) > 1:
        avg_abs_corr = float((np.sum(np.abs(corr)) - len(symbols)) / (len(symbols) ** 2 - len(symbols)))
    else:
        avg_abs_corr = 0.0
    rc = risk_contributions(weights_array, cov)
    rc_error = float(np.sum((rc - (1.0 / len(symbols))) ** 2))
    score = sharpe + (0.35 * diversification_ratio) - (0.5 * avg_abs_corr) - (2.0 * rc_error)
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
    for universe in configured:
        columns = [symbol for symbol in universe if symbol in returns_frame.columns]
        if len(columns) < 2:
            continue
        subset = returns_frame[columns].dropna(how="all").fillna(0.0)
        if len(subset) < 2:
            continue
        selections.append(
            score_risk_parity_universe(
                columns,
                subset.to_numpy(dtype=float),
                min_weight=min_weight,
                max_weight=max_weight,
            )
        )
    if not selections:
        raise ValueError("No risk parity universe had enough return history")

    selections.sort(key=lambda item: (item.score, item.sharpe, len(item.symbols)), reverse=True)
    best = selections[0]
    return {
        "best_symbols": best.symbols,
        "best_weights": best.weights,
        "best_score": best.score,
        "best_sharpe": best.sharpe,
        "best_volatility": best.volatility,
        "best_diversification_ratio": best.diversification_ratio,
        "best_avg_abs_correlation": best.avg_abs_correlation,
        "best_risk_contributions": best.risk_contributions,
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

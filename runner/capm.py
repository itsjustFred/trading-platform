"""CAPM helper functions for strategy-vs-benchmark regression analysis."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
from scipy.stats import linregress

TRADING_DAYS_PER_YEAR = 252


def _normalize_return_series(series: pd.Series, *, name: str) -> pd.Series:
    if series is None:
        raise ValueError(f"{name} series is required")

    normalized = pd.to_numeric(series.copy(), errors="coerce")
    if normalized.empty:
        raise ValueError(f"{name} series is empty")

    if isinstance(normalized.index, pd.DatetimeIndex):
        dt_index = normalized.index
        if dt_index.tz is None:
            dt_index = dt_index.tz_localize("UTC")
        else:
            dt_index = dt_index.tz_convert("UTC")
    else:
        dt_index = pd.to_datetime(normalized.index, utc=True, errors="coerce")

    normalized.index = dt_index
    normalized = normalized.replace([math.inf, -math.inf], pd.NA).dropna()
    normalized = normalized[~normalized.index.isna()]
    normalized = normalized.sort_index()
    if normalized.empty:
        raise ValueError(f"{name} series has no valid numeric values")

    return normalized.groupby(level=0).last()


def align_return_series(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> pd.DataFrame:
    """Align strategy and benchmark return series on common timestamps."""
    strategy = _normalize_return_series(strategy_returns, name="strategy_returns")
    benchmark = _normalize_return_series(benchmark_returns, name="benchmark_returns")
    aligned = pd.concat(
        [
            strategy.rename("strategy_return"),
            benchmark.rename("benchmark_return"),
        ],
        axis=1,
    ).dropna()
    if len(aligned) < 2:
        raise ValueError("At least two aligned return observations are required for CAPM regression")
    return aligned


def calculate_capm(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict[str, Any]:
    """Compute CAPM regression metrics from aligned return series."""
    aligned = align_return_series(strategy_returns, benchmark_returns)
    regression = linregress(aligned["benchmark_return"], aligned["strategy_return"])
    alpha_daily = float(regression.intercept)
    beta = float(regression.slope)
    r_squared = float(regression.rvalue**2)
    p_value = float(regression.pvalue)

    return {
        "alpha": alpha_daily * TRADING_DAYS_PER_YEAR,
        "beta": beta,
        "r_squared": r_squared,
        "p_value": p_value,
    }

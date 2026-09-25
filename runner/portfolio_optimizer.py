"""
Portfolio Asset Allocation Optimizer with Bayesian Optimization.

Optimizes strategy weights for maximum Sharpe ratio with negative correlation (diversification).
Uses Optuna for Bayesian optimization with correlation penalty.
Supports Out-of-Sample (OOS) train/test split for walk-forward validation.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import optuna
import pandas as pd

from shared.atomic_io import write_text_atomically

logger = logging.getLogger(__name__)


@dataclass
class StrategyEquityPoint:
    """Equity point from a single strategy backtest."""
    time: int  # Unix timestamp in seconds
    equity: float


#: Annualization convention for the portfolio Sharpe (Cleanup 16-D1, F36):
#: daily equity curves are the documented default cadence, annualized with
#: 252 trading periods — the same convention as ``runner/stats.py``.
PORTFOLIO_PERIODS_PER_YEAR_DEFAULT = 252.0

#: Timestamps (as datetime-like objects) of the equity matrix most recently
#: prepared for Sharpe evaluation, used to infer the annualization cadence.
#: Module-level state because the Sharpe helper's numpy-only signature is
#: part of the module's (tested) surface.
_LAST_PORTFOLIO_TIMESTAMPS: list[Any] = []


def _record_portfolio_timestamps(curves: list[list[StrategyEquityPoint]], times: list[int]) -> None:
    """Remember the wall-clock timestamps matching the aligned equity matrix."""
    by_time: dict[int, list[Any]] = {}
    for curve in curves:
        for point in curve:
            by_time.setdefault(point.time, []).append(_parse_time_to_ts(point.time))
    _LAST_PORTFOLIO_TIMESTAMPS.clear()
    for timestamp in times:
        parsed = by_time.get(timestamp)
        if not parsed:
            continue
        _LAST_PORTFOLIO_TIMESTAMPS.append(
            pd.Timestamp(parsed[-1], unit="s", tz="UTC")
            if isinstance(parsed[-1], (int, float))
            else pd.Timestamp(parsed[-1])
        )


def _parse_time_to_ts(time_str: str | int) -> float:
    """Parse time string (ISO or Unix timestamp) to Unix timestamp (seconds)."""
    if not time_str:
        return 0.0
    # Try ISO format first
    try:
        from datetime import datetime
        if isinstance(time_str, str):
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        pass
    # Fall back to Unix timestamp as int or float
    try:
        return float(time_str)
    except (ValueError, TypeError):
        pass
    try:
        parsed_timestamp = pd.Timestamp(time_str)
        if not pd.isna(parsed_timestamp):
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.tz_localize("UTC")
            return parsed_timestamp.timestamp()
    except (ValueError, TypeError, OverflowError):
        pass
    return 0.0


def _split_equity_curves_by_time(
    curves: list[list[StrategyEquityPoint]],
    train_months: int | None,
    test_months: int | None,
) -> tuple[list[list[StrategyEquityPoint]], list[list[StrategyEquityPoint]]]:
    """Split equity curves into train (IS) and test (OOS) by time.

    Args:
        curves: List of equity curves for each strategy
        train_months: Number of months for training period (from start). Only
            applied when test_months is set; if None, use all data before the
            test period.
        test_months: Number of months for testing period (from end). If None, no OOS split.

    Returns:
        Tuple of (train_curves, test_curves). test_curves may be empty if test_months is None.
    """
    if not curves or test_months is None or test_months <= 0:
        return curves, []

    # Find overall time range across all curves
    all_timestamps: list[float] = []
    for curve in curves:
        for pt in curve:
            ts = _parse_time_to_ts(pt.time)
            all_timestamps.append(ts)

    if not all_timestamps:
        return curves, []

    min_ts = min(all_timestamps)
    max_ts = max(all_timestamps)

    # Calculate split points
    seconds_per_month = 30 * 24 * 60 * 60
    test_start_ts = max_ts - (test_months * seconds_per_month)

    # If train_months specified, limit training data to first N months
    if train_months is not None and train_months > 0:
        train_end_ts = min_ts + (train_months * seconds_per_month)
        # Ensure train period doesn't overlap with test period
        if train_end_ts >= test_start_ts:
            train_end_ts = test_start_ts
    else:
        train_end_ts = test_start_ts

    train_curves: list[list[StrategyEquityPoint]] = []
    test_curves: list[list[StrategyEquityPoint]] = []

    for curve in curves:
        train_curve = []
        test_curve = []
        for pt in curve:
            ts = _parse_time_to_ts(pt.time)
            if ts < train_end_ts:
                train_curve.append(pt)
            elif ts >= test_start_ts:
                test_curve.append(pt)
            # Points between train_end_ts and test_start_ts are excluded (gap)
        train_curves.append(train_curve)
        test_curves.append(test_curve)

    return train_curves, test_curves


def _load_strategy_equity(
    backtest_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[StrategyEquityPoint]:
    """Load an equity curve for a strategy backtest from SQLite (C3).

    Load errors surface with the backtest id attached instead of decaying
    into an empty curve and a late, misleading "need 2 curves" error.
    An id with genuinely no usable points still returns [] (caller decides).
    """
    try:
        from shared.database import load_backtest_result

        result = load_backtest_result(backtest_id)
    except Exception as exc:
        raise ValueError(
            f"Could not load equity curve for backtest {backtest_id!r}: {exc}"
        ) from exc
    if result is None:
        raise ValueError(f"Unknown backtest id {backtest_id!r}: no stored result")
    if not result.equity_curve:
        return []
    points = []
    start_ts = _parse_time_to_ts(start_date) if start_date else None
    end_ts = _parse_time_to_ts(end_date) if end_date else None
    if end_ts is not None and "T" not in str(end_date) and ":" not in str(end_date):
        end_ts += 86_400.0 - 1e-6
    for point in result.equity_curve:
        time_ts = _parse_time_to_ts(point.time)
        if start_ts is not None and time_ts < start_ts:
            continue
        if end_ts is not None and time_ts > end_ts:
            continue
        if math.isfinite(time_ts) and math.isfinite(float(point.equity)):
            points.append(
                StrategyEquityPoint(time=int(time_ts), equity=float(point.equity))
            )
    return points


def _normalize_weights(weights: np.ndarray) -> np.ndarray:
    """Normalize weights to sum to one, pruning degenerate trials (#87).

    An all-zero / non-finite weight vector carries no allocation information;
    silently promoting it to equal-weight let a degenerate trial become the
    best trial. Raise so the Optuna objective fails (and is caught) instead.
    """
    weights = np.asarray(weights, dtype=float)
    if weights.size == 0:
        return weights

    total = weights.sum()
    if not np.isfinite(total) or total <= 0:
        raise ValueError(
            "Degenerate weight vector (zero or non-finite sum) — trial pruned "
            "instead of falling back to equal-weight."
        )
    normalized = weights / total
    if not bool(np.all(np.isfinite(normalized))):
        raise ValueError("Non-finite normalized weights — trial pruned.")
    return normalized


def _common_timestamps(
    curves: list[list[StrategyEquityPoint]],
    require_overlap: bool,
) -> list[int]:
    """Return timestamps that are valid for all strategy lifetimes."""
    if require_overlap:
        common_times = {point.time for point in curves[0]}
        for curve in curves[1:]:
            common_times &= {point.time for point in curve}
        return sorted(common_times)

    window_start = max(min(point.time for point in curve) for curve in curves)
    window_end = min(max(point.time for point in curve) for curve in curves)
    if window_start > window_end:
        return []
    return sorted(
        {
            point.time
            for curve in curves
            for point in curve
            if window_start <= point.time <= window_end
        }
    )


def _aligned_curve_values(
    curve: list[StrategyEquityPoint],
    sorted_times: list[int],
    require_overlap: bool,
) -> list[float]:
    """Align one curve to prepared timestamps without crossing its lifetime."""
    equity_values = {point.time: point.equity for point in curve}
    if require_overlap:
        return [equity_values[timestamp] for timestamp in sorted_times]

    prior_times = [timestamp for timestamp in equity_values if timestamp <= sorted_times[0]]
    last_value = equity_values[max(prior_times)]
    aligned_values = []
    for timestamp in sorted_times:
        last_value = equity_values.get(timestamp, last_value)
        aligned_values.append(last_value)
    return aligned_values


def _align_equity_curves(
    curves: list[list[StrategyEquityPoint]],
    require_overlap: bool = False,
) -> np.ndarray:
    """Align equity curves to common timestamps, return as 2D array.

    Args:
        curves: List of equity curves for each strategy
        require_overlap: If True, only use timestamps present in ALL curves.
                         If False (default), use the union of timestamps within
                         the common covered time window, with forward-fill.
    """
    if not curves or any(not curve for curve in curves):
        return np.empty((0, 0))

    sorted_times = _common_timestamps(curves, require_overlap)
    if not sorted_times:
        return np.empty((0, len(curves)))

    aligned_columns = [
        _aligned_curve_values(curve, sorted_times, require_overlap)
        for curve in curves
    ]
    return np.array(aligned_columns, dtype=float).T


def _normalize_equity_matrix(equity_matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(equity_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return np.empty((0, 0))
    baseline = matrix[0]
    if (
        not np.all(np.isfinite(matrix))
        or not np.all(np.isfinite(baseline))
        or np.any(baseline <= 0)
    ):
        raise ValueError("equity matrix must be finite and have a positive first observation")
    normalized = matrix / baseline
    if not np.all(np.isfinite(normalized)) or np.any(normalized <= 0):
        raise ValueError("normalized equity matrix must be finite and positive")
    return normalized


def _period_percentage_returns(equity_matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(equity_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        return np.empty((0, matrix.shape[1] if matrix.ndim == 2 else 0))
    previous = matrix[:-1]
    current = matrix[1:]
    valid = np.isfinite(previous) & np.isfinite(current) & (previous > 0)
    returns = np.full((previous.shape[0], matrix.shape[1]), np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        np.divide(current - previous, previous, out=returns, where=valid)
    return returns[np.all(np.isfinite(returns), axis=1)]


def _compute_correlation_matrix(returns: np.ndarray) -> np.ndarray:
    """Compute correlation matrix of strategy returns."""
    if returns.shape[0] < 2 or returns.shape[1] < 2:
        return np.eye(returns.shape[1])
    corr = np.corrcoef(returns.T)
    return np.nan_to_num(corr, nan=0.0)


def _average_off_diagonal(correlation_matrix: np.ndarray) -> float:
    """Return the mean off-diagonal correlation, or zero for one strategy."""
    strategy_count = correlation_matrix.shape[0]
    if strategy_count <= 1:
        return 0.0

    off_diagonal_sum = correlation_matrix.sum() - np.trace(correlation_matrix)
    return float(off_diagonal_sum / (strategy_count * strategy_count - strategy_count))


def _infer_portfolio_periods_per_year(equity_matrix: np.ndarray) -> float:
    """Infer annualization periods from the median point spacing.

    Daily-spaced equity points → 252 trading periods per year (calendar 365
    spacing → 365); sub-daily spacing scales proportionally. Falls back to
    252 when the timestamps carry no usable cadence.
    """
    if equity_matrix.shape[0] < 3 or len(_LAST_PORTFOLIO_TIMESTAMPS) < 3:
        return PORTFOLIO_PERIODS_PER_YEAR_DEFAULT

    timestamps = pd.to_datetime(pd.Series(_LAST_PORTFOLIO_TIMESTAMPS), utc=True)
    if len(timestamps) != equity_matrix.shape[0]:
        return PORTFOLIO_PERIODS_PER_YEAR_DEFAULT

    spacings = timestamps.diff().dt.total_seconds().dropna()
    spacing = float(spacings.median()) if not spacings.empty else 0.0
    if not math.isfinite(spacing) or spacing <= 0:
        return PORTFOLIO_PERIODS_PER_YEAR_DEFAULT

    seconds_per_year = 365.25 * 24 * 60 * 60
    periods_per_year = seconds_per_year / spacing
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        return PORTFOLIO_PERIODS_PER_YEAR_DEFAULT

    # Equity curves are trading-day series in practice; a calendar-daily
    # cadence is mapped onto the 252 trading-day convention so the number is
    # comparable with stats.py's daily Sharpe.
    if 350.0 <= periods_per_year <= 380.0:
        return PORTFOLIO_PERIODS_PER_YEAR_DEFAULT
    # Coarser-than-daily cadences (weekly ≈ 52, biweekly ≈ 26, monthly ≈ 12)
    # are legitimate period counts and scale proportionally — sqrt(52) for a
    # weekly curve is the correct annualization, NOT 252 (CodeRabbit round:
    # the earlier framing suggested anything above ~8.7h should collapse to
    # 252, which would misstate weekly Sharpe by sqrt(252/52) ≈ 2.2x).
    # Sub-daily cadences scale too. No artificial ceiling: clamping an
    # intraday curve's period count (e.g. 2192 for 4-hourly points) would
    # understate the annualization relative to the daily-resampled
    # convention that stats.py reports, so the inferred count is used as is.
    return float(periods_per_year)


def _portfolio_sharpe_from_weights(
    weights: np.ndarray,
    equity_matrix: np.ndarray,
) -> tuple[float, float]:
    """Compute portfolio Sharpe and max drawdown from weights.

    Sharpe definition (Cleanup 16-D1, F36): per-observation mean excess
    return / standard deviation, ANNUALIZED by the square root of the
    number of observation periods per year (inferred from the equity-curve
    cadence, 252 for daily curves). The previous raw mean/std was not
    annualized and therefore not comparable with ``runner.stats``' Sharpe
    even though both surface under the same name.
    """
    if equity_matrix.size == 0 or equity_matrix.shape[0] < 2 or len(weights) == 0:
        return 0.0, 1.0

    equity_matrix = _normalize_equity_matrix(equity_matrix)
    portfolio_eq = equity_matrix @ weights
    if not np.all(np.isfinite(portfolio_eq)) or np.any(portfolio_eq <= 0):
        raise ValueError("portfolio equity must be finite and positive")
    returns = np.diff(portfolio_eq) / portfolio_eq[:-1]

    mean_ret = np.mean(returns)
    std_ret = np.std(returns)

    periods_per_year = _infer_portfolio_periods_per_year(equity_matrix)
    sharpe = (
        float(mean_ret / std_ret) * math.sqrt(periods_per_year)
        if std_ret > 0
        else 0.0
    )

    # Max drawdown
    peak = np.maximum.accumulate(portfolio_eq)
    drawdown = (peak - portfolio_eq) / peak
    max_dd = float(np.max(drawdown))

    return sharpe, max_dd


def _weighted_correlation_penalty(weights: np.ndarray, correlation_matrix: np.ndarray) -> float:
    """Per-trial diversification penalty (#87).

    The old constant (mean off-diagonal of the unweighted IS returns,
    subtracted from every trial) was invisible to the optimization. This is
    the weight-weighted off-diagonal mass ``w @ (C - I) @ w``: concentrated
    bets on correlated pairs pay more than diversified ones.
    """
    weights = np.asarray(weights, dtype=float)
    if weights.size == 0 or correlation_matrix.size == 0:
        return 0.0
    centered = np.asarray(correlation_matrix, dtype=float) - np.eye(weights.size)
    return float(weights @ centered @ weights)


def _build_objective(
    backtest_ids: list[str],
    equity_matrix: np.ndarray,
    returns: np.ndarray,
    target_sharpe: float = 0.0,
) -> Callable[[optuna.Trial], float]:
    """Build the Optuna objective for the prepared in-sample data."""
    correlation_matrix = _compute_correlation_matrix(returns)

    def objective(trial: optuna.Trial) -> float:
        weights_raw = [
            trial.suggest_float(f"weight_{index}", 0.0, 1.0)
            for index in range(len(backtest_ids))
        ]
        weights = _normalize_weights(np.array(weights_raw))
        sharpe, max_drawdown = _portfolio_sharpe_from_weights(weights, equity_matrix)
        # #87: per-trial weighted penalty — weight-dependent, so the
        # optimizer can actually see diversification.
        penalty = _weighted_correlation_penalty(weights, correlation_matrix)
        score = sharpe - (5.0 * penalty) - (
            3.0 * max(0, max_drawdown - 0.3)
        )
        score -= max(0.0, float(target_sharpe) - sharpe)

        trial.report(sharpe, trial.number)
        return -score

    return objective


def _run_optimization(
    objective: Callable[[optuna.Trial], float],
    n_trials: int,
    progress_callback: Callable[[float, str], None] | None,
) -> optuna.Study:
    """Run the requested trials and report progress in ten-trial increments."""
    if n_trials <= 0:
        raise ValueError("n_trials must be > 0")
    study = optuna.create_study(direction="minimize")
    for trial_index in range(n_trials):
        try:
            study.optimize(objective, n_trials=1, catch=())
        except ValueError as exc:
            logger.warning(
                "Portfolio optimization trial %d failed: %s",
                trial_index + 1,
                exc,
            )
        completed_trials = trial_index + 1
        if progress_callback and completed_trials % 10 == 0:
            progress_callback(
                completed_trials / n_trials * 100,
                f"Trial {completed_trials}/{n_trials}",
            )
    return study


def _build_oos_equity_curve(
    test_curves: list[list[StrategyEquityPoint]],
    best_weights: np.ndarray,
) -> list[dict[str, float | int]]:
    if not test_curves or len(test_curves) < 2:
        return []
    timestamps = _common_timestamps(test_curves, require_overlap=True)
    matrix = _align_equity_curves(test_curves, require_overlap=True)
    if matrix.size == 0 or matrix.shape[0] < 2 or matrix.shape[1] != len(best_weights):
        return []
    matrix = _normalize_equity_matrix(matrix)
    portfolio = matrix @ best_weights
    return [
        {"time": int(timestamp), "equity": float(equity)}
        for timestamp, equity in zip(timestamps, portfolio, strict=False)
        if math.isfinite(float(equity))
    ]


def _compute_oos_metrics(
    test_curves: list[list[StrategyEquityPoint]],
    best_weights: np.ndarray,
) -> tuple[float | None, float | None]:
    """Compute OOS metrics when at least two aligned curves are available."""
    if not test_curves:
        return None, None

    oos_equity_matrix = _align_equity_curves(test_curves, require_overlap=True)
    if (
        oos_equity_matrix.size == 0
        or oos_equity_matrix.shape[0] < 2
        or oos_equity_matrix.shape[1] < 2
    ):
        return None, None
    # Annualization cadence must come from the OOS timestamps themselves,
    # not the IS matrix recorded earlier.
    _record_portfolio_timestamps(
        test_curves, _common_timestamps(test_curves, require_overlap=True)
    )
    try:
        return _portfolio_sharpe_from_weights(best_weights, oos_equity_matrix)
    finally:
        _LAST_PORTFOLIO_TIMESTAMPS.clear()


def _build_optimization_result(
    backtest_ids: list[str],
    best_weights: np.ndarray,
    sharpe: float,
    max_drawdown: float,
    correlation_matrix: np.ndarray,
    n_trials: int,
    train_months: int | None,
    test_months: int | None,
    oos_metrics: tuple[float | None, float | None],
    target_sharpe: float = 0.0,
    oos_equity_curve: list[dict[str, float | int]] | None = None,
    train_start: str | None = None,
    train_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build the persisted result and its output path."""
    now = datetime.now(timezone.utc)
    optimization_id = (
        f"portfolio_{len(backtest_ids)}strat_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}"
    )
    output_path = OPTIMIZATIONS_DIR / f"{optimization_id}.json"
    result: dict[str, Any] = {
        "id": optimization_id,
        "file": output_path.name,
        "created_at": now.isoformat(),
        "backtest_ids": backtest_ids,
        "weights": {
            backtest_id: float(weight)
            for backtest_id, weight in zip(backtest_ids, best_weights)
        },
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "correlation_matrix": {
            backtest_ids[row]: {
                backtest_ids[column]: float(correlation_matrix[row, column])
                for column in range(len(backtest_ids))
            }
            for row in range(len(backtest_ids))
        },
        "n_trials": n_trials,
        "target_sharpe": float(target_sharpe),
        "target_met": bool(sharpe >= target_sharpe),
        "target_shortfall": max(0.0, float(target_sharpe) - float(sharpe)),
    }

    oos_sharpe, oos_max_drawdown = oos_metrics
    # #87: oos_status is always persisted — missing OOS must be
    # distinguishable from "not requested".
    oos_requested = test_months is not None or any(
        value is not None for value in (train_start, train_end, test_start, test_end)
    )
    if oos_sharpe is not None:
        oos_status = "available"
    elif oos_requested:
        oos_status = "insufficient_overlap"
    else:
        oos_status = "no_oos_requested"
    result["oos_status"] = oos_status
    if oos_sharpe is not None:
        result["oos_sharpe"] = oos_sharpe
        result["oos_max_drawdown"] = oos_max_drawdown
        result["oos_split"] = {
            "train_months": train_months,
            "test_months": test_months,
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
        }
        if oos_equity_curve:
            result["oos_equity_curve"] = oos_equity_curve
    return result, output_path


OPTIMIZATIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "optimizations"


def _explicit_boundary(value: str, *, is_end: bool = False) -> float:
    timestamp = _parse_time_to_ts(value)
    if timestamp <= 0:
        raise ValueError(f"Invalid portfolio boundary: {value!r}")
    raw = str(value).strip()
    numeric_boundary = isinstance(value, (int, float)) or raw.replace(".", "", 1).isdigit()
    if is_end and not numeric_boundary and "T" not in raw and ":" not in raw:
        timestamp += 86_400.0 - 1e-6
    return timestamp


def _split_equity_curves_by_boundaries(
    curves: list[list[StrategyEquityPoint]],
    *,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> tuple[list[list[StrategyEquityPoint]], list[list[StrategyEquityPoint]]]:
    train_start_ts = _explicit_boundary(train_start)
    train_end_ts = _explicit_boundary(train_end, is_end=True)
    test_start_ts = _explicit_boundary(test_start)
    test_end_ts = _explicit_boundary(test_end, is_end=True)
    if not train_start_ts < train_end_ts < test_start_ts <= test_end_ts:
        raise ValueError(
            "Explicit portfolio boundaries must be ordered train_start < train_end "
            "< test_start <= test_end"
        )

    train_curves: list[list[StrategyEquityPoint]] = []
    test_curves: list[list[StrategyEquityPoint]] = []
    for curve in curves:
        train_curve: list[StrategyEquityPoint] = []
        test_curve: list[StrategyEquityPoint] = []
        for point in curve:
            point_ts = _parse_time_to_ts(point.time)
            if train_start_ts <= point_ts <= train_end_ts:
                train_curve.append(point)
            if test_start_ts <= point_ts <= test_end_ts:
                test_curve.append(point)
        train_curves.append(train_curve)
        test_curves.append(test_curve)
    return train_curves, test_curves


def _persist_optimization_result(result: dict[str, Any], output_path: Path) -> None:
    """Persist a result only inside the configured optimization directory."""
    output_directory = OPTIMIZATIONS_DIR.resolve()
    validated_path = output_path.resolve()
    if validated_path.parent != output_directory:
        raise ValueError("Optimization output path must stay inside OPTIMIZATIONS_DIR")

    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / validated_path.name
    write_text_atomically(output_path, json.dumps(result, indent=2))


def optimize_portfolio_allocation(
    backtest_ids: list[str],
    target_sharpe: float = 1.5,
    n_trials: int = 100,
    progress_callback: Callable[[float, str], None] | None = None,
    train_months: int | None = None,
    test_months: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    train_start: str | None = None,
    train_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
) -> dict[str, Any]:
    """Optimize portfolio weights for maximum Sharpe with negative correlation.

    Args:
        backtest_ids: List of backtest IDs to combine
        target_sharpe: Target Sharpe ratio (for constraint)
        n_trials: Number of optimization trials
        progress_callback: Optional callback for progress updates
        train_months: Number of months for training period (from start). If None,
            use all data.
        test_months: Number of months for testing/OOS period (from end). If None,
            do not create an OOS split.

    Returns:
        Optimization result with best weights and stats. Includes both IS (in-sample)
        and OOS (out-of-sample) Sharpe when test_months is provided.
    """
    if isinstance(target_sharpe, bool):
        raise ValueError("target_sharpe must be numeric")
    try:
        target = float(target_sharpe)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_sharpe must be numeric") from exc
    if not math.isfinite(target) or target < 0:
        raise ValueError("target_sharpe must be finite and >= 0")

    # Load equity curves
    if start_date or end_date:
        curves = [
            _load_strategy_equity(bt_id, start_date=start_date, end_date=end_date)
            for bt_id in backtest_ids
        ]
    else:
        curves = [_load_strategy_equity(bt_id) for bt_id in backtest_ids]

    explicit_boundaries = (train_start, train_end, test_start, test_end)
    has_explicit_boundaries = all(value is not None for value in explicit_boundaries)
    if any(value is not None for value in explicit_boundaries) and not has_explicit_boundaries:
        raise ValueError("All explicit portfolio train/test boundaries are required")
    if has_explicit_boundaries:
        train_curves, test_curves = _split_equity_curves_by_boundaries(
            curves,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        )
    else:
        train_curves, test_curves = _split_equity_curves_by_time(
            curves, train_months, test_months
        )

    curves_to_optimize = train_curves if has_explicit_boundaries or train_curves else curves
    equity_matrix = _align_equity_curves(curves_to_optimize, require_overlap=True)

    # Weight sum guard: ensure we have valid, non-zero weight capacity
    if equity_matrix.size == 0 or equity_matrix.shape[1] < 2:
        return {
            "status": "error",
            "message": "Need at least 2 valid backtest equity curves with overlapping timestamps",
            "backtest_ids": backtest_ids,
        }

    equity_matrix = _normalize_equity_matrix(equity_matrix)

    # Overlap alignment validation: warn if insufficient common timestamps
    n_points = equity_matrix.shape[0]
    n_strategies = equity_matrix.shape[1]
    _record_portfolio_timestamps(
        curves_to_optimize,
        _common_timestamps(curves_to_optimize, require_overlap=True),
    )
    if n_points < 10:
        return {
            "status": "error",
            "message": (
                f"Insufficient overlap: only {n_points} common timestamps across "
                f"{n_strategies} strategies (need >= 10)"
            ),
            "backtest_ids": backtest_ids,
            "overlap_points": n_points,
        }

    returns = _period_percentage_returns(equity_matrix)
    objective = _build_objective(
        backtest_ids,
        equity_matrix,
        returns,
        target_sharpe=target,
    )
    study = _run_optimization(objective, n_trials, progress_callback)
    completed_trials = study.get_trials(
        deepcopy=False,
        states=(optuna.trial.TrialState.COMPLETE,),
    )
    if not completed_trials:
        return {
            "status": "error",
            "message": "No optimization trial completed successfully",
            "backtest_ids": backtest_ids,
        }

    best_raw = [
        study.best_trial.params[f"weight_{index}"]
        for index in range(len(backtest_ids))
    ]
    best_weights = _normalize_weights(np.array(best_raw))
    sharpe, max_drawdown = _portfolio_sharpe_from_weights(
        best_weights,
        equity_matrix,
    )
    correlation_matrix = _compute_correlation_matrix(returns)
    oos_metrics = _compute_oos_metrics(test_curves, best_weights)
    oos_equity_curve = _build_oos_equity_curve(test_curves, best_weights)
    result, output_path = _build_optimization_result(
        backtest_ids,
        best_weights,
        sharpe,
        max_drawdown,
        correlation_matrix,
        n_trials,
        train_months,
        test_months,
        oos_metrics,
        target_sharpe=target,
        oos_equity_curve=oos_equity_curve,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
    )

    _persist_optimization_result(result, output_path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-ids", nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument(
        "--train-months",
        type=int,
        default=None,
        help="Months of data for IS training",
    )
    parser.add_argument(
        "--test-months",
        type=int,
        default=None,
        help="Months of data for OOS testing",
    )
    args = parser.parse_args()

    result = optimize_portfolio_allocation(
        args.backtest_ids,
        n_trials=args.trials,
        train_months=args.train_months,
        test_months=args.test_months,
    )
    print(json.dumps(result, indent=2))

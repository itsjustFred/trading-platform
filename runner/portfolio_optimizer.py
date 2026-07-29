"""
Portfolio Asset Allocation Optimizer with Bayesian Optimization.

Optimizes strategy weights for maximum Sharpe ratio with negative correlation (diversification).
Uses Optuna for Bayesian optimization with correlation penalty.
Supports Out-of-Sample (OOS) train/test split for walk-forward validation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import optuna


@dataclass
class StrategyEquityPoint:
    """Equity point from a single strategy backtest."""
    time: int  # Unix timestamp in seconds
    equity: float


def _parse_time_to_ts(time_str: str | int) -> float:
    """Parse time string (ISO or Unix timestamp) to Unix timestamp (seconds)."""
    if not time_str:
        return 0.0
    # Try ISO format first
    try:
        from datetime import datetime
        if isinstance(time_str, str):
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        pass
    # Fall back to Unix timestamp as int or float
    try:
        return float(time_str)
    except (ValueError, TypeError):
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


def _load_strategy_equity(backtest_id: str) -> list[StrategyEquityPoint]:
    """Load an equity curve for a strategy backtest from SQLite."""
    try:
        from shared.database import load_backtest_result

        result = load_backtest_result(backtest_id)
        if result and result.equity_curve:
            points = []
            for point in result.equity_curve:
                time_ts = _parse_time_to_ts(point.time)
                if time_ts and point.equity:
                    points.append(
                        StrategyEquityPoint(time=int(time_ts), equity=float(point.equity))
                    )
            if points:
                return points
    except Exception:
        return []
    return []


def _normalize_weights(weights: np.ndarray) -> np.ndarray:
    """Normalize weights to sum to one with a finite equal-weight fallback."""
    weights = np.asarray(weights, dtype=float)
    if weights.size == 0:
        return weights

    total = weights.sum()
    if not np.isfinite(total) or total <= 0:
        return np.ones(weights.size) / weights.size
    return weights / total


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


def _portfolio_sharpe_from_weights(
    weights: np.ndarray,
    equity_matrix: np.ndarray,
) -> tuple[float, float]:
    """Compute portfolio Sharpe and max drawdown from weights."""
    if equity_matrix.size == 0 or equity_matrix.shape[0] < 2 or len(weights) == 0:
        return 0.0, 1.0

    # Weighted combination
    portfolio_eq = equity_matrix @ weights
    returns = np.diff(portfolio_eq) / (portfolio_eq[:-1] + 1e-10)

    mean_ret = np.mean(returns)
    std_ret = np.std(returns) + 1e-10

    sharpe = float(mean_ret / std_ret)  # Raw Sharpe

    # Max drawdown
    peak = np.maximum.accumulate(portfolio_eq)
    drawdown = (peak - portfolio_eq) / (peak + 1e-10)
    max_dd = float(np.max(drawdown))

    return sharpe, max_dd


def _build_objective(
    backtest_ids: list[str],
    equity_matrix: np.ndarray,
    returns: np.ndarray,
) -> Callable[[optuna.Trial], float]:
    """Build the Optuna objective for the prepared in-sample data."""
    correlation_penalty = _average_off_diagonal(_compute_correlation_matrix(returns))

    def objective(trial: optuna.Trial) -> float:
        weights_raw = [
            trial.suggest_float(f"weight_{index}", 0.0, 1.0)
            for index in range(len(backtest_ids))
        ]
        weights = _normalize_weights(np.array(weights_raw))
        sharpe, max_drawdown = _portfolio_sharpe_from_weights(weights, equity_matrix)
        score = sharpe - (5.0 * correlation_penalty) - (
            3.0 * max(0, max_drawdown - 0.3)
        )

        trial.report(sharpe, trial.number)
        return -score

    return objective


def _run_optimization(
    objective: Callable[[optuna.Trial], float],
    n_trials: int,
    progress_callback: Callable[[float, str], None] | None,
) -> optuna.Study:
    """Run the requested trials and report progress in ten-trial increments."""
    study = optuna.create_study(direction="minimize")
    for trial_index in range(n_trials):
        study.optimize(objective, n_trials=1, catch=(ValueError,))
        completed_trials = trial_index + 1
        if progress_callback and completed_trials % 10 == 0:
            progress_callback(
                completed_trials / n_trials * 100,
                f"Trial {completed_trials}/{n_trials}",
            )
    return study


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
    return _portfolio_sharpe_from_weights(best_weights, oos_equity_matrix)


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
) -> tuple[dict[str, Any], Path]:
    """Build the persisted result and its output path."""
    now = datetime.now(timezone.utc)
    optimization_id = (
        f"portfolio_{len(backtest_ids)}strat_{now.strftime('%Y%m%d_%H%M%S')}"
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
    }

    oos_sharpe, oos_max_drawdown = oos_metrics
    if oos_sharpe is not None:
        result["oos_sharpe"] = oos_sharpe
        result["oos_max_drawdown"] = oos_max_drawdown
        result["oos_split"] = {
            "train_months": train_months,
            "test_months": test_months,
        }
    return result, output_path


OPTIMIZATIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "optimizations"


def _persist_optimization_result(result: dict[str, Any], output_path: Path) -> None:
    """Persist a result only inside the configured optimization directory."""
    output_directory = OPTIMIZATIONS_DIR.resolve()
    validated_path = output_path.resolve()
    if validated_path.parent != output_directory:
        raise ValueError("Optimization output path must stay inside OPTIMIZATIONS_DIR")

    output_directory.mkdir(parents=True, exist_ok=True)
    validated_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def optimize_portfolio_allocation(
    backtest_ids: list[str],
    target_sharpe: float = 1.5,
    n_trials: int = 100,
    progress_callback: Callable[[float, str], None] | None = None,
    train_months: int | None = None,
    test_months: int | None = None,
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
    # Load equity curves
    curves = [_load_strategy_equity(bt_id) for bt_id in backtest_ids]

    # Split curves by time for OOS validation
    train_curves, test_curves = _split_equity_curves_by_time(curves, train_months, test_months)

    # Use train curves if available, otherwise all curves
    curves_to_optimize = train_curves if train_curves else curves
    equity_matrix = _align_equity_curves(curves_to_optimize, require_overlap=True)

    # Weight sum guard: ensure we have valid, non-zero weight capacity
    if equity_matrix.size == 0 or equity_matrix.shape[1] < 2:
        return {
            "status": "error",
            "message": "Need at least 2 valid backtest equity curves with overlapping timestamps",
            "backtest_ids": backtest_ids,
        }

    # Overlap alignment validation: warn if insufficient common timestamps
    n_points = equity_matrix.shape[0]
    n_strategies = equity_matrix.shape[1]
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

    returns = np.diff(equity_matrix, axis=0)
    objective = _build_objective(backtest_ids, equity_matrix, returns)
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

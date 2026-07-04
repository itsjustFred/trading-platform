"""
Portfolio Asset Allocation Optimizer with Bayesian Optimization.

Optimizes strategy weights for maximum Sharpe ratio with negative correlation (diversification).
Uses Optuna for Bayesian optimization with correlation penalty.
Supports Out-of-Sample (OOS) train/test split for walk-forward validation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import optuna
import numpy as np


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
        train_months: Currently unused - reserved for future walk-forward implementation.
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
    
    max_ts = max(all_timestamps)
    
    # Calculate split point for test period
    seconds_per_month = 30 * 24 * 60 * 60
    test_start_ts = max_ts - (test_months * seconds_per_month)
    
    train_curves: list[list[StrategyEquityPoint]] = []
    test_curves: list[list[StrategyEquityPoint]] = []
    
    for curve in curves:
        train_curve = []
        test_curve = []
        for pt in curve:
            ts = _parse_time_to_ts(pt.time)
            if ts < test_start_ts:
                train_curve.append(pt)
            else:
                test_curve.append(pt)
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


def _align_equity_curves(
    curves: list[list[StrategyEquityPoint]],
    require_overlap: bool = False,
) -> np.ndarray:
    """Align equity curves to common timestamps, return as 2D array.
    
    Args:
        curves: List of equity curves for each strategy
        require_overlap: If True, only use timestamps present in ALL curves (overlap window).
                         If False (default), use union of all timestamps with forward-fill.
    """
    if not curves:
        return np.array([]).reshape(0, 0)
    
    if require_overlap:
        # Find timestamps present in ALL curves (intersection)
        common_times = None
        for curve in curves:
            curve_times = {pt.time for pt in curve}
            if common_times is None:
                common_times = curve_times
            else:
                common_times &= curve_times
        sorted_times = sorted(common_times) if common_times else []
    else:
        # Find all unique timestamps across all curves (union)
        all_times = set()
        for curve in curves:
            for pt in curve:
                all_times.add(pt.time)
        sorted_times = sorted(all_times)
    
    n_points = len(sorted_times)
    n_strategies = len(curves)
    aligned = np.zeros((n_points, n_strategies))
    
    if n_points == 0:
        return aligned
    
    for col, curve in enumerate(curves):
        equity_values = {}
        for pt in curve:
            equity_values[pt.time] = pt.equity
        
        if require_overlap:
            # No forward-fill needed in overlap window - all timestamps exist
            for row, t in enumerate(sorted_times):
                aligned[row, col] = equity_values.get(t, 0.0)
        else:
            # Fill values, forward-fill missing values. Start from the first known
            # point for this strategy so missing leading days do not create a bogus
            # zero-equity row and an artificial return spike.
            last_val: float | None = None
            first_valid = next((pt.equity for pt in curve if pt.equity), None)
            if first_valid is None:
                first_valid = 0.0
            for row, t in enumerate(sorted_times):
                if t in equity_values:
                    last_val = equity_values[t]
                aligned[row, col] = first_valid if last_val is None else last_val
    
    return aligned


def _compute_correlation_matrix(returns: np.ndarray) -> np.ndarray:
    """Compute correlation matrix of strategy returns."""
    if returns.shape[0] < 2 or returns.shape[1] < 2:
        return np.eye(returns.shape[1])
    corr = np.corrcoef(returns.T)
    return np.nan_to_num(corr, nan=0.0)


def _portfolio_sharpe_from_weights(
    weights: np.ndarray,
    equity_matrix: np.ndarray,
) -> tuple[float, float]:
    """Compute portfolio Sharpe and max drawdown from weights."""
    if equity_matrix.size == 0 or len(weights) == 0:
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


OPTIMIZATIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "optimizations"


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
        train_months: Number of months for training period (from start). If None, use all data.
        test_months: Number of months for testing/OOS period (from end). If None, no OOS split.
    
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
            "message": f"Insufficient overlap: only {n_points} common timestamps across {n_strategies} strategies (need >= 10)",
            "backtest_ids": backtest_ids,
            "overlap_points": n_points,
        }

    # Compute returns for correlation
    returns = np.diff(equity_matrix, axis=0)

    def objective(trial: optuna.Trial) -> float:
            # Sample weights (softmax later)
            weights_raw = [
                trial.suggest_float(f"weight_{i}", 0.0, 1.0)
                for i in range(len(backtest_ids))
            ]
            # Normalize to sum=1
            weights = np.array(weights_raw)
            w_sum = weights.sum()
            if not np.isfinite(w_sum) or w_sum <= 0:
                # All weights are zero - trial should be pruned
                raise optuna.TrialPruned("All weights are zero")
            weights = weights / w_sum

            # Portfolio metrics
            sharpe, max_dd = _portfolio_sharpe_from_weights(weights, equity_matrix)

            # Correlation penalty: encourage negative correlation off-diagonal / minimize positive correlation
            corr_matrix = _compute_correlation_matrix(returns)
            avg_off_diag = 0.0
            n = corr_matrix.shape[0]
            if n > 1:
                off_diag_sum = corr_matrix.sum() - np.trace(corr_matrix)
                avg_off_diag = off_diag_sum / (n * n - n)

            # Objective: maximize Sharpe, minimize positive correlation, penalize excessive drawdown
            # Note: avg_off_diag > 0 means positive correlation (bad for diversification), so we SUBTRACT it
            score = sharpe - (5.0 * avg_off_diag) - (3.0 * max(0, max_dd - 0.3))

            trial.report(sharpe, trial.number)
            return -score  # Minimize negative score
    
    study = optuna.create_study(direction="minimize")
    
    for i in range(n_trials):
        study.optimize(objective, n_trials=1, catch=(ValueError,))
        if progress_callback and (i + 1) % 10 == 0:
            progress_callback((i + 1) / n_trials * 100, f"Trial {i + 1}/{n_trials}")
    
    # Extract best weights
    best_raw = [study.best_trial._params[f"weight_{i}"] for i in range(len(backtest_ids))]
    best_weights = np.array(best_raw)
    w_sum = best_weights.sum()
    if not np.isfinite(w_sum) or w_sum <= 0:
        best_weights = np.ones(len(backtest_ids)) / len(backtest_ids)
    else:
        best_weights = best_weights / w_sum

    sharpe, max_dd = _portfolio_sharpe_from_weights(best_weights, equity_matrix)
    corr_matrix = _compute_correlation_matrix(returns)

    # Compute OOS Sharpe if test curves available
    oos_sharpe: float | None = None
    oos_max_dd: float | None = None
    if test_curves:
        oos_equity_matrix = _align_equity_curves(test_curves, require_overlap=True)
        if oos_equity_matrix.size > 0 and oos_equity_matrix.shape[1] >= 2:
            oos_sharpe, oos_max_dd = _portfolio_sharpe_from_weights(best_weights, oos_equity_matrix)

    # Save optimization
    now = datetime.now(timezone.utc)
    opt_id = f"portfolio_{len(backtest_ids)}strat_{now.strftime('%Y%m%d_%H%M%S')}"
    output_path = OPTIMIZATIONS_DIR / f"{opt_id}.json"
    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    result: dict[str, Any] = {
        "id": opt_id,
        "file": output_path.name,
        "created_at": now.isoformat(),
        "backtest_ids": backtest_ids,
        "weights": {bt_id: float(w) for bt_id, w in zip(backtest_ids, best_weights)},
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "correlation_matrix": {
            backtest_ids[i]: {backtest_ids[j]: float(corr_matrix[i, j]) 
                      for j in range(len(backtest_ids))}
            for i in range(len(backtest_ids))
        },
        "n_trials": n_trials,
    }
    
    # Add OOS metrics if applicable
    if oos_sharpe is not None:
        result["oos_sharpe"] = oos_sharpe
        result["oos_max_drawdown"] = oos_max_dd
        result["oos_split"] = {
            "train_months": train_months,
            "test_months": test_months,
        }
    
    output_path.write_text(json.dumps(result, indent=2))
    
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-ids", nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--train-months", type=int, default=None, help="Months of data for IS training")
    parser.add_argument("--test-months", type=int, default=None, help="Months of data for OOS testing")
    args = parser.parse_args()
    
    result = optimize_portfolio_allocation(
        args.backtest_ids,
        n_trials=args.trials,
        train_months=args.train_months,
        test_months=args.test_months,
    )
    print(json.dumps(result, indent=2))

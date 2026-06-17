"""
Portfolio Asset Allocation Optimizer with Bayesian Optimization.

Optimizes strategy weights for maximum Sharpe ratio with negative correlation (diversification).
Uses Optuna for Bayesian optimization with correlation penalty.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import optuna
import numpy as np

from shared.constants import DATA_DIR
from shared.models import Portfolio, PortfolioComponent, PortfolioStats

OPTIMIZATIONS_DIR = DATA_DIR / "optimizations"


@dataclass
class StrategyEquityPoint:
    """Equity point from a single strategy backtest."""
    time: int
    equity: float


def _load_strategy_equity(backtest_id: str) -> list[StrategyEquityPoint]:
    """Load equity curve for a strategy backtest.
    
    Tries SQLite first, falls back to JSON for backward compatibility.
    """
    # Try SQLite first (single source of truth)
    try:
        from shared.database import init_db, get_session
        from sqlmodel import select
        from shared.database import BacktestRecord
        
        init_db()
        with get_session() as session:
            record = session.exec(
                select(BacktestRecord).where(BacktestRecord.id == backtest_id)
            ).first()
            if record and record.equity_curve:
                points = []
                for p in record.equity_curve:
                    if isinstance(p, dict):
                        if p.get("time") and p.get("equity"):
                            points.append(StrategyEquityPoint(
                                time=int(p.get("time", 0)),
                                equity=float(p.get("equity", 0))
                            ))
                    elif hasattr(p, "time") and hasattr(p, "equity"):
                        points.append(StrategyEquityPoint(
                            time=int(getattr(p, "time", 0)),
                            equity=float(getattr(p, "equity", 0))
                        ))
                if points:
                    return points
    except Exception:
        pass  # Fall through to JSON fallback
    
    # JSON fallback for backward compatibility
    path = Path(DATA_DIR) / "backtests" / f"{backtest_id}.trades.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        equity = data.get("equity_curve", [])
        return [
            StrategyEquityPoint(
                time=int(p.get("time", 0)),
                equity=float(p.get("equity", 0))
            )
            for p in equity
            if p.get("time") and p.get("equity")
        ]
    except (json.JSONDecodeError, KeyError, ValueError):
        return []


def _align_equity_curves(
    curves: list[list[StrategyEquityPoint]]
) -> np.ndarray:
    """Align equity curves to common timestamps, return as 2D array."""
    if not curves:
        return np.array([]).reshape(0, 0)
    
    # Find common time range
    all_times = set()
    time_to_idx: dict[int, int] = {}
    for curve in curves:
        for pt in curve:
            all_times.add(pt.time)
    sorted_times = sorted(all_times)
    for idx, t in enumerate(sorted_times):
        time_to_idx[t] = idx
    
    n_points = len(sorted_times)
    n_strategies = len(curves)
    aligned = np.zeros((n_points, n_strategies))
    
    for col, curve in enumerate(curves):
        equity_values = {}
        for pt in curve:
            equity_values[pt.time] = pt.equity
        
        # Fill values, forward-fill missing
        last_val = 0.0
        for row, t in enumerate(sorted_times):
            if t in equity_values:
                last_val = equity_values[t]
            aligned[row, col] = last_val
    
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
    
    sharpe = float(mean_ret / std_ret * np.sqrt(252 * 24 * 60))  # Approx annualization
    
    # Max drawdown
    peak = np.maximum.accumulate(portfolio_eq)
    drawdown = (peak - portfolio_eq) / (peak + 1e-10)
    max_dd = float(np.max(drawdown))
    
    return sharpe, max_dd


def optimize_portfolio_allocation(
    backtest_ids: list[str],
    target_sharpe: float = 1.5,
    n_trials: int = 100,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Optimize portfolio weights for maximum Sharpe with negative correlation.
    
    Args:
        backtest_ids: List of backtest IDs to combine
        target_sharpe: Target Sharpe ratio (for constraint)
        n_trials: Number of optimization trials
        
    Returns:
        Optimization result with best weights and stats
    """
    # Load equity curves
    curves = [_load_strategy_equity(bt_id) for bt_id in backtest_ids]
    equity_matrix = _align_equity_curves(curves)
    
    if equity_matrix.size == 0 or equity_matrix.shape[1] < 2:
        return {
            "status": "error",
            "message": "Need at least 2 valid backtest equity curves",
            "backtest_ids": backtest_ids,
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
        weights = weights / weights.sum()
        
        # Portfolio metrics
        sharpe, max_dd = _portfolio_sharpe_from_weights(weights, equity_matrix)
        
        # Correlation penalty: encourage negative correlation off-diagonal
        corr_matrix = _compute_correlation_matrix(returns)
        avg_off_diag = 0.0
        n = corr_matrix.shape[0]
        if n > 1:
            off_diag_sum = corr_matrix.sum() - np.trace(corr_matrix)
            avg_off_diag = off_diag_sum / (n * n - n)
        
        # Objective: maximize Sharpe, minimize positive correlation, maximize negative drawdown
        score = sharpe + (5.0 * avg_off_diag) - (3.0 * max(0, max_dd - 0.3))
        
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
    best_weights = best_weights / best_weights.sum()
    
    sharpe, max_dd = _portfolio_sharpe_from_weights(best_weights, equity_matrix)
    corr_matrix = _compute_correlation_matrix(returns)
    
    # Save optimization
    now = datetime.now(timezone.utc)
    opt_id = f"portfolio_{len(backtest_ids)}strat_{now.strftime('%Y%m%d_%H%M%S')}"
    output_path = OPTIMIZATIONS_DIR / f"{opt_id}.json"
    OPTIMIZATIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    result = {
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
    
    output_path.write_text(json.dumps(result, indent=2))
    
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-ids", nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=100)
    args = parser.parse_args()
    
    result = optimize_portfolio_allocation(args.backtest_ids, n_trials=args.trials)
    print(json.dumps(result, indent=2))
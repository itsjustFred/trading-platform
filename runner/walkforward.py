"""Walk-Forward validation runner.

Rollierendes Window: Train/Test Split für Anti-Overfitting.
Default: 12 Monate Train, 3 Monate Test, 1 Monat Step.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.portfolio_optimizer import optimize_portfolio_allocation
from shared.constants import DATA_DIR


def run_walkforward(
    backtest_ids: list[str],
    train_months: int = 12,
    test_months: int = 3,
    step_months: int = 1,
    n_trials: int = 100,
) -> dict[str, Any]:
    """Run walk-forward validation across time windows.
    
    Args:
        backtest_ids: List of backtest IDs to combine
        train_months: Months for training period
        test_months: Months for testing/OOS period  
        step_months: Step size in months for rolling window
        n_trials: Optimization trials per fold
        
    Returns:
        Aggregated walk-forward results
    """
    output_dir = DATA_DIR / "walkforward_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    fold_stats = []
    
    # For now: single fold with train/test split
    # Full implementation would iterate with step offset
    result = optimize_portfolio_allocation(
        backtest_ids=backtest_ids,
        n_trials=n_trials,
        train_months=train_months,
        test_months=test_months,
    )
    
    fold_stats.append({
        "fold": 1,
        "is_sharpe": result.get("sharpe"),
        "oos_sharpe": result.get("oos_sharpe"),
        "oos_max_drawdown": result.get("oos_max_drawdown"),
    })
    
    # Aggregate metrics
    oos_sharpes = [f["oos_sharpe"] for f in fold_stats if f["oos_sharpe"] is not None]
    
    payload = {
        "backtest_ids": backtest_ids,
        "config": {
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "n_trials": n_trials,
        },
        "folds": fold_stats,
        "summary": {
            "avg_oos_sharpe": sum(oos_sharpes) / len(oos_sharpes) if oos_sharpes else None,
            "n_profitable_folds": len([s for s in oos_sharpes if s and s > 0]),
            "profitable_fold_pct": len([s for s in oos_sharpes if s and s > 0]) / len(oos_sharpes) * 100 if oos_sharpes else 0,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    
    now = datetime.now(timezone.utc)
    out_path = output_dir / f"wf_{now.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward Portfolio Validation")
    parser.add_argument("--backtest-ids", nargs="+", required=True)
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--test-months", type=int, default=3)
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--trials", type=int, default=100)
    args = parser.parse_args()
    
    result = run_walkforward(
        backtest_ids=args.backtest_ids,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        n_trials=args.trials,
    )
    print(json.dumps(result, indent=2))
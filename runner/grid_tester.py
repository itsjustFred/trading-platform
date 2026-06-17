"""Grid Strategy Tester - Tests strategies across symbols/timeframes.

Generates a performance matrix to identify strategy strength/symbol combinations.
Saves results to SQLite for portfolio optimizer consumption.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from runner.cli import get_engine
from runner.engines.base import BacktestConfig
from shared.constants import DATA_DIR
from shared.database import save_result_to_sqlite
from shared.strategy_registry import find_strategy_entry

GRID_RESULTS_DIR = DATA_DIR / "grid_results"


def run_strategy_grid(
    strategy_slug: str,
    symbols: list[str],
    timeframes: list[str],
    start: str,
    end: str,
    engine_name: str = "vectorbt",
    capital: float = 100_000.0,
    risk_per_trade_pct: float = 0.01,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Run a strategy across all symbol/timeframe combinations.
    
    Returns a grid matrix showing which combinations work best.
    """
    entry = find_strategy_entry(strategy_slug)
    if entry is None:
        return {"status": "error", "message": f"Strategy {strategy_slug} not found"}
    
    engine = get_engine(engine_name)
    results_matrix: dict[str, dict[str, dict[str, Any]]] = {
        sym: {} for sym in symbols
    }
    
    total_runs = len(symbols) * len(timeframes)
    completed = 0
    
    for symbol in symbols:
        for timeframe in timeframes:
            completed += 1
            progress = completed / total_runs * 100
            msg = f"{symbol}/{timeframe}"
            if progress_callback:
                progress_callback(progress, msg)
            
            # Try to run backtest
            try:
                config = BacktestConfig(
                    strategy_name=strategy_slug,
                    symbols=[symbol],
                    timeframe=timeframe,
                    start_date=start,
                    end_date=end,
                    initial_capital=capital,
                    risk_per_trade_pct=risk_per_trade_pct,
                )
                result = engine.run(config)
                
                # Collect stats
                stats = result.stats if result.stats else {}
                results_matrix[symbol][timeframe] = {
                    "status": "success",
                    "sharpe": float(stats.get("sharpe", 0)),
                    "cagr": float(stats.get("cagr", 0)),
                    "max_drawdown_pct": float(stats.get("max_drawdown_pct", 0)),
                    "win_rate": float(stats.get("win_rate", 0)),
                    "total_trades": int(stats.get("total_trades", 0)),
                    "net_profit_pct": float(stats.get("net_profit_pct", 0)),
                }
            except Exception as e:
                results_matrix[symbol][timeframe] = {
                    "status": "error",
                    "error": str(e),
                }
    
    # Save grid results
    GRID_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    grid_id = f"grid_{strategy_slug}_{now.strftime('%Y%m%d_%H%M%S')}"
    output_path = GRID_RESULTS_DIR / f"{grid_id}.json"
    
    payload = {
        "id": grid_id,
        "strategy": strategy_slug,
        "created_at": now.isoformat(),
        "symbols": symbols,
        "timeframes": timeframes,
        "results": results_matrix,
        "engine": engine_name,
    }
    
    output_path.write_text(json.dumps(payload, indent=2))
    
    return payload


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--engine", default="vectorbt")
    parser.add_argument("--trials", type=int, default=100)
    args = parser.parse_args()
    
    result = run_strategy_grid(
        args.strategy,
        args.symbols,
        args.timeframes,
        args.start,
        args.end,
        engine_name=args.engine,
    )
    print(json.dumps(result, indent=2))
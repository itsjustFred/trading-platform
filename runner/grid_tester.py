"""Grid Strategy Tester - Tests strategies across symbols/timeframes.

Generates a performance matrix to identify strategy strength/symbol combinations.
Saves results to SQLite for portfolio optimizer consumption.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from runner.cli import get_engine
from runner.engines.base import BacktestConfig
from shared.constants import DATA_DIR
from shared.database import get_grid_results
from shared.strategy_registry import find_strategy_entry

GRID_RESULTS_DIR = DATA_DIR / "grid_results"


def load_grid_results(limit: int = 100) -> list[dict[str, Any]]:
    """Load persisted grid results from SQLite."""
    return get_grid_results(limit=limit)


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

    strategy_path = str(Path(entry.file_path).resolve())
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
                validate = getattr(engine, "validate_config", None)
                if callable(validate):
                    errors = validate(config)
                    if errors:
                        raise ValueError("; ".join(errors))
                result = engine.run(config, strategy_path)

                # Canonical stats keys only — never fabricate zero metrics for
                # missing keys, and keep no-trade runs distinguishable.
                stats = result.stats if result.stats else {}
                total_trades = int(stats.get("total_trades", 0) or 0)
                cell: dict[str, Any] = {
                    "status": "no_trades" if total_trades == 0 else "success",
                    "total_trades": total_trades,
                }
                for source_key, canonical_key in (
                    ("sharpe_ratio", "sharpe_ratio"),
                    ("sharpe", "sharpe_ratio"),
                    ("cagr", "cagr"),
                    ("max_drawdown_pct", "max_drawdown_pct"),
                    ("win_rate", "win_rate"),
                    ("total_return", "total_return"),
                ):
                    value = stats.get(source_key)
                    if value is None:
                        continue
                    numeric = float(value)
                    if not isfinite(numeric):
                        continue
                    cell[canonical_key] = numeric
                results_matrix[symbol][timeframe] = cell
            except Exception as e:
                results_matrix[symbol][timeframe] = {
                    "status": "error",
                    "error": str(e),
                }
    
    # Save grid results
    GRID_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # Use microseconds + UUID to avoid collisions in parallel runs
    grid_id = f"grid_{strategy_slug}_{now.strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}"
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

    # Also save to SQLite
    from shared.database import save_grid_result

    save_grid_result(
        grid_id=grid_id,
        strategy=strategy_slug,
        symbols=symbols,
        timeframes=timeframes,
        results=results_matrix,
        engine=engine_name,
    )

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

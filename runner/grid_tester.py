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

from runner.approval_gate import require_generated_strategy_approval
from runner.cli import get_engine
from runner.engines.base import BacktestConfig
from shared.constants import DATA_DIR
from shared.database import get_grid_results
from shared.strategy_registry import find_strategy_entry

GRID_RESULTS_DIR = DATA_DIR / "grid_results"

# Engine-provided stats keys mapped onto their canonical counterparts. The first
# match wins, so engine-native keys are surfaced under the canonical name (F40).
_CANONICAL_STAT_KEYS: tuple[tuple[str, str], ...] = (
    ("sharpe_ratio", "sharpe_ratio"),
    ("sharpe", "sharpe_ratio"),
    ("cagr", "cagr"),
    ("max_drawdown_pct", "max_drawdown_pct"),
    ("win_rate", "win_rate"),
    ("total_return", "total_return"),
)


def load_grid_results(limit: int = 100) -> list[dict[str, Any]]:
    """Load persisted grid results from SQLite."""
    return get_grid_results(limit=limit)


def _run_grid_cell(
    engine: Any,
    strategy_path: str,
    *,
    strategy_slug: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    capital: float,
    risk_per_trade_pct: float,
) -> dict[str, Any]:
    """Run a single grid cell and classify it as success / no_trades / error."""
    try:
        require_generated_strategy_approval(strategy_path)
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
    except Exception as exc:  # noqa: BLE001 — a failing cell must not abort the grid
        return {"status": "error", "error": str(exc)}

    return _cell_from_stats(
        result.stats if result.stats else {},
        has_trades=bool(result.trades),
        trade_count=len(result.trades),
    )


def _cell_from_stats(
    stats: dict[str, Any],
    *,
    has_trades: bool,
    trade_count: int,
) -> dict[str, Any]:
    """Build the cell payload from engine stats.

    Canonical stats keys only — never fabricate zero metrics for missing keys.
    A no-trade run is distinguishable from a success (and from an error), but
    only trades observed on the result decide that status: an engine that
    trades yet reports no stats must not be flattened into ``no_trades``
    (F40 — never fabricate a zero).
    """
    cell: dict[str, Any] = {
        "status": "no_trades" if not has_trades else "success",
        "total_trades": _total_trades_from(stats, has_trades, trade_count),
    }
    for source_key, canonical_key in _CANONICAL_STAT_KEYS:
        # First valid match wins: a later alias row (e.g. legacy "sharpe") must
        # not overwrite the canonical key already mapped from "sharpe_ratio".
        if canonical_key in cell:
            continue
        value = stats.get(source_key)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            # A non-numeric engine stat is skipped, not a grid-wide failure.
            continue
        if not isfinite(numeric):
            continue
        cell[canonical_key] = numeric
    return cell


def _total_trades_from(
    stats: dict[str, Any],
    has_trades: bool,
    trade_count: int,
) -> int | None:
    """Best-effort trade count from engine stats.

    A count present in the stats is surfaced as-is (an explicit zero for a
    no-trade run is observed, not fabricated). A missing or unparseable count
    falls back to the number of trades observed on the result; only when
    neither source knows is it ``None`` — never a fabricated zero or minimum.
    """
    raw = stats.get("total_trades")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    if has_trades:
        return trade_count
    return 0 if not stats else None


def _all_cells_failed(results_matrix: dict[str, dict[str, dict[str, Any]]]) -> bool:
    total_cells = sum(len(cells) for cells in results_matrix.values())
    error_cells = sum(
        1
        for cells in results_matrix.values()
        for cell in cells.values()
        if cell.get("status") == "error"
    )
    return bool(total_cells) and error_cells == total_cells


def _grid_payload(
    *,
    grid_id: str,
    strategy_slug: str,
    symbols: list[str],
    timeframes: list[str],
    results_matrix: dict[str, dict[str, dict[str, Any]]],
    engine_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Build the grid payload written to JSON, SQLite and the caller."""
    return {
        "id": grid_id,
        "strategy": strategy_slug,
        "created_at": now.isoformat(),
        "symbols": symbols,
        "timeframes": timeframes,
        "results": results_matrix,
        "engine": engine_name,
    }


def _persist_grid_result(payload: dict[str, Any]) -> None:
    """Write the grid payload to JSON and SQLite."""
    output_path = GRID_RESULTS_DIR / f"{payload['id']}.json"
    output_path.write_text(json.dumps(payload, indent=2))

    from shared.database import save_grid_result as _save_grid_result

    _save_grid_result(
        grid_id=payload["id"],
        strategy=payload["strategy"],
        symbols=payload["symbols"],
        timeframes=payload["timeframes"],
        results=payload["results"],
        engine=payload["engine"],
    )


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
            if progress_callback:
                progress_callback(completed / total_runs * 100, f"{symbol}/{timeframe}")

            results_matrix[symbol][timeframe] = _run_grid_cell(
                engine,
                strategy_path,
                strategy_slug=strategy_slug,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                capital=capital,
                risk_per_trade_pct=risk_per_trade_pct,
            )

    # A grid where every cell failed is a failed run, not a completed one:
    # never persist a successful-looking artifact for it (Decision Record).
    if _all_cells_failed(results_matrix):
        failed = sum(
            1
            for cells in results_matrix.values()
            for cell in cells.values()
            if cell.get("status") == "error"
        )
        return {
            "status": "error",
            "message": (
                f"All {failed} grid cells failed for {strategy_slug}; "
                "refusing to persist the all-error matrix as a completed run"
            ),
            "results": results_matrix,
            "engine": engine_name,
        }

    # Save grid results
    GRID_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # Use microseconds + UUID to avoid collisions in parallel runs
    grid_id = f"grid_{strategy_slug}_{now.strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}"
    payload = _grid_payload(
        grid_id=grid_id,
        strategy_slug=strategy_slug,
        symbols=symbols,
        timeframes=timeframes,
        results_matrix=results_matrix,
        engine_name=engine_name,
        now=now,
    )
    _persist_grid_result(payload)

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

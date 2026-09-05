#!/usr/bin/env python3
"""Run the backtest grid matrix across every market-data strategy.

This module wraps ``runner.backtest_grid.run_backtest_grid`` and produces:
- ``matrix_summary.json``: all strategy/cell results
- ``matrix_matrix.csv``: one row per strategy × symbol × timeframe cell
- ``matrix_daily_equity.csv``: daily equity and daily returns for every OK cell

The daily equity file is intended for downstream portfolio allocation / BayesOpt:
each row is a strategy cell at a calendar date with equity and pct return.

It also hosts the legacy single-strategy quick grid (``run_strategy_grid``),
which persists its matrix to SQLite for portfolio optimizer consumption.
``runner/backtest_grid.py`` remains the canonical grid execution engine; this
module is the multi-strategy orchestration/aggregation wrapper over it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from runner.backtest_grid import (
    GRID_SYMBOLS,
    GRID_TIMEFRAMES,
    build_run_id,
    get_engine,
    normalize_symbol,
    normalize_timeframe_value,
    parse_csv_list,
    parse_params,
    run_backtest_grid,
)
from shared.constants import DATA_DIR
from shared.database import save_grid_result
from shared.logging import get_logger
from shared.strategy_registry import (
    StrategyEntry,
    find_strategy_entry,
    iter_strategy_files,
    load_strategy_meta,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
)

logger = get_logger("runner.matrix")

MATRIX_OUTPUT_DIR = DATA_DIR / "backtests" / "matrix_runner"
GRID_RESULTS_DIR = DATA_DIR / "grid_results"


def load_grid_results(limit: int = 100) -> list[dict[str, Any]]:
    """Load persisted grid results from SQLite (patch seam for tests)."""
    from shared.database import get_grid_results

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

    engine = get_engine(engine_name)
    results_matrix: dict[str, dict[str, dict[str, Any]]] = {sym: {} for sym in symbols}

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
                from runner.engines.base import BacktestConfig

                config = BacktestConfig(
                    strategy_name=strategy_slug,
                    symbols=[symbol],
                    timeframe=timeframe,
                    start_date=start,
                    end_date=end,
                    initial_capital=capital,
                    risk_per_trade_pct=risk_per_trade_pct,
                )
                result = engine.run(config, str(entry.file_path))

                # Collect stats
                stats = result.stats if result.stats else {}

                # Explicit None metric values (engine produced no number) fall
                # back to 0 instead of crashing float() and flipping a valid
                # cell to "error" (CodeRabbit round on 16-D2). The sharpe key is
                # read under both spellings ('sharpe' and the canonical
                # 'sharpe_ratio' emitted by runner.stats.compute_stats_for_result)
                # so a populated engine result is never reported as 0.
                def _num(*keys: str, default: float = 0.0) -> float:
                    for key in keys:
                        value = stats.get(key)
                        if value is not None:
                            return float(value)
                    return default

                results_matrix[symbol][timeframe] = {
                    "status": "success",
                    "sharpe": _num("sharpe", "sharpe_ratio"),
                    "cagr": _num("cagr"),
                    "max_drawdown_pct": _num("max_drawdown_pct"),
                    "win_rate": _num("win_rate"),
                    "total_trades": int(_num("total_trades")),
                    "net_profit_pct": _num("net_profit_pct"),
                }
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

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Also save to SQLite
    save_grid_result(
        grid_id=grid_id,
        strategy=strategy_slug,
        symbols=symbols,
        timeframes=timeframes,
        results=results_matrix,
        engine=engine_name,
    )

    return payload


def _strategy_matches(entry, identifiers: Iterable[str] | None) -> bool:
    if not identifiers:
        return True
    needles = {_identifier_key(item) for item in identifiers if item}
    if not needles:
        return True
    aliases = {
        entry.slug,
        entry.relative_file,
        entry.file_path.name,
        entry.file_path.stem,
        entry.display_name,
    }
    return {_identifier_key(alias) for alias in aliases} & needles


def _identifier_key(value: str) -> str:
    return " ".join(ch.lower() if ch.isalnum() else " " for ch in str(value))


def _field(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return value


def _load_daily_rows(
    path: Path, *, strategy: str, symbol: str, timeframe: str
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    returns = payload.get("stats", {}).get("daily_returns", [])
    if not isinstance(returns, list):
        return []
    run_id = build_run_id(str(Path(strategy)), symbol, timeframe)
    rows: list[dict[str, Any]] = []
    for row in returns:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "run_id": run_id,
                "time": row.get("date", row.get("time", "")),
                "equity": row.get("equity", ""),
                "daily_return": row.get("return", ""),
            }
        )
    return rows


def _run_strategy_matrix_cell(
    *,
    entry: StrategyEntry,
    normalized_symbols: list[str],
    normalized_timeframes: list[str],
    start: str,
    end: str,
    engine: str,
    capital: float,
    risk_pct: float | None,
    max_position_pct: float | None,
    max_leverage: float | None,
    intrabar_resolution: str,
    params: dict[str, Any],
    output_dir: Path,
    data_dir: str,
    max_workers: int,
    timeout_seconds: int,
    skip_existing: bool,
    use_subprocess: bool,
    save_sqlite: bool,
    keep_indicators: bool,
) -> dict[str, Any]:
    strategy_engine = engine or entry.engine
    strategy_output_dir = output_dir / entry.slug
    strategy_output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Running matrix strategy=%s engine=%s", entry.slug, strategy_engine)
    summary = run_backtest_grid(
        strategy=str(entry.file_path),
        symbols=normalized_symbols,
        timeframes=normalized_timeframes,
        start=start,
        end=end,
        engine=strategy_engine,
        capital=capital,
        risk_pct=risk_pct,
        max_position_pct=max_position_pct,
        max_leverage=max_leverage,
        intrabar_resolution=intrabar_resolution,
        params=params,
        output_dir=strategy_output_dir,
        data_dir=data_dir,
        max_workers=max_workers,
        timeout_seconds=timeout_seconds,
        skip_existing=skip_existing,
        use_subprocess=use_subprocess,
        save_sqlite=save_sqlite,
        keep_indicators=keep_indicators,
    )
    (strategy_output_dir / "grid_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for row in summary.get("runs", []):
        enriched = {"strategy": entry.slug, "strategy_name": entry.display_name, **row}
        if enriched.get("backtest_id"):
            enriched["backtest_url"] = f"/backtests/{enriched['backtest_id']}"
            enriched["trades_url"] = f"/api/backtest/{enriched['backtest_id']}/trades"
        enriched["matrix_node_id"] = (
            f"{entry.slug}:{enriched.get('symbol', '')}:{enriched.get('timeframe', '')}"
        )
        rows.append(enriched)
        if row.get("status") == "ok" and row.get("output"):
            daily_rows.extend(
                _load_daily_rows(
                    Path(str(row["output"])),
                    strategy=entry.slug,
                    symbol=str(row.get("symbol", "")),
                    timeframe=str(row.get("timeframe", "")),
                )
            )
    return {
        "summary": summary,
        "rows": rows,
        "daily_rows": daily_rows,
    }


def write_matrix_csv(output_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = output_dir / "matrix_matrix.csv"
    fieldnames = [
        "strategy",
        "symbol",
        "timeframe",
        "status",
        "backtest_id",
        "trade_data_available",
        "total_trades",
        "total_pnl",
        "total_return",
        "net_profit_pct",
        "sharpe_ratio",
        "cagr",
        "max_drawdown_pct",
        "win_rate",
        "daily_return_count",
        "output",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _field(row.get(key)) for key in fieldnames})
    return path


def write_daily_equity_csv(output_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = output_dir / "matrix_daily_equity.csv"
    fieldnames = ["strategy", "symbol", "timeframe", "run_id", "time", "equity", "daily_return"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _field(row.get(key)) for key in fieldnames})
    return path


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_strategy_overview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        by_strategy.setdefault(strategy, []).append(row)

    overview: list[dict[str, Any]] = []
    for strategy, strategy_rows in by_strategy.items():
        ok_rows = [row for row in strategy_rows if row.get("status") == "ok"]
        sharpe_values = [_safe_float(row.get("sharpe_ratio")) for row in ok_rows]
        sharpe_values = [value for value in sharpe_values if value is not None]
        pnl_values = [_safe_float(row.get("total_pnl")) for row in ok_rows]
        pnl_values = [value for value in pnl_values if value is not None]
        trade_values = [_safe_float(row.get("total_trades")) for row in ok_rows]
        trade_values = [int(value) for value in trade_values if value is not None]
        return_values = [_safe_float(row.get("net_profit_pct")) for row in ok_rows]
        return_values = [value for value in return_values if value is not None]
        best_row = max(ok_rows, key=lambda row: _safe_float(row.get("sharpe_ratio")) or float("-inf"), default=None)
        overview.append(
            {
                "strategy": strategy,
                "strategy_name": ok_rows[0].get("strategy_name")
                if ok_rows
                else strategy_rows[0].get("strategy_name"),
                "ok_cells": len(ok_rows),
                "failed_cells": sum(1 for row in strategy_rows if row.get("status") == "failed"),
                "skipped_existing_cells": sum(
                    1 for row in strategy_rows if row.get("status") == "skipped_existing"
                ),
                "total_trades": sum(trade_values),
                "total_pnl": sum(pnl_values),
                "avg_sharpe": sum(sharpe_values) / len(sharpe_values) if sharpe_values else None,
                "best_sharpe": max(sharpe_values) if sharpe_values else None,
                "best_net_profit_pct": max(return_values) if return_values else None,
                "best_symbol": best_row.get("symbol") if best_row else None,
                "best_timeframe": best_row.get("timeframe") if best_row else None,
                "best_run_id": best_row.get("run_id") if best_row else None,
            }
        )
    overview.sort(key=lambda row: row.get("best_sharpe") or float("-inf"), reverse=True)
    return overview


def run_backtest_matrix(
    *,
    strategy_identifiers: list[str] | None = None,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    start: str = "2020-01-01",
    end: str = "",
    engine: str = "",
    capital: float = 100_000.0,
    risk_pct: float | None = 0.01,
    max_position_pct: float | None = None,
    max_leverage: float | None = None,
    intrabar_resolution: str = "open_close",
    params: dict[str, Any] | None = None,
    output_dir: Path | None = None,
    data_dir: str = "",
    max_workers: int = 1,
    strategy_workers: int | None = None,
    timeout_seconds: int = 1800,
    skip_existing: bool = False,
    use_subprocess: bool = False,
    save_sqlite: bool = True,
    keep_indicators: bool = False,
    strategies_dir: Path = Path("strategies"),
) -> dict[str, Any]:
    """Run every selected strategy over the symbol/timeframe grid."""
    output_dir = output_dir or MATRIX_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_symbols = [normalize_symbol(symbol) for symbol in (symbols or GRID_SYMBOLS)]
    normalized_timeframes = [
        normalize_timeframe_value(tf) for tf in (timeframes or GRID_TIMEFRAMES)
    ]
    params = dict(params or {})

    skipped_strategies: list[dict[str, Any]] = []
    entries: list[StrategyEntry] = []
    for path in iter_strategy_files(strategies_dir):
        slug = strategy_slug_for_path(path, strategies_dir)
        probe = StrategyEntry(
            slug=slug,
            file_path=path,
            relative_file=strategy_relative_file(path, strategies_dir),
            directory=path.parent,
            documentation_path=None,
            meta={},
        )
        if not _strategy_matches(probe, strategy_identifiers):
            continue
        try:
            meta = load_strategy_meta(path, strategies_dir)
        except Exception as exc:
            skipped_strategies.append(
                {"strategy": slug, "status": "skipped_unavailable", "error": str(exc)}
            )
            continue
        entries.append(
            StrategyEntry(
                slug=slug,
                file_path=path,
                relative_file=strategy_relative_file(path, strategies_dir),
                directory=path.parent,
                documentation_path=strategy_documentation_path(path, strategies_dir),
                meta=meta,
            )
        )
    if not entries:
        raise ValueError("No matching strategies found")

    all_rows: list[dict[str, Any]] = []
    all_daily_rows: list[dict[str, Any]] = []
    strategy_summaries: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc).isoformat()

    strategy_worker_count = strategy_workers or min(len(entries), max(1, os.cpu_count() or 1))
    strategy_worker_count = max(1, min(strategy_worker_count, len(entries)))
    strategy_tasks = [
        {
            "entry": entry,
            "normalized_symbols": normalized_symbols,
            "normalized_timeframes": normalized_timeframes,
            "start": start,
            "end": end,
            "engine": engine,
            "capital": capital,
            "risk_pct": risk_pct,
            "max_position_pct": max_position_pct,
            "max_leverage": max_leverage,
            "intrabar_resolution": intrabar_resolution,
            "params": params,
            "output_dir": output_dir,
            "data_dir": data_dir,
            "max_workers": max_workers,
            "timeout_seconds": timeout_seconds,
            "skip_existing": skip_existing,
            "use_subprocess": use_subprocess,
            "save_sqlite": save_sqlite,
            "keep_indicators": keep_indicators,
        }
        for entry in entries
    ]

    def handle_strategy_result(entry: StrategyEntry, result: dict[str, Any]) -> None:
        strategy_summaries.append(result["summary"])
        all_rows.extend(result["rows"])
        all_daily_rows.extend(result["daily_rows"])

    if strategy_worker_count > 1:
        with ThreadPoolExecutor(max_workers=strategy_worker_count) as executor:
            futures = {
                executor.submit(_run_strategy_matrix_cell, **task): task["entry"]
                for task in strategy_tasks
            }
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    handle_strategy_result(entry, future.result())
                except Exception as exc:
                    logger.exception("Matrix strategy failed: %s", entry.slug)
                    all_rows.append(
                        {
                            "strategy": entry.slug,
                            "strategy_name": entry.display_name,
                            "symbol": "",
                            "timeframe": "",
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
    else:
        for task in strategy_tasks:
            entry = task["entry"]
            assert isinstance(entry, StrategyEntry)
            try:
                handle_strategy_result(entry, _run_strategy_matrix_cell(**task))
            except Exception as exc:
                logger.exception("Matrix strategy failed: %s", entry.slug)
                all_rows.append(
                    {
                        "strategy": entry.slug,
                        "strategy_name": entry.display_name,
                        "symbol": "",
                        "timeframe": "",
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    all_rows.sort(
        key=lambda row: (row.get("strategy", ""), row.get("symbol", ""), row.get("timeframe", ""))
    )
    all_daily_rows.sort(
        key=lambda row: (
            row.get("strategy", ""),
            row.get("symbol", ""),
            row.get("timeframe", ""),
            row.get("time", ""),
        )
    )
    matrix_csv = write_matrix_csv(output_dir, all_rows)
    daily_csv = write_daily_equity_csv(output_dir, all_daily_rows)

    summary: dict[str, Any] = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "strategies": [entry.slug for entry in entries],
        "strategy_names": [entry.display_name for entry in entries],
        "skipped_strategies": skipped_strategies,
        "symbols": normalized_symbols,
        "timeframes": normalized_timeframes,
        "start": start,
        "end": end,
        "engine": engine or "auto",
        "capital": capital,
        "risk_pct": risk_pct,
        "max_position_pct": max_position_pct,
        "max_leverage": max_leverage,
        "intrabar_resolution": intrabar_resolution,
        "params": params,
        "output_dir": str(output_dir),
        "data_dir": data_dir,
        "max_workers": max_workers,
        "strategy_workers": strategy_worker_count,
        "timeout_seconds": timeout_seconds,
        "skip_existing": skip_existing,
        "use_subprocess": use_subprocess,
        "save_sqlite": save_sqlite,
        "requested_cells": len(entries) * len(normalized_symbols) * len(normalized_timeframes),
        "ok_cells": sum(1 for row in all_rows if row.get("status") == "ok"),
        "failed_cells": sum(1 for row in all_rows if row.get("status") == "failed"),
        "skipped_existing_cells": sum(
            1 for row in all_rows if row.get("status") == "skipped_existing"
        ),
        "daily_equity_rows": len(all_daily_rows),
        "matrix_csv": str(matrix_csv),
        "daily_equity_csv": str(daily_csv),
        "strategy_overview": build_strategy_overview(all_rows),
        "runs": all_rows,
        "strategy_summaries": strategy_summaries,
    }
    summary_path = output_dir / "matrix_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest grid matrix for all strategies")
    parser.add_argument(
        "--strategy",
        "-s",
        action="append",
        default=[],
        help="Strategy slug/name/path to include; repeatable. Default: all.",
    )
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--symbols", default=",".join(GRID_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument(
        "--timeframes", default=",".join(GRID_TIMEFRAMES), help="Comma-separated timeframes"
    )
    parser.add_argument("--start", default="2020-01-01", help="Start date")
    parser.add_argument("--end", default="", help="End date, empty lets runner.cli auto-detect")
    parser.add_argument(
        "--engine", default="", help="Override engine. Empty uses each strategy's detected engine."
    )
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial capital")
    parser.add_argument("--risk-pct", type=float, default=0.01, help="Risk per trade, e.g. 0.01")
    parser.add_argument("--max-position-pct", type=float, default=None)
    parser.add_argument("--max-leverage", type=float, default=None)
    parser.add_argument("--intrabar-resolution", default="open_close")
    parser.add_argument("--params", default="", help="Strategy params JSON")
    parser.add_argument("--output-dir", type=Path, default=MATRIX_OUTPUT_DIR)
    parser.add_argument("--data-dir", default="", help="Override data directory")
    parser.add_argument(
        "--max-workers", type=int, default=1, help="Parallel backtest cells per strategy"
    )
    parser.add_argument(
        "--strategy-workers",
        type=int,
        default=None,
        help="Parallel strategies in the matrix runner",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per cell")
    parser.add_argument(
        "--resume", action="store_true", help="Skip/rehydrate cells with existing output JSON"
    )
    parser.add_argument(
        "--subprocess", action="store_true", help="Run each grid cell via runner.cli subprocess"
    )
    parser.add_argument(
        "--skip-sqlite", action="store_true", help="Do not persist grid cells to SQLite"
    )
    args = parser.parse_args()

    summary = run_backtest_matrix(
        strategy_identifiers=args.strategy,
        symbols=parse_csv_list(args.symbols),
        timeframes=parse_csv_list(args.timeframes),
        start=args.start,
        end=args.end,
        engine=args.engine,
        capital=args.capital,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
        max_leverage=args.max_leverage,
        intrabar_resolution=args.intrabar_resolution,
        params=parse_params(args.params),
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        max_workers=args.max_workers,
        strategy_workers=args.strategy_workers,
        timeout_seconds=args.timeout_seconds,
        skip_existing=args.resume,
        use_subprocess=args.subprocess,
        save_sqlite=not args.skip_sqlite,
        strategies_dir=args.strategies_dir,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["failed_cells"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

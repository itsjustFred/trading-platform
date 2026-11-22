#!/usr/bin/env python3
"""Speed benchmark: native vs the vectorbt adapter on an optimizer-like workload.

Phase-S deliverable for issue #318 (see
``docs/adr/0001-native-single-strategy-engine.md``): times both engines over a
grid of strategy-parameter cells — the shape of an optimizer sweep — and writes
a JSON report. **Report only**: it asserts nothing about which engine is
faster and changes no defaults; the numbers inform the phase-M go/no-go.

Data resolution matches ``scripts.engine_migration_gate``: ``--data-dir``,
then ``TP_TEST_OHLCV_DIR`` (read-only), then deterministic synthetic OHLCV.

Run from the repo root::

    python -m scripts.engine_speed_benchmark \
        --strategy strategies/example_sma/example_sma.py \
        --output data/engine-benchmark/report.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.engine_speed_benchmark
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("engine_speed_benchmark")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time - it
# is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of scripts
# that must carry the guard. Kept deliberately, and silenced rather than removed: dropping it would
# take this script off the roster and out of the gate that keeps the guard alive.
import shared  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from scripts import engine_migration_gate as gate
from shared.constants import normalize_timeframe
from shared.strategy_registry import load_strategy_meta

DEFAULT_STRATEGY = PROJECT_ROOT / "strategies" / "example_sma" / "example_sma.py"
ENGINES = ("native", "vectorbt")


def _numeric_meta_parameters(strategy_path: Path) -> dict[str, float]:
    """Numeric STRATEGY_META.parameters defaults, for grid derivation."""
    meta = load_strategy_meta(strategy_path, strategy_path.parent)
    params = meta.get("parameters", {})
    if not isinstance(params, dict):
        return {}
    return {
        key: float(value)
        for key, value in params.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _default_param_grid(
    strategy_path: Path, max_cells: int
) -> list[dict[str, Any]]:
    """Optimizer-like cell set: up to two numeric params at 0.8x/1x/1.2x.

    Deterministic and strategy-agnostic: strategies without numeric meta
    parameters get a single ``{}`` cell.
    """
    numeric = _numeric_meta_parameters(strategy_path)
    if not numeric:
        return [{}]
    names = sorted(numeric)[:2]
    multipliers = (0.8, 1.0, 1.2)
    cells: list[dict[str, Any]] = []
    for combo in itertools.product(*(multipliers for _ in names)):
        cell: dict[str, Any] = {}
        for name, multiplier in zip(names, combo):
            cell[name] = numeric[name] * multiplier
        cells.append(cell)
        if len(cells) >= max_cells:
            break
    return cells or [{}]


def _parse_param_grid(raw: str, strategy_path: Path, max_cells: int) -> list[dict[str, Any]]:
    if not raw:
        return _default_param_grid(strategy_path, max_cells)
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(c, dict) for c in parsed):
        raise ValueError("--param-grid must decode to a JSON list of objects")
    return parsed[:max_cells]


def _make_config(
    *,
    strategy_path: Path,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    capital: float,
    risk_pct: float | None,
    modeling_resolution: str,
    data_dir: str,
    parameters: dict[str, Any],
) -> BacktestConfig:
    return BacktestConfig(
        strategy_name=strategy_path.stem,
        symbols=list(symbols),
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=capital,
        risk_per_trade_pct=risk_pct,
        modeling_resolution=modeling_resolution,
        data_dir=data_dir,
        parameters=dict(parameters),
    )


def _config_for_cell(
    args: argparse.Namespace,
    *,
    strategy_path: Path,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    data_dir: str,
    parameters: dict[str, Any],
) -> BacktestConfig:
    return _make_config(
        strategy_path=strategy_path,
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        capital=args.capital,
        risk_pct=args.risk_pct,
        modeling_resolution=args.modeling_resolution,
        data_dir=data_dir,
        parameters=parameters,
    )


def _timed_cells(
    args: argparse.Namespace,
    adapters: dict[str, Any],
    cells: list[dict[str, Any]],
    base_parameters: dict[str, Any],
    *,
    strategy_path: Path,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    data_dir: str,
) -> list[dict[str, Any]]:
    """Time every (repeat, cell, engine) combination; returns run rows."""
    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for cell_index, cell_params in enumerate(cells):
            config = _config_for_cell(
                args,
                strategy_path=strategy_path,
                symbols=symbols,
                timeframe=timeframe,
                start=start,
                end=end,
                data_dir=data_dir,
                parameters={**base_parameters, **cell_params},
            )
            for engine_name in ENGINES:
                rows.append(
                    {
                        "repeat": repeat,
                        "cell": cell_index,
                        "params": cell_params,
                        "engine": engine_name,
                        **_time_run(adapters[engine_name], config, strategy_path),
                    }
                )
    return rows


def _summarize_cells(
    rows: list[dict[str, Any]], warmup: dict[str, Any]
) -> dict[str, Any]:
    """Per-engine totals plus the vectorbt/native total-time ratio."""
    summary: dict[str, Any] = {"warmup": warmup, "cells_per_engine": {}}
    for engine_name in ENGINES:
        engine_rows = [r for r in rows if r["engine"] == engine_name]
        ok = [r for r in engine_rows if r["status"] == "ok"]
        seconds = [r["seconds"] for r in ok]
        summary["cells_per_engine"][engine_name] = {
            "runs": len(engine_rows),
            "errors": len(engine_rows) - len(ok),
            "total_seconds": round(sum(seconds), 4),
            "mean_cell_seconds": round(sum(seconds) / len(seconds), 4)
            if seconds
            else None,
            "trades_total": sum(r.get("trade_count", 0) for r in ok),
        }
    native_total = summary["cells_per_engine"]["native"]["total_seconds"]
    vectorbt_total = summary["cells_per_engine"]["vectorbt"]["total_seconds"]
    if native_total and vectorbt_total:
        summary["vectorbt_over_native_total_seconds"] = round(
            vectorbt_total / native_total, 4
        )
    return summary


def _time_run(adapter: Any, config: BacktestConfig, strategy_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = adapter.run(config, str(strategy_path))
    except Exception as exc:
        return {
            "seconds": time.perf_counter() - started,
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    return {
        "seconds": time.perf_counter() - started,
        "status": "ok",
        "trade_count": len(result.trades),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.is_file():
        raise SystemExit(f"strategy not found: {strategy_path}")

    timeframe = normalize_timeframe(args.timeframe) or gate.DEFAULT_TIMEFRAME
    resolution = gate._resolve_data_root(args.data_dir, synthetic_dir=args.synthetic_dir)
    try:
        ohlcv_root = gate._ohlcv_root(resolution.data_root)
        symbols, start, end = gate._resolve_symbols_and_window(
            args, resolution, ohlcv_root, timeframe
        )

        base_parameters = {
            **gate.DEFAULT_PARAMETERS,
            **gate._parse_params(args.params),
        }
        cells = _parse_param_grid(args.param_grid, strategy_path, args.max_cells)
        if not cells:
            raise SystemExit("--param-grid produced no cells")

        adapters = {"native": NativeAdapter(), "vectorbt": VectorbtAdapter()}

        # Unmeasured warmup per engine so first-call imports/JIT do not skew
        # the timed cells (still reported, marked warmup).
        warmup_config = _config_for_cell(
            args,
            strategy_path=strategy_path,
            symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            data_dir=str(resolution.data_root),
            parameters={**base_parameters, **(cells[0] if cells else {})},
        )
        warmup: dict[str, Any] = {}
        for engine_name in ENGINES:
            warmup[engine_name] = _time_run(
                adapters[engine_name], warmup_config, strategy_path
            )

        rows = _timed_cells(
            args,
            adapters,
            cells,
            base_parameters,
            strategy_path=strategy_path,
            symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            data_dir=str(resolution.data_root),
        )
    finally:
        if resolution.cleanup is not None:
            resolution.cleanup.cleanup()

    summary = _summarize_cells(rows, warmup)

    return gate._jsonable(
        {
            "benchmark": "native vs vectorbt adapter, optimizer-like grid (#318 phase S)",
            "note": "report only — no defaults change and no speed claim is asserted",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "strategy": str(strategy_path),
                "symbols": symbols,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "capital": args.capital,
                "risk_per_trade_pct": args.risk_pct,
                "modeling_resolution": args.modeling_resolution,
                "data_mode": resolution.mode,
                "ohlcv_root": str(ohlcv_root),
                "param_cells": cells,
                "repeats": args.repeats,
            },
            "summary": summary,
            "runs": rows,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--timeframe", default=gate.DEFAULT_TIMEFRAME)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--risk-pct", type=float, default=gate.DEFAULT_RISK_PCT)
    parser.add_argument("--modeling-resolution", default="open_close")
    parser.add_argument("--params", default="")
    parser.add_argument(
        "--param-grid",
        default="",
        help="JSON list of parameter-override objects (optimizer-like cells). "
        "Default: derived from numeric STRATEGY_META.parameters.",
    )
    parser.add_argument("--max-cells", type=int, default=9)
    parser.add_argument("--max-symbols", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--synthetic-dir", type=Path, default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/engine-benchmark/report.json"),
    )
    args = parser.parse_args(argv)

    report = run_benchmark(args)
    out = gate.write_report(report, args.output)

    cells = report["summary"]["cells_per_engine"]
    for engine_name in ENGINES:
        cell = cells[engine_name]
        print(
            f"{engine_name:9s}: {cell['runs']} runs, {cell['errors']} errors, "
            f"total {cell['total_seconds']}s, mean/cell {cell['mean_cell_seconds']}s, "
            f"trades {cell['trades_total']}"
        )
    ratio = report["summary"].get("vectorbt_over_native_total_seconds")
    if ratio is not None:
        print(f"vectorbt/native total-time ratio: {ratio} (report only)")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

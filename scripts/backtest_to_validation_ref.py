"""Convert a platform BacktestResult JSON into a ValidationReference JSON.

Usage:
    python -m scripts.backtest_to_validation_ref \
        data/backtests/result.json \
        data/backtests/result_vref.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.backtest_to_validation_ref
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("backtest_to_validation_ref")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time - it
# is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of scripts
# that must carry the guard. Kept deliberately, and silenced rather than removed: dropping it would
# take this script off the roster and out of the gate that keeps the guard alive.
import shared  # noqa: F401
from scripts._safe_path import under_root
from shared.atomic_io import write_text_atomically
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.reference import ValidationReference, resolve_initial_capital


def _safe_input_path(raw: Path) -> Path:
    """Confine the caller-supplied input path to the working tree.

    The input is read, not written, but it is still a path a runbook may have quoted from an LLM,
    and a read outside the tree the operator is working in is never what the invocation meant. The
    same confinement as the output, so both ends of the conversion say the same thing.
    """
    return under_root(raw, root=Path.cwd(), field="input")


def _load_backtest_result(path: Path) -> BacktestResult:
    source = _safe_input_path(path)
    if not source.is_file():
        raise SystemExit(f"input: not a regular file ({source})")
    d = json.loads(source.read_text(encoding="utf-8"))
    meta = BacktestMeta(**d["meta"])
    trades = [Trade(**t) for t in d.get("trades", [])]
    for t in trades:
        t.exits = [TradeExit(**e) for e in t.exits]
    return BacktestResult(
        meta=meta,
        trades=trades,
        equity_curve=d.get("equity_curve", []),
        stats=d.get("stats", {}),
        indicators=d.get("indicators", []),
        drawings=d.get("drawings", []),
    )


def _to_validation_reference(result: BacktestResult) -> ValidationReference:
    raw_metadata = dict(result.meta.__dict__)
    raw_metadata["parsed_meta"] = result.meta.to_dict()
    reference = ValidationReference(
        strategy_name=result.meta.strategies[0] if result.meta.strategies else "unknown",
        source_framework=result.meta.engine,
        symbol=result.meta.data["symbols"][0],
        timeframe=result.meta.data["timeframes"][0],
        settings=dict(result.meta.parameters),
        trades=result.trades,
        stats=result.stats,
        raw_metadata=raw_metadata,
    )
    reference.settings["initial_capital"] = resolve_initial_capital(reference)
    return reference


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert BacktestResult JSON to ValidationReference JSON."
    )
    parser.add_argument("input", type=Path, help="Path to BacktestResult JSON.")
    parser.add_argument("output", type=Path, help="Path to write ValidationReference JSON.")
    args = parser.parse_args()

    res = _load_backtest_result(args.input)
    ref = _to_validation_reference(res)
    safe = under_root(args.output, root=Path.cwd(), field="--output")
    # The sink lives in the shared atomic-write helper, not here.
    write_text_atomically(safe, ref.model_dump_json(indent=2))
    print(f"Wrote ValidationReference to {safe}")


if __name__ == "__main__":
    main()

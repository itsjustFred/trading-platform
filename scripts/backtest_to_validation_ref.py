"""Convert a platform BacktestResult JSON into a ValidationReference JSON.

Usage:
    python -m scripts.backtest_to_validation_ref \
        data/backtests/result.json \
        data/backtests/result_vref.json
"""
from __future__ import annotations

import argparse
import json
import os
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
from scripts._safe_path import reject_traversal, under_root
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.reference import ValidationReference, resolve_initial_capital


def _safe_input_path(raw: Path) -> Path:
    """Refuse a traversal segment in the caller-supplied input path.

    The input is read, not written, and may legitimately live outside the repo - so this is the
    traversal check only, not the ``under_root`` confinement ``compare_strategy_metrics.py`` applies
    to its output. The point is the legible failure: a runbook quoting a path an LLM produced gets
    "name the directory directly" instead of a confusing half-populated result.
    """
    return reject_traversal(raw, field="input")


def _load_backtest_result(path: Path) -> BacktestResult:
    source = Path(os.path.realpath(_safe_input_path(path)))
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
    root = Path(os.path.realpath(str(Path.cwd())))
    safe = under_root(args.output, root=root, field="--output")
    # Rebuild the sink from sanitised components: each directory level passes through
    # `os.path.basename`, the traversal sanitiser, which is the identity for a resolved relative
    # path. The result is the same path for any legitimate input.
    output = root.joinpath(*(os.path.basename(part) for part in safe.relative_to(root).parts))
    output.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote ValidationReference to {output}")


if __name__ == "__main__":
    main()

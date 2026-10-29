"""Convert a platform BacktestResult JSON into a ValidationReference JSON.

Usage:
    python -m scripts.backtest_to_validation_ref \
        data/backtests/result.json \
        data/backtests/result_vref.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# isort: split

# --- import-origin guard -------------------------------------------------
# Running a script puts `scripts/` on `sys.path[0]`, not the repo root, so `import shared` does not
# find the checkout this file lives in: it falls through to the installed package, which points at a
# *different* tree. Measured on 2026-10-04: the two were genuinely different revisions, and the
# installed one carried a *shorter migration set*, so a script run this way applied an older schema
# than the code believes and then reported success.
#
# Refused rather than silently corrected: quietly importing a different tree than the one being
# edited is the hazard, not the fix. Run from the repo root as
#     python -m scripts.backtest_to_validation_ref
# to use this checkout. Pinned by tests/test_script_import_origin.py.
import shared as _shared_package

_EXPECTED_PKG = Path(__file__).resolve().parent.parent / "shared"
_ACTUAL_PKG = Path(next(iter(_shared_package.__path__))).resolve()
if _ACTUAL_PKG != _EXPECTED_PKG:
    print(
        "refusing to run: `shared` was imported from the wrong tree.\n"
        f"  this script lives in : {Path(__file__).resolve().parent.parent}\n"
        f"  expected shared/     : {_EXPECTED_PKG}\n"
        f"  actually imported    : {_ACTUAL_PKG}\n"
        "\n"
        "Running a script puts scripts/ on sys.path[0], not the repo root, so `import shared`\n"
        "falls through to the installed package - a different revision, with a shorter\n"
        "migration set. To run against the checkout being edited, from the repo root:\n"
        "    python -m scripts.backtest_to_validation_ref\n",
        file=sys.stderr,
    )
    raise SystemExit(2)
# --- end import-origin guard ---------------------------------------------
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.reference import ValidationReference, resolve_initial_capital


def _load_backtest_result(path: Path) -> BacktestResult:
    d = json.loads(path.read_text(encoding="utf-8"))
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
    args.output.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote ValidationReference to {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate a cross-platform metrics comparison matrix."""

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
#     python -m scripts.compare_strategy_metrics
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("compare_strategy_metrics")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time - it
# is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of scripts
# that must carry the guard. Kept deliberately, and silenced rather than removed: dropping it would
# take this script off the roster and out of the gate that keeps the guard alive.
import shared  # noqa: F401
from scripts._safe_path import under_root
from shared.atomic_io import write_text_atomically
from validation.statistics.cross_platform import build_comparison_matrix

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _safe_output_path(raw: Path) -> Path:
    """Confine `--output` to the project root, refusing traversal rather than trusting it."""
    return under_root(raw, root=PROJECT_ROOT, field="--output")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare platform metrics with MT5/QC references.")
    parser.add_argument(
        "--comparisons-dir",
        type=Path,
        default=Path("strategy-comparisons"),
        help="Directory containing external reference reports.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="Directory containing legacy platform report artifacts.",
    )
    parser.add_argument(
        "--platform-data-dir",
        type=Path,
        default=Path("data/backtests"),
        help="Directory containing regenerated platform backtest artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/strategy_metric_comparison_matrix_20260630.json"),
        help="Output JSON matrix path.",
    )
    args = parser.parse_args()

    matrix = build_comparison_matrix(
        comparisons_dir=args.comparisons_dir,
        reports_dir=args.reports_dir,
        platform_data_dir=args.platform_data_dir,
    )
    safe = _safe_output_path(args.output)
    # The sink lives in the shared atomic-write helper, not here: it validates the target, stages
    # a temp file beside it and renames it. This script only decides which path it wants.
    write_text_atomically(safe, json.dumps(matrix, indent=2))
    print(f"Wrote {safe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

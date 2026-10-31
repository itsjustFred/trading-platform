#!/usr/bin/env python3
"""Generate a cross-platform metrics comparison matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts._safe_path import under_root
from validation.statistics.cross_platform import build_comparison_matrix


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
    output = _safe_output_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

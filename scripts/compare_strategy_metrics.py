#!/usr/bin/env python3
"""Generate a cross-platform metrics comparison matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from validation.statistics.cross_platform import build_comparison_matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare platform metrics with MT5/QC references.")
    parser.add_argument(
        "--comparisons-dir",
        type=Path,
        default=PROJECT_ROOT / "strategy-comparisons",
        help="Directory containing external reference reports.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=PROJECT_ROOT / "reports",
        help="Directory containing legacy platform report artifacts.",
    )
    parser.add_argument(
        "--platform-data-dir",
        type=Path,
        default=PROJECT_ROOT / "data/backtests",
        help="Directory containing regenerated platform backtest artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "reports/strategy_metric_comparison_matrix_20260630.json",
        help="Output JSON matrix path.",
    )
    args = parser.parse_args()

    matrix = build_comparison_matrix(
        comparisons_dir=args.comparisons_dir,
        reports_dir=args.reports_dir,
        platform_data_dir=args.platform_data_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

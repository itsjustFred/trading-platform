#!/usr/bin/env python3
"""Generate golden fixture JSON files from MT5 XLSX backtest reports."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "validation" / "goldens"
REPORTS = [
    (
        PROJECT_ROOT
        / "strategy-comparisons"
        / "intraday_volatility_breakout"
        / "EURUSD_H1.xlsx",
        "eurusd_h1_golden.json",
    ),
    (
        PROJECT_ROOT
        / "strategy-comparisons"
        / "intraday_volatility_breakout"
        / "NAS100_M15.xlsx",
        "nas100_m15_golden.json",
    ),
    (
        PROJECT_ROOT
        / "strategy-comparisons"
        / "intraday_volatility_breakout"
        / "SP500_M1.xlsx",
        "sp500_m1_golden.json",
    ),
]


def generate() -> None:
    importer = MT5XLSXImporter()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    for source_path, filename in REPORTS:
        if not source_path.exists():
            print(f"[ERROR] Report not found: {source_path}", file=sys.stderr)
            sys.exit(1)

        ref: ValidationReference = importer.load(source_path)
        json_path = FIXTURE_DIR / filename

        # model_dump(mode="json") converts datetimes/dates to ISO strings
        data = ref.model_dump(mode="json")
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[OK] {filename}  ({len(ref.trades)} trades)")


if __name__ == "__main__":
    generate()

"""Regression tests for golden fixture files generated from MT5 XLSX reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from validation.reference import ValidationReference

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "goldens"


def _list_golden_files() -> list[Path]:
    if not GOLDEN_DIR.exists():
        return []
    return sorted(GOLDEN_DIR.glob("*.json"))


class TestGoldenFilesExist:
    def test_at_least_three_golden_files(self):
        files = _list_golden_files()
        assert len(files) >= 3, f"Expected >= 3 golden files, found {len(files)} in {GOLDEN_DIR}"


class TestGoldenFilesLoad:
    def test_each_file_loads_as_validation_reference(self):
        files = _list_golden_files()
        assert files, "No golden files found"
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            ref = ValidationReference.model_validate(data)
            assert isinstance(ref, ValidationReference)
            assert ref.source_framework == "mt5_xlsx"


class TestGoldenTradeCounts:
    def test_each_file_contains_trades(self):
        files = _list_golden_files()
        assert files, "No golden files found"
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            ref = ValidationReference.model_validate(data)
            assert len(ref.trades) > 0, f"{path.name} contains no trades"


class TestGoldenStats:
    def test_each_file_contains_known_statistics(self):
        files = _list_golden_files()
        assert files, "No golden files found"
        required_stat_keys = {"total_pnl", "gross_profit"}
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            ref = ValidationReference.model_validate(data)
            missing = required_stat_keys - set(ref.stats.keys())
            assert not missing, f"{path.name} missing stats: {missing}"

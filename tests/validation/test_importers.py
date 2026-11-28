"""Base importer acceptance tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from validation.importers import BaseImporter, MT5XLSXImporter
from validation.reference import ValidationReference

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "validation"


def test_importer_package_exports_supported_importers() -> None:
    assert issubclass(MT5XLSXImporter, BaseImporter)


def test_base_importer_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseImporter()


def test_xlsx_importer_normalizes_to_validation_reference_from_any_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    ref = MT5XLSXImporter().load(FIXTURE_ROOT / "mt5_xlsx" / "hedged_partial_open.xlsx")

    assert isinstance(ref, ValidationReference)
    assert ref.source_framework == "mt5_xlsx"
    assert ref.strategy_name == "Synthetic Hedge Partial Fixture"
    assert ref.symbol == "EURUSD"
    assert ref.timeframe == "h1"
    assert len(ref.trades) == 3
    assert ref.stats["total_trades"] == 3


def test_importer_missing_source_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        MT5XLSXImporter().load(FIXTURE_ROOT / "mt5_xlsx" / "missing.xlsx")

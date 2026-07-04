"""Base importer acceptance tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from validation.importers import BaseImporter, MT5HTMLImporter, MT5XLSXImporter
from validation.reference import ValidationReference


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "validation"


def test_importer_package_exports_supported_importers() -> None:
    assert issubclass(MT5HTMLImporter, BaseImporter)
    assert issubclass(MT5XLSXImporter, BaseImporter)


def test_base_importer_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseImporter()


def test_html_importer_normalizes_to_validation_reference_from_any_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    ref = MT5HTMLImporter().load(FIXTURE_ROOT / "mt5_html" / "sample_report.html")

    assert isinstance(ref, ValidationReference)
    assert ref.source_framework == "mt5_html"
    assert ref.strategy_name == "ZenomTrader_IntradayVolatilityBreakout"
    assert ref.symbol == "EURUSD"
    assert ref.timeframe == "h1"
    assert len(ref.trades) == 5
    assert ref.stats["total_trades"] == 5


def test_html_importer_applies_declared_source_timezone() -> None:
    ref = MT5HTMLImporter().load(
        FIXTURE_ROOT / "mt5_html" / "sample_report.html",
        source_timezone="Europe/Athens",
    )

    first = ref.trades[0]
    assert first.entry_time == "2024-01-03T06:00:00Z"
    assert first.exits[0].time == "2024-01-03T14:00:00Z"


def test_importer_missing_source_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        MT5HTMLImporter().load(FIXTURE_ROOT / "missing.html")

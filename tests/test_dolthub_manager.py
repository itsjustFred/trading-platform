from pathlib import Path

from data_tools.dolthub_manager import _stock_export_path


def test_stock_export_path_migrates_legacy_flat_export(tmp_path: Path):
    target_dir = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
    target_dir.mkdir(parents=True)
    legacy_path = target_dir / "AAPL.parquet"
    legacy_path.write_text("legacy parquet placeholder")

    canonical_path = _stock_export_path(target_dir, "AAPL")

    assert canonical_path == target_dir / "AAPL" / "1d.parquet"
    assert canonical_path.read_text() == "legacy parquet placeholder"
    assert not legacy_path.exists()

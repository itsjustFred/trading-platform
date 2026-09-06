import logging
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


def test_export_stocks_skips_symbols_that_fail_symbol_validation(tmp_path, monkeypatch, caplog):
    """_export_stocks must not route symbol values through path helpers before validation."""
    import data_tools.dolthub_manager as dolthub_manager

    stocks_repo = tmp_path / "stocks"
    (stocks_repo / dolthub_manager.DOLT_METADATA_DIR).mkdir(parents=True)
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(dolthub_manager, "DOLT_DIR", tmp_path)
    monkeypatch.setattr(dolthub_manager, "EXPORT_DIR", export_dir)

    monkeypatch.setattr(dolthub_manager, "_stocks_table_name", lambda repo: "ohlcv")
    monkeypatch.setattr(
        dolthub_manager,
        "_list_symbols",
        lambda repo, table: ["AAPL", "../escape", "BAD SYMBOL"],
    )
    exported: list[str] = []

    def _capture_export(repo, table, symbol, target_dir, out_parquet):
        exported.append(symbol)
        out_parquet.parent.mkdir(parents=True, exist_ok=True)
        out_parquet.write_text("parquet placeholder")

    monkeypatch.setattr(dolthub_manager, "_export_symbol_csv", _capture_export)

    with caplog.at_level(logging.WARNING, logger="dolthub_manager"):
        dolthub_manager._export_stocks()

    assert exported == ["AAPL"]
    assert (export_dir / "equity" / "US" / "AAPL" / "1d.parquet").exists()
    assert not (export_dir / "equity" / "US" / ".." / "escape").exists()
    warnings = [record.getMessage() for record in caplog.records]
    assert any("../escape" in message for message in warnings)
    assert any("BAD SYMBOL" in message for message in warnings)

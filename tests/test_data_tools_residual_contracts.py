from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

import data_tools.cache_manager as cache_manager
import data_tools.migrate_csv as migrate_csv
import data_tools.validate as validation
from data_tools.auto_download import DataRequirement, analyze_ohlcv_coverage
from data_tools.download_grid_market_data import ensure_alias_copy
from data_tools.downloaders.alpaca_dl import AlpacaDownloader
from data_tools.downloaders.huggingface_dl import HuggingFaceDownloader
from data_tools.path_safety import build_dataset_path, validate_dataset_component


@pytest.mark.parametrize(
    "component",
    [
        ".",
        "..",
        "../escape",
        "folder/file",
        r"folder\\file",
        "*",
        "name?",
        "name[0]",
        "line\nbreak",
        "CON",
        "nul.txt",
        "name.",
    ],
)
def test_dataset_component_rejects_unsafe_values(component: str) -> None:
    with pytest.raises(ValueError):
        validate_dataset_component(component)


def test_validation_uses_custom_ohlcv_root_for_relative_inference(tmp_path: Path) -> None:
    path = tmp_path / "custom" / "ohlcv" / "source" / "equity" / "AAPL" / "1d.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC"),
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.0, 1.0],
            "close": [1.5, 2.5],
            "volume": [10.0, 11.0],
        }
    ).to_parquet(path, index=False)

    results = validation.validate_all(tmp_path / "custom")

    assert len(results) == 1
    assert results[0].source == "source"
    assert results[0].path == "source/equity/AAPL/1d.parquet"


def test_dataset_component_rejects_explicit_parent_component() -> None:
    with pytest.raises(ValueError, match="traversal component"):
        validate_dataset_component("..")


def test_dataset_component_rejects_current_directory_component() -> None:
    with pytest.raises(ValueError, match="traversal component"):
        validate_dataset_component(".")


def test_dataset_path_rejects_parent_traversal_in_root(tmp_path: Path) -> None:
    root = tmp_path / "unused" / ".." / "datasets"

    with pytest.raises(ValueError, match="dataset root must not traverse upward"):
        build_dataset_path(root, "EURUSD", "1m.parquet")


def test_dataset_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|escapes"):
        build_dataset_path(root, "linked", "AAPL", "1m.parquet")


def test_downloader_path_rejects_unsafe_symbol(tmp_path: Path) -> None:
    from data_tools.downloaders.yfinance_dl import YFinanceDownloader

    with pytest.raises(ValueError):
        YFinanceDownloader()._make_us_equity_output_path(tmp_path, "../escape", "1d")


def test_coverage_rejects_glob_symbol_before_scanning(tmp_path: Path) -> None:
    report = analyze_ohlcv_coverage(
        tmp_path,
        DataRequirement(symbol="../*", timeframe="1d", start="", end=""),
    )

    assert report["status"] == "failed"
    assert "glob" in report["error"]


def test_alias_refresh_uses_file_fingerprint(tmp_path: Path) -> None:
    canonical = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1m.parquet"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"new canonical data")
    alias = ensure_alias_copy(canonical, "GBPUSD", "forex", tmp_path)
    alias.write_bytes(b"old canonical data")

    refreshed = ensure_alias_copy(canonical, "GBPUSD", "forex", tmp_path)

    assert refreshed.read_bytes() == canonical.read_bytes()


def test_catalog_cache_rebuilds_after_fingerprint_change(tmp_path: Path) -> None:
    parquet = tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1h.parquet"
    parquet.parent.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.0, 1.0],
            "close": [1.5, 2.5],
        }
    )
    frame.to_parquet(parquet, index=False)
    cache_manager.invalidate_cache()
    try:
        first = cache_manager.get_catalog(tmp_path)
        pd.concat(
            [frame, frame.iloc[[-1]].assign(timestamp=pd.Timestamp("2024-01-01T03:00:00Z"))],
            ignore_index=True,
        ).to_parquet(parquet, index=False)
        second = cache_manager.get_catalog(tmp_path)
    finally:
        cache_manager.invalidate_cache()

    assert first["ohlcv"][0]["rows"] == 2
    assert second["ohlcv"][0]["rows"] == 3


def test_catalog_cache_ignores_unparseable_ohlcv_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1h.parquet"
    valid.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=1, freq="h", tz="UTC"),
            "open": [1.0],
            "high": [2.0],
            "low": [0.0],
            "close": [1.5],
        }
    ).to_parquet(valid, index=False)
    bad = tmp_path / "ohlcv" / "src" / "bad.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a parquet file")

    original_build = cache_manager.build_catalog
    calls = 0

    def counting_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(cache_manager, "build_catalog", counting_build)
    cache_manager.invalidate_cache()
    try:
        cache_manager.get_catalog(tmp_path)
        cache_manager.get_catalog(tmp_path)
    finally:
        cache_manager.invalidate_cache()

    assert calls == 1


def test_csv_migration_keeps_previous_artifact_when_atomic_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-01-01T00:00:00Z,1,2,0,1.5,10\n",
        encoding="utf-8",
    )
    destination = tmp_path / "destination.parquet"
    assert migrate_csv._convert_ohlcv(source, destination, False) == 1
    before = destination.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(migrate_csv, "write_parquet_atomically", fail_write)
    assert migrate_csv._convert_ohlcv(source, destination, False, force=True) == 0
    assert destination.read_bytes() == before


def test_validation_rejects_symlinked_file_outside_data_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside.parquet"
    target = root / "ohlcv" / "test" / "equity" / "AAPL" / "1d.parquet"
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"], utc=True),
            "open": [1.0],
            "high": [2.0],
            "low": [0.0],
            "close": [1.5],
        }
    ).to_parquet(outside, index=False)
    target.symlink_to(outside)

    results = validation.validate_all(root)

    assert len(results) == 1
    assert results[0].status == "error"
    assert "escapes" in results[0].errors[0]


def test_validation_cli_returns_nonzero_for_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = validation.ValidationResult(
        path="bad.parquet",
        symbol="AAPL",
        timeframe="1d",
        source="test",
        status="error",
    )
    monkeypatch.setattr(validation, "validate_all", lambda *_args, **_kwargs: [result])
    monkeypatch.setattr(sys, "argv", ["data_tools.validate", "--data-dir", "."])

    assert validation.main() == 1


def test_unavailable_volume_remains_missing_in_downloaders() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"], utc=True),
            "open": [1.0],
            "high": [2.0],
            "low": [0.0],
            "close": [1.5],
        }
    )
    start = pd.Timestamp("2024-01-01T00:00:00Z")
    end = pd.Timestamp("2024-01-02T00:00:00Z")

    alpaca = AlpacaDownloader()._normalize_response_frame(
        frame, symbol="AAPL", start=start, end=end
    )
    huggingface = HuggingFaceDownloader()._normalize_month_frame(
        frame, symbol="AAPL", start_ts=start, end_ts=end
    )

    assert pd.isna(alpaca.loc[0, "volume"])
    assert pd.isna(huggingface.loc[0, "volume"])

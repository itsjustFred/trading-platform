import json
from pathlib import Path

import pandas as pd
import pytest

import data_tools.catalog_builder as catalog_builder
from data_tools.catalog_builder import build_catalog


@pytest.fixture
def sample_data_dir(tmp_path):
    """Create a minimal data directory for testing."""
    ohlcv = tmp_path / "ohlcv" / "test_source" / "equity" / "US" / "AAPL"
    ohlcv.mkdir(parents=True)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC"),
        "open": range(100),
        "high": range(1, 101),
        "low": range(100),
        "close": range(100),
        "volume": range(100),
    })
    df.to_parquet(ohlcv / "1h.parquet", index=False)

    uni = tmp_path / "universes"
    uni.mkdir()
    (uni / "test.txt").write_text("AAPL\nMSFT\n")

    return tmp_path


def test_build_catalog(sample_data_dir):
    catalog = build_catalog(sample_data_dir)

    assert catalog["summary"]["total_datasets"] == 1
    assert catalog["summary"]["symbols"] == 1
    assert catalog["summary"]["sources"] == ["test_source"]
    assert catalog["summary"]["timeframes"] == ["1h"]

    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "AAPL"
    assert ds["timeframe"] == "1h"
    assert ds["rows"] == 100
    assert ds["format"] == "parquet"
    assert "timestamp" in ds["columns"]
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-01-05"
    assert ds["last_updated"] is not None

    assert len(catalog["universes"]) == 1
    assert catalog["universes"][0]["name"] == "test"


def test_build_catalog_reuses_cached_metadata_for_unchanged_files(sample_data_dir, monkeypatch):
    first_catalog = build_catalog(sample_data_dir)
    (sample_data_dir / "catalog.json").write_text(json.dumps(first_catalog))

    def fail_if_called(_: Path) -> dict:
        raise AssertionError("read_parquet_summary should not run for unchanged cached files")

    monkeypatch.setattr(catalog_builder, "read_parquet_summary", fail_if_called)

    cached_catalog = build_catalog(sample_data_dir)

    ds = cached_catalog["ohlcv"][0]
    assert ds["symbol"] == "AAPL"
    assert ds["timeframe"] == "1h"
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-01-05"
    assert ds["rows"] == 100
    assert ds["last_updated"] is not None


def test_build_catalog_handles_legacy_flat_dolthub_exports(tmp_path):
    ohlcv = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
    ohlcv.mkdir(parents=True)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-02-01", periods=3, freq="D", tz="UTC"),
        "open": [10, 11, 12],
        "high": [11, 12, 13],
        "low": [9, 10, 11],
        "close": [10.5, 11.5, 12.5],
        "volume": [100, 110, 120],
    })
    df.to_parquet(ohlcv / "AAPL.parquet", index=False)

    catalog = build_catalog(tmp_path)

    assert catalog["summary"]["sources"] == ["dolthub"]
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "AAPL"
    assert ds["timeframe"] == "1d"
    assert ds["source"] == "dolthub"
    assert ds["region"] == "US"
    assert ds["start"] == "2024-02-01"
    assert ds["end"] == "2024-02-03"


def test_build_catalog_groups_segmented_files_into_single_dataset(tmp_path):
    symbol_dir = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD"
    symbol_dir.mkdir(parents=True)

    jan = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z"],
            utc=True,
        ),
        "bid": [1.10, 1.20],
        "ask": [1.11, 1.21],
    })
    feb = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-02-01T00:00:00Z", "2024-02-28T23:59:59Z"],
            utc=True,
        ),
        "bid": [1.30, 1.40],
        "ask": [1.31, 1.41],
    })

    jan.to_parquet(symbol_dir / "tick_2024_01.parquet", index=False)
    feb.to_parquet(symbol_dir / "tick_2024_02.parquet", index=False)

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "EURUSD"
    assert ds["timeframe"] == "tick"
    assert ds["rows"] == 4
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-02-28"
    assert ds["segment_count"] == 2
    assert [segment["segment"] for segment in ds["segments"]] == ["2024_01", "2024_02"]
    assert {Path(segment["path"]).name for segment in ds["segments"]} == {
        "tick_2024_01.parquet",
        "tick_2024_02.parquet",
    }

    assert catalog["summary"]["total_datasets"] == 1
    assert catalog["summary"]["symbols"] == 1
    assert catalog["summary"]["sources"] == ["dukascopy"]
    assert catalog["summary"]["timeframes"] == ["tick"]


def test_build_catalog_groups_symbol_prefixed_segmented_files_into_single_dataset(tmp_path):
    ohlcv_root = tmp_path / "ohlcv" / "dukascopy" / "forex"
    ohlcv_root.mkdir(parents=True)

    jan = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z"],
            utc=True,
        ),
        "bid": [2000.1, 2010.2],
        "ask": [2000.3, 2010.4],
    })
    feb = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z"],
            utc=True,
        ),
        "bid": [2020.1, 2030.2],
        "ask": [2020.3, 2030.4],
    })

    jan.to_parquet(ohlcv_root / "XAUUSD_tick_2024_01.parquet", index=False)
    feb.to_parquet(ohlcv_root / "XAUUSD_tick_2024_02.parquet", index=False)

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["source"] == "dukascopy"
    assert ds["asset_class"] == "forex"
    assert ds["symbol"] == "XAUUSD"
    assert ds["timeframe"] == "tick"
    assert ds["rows"] == 4
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-02-29"
    assert ds["segment_count"] == 2
    assert [segment["segment"] for segment in ds["segments"]] == ["2024_01", "2024_02"]
    assert {Path(segment["path"]).name for segment in ds["segments"]} == {
        "XAUUSD_tick_2024_01.parquet",
        "XAUUSD_tick_2024_02.parquet",
    }


def test_build_catalog_merges_same_symbol_timeframe_across_sources(tmp_path):
    dukascopy_dir = tmp_path / "ohlcv" / "dukascopy" / "forex" / "XAUUSD"
    histdata_dir = tmp_path / "ohlcv" / "histdata" / "forex" / "XAUUSD"
    dukascopy_dir.mkdir(parents=True)
    histdata_dir.mkdir(parents=True)

    jan = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z"],
            utc=True,
        ),
        "bid": [2000.1, 2010.2],
        "ask": [2000.3, 2010.4],
    })
    feb = pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z"],
            utc=True,
        ),
        "bid": [2020.1, 2030.2],
        "ask": [2020.3, 2030.4],
    })

    jan.to_parquet(dukascopy_dir / "4h_2024_01.parquet", index=False)
    feb.to_parquet(histdata_dir / "4h_2024_02.parquet", index=False)

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "XAUUSD"
    assert ds["timeframe"] == "4h"
    assert ds["source"] == "mixed"
    assert ds["asset_class"] == "forex"
    assert ds["sources"] == ["dukascopy", "histdata"]
    assert ds["rows"] == 4
    assert ds["row_count"] == 4
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-02-29"
    assert ds["start_date"] == "2024-01-01"
    assert ds["end_date"] == "2024-02-29"
    assert ds["segment_count"] == 2
    assert ds["file_count"] == 2
    assert {Path(path).name for path in ds["files"]} == {
        "4h_2024_01.parquet",
        "4h_2024_02.parquet",
    }
    assert {Path(segment["path"]).name for segment in ds["segments"]} == {
        "4h_2024_01.parquet",
        "4h_2024_02.parquet",
    }


def test_empty_catalog(tmp_path):
    catalog = build_catalog(tmp_path)
    assert catalog["summary"]["total_datasets"] == 0
    assert catalog["ohlcv"] == []
    assert catalog["universes"] == []

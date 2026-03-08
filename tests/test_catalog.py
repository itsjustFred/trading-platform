"""Tests for data_tools.catalog_builder."""

import data_tools.catalog_builder as catalog_builder
import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest

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


def test_empty_catalog(tmp_path):
    catalog = build_catalog(tmp_path)
    assert catalog["summary"]["total_datasets"] == 0
    assert catalog["ohlcv"] == []
    assert catalog["universes"] == []

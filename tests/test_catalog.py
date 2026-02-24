"""Tests for data_tools.catalog_builder."""

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

    assert len(catalog["universes"]) == 1
    assert catalog["universes"][0]["name"] == "test"


def test_empty_catalog(tmp_path):
    catalog = build_catalog(tmp_path)
    assert catalog["summary"]["total_datasets"] == 0
    assert catalog["ohlcv"] == []
    assert catalog["universes"] == []

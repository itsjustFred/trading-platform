import json
import os
from pathlib import Path

import pandas as pd
import pytest

import data_tools.catalog_builder as catalog_builder
from data_tools.catalog_builder import build_catalog
from shared import ohlcv_store


def _write_ohlcv(
    data_root: Path,
    source: str,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    *,
    asset_class: str,
    region: str = "",
    segment: str = "",
) -> Path:
    return ohlcv_store.write_frame(
        data_root,
        source,
        symbol,
        timeframe,
        frame,
        asset_class=asset_class,
        region=region,
        segment=segment,
    )


def _register_parquet(
    data_root: Path,
    source: str,
    symbol: str,
    timeframe: str,
    segment: str,
    frame: pd.DataFrame,
    *,
    asset_class: str,
    region: str = "",
) -> Path:
    """Write a parquet the store contract would reject (e.g. tick bid/ask) and
    declare it in the manifest, the same way alias copies are registered."""
    path = ohlcv_store.data_path(data_root, source, symbol, timeframe, segment=segment)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return ohlcv_store.register_file(
        data_root,
        source,
        symbol,
        timeframe,
        path,
        asset_class=asset_class,
        region=region,
        segment=segment,
    )


@pytest.fixture
def sample_data_dir(tmp_path):
    """Create a minimal data directory for testing."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC"),
        "open": range(100),
        "high": range(1, 101),
        "low": range(100),
        "close": range(100),
        "volume": range(100),
    })
    _write_ohlcv(tmp_path, "test_source", "AAPL", "1h", df, asset_class="equity", region="US")

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


def test_build_catalog_reads_dolthub_store_exports(tmp_path):
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-02-01", periods=3, freq="D", tz="UTC"),
        "open": [10, 11, 12],
        "high": [11, 12, 13],
        "low": [9, 10, 11],
        "close": [10.5, 11.5, 12.5],
        "volume": [100, 110, 120],
    })
    _write_ohlcv(
        tmp_path, "dolthub", "AAPL", "1d", df,
        asset_class="equity", region="US",
    )

    catalog = build_catalog(tmp_path)

    assert catalog["summary"]["sources"] == ["dolthub"]
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "AAPL"
    assert ds["timeframe"] == "1d"
    assert ds["source"] == "dolthub"
    assert ds["asset_class"] == "equity"
    assert ds["region"] == "us"
    assert ds["start"] == "2024-02-01"
    assert ds["end"] == "2024-02-03"


def _tick_frame(first: str, last: str, base: float) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime([first, last], utc=True),
        "bid": [base, base + 0.1],
        "ask": [base + 0.01, base + 0.11],
    })


def test_build_catalog_groups_segmented_files_into_single_dataset(tmp_path):
    jan = _tick_frame("2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z", 1.10)
    feb = _tick_frame("2024-02-01T00:00:00Z", "2024-02-28T23:59:59Z", 1.30)

    _register_parquet(
        tmp_path, "dukascopy", "EURUSD", "tick", "2024_01", jan, asset_class="forex"
    )
    _register_parquet(
        tmp_path, "dukascopy", "EURUSD", "tick", "2024_02", feb, asset_class="forex"
    )

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
        "EURUSD_TICK_2024_01.parquet",
        "EURUSD_TICK_2024_02.parquet",
    }

    assert catalog["summary"]["total_datasets"] == 1
    assert catalog["summary"]["symbols"] == 1
    assert catalog["summary"]["sources"] == ["dukascopy"]
    assert catalog["summary"]["timeframes"] == ["tick"]


def test_build_catalog_groups_metals_segments_into_single_dataset(tmp_path):
    jan = _tick_frame("2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z", 2000.1)
    feb = _tick_frame("2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z", 2020.1)

    _register_parquet(
        tmp_path, "dukascopy", "XAUUSD", "tick", "2024_01", jan, asset_class="metals"
    )
    _register_parquet(
        tmp_path, "dukascopy", "XAUUSD", "tick", "2024_02", feb, asset_class="metals"
    )

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["source"] == "dukascopy"
    assert ds["asset_class"] == "metals"
    assert ds["symbol"] == "XAUUSD"
    assert ds["timeframe"] == "tick"
    assert ds["rows"] == 4
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-02-29"
    assert ds["segment_count"] == 2
    assert [segment["segment"] for segment in ds["segments"]] == ["2024_01", "2024_02"]
    assert {Path(segment["path"]).name for segment in ds["segments"]} == {
        "XAUUSD_TICK_2024_01.parquet",
        "XAUUSD_TICK_2024_02.parquet",
    }


def _ohlcv_segment_frame(first: str, last: str, base: float) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime([first, last], utc=True),
        "open": [base, base + 1.0],
        "high": [base + 0.1, base + 1.1],
        "low": [base - 0.1, base + 0.9],
        "close": [base + 0.05, base + 1.05],
        "volume": [10, 12],
    })


def test_build_catalog_merges_same_symbol_timeframe_across_sources(tmp_path):
    jan = _ohlcv_segment_frame("2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z", 2000.1)
    feb = _ohlcv_segment_frame("2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z", 2020.1)

    _write_ohlcv(
        tmp_path, "dukascopy", "XAUUSD", "4h", jan,
        asset_class="metals", segment="2024_01",
    )
    _write_ohlcv(
        tmp_path, "histdata", "XAUUSD", "4h", feb,
        asset_class="metals", segment="2024_02",
    )

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "XAUUSD"
    assert ds["timeframe"] == "4h"
    assert ds["source"] == "mixed"
    assert ds["asset_class"] == "metals"
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
        "XAUUSD_H4_2024_01.parquet",
        "XAUUSD_H4_2024_02.parquet",
    }
    assert {Path(segment["path"]).name for segment in ds["segments"]} == {
        "XAUUSD_H4_2024_01.parquet",
        "XAUUSD_H4_2024_02.parquet",
    }


def test_build_catalog_normalizes_timeframe_aliases_and_merges_duplicates(tmp_path):
    jan = _ohlcv_segment_frame("2024-01-01T00:00:00Z", "2024-01-31T23:59:59Z", 1.10)
    feb = _ohlcv_segment_frame("2024-02-01T00:00:00Z", "2024-02-29T23:59:59Z", 1.30)

    # "m1" and "1m" are aliases for the same platform timeframe; the store
    # maps both to the M1 file token, so the files merge into one dataset.
    _write_ohlcv(
        tmp_path, "dukascopy", "EURUSD", "m1", jan,
        asset_class="forex", segment="2024_01",
    )
    _write_ohlcv(
        tmp_path, "dukascopy", "EURUSD", "1m", feb,
        asset_class="forex", segment="2024_02",
    )

    catalog = build_catalog(tmp_path)

    assert len(catalog["ohlcv"]) == 1
    ds = catalog["ohlcv"][0]
    assert ds["symbol"] == "EURUSD"
    assert ds["timeframe"] == "1m"
    assert ds["rows"] == 4
    assert ds["start"] == "2024-01-01"
    assert ds["end"] == "2024-02-29"
    assert ds["file_count"] == 2
    assert {Path(path).name for path in ds["files"]} == {
        "EURUSD_M1_2024_01.parquet",
        "EURUSD_M1_2024_02.parquet",
    }
    assert catalog["summary"]["timeframes"] == ["1m"]


def test_empty_catalog(tmp_path):
    catalog = build_catalog(tmp_path)
    assert catalog["summary"]["total_datasets"] == 0
    assert catalog["ohlcv"] == []
    assert catalog["universes"] == []


def test_build_catalog_recomputes_on_content_change_same_second(
    sample_data_dir, monkeypatch
) -> None:
    """B3: same-mtime rewrite with different bytes must not reuse the cache."""
    first_catalog = build_catalog(sample_data_dir)
    (sample_data_dir / "catalog.json").write_text(json.dumps(first_catalog))

    # Rewrite one parquet with different content but coerce the mtime back
    # so only the size fingerprint distinguishes it.
    target = next((sample_data_dir / "ohlcv").rglob("*.parquet"))
    before = target.stat()
    frame = pd.read_parquet(target)
    frame = pd.concat([frame, frame.iloc[:5]], ignore_index=True)
    frame.to_parquet(target, index=False)
    os.utime(target, ns=(before.st_mtime_ns, before.st_mtime_ns))

    recomputed = build_catalog(sample_data_dir)
    assert recomputed["ohlcv"][0]["rows"] != first_catalog["ohlcv"][0]["rows"]

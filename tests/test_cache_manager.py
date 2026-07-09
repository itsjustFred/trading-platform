"""Tests for data_tools.cache_manager."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from data_tools import cache_manager


def _catalog_payload(data_dir: Path, include_validation: bool) -> dict:
    return {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "data_dir": str(data_dir),
        "summary": {
            "total_datasets": 1,
            "symbols": 1,
            "sources": ["test_source"],
            "timeframes": ["1h"],
            "total_size_bytes": 1024,
        },
        "ohlcv": [
            {
                "source": "test_source",
                "asset_class": "equity",
                "symbol": "AAPL",
                "timeframe": "1h",
                "path": str(
                    data_dir
                    / "ohlcv"
                    / "test_source"
                    / "equity"
                    / "US"
                    / "AAPL"
                    / "1h.parquet"
                ),
                "validation": {"status": "ok"} if include_validation else None,
            }
        ],
        "fundamentals": [],
        "universes": [],
    }


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    cache_manager.invalidate_cache()
    yield
    cache_manager.invalidate_cache()


def test_get_catalog_lazy_builds_and_returns_cached_copy(monkeypatch, tmp_path):
    calls: list[tuple[Path, bool]] = []

    def fake_build_catalog(data_dir: Path, include_validation: bool = False) -> dict:
        calls.append((Path(data_dir), include_validation))
        return _catalog_payload(Path(data_dir), include_validation)

    monkeypatch.setattr(cache_manager, "build_catalog", fake_build_catalog)

    before = cache_manager.get_cache_stats()
    first = cache_manager.get_catalog(tmp_path)
    first["summary"]["symbols"] = 999
    second = cache_manager.get_catalog(tmp_path)
    after = cache_manager.get_cache_stats()

    assert len(calls) == 1
    assert second["summary"]["symbols"] == 1
    assert after["misses"] == before["misses"] + 1
    assert after["hits"] == before["hits"] + 1
    assert after["builds"] == before["builds"] + 1


def test_get_overview_metadata_reuses_cached_value(monkeypatch, tmp_path):
    calls: list[tuple[Path, bool]] = []

    def fake_build_catalog(data_dir: Path, include_validation: bool = False) -> dict:
        calls.append((Path(data_dir), include_validation))
        return _catalog_payload(Path(data_dir), include_validation)

    monkeypatch.setattr(cache_manager, "build_catalog", fake_build_catalog)

    first = cache_manager.get_overview_metadata(tmp_path, include_validation=True)
    second = cache_manager.get_overview_metadata(tmp_path, include_validation=True)

    assert first == second
    assert first["counts"]["ohlcv"] == 1
    assert first["summary"]["sources"] == ["test_source"]
    assert calls == [(tmp_path.resolve(), True)]


def test_invalidate_helpers_evict_matching_entries(monkeypatch, tmp_path):
    calls: list[tuple[Path, bool]] = []

    def fake_build_catalog(data_dir: Path, include_validation: bool = False) -> dict:
        calls.append((Path(data_dir), include_validation))
        return _catalog_payload(Path(data_dir), include_validation)

    monkeypatch.setattr(cache_manager, "build_catalog", fake_build_catalog)

    cache_manager.get_catalog(tmp_path, include_validation=False)
    cache_manager.get_catalog(tmp_path, include_validation=True)
    assert len(calls) == 2

    removed = cache_manager.invalidate_after_download(tmp_path)
    assert removed == 2
    assert cache_manager.get_cache_stats()["entries"] == 0

    cache_manager.get_catalog(tmp_path, include_validation=False)
    assert len(calls) == 3

    removed_resample = cache_manager.invalidate_after_resample(tmp_path)
    assert removed_resample == 1
    stats = cache_manager.get_cache_stats()
    assert stats["invalidation_reasons"]["post_download"] >= 2
    assert stats["invalidation_reasons"]["post_resample"] >= 1


def test_get_catalog_thread_safe_single_build_under_concurrency(monkeypatch, tmp_path):
    build_count = 0
    build_count_lock = threading.Lock()

    def fake_build_catalog(data_dir: Path, include_validation: bool = False) -> dict:
        nonlocal build_count
        with build_count_lock:
            build_count += 1
            serial = build_count
        time.sleep(0.05)
        payload = _catalog_payload(Path(data_dir), include_validation)
        payload["build_serial"] = serial
        return payload

    monkeypatch.setattr(cache_manager, "build_catalog", fake_build_catalog)

    errors: list[Exception] = []
    results: list[dict] = []
    start_barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            start_barrier.wait()
            results.append(cache_manager.get_catalog(tmp_path))
        except Exception as exc:  # pragma: no cover - defensive for thread diagnostics
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 8
    assert build_count == 1
    assert {result["build_serial"] for result in results} == {1}


def test_get_catalog_reads_catalog_json_without_build(monkeypatch, tmp_path):
    payload = _catalog_payload(tmp_path, include_validation=False)
    (tmp_path / "catalog.json").write_text(json.dumps(payload), encoding="utf-8")

    def fail_build_catalog(*_args, **_kwargs):
        raise AssertionError("build_catalog should not be called when catalog.json exists")

    monkeypatch.setattr(cache_manager, "build_catalog", fail_build_catalog)

    loaded = cache_manager.get_catalog(tmp_path, include_validation=False)
    assert loaded.get("ohlcv", [{}])[0].get("symbol") == "AAPL"


def test_get_catalog_normalizes_and_merges_symbol_timeframe_entries_from_disk(monkeypatch, tmp_path):
    raw_catalog = {
        "summary": {"total_datasets": 99, "symbols": 99},
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "dukascopy" / "forex" / "XAUUSD" / "4h_2024_01.parquet"),
                "symbol": "XAUUSD",
                "timeframe": "4h",
                "source": "dukascopy",
                "asset_class": "forex",
                "size_bytes": 100,
                "rows": 3,
                "start": "2024-01-01",
                "end": "2024-01-31",
            },
            {
                "path": str(tmp_path / "ohlcv" / "histdata" / "forex" / "XAUUSD" / "4h_2024_02.parquet"),
                "symbol": "XAUUSD",
                "timeframe": "4h",
                "source": "histdata",
                "asset_class": "forex",
                "size_bytes": 200,
                "rows": 2,
                "start": "2024-02-01",
                "end": "2024-02-29",
            },
        ],
        "fundamentals": [],
        "universes": [],
    }
    (tmp_path / "catalog.json").write_text(json.dumps(raw_catalog), encoding="utf-8")

    def fail_build_catalog(*_args, **_kwargs):
        raise AssertionError("build_catalog should not be called when catalog.json exists")

    monkeypatch.setattr(cache_manager, "build_catalog", fail_build_catalog)

    loaded = cache_manager.get_catalog(tmp_path, include_validation=False)
    assert loaded["summary"]["total_datasets"] == 1
    assert loaded["summary"]["symbols"] == 1
    assert loaded["summary"]["sources"] == ["dukascopy", "histdata"]
    assert loaded["summary"]["timeframes"] == ["4h"]
    assert loaded["summary"]["total_size_bytes"] == 300

    assert len(loaded["ohlcv"]) == 1
    merged = loaded["ohlcv"][0]
    assert merged["symbol"] == "XAUUSD"
    assert merged["timeframe"] == "4h"
    assert merged["source"] == "mixed"
    assert merged["sources"] == ["dukascopy", "histdata"]
    assert merged["row_count"] == 5
    assert merged["rows"] == 5
    assert merged["start"] == "2024-01-01"
    assert merged["end"] == "2024-02-29"
    assert merged["file_count"] == 2


def test_get_catalog_normalizes_tick_segment_symbols_from_disk(monkeypatch, tmp_path):
    raw_catalog = {
        "summary": {"total_datasets": 999, "symbols": 999},
        "ohlcv": [
            {
                "path": str(
                    tmp_path / "ohlcv" / "dukascopy" / "metals" / "XAUUSD" / "tick_2003_05.parquet"
                ),
                "symbol": "tick_2003_05",
                "timeframe": "1d",
                "source": "dukascopy",
                "asset_class": "metals",
                "size_bytes": 100,
                "rows": 10,
                "start": "2003-05-05",
                "end": "2003-05-30",
            },
            {
                "path": str(
                    tmp_path / "ohlcv" / "dukascopy" / "metals" / "XAUUSD" / "tick_2003_06.parquet"
                ),
                "symbol": "tick_2003_06",
                "timeframe": "1d",
                "source": "dukascopy",
                "asset_class": "metals",
                "size_bytes": 120,
                "rows": 12,
                "start": "2003-06-02",
                "end": "2003-06-30",
            },
        ],
        "fundamentals": [],
        "universes": [],
    }
    (tmp_path / "catalog.json").write_text(json.dumps(raw_catalog), encoding="utf-8")

    def fail_build_catalog(*_args, **_kwargs):
        raise AssertionError("build_catalog should not be called when catalog.json exists")

    monkeypatch.setattr(cache_manager, "build_catalog", fail_build_catalog)

    loaded = cache_manager.get_catalog(tmp_path, include_validation=False)

    assert loaded["summary"]["total_datasets"] == 1
    assert loaded["summary"]["symbols"] == 1
    assert loaded["summary"]["sources"] == ["dukascopy"]
    assert loaded["summary"]["timeframes"] == ["tick"]

    assert len(loaded["ohlcv"]) == 1
    merged = loaded["ohlcv"][0]
    assert merged["symbol"] == "XAUUSD"
    assert merged["timeframe"] == "tick"
    assert merged["source"] == "dukascopy"
    assert merged["sources"] == ["dukascopy"]
    assert merged["row_count"] == 22
    assert merged["rows"] == 22
    assert merged["start"] == "2003-05-05"
    assert merged["end"] == "2003-06-30"
    assert merged["file_count"] == 2
    assert {Path(path).name for path in merged["files"]} == {
        "tick_2003_05.parquet",
        "tick_2003_06.parquet",
    }


def test_get_catalog_normalizes_timeframe_aliases_from_disk(monkeypatch, tmp_path):
    raw_catalog = {
        "summary": {"total_datasets": 999, "symbols": 999},
        "ohlcv": [
            {
                "path": str(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "m1.parquet"),
                "symbol": "EURUSD",
                "timeframe": "m1",
                "source": "dukascopy",
                "asset_class": "forex",
                "size_bytes": 100,
                "rows": 10,
                "start": "2024-01-01",
                "end": "2024-01-31",
            },
            {
                "path": str(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1m.parquet"),
                "symbol": "EURUSD",
                "timeframe": "1m",
                "source": "dukascopy",
                "asset_class": "forex",
                "size_bytes": 120,
                "rows": 12,
                "start": "2024-02-01",
                "end": "2024-02-29",
            },
        ],
        "fundamentals": [],
        "universes": [],
    }
    (tmp_path / "catalog.json").write_text(json.dumps(raw_catalog), encoding="utf-8")

    def fail_build_catalog(*_args, **_kwargs):
        raise AssertionError("build_catalog should not be called when catalog.json exists")

    monkeypatch.setattr(cache_manager, "build_catalog", fail_build_catalog)

    loaded = cache_manager.get_catalog(tmp_path, include_validation=False)

    assert loaded["summary"]["total_datasets"] == 1
    assert loaded["summary"]["symbols"] == 1
    assert loaded["summary"]["timeframes"] == ["1m"]
    assert len(loaded["ohlcv"]) == 1
    merged = loaded["ohlcv"][0]
    assert merged["symbol"] == "EURUSD"
    assert merged["timeframe"] == "1m"
    assert merged["row_count"] == 22
    assert merged["rows"] == 22
    assert merged["file_count"] == 2

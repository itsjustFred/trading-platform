"""Unit tests for the strategy data-gate in tests/strategies/conftest.py.

These cover the three silent-skip defects of Cleanup 17-B:

- M1: missing local data silently skips whole modules. A strict mode
      (``FAIL_ON_MISSING_DATA=1``) must be able to turn those skips into
      failures (Decision Record 2026-08-31: skip locally allowed, CI can
      opt into strict mode).
- M2: the coverage check unioned shard extents, so Jan-Mar + Dec shards
      satisfied an Apr-Nov request. Coverage is now verified by shard
      contiguity: sorted extents must chain without a hole across the
      requested window (so year-sharded datasets stay valid).
- M3: unreadable/corrupt parquet files were swallowed into "no coverage".
      Corruption must surface in the skip/failure reason.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "strategies_conftest_under_test",
        Path(__file__).resolve().parent / "strategies" / "conftest.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(monkeypatch, tmp_path):
    module = _load_gate()
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "FAIL_ON_MISSING_DATA", False)
    return module


def _write_shard(path: Path, start: str, end: str, *, corrupt: bool = False) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_bytes(b"not-a-parquet-file")
        return
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(start, end, freq="1h", tz="UTC"),
            "close": 1.0,
        }
    )
    frame.to_parquet(path, index=False)


def test_strict_mode_fails_on_missing_dataset(gate, monkeypatch):
    monkeypatch.setattr(gate, "FAIL_ON_MISSING_DATA", True)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")

    assert "missing OHLCV dataset" in str(excinfo.value)


def test_lenient_mode_skips_on_missing_dataset(gate):
    with pytest.raises(pytest.skip.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")

    assert "missing OHLCV dataset" in str(excinfo.value)


def test_lenient_mode_skips_on_insufficient_coverage(gate, tmp_path):
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "1h.parquet", "2025-01-01", "2025-02-15"
    )

    with pytest.raises(pytest.skip.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")

    assert "insufficient OHLCV coverage" in str(excinfo.value)


def test_strict_mode_fails_on_insufficient_coverage(gate, monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "FAIL_ON_MISSING_DATA", True)
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "1h.parquet", "2025-01-01", "2025-02-15"
    )

    with pytest.raises(pytest.fail.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")

    assert "insufficient OHLCV coverage" in str(excinfo.value)


def test_coverage_check_does_not_union_shard_extents(gate, tmp_path):
    """M2: shards with a hole between them must not pass as 'covered'."""
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "shard-a" / "1h.parquet",
        "2025-01-01",
        "2025-03-01",
    )
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "shard-b" / "1h.parquet",
        "2025-12-01",
        "2025-12-31",
    )

    assert not gate._covers_requested_period(
        "US500", "1h", "2025-01-01", "2025-12-31"
    )


def test_contiguous_year_shards_cover_the_window(gate, tmp_path):
    """Year-sharded datasets are legitimate: sorted extents must chain."""
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "2024" / "1h.parquet",
        "2024-01-01",
        "2025-01-01",
    )
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "2025" / "1h.parquet",
        "2025-01-01",
        "2025-09-30",
    )

    gate._require_ohlcv_coverage("US500", "1h", "2024-01-01", "2025-06-30")


def test_gap_between_shards_is_reported_with_labels(gate, tmp_path):
    """A hole between shards is a coverage failure naming both shards."""
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "a" / "1h.parquet",
        "2025-01-01",
        "2025-03-01",
    )
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "b" / "1h.parquet",
        "2025-09-01",
        "2025-12-31",
    )

    with pytest.raises(pytest.skip.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-12-31")

    message = str(excinfo.value)
    assert "insufficient OHLCV coverage" in message
    assert "gap between" in message


def test_corrupt_parquet_is_surfaced_not_silently_ignored(gate, tmp_path):
    """M3: an unreadable file must be named in the reason, not dropped."""
    corrupt = tmp_path / "ohlcv" / "US500" / "broken" / "1h.parquet"
    _write_shard(corrupt, "2025-01-01", "2025-03-01", corrupt=True)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")

    message = str(excinfo.value)
    assert "unreadable" in message
    assert "broken" in message


def test_corrupt_parquet_fails_with_strict_mode(gate, monkeypatch, tmp_path):
    """A corrupt file next to a covering file is still a hard failure in strict mode."""
    corrupt = tmp_path / "ohlcv" / "US500" / "broken" / "1h.parquet"
    _write_shard(corrupt, "2025-01-01", "2025-03-01", corrupt=True)
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "good" / "1h.parquet",
        "2025-01-01",
        "2025-03-31",
    )

    monkeypatch.setattr(gate, "FAIL_ON_MISSING_DATA", True)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")
    assert "unreadable" in str(excinfo.value)


def test_fully_covering_single_file_passes(gate, tmp_path):
    _write_shard(
        tmp_path / "ohlcv" / "US500" / "1h.parquet", "2025-01-01", "2025-04-01"
    )

    gate._require_ohlcv_coverage("US500", "1h", "2025-01-01", "2025-03-31")


def test_session_finish_summary_counts_data_skips(gate, tmp_path, capsys):
    """M1 visibility: the gate must report how many modules it skipped."""
    gate.record_data_skip("missing OHLCV dataset for US500/1h")
    gate.record_data_skip("missing OHLCV dataset for SPY/1d")

    gate.summarize_data_skips(
        config=type("Config", (), {"pluginmanager": type("PM", (), {"get_plugin": lambda self, name: None})()})()
    )

    output = capsys.readouterr().out
    assert "2" in output
    assert "missing OHLCV dataset" in output


def test_session_finish_summary_is_silent_without_skips(gate, capsys):
    gate.summarize_data_skips(
        config=type("Config", (), {"pluginmanager": type("PM", (), {"get_plugin": lambda self, name: None})()})()
    )

    assert capsys.readouterr().out == ""

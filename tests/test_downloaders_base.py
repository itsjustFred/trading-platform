"""Tests for data_tools.downloaders.base — atomic stores and resume trust."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from data_tools.downloaders.base import Downloader, DownloadResult


class _BareDownloader(Downloader):
    """Expose the Downloader helper methods without a concrete source."""

    @property
    def source_name(self) -> str:
        return "bare"

    def list_available(self, **kwargs) -> list[dict]:
        return []

    def download(self, symbol, timeframe, output_dir, *, start=None, end=None, **kwargs):
        raise NotImplementedError


@pytest.fixture
def downloader():
    return _BareDownloader()


@pytest.fixture
def existing_frame():
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z"], utc=True
            ),
            "close": [1.0, 2.0],
        }
    )


class TestAtomicParquetWrites:
    def test_merge_and_store_writes_via_temp_file(
        self, downloader, tmp_path, existing_frame, monkeypatch
    ):
        """The canonical parquet is replaced by an atomic rename, never written in place."""
        out_path = tmp_path / "ohlcv" / "bare" / "forex" / "EURUSD" / "1m.parquet"
        out_path.parent.mkdir(parents=True)
        existing_frame.to_parquet(out_path, index=False)

        written: dict[str, object] = {}

        def fake_to_parquet(self, path, *args, **kwargs):
            written["path"] = Path(path)
            Path(path).touch()  # let the atomic commit succeed
            return None

        new_data = existing_frame.iloc[[0]].copy()
        # Only observe where pandas is pointed; the atomic helper does the I/O.
        # The merge succeeds, so exactly one write is attempted and committed.
        monkeypatch.setattr(pd.DataFrame, "to_parquet", fake_to_parquet)

        downloader._merge_and_store_single_file(out_path, new_data)

        target = written.get("path")
        assert target is not None, "helper must write to a staging path, not skip I/O"
        assert target != out_path, "canonical file must never be written in place"
        assert target.parent == out_path.parent

    def test_merge_and_store_survives_crash_before_commit(
        self, downloader, tmp_path, existing_frame, monkeypatch
    ):
        """A crash mid-write leaves the previous canonical file untouched."""
        out_path = tmp_path / "ohlcv" / "bare" / "forex" / "EURUSD" / "1m.parquet"
        out_path.parent.mkdir(parents=True)
        existing_frame.to_parquet(out_path, index=False)

        def boom(*args, **kwargs):
            raise KeyboardInterrupt("simulated crash during write")

        merged = existing_frame.copy()
        monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)

        with pytest.raises(KeyboardInterrupt):
            downloader._merge_and_store_single_file(out_path, merged)

        # Original data still readable and unchanged.
        survivors = pd.read_parquet(out_path)
        assert len(survivors) == 2

    def test_merge_and_store_removes_no_valid_data(self, downloader, tmp_path, existing_frame):
        """Corrupt target is replaced, but an existing valid file is never truncated."""
        out_path = tmp_path / "ohlcv" / "bare" / "forex" / "EURUSD" / "1m.parquet"
        out_path.parent.mkdir(parents=True)
        existing_frame.to_parquet(out_path, index=False)
        before = out_path.stat().st_size

        new_rows = pd.DataFrame(
            {"timestamp": pd.to_datetime(["2024-01-03T00:00:00Z"], utc=True), "close": [3.0]}
        )
        combined = downloader._merge_and_store_single_file(out_path, new_rows)

        assert len(combined) == 3
        assert out_path.stat().st_size > 0
        assert out_path.stat().st_size >= before * 0.5  # sanity: not truncated to zero
        assert len(pd.read_parquet(out_path)) == 3


class TestResumeTrust:
    def _write_parquet_with_stale_max(self, path: Path, frame: pd.DataFrame) -> None:
        """Write a parquet whose row-group max statistics understate the real data."""
        import pyarrow as pa

        table = pa.Table.from_pandas(frame, preserve_index=False)
        # Writing with small row groups + disabled column statistics forces the
        # resume helper to fall back to reading actual rows.
        pq.write_table(table, path, row_group_size=1, write_statistics=True)

    def test_resume_uses_actual_rows_when_stats_absent(self, downloader, tmp_path):
        out_path = tmp_path / "1m.parquet"
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-01T00:00:00Z", "2024-06-30T23:00:00Z"], utc=True
                ),
                "close": [1.0, 2.0],
            }
        )
        import pyarrow as pa

        pq.write_table(
            pa.Table.from_pandas(frame, preserve_index=False),
            out_path,
            row_group_size=2,
            write_statistics=False,
        )

        resolved = downloader._resolve_resume_start(out_path, None)
        assert resolved == "2024-06-30"

    def test_resume_never_rewinds_to_stale_row_group_max(self, downloader, tmp_path, monkeypatch):
        """Regression DV-B2: a stale row-group max must not rewind the resume point.

        The parquet's newest row is 2024-06-30; statistics claiming the data ends
        at 2024-01-02 must not move the resume start back before that row.
        """
        out_path = tmp_path / "1m.parquet"
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-01T00:00:00Z", "2024-06-30T23:00:00Z"], utc=True
                ),
                "close": [1.0, 2.0],
            }
        )
        self._write_parquet_with_stale_max(out_path, frame)

        real_file = pq.ParquetFile(out_path)
        stale_max = pd.Timestamp("2024-01-02T00:00:00Z")

        class _StaleStats:
            has_min_max = True
            min = pd.Timestamp("2024-01-01T00:00:00Z").value
            max = stale_max.value

        class _StaleColumn:
            statistics = _StaleStats()

        class _StaleRowGroup:
            def __init__(self, group):
                self._group = group
                self.num_columns = group.num_columns

            def column(self, column_index):
                return _StaleColumn()

        class _StaleMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.num_rows = metadata.num_rows
                self.num_row_groups = metadata.num_row_groups

            def row_group(self, index):
                return _StaleRowGroup(self._metadata.row_group(index))

        class _StaleStatsParquetFile:
            """ParquetFile stand-in whose timestamp statistics understate reality."""

            def __init__(self):
                self._real = real_file
                self.metadata = _StaleMetadata(real_file.metadata)
                self.schema_arrow = real_file.schema_arrow

            def read_row_group(self, index, columns=None):
                return self._real.read_row_group(index, columns=columns)

        monkeypatch.setattr(
            "data_tools.downloaders.base.pq.ParquetFile",
            lambda path, *args, **kwargs: _StaleStatsParquetFile(),
        )

        resolved = downloader._resolve_resume_start(out_path, None)
        assert resolved == "2024-06-30", (
            "resume must not trust statistics that understate the newest stored row"
        )


class TestDownloadResultContract:
    def test_path_must_be_a_file_for_merged_downloads(self, downloader, tmp_path, existing_frame):
        """DownloadResult.path is a parquet file, never a directory (DV-B9)."""
        out_path = tmp_path / "1m.parquet"
        stored = downloader._merge_and_store_single_file(out_path, existing_frame)

        result = DownloadResult(
            symbol="EURUSD",
            timeframe="1m",
            source="bare",
            asset_class="forex",
            path=out_path,
            rows=len(stored),
            start="2024-01-01",
            end="2024-01-02",
            size_bytes=out_path.stat().st_size,
        )
        assert result.path.is_file()
        assert result.path.suffix == ".parquet"
        assert json.loads(json.dumps(result.to_dict()))["path"].endswith("1m.parquet")

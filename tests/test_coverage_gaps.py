"""Coverage validation with internal-hole detection and failed-download visibility.

Parent Decision Record (2026-08-31): coverage checks fail closed on
significant internal gaps; small missing-bar windows may warn, larger gaps
fail. Proposed default (open): internal gap > 3 calendar days fails.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from data_tools import auto_download
from data_tools.auto_download import (
    DataRequirement,
    analyze_ohlcv_coverage,
    find_internal_gaps,
    has_ohlcv_bars_in_range,
)


def _write_bars(path: Path, timestamps: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": [1.0] * len(timestamps),
            "high": [1.1] * len(timestamps),
            "low": [0.9] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [10] * len(timestamps),
        }
    )
    frame.to_parquet(path, index=False)


class TestInternalGapDetection:
    def test_multi_year_hole_between_contiguous_runs_is_detected(self, tmp_path):
        """Regression DV-B15: min/max only checks pass multi-year holes."""
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [
                "2019-01-01T00:00:00Z",
                "2019-01-01T01:00:00Z",
                "2024-06-01T00:00:00Z",
                "2024-06-01T01:00:00Z",
            ],
        )
        gaps = find_internal_gaps(tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD")
        assert gaps, "a 5-year internal hole must be detected"
        assert gaps[0]["gap_days"] > 1000

    def test_small_gap_warns_large_gap_fails(self, tmp_path):
        """Decision Record: small missing windows warn, significant gaps fail."""
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [
                "2024-01-01T00:00:00Z",
                "2024-01-01T01:00:00Z",
                # ~1.5 calendar days missing -> warn (inside the fail threshold)
                "2024-01-02T12:30:00Z",
                "2024-01-02T13:00:00Z",
            ],
        )
        report = analyze_ohlcv_coverage(
            tmp_path,
            DataRequirement(symbol="EURUSD", timeframe="1h", start="2024-01-01", end="2024-01-02"),
        )
        assert report["status"] == "warning"
        assert report["gaps"] and 0 < report["gaps"][0]["gap_days"] <= 3

        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "GBPUSD" / "1h.parquet",
            [
                "2024-01-01T00:00:00Z",
                "2024-01-01T01:00:00Z",
                # 30 calendar days missing -> fail
                "2024-01-31T00:00:00Z",
                "2024-01-31T01:00:00Z",
            ],
        )
        failed = analyze_ohlcv_coverage(
            tmp_path,
            DataRequirement(symbol="GBPUSD", timeframe="1h", start="2024-01-01", end="2024-01-31"),
        )
        assert failed["status"] == "failed"
        assert failed["gaps"] and failed["gaps"][0]["gap_days"] > 3

    def test_no_internal_gap_reports_ok(self, tmp_path):
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [
                "2024-01-01T00:00:00Z",
                "2024-01-01T01:00:00Z",
                "2024-01-02T00:00:00Z",
            ],
        )
        report = analyze_ohlcv_coverage(
            tmp_path,
            DataRequirement(symbol="EURUSD", timeframe="1h", start="2024-01-01", end="2024-01-02"),
        )
        assert report["status"] == "ok"
        assert report["gaps"] == []

    def test_missing_file_fails_closed(self, tmp_path):
        report = analyze_ohlcv_coverage(
            tmp_path,
            DataRequirement(symbol="USDCAD", timeframe="1h", start="2024-01-01", end="2024-01-02"),
        )
        assert report["status"] == "failed"
        assert "error" in report


class TestFailedDownloadVisibility:
    def test_ensure_backtest_data_raises_when_download_produces_nothing(self, tmp_path, monkeypatch):
        """A failed download must surface as an error, not as silent coverage."""
        calls: list[dict] = []

        def failing_download(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            auto_download, "_download_missing_ohlcv_data", lambda **kwargs: failing_download(**kwargs)
        )

        with pytest.raises(RuntimeError, match="did not provide required OHLCV coverage"):
            auto_download.ensure_backtest_data(
                symbols=["EURUSD"],
                timeframe="1h",
                start="2024-01-01",
                end="2024-01-02",
                data_dir=tmp_path,
            )
        assert calls, "download attempt must have been made"

    def test_has_ohlcv_bars_in_range_ignores_global_min_max_outside_range(self, tmp_path):
        """Bars outside the requested window must not count as coverage."""
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            ["2019-01-01T00:00:00Z", "2019-01-01T01:00:00Z"],
        )
        assert not has_ohlcv_bars_in_range(
            symbol="EURUSD", timeframe="1h", start="2024-01-01", end="2024-01-02", data_dir=tmp_path
        )


class TestGapDetectionSingleFile:
    def test_hole_inside_single_file_row_groups_is_detected(self, tmp_path):
        """A single file whose bars jump by years must be caught (DV-B15)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        target = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
        target.parent.mkdir(parents=True)
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2019-01-01T00:00:00Z",
                        "2019-01-01T01:00:00Z",
                        "2024-06-01T00:00:00Z",
                        "2024-06-01T01:00:00Z",
                    ],
                    utc=True,
                ),
                "open": [1.0] * 4,
                "high": [1.1] * 4,
                "low": [0.9] * 4,
                "close": [1.0] * 4,
                "volume": [10] * 4,
            }
        )
        pq.write_table(
            pa.Table.from_pandas(frame, preserve_index=False), target, row_group_size=2
        )

        gaps = find_internal_gaps(target.parent, "1h")
        assert gaps, "hole inside one file must be detected"
        assert gaps[0]["gap_days"] > 1000

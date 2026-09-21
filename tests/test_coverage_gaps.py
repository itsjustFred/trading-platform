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
    has_sufficient_segment_coverage,
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
        assert report["gaps"], "a ~1.5-day internal hole must be reported"
        assert 0 < report["gaps"][0]["gap_days"] <= 3

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
        assert failed["gaps"], "a ~30-day internal hole must be reported"
        assert failed["gaps"][0]["gap_days"] > 3

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
    def test_ensure_backtest_data_raises_when_download_produces_nothing(
        self, tmp_path, monkeypatch
    ):
        """A failed download must surface as an error, not as silent coverage."""
        calls: list[dict] = []

        def failing_download(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            auto_download,
            "_download_missing_ohlcv_data",
            lambda **kwargs: failing_download(**kwargs),
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


class TestSegmentCoverageGate:
    """#86: a 1-bar month must be rejected; dense segments pass."""

    def test_single_bar_in_month_is_rejected(self, tmp_path):
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            ["2024-01-15T00:00:00Z"],
        )
        # Presence-only helper still sees the bar (freshness semantics).
        assert has_ohlcv_bars_in_range(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-01", end="2024-01-31", data_dir=tmp_path,
        )
        # Strict gate rejects it: 1/721 bars, huge max gap.
        assert not has_sufficient_segment_coverage(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-01", end="2024-01-31", data_dir=tmp_path,
        )

    def test_dense_segment_passes(self, tmp_path):
        stamps = pd.date_range("2024-01-01", "2024-01-02", freq="1h", tz="UTC")
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in stamps],
        )
        assert has_sufficient_segment_coverage(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-01", end="2024-01-02", data_dir=tmp_path,
        )

    def test_single_large_gap_fails_despite_density(self, tmp_path):
        # Two dense halves with a 12h hole (>3x 1h) in the middle.
        first = pd.date_range("2024-01-01", "2024-01-01T11:00", freq="1h", tz="UTC")
        second = pd.date_range("2024-01-01T23:00", "2024-01-02T10:00", freq="1h", tz="UTC")
        stamps = list(first) + list(second)
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in stamps],
        )
        assert not has_sufficient_segment_coverage(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-01", end="2024-01-02", data_dir=tmp_path,
        )


class TestValidateGapBounds:
    """#86: validate_parquet must report exact gap boundaries."""

    def test_gap_bounds_are_exact_after_dropna(self, tmp_path):
        from data_tools.validate import validate_parquet

        # Non-default index + a NaT row: label index != positional index,
        # which the old iloc[label] code mis-reported.
        frame = pd.DataFrame(
            {
                "timestamp": [
                    "2024-01-01T00:00:00Z",
                    "not-a-timestamp",
                    "2024-01-01T01:00:00Z",
                    "2024-01-05T00:00:00Z",
                ],
                "open": [1.0] * 4,
                "high": [1.1] * 4,
                "low": [0.9] * 4,
                "close": [1.0] * 4,
                "volume": [10] * 4,
            }
        )
        target = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False)
        result = validate_parquet(target)
        assert result.gaps, "the 4-day hole must be reported"
        assert "2024-01-01" in result.gaps[0]["start"] and "01:00" in result.gaps[0]["start"]
        assert "2024-01-05" in result.gaps[0]["end"] and "00:00" in result.gaps[0]["end"]


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

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


class TestSegmentCoverageWeekends:
    """Weekends are non-trading: no false rejects, real holes still fail."""

    def test_daily_equity_segment_over_weekend_passes(self, tmp_path):
        # Mon 2024-01-01 .. Fri 2024-01-12, weekdays only (real equity feed).
        stamps = pd.date_range("2024-01-01", "2024-01-12", freq="B", tz="UTC")
        _write_bars(
            tmp_path / "ohlcv" / "test" / "AAPL" / "1d.parquet",
            [ts.isoformat() for ts in stamps],
        )
        assert has_sufficient_segment_coverage(
            symbol="AAPL", timeframe="1d",
            start="2024-01-01", end="2024-01-12", data_dir=tmp_path,
        )

    def test_hourly_segment_with_weekend_gap_passes(self, tmp_path):
        # Fri 22:00 -> Mon 00:00 UTC gap is weekend, not missing data.
        stamps = list(pd.date_range("2024-01-05T00:00", "2024-01-05T22:00", freq="1h", tz="UTC"))
        stamps += list(pd.date_range("2024-01-08T00:00", "2024-01-08T10:00", freq="1h", tz="UTC"))
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in stamps],
        )
        assert has_sufficient_segment_coverage(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-05", end="2024-01-08T10:00", data_dir=tmp_path,
        )

    def test_sunday_to_monday_hourly_fx_weekend_gap_is_expected(self):
        from data_tools.validate import _gap_is_expected_nontrading

        assert _gap_is_expected_nontrading(
            pd.Timestamp("2024-01-07T00:00:00Z"),
            pd.Timestamp("2024-01-08T00:00:00Z"),
            "forex",
            "1h",
        )

    def test_session_window_overrides_generic_weekday_grid(self, tmp_path):
        stamps = []
        for day in ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"):
            stamps.extend(
                pd.date_range(f"{day}T14:30", periods=5, freq="1h", tz="UTC")
            )
        _write_bars(
            tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1h.parquet",
            [stamp.isoformat() for stamp in stamps],
        )

        assert has_sufficient_segment_coverage(
            symbol="AAPL",
            timeframe="1h",
            start="2024-01-02T00:00:00Z",
            end="2024-01-05T23:59:59Z",
            data_dir=tmp_path,
            session={"start": "14:30", "end": "19:30"},
        )

    def test_midweek_hole_still_fails(self, tmp_path):
        # 12h hole Tue->Wed (no weekend involved) exceeds 3x 1h.
        stamps = list(pd.date_range("2024-01-09T00:00", "2024-01-09T05:00", freq="1h", tz="UTC"))
        stamps += list(pd.date_range("2024-01-09T17:00", "2024-01-09T23:00", freq="1h", tz="UTC"))
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in stamps],
        )
        assert not has_sufficient_segment_coverage(
            symbol="EURUSD", timeframe="1h",
            start="2024-01-09", end="2024-01-09T23:00", data_dir=tmp_path,
        )


class TestValidateHolidayCalendar:
    """B3: NYSE holidays are not data holes (equity-like paths only)."""

    def test_weekend_boundary_rejects_more_than_three_calendar_days(self) -> None:
        from data_tools.validate import _weekend_boundary

        assert not _weekend_boundary(
            pd.Timestamp("2024-01-05T20:00:00Z"),
            pd.Timestamp("2024-01-09T02:00:00Z"),
        )

    def _write_equity_bars(self, root: Path, timestamps: list[str]) -> Path:
        import pandas as pd

        target = root / "ohlcv" / "test" / "equity" / "AAPL" / "1d.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, utc=True),
                "open": [100.0] * len(timestamps),
                "high": [101.0] * len(timestamps),
                "low": [99.0] * len(timestamps),
                "close": [100.5] * len(timestamps),
                "volume": [1000.0] * len(timestamps),
            }
        )
        frame.to_parquet(target, index=False)
        return target

    def test_session_overlap_starts_after_prior_bar_close(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-01-09T15:00:00Z"),
            pd.Timestamp("2024-01-10T09:30:00Z"),
            "equity",
            "1h",
            session={"start": "09:30", "end": "16:00"},
        ) == "overnight"

    def test_validate_uses_shared_timeframe_step_mapping(self) -> None:
        import data_tools.validate as validate_module

        assert validate_module.TIMEFRAME_MINUTES is auto_download.TIMEFRAME_MINUTES

    def test_july4_gap_skipped_for_equity(self, tmp_path, monkeypatch) -> None:
        import data_tools.validate as validate_module

        monkeypatch.setattr(validate_module, "DATA_DIR", tmp_path)
        # Wed 2024-07-03 -> Fri 2024-07-05: Thursday July 4th (holiday).
        target = self._write_equity_bars(
            tmp_path, ["2024-07-03T00:00:00Z", "2024-07-05T00:00:00Z"]
        )
        result = validate_module.validate_parquet(target)
        assert result.gaps == []

    def test_long_outage_spanning_holiday_remains_an_error(self, tmp_path, monkeypatch) -> None:
        import data_tools.validate as validate_module

        monkeypatch.setattr(validate_module, "DATA_DIR", tmp_path)
        target = self._write_equity_bars(
            tmp_path, ["2024-07-03T00:00:00Z", "2024-07-08T00:00:00Z"]
        )

        result = validate_module.validate_parquet(target)

        assert len(result.gaps) == 1

    def test_holiday_does_not_hide_an_additional_weekday_outage(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-07-03T16:00:00Z"),
            pd.Timestamp("2024-07-09T09:30:00Z"),
            "forex",
            "1h",
            holidays=["2024-07-04"],
        ) == "data"

    def test_same_gap_flagged_for_forex(self, tmp_path, monkeypatch) -> None:
        import pandas as pd

        import data_tools.validate as validate_module

        monkeypatch.setattr(validate_module, "DATA_DIR", tmp_path)
        # 1h bars: the 2-day span exceeds the 180-minute threshold even
        # though Thursday is a holiday (FX prints through it).
        target = tmp_path / "ohlcv" / "test" / "forex" / "EURUSD" / "1h.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-07-03T00:00:00Z", "2024-07-05T00:00:00Z"], utc=True
                ),
                "open": [1.0, 1.0],
                "high": [1.1, 1.1],
                "low": [0.9, 0.9],
                "close": [1.0, 1.0],
                "volume": [10, 10],
            }
        ).to_parquet(target, index=False)
        result = validate_module.validate_parquet(target)
        assert len(result.gaps) == 1

    def test_validate_parquet_accepts_explicit_session_boundaries(
        self, tmp_path, monkeypatch
    ) -> None:
        import data_tools.validate as validate_module

        monkeypatch.setattr(validate_module, "DATA_DIR", tmp_path)
        target = tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1h.parquet"
        target.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-09T16:00:00Z", "2024-01-10T09:30:00Z"], utc=True
                ),
                "open": [100.0, 100.0],
                "high": [100.5, 100.5],
                "low": [99.5, 99.5],
                "close": [100.0, 100.0],
                "volume": [10, 10],
            }
        ).to_parquet(target, index=False)

        result = validate_module.validate_parquet(
            target,
            session_start="09:30",
            session_end="16:00",
        )

        assert result.gaps == []

    def test_session_gap_during_trading_window_remains_a_data_gap(
        self, tmp_path, monkeypatch
    ) -> None:
        import data_tools.validate as validate_module

        monkeypatch.setattr(validate_module, "DATA_DIR", tmp_path)
        target = tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1h.parquet"
        target.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-09T10:00:00Z", "2024-01-09T14:00:00Z"], utc=True
                ),
                "open": [100.0, 100.0],
                "high": [100.5, 100.5],
                "low": [99.5, 99.5],
                "close": [100.0, 100.0],
                "volume": [10, 10],
            }
        ).to_parquet(target, index=False)

        result = validate_module.validate_parquet(
            target,
            session={"start": "09:30", "end": "16:00"},
        )

        assert len(result.gaps) == 1
        assert result.gaps[0]["classification"] == "session"

    def test_session_metadata_classifies_weekday_overnight_gap(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-01-09T19:30:00Z"),
            pd.Timestamp("2024-01-10T09:30:00Z"),
            "equity",
            "1h",
            session={"start": "09:30", "end": "16:00"},
        ) == "overnight"

    def test_overnight_session_metadata_classifies_weekday_close_gap(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-01-09T17:00:00Z"),
            pd.Timestamp("2024-01-09T18:00:00Z"),
            "futures",
            "1h",
            session={"start": "18:00", "end": "17:00"},
        ) == "overnight"

    def test_session_metadata_keeps_intraday_gap_as_data_loss(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-01-09T10:00:00Z"),
            pd.Timestamp("2024-01-09T14:00:00Z"),
            "equity",
            "1h",
            session={"start": "09:30", "end": "16:00"},
        ) == "session"

    def test_holiday_metadata_classifies_only_holiday_gap(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-07-03T16:00:00Z"),
            pd.Timestamp("2024-07-05T09:30:00Z"),
            "equity",
            "1h",
            session={"start": "09:30", "end": "16:00"},
            holidays=["2024-07-04"],
        ) == "holiday"
        assert _gap_classification(
            pd.Timestamp("2024-07-03T16:00:00Z"),
            pd.Timestamp("2024-07-08T09:30:00Z"),
            "equity",
            "1h",
            session={"start": "09:30", "end": "16:00"},
            holidays=["2024-07-04"],
        ) == "session"

    def test_weekend_and_missing_session_are_distinct(self) -> None:
        from data_tools.validate import _gap_classification

        assert _gap_classification(
            pd.Timestamp("2024-01-05T22:00:00Z"),
            pd.Timestamp("2024-01-08T00:00:00Z"),
            "forex",
            "1h",
        ) == "weekend"
        assert _gap_classification(
            pd.Timestamp("2024-01-09T16:00:00Z"),
            pd.Timestamp("2024-01-10T09:30:00Z"),
            "forex",
            "1h",
        ) == "data"


class TestEnsureBacktestDataGapGate:
    """C5: ensure_backtest_data fails on internal gaps, not just bounds."""

    def test_internal_gap_fails_coverage(self, tmp_path, monkeypatch):
        from data_tools import auto_download

        # Two dense clusters with a 10-day hole inside the required window.
        early = pd.date_range("2024-01-01", "2024-01-05", freq="1h", tz="UTC")
        late = pd.date_range("2024-01-15", "2024-01-20", freq="1h", tz="UTC")
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in list(early) + list(late)],
        )
        monkeypatch.setattr(
            auto_download,
            "_download_missing_ohlcv_data",
            lambda **kwargs: None,
        )
        with pytest.raises(RuntimeError, match="internal gaps"):
            auto_download.ensure_backtest_data(
                symbols=["EURUSD"],
                timeframe="1h",
                start="2024-01-01",
                end="2024-01-20",
                data_dir=tmp_path,
            )


def test_segment_ending_on_weekend_counts_trading_bars_only(tmp_path):
    """Pre-mortem R1: a Mon-Sat segment with perfect weekday data passes."""
    stamps = pd.date_range("2024-01-01", "2024-01-05", freq="B", tz="UTC")
    _write_bars(
        tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1d.parquet",
        [ts.isoformat() for ts in stamps],
    )
    assert has_sufficient_segment_coverage(
        symbol="EURUSD", timeframe="1d",
        start="2024-01-01", end="2024-01-06", data_dir=tmp_path,
    )


def test_gap_outside_required_window_does_not_fail(tmp_path: Path) -> None:
    """CodeRabbit round: ancient history outside [start, end] must not
    fail a requirement that never touches it."""
    _write_bars(
        tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
        [
            "2019-01-01T00:00:00Z",
            "2019-01-01T01:00:00Z",
            "2024-06-01T00:00:00Z",
            "2024-06-01T01:00:00Z",
            "2024-06-01T02:00:00Z",
        ],
    )
    report = analyze_ohlcv_coverage(
        tmp_path,
        DataRequirement(
            symbol="EURUSD", timeframe="1h", start="2024-06-01", end="2024-06-01"
        ),
    )
    assert report["status"] != "failed"


def test_gap_inside_required_window_still_fails(tmp_path: Path) -> None:
    """The window scope must not swallow holes that matter."""
    _write_bars(
        tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
        [
            "2024-01-01T00:00:00Z",
            "2024-01-01T01:00:00Z",
            "2024-06-01T00:00:00Z",
            "2024-06-01T01:00:00Z",
        ],
    )
    report = analyze_ohlcv_coverage(
        tmp_path,
        DataRequirement(
            symbol="EURUSD", timeframe="1h", start="2024-01-01", end="2024-06-30"
        ),
    )
    assert report["status"] == "failed"

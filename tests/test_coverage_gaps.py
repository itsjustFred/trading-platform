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


def test_a_hole_of_exactly_the_fail_threshold_warns_rather_than_fails(tmp_path):
    """The decision record says a hole *longer than* 3 calendar days fails, so a
    hole of exactly 3.00 days is still a warning. An off-by-one on that
    comparison would fail every hole that lands exactly on the budget."""
    _write_bars(
        tmp_path / "ohlcv" / "alpaca" / "equity" / "AAPL" / "1m.parquet",
        ["2026-09-08T20:00:00Z", "2026-09-11T20:00:00Z"],
    )

    report = analyze_ohlcv_coverage(
        tmp_path,
        DataRequirement(
            symbol="AAPL", timeframe="1m", start="2026-09-08", end="2026-09-11"
        ),
    )

    assert report["gaps"][0]["gap_days"] == 3.0
    assert report["status"] == "warning"
    assert report["failed_downloads"] == []


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

    def test_gap_outside_required_window_does_not_fail(self, tmp_path):
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
        result = validate_parquet(target, ohlcv_root=tmp_path / "ohlcv")
        assert result.gaps, "the 4-day hole must be reported"
        assert "2024-01-01" in result.gaps[0]["start"] and "01:00" in result.gaps[0]["start"]
        assert "2024-01-05" in result.gaps[0]["end"] and "00:00" in result.gaps[0]["end"]

    def test_rejects_path_outside_configured_root(self, tmp_path, monkeypatch):
        import data_tools.validate as validate_module

        configured_root = tmp_path / "configured"
        outside = tmp_path / "outside" / "1d.parquet"
        outside.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02T00:00:00Z"], utc=True),
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.0],
                "volume": [10.0],
            }
        ).to_parquet(outside, index=False)
        monkeypatch.setattr(validate_module, "DATA_DIR", configured_root)

        with pytest.raises(ValueError, match="escapes its root"):
            validate_module.validate_parquet(outside)


class TestEnsureBacktestDataGapGateViaModuleImport:
    """The same internal-gap scenario, driven through the module-level import.

    Merge resolution: both sides of the merge added this scenario in a class
    called ``TestEnsureBacktestDataGapGate``, so the later definition silently
    shadowed the earlier one and half the gap-gate coverage stopped running.
    The classes are renamed apart so both are collected. The only real
    difference between them is the import binding under test: this one resolves
    ``auto_download`` from module scope, so it also guards that binding.
    """

    def test_internal_gap_fails_coverage(self, tmp_path, monkeypatch):
        early = pd.date_range("2024-01-01", "2024-01-05", freq="1h", tz="UTC")
        late = pd.date_range("2024-01-15", "2024-01-20", freq="1h", tz="UTC")
        _write_bars(
            tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet",
            [ts.isoformat() for ts in [*early, *late]],
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

    def test_us_equity_holiday_does_not_reduce_expected_coverage(self, tmp_path):
        stamps = pd.to_datetime(
            ["2024-07-03T00:00:00Z", "2024-07-05T00:00:00Z", "2024-07-08T00:00:00Z"],
            utc=True,
        )
        _write_bars(
            tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1d.parquet",
            [stamp.isoformat() for stamp in stamps],
        )

        assert has_sufficient_segment_coverage(
            symbol="AAPL",
            timeframe="1d",
            start="2024-07-03",
            end="2024-07-08",
            data_dir=tmp_path,
        )

    def test_us_equity_holiday_does_not_hide_weekday_outage(self, tmp_path):
        stamps = pd.to_datetime(
            ["2024-07-03T00:00:00Z", "2024-07-08T00:00:00Z"],
            utc=True,
        )
        _write_bars(
            tmp_path / "ohlcv" / "test" / "equity" / "AAPL" / "1d.parquet",
            [stamp.isoformat() for stamp in stamps],
        )

        assert not has_sufficient_segment_coverage(
            symbol="AAPL",
            timeframe="1d",
            start="2024-07-03",
            end="2024-07-08",
            data_dir=tmp_path,
        )

    def test_non_us_equity_venue_does_not_use_nyse_calendar(self, tmp_path):
        stamps = [
            *pd.date_range("2024-07-03T00:00:00", periods=24, freq="1h", tz="UTC"),
            *pd.date_range("2024-07-05T00:00:00", periods=3, freq="1h", tz="UTC"),
        ]
        _write_bars(
            tmp_path / "ohlcv" / "international" / "equity" / "ASX" / "1h.parquet",
            [stamp.isoformat() for stamp in stamps],
        )

        assert not has_sufficient_segment_coverage(
            symbol="ASX",
            timeframe="1h",
            start="2024-07-03",
            end="2024-07-05T02:00:00Z",
            data_dir=tmp_path,
        )

    def test_crypto_symbol_does_not_use_us_equity_holiday_calendar(self, tmp_path):
        stamps = [
            *pd.date_range("2024-07-03T00:00:00", periods=24, freq="1h", tz="UTC"),
            *pd.date_range("2024-07-05T00:00:00", periods=3, freq="1h", tz="UTC"),
        ]
        _write_bars(
            tmp_path / "ohlcv" / "test" / "crypto" / "bch-usd" / "1h.parquet",
            [stamp.isoformat() for stamp in stamps],
        )

        assert not has_sufficient_segment_coverage(
            symbol="BCH-USD",
            timeframe="1h",
            start="2024-07-03",
            end="2024-07-05T02:00:00Z",
            data_dir=tmp_path,
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

    @pytest.mark.parametrize("date_text", ["2021-06-18", "2021-12-31"])
    def test_new_year_exception_and_pre_2022_juneteenth_stay_open(
        self, date_text: str
    ) -> None:
        assert not auto_download.is_us_equity_holiday(date_text)

    @pytest.mark.parametrize("date_text", ["2020-07-03", "2021-12-24"])
    def test_saturday_holidays_are_observed_on_preceding_friday(
        self, date_text: str
    ) -> None:
        assert auto_download.is_us_equity_holiday(date_text)

    def test_sunday_holiday_is_observed_monday(self) -> None:
        assert auto_download.is_us_equity_holiday("2022-06-20")

    def test_weekend_holiday_is_not_double_subtracted(self) -> None:
        start = pd.Timestamp("2024-07-05T22:00:00Z")
        end = pd.Timestamp("2024-07-07T01:00:00Z")
        baseline = auto_download._trading_minutes_between(start, end)
        minutes = auto_download._trading_minutes_between(
            start,
            end,
            frozenset({pd.Timestamp("2024-07-06").date()}),
        )

        assert minutes == pytest.approx(baseline)

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


def test_observed_session_does_not_merge_different_daily_windows() -> None:
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=4, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T14:00", periods=4, freq="1h", tz="UTC"),
    ]

    assert auto_download._observed_session_window(stamps, 60.0) is None


def test_observed_session_does_not_shrink_for_a_truncated_day() -> None:
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=5, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T09:00", periods=2, freq="1h", tz="UTC"),
    ]

    assert auto_download._observed_session_window(stamps, 60.0) is None


def test_observed_session_allows_a_final_day_cut_at_the_requested_end() -> None:
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=5, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T09:00", periods=2, freq="1h", tz="UTC"),
    ]

    assert auto_download._observed_session_window(
        stamps,
        60.0,
        start_ts=pd.Timestamp("2024-01-02T00:00:00Z"),
        end_ts=pd.Timestamp("2024-01-03T10:00:00Z"),
    ) == (540, 840)
    assert auto_download._observed_session_window(
        stamps,
        60.0,
        start_ts=pd.Timestamp("2024-01-02T00:00:00Z"),
        end_ts=pd.Timestamp("2024-01-03T13:00:00Z"),
    ) is None


def test_observed_session_uses_an_interior_window_at_boundary_cuts() -> None:
    stamps = [
        *pd.date_range("2024-01-02T10:00", periods=5, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T09:00", periods=6, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-04T09:00", periods=4, freq="1h", tz="UTC"),
    ]

    assert auto_download._observed_session_window(
        stamps,
        60.0,
        start_ts=pd.Timestamp("2024-01-02T10:30:00Z"),
        end_ts=pd.Timestamp("2024-01-04T13:00:00Z"),
    ) == (540, 900)
# ─── the allowed hole length must scale with the timeframe ──────────────────


def _write_series(directory, filename, timestamps):
    import pandas as pd

    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
    ).to_parquet(directory / filename)
    return directory


def test_regular_weekly_data_is_not_reported_as_a_gap(tmp_path):
    """Weekly bars sit 7 days apart, so a 1-day threshold flagged every bar.

    The reported hole made weekly coverage always fail, blocking any backtest
    that used a weekly timeframe.
    """
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    symbol_dir = _write_series(
        Path(tmp_path) / "GOLD1",
        "1w.parquet",
        pd.date_range("2024-01-05", periods=60, freq="7D", tz="UTC"),
    )
    assert find_internal_gaps(symbol_dir, "1w") == []


def test_regular_monthly_data_is_not_reported_as_a_gap(tmp_path):
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    symbol_dir = _write_series(
        Path(tmp_path) / "GOLD1",
        "1mo.parquet",
        pd.date_range("2024-01-01", periods=36, freq="30D", tz="UTC"),
    )
    assert find_internal_gaps(symbol_dir, "1mo") == []


def test_regular_daily_data_still_tolerates_a_weekend(tmp_path):
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    # Business-day cadence leaves 2-day weekend holes.
    symbol_dir = _write_series(
        Path(tmp_path) / "GOLD1",
        "1d.parquet",
        pd.bdate_range("2024-01-01", periods=60, tz="UTC"),
    )
    assert find_internal_gaps(symbol_dir, "1d") == []


def test_a_real_hole_in_daily_data_is_still_reported(tmp_path):
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    ts = list(pd.date_range("2024-01-01", periods=20, freq="1D", tz="UTC"))
    ts += list(pd.date_range("2024-03-01", periods=20, freq="1D", tz="UTC"))
    symbol_dir = _write_series(Path(tmp_path) / "GOLD1", "1d.parquet", ts)
    gaps = find_internal_gaps(symbol_dir, "1d")
    assert gaps and gaps[0]["gap_days"] > 30, f"a six-week hole must still fail: {gaps}"


def test_gap_threshold_scales_with_timeframe_but_keeps_an_intraday_floor():
    from data_tools.auto_download import scaled_gap_threshold_days

    # The warning threshold keeps a tight 1-day floor for sub-daily timeframes,
    # so a genuine intraday hole is still surfaced rather than accepted.
    assert scaled_gap_threshold_days("1m", 1) == 1.0
    assert scaled_gap_threshold_days("1h", 1) == 1.0
    # REWRITTEN. These two lines used to assert that the failing threshold is
    # 4.0 days, pinning the premise that a long weekend *is* four calendar days
    # and that the 4.0-day floor belongs in the length budget. Neither is true.
    # On the US equity grid an ordinary long weekend runs from Friday's close to
    # Monday's open, which is 2.73 days -- already inside the 3-day base. And the
    # 4.0-day floor was a *minimum* over that base, so it overrode it for every
    # timeframe and let 1-minute data with a 3.6-day hole through the gate. The
    # long-weekend allowance is now applied per hole against the exchange
    # calendar (see test_long_weekend_closure_does_not_fail_coverage and
    # test_calendar_distinguishes_a_weekend_from_a_downloader_outage), so the
    # length budget is the base again.
    assert scaled_gap_threshold_days("1h", 3, allow_long_weekend=True) == 3.0
    assert scaled_gap_threshold_days("1d", 3, allow_long_weekend=True) == 3.0
    # The long-weekend flag no longer changes the length budget at all, for any
    # timeframe: the two shapes must be impossible to confuse.
    for timeframe in ("1m", "5m", "1h", "4h", "1d", "1w", "1mo", "3mo"):
        assert scaled_gap_threshold_days(
            timeframe, 3, allow_long_weekend=True
        ) == scaled_gap_threshold_days(timeframe, 3), timeframe
    # Longer-spaced series widen proportionally, from the timeframe's own nominal
    # spacing, and are not loosened by the closure allowance on top.
    assert scaled_gap_threshold_days("1d", 1) == 3.0
    assert scaled_gap_threshold_days("1w", 1) == 21.0
    assert scaled_gap_threshold_days("1mo", 1) == 90.0
    assert scaled_gap_threshold_days("1w", 3, allow_long_weekend=True) == 21.0
    assert scaled_gap_threshold_days("1mo", 3, allow_long_weekend=True) == 90.0


def test_weekly_coverage_analysis_does_not_fail_on_regular_data(tmp_path):
    """`analyze_ohlcv_coverage` is the gate that actually blocks a backtest.

    With a fixed 3-day fail threshold, regular weekly data (7-day spacing) was
    reported as `failed` for internal gaps, so no weekly backtest could start.
    """
    import pandas as pd

    from data_tools.auto_download import DataRequirement, analyze_ohlcv_coverage

    root = tmp_path / "data"
    symbol_dir = root / "ohlcv" / "GOLD1"
    ts = pd.date_range("2024-01-05", periods=60, freq="7D", tz="UTC")
    _write_series(symbol_dir, "1w.parquet", ts)
    report = analyze_ohlcv_coverage(
        root,
        DataRequirement(
            symbol="GOLD1",
            timeframe="1w",
            # Inside the stored range: an out-of-range request fails for an
            # unrelated boundary reason and would not exercise the gap check.
            start="2024-01-05",
            end="2025-02-20",
        ),
    )
    assert report["status"] != "failed", f"regular weekly data must not fail: {report}"
    assert not report["gaps"], f"regular weekly data must report no gaps: {report}"


def test_nominal_days_scales_by_the_numeric_prefix():
    """`4w` bars are four weeks apart and `5m` bars are five minutes apart.

    Ignoring the prefix sized the allowed hole against the wrong spacing, so a
    4-week series would be judged against a 3-day threshold.
    """
    from data_tools.auto_download import timeframe_nominal_days

    assert timeframe_nominal_days("4w") == 28.0
    assert timeframe_nominal_days("1w") == 7.0
    assert timeframe_nominal_days("3mo") == 90.0
    assert timeframe_nominal_days("1mo") == 30.0
    assert timeframe_nominal_days("5m") == pytest.approx(5 / 1440)
    assert timeframe_nominal_days("4h") == pytest.approx(4 / 24)
    assert timeframe_nominal_days("2d") == 2.0
    # Unparseable input falls back to daily rather than raising.
    assert timeframe_nominal_days("nonsense") == 1.0
    assert timeframe_nominal_days("") == 1.0


def test_multi_week_timeframes_get_a_wider_gap_threshold():
    from data_tools.auto_download import scaled_gap_threshold_days

    assert scaled_gap_threshold_days("4w", 1) == 84.0
    assert scaled_gap_threshold_days("2w", 1) == 42.0


def test_gap_analysis_without_a_timeframe_keeps_the_fixed_floor(tmp_path):
    """With no timeframe there is no spacing to scale against.

    Widening anyway would only hide real holes, so the floor is kept.
    """
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    # 2 days apart with no timeframe given: must still be reported. Scaling
    # would raise the bar to 3 days and hide it.
    symbol_dir = _write_series(
        Path(tmp_path) / "GOLD1",
        "custom.parquet",
        pd.date_range("2024-01-01", periods=12, freq="2D", tz="UTC"),
    )
    gaps = find_internal_gaps(symbol_dir, None)
    assert gaps and gaps[0]["gap_days"] == pytest.approx(2.0), (
        f"a 2-day hole must be reported when no timeframe is known: {gaps}"
    )


def test_long_weekend_closure_does_not_fail_coverage(tmp_path):
    """Friday close to Monday open is about three days even for hourly bars.

    With a fixed 3-day fail threshold that escalated to `failed` and blocked the
    backtest. It must stay a warning: the closure is real market behaviour, and a
    warning is still surfaced rather than silently accepted.
    """
    import pandas as pd

    from data_tools.auto_download import DataRequirement, analyze_ohlcv_coverage

    root = tmp_path / "data"
    stamps = list(
        pd.date_range("2024-03-06 00:00", "2024-03-08 21:00", freq="1h", tz="UTC")
    ) + [pd.Timestamp("2024-03-11 22:00", tz="UTC")]
    # AAPL, not the metal this used to use. The shape asserted here - closed
    # Friday 21:00 UTC, no bars until Monday - is a *US equity* weekend, and the
    # calendar that excuses it models US equities. On GOLD1 the same fixture is
    # not market behaviour at all: a 24/5 Dukascopy venue trades Sunday 22:00
    # through Monday, so those bars should exist. Asserting a US closure on a
    # non-US venue is the same confusion that let a four-day FX outage be excused
    # as a holiday, so the symbol is corrected rather than the assertion.
    _write_series(root / "ohlcv" / "AAPL", "1h.parquet", stamps)
    report = analyze_ohlcv_coverage(
        root,
        DataRequirement(
            symbol="AAPL", timeframe="1h",
            start="2024-03-06", end="2024-03-11",
        ),
    )
    assert report["status"] != "failed", f"a long weekend must not fail coverage: {report}"
    assert report["gaps"], "the closure should still be reported as a gap"


def test_a_real_multi_day_hole_in_intraday_data_is_still_reported(tmp_path):
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    stamps = list(pd.date_range("2024-03-01", periods=5, freq="1h", tz="UTC")) + list(
        pd.date_range("2024-03-09", periods=5, freq="1h", tz="UTC")
    )
    symbol_dir = _write_series(Path(tmp_path) / "GOLD1", "1h.parquet", stamps)
    gaps = find_internal_gaps(symbol_dir, "1h")
    assert gaps and gaps[0]["gap_days"] > 4, f"an 8-day hole must be reported: {gaps}"

# ── The long-weekend allowance must be a calendar term, not a flat floor ──────
#
# `LONG_WEEKEND_GAP_DAYS = 4.0` was applied as a *minimum* over the failing
# threshold, so it replaced the 3.0-day base for every timeframe instead of
# adding to it. The length budget went to 4.00 days for 1m, 4h and 1d alike, so
# 1-minute data with a 3.6-day hole -- a downloader that silently failed for
# most of a week -- passed the gate, the backtest ran over forward-filled prices
# and reported a Sharpe. It bought nothing real either: an ordinary long weekend
# is 65.5 hours on the US equity grid, already inside the 3.0-day base, and the
# 4.0-day floor sat in the gap between the two closure shapes that do occur.
#
# No single length budget can fix this, and that is the point of the tests
# below. A long weekend and a downloader outage are both about three days long,
# so the allowance has to be decided per hole against the calendar.


def _around(before: str, after: str, *, minutes: int = 20) -> list:
    """1-minute bars clustered either side of a hole, so only the hole is wide.

    The last bar before the hole lands exactly on ``before`` and the first after
    it exactly on ``after``, so the measured hole is precisely ``after - before``.
    """
    lead = pd.Timestamp(before) - pd.Timedelta(minutes=minutes)
    trail = pd.Timestamp(after) + pd.Timedelta(minutes=minutes)
    return list(pd.date_range(lead, before, freq="1min", tz="UTC")) + list(
        pd.date_range(after, trail, freq="1min", tz="UTC")
    )


def _one_minute_report(tmp_path, before: str, after: str, start: str, end: str):
    root = tmp_path / "data"
    _write_series(root / "ohlcv" / "AAPL", "1m.parquet", _around(before, after))
    return analyze_ohlcv_coverage(
        root,
        DataRequirement(symbol="AAPL", timeframe="1m", start=start, end=end),
    )


def test_a_3_6_day_hole_in_1m_data_still_fails_coverage(tmp_path):
    """The defect, as a length. 3.6 days is just under the old 4.0-day floor and
    spans two ordinary sessions, so nothing about it is market behaviour."""
    report = _one_minute_report(
        tmp_path,
        before="2026-09-17 20:00",   # Thursday 17 September's close
        after="2026-09-21 11:36",    # Monday mid-morning: 87.6 hours later
        start="2026-09-17",
        end="2026-09-21",
    )

    assert report["status"] == "failed", (
        f"a 3.6-day hole in 1m data is a dead downloader, not a closure: {report}"
    )
    assert report["failed_downloads"], f"the hole must be named: {report}"
    assert report["failed_downloads"][0]["gap_days"] == pytest.approx(3.65, abs=0.01)


def test_a_closure_shaped_hole_wider_than_the_closure_is_not_excused(tmp_path):
    """The dates look exactly like Good Friday 2026 -- Thursday to Monday, with
    no session in between -- but the hole runs from Thursday midnight to Monday
    midnight, five days against a 64.5-hour closure. The extended-hours tail
    exists for a venue that resumes a few hours late, not for 56 extra hours of
    missing session time."""
    report = _one_minute_report(
        tmp_path,
        before="2026-04-02 00:00",   # Thursday, before Good Friday
        after="2026-04-06 23:59",    # Monday: five days later
        start="2026-04-02",
        end="2026-04-06",
    )

    assert report["gaps"] and report["gaps"][0]["gap_days"] == pytest.approx(5.0, abs=0.01)
    assert report["status"] == "failed", report


# Table-driven on real 2026 US closure dates, in 1-minute data, which is the
# case the flat floor hurt most. Every `after` is 10:00 Eastern on the reopening
# session, i.e. a live series that resumed trading.
LONG_WEEKEND_CASES = [
    pytest.param(
        "ordinary long weekend: Fri 11 Sep close, Mon 14 Sep open",
        "2026-09-11 20:00", "2026-09-14 13:30", "2026-09-11", "2026-09-14",
        id="ordinary-long-weekend",
    ),
    pytest.param(
        "Independence Day 2026, observed Friday 3 July: 89.5 hours",
        "2026-07-02 20:00", "2026-07-06 13:30", "2026-07-02", "2026-07-06",
        id="independence-day-2026",
    ),
    pytest.param(
        "Labor Day 2026 on Monday 7 September: 89.5 hours",
        "2026-09-04 20:00", "2026-09-08 13:30", "2026-09-04", "2026-09-08",
        id="labor-day-2026",
    ),
    pytest.param(
        "Martin Luther King Jr Day 2026 on Monday 19 January",
        "2026-01-16 21:00", "2026-01-20 14:30", "2026-01-16", "2026-01-20",
        id="mlk-day-2026",
    ),
    pytest.param(
        "Memorial Day 2026 on Monday 25 May",
        "2026-05-22 20:00", "2026-05-26 13:30", "2026-05-22", "2026-05-26",
        id="memorial-day-2026",
    ),
    pytest.param(
        "Juneteenth 2026 on Friday 19 June",
        "2026-06-18 20:00", "2026-06-22 13:30", "2026-06-18", "2026-06-22",
        id="juneteenth-2026",
    ),
    pytest.param(
        "Good Friday 2026 on Friday 3 April (Maundy Thursday trades)",
        "2026-04-02 20:00", "2026-04-06 13:30", "2026-04-02", "2026-04-06",
        id="good-friday-2026",
    ),
    pytest.param(
        "Christmas 2026 on a Friday",
        "2026-12-24 21:00", "2026-12-28 14:30", "2026-12-24", "2026-12-28",
        id="christmas-2026",
    ),
    pytest.param(
        "New Year 2027 on a Friday, closing out the 2026 regular week",
        "2026-12-31 21:00", "2027-01-04 14:30", "2026-12-31", "2027-01-04",
        id="new-year-2027",
    ),
]


@pytest.mark.parametrize(
    ("label", "before", "after", "start", "end"), LONG_WEEKEND_CASES
)
def test_a_genuine_long_weekend_in_1m_data_still_passes(
    tmp_path, label, before, after, start, end
):
    report = _one_minute_report(tmp_path, before, after, start, end)

    assert report["status"] != "failed", f"{label} must not fail coverage: {report}"
    assert report["gaps"], f"{label} should still be reported, not silently hidden"


def test_the_closure_allowance_is_calendar_derived_not_declared():
    """It must move with the calendar, and per hole rather than as one number.

    A plain weekend and a Monday-holiday week are both excusable by shape, so a
    single longest-closure allowance would let the weekend be a day wider than
    the calendar can account for.
    """
    import datetime as dt

    from data_tools.auto_download import closure_hole_allowance_seconds
    from shared import market_calendar

    weekend = closure_hole_allowance_seconds(
        "1m", start=dt.date(2026, 9, 11), end=dt.date(2026, 9, 14)
    )
    holiday_week = closure_hole_allowance_seconds(
        "1m", start=dt.date(2026, 9, 4), end=dt.date(2026, 9, 8)
    )

    # Friday close to Monday open is 65.5 hours, to Tuesday open 89.5.
    assert holiday_week - weekend == pytest.approx(24.0 * 3600.0)
    assert weekend == pytest.approx(
        (market_calendar.session_open_utc(dt.date(2026, 9, 14))
         - market_calendar.session_close_utc(dt.date(2026, 9, 11))).total_seconds()
        + 2.0 * market_calendar.session_span_seconds(dt.date(2026, 9, 11))
        + 60.0
    )


def test_weekly_and_monthly_thresholds_are_unchanged_by_the_allowance():
    """`GAP_SPACING_MULTIPLE` still sizes those from the bar spacing, and the
    closure allowance no longer touches them."""
    from data_tools.auto_download import scaled_gap_threshold_days

    assert scaled_gap_threshold_days("1w", 1) == 21.0
    assert scaled_gap_threshold_days("1mo", 1) == 90.0
    assert scaled_gap_threshold_days("1w", 3, allow_long_weekend=True) == 21.0
    assert scaled_gap_threshold_days("1mo", 3, allow_long_weekend=True) == 90.0


def test_a_gap_with_unreadable_endpoints_is_never_excused():
    """Fails closed: a gap report this function cannot read is a bug, and
    excusing it would hide that bug behind a closure."""
    from data_tools.auto_download import _gap_is_market_closure

    weekend = {"start": "2026-09-11", "end": "2026-09-14", "gap_seconds": 235_800}
    # The positive control names a US venue, because that is the only kind this
    # calendar can account for. Called with no symbol it now returns False - an
    # unstated venue is not a licence to excuse, and that is the defect.
    assert _gap_is_market_closure(weekend, timeframe="1m", symbol="AAPL")

    # Every negative case names the US venue too. Without it these assertions
    # still passed - but for the wrong reason: an unnamed venue is refused before
    # the endpoints are ever read, so the test was measuring the venue guard three
    # times over and the fail-closed parsing it exists to pin was not exercised at
    # all. Found by a CodeRabbit review scoped to tests/, one commit after the
    # venue guard landed.
    assert not _gap_is_market_closure(
        {**weekend, "start": ""}, timeframe="1m", symbol="AAPL"
    )
    assert not _gap_is_market_closure(
        {**weekend, "end": "not-a-date"}, timeframe="1m", symbol="AAPL"
    )
    # A report with no exact width is not guessed at from the rounded
    # `gap_days`, which cannot resolve a one-minute bar.
    assert not _gap_is_market_closure(
        {"start": "2026-09-11", "end": "2026-09-14", "gap_days": 2.73},
        timeframe="1m",
        symbol="AAPL",
    )


def test_a_four_day_fx_outage_is_not_excused_by_the_us_equity_calendar():
    """The defect: a real hole in FX, certified as a holiday.

    Martin Luther King Jr Day 2026 closed US equities on Monday 19 January, and
    the calendar's shape test asks whether any *US session* falls between the
    hole's endpoints. Between a Friday close and a Tuesday resume there is none -
    Saturday, Sunday, and the holiday itself - so a Dukascopy series that simply
    died for those four days matched the shape exactly and was excused, even
    though `_uses_us_equity_calendar` already knew EURUSD was not a US venue.

    A silent four-day hole in FX is worse than a spurious failure: the backtest
    runs over a series with forward-filled prices and reports a result.
    `shared/market_calendar` says it outright - "Non-US venues are not modelled ...
    nothing here should be used to judge those" - and the check ignored it.
    """
    from data_tools.auto_download import _gap_is_market_closure, _uses_us_equity_calendar

    fx_outage = {
        "start": "2026-01-16",   # Friday 21:00 UTC, last bar
        "end": "2026-01-20",     # Tuesday 22:00 UTC, first bar back
        "gap_seconds": 349_200.0,
        "gap_days": 4.04,
    }

    for symbol in ("EURUSD", "USDJPY", "XAUUSD", "BTCUSD"):
        assert not _uses_us_equity_calendar(symbol), (
            f"{symbol} is not a US equity venue, so the calendar cannot account "
            f"for its closures"
        )
        assert not _gap_is_market_closure(
            fx_outage, timeframe="1m", symbol=symbol
        ), (
            f"a four-day hole in {symbol} was excused as a market closure. The US "
            "equity calendar does not model this venue, so it cannot certify its "
            "outage as a holiday - the data is missing, not closed."
        )

    # The identical hole on a US equity is a genuine long weekend, and still
    # passes. The fix narrows the excuse; it does not remove it.
    for symbol in ("AAPL", "SPY"):
        assert _gap_is_market_closure(fx_outage, timeframe="1m", symbol=symbol), (
            f"a real US equity holiday week stopped being excused for {symbol}"
        )


def test_an_unknown_venue_is_never_excused():
    """No symbol is not a licence.

    The positive control in the fail-closed test above had to start naming a US
    venue, because with no symbol the function now returns False. That is
    deliberate: `closure_explains_hole` cannot know what it is being asked about,
    and guessing "probably equities" is how a four-day hole becomes a warning.
    """
    from data_tools.auto_download import _gap_is_market_closure

    weekend = {"start": "2026-09-11", "end": "2026-09-14", "gap_seconds": 235_800}

    assert not _gap_is_market_closure(weekend, timeframe="1m")
    assert not _gap_is_market_closure(weekend, timeframe="1m", symbol="")
    assert not _gap_is_market_closure(weekend, timeframe="1m", symbol="   ")


def test_a_four_day_fx_hole_fails_coverage_end_to_end(tmp_path):
    """The whole path, not just the helper: a status the backtest actually sees."""
    import pandas as pd

    from data_tools.auto_download import DataRequirement, analyze_ohlcv_coverage

    root = tmp_path / "data"
    # Friday 21:00 UTC, then nothing until Tuesday 22:00 - a 24/5 FX venue that
    # was open the whole time in between.
    stamps = list(
        pd.date_range("2026-01-16 15:00", "2026-01-16 21:00", freq="1h", tz="UTC")
    ) + [pd.Timestamp("2026-01-20 22:00", tz="UTC")]
    _write_series(root / "ohlcv" / "EURUSD", "1h.parquet", stamps)

    report = analyze_ohlcv_coverage(
        root,
        DataRequirement(
            symbol="EURUSD", timeframe="1h", start="2026-01-16", end="2026-01-20"
        ),
    )

    assert report["status"] == "failed", (
        f"a four-day hole in FX must fail coverage, not warn: {report}"
    )
    assert report["gaps"], "the hole must still be reported, not merely fail silently"


def test_unit_first_sub_daily_labels_are_not_sized_as_daily_bars():
    """`s1` and `tick` are one second, and the fallback said one day.

    The numeric-prefix pattern this function uses (`5m`, `4w`) cannot match a
    label that puts the unit first, so `s1` and `tick` fell through to the `1.0`
    daily fallback. That silently loosened their gap threshold from 1.0 to 3.0
    days, and a two-day hole in tick data was reported as `ok` instead of as a
    warning - a coverage report nobody should believe.

    These are the repo's own labels, not hypotheticals: `KNOWN_TIMEFRAMES` in
    `consolidate_parquets.py` and the viewer's timeframe list both spell them this
    way, and the grid downloader names them in `UNSUPPORTED_TIMEFRAMES`.

    The unparseable fallback is asserted too, because the fix must not turn a
    genuinely unknown label into a permissive one - `nonsense` is still a day, and
    that is the existing deliberate behaviour.
    """
    from data_tools.auto_download import (
        scaled_gap_threshold_days,
        timeframe_nominal_days,
    )

    one_second = 1.0 / 86_400.0
    for label in ("s1", "tick", "S1", "TICK", " s1 "):
        assert timeframe_nominal_days(label) == pytest.approx(one_second), (
            f"{label!r} sized as {timeframe_nominal_days(label)} days; it is one second"
        )
        assert scaled_gap_threshold_days(label, 1) == 1.0, (
            f"{label!r} got a {scaled_gap_threshold_days(label, 1)}-day threshold; "
            f"a sub-daily series must not be allowed a multi-day hole"
        )

    # `1s` and `s1` are the same spacing expressed two ways, so they must agree.
    assert timeframe_nominal_days("s1") == pytest.approx(timeframe_nominal_days("1s"))

    # And the pre-existing fallback is untouched.
    assert timeframe_nominal_days("nonsense") == 1.0
    assert timeframe_nominal_days(None) == 1.0
    assert timeframe_nominal_days("") == 1.0


def _two_holes_one_excusable(tmp_path):
    """A real 3.5-day outage alongside a longer, excusable US holiday week.

    Three bar ranges, so two distinct holes: Friday 16 January's close to Tuesday
    20 January is the MLK week and the calendar excuses it, while 10 to 14 January
    is 3.5 days of missing bars - above the 3.0-day fail threshold, so it has to
    fail coverage. The holiday is the *larger* of the two, which is the whole point.
    """
    import pandas as pd

    from data_tools.auto_download import DataRequirement, analyze_ohlcv_coverage

    root = tmp_path / "data"
    stamps = (
        list(pd.date_range("2026-01-10 00:00", "2026-01-10 23:00", freq="1h", tz="UTC"))
        + list(
            pd.date_range("2026-01-14 11:00", "2026-01-16 12:00", freq="1h", tz="UTC")
        )
        + list(pd.date_range("2026-01-20 12:00", "2026-01-20 23:00", freq="1h", tz="UTC"))
    )
    _write_series(root / "ohlcv" / "AAPL", "1h.parquet", stamps)
    report = analyze_ohlcv_coverage(
        root,
        DataRequirement(
            symbol="AAPL", timeframe="1h", start="2026-01-10", end="2026-01-20"
        ),
    )
    return report


def test_a_smaller_hole_next_to_an_excusable_one_is_still_caught(tmp_path):
    """Collapsing to the largest hole hid the hole that mattered.

    `find_internal_gaps` returned only the longest hole per symbol directory, and
    the coverage verdict is computed per gap - so a second, smaller hole was never
    examined. The bias points straight at the failure: **the largest hole is
    systematically the most likely to be an excusable closure**, because holiday
    weeks are the longest gaps there are. Picking the largest therefore surfaced
    the one hole the calendar would excuse and discarded the one it would not.

    Measured before the fix, on these bar boundaries: one gap reported (the 4.00-day
    holiday), status `warning`, `failed_downloads` empty - while a 3.50-day hole sat
    above the 3.0-day fail threshold the whole time.
    """
    report = _two_holes_one_excusable(tmp_path)

    assert report["status"] == "failed", (
        f"a 3.5-day hole next to an excusable holiday must fail coverage: {report}"
    )
    failed = report.get("failed_downloads") or []
    assert any(abs(float(gap["gap_days"]) - 3.5) < 0.01 for gap in failed), (
        f"the real outage is not among the failed downloads: {failed}"
    )
    # And the genuine holiday is still excused - the fix must not turn every
    # multi-day gap into a failure.
    assert not any(
        abs(float(gap["gap_days"]) - 4.0) < 0.01 for gap in failed
    ), f"the MLK holiday week was failed as a data hole: {failed}"


def test_every_hole_above_the_threshold_is_returned_largest_first(tmp_path):
    """The report is the whole set, ordered so the first is still the worst.

    Largest first preserves what the `max()` guaranteed for any caller that only
    looks at the first element, while no longer discarding the rest.
    """
    import pandas as pd

    from data_tools.auto_download import find_internal_gaps

    root = tmp_path / "data"
    stamps = (
        list(pd.date_range("2026-01-10 00:00", "2026-01-10 23:00", freq="1h", tz="UTC"))
        + list(
            pd.date_range("2026-01-14 11:00", "2026-01-16 12:00", freq="1h", tz="UTC")
        )
        + list(pd.date_range("2026-01-20 12:00", "2026-01-20 23:00", freq="1h", tz="UTC"))
    )
    _write_series(root / "ohlcv" / "AAPL", "1h.parquet", stamps)

    gaps = find_internal_gaps(root / "ohlcv" / "AAPL", "1h", threshold_days=1.0)

    assert len(gaps) == 2, f"expected both holes, got {gaps}"
    assert [round(float(gap["gap_days"]), 2) for gap in gaps] == [4.0, 3.5], (
        f"gaps must come back largest first: {[(g['start'], g['gap_days']) for g in gaps]}"
    )


def _segmented_gap_file(tmp_path, name: str):
    """One 1-minute file with a 30-minute hole, written under `name`."""
    import numpy as np
    import pandas as pd

    from data_tools.validate import validate_parquet

    idx = pd.date_range("2024-03-01 14:30", periods=240, freq="1min", tz="UTC")
    idx = idx.delete(slice(120, 150))
    base = np.arange(len(idx), dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": idx,
            "open": base + 100,
            "high": base + 101,
            "low": base + 99,
            "close": base + 100.5,
            "volume": 1.0,
        }
    )
    root = tmp_path / "ohlcv"
    directory = root / "nasdaq" / "NQ" / name
    directory.mkdir(parents=True)
    path = directory / f"{name}.parquet"
    frame.to_parquet(path, index=False)
    return validate_parquet(path, ohlcv_root=root)


def test_a_segmented_dataset_is_validated_at_its_own_timeframe(tmp_path):
    """A segmented file must not be validated at the 3-hour default.

    `validate_parquet` took `candidate.stem` as the timeframe, so every segmented
    dataset became a timeframe called `1m_2024_03`. That misses the gap-threshold
    table and falls back to 180 minutes - a **36x** loosening against the 5 minutes
    a 1-minute file actually gets. Measured on identical data, before the fix:

        1m.parquet          -> status=warning  gaps=1
        1m_2024_03.parquet  -> status=ok      gaps=0

    A 30-minute hole in a segmented 1m file was not merely under-reported; it was
    invisible, and `status=ok` is a claim that the data is clean. Segmented storage
    is a layout this repo deliberately supports - `catalog_builder` parses the
    segment out, has whole machinery for `tick_2024_01` monthly layouts, and merges
    the entries - so its datasets were being validated by a reader that did not know
    they existed.

    It reached further than the threshold too: this same value is passed to
    `_gap_classification`, so gap classification was deciding with a name no
    calendar could match.

    Both files now report the same timeframe and the same gap, and the segmented
    name is still recognised as segmented by the shared parser.
    """
    plain = _segmented_gap_file(tmp_path, "1m")
    segmented = _segmented_gap_file(tmp_path, "1m_2024_03")

    assert segmented.timeframe == plain.timeframe == "1m", (
        f"segmented reported {segmented.timeframe!r} and plain {plain.timeframe!r}; "
        f"one parser should serve both readers"
    )
    assert len(segmented.gaps) == len(plain.gaps) == 1, (
        f"the same data yielded {len(plain.gaps)} gap(s) plain and "
        f"{len(segmented.gaps)} segmented"
    )
    assert segmented.status == plain.status, (
        f"the same data was {plain.status} plain and {segmented.status} segmented"
    )
    # "warning", not "ok": the hole is real and must be surfaced, not tolerated.
    assert segmented.status != "ok", (
        "a 30-minute hole in a 1-minute series reported 'ok', which is a claim the "
        "data is clean"
    )


def test_a_tick_segmented_dataset_is_still_recognised(tmp_path):
    """The other segmented shape the catalog builder supports explicitly.

    `tick_2024_01` is a monthly tick layout with its own handling in the shared
    parser, so it is the case most likely to regress if the parser is ever changed
    to reject rather than strip a segment.
    """
    import numpy as np
    import pandas as pd

    from data_tools.catalog_builder import _extract_timeframe_and_segment
    from data_tools.validate import validate_parquet

    timeframe, segment = _extract_timeframe_and_segment("tick_2024_01")
    assert (timeframe, segment) == ("tick", "2024_01")

    idx = pd.date_range("2024-01-02 09:00", periods=200, freq="min", tz="UTC")
    base = np.arange(len(idx), dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": idx,
            "open": base + 100,
            "high": base + 101,
            "low": base + 99,
            "close": base + 100.5,
            "volume": 1.0,
        }
    )
    root = tmp_path / "ohlcv"
    directory = root / "nasdaq" / "NQ" / "tick_2024_01"
    directory.mkdir(parents=True)
    path = directory / "tick_2024_01.parquet"
    frame.to_parquet(path, index=False)

    result = validate_parquet(path, ohlcv_root=root)

    assert result.timeframe == "tick", (
        f"a monthly tick layout was read as {result.timeframe!r}"
    )


def _many_expected_gaps_plus_one_real_hole(tmp_path):
    """25 weekend-sized gaps first, then one genuine hole late in the file.

    Mirrors the shape that defeats a "look at the first N gaps" rule: in a real
    multi-month intraday file the earliest large gaps are all weekends, holidays and
    overnight closes, and they classify as *expected*.
    """
    import numpy as np
    import pandas as pd

    from data_tools.validate import validate_parquet

    days = pd.date_range("2024-01-01", "2024-02-05", freq="D", tz="UTC")
    stamps: list[pd.Timestamp] = []
    for day in days:
        if day.weekday() >= 5:
            continue
        segment = pd.date_range(
            day + pd.Timedelta(hours=14), periods=30, freq="1min", tz="UTC"
        )
        if day.day > 20:
            segment = segment.delete(slice(10, 18))  # 8 minutes, mid-session
        stamps.extend(segment)
    index = pd.DatetimeIndex(sorted(set(stamps)))
    base = np.arange(len(index), dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": index,
            "open": base + 100,
            "high": base + 101,
            "low": base + 99,
            "close": base + 100.5,
            "volume": 1.0,
        }
    )
    root = tmp_path / "ohlcv"
    directory = root / "nasdaq" / "NQ" / "1m"
    directory.mkdir(parents=True)
    path = directory / "1m.parquet"
    frame.to_parquet(path, index=False)
    return validate_parquet(path, ohlcv_root=root)


def test_a_gap_after_the_twentieth_is_still_examined(tmp_path):
    """A verdict computed from the first 20 of 130 gaps is a claim about the first 20.

    The loop was `for pos in range(min(20, len(large_gaps)))`. In a multi-month
    intraday file the earliest large gaps are weekends, holidays and overnight
    closes, and they classify as expected - so they consumed the whole budget and
    nothing after them was ever looked at. A real 45-minute hole in the middle of a
    session reported `status=ok` with no warnings at all.

    The cap is kept for the *report*, which is a fair thing to want, and lifted for
    the *examination*: a file with 130 gaps and one real hole must still say it has a
    real hole. And when the list is truncated it says so, because a truncated list
    that does not mention the truncation is how the cap became invisible.
    """
    from data_tools.validate import MAX_REPORTED_GAPS

    result = _many_expected_gaps_plus_one_real_hole(tmp_path)

    detected = [w for w in result.warnings if "data gaps detected" in w]
    assert detected, f"no gap was reported at all: {result.warnings}"
    # The property, not a count I guessed: more unexpected gaps exist than the
    # report lists, so the examination ran past the cap. (My first attempt asserted
    # an exact number from a fixture whose arithmetic I had not checked - 28 gaps,
    # not 21 - so the assertion was wrong in a way that had nothing to do with the
    # code under test.)
    reported_total = int(detected[0].split()[0])
    assert reported_total > MAX_REPORTED_GAPS, (
        f"only {reported_total} gap(s) were examined, so the cap still bounds the "
        f"examination rather than only the report"
    )
    assert len(result.gaps) <= MAX_REPORTED_GAPS, (
        f"the report grew past its own cap: {len(result.gaps)}"
    )
    assert any("not listed" in w for w in result.warnings), (
        f"the report was truncated without saying so: {result.warnings}. A capped "
        f"list that does not mention the cap reads as a complete one."
    )
    assert result.status == "warning", (
        f"a file with a genuine mid-session hole reported {result.status}"
    )


def test_a_file_with_only_expected_gaps_reports_none(tmp_path):
    """The other direction: lifting the cap must not manufacture findings."""
    import numpy as np
    import pandas as pd

    from data_tools.validate import validate_parquet

    # One continuous session, no hole anywhere. The first version of this fixture
    # used a 30-minute window per weekday, which makes the weekday-to-weekday
    # overnight a *genuine* 23.5-hour gap rather than an expected close - so the
    # test failed for a correct reason and was testing the wrong thing.
    index = pd.date_range("2024-01-02 14:30", periods=600, freq="1min", tz="UTC")
    base = np.arange(len(index), dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": index,
            "open": base + 100,
            "high": base + 101,
            "low": base + 99,
            "close": base + 100.5,
            "volume": 1.0,
        }
    )
    root = tmp_path / "ohlcv"
    directory = root / "nasdaq" / "NQ" / "1m"
    directory.mkdir(parents=True)
    path = directory / "1m.parquet"
    frame.to_parquet(path, index=False)

    result = validate_parquet(path, ohlcv_root=root)

    assert not result.gaps, (
        f"weekend and overnight gaps must stay expected, got {result.gaps[:3]}"
    )
    assert result.status == "ok", (
        f"a clean weekday series reported {result.status}: {result.warnings}"
    )


def test_source_meta_is_found_under_the_key_the_downloader_actually_writes(
    tmp_path, monkeypatch
):
    """Two readers of one shared file must agree on its key.

    The downloader writes `source/asset_class/symbol/timeframe` and `catalog_builder`
    reads that shape. `validate` looked up three *path-shaped* keys - the absolute
    path, the posix path, and the path relative to `DATA_DIR`, all of which end in
    `.parquet`. Verified against the live `data/source_meta.json`: 56 keys, every
    one canonical, none path-shaped.

    So the lookup never matched anything and `_dataset_metadata` always returned
    empty, and `validate_parquet` fell back to the observed-session heuristic. That
    fallback is the lossy consumer: the metadata carries the session and calendar the
    downloader *knows*, and it was being discarded, so the verdict on whether data
    is clean was made from an inference.

    Legacy path-shaped keys are still accepted - the point is not to break an older
    file - but the canonical shape is tried first because that is what is written.
    """
    import json

    import pandas as pd

    from data_tools import validate as validate_module
    from data_tools.validate import _dataset_metadata

    key = "dukascopy/forex/EURUSD/1h"
    declared = {"session": {"start": "08:00", "end": "16:00"}, "calendar": "FX"}

    root = tmp_path
    (root / "ohlcv").mkdir()
    target = root / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-02 09:00", tz="UTC")],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    ).to_parquet(target, index=False)
    (root / "source_meta.json").write_text(json.dumps({key: declared}))
    monkeypatch.setattr(validate_module, "DATA_DIR", root)

    assert _dataset_metadata(target) == declared, (
        f"the dataset metadata was not found under the key the downloader writes "
        f"({key!r}), so validation fell back to inferring the session from the data"
    )


def test_a_legacy_path_shaped_source_meta_key_is_still_accepted(
    tmp_path, monkeypatch
):
    """The fix must not break a file written under the old scheme."""
    import json

    import pandas as pd

    from data_tools import validate as validate_module
    from data_tools.validate import _dataset_metadata

    root = tmp_path
    (root / "ohlcv").mkdir()
    target = root / "ohlcv" / "dukascopy" / "forex" / "EURUSD" / "1h.parquet"
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-02 09:00", tz="UTC")],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    ).to_parquet(target, index=False)
    (root / "source_meta.json").write_text(
        json.dumps({str(target): {"calendar": "legacy"}})
    )
    monkeypatch.setattr(validate_module, "DATA_DIR", root)

    assert _dataset_metadata(target) == {"calendar": "legacy"}, (
        "an existing path-shaped key stopped resolving, which would discard metadata "
        "for any dataset written before the canonical scheme"
    )


# ── A malformed session is not an absent one ────────────────────────────────
#
# `_session_window` used to return `None` for "nothing configured", "no
# separator" and "a clock that will not parse" alike, and for equity-like assets
# the caller then substituted an *observed* window - so a configured session that
# never parsed was silently replaced by a guess. It is now a distinct state that
# warns, while still classifying exactly as before.


class TestMalformedSessionIsNotAnAbsentOne:
    def test_a_session_without_a_separator_is_malformed_not_absent(self):
        from data_tools.validate import MALFORMED_SESSION, _session_window

        assert _session_window("09:30-16:00") == (570, 960)
        assert _session_window("garbage") is MALFORMED_SESSION
        assert _session_window("") is None, "an empty string is empty config, not broken config"
        assert _session_window(None) is None
        assert _session_window({"start": None, "end": None}) is None, (
            "an explicitly empty window is absent, not malformed"
        )

    def test_an_unparseable_clock_is_malformed(self):
        from data_tools.validate import MALFORMED_SESSION, _session_window

        assert _session_window("nine-16:00") is MALFORMED_SESSION
        assert _session_window({"start": "09:30", "end": "later"}) is MALFORMED_SESSION

    def test_a_malformed_session_warns_once_per_value(self, caplog):
        """The warning used to sit in the per-gap path, so a typo on a multi-year
        intraday file produced one warning per gap - which trains an operator to
        ignore the only signal that their own config caused the warnings."""
        from data_tools.validate import (
            _warn_malformed_session_once,
            reset_malformed_session_warnings,
        )

        reset_malformed_session_warnings()
        with caplog.at_level("WARNING"):
            for _ in range(3):
                _warn_malformed_session_once("garbage", "equity")
            _warn_malformed_session_once("other garbage", "equity")

        messages = [r.message for r in caplog.records if "could not be parsed" in r.message]
        assert len(messages) == 2, f"expected one warning per distinct value, got {messages}"

    def test_a_malformed_session_still_classifies_like_no_session(self):
        """Preserved behaviour: malformed maps to "no session", so for an
        equity-like asset the observed window is substituted. What changed is
        that the substitution is no longer silent."""
        from data_tools.validate import (
            _gap_classification,
            reset_malformed_session_warnings,
        )

        reset_malformed_session_warnings()
        start = pd.Timestamp("2026-09-08 15:00", tz="UTC")   # 11:00 Eastern
        end = pd.Timestamp("2026-09-08 15:30", tz="UTC")
        observed = (13 * 60 + 30, 20 * 60)                    # 09:30-16:00 Eastern in UTC

        absent = _gap_classification(start, end, "equity", "1m", observed_session=observed)
        malformed = _gap_classification(
            start,
            end,
            "equity",
            "1m",
            session="garbage",
            observed_session=observed,
        )

        assert absent == "session"
        assert malformed == absent, (
            "a malformed session stopped behaving like an absent one; the classification "
            "now depends on a value that never parsed"
        )


def test_the_report_path_must_stay_inside_the_data_directory(tmp_path):
    """`--report` reaches this CLI from the viewer's `/api/data/validate` payload,
    so it is caller-controlled. Writing wherever it points is a path-injection
    sink; the report is confined to the data directory it describes."""
    from data_tools.validate import _report_path

    root = tmp_path / "data"
    root.mkdir()

    assert _report_path("validation_report.json", root) == root / "validation_report.json"
    assert _report_path(str(root / "nested" / "r.json"), root) == root / "nested" / "r.json"

    for escaping in ("../outside.json", "/etc/passwd", "nested/../../outside.json"):
        with pytest.raises(ValueError, match="inside the data directory"):
            _report_path(escaping, root)

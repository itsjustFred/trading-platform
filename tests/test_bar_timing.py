"""Bar-spacing helpers shared by the clock-exit strategies."""

import pandas as pd
import pytest

from shared.bar_timing import clock_exit_signal_mask, modal_bar_minutes


def test_modal_spacing_ignores_gaps_and_weekends():
    idx = pd.date_range("2024-01-05 20:00", periods=4, freq="5min", tz="UTC").append(
        pd.date_range("2024-01-08 14:00", periods=4, freq="5min", tz="UTC")
    )
    assert modal_bar_minutes(idx) == 5.0


@pytest.mark.parametrize("values", [[], ["2024-01-01"]])
def test_modal_spacing_unknown_is_zero(values):
    assert modal_bar_minutes(pd.DatetimeIndex(values, tz="UTC")) == 0.0


def test_modal_spacing_accepts_a_series_and_skips_nat():
    series = pd.Series(pd.to_datetime(["2024-01-01 00:00", None, "2024-01-01 01:00", "2024-01-01 02:00"], utc=True))
    assert modal_bar_minutes(series) == 60.0


def test_clock_exit_signal_is_the_bar_before_the_exit_open():
    minutes = pd.Series([15 * 60 + 40, 15 * 60 + 44, 15 * 60 + 45, 16 * 60])
    mask = clock_exit_signal_mask(minutes, 1.0, 15 * 60 + 45)
    assert mask.tolist() == [False, True, True, True]


def test_clock_exit_signal_with_coarse_bars_and_zero_spacing():
    minutes = pd.Series([15 * 60, 15 * 60 + 30])
    assert clock_exit_signal_mask(minutes, 30.0, 15 * 60 + 45).tolist() == [False, True]
    assert clock_exit_signal_mask(minutes, 0.0, 15 * 60 + 45).tolist() == [False, False]

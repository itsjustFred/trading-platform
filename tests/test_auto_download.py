import pandas as pd

from data_tools.auto_download import _auto_download_source, _observed_session_window


def test_index_cfd_aliases_use_dukascopy_source():
    assert _auto_download_source("SP500", "1m") == "dukascopy"
    assert _auto_download_source("NAS100", "15m") == "dukascopy"
    assert _auto_download_source("US100", "15m") == "dukascopy"


def test_us_stock_intraday_still_uses_intraday_stock_plan():
    assert _auto_download_source("AAPL", "1h") == "huggingface"


def test_observed_session_does_not_merge_different_daily_windows():
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=4, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T14:00", periods=4, freq="1h", tz="UTC"),
    ]

    assert _observed_session_window(stamps, 60.0) is None


def test_observed_session_does_not_shrink_for_a_truncated_day():
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=5, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T09:00", periods=2, freq="1h", tz="UTC"),
    ]

    assert _observed_session_window(stamps, 60.0) is None


def test_observed_session_allows_a_final_day_cut_at_the_requested_end():
    stamps = [
        *pd.date_range("2024-01-02T09:00", periods=5, freq="1h", tz="UTC"),
        *pd.date_range("2024-01-03T09:00", periods=2, freq="1h", tz="UTC"),
    ]

    assert _observed_session_window(
        stamps,
        60.0,
        start_ts=pd.Timestamp("2024-01-02T00:00:00Z"),
        end_ts=pd.Timestamp("2024-01-03T10:00:00Z"),
    ) == (540, 840)
    assert _observed_session_window(
        stamps,
        60.0,
        start_ts=pd.Timestamp("2024-01-02T00:00:00Z"),
        end_ts=pd.Timestamp("2024-01-03T13:00:00Z"),
    ) is None

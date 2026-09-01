"""Regression coverage for the benchmark payload helpers (Sonar S3776 slice).

`_benchmark_payload` used to be a single high-complexity function that mixed
timeframe fallback, daily resampling, window filtering and equity construction.
These tests pin the extracted pieces so the refactor stays behavior-preserving.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException  # noqa: E402

from viewer.main import (  # noqa: E402
    _benchmark_equity_points,
    _filter_benchmark_df,
    _load_benchmark_daily_df,
    _resample_to_daily,
)


def _intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 10:00:00",
                    "2024-01-01 11:00:00",
                    "2024-01-02 10:00:00",
                ],
                utc=True,
            ),
            "open": [10.0, 11.0, 12.0],
            "high": [11.0, 12.0, 13.0],
            "low": [9.5, 10.5, 11.5],
            "close": [10.5, 11.5, 12.5],
            "volume": [100, 200, 300],
        }
    )


def test_resample_to_daily_produces_one_bar_per_day_with_timestamp_column():
    daily = _resample_to_daily(_intraday_frame())

    assert list(daily.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(daily) == 2
    first = daily.iloc[0]
    assert first["open"] == pytest.approx(10.0)
    assert first["high"] == pytest.approx(12.0)
    assert first["low"] == pytest.approx(9.5)
    assert first["close"] == pytest.approx(11.5)
    assert first["volume"] == pytest.approx(300)


def test_load_benchmark_daily_df_resamples_intraday_fallback_for_1d():
    intraday = _intraday_frame()

    def fake_load(symbol, timeframe):
        return intraday if timeframe == "4h" else None

    with patch("viewer.main.legacy_viewer._load_ohlcv_df", side_effect=fake_load):
        df, resolved = _load_benchmark_daily_df("SPY", "1d")

    assert resolved == "1d"
    assert df is not None and len(df) == 2


def test_load_benchmark_daily_df_returns_direct_hit_without_resampling():
    daily = _resample_to_daily(_intraday_frame())

    with patch(
        "viewer.main.legacy_viewer._load_ohlcv_df",
        side_effect=lambda symbol, timeframe: daily if timeframe == "1d" else None,
    ):
        df, resolved = _load_benchmark_daily_df("SPY", "1d")

    assert resolved == "1d"
    assert df is not None and len(df) == 2


def test_load_benchmark_daily_df_returns_none_when_no_timeframe_has_data():
    with patch("viewer.main.legacy_viewer._load_ohlcv_df", return_value=None):
        df, resolved = _load_benchmark_daily_df("SPY", "1d")

    assert df is None
    assert resolved == "1d"


def test_filter_benchmark_df_applies_start_and_end_bounds():
    df = _resample_to_daily(_intraday_frame())

    filtered = _filter_benchmark_df(df, start="2024-01-02", end="2024-01-02")

    assert len(filtered) == 1
    assert filtered.iloc[0]["close"] == pytest.approx(12.5)


def test_benchmark_equity_points_computes_buy_and_hold_curve():
    df = _resample_to_daily(_intraday_frame())

    points = _benchmark_equity_points(df, initial=1000.0)

    # First daily close is 11.5 (day-1 last bar); share count is fixed from it.
    assert [p.equity for p in points] == pytest.approx(
        [1000.0, round(1000.0 / 11.5 * 12.5, 2)]
    )
    assert points[0].time < points[1].time


def test_benchmark_endpoint_returns_resampled_equity_for_intraday_symbol(viewer_client):
    """The route keeps serving an equity curve when only intraday data exists."""
    intraday = _intraday_frame()

    with patch(
        "viewer.main.legacy_viewer._load_ohlcv_df",
        side_effect=lambda symbol, timeframe: intraday if timeframe == "4h" else None,
    ):
        resp = viewer_client.get("/api/benchmark/SPY")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["timeframe"] == "1d"
    assert len(payload["equity"]) == 2


def test_benchmark_endpoint_still_404s_without_any_data(viewer_client):
    with patch("viewer.main.legacy_viewer._load_ohlcv_df", return_value=None):
        resp = viewer_client.get("/api/benchmark/SPY")

    assert resp.status_code == 404


def test_benchmark_endpoint_404s_when_window_filters_out_all_rows(viewer_client):
    intraday = _intraday_frame()

    with patch(
        "viewer.main.legacy_viewer._load_ohlcv_df",
        side_effect=lambda symbol, timeframe: intraday if timeframe == "1d" else None,
    ):
        resp = viewer_client.get("/api/benchmark/SPY?start=2030-01-01&end=2030-12-31")

    assert resp.status_code == 404
    assert "No data in range" in resp.json()["detail"]

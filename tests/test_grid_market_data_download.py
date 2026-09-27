from pathlib import Path

import pandas as pd

import data_tools.download_grid_market_data as dl
from data_tools.download_grid_market_data import (
    DEFAULT_SYMBOLS,
    _timeframe_sort_key,
    normalize_ohlcv_columns,
    normalize_symbol,
    normalize_timeframe_value,
    parse_csv_list,
)


def test_grid_market_data_defaults_and_aliases():
    assert parse_csv_list("xauusd, sp500,NAS100,eurusd,usdjpy,oil") == DEFAULT_SYMBOLS
    assert [normalize_symbol(symbol) for symbol in DEFAULT_SYMBOLS] == ["XAUUSD", "US500", "USTEC", "EURUSD", "USDJPY", "USOIL"]
    assert [normalize_timeframe_value(tf) for tf in ["1min", "h1", "h4", "daily"]] == ["1m", "1h", "4h", "1d"]


def test_normalize_ohlcv_columns_fills_from_bid(tmp_path: Path):
    path = tmp_path / "test.parquet"
    df = pd.DataFrame({
        "timestamp": pd.date_range("2020-01-01", periods=2, freq="min", tz="UTC"),
        "open": [float("nan"), 2.0],
        "high": [float("nan"), 2.1],
        "low": [float("nan"), 1.9],
        "close": [float("nan"), 2.05],
        "volume": [float("nan"), 10.0],
        "bid_open": [1.0, 2.0],
        "bid_high": [1.1, 2.1],
        "bid_low": [0.9, 1.9],
        "bid_close": [1.05, 2.05],
        "bid_volume": [1.0, 10.0],
    })
    df.to_parquet(path, index=False)

    normalize_ohlcv_columns(path)

    actual = pd.read_parquet(path)
    assert actual.loc[0, "open"] == 1.0
    assert actual.loc[0, "high"] == 1.1
    assert actual.loc[0, "low"] == 0.9
    assert actual.loc[0, "close"] == 1.05
    assert actual.loc[0, "volume"] == 1.0
    assert actual.loc[1, "open"] == 2.0


def test_normalize_timeframe_value_handles_m30():
    # Regression: m30/30m must normalize to the canonical 30m bucket.
    assert normalize_timeframe_value("m30") == "30m"
    assert normalize_timeframe_value("30m") == "30m"


def test_timeframe_sort_key_orders_30m_without_raising():
    # Regression for the ValueError that aborted the whole batch when 30m was
    # requested: 30m is now part of the canonical order and sorts cleanly.
    timeframes = {normalize_timeframe_value(v) for v in ["m30", "30m", "1h", "1m"]}
    assert sorted(timeframes, key=_timeframe_sort_key) == ["1m", "30m", "1h"]


def test_timeframe_sort_key_unknown_sorts_last():
    assert sorted(["zz", "1m", "30m"], key=_timeframe_sort_key) == ["1m", "30m", "zz"]


def test_download_symbol_isolates_unknown_timeframe(tmp_path: Path, monkeypatch):
    # An unsupported timeframe marks only that download failed and never raises,
    # so the surrounding batch keeps processing other symbols/timeframes.
    monkeypatch.setattr(dl, "DukascopyNativeDownloader", lambda: object())
    result = dl.download_symbol(
        requested_symbol="EURUSD",
        canonical_symbol="EURUSD",
        timeframes=["7m"],
        output_dir=tmp_path,
        start="2020-01-01",
        end=None,
        price_type="bid",
        batch_size=1,
        pipeline_batches=1,
        pause_ms=0,
        force=False,
    )
    assert result["status"] == "failed"
    assert "7m" in result["timeframe_errors"]

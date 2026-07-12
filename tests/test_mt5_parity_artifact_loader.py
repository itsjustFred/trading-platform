from pathlib import Path

import numpy as np
import pandas as pd

from scripts.regenerate_mt5_parity_artifacts import (
    _build_trades,
    _load_ohlcv,
    _price_unit_value,
    _round_mt5_lots,
)


def test_load_ohlcv_fills_sparse_canonical_ohlc_from_bid_columns(tmp_path: Path):
    symbol_dir = tmp_path / "ohlcv" / "dukascopy" / "indices" / "US500"
    symbol_dir.mkdir(parents=True)
    timestamps = pd.date_range("2018-01-02 07:05", periods=3, freq="1min", tz="UTC")
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [np.nan, np.nan, 2700.0],
            "high": [np.nan, np.nan, 2702.0],
            "low": [np.nan, np.nan, 2699.0],
            "close": [np.nan, np.nan, 2701.0],
            "volume": [np.nan, np.nan, 10.0],
            "bid_open": [2600.0, 2601.0, 2602.0],
            "bid_high": [2600.5, 2601.5, 2602.5],
            "bid_low": [2599.5, 2600.5, 2601.5],
            "bid_close": [2600.2, 2601.2, 2602.2],
            "bid_volume": [1.0, 2.0, 3.0],
        }
    ).to_parquet(symbol_dir / "1m.parquet", index=False)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "bid_open": [9999.0, 9999.0, 9999.0],
            "bid_high": [9999.0, 9999.0, 9999.0],
            "bid_low": [9999.0, 9999.0, 9999.0],
            "bid_close": [9999.0, 9999.0, 9999.0],
        }
    ).to_parquet(symbol_dir / "m1_bid.parquet", index=False)

    frame = _load_ohlcv(tmp_path, "US500", "1m", "2018-01-02", "2018-01-03")

    assert len(frame) == 3
    assert frame["open"].tolist() == [2600.0, 2601.0, 2700.0]
    assert frame["high"].tolist() == [2600.5, 2601.5, 2702.0]
    assert frame["low"].tolist() == [2599.5, 2600.5, 2699.0]
    assert frame["close"].tolist() == [2600.2, 2601.2, 2701.0]


def test_load_ohlcv_uses_m1_bid_when_canonical_file_is_absent(tmp_path: Path):
    symbol_dir = tmp_path / "ohlcv" / "dukascopy" / "forex" / "EURUSD"
    symbol_dir.mkdir(parents=True)
    timestamps = pd.date_range("2020-01-01", periods=2, freq="1min", tz="UTC")
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "bid_open": [1.10, 1.11],
            "bid_high": [1.101, 1.111],
            "bid_low": [1.099, 1.109],
            "bid_close": [1.1005, 1.1105],
            "bid_volume": [100, 200],
        }
    ).to_parquet(symbol_dir / "m1_bid.parquet", index=False)

    frame = _load_ohlcv(tmp_path, "EURUSD", "1m", "2020-01-01", "2020-01-02")

    assert frame["open"].tolist() == [1.10, 1.11]
    assert frame["close"].tolist() == [1.1005, 1.1105]


def test_price_unit_value_uses_eurusd_account_conversion():
    conversion = pd.Series(
        [1.25],
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-01 12:00", tz="UTC")]),
    )

    assert _price_unit_value("EURUSD", "2024-01-01T12:00:00Z", conversion) == 80_000.0
    assert _price_unit_value("USA500.IDX", "2024-01-01T12:00:00Z", conversion) == 0.8


def test_round_mt5_lots_uses_report_symbol_steps():
    assert _round_mt5_lots("EURUSD", 3.339999999999) == 3.33
    assert _round_mt5_lots("USA500.IDX", 37.9) == 37.0


def test_build_trades_prefers_shifted_stop_distance_for_fill_anchoring():
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [101.0, 100.0, 99.8, 102.0],
            "high": [101.2, 100.2, 100.1, 102.2],
            "low": [100.8, 99.8, 99.4, 101.8],
            "close": [101.0, 100.0, 99.9, 102.0],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        # This shifted absolute SL would imply a too-tight 0.5 stop if compared
        # with the pre-fill signal close.  The explicit shifted stop_distance is
        # the source-faithful value to re-anchor from the fill price.
        "sl_price": pd.Series([100.5, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([2.0, np.nan, np.nan, np.nan], index=frame.index),
    }

    trades = _build_trades(
        case={"symbol": "USA500.IDX", "execution_symbol": "US500", "execution_timeframe": "1m"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=None,
    )

    assert len(trades) == 1
    assert trades[0].exits[0].reason == "signal"
    assert trades[0].meta["sl_price"] == 98.0


def test_build_trades_ignores_non_positive_stop_distance():
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [101.0, 100.0, 99.8, 102.0],
            "high": [101.2, 100.2, 100.1, 102.2],
            "low": [100.8, 99.8, 99.4, 101.8],
            "close": [101.0, 100.0, 99.9, 102.0],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        "sl_price": pd.Series([100.5, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([0.0, np.nan, np.nan, np.nan], index=frame.index),
    }

    trades = _build_trades(
        case={"symbol": "USA500.IDX", "execution_symbol": "US500", "execution_timeframe": "1m"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=None,
    )

    assert len(trades) == 1
    assert trades[0].exits[0].reason == "stop_loss"
    assert trades[0].meta["sl_price"] == 99.5

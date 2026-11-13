from pathlib import Path

import numpy as np
import pandas as pd

from scripts.regenerate_mt5_parity_artifacts import (
    COMPARISON_CASES,
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

    # EUR-Account: USD->EUR Konvertierung (Legacy-Verhalten, jetzt explizit).
    assert _price_unit_value("EURUSD", "2024-01-01T12:00:00Z", conversion, account_currency="EUR") == 80_000.0
    assert _price_unit_value("USA500.IDX", "2024-01-01T12:00:00Z", conversion, account_currency="EUR") == 0.8


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


# ---------------------------------------------------------------------------
# IVB-Parity-Audit Regressionstests (Bugs 1-4, TDD red-first)
# ---------------------------------------------------------------------------
def _synthetic_conversion(rates, start="2024-01-01 00:00"):
    idx = pd.DatetimeIndex(
        [pd.Timestamp(start, tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(len(rates))]
    )
    return pd.Series(rates, index=idx)


def test_bug1_only_eur_accounts_convert():
    conversion = pd.Series(
        [1.25],
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-01 12:00", tz="UTC")]),
    )
    # USD-Accounts: keine Konvertierung (Contract pur)
    assert _price_unit_value("EURUSD", "2024-01-01T12:00:00Z", conversion, account_currency="USD") == 100_000.0
    assert _price_unit_value("USA500.IDX", "2024-01-01T12:00:00Z", conversion, account_currency="USD") == 1.0
    # EUR-Accounts: USD->EUR Konvertierung
    assert _price_unit_value("EURUSD", "2024-01-01T12:00:00Z", conversion, account_currency="EUR") == 80_000.0
    assert _price_unit_value("USATECH.IDX", "2024-01-01T12:00:00Z", conversion, account_currency="EUR") == 0.8


def test_bug1_comparison_cases_carry_account_currency_from_setfiles():
    by_label = {c["label"]: c for c in COMPARISON_CASES}
    # Laut Setfiles Currency=... (references/mt5-*/ *_setfile.ini)
    assert by_label["EURUSD_H1"]["account_currency"] == "USD"
    assert by_label["NAS100_M15"]["account_currency"] == "EUR"
    assert by_label["SP500_M1"]["account_currency"] == "USD"


def test_bug1_build_trades_risk_unit_value_respects_account_currency():
    conversion = pd.Series(
        [1.22],
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02 14:00", tz="UTC")]),
    )
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0, 100.0],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        "sl_price": pd.Series([99.0, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([1.0, np.nan, np.nan, np.nan], index=frame.index),
    }
    usd_trades = _build_trades(
        case={"symbol": "USA500.IDX", "execution_symbol": "US500", "execution_timeframe": "1m", "account_currency": "USD"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=conversion,
    )
    assert usd_trades[0].meta["risk_unit_value"] == 1.0
    assert usd_trades[0].meta["pnl_unit_value"] == 1.0


def test_bug2_stop_search_includes_fill_bar():
    # LONG: Fill-Bar-Low 80 < SL 99 -> stop_loss (Broker-Stop ab Fill aktiv).
    # Alte Range [fill+1:exit+1] verpasst die Fill-Bar und meldet fälschlich signal.
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0, 102.0],
            "low": [99.5, 80.0, 99.5, 101.0],
            "close": [100.0, 100.0, 100.0, 102.0],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        "sl_price": pd.Series([np.nan, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([1.0, np.nan, np.nan, np.nan], index=frame.index),
    }
    trades = _build_trades(
        case={"symbol": "USA500.IDX", "execution_symbol": "US500", "execution_timeframe": "1m", "account_currency": "USD"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=None,
    )
    assert len(trades) == 1
    assert trades[0].exits[0].reason == "stop_loss"


def test_bug3a_entry_and_time_exit_use_bar_open():
    # MT5-First-Tick-Näherung: Entry- und Time-Exit-Fills zum Bar-Open,
    # nicht zum Bar-Close (bisher). LONG-Eintritt=Ask-Open, Zeit-Exit=Bid-Open.
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 110.0, 111.0, 120.0],
            "high": [101.0, 111.0, 112.0, 121.0],
            "low": [99.0, 109.0, 110.0, 119.0],
            "close": [100.5, 115.0, 116.0, 125.0],
            "bid_open": [99.9, 109.9, 110.9, 119.9],
            "ask_open": [100.1, 110.1, 111.1, 120.1],
            "bid_close": [100.4, 114.9, 115.9, 124.9],
            "ask_close": [100.6, 115.1, 116.1, 125.1],
            "bid_low": [98.9, 108.9, 109.9, 118.9],
            "bid_high": [101.1, 111.1, 112.1, 121.1],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        "sl_price": pd.Series([50.0, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([5.0, np.nan, np.nan, np.nan], index=frame.index),
    }
    trades = _build_trades(
        case={"symbol": "EURUSD", "execution_symbol": "EURUSD", "execution_timeframe": "1m", "account_currency": "USD"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=None,
    )
    assert len(trades) == 1
    # LONG-Entry = Ask-Open der Fill-Bar (Index 1), Time-Exit = Bid-Open (Index 3)
    assert trades[0].entry_price == 110.1
    assert trades[0].exits[0].price == 119.9
    assert trades[0].exits[0].reason == "signal"


def test_bug3b_tick_sizing_matches_mt5_and_clamps():
    # EURUSD-Audit-Repro: SL-Distanz 0.002 (20 Pips), Risk 500 USD ->
    # MT5 2.50 Lots (tick 1e-5/1.0). Alte contract/rate-Näherung mit
    # EURUSD 1.09 gab 2.725.
    conversion = pd.Series(
        [1.09],
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02 14:00", tz="UTC")]),
    )
    timestamps = pd.date_range("2024-01-02 14:44", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.1000, 1.1000, 1.1000, 1.1000],
            "high": [1.1010, 1.1010, 1.1010, 1.1010],
            "low": [1.0990, 1.0990, 1.0990, 1.0990],
            "close": [1.1000, 1.1000, 1.1000, 1.1000],
        }
    )
    entries = pd.Series([True, False, False, False], index=frame.index)
    exits = pd.Series([False, False, False, True], index=frame.index)
    indicators = {
        "fixed_risk_amount": 500.0,
        "direction": pd.Series(["LONG", None, None, None], index=frame.index),
        "sl_price": pd.Series([np.nan, np.nan, np.nan, np.nan], index=frame.index),
        "stop_distance": pd.Series([0.002, np.nan, np.nan, np.nan], index=frame.index),
    }
    trades = _build_trades(
        case={"symbol": "EURUSD", "execution_symbol": "EURUSD", "execution_timeframe": "1m", "account_currency": "USD"},
        df=frame,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=100_000.0,
        conversion=conversion,
    )
    assert len(trades) == 1
    assert trades[0].quantity == 2.5
    # Broker-Clamp (.mq5 MathMax(min)/MathMin(max)): kein 0-Lot, kein Riesen-Lot
    assert _round_mt5_lots("EURUSD", 0.001) >= 0.01
    assert _round_mt5_lots("EURUSD", 100000.0) <= 1000.0
    assert _round_mt5_lots("USA500.IDX", 0.2) >= 1.0


def test_bug4_searchsorted_uses_last_known_rate():
    # Lücken-Szenario: Raten 00:00=1.10, 00:05=1.20; Anfrage 00:02 ->
    # last-known 1.10 (side='right'-1), nicht nächste Rate 1.20.
    conversion = pd.Series(
        [1.10, 1.20],
        index=pd.DatetimeIndex(
            [pd.Timestamp("2024-01-01 00:00", tz="UTC"), pd.Timestamp("2024-01-01 00:05", tz="UTC")]
        ),
    )
    value = _price_unit_value("USATECH.IDX", "2024-01-01T00:02:00Z", conversion, account_currency="EUR")
    assert value == 1.0 / 1.10


def test_comparison_cases_run_on_the_broker_clock_without_utc_day_d1_files():
    from shared.broker_clock import resolve_broker_clock

    by_label = {c["label"]: c for c in COMPARISON_CASES}
    assert by_label["EURUSD_H1"]["broker_clock"] == "dukascopy_mt5_forex"
    assert by_label["NAS100_M15"]["broker_clock"] == "dukascopy_mt5_index"
    assert by_label["SP500_M1"]["broker_clock"] == "dukascopy_mt5_index"
    for case in COMPARISON_CASES:
        assert resolve_broker_clock(case["broker_clock"]) is not None
        # UTC-day D1 files have other day boundaries than the broker's server-clock D1 bars.
        assert not case.get("use_native_daily_ohlcv")

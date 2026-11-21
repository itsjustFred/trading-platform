"""Venue/session calendar resolution, cache safety, and FX daily convention (#133)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from shared import venue_calendar
from shared.venue_calendar import (
    BrokerScheduleCalendar,
    ContinuousCalendar,
    ExchangeSessionCalendar,
    FxWeekCalendar,
    NyseCalendar,
    load_cached_calendar,
    resolve_venue_calendar,
    save_cached_calendar,
)


def _write_cache(data_dir: Path, source: str, symbol: str, payload: dict) -> None:
    path = data_dir / "venue_calendars" / source / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "symbol,asset_class,source,expected_type",
    [
        # Strict FX shapes: explicit ISO currency pairs only.
        ("EURUSD", None, None, FxWeekCalendar),
        ("EUR/USD", None, None, FxWeekCalendar),
        ("GBPJPY", None, "dukascopy", FxWeekCalendar),
        # Crypto only from the known-base allowlist.
        ("BTCUSD", None, None, ContinuousCalendar),
        ("BTC-USD", None, "ctrader", ContinuousCalendar),
        ("ETH/USD", None, None, ContinuousCalendar),
        # US equities need a known equity source.
        ("AAPL", None, "alpaca", NyseCalendar),
        ("AAPL", "equity", "test", NyseCalendar),
        ("BRK.B", None, "yfinance", NyseCalendar),
        # Explicit metadata wins over shapes.
        ("EURUSD", "forex", None, FxWeekCalendar),
        ("BTCUSD", "crypto", None, ContinuousCalendar),
        ("AAPL", "equity", "alpaca", NyseCalendar),
        # Ambiguous or broker-CFD shapes fail closed.
        ("XAUUSD", None, None, type(None)),
        ("XAUUSD", "metals", None, type(None)),
        ("XAU", None, None, type(None)),
        ("GOLD", None, None, type(None)),
        ("SPX", None, None, type(None)),
        ("NVIDIA", None, "alpaca", type(None)),
        ("AAPL", None, None, type(None)),
        ("AAPL", "equity", "ctrader", type(None)),
        ("FOO/BAR", None, None, type(None)),
        ("", None, None, type(None)),
    ],
)
def test_resolve_venue_calendar_symbol_shapes(
    symbol, asset_class, source, expected_type, tmp_path: Path
):
    calendar = resolve_venue_calendar(
        symbol=symbol,
        asset_class=asset_class,
        source=source,
        data_dir=tmp_path,
    )
    if expected_type is type(None):
        assert calendar is None, f"{symbol!r} must fail closed"
    else:
        assert isinstance(calendar, expected_type), (
            f"{symbol!r} resolved to {type(calendar).__name__}"
        )


def test_alpaca_cache_beats_shape_heuristics(tmp_path: Path):
    _write_cache(
        tmp_path,
        "alpaca",
        "AAPL",
        {
            "kind": "exchange_sessions",
            "name": "alpaca-AAPL-exchange",
            "source": "alpaca",
            "schedule_timezone": "America/New_York",
            "sessions": [
                {"date": "2024-07-03", "open": "09:30", "close": "13:00"},
                {"date": "2024-07-05", "open": "09:30", "close": "16:00"},
            ],
        },
    )
    calendar = resolve_venue_calendar(
        symbol="AAPL", asset_class="equity", source="alpaca", data_dir=tmp_path
    )
    assert isinstance(calendar, ExchangeSessionCalendar)
    # Early close on 2024-07-03 is honoured: session ends 13:00 ET.
    intervals = calendar.trading_intervals(
        pd.Timestamp("2024-07-03T00:00:00Z"), pd.Timestamp("2024-07-03T23:59:59Z")
    )
    assert len(intervals) == 1
    assert intervals[0].close == pd.Timestamp("2024-07-03T17:00:00Z")


def test_ctrader_cache_used_for_broker_cfd_asset_class(tmp_path: Path):
    _write_cache(
        tmp_path,
        "ctrader",
        "XAUUSD",
        {
            "kind": "broker_schedule",
            "name": "ctrader-XAUUSD",
            "source": "ctrader",
            "symbol": "XAUUSD",
            "schedule_timezone": "UTC",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "schedule": [{"start_second": 0, "end_second": 86400}],
            "holidays": [],
        },
    )
    calendar = resolve_venue_calendar(
        symbol="XAUUSD", asset_class="metals", source="dukascopy", data_dir=tmp_path
    )
    assert isinstance(calendar, BrokerScheduleCalendar)


def test_malformed_cache_fails_closed(tmp_path: Path):
    cases = [
        "not-json{",
        json.dumps(["a", "list"]),
        json.dumps({"kind": "broker_schedule", "schedule": []}),
        json.dumps({"kind": "exchange_sessions", "sessions": []}),
        json.dumps({"kind": "broker_schedule", "schedule": [{"start_second": "x"}]}),
        json.dumps({"kind": "unknown_kind"}),
    ]
    for index, raw in enumerate(cases):
        path = tmp_path / "venue_calendars" / "ctrader" / f"BAD{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        assert load_cached_calendar(
            data_dir=tmp_path, source="ctrader", symbol=f"BAD{index}"
        ) is None, f"case {index} must fail closed"


def test_holiday_without_date_is_dropped_not_defaulted():
    calendar = BrokerScheduleCalendar(
        {
            "kind": "broker_schedule",
            "schedule_timezone": "UTC",
            "schedule": [{"start_second": 0, "end_second": 86400}],
            "holidays": [
                {"is_recurring": True},  # no date: must not become Jan 1
                {"holiday_date": 0, "is_recurring": True},  # explicit 1970-01-01
            ],
        }
    )
    assert len(calendar.holidays) == 1
    assert calendar.holidays[0]["date_days"] == 0


def test_cache_source_is_allowlisted(tmp_path: Path):
    with pytest.raises(ValueError):
        save_cached_calendar(
            data_dir=tmp_path, source="../../etc", symbol="X", payload={}
        )
    assert load_cached_calendar(
        data_dir=tmp_path, source="../escape", symbol="X"
    ) is None


def test_fx_week_expects_five_daily_bars_per_week():
    calendar = FxWeekCalendar()
    # Mon 2024-01-01 .. Sat 2024-01-06: five NY-close-labelled daily bars.
    # (Sunday 17:00 ET reopens the NEXT week, so its Monday-labelled bar only
    # counts once the window reaches Sunday evening ET.)
    dates = calendar.daily_bar_dates(
        pd.Timestamp("2024-01-01T00:00:00Z"), pd.Timestamp("2024-01-06T23:59:59Z")
    )
    assert dates == {
        pd.Timestamp(d).date()
        for d in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")
    }
    # A stamp inside the Sunday-reopen session labels Monday's bar.
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-07T23:00:00Z")) == pd.Timestamp(
        "2024-01-08"
    ).date()
    # Saturday stamps are surplus, never a daily bar.
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-06T12:00:00Z")) is None


def test_fx_daily_bars_use_ny_close_not_utc_sunday():
    """A real-shaped Dukascopy D1 sample: Mon-Fri stamps, 5 bars/week."""
    calendar = FxWeekCalendar()
    stamps = [
        pd.Timestamp(f"2024-01-0{day}T21:00:00Z") for day in range(1, 6)
    ]
    start, end = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-07")
    expected = calendar.daily_bar_dates(start, end)
    present = {calendar.daily_bar_date(s) for s in stamps}
    assert present == expected
    assert len(expected) == 5


def test_nyse_daily_dates_skip_weekends_and_holidays():
    calendar = NyseCalendar()
    dates = calendar.daily_bar_dates(
        pd.Timestamp("2024-07-01T00:00:00Z"), pd.Timestamp("2024-07-10T23:59:59Z")
    )
    # Jul 4 is a NYSE holiday; Jul 6-7 is a weekend.
    assert dates == {
        pd.Timestamp(d).date()
        for d in ("2024-07-01", "2024-07-02", "2024-07-03", "2024-07-05", "2024-07-08", "2024-07-09", "2024-07-10")
    }


def test_save_and_load_roundtrip(tmp_path: Path):
    payload = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
    )
    path = save_cached_calendar(
        data_dir=tmp_path, source="alpaca", symbol="AAPL", payload=payload
    )
    assert path.exists()
    calendar = load_cached_calendar(data_dir=tmp_path, source="alpaca", symbol="AAPL")
    assert isinstance(calendar, ExchangeSessionCalendar)


# ─── Coverage-gate acceptance (#133) ─────────────────────────────────────────


def _write_bars(root: Path, rel_parts: list[str], timestamps: list[str]) -> None:
    path = root.joinpath(*rel_parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": [1.0] * len(timestamps),
            "high": [1.1] * len(timestamps),
            "low": [0.9] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [10] * len(timestamps),
        }
    ).to_parquet(path, index=False)


def _coverage(**kwargs) -> bool:
    from data_tools.auto_download import has_sufficient_segment_coverage

    return has_sufficient_segment_coverage(**kwargs)


def test_crypto_weekend_gap_fails_coverage(tmp_path: Path):
    """Crypto trades 24/7: a weekend hole is missing data, not a closure."""
    stamps = [
        *pd.date_range("2024-01-05T00:00", "2024-01-05T23:00", freq="1h", tz="UTC"),
        *pd.date_range("2024-01-08T00:00", "2024-01-08T23:00", freq="1h", tz="UTC"),
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "test", "crypto", "BTCUSD", "1h.parquet"],
        [ts.isoformat() for ts in stamps],
    )
    assert not _coverage(
        symbol="BTCUSD", timeframe="1h",
        start="2024-01-05", end="2024-01-08T23:00", data_dir=tmp_path,
    )


def test_fx_sunday_reopen_is_not_a_missing_bar(tmp_path: Path):
    """FX reopens Sunday 22:00 UTC; Fri close -> Sun reopen is expected."""
    stamps = [
        *pd.date_range("2024-01-05T00:00", "2024-01-05T21:00", freq="1h", tz="UTC"),
        *pd.date_range("2024-01-07T22:00", "2024-01-08T10:00", freq="1h", tz="UTC"),
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "dukascopy", "forex", "EURUSD", "1h.parquet"],
        [ts.isoformat() for ts in stamps],
    )
    assert _coverage(
        symbol="EURUSD", timeframe="1h",
        start="2024-01-05", end="2024-01-08T10:00", data_dir=tmp_path,
    )


def test_nyse_daily_coverage_unchanged(tmp_path: Path):
    stamps = pd.date_range("2024-01-01", "2024-01-12", freq="B", tz="UTC")
    _write_bars(
        tmp_path,
        ["ohlcv", "test", "equity", "AAPL", "1d.parquet"],
        [ts.isoformat() for ts in stamps],
    )
    assert _coverage(
        symbol="AAPL", timeframe="1d",
        start="2024-01-01", end="2024-01-12", data_dir=tmp_path,
    )


def test_unknown_venue_fails_closed(tmp_path: Path):
    """A broker-CFD asset class with no cached schedule cannot be assumed 24/7."""
    stamps = list(pd.date_range("2024-01-01", "2024-01-10", freq="1h", tz="UTC"))
    _write_bars(
        tmp_path,
        ["ohlcv", "dukascopy", "indices", "US30", "1h.parquet"],
        [ts.isoformat() for ts in stamps],
    )
    assert not _coverage(
        symbol="US30", timeframe="1h",
        start="2024-01-01", end="2024-01-10", data_dir=tmp_path,
    )


def test_out_of_session_bars_are_surplus_not_missing(tmp_path: Path):
    """Weekend-stamped equity bars do not count against NYSE coverage."""
    weekday = pd.date_range("2024-01-02", "2024-01-05", freq="B", tz="UTC")
    weekend = pd.date_range("2024-01-06", "2024-01-07", freq="D", tz="UTC")
    stamps = [*weekday, *weekend]
    _write_bars(
        tmp_path,
        ["ohlcv", "test", "equity", "MSFT", "1d.parquet"],
        [ts.isoformat() for ts in stamps],
    )
    assert _coverage(
        symbol="MSFT", timeframe="1d",
        start="2024-01-02", end="2024-01-07", data_dir=tmp_path,
    )

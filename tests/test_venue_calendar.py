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
        for d in (
            "2024-07-01", "2024-07-02", "2024-07-03", "2024-07-05",
            "2024-07-08", "2024-07-09", "2024-07-10",
        )
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


# ─── Broker cache writers (fakes only, no broker network) ────────────────────


def test_alpaca_exchange_calendar_fetch_uses_injected_session():
    from data_tools.downloaders.alpaca_dl import fetch_alpaca_exchange_calendar

    calls = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"date": "2024-07-03", "open": "09:30", "close": "13:00"},
                {"date": "2024-07-05", "open": "09:30", "close": "16:00"},
            ]

    class _Session:
        def get(self, url, **kwargs):
            calls["url"] = url
            calls["kwargs"] = kwargs
            return _Response()

    sessions = fetch_alpaca_exchange_calendar(
        api_key="k", secret_key="s", start="2024-07-01", end="2024-07-31",
        session=_Session(),
    )
    assert calls["url"].endswith("/v2/calendar")
    assert calls["kwargs"]["params"] == {"start": "2024-07-01", "end": "2024-07-31"}
    assert sessions[0]["date"] == "2024-07-03"


def test_alpaca_downloader_writes_exchange_calendar_cache(tmp_path: Path, monkeypatch):
    import data_tools.downloaders.alpaca_dl as alpaca_dl
    from data_tools.downloaders.alpaca_dl import AlpacaDownloader

    monkeypatch.setattr(
        alpaca_dl,
        "fetch_alpaca_exchange_calendar",
        lambda **kwargs: [
            {"date": "2024-07-03", "open": "09:30", "close": "13:00"},
            {"date": "2024-07-05", "open": "09:30", "close": "16:00"},
        ],
    )
    downloader = AlpacaDownloader(api_key="k", secret_key="s")
    downloader._cache_exchange_calendar(
        tmp_path, "AAPL", pd.Timestamp("2024-07-01", tz="UTC"),
        pd.Timestamp("2024-07-31", tz="UTC"),
    )
    calendar = load_cached_calendar(
        data_dir=tmp_path, source="alpaca", symbol="AAPL"
    )
    assert isinstance(calendar, ExchangeSessionCalendar)
    # The early close is preserved in the cached session list.
    assert calendar.sessions[pd.Timestamp("2024-07-03").date()][1].hour == 13


def test_ctrader_adapter_persists_symbol_schedule(tmp_path: Path, monkeypatch):
    from live_runner import ctrader_adapter

    monkeypatch.setattr(ctrader_adapter, "DATA_DIR", tmp_path)

    class _Interval:
        def __init__(self, start, end):
            self.startSecond = start
            self.endSecond = end

    class _Holiday:
        holidayDate = 19723  # 2024-01-01
        isRecurring = False

        def HasField(self, name):
            return name in {"startSecond", "endSecond"}

        startSecond = 0
        endSecond = 0

    detail = type(
        "FakeProtoOASymbol",
        (),
        {
            "schedule": [_Interval(0, 86400)],
            "holiday": [_Holiday()],
            "scheduleTimeZone": "Europe/Prague",
        },
    )()
    # Instance attributes, not class attrs, for proto-like access.
    detail.schedule = [_Interval(0, 86400)]
    detail.holiday = [_Holiday()]
    detail.scheduleTimeZone = "Europe/Prague"

    ctrader_adapter.CTraderLiveAdapter._persist_symbol_venue_calendar(
        object(), detail, "XAUUSD"
    )
    calendar = load_cached_calendar(
        data_dir=tmp_path, source="ctrader", symbol="XAUUSD"
    )
    assert isinstance(calendar, BrokerScheduleCalendar)
    assert calendar.schedule_timezone == "Europe/Prague"
    assert calendar.intervals == [(0, 86400)]
    assert len(calendar.holidays) == 1


# ─── Live closed-vs-stale (#133) ──────────────────────────────────────────────


def _staleness_check(symbol, timeframe, is_crypto, newest, now):
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    AlpacaLiveAdapter._raise_if_market_data_stale(
        symbol, timeframe, is_crypto, newest, now
    )


def test_live_equity_gap_while_market_closed_is_not_stale():
    # Saturday: NYSE closed; a Friday-close newest bar is legitimate.
    now = pd.Timestamp("2024-01-06T18:00:00Z")  # Saturday noon ET
    newest = pd.Timestamp("2024-01-05T20:00:00Z")  # Friday close
    _staleness_check("AAPL", "1h", False, newest, now)


def test_live_equity_gap_while_market_open_is_stale():
    # Wednesday 15:00 UTC (10:00/11:00 ET): session open; a bar from last week
    # is a dead feed, not a closed market.
    now = pd.Timestamp("2024-01-10T15:00:00Z")
    newest = pd.Timestamp("2024-01-03T20:00:00Z")
    with pytest.raises(ValueError, match="stale"):
        _staleness_check("AAPL", "1h", False, newest, now)


def test_live_equity_recent_bar_while_open_passes():
    now = pd.Timestamp("2024-01-10T15:00:00Z")
    newest = pd.Timestamp("2024-01-10T14:00:00Z")
    _staleness_check("AAPL", "1h", False, newest, now)


def test_live_crypto_never_closed_so_gap_is_stale():
    # Crypto trades 24/7: a two-day-old 1h bar is always a dead feed.
    now = pd.Timestamp("2024-01-10T15:00:00Z")
    newest = pd.Timestamp("2024-01-08T15:00:00Z")
    with pytest.raises(ValueError, match="stale"):
        _staleness_check("BTCUSD", "1h", True, newest, now)


def test_live_crypto_fresh_bar_passes():
    now = pd.Timestamp("2024-01-10T15:00:00Z")
    newest = pd.Timestamp("2024-01-10T10:00:00Z")
    _staleness_check("BTCUSD", "1h", True, newest, now)


# ─── Session-cache coverage bounds (#133 fix round) ──────────────────────────


def _session_cache_payload(
    *,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    sessions: list[dict] | None = None,
) -> dict:
    payload = {
        "kind": "exchange_sessions",
        "name": "alpaca-AAPL-exchange",
        "source": "alpaca",
        "schedule_timezone": "America/New_York",
        "sessions": sessions
        if sessions is not None
        else [
            {"date": "2024-07-03", "open": "09:30", "close": "16:00"},
            {"date": "2024-07-05", "open": "09:30", "close": "16:00"},
        ],
    }
    if coverage_start is not None:
        payload["coverage_start"] = coverage_start
    if coverage_end is not None:
        payload["coverage_end"] = coverage_end
    return payload


def test_exchange_session_cache_is_only_authoritative_inside_coverage():
    calendar = ExchangeSessionCalendar(
        _session_cache_payload(
            coverage_start="2024-07-01", coverage_end="2024-07-31"
        )
    )
    inside = pd.Timestamp("2024-07-10T15:00:00Z")
    before = pd.Timestamp("2024-01-10T15:00:00Z")
    after = pd.Timestamp("2025-01-10T15:00:00Z")
    assert calendar.covers_window(
        pd.Timestamp("2024-07-02T00:00:00Z"), pd.Timestamp("2024-07-30T00:00:00Z")
    )
    assert not calendar.covers_window(
        pd.Timestamp("2024-01-02T00:00:00Z"), pd.Timestamp("2024-01-30T00:00:00Z")
    )
    assert calendar.session_state(inside) in {"open", "closed"}
    # Outside the fetched range the calendar must not claim the venue is
    # closed — "unknown" sends callers to the flat-age fallback.
    assert calendar.session_state(before) == "unknown"
    assert calendar.session_state(after) == "unknown"


def test_exchange_session_cache_without_bounds_covers_nothing():
    """Pre-coverage-tracking payloads cannot prove which dates were fetched."""
    calendar = ExchangeSessionCalendar(_session_cache_payload())
    assert not calendar.covers_window(
        pd.Timestamp("2024-07-02T00:00:00Z"), pd.Timestamp("2024-07-30T00:00:00Z")
    )
    assert calendar.session_state(pd.Timestamp("2024-07-10T15:00:00Z")) == "unknown"


def test_partial_session_cache_falls_back_to_nyse_for_coverage(tmp_path: Path):
    """A cache fetched for 2026 must not vacuously 'cover' a 2024 segment."""
    _write_cache(
        tmp_path,
        "alpaca",
        "AAPL",
        _session_cache_payload(
            coverage_start="2026-01-01",
            coverage_end="2026-12-31",
            sessions=[{"date": "2026-01-05", "open": "09:30", "close": "16:00"}],
        ),
    )
    # Dense NYSE-weekday daily bars in Jan 2024.
    stamps = [
        ts.isoformat()
        for ts in pd.date_range("2024-01-01", "2024-01-31", freq="B", tz="UTC")
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "alpaca", "equity", "US", "AAPL", "1d.parquet"],
        stamps,
    )
    assert _coverage(
        symbol="AAPL", timeframe="1d",
        start="2024-01-01", end="2024-01-31", data_dir=tmp_path,
    )
    # One stray bar is still not coverage: the fallback is NYSE, not 24/7.
    _write_bars(
        tmp_path,
        ["ohlcv", "alpaca", "equity", "US", "MSFT", "1d.parquet"],
        ["2024-01-02T00:00:00+00:00"],
    )
    _write_cache(
        tmp_path,
        "alpaca",
        "MSFT",
        _session_cache_payload(
            coverage_start="2026-01-01",
            coverage_end="2026-12-31",
            sessions=[{"date": "2026-01-05", "open": "09:30", "close": "16:00"}],
        ),
    )
    assert not _coverage(
        symbol="MSFT", timeframe="1d",
        start="2024-01-01", end="2024-01-31", data_dir=tmp_path,
    )


def test_live_unknown_session_state_uses_flat_age_rule(tmp_path, monkeypatch):
    """A session cache that does not reach 'now' must not read as closed."""
    import live_runner.alpaca_adapter as alpaca_adapter

    _write_cache(
        tmp_path,
        "alpaca",
        "AAPL",
        _session_cache_payload(
            coverage_start="2024-01-01",
            coverage_end="2024-01-31",
            sessions=[{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
        ),
    )
    monkeypatch.setattr(alpaca_adapter, "DATA_DIR", tmp_path)
    now = pd.Timestamp("2024-06-10T15:00:00Z")  # far past the cache range
    # Two days old: inside the flat 5-day intraday fallback -> not stale.
    _staleness_check("AAPL", "1h", False, pd.Timestamp("2024-06-08T15:00:00Z"), now)
    # Eight days old: past the flat fallback -> stale (never 'closed').
    with pytest.raises(ValueError, match="stale"):
        _staleness_check("AAPL", "1h", False, pd.Timestamp("2024-06-01T15:00:00Z"), now)


def test_disjoint_session_cache_ranges_do_not_cover_the_gap():
    """Two non-contiguous fetches must not merge into one covered envelope.

    The cache is per symbol but downloads are per timeframe with their own
    resume start, so AAPL 1d for 2015 followed by AAPL 1h for 2024 leaves a
    real gap. min(start)/max(end) merging used to mark the gap covered.
    """
    first = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2015-01-02", "open": "09:30", "close": "16:00"}],
        coverage_start="2015-01-01",
        coverage_end="2015-01-31",
    )
    second = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2024-06-03", "open": "09:30", "close": "16:00"}],
        coverage_start="2024-06-01",
        coverage_end="2024-06-30",
        prior=first,
    )
    calendar = ExchangeSessionCalendar(second)
    # Each fetched range stays covered...
    assert calendar.covers_window(
        pd.Timestamp("2015-01-05T00:00:00Z"), pd.Timestamp("2015-01-28T00:00:00Z")
    )
    assert calendar.covers_window(
        pd.Timestamp("2024-06-10T00:00:00Z"), pd.Timestamp("2024-06-20T00:00:00Z")
    )
    # ...but the gap between them is not, and neither is a window spanning
    # both ranges (no single fetched range can prove it).
    assert not calendar.covers_window(
        pd.Timestamp("2020-01-02T00:00:00Z"), pd.Timestamp("2020-01-31T00:00:00Z")
    )
    assert not calendar.covers_window(
        pd.Timestamp("2015-01-05T00:00:00Z"), pd.Timestamp("2024-06-10T00:00:00Z")
    )
    # Live 'now' inside the gap is unknown, never closed.
    assert calendar.session_state(pd.Timestamp("2020-01-07T15:00:00Z")) == "unknown"


def test_touching_session_cache_ranges_merge():
    """Ranges that overlap or touch (end + 1 day == next start) are contiguous
    coverage and must merge, or incremental fetches would fragment."""
    first = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
        coverage_start="2024-01-01",
        coverage_end="2024-01-31",
    )
    second = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2024-02-01", "open": "09:30", "close": "16:00"}],
        coverage_start="2024-02-01",
        coverage_end="2024-02-29",
        prior=first,
    )
    calendar = ExchangeSessionCalendar(second)
    assert calendar.covers_window(
        pd.Timestamp("2024-01-15T00:00:00Z"), pd.Timestamp("2024-02-15T00:00:00Z")
    )


def test_merged_disjoint_cache_gap_fails_coverage(tmp_path: Path):
    """End-to-end: one stray bar inside a cache gap is not coverage."""
    first = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2015-01-02", "open": "09:30", "close": "16:00"}],
        coverage_start="2015-01-01",
        coverage_end="2015-01-31",
    )
    second = venue_calendar.exchange_sessions_payload(
        source="alpaca",
        symbol="AAPL",
        sessions=[{"date": "2024-06-03", "open": "09:30", "close": "16:00"}],
        coverage_start="2024-06-01",
        coverage_end="2024-06-30",
        prior=first,
    )
    save_cached_calendar(
        data_dir=tmp_path, source="alpaca", symbol="AAPL", payload=second
    )
    for timeframe in ("1h", "1d"):
        _write_bars(
            tmp_path,
            ["ohlcv", "alpaca", "equity", "US", "AAPL", f"{timeframe}.parquet"],
            ["2020-01-15T15:00:00+00:00" if timeframe == "1h" else "2020-01-15T00:00:00+00:00"],
        )
        assert not _coverage(
            symbol="AAPL", timeframe=timeframe,
            start="2020-01-02", end="2020-01-31", data_dir=tmp_path,
        ), f"{timeframe} must fail closed inside the unfetched gap"


def test_covering_calendar_with_zero_expected_bars_fails_closed(tmp_path: Path):
    """A calendar that claims the window but expects zero bars while the
    built-in NYSE model sees trading days is broken, not 'fully closed'."""
    # Exchange-session cache whose coverage reaches the window but whose
    # session list contains nothing inside it.
    _write_cache(
        tmp_path,
        "alpaca",
        "AAPL",
        _session_cache_payload(
            coverage_start="2024-01-01",
            coverage_end="2024-01-31",
            sessions=[{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
        ),
    )
    # Window fully inside coverage but containing no listed session:
    # 2024-01-22 is absent from the session list while coverage claims it.
    stamps = [
        f"2024-01-22T{hour:02d}:00:00+00:00" for hour in range(14, 21)
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "alpaca", "equity", "US", "AAPL", "1h.parquet"],
        stamps,
    )
    assert not _coverage(
        symbol="AAPL", timeframe="1h",
        start="2024-01-22", end="2024-01-22T23:00", data_dir=tmp_path,
    )


def test_covering_calendar_with_zero_expected_daily_bars_fails_closed(tmp_path: Path):
    """The daily branch of the same guard: a session cache whose coverage
    reaches a weekday window but whose session list omits it expects zero
    daily bars - believable only when the NYSE model agrees the window has
    no trading day. 2024-01-22 is a Monday NYSE trades, so coverage must
    fail closed; this is the ``1d`` twin of the intraday test above, and the
    branch the bundle-26 disjoint-range hole went through."""
    _write_cache(
        tmp_path,
        "alpaca",
        "AAPL",
        _session_cache_payload(
            coverage_start="2024-01-01",
            coverage_end="2024-01-31",
            sessions=[{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
        ),
    )
    _write_bars(
        tmp_path,
        ["ohlcv", "alpaca", "equity", "US", "AAPL", "1d.parquet"],
        ["2024-01-22T00:00:00+00:00"],
    )
    assert not _coverage(
        symbol="AAPL", timeframe="1d",
        start="2024-01-22", end="2024-01-23", data_dir=tmp_path,
    )


def test_incremental_session_cache_merges_coverage(tmp_path: Path, monkeypatch):
    """A second, narrower fetch must not shrink what the cache can prove."""
    import data_tools.downloaders.alpaca_dl as alpaca_dl
    from data_tools.downloaders.alpaca_dl import AlpacaDownloader

    responses = iter(
        [
            [{"date": "2024-01-02", "open": "09:30", "close": "16:00"}],
            [{"date": "2024-03-01", "open": "09:30", "close": "16:00"}],
        ]
    )
    monkeypatch.setattr(
        alpaca_dl,
        "fetch_alpaca_exchange_calendar",
        lambda **kwargs: next(responses),
    )
    downloader = AlpacaDownloader(api_key="k", secret_key="s")
    downloader._cache_exchange_calendar(
        tmp_path, "AAPL", pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-31", tz="UTC"),
    )
    downloader._cache_exchange_calendar(
        tmp_path, "AAPL", pd.Timestamp("2024-03-01", tz="UTC"),
        pd.Timestamp("2024-03-31", tz="UTC"),
    )
    calendar = load_cached_calendar(data_dir=tmp_path, source="alpaca", symbol="AAPL")
    assert isinstance(calendar, ExchangeSessionCalendar)
    assert set(calendar.sessions) == {
        pd.Timestamp("2024-01-02").date(),
        pd.Timestamp("2024-03-01").date(),
    }
    # Each fetched range stays covered; the unfetched gap between them does
    # not — disjoint ranges must never merge into one envelope.
    assert calendar.covers_window(
        pd.Timestamp("2024-01-15T00:00:00Z"), pd.Timestamp("2024-01-20T00:00:00Z")
    )
    assert calendar.covers_window(
        pd.Timestamp("2024-03-10T00:00:00Z"), pd.Timestamp("2024-03-20T00:00:00Z")
    )
    assert not calendar.covers_window(
        pd.Timestamp("2024-01-15T00:00:00Z"), pd.Timestamp("2024-03-15T00:00:00Z")
    )
    assert not calendar.covers_window(
        pd.Timestamp("2024-02-01T00:00:00Z"), pd.Timestamp("2024-02-29T00:00:00Z")
    )


def test_alpaca_calendar_uses_the_trading_api_host(monkeypatch):
    import data_tools.downloaders.alpaca_dl as alpaca_dl

    for var in ("ALPACA_TRADING_BASE_URL", "APCA_API_BASE_URL", "ALPACA_PAPER"):
        monkeypatch.delenv(var, raising=False)
    assert alpaca_dl._alpaca_trading_base_url() == "https://paper-api.alpaca.markets"

    monkeypatch.setenv("ALPACA_PAPER", "false")
    assert alpaca_dl._alpaca_trading_base_url() == "https://api.alpaca.markets"

    monkeypatch.setenv("APCA_API_BASE_URL", "https://custom.example.com/")
    assert alpaca_dl._alpaca_trading_base_url() == "https://custom.example.com"

    monkeypatch.setenv("ALPACA_TRADING_BASE_URL", "https://override.example.com")
    assert alpaca_dl._alpaca_trading_base_url() == "https://override.example.com"


def test_alpaca_session_fetch_hits_trading_host_not_data_host(monkeypatch):
    from data_tools.downloaders.alpaca_dl import fetch_alpaca_exchange_calendar

    monkeypatch.delenv("ALPACA_TRADING_BASE_URL", raising=False)
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    calls = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class _Session:
        def get(self, url, **kwargs):
            calls["url"] = url
            return _Response()

    fetch_alpaca_exchange_calendar(
        api_key="k", secret_key="s",
        start="2024-07-01", end="2024-07-31", session=_Session(),
    )
    assert calls["url"] == "https://paper-api.alpaca.markets/v2/calendar"


def test_alpaca_cache_is_never_used_for_crypto_symbols(tmp_path: Path):
    """A stray Alpaca cache must not give BTCUSD NYSE sessions."""
    _write_cache(
        tmp_path,
        "alpaca",
        "BTCUSD",
        _session_cache_payload(),
    )
    calendar = resolve_venue_calendar(
        symbol="BTCUSD", asset_class="crypto", source="alpaca", data_dir=tmp_path
    )
    assert isinstance(calendar, ContinuousCalendar)
    calendar = resolve_venue_calendar(
        symbol="BTCUSD", source="alpaca", data_dir=tmp_path
    )
    assert isinstance(calendar, ContinuousCalendar)


# ─── Broker-schedule daily bars + historical holidays (#133 fix round) ───────

# cTrader FX/CFD week: Sunday 22:00 -> Friday 22:00 UTC.
_FX_SCHEDULE = {"start_second": 22 * 3600, "end_second": 5 * 86400 + 22 * 3600}


def _broker_schedule_calendar(holidays: list[dict] | None = None) -> BrokerScheduleCalendar:
    return BrokerScheduleCalendar(
        {
            "kind": "broker_schedule",
            "name": "ctrader-XAUUSD",
            "source": "ctrader",
            "symbol": "XAUUSD",
            "schedule_timezone": "UTC",
            "schedule": [_FX_SCHEDULE],
            "holidays": holidays or [],
        }
    )


def test_broker_schedule_daily_bars_follow_the_week_rollover():
    """Sun22->Fri22 yields FIVE daily bars/week, not a Sunday sixth."""
    calendar = _broker_schedule_calendar()
    # Mon 2024-01-08 .. Sun 2024-01-14.
    dates = calendar.daily_bar_dates(
        pd.Timestamp("2024-01-08T00:00:00Z"), pd.Timestamp("2024-01-14T23:59:59Z")
    )
    assert dates == {
        pd.Timestamp(d).date()
        for d in ("2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12")
    }
    # Weekday stamps label their own bar.
    for day in range(8, 13):
        assert calendar.daily_bar_date(
            pd.Timestamp(f"2024-01-{day:02d}T21:00:00Z")
        ) == pd.Timestamp(f"2024-01-{day:02d}").date()
    # A stamp inside the closed weekend tail is surplus, not a daily bar.
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-14T10:00:00Z")) is None
    # The Sunday-evening reopen belongs to Monday's bar.
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-14T23:00:00Z")) == pd.Timestamp(
        "2024-01-15"
    ).date()


def test_ctrader_metals_daily_coverage_counts_five_bars_per_week(tmp_path: Path):
    """Real-shaped cTrader daily bars (Mon-Fri) must pass the 1d gate."""
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
            "schedule": [_FX_SCHEDULE],
            "holidays": [],
        },
    )
    stamps = [
        f"2024-01-{day:02d}T21:00:00+00:00" for day in range(8, 13)
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "ctrader", "metals", "XAUUSD", "1d.parquet"],
        stamps,
    )
    assert _coverage(
        symbol="XAUUSD", timeframe="1d",
        start="2024-01-08", end="2024-01-14", data_dir=tmp_path,
    )


def _holiday_epoch_days(day: str) -> int:
    import datetime as _dt

    return (_dt.date.fromisoformat(day) - _dt.date(1970, 1, 1)).days


def test_past_broker_holiday_is_tolerated_for_historical_segments(tmp_path: Path):
    """The cached holiday list only names the CURRENT year; a 2023 Christmas
    closure must not fail a Dec-2023 coverage segment."""
    holiday_2026_only = [
        {
            "date_days": _holiday_epoch_days("2026-12-25"),
            "is_recurring": False,
            "start_second": None,
            "end_second": None,
        }
    ]
    for symbol in ("XAUUSD", "XAGUSD"):
        _write_cache(
            tmp_path,
            "ctrader",
            symbol,
            {
                "kind": "broker_schedule",
                "name": f"ctrader-{symbol}",
                "source": "ctrader",
                "symbol": symbol,
                "schedule_timezone": "UTC",
                "schedule": [_FX_SCHEDULE],
                "holidays": holiday_2026_only,
            },
        )
    # Hourly bars Dec 18-22 and Dec 26-29 2023; closed all of Monday Dec 25
    # (Christmas — a real venue closure the cache does not list for 2023).
    trading_days = ["2023-12-18", "2023-12-19", "2023-12-20", "2023-12-21",
                    "2023-12-22", "2023-12-26", "2023-12-27", "2023-12-28",
                    "2023-12-29"]
    stamps = [
        ts.isoformat()
        for day in trading_days
        for ts in pd.date_range(f"{day}T00:00", f"{day}T23:00", freq="1h", tz="UTC")
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "dukascopy", "metals", "XAUUSD", "1h.parquet"],
        stamps,
    )
    assert _coverage(
        symbol="XAUUSD", timeframe="1h",
        start="2023-12-18", end="2023-12-29T23:00", data_dir=tmp_path,
    )
    # Control: the same window missing an EXTRA ordinary day (Dec 27 is not a
    # US-market closure) must still fail — tolerance forgives holidays only.
    stamps_missing_extra = [
        ts for ts in stamps if not ts.startswith("2023-12-27")
    ]
    _write_bars(
        tmp_path,
        ["ohlcv", "dukascopy", "metals", "XAGUSD", "1h.parquet"],
        stamps_missing_extra,
    )
    assert not _coverage(
        symbol="XAGUSD", timeframe="1h",
        start="2023-12-18", end="2023-12-29T23:00", data_dir=tmp_path,
    )


def test_broker_schedule_stamp_at_rollover_opens_the_new_daily_bar():
    """Daily bars are stamped at their OPEN (repo OHLCV convention): a stamp
    exactly on the 22:00Z rollover belongs to the window starting there, not
    the one that just closed."""
    calendar = _broker_schedule_calendar()
    # Sun 2024-01-07 .. Thu 2024-01-11 at 22:00Z: each stamp opens the next
    # daily window, so the labels are Mon Jan 8 .. Fri Jan 12.
    dates = [
        calendar.daily_bar_date(pd.Timestamp(f"2024-01-{day:02d}T22:00:00Z"))
        for day in range(7, 12)
    ]
    assert dates == [
        pd.Timestamp(f"2024-01-{day:02d}").date() for day in range(8, 13)
    ]
    # A stamp exactly at the Friday close has no window opening there: the
    # venue is shut until Sunday, so it is surplus.
    assert (
        calendar.daily_bar_date(pd.Timestamp("2024-01-12T22:00:00Z")) is None
    )
    # Mid-window and 00:00Z stamps are unaffected.
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-09T12:00:00Z")) == (
        pd.Timestamp("2024-01-09").date()
    )
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-09T00:00:00Z")) == (
        pd.Timestamp("2024-01-09").date()
    )


def test_fx_daily_bar_stamp_at_rollover_opens_the_new_daily_bar():
    """Same off-by-one on the FX week calendar: 22:00Z == 17:00 ET in
    January, exactly on each NY-close boundary."""
    calendar = FxWeekCalendar()
    dates = [
        calendar.daily_bar_date(pd.Timestamp(f"2024-01-{day:02d}T22:00:00Z"))
        for day in range(7, 12)
    ]
    assert dates == [
        pd.Timestamp(f"2024-01-{day:02d}").date() for day in range(8, 13)
    ]
    # Friday 17:00 ET close: no bar opens there (market shut until Sunday).
    assert calendar.daily_bar_date(pd.Timestamp("2024-01-12T22:00:00Z")) is None


def test_fx_daily_dates_exclude_the_still_open_bar():
    """A daily bar whose window has not closed at ``end`` is not expected —
    matching BrokerScheduleCalendar's 'hi <= end' rule."""
    calendar = FxWeekCalendar()
    # Ends Sunday 2024-01-14 23:59Z: Monday Jan 15's bar (window opens
    # Sun 22:00Z, closes Mon 22:00Z) is still open and must not count.
    dates = calendar.daily_bar_dates(
        pd.Timestamp("2024-01-08T00:00:00Z"), pd.Timestamp("2024-01-14T23:59:59Z")
    )
    assert pd.Timestamp("2024-01-15").date() not in dates
    assert len(dates) == 5
    # Once the window closes inside the segment it counts again.
    dates_closed = calendar.daily_bar_dates(
        pd.Timestamp("2024-01-08T00:00:00Z"), pd.Timestamp("2024-01-15T23:00:00Z")
    )
    assert pd.Timestamp("2024-01-15").date() in dates_closed

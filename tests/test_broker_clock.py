"""Broker server clocks (shared.broker_clock) around every DST switch 2018-2026."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from shared.broker_clock import (
    BROKER_CLOCKS,
    DUKASCOPY_MT5_FOREX,
    DUKASCOPY_MT5_INDEX,
    NY_CLOSE,
    BrokerClock,
    ClockSegment,
    resolve_broker_clock,
)

YEARS = range(2018, 2027)


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _last_sunday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _us_spring(year: int) -> date:
    return _nth_sunday(year, 3, 2)


def _us_fall(year: int) -> date:
    return _nth_sunday(year, 11, 1)


def _eu_spring(year: int) -> date:
    return _last_sunday(year, 3)


def _eu_fall(year: int) -> date:
    return _last_sunday(year, 10)


def _offset(clock: BrokerClock, utc: str | pd.Timestamp) -> float:
    ts = pd.Timestamp(utc)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return float(clock.offset_hours([ts])[0])


def _at_1330(day: date) -> pd.Timestamp:
    return pd.Timestamp(day.isoformat() + " 13:30")


# ---------------------------------------------------------------------------
# ny_close: America/New_York + 7 h, US switch dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("year", YEARS)
def test_ny_close_switches_on_us_dates_exactly_at_the_us_instant(year):
    spring, fall = _us_spring(year), _us_fall(year)
    # Spring forward at 02:00 EST = 07:00 UTC, fall back at 02:00 EDT = 06:00 UTC.
    assert _offset(NY_CLOSE, f"{spring} 06:59") == 2
    assert _offset(NY_CLOSE, f"{spring} 07:00") == 3
    assert _offset(NY_CLOSE, f"{fall} 05:59") == 3
    assert _offset(NY_CLOSE, f"{fall} 06:00") == 2


@pytest.mark.parametrize("year", YEARS)
def test_ny_close_ignores_eu_switch_dates(year):
    # Between the US and the EU spring switch New York close is already UTC+3,
    # between the EU and the US autumn switch it is still UTC+3.
    assert _offset(NY_CLOSE, _at_1330(_us_spring(year) + timedelta(days=1))) == 3
    assert _offset(NY_CLOSE, _at_1330(_eu_spring(year) - timedelta(days=2))) == 3
    assert _offset(NY_CLOSE, _at_1330(_eu_fall(year) + timedelta(days=1))) == 3
    assert _offset(NY_CLOSE, _at_1330(_us_fall(year) - timedelta(days=2))) == 3
    assert _offset(NY_CLOSE, _at_1330(_us_fall(year) + timedelta(days=1))) == 2
    assert _offset(NY_CLOSE, _at_1330(_us_spring(year) - timedelta(days=2))) == 2


def test_ny_close_week_opens_at_monday_midnight_server_time():
    # New York 17:00 on Sunday = 00:00 Monday server, winter and summer.
    winter = NY_CLOSE.to_server_time(pd.DatetimeIndex(["2026-01-11 22:00"], tz="UTC"))
    summer = NY_CLOSE.to_server_time(pd.DatetimeIndex(["2026-07-12 21:00"], tz="UTC"))
    assert winter[0] == pd.Timestamp("2026-01-12 00:00")
    assert summer[0] == pd.Timestamp("2026-07-13 00:00")


# ---------------------------------------------------------------------------
# Dukascopy-demo MT5 tester history clock
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("year", range(2018, 2024))
def test_dukascopy_forex_legacy_history_follows_athens_on_eu_dates(year):
    clock = DUKASCOPY_MT5_FOREX
    # US-only DST windows stay on Athens' offset (UTC+2) ...
    assert _offset(clock, _at_1330(_us_spring(year) + timedelta(days=1))) == 2
    assert _offset(clock, _at_1330(_eu_spring(year) - timedelta(days=2))) == 2
    assert _offset(clock, _at_1330(_eu_fall(year) + timedelta(days=1))) == 2
    # ... and the EU switch itself moves the clock, at 01:00 UTC.
    assert _offset(clock, f"{_eu_spring(year)} 00:59") == 2
    assert _offset(clock, f"{_eu_spring(year)} 01:00") == 3
    assert _offset(clock, f"{_eu_fall(year)} 00:59") == 3
    assert _offset(clock, f"{_eu_fall(year)} 01:00") == 2
    assert _offset(clock, _at_1330(date(year, 7, 3))) == 3


@pytest.mark.parametrize("year", range(2018, 2024))
def test_dukascopy_index_legacy_history_is_fixed_utc_plus_2(year):
    clock = DUKASCOPY_MT5_INDEX
    for day in (
        date(year, 1, 10),
        _us_spring(year) + timedelta(days=1),
        _eu_spring(year) + timedelta(days=1),
        date(year, 7, 3),
        _eu_fall(year) + timedelta(days=1),
        _us_fall(year) + timedelta(days=1),
    ):
        assert _offset(clock, _at_1330(day)) == 2, day


@pytest.mark.parametrize("clock", [DUKASCOPY_MT5_FOREX, DUKASCOPY_MT5_INDEX], ids=lambda c: c.name)
def test_dukascopy_transition_and_current_history(clock):
    # Transition: fixed UTC+2 through the week of 2024-04-01 (US and EU already on DST).
    assert _offset(clock, "2024-03-11 13:30") == 2
    assert _offset(clock, "2024-04-05 13:30") == 2
    # Current history: New York close from the weekend of 2024-04-06/07.
    assert _offset(clock, "2024-04-05 23:59") == 2
    assert _offset(clock, "2024-04-06 00:00") == 3
    assert _offset(clock, "2024-04-08 13:30") == 3
    assert _offset(clock, "2024-10-28 13:30") == 3  # EU already back on winter time
    # Autumn 2024 switched one week after the US rule.
    assert _offset(clock, "2024-11-03 05:59") == 3
    assert _offset(clock, "2024-11-04 13:30") == 3
    assert _offset(clock, "2024-11-08 21:30") == 3
    assert _offset(clock, "2024-11-10 06:59") == 3
    assert _offset(clock, "2024-11-10 07:00") == 2
    assert _offset(clock, "2024-11-11 13:30") == 2


@pytest.mark.parametrize("year", [2025, 2026])
@pytest.mark.parametrize("clock", [DUKASCOPY_MT5_FOREX, DUKASCOPY_MT5_INDEX], ids=lambda c: c.name)
def test_dukascopy_current_history_matches_ny_close_on_every_later_switch(clock, year):
    switches = [_us_spring(year), _eu_spring(year)]
    if year < 2026:
        switches += [_eu_fall(year), _us_fall(year)]
    instants = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="1h", tz="UTC")
    np.testing.assert_array_equal(clock.offset_hours(instants), NY_CLOSE.offset_hours(instants))
    for switch in switches:
        window = pd.date_range(f"{switch} 00:00", periods=48, freq="15min", tz="UTC")
        np.testing.assert_array_equal(clock.offset_hours(window), NY_CLOSE.offset_hours(window))


# Days the IntradayVolatilityBreakout tester logs decide unambiguously (the logged 16:30
# open equals the UTC M1 open at exactly one of 16:30-2h / 16:30-3h). One day per
# symbol, season and observed offset inside the windows where the candidate rules
# disagree (US-only DST weeks, the 2024 transition weeks).
OBSERVED_TESTER_OFFSETS = [
    ("2018-03-20", "eurusd", 2),
    ("2019-10-28", "eurusd", 2),
    ("2020-03-11", "eurusd", 2),
    ("2020-10-28", "eurusd", 2),
    ("2022-03-15", "eurusd", 2),
    ("2023-03-22", "eurusd", 2),
    ("2024-03-11", "eurusd", 2),
    ("2024-04-01", "eurusd", 2),
    ("2024-04-09", "eurusd", 3),
    ("2024-10-28", "eurusd", 3),
    ("2024-11-04", "eurusd", 3),
    ("2024-11-11", "eurusd", 2),
    ("2025-03-10", "eurusd", 3),
    ("2025-10-27", "eurusd", 3),
    ("2025-11-03", "eurusd", 2),
    ("2026-03-09", "eurusd", 3),
    ("2018-03-12", "nas100", 2),
    ("2018-07-09", "nas100", 2),
    ("2018-11-01", "nas100", 2),
    ("2019-03-11", "nas100", 2),
    ("2019-10-28", "nas100", 2),
    ("2020-03-10", "nas100", 2),
    ("2020-10-29", "nas100", 2),
    ("2021-03-15", "nas100", 2),
    ("2021-11-01", "nas100", 2),
    ("2022-03-14", "nas100", 2),
    ("2022-10-31", "nas100", 2),
    ("2023-03-13", "nas100", 2),
    ("2024-03-12", "nas100", 2),
    ("2024-04-01", "nas100", 2),
    ("2024-04-08", "nas100", 3),
    ("2024-10-28", "nas100", 3),
    ("2024-11-04", "nas100", 3),
    ("2024-11-11", "nas100", 2),
    ("2025-03-10", "nas100", 3),
    ("2025-10-27", "nas100", 3),
    ("2026-03-10", "nas100", 3),
    ("2018-03-12", "sp500", 2),
    ("2018-10-29", "sp500", 2),
    ("2019-03-11", "sp500", 2),
    ("2019-10-28", "sp500", 2),
    ("2020-03-17", "sp500", 2),
    ("2020-10-27", "sp500", 2),
    ("2021-03-16", "sp500", 2),
    ("2021-11-01", "sp500", 2),
    ("2022-03-15", "sp500", 2),
    ("2022-10-31", "sp500", 2),
    ("2023-03-13", "sp500", 2),
    ("2023-11-01", "sp500", 2),
    ("2024-03-12", "sp500", 2),
    ("2024-04-08", "sp500", 3),
    ("2024-10-30", "sp500", 3),
    ("2024-11-04", "sp500", 3),
    ("2024-11-13", "sp500", 2),
    ("2025-03-10", "sp500", 3),
    ("2025-10-27", "sp500", 3),
    ("2026-03-09", "sp500", 3),
]


@pytest.mark.parametrize(("day", "symbol", "observed"), OBSERVED_TESTER_OFFSETS)
def test_dukascopy_clock_reproduces_offsets_observed_in_the_tester_logs(day, symbol, observed):
    clock = DUKASCOPY_MT5_FOREX if symbol == "eurusd" else DUKASCOPY_MT5_INDEX
    utc = pd.Timestamp(f"{day} 16:30", tz="UTC") - pd.Timedelta(hours=observed)
    assert _offset(clock, utc) == observed
    assert clock.to_server_time(pd.DatetimeIndex([utc]))[0] == pd.Timestamp(f"{day} 16:30")


def test_dukascopy_weekend_d1_bars_only_after_the_legacy_history():
    clock = DUKASCOPY_MT5_FOREX
    # Legacy: the Sunday 23:00 Athens hour of a US-only DST week has no D1 bar.
    assert not clock.weekend_d1_bars([pd.Timestamp("2023-10-29 21:30", tz="UTC")])[0]
    assert not clock.weekend_d1_bars([pd.Timestamp("2019-03-10 21:30", tz="UTC")])[0]
    # Transition / current: the broker history does carry those weekend bars.
    assert clock.weekend_d1_bars([pd.Timestamp("2024-03-10 21:30", tz="UTC")])[0]
    assert clock.weekend_d1_bars([pd.Timestamp("2024-11-08 21:30", tz="UTC")])[0]
    # ... and they land on Sunday / Saturday server dates respectively.
    sunday, saturday = clock.to_server_time(
        pd.DatetimeIndex(["2024-03-10 21:30", "2024-11-08 21:30"], tz="UTC")
    )
    assert sunday == pd.Timestamp("2024-03-10 23:30")
    assert saturday == pd.Timestamp("2024-11-09 00:30")


# ---------------------------------------------------------------------------
# Conversion contract and validation
# ---------------------------------------------------------------------------
def test_to_server_time_returns_naive_wall_time_and_keeps_nat():
    out = DUKASCOPY_MT5_INDEX.to_server_time(pd.Series(["2025-07-01T13:30:00Z", None]))
    assert out.tz is None
    assert out[0] == pd.Timestamp("2025-07-01 16:30")
    assert pd.isna(out[1])


def test_naive_input_is_read_as_utc():
    naive = NY_CLOSE.to_server_time(pd.DatetimeIndex(["2025-07-01 13:30"]))
    aware = NY_CLOSE.to_server_time(pd.DatetimeIndex(["2025-07-01 13:30"], tz="UTC"))
    assert naive.equals(aware)


def test_offset_accepts_any_timestamp_resolution():
    for unit in ("s", "ms", "us", "ns"):
        index = pd.DatetimeIndex(["2024-04-01 13:30", "2024-04-08 13:30"], tz="UTC").as_unit(unit)
        assert DUKASCOPY_MT5_INDEX.offset_hours(index).tolist() == [2.0, 3.0], unit


def test_resolve_broker_clock():
    assert resolve_broker_clock("") is None
    assert resolve_broker_clock(None) is None
    assert resolve_broker_clock(" Dukascopy_MT5_Forex ") is DUKASCOPY_MT5_FOREX
    assert set(BROKER_CLOCKS) == {"ny_close", "dukascopy_mt5_forex", "dukascopy_mt5_index"}
    with pytest.raises(ValueError, match="Unknown broker_clock"):
        resolve_broker_clock("Europe/Athens")


def test_clock_segments_are_validated():
    with pytest.raises(ValueError, match="no segments"):
        BrokerClock("empty", ())
    with pytest.raises(ValueError, match="open-ended"):
        BrokerClock("bad", (ClockSegment(pd.Timestamp("2020-01-01", tz="UTC"), "UTC", 2, True),))
    with pytest.raises(ValueError, match="strictly increasing"):
        BrokerClock(
            "bad",
            (
                ClockSegment(None, "UTC", 2, True),
                ClockSegment(pd.Timestamp("2021-01-01", tz="UTC"), "UTC", 3, True),
                ClockSegment(pd.Timestamp("2020-01-01", tz="UTC"), "UTC", 2, True),
            ),
        )

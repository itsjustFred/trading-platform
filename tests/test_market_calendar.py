"""The US equity calendar: scheduled sessions, and the closures between them.

`shared/market_calendar.py` answers two questions that a constant cannot: whether
a hole between two bars spans a scheduled session, and how many sessions a
newest bar trails the clock by. Both are table-driven here against real 2026
NYSE/NASDAQ closure dates, so a rule the calendar gets wrong shifts a real
closure week rather than moving a number in a comment.
"""

from __future__ import annotations

import datetime as dt

import pytest

from shared import market_calendar

UTC = dt.timezone.utc


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


#: Every scheduled US equity closure in 2026, as published by the exchanges.
NYSE_2026_CLOSURES = {
    "2026-01-01": "New Year's Day (Thursday)",
    "2026-01-19": "Martin Luther King Jr Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day, observed Friday 3 July (4 July is a Saturday)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
}


def test_calendar_matches_the_published_2026_closure_dates():
    found = {
        day.isoformat()
        for day in market_calendar.holidays(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    }

    assert found == set(NYSE_2026_CLOSURES)


@pytest.mark.parametrize(
    ("observed", "why"),
    [
        ("2026-07-03", "4 July 2026 is a Saturday, observed the preceding Friday"),
        ("2026-12-25", "25 December 2026 is a Friday, observed on the day"),
        ("2021-12-24", "25 December 2021 was a Saturday, observed the preceding Friday"),
        ("2022-12-26", "25 December 2022 was a Sunday, observed the following Monday"),
    ],
)
def test_weekend_holidays_move_off_the_weekend(observed, why):
    """`pandas`' federal calendar only shifts Sunday holidays, so a Saturday
    holiday would be scheduled as a trading day -- and 4 July 2026 is one."""
    assert not market_calendar.is_session(dt.date.fromisoformat(observed)), why


def test_a_saturday_new_years_day_does_not_close_the_preceding_friday():
    """New Year's Day is the exception to the Saturday rule: the exchanges trade
    on 31 December, because that Friday is the last session of the year. Treating
    it as a closure would invent a missing session in every such year."""
    assert market_calendar.is_session(dt.date(2021, 12, 31))
    assert market_calendar.is_session(dt.date(2022, 1, 3)), (
        "Monday 3 January 2022 was a trading day"
    )
    assert not market_calendar.is_session(dt.date(2022, 1, 1))


def test_a_sunday_new_years_day_is_observed_on_monday():
    assert not market_calendar.is_session(dt.date(2023, 1, 2))
    assert market_calendar.is_session(dt.date(2023, 1, 3))


def test_juneteenth_is_a_closure_only_from_2022():
    """The exchanges began observing it in 2022. A 2020 or 2021 backtest must
    not lose a session to a holiday that did not exist yet."""
    assert market_calendar.is_session(dt.date(2021, 6, 18))
    assert market_calendar.is_session(dt.date(2021, 6, 21))
    assert not market_calendar.is_session(dt.date(2022, 6, 20)), (
        "19 June 2022 was a Sunday, so the closure is the Monday"
    )


def test_early_close_days_are_sessions_not_closures():
    """The day after Thanksgiving 2026 is a Friday that closes at 13:00 Eastern.
    It still prints bars, so it must not create a closure gap; modelling it as a
    closure would under-size every closure around it."""
    day = dt.date(2026, 11, 27)

    assert market_calendar.is_session(day)
    assert not market_calendar.holidays(day, day)


def test_longest_modelled_closure_is_a_three_closed_day_week():
    """Derived, not declared: the longest gap the calendar itself produces is
    the one where three consecutive weekdays are shut (a Monday or Friday
    holiday plus the weekend), 89.5 hours on the equity grid."""
    limit = market_calendar.longest_scheduled_closure_days()

    # 3.729 days is 89.5 hours: Friday's close to Tuesday's open across a
    # Monday holiday.
    assert limit == pytest.approx(3.729, abs=0.01)
    assert limit * 24.0 == pytest.approx(89.5, abs=0.1)


def test_session_bounds_follow_daylight_saving():
    """A session is 09:30-16:00 *Eastern*, so its UTC bounds move with DST:
    14:30-21:00 in winter and 13:30-20:00 in summer."""
    assert market_calendar.session_open_utc(dt.date(2026, 1, 15)) == _at(2026, 1, 15, 14, 30)
    assert market_calendar.session_close_utc(dt.date(2026, 1, 15)) == _at(2026, 1, 15, 21, 0)
    assert market_calendar.session_open_utc(dt.date(2026, 7, 15)) == _at(2026, 7, 15, 13, 30)
    assert market_calendar.session_close_utc(dt.date(2026, 7, 15)) == _at(2026, 7, 15, 20, 0)


def test_sessions_in_range_skips_weekends_and_holidays():
    days = market_calendar.sessions_in_range(dt.date(2026, 11, 25), dt.date(2026, 11, 30))

    assert [day.isoformat() for day in days] == [
        "2026-11-25",
        "2026-11-27",
        "2026-11-30",
    ]


def test_sessions_in_range_is_empty_when_reversed():
    assert market_calendar.sessions_in_range(dt.date(2026, 1, 2), dt.date(2026, 1, 1)) == ()


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Inside a session: that session.
        (_at(2026, 9, 8, 15), "2026-09-08"),
        # Before the open: the previous session, because today's bars do not exist yet.
        (_at(2026, 9, 10, 12), "2026-09-09"),
        # Saturday: Friday.
        (_at(2026, 9, 12, 12), "2026-09-11"),
        # Labor Day 2026 (Monday): the Friday before.
        (_at(2026, 9, 7, 15), "2026-09-04"),
    ],
)
def test_last_session_at_or_before(moment, expected):
    assert market_calendar.last_session_at_or_before(moment).isoformat() == expected


def test_last_session_at_or_before_refuses_a_naive_moment():
    naive = dt.datetime(2026, 9, 8, 15)
    with pytest.raises(ValueError, match="timezone-aware"):
        market_calendar.last_session_at_or_before(naive)


# ── Sessions behind: closures are not staleness, outages are ─────────────────


def test_a_closure_week_leaves_the_bar_within_tolerance():
    """Labor Day 2026 was a Monday. On Tuesday morning the newest daily bar is
    Friday's, one session behind because Tuesday's bar publishes after the close
    -- 91 hours old, and not stale."""
    behind, bar_session, expected = market_calendar.bar_staleness_sessions(
        _at(2026, 9, 4, 20), _at(2026, 9, 8, 15), nominal_days=1.0
    )

    assert (behind, bar_session, expected) == (1, dt.date(2026, 9, 4), dt.date(2026, 9, 8))
    assert behind <= market_calendar.AD_HOC_CLOSURE_TOLERANCE_SESSIONS


def test_a_dead_feed_is_measured_in_sessions():
    behind, bar_session, expected = market_calendar.bar_staleness_sessions(
        _at(2026, 9, 8, 19, 55), _at(2026, 9, 11, 16), nominal_days=5.0 / 1440.0
    )

    assert behind == 3
    assert bar_session == dt.date(2026, 9, 8)
    assert expected == dt.date(2026, 9, 11)


def test_the_daily_publication_lag_is_one_session_and_within_tolerance():
    behind, _, _ = market_calendar.bar_staleness_sessions(
        _at(2026, 9, 9, 4), _at(2026, 9, 10, 20, 30), nominal_days=1.0
    )

    assert behind == 1
    assert behind <= market_calendar.AD_HOC_CLOSURE_TOLERANCE_SESSIONS


def test_a_bar_stamped_exactly_at_the_open_covers_its_session():
    """A bar's timestamp is the *start* of its interval, so the bar stamped at
    the open contains the open. The half-open interval is `[bar, bar + spacing)`,
    and an off-by-one that made it `(bar, bar + spacing]` would drop every bar
    that starts exactly on the session open - which is every 1-minute bar of the
    day."""
    assert market_calendar.bar_covering_session(
        _at(2026, 9, 10, 13, 30), dt.date(2026, 9, 10), nominal_days=5.0 / 1440.0
    )


def test_a_daily_bar_stamped_at_midnight_covers_its_session():
    assert market_calendar.bar_covering_session(
        _at(2026, 9, 10, 0), dt.date(2026, 9, 10), nominal_days=1.0
    )


def test_a_weekly_bar_stamped_on_monday_covers_the_week():
    monday = _at(2026, 9, 7, 0)

    for day in (7, 8, 9, 10, 11):
        assert market_calendar.bar_covering_session(
            monday, dt.date(2026, 9, day), nominal_days=7.0
        ), day


def test_bar_staleness_refuses_a_naive_bar():
    naive_bar = dt.datetime(2026, 9, 8, 15)
    now = _at(2026, 9, 9, 15)
    with pytest.raises(ValueError, match="timezone-aware"):
        market_calendar.bar_staleness_sessions(naive_bar, now, nominal_days=1.0)


# ── The shape half: did the hole span a scheduled session? ──────────────────


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        ("2026-07-02", "2026-07-06", "Independence Day observed Friday 3 July"),
        ("2026-09-04", "2026-09-08", "Labor Day Monday"),
        ("2026-06-18", "2026-06-22", "Juneteenth on a Friday"),
    ],
)
def test_a_closure_shaped_hole_is_explained(start, end, why):
    assert market_calendar.closure_explains_hole(
        dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    ), why


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        ("2026-09-08", "2026-09-11", "three live sessions strictly inside"),
        ("2026-09-11", "2026-09-15", "a weekend plus the live Monday inside"),
        ("2026-07-02", "2026-07-07", "a holiday, the weekend, and the live Monday inside"),
        ("2026-09-05", "2026-09-08", "starts on a Saturday, so no session at the edge"),
        ("2026-09-08", "2026-09-08", "empty hole"),
        ("2026-09-08", "2026-09-01", "reversed"),
    ],
)
def test_an_outage_shaped_hole_is_not_explained(start, end, why):
    assert not market_calendar.closure_explains_hole(
        dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    ), why


def test_a_plain_weekend_is_excused_by_the_calendar():
    """Friday to Monday spans no scheduled session of its own, so the shape is a
    closure -- the width policy that decides how wide such a hole may be lives in
    `data_tools.auto_download`, not here."""
    assert market_calendar.closure_explains_hole(dt.date(2026, 9, 11), dt.date(2026, 9, 14))


def test_session_span_seconds_is_the_modelled_regular_session():
    assert market_calendar.session_span_seconds(dt.date(2026, 9, 8)) == pytest.approx(
        6.5 * 3600.0
    )


# ---------------------------------------------------------------------------
# Special (unscheduled) full-day closures
# ---------------------------------------------------------------------------

SPECIAL_CLOSURE_DATES = [
    ("2001-09-11", "September 11 attacks, Tuesday"),
    ("2001-09-12", "September 11 attacks, Wednesday"),
    ("2001-09-13", "September 11 attacks, Thursday"),
    ("2001-09-14", "September 11 attacks, Friday"),
    ("2004-06-11", "National Day of Mourning, Reagan"),
    ("2007-01-02", "National Day of Mourning, Ford"),
    ("2012-10-29", "Hurricane Sandy, Monday"),
    ("2012-10-30", "Hurricane Sandy, Tuesday"),
    ("2018-12-05", "National Day of Mourning, Bush"),
    ("2025-01-09", "National Day of Mourning, Carter"),
]


@pytest.mark.parametrize(("day", "why"), SPECIAL_CLOSURE_DATES)
def test_special_closures_are_not_sessions(day, why):
    value = dt.date.fromisoformat(day)
    assert value.weekday() < 5, "a special closure is a weekday by construction"
    assert not market_calendar.is_session(value), why
    assert value in market_calendar.holidays(value, value), why


def test_special_closure_list_is_exactly_the_documented_ten_days():
    assert market_calendar.SPECIAL_CLOSURES == {
        dt.date.fromisoformat(day) for day, _ in SPECIAL_CLOSURE_DATES
    }


@pytest.mark.parametrize(
    ("last_bar_day", "first_bar_day"),
    [
        ("2001-09-10", "2001-09-17"),  # four closed sessions, then reopening Monday
        ("2012-10-26", "2012-10-31"),  # Friday -> Wednesday across Sandy
        ("2025-01-08", "2025-01-10"),  # Wednesday -> Friday across Carter day
        ("2018-12-04", "2018-12-06"),
        ("2006-12-29", "2007-01-03"),  # New Year's Monday, then the Ford day
    ],
)
def test_a_hole_over_a_special_closure_is_explained(last_bar_day, first_bar_day):
    start = dt.date.fromisoformat(last_bar_day)
    end = dt.date.fromisoformat(first_bar_day)
    assert market_calendar.closure_explains_hole(start, end)


def test_the_day_after_a_special_closure_is_a_session_again():
    assert market_calendar.is_session(dt.date(2001, 9, 17))
    assert market_calendar.is_session(dt.date(2025, 1, 10))
    assert market_calendar.is_session(dt.date(2007, 1, 3))


def _reference_nyse_holidays(year: int) -> frozenset[str]:
    """Frozen copy of the hand-written table `data_tools.auto_download` used to carry.

    Kept verbatim as the oracle for the delegation to `shared.market_calendar`.
    """

    def observed(month: int, day: int) -> dt.date:
        day_date = dt.date(year, month, day)
        if day_date.weekday() == 5 and (month, day) != (1, 1):
            return day_date - dt.timedelta(days=1)
        if day_date.weekday() == 6:
            return day_date + dt.timedelta(days=1)
        return day_date

    def nth_weekday(month: int, weekday: int, occurrence: int) -> dt.date:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (occurrence - 1))

    def last_weekday(month: int, weekday: int) -> dt.date:
        probe = (
            dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
            if month == 12
            else dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        )
        return probe - dt.timedelta(days=(probe.weekday() - weekday) % 7)

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    wc = (32 + 2 * e + 2 * i - h - k) % 7
    ma = (a + 11 * h + 22 * wc) // 451
    easter = (
        (h + wc - 7 * ma + 114) // 31,
        ((h + wc - 7 * ma + 114) % 31) + 1,
    )
    found = {
        observed(1, 1),
        nth_weekday(1, 0, 3),
        nth_weekday(2, 0, 3),
        last_weekday(5, 0),
        observed(7, 4),
        nth_weekday(9, 0, 1),
        nth_weekday(11, 3, 4),
        observed(12, 25),
        dt.date(year, *easter) - dt.timedelta(days=2),
    }
    if year >= 2022:
        found.add(observed(6, 19))
    return frozenset(day.isoformat() for day in found)


@pytest.mark.parametrize("year", range(2000, 2031))
def test_auto_download_holidays_equal_the_old_table_plus_special_closures(year):
    from data_tools.auto_download import _nyse_holidays

    special = {
        day.isoformat()
        for day in market_calendar.SPECIAL_CLOSURES
        if day.year == year
    }
    assert _nyse_holidays(year) == _reference_nyse_holidays(year) | special

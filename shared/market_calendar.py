"""US equity exchange calendar: scheduled sessions, and the closures between them.

Two gates in this repo need to know whether the US stock market was open, and
neither could answer that:

* ``live_runner.alpaca_adapter.get_market_data`` compared the newest bar's age
  against a flat 72-hour (intraday) / 7-day (daily) budget. One shut weekday
  plus the weekend already leaves the newest intraday bar 89.5 hours old, so the
  gate raised for *every* symbol from the Thursday close until Tuesday, and the
  ``ValueError`` was swallowed as a rejected signal.
* ``data_tools.auto_download`` sized its coverage-hole threshold with a flat
  ``LONG_WEEKEND_GAP_DAYS`` floor that overrode the base threshold for every
  timeframe, so 1-minute data with a 3.6-day hole passed the gate.

Both are answered here, from the exchange calendar rather than from a constant.
This module answers the two questions that need a calendar: *shape* (did the
hole span any scheduled session?) and *sessions behind* (how many sessions past
the newest bar should have printed?). Width policy and the failing/warning
threshold stay with the callers, which know the timeframe and therefore the bar
width.

Scope and honesty about the model
---------------------------------
The model is the **NYSE/NASDAQ regular session**: Monday-Friday, 09:30-16:00
America/New_York, minus the fixed-date and nth-weekday holidays the exchanges
observe. It is deliberately *not* claimed to be complete:

* **Early-close days are sessions, not closures.** A 13:00 close on the day
  after Thanksgiving, or an early close before Independence Day, still prints
  bars, so it must not create a closure gap. The consequence is that
  :func:`longest_scheduled_closure_days` places a session's close at 16:00 even
  when the real one was 13:00, so a measured closure is up to three hours
  shorter than the truth. That error is in the safe direction: a shorter
  closure limit excuses fewer holes, never more.
* **Unscheduled closures are absent.** No published calendar contains a
  National Day of Mourning, a hurricane shutdown or a funeral closure, and no
  API enumerates them in advance. The tolerance a staleness gate must add is
  derived from, and documented against, this gap -- see
  ``AD_HOC_CLOSURE_TOLERANCE_SESSIONS``.
* **Non-US venues are not modelled.** This calendar answers questions about US
  equities, which is what both callers ask. The repo also stores FX, metals and
  index CFDs, whose trading week differs; nothing here should be used to judge
  those.

Holiday rules are taken from ``pandas.tseries.holiday`` (already a hard
dependency via ``pandas>=2.0,<4``) rather than from a hand-maintained table, so
they are procedural and hold for any year. The two rules pandas does not
provide -- "Saturday holiday is observed the preceding Friday" and "a New
Year's Day falling on Saturday is observed on 31 December of the previous
year" -- are applied explicitly below and are covered by the table-driven
tests.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

from pandas.tseries.holiday import (
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    sunday_to_monday,
)

__all__ = [
    "AD_HOC_CLOSURE_TOLERANCE_SESSIONS",
    "SESSION_CLOSE",
    "SESSION_OPEN",
    "US_EASTERN",
    "bar_covering_session",
    "bar_staleness_sessions",
    "closure_explains_hole",
    "is_session",
    "last_session_at_or_before",
    "longest_scheduled_closure_days",
    "session_close_utc",
    "session_open_utc",
    "session_span_seconds",
    "sessions_in_range",
]

US_EASTERN = ZoneInfo("America/New_York")
SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)

_ONE_DAY = dt.timedelta(days=1)


def _saturday_holiday_to_friday(day: dt.date) -> dt.date:
    """A fixed-date holiday landing on Saturday is observed the Friday before.

    Independence Day and Christmas both do this on the US exchanges (NYSE rule
    7.2.1), so 4 July 2026 (a Saturday) closes the market on Friday 3 July.
    ``pandas``' federal calendar only shifts Sunday holidays, so without this
    the Saturday case would be treated as a trading day.
    """
    return day - _ONE_DAY if day.weekday() == 5 else day


def _new_years_day(day: dt.date) -> dt.date:
    """1 January falling on Saturday is observed on 31 December, not 2 January.

    2022-01-01 was a Saturday and the exchanges were closed on Friday
    2021-12-31. A naive "shift forward" would have reported 3 January 2022 as
    the reopening day and mis-sized the surrounding closure by a session.
    """
    if day.weekday() != 5:
        return day
    return day - dt.timedelta(days=1)


#: Procedural holiday rules. ``observance`` handles the Sunday case; the
#: Saturday case is applied by the post-processing pass in :func:`holidays`.
_HOLIDAY_RULES: tuple[Holiday, ...] = (
    Holiday("New Year's Day", month=1, day=1, observance=_new_years_day),
    USMartinLutherKingJr,
    USPresidentsDay,
    # The US exchanges have observed Juneteenth National Independence Day since
    # 2022. `pandas`' federal calendar does not carry it, so an unmodelled
    # calendar would schedule a session on 19 June -- which for 2026 (a Friday)
    # means a real closure day that this module would happily call a session.
    Holiday(
        "Juneteenth National Independence Day",
        month=6,
        day=19,
        observance=sunday_to_monday,
    ),
    GoodFriday,
    USMemorialDay,
    USLaborDay,
    USThanksgivingDay,
    Holiday("Independence Day", month=7, day=4, observance=sunday_to_monday),
    Holiday("Christmas", month=12, day=25, observance=sunday_to_monday),
)


@lru_cache(maxsize=8)
def holidays(start: dt.date, end: dt.date) -> frozenset[dt.date]:
    """Scheduled US equity closures in the inclusive range ``[start, end]``.

    The Saturday-observed-on-Friday shift is applied here rather than through a
    ``DateOffset`` because pandas rejects a callable in ``offset`` and a
    ``BaseOffset`` subclass emits a ``PerformanceWarning`` on every call.
    """
    found: set[dt.date] = set()
    for rule in _HOLIDAY_RULES:
        for moment in rule.dates(
            dt.datetime(start.year - 1, 1, 1), dt.datetime(end.year + 1, 12, 31)
        ):
            observed = _saturday_holiday_to_friday(moment.date())
            if start <= observed <= end:
                found.add(observed)
    return frozenset(found)


def is_session(day: dt.date) -> bool:
    """Whether the US exchanges were scheduled to trade a regular session."""
    return day.weekday() < 5 and day not in holidays(day, day)


def session_open_utc(day: dt.date) -> dt.datetime:
    """First instant of the regular session, DST-correct and in UTC."""
    return dt.datetime.combine(day, SESSION_OPEN, tzinfo=US_EASTERN).astimezone(dt.timezone.utc)


def session_close_utc(day: dt.date) -> dt.datetime:
    """Last instant of the regular session, DST-correct and in UTC."""
    return dt.datetime.combine(day, SESSION_CLOSE, tzinfo=US_EASTERN).astimezone(dt.timezone.utc)


@lru_cache(maxsize=256)
def sessions_in_range(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    """Scheduled sessions in the inclusive range ``[start, end]``, ascending."""
    if end < start:
        return ()
    closures = holidays(start, end)
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in closures:
            days.append(cursor)
        cursor += _ONE_DAY
    return tuple(days)


def _require_utc(moment: dt.datetime, label: str) -> dt.datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware; got a naive {moment!r}")
    return moment.astimezone(dt.timezone.utc)


def last_session_at_or_before(moment: dt.datetime) -> dt.date:
    """The latest session that should already have printed a bar by ``moment``.

    A moment *before* the open rolls back to the previous session: at 08:00
    Eastern on Tuesday, Tuesday's bars do not exist yet, so Tuesday is not an
    answer here. A moment inside a closure rolls back to the last session before
    it, which is what makes "the market is simply closed" indistinguishable
    from "the feed is current" -- both are healthy.
    """
    moment = _require_utc(moment, "moment")
    day = moment.astimezone(US_EASTERN).date()
    if is_session(day):
        if moment >= session_open_utc(day):
            return day
        # Before today's open: today's bars do not exist yet, so step back. The
        # walk below would otherwise answer `day` on its first iteration, because
        # `day` is a session -- which is exactly the case this branch exists for.
        day -= _ONE_DAY
    # A weekend or holiday can be up to four calendar days long (Thanksgiving),
    # so walk back until a session is found rather than assuming one step.
    for _ in range(14):
        if is_session(day):
            return day
        day -= _ONE_DAY
    raise ValueError(f"no US equity session found within 14 days before {moment.isoformat()}")


def bar_covering_session(
    bar: dt.datetime, session: dt.date, nominal_days: float
) -> bool:
    """Whether ``bar`` is the series' bar for ``session`` (or a later one).

    A bar's timestamp is the *start* of the interval it summarises, so the bar
    for a session is the one whose interval ``[bar, bar + spacing)`` contains
    that session's open. This is what lets one expression cover every
    timeframe: a 1-minute bar stamped at the open contains it, a daily bar
    stamped at midnight contains it, and a weekly bar stamped on Monday
    contains every session from Monday to Friday.
    """
    bar = _require_utc(bar, "bar")
    opens_at = session_open_utc(session)
    span = dt.timedelta(days=max(float(nominal_days), 0.0))
    return bar <= opens_at < bar + span


def bar_staleness_sessions(
    newest_bar: dt.datetime,
    now: dt.datetime,
    *,
    nominal_days: float,
) -> tuple[int, dt.date, dt.date]:
    """How many scheduled sessions the newest bar trails ``now`` by.

    Returns ``(behind, bar_session, expected_session)``. ``behind == 0`` means
    the bar is the current session's (or a newer one, if the broker is ahead of
    the clock). ``behind > 0`` means the bar's session is ``behind`` scheduled
    sessions before the last session that should have printed by now.

    The measure is in *sessions*, not hours, which is the whole point: the same
    bar is 0 sessions behind across any weekend and any holiday, so a
    calendar-aware answer needs no length budget at all.
    """
    newest_bar = _require_utc(newest_bar, "newest_bar")
    now = _require_utc(now, "now")
    expected = last_session_at_or_before(now)
    if bar_covering_session(newest_bar, expected, nominal_days):
        return 0, expected, expected

    # The bar does not cover the expected session, so find the session it does
    # belong to: the first session opening inside the bar's own interval, and
    # failing that the last session that opened before the bar.
    span = dt.timedelta(days=max(float(nominal_days), 0.0))
    window_start = newest_bar.date() - _ONE_DAY
    window_end = newest_bar.date() + _ONE_DAY
    for session in sessions_in_range(window_start, window_end):
        opens_at = session_open_utc(session)
        if newest_bar <= opens_at < newest_bar + span:
            return (
                _sessions_between(session, expected),
                session,
                expected,
            )
    bar_session = last_session_at_or_before(newest_bar)
    return _sessions_between(bar_session, expected), bar_session, expected


def _sessions_between(earlier: dt.date, later: dt.date) -> int:
    """Scheduled sessions in the half-open range ``(earlier, later]``."""
    if later <= earlier:
        return 0
    return len(sessions_in_range(earlier + _ONE_DAY, later))


#: Sessions of slack a staleness gate must grant on top of the calendar's own
#: answer. Two, for two separately necessary reasons:
#:
#: 1. **One session of publication lag.** A broker publishes a session's daily
#:    bar only after that session has closed, so a daily series legitimately
#:    trails the current session by one. Treating that as stale would refuse
#:    every deployment for the whole session.
#: 2. **One unmodelled closure.** The calendar cannot contain a National Day of
#:    Mourning or a hurricane shutdown, and none is published in advance. Every
#:    unscheduled US equity closure on record closed a **single** session
#:    (1972 Nixon, 1973 Johnson, 1977 blackout, 1985 power cuts, 1994, 2004
#:    Reagan, 2005 Katrina, 2007 Ford, 2012 Sandy, 2018 Bush, 2025 Carter), so
#:    one session of slack covers the one closure no calendar can know about.
#:
#: What this costs, stated plainly: an unannounced closure is *indistinguishable*
#: from a feed that stopped printing, so no choice of tolerance is free. At two,
#: a two-session broker outage also passes the gate. At one, the outage is caught
#: but the session after an unannounced closure is refused, which reproduces the
#: failure this module exists to remove. Two is chosen because that failure is
#: systematic (it hits every deployment, ten times a year, for a whole session)
#: and silent, whereas a two-session Alpaca outage is rare, local to one feed,
#: and still yields real prices from the right series rather than invented ones.
#: A third session is refused, which is stricter than the flat 72-hour budget it
#: replaces: that one accepted a series up to three calendar days old in every
#: week of the year.
AD_HOC_CLOSURE_TOLERANCE_SESSIONS = 2


@lru_cache(maxsize=4)
def longest_scheduled_closure_days(years: tuple[int, int] = (2025, 2031)) -> float:
    """Longest bar-to-bar closure the modelled calendar produces, in days.

    Measured on the exchange's own grid: from the close of the last session
    before a closure to the open of the first session after it. Computed from
    the calendar rather than declared, so it moves when the rules do. The
    seven-year window is enough to contain every recurring pattern (leap years,
    and each weekday alignment of each fixed date); a longer window cannot find
    a longer closure, because the rules are periodic in the Gregorian cycle.
    """
    first, last = years
    days = sessions_in_range(dt.date(first, 1, 1), dt.date(last, 12, 31))
    if len(days) < 2:
        raise ValueError(f"calendar window {years} contains no sessions")
    gaps = [
        (session_open_utc(nxt) - session_close_utc(cur)).total_seconds() / 86_400.0
        for cur, nxt in zip(days, days[1:])
    ]
    return max(gaps)


def session_span_seconds(day: dt.date) -> float:
    """Length of one modelled regular session, in seconds.

    The calendar models the 6.5-hour regular session. A venue that trades around
    it -- which is what this repo's forex and index-CFD sources do -- places its
    first bar of a reopening session after the modelled open: the real FX weekend
    reopens at 22:00 UTC, 8.5 hours after the US equity open. Callers sizing a
    coverage-hole allowance need that width, so it is derived here rather than
    restated as a number.
    """
    return (session_close_utc(day) - session_open_utc(day)).total_seconds()


def closure_explains_hole(start: dt.date, end: dt.date) -> bool:
    """Whether the exchange calendar accounts for a hole between two dates.

    ``start`` and ``end`` are the dates of the last bar before the hole and the
    first bar after it -- the shape a coverage report records. This is the
    *shape* half of the question, and it is the half that no length budget can
    express. Three conditions:

    1. ``start`` is a scheduled session, so the hole begins at or after one
       rather than in the middle of a trading day.
    2. ``end`` is a scheduled session, so the series resumes on one.
    3. **No scheduled session falls strictly between them**, so the hole spans
       no complete trading day of its own, and at least one non-session day is
       covered.

    Condition 3 is what separates market behaviour from a dead downloader. A long
    weekend and a three-session outage are both about three days long, so any
    constant that excuses the first also passes the second -- which is how a flat
    4.0-day floor came to accept 1-minute data with a 3.6-day hole. An outage
    leaves a scheduled session inside the hole and is reported here; a closure
    does not.

    How *wide* a hole of this shape may be is deliberately not decided here: that
    is a policy question and belongs to the caller, which knows the timeframe
    and therefore the bar width. See ``closure_hole_allowance_seconds`` in
    ``data_tools.auto_download``.

    Note the deliberate looseness of a *date* comparison. A 24/5 venue that
    trades Friday evening to Monday evening closes for 73 hours over a weekend
    where the US equity regular session closes for 65.5, and both shapes land on
    the same two dates. Judging on dates accepts both; judging on clock times
    would have to pick a venue, and nothing here knows which venue a file holds.
    """
    if end <= start:
        return False
    if not is_session(start) or not is_session(end):
        return False
    # Strictly between: `start` and `end` are themselves sessions and are
    # allowed to be missing bars at their edges -- the hole starts at the end of
    # one and ends at the start of the other.
    if sessions_in_range(start + _ONE_DAY, end - _ONE_DAY):
        return False
    return _has_non_session_day(start, end)


def _has_non_session_day(start: dt.date, end: dt.date) -> bool:
    """Whether any day in ``(start, end]`` was not a scheduled session."""
    cursor = start + _ONE_DAY
    while cursor <= end:
        if not is_session(cursor):
            return True
        cursor += _ONE_DAY
    return False

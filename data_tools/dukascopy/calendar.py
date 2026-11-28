"""Market-closed calendar for Dukascopy gap validation.

The published dataset has no exchange-calendar feed: whether a day *should*
have bars is decided empirically from the symbol's own history plus a small
set of rules measured against the real dataset:

- **Saturday** is closed for every instrument.
- **Sunday** is closed when the symbol has no Sunday session — measured as
  fewer than a third of the Sundays in the year around the candidate day
  having bars (index/energy feeds here have eras with and without Sunday
  sessions, so a static list cannot work).
- **Good Friday** is closed for every instrument except the FX pairs, which
  trade through it in this feed.
- **Easter Monday** is closed for EURO50 and BRENT (the only symbols whose
  history shows a consistent Easter-Monday closure).
- **Year-end** (Dec 24 – Jan 2) is treated as closed for every instrument:
  the feed's behaviour here varies by year and instrument, and a missed
  detection for a few days a year is cheaper than a false alarm that blocks
  the whole weekly publish.
- Any other **weekday** is closed when the same calendar date was empty in
  at least half of its occurrences over the last eight years of the symbol's
  own bars (covers fixed-date holidays like May 1 or Oct 3 that recur on the
  same month-day).

``bar_days`` is the set of ``datetime.date`` on which the symbol produced at
least one M1 bar on either side, covering stored history plus the fresh
fetch. Everything here is read-only on that set.
"""

from __future__ import annotations

from datetime import date, timedelta

#: Canonical symbols whose feed trades through Good Friday.
FX_SYMBOLS = frozenset({"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD"})

#: Symbols with a consistent Easter-Monday closure in the published history.
EASTER_MONDAY_CLOSED = frozenset({"EURO50", "BRENT"})

_SUNDAY_WINDOW_DAYS = 364
_SUNDAY_MIN_FRACTION = 1 / 3
_RECURRENCE_YEARS = 8


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def good_friday(year: int) -> date:
    return easter_sunday(year) - timedelta(days=2)


def easter_monday(year: int) -> date:
    return easter_sunday(year) + timedelta(days=1)


def _sundays_with_bars(bar_days: set[date], day: date) -> tuple[int, int]:
    """(Sundays with bars, total Sundays) within a year either side of ``day``."""
    start = day - timedelta(days=_SUNDAY_WINDOW_DAYS)
    end = day + timedelta(days=_SUNDAY_WINDOW_DAYS)
    # Walk backwards to the Sunday on/before ``start``.
    cursor = start - timedelta(days=(start.weekday() + 1) % 7)
    total = with_bars = 0
    while cursor <= end:
        if cursor in bar_days:
            with_bars += 1
        total += 1
        cursor += timedelta(days=7)
    return with_bars, total


def _sunday_closed(day: date, bar_days: set[date]) -> bool:
    with_bars, total = _sundays_with_bars(bar_days, day)
    if total == 0:
        return True
    return with_bars / total < _SUNDAY_MIN_FRACTION


def _year_end_closed(day: date) -> bool:
    return (day.month == 12 and day.day >= 24) or (day.month == 1 and day.day <= 2)


def _recurrence_closed(day: date, bar_days: set[date]) -> bool:
    """True when the same month-day was empty in >= half of past occurrences."""
    first_year = min(bar_days).year if bar_days else day.year
    closed = occurrences = 0
    for year in range(max(first_year, day.year - _RECURRENCE_YEARS), day.year):
        try:
            past = date(year, day.month, day.day)
        except ValueError:
            continue  # Feb 29
        if past.weekday() >= 5:
            continue  # weekends are handled by the weekday rules
        occurrences += 1
        if past not in bar_days:
            closed += 1
    return occurrences > 0 and closed * 2 >= occurrences


def market_closed(symbol: str, day: date, bar_days: set[date]) -> bool:
    """Whether ``day`` is a market-closed day for ``symbol``."""
    if day.weekday() == 5:
        return True
    if day.weekday() == 6:
        return _sunday_closed(day, bar_days)
    if _year_end_closed(day):
        return True
    if symbol not in FX_SYMBOLS and day == good_friday(day.year):
        return True
    if symbol in EASTER_MONDAY_CLOSED and day == easter_monday(day.year):
        return True
    return _recurrence_closed(day, bar_days)


def empty_days_in_window(
    bid_days: set[date], ask_days: set[date], start: date, end: date
) -> dict[str, list[str]]:
    """Classify each day in ``[start, end)`` by side coverage.

    Returns ``{"both": [...], "bid": [...], "ask": [...]}`` — ISO dates where
    neither side, only the bid side, or only the ask side produced bars.
    """
    both: list[str] = []
    bid_only: list[str] = []
    ask_only: list[str] = []
    day = start
    while day < end:
        has_bid = day in bid_days
        has_ask = day in ask_days
        if has_bid and has_ask:
            pass
        elif has_bid:
            bid_only.append(day.isoformat())
        elif has_ask:
            ask_only.append(day.isoformat())
        else:
            both.append(day.isoformat())
        day += timedelta(days=1)
    return {"both": both, "bid": bid_only, "ask": ask_only}

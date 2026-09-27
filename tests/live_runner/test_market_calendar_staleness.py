"""The market-data staleness budget must follow the exchange calendar.

`AlpacaLiveAdapter.get_market_data` compared the newest bar's age against a flat
72 hours for intraday and 7 days for daily. The US exchanges close for up to
three consecutive weekdays plus a weekend, which puts the newest intraday bar at
89.5 hours, so that budget raised for *every* symbol from the Thursday close
until Tuesday of any single-holiday week. The `ValueError` is caught by the
daemon and logged as "Signal processor rejected discord signal", the deployment
still reports ACTIVE and the listener still shows connected, so the operator
concludes the strategy stopped firing.

The replacement counts *scheduled sessions* rather than hours
(`shared.market_calendar`), which is the same answer in every week of the year.
Each table below is driven by real 2026 NYSE/NASDAQ closure dates, and each row
also evaluates the budget the code used to apply so the defect is pinned rather
than merely described.
"""

from __future__ import annotations

import datetime as dt

import pytest

from shared import market_calendar

UTC = dt.timezone.utc


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── The calendar itself ──────────────────────────────────────────────────────

#: Every scheduled US equity closure in 2026, as published by the exchanges.
#: Table-driven on purpose: a rule the calendar gets wrong shifts a real
#: closure week rather than moving a number in a comment.
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
    ("holiday", "why"),
    [
        ("2026-07-03", "4 July 2026 is a Saturday, observed the preceding Friday"),
        ("2026-12-25", "25 December 2026 is a Friday, observed on the day"),
        ("2021-12-31", "1 January 2022 was a Saturday, observed in the prior year"),
        ("2022-12-24", "25 December 2021 was a Saturday, observed the preceding Friday"),
    ],
)
def test_saturday_holidays_are_observed_on_the_preceding_friday(holiday, why):
    """`pandas`' federal calendar only shifts Sunday holidays, so a Saturday
    holiday would be scheduled as a trading day -- and 4 July 2026 is one."""
    day = dt.date.fromisoformat(holiday)

    assert not market_calendar.is_session(day), why
    assert market_calendar.is_session(day - dt.timedelta(days=1)) or day.weekday() == 4


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

    assert limit == pytest.approx(89.5 / 24.0)


# ── The budget, table-driven over real closures ───────────────────────────────


def _old_budget_would_refuse(newest: dt.datetime, now: dt.datetime, timeframe: str):
    """The budget this replaced, evaluated as an oracle for the tables below."""
    budget = dt.timedelta(days=7) if timeframe == "1d" else dt.timedelta(hours=72)
    return newest < now - budget


def _reason(newest: dt.datetime, now: dt.datetime, timeframe: str) -> str | None:
    import pandas as pd

    from live_runner.alpaca_adapter import (
        _alpaca_timeframe,
        _market_data_staleness_reason,
    )

    resolved, _ = _alpaca_timeframe(timeframe)
    return _market_data_staleness_reason(
        pd.Timestamp(newest), pd.Timestamp(now), resolved_timeframe=resolved
    )


#: (label, last bar before the closure, the moment the gate runs, timeframe,
#: the flat budget's verdict). Every "now" is a moment when a live deployment
#: would actually be dispatching: inside a session, or in the first hours after
#: the reopen.
CLOSURE_CASES = [
    pytest.param(
        "Thanksgiving 2026: Wed 25 and Thu 26 closed, first session Mon 30",
        _at(2026, 11, 26, 20), _at(2026, 11, 30, 15), "5m", True,
        id="thanksgiving-2026",
    ),
    pytest.param(
        "mid-Thanksgiving, Thu 26 while the market is still shut",
        _at(2026, 11, 25, 20), _at(2026, 11, 26, 16), "5m", False,
        id="thanksgiving-mid-closure",
    ),
    pytest.param(
        "Thanksgiving 2026 early-close Friday: 13:00 close is still a session",
        _at(2026, 11, 25, 20), _at(2026, 11, 27, 19), "5m", False,
        id="thanksgiving-early-close",
    ),
    pytest.param(
        "Christmas 2026 on a Friday: Thu 24 close, Fri 25 shut, resumes Mon 28",
        _at(2026, 12, 24, 20), _at(2026, 12, 28, 15), "5m", True,
        id="christmas-on-a-friday",
    ),
    pytest.param(
        "New Year 2027 on a Friday: Thu 31 Dec close, Fri 1 Jan shut, resumes Tue 5",
        _at(2026, 12, 31, 20), _at(2027, 1, 5, 15), "5m", True,
        id="new-year-on-a-friday",
    ),
    pytest.param(
        "Labor Day 2026 (Monday): Fri 4 Sep close, resumes Tue 8",
        _at(2026, 9, 4, 20), _at(2026, 9, 8, 15), "5m", True,
        id="labor-day-2026",
    ),
    pytest.param(
        "Juneteenth 2026 (Friday): Thu 18 Jun close, resumes Mon 22",
        _at(2026, 6, 18, 20), _at(2026, 6, 22, 15), "5m", True,
        id="juneteenth-2026",
    ),
    pytest.param(
        "an ordinary long weekend: Fri 3 Jul close, resumes Mon 6",
        _at(2026, 7, 3, 20), _at(2026, 7, 6, 15), "5m", False,
        id="ordinary-long-weekend",
    ),
]


@pytest.mark.parametrize(
    ("label", "newest", "now", "timeframe", "old_refused"), CLOSURE_CASES
)
def test_a_real_closure_never_silences_the_adapter(
    label, newest, now, timeframe, old_refused
):
    assert _reason(newest, now, timeframe) is None, (
        "the market was shut; refusing here is the defect"
    )
    # Pin the defect rather than the prose: for the closure weeks the flat
    # budget really did refuse.
    assert _old_budget_would_refuse(newest, now, timeframe) is old_refused


def test_a_closure_is_silent_even_when_the_newest_bar_is_days_old():
    """The failure was quiet, so name the worst case explicitly: 115 hours, on
    the 1-minute timeframe, is legitimate market behaviour."""
    newest = _at(2026, 12, 31, 20)   # Thursday 31 December 2026 close
    now = _at(2027, 1, 5, 15)        # Tuesday 5 January, after the reopen
    assert now - newest == dt.timedelta(hours=115)

    assert _old_budget_would_refuse(newest, now, "1m") is True
    assert _reason(newest, now, "1m") is None


# ── A genuinely broken feed must still be refused ────────────────────────────

BROKEN_FEED_CASES = [
    pytest.param(
        "5m feed stopped for three sessions",
        _at(2026, 9, 8, 19, 55), _at(2026, 9, 11, 16), "5m", 3,
        id="three-sessions-5m",
    ),
    pytest.param(
        "15m feed stopped for four sessions across a weekend",
        _at(2026, 9, 11, 19, 55), _at(2026, 9, 16, 16), "15m", 3,
        id="four-sessions-15m",
    ),
    pytest.param(
        "1h feed stopped for four sessions",
        _at(2026, 9, 8, 19, 55), _at(2026, 9, 12, 14), "1h", 3,
        id="four-sessions-1h",
    ),
    pytest.param(
        "1d bars four sessions old",
        _at(2026, 9, 8, 4), _at(2026, 9, 11, 20, 30), "1d", 3,
        id="four-sessions-1d",
    ),
]


@pytest.mark.parametrize(
    ("label", "newest", "now", "timeframe", "behind"), BROKEN_FEED_CASES
)
def test_a_broken_feed_is_still_refused(label, newest, now, timeframe, behind):
    reason = _reason(newest, now, timeframe)

    assert reason is not None, "a dead feed must refuse, not trade on old prices"
    assert f"{behind} scheduled sessions behind" in reason
    assert str(market_calendar.AD_HOC_CLOSURE_TOLERANCE_SESSIONS) in reason


def test_the_daily_publication_lag_is_not_mistaken_for_staleness():
    """A broker publishes a session's daily bar only after that session closed,
    so a daily series legitimately trails the current session by one. Treating
    that as stale would refuse every deployment for a whole session."""
    newest = _at(2026, 9, 9, 4)        # Wednesday's daily bar
    now = _at(2026, 9, 10, 20, 30)     # Thursday, half an hour after its close

    assert _reason(newest, now, "1d") is None


def test_an_unannounced_closure_is_covered_by_the_tolerance():
    """No calendar contains a National Day of Mourning. Thursday 15 January 2026
    is an ordinary session as far as anyone can know in advance, so the newest
    bar is one and then two sessions behind -- which is exactly the allowance.

    Stated plainly because it is the cost of the choice: a two-session broker
    outage is indistinguishable from an unannounced closure and is also allowed.
    See `AD_HOC_CLOSURE_TOLERANCE_SESSIONS` for why that side of the trade was
    taken.
    """
    mid = _at(2026, 1, 15, 16)   # Thursday, while an unannounced closure holds
    after = _at(2026, 1, 16, 16)  # Friday, the first session after it

    assert _reason(_at(2026, 1, 14, 20), mid, "5m") is None
    assert _reason(_at(2026, 1, 14, 20), after, "5m") is None


# ── "The market is simply closed" and the edges ──────────────────────────────


def test_a_closed_market_is_indistinguishable_from_a_current_feed():
    """Saturday noon: the newest bar is Friday's close, 16 hours old, and there
    is nothing wrong with anything."""
    newest = _at(2026, 9, 11, 20)   # Friday 11 September 2026 close
    now = _at(2026, 9, 12, 12)      # Saturday 12 September, midday UTC

    assert not market_calendar.is_session(dt.date(2026, 9, 12))
    assert _reason(newest, now, "1m") is None


def test_before_the_open_the_previous_session_is_still_current():
    newest = _at(2026, 9, 9, 20)    # Wednesday's close
    now = _at(2026, 9, 10, 12)      # Thursday 08:00 Eastern, before the open

    assert _reason(newest, now, "5m") is None


def test_a_bar_from_the_current_session_is_fresh_at_every_timeframe():
    import pandas as pd

    from live_runner.alpaca_adapter import (
        _alpaca_timeframe,
        _alpaca_timeframe_nominal_days,
        _market_data_staleness_reason,
    )

    for timeframe in ("1m", "5m", "1h", "1d"):
        resolved, _ = _alpaca_timeframe(timeframe)
        # 11 September 2026 19:55 UTC = 15:55 Eastern, inside the session. The
        # daily bar for the session is stamped at its midnight, so the intraday
        # and daily cases differ in where the bar is stamped, not in verdict.
        newest = pd.Timestamp(_at(2026, 9, 11, 19, 55))
        now = pd.Timestamp(_at(2026, 9, 11, 20, 0))

        assert _alpaca_timeframe_nominal_days(resolved) > 0
        assert (
            _market_data_staleness_reason(newest, now, resolved_timeframe=resolved)
            is None
        ), timeframe


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc():
    """Fail closed on ambiguity: a timestamp with no zone cannot be compared to
    a session, and guessing Eastern would be guessing."""
    import pandas as pd

    from live_runner.alpaca_adapter import (
        _alpaca_timeframe,
        _market_data_staleness_reason,
    )

    resolved, _ = _alpaca_timeframe("5m")
    with pytest.raises(ValueError, match="timezone-aware"):
        _market_data_staleness_reason(
            pd.Timestamp("2026-09-11 19:55"),
            pd.Timestamp(_at(2026, 9, 11, 20)),
            resolved_timeframe=resolved,
        )


def test_an_unknown_timeframe_unit_is_refused_rather_than_given_a_default_budget():
    class _FakeTimeFrame:
        amount = 1
        unit_value = None

    from live_runner.alpaca_adapter import _alpaca_timeframe_nominal_days

    with pytest.raises(ValueError, match="staleness budget"):
        _alpaca_timeframe_nominal_days(_FakeTimeFrame())


# ── The wiring, not just the helper ──────────────────────────────────────────
#
# Everything above calls `_market_data_staleness_reason` directly, which pins the
# policy but not the fact that `get_market_data` consults it. These two drive the
# real method with a fake broker, so a budget that is computed correctly and then
# not used is still caught.


def _adapter_returning(newest_bar: dt.datetime, periods: int = 200):
    """An AlpacaLiveAdapter whose broker returns a series ending at newest_bar."""
    import pandas as pd

    from live_runner import alpaca_adapter as module

    stamps = pd.date_range(end=pd.Timestamp(newest_bar), periods=periods, freq="1h")
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [1.0] * periods,
            "high": [1.0] * periods,
            "low": [1.0] * periods,
            "close": [1.0] * periods,
        }
    ).set_index("timestamp")

    class _Client:
        def get_stock_bars(self, _request):
            return type("R", (), {"df": frame})()

    adapter = module.AlpacaLiveAdapter.__new__(module.AlpacaLiveAdapter)
    adapter._connected = True
    adapter._historical_data_client = _Client()
    return adapter, frame


def _freeze_now(monkeypatch, frozen: dt.datetime) -> None:
    """Pin the adapter's clock so a closure week can be reached deterministically."""
    import pandas as pd

    from live_runner import alpaca_adapter as module

    monkeypatch.setattr(module, "_now_utc", lambda: pd.Timestamp(frozen))


def test_get_market_data_accepts_a_series_that_spans_a_two_session_closure(
    monkeypatch,
):
    """The defect, through the real method: the newest bar is 89.5 hours old
    because Labor Day 2026 was a Monday, and the adapter used to raise for
    every symbol from Friday's close until Tuesday."""
    import asyncio

    frozen_now = _at(2026, 9, 8, 15)  # Tuesday 8 September, after the open
    _freeze_now(monkeypatch, frozen_now)
    adapter, _ = _adapter_returning(_at(2026, 9, 4, 20))  # Friday's close

    assert (frozen_now - _at(2026, 9, 4, 20)) == dt.timedelta(hours=91)
    out = asyncio.run(adapter.get_market_data("AAPL", timeframe="5m", limit=200))

    assert not out.empty


def test_get_market_data_still_refuses_a_genuinely_stale_series(monkeypatch):
    import asyncio

    import pytest

    _freeze_now(monkeypatch, _at(2026, 9, 16, 15))
    adapter, _ = _adapter_returning(_at(2026, 6, 1, 20))  # months old

    with pytest.raises(ValueError, match="market data is stale"):
        asyncio.run(adapter.get_market_data("AAPL", timeframe="5m", limit=200))

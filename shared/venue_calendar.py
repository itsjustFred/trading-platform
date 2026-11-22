"""Venue-aware trading-session calendars for coverage and staleness checks.

The coverage gate used to assume a US-equity weekday calendar for every
series.  That is wrong for crypto (24/7) and FX (24/5 with a Sunday-evening
reopen), and it is only an approximation for broker CFDs whose sessions,
breaks and holidays come from the broker itself.

This module keeps the calendar model in-repo (no new dependency) and exposes
one resolution path for both batch coverage checks and live staleness:

* broker calendars win when a cached schedule exists (Alpaca ``/v2/calendar``
  for US equities, cTrader ``ProtoOASymbol.schedule`` for cTrader symbols);
* otherwise a built-in model is used only when the venue is unambiguous:
  crypto -> 24/7, FX -> 24/5 New-York-week, US equity -> NYSE regular
  sessions;
* anything else resolves to ``None`` and callers must fail closed.

Observed bar timestamps are never used to infer session hours here; they are
diagnostic only.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from shared import market_calendar

logger = logging.getLogger(__name__)

UTC = dt.timezone.utc
SECONDS_PER_WEEK = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class TradingInterval:
    """One half-open ``[open, close)`` trading interval in UTC."""

    open: pd.Timestamp
    close: pd.Timestamp

    def __post_init__(self) -> None:
        if self.open.tzinfo is None or self.close.tzinfo is None:
            raise ValueError("TradingInterval bounds must be timezone-aware")
        if self.close <= self.open:
            raise ValueError("TradingInterval close must be after open")


class VenueCalendar:
    """Session model for one symbol/venue.

    ``trading_intervals`` is the authoritative primitive: expected bars,
    trading-minute gaps and live open/closed state are all derived from it so
    the same model answers every question.
    """

    name: str = "unknown"
    source: str = "builtin"

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        raise NotImplementedError

    def is_trading_instant(self, ts: pd.Timestamp) -> bool:
        ts = _as_utc(ts)
        # trading_intervals() returns [] for an empty [start, end) range, so a
        # point query needs a small window around the instant. Two microseconds
        # is enough: every implementation clips intervals to [start, end) and
        # keeps pieces with hi > lo, so an instant strictly inside a session
        # always yields a non-empty piece containing it.
        probe = pd.Timedelta(microseconds=1)
        return any(
            iv.open <= ts < iv.close
            for iv in self.trading_intervals(ts - probe, ts + probe)
        )

    def trading_minutes_between(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> float:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start:
            return 0.0
        total = 0.0
        for iv in self.trading_intervals(start, end):
            overlap_start = max(start, iv.open)
            overlap_end = min(end, iv.close)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60.0
        return total

    def expected_bars(
        self,
        start: pd.Timestamp,
        end: pd.Timestamp,
        step_minutes: float,
    ) -> int:
        """Expected bar *start* instants inside trading time.

        Intraday grids are aligned to the interval open for session calendars
        and to the UTC epoch for continuous calendars (FX/crypto bars are
        clock-aligned, not session-open-aligned).  Daily+ bars count trading
        dates, matching the stamped-date convention used by the repo's daily
        series.
        """
        start = _as_utc(start)
        end = _as_utc(end)
        if end < start or step_minutes <= 0:
            return 0
        if step_minutes >= 10080.0:
            # Weekly bars exist for every venue and are sparse enough that the
            # old wall-clock grid is the documented convention.
            steps = int((end - start).total_seconds() / 60.0 // step_minutes)
            return steps + 1
        if step_minutes >= 1440.0:
            return self._expected_daily_bars(start, end, step_minutes)
        return self._expected_intraday_bars(start, end, step_minutes)

    def _expected_intraday_bars(
        self, start: pd.Timestamp, end: pd.Timestamp, step_minutes: float
    ) -> int:
        count = 0
        step = pd.Timedelta(minutes=step_minutes)
        intervals = self.trading_intervals(start, end)
        for index, iv in enumerate(intervals):
            first = max(start, iv.open)
            last = min(end, iv.close - pd.Timedelta(microseconds=1))
            if last < first:
                continue
            if self.grid_anchor == "session_open":
                # The first interval's open is clipped at the query boundary;
                # anchoring the bar grid to the CLIPPED open would shift every
                # expected bar. Recover the session's true open instead.
                anchor_open = (
                    self._session_open_anchor(iv.open)
                    if index == 0 and iv.open == start
                    else iv.open
                )
                offset = math.ceil(
                    (first - anchor_open).total_seconds() / (step_minutes * 60.0)
                )
                instant = anchor_open + pd.Timedelta(minutes=offset * step_minutes)
            else:
                # Epoch-aligned grid (UTC clock bars).
                epoch = pd.Timestamp(0, tz=UTC)
                offset = math.ceil(
                    (first - epoch).total_seconds() / (step_minutes * 60.0)
                )
                instant = epoch + pd.Timedelta(minutes=offset * step_minutes)
            while instant <= last:
                count += 1
                instant += step
        return count

    def _session_open_anchor(self, clipped_open: pd.Timestamp) -> pd.Timestamp:
        """The unclipped session open behind a boundary-clipped interval open.

        ``trading_intervals`` clips every interval to the query range, so an
        interval starting exactly at ``start`` may really have opened earlier.
        Re-querying a window that reaches back before the clip recovers the
        session's true open; the grid then aligns to the session, not to the
        arbitrary segment boundary.
        """
        probe_start = clipped_open - pd.Timedelta(days=2)
        probe_end = clipped_open + pd.Timedelta(microseconds=1)
        candidates = self.trading_intervals(probe_start, probe_end)
        for cand in reversed(candidates):
            if cand.open <= clipped_open < cand.close:
                return cand.open
        return clipped_open

    def _expected_daily_bars(
        self, start: pd.Timestamp, end: pd.Timestamp, step_minutes: float
    ) -> int:
        # Daily+ bars are labelled by trading date, not by grid position:
        # count the distinct dates the venue considers a trading day inside
        # ``[start, end]``.  ``FxWeekCalendar`` overrides the date identity
        # (NY-close session dates, Mon-Fri) because its UTC Sunday carries a
        # partial session that is not a fifth/sixth daily bar.
        return len(self.daily_bar_dates(start, end))

    def daily_bar_dates(self, start: pd.Timestamp, end: pd.Timestamp) -> set[dt.date]:
        """Trading dates that own a daily bar inside ``[start, end]``."""
        start = _as_utc(start)
        end = _as_utc(end)
        dates: set[dt.date] = set()
        probe = start.normalize()
        while probe <= end.normalize():
            day_start = probe.tz_localize(UTC) if probe.tzinfo is None else probe
            if (
                self.trading_minutes_between(
                    day_start, day_start + pd.Timedelta(days=1)
                )
                > 0
            ):
                dates.add(day_start.date())
            probe += pd.Timedelta(days=1)
        return dates

    def daily_bar_date(self, ts: pd.Timestamp) -> dt.date | None:
        """The daily-bar identity of a bar stamped ``ts`` (``None`` = surplus)."""
        return _as_utc(ts).date()

    grid_anchor: str = "utc"

    def covers_window(self, start: pd.Timestamp, end: pd.Timestamp) -> bool:
        """Whether this calendar is authoritative over all of ``[start, end]``.

        Calendars rebuilt from a fetched payload (exchange session lists) only
        know the range they were fetched for; outside it they must not answer
        as if every absent date were closed. Built-in calendars cover every
        window.
        """
        return True

    def covers_instant(self, ts: pd.Timestamp) -> bool:
        """Whether this calendar is authoritative at ``ts``."""
        return self.covers_window(_as_utc(ts), _as_utc(ts))

    def session_state(self, now: pd.Timestamp) -> str:
        """``open``/``closed``/``unknown`` for live staleness checks.

        ``unknown`` means the calendar cannot speak for ``now`` (for example
        a session cache whose fetched range does not reach it); callers must
        treat it like an unresolved venue, never like ``closed``.
        """
        now = _as_utc(now)
        if not self.covers_instant(now):
            return "unknown"
        return "open" if self.is_trading_instant(now) else "closed"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source}


class ContinuousCalendar(VenueCalendar):
    """24/7 venue (crypto)."""

    def __init__(self, name: str = "crypto-24x7") -> None:
        self.name = name
        self.source = "builtin"
        self.grid_anchor = "utc"

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start:
            return []
        return [TradingInterval(start, end)]


class FxWeekCalendar(VenueCalendar):
    """FX 24/5: Sunday 17:00 -> Friday 17:00 America/New_York, DST-aware."""

    def __init__(self) -> None:
        self.name = "fx-24x5"
        self.source = "builtin"
        self.grid_anchor = "utc"
        self.zone = ZoneInfo("America/New_York")

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start:
            return []
        intervals: list[TradingInterval] = []
        # Each trading week opens Sunday 17:00 ET and closes Friday 17:00 ET.
        # Walk week-open candidates around the range; the close is 5 days later.
        cursor = (start - pd.Timedelta(days=8)).tz_convert(self.zone).date()
        last = (end + pd.Timedelta(days=8)).tz_convert(self.zone).date()
        while cursor <= last:
            # Sunday of this calendar week in New York local time.
            days_ahead = (6 - cursor.weekday()) % 7  # weekday(): Mon=0..Sun=6
            sunday = cursor + dt.timedelta(days=days_ahead)
            open_local = dt.datetime.combine(sunday, dt.time(17, 0), tzinfo=self.zone)
            close_local = open_local + dt.timedelta(days=5)
            open_utc = pd.Timestamp(open_local.astimezone(UTC))
            close_utc = pd.Timestamp(close_local.astimezone(UTC))
            lo = max(start, open_utc)
            hi = min(end, close_utc)
            if hi > lo:
                intervals.append(TradingInterval(lo, hi))
            cursor += dt.timedelta(days=7)
        return intervals

    def daily_bar_dates(self, start: pd.Timestamp, end: pd.Timestamp) -> set[dt.date]:
        # FX daily bars follow the New-York-close convention: each bar covers
        # the 24h ending at 17:00 ET and is labelled by that close's NY date,
        # giving exactly five bars per week (Mon-Fri).  Sunday 17:00 ET opens
        # the week; the partial Sunday UTC session is NOT a sixth bar.
        start = _as_utc(start)
        end = _as_utc(end)
        dates: set[dt.date] = set()
        first = start.tz_convert(self.zone).date() - dt.timedelta(days=2)
        last = end.tz_convert(self.zone).date() + dt.timedelta(days=2)
        day = first
        while day <= last:
            if day.weekday() < 5:  # Mon-Fri close dates only
                boundary_local = dt.datetime.combine(day, dt.time(17, 0), tzinfo=self.zone)
                boundary_utc = pd.Timestamp(boundary_local.astimezone(UTC))
                segment_open = pd.Timestamp(
                    (boundary_local - dt.timedelta(days=1)).astimezone(UTC)
                )
                # A bar is expected only once its window has CLOSED inside
                # the segment (``boundary_utc <= end``), matching
                # ``BrokerScheduleCalendar.daily_bar_dates``.
                if (
                    segment_open < end
                    and boundary_utc > start
                    and boundary_utc <= end
                ):
                    dates.add(day)
            day += dt.timedelta(days=1)
        return dates

    def daily_bar_date(self, ts: pd.Timestamp) -> dt.date | None:
        ts = _as_utc(ts)
        local = ts.tz_convert(self.zone)
        # The trading week runs Sunday 17:00 -> Friday 17:00 ET.  Check this
        # week's Sunday and the previous one (``ts`` may precede this Sunday's
        # open while still belonging to the prior week is impossible, but the
        # extra candidate keeps boundary stamps unambiguous).
        days_ahead = (6 - local.date().weekday()) % 7
        sunday = local.date() + dt.timedelta(days=days_ahead)
        for week_sunday in (sunday, sunday - dt.timedelta(days=7)):
            open_local = dt.datetime.combine(
                week_sunday, dt.time(17, 0), tzinfo=self.zone
            )
            open_utc = pd.Timestamp(open_local.astimezone(UTC))
            close_local = open_local + dt.timedelta(days=5)
            close_utc = pd.Timestamp(close_local.astimezone(UTC))
            # Half-open [open, close): a stamp exactly on a daily boundary is
            # the OPEN of the next bar (bars are stamped at their open), not
            # the close of the previous one.
            if not (open_utc <= ts < close_utc):
                continue
            for k in range(1, 6):
                boundary_local = open_local + dt.timedelta(days=k)
                boundary_utc = pd.Timestamp(boundary_local.astimezone(UTC))
                if ts < boundary_utc:
                    return boundary_local.date()
        return None


class NyseCalendar(VenueCalendar):
    """US equity regular sessions via shared.market_calendar."""

    def __init__(self, name: str = "nyse-regular") -> None:
        self.name = name
        self.source = "builtin"
        self.grid_anchor = "session_open"

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start:
            return []
        first_day = start.tz_convert(market_calendar.US_EASTERN).date() - dt.timedelta(days=1)
        last_day = end.tz_convert(market_calendar.US_EASTERN).date() + dt.timedelta(days=1)
        intervals: list[TradingInterval] = []
        for day in market_calendar.sessions_in_range(first_day, last_day):
            open_utc = pd.Timestamp(market_calendar.session_open_utc(day))
            close_utc = pd.Timestamp(market_calendar.session_close_utc(day))
            lo = max(start, open_utc)
            hi = min(end, close_utc)
            if hi > lo:
                intervals.append(TradingInterval(lo, hi))
        return intervals


class ExchangeSessionCalendar(VenueCalendar):
    """Calendar rebuilt from an exchange's published session list.

    This is the shape Alpaca ``/v2/calendar`` returns for US equities: one
    entry per trading day with ``open``/``close`` clock times in
    ``America/New_York``. Early closes (``close="13:00"``) are first-class
    here, which the built-in NYSE model cannot express. Dates absent from the
    payload are closed — holidays need no separate list.

    The payload also records the ``[coverage_start, coverage_end]`` date range
    the sessions were fetched for. Outside that range the calendar is NOT
    authoritative: dates absent from the payload may simply never have been
    fetched, so :meth:`covers_window` reports ``False`` and callers must fall
    back rather than treating every uncovered day as closed.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.name = str(payload.get("name") or "exchange-sessions")
        self.source = str(payload.get("source") or "broker")
        self.grid_anchor = "session_open"
        try:
            self.zone = ZoneInfo(str(payload.get("schedule_timezone") or "America/New_York"))
        except Exception:
            self.zone = ZoneInfo("America/New_York")
        self.sessions: dict[dt.date, tuple[dt.time, dt.time]] = {}
        for item in payload.get("sessions") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                day = dt.date.fromisoformat(str(item["date"]))
                open_t = _parse_hhmm(item.get("open"))
                close_t = _parse_hhmm(item.get("close"))
            except (KeyError, TypeError, ValueError):
                continue
            if open_t is None or close_t is None or close_t <= open_t:
                continue
            self.sessions[day] = (open_t, close_t)
        # Fetched coverage, as UTC instants: a sorted list of disjoint
        # ``[start, end)`` ranges, each spanning whole dates
        # ([coverage_start 00:00, coverage_end+1 00:00)). Coverage is per
        # RANGE, not per envelope: downloads are per timeframe with their own
        # resume start, so two fetches can leave a real gap that min(start)/
        # max(end) merging would falsely mark as covered. A payload without
        # bounds (written before coverage tracking existed) covers nothing:
        # it cannot prove which dates were ever fetched.
        self._covered_ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        for start_day, end_day in _payload_coverage_date_ranges(payload):
            self._covered_ranges.append(
                (
                    pd.Timestamp(start_day, tz=UTC),
                    pd.Timestamp(end_day + dt.timedelta(days=1), tz=UTC),
                )
            )

    def covers_window(self, start: pd.Timestamp, end: pd.Timestamp) -> bool:
        start = _as_utc(start)
        end = _as_utc(end)
        # A window is covered only when ONE fetched range contains it whole;
        # spanning two disjoint ranges proves nothing about the gap between.
        return any(
            lo <= start and end <= hi for lo, hi in self._covered_ranges
        )

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start or not self.sessions:
            return []
        first_day = start.tz_convert(self.zone).date() - dt.timedelta(days=1)
        last_day = end.tz_convert(self.zone).date() + dt.timedelta(days=1)
        intervals: list[TradingInterval] = []
        day = first_day
        while day <= last_day:
            bounds = self.sessions.get(day)
            if bounds is not None:
                open_local = dt.datetime.combine(day, bounds[0], tzinfo=self.zone)
                close_local = dt.datetime.combine(day, bounds[1], tzinfo=self.zone)
                open_utc = pd.Timestamp(open_local.astimezone(UTC))
                close_utc = pd.Timestamp(close_local.astimezone(UTC))
                lo = max(start, open_utc)
                hi = min(end, close_utc)
                if hi > lo:
                    intervals.append(TradingInterval(lo, hi))
            day += dt.timedelta(days=1)
        return intervals


def _parse_hhmm(value: Any) -> dt.time | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def _parse_coverage_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _payload_coverage_date_ranges(
    payload: Mapping[str, Any],
) -> list[tuple[dt.date, dt.date]]:
    """Covered ``(start, end)`` date pairs from a cache payload.

    ``coverage_ranges`` (written by current versions) is authoritative: a
    payload carrying the key yields ONLY its parsed ranges, so a corrupt or
    empty list can never fall back to the envelope fields and reopen the
    disjoint-range hole. Legacy payloads without the key contribute their
    single ``coverage_start``/``coverage_end`` pair.
    """
    ranges: list[tuple[dt.date, dt.date]] = []
    if "coverage_ranges" in payload:
        raw_ranges = payload.get("coverage_ranges")
        if not isinstance(raw_ranges, list):
            return ranges
        for item in raw_ranges:
            start_raw = end_raw = None
            if isinstance(item, Mapping):
                start_raw = item.get("start", item.get("coverage_start"))
                end_raw = item.get("end", item.get("coverage_end"))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                start_raw, end_raw = item[0], item[1]
            start_day = _parse_coverage_date(start_raw)
            end_day = _parse_coverage_date(end_raw)
            if start_day is not None and end_day is not None and end_day >= start_day:
                ranges.append((start_day, end_day))
        return ranges
    start_day = _parse_coverage_date(payload.get("coverage_start"))
    end_day = _parse_coverage_date(payload.get("coverage_end"))
    if start_day is not None and end_day is not None and end_day >= start_day:
        return [(start_day, end_day)]
    return []


def _merge_coverage_date_ranges(
    ranges: Iterable[tuple[dt.date, dt.date]],
) -> list[tuple[dt.date, dt.date]]:
    """Sort and merge ranges that overlap or touch (end + 1 day == start)."""
    merged: list[tuple[dt.date, dt.date]] = []
    for start_day, end_day in sorted(ranges):
        if merged and start_day <= merged[-1][1] + dt.timedelta(days=1):
            if end_day > merged[-1][1]:
                merged[-1] = (merged[-1][0], end_day)
        else:
            merged.append((start_day, end_day))
    return merged


def exchange_sessions_payload(
    *,
    source: str,
    symbol: str,
    sessions: Iterable[Mapping[str, Any]],
    schedule_timezone: str = "America/New_York",
    fetched_at: str | None = None,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Cache payload for an exchange-published session list (Alpaca /v2/calendar).

    ``coverage_start``/``coverage_end`` are the ISO dates the fetch covered;
    the calendar is only authoritative inside them. ``prior`` is an existing
    payload of the same kind: sessions are merged (new entries win on a date
    collision) and coverage ranges are merged — overlapping or touching
    ranges fuse, disjoint ones stay separate so an incremental download
    never claims the unfetched gap between them.
    """
    merged: dict[str, dict[str, str]] = {}
    covered: list[tuple[dt.date, dt.date]] = []
    stamp = fetched_at
    if isinstance(prior, Mapping) and prior.get("kind") == "exchange_sessions":
        for item in prior.get("sessions") or []:
            if isinstance(item, Mapping) and item.get("date") is not None:
                merged[str(item["date"])] = {
                    "date": str(item["date"]),
                    "open": str(item.get("open") or ""),
                    "close": str(item.get("close") or ""),
                }
        covered.extend(_payload_coverage_date_ranges(prior))
        prior_stamp = prior.get("fetched_at")
        if prior_stamp is not None and (stamp is None or str(prior_stamp) > str(stamp)):
            stamp = str(prior_stamp)
    new_start = _parse_coverage_date(coverage_start)
    new_end = _parse_coverage_date(coverage_end)
    if new_start is not None and new_end is not None and new_end >= new_start:
        covered.append((new_start, new_end))
    for item in sessions:
        if isinstance(item, Mapping) and item.get("date") is not None:
            merged[str(item["date"])] = {
                "date": str(item["date"]),
                "open": str(item.get("open") or ""),
                "close": str(item.get("close") or ""),
            }
    covered = _merge_coverage_date_ranges(covered)
    payload: dict[str, Any] = {
        "kind": "exchange_sessions",
        "name": f"{source}-{symbol.upper()}-exchange",
        "source": source,
        "symbol": symbol.upper(),
        "schedule_timezone": schedule_timezone,
        "fetched_at": stamp or dt.datetime.now(UTC).isoformat(),
        "sessions": [merged[key] for key in sorted(merged)],
    }
    if covered:
        payload["coverage_ranges"] = [
            {"start": start_day.isoformat(), "end": end_day.isoformat()}
            for start_day, end_day in covered
        ]
        # Envelope fields kept for readability and older readers; they are
        # NOT authoritative — coverage_ranges is.
        payload["coverage_start"] = covered[0][0].isoformat()
        payload["coverage_end"] = covered[-1][1].isoformat()
    return payload


class BrokerScheduleCalendar(VenueCalendar):
    """Calendar rebuilt from a cached broker session schedule.

    cTrader ``ProtoOAInterval`` values are seconds from Sunday 00:00 in
    ``scheduleTimeZone``; ``ProtoOAHoliday`` dates are days since 1970-01-01.
    The cache stores those raw fields plus ``fetched_at`` so historical windows
    can be approximated by the current schedule (documented tolerance).
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.name = str(payload.get("name") or "broker-schedule")
        self.source = str(payload.get("source") or "broker")
        self.schedule_timezone = str(payload.get("schedule_timezone") or "UTC")
        try:
            self.zone = ZoneInfo(self.schedule_timezone)
        except Exception:
            self.zone = ZoneInfo("UTC")
        self.intervals = [
            (int(item["start_second"]), int(item["end_second"]))
            for item in payload.get("schedule") or []
            if isinstance(item, Mapping)
            and "start_second" in item
            and "end_second" in item
        ]
        self.holidays = _normalise_holidays(payload.get("holidays") or [])
        self.grid_anchor = "utc"

    def _week_base(self, moment: dt.datetime) -> dt.datetime:
        local = moment.astimezone(self.zone)
        days_since_sunday = (local.weekday() + 1) % 7
        sunday = (local - dt.timedelta(days=days_since_sunday)).date()
        return dt.datetime.combine(sunday, dt.time(0, 0), tzinfo=self.zone)

    def _holiday_blocks(self, start: pd.Timestamp, end: pd.Timestamp) -> list[TradingInterval]:
        blocks: list[TradingInterval] = []
        if not self.holidays:
            return blocks
        cursor = start.tz_convert(self.zone).date() - dt.timedelta(days=1)
        last = end.tz_convert(self.zone).date() + dt.timedelta(days=1)
        while cursor <= last:
            for holiday in self.holidays:
                if _holiday_matches(holiday, cursor):
                    h_open, h_close = _holiday_bounds(holiday, cursor, self.zone)
                    blocks.append(TradingInterval(h_open, h_close))
            cursor += dt.timedelta(days=1)
        return blocks

    def trading_intervals(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[TradingInterval]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end <= start or not self.intervals:
            return []
        holidays = self._holiday_blocks(start, end)
        intervals: list[TradingInterval] = []
        cursor = self._week_base(start.to_pydatetime())
        last_base = self._week_base(end.to_pydatetime())
        while cursor <= last_base:
            for start_second, end_second in self.intervals:
                open_local = cursor + dt.timedelta(seconds=start_second)
                close_local = cursor + dt.timedelta(seconds=end_second)
                open_utc = pd.Timestamp(open_local.astimezone(UTC))
                close_utc = pd.Timestamp(close_local.astimezone(UTC))
                # Clip against every holiday block overlapping this interval.
                pieces = [(open_utc, close_utc)]
                for holiday_block in holidays:
                    h_open, h_close = holiday_block.open, holiday_block.close
                    nxt: list[tuple[pd.Timestamp, pd.Timestamp]] = []
                    for lo, hi in pieces:
                        if h_close <= lo or h_open >= hi:
                            nxt.append((lo, hi))
                        else:
                            if lo < h_open:
                                nxt.append((lo, h_open))
                            if hi > h_close:
                                nxt.append((h_close, hi))
                    pieces = nxt
                for lo, hi in pieces:
                    clipped_lo = max(start, lo)
                    clipped_hi = min(end, hi)
                    if clipped_hi > clipped_lo:
                        intervals.append(TradingInterval(clipped_lo, clipped_hi))
            cursor += dt.timedelta(days=7)
        intervals.sort(key=lambda iv: iv.open)
        return intervals

    # ─── Daily-bar identity ──────────────────────────────────────────────
    #
    # A broker CFD trading week (e.g. cTrader FX: Sunday ~22:00 -> Friday
    # ~22:00) is ONE continuous interval, so the base UTC-date rule would
    # count the short Sunday-evening tail as a sixth daily bar per week.
    # Real broker daily bars follow the FX convention instead: five bars per
    # week, each covering the 24h ending at the broker's daily rollover —
    # the same local time-of-day the week opens. This is the
    # ``FxWeekCalendar`` rule generalised to an arbitrary schedule timezone
    # and open time: a bar is labelled by the local date of its window's
    # close, and a stamp maps to the window containing it.

    def _daily_anchor(self) -> dt.time:
        """Local time-of-day anchoring the daily-bar grid.

        The daily rollover sits at the same local time the trading week
        opens, so the anchor is the earliest interval open's time-of-day in
        ``schedule_timezone``.
        """
        first_open = min(int(start) for start, _ in self.intervals)
        seconds = first_open % 86_400
        return dt.time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)

    def _daily_window(self, day: dt.date) -> tuple[pd.Timestamp, pd.Timestamp]:
        """``(open, close)`` UTC instants of the daily bar labelled ``day``."""
        close_local = dt.datetime.combine(day, self._daily_anchor(), tzinfo=self.zone)
        open_local = close_local - dt.timedelta(days=1)
        return (
            pd.Timestamp(open_local.astimezone(UTC)),
            pd.Timestamp(close_local.astimezone(UTC)),
        )

    def daily_bar_dates(self, start: pd.Timestamp, end: pd.Timestamp) -> set[dt.date]:
        start = _as_utc(start)
        end = _as_utc(end)
        dates: set[dt.date] = set()
        first_day = start.tz_convert(self.zone).date() - dt.timedelta(days=2)
        last_day = end.tz_convert(self.zone).date() + dt.timedelta(days=2)
        day = first_day
        while day <= last_day:
            lo, hi = self._daily_window(day)
            # A daily bar can only exist once its window has closed inside
            # the segment; a window still open at ``end`` is not expected.
            if hi <= end and hi > start:
                clipped_lo = max(lo, start)
                clipped_hi = min(hi, end)
                if self.trading_minutes_between(clipped_lo, clipped_hi) > 0:
                    dates.add(day)
            day += dt.timedelta(days=1)
        return dates

    def daily_bar_date(self, ts: pd.Timestamp) -> dt.date | None:
        ts = _as_utc(ts)
        local_day = ts.tz_convert(self.zone).date()
        for day in (
            local_day - dt.timedelta(days=2),
            local_day - dt.timedelta(days=1),
            local_day,
            local_day + dt.timedelta(days=1),
            local_day + dt.timedelta(days=2),
        ):
            lo, hi = self._daily_window(day)
            # Half-open [lo, hi): daily bars are stamped at their OPEN, so a
            # stamp exactly on the rollover belongs to the window starting
            # there. A stamp inside a window with no trading at all is
            # surplus; the same window is also absent from
            # ``daily_bar_dates``.
            if lo <= ts < hi:
                if self.trading_minutes_between(lo, hi) > 0:
                    return day
                return None
        return None

    def with_extra_closures(self, dates: Iterable[dt.date]) -> "BrokerScheduleCalendar":
        """A copy of this calendar with additional full-day closures.

        Used for the documented historical-holiday tolerance: the cached
        ``ProtoOAHoliday`` list only names the CURRENT year's non-recurring
        closures, so past dates that were really closed are indistinguishable
        from missing data. Treating plausible closure dates (the built-in
        US-market holiday model) as closed lets the coverage gate forgive
        exactly those days — and nothing else.
        """
        epoch = dt.date(1970, 1, 1)
        holidays = [dict(item) for item in self.holidays]
        existing = {
            (item.get("is_recurring"), int(item.get("date_days", -1)))
            for item in holidays
        }
        for day in dates:
            entry = {
                "date_days": (day - epoch).days,
                "is_recurring": False,
                "start_second": None,
                "end_second": None,
            }
            key = (False, int(entry["date_days"]))
            if key not in existing:
                holidays.append(entry)
                existing.add(key)
        return BrokerScheduleCalendar(
            {
                "kind": "broker_schedule",
                "name": self.name,
                "source": self.source,
                "schedule_timezone": self.schedule_timezone,
                "schedule": [
                    {"start_second": start, "end_second": end}
                    for start, end in self.intervals
                ],
                "holidays": holidays,
            }
        )


def us_holiday_tolerant_calendar(
    calendar: VenueCalendar,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> VenueCalendar:
    """Calendar treating plausible US-market closure dates as closed.

    Only ``BrokerScheduleCalendar`` gets a tolerant copy: its cached holiday
    list names only the current year's non-recurring closures, so a segment
    crossing a PAST broker holiday fails the strict gap/density checks even
    though the venue really was closed. The built-in US-exchange holiday model
    (:mod:`shared.market_calendar`) supplies the plausible closure dates —
    metals/index/energy CFDs largely follow it, and for FX it is a documented
    over-approximation (a real one-day hole on e.g. July 4 would be excused;
    multi-day holes still fail). Every other calendar type is returned
    unchanged.
    """
    if not isinstance(calendar, BrokerScheduleCalendar):
        return calendar
    first = _as_utc(start).date() - dt.timedelta(days=1)
    last = _as_utc(end).date() + dt.timedelta(days=1)
    plausible = market_calendar.holidays(first, last)
    if not plausible:
        return calendar
    tolerant = calendar.with_extra_closures(plausible)
    return tolerant


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize(UTC)
    return ts.tz_convert(UTC)


def _normalise_holidays(raw: Iterable[Any]) -> list[dict[str, Any]]:
    holidays: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        # A holiday without a parseable date is dropped, never defaulted:
        # ``date_days=0`` would make every 1 January a holiday when
        # ``is_recurring`` is set.
        raw_date = item.get("holiday_date", item.get("date_days"))
        if raw_date is None:
            continue
        start_second = item.get("start_second")
        end_second = item.get("end_second")
        try:
            day_number = int(raw_date)
            start_second = int(start_second) if start_second is not None else None
            end_second = int(end_second) if end_second is not None else None
        except (TypeError, ValueError):
            continue
        holidays.append(
            {
                "date_days": day_number,
                "start_second": start_second,
                "end_second": end_second,
                "is_recurring": bool(item.get("is_recurring", False)),
            }
        )
    return holidays


def _holiday_matches(holiday: Mapping[str, Any], day: dt.date) -> bool:
    epoch = dt.date(1970, 1, 1)
    if holiday.get("is_recurring"):
        base = epoch + dt.timedelta(days=int(holiday["date_days"]))
        return (day.month, day.day) == (base.month, base.day)
    return day == epoch + dt.timedelta(days=int(holiday["date_days"]))


def _holiday_bounds(
    holiday: Mapping[str, Any], day: dt.date, zone: ZoneInfo
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_second = holiday.get("start_second")
    end_second = holiday.get("end_second")
    day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=zone)
    if start_second is None and end_second is None:
        open_utc = pd.Timestamp(day_start.astimezone(UTC))
        return open_utc, open_utc + pd.Timedelta(days=1)
    open_local = day_start + dt.timedelta(seconds=int(start_second or 0))
    close_local = day_start + dt.timedelta(seconds=int(end_second or 86_400))
    return (
        pd.Timestamp(open_local.astimezone(UTC)),
        pd.Timestamp(close_local.astimezone(UTC)),
    )


# ─── Resolution ──────────────────────────────────────────────────────────────

FOREX_ASSET_CLASSES = frozenset({"forex", "fx", "currency"})
CRYPTO_ASSET_CLASSES = frozenset({"crypto", "cryptocurrency"})
METALS_ASSET_CLASSES = frozenset({"metals", "metal", "xau", "xag", "xpt", "xpd"})
INDEX_ASSET_CLASSES = frozenset({"index", "indices", "idx"})
FUTURES_ASSET_CLASSES = frozenset({"futures", "future"})
US_EQUITY_ASSET_CLASSES = frozenset({"equity", "stock", "stocks", "etf"})
US_EQUITY_SOURCES = frozenset({"alpaca", "dolthub", "huggingface", "test", "yfinance"})

#: Asset classes whose venue is a broker CFD schedule: no built-in session
#: model exists for them, so only a cached broker calendar (cTrader) or an
#: explicit fail-closed ``None`` can answer.
BROKER_CFD_ASSET_CLASSES = frozenset(
    METALS_ASSET_CLASSES
    | INDEX_ASSET_CLASSES
    | FUTURES_ASSET_CLASSES
    | {"energy", "volatility", "commodity", "commodities"}
)

#: ISO-4217-style currency codes for strict FX pair shapes.  Mirrors the
#: cTrader fallback table in ``live_runner/ctrader_adapter.py``; a six-letter
#: symbol is FX only when BOTH halves are currency codes, so ``NVIDIA`` or
#: ``GOLD`` can never pass.
FX_CURRENCY_CODES = frozenset(
    {
        "AUD", "BRL", "CAD", "CHF", "CNH", "CNY", "CZK", "DKK", "EUR",
        "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN",
        "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "RUB", "SAR", "SEK",
        "SGD", "THB", "TRY", "USD", "ZAR",
    }
)

#: Known crypto bases for strict crypto-pair shapes: the union of the Alpaca
#: allowlist (``live_runner/symbol_class.py``) and the cTrader fallback bases.
#: ``split_crypto_pair`` alone accepts ANY ``XXX/USD`` shape, which would
#: classify ``EUR/USD`` and ``XAUUSD`` as crypto.
CRYPTO_BASES = frozenset(
    {
        "AAVE", "ADA", "ALGO", "APT", "ARB", "ATOM", "AVAX", "BAT", "BCH",
        "BNB", "BTC", "CRV", "DASH", "DOGE", "DOT", "EOS", "ETC", "ETH",
        "FIL", "GRT", "HBAR", "INJ", "LTC", "LINK", "MANA", "MATIC", "MKR",
        "NEAR", "OP", "PEPE", "SHIB", "SNX", "SOL", "SUI", "SUSHI", "SAND",
        "TRUMP", "TRX", "UNI", "USDT", "VET", "XLM", "XMR", "XRP", "XTZ",
        "YFI", "ZEC",
    }
)

_US_EQUITY_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?$")


def _dukascopy_symbol_config() -> Mapping[str, Any]:
    """``SYMBOL_CONFIG`` from the Dukascopy downloader, or ``{}`` on failure.

    The import is lazy and failures are logged rather than swallowed: a
    missing config degrades symbol-shape resolution to the strict heuristics
    below instead of silently changing the venue.
    """
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG
    except Exception:
        logger.warning(
            "dukascopy SYMBOL_CONFIG unavailable; venue resolution uses "
            "symbol-shape heuristics only",
            exc_info=True,
        )
        return {}
    return SYMBOL_CONFIG


def _config_asset_class(symbol: str) -> str:
    """Declared asset class for ``symbol`` in Dukascopy's SYMBOL_CONFIG."""
    config = _dukascopy_symbol_config()
    entry = config.get(symbol) or config.get(symbol.replace("/", ""))
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("asset_class") or "").strip().lower()


def _is_crypto_symbol(symbol: str) -> bool:
    from shared.alpaca_symbols import split_crypto_pair

    pair = split_crypto_pair(symbol)
    return pair is not None and pair[0] in CRYPTO_BASES


def _is_fx_pair(symbol: str) -> bool:
    """Strict FX shape: ``CCYCCY`` or ``CCY/CCY`` with ISO currency codes."""
    normalized = str(symbol or "").strip().upper()
    if "/" in normalized:
        parts = normalized.split("/")
        if len(parts) == 2:
            return all(part in FX_CURRENCY_CODES for part in parts)
        return False
    if len(normalized) == 6 and normalized.isalpha():
        return (
            normalized[:3] in FX_CURRENCY_CODES
            and normalized[3:] in FX_CURRENCY_CODES
        )
    return False


def _is_us_equity_symbol(symbol: str) -> bool:
    return _US_EQUITY_TICKER_RE.fullmatch(str(symbol or "").strip().upper()) is not None


#: Only broker sources with a cache writer may appear in cache paths; the
#: component is an allowlist, not a sanitised filename, so ``../../x`` can
#: never escape ``venue_calendars/``.
_CALENDAR_CACHE_SOURCES = frozenset({"ctrader", "alpaca"})


def _calendar_cache_path(data_dir: str | Path, source: str, symbol: str) -> Path:
    src = str(source or "").strip().lower()
    if src not in _CALENDAR_CACHE_SOURCES:
        raise ValueError(f"unsupported venue calendar source: {source!r}")
    root = Path(data_dir) if data_dir else Path(".")
    safe_symbol = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in symbol.upper())
    return root / "venue_calendars" / src / f"{safe_symbol}.json"


def load_cached_calendar(
    *,
    data_dir: str | Path,
    source: str,
    symbol: str,
) -> VenueCalendar | None:
    """Load a cached broker calendar; any malformed payload fails closed.

    A missing cache file is the normal state for most symbols, not an error:
    it returns ``None`` silently. Only an existing-but-unusable file warns.
    """
    try:
        path = _calendar_cache_path(data_dir, source, symbol)
    except Exception as exc:
        logger.warning(
            "Ignoring unusable venue calendar cache for %s/%s: %s",
            source,
            symbol,
            exc,
        )
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        kind = str(payload.get("kind") or "")
        if kind == "broker_schedule":
            calendar = BrokerScheduleCalendar(payload)
            # A schedule with no usable intervals can never prove coverage:
            # fail closed rather than letting an empty calendar vacuously
            # pass every segment.
            return calendar if calendar.intervals else None
        if kind == "exchange_sessions":
            calendar = ExchangeSessionCalendar(payload)
            return calendar if calendar.sessions else None
        if kind == "alpaca_equity":
            return NyseCalendar(name="alpaca-equity")
    except Exception as exc:
        logger.warning(
            "Ignoring unusable venue calendar cache for %s/%s: %s",
            source,
            symbol,
            exc,
        )
        return None
    return None


def load_cached_calendar_payload(
    *,
    data_dir: str | Path,
    source: str,
    symbol: str,
) -> dict[str, Any] | None:
    """Raw cached calendar payload, or ``None`` when absent/unusable.

    Unlike :func:`load_cached_calendar` this does not interpret ``kind``;
    writers use it to merge an incremental fetch into the existing cache.
    """
    path = _calendar_cache_path(data_dir, source, symbol)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_cached_calendar(
    *,
    data_dir: str | Path,
    source: str,
    symbol: str,
    payload: Mapping[str, Any],
) -> Path:
    path = _calendar_cache_path(data_dir, source, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def ctrader_schedule_payload(
    *,
    symbol: str,
    schedule: Sequence[Any],
    schedule_timezone: str,
    holidays: Sequence[Any],
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "broker_schedule",
        "name": f"ctrader-{symbol.upper()}",
        "source": "ctrader",
        "symbol": symbol.upper(),
        "schedule_timezone": schedule_timezone or "UTC",
        "fetched_at": fetched_at or dt.datetime.now(UTC).isoformat(),
        "schedule": [
            {"start_second": int(iv.startSecond), "end_second": int(iv.endSecond)}
            for iv in schedule
        ],
        "holidays": [
            {
                "holiday_date": int(h.holidayDate),
                "is_recurring": bool(h.isRecurring),
                "start_second": int(h.startSecond) if h.HasField("startSecond") else None,
                "end_second": int(h.endSecond) if h.HasField("endSecond") else None,
            }
            for h in holidays
        ],
    }


def resolve_venue_calendar(
    *,
    symbol: str,
    timeframe: str = "",
    data_dir: str | Path = "",
    asset_class: str | None = None,
    source: str | None = None,
) -> VenueCalendar | None:
    """Resolve the authoritative session calendar for a segment.

    Precedence: explicit ``asset_class``/``source`` metadata and broker cache
    entries beat symbol-shape heuristics, which are strict and only resolve
    unambiguous shapes (known crypto bases, ISO FX pairs, US equity tickers on
    a known equity source).  ``None`` means the venue is unknown: callers must
    fail closed and must never treat it as 24/7.
    """
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return None

    asset = str(asset_class or "").strip().lower()
    src = str(source or "").strip().lower()

    # 1. Broker caches are consulted before any shape heuristic.  The cTrader
    #    schedule is authoritative for cTrader-sourced data AND for broker-CFD
    #    asset classes (metals/indices/futures), which have no built-in model
    #    regardless of which vendor supplied the bars.  The Alpaca /v2/calendar
    #    cache applies to US-equity data from the Alpaca source.
    if data_dir:
        if src == "ctrader":
            cached = load_cached_calendar(
                data_dir=data_dir, source="ctrader", symbol=normalized
            )
            if cached is not None:
                return cached
        elif src == "alpaca" and (
            asset in US_EQUITY_ASSET_CLASSES
            or (not asset and _is_us_equity_symbol(normalized))
        ):
            # The exchange-session cache only ever describes US equity
            # venues: without asset metadata a crypto/FX-shaped symbol must
            # not pick up NYSE sessions from a stray cache entry.
            cached = load_cached_calendar(
                data_dir=data_dir, source="alpaca", symbol=normalized
            )
            if cached is not None:
                return cached
        if asset in BROKER_CFD_ASSET_CLASSES or (
            not asset and _config_asset_class(normalized) in BROKER_CFD_ASSET_CLASSES
        ):
            cached = load_cached_calendar(
                data_dir=data_dir, source="ctrader", symbol=normalized
            )
            if cached is not None:
                return cached

    # 2. Explicit asset_class metadata.
    if asset:
        if asset in CRYPTO_ASSET_CLASSES:
            return ContinuousCalendar()
        if asset in FOREX_ASSET_CLASSES:
            return FxWeekCalendar()
        if asset in US_EQUITY_ASSET_CLASSES:
            if not src or src in US_EQUITY_SOURCES:
                return NyseCalendar()
            return None
        # Broker-CFD and unrecognised asset classes: cache was already tried.
        return None

    # 3. No asset_class: Dukascopy SYMBOL_CONFIG metadata, then strict shapes.
    config_asset = _config_asset_class(normalized)
    if config_asset in FOREX_ASSET_CLASSES or _is_fx_pair(normalized):
        return FxWeekCalendar()
    if config_asset in CRYPTO_ASSET_CLASSES or _is_crypto_symbol(normalized):
        return ContinuousCalendar()
    if config_asset in BROKER_CFD_ASSET_CLASSES:
        return None  # no built-in model; cTrader cache already consulted
    if src in US_EQUITY_SOURCES and _is_us_equity_symbol(normalized):
        return NyseCalendar()
    return None

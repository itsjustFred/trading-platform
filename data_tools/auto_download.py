"""Ensure OHLCV data exists before runner scripts start a backtest."""
from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow.parquet as pq

from data_tools.path_safety import ensure_dataset_path_within, validate_dataset_component
from shared import market_calendar
from shared.constants import DATA_DIR, normalize_timeframe

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
US_INTRADAY_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h"}
DEFAULT_HF_INTRADAY_COVERAGE_END = "2025-07-31"
DEFAULT_ALPACA_INTRADAY_START = "2025-07-01"
_US_STOCK_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

# Parent Decision Record (2026-08-31): coverage checks fail closed on
# significant internal gaps. Small missing-bar windows may warn; larger gaps
# fail. The parent issue's figures are INTERNAL_GAP_FAIL_DAYS (3 calendar days)
# and INTERNAL_GAP_WARN_DAYS (1 calendar day), and those remain the base.
#
# They are the base, not the whole rule. `scaled_gap_threshold_days` scales the
# threshold by the timeframe's own bar spacing, because a flat 3-day budget flags
# every bar of a weekly series as a hole and no weekly or monthly backtest could
# pass the gate at all. What is actually applied:
#
#     1m .. 1d   3 days   (unchanged from the parent record)
#     1w        21 days   (3 x the 7-day bar spacing)
#     1mo       90 days   (3 x the 30-day bar spacing)
#
# A separate calendar-derived allowance covers scheduled closures; see
# `_gap_is_market_closure` for why it only speaks for US equity venues. This
# comment previously stated a flat 3 days for every timeframe, which was wrong for
# 1w and 1mo by a factor of seven and thirty - anyone reasoning about the gate from
# the comment rather than from `scaled_gap_threshold_days` would have modelled it
# incorrectly.
#
# Thresholds deliberately count calendar days, not business days (the parent
# record states the fail threshold in calendar days): a regular FX weekend
# (~2 days) therefore warns, and only holes beyond 3 calendar days fail.
INTERNAL_GAP_FAIL_DAYS = 3
INTERNAL_GAP_WARN_DAYS = 1

# #86: segment-coverage rule replacing the 1-bar check. Expected bars are
# derived from timeframe x TRADING minutes in the segment (Saturdays and
# Sundays excluded — no venue in this repo prints Sat bars, so wall-clock
# counting would reject every multi-week segment); >=95% of expected bars
# must be present and no single bar-to-bar gap may exceed 3x the timeframe
# in trading minutes, regardless of density.
TIMEFRAME_MINUTES: dict[str, float] = {
    "1m": 1.0,
    "5m": 5.0,
    "15m": 15.0,
    "30m": 30.0,
    "1h": 60.0,
    "4h": 240.0,
    "1d": 1440.0,
    "1w": 10080.0,
}
SEGMENT_MIN_DENSITY = 0.95
SEGMENT_MAX_GAP_MULTIPLIER = 3.0
US_EQUITY_ASSET_CLASSES = frozenset(
    {"equity", "stock", "stocks", "etf", "index"}
)
US_EQUITY_CALENDAR_SOURCES = frozenset(
    {"alpaca", "dolthub", "huggingface", "test", "yfinance"}
)


@lru_cache(maxsize=None)
def _nyse_holidays(year: int) -> frozenset[str]:
    def observed(month: int, day: int) -> date:
        day_date = date(year, month, day)
        if day_date.weekday() == 5 and (month, day) != (1, 1):
            return day_date - timedelta(days=1)
        if day_date.weekday() == 6:
            return day_date + timedelta(days=1)
        return day_date

    def nth_weekday(month: int, weekday: int, occurrence: int) -> date:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (occurrence - 1))

    def last_weekday(month: int, weekday: int) -> date:
        probe = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if month == 12
            else date(year, month + 1, 1) - timedelta(days=1)
        )
        offset = (probe.weekday() - weekday) % 7
        return probe - timedelta(days=offset)

    easter = _easter_sunday(year)
    holidays = {
        observed(1, 1),
        nth_weekday(1, 0, 3),
        nth_weekday(2, 0, 3),
        last_weekday(5, 0),
        observed(7, 4),
        nth_weekday(9, 0, 1),
        nth_weekday(11, 3, 4),
        observed(12, 25),
        date(year, *easter) - timedelta(days=2),
    }
    if year >= 2022:
        holidays.add(observed(6, 19))
    return frozenset(day.isoformat() for day in holidays)


def _easter_sunday(year: int) -> tuple[int, int]:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_correction = (32 + 2 * e + 2 * i - h - k) % 7
    month_adjustment = (a + 11 * h + 22 * weekday_correction) // 451
    month = (h + weekday_correction - 7 * month_adjustment + 114) // 31
    day = ((h + weekday_correction - 7 * month_adjustment + 114) % 31) + 1
    return month, day


def is_us_equity_holiday(value: object) -> bool:
    day = pd.Timestamp(value).date()
    return any(
        day.isoformat() in _nyse_holidays(adjacent_year)
        for adjacent_year in (day.year - 1, day.year, day.year + 1)
    )


def _weekend_overlap_minutes(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> float:
    """Minutes of ``[start_ts, end_ts]`` falling on Saturday/Sunday (UTC)."""
    if end_ts <= start_ts:
        return 0.0
    total = 0.0
    cursor = start_ts.normalize()
    while cursor <= end_ts:
        if cursor.weekday() >= 5:
            day_start = max(start_ts, cursor)
            day_end = min(end_ts, cursor + pd.Timedelta(days=1))
            if day_end > day_start:
                total += (day_end - day_start).total_seconds() / 60.0
        cursor += pd.Timedelta(days=1)
    return total


def _holiday_overlap_minutes(
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    holidays: frozenset[date],
) -> float:
    if end_ts <= start_ts:
        return 0.0
    total = 0.0
    cursor = start_ts.normalize()
    while cursor <= end_ts.normalize():
        if cursor.weekday() < 5 and cursor.date() in holidays:
            day_start = max(start_ts, cursor)
            day_end = min(end_ts, cursor + pd.Timedelta(days=1))
            if day_end > day_start:
                total += (day_end - day_start).total_seconds() / 60.0
        cursor += pd.Timedelta(days=1)
    return total


def _trading_minutes_between(
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    holidays: frozenset[date] = frozenset(),
) -> float:
    """Wall-clock minutes minus full-weekend overlap (CodeRabbit round)."""
    total = max((end_ts - start_ts).total_seconds() / 60.0, 0.0)
    weekend = _weekend_overlap_minutes(start_ts, end_ts)
    holiday = _holiday_overlap_minutes(start_ts, end_ts, holidays)
    return max(total - weekend - holiday, 0.0)


def _is_trading_instant(
    ts: pd.Timestamp,
    step_minutes: float,
    holidays: frozenset[date] = frozenset(),
) -> bool:
    """Whether a bar grid instant falls in trading time."""
    if step_minutes >= 10080.0:
        return True
    return ts.weekday() < 5 and ts.date() not in holidays


def _parse_session_clock(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("session clock must not be empty")
    if text in {"24:00", "24:00:00"}:
        return 1440
    parsed = pd.to_datetime(f"2000-01-01 {text}", errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid session clock: {value!r}")
    return int(parsed.hour * 60 + parsed.minute)


def _explicit_session_window(
    session: Mapping[str, object] | tuple[str, str] | None,
    session_start: str | None,
    session_end: str | None,
) -> tuple[int, int] | None:
    start_value: object = session_start
    end_value: object = session_end
    if isinstance(session, Mapping):
        start_value = session.get("start", session.get("session_start", session.get("open")))
        end_value = session.get("end", session.get("session_end", session.get("close")))
    elif isinstance(session, (tuple, list)) and len(session) == 2:
        start_value, end_value = session
    if start_value is None and end_value is None:
        return None
    if start_value is None or end_value is None:
        raise ValueError("session requires both start and end")
    start_minutes = _parse_session_clock(start_value)
    end_minutes = _parse_session_clock(end_value)
    if end_minutes <= start_minutes:
        raise ValueError("session end must be after session start")
    return start_minutes, end_minutes


def _observed_session_window(
    stamps: list[pd.Timestamp],
    step_minutes: float,
    *,
    start_ts: pd.Timestamp | None = None,
    end_ts: pd.Timestamp | None = None,
) -> tuple[int, int] | None:
    if step_minutes >= 1440.0 or len(stamps) < 4:
        return None
    by_day: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    for stamp in stamps:
        by_day.setdefault(stamp.normalize(), []).append(stamp)
    if len(by_day) < 2 or max(len(rows) for rows in by_day.values()) < 2:
        return None
    day_windows = [
        (
            day,
            min(int(stamp.hour * 60 + stamp.minute) for stamp in rows),
            max(int(stamp.hour * 60 + stamp.minute) for stamp in rows)
            + int(step_minutes),
        )
        for day, rows in sorted(by_day.items())
    ]
    windows = {(start, end) for _, start, end in day_windows}
    if len(windows) == 1:
        start_minutes, end_minutes = next(iter(windows))
    else:
        if start_ts is None or end_ts is None or len(day_windows) < 2:
            return None
        first_day = day_windows[0][0]
        last_day = day_windows[-1][0]
        interior_windows = {
            (start, end)
            for day, start, end in day_windows
            if first_day < day < last_day
        }
        if len(interior_windows) > 1:
            return None
        if interior_windows:
            start_minutes, end_minutes = next(iter(interior_windows))
        else:
            start_minutes = day_windows[-1][1]
            end_minutes = day_windows[0][2]
        step = pd.Timedelta(minutes=int(step_minutes))
        for day, window_start, window_end in day_windows:
            if window_start != start_minutes and not (
                day == first_day
                and start_ts.normalize() == first_day
                and start_ts > day + pd.Timedelta(minutes=start_minutes)
                and day + pd.Timedelta(minutes=window_start)
                <= start_ts
                <= day + pd.Timedelta(minutes=window_start) + step
            ):
                return None
            if window_end != end_minutes and not (
                day == last_day
                and end_ts.normalize() == last_day
                and end_ts < day + pd.Timedelta(minutes=end_minutes)
                and day + pd.Timedelta(minutes=window_end - int(step_minutes))
                <= end_ts
                <= day + pd.Timedelta(minutes=window_end)
            ):
                return None
    if end_minutes - start_minutes < step_minutes * 3:
        return None
    return start_minutes, end_minutes


def _session_trading_minutes_between(
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    session_window: tuple[int, int],
    holidays: frozenset[date] = frozenset(),
) -> float:
    if end_ts <= start_ts:
        return 0.0
    start_minutes, end_minutes = session_window
    total = 0.0
    cursor = start_ts.normalize()
    while cursor <= end_ts.normalize():
        if cursor.weekday() < 5 and cursor.date() not in holidays:
            session_start = cursor + pd.Timedelta(minutes=start_minutes)
            session_end = cursor + pd.Timedelta(minutes=end_minutes)
            overlap_start = max(start_ts, session_start)
            overlap_end = min(end_ts, session_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60.0
        cursor += pd.Timedelta(days=1)
    return total


def _expected_trading_bars(
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    step_minutes: float,
    session_window: tuple[int, int] | None = None,
    holidays: frozenset[date] = frozenset(),
) -> int:
    """Count expected bars on the venue/session grid when it is known."""
    if step_minutes <= 0:
        return 0
    if session_window is None:
        span_minutes = (end_ts - start_ts).total_seconds() / 60.0
        steps = int(span_minutes // step_minutes)
        return sum(
            1
            for k in range(steps + 1)
            if _is_trading_instant(
                start_ts + pd.Timedelta(minutes=k * step_minutes),
                step_minutes,
                holidays,
            )
        )
    start_minutes, end_minutes = session_window
    count = 0
    cursor = start_ts.normalize()
    while cursor <= end_ts.normalize():
        if cursor.weekday() < 5 and cursor.date() not in holidays:
            day_start = cursor + pd.Timedelta(minutes=start_minutes)
            day_end = cursor + pd.Timedelta(minutes=end_minutes)
            first = max(start_ts, day_start)
            last = min(end_ts, day_end)
            if last >= first:
                offset = math.ceil(
                    (first - day_start).total_seconds() / (step_minutes * 60)
                )
                instant = day_start + pd.Timedelta(minutes=offset * step_minutes)
                while instant <= last and instant < day_end:
                    count += 1
                    instant += pd.Timedelta(minutes=step_minutes)
        cursor += pd.Timedelta(days=1)
    return count


@dataclass
class InternalGap:
    """A hole between two consecutive bars inside the stored range."""

    start: str
    end: str
    gap_days: float

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "gap_days": round(self.gap_days, 2),
            # The rounded `gap_days` above cannot resolve a one-minute bar
            # (1/1440 of a day is 0.0007, and the report carries two decimals),
            # and the closure allowance needs a one-bar margin. The exact width
            # travels alongside it so the calendar check never has to work from
            # a rounded number.
            "gap_seconds": round(self.gap_days * 86_400.0),
        }


def _coverage_window_from_env(env_value: str | None, default: str) -> str:
    text = str(env_value or "").strip()
    if not text:
        return default
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        logger.warning("Ignoring invalid coverage window value %r; using %s", env_value, default)
        return default
    return parsed.date().isoformat()


def reload_coverage_windows() -> tuple[str, str]:
    """(Re)load the vendor coverage windows from the environment.

    Called at import and after tests change the env, so the bounds stay
    configurable without a code change when a vendor extends coverage.
    """
    global HF_INTRADAY_COVERAGE_END, ALPACA_INTRADAY_START
    HF_INTRADAY_COVERAGE_END = _coverage_window_from_env(
        os.environ.get("HF_INTRADAY_COVERAGE_END"),
        DEFAULT_HF_INTRADAY_COVERAGE_END,
    )
    ALPACA_INTRADAY_START = _coverage_window_from_env(
        os.environ.get("ALPACA_INTRADAY_START"),
        DEFAULT_ALPACA_INTRADAY_START,
    )
    return HF_INTRADAY_COVERAGE_END, ALPACA_INTRADAY_START


reload_coverage_windows()


@dataclass(frozen=True)
class DataRequirement:
    symbol: str
    timeframe: str
    start: str
    end: str


def ensure_backtest_data(
    *,
    symbols: list[str],
    timeframe: str,
    start: str = "",
    end: str = "",
    data_dir: str | Path = "",
    modeling_resolution: str = "main_timeframe",
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> list[dict[str, Any]]:
    """Download missing OHLCV files/ranges needed by a backtest.

    Returns a list of downloaded requirement payloads. If everything is already
    covered locally, returns an empty list.
    """
    data_root = _resolve_data_root(data_dir)
    requirements = _build_requirements(
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        modeling_resolution=modeling_resolution,
    )
    if not requirements:
        return []

    missing = [
        requirement
        for requirement in requirements
        if not _has_required_coverage(data_root, requirement)
    ]
    if not missing:
        _fail_on_internal_gaps(data_root, requirements)
        return []

    downloaded: list[dict[str, Any]] = []
    total = len(missing)
    for index, requirement in enumerate(missing, start=1):
        if progress_callback is not None:
            progress_callback(
                5.0 + ((index - 1) / total) * 35.0,
                f"Downloading data for {requirement.symbol} {requirement.timeframe}...",
            )

        source_plan = _auto_download_source_plan(
            symbol=requirement.symbol,
            timeframe=requirement.timeframe,
            start=requirement.start or None,
            end=requirement.end or None,
        )
        for source_index, step in enumerate(source_plan, start=1):
            source = str(step.get("source") or "").strip().lower()
            step_start = str(step.get("start") or "").strip() or None
            step_end = str(step.get("end") or "").strip() or None
            if log_callback is not None:
                suffix = f" [{source_index}/{len(source_plan)}]" if len(source_plan) > 1 else ""
                coverage_note = (
                    " [coverage unknown: no configured vendor window covers this range]"
                    if str(step.get("coverage") or "") == "unknown"
                    else ""
                )
                log_callback(
                    f"Auto-download{suffix}: {requirement.symbol} {requirement.timeframe} "
                    f"via {source} ({step_start or 'auto'} -> {step_end or 'latest'})"
                    f"{coverage_note}"
                )
            _download_missing_ohlcv_data(
                data_root=data_root,
                symbol=requirement.symbol,
                timeframe=requirement.timeframe,
                start=step_start,
                end=step_end,
                source=source,
            )

        if not _has_required_coverage(data_root, requirement):
            raise RuntimeError(
                "Auto-download did not provide required OHLCV coverage for "
                f"{requirement.symbol} {requirement.timeframe} "
                f"({requirement.start or 'auto'} -> {requirement.end or 'latest'})"
            )
        payload = {
            "symbol": requirement.symbol,
            "timeframe": requirement.timeframe,
            "start": requirement.start,
            "end": requirement.end,
            "sources": [step.get("source") for step in source_plan],
        }
        downloaded.append(payload)

        if progress_callback is not None:
            progress_callback(
                5.0 + (index / total) * 35.0,
                f"Downloaded data for {requirement.symbol} {requirement.timeframe}",
            )

    # C5: global min/max bounds are not coverage — fail on internal gaps
    # the bounds check cannot see (DV-B15). Verified for every requirement,
    # not just freshly downloaded ones.
    _fail_on_internal_gaps(data_root, requirements)

    _invalidate_catalog_cache(data_root)
    return downloaded


def _fail_on_internal_gaps(data_root: Path, requirements: list[DataRequirement]) -> None:
    """Raise when any requirement's stored range hides an internal hole."""
    for requirement in requirements:
        coverage_report = analyze_ohlcv_coverage(data_root, requirement)
        if coverage_report.get("status") == "failed":
            raise RuntimeError(
                "OHLCV coverage has internal gaps for "
                f"{requirement.symbol} {requirement.timeframe}: "
                f"{coverage_report.get('gaps') or coverage_report.get('error')}"
            )


def _resolve_data_root(data_dir: str | Path) -> Path:
    if data_dir:
        return Path(data_dir).expanduser().resolve()
    return DATA_DIR


# A bar-to-bar hole only means something when it is far larger than the normal
# spacing of the series. Judging holes by an absolute calendar-day count instead
# misreports perfectly regular data: weekly bars sit 7 days apart, so every
# single one looked like a week-long hole and weekly coverage always failed.
# Sub-daily timeframes keep the fixed floor, because an overnight or weekend
# closure is legitimately longer than several bars of an intraday series.
GAP_SPACING_MULTIPLE = 3.0
MIN_INTERNAL_GAP_THRESHOLD_DAYS = INTERNAL_GAP_WARN_DAYS


def long_weekend_gap_days() -> float:
    """Longest bar-to-bar closure the exchange calendar produces, in days.

    This replaces a flat ``LONG_WEEKEND_GAP_DAYS = 4.0`` that was applied as a
    *floor* over the failing threshold, which meant it overrode the base
    threshold for every timeframe rather than adding anything to it. The
    consequences were all in the permissive direction:

    * 1m, 4h and 1d data all failed at 4.00 days instead of 3.00, so a downloader
      that silently failed for 3.6 days passed the gate and the backtest ran
      over forward-filled prices and reported a Sharpe.
    * Nothing real was bought by it. An ordinary long weekend closes from
      Friday's close to Monday's open, which the *3.0-day base already covers*
      (2.73 days of elapsed time on the US equity grid). The next closure shape
      up -- a Monday or Friday holiday, at 3.73 days -- is the one that needed
      help, and 4.0 does not reach it either. So the floor sat in the dead band
      between two closure shapes, and excused misaligned holes instead.

    The allowance is now a separate, calendar-derived term and it is applied
    per hole by :func:`market_calendar.closure_explains_hole`, not as a global
    minimum: a hole the calendar explains is market behaviour, and a hole of the
    same length that straddles a live session is a data hole. That distinction
    cannot be made by any single length budget, which is why the 4.0-day floor
    could not simply be re-tuned.
    """
    return float(market_calendar.longest_scheduled_closure_days())


# Kept as a module constant for callers that only need the number. Assigned at
# import because the calendar is procedural and cheap to evaluate once, and read
# by `closure_hole_allowance_seconds`.
LONG_WEEKEND_GAP_DAYS = long_weekend_gap_days()


#: Unit-first sub-daily labels, which the numeric-prefix pattern cannot express.
#: Values are the nominal bar spacing in days.
_SUBD_DAILY_TIMEFRAME_NOMINAL_DAYS: dict[str, float] = {
    "s1": 1.0 / 86_400.0,
    "tick": 1.0 / 86_400.0,
}


def timeframe_nominal_days(timeframe: str | None) -> float:
    """Nominal calendar spacing of one bar in ``timeframe``.

    The numeric prefix is part of the timeframe: ``5m`` bars are five minutes
    apart and ``4w`` bars are four weeks apart, so ignoring it would size the
    allowed hole against the wrong spacing.
    """
    normalized = str(timeframe or "").strip().lower()
    # The repo's own sub-daily labels put the unit first, so the numeric-prefix regex
    # below never matched them and they fell through to the `1.0` daily fallback -
    # sizing `s1` and `tick` as *one day* when they are one second. That silently
    # loosened their gap threshold 3x (1.0 -> 3.0 days), so a two-day hole in tick
    # data was reported as `ok` rather than as a warning. These are real labels:
    # `KNOWN_TIMEFRAMES` in consolidate_parquets and the viewer both spell them this
    # way, and the grid downloader lists them by name in UNSUPPORTED_TIMEFRAMES.
    #
    # Not a backtest hazard on its own - `TIMEFRAME_MINUTES` has no entry for either,
    # so `has_sufficient_segment_coverage` returns False and they cannot be traded -
    # but a coverage report that says "ok" about a two-day hole is a report nobody
    # should believe.
    if normalized in _SUBD_DAILY_TIMEFRAME_NOMINAL_DAYS:
        return _SUBD_DAILY_TIMEFRAME_NOMINAL_DAYS[normalized]
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(mo|[a-z]+)$", normalized)
    if match is None:
        return 1.0
    try:
        count = float(match.group(1))
    except ValueError:  # pragma: no cover - guarded by the regex above
        return 1.0
    unit = match.group(2)
    if unit == "mo":
        return count * 30.0
    if unit == "w":
        return count * 7.0
    if unit == "d":
        return count
    if unit == "h":
        return count / 24.0
    if unit == "m":
        return count / 1440.0
    if unit == "s":
        return count / 86_400.0
    return count


def scaled_gap_threshold_days(
    timeframe: str | None,
    base_days: float,
    *,
    allow_long_weekend: bool = False,
) -> float:
    """Length budget: the longest hole in ``timeframe`` that still FAILS coverage.

    Widened for long-spaced series because a weekly bar sits 7 days apart, so a
    fixed 3-day allowance reported every weekly bar as a hole.

    ``allow_long_weekend`` used to raise a *floor* to 4 days, which meant it
    replaced the base threshold instead of adding to it and silently loosened
    every timeframe to 4 days. It no longer changes the length budget at all:
    the long-weekend allowance is now applied per hole, by
    :func:`market_calendar.closure_explains_hole`, so a hole the exchange
    calendar explains is excused while a hole of the same length straddling a
    live session is reported. The parameter is kept so the call sites that
    distinguish the failing threshold from the warning threshold stay explicit
    about which one they are asking for.
    """
    scaled = timeframe_nominal_days(timeframe) * GAP_SPACING_MULTIPLE
    return max(float(base_days), scaled, MIN_INTERNAL_GAP_THRESHOLD_DAYS)


def _fail_on_internal_gaps(data_root: Path, requirements: list[DataRequirement]) -> None:
    for requirement in requirements:
        report = analyze_ohlcv_coverage(data_root, requirement)
        if report.get("status") == "failed":
            raise RuntimeError(
                "OHLCV coverage has internal gaps for "
                f"{requirement.symbol} {requirement.timeframe}: "
                f"{report.get('gaps') or report.get('error')}"
            )


def _parquet_has_bar_in_range(path: Path, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> bool:
    """Return whether the parquet at ``path`` has any timestamp within ``[start_ts, end_ts]``."""
    try:
        frame = pd.read_parquet(path, columns=["timestamp"])
    except Exception as exc:
        logger.warning("Could not inspect OHLCV timestamps for %s: %s", path, exc)
        return False
    if "timestamp" not in frame.columns or frame.empty:
        return False
    timestamps = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce", format="mixed"
    ).dropna()
    return ((timestamps >= start_ts) & (timestamps <= end_ts)).any()


def _timestamp_column_index(parquet_file: "pq.ParquetFile") -> int | None:
    """Index of the (case-insensitive) ``timestamp`` column, or None if absent."""
    columns = list(parquet_file.schema_arrow.names)
    lowered = [column.lower() for column in columns]
    if "timestamp" not in lowered:
        return None
    return lowered.index("timestamp")


def _stat_boundary(
    metadata: "pq.FileMetaData", column_index: int, row_group_index: int, boundary: str
) -> pd.Timestamp | None:
    """Min/max timestamp statistic of one row group, or None when unavailable."""
    stats = metadata.row_group(row_group_index).column(column_index).statistics
    if stats is None or not getattr(stats, "has_min_max", False):
        return None
    value = stats.min if boundary == "min" else stats.max
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _row_group_edge_timestamp(
    parquet_file: "pq.ParquetFile",
    columns: list[str],
    column_index: int,
    row_group_index: int,
    *,
    last: bool,
) -> pd.Timestamp | None:
    """First (``last=False``) or last (``last=True``) timestamp of a row group."""
    group = parquet_file.read_row_group(row_group_index, columns=[columns[column_index]])
    if not group.num_rows:
        return None
    position = group.num_rows - 1 if last else 0
    parsed = pd.to_datetime(group.column(0)[position].as_py(), utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def ohlcv_timestamp_bounds(path: Path | str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Public entry point for :func:`_parquet_timestamp_bounds`.

    Returns ``(first_timestamp, last_timestamp)`` of a stored OHLCV parquet, or
    None when the bounds cannot be established (unreadable file, no timestamp
    column, no rows). Callers that need to know *when* a symbol's data exists —
    e.g. the universe provenance / survivorship diagnostic — read the bounds
    through this name; None means "unknown", never "no data".
    """
    return _parquet_timestamp_bounds(Path(path))


def _parquet_timestamp_bounds(path: Path) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Read the min/max timestamp of a parquet without materializing the column.

    Row-group min/max statistics are used when present; only the boundary row
    groups are read as a fallback. This keeps repeated coverage checks cheap
    instead of scanning the full timestamp column every time (DV-B17).

    Statistics are only a *lower bound* for the true max: a writer that
    appended rows out of order (or disabled statistics) reports a max below
    the newest stored row. The last-group statistic is therefore
    cross-checked against the actual last row of that group — the same rule
    as :meth:`data_tools.downloaders.base.Downloader._read_latest_timestamp`.
    """
    try:
        parquet_file = pq.ParquetFile(path)
    except Exception as exc:
        logger.warning("Could not inspect OHLCV timestamps for %s: %s", path, exc)
        return None

    column_index = _timestamp_column_index(parquet_file)
    if column_index is None:
        return None
    columns = list(parquet_file.schema_arrow.names)
    metadata = parquet_file.metadata
    if metadata is None or metadata.num_rows == 0 or metadata.num_row_groups == 0:
        return None

    last_group = metadata.num_row_groups - 1
    start = _stat_boundary(metadata, column_index, 0, "min")
    end = _stat_boundary(metadata, column_index, last_group, "max")

    # The last-group statistic understates the newest row for out-of-order
    # appends, so it is cross-checked against the group's actual last row.
    row_end = _row_group_edge_timestamp(
        parquet_file, columns, column_index, last_group, last=True
    )
    if row_end is not None and (end is None or row_end > end):
        end = row_end

    if start is None:
        start = _row_group_edge_timestamp(parquet_file, columns, column_index, 0, last=False)

    if start is None or end is None:
        return None
    return start, end


def _find_gaps_inside_file(
    path: Path, *, threshold_days: float = INTERNAL_GAP_WARN_DAYS
) -> list[InternalGap]:
    """Find the largest bar-to-bar hole inside one parquet file.

    Internal holes are not derivable from row-group min/max statistics, so
    this reads the timestamp column of **every** row group. Cost is
    O(file): fine for coverage checks at download time, not for hot paths.
    """
    try:
        parquet_file = pq.ParquetFile(path)
    except Exception as exc:
        logger.warning("Could not inspect %s for internal gaps: %s", path, exc)
        return []

    metadata = parquet_file.metadata
    columns = list(parquet_file.schema_arrow.names)
    lowered = [column.lower() for column in columns]
    if "timestamp" not in lowered or metadata is None or metadata.num_row_groups == 0:
        return []
    column_index = lowered.index("timestamp")
    threshold = pd.Timedelta(days=threshold_days)

    timestamps: list[pd.Timestamp] = []
    for group_index in range(metadata.num_row_groups):
        group = parquet_file.read_row_group(group_index, columns=[columns[column_index]])
        parsed = pd.to_datetime(group.column(0).to_pandas(), utc=True, errors="coerce").dropna()
        timestamps.extend(pd.Timestamp(value) for value in parsed)

    if len(timestamps) < 2:
        return []

    series = pd.Series(pd.to_datetime(sorted(set(timestamps)), utc=True))
    diffs = series.diff().dropna()
    large = diffs[diffs > threshold]
    gaps: list[InternalGap] = []
    for index in large.index:
        gaps.append(
            InternalGap(
                start=series[index - 1].date().isoformat(),
                end=series[index].date().isoformat(),
                gap_days=diffs[index].total_seconds() / 86_400,
            )
        )

    # Report the largest hole per file so callers get a single actionable gap.
    largest = max(gaps, key=lambda gap: gap.gap_days, default=None)
    return [largest] if largest is not None else []


def find_internal_gaps(
    symbol_dir: Path,
    timeframe: str | None = None,
    *,
    threshold_days: float | None = None,
) -> list[dict[str, object]]:
    """Detect holes *inside* the stored timestamp range of one symbol directory.

    Coverage based on global min/max passes files that simply stop in the
    middle (DV-B15). This compares the bar-to-bar boundaries of every parquet
    that belongs to the symbol: adjacent files (segmented/monthly storage) and
    the bars inside one file. Only holes longer than ``threshold_days``
    (default: ``INTERNAL_GAP_WARN_DAYS``, 1 calendar day) is reported, and only
    the **largest** one — so a caller gets a single actionable gap, but a
    second, smaller hole in the same symbol directory is not surfaced and the
    coverage verdict is computed from the largest alone. That is a known
    limitation rather than an accident: removing it means returning every hole
    above the threshold. — gaps are measured in calendar
    days, so a regular weekend (~2 days) is reported as a warning.
    """
    def _matches_timeframe(path: Path) -> bool:
        if timeframe is None:
            return True
        stem = path.stem.lower()
        normalized = timeframe.lower()
        return (
            stem == normalized
            or stem.startswith(f"{normalized}_")
            or stem.startswith(f"{normalized}-")
        )

    if threshold_days is None:
        # Without a timeframe there is no nominal spacing to scale against, so
        # widening would only hide real holes. Keep the fixed floor.
        threshold_days = (
            scaled_gap_threshold_days(timeframe, INTERNAL_GAP_WARN_DAYS)
            if timeframe
            else INTERNAL_GAP_WARN_DAYS
        )

    parquet_paths = sorted(
        path for path in symbol_dir.rglob("*.parquet") if _matches_timeframe(path)
    )
    bounds: list[tuple[pd.Timestamp, pd.Timestamp, Path]] = []
    for path in parquet_paths:
        file_bounds = _parquet_timestamp_bounds(path)
        if file_bounds is not None:
            bounds.append((file_bounds[0], file_bounds[1], path))
    if not bounds:
        return []

    gaps: list[InternalGap] = []

    # Gap between the bars of adjacent files (e.g. monthly/segmented storage).
    bounds.sort(key=lambda item: item[0])
    for (_, previous_end, _), (next_start, _, _) in zip(bounds, bounds[1:]):
        gap_days = (next_start - previous_end).total_seconds() / 86_400
        if gap_days > threshold_days:
            gaps.append(
                InternalGap(
                    start=previous_end.date().isoformat(),
                    end=next_start.date().isoformat(),
                    gap_days=gap_days,
                )
            )

    # Largest hole inside each file; see _find_gaps_inside_file for the cost.
    for _, _, path in bounds:
        file_gaps = _find_gaps_inside_file(path, threshold_days=threshold_days)
        gaps.extend(file_gaps)

    largest = max(gaps, key=lambda gap: gap.gap_days, default=None)
    if largest is not None:
        return [largest.to_dict()]
    return []


def _contained_rglob(root: Path, pattern: str) -> Iterator[Path]:
    for path in root.rglob(pattern):
        try:
            ensure_dataset_path_within(root, path)
        except ValueError:
            continue
        yield path


def has_ohlcv_bars_in_range(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    data_dir: str | Path = "",
) -> bool:
    """Return whether canonical OHLCV data contains a valid bar in ``[start, end]``.

    NOTE (#86): presence-only — a single bar in a month-long segment still
    returns True. Backtest/optimizer segment gates must use
    :func:`has_sufficient_segment_coverage` instead; this helper stays for
    freshness checks (e.g. weekly refresh) where presence is the question.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_symbol or not normalized_timeframe:
        return False

    start_ts = pd.to_datetime(str(start or ""), utc=True, errors="coerce")
    end_ts = pd.to_datetime(str(end or ""), utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        return False

    try:
        validate_dataset_component(normalized_symbol, name="symbol")
        validate_dataset_component(normalized_timeframe, name="timeframe")
    except ValueError:
        return False

    ohlcv_root = _resolve_data_root(data_dir) / "ohlcv"
    matches = sorted(
        path
        for path in _contained_rglob(ohlcv_root, f"**/{normalized_timeframe}.parquet")
        if path.parent.name.upper() == normalized_symbol
    )
    return any(_parquet_has_bar_in_range(path, start_ts, end_ts) for path in matches)


def _segment_timestamps_in_range(
    symbol: str,
    timeframe: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    data_dir: str | Path = "",
) -> list[pd.Timestamp]:
    """Collect sorted unique bar timestamps in ``[start_ts, end_ts]``."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    try:
        validate_dataset_component(normalized_symbol, name="symbol")
        validate_dataset_component(normalized_timeframe, name="timeframe")
    except ValueError:
        return []

    ohlcv_root = _resolve_data_root(data_dir) / "ohlcv"
    matches = sorted(
        path
        for path in _contained_rglob(ohlcv_root, f"**/{normalized_timeframe}.parquet")
        if path.parent.name.upper() == normalized_symbol
    )
    stamps: set[pd.Timestamp] = set()
    for path in matches:
        try:
            frame = pd.read_parquet(path, columns=["timestamp"])
        except Exception:
            continue
        if "timestamp" not in frame.columns or frame.empty:
            continue
        parsed = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce", format="mixed"
        ).dropna()
        for value in parsed:
            ts = pd.Timestamp(value)
            if start_ts <= ts <= end_ts:
                stamps.add(ts)
    return sorted(stamps)


def _segment_venue_metadata(
    symbol: str,
    timeframe: str,
    data_dir: str | Path,
) -> tuple[set[str], set[str]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    ohlcv_root = _resolve_data_root(data_dir) / "ohlcv"
    matches = _contained_rglob(
        ohlcv_root,
        f"**/{normalized_timeframe}.parquet",
    )
    asset_classes: set[str] = set()
    sources: set[str] = set()
    for path in matches:
        if path.parent.name.upper() != normalized_symbol:
            continue
        parts = path.relative_to(ohlcv_root).parts
        if len(parts) >= 4:
            sources.add(parts[0].lower())
            asset_classes.add(parts[1].lower())
    return asset_classes, sources


def _uses_us_equity_calendar(
    symbol: str,
    timeframe: str = "",
    data_dir: str | Path = "",
) -> bool:
    asset_classes, sources = _segment_venue_metadata(symbol, timeframe, data_dir)
    if asset_classes and sources:
        return asset_classes.issubset(US_EQUITY_ASSET_CLASSES) and sources.issubset(
            US_EQUITY_CALENDAR_SOURCES
        )
    if asset_classes:
        return False
    normalized = str(symbol or "").strip().upper()
    if _is_forex_symbol(normalized) or _is_dukascopy_symbol(normalized):
        return False
    crypto_bases = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "AVAX"}
    base = normalized.split("-", 1)[0].removesuffix("USD")
    if base in crypto_bases:
        return False
    return _US_STOCK_SYMBOL_PATTERN.fullmatch(normalized) is not None


def _expected_us_equity_holidays(
    symbol: str,
    timeframe: str,
    data_dir: str | Path,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> frozenset[date]:
    if not _uses_us_equity_calendar(symbol, timeframe, data_dir):
        return frozenset()
    values: set[date] = set()
    for year in range(start_ts.year - 1, end_ts.year + 2):
        values.update(
            pd.Timestamp(value).date()
            for value in _nyse_holidays(year)
            if start_ts.date() <= pd.Timestamp(value).date() <= end_ts.date()
        )
    return frozenset(values)


def has_sufficient_segment_coverage(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    data_dir: str | Path = "",
    min_density: float = SEGMENT_MIN_DENSITY,
    max_gap_multiplier: float = SEGMENT_MAX_GAP_MULTIPLIER,
    session: Mapping[str, object] | tuple[str, str] | None = None,
    session_start: str | None = None,
    session_end: str | None = None,
) -> bool:
    """Return whether ``[start, end]`` has dense, gap-free coverage (#86).

    Expected bars = TRADING minutes / timeframe step (weekends excluded);
    at least ``min_density`` (default 95%) must be present, and no single
    bar-to-bar gap may exceed ``max_gap_multiplier`` x the step in trading
    minutes (default 3x), regardless of density. A month-long segment with
    a single bar is rejected. Unknown timeframes fail closed.
    """
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    step_minutes = TIMEFRAME_MINUTES.get(normalized_timeframe)
    if step_minutes is None:
        return False
    start_ts = pd.to_datetime(str(start or ""), utc=True, errors="coerce")
    end_ts = pd.to_datetime(str(end or ""), utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        return False
    try:
        explicit_session = _explicit_session_window(session, session_start, session_end)
    except ValueError:
        return False
    stamps = _segment_timestamps_in_range(symbol, normalized_timeframe, start_ts, end_ts, data_dir)
    if not stamps:
        return False
    if step_minutes >= 1440.0:
        session_window = None
    else:
        session_window = explicit_session or _observed_session_window(
            stamps,
            step_minutes,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    holidays = _expected_us_equity_holidays(
        symbol,
        normalized_timeframe,
        data_dir,
        start_ts,
        end_ts,
    )
    expected = max(
        _expected_trading_bars(
            start_ts,
            end_ts,
            step_minutes,
            session_window,
            holidays,
        ),
        1,
    )
    if len(stamps) / expected < min_density:
        return False
    limit_minutes = step_minutes * max_gap_multiplier
    for prev, cur in zip(stamps, stamps[1:]):
        if session_window is None:
            gap_trading_minutes = _trading_minutes_between(prev, cur, holidays)
        else:
            gap_trading_minutes = _session_trading_minutes_between(
                prev,
                cur,
                session_window,
                holidays,
            )
        if gap_trading_minutes > limit_minutes:
            return False
    return True


def _build_requirements(
    *,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    modeling_resolution: str,
) -> list[DataRequirement]:
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_timeframe:
        return []

    timeframes = [normalized_timeframe]
    intrabar_timeframe = _resolve_intrabar_timeframe(normalized_timeframe, modeling_resolution)
    if intrabar_timeframe and intrabar_timeframe not in timeframes:
        timeframes.append(intrabar_timeframe)

    requirements: list[DataRequirement] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        validate_dataset_component(symbol, name="symbol")
        for required_timeframe in timeframes:
            validate_dataset_component(required_timeframe, name="timeframe")
            requirements.append(
                DataRequirement(
                    symbol=symbol,
                    timeframe=required_timeframe,
                    start=str(start or "").strip(),
                    end=str(end or "").strip(),
                )
            )
    return requirements


def _resolve_intrabar_timeframe(base_timeframe: str, modeling_resolution: str | None) -> str | None:
    normalized = str(modeling_resolution or "").strip().lower()
    if normalized in {
        "",
        "main",
        "main_timeframe",
        "open_close",
        "open-close",
        base_timeframe.lower(),
    }:
        return None
    if normalized in {"m1", "1min", "min1"}:
        normalized = "1m"
    elif normalized not in {"s1", "tick"}:
        normalized = normalize_timeframe(normalized)

    if normalized == "1-lower":
        try:
            base_index = TIMEFRAME_ORDER.index(base_timeframe)
        except ValueError:
            return None
        return TIMEFRAME_ORDER[base_index - 1] if base_index > 0 else None

    try:
        base_index = TIMEFRAME_ORDER.index(base_timeframe)
        target_index = TIMEFRAME_ORDER.index(normalized)
    except ValueError:
        return None
    if target_index >= base_index:
        return None
    return normalized


def _has_required_coverage(data_root: Path, requirement: DataRequirement) -> bool:
    coverage = _coverage_for(data_root, requirement.symbol, requirement.timeframe)
    if coverage is None:
        return False

    required_start = _normalize_coverage_date(requirement.start)
    required_end = _normalize_coverage_date(requirement.end)
    if required_start is None and required_end is None:
        return True

    available_start, available_end = coverage
    if required_start is not None and required_start < available_start:
        return False
    if required_end is not None and required_end > available_end:
        return False
    return True


def _coverage_for(data_root: Path, symbol: str, timeframe: str) -> tuple[str, str] | None:
    try:
        validate_dataset_component(symbol, name="symbol")
        validate_dataset_component(timeframe, name="timeframe")
    except ValueError:
        return None
    ohlcv_root = data_root / "ohlcv"
    matches = sorted(_contained_rglob(ohlcv_root, f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None

    starts: list[str] = []
    ends: list[str] = []
    for path in matches:
        bounds = _parquet_timestamp_bounds(path)
        if bounds is None:
            logger.warning("Could not inspect OHLCV coverage for %s", path)
            continue
        start, end = bounds
        starts.append(start.date().isoformat())
        ends.append(end.date().isoformat())

    if not starts or not ends:
        return None
    return min(starts), max(ends)


def closure_hole_allowance_seconds(
    timeframe: str | None, *, start: date
) -> float:
    """Widest hole the exchange calendar can account for, in seconds.

    Composed from calendar facts rather than declared, and it is *not* a
    threshold that fails or passes coverage on its own -- it is only ever
    consulted for a hole that :func:`market_calendar.closure_explains_hole` has
    already accepted by shape. Three terms:

    * the calendar's own longest scheduled closure
      (:func:`long_weekend_gap_days`), which for the modelled rules is 89.5
      hours: a week with one holiday shut plus the weekend.
    * two regular session lengths of extended-hours tail, because the calendar
      models the 6.5-hour regular session while a 24/5 venue may place its first
      bar of a reopening session up to a full session after the modelled open.
    * one bar, which is arithmetic rather than slack: the last bar before a
      closure is stamped at the last minute of the session, so the measured hole
      is one bar interval wider than the closure itself.
    """
    bar_seconds = max(1.0, round(timeframe_nominal_days(timeframe) * 86_400.0))
    return (
        LONG_WEEKEND_GAP_DAYS * 86_400.0
        + 2.0 * market_calendar.session_span_seconds(start)
        + bar_seconds
    )


def _gap_is_market_closure(
    gap: dict[str, object],
    *,
    timeframe: str | None,
    symbol: str = "",
    data_dir: str | Path = "",
) -> bool:
    """Whether the exchange calendar accounts for this hole.

    Three questions, in this order. **First: is this venue one the calendar
    models at all?** :func:`market_calendar` says so itself — "Non-US venues are
    not modelled ... nothing here should be used to judge those" — and the shape
    test it offers is not venue-agnostic. A Monday-holiday week in US equities
    has no US session between Friday and Tuesday, so a Dukascopy FX series that
    simply died for those four days matched the shape exactly and was excused,
    while `_uses_us_equity_calendar` already knew the symbol was not a US venue.
    A silent four-day hole in FX is worse than a spurious failure, so a venue the
    calendar does not model gets no excuse at all.

    That is deliberately fail-closed, and it has a known cost: a *genuine* 24/5
    venue closure (FX reopens Sunday ~22:00 UTC, so the resuming bar's date is a
    Sunday and the shape test refuses it) now hard-fails rather than being
    excused. Closing that properly needs a real per-venue calendar, and guessing
    a width for a market we do not model is the failure this whole check exists
    to prevent. Recorded rather than papered over.

    The remaining two questions are the original ones. The shape comes from
    :func:`market_calendar.closure_explains_hole` -- did the hole span any
    scheduled session? Then the width is checked against
    :func:`closure_hole_allowance_seconds`.

    Fails closed on anything it cannot read: a gap whose endpoints or exact width
    are missing or unparseable is treated as a data hole, never as market
    behaviour. An unparseable date is a bug in the gap report, and excusing it
    would hide it -- as would guessing a width from the rounded ``gap_days``,
    which cannot resolve a one-minute bar.
    """
    if not _uses_us_equity_calendar(symbol, timeframe or "", data_dir):
        # A venue the calendar does not model. Refusing here is the whole point:
        # the alternative is certifying a data hole as market behaviour.
        return False
    raw_start = str(gap.get("start") or "").strip()
    raw_end = str(gap.get("end") or "").strip()
    if not raw_start or not raw_end or "gap_seconds" not in gap:
        return False
    try:
        start_date = date.fromisoformat(raw_start)
        end_date = date.fromisoformat(raw_end)
        gap_seconds = float(gap["gap_seconds"])
    except (TypeError, ValueError):
        return False
    if not market_calendar.closure_explains_hole(start_date, end_date):
        return False
    return gap_seconds <= closure_hole_allowance_seconds(timeframe, start=start_date)


def _gaps_overlapping_window(
    gaps: list[dict[str, object]], start: str | None, end: str | None
) -> list[dict[str, object]]:
    """Keep only holes strictly overlapping [start, end] (CodeRabbit round).

    Ancient history outside the required window must not fail a requirement
    that never touches it; boundary-touching holes carry no interior
    missing bars and are excluded too. The list is copied on the pass-through
    path so the caller never receives an alias of the input.
    """
    if start is None or end is None:
        return list(gaps)
    return [
        gap
        for gap in gaps
        if str(gap.get("end", "")) > start and str(gap.get("start", "")) < end
    ]


def analyze_ohlcv_coverage(data_root: Path, requirement: DataRequirement) -> dict[str, Any]:
    """Report coverage for one requirement, including internal holes.

    Returns a dict with ``status`` ("ok", "warning" or "failed"), the global
    ``start``/``end`` bounds, and any internal ``gaps``. Status is fail-closed:
    missing data and significant internal holes both report "failed" (Decision
    Record 2026-08-31), small holes warn.
    """
    symbol = str(requirement.symbol or "").strip().upper()
    timeframe = normalize_timeframe(str(requirement.timeframe or "").strip())
    try:
        validate_dataset_component(symbol, name="symbol")
        validate_dataset_component(timeframe, name="timeframe")
    except ValueError as exc:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "failed",
            "gaps": [],
            "failed_downloads": [],
            "error": str(exc),
        }
    symbol_dir_candidates = sorted(
        _contained_rglob(data_root / "ohlcv", f"**/{symbol}")
    )
    gaps: list[dict[str, object]] = []
    bounds = _coverage_for(data_root, symbol, timeframe)

    for candidate in symbol_dir_candidates:
        gaps.extend(find_internal_gaps(candidate, timeframe))

    error: str | None = None
    # Both the stored-bounds check below and the gap window filter further down
    # need the requested window, so it is normalized once, unconditionally. The
    # explicit annotations keep it bound on every path, including the
    # ``bounds is None`` branch where no bounds comparison happens.
    required_start: str | None = _normalize_coverage_date(requirement.start)
    required_end: str | None = _normalize_coverage_date(requirement.end)
    if bounds is None:
        error = (
            f"No {symbol} {timeframe} parquet found under {data_root / 'ohlcv'}"
        )
    else:
        if required_start is not None and required_start < bounds[0]:
            error = (
                f"Requested start {required_start} is before stored coverage start {bounds[0]}"
            )
        elif required_end is not None and required_end > bounds[1]:
            error = f"Requested end {required_end} is after stored coverage end {bounds[1]}"

    # The fail threshold scales with the timeframe: a weekly series has 7-day
    # bar spacing, so a fixed 3-day allowance flagged every bar as a hole and no
    # weekly or monthly backtest could pass the gate.
    #
    # The long-weekend allowance is a SEPARATE term applied here rather than a
    # floor on that threshold. A hole the exchange calendar explains -- it starts
    # on a session, resumes on the next session after a closure, and spans no
    # session of its own -- is market behaviour and stays a warning. A hole of the
    # same length that straddles a live session is a downloader that stopped, and
    # fails. No single length budget can tell those apart: an ordinary long
    # weekend is 2.73 days and a Monday-holiday week is 3.73, so any constant big
    # enough for the second also passes the 3.6-day hole that motivated the
    # original 4.0-day floor.
    #
    # The calendar only speaks for venues it models, so `_gap_is_market_closure`
    # returns False for anything else. Known cost, accepted deliberately: a
    # genuine multi-day closure on a 24/5 venue (FX, metals, crypto) now hard-fails
    # rather than being excused, because a 24/5 venue reopens Sunday ~22:00 UTC and
    # the resuming bar's date is a Sunday, which the US shape test refuses. Closing
    # that properly needs a real per-venue calendar; guessing closure dates for a
    # market we do not model is the exact failure this check prevents. Ordinary
    # weekends on those venues are unaffected -- they pass on the threshold above,
    # not on the calendar.
    fail_threshold = scaled_gap_threshold_days(
        requirement.timeframe,
        INTERNAL_GAP_FAIL_DAYS,
        allow_long_weekend=True,
    )
    significant_gaps = _gaps_overlapping_window(
        [
            gap
            for gap in gaps
            if float(gap.get("gap_days", 0)) > fail_threshold
            and not _gap_is_market_closure(
                gap,
                timeframe=requirement.timeframe,
                symbol=requirement.symbol,
                # data_root is the already-resolved root; _resolve_data_root
                # accepts either form, so passing it here keeps the venue lookup
                # reading the same tree the coverage check just read.
                data_dir=data_root,
            )
        ],
        required_start,
        required_end,
    )

    if error or significant_gaps:
        status = "failed"
    elif gaps:
        status = "warning"
    else:
        status = "ok"

    report: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "gaps": gaps,
        "failed_downloads": list(significant_gaps),
    }
    if bounds is not None:
        report["start"] = bounds[0]
        report["end"] = bounds[1]
    if error:
        report["error"] = error
    return report


def _normalize_coverage_date(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = pd.to_datetime(str(value), utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).date().isoformat()


def _is_forex_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if "/" in normalized:
        return True
    if len(normalized) == 6 and normalized.isalpha():
        return True
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        return normalized in {
            key.upper()
            for key, value in SYMBOL_CONFIG.items()
            if str(value.get("asset_class", "")).strip().lower() == "forex"
        }
    except Exception:
        return False


def _is_dukascopy_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    try:
        from data_tools.downloaders.dukascopy_native import SYMBOL_CONFIG

        return normalized in {key.upper() for key in SYMBOL_CONFIG}
    except Exception:
        return False


def _is_us_intraday_stock(symbol: str, timeframe: str) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if normalized_timeframe not in US_INTRADAY_TIMEFRAMES:
        return False
    if _is_forex_symbol(normalized_symbol) or _is_dukascopy_symbol(normalized_symbol):
        return False
    if not _US_STOCK_SYMBOL_PATTERN.fullmatch(normalized_symbol):
        return False
    if normalized_symbol.endswith("USD") and len(normalized_symbol) > 5:
        return False
    return True


def _auto_download_source(symbol: str, timeframe: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if _is_dukascopy_symbol(normalized_symbol) or _is_forex_symbol(normalized_symbol):
        return "dukascopy"
    if _is_us_intraday_stock(normalized_symbol, normalized_timeframe):
        return "huggingface"
    return "yfinance"


def _auto_download_source_plan(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
) -> list[dict[str, str | None]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    default_source = _auto_download_source(normalized_symbol, normalized_timeframe)
    if default_source != "huggingface":
        return [{"source": default_source, "start": start, "end": end}]

    normalized_start = _normalize_coverage_date(start)
    normalized_end = _normalize_coverage_date(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        normalized_start, normalized_end = normalized_end, normalized_start

    if normalized_start is None and normalized_end is None:
        return [{"source": "alpaca", "start": None, "end": None}]

    plan: list[dict[str, str | None]] = []
    hf_start = normalized_start or normalized_end
    hf_end = normalized_end
    if hf_start is not None and hf_start > HF_INTRADAY_COVERAGE_END:
        hf_start = None
        hf_end = None
    elif hf_end is None or hf_end > HF_INTRADAY_COVERAGE_END:
        hf_end = HF_INTRADAY_COVERAGE_END
    if hf_start and hf_end and hf_start <= hf_end:
        plan.append({"source": "huggingface", "start": hf_start, "end": hf_end})

    alpaca_start = normalized_start or normalized_end
    alpaca_end = normalized_end
    if alpaca_end is None or alpaca_end >= ALPACA_INTRADAY_START:
        if alpaca_start is None or alpaca_start < ALPACA_INTRADAY_START:
            alpaca_start = ALPACA_INTRADAY_START
        if alpaca_end is None or alpaca_start <= alpaca_end:
            plan.append({"source": "alpaca", "start": alpaca_start, "end": alpaca_end or end})

    if not plan:
        # No known vendor window covers this range: say so explicitly instead
        # of silently re-routing to a daily-grade fallback.
        logger.warning(
            "No configured vendor coverage for %s %s %s..%s; coverage unknown",
            normalized_symbol,
            normalized_timeframe,
            normalized_start,
            normalized_end,
        )
        return [
            {
                "source": "alpaca",
                "start": start,
                "end": end,
                "coverage": "unknown",
            }
        ]
    return plan


def _download_missing_ohlcv_data(
    *,
    data_root: Path,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    source: str,
) -> None:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(timeframe) or "1d"
    selected_source = str(source or "").strip().lower() or "dukascopy"

    if selected_source == "huggingface":
        from data_tools.downloaders.huggingface_dl import HuggingFaceDownloader

        HuggingFaceDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source == "alpaca":
        from data_tools.downloaders.alpaca_dl import AlpacaDownloader

        try:
            AlpacaDownloader().download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=data_root,
                start=start,
                end=end,
            )
        except RuntimeError as exc:
            if "Missing Alpaca credentials" not in str(exc):
                raise
            logger.warning(
                "Alpaca credentials missing; falling back to yfinance for %s", normalized_symbol
            )
            from data_tools.downloaders.yfinance_dl import YFinanceDownloader

            YFinanceDownloader().download(
                symbol=normalized_symbol,
                timeframe=normalized_timeframe,
                output_dir=data_root,
                start=start,
                end=end,
            )
        return

    if selected_source == "yfinance":
        from data_tools.downloaders.yfinance_dl import YFinanceDownloader

        YFinanceDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source == "dolthub":
        from data_tools.downloaders.dolthub_dl import DolthubDownloader

        DolthubDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
        )
        return

    if selected_source in {"dukascopy", "jforex"}:
        from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader

        DukascopyNativeDownloader().download(
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            output_dir=data_root,
            start=start,
            end=end,
            offer_side="bid",
        )
        return

    raise ValueError(
        f"Unsupported auto-download source '{selected_source}' for {normalized_symbol}"
    )


def _invalidate_catalog_cache(data_root: Path) -> None:
    try:
        from data_tools import cache_manager

        cache_manager.invalidate_after_download(data_root)
    except Exception as exc:
        logger.debug("Could not invalidate catalog cache after auto-download: %s", exc)

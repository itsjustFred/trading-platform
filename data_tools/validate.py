"""
Data Validation — automated quality checks for OHLCV Parquet files.

Checks:
  1. Gap detection (missing bars, accounting for weekends/holidays)
  2. Duplicate timestamps
  3. OHLC consistency (high >= max(open,close), low <= min(open,close))
  4. Null/NaN values
  5. Zero-volume bars
  6. Timestamp continuity (monotonically increasing)

Usage:
    python -m data_tools.validate                         # validate all
    python -m data_tools.validate --symbol AAPL           # single symbol
    python -m data_tools.validate --report ~/data/validation_report.json
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_tools.auto_download import TIMEFRAME_MINUTES
from data_tools.path_safety import ensure_dataset_path_within
from shared.constants import DATA_DIR, normalize_timeframe

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# Expected bars per timeframe per trading day (forex ~24h, equities ~7h)
EXPECTED_BARS_PER_DAY = {
    "1m": {"equity": 390, "forex": 1440},
    "5m": {"equity": 78, "forex": 288},
    "15m": {"equity": 26, "forex": 96},
    "30m": {"equity": 13, "forex": 48},
    "1h": {"equity": 7, "forex": 24},
    "4h": {"equity": 2, "forex": 6},
    "1d": {"equity": 1, "forex": 1},
}

# Gap thresholds per timeframe (in minutes) — gaps larger than this are flagged
GAP_THRESHOLD_MINUTES = {
    "1m": 5,
    "5m": 15,
    "15m": 45,
    "30m": 90,
    "1h": 180,
    "4h": 480,
    "1d": 4320,  # 3 days (weekend)
}

#: Asset classes with exchange holidays (B3: gap skips apply to these only —
#: FX/metals trade through most US holidays, so blanket skipping would hide
#: real data holes there).
_EQUITY_LIKE_ASSET_CLASSES = frozenset({"equity", "stock", "stocks", "etf", "index"})
_EXPECTED_GAP_CLASSIFICATIONS = frozenset({"overnight", "weekend", "holiday"})


def _easter_sunday(year: int) -> tuple[int, int]:
    """Month/day of Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_corr = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_corr) // 451
    month = (h + weekday_corr - 7 * m + 114) // 31
    day = ((h + weekday_corr - 7 * m + 114) % 31) + 1
    return month, day


def _nyse_holidays(year: int) -> set[str]:
    """NYSE holiday dates (ISO) for one year, incl. observed shifts (B3)."""
    from datetime import date, timedelta

    def observed(month: int, day: int) -> date:
        day_date = date(year, month, day)
        if day_date.weekday() == 5:  # Saturday -> preceding Friday
            return day_date - timedelta(days=1)
        if day_date.weekday() == 6:  # Sunday -> following Monday
            return day_date + timedelta(days=1)
        return day_date

    def nth_weekday(month: int, weekday: int, n: int) -> date:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    def last_weekday(month: int, weekday: int) -> date:
        if month == 12:
            probe = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            probe = date(year, month + 1, 1) - timedelta(days=1)
        offset = (probe.weekday() - weekday) % 7
        return probe - timedelta(days=offset)

    holidays = {
        observed(1, 1),  # New Year's Day
        nth_weekday(1, 0, 3),  # MLK Day
        nth_weekday(2, 0, 3),  # Presidents Day
        last_weekday(5, 0),  # Memorial Day
        observed(6, 19) if year >= 2021 else None,  # Juneteenth (from 2021)
        observed(7, 4),  # Independence Day
        nth_weekday(9, 0, 1),  # Labor Day
        nth_weekday(11, 3, 4),  # Thanksgiving
        observed(12, 25),  # Christmas
    }
    easter_month, easter_day = _easter_sunday(year)
    good_friday = date(year, easter_month, easter_day) - timedelta(days=2)
    holidays.add(good_friday)
    return {day.isoformat() for day in holidays if day is not None and day.year == year}


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _parse_session_clock(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in {"24:00", "24:00:00"}:
        return 1440
    parsed = pd.to_datetime(f"2000-01-01 {text}", errors="coerce")
    if pd.isna(parsed):
        return None
    return int(parsed.hour * 60 + parsed.minute)


def _session_window(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        start = value.get("start", value.get("open", value.get("session_start")))
        end = value.get("end", value.get("close", value.get("session_end")))
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        start, end = value
    else:
        text = str(value).strip()
        if not text:
            return None
        separator = "-" if "-" in text else (" to " if " to " in text else None)
        if separator is None:
            return None
        start, end = text.split(separator, 1)
    start_minutes = _parse_session_clock(start)
    end_minutes = _parse_session_clock(end)
    if start_minutes is None or end_minutes is None:
        return None
    if end_minutes <= start_minutes:
        end_minutes += 1440
    return start_minutes, end_minutes


def _holiday_dates(
    value: object,
    start_year: int,
    end_year: int,
    *,
    include_nyse: bool = False,
) -> set[str]:
    dates: set[str] = set()
    if isinstance(value, Mapping):
        value = value.get("holidays", value.get("holiday_dates", value.get("calendar")))
    if isinstance(value, str):
        value = [item for item in re.split(r"[,;\s]+", value) if item]
    elif value is not None and not isinstance(value, (list, tuple, set, frozenset)):
        value = [value]
    if value is not None:
        for item in value:
            parsed = pd.to_datetime(item, errors="coerce")
            if not pd.isna(parsed):
                dates.add(pd.Timestamp(parsed).date().isoformat())
    if include_nyse:
        for year in range(start_year, end_year + 1):
            dates |= _nyse_holidays(year)
    return dates


def _observed_session_window(
    timestamps: list[pd.Timestamp], timeframe: str
) -> tuple[int, int] | None:
    normalized = normalize_timeframe(str(timeframe).strip())
    step_minutes = TIMEFRAME_MINUTES.get(normalized)
    if step_minutes is None or step_minutes >= 1440.0 or len(timestamps) < 2:
        return None
    by_day: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    for timestamp in timestamps:
        by_day.setdefault(timestamp.normalize(), []).append(timestamp)
    starts = [
        min(int(point.hour * 60 + point.minute) for point in points)
        for points in by_day.values()
    ]
    ends = [
        max(int(point.hour * 60 + point.minute) for point in points)
        + int(step_minutes)
        for points in by_day.values()
    ]
    start_minutes = int(round(float(sorted(starts)[len(starts) // 2])))
    end_minutes = int(round(float(sorted(ends)[len(ends) // 2])))
    if end_minutes <= start_minutes or end_minutes - start_minutes < step_minutes * 2:
        return None
    return start_minutes, end_minutes


def _session_overlap(
    start: pd.Timestamp,
    end: pd.Timestamp,
    session: tuple[int, int],
    holidays: set[str],
    step_minutes: float = 0.0,
) -> bool:
    start = start + pd.Timedelta(minutes=step_minutes)
    cursor = start.normalize() - pd.Timedelta(days=1)
    while cursor <= end.normalize():
        if cursor.weekday() < 5 and cursor.date().isoformat() not in holidays:
            session_start = cursor + pd.Timedelta(minutes=session[0])
            session_end = cursor + pd.Timedelta(minutes=session[1])
            if min(end, session_end) > max(start, session_start):
                return True
        cursor += pd.Timedelta(days=1)
    return False


def _interval_has_weekend(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    cursor = start.normalize() + pd.Timedelta(days=1)
    saw_day = False
    while cursor < end.normalize():
        saw_day = True
        if cursor.weekday() < 5:
            return False
        cursor += pd.Timedelta(days=1)
    return saw_day


def _weekend_boundary(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    start_day = start.normalize()
    end_day = end.normalize()
    if start_day.weekday() == 4 and start.time() >= pd.Timestamp("20:00").time():
        return end_day.weekday() in {5, 6, 0} and end.time() <= pd.Timestamp("02:00").time()
    if start_day.weekday() == 5:
        return end_day.weekday() in {6, 0} and end.time() <= pd.Timestamp("02:00").time()
    if start_day.weekday() == 6:
        return end_day.weekday() == 0 and end.time() <= pd.Timestamp("02:00").time()
    return False


def _is_intraday_timeframe(timeframe: str) -> bool:
    return str(timeframe).strip().lower() not in {
        "",
        "1d",
        "1w",
        "d",
        "day",
        "daily",
        "1day",
    }


def _gap_classification(
    gap_start: pd.Timestamp,
    gap_end: pd.Timestamp,
    asset_class: str,
    timeframe: str = "",
    *,
    session: object = None,
    holidays: object = None,
    calendar: object = None,
    observed_session: tuple[int, int] | None = None,
    session_start: str | None = None,
    session_end: str | None = None,
) -> str:
    if session is None and (session_start is not None or session_end is not None):
        if session_start is None or session_end is None:
            raise ValueError("session_start and session_end must be supplied together")
        session = {"start": session_start, "end": session_end}
    if gap_end <= gap_start:
        return "data"
    start = _utc_timestamp(gap_start)
    end = _utc_timestamp(gap_end)
    intraday = _is_intraday_timeframe(timeframe)
    normalized_timeframe = normalize_timeframe(str(timeframe).strip())
    step_minutes = TIMEFRAME_MINUTES.get(normalized_timeframe)
    normalized_asset_class = str(asset_class).strip().lower()
    session_value = session
    if session_value is None and isinstance(calendar, Mapping):
        session_value = calendar.get("session", calendar.get("trading_session"))
    resolved_session = _session_window(session_value)
    if resolved_session is None and normalized_asset_class in _EQUITY_LIKE_ASSET_CLASSES:
        resolved_session = observed_session
    holiday_value = holidays
    if holiday_value is None and isinstance(calendar, Mapping):
        holiday_value = calendar.get("holidays", calendar.get("holiday_dates"))
    holiday_set = _holiday_dates(
        holiday_value,
        start.year,
        end.year,
        include_nyse=normalized_asset_class in _EQUITY_LIKE_ASSET_CLASSES,
    )

    if intraday and resolved_session is not None:
        if _session_overlap(
            start,
            end,
            resolved_session,
            holiday_set,
            step_minutes=step_minutes or 0.0,
        ):
            return "session"
        if any(
            start.normalize() + pd.Timedelta(days=offset) < end.normalize()
            and (start.normalize() + pd.Timedelta(days=offset)).date().isoformat() in holiday_set
            for offset in range((end.normalize() - start.normalize()).days + 1)
        ):
            return "holiday"
        if _interval_has_weekend(start, end) or _weekend_boundary(start, end):
            return "weekend"
        return "overnight"

    if not intraday:
        if _interval_has_weekend(start, end):
            return "weekend"
        cursor = start.normalize() + pd.Timedelta(days=1)
        saw_missing_day = False
        while cursor < end.normalize():
            saw_missing_day = True
            if cursor.weekday() < 5 and cursor.date().isoformat() not in holiday_set:
                return "data"
            cursor += pd.Timedelta(days=1)
        if saw_missing_day:
            return "holiday"
        return "data"

    if _weekend_boundary(start, end):
        return "weekend"
    if _interval_has_weekend(start, end):
        return "weekend"
    cursor = start.normalize() + pd.Timedelta(days=1)
    saw_missing_day = False
    while cursor < end.normalize():
        saw_missing_day = True
        if cursor.weekday() < 5 and cursor.date().isoformat() not in holiday_set:
            return "data"
        cursor += pd.Timedelta(days=1)
    return "holiday" if saw_missing_day else "data"


def _gap_is_expected_nontrading(
    gap_start: pd.Timestamp,
    gap_end: pd.Timestamp,
    asset_class: str,
    timeframe: str = "",
    *,
    session: object = None,
    holidays: object = None,
    calendar: object = None,
    observed_session: tuple[int, int] | None = None,
    session_start: str | None = None,
    session_end: str | None = None,
) -> bool:
    return _gap_classification(
        gap_start,
        gap_end,
        asset_class,
        timeframe,
        session=session,
        holidays=holidays,
        calendar=calendar,
        observed_session=observed_session,
        session_start=session_start,
        session_end=session_end,
    ) in _EXPECTED_GAP_CLASSIFICATIONS


def _asset_class_from_parts(parts: tuple[str, ...]) -> str:
    for index, part in enumerate(parts):
        if part.lower() == "ohlcv" and index + 1 < len(parts):
            return parts[index + 1].lower()
    if len(parts) >= 2:
        return parts[1].lower()
    return ""


def _first_column_value(frame: pd.DataFrame, names: tuple[str, ...]) -> object:
    for name in names:
        if name not in frame.columns:
            continue
        values = frame[name].dropna()
        if not values.empty:
            return values.iloc[0]
    return None


def _dataset_metadata(path: Path) -> dict:
    candidates = [DATA_DIR / "source_meta.json", path.parent / "source_meta.json"]
    for metadata_path in candidates:
        if not metadata_path.exists():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        keys = [str(path), path.as_posix()]
        try:
            keys.append(str(path.resolve().relative_to(DATA_DIR.resolve())))
        except (OSError, RuntimeError, ValueError):
            pass
        for key in keys:
            value = payload.get(key)
            if isinstance(value, Mapping):
                return dict(value)
        continue
    return {}


@dataclass
class ValidationResult:
    """Result of validating a single Parquet file."""

    path: str
    symbol: str
    timeframe: str
    source: str
    status: str = "ok"  # "ok", "warning", "error"
    total_rows: int = 0
    date_range_start: str = ""
    date_range_end: str = ""
    gaps: list[dict] = field(default_factory=list)
    duplicates: int = 0
    null_counts: dict = field(default_factory=dict)
    ohlc_violations: int = 0
    zero_volume_pct: float = 0.0
    monotonic: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """Compact summary for catalog integration."""
        return {
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "status": self.status,
            "gaps": len(self.gaps),
            "duplicates": self.duplicates,
            "warnings": self.warnings[:5],
        }


def validate_parquet(
    path: Path,
    *,
    ohlcv_root: Path | None = None,
    session: object = None,
    holidays: object = None,
    calendar: object = None,
    session_start: str | None = None,
    session_end: str | None = None,
    holiday_dates: object = None,
) -> ValidationResult:
    """Validate a single OHLCV Parquet file."""
    if session is None and (session_start is not None or session_end is not None):
        if session_start is None or session_end is None:
            raise ValueError("session_start and session_end must be supplied together")
        session = {"start": session_start, "end": session_end}
    if holidays is None and holiday_dates is not None:
        holidays = holiday_dates
    candidate = Path(path).expanduser()
    try:
        configured_root = (ohlcv_root or (DATA_DIR / "ohlcv")).resolve()
        rel = candidate.resolve().relative_to(configured_root)
    except (OSError, RuntimeError, ValueError):
        rel = candidate
    parts = rel.parts

    # Extract metadata from path: {source}/{asset_class}/.../{symbol}/{tf}.parquet
    source = parts[0] if len(parts) >= 4 else "unknown"
    symbol = candidate.parent.name
    timeframe = candidate.stem

    result = ValidationResult(
        path=str(rel),
        symbol=symbol,
        timeframe=timeframe,
        source=source,
    )

    try:
        df = pd.read_parquet(candidate)
    except Exception as e:
        result.status = "error"
        result.errors.append(f"Could not read parquet: {e}")
        return result

    df.columns = [c.lower() for c in df.columns]
    dataset_metadata = dict(df.attrs) if isinstance(df.attrs, Mapping) else {}
    dataset_metadata.update(_dataset_metadata(candidate))
    frame_session = _first_column_value(
        df, ("session", "trading_session", "trading_hours", "market_hours")
    )
    if frame_session is None and {"session_start", "session_end"}.issubset(df.columns):
        frame_session = {
            "start": _first_column_value(df, ("session_start", "market_open")),
            "end": _first_column_value(df, ("session_end", "market_close")),
        }
    frame_calendar = _first_column_value(df, ("calendar", "holiday_calendar"))
    frame_holidays = _first_column_value(df, ("holiday", "holidays", "holiday_dates"))
    resolved_session = session if session is not None else frame_session
    if resolved_session is None:
        resolved_session = dataset_metadata.get("session", dataset_metadata.get("trading_session"))
    if resolved_session is None:
        metadata_start = dataset_metadata.get("session_start", dataset_metadata.get("market_open"))
        metadata_end = dataset_metadata.get("session_end", dataset_metadata.get("market_close"))
        if metadata_start is not None or metadata_end is not None:
            resolved_session = {"start": metadata_start, "end": metadata_end}
    resolved_calendar = calendar if calendar is not None else frame_calendar
    if resolved_calendar is None:
        resolved_calendar = dataset_metadata.get("calendar")
    resolved_holidays = holidays if holidays is not None else frame_holidays
    if resolved_holidays is None:
        resolved_holidays = dataset_metadata.get(
            "holidays",
            dataset_metadata.get(
                "holiday_dates", dataset_metadata.get("holiday_calendar")
            ),
        )
    result.total_rows = len(df)

    if len(df) == 0:
        result.status = "error"
        result.errors.append("File is empty")
        return result

    # ─── Timestamp checks ──────────────────────────────────────────────

    if "timestamp" not in df.columns:
        result.status = "warning"
        result.warnings.append("No 'timestamp' column found")
        # Try first column
        df = df.rename(columns={df.columns[0]: "timestamp"})

    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    null_ts = ts.isna().sum()
    if null_ts > 0:
        result.warnings.append(f"{null_ts} unparseable timestamps")

    ts = ts.dropna()
    observed_session = _observed_session_window(
        [_utc_timestamp(value) for value in ts.tolist()], timeframe
    )
    if len(ts) > 0:
        result.date_range_start = str(ts.iloc[0])
        result.date_range_end = str(ts.iloc[-1])

        # Check if timestamps look like epoch seconds stored wrong (XAUUSD issue)
        if ts.iloc[0].year < 1990:
            result.status = "error"
            result.errors.append(f"Timestamps appear corrupt (starts at {ts.iloc[0]})")
            return result

    # ─── Duplicates ────────────────────────────────────────────────────

    result.duplicates = int(ts.duplicated().sum())
    if result.duplicates > 0:
        result.warnings.append(f"{result.duplicates} duplicate timestamps")

    # ─── Monotonicity ──────────────────────────────────────────────────

    result.monotonic = bool(ts.is_monotonic_increasing)
    if not result.monotonic:
        result.warnings.append("Timestamps are not monotonically increasing")

    # ─── Gap detection ─────────────────────────────────────────────────

    if len(ts) > 1:
        # #86: reset to a positional RangeIndex before diff — after dropna()
        # the labels are non-contiguous, so using a label as .iloc position
        # reports wrong gap boundaries.
        ts = ts.reset_index(drop=True)
        diffs = ts.diff().dropna()
        threshold = pd.Timedelta(minutes=GAP_THRESHOLD_MINUTES.get(timeframe, 180))

        large_gaps = diffs[diffs > threshold]
        for pos in range(min(20, len(large_gaps))):
            idx = large_gaps.index[pos]
            gap_start = ts.iloc[idx - 1] if idx > 0 else ts.iloc[0]
            gap_end = ts.loc[idx]
            gap_minutes = large_gaps.iloc[pos].total_seconds() / 60

            asset_class = _asset_class_from_parts(parts)
            classification = _gap_classification(
                gap_start,
                gap_end,
                asset_class,
                timeframe,
                session=resolved_session,
                holidays=resolved_holidays,
                calendar=resolved_calendar,
                observed_session=observed_session,
            )
            if classification in _EXPECTED_GAP_CLASSIFICATIONS:
                continue

            result.gaps.append(
                {
                    "start": str(gap_start),
                    "end": str(gap_end),
                    "gap_minutes": round(gap_minutes, 1),
                    "classification": classification,
                }
            )

        if len(result.gaps) > 0:
            result.warnings.append(f"{len(result.gaps)} data gaps detected")

    # ─── OHLC consistency ──────────────────────────────────────────────

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            result.status = "error"
            result.errors.append(f"Missing column: {col}")
            return result

    o, h, low, c = df["open"], df["high"], df["low"], df["close"]

    # high should be >= max(open, close) and low should be <= min(open, close)
    high_violations = (h < o) | (h < c)
    low_violations = (low > o) | (low > c)
    hl_violations = h < low

    result.ohlc_violations = int(high_violations.sum() + low_violations.sum() + hl_violations.sum())
    if result.ohlc_violations > 0:
        result.warnings.append(f"{result.ohlc_violations} OHLC consistency violations")

    # ─── Null/NaN values ───────────────────────────────────────────────

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            nulls = int(df[col].isna().sum())
            if nulls > 0:
                result.null_counts[col] = nulls

    if result.null_counts:
        result.warnings.append(f"Null values: {result.null_counts}")

    # ─── Zero volume ──────────────────────────────────────────────────

    if "volume" in df.columns:
        zero_vol = (df["volume"] == 0).sum()
        result.zero_volume_pct = round(zero_vol / len(df) * 100, 2)
        if result.zero_volume_pct > 50:
            result.warnings.append(f"{result.zero_volume_pct}% zero-volume bars")

    # ─── Final status ──────────────────────────────────────────────────

    if result.errors:
        result.status = "error"
    elif result.warnings:
        result.status = "warning"
    else:
        result.status = "ok"

    return result


def validate_all(data_dir: Path, symbol_filter: str | None = None) -> list[ValidationResult]:
    """Validate all Parquet files in the OHLCV directory."""
    ohlcv_root = data_dir / "ohlcv"
    if not ohlcv_root.exists():
        logger.error("OHLCV directory not found: %s", ohlcv_root)
        return []

    results = []
    for parquet in sorted(ohlcv_root.rglob("*.parquet")):
        try:
            validated_path = ensure_dataset_path_within(ohlcv_root, parquet)
        except ValueError as exc:
            result = ValidationResult(
                path=str(parquet),
                symbol=parquet.parent.name,
                timeframe=parquet.stem,
                source="unknown",
                status="error",
            )
            result.errors.append(str(exc))
            results.append(result)
            continue
        if symbol_filter and parquet.parent.name.upper() != symbol_filter.upper():
            continue

        logger.info("Validating %s ...", validated_path.relative_to(ohlcv_root.resolve()))
        r = validate_parquet(validated_path, ohlcv_root=ohlcv_root)

        status_icon = {"ok": "✅", "warning": "⚠️", "error": "❌"}.get(r.status, "?")
        logger.info(
            "  %s %s %s: %d rows, %d gaps, %d dupes, %d OHLC violations",
            status_icon,
            r.symbol,
            r.timeframe,
            r.total_rows,
            len(r.gaps),
            r.duplicates,
            r.ohlc_violations,
        )

        for w in r.warnings:
            logger.info("    ⚠️  %s", w)
        for e in r.errors:
            logger.error("    ❌  %s", e)

        results.append(r)

    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate OHLCV data quality")
    parser.add_argument("--symbol", help="Validate only this symbol")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Data directory")
    parser.add_argument("--report", help="Save JSON report to this path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    results = validate_all(data_dir, symbol_filter=args.symbol)
    missing_root = not (data_dir / "ohlcv").is_dir()

    # Summary
    ok = sum(1 for r in results if r.status == "ok")
    warn = sum(1 for r in results if r.status == "warning")
    err = sum(1 for r in results if r.status == "error")
    total = len(results)

    print(f"\n{'=' * 50}")
    print(f"Validation Summary: {total} files")
    print(f"  ✅ OK:       {ok}")
    print(f"  ⚠️  Warning:  {warn}")
    print(f"  ❌ Error:    {err}")

    if args.report:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"total": total, "ok": ok, "warning": warn, "error": err},
            "results": [r.to_dict() for r in results],
        }
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  Report saved to: {args.report}")

    return 1 if err or missing_root else 0


if __name__ == "__main__":
    raise SystemExit(main())

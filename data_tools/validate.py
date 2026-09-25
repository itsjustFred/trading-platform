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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_tools.path_safety import ensure_dataset_path_within
from shared.constants import DATA_DIR

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


def _gap_is_expected_nontrading(
    gap_start: pd.Timestamp,
    gap_end: pd.Timestamp,
    asset_class: str,
    timeframe: str = "",
) -> bool:
    if gap_end <= gap_start:
        return False
    start = pd.Timestamp(gap_start).tz_convert("UTC")
    end = pd.Timestamp(gap_end).tz_convert("UTC")
    start_day = start.normalize()
    end_day = end.normalize()
    intraday = str(timeframe).lower() not in {"", "1d", "1w"}
    normalized_asset_class = str(asset_class).strip().lower()
    if intraday:
        if normalized_asset_class in _EQUITY_LIKE_ASSET_CLASSES:
            return False
        weekend_boundary = (
            start_day.weekday() == 4
            and end_day.weekday() in {6, 0}
            and start.time() >= pd.Timestamp("21:00").time()
            and end.time() <= pd.Timestamp("01:00").time()
        ) or (
            start_day.weekday() == 6
            and end_day.weekday() == 0
            and start.time() <= pd.Timestamp("01:00").time()
            and end.time() <= pd.Timestamp("01:00").time()
        )
        if not weekend_boundary:
            return False
        return True
    holidays: set[str] = set()
    if normalized_asset_class in _EQUITY_LIKE_ASSET_CLASSES:
        for year in range(start_day.year, end_day.year + 1):
            holidays |= _nyse_holidays(year)
    cursor = start_day + pd.Timedelta(days=1)
    saw_missing_day = False
    while cursor < end_day:
        saw_missing_day = True
        if cursor.weekday() < 5 and cursor.date().isoformat() not in holidays:
            return False
        cursor += pd.Timedelta(days=1)
    return saw_missing_day


def _asset_class_from_parts(parts: tuple[str, ...]) -> str:
    for index, part in enumerate(parts):
        if part.lower() == "ohlcv" and index + 1 < len(parts):
            return parts[index + 1].lower()
    if len(parts) >= 2:
        return parts[1].lower()
    return ""


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


def validate_parquet(path: Path) -> ValidationResult:
    """Validate a single OHLCV Parquet file."""
    candidate = Path(path).expanduser()
    try:
        configured_root = (DATA_DIR / "ohlcv").resolve()
        rel = candidate.resolve().relative_to(configured_root)
    except (OSError, RuntimeError, ValueError):
        rel = candidate
    parts = rel.parts

    # Extract metadata from path: {source}/{asset_class}/.../{symbol}/{tf}.parquet
    source = parts[0] if len(parts) >= 4 else "unknown"
    symbol = path.parent.name
    timeframe = path.stem

    result = ValidationResult(
        path=str(rel),
        symbol=symbol,
        timeframe=timeframe,
        source=source,
    )

    try:
        df = pd.read_parquet(path)
    except Exception as e:
        result.status = "error"
        result.errors.append(f"Could not read parquet: {e}")
        return result

    df.columns = [c.lower() for c in df.columns]
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
            if _gap_is_expected_nontrading(gap_start, gap_end, asset_class, timeframe):
                continue

            result.gaps.append(
                {
                    "start": str(gap_start),
                    "end": str(gap_end),
                    "gap_minutes": round(gap_minutes, 1),
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
        r = validate_parquet(validated_path)

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

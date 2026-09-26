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
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

from shared.constants import DATA_DIR

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


def _gap_touches_holiday(gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> bool:
    """Whether a gap interval covers a NYSE holiday (B3, equity-like only)."""
    years = range(gap_start.year, gap_end.year + 1)
    holidays: set[str] = set()
    for year in years:
        holidays |= _nyse_holidays(year)
    cursor = gap_start.normalize()
    end = gap_end.normalize()
    while cursor <= end:
        if cursor.date().isoformat() in holidays:
            return True
        cursor += pd.Timedelta(days=1)
    return False


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
    rel = path.relative_to(DATA_DIR / "ohlcv") if str(path).startswith(str(DATA_DIR)) else path
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

            # Skip weekends for non-forex
            if gap_start.weekday() == 4 and gap_end.weekday() == 0:
                continue  # Friday→Monday is expected
            if gap_start.weekday() == 4 and gap_end.weekday() == 6:
                continue  # Friday→Sunday
            if gap_start.weekday() == 6 and gap_end.weekday() == 0:
                continue  # Sunday→Monday

            # B3: NYSE holidays are not data holes — but only for
            # equity-like paths (FX/metals print through US holidays).
            asset_class = str(parts[1]).strip().lower() if len(parts) >= 4 else ""
            if asset_class in _EQUITY_LIKE_ASSET_CLASSES and _gap_touches_holiday(
                gap_start, gap_end
            ):
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

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    # high should be >= max(open, close) and low should be <= min(open, close)
    high_violations = (h < o) | (h < c)
    low_violations = (l > o) | (l > c)
    hl_violations = h < l

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
        if symbol_filter and parquet.parent.name.upper() != symbol_filter.upper():
            continue

        logger.info("Validating %s ...", parquet.relative_to(ohlcv_root))
        r = validate_parquet(parquet)

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


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate OHLCV data quality")
    parser.add_argument("--symbol", help="Validate only this symbol")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Data directory")
    parser.add_argument("--report", help="Save JSON report to this path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    results = validate_all(data_dir, symbol_filter=args.symbol)

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


if __name__ == "__main__":
    main()

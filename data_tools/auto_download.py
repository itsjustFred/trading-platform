"""Ensure OHLCV data exists before runner scripts start a backtest."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow.parquet as pq

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
# fail. Defaults follow the thresholds proposed in the parent issue: an
# internal hole longer than INTERNAL_GAP_FAIL_DAYS (3 calendar days) fails the
# coverage check, a hole longer than INTERNAL_GAP_WARN_DAYS (1 calendar day)
# warns, and anything shorter is tolerated (source downtime).
#
# Thresholds deliberately count calendar days, not business days (the parent
# record states the fail threshold in calendar days): a regular FX weekend
# (~2 days) therefore warns, and only holes beyond 3 calendar days fail.
# Per-source overrides (crypto, holidays) are an explicit open item of the
# parent issue and are intentionally not implemented here.
INTERNAL_GAP_FAIL_DAYS = 3
INTERNAL_GAP_WARN_DAYS = 1


@dataclass
class InternalGap:
    """A hole between two consecutive bars inside the stored range."""

    start: str
    end: str
    gap_days: float

    def to_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "gap_days": round(self.gap_days, 2)}


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

    _fail_on_internal_gaps(data_root, requirements)
    _invalidate_catalog_cache(data_root)
    return downloaded


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
# A single long weekend reaches four calendar days: Friday and Monday both
# closed, with Saturday and Sunday between. Christmas and Easter routinely
# produce exactly that, and a 3-day allowance reported them as data holes.
LONG_WEEKEND_GAP_DAYS = 4.0


def timeframe_nominal_days(timeframe: str | None) -> float:
    """Nominal calendar spacing of one bar in ``timeframe``.

    The numeric prefix is part of the timeframe: ``5m`` bars are five minutes
    apart and ``4w`` bars are four weeks apart, so ignoring it would size the
    allowed hole against the wrong spacing.
    """
    normalized = str(timeframe or "").strip().lower()
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
    """Allowed hole length for ``timeframe``, widened for long-spaced series.

    ``allow_long_weekend`` raises the floor to cover a Friday-and-Monday
    closure. It is set only for the threshold that *fails* coverage: a long
    weekend is real market behaviour and must not block a backtest, but it is
    still worth warning about, so the warning threshold keeps the tighter floor
    that surfaces genuine intraday holes.
    """
    scaled = timeframe_nominal_days(timeframe) * GAP_SPACING_MULTIPLE
    floor = MIN_INTERNAL_GAP_THRESHOLD_DAYS
    if allow_long_weekend:
        floor = max(floor, LONG_WEEKEND_GAP_DAYS)
    return max(float(base_days), scaled, floor)


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
    the bars inside one file. The largest hole longer than ``threshold_days``
    (default: 1 calendar day) is reported — gaps are measured in calendar
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


def has_ohlcv_bars_in_range(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    data_dir: str | Path = "",
) -> bool:
    """Return whether canonical OHLCV data contains a valid bar in ``[start, end]``."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = normalize_timeframe(str(timeframe or "").strip())
    if not normalized_symbol or not normalized_timeframe:
        return False

    start_ts = pd.to_datetime(str(start or ""), utc=True, errors="coerce")
    end_ts = pd.to_datetime(str(end or ""), utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        return False

    ohlcv_root = _resolve_data_root(data_dir) / "ohlcv"
    matches = sorted(
        path
        for path in ohlcv_root.rglob(f"**/{normalized_timeframe}.parquet")
        if path.parent.name.upper() == normalized_symbol
    )
    return any(_parquet_has_bar_in_range(path, start_ts, end_ts) for path in matches)


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
        for required_timeframe in timeframes:
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
    ohlcv_root = data_root / "ohlcv"
    matches = sorted(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
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


def _gaps_overlapping_window(
    gaps: list[dict[str, object]], start: str | None, end: str | None
) -> list[dict[str, object]]:
    if start is None or end is None:
        return gaps
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
    symbol_dir_candidates = sorted((data_root / "ohlcv").rglob(f"**/{requirement.symbol}"))
    timeframe = normalize_timeframe(str(requirement.timeframe or "").strip())
    gaps: list[dict[str, object]] = []
    bounds = _coverage_for(data_root, requirement.symbol, timeframe)

    for candidate in symbol_dir_candidates:
        gaps.extend(find_internal_gaps(candidate, timeframe))

    error: str | None = None
    required_start = _normalize_coverage_date(requirement.start)
    required_end = _normalize_coverage_date(requirement.end)
    if bounds is None:
        error = (
            f"No {requirement.symbol} {timeframe} parquet found under {data_root / 'ohlcv'}"
        )
    else:
        if required_start is not None and required_start < bounds[0]:
            error = (
                f"Requested start {required_start} is before stored coverage start {bounds[0]}"
            )
        elif required_end is not None and required_end > bounds[1]:
            error = f"Requested end {required_end} is after stored coverage end {bounds[1]}"

    significant_gaps = _gaps_overlapping_window(
        [
            gap
            for gap in gaps
            if float(gap.get("gap_days", 0)) > scaled_gap_threshold_days(
                requirement.timeframe,
                INTERNAL_GAP_FAIL_DAYS,
                allow_long_weekend=True,
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
        "symbol": requirement.symbol,
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

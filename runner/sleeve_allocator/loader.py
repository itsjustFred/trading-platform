"""Sleeve return-series loading and business-day alignment (issue #307, phase 1).

A *sleeve* is one strategy × asset × timeframe run at a fixed base risk. The
allocator consumes **out-of-sample daily returns only**: matrix daily-equity
rows and walk-forward ``oos_equity_curve`` segments. Walk-forward payloads
carry OOS data by construction (only ``oos_equity_curve`` is read). Matrix
rows are full-window backtest curves — this module cannot verify they are
out-of-sample, so callers must assert it (``--matrix-is-oos`` at the CLI)
and the report discloses matrix sleeves as unverified.

Calendar contract
-----------------
Every sleeve is resampled onto ONE business-day (Monday–Friday) calendar:

* returns observed on Saturday/Sunday are compounded into the next business
  day (Monday), so a crypto sleeve's weekend P&L is not lost and an equity
  sleeve's flat weekend does not invent observations;
* business days where a sleeve has no observation stay ``NaN`` — never
  zero-filled, because zero-filling deflates volatility and biases correlations
  toward zero (the #229 failure mode). Downstream estimation uses
  pairwise-complete statistics; the harness treats an unobserved sleeve-day as
  flat *only* when applying already-chosen weights.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from data_tools.auto_download import is_us_equity_holiday

__all__ = [
    "SleeveMetadata",
    "SleeveReturns",
    "TradePeriod",
    "align_sleeve_returns",
    "load_sleeve_returns",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradePeriod:
    """An out-of-sample window and the trades observed in it.

    ``trades=None`` means the payload did not report a count for the
    period — unknown evidence, not zero trades.
    """

    start: str
    end: str
    trades: int | None


@dataclass(frozen=True)
class SleeveMetadata:
    """Per-sleeve provenance and eligibility evidence."""

    sleeve_id: str
    strategy: str
    symbol: str
    timeframe: str
    source: str
    run_id: str | None = None
    source_path: str | None = None
    first_date: str | None = None
    last_date: str | None = None
    n_observations: int = 0
    total_trades: int | None = None
    trade_periods: tuple[TradePeriod, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SleeveReturns:
    """Aligned daily sleeve returns plus per-sleeve metadata.

    ``returns`` is a business-day-indexed DataFrame; columns are sleeve ids,
    values are simple daily returns (fractions, e.g. 0.01 = 1%). ``NaN`` marks
    a business day the sleeve did not observe — it is missing data, not a
    zero return.
    """

    returns: pd.DataFrame
    metadata: dict[str, SleeveMetadata]

    @property
    def sleeve_ids(self) -> list[str]:
        return [str(c) for c in self.returns.columns]

    @property
    def calendar(self) -> pd.DatetimeIndex:
        return self.returns.index

    @property
    def empty(self) -> bool:
        return self.returns.empty


def _sleeve_id(strategy: Any, symbol: Any, timeframe: Any) -> str:
    parts = [str(p).strip() for p in (strategy, symbol, timeframe) if str(p or "").strip()]
    return "|".join(parts) if parts else "unknown"


def _normalise_day_index(index: pd.Index) -> pd.DatetimeIndex:
    """Naive, midnight-normalised dates for any datetime-like index."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.normalize()


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _next_business_day(day: pd.Timestamp) -> pd.Timestamp:
    """Roll a calendar day forward past the weekend (Sat/Sun -> Monday)."""
    while day.weekday() >= 5:
        day = day + pd.Timedelta(days=1)
    return day


def _compound(values: list[float]) -> float:
    growth = 1.0
    for value in values:
        growth *= 1.0 + value
    return growth - 1.0


def _gap_is_only_weekend_or_holiday(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    """True when every calendar day strictly inside ``(start, end)`` is a
    weekend day or a US equity holiday — the market could not have traded,
    so a close-to-close move across the gap is still one trading move.
    Mirrors ``runner.walkforward._gap_contains_only_weekend``.
    """
    cursor = start.normalize() + pd.Timedelta(days=1)
    end_day = end.normalize()
    while cursor < end_day:
        if cursor.weekday() < 5 and not is_us_equity_holiday(cursor):
            return False
        cursor += pd.Timedelta(days=1)
    return True


def _to_business_day_series(series: pd.Series) -> pd.Series:
    """Map calendar-day returns onto business days, compounding collisions.

    A return observed on a weekend day is assigned to the next business day
    and compounded with any other returns landing there, so Monday carries the
    full Friday-close -> Monday-close move for 24/7 sleeves.
    """
    grouped: dict[pd.Timestamp, list[float]] = {}
    for timestamp, value in series.items():
        if pd.isna(value):
            continue
        day = _next_business_day(pd.Timestamp(timestamp).normalize())
        grouped.setdefault(day, []).append(float(value))
    if not grouped:
        return pd.Series(dtype=float)
    return pd.Series(
        {day: _compound(values) for day, values in grouped.items()},
        dtype=float,
    ).sort_index()


def align_sleeve_returns(
    returns: Mapping[str, Any],
    metadata: Mapping[str, SleeveMetadata] | None = None,
) -> SleeveReturns:
    """Align per-sleeve calendar-day return series on one business-day calendar.

    ``returns`` maps sleeve id -> a Series indexed by a DatetimeIndex of
    daily simple returns. ``metadata`` optionally supplies provenance;
    missing entries get a metadata record derived from the sleeve id.
    Business days with no observation for ANY sleeve are dropped from the
    calendar; per-sleeve gaps stay ``NaN``. Walk-forward fold returns are
    produced upstream by ``_oos_fold_returns``, which leaves the first day
    after a gap unobserved when the gap contains a weekday that was not a
    US equity holiday (mirroring ``runner.walkforward``): a multi-day move
    is never booked as a one-day return. Matrix ``daily_return`` values
    are taken per observation as recorded.
    """
    if not returns:
        return SleeveReturns(returns=pd.DataFrame(), metadata={})

    business_series: dict[str, pd.Series] = {}
    for sleeve_id, raw in returns.items():
        series = pd.Series(raw, dtype=float)
        if series.empty:
            continue
        if not isinstance(series.index, pd.DatetimeIndex):
            raise ValueError(
                f"sleeve {sleeve_id!r}: return series needs a datetime index "
                f"(got {type(series.index).__name__}); pass a Series with a "
                "DatetimeIndex, not a plain list or RangeIndex"
            )
        try:
            parsed_index = pd.to_datetime(series.index, utc=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"sleeve {sleeve_id!r}: return series needs a datetime index"
            ) from exc
        series.index = _normalise_day_index(parsed_index)
        series = series[~series.index.duplicated(keep="last")]
        series = series[np.isfinite(series.to_numpy(dtype=float))]
        aligned = _to_business_day_series(series)
        if not aligned.empty:
            business_series[str(sleeve_id)] = aligned

    if not business_series:
        return SleeveReturns(returns=pd.DataFrame(), metadata={})

    first = min(s.index.min() for s in business_series.values())
    last = max(s.index.max() for s in business_series.values())
    calendar = pd.bdate_range(first, last)
    frame = pd.DataFrame(business_series).reindex(calendar)
    # A business day nobody observed carries no information; drop it rather
    # than publish an all-NaN row that would dilute day counts downstream.
    frame = frame.dropna(how="all")

    metadata = metadata or {}
    built: dict[str, SleeveMetadata] = {}
    for sleeve_id in frame.columns:
        observed = frame[sleeve_id].dropna()
        base = metadata.get(sleeve_id) or _default_metadata(sleeve_id)
        built[sleeve_id] = replace(
            base,
            sleeve_id=sleeve_id,
            first_date=observed.index[0].date().isoformat() if len(observed) else None,
            last_date=observed.index[-1].date().isoformat() if len(observed) else None,
            n_observations=len(observed),
        )
    return SleeveReturns(returns=frame, metadata=built)


def _default_metadata(sleeve_id: str) -> SleeveMetadata:
    parts = str(sleeve_id).split("|")
    strategy = parts[0] if len(parts) > 0 else ""
    symbol = parts[1] if len(parts) > 1 else ""
    timeframe = parts[2] if len(parts) > 2 else ""
    return SleeveMetadata(
        sleeve_id=sleeve_id,
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        source="aligned",
    )


# ---------------------------------------------------------------------------
# matrix_daily_equity.csv
# ---------------------------------------------------------------------------

def _read_matrix_csv(path: Path) -> dict[str, dict[str, Any]]:
    """Group matrix daily-equity rows into per-sleeve observation lists."""
    sleeves: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            strategy = str(row.get("strategy") or "").strip()
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            if not (strategy and symbol and timeframe):
                continue
            sleeve_id = _sleeve_id(strategy, symbol, timeframe)
            entry = sleeves.setdefault(
                sleeve_id,
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "run_id": "",
                    "observations": [],
                },
            )
            run_id = str(row.get("run_id") or "").strip()
            if run_id and not entry["run_id"]:
                entry["run_id"] = run_id
            timestamp = pd.to_datetime(row.get("time"), utc=True, errors="coerce")
            if pd.isna(timestamp):
                continue
            entry["observations"].append(
                {
                    "time": pd.Timestamp(timestamp),
                    "equity": _finite_float(row.get("equity")),
                    "daily_return": _finite_float(row.get("daily_return")),
                }
            )
    return sleeves


def _matrix_sleeve_series(entry: dict[str, Any]) -> pd.Series:
    """Calendar-day return series for one matrix sleeve.

    ``daily_return`` is authoritative when present (it is already a fraction).
    When a row lacks it, the return is recovered from the equity column against
    the previous observation of the SAME sleeve — never against another sleeve.
    """
    observations = sorted(entry["observations"], key=lambda o: o["time"])
    by_day: dict[pd.Timestamp, float] = {}
    previous_equity: float | None = None
    for obs in observations:
        day = obs["time"].tz_convert("UTC").tz_localize(None).normalize()
        value = obs["daily_return"]
        if value is None and obs["equity"] is not None and previous_equity:
            if previous_equity > 0:
                value = obs["equity"] / previous_equity - 1.0
        if obs["equity"] is not None:
            previous_equity = obs["equity"]
        if value is not None:
            by_day[day] = value
    return pd.Series(by_day, dtype=float).sort_index()


def _load_matrix_summary_trades(path: Path | str | None) -> dict[str, int]:
    """run_id -> total_trades from a ``matrix_summary.json`` payload."""
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("matrix summary %s unreadable, trade counts skipped: %s", path, exc)
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("runs"), list):
        logger.warning("matrix summary %s has no 'runs' list, trade counts skipped", path)
        return {}
    trades: dict[str, int] = {}
    for row in payload.get("runs") or []:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or "").strip()
        count = _finite_float(row.get("total_trades"))
        if run_id and count is not None:
            trades[run_id] = int(count)
    return trades


# ---------------------------------------------------------------------------
# walk-forward result JSONs
# ---------------------------------------------------------------------------


def _equity_point_time(value: Any) -> pd.Timestamp | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # runner.walkforward writes OOS points as int Unix SECONDS; letting
        # pandas guess would read them as nanoseconds and collapse the
        # curve to 1970, silently dropping the sleeve.
        try:
            parsed = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        try:
            parsed = pd.to_datetime(value, utc=True, errors="coerce")
        except (TypeError, ValueError, OverflowError):
            return None
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def _oos_fold_returns(curve: Any) -> pd.Series:
    """Daily close-to-close returns inside ONE out-of-sample fold.

    Mirrors ``runner.walkforward._daily_close_series``: last observation per UTC
    day, then returns between consecutive observed days only. The first raw
    equity point anchors the fold — the first observed day's return is measured
    against it (the fold's starting mark), and equity is never chained across
    folds (each fold restarts at its own capital).
    """
    if not isinstance(curve, list):
        return pd.Series(dtype=float)
    observations: list[tuple[pd.Timestamp, float]] = []
    for point in curve:
        if not isinstance(point, dict):
            continue
        timestamp = _equity_point_time(point.get("time"))
        equity = _finite_float(point.get("equity"))
        if timestamp is not None and equity is not None:
            observations.append((timestamp, equity))
    if len(observations) < 2:
        return pd.Series(dtype=float)
    series = pd.Series(
        [equity for _, equity in observations],
        index=pd.DatetimeIndex([ts for ts, _ in observations]),
        dtype=float,
    ).sort_index(kind="stable")
    series = series[~series.index.duplicated(keep="last")]
    daily = series.resample("D").last().dropna()
    if len(daily) < 2:
        return pd.Series(dtype=float)
    returns = daily / daily.shift(1) - 1.0
    # A gap containing a weekday that was not a US equity holiday means
    # observations are missing on days the market was open: the
    # close-to-close move spans several days and must not be booked as a
    # one-day return. runner.walkforward skips such pairs when aggregating
    # OOS metrics; here the post-gap day stays unobserved instead.
    observed_days = daily.index
    for position in range(1, len(observed_days)):
        gap_days = (observed_days[position] - observed_days[position - 1]).days
        if gap_days > 1 and not _gap_is_only_weekend_or_holiday(
            observed_days[position - 1], observed_days[position]
        ):
            returns.iloc[position] = np.nan
    # The first raw equity point anchors the fold (it is the fold's starting
    # mark, typically the test-window capital): the FIRST observed day's
    # return is close(day 1) / anchor - 1, not dropped. Without this every
    # fold silently loses its first day of OOS performance.
    anchor_equity = float(series.iloc[0])
    if math.isfinite(anchor_equity) and anchor_equity > 0:
        returns.iloc[0] = float(daily.iloc[0]) / anchor_equity - 1.0
    return returns.dropna().astype(float)


def _trade_count(stats: Any) -> int | None:
    if not isinstance(stats, dict):
        return None
    value = _finite_float(stats.get("total_trades"))
    return int(value) if value is not None else None


def _load_walkforward_file(path: Path) -> dict[str, Any] | None:
    """Return sleeve info for one walk-forward payload, or None."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"walk-forward result {path} is unreadable: {exc}") from exc
    folds = payload.get("folds")
    if not isinstance(folds, list) or not folds:
        return None

    strategy_raw = str(payload.get("strategy") or "").strip()
    strategy = (
        Path(strategy_raw).stem
        if strategy_raw
        else str(payload.get("mode") or "walkforward")
    )
    symbols = payload.get("symbols")
    if isinstance(symbols, list) and symbols:
        symbol = ",".join(str(s) for s in symbols)
    else:
        symbol = str(payload.get("symbol") or "")
    timeframe = str(payload.get("timeframe") or "")
    sleeve_id = _sleeve_id(strategy, symbol, timeframe)

    series_parts: list[pd.Series] = []
    trade_periods: list[TradePeriod] = []
    total_trades = 0
    trades_seen = False
    trades_missing = False
    for fold in folds:
        if not isinstance(fold, dict):
            continue
        returns = _oos_fold_returns(fold.get("oos_equity_curve"))
        if returns.empty:
            continue
        series_parts.append(returns)
        trades = _trade_count(fold.get("oos_stats"))
        if trades is not None:
            trades_seen = True
            total_trades += trades
        else:
            trades_missing = True
        start = str(fold.get("test_start") or returns.index[0].date().isoformat())
        end = str(fold.get("test_end") or returns.index[-1].date().isoformat())
        trade_periods.append(
            TradePeriod(start=start, end=end, trades=trades)
        )
    if not series_parts:
        return None

    combined = pd.concat(series_parts).sort_index(kind="stable")
    combined = combined[~combined.index.duplicated(keep="last")]
    info = {
        "sleeve_id": sleeve_id,
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "walkforward",
        "source_path": str(path),
        "series": combined,
        "total_trades": (
            total_trades if trades_seen and not trades_missing else None
        ),
        "trade_periods": tuple(trade_periods),
    }
    return info


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _unique_sleeve_id(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    suffix = 2
    while f"{base}~{suffix}" in used:
        suffix += 1
    candidate = f"{base}~{suffix}"
    used.add(candidate)
    return candidate


def load_sleeve_returns(
    *,
    matrix_csv: str | Path | None = None,
    matrix_summary: str | Path | None = None,
    walkforward_paths: Iterable[str | Path] = (),
    trade_counts: Mapping[str, int] | None = None,
) -> SleeveReturns:
    """Load OOS sleeve daily returns from matrix and/or walk-forward outputs.

    Parameters
    ----------
    matrix_csv:
        Path to a ``matrix_daily_equity.csv`` (columns strategy, symbol,
        timeframe, run_id, time, equity, daily_return). These are full-window
        backtest curves; callers must only feed windows that are out-of-sample
        for the strategies involved.
    matrix_summary:
        Optional ``matrix_summary.json``; its ``runs[].total_trades`` supplies
        trade counts keyed by ``run_id``.
    walkforward_paths:
        Walk-forward result JSONs (``runner.walkforward`` payloads). Only
        ``oos_equity_curve`` fold segments are read — train folds and
        in-sample stats are ignored by construction.
    trade_counts:
        Optional explicit ``sleeve_id -> trade count`` overrides, applied
        last. For walk-forward sleeves an override is authoritative: it
        replaces the fold ``trade_periods`` as well, so the sleeve carries
        a single ``total_trades`` basis — a full-sample figure under
        ``as_of`` that eligibility rejects unless the caller opts in.

    Sleeve ids are ``strategy|symbol|timeframe``. Collisions between sources
    (e.g. the same strategy in a matrix run and a walk-forward run) are
    disambiguated with a deterministic ``~N`` suffix; the first occurrence wins
    the bare id.
    """
    series_map: dict[str, pd.Series] = {}
    metadata_map: dict[str, SleeveMetadata] = {}
    used_ids: set[str] = set()
    overrides = dict(trade_counts or {})

    if matrix_csv is not None:
        summary_trades = _load_matrix_summary_trades(matrix_summary)
        for base_id, entry in sorted(_read_matrix_csv(Path(matrix_csv)).items()):
            series = _matrix_sleeve_series(entry)
            if series.empty:
                continue
            sleeve_id = _unique_sleeve_id(base_id, used_ids)
            run_id = entry["run_id"] or None
            trades = overrides.get(sleeve_id)
            if trades is None and run_id:
                trades = summary_trades.get(run_id)
            metadata_map[sleeve_id] = SleeveMetadata(
                sleeve_id=sleeve_id,
                strategy=entry["strategy"],
                symbol=entry["symbol"],
                timeframe=entry["timeframe"],
                source="matrix",
                run_id=run_id,
                source_path=str(matrix_csv),
                total_trades=trades,
            )
            series_map[sleeve_id] = series

    for raw_path in sorted(walkforward_paths, key=str):
        path = Path(raw_path)
        info = _load_walkforward_file(path)
        if info is None:
            continue
        base_id = str(info["sleeve_id"])
        sleeve_id = _unique_sleeve_id(base_id, used_ids)
        trades = overrides.get(sleeve_id, info["total_trades"])
        trade_periods = info["trade_periods"]
        if sleeve_id in overrides:
            # An explicit trade-count override is authoritative: it replaces
            # the fold periods too, so eligibility sees ONE total_trades
            # basis (full-sample under as_of) instead of silently preferring
            # the periods the override was meant to replace.
            trade_periods = ()
        metadata_map[sleeve_id] = SleeveMetadata(
            sleeve_id=sleeve_id,
            strategy=info["strategy"],
            symbol=info["symbol"],
            timeframe=info["timeframe"],
            source="walkforward",
            source_path=info["source_path"],
            total_trades=trades,
            trade_periods=trade_periods,
            extra={"deduplicated_from": base_id} if sleeve_id != base_id else {},
        )
        series_map[sleeve_id] = info["series"]

    return align_sleeve_returns(series_map, metadata=metadata_map)

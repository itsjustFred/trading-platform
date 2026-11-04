"""Daily equity curve construction from backtest trades."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, NamedTuple

import pandas as pd

from shared.logging import get_logger
from shared.models import BacktestResult, EquityPoint, Trade

logger = get_logger("runner.daily_equity")

MAX_PRESERVED_CURVE_GAP = pd.Timedelta(days=4)
MAX_PRESERVED_BOUNDARY_GAP = pd.Timedelta(days=4)
MIN_PRESERVED_DAY_COVERAGE = 0.5

#: An engine curve may replace the calendar fill only when it carries a path,
#: i.e. when it is not endpoint-only. Two points (start + end) describe the
#: same total return the calendar fill already describes, and adopting them
#: would publish a 2-point "daily" curve that hides every intermediate day.
MIN_RECONCILING_ENGINE_CURVE_POINTS = 3

#: Half of the last representable place of the persisted equity series. Every
#: engine rounds an equity observation to cents (``round(running, 2)`` — see
#: ``runner/engines/vectorbt_adapter.py`` and ``runner/engines/_shared.py``),
#: so a persisted value can sit up to half a cent away from the exact one.
EQUITY_HALF_CENT = 0.005
#: One whole cent: the tolerance's absolute floor and its per-trade term. Two
#: half-cent roundings (see :func:`_reconciliation_tolerance`) reach it.
EQUITY_CENT = 2 * EQUITY_HALF_CENT
#: Machine epsilon for float64. Summing ``n`` doubles of magnitude ``m`` is
#: only accurate to about ``n * 2**-52 * m``; the reconciliation tolerance
#: scales with it so an account large enough to make that term visible (a
#: $1e12 account, or a six-figure trade count) is not held to a standard the
#: arithmetic cannot meet.
FLOAT64_EPSILON = 2.0**-52
#: Safety factor on the float64 accumulation term. Kahan-free ``sum()`` in
#: CPython is naive, and the per-term error bound is pessimistic; 8x leaves
#: headroom without ever letting a real disagreement hide behind it (a genuine
#: contradiction between an engine curve and the trade list is orders of
#: magnitude larger than any of these terms).
FLOAT64_ACCUMULATION_SAFETY = 8.0

WindowStartBasis = Literal["engine", "net_ledger"]


class WindowStartingEquity(NamedTuple):
    """Equity at the window start and the basis it was taken from.

    ``basis`` is published as ``meta.data["window_start_basis"]``:

    ``"engine"``
        No ledger fallback: the configured initial capital, an engine point
        that agreed with the ledger within one cent, or no engine point at
        all (nothing to disagree with).
    ``"net_ledger"``
        An engine point disagreed with the realized net PnL beyond one cent,
        so the ledger value was used. Always accompanied by a warning.

    The field is dropped only when no window base is computed at all (no
    trades and no engine curve).
    """

    equity: float
    basis: WindowStartBasis


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp; None when it cannot be understood."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_day_bound(value: str | None, *, is_end: bool) -> datetime | None:
    """Normalize a YYYY-MM-DD (or ISO) boundary for trade-time comparison."""
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    raw = str(value or "").strip()
    if is_end and "T" not in raw and ":" not in raw:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _last_exit_time(trade: Trade) -> str | None:
    """The trade's last non-empty exit time, or None when it has none."""
    exit_time = None
    for exit_ in trade.exits:
        if exit_.time:
            exit_time = exit_.time
    return exit_time


def _parsed_in_window(
    parsed: datetime | None,
    start_bound: datetime | None,
    end_bound: datetime | None,
) -> bool:
    """Window test on an already-parsed timestamp (None never passes)."""
    if parsed is None:
        return False
    if start_bound and parsed < start_bound:
        return False
    if end_bound and parsed > end_bound:
        return False
    return True


def _trade_in_window(
    trade: Trade,
    start_bound: datetime | None,
    end_bound: datetime | None,
) -> bool:
    exit_time = _last_exit_time(trade)
    if not exit_time:
        return False
    parsed = _parse_ts(exit_time)
    # A timestamp we cannot understand must not silently pass the filter.
    return _parsed_in_window(parsed, start_bound, end_bound)


def _in_window(value: str, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    return _parsed_in_window(_parse_ts(value), start_bound, end_bound)


def _trade_net_pnl(trade: Trade) -> float | None:
    """The trade's net PnL, or None when it is not a finite number."""
    pnl = (
        float(trade.net_pnl)
        if trade.net_pnl is not None
        else float(trade.pnl or 0.0) - float(trade.fees or 0.0)
    )
    return pnl if math.isfinite(pnl) else None


def _net_pnl_before(
    result: BacktestResult,
    cutoff: datetime,
    *,
    inclusive: bool = False,
) -> float:
    """Sum net trade PnL of exits before (or at, when inclusive) ``cutoff``."""
    total = 0.0
    for trade in result.trades:
        exit_time = _parse_ts(_last_exit_time(trade))
        if exit_time is None:
            continue
        if exit_time > cutoff or (not inclusive and exit_time == cutoff):
            continue
        pnl = _trade_net_pnl(trade)
        if pnl is None:
            continue
        total += pnl
        if not math.isfinite(total):
            raise ValueError("Net trade PnL accumulation is non-finite")
    return total


def _reconciliation_tolerance(*, expected_final: float, trade_count: int) -> float:
    """How far the engine's final equity may sit from the trade-derived total.

    The comparison is between two *cent-quantised sums of the same trades*, so
    the bound is derived from the arithmetic both sides actually perform instead
    of from a round-number dollar amount:

    1. **Per-trade cent drift.** ``runner/engines/vectorbt_adapter.py`` stores
       each trade as ``pnl=round(pnl, 2)``, ``fees=round(fees, 2)`` and
       ``net_pnl=round(pnl - fees, 2)``, and the curve as
       ``equity=round(running, 2)``. For one trade the final observation is up
       to half a cent from the exact running total and the trade's own
       ``net_pnl`` rounding adds at most another half cent: one whole cent. An
       engine that accumulates its curve from the *unrounded* ``pnl - fees``
       while this module sums the *stored* ``net_pnl`` disagrees by the same
       amount, and those differences accumulate linearly in the trade count
       instead of cancelling. One cent per trade covers both halves; a separate
       flat cent on top would double-count the first trade.
    2. **Float64 accumulation.** Naive summation of ``n`` doubles of magnitude
       ``m`` carries an error of about ``n * 2**-52 * m``, scaled by 8 for the
       per-term error bound. This is the relative epsilon: it is the only term
       that grows with the account, because cent quantisation does not.

    The two terms are combined with ``max``, not addition: they describe the
    same disagreement from two angles, and the per-trade term already contains
    the endpoint quantisation for the first trade. That keeps the single-trade
    bound at exactly one cent — the same absolute cliff
    ``_preserves_engine_curve`` applies — while a twenty-trade ledger still gets
    its twenty-cent allowance.

    A genuine contradiction — an engine curve ending somewhere the trades cannot
    reach, e.g. 105000.0 against 100500.0 — sits four orders of magnitude above
    the resulting bound for any realistic trade count, so this gate is not
    weaker than a flat tolerance. A flat $0.05 was simply wrong in the other
    direction too: it *rejected* curves that were off by six cents across twenty
    trades, discarding the mark-to-market path this function exists to preserve.
    """
    trades = max(int(trade_count), 1)
    float64_drift = (
        FLOAT64_ACCUMULATION_SAFETY * trades * FLOAT64_EPSILON * abs(expected_final)
    )
    return max(trades * EQUITY_CENT, float64_drift)


def _fill_engine_curve_gaps(
    anchor_date: date,
    anchor_equity: float,
    last_by_day: dict[str, tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Order the observed engine-curve days and insert flat points for gaps.

    ``build_daily_equity_curve`` emits a point for *every* calendar day of the
    window, zero-return days included. An engine curve only carries a point for
    the days it happened to observe, and deriving returns from consecutive
    *observed* days books a multi-day move as a single day: a four-calendar-day
    span of two observations produced two returns that were then annualised as
    if the whole move had happened in one day, roughly doubling the reported
    figure — and the size of the error depended on how many days the engine
    curve happened to skip.

    Every calendar day from ``anchor_date`` through the last observed day is
    therefore emitted: an observed day at its own equity, a skipped day flat at
    the previous equity (an explicit ``0.0`` return). A day with no observation
    genuinely has no measurable return, which is exactly what the calendar fill
    already reports for a day with no closed trade. ``anchor_date`` and
    ``anchor_equity`` are the opening point of the curve, so the flat period
    before the engine's first observation is filled too.
    """
    ordered: list[tuple[datetime, float]] = []
    cursor_date = anchor_date
    cursor_equity = anchor_equity
    for day in sorted(last_by_day):
        timestamp, equity = last_by_day[day]
        observed_date = timestamp.date()
        # Fill only the days strictly BEFORE this observation: the observation
        # itself is appended below, so including its own day would emit two
        # points for one calendar day.
        while cursor_date + timedelta(days=1) < observed_date:
            cursor_date += timedelta(days=1)
            ordered.append(
                (
                    datetime.combine(
                        cursor_date, datetime.min.time(), tzinfo=timezone.utc
                    ),
                    cursor_equity,
                )
            )
        ordered.append((timestamp, equity))
        cursor_date = observed_date
        cursor_equity = equity
    return ordered


def _daily_points_from_engine_curve(
    result: BacktestResult,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    starting_equity: float | None = None,
) -> tuple[list[EquityPoint], list[dict[str, Any]]] | None:
    """Compact a reconciling engine curve to one point per calendar day.

    The calendar fill can only see CLOSED trades, so an open position's
    mark-to-market drawdown is invisible to it. When the engine's own curve
    reconciles with the realized trades (final equity == starting equity + net
    PnL) it is the more faithful daily series and is preferred. Days the engine
    curve skipped are emitted as explicit flat points (see
    :func:`_fill_engine_curve_gaps`) so no multi-day move is booked as a single
    day, and the series is carried flat through the window end so it describes
    the same period the calendar fill does — otherwise the same window would
    publish a different number of daily returns depending on which path built
    it, and Sharpe's denominator would depend on where the engine stopped
    observing. Returns None when the curve does not reconcile, is not parseable,
    or is endpoint-only (fewer than ``MIN_RECONCILING_ENGINE_CURVE_POINTS``
    points carry no path).
    """
    if not result.trades:
        return None

    curve: list[tuple[datetime, float]] = []
    for point in result.equity_curve or []:
        parsed = _parse_ts(point.time)
        try:
            equity = float(point.equity)
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed is None or not math.isfinite(equity):
            continue
        curve.append((parsed.astimezone(timezone.utc), equity))
    if len(curve) < MIN_RECONCILING_ENGINE_CURVE_POINTS:
        return None
    curve.sort(key=lambda item: item[0])

    # A curve that stops before the last trade it is supposed to describe
    # cannot be the window's path, however well its endpoint happens to match.
    # Compared by UTC day: an engine curve stamps its closes at the start of the
    # day it closes, so an intraday exit on the curve's last day is still
    # covered by that day's observation.
    trade_exits = [
        parsed
        for trade in result.trades
        if (parsed := _parse_ts(_last_exit_time(trade))) is not None
    ]
    if trade_exits and curve[-1][0].date() < max(trade_exits).date():
        return None

    # A windowed aggregation starts from the equity the account actually had at
    # the window start, so the engine curve is reconciled against that base and
    # not against the account's original capital.
    base_equity = float(
        result.meta.initial_capital if starting_equity is None else starting_equity
    )
    if not math.isfinite(base_equity):
        return None
    expected_final = base_equity + sum(
        pnl for trade in result.trades if (pnl := _trade_net_pnl(trade)) is not None
    )
    tolerance = _reconciliation_tolerance(
        expected_final=expected_final, trade_count=len(result.trades)
    )
    if abs(curve[-1][1] - expected_final) > tolerance:
        return None

    baseline_ts = start or _parse_ts(result.meta.backtest_start) or curve[0][0]
    baseline = baseline_ts.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    points = [EquityPoint(time=baseline.isoformat(), equity=round(base_equity, 2))]
    daily_returns: list[dict[str, Any]] = []
    previous_equity = base_equity

    start_index = 0
    if (
        curve[0][0].date() == baseline.date()
        and abs(curve[0][1] - base_equity) <= tolerance
    ):
        start_index = 1

    last_by_day: dict[str, tuple[datetime, float]] = {}
    for timestamp, equity in curve[start_index:]:
        # Anything BEFORE the baseline day is outside the window. A later
        # observation on the baseline day itself is that day's close and is
        # kept: dropping it would silently discard the first day's move, which
        # is the same defect the daily-return series had.
        if timestamp.date() < baseline.date():
            continue
        last_by_day[timestamp.date().isoformat()] = (timestamp, equity)

    ordered = _fill_engine_curve_gaps(baseline.date(), base_equity, last_by_day)

    # Carry the flat tail through the window end. The calendar fill emits every
    # day of the window, so a series that stopped at the engine's last
    # observation would report fewer daily returns for the same window — a
    # shorter Sharpe sample decided by where the engine happened to stop.
    if end is not None and ordered:
        end_day = end.astimezone(timezone.utc).date()
        cursor_date, cursor_equity = ordered[-1][0].date(), ordered[-1][1]
        while cursor_date < end_day:
            cursor_date += timedelta(days=1)
            ordered.append(
                (
                    datetime.combine(
                        cursor_date, datetime.min.time(), tzinfo=timezone.utc
                    ),
                    cursor_equity,
                )
            )

    for timestamp, equity in ordered:
        points.append(EquityPoint(time=timestamp.isoformat(), equity=round(equity, 2)))
        daily_return = (
            (equity - previous_equity) / previous_equity if previous_equity > 0 else 0.0
        )
        # Same contract as the calendar fill: the exact value, not the cent
        # rounding the persisted point carries.
        daily_returns.append(
            {
                "date": timestamp.date().isoformat(),
                "return": daily_return,
                "equity": equity,
            }
        )
        previous_equity = equity

    if len(points) < 2:
        return None
    return points, daily_returns


def _window_starting_equity(
    result: BacktestResult,
    start_bound: datetime | None,
) -> WindowStartingEquity:
    """Equity at the window start, plus the basis it was taken from.

    A windowed aggregation must start from the equity the account actually had
    at the window start, not from the initial capital: otherwise every return
    inside the window is computed against the wrong base.  When the engine
    curve already carries points before the window, the reconstructed value is
    reconciled against them with a one-cent tolerance and the engine value is
    preferred only when it agrees.

    A disagreement is not silent: it warns once per call (once per attach/run,
    never per trade or day) and reports ``basis="net_ledger"`` so the published
    report shows that the window base came from the realized net ledger instead
    of the engine curve.
    """
    initial_capital = float(result.meta.initial_capital)
    if not math.isfinite(initial_capital):
        raise ValueError("Initial capital must be finite")
    if start_bound is None:
        return WindowStartingEquity(initial_capital, "engine")

    prior_points: list[tuple[datetime, float]] = []
    for point in result.equity_curve:
        point_time = _parse_ts(point.time)
        equity = float(point.equity)
        if point_time is None or point_time >= start_bound or not math.isfinite(equity):
            continue
        prior_points.append((point_time, equity))

    starting_equity = initial_capital + _net_pnl_before(result, start_bound)
    if not prior_points:
        # No engine point to reconcile against: the ledger is the only source
        # and there is no divergence to report.
        return WindowStartingEquity(starting_equity, "engine")

    reconciled_through, reconciled_equity = max(prior_points, key=lambda item: item[0])
    net_through_reconciled = _net_pnl_before(result, reconciled_through, inclusive=True)
    expected_equity = initial_capital + net_through_reconciled
    if not math.isclose(
        reconciled_equity,
        expected_equity,
        rel_tol=0.0,
        abs_tol=0.01,
    ):
        # The engine point disagrees with the net ledger: trust the ledger, but
        # make the fallback visible. One warning per call keeps a systematic
        # divergence (e.g. an engine reporting gross instead of net equity)
        # discoverable without logging once per trade or day.
        basis: WindowStartBasis = "net_ledger"
        logger.warning(
            "Engine starting equity disagrees with the net ledger; "
            "using the realized net ledger as the window base",
            extra={
                "data": {
                    "window_start_basis": basis,
                    "engine_equity": reconciled_equity,
                    "ledger_equity": expected_equity,
                    "delta": reconciled_equity - expected_equity,
                    "start_bound": start_bound.isoformat(),
                }
            },
        )
        return WindowStartingEquity(starting_equity, basis)
    return WindowStartingEquity(
        reconciled_equity + (starting_equity - initial_capital - net_through_reconciled),
        "engine",
    )


def _strictly_increasing_curve(
    curve: list[EquityPoint],
    inception: datetime,
    end_bound: datetime | None,
) -> list[EquityPoint] | None:
    """Normalize a curve to strictly increasing, finite timestamps.

    Duplicate timestamps are coalesced (the later point wins) and points that
    still collide with the previous timestamp are shifted by one microsecond.
    Returns None when such a shift would leave the requested window.
    """
    coalesced: list[EquityPoint] = []
    previous_source: datetime | None = None
    for point in curve:
        current = _parse_ts(point.time)
        if current is None:
            raise ValueError("Equity curve contains an invalid timestamp")
        if previous_source is not None and current < previous_source:
            raise ValueError("Equity curve timestamps are out of order")
        if previous_source is not None and current == previous_source:
            coalesced[-1] = point
        else:
            coalesced.append(point)
        previous_source = current

    normalized: list[EquityPoint] = []
    previous = inception
    for point in coalesced:
        current = _parse_ts(point.time)
        if current is None or current < previous:
            raise ValueError("Equity curve timestamps are not strictly ordered")
        equity = float(point.equity)
        if not math.isfinite(equity):
            raise ValueError("Equity curve contains non-finite equity")
        if current <= previous:
            current = previous + timedelta(microseconds=1)
            if end_bound is not None and current > end_bound:
                return None
        normalized.append(EquityPoint(time=current.isoformat(), equity=equity))
        previous = current
    return normalized


def _normalize_engine_curve(curve: list[EquityPoint]) -> list[EquityPoint]:
    """Validate an engine-provided curve without inventing an inception point.

    Duplicate timestamps are coalesced and colliding ones shifted by a
    microsecond, so the published curve is strictly increasing and finite.  An
    unparseable timestamp, an out-of-order point or a non-finite equity is a
    corrupt curve and raises instead of being published.
    """
    if not curve:
        return []
    first_time = _parse_ts(curve[0].time)
    if first_time is None:
        raise ValueError("Equity curve contains an invalid timestamp")
    normalized = _strictly_increasing_curve(
        curve,
        first_time - timedelta(microseconds=1),
        None,
    )
    return normalized or []


def _scoped_view(
    result: BacktestResult, start_bound: datetime | None, end_bound: datetime | None
) -> BacktestResult:
    """Return a shallow copy of the result limited to the requested window."""
    if not start_bound and not end_bound:
        return result

    trades = [trade for trade in result.trades if _trade_in_window(trade, start_bound, end_bound)]
    equity_curve = result.equity_curve
    if equity_curve:
        equity_curve = [
            point
            for point in equity_curve
            if _in_window(point.time, start_bound, end_bound)
        ]
    return BacktestResult(
        meta=result.meta,
        trades=trades,
        equity_curve=equity_curve,
        stats=dict(result.stats or {}),
        indicators=list(result.indicators or []),
        drawings=list(result.drawings or []),
    )


def _windowed_curve(
    result: BacktestResult, start_bound: datetime | None, end_bound: datetime | None
) -> list[EquityPoint] | None:
    """The engine-provided curve scoped to the same window as the aggregation.

    Delegates to ``_scoped_view``: with a window it is the windowed curve,
    without one the engine curve passes through unchanged (pre-mortem 16-B #7).
    """
    return _scoped_view(result, start_bound, end_bound).equity_curve


def _preserves_engine_curve(
    preserved_curve: list[EquityPoint] | None,
    daily_points: list[EquityPoint],
    trades: list[Trade],
) -> bool:
    """Whether the engine curve may replace the net-PnL daily build.

    The engine curve is only kept when it is dense enough to describe the same
    window (coverage, gaps, boundaries), is ordered and finite, reaches at
    least to the last trade exit, and its endpoint agrees with the net ledger
    within one cent.  The endpoint tolerance is absolute: a relative tolerance
    would silently accept large absolute errors on high-priced or leveraged
    accounts.

    Only the endpoint is reconciled.  Interior points stay engine-owned: an
    intraday curve carries unrealized PnL for positions that are still open at
    a bar close, so comparing interior values against the realized ledger would
    reject legitimate curves.
    """
    if len(preserved_curve or ()) < 2 or not daily_points:
        return False
    try:
        preserved_index = pd.DatetimeIndex(
            pd.to_datetime(
                [point.time for point in preserved_curve],
                utc=True,
                errors="coerce",
                format="mixed",
            )
        )
        daily_index = pd.DatetimeIndex(
            pd.to_datetime(
                [point.time for point in daily_points],
                utc=True,
                errors="coerce",
                format="mixed",
            )
        )
        preserved_values = pd.Series(
            pd.to_numeric(
                [point.equity for point in preserved_curve], errors="coerce"
            ),
            dtype=float,
        )
        daily_final = float(daily_points[-1].equity)
        preserved_final = float(preserved_values.iloc[-1])
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        preserved_index.hasnans
        or daily_index.hasnans
        or preserved_values.isna().any()
        or not all(math.isfinite(float(value)) for value in preserved_values)
        or not math.isfinite(preserved_final)
        or not math.isfinite(daily_final)
    ):
        return False
    if not preserved_index.is_monotonic_increasing or preserved_index.has_duplicates:
        return False
    preserved_start = preserved_index[0].normalize()
    daily_start = daily_index[0].normalize()
    preserved_end = preserved_index[-1].normalize()
    daily_end = daily_index[-1].normalize()
    if preserved_start < daily_start or preserved_end > daily_end:
        return False
    if preserved_start - daily_start > MAX_PRESERVED_BOUNDARY_GAP:
        return False
    if daily_end - preserved_end > MAX_PRESERVED_BOUNDARY_GAP:
        return False
    preserved_days = preserved_index.normalize().nunique()
    daily_days = daily_index.normalize().nunique()
    if preserved_days / max(daily_days, 1) < MIN_PRESERVED_DAY_COVERAGE:
        return False
    gaps = preserved_index.to_series().diff().dropna()
    if gaps.empty or gaps.max() > MAX_PRESERVED_CURVE_GAP:
        return False
    trade_exits = [
        parsed
        for trade in trades
        if (parsed := _parse_ts(_last_exit_time(trade))) is not None
    ]
    if trade_exits and preserved_index[-1] < max(trade_exits):
        # The curve stops before the last trade it is supposed to describe.
        return False
    return abs(preserved_final - daily_final) <= 0.01


def _curve_frequency(points: list[EquityPoint]) -> str:
    if len(points) < 2:
        return "unknown"
    try:
        spacing = (
            pd.to_datetime(
                [point.time for point in points],
                utc=True,
                format="mixed",
            )
            .to_series()
            .diff()
            .dropna()
            .median()
        )
    except (TypeError, ValueError):
        return "unknown"
    if pd.isna(spacing):
        return "unknown"
    if spacing >= pd.Timedelta(days=1):
        return "daily"
    return "intraday"


def _attach_daily_stats(
    result: BacktestResult,
    daily_returns: list[dict[str, Any]],
    *,
    equity_frequency: str = "daily",
) -> None:
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    result.meta.data["equity_frequency"] = equity_frequency
    result.meta.data["daily_return_count"] = len(daily_returns)


def build_daily_equity_curve(
    result: BacktestResult,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    starting_equity: float | None = None,
) -> tuple[list[EquityPoint], list[dict[str, Any]]]:
    initial_capital = float(result.meta.initial_capital)
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and > 0")
    if starting_equity is None:
        starting_equity = initial_capital
    starting_equity = float(starting_equity)
    if not math.isfinite(starting_equity):
        raise ValueError("starting equity must be finite")

    equity_by_day: dict[str, float] = defaultdict(float)
    event_days: list[datetime] = []
    for trade in result.trades:
        pnl = _trade_net_pnl(trade)
        if pnl is None:
            continue
        parsed = _parse_ts(_last_exit_time(trade))
        if parsed is None:
            continue
        event_days.append(parsed)
        # Bucket by the UTC day: an exit at 23:30-05:00 belongs to the next
        # UTC day, and using the raw offset date would misdate the PnL.
        equity_by_day[parsed.astimezone(timezone.utc).date().isoformat()] += pnl

    if not event_days:
        return [], []

    # A date-only bound describes a whole day and keeps its end-of-day close;
    # an instant bound (explicit or from ``meta.backtest_end``) is a hard
    # boundary and must not be overshot.
    start_bound = _parse_ts(start) if start is not None else _parse_ts(result.meta.backtest_start)
    end_bound = (
        _parse_ts(end)
        if end is not None
        else _parse_day_bound(result.meta.backtest_end, is_end=True)
    )
    start = start_bound or (
        min(event_days).replace(hour=0, minute=0, second=0, microsecond=0)
        if event_days
        else None
    )
    end = end_bound or (
        max(event_days).replace(hour=0, minute=0, second=0, microsecond=0)
        if event_days
        else start
    )
    if start is None or end is None or end < start:
        return [], []

    # The inception point sits at the requested start instant, so the curve
    # never begins before the window it describes.
    inception = start.astimezone(timezone.utc)
    cursor = inception.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.astimezone(timezone.utc).date()
    end_instant = end.astimezone(timezone.utc)

    daily_points = [EquityPoint(time=inception.isoformat(), equity=round(starting_equity, 2))]
    daily_returns: list[dict[str, Any]] = []
    running_equity = starting_equity
    while cursor.date() <= end_day:
        day = cursor.date().isoformat()
        pnl = equity_by_day.get(day, 0.0)
        previous = running_equity
        running_equity += pnl
        if not math.isfinite(running_equity):
            raise ValueError("Daily equity accumulation is non-finite")
        # Cent-quantise the reported equity exactly like the curve closes below,
        # so ``daily_returns[i]["equity"]`` and the matching
        # ``EquityPoint.equity`` describe the same number.  An unrounded value
        # here would let the two published views disagree on the same instant
        # (up to half a cent per row) and make a downstream comparison against
        # the curve look like drift.
        closed_equity = round(running_equity, 2)
        daily_returns.append(
            {
                "date": day,
                "return": (running_equity - previous) / previous if previous > 0 else None,
                "equity": closed_equity,
            }
        )
        point_time = cursor + timedelta(days=1) - timedelta(microseconds=1)
        if end_bound is not None and cursor.date() == end_day:
            # Never let the last close overshoot the window end.
            point_time = min(point_time, end_instant)
        daily_points.append(
            EquityPoint(time=point_time.isoformat(), equity=closed_equity)
        )
        cursor += timedelta(days=1)

    normalized_closes = _strictly_increasing_curve(
        daily_points[1:],
        inception,
        end_bound,
    )
    if normalized_closes is None:
        raise ValueError("Daily equity closes cannot be normalized within the requested window")
    return [daily_points[0], *normalized_closes], daily_returns


def attach_daily_equity(
    result: BacktestResult,
    start: str | None = None,
    end: str | None = None,
) -> BacktestResult:
    """
    Attach daily equity curve to a backtest result.

    Args:
        result: BacktestResult to update
        start: Optional start date filter (YYYY-MM-DD); trades and equity
            points before this date are excluded from the daily aggregation.
        end: Optional end date filter (YYYY-MM-DD); trades and equity points
            after this date are excluded from the daily aggregation.

    Returns:
        The same result object with daily equity attached
    """
    start_bound = _parse_day_bound(start, is_end=False)
    end_bound = _parse_day_bound(end, is_end=True)
    if start is not None and start_bound is None:
        raise ValueError("Daily equity start bound is invalid")
    if end is not None and end_bound is None:
        raise ValueError("Daily equity end bound is invalid")
    if start_bound is not None and end_bound is not None and start_bound > end_bound:
        raise ValueError("Daily equity start bound must not exceed end bound")
    if start_bound is not None and end_bound is not None and start_bound == end_bound:
        raise ValueError("Daily equity start and end bounds must differ")

    # If there are no trades and no existing equity curve, mark as unknown frequency
    if not result.trades and not result.equity_curve:
        result.meta.data = result.meta.data or {}
        result.meta.data["equity_frequency"] = "unknown"
        result.meta.data.pop("daily_return_count", None)
        result.meta.data.pop("window_start_basis", None)
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        result.stats["daily_returns"] = []
        return result

    # Build daily equity from trades, restricted to the requested window. The
    # window can come from ``meta.backtest_start``/``meta.backtest_end`` alone,
    # so resolve it once and use it for trades, engine curve and build alike:
    # a trade after the end instant must not contribute to the last day.
    effective_start = start_bound or _parse_ts(result.meta.backtest_start)
    effective_end = end_bound or _parse_day_bound(result.meta.backtest_end, is_end=True)
    scoped_result = _scoped_view(result, effective_start, effective_end)
    # Unscoped on purpose: the starting equity comes from trades that exited
    # *before* the window, which ``scoped_result`` no longer contains.
    window_start = _window_starting_equity(result, effective_start)
    # Published on every path that computed a window base, including the
    # engine-curve fallback: the warning refers to this base, so a report must
    # not show the warning without the basis it is about.
    result.meta.data = result.meta.data or {}
    result.meta.data["window_start_basis"] = window_start.basis
    daily_points, daily_returns = build_daily_equity_curve(
        scoped_result,
        start=effective_start,
        end=effective_end,
        starting_equity=window_start.equity,
    )

    # If no daily points could be built (e.g., no valid exit times), fallback to existing curve
    if not daily_points:
        result.meta.data = result.meta.data or {}
        # Fall back to the engine curve *scoped to the same window*: publishing
        # the unwindowed curve here would describe a different period than the
        # requested one. The same rule as the preserved-curve path below.
        fallback_curve = _normalize_engine_curve(scoped_result.equity_curve or [])
        result.equity_curve = fallback_curve
        # Only mark as daily if we actually built a daily curve
        if fallback_curve and len(fallback_curve) > 1:
            result.meta.data["equity_frequency"] = _curve_frequency(fallback_curve)
        else:
            result.meta.data["equity_frequency"] = "unknown"
        if result.meta.data["equity_frequency"] == "daily":
            result.meta.data["daily_return_count"] = 0
        else:
            result.meta.data.pop("daily_return_count", None)
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        result.stats["daily_returns"] = []
        return result

    # When a window is active, an engine-provided curve must be scoped to the
    # same bounds before the length comparison: preserving the full unwindowed
    # curve next to window-scoped daily_returns would make the curve and the
    # reported daily returns describe different periods (pre-mortem 16-B #7).
    # _scoped_view computes exactly this view; reuse it instead of re-filtering.
    preserved_curve = _windowed_curve(result, effective_start, effective_end)
    result.meta.data = result.meta.data or {}
    result.meta.data["daily_equity_curve"] = [point.to_dict() for point in daily_points]
    if _preserves_engine_curve(preserved_curve, daily_points, scoped_result.trades):
        result.equity_curve = preserved_curve
        _attach_daily_stats(
            result,
            daily_returns,
            equity_frequency=_curve_frequency(preserved_curve),
        )
        return result

    # The engine curve may still be worth adopting even when it is too sparse
    # to pass the density gate above: a reconciling curve carries the
    # mark-to-market path of open positions, which the calendar fill (built
    # from closed trades only) cannot see. Gated on being non-endpoint-only and
    # on reconciling with the net ledger within the derived tolerance.
    # The window end is the explicit bound when one was requested, else the
    # artifact's own `backtest_end` — the same window `build_daily_equity_curve`
    # filled, so both paths publish the same period.
    window_end = end_bound if end_bound is not None else _parse_ts(result.meta.backtest_end)
    engine_daily = _daily_points_from_engine_curve(
        scoped_result,
        start=start_bound,
        end=window_end,
        starting_equity=window_start.equity,
    )
    if engine_daily is not None:
        engine_points, engine_returns = engine_daily
        result.equity_curve = engine_points
        result.meta.data["daily_equity_curve"] = [
            point.to_dict() for point in engine_points
        ]
        _attach_daily_stats(result, engine_returns, equity_frequency="daily")
        return result

    result.equity_curve = daily_points
    _attach_daily_stats(result, daily_returns, equity_frequency="daily")

    return result

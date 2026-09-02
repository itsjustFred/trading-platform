"""Shared cross-engine backtest contract primitives.

Documented contract: ``docs/cross-engine-backtest-contract.md``.

This module holds the small, engine-neutral pieces of the cross-engine
execution contract so that every adapter conforms by construction instead of
re-implementing (and eventually diverging from) the same rules inline:

- :func:`parse_date_bound` / :func:`apply_date_filter` — the date-filter
  convention (F9). A date-only end bound is inclusive of the whole last day.
- :func:`resolve_same_bar_exit` — the same-bar stop/take-profit priority (F8).
  Stop-first whenever intrabar data cannot order the events, the chronological
  intrabar event when it can.
- :func:`flag_missing_intrabar` — the intrabar fallback flag required by the
  Decision Record.
- :class:`EngineStatsKeys` — the ``result.stats`` key set every engine must be
  able to expose (F5: stats keys per engine).

Scope note: this module implements the *facets* the engines already agree on
and the Decision-Record facets that were previously implemented three times
with different semantics. It does not unify the three execution-timing models
(F10); those remain documented, per-engine, in the contract document.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pandas as pd

__all__ = [
    "apply_date_filter",
    "flag_missing_intrabar",
    "parse_date_bound",
    "resolve_same_bar_exit",
    "EngineStatsKeys",
]


# ─── Date filter convention (F9) ────────────────────────────────────────────


def parse_date_bound(value: str, *, is_end: bool) -> pd.Timestamp:
    """Parse a ``start_date``/``end_date`` value into an inclusive filter bound.

    Decision Record (Cleanup 16-A, 2026-08-31): a date-only end boundary is
    inclusive of the full day. Concretely:

    - ``"2024-01-15"`` as a start bound → ``2024-01-15T00:00:00Z``;
    - ``"2024-01-15"`` as an end bound → ``2024-01-15T23:59:59.999999Z``;
    - a value carrying a time component (``"2024-01-15T14:30:00Z"``) is used
      as-is; naive timestamps are interpreted as UTC.

    Raises:
        ValueError: If ``value`` is empty or not parseable as a timestamp.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Date filter value cannot be empty")

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid date filter value: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if is_end and "T" not in raw and ":" not in raw:
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return pd.Timestamp(parsed)


def apply_date_filter(
    frame: pd.DataFrame,
    *,
    start: str | None,
    end: str | None,
    timestamp_column: str = "timestamp",
) -> pd.DataFrame:
    """Filter ``frame`` to the inclusive [start, end] window (see F9 above).

    Empty/None bounds are treated as unbounded on that side. The frame must
    carry a UTC-convertible ``timestamp`` column.

    This must be the ONLY date filter applied to the frame. A caller that
    pre-filters with a bare-date string comparison (``df[df["timestamp"] <=
    end_date]``) truncates at midnight and reintroduces the F9 end-day
    truncation bug before this helper ever sees the data.
    """
    if start:
        frame = frame[
            frame[timestamp_column] >= parse_date_bound(start, is_end=False)
        ]
    if end:
        frame = frame[
            frame[timestamp_column] <= parse_date_bound(end, is_end=True)
        ]
    return frame


# ─── Same-bar stop / take-profit priority (F8) ──────────────────────────────


def resolve_same_bar_exit(
    *,
    direction: str,
    high: float,
    low: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> str | None:
    """Classify one bar (or one intrabar step) into ``stop``/``tp``/``None``.

    Decision Record (Cleanup 16-A, 2026-08-31):

    - Same-bar SL/TP priority is **stop-first everywhere when intrabar data is
      unavailable**;
    - with intrabar data enabled and available, the **actual chronological
      event** wins;
    - if intrabar data is missing, fall back to stop-first **and flag the
      result** (see :func:`flag_missing_intrabar`).

    This helper implements the classification for ONE bar/step. When both
    levels are hit by the same bar it returns ``"stop"`` — the correct answer
    whenever no finer-grained data can order the two events, and conservative
    for sizing decisions. Callers that DO have intrabar data must walk it
    chronologically and call this helper per intrabar step, so the first
    chronological event wins rather than the stop-first tie-break.

    ``direction`` must be exactly ``"LONG"`` or ``"SHORT"`` (the engine-wide
    convention). Anything else raises ``ValueError``: an unknown direction
    silently evaluated as SHORT would invert stop and TP logic and produce
    plausible-looking, wrong exits.
    """
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")
    if direction == "LONG":
        stop_hit = stop_price is not None and low <= stop_price
        tp_hit = take_profit_price is not None and high >= take_profit_price
    else:
        stop_hit = stop_price is not None and high >= stop_price
        tp_hit = take_profit_price is not None and low <= take_profit_price

    if stop_hit:
        return "stop"
    if tp_hit:
        return "tp"
    return None


def flag_missing_intrabar(
    *,
    intrabar_resolution: str | None,
    intrabar_coverage: bool,
    reason: str = "intrabar_data_missing",
) -> dict[str, Any] | None:
    """Build the same-bar-ambiguity flag required by the Decision Record.

    Returns ``None`` when no flag is needed — either no intrabar resolution was
    requested (the stop-first fallback is the documented default, nothing to
    disclose) or the intrabar data actually covers the ambiguous bar (the
    chronological event is authoritative).

    Returns a flag payload when an intrabar resolution WAS requested but the
    walk could not resolve the chronology: the result then silently fell back
    to the stop-first tie-break and that fact must be visible on the
    trade/meta. ``reason`` distinguishes why the walk failed —
    ``"intrabar_data_missing"`` (no sub-bars for the bar) vs.
    ``"intrabar_unresolved"`` (sub-bars exist but neither level was touched).
    """
    if intrabar_resolution is None:
        return None
    if intrabar_coverage and reason == "intrabar_data_missing":
        return None
    return {
        "same_bar_priority": "stop_first_fallback",
        "intrabar_resolution": intrabar_resolution,
        "intrabar_available": intrabar_coverage,
        "reason": reason,
    }


# ─── Stats key set (F5) ─────────────────────────────────────────────────────


class EngineStatsKeys:
    """The ``result.stats`` key set every engine must be able to expose.

    ``runner.stats.compute_stats`` derives these from ``trades`` plus the
    ``equity_curve``, so any adapter that returns real trades and a real equity
    curve can satisfy the full set by routing through that helper instead of
    emitting a bespoke, shorter key set.
    """

    REQUIRED = frozenset(
        {
            "total_trades",
            "winners",
            "losers",
            "win_rate",
            "profit_factor",
            "total_pnl",
            "avg_winner",
            "avg_loser",
            "max_consecutive_wins",
            "max_consecutive_losses",
            "max_drawdown_pct",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "total_return",
            "p_value",
        }
    )

    OPTIONAL = frozenset({"avg_holding_bars", "psr", "max_drawdown_duration"})

    @classmethod
    def missing_from(cls, stats: Mapping[str, Any] | None) -> frozenset[str]:
        """Return the required keys absent from ``stats`` (empty = conforms)."""
        if not stats:
            return cls.REQUIRED
        return frozenset(cls.REQUIRED - set(stats.keys()))

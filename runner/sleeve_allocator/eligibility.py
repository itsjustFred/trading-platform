"""Sleeve eligibility filter (issue #307, phase 1).

A sleeve enters the allocation only with enough out-of-sample evidence:
minimum history span, minimum trade count and no ruin. Every check fails
closed — missing evidence rejects the sleeve rather than admitting it on a
default.

``as_of`` support exists so the walk-forward harness can ask "which sleeves
were eligible at time t" using only data strictly before t (the report's
selection-look-ahead pitfall). Trade counts come from two bases:

* ``trade_periods`` (walk-forward payloads): only periods whose END is at or
  before ``as_of`` are counted, so no future trades leak into the decision.
* ``total_trades`` (matrix runs, single-window counts): this is a
  full-sample figure. When ``as_of`` is set it is still used — there is no
  per-period timing to do better with — and the result records
  ``trade_count_basis="full_sample"`` so the report can disclose it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .loader import SleeveMetadata, SleeveReturns, TradePeriod

__all__ = ["EligibilityConfig", "EligibilityResult", "filter_eligible_sleeves"]


@dataclass(frozen=True)
class EligibilityConfig:
    """Thresholds for sleeve admission. Defaults follow the issue text."""

    min_history_days: int = 504  # ~2y of calendar-day span
    min_trades: int = 100
    ruin_equity_floor: float = 0.01  # min running equity ratio <= floor => ruined
    min_coverage: float = 0.0  # optional: fraction of calendar days observed


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of one eligibility pass."""

    eligible: tuple[str, ...]
    rejected: dict[str, tuple[str, ...]]
    trade_count_basis: dict[str, str] = field(default_factory=dict)
    as_of: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": list(self.eligible),
            "rejected": {k: list(v) for k, v in self.rejected.items()},
            "trade_count_basis": dict(self.trade_count_basis),
            "as_of": self.as_of,
        }


def _cutoff(as_of: Any) -> pd.Timestamp | None:
    if as_of is None:
        return None
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def _slice_before(df: pd.DataFrame, as_of: pd.Timestamp | None) -> pd.DataFrame:
    if as_of is None:
        return df
    return df[df.index < as_of]


def _period_end_ts(period: TradePeriod) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(period.end)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def _trades_before(
    meta: SleeveMetadata, as_of: pd.Timestamp | None
) -> tuple[int | None, str]:
    """Trade count usable at ``as_of`` and the basis it came from."""
    if meta.trade_periods:
        if as_of is None:
            return sum(p.trades for p in meta.trade_periods), "periods"
        counted = 0
        for period in meta.trade_periods:
            end = _period_end_ts(period)
            if end is not None and end <= as_of:
                counted += int(period.trades)
        return counted, "periods"
    if meta.total_trades is not None:
        return int(meta.total_trades), "full_sample"
    return None, "none"


def _check_sleeve(
    observed: pd.Series,
    calendar_size: int,
    meta: SleeveMetadata,
    config: EligibilityConfig,
    as_of: pd.Timestamp | None,
) -> tuple[list[str], str]:
    """Return (rejection reasons, trade-count basis) for one sleeve."""
    reasons: list[str] = []
    if observed.empty:
        reasons.append("no_observations")
    else:
        span_days = int((observed.index[-1] - observed.index[0]).days)
        if config.min_history_days > 0 and span_days < config.min_history_days:
            reasons.append("insufficient_history")
        if (
            config.min_coverage > 0.0
            and calendar_size > 0
            and len(observed) / calendar_size < config.min_coverage
        ):
            reasons.append("insufficient_coverage")
        values = observed.to_numpy(dtype=float)
        if not np.all(np.isfinite(values)) or bool((values <= -1.0).any()):
            reasons.append("invalid_returns")
        else:
            equity = np.cumprod(1.0 + values)
            if equity.size and (
                not bool(np.all(np.isfinite(equity)))
                or float(np.min(equity)) <= config.ruin_equity_floor
            ):
                reasons.append("ruined")

    basis = "none"
    if config.min_trades > 0:
        count, basis = _trades_before(meta, as_of)
        if count is None:
            reasons.append("trades_unknown")
        elif count < config.min_trades:
            reasons.append("insufficient_trades")
    elif meta.trade_periods or meta.total_trades is not None:
        _, basis = _trades_before(meta, as_of)
    return reasons, basis


def filter_eligible_sleeves(
    sleeve_returns: SleeveReturns,
    config: EligibilityConfig | None = None,
    *,
    as_of: Any = None,
) -> EligibilityResult:
    """Split sleeves into eligible / rejected with machine-readable reasons.

    ``as_of`` is EXCLUSIVE: only observations strictly before that timestamp
    count as evidence. ``None`` evaluates the full loaded history.
    """
    cfg = config or EligibilityConfig()
    cutoff = _cutoff(as_of)
    frame = _slice_before(sleeve_returns.returns, cutoff)
    calendar_size = len(frame.index)

    eligible: list[str] = []
    rejected: dict[str, tuple[str, ...]] = {}
    bases: dict[str, str] = {}
    for sleeve_id in frame.columns:
        meta = sleeve_returns.metadata.get(sleeve_id) or SleeveMetadata(
            sleeve_id=sleeve_id,
            strategy="",
            symbol="",
            timeframe="",
            source="unknown",
        )
        observed = frame[sleeve_id].dropna()
        reasons, basis = _check_sleeve(observed, calendar_size, meta, cfg, cutoff)
        bases[sleeve_id] = basis
        if reasons:
            rejected[sleeve_id] = tuple(reasons)
        else:
            eligible.append(sleeve_id)

    return EligibilityResult(
        eligible=tuple(eligible),
        rejected=rejected,
        trade_count_basis=bases,
        as_of=cutoff.date().isoformat() if cutoff is not None else None,
    )

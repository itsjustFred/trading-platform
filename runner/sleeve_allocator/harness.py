"""Walk-forward allocation harness (issue #307, phase 1).

Chains out-of-sample portfolio returns: at each monthly rebalance date ``t``
the allocator estimates weights on the trailing ``lookback_days`` business
days STRICTLY before ``t`` (eligibility is likewise evaluated on data
``< t``), then applies them from ``t`` until the next rebalance. The chained
curve is the only performance number this module reports — there is no
full-sample fit anywhere in the loop.

The report ALWAYS contains the ``equal_weight`` (1/N) and ``inverse_vol``
baselines, even when the caller requests a subset of objectives: a proposed
allocation that cannot beat the baselines out of sample must be visible as
such, not absent.

Known phase-1 limitations (documented, not hidden):

* ``total_trades``-only sleeves are rejected at point-in-time rebalances
  unless the caller opts into ``allow_full_sample_trade_counts`` — the
  count is full-sample (future-inclusive) evidence (see ``eligibility``);
  sleeves with ``trade_periods`` are evaluated strictly point-in-time.
* Sleeve-day ``NaN`` is treated as flat (0% contribution) when APPLYING
  weights — correct for market holidays, since the equity genuinely did not
  move; estimation statistics stay pairwise-complete and never see the fill.
* Metrics annualise at the measured observation density (#349's
  convention), falling back to sqrt(252) only when the density cannot
  be measured.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from runner.stats import DAILY_RETURN_PERIODS_PER_YEAR

from .eligibility import EligibilityConfig, EligibilityResult, filter_eligible_sleeves
from .loader import SleeveReturns
from .objectives import BASELINE_OBJECTIVES, SUPPORTED_OBJECTIVES, solve_weights

__all__ = [
    "AllocationConfig",
    "AllocationResult",
    "RebalanceRecord",
    "curve_metrics",
    "walk_forward_allocation",
]


@dataclass(frozen=True)
class AllocationConfig:
    """Walk-forward harness settings. Phase 1 supports monthly rebalance only."""

    lookback_days: int = 252
    min_window_observations: int = 30
    objectives: tuple[str, ...] = ("equal_weight", "inverse_vol")
    eligibility: EligibilityConfig = field(default_factory=EligibilityConfig)


@dataclass(frozen=True)
class RebalanceRecord:
    """One rebalance decision for one objective."""

    date: str
    weights: dict[str, float]  # every sleeve column; ineligible sleeves are 0.0
    n_eligible: int
    carried: bool = False  # weights carried from the previous rebalance
    # The solver could not estimate and the previous weights were reused
    # (renormalised over the still-eligible sleeves), or - with no previous
    # weights - replaced by 1/N over the eligible set.
    fallback_equal_weight: bool = False
    # Nothing was eligible: the whole risk budget sits in cash (all weights
    # 0.0) rather than staying with a sleeve the filter just rejected.
    cash: bool = False


@dataclass
class AllocationResult:
    returns: dict[str, pd.Series]  # objective -> chained daily returns
    equity: dict[str, pd.Series]  # objective -> equity curve (starts at 1.0)
    rebalances: dict[str, list[RebalanceRecord]]
    report: dict[str, Any]


def _month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Last observed business day of each calendar month in ``index``."""
    if len(index) == 0:
        return []
    frame = pd.DataFrame({"ts": index})
    last = frame.groupby(index.to_period("M"))["ts"].max()
    return [pd.Timestamp(ts) for ts in last.tolist()]


def _equity_from_returns(returns: pd.Series) -> pd.Series:
    """Equity curve with an explicit 1.0 inception point.

    The inception point sits on the business day before the first return, the
    mark at which the full risk budget is notionally deployed.
    """
    if returns.empty:
        return pd.Series(dtype=float)
    growth = (1.0 + returns).cumprod()
    start = returns.index[0] - pd.offsets.BDay(1)
    return pd.concat(
        [pd.Series([1.0], index=pd.DatetimeIndex([start])), growth]
    )


def _return_periods_per_year(returns: pd.Series) -> float:
    """Measured observation density: return observations per calendar year.

    Mirrors the ``runner.stats`` convention landing with #349 (pending
    merge — the function does not exist on main yet): a
    calendar-filled curve observes ~365.25 rows a year, a business-day
    curve ~261, so annualising at a fixed sqrt(252) understates both
    Sharpe and the ``ann_return``/``ann_volatility`` figures. The span is
    first-to-last observation plus one day bucket; series too short to
    measure fall back to the platform's ``DAILY_RETURN_PERIODS_PER_YEAR``.
    """
    index = returns.index
    if not isinstance(index, pd.DatetimeIndex) or len(returns) < 2:
        return float(DAILY_RETURN_PERIODS_PER_YEAR)
    span_days = (index[-1] - index[0]).total_seconds() / 86400.0 + 1.0
    if span_days <= 0:
        return float(DAILY_RETURN_PERIODS_PER_YEAR)
    return float(len(returns)) * 365.25 / span_days


def curve_metrics(returns: pd.Series) -> dict[str, Any]:
    """Standard metrics for one chained daily-return series.

    Sharpe/volatility/annualised return use the measured observation
    density (``_return_periods_per_year``, the #349 convention) rather
    than a fixed sqrt(252); series too short to measure fall back to
    ``runner.stats.DAILY_RETURN_PERIODS_PER_YEAR``.
    """
    n = int(len(returns))
    metrics: dict[str, Any] = {"n_days": n}
    keys = (
        "total_return",
        "ann_return",
        "ann_volatility",
        "sharpe",
        "max_drawdown",
        "final_equity",
    )
    if n == 0:
        metrics.update({key: None for key in keys})
        return metrics

    periods_per_year = _return_periods_per_year(returns)
    equity = _equity_from_returns(returns)
    final = float(equity.iloc[-1])
    metrics["final_equity"] = final
    metrics["total_return"] = final - 1.0
    metrics["ann_return"] = (
        float(final ** (periods_per_year / n) - 1.0) if final > 0 else None
    )
    peak = equity.cummax()
    drawdown = (peak - equity) / peak.replace(0.0, np.nan)
    metrics["max_drawdown"] = float(drawdown.max()) if len(drawdown) else 0.0
    if n > 1:
        std = float(returns.std(ddof=1))
        mean = float(returns.mean())
        metrics["ann_volatility"] = std * math.sqrt(periods_per_year)
        metrics["sharpe"] = (
            mean / std * math.sqrt(periods_per_year) if std > 0 else 0.0
        )
    else:
        metrics["ann_volatility"] = None
        metrics["sharpe"] = None
    return metrics


def _turnover(records: list[RebalanceRecord], columns: list[str]) -> float | None:
    """Mean one-sided turnover (0.5 * L1 distance) between consecutive rebalances."""
    if len(records) < 2:
        return None
    distances = []
    for previous, current in zip(records[:-1], records[1:], strict=False):
        delta = sum(
            abs(current.weights.get(col, 0.0) - previous.weights.get(col, 0.0))
            for col in columns
        )
        distances.append(0.5 * delta)
    return float(np.mean(distances)) if distances else None


def _weights_for_objective(
    objective: str,
    eligible_ids: list[str],
    estimation_window: pd.DataFrame,
    previous: dict[str, float] | None,
    columns: list[str],
    config: AllocationConfig,
) -> tuple[dict[str, float] | None, bool, bool, bool]:
    """``(weights over ALL columns, carried, fallback_equal_weight, cash)``.

    * No eligible sleeves -> all-zero weights (cash), never the previous
      allocation: carrying weights would keep risk on a sleeve the filter
      just rejected (e.g. as ruined) for a whole month.
    * Solver failure -> carry ONLY the previous weights of still-eligible
      sleeves, renormalised; when there is nothing eligible to carry (or no
      previous weights), fall back to 1/N over the eligible set and flag it
      ``fallback_equal_weight`` - it is not a carry.
    """
    zero = {col: 0.0 for col in columns}
    if not eligible_ids:
        return dict(zero), False, False, True

    def _spread(solved: np.ndarray) -> dict[str, float]:
        weights = dict(zero)
        for sleeve_id, weight in zip(eligible_ids, solved, strict=True):
            weights[sleeve_id] = float(weight)
        return weights

    solved = solve_weights(
        objective,
        eligible_ids,
        estimation_window,
        min_window_observations=config.min_window_observations,
    )
    if solved is None:
        if previous is not None:
            kept = {
                sleeve_id: float(previous.get(sleeve_id, 0.0))
                for sleeve_id in eligible_ids
            }
            kept = {sid: w for sid, w in kept.items() if w > 0.0}
            total = sum(kept.values())
            if total > 0.0:
                weights = dict(zero)
                for sleeve_id, weight in kept.items():
                    weights[sleeve_id] = weight / total
                return weights, True, False, False
        # Nothing eligible to carry (first rebalance, or the previous weights
        # sat entirely on now-ineligible sleeves): fall back to 1/N over the
        # eligible set rather than emit nothing.
        solved = solve_weights(
            "equal_weight",
            eligible_ids,
            estimation_window,
            min_window_observations=config.min_window_observations,
        )
        if solved is None:
            return None, False, False, False
        return _spread(solved), False, True, False
    return _spread(solved), False, False, False


def walk_forward_allocation(
    sleeve_returns: SleeveReturns,
    config: AllocationConfig | None = None,
) -> AllocationResult:
    """Chain monthly walk-forward allocations into OOS portfolio curves.

    Raises ``ValueError`` on an empty return frame or an unknown objective.
    """
    cfg = config or AllocationConfig()
    frame = sleeve_returns.returns
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("sleeve_returns contains no sleeve return series")
    unknown = sorted(set(cfg.objectives) - set(SUPPORTED_OBJECTIVES))
    if unknown:
        raise ValueError(f"unsupported allocation objectives: {unknown}")

    columns = [str(c) for c in frame.columns]
    run_objectives = tuple(sorted(set(cfg.objectives) | set(BASELINE_OBJECTIVES)))
    rebalance_dates = _month_end_dates(frame.index)

    previous_weights: dict[str, dict[str, float] | None] = {
        name: None for name in run_objectives
    }
    applied: dict[str, list[pd.Series]] = {name: [] for name in run_objectives}
    rebalances: dict[str, list[RebalanceRecord]] = {name: [] for name in run_objectives}
    eligibility_log: list[dict[str, Any]] = []
    skipped_rebalances = 0

    for position, rebalance_day in enumerate(rebalance_dates):
        window = frame.loc[frame.index < rebalance_day]
        if len(window) < cfg.lookback_days:
            skipped_rebalances += 1
            continue
        estimation_window = window.tail(cfg.lookback_days)

        eligibility: EligibilityResult = filter_eligible_sleeves(
            sleeve_returns, cfg.eligibility, as_of=rebalance_day
        )
        eligible_ids = list(eligibility.eligible)
        # Estimation-window coverage is part of eligibility at each
        # rebalance: a sleeve whose last observation predates the window
        # (e.g. a strategy that stopped trading) must not soak up budget.
        # equal_weight would otherwise hand it 1/N that earns nothing while
        # inverse_vol gives it 0 and redistributes — the baselines would
        # see different universes.
        rejected_log = {
            key: list(value) for key, value in eligibility.rejected.items()
        }
        if cfg.min_window_observations > 0:
            short_window = [
                sleeve_id
                for sleeve_id in eligible_ids
                if int(estimation_window[sleeve_id].count())
                < cfg.min_window_observations
            ]
            if short_window:
                short_set = set(short_window)
                eligible_ids = [
                    sleeve_id
                    for sleeve_id in eligible_ids
                    if sleeve_id not in short_set
                ]
                for sleeve_id in short_window:
                    rejected_log.setdefault(sleeve_id, []).append(
                        "insufficient_window_observations"
                    )
        eligibility_log.append(
            {
                "date": rebalance_day.date().isoformat(),
                "eligible": eligible_ids,
                "rejected": rejected_log,
                "trade_count_basis": dict(eligibility.trade_count_basis),
            }
        )

        next_day = rebalance_dates[position + 1] if position + 1 < len(rebalance_dates) else None
        segment_index = frame.index[
            (frame.index >= rebalance_day)
            & (frame.index < next_day if next_day is not None else True)
        ]
        segment_frame = frame.loc[segment_index, columns]

        for objective in run_objectives:
            weights, carried, fallback_equal_weight, cash = _weights_for_objective(
                objective,
                eligible_ids,
                estimation_window,
                previous_weights[objective],
                columns,
                cfg,
            )
            if weights is None:
                continue
            previous_weights[objective] = weights
            vector = np.array([weights.get(col, 0.0) for col in columns], dtype=float)
            portfolio_returns = segment_frame.fillna(0.0).to_numpy() @ vector
            applied[objective].append(
                pd.Series(portfolio_returns, index=segment_index, dtype=float)
            )
            rebalances[objective].append(
                RebalanceRecord(
                    date=rebalance_day.date().isoformat(),
                    weights=dict(weights),
                    n_eligible=len(eligible_ids),
                    carried=carried,
                    fallback_equal_weight=fallback_equal_weight,
                    cash=cash,
                )
            )

    returns_map: dict[str, pd.Series] = {}
    equity_map: dict[str, pd.Series] = {}
    for objective in run_objectives:
        pieces = applied[objective]
        if pieces:
            chained = pd.concat(pieces).sort_index(kind="stable")
        else:
            chained = pd.Series(dtype=float)
        returns_map[objective] = chained
        equity_map[objective] = _equity_from_returns(chained)

    objectives_report: dict[str, Any] = {}
    for objective in run_objectives:
        records = rebalances[objective]
        objectives_report[objective] = {
            "metrics": curve_metrics(returns_map[objective]),
            "n_rebalances": len(records),
            "carried_rebalances": sum(1 for r in records if r.carried),
            "fallback_equal_weight_rebalances": sum(
                1 for r in records if r.fallback_equal_weight
            ),
            "cash_rebalances": sum(1 for r in records if r.cash),
            "avg_turnover": _turnover(records, columns),
            "weights_history": [
                {"date": r.date, "n_eligible": r.n_eligible, "carried": r.carried,
                 "fallback_equal_weight": r.fallback_equal_weight,
                 "cash": r.cash,
                 "weights": {k: v for k, v in r.weights.items() if v > 0.0}}
                for r in records
            ],
        }

    union_eligible = sorted(
        {sid for entry in eligibility_log for sid in entry["eligible"]}
    )
    union_rejected: dict[str, set[str]] = {}
    for entry in eligibility_log:
        for sleeve_id, reasons in entry["rejected"].items():
            union_rejected.setdefault(sleeve_id, set()).update(reasons)

    notes: list[str] = []
    if skipped_rebalances:
        notes.append(
            f"{skipped_rebalances} month-end dates skipped: insufficient "
            f"lookback ({cfg.lookback_days} business days) before them"
        )
    if not union_eligible:
        notes.append("no sleeve was eligible at any rebalance date")
    full_sample_trades = sorted(
        {
            sleeve_id
            for entry in eligibility_log
            for sleeve_id, basis in entry["trade_count_basis"].items()
            if basis == "full_sample"
        }
    )
    matrix_sleeves = sorted(
        sleeve_id
        for sleeve_id, meta in sleeve_returns.metadata.items()
        if meta.source == "matrix"
    )
    if matrix_sleeves:
        notes.append(
            "matrix sleeves ("
            + ", ".join(matrix_sleeves)
            + ") are full-window backtest curves and are NOT verified "
            "out-of-sample by this module; OOS-ness is the caller's "
            "assertion (--matrix-is-oos at the CLI)"
        )
    if cfg.eligibility.allow_full_sample_trade_counts:
        notes.append(
            "allow_full_sample_trade_counts is ON: total_trades-only sleeves "
            "are admitted at point-in-time rebalances on a full-sample "
            "(future-inclusive) trade count"
        )
    if full_sample_trades:
        notes.append(
            "trade counts for "
            + ", ".join(full_sample_trades)
            + " use full-sample totals (no per-period timing available)"
        )

    report = {
        "module": "runner.sleeve_allocator",
        "phase": 1,
        "config": {
            "lookback_days": cfg.lookback_days,
            "min_window_observations": cfg.min_window_observations,
            "objectives_requested": list(cfg.objectives),
            "rebalance": "monthly",
            "eligibility": {
                "min_history_days": cfg.eligibility.min_history_days,
                "min_trades": cfg.eligibility.min_trades,
                "ruin_equity_floor": cfg.eligibility.ruin_equity_floor,
                "min_coverage": cfg.eligibility.min_coverage,
                "allow_full_sample_trade_counts": (
                    cfg.eligibility.allow_full_sample_trade_counts
                ),
            },
        },
        "baselines": list(BASELINE_OBJECTIVES),
        "objectives": objectives_report,
        "rebalance_dates": [d.date().isoformat() for d in rebalance_dates],
        "skipped_rebalances": skipped_rebalances,
        "sleeves": {
            "sources": {
                sleeve_id: meta.source
                for sleeve_id, meta in sleeve_returns.metadata.items()
            },
            "eligible_union": union_eligible,
            "rejected_union": {
                key: sorted(value) for key, value in union_rejected.items()
            },
            "eligibility_history": eligibility_log,
        },
        "notes": notes,
    }

    return AllocationResult(
        returns=returns_map,
        equity=equity_map,
        rebalances=rebalances,
        report=report,
    )

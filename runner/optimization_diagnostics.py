"""Statistical diagnostics for parameter optimization.

A single train/test split with a raw metric is not evidence that a parameter
choice generalises. This module supplies the pieces that make the result
interpretable, each as a pure function so it can be tested in isolation:

* :func:`embargo_split` - insert a purge/embargo gap between train and test so
  overlapping label windows and holding periods cannot leak test information
  backwards into training.
* :func:`walk_forward_splits` - repeated sequential folds, so the result is not a
  single lucky split.
* :func:`plateau_analysis` - distinguish a broad, stable optimum from a single
  spiky parameter value, which is the usual signature of curve fitting.
* :func:`multiple_testing_correction` - account for the fact that the best of N
  trials is biased upward.

None of these change the optimizer's selection rule; they report how much the
selection should be trusted.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pandas as pd

__all__ = [
    "EmbargoSplit",
    "embargo_split",
    "walk_forward_splits",
    "plateau_analysis",
    "multiple_testing_correction",
]


def _as_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


@dataclass(frozen=True)
class EmbargoSplit:
    """A train/test split with a purge gap between the two windows."""

    train: dict[str, str]
    test: dict[str, str]
    embargo: dict[str, str]
    embargo_bars_equivalent: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": self.train,
            "test": self.test,
            "embargo": self.embargo,
            "embargo_bars_equivalent": self.embargo_bars_equivalent,
        }


def embargo_split(
    start: Any,
    end: Any,
    train_split_pct: float,
    *,
    embargo_pct: float = 0.0,
    bar_duration: pd.Timedelta | None = None,
    holding_bars: int = 0,
) -> EmbargoSplit:
    """Split a period into train / embargo / test.

    ``embargo_pct`` is the fraction of the total window to drop between train and
    test. ``holding_bars`` additionally drops as many bars as the longest trade
    can stay open, which is the case look-ahead actually leaks through: a trade
    opened just before the split can close inside the test window, so its label
    is partly test information.

    Raises ValueError when the requested split or embargo leaves an empty
    window, rather than silently producing a degenerate split.
    """
    if not 0.0 < train_split_pct < 1.0:
        raise ValueError("train_split_pct must be strictly between 0 and 1")
    if embargo_pct < 0.0:
        raise ValueError("embargo_pct must not be negative")
    if holding_bars < 0:
        raise ValueError("holding_bars must not be negative")

    start_ts = _as_timestamp(start)
    end_ts = _as_timestamp(end)
    if end_ts <= start_ts:
        raise ValueError(f"Invalid period: start={start!r}, end={end!r}")

    total = end_ts - start_ts
    split_ts = start_ts + total * train_split_pct
    embargo_duration = total * embargo_pct
    if holding_bars and bar_duration is not None:
        holding_duration = bar_duration * int(holding_bars)
        embargo_duration = max(embargo_duration, holding_duration)

    test_start_ts = split_ts + embargo_duration
    if split_ts <= start_ts or test_start_ts >= end_ts:
        raise ValueError(
            "train_split_pct/embargo_pct produce an empty train or test period; "
            f"train={start_ts}..{split_ts}, test={test_start_ts}..{end_ts}"
        )

    bars_equivalent = 0.0
    if bar_duration is not None and pd.notna(bar_duration) and bar_duration > pd.Timedelta(0):
        bars_equivalent = float(embargo_duration / bar_duration)

    return EmbargoSplit(
        train={"start": _iso(start_ts), "end": _iso(split_ts)},
        test={"start": _iso(test_start_ts), "end": _iso(end_ts)},
        embargo={"start": _iso(split_ts), "end": _iso(test_start_ts)},
        embargo_bars_equivalent=bars_equivalent,
    )


def walk_forward_splits(
    start: Any,
    end: Any,
    *,
    n_splits: int = 3,
    train_fraction: float = 0.7,
    embargo_pct: float = 0.0,
    bar_duration: pd.Timedelta | None = None,
    holding_bars: int = 0,
) -> list[dict[str, Any]]:
    """Sequential walk-forward folds across the period.

    Each fold trains on a rolling earlier window and tests on the immediately
    following (embargo-separated) window, then advances. Folds are returned in
    chronological order. Raises ValueError on a non-positive ``n_splits`` or a
    window too short to yield one fold.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be at least 1")
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be strictly between 0 and 1")

    start_ts = _as_timestamp(start)
    end_ts = _as_timestamp(end)
    if end_ts <= start_ts:
        raise ValueError(f"Invalid period: start={start!r}, end={end!r}")

    total = end_ts - start_ts
    fold_length = total / n_splits
    folds: list[dict[str, Any]] = []
    for index in range(n_splits):
        fold_start = start_ts + fold_length * index
        fold_end = fold_start + fold_length
        if index == n_splits - 1:
            fold_end = end_ts
        if fold_end <= fold_start:
            continue
        try:
            split = embargo_split(
                fold_start,
                fold_end,
                train_split_pct=train_fraction,
                embargo_pct=embargo_pct,
                bar_duration=bar_duration,
                holding_bars=holding_bars,
            )
        except ValueError:
            # A fold too short for the requested fractions is skipped rather than
            # reported as a valid fold.
            continue
        folds.append({"fold": index, **split.as_dict()})

    if not folds:
        raise ValueError(
            "Period is too short to produce a single walk-forward fold with the "
            "requested train_fraction/embargo_pct"
        )
    return folds


@dataclass
class PlateauAnalysis:
    """How stable the chosen parameter is across the evaluated grid."""

    is_plateau: bool
    plateau_width: int
    best_value: Any
    within_1pct_of_best: int
    total_evaluated: int
    sensitivity: float | None = None
    neighbours: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_plateau": self.is_plateau,
            "plateau_width": self.plateau_width,
            "best_value": self.best_value,
            "within_1pct_of_best": self.within_1pct_of_best,
            "total_evaluated": self.total_evaluated,
            "sensitivity": self.sensitivity,
            "neighbours": self.neighbours,
        }


def plateau_analysis(
    trials: Iterable[dict[str, Any]],
    *,
    parameter: str,
    metric: str,
    tolerance: float = 0.01,
    min_neighbours: int = 1,
) -> PlateauAnalysis | None:
    """Decide whether the winning parameter sits on a stable plateau.

    ``trials`` are mappings with at least ``parameter`` and ``metric``. The best
    trial is the one with the highest metric. A genuine edge usually has several
    neighbouring parameter values scoring within ``tolerance`` (relative) of the
    best; a lone spike is the classic overfitting signature and is reported as
    ``is_plateau=False``.

    Returns None when there is nothing to analyse.
    """
    rows = [
        trial
        for trial in trials
        if trial.get(parameter) is not None and trial.get(metric) is not None
    ]
    if not rows:
        return None

    def _score(trial: dict[str, Any]) -> float:
        value = trial.get(metric)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return float("-inf")
        return numeric if math.isfinite(numeric) else float("-inf")

    best = max(rows, key=_score)
    best_score = _score(best)
    if not math.isfinite(best_score):
        return None

    threshold = abs(best_score) * tolerance if best_score != 0 else tolerance
    near_best = [
        trial
        for trial in rows
        if math.isfinite(_score(trial)) and (best_score - _score(trial)) <= threshold
    ]

    neighbours: list[dict[str, Any]] = []
    sensitivity: float | None = None
    for trial in rows:
        try:
            parameter_value = float(trial.get(parameter))
        except (TypeError, ValueError):
            continue
        try:
            best_parameter_value = float(best.get(parameter))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(_score(trial)):
            continue
        neighbours.append(
            {
                "value": trial.get(parameter),
                "metric": trial.get(metric),
                "delta": round(_score(trial) - best_score, 10),
                "relative_delta": (
                    round((_score(trial) - best_score) / abs(best_score), 6)
                    if best_score
                    else None
                ),
                "is_best": trial is best,
                "distance": round(abs(parameter_value - best_parameter_value), 10),
            }
        )
    neighbours.sort(key=lambda item: item["distance"])

    if len(neighbours) > 1 and abs(best_score) > 0:
        nearest_other = next((item for item in neighbours if not item["is_best"]), None)
        if nearest_other is not None:
            sensitivity = abs(nearest_other["delta"]) / abs(best_score)

    return PlateauAnalysis(
        is_plateau=len(near_best) >= max(1, min_neighbours) + 1,
        plateau_width=len(near_best),
        best_value=best.get(parameter),
        within_1pct_of_best=len(near_best),
        total_evaluated=len(rows),
        sensitivity=sensitivity,
        neighbours=neighbours[:10],
    )


def multiple_testing_correction(
    trial_metrics: Sequence[float],
    *,
    observed_metric: float | None = None,
    alpha: float = 0.05,
    penalty_floor: float | None = None,
) -> dict[str, Any]:
    """Correct for selecting the best of many trials.

    Reports the Bonferroni threshold and a deflated-Sharpe-style adjustment: the
    expected maximum of N independent trials under the null, which is the amount
    by which the best score is inflated purely by search. When ``observed_metric``
    is supplied it is compared against that expectation, so a result can be
    labelled as surviving selection bias or not.
    """
    # No-trade trials are sentinels, not strategy performances: their metric is a
    # large fixed penalty. Leaving them in would put a -1e9 in the sample and
    # make every spread statistic meaningless, so they are counted separately.
    penalty = penalty_floor
    clean: list[float] = []
    penalties = 0
    for value in trial_metrics or []:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            continue
        if penalty is not None and numeric <= penalty:
            penalties += 1
            continue
        clean.append(numeric)

    n_trials = len(clean)
    if n_trials == 0:
        return {
            "n_trials": 0,
            "alpha": alpha,
            "bonferroni_threshold": None,
            "expected_max_under_null": None,
            "observed_metric": None,
            "survives_selection_bias": None,
            "note": "no finite trial metrics to correct",
        }

    bonferroni = alpha / n_trials
    # Expected maximum of N standard normals under the null, via the standard
    # normal quantile and Euler-Mascheroni. This is a dimensionless z-score: it
    # has to be scaled onto the metric's own distribution before it can be
    # compared with a metric value.
    z_expected_max = (
        math.sqrt(2.0 * math.log(n_trials))
        - (math.log(math.log(n_trials)) + 0.5772156649015329)
        / math.sqrt(2.0 * math.log(n_trials))
        if n_trials > 1
        else 0.0
    )
    mean_metric = statistics.fmean(clean)
    sigma_metric = statistics.pstdev(clean) if len(clean) > 1 else 0.0
    expected_max = mean_metric + sigma_metric * z_expected_max

    # The *expected* max is the centre of the best-of-N distribution, so roughly
    # half of all pure-noise searches would beat it: it is a diagnostic, not a
    # bar to clear. The bar is the Bonferroni-corrected one-sided quantile, so a
    # noise search passes it only alpha of the time.
    z_significance = statistics.NormalDist().inv_cdf(1.0 - bonferroni)
    significance_threshold = mean_metric + sigma_metric * z_significance

    best_observed = max(clean)
    reported = best_observed if observed_metric is None else float(observed_metric)

    return {
        "n_trials": n_trials,
        "alpha": alpha,
        "bonferroni_threshold": round(bonferroni, 8),
        "expected_max_under_null": round(expected_max, 6),
        "significance_threshold": round(significance_threshold, 6),
        "n_penalty_trials_excluded": penalties,
        "trial_metric_mean": round(mean_metric, 6),
        "trial_metric_std": round(sigma_metric, 6),
        "observed_metric": round(reported, 6),
        "best_trial_metric": round(best_observed, 6),
        # A single-trial result has no search bias to correct for.
        "survives_selection_bias": (
            None if n_trials <= 1 else reported >= significance_threshold
        ),
        "note": (
            "expected_max_under_null is the trial mean plus the standard "
            f"deviation times the expected maximum of {n_trials} independent "
            "standard normals under the null, and is the centre of the "
            "best-of-N distribution rather than a bar to clear. "
            "significance_threshold is the Bonferroni-corrected one-sided "
            "quantile, which is what survives_selection_bias compares against. "
            "No-trade penalty trials are excluded from the mean and spread."
        ),
    }

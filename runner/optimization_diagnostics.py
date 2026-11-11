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
* :func:`subperiods_from_walk_forward` - turn the walk-forward folds into the
  sub-periods a per-period stability view is measured over.
* :func:`realized_pnl_by_subperiod` - attribute each trial's realized result to
  the sub-period it was realized in.
* :func:`subperiod_parameter_stability` - does the parameter set that wins
  overall also win in each sub-period? This is where a universe that is not
  point-in-time shows up: a list that drifts through the window rewards
  different parameters at different times.

None of these change the optimizer's selection rule; they report how much the
selection should be trusted.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = [
    "EmbargoSplit",
    "embargo_split",
    "ThreeWaySplit",
    "three_way_split",
    "walk_forward_splits",
    "walk_forward_train_validation_folds",
    "select_by_validation",
    "plateau_analysis",
    "multiple_testing_correction",
    "subperiods_from_walk_forward",
    "realized_pnl_by_subperiod",
    "subperiod_parameter_stability",
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


def _try_timestamp(value: Any) -> pd.Timestamp | None:
    """A parsed timestamp, or None when the value cannot be read.

    Used where the input is a record's own field rather than a validated
    argument: a trade with an unreadable exit time is skipped, never allowed to
    abort the run or be silently read as the start of the epoch.
    """
    if value is None:
        return None
    try:
        return _as_timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


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


@dataclass(frozen=True)
class ThreeWaySplit:
    """A train/validation/test split with embargo gaps between segments."""

    train: dict[str, str]
    validation: dict[str, str]
    test: dict[str, str]
    embargo_train_val: dict[str, str]
    embargo_val_test: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": self.train,
            "validation": self.validation,
            "test": self.test,
            "embargo_train_val": self.embargo_train_val,
            "embargo_val_test": self.embargo_val_test,
        }


def three_way_split(
    start: Any,
    end: Any,
    *,
    train_fraction: float,
    validation_fraction: float,
    test_fraction: float,
    embargo_days: int = 0,
    embargo_pct: float = 0.0,
) -> ThreeWaySplit:
    """Split a period into train / embargo / validation / embargo / test.

    Layout in time order: train, embargo gap, validation, embargo gap, test.
    TEST is the last segment and ends at ``end``.

    The embargo gap has the same size semantics as ``_split_backtest_period``:
    fixed ``embargo_days`` plus ``embargo_pct`` of the whole range.

    Raises ValueError if any segment would be empty.
    """
    if not (0.0 < train_fraction < 1.0):
        raise ValueError("train_fraction must be strictly between 0 and 1")
    if not (0.0 < validation_fraction < 1.0):
        raise ValueError("validation_fraction must be strictly between 0 and 1")
    if not (0.0 < test_fraction < 1.0):
        raise ValueError("test_fraction must be strictly between 0 and 1")
    total_frac = train_fraction + validation_fraction + test_fraction
    if abs(total_frac - 1.0) > 1e-9:
        raise ValueError(
            f"train_fraction + validation_fraction + test_fraction must sum to 1.0, "
            f"got {total_frac:.10f}"
        )
    if embargo_days < 0:
        raise ValueError("embargo_days must not be negative")
    if embargo_pct < 0.0:
        raise ValueError("embargo_pct must not be negative")

    start_ts = _as_timestamp(start)
    end_ts = _as_timestamp(end)
    if end_ts <= start_ts:
        raise ValueError(f"Invalid period: start={start!r}, end={end!r}")

    total = end_ts - start_ts
    embargo_gap = pd.Timedelta(days=max(int(embargo_days), 0)) + (total * float(embargo_pct))

    # Compute segment boundaries working backwards from end (test ends at end)
    test_end_ts = end_ts
    test_start_ts = end_ts - total * test_fraction
    embargo_val_test_end_ts = test_start_ts
    embargo_val_test_start_ts = embargo_val_test_end_ts - embargo_gap
    validation_end_ts = embargo_val_test_start_ts
    validation_start_ts = validation_end_ts - total * validation_fraction
    embargo_train_val_end_ts = validation_start_ts
    embargo_train_val_start_ts = embargo_train_val_end_ts - embargo_gap
    train_end_ts = embargo_train_val_start_ts
    train_start_ts = start_ts

    # Verify all segments have positive duration (embargo gaps can be zero)
    if train_end_ts <= train_start_ts:
        raise ValueError("train segment would be empty")
    if embargo_train_val_end_ts < embargo_train_val_start_ts:
        raise ValueError("embargo between train and validation would be negative")
    if validation_end_ts <= validation_start_ts:
        raise ValueError("validation segment would be empty")
    if embargo_val_test_end_ts < embargo_val_test_start_ts:
        raise ValueError("embargo between validation and test would be negative")
    if test_end_ts <= test_start_ts:
        raise ValueError("test segment would be empty")

    # Verify order
    if not (train_start_ts < train_end_ts <= embargo_train_val_start_ts <= embargo_train_val_end_ts <= validation_start_ts < validation_end_ts <= embargo_val_test_start_ts <= embargo_val_test_end_ts <= test_start_ts < test_end_ts):
        raise ValueError("segment boundaries are not in correct order")

    return ThreeWaySplit(
        train={"start": _iso(train_start_ts), "end": _iso(train_end_ts)},
        validation={"start": _iso(validation_start_ts), "end": _iso(validation_end_ts)},
        test={"start": _iso(test_start_ts), "end": _iso(test_end_ts)},
        embargo_train_val={"start": _iso(embargo_train_val_start_ts), "end": _iso(embargo_train_val_end_ts)},
        embargo_val_test={"start": _iso(embargo_val_test_start_ts), "end": _iso(embargo_val_test_end_ts)},
    )


def walk_forward_train_validation_folds(
    start: Any,
    end: Any,
    *,
    n_splits: int = 3,
    train_fraction: float = 0.7,
    embargo_pct: float = 0.0,
    bar_duration: pd.Timedelta | None = None,
    holding_bars: int = 0,
    test_fraction: float = 0.2,
) -> list[dict[str, Any]]:
    """Walk-forward folds applied ONLY to the range before the test holdout.

    The test holdout is the last ``test_fraction`` of the period (plus an embargo
    gap before it). Walk-forward folds split the remaining range (train +
    validation) into ``n_splits`` sequential folds, each being train + embargo +
    validation.

    Reuses ``walk_forward_splits`` for the fold logic.
    """
    if not (0.0 < test_fraction < 1.0):
        raise ValueError("test_fraction must be strictly between 0 and 1")
    if n_splits < 1:
        raise ValueError("n_splits must be at least 1")

    start_ts = _as_timestamp(start)
    end_ts = _as_timestamp(end)
    if end_ts <= start_ts:
        raise ValueError(f"Invalid period: start={start!r}, end={end!r}")

    total = end_ts - start_ts
    # The range available for walk-forward is everything before the test holdout
    # Test starts at end - total * test_fraction, minus embargo gap
    # For the walk-forward folds, we use the range [start, test_start - embargo)
    # Since embargo_pct applies to the whole range, the embargo before test is:
    embargo_gap = total * embargo_pct
    test_start_ts = end_ts - total * test_fraction
    wf_end_ts = test_start_ts - embargo_gap
    if wf_end_ts <= start_ts:
        raise ValueError(
            "test_fraction and embargo leave no room for walk-forward folds"
        )

    # Use walk_forward_splits on the range before the test holdout
    # The train_fraction here is relative to each fold, not the whole range
    folds = walk_forward_splits(
        start_ts,
        wf_end_ts,
        n_splits=n_splits,
        train_fraction=train_fraction,
        embargo_pct=embargo_pct,
        bar_duration=bar_duration,
        holding_bars=holding_bars,
    )
    return folds


def select_by_validation(
    trials: Sequence[Mapping[str, Any]],
    *,
    top_fraction: float,
    score_key: str = "validation_score",
    trial_number_key: str = "number",
) -> dict[str, Any]:
    """Select top trials by validation score.

    Each trial must have a trial number (``trial_number_key``) and a validation
    score (``score_key``). Trials without a finite validation score are excluded.
    Higher scores are better.

    Returns a dict with:
    - ``ranked``: all trials with finite scores, sorted by score descending,
      then by trial number ascending (deterministic tie-break).
    - ``winner``: the top-ranked trial (rank 1), or None if no valid trials.
    - ``top_set``: the top ``max(1, ceil(top_fraction * n_ranked))`` trials.

    Deterministic tie-break: equal scores are ordered by lower trial number first.
    """
    if not 0.0 < top_fraction <= 1.0:
        raise ValueError("top_fraction must be in (0, 1]")

    valid_trials = []
    for trial in trials:
        score = trial.get(score_key)
        number = trial.get(trial_number_key)
        if score is None or number is None:
            continue
        try:
            score_float = float(score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score_float):
            continue
        try:
            number_int = int(number)
        except (TypeError, ValueError):
            continue
        valid_trials.append((score_float, number_int, trial))

    # Sort by score descending, then by trial number ascending (tie-break)
    valid_trials.sort(key=lambda x: (-x[0], x[1]))
    ranked = [t for _, _, t in valid_trials]

    if not ranked:
        return {"ranked": [], "winner": None, "top_set": []}

    winner = ranked[0]
    n_ranked = len(ranked)
    top_k = max(1, math.ceil(top_fraction * n_ranked))
    top_set = ranked[:top_k]

    return {"ranked": ranked, "winner": winner, "top_set": top_set}


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
    direction: str = "maximize"

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
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
    direction: str = "maximize",
) -> PlateauAnalysis | None:
    """Decide whether the winning parameter sits on a stable plateau.

    ``trials`` are mappings with at least ``parameter`` and ``metric``. The best
    trial is the one the study would select: the highest metric for
    ``direction="maximize"``, the lowest for ``"minimize"`` (``p_value`` is a
    minimize target; centring its plateau on the highest value would analyse the
    WORST trial). Deltas are reported in "higher is better" space, so a trial
    that is worse than the best has a negative ``delta`` in either direction;
    the raw ``metric`` is carried unchanged. A genuine edge usually has several
    neighbouring parameter values scoring within ``tolerance`` (relative) of the
    best; a lone spike is the classic overfitting signature and is reported as
    ``is_plateau=False``.

    Returns None when there is nothing to analyse.
    """
    normalised_direction = str(direction or "").strip().lower()
    if normalised_direction not in {"maximize", "minimize"}:
        raise ValueError("direction must be 'maximize' or 'minimize'")
    sign = -1.0 if normalised_direction == "minimize" else 1.0
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
        return sign * numeric if math.isfinite(numeric) else float("-inf")

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
        direction=normalised_direction,
    )


def multiple_testing_correction(
    trial_metrics: Sequence[float],
    *,
    observed_metric: float | None = None,
    alpha: float = 0.05,
    penalty_floor: float | None = None,
    direction: str = "maximize",
) -> dict[str, Any]:
    """Correct for selecting the best of many trials.

    Reports the Bonferroni threshold and a deflated-Sharpe-style adjustment: the
    expected extreme of N independent trials under the null, which is the amount
    by which the selected score is displaced purely by search. When
    ``observed_metric`` is supplied it is compared against that expectation, so a
    result can be labelled as surviving selection bias or not.

    ``direction`` must match how the study actually selects its winner, because
    the whole statistic is signed around it. A ``minimize`` study selects its
    *smallest* trial (``p_value`` is the one such target), so its expected
    extreme, its significance bar and the survives test are all reflected rather
    than added. Taking the maximum of a minimize study reports the search bias of
    its WORST trial, which inverts the verdict: a p-value study would claim to
    survive selection bias exactly when its least significant trial cleared a
    high bar.
    """
    minimize = str(direction or "maximize").strip().lower() == "minimize"
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
            "direction": direction,
            "bonferroni_threshold": None,
            "expected_max_under_null": None,
            "observed_metric": None,
            "survives_selection_bias": None,
            "note": "no finite trial metrics to correct",
        }

    bonferroni = alpha / n_trials
    # Expected extreme of N standard normals under the null, via the standard
    # normal quantile and Euler-Mascheroni. This is a dimensionless z-score: it
    # has to be scaled onto the metric's own distribution before it can be
    # compared with a metric value. It is a magnitude, so it is *subtracted* for
    # a minimize study.
    z_expected_max = (
        math.sqrt(2.0 * math.log(n_trials))
        - (math.log(math.log(n_trials)) + 0.5772156649015329)
        / math.sqrt(2.0 * math.log(n_trials))
        if n_trials > 1
        else 0.0
    )
    mean_metric = statistics.fmean(clean)
    sigma_metric = statistics.pstdev(clean) if len(clean) > 1 else 0.0
    sign = -1.0 if minimize else 1.0
    expected_max = mean_metric + sign * sigma_metric * z_expected_max

    # The *expected* extreme is the centre of the best-of-N distribution, so
    # roughly half of all pure-noise searches would beat it: it is a diagnostic,
    # not a bar to clear. The bar is the Bonferroni-corrected one-sided quantile,
    # so a noise search passes it only alpha of the time. For a minimize study
    # the relevant tail is the lower one.
    z_significance = statistics.NormalDist().inv_cdf(1.0 - bonferroni)
    significance_threshold = mean_metric + sign * sigma_metric * z_significance

    best_observed = min(clean) if minimize else max(clean)
    reported = best_observed if observed_metric is None else float(observed_metric)
    # A bounded metric gets the direct test instead of the z-bar.
    #
    # `mean - z*sigma` is the right construction for an unbounded metric like
    # Sharpe, and structurally wrong for one bounded in [0, 1] like a p-value: a
    # uniform p-value sample has mean ~0.5 and sigma ~0.29, so at n=60 the bar
    # lands at 0.56 - 3.16*0.29 = -0.36, and no p-value can ever be <= -0.36.
    # The verdict was a constant False - measured 0/300 on realistic p-value
    # studies - and it read as a *measured negative* rather than an absent
    # measurement, which is the "never render absence as a value" failure this
    # repo keeps fixing elsewhere.
    #
    # The direct test is `min(p) <= alpha / n`: exact, bounded, and interpretable
    # as "does the best trial clear the Bonferroni-corrected bar". Its false
    # positive rate on pure noise is bounded by alpha, which is the entire point of
    # a multiple-testing correction.
    #
    # Detected from the data rather than passed in, because the caller knows the
    # direction but not the metric name, and a hardcoded metric list here would be
    # exactly the "one rule in two places" shape this branch has been closing.
    # Every observed value in [0, 1] is a necessary condition rather than a
    # sufficient one, so an unbounded metric that happens to land there degrades
    # to the stricter test instead of to a wrong verdict.
    if minimize and all(0.0 <= value <= 1.0 for value in clean):
        significance_threshold = bonferroni
        survives = reported <= bonferroni
    else:
        survives = (
            reported <= significance_threshold
            if minimize
            else reported >= significance_threshold
        )

    return {
        "n_trials": n_trials,
        "alpha": alpha,
        "direction": direction,
        "bonferroni_threshold": round(bonferroni, 8),
        "expected_max_under_null": round(expected_max, 6),
        "significance_threshold": round(significance_threshold, 6),
        "n_penalty_trials_excluded": penalties,
        "trial_metric_mean": round(mean_metric, 6),
        "trial_metric_std": round(sigma_metric, 6),
        "observed_metric": round(reported, 6),
        "best_trial_metric": round(best_observed, 6),
        # A single-trial result has no search bias to correct for.
        "survives_selection_bias": (None if n_trials <= 1 else survives),
        "note": (
            f"This study is a {direction} study, so expected_max_under_null and "
            "significance_threshold are measured from the "
            f"{'minimum' if minimize else 'maximum'} end of the trial sample and "
            "the survives test is the matching inequality. "
            "expected_max_under_null is the trial mean plus the standard "
            f"deviation times the expected maximum of {n_trials} independent "
            "standard normals under the null, and is the centre of the "
            "best-of-N distribution rather than a bar to clear. "
            "significance_threshold is the Bonferroni-corrected one-sided "
            "quantile, which is what survives_selection_bias compares against. "
            "No-trade penalty trials are excluded from the mean and spread."
        ),
    }


# ─── per-sub-period parameter stability ──────────────────────────────────────

#: Sentinel for "no selected value was supplied", kept distinct from ``None``
#: because ``None`` is itself a legal parameter value.
_UNSET: Any = object()


def subperiods_from_walk_forward(
    folds: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Turn walk-forward folds into the sub-periods a stability view measures.

    Each fold's *test* window is one sub-period: those windows are the
    sequential, chronological slices a reader can compare, and reusing them
    means the stability view cannot disagree with the folds already reported
    next to it. The windows do not tile the period exactly — each fold's
    training slice and embargo gap are not part of any sub-period — so the
    returned sub-periods carry their own bounds and callers can report the
    coverage they actually measured.

    Raises ValueError on a fold that carries no usable test window, rather
    than silently shortening the set of sub-periods.
    """
    subperiods: list[dict[str, Any]] = []
    for fold in folds or []:
        if not isinstance(fold, Mapping):
            raise ValueError(f"Walk-forward fold is not a mapping: {fold!r}")
        test_window = fold.get("test")
        start = test_window.get("start") if isinstance(test_window, Mapping) else None
        end = test_window.get("end") if isinstance(test_window, Mapping) else None
        if not start or not end:
            raise ValueError(
                f"Walk-forward fold {fold.get('fold')!r} carries no test window"
            )
        subperiods.append(
            {
                "label": f"fold_{fold.get('fold')}",
                "fold": fold.get("fold"),
                "start": start,
                "end": end,
            }
        )
    return subperiods


def realized_pnl_by_subperiod(
    events: Iterable[tuple[Any, float]],
    subperiods: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """Attribute each ``(timestamp, pnl)`` event to the sub-period holding it.

    Attribution is by realization time, so a trade opened in one sub-period and
    closed in the next counts in the one that produced the result. Every
    declared sub-period appears in the result, defaulting to ``0.0``, so a
    reader can tell "nothing was realized here" from "this sub-period was
    never measured". Events whose timestamp cannot be read, or that fall
    outside every sub-period (the embargo gaps and training slices), are
    dropped — they belong to no measured period and inventing one for them
    would misstate what was covered.
    """
    windows: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    for subperiod in subperiods or []:
        label = str(subperiod.get("label") or "")
        start = _try_timestamp(subperiod.get("start"))
        end = _try_timestamp(subperiod.get("end"))
        if not label or start is None or end is None or end <= start:
            continue
        windows.append((label, start, end))

    realized = {label: 0.0 for label, _start, _end in windows}
    for timestamp, pnl in events or []:
        moment = _try_timestamp(timestamp)
        if moment is None:
            continue
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        for label, start, end in windows:
            if start <= moment <= end:
                realized[label] += value
                break
    return realized


def _value_key(value: Any) -> str:
    """A hashable, stable key for a parameter value of any type."""
    return json.dumps(value, sort_keys=True, default=str)


def _ranks(scores: Mapping[str, float], *, direction: str) -> dict[str, int]:
    """Competition ranking (1 = best): equal scores share a rank, the next
    distinct score skips accordingly."""
    ordered = sorted(set(scores.values()), reverse=(direction != "minimize"))
    rank_of_score = {score: index + 1 for index, score in enumerate(ordered)}
    return {key: rank_of_score[score] for key, score in scores.items()}


def _unknown_stability(reason: str, *, parameter: str, metric_name: str) -> dict[str, Any]:
    """The shape a reader gets when the view cannot be computed.

    Every count is ``None`` and the reason is named, following the
    ``undefined_metrics`` convention: an absent number must never be readable as
    a zero or a pass.
    """
    return {
        "status": "unknown",
        "parameter": parameter,
        "metric": metric_name or None,
        "n_periods": None,
        "n_parameter_values": None,
        "selected_value": None,
        "selected_wins_n_periods": None,
        "selected_win_rate": None,
        "selected_mean_rank": None,
        "selected_worst_rank": None,
        "distinct_period_winners": None,
        "winner_agreement": "unknown",
        "per_period": [],
        "per_parameter_value": [],
        "subperiods": [],
        "reason": reason,
        "undefined_metrics": ["subperiod_parameter_stability"],
    }


def subperiod_parameter_stability(
    observations: Iterable[Mapping[str, Any]],
    *,
    parameter: str,
    metric: str = "metric",
    direction: str = "maximize",
    selected_value: Any = _UNSET,
    selected_parameters: Mapping[str, Any] | None = None,
    subperiods: Sequence[Mapping[str, Any]] | None = None,
    metric_name: str = "",
    period_key: str = "period",
) -> dict[str, Any]:
    """Does the winning parameter set also win in every sub-period?

    ``observations`` are mappings with a sub-period label (``period_key``), a
    ``params`` mapping and a numeric ``metric``. Trials that share a parameter
    value inside one sub-period are reduced by the **mean** of their metrics,
    so an axis value that was sampled more often does not win on sample size;
    the per-value detail is reported in ``per_parameter_value`` so that
    imbalance stays visible.

    ``selected_value`` is the parameter value the run actually chose;
    ``selected_parameters`` is the whole chosen parameter mapping, from which
    the axis value is taken. When neither supplies a value, the one with the
    best mean across all sub-periods is used and the payload says so
    (``selected_value_source``). ``winner_agreement`` answers the question
    directly — ``"stable"`` when the selected value is the best in every
    sub-period it was observed in, ``"unstable"`` otherwise, ``"unknown"`` when
    fewer than two sub-periods carry a usable comparison.

    Returns the ``unknown`` shape (all counts ``None``, reason recorded) when
    there is nothing to compare. It never invents a rank for a sub-period in
    which the selected value was not observed.
    """
    if selected_value is _UNSET and selected_parameters is not None:
        if parameter in selected_parameters:
            selected_value = selected_parameters[parameter]
    if str(direction) not in {"maximize", "minimize"}:
        raise ValueError("direction must be 'maximize' or 'minimize'")

    by_period: dict[str, dict[str, list[float]]] = {}
    display_value: dict[str, Any] = {}
    for observation in observations or []:
        if not isinstance(observation, Mapping):
            continue
        period = str(observation.get(period_key) or "")
        params = observation.get("params")
        if not period or not isinstance(params, Mapping) or parameter not in params:
            continue
        value = params.get(parameter)
        if value is None:
            continue
        try:
            score = float(observation.get(metric))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        key = _value_key(value)
        by_period.setdefault(period, {}).setdefault(key, []).append(score)
        display_value.setdefault(key, value)

    if not by_period:
        return _unknown_stability(
            f"no sub-period observations for parameter {parameter!r}",
            parameter=parameter,
            metric_name=metric_name or metric,
        )

    # Mean metric per (sub-period, parameter value).
    period_scores: dict[str, dict[str, float]] = {}
    for period, per_value in by_period.items():
        period_scores[period] = {
            key: statistics.fmean(scores) for key, scores in per_value.items()
        }
    period_ranks = {
        period: _ranks(scores, direction=direction) for period, scores in period_scores.items()
    }

    pooled: dict[str, list[float]] = {}
    for scores in period_scores.values():
        for key, score in scores.items():
            pooled.setdefault(key, []).append(score)
    pooled_mean = {key: statistics.fmean(values) for key, values in pooled.items()}

    if selected_value is _UNSET:
        selected_key = min(
            pooled_mean,
            key=lambda key: (-pooled_mean[key] if direction == "maximize" else pooled_mean[key]),
        )
        selection_source = "best_mean_across_subperiods"
    else:
        selected_key = _value_key(selected_value)
        selection_source = "declared"
    if selected_key not in display_value:
        return _unknown_stability(
            f"selected value {selected_value!r} of parameter {parameter!r} was "
            "never observed",
            parameter=parameter,
            metric_name=metric_name or metric,
        )

    per_period: list[dict[str, Any]] = []
    selected_ranks: list[int] = []
    selected_wins = 0
    winners: set[str] = set()
    for period in sorted(period_scores):
        scores = period_scores[period]
        ranks = period_ranks[period]
        best_key = min(
            scores, key=lambda key: (-scores[key] if direction == "maximize" else scores[key])
        )
        winners.add(best_key)
        selected_here = selected_key in scores
        rank = ranks.get(selected_key)
        if selected_here:
            selected_ranks.append(int(rank))
            if best_key == selected_key:
                selected_wins += 1
        per_period.append(
            {
                "period": period,
                "best_value": display_value[best_key],
                "best_metric": round(scores[best_key], 6),
                "n_parameter_values": len(scores),
                "selected_value": display_value[selected_key],
                "selected_observed": selected_here,
                "selected_metric": round(scores[selected_key], 6) if selected_here else None,
                "selected_rank": rank,
            }
        )

    n_periods = len(per_period)
    n_selected_periods = len(selected_ranks)
    per_value: list[dict[str, Any]] = []
    for key, value in display_value.items():
        ranks_here = [ranks[key] for ranks in period_ranks.values() if key in ranks]
        best_in = sum(1 for row in per_period if row["best_value"] == value)
        per_value.append(
            {
                "value": value,
                "periods_observed": len(ranks_here),
                "mean_metric": round(pooled_mean[key], 6),
                "mean_rank": round(statistics.fmean(ranks_here), 4) if ranks_here else None,
                "worst_rank": max(ranks_here) if ranks_here else None,
                "best_in_n_periods": best_in,
            }
        )
    per_value.sort(
        key=lambda row: (row["mean_rank"] is None, row["mean_rank"] or 0, _value_key(row["value"]))
    )

    if n_selected_periods >= 2:
        agreement = "stable" if selected_wins == n_selected_periods else "unstable"
    else:
        agreement = "unknown"

    undefined_metrics: list[str] = []
    if n_selected_periods < n_periods:
        undefined_metrics.append("selected_value_rank_in_every_subperiod")
    if n_periods < 2:
        undefined_metrics.append("cross_subperiod_comparison")

    return {
        "status": "ok",
        "parameter": parameter,
        "metric": metric_name or metric,
        "direction": direction,
        "selected_value": display_value[selected_key],
        "selected_value_source": selection_source,
        "n_periods": n_periods,
        "n_parameter_values": len(display_value),
        "n_periods_selected_observed": n_selected_periods,
        "selected_wins_n_periods": selected_wins,
        "selected_win_rate": (
            round(selected_wins / n_selected_periods, 4) if n_selected_periods else None
        ),
        "selected_mean_rank": (
            round(statistics.fmean(selected_ranks), 4) if selected_ranks else None
        ),
        "selected_worst_rank": max(selected_ranks) if selected_ranks else None,
        "distinct_period_winners": len(winners),
        "winner_agreement": agreement,
        "per_period": per_period,
        "per_parameter_value": per_value,
        "subperiods": [dict(subperiod) for subperiod in (subperiods or [])],
        "note": (
            "Trials sharing a parameter value inside one sub-period are reduced "
            "by the mean of their metrics, so a value cannot win on sample size "
            f"alone; '{metric_name or metric}' is compared per sub-period in "
            f"direction '{direction}'. Sub-periods are the walk-forward test "
            "windows, so the embargo gaps and training slices between them are "
            "measured by no sub-period."
        ),
        "undefined_metrics": sorted(undefined_metrics),
    }

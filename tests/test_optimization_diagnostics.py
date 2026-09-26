"""Tests for optimizer statistical diagnostics."""

from __future__ import annotations

import pandas as pd
import pytest

from runner.optimization_diagnostics import (
    embargo_split,
    multiple_testing_correction,
    plateau_analysis,
    walk_forward_splits,
)


# ─── embargo_split ───────────────────────────────────────────────────────────


def test_embargo_split_drops_a_real_gap_between_train_and_test():
    split = embargo_split(
        "2024-01-01T00:00:00+00:00",
        "2024-01-11T00:00:00+00:00",
        train_split_pct=0.7,
        embargo_pct=0.1,
    )
    train_end = pd.Timestamp(split.train["end"])
    test_start = pd.Timestamp(split.test["start"])
    assert test_start > train_end, "test must start strictly after the train window"
    # 10 days total, 10% embargo => ~1 day gap.
    assert (test_start - train_end) == pytest.approx(pd.Timedelta(days=1), abs=pd.Timedelta(minutes=1))


def test_embargo_split_without_embargo_is_contiguous():
    split = embargo_split(
        "2024-01-01T00:00:00+00:00",
        "2024-01-11T00:00:00+00:00",
        train_split_pct=0.7,
    )
    assert pd.Timestamp(split.test["start"]) == pd.Timestamp(split.train["end"])


def test_embargo_split_holding_bars_widens_the_gap():
    """A trade open across the split leaks its label, so the gap must cover the
    longest possible holding period even when embargo_pct is zero."""
    hourly = pd.Timedelta(hours=1)
    without = embargo_split(
        "2024-01-01T00:00:00+00:00",
        "2024-01-11T00:00:00+00:00",
        train_split_pct=0.7,
        bar_duration=hourly,
        holding_bars=0,
    )
    with_hold = embargo_split(
        "2024-01-01T00:00:00+00:00",
        "2024-01-11T00:00:00+00:00",
        train_split_pct=0.7,
        bar_duration=hourly,
        holding_bars=48,
    )
    gap_without = pd.Timestamp(without.test["start"]) - pd.Timestamp(without.train["end"])
    gap_with = pd.Timestamp(with_hold.test["start"]) - pd.Timestamp(with_hold.train["end"])
    assert gap_with > gap_without
    assert with_hold.embargo_bars_equivalent >= 48


def test_embargo_split_reports_bars_equivalent():
    split = embargo_split(
        "2024-01-01T00:00:00+00:00",
        "2024-01-11T00:00:00+00:00",
        train_split_pct=0.5,
        embargo_pct=0.1,
        bar_duration=pd.Timedelta(hours=1),
    )
    assert split.embargo_bars_equivalent == pytest.approx(24.0, abs=0.5)


def test_embargo_split_rejects_degenerate_configuration():
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        embargo_split("2024-01-01", "2024-01-11", train_split_pct=1.0)
    with pytest.raises(ValueError, match="must not be negative"):
        embargo_split("2024-01-01", "2024-01-11", train_split_pct=0.5, embargo_pct=-0.1)
    # A gap that swallows the test window must fail loudly, not silently shrink.
    with pytest.raises(ValueError, match="empty train or test period"):
        embargo_split("2024-01-01", "2024-01-11", train_split_pct=0.9, embargo_pct=0.5)
    with pytest.raises(ValueError, match="Invalid period"):
        embargo_split("2024-01-11", "2024-01-01", train_split_pct=0.5)


# ─── walk_forward_splits ─────────────────────────────────────────────────────


def test_walk_forward_produces_ordered_non_overlapping_test_folds():
    folds = walk_forward_splits(
        "2024-01-01T00:00:00+00:00",
        "2024-04-01T00:00:00+00:00",
        n_splits=3,
        train_fraction=0.7,
        embargo_pct=0.05,
    )
    assert len(folds) == 3
    assert [fold["fold"] for fold in folds] == [0, 1, 2]
    test_starts = [pd.Timestamp(fold["test"]["start"]) for fold in folds]
    test_ends = [pd.Timestamp(fold["test"]["end"]) for fold in folds]
    assert test_starts == sorted(test_starts), "folds must be chronological"
    for start, end in zip(test_starts, test_ends):
        assert end > start
    for earlier, later in zip(test_ends, test_starts[1:]):
        assert later >= earlier, "test windows must not overlap"
    # Every fold's train window precedes its own test window.
    for fold in folds:
        assert pd.Timestamp(fold["train"]["end"]) <= pd.Timestamp(fold["test"]["start"])


def test_walk_forward_rejects_bad_configuration():
    with pytest.raises(ValueError, match="at least 1"):
        walk_forward_splits("2024-01-01", "2024-04-01", n_splits=0)
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        walk_forward_splits("2024-01-01", "2024-04-01", train_fraction=1.0)
    with pytest.raises(ValueError, match="too short"):
        # An embargo wider than what is left after training leaves no usable
        # test window in any fold.
        walk_forward_splits(
            "2024-01-01", "2024-04-01", n_splits=3, train_fraction=0.5, embargo_pct=0.9
        )


def test_walk_forward_never_lets_training_see_test_data():
    """The point of walk-forward: each fold's train window must end at or before
    its test window starts, and later folds must not train on earlier test data."""
    folds = walk_forward_splits(
        "2023-01-01T00:00:00+00:00",
        "2023-12-31T00:00:00+00:00",
        n_splits=4,
        train_fraction=0.6,
        embargo_pct=0.02,
    )
    for index, fold in enumerate(folds):
        train_end = pd.Timestamp(fold["train"]["end"])
        test_start = pd.Timestamp(fold["test"]["start"])
        assert train_end < test_start
        if index > 0:
            previous_test_end = pd.Timestamp(folds[index - 1]["test"]["end"])
            assert train_end > previous_test_end, (
                "rolling folds should retrain forward in time, not reuse old test data"
            )


# ─── plateau_analysis ────────────────────────────────────────────────────────


def test_plateau_detected_when_neighbours_score_similarly():
    trials = [
        {"atr": 2.0, "sharpe": 1.00},
        {"atr": 2.1, "sharpe": 0.995},
        {"atr": 2.2, "sharpe": 0.99},
        {"atr": 2.3, "sharpe": 0.70},
    ]
    result = plateau_analysis(trials, parameter="atr", metric="sharpe")
    assert result is not None
    assert result.is_plateau is True
    assert result.plateau_width >= 2
    assert result.best_value == 2.0
    assert result.sensitivity is not None
    assert result.sensitivity < 0.05


def test_spiky_optimum_is_not_a_plateau():
    """One lucky parameter with everything else far worse is the overfitting
    signature and must be reported as such."""
    trials = [
        {"atr": 2.0, "sharpe": 4.00},
        {"atr": 2.1, "sharpe": -0.50},
        {"atr": 2.2, "sharpe": 0.10},
        {"atr": 2.3, "sharpe": -0.90},
    ]
    result = plateau_analysis(trials, parameter="atr", metric="sharpe")
    assert result is not None
    assert result.is_plateau is False
    assert result.plateau_width == 1
    assert result.sensitivity is not None
    assert result.sensitivity > 0.5


def test_plateau_analysis_handles_empty_and_nonfinite_input():
    assert plateau_analysis([], parameter="atr", metric="sharpe") is None
    assert (
        plateau_analysis(
            [{"atr": None, "sharpe": 1.0}, {"atr": 2.0, "sharpe": None}],
            parameter="atr",
            metric="sharpe",
        )
        is None
    )
    result = plateau_analysis(
        [{"atr": 2.0, "sharpe": float("nan")}], parameter="atr", metric="sharpe"
    )
    assert result is None, "a non-finite best score cannot be analysed"


def test_plateau_works_with_negative_best_metric():
    trials = [{"atr": 2.0, "sharpe": -1.0}, {"atr": 2.1, "sharpe": -1.005}]
    result = plateau_analysis(trials, parameter="atr", metric="sharpe")
    assert result is not None
    assert result.is_plateau is True


# ─── multiple_testing_correction ─────────────────────────────────────────────


def test_correction_reports_nothing_without_finite_metrics():
    result = multiple_testing_correction([])
    assert result["n_trials"] == 0
    assert result["survives_selection_bias"] is None
    assert result["bonferroni_threshold"] is None


def test_correction_tightens_threshold_with_more_trials():
    # The trials need spread: with every trial identical the standard deviation
    # is zero, the best-of-N cannot be inflated by search, and the expected max
    # collapses to the mean. A zero-variance input cannot exercise "more trials
    # tighten the correction".
    few = multiple_testing_correction([0.1, 0.2, 0.3])
    many = multiple_testing_correction([0.1, 0.2, 0.3] * 60)
    assert many["bonferroni_threshold"] < few["bonferroni_threshold"]
    assert many["significance_threshold"] > few["significance_threshold"]


def test_single_trial_has_no_selection_bias_to_correct():
    result = multiple_testing_correction([1.5])
    assert result["n_trials"] == 1
    assert result["survives_selection_bias"] is None


def test_weak_best_of_many_does_not_survive_selection_bias():
    """A best-of-100 result barely above the null expectation is noise, and the
    correction must say so rather than reporting a confident edge."""
    import random

    rng = random.Random(1234)
    noise = [rng.gauss(0.0, 1.0) for _ in range(100)]
    result = multiple_testing_correction(noise)
    assert result["survives_selection_bias"] is False
    # Compared against the significance bar, not expected_max_under_null: the
    # expected max is the *centre* of the best-of-N distribution, so about half
    # of all pure-noise searches beat it. Measured over 2,000 noise draws the old
    # comparison reported 60.6% of noise as surviving, against 4.3% here.
    assert result["best_trial_metric"] < result["significance_threshold"]


def test_strong_observed_metric_survives_selection_bias():
    noise = [0.0] * 50
    result = multiple_testing_correction(noise, observed_metric=4.0)
    assert result["survives_selection_bias"] is True
    assert result["observed_metric"] == 4.0


def test_correction_ignores_non_numeric_entries():
    result = multiple_testing_correction([1.0, "not-a-number", None, float("inf"), 2.0])
    assert result["n_trials"] == 2
    assert result["best_trial_metric"] == 2.0


# ─── bar duration translation used for reporting the embargo in bars ─────────


def test_bar_duration_covers_both_timeframe_spellings():
    from runner.optimizer import _timeframe_bar_duration

    # normalize_timeframe passes through both families unchanged.
    assert _timeframe_bar_duration("1h") == pd.Timedelta(hours=1)
    assert _timeframe_bar_duration("h1") == pd.Timedelta(hours=1)
    assert _timeframe_bar_duration("15m") == pd.Timedelta(minutes=15)
    assert _timeframe_bar_duration("m15") == pd.Timedelta(minutes=15)
    assert _timeframe_bar_duration("m5") == pd.Timedelta(minutes=5)
    assert _timeframe_bar_duration("1d") == pd.Timedelta(days=1)
    assert _timeframe_bar_duration("d1") == pd.Timedelta(days=1)
    assert _timeframe_bar_duration("1w") == pd.Timedelta(weeks=1)
    assert _timeframe_bar_duration("s30") == pd.Timedelta(seconds=30)
    # Genuinely unresolvable labels report None rather than guessing.
    assert _timeframe_bar_duration("tick") is None
    assert _timeframe_bar_duration("nonsense") is None
    assert _timeframe_bar_duration(None) is None


def test_diagnostics_payload_reports_embargo_in_bars():
    import runner.optimizer as opt

    from types import SimpleNamespace

    config = opt.OptimizationConfig(
        base_backtest_id="bt_x",
        target_metric="sharpe_ratio",
        n_trials=4,
        train_split_pct=0.7,
        embargo_pct=0.05,
        walk_forward_splits=2,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 5}},
    )
    trials = [
        SimpleNamespace(params={"window": w}, value=v)
        for w, v in [(2, 0.1), (3, 0.4), (4, 0.39), (5, 0.05)]
    ]
    payload = opt._build_diagnostics_payload(
        trials=trials,
        config=config,
        train_period={"start": "2024-01-01T00:00:00+00:00", "end": "2024-04-01T00:00:00+00:00"},
        test_period={"start": "2024-04-01T00:00:00+00:00", "end": "2024-07-01T00:00:00+00:00"},
        target_metric="sharpe_ratio",
        timeframe="1h",
    )
    assert payload["split"]["embargo_bars_equivalent"] > 0, (
        "the embargo must be expressible in bars so it is actionable"
    )
    assert len(payload["walk_forward"]) == 2
    assert payload["plateau"]["parameter"] == "window"
    assert payload["multiple_testing"]["n_trials"] == 4


def test_penalty_trials_do_not_poison_the_spread():
    """No-trade trials carry a -1e9 sentinel, not a strategy performance.

    Leaving them in put a -1e9 in the sample, so the mean and standard deviation
    described the sentinel rather than the strategies and every comparison below
    it looked like a real edge.
    """
    penalty = -1e9
    result = multiple_testing_correction(
        [0.10, 0.20, 0.30, penalty, penalty], penalty_floor=penalty
    )
    assert result["n_trials"] == 3, "penalty trials must not count as trials"
    assert result["n_penalty_trials_excluded"] == 2
    # The payload rounds to 6 decimals.
    assert result["trial_metric_mean"] == pytest.approx(0.20, abs=1e-6)
    assert result["trial_metric_std"] == pytest.approx(0.0816497, abs=1e-6)


def test_without_a_penalty_floor_penalties_still_count_as_trials():
    """Callers that do not declare a floor get the old behaviour."""
    result = multiple_testing_correction([0.10, -1e9])
    assert result["n_trials"] == 2
    assert result["n_penalty_trials_excluded"] == 0


def test_expected_max_is_scaled_onto_the_metrics_own_distribution():
    """The bar must be in metric units, not a dimensionless z-score.

    Comparing a raw z-score against a metric value is meaningless: a Sharpe of
    1.5 against a z of 2.5 says nothing about the spread of the trials.
    """
    tight = multiple_testing_correction([1.0, 1.1, 0.9, 1.05, 0.95])
    wide = multiple_testing_correction([-50.0, 50.0, 0.0, 0.0, 0.0])
    # Same number of trials, so the same z; the bars differ only by the spread.
    assert tight["significance_threshold"] > 1.0
    assert wide["significance_threshold"] > 30.0
    assert wide["expected_max_under_null"] != tight["expected_max_under_null"]

"""Tests for optimizer statistical diagnostics."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from runner.optimization_diagnostics import (
    embargo_split,
    multiple_testing_correction,
    plateau_analysis,
    realized_pnl_by_subperiod,
    subperiod_parameter_stability,
    subperiods_from_walk_forward,
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
    assert (test_start - train_end) == pytest.approx(
        pd.Timedelta(days=1), abs=pd.Timedelta(minutes=1)
    )


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
    from types import SimpleNamespace

    import runner.optimizer as opt

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


# ─── diagnostics payload: what is a trial row ─────────────────────────────────


def _payload_for(trials, **config_kwargs):
    import runner.optimizer as opt

    config = opt.OptimizationConfig(
        base_backtest_id="bt_x",
        target_metric="sharpe_ratio",
        n_trials=len(trials),
        train_split_pct=0.7,
        walk_forward_splits=2,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 9}},
        **config_kwargs,
    )
    return opt._build_diagnostics_payload(
        trials=trials,
        config=config,
        train_period={
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-04-01T00:00:00+00:00",
        },
        test_period={
            "start": "2024-04-01T00:00:00+00:00",
            "end": "2024-07-01T00:00:00+00:00",
        },
        target_metric="sharpe_ratio",
        timeframe="1h",
    )


def _trial(params, value, *, penalty=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        params=params, value=value, user_attrs={"penalty_applied": penalty}
    )


def test_no_trade_trials_are_excluded_from_the_plateau():
    """A no-trade trial is a sentinel, not a strategy performance.

    The `penalty_floor` threaded into `multiple_testing_correction` does NOT
    keep these rows out: the sentinel is *dynamic*
    (`_no_trade_penalty_value` returns `min(values) - 1.0` once any trading
    trial has completed), so the value-level filter only ever caught the very
    first trial's -1e9. Every later sentinel reached the plateau, where it
    inflated `total_evaluated` and — because `neighbours` is ordered by
    distance from the best parameter value — a sentinel at the *same*
    parameter value became the nearest other neighbour, so `sensitivity`
    reported a ratio against a penalty.
    """
    trials = [
        _trial({"window": 2}, 0.10),
        _trial({"window": 3}, 0.40),
        _trial({"window": 4}, 0.39),
        _trial({"window": 5}, 0.05),
        # Dynamic sentinel: min(observed trading values) - 1.0 == -0.95.
        _trial({"window": 3}, -0.95, penalty=True),
        _trial({"window": 4}, -0.95, penalty=True),
    ]

    payload = _payload_for(trials)

    plateau = payload["plateau"]
    assert plateau["total_evaluated"] == 4, "a sentinel is not an evaluated trial"
    assert all(
        neighbour["metric"] > -1.0 for neighbour in plateau["neighbours"]
    ), "no penalty value may appear in the plateau's neighbour list"
    # `sensitivity` is the ratio of the nearest OTHER parameter value's delta to
    # the best. Against the -0.95 sentinel at window=3 the ratio was ~0.95/0.4;
    # against the real window=2 neighbour it is 0.3/0.4.
    assert plateau["sensitivity"] == pytest.approx(0.3 / 0.4, rel=1e-6)
    assert plateau["parameter"] == "window"
    assert payload["n_no_trade_trials_excluded"] == 2
    # The same rows must not reach the selection-bias statistics either.
    multiple_testing = payload["multiple_testing"]
    assert multiple_testing["n_trials"] == 4
    assert multiple_testing["n_penalty_trials_excluded"] == 2
    assert multiple_testing["trial_metric_mean"] == pytest.approx(0.235, abs=1e-6)
    assert multiple_testing["survives_selection_bias"] is not None


def test_internal_params_are_never_the_plateau_axis():
    """`__internal_symbol_universe` is plumbing, not a strategy parameter.

    `varied` counted every key of every trial row, and the plateau axis is the
    parameter with the most distinct values. A study that enumerated six symbol
    universes across only two window values therefore reported its "plateau"
    along `__internal_symbol_universe` and published `best_value: 5` — an
    internal index, meaningless to whoever reads the optimization report.

    The two domains are deliberately disjoint here (windows 20/30, universe
    indices 0..5) so the reported `best_value` cannot accidentally look like a
    window.
    """
    trials = [
        _trial(
            {"window": 20 + 10 * (index % 2), "__internal_symbol_universe": index},
            0.1 * (index + 1),
        )
        for index in range(6)
    ]

    payload = _payload_for(trials)

    assert payload["plateau"]["parameter"] == "window"
    assert payload["plateau"]["best_value"] in {20, 30}
    assert payload["plateau"]["total_evaluated"] == 6
    assert {neighbour["value"] for neighbour in payload["plateau"]["neighbours"]} <= {
        20,
        30,
    }


def test_a_row_of_only_internal_params_is_not_a_trial():
    """Nothing but bookkeeping params means nothing to analyse."""
    trials = [
        _trial({"window": 2}, 0.10),
        _trial({"window": 3}, 0.40),
        _trial({"__internal_symbol_universe": 0}, 0.30),
    ]

    payload = _payload_for(trials)

    assert payload["plateau"]["total_evaluated"] == 2
    assert payload["multiple_testing"]["n_trials"] == 2


def test_all_no_trade_trials_leave_the_statistics_empty_not_wrong():
    """An all-sentinel study reports nothing rather than describing sentinels."""
    trials = [_trial({"window": 2 + index}, -0.95, penalty=True) for index in range(3)]

    payload = _payload_for(trials)

    assert payload["plateau"] is None
    assert payload["multiple_testing"]["n_trials"] == 0
    assert payload["multiple_testing"]["survives_selection_bias"] is None
    assert payload["n_no_trade_trials_excluded"] == 3


# ─── per-sub-period parameter stability ──────────────────────────────────────


def _observation(period: str, value: int, metric: float) -> dict:
    return {"period": period, "params": {"window": value}, "metric": metric}


def test_subperiods_are_the_walk_forward_test_windows():
    """The stability view reuses the folds already reported next to it, so the
    two can never describe different slices of the window."""
    folds = walk_forward_splits(
        "2024-01-01T00:00:00+00:00",
        "2024-04-01T00:00:00+00:00",
        n_splits=3,
        train_fraction=0.7,
    )

    subperiods = subperiods_from_walk_forward(folds)

    assert [subperiod["label"] for subperiod in subperiods] == ["fold_0", "fold_1", "fold_2"]
    for subperiod, fold in zip(subperiods, folds):
        assert subperiod["start"] == fold["test"]["start"]
        assert subperiod["end"] == fold["test"]["end"]
        assert subperiod["fold"] == fold["fold"]


def test_subperiods_reject_a_fold_without_a_test_window():
    """Silently dropping a fold would shorten the measured coverage while still
    reporting a confident result."""
    with pytest.raises(ValueError, match="no test window"):
        subperiods_from_walk_forward([{"fold": 0}])
    with pytest.raises(ValueError, match="not a mapping"):
        subperiods_from_walk_forward(["fold_0"])


def test_realized_pnl_is_attributed_to_the_subperiod_that_produced_it():
    """A trade opened in one sub-period and closed in the next belongs to the
    second: the result is what the period actually produced."""
    subperiods = [
        {"label": "fold_0", "start": "2024-01-01T00:00:00+00:00", "end": "2024-02-01T00:00:00+00:00"},
        {"label": "fold_1", "start": "2024-02-01T00:00:00+00:00", "end": "2024-03-01T00:00:00+00:00"},
    ]

    realized = realized_pnl_by_subperiod(
        [
            ("2024-01-05T00:00:00+00:00", 100.0),   # in fold_0
            ("2024-02-10T00:00:00+00:00", -40.0),  # in fold_1
            ("2024-01-06T00:00:00+00:00", 5.0),    # also in fold_0
            ("2024-06-01T00:00:00+00:00", 999.0),  # outside every sub-period
            ("not-a-date", 777.0),                 # unreadable
            ("2024-02-10T00:00:00+00:00", float("nan")),
        ],
        subperiods,
    )

    assert realized == {"fold_0": 105.0, "fold_1": -40.0}


def test_realized_pnl_reports_every_subperiod_even_with_no_events():
    """"Nothing was realized here" and "this sub-period was never measured"
    must not look the same to a reader."""
    subperiods = [
        {"label": "fold_0", "start": "2024-01-01T00:00:00+00:00", "end": "2024-02-01T00:00:00+00:00"},
        {"label": "fold_1", "start": "2024-02-01T00:00:00+00:00", "end": "2024-03-01T00:00:00+00:00"},
    ]

    assert realized_pnl_by_subperiod([], subperiods) == {"fold_0": 0.0, "fold_1": 0.0}


def test_realized_pnl_is_never_counted_twice_when_subperiods_overlap():
    """Overlapping windows are a caller's choice, and one result cannot inflate
    two periods: it is attributed to the first period that contains it."""
    subperiods = [
        {"label": "wide", "start": "2024-01-01T00:00:00+00:00", "end": "2024-03-01T00:00:00+00:00"},
        {"label": "narrow", "start": "2024-02-01T00:00:00+00:00", "end": "2024-02-15T00:00:00+00:00"},
    ]

    realized = realized_pnl_by_subperiod(
        [("2024-02-05T00:00:00+00:00", 100.0), ("2024-01-10T00:00:00+00:00", 7.0)],
        subperiods,
    )

    # The 100.0 event falls inside both windows and is charged once, to the
    # first; the 7.0 event belongs to "wide" alone.
    assert realized == {"wide": 107.0, "narrow": 0.0}


def test_the_winning_parameter_that_also_wins_every_subperiod_is_stable():
    observations = [
        _observation("fold_0", 3, 100.0),
        _observation("fold_0", 2, 50.0),
        _observation("fold_1", 3, 80.0),
        _observation("fold_1", 2, 10.0),
        _observation("fold_2", 3, 60.0),
        _observation("fold_2", 2, 20.0),
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=3, metric_name="realized_net_pnl"
    )

    assert result["status"] == "ok"
    assert result["selected_value"] == 3
    assert result["selected_wins_n_periods"] == 3
    assert result["selected_win_rate"] == pytest.approx(1.0)
    assert result["selected_mean_rank"] == pytest.approx(1.0)
    assert result["distinct_period_winners"] == 1
    assert result["winner_agreement"] == "stable"
    assert [row["period"] for row in result["per_period"]] == ["fold_0", "fold_1", "fold_2"]


def test_a_winner_that_only_wins_the_first_half_is_reported_unstable():
    """The case a current-membership universe produces: the parameters that
    win early do not win later, because the membership behind them changed."""
    observations = [
        _observation("fold_0", 3, 100.0),
        _observation("fold_0", 2, 50.0),
        _observation("fold_1", 3, 10.0),
        _observation("fold_1", 2, 80.0),
        _observation("fold_2", 3, 5.0),
        _observation("fold_2", 2, 90.0),
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=3, metric_name="realized_net_pnl"
    )

    assert result["winner_agreement"] == "unstable"
    assert result["selected_wins_n_periods"] == 1
    assert result["selected_win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert result["selected_worst_rank"] == 2
    assert result["distinct_period_winners"] == 2
    by_value = {row["value"]: row for row in result["per_parameter_value"]}
    assert by_value[3]["best_in_n_periods"] == 1
    assert by_value[2]["best_in_n_periods"] == 2


def test_subperiod_ranking_uses_the_mean_so_sampling_count_cannot_win():
    """A value sampled four times must not beat one sampled once on the mean,
    and the per-value detail must expose the imbalance."""
    observations = [
        _observation("fold_0", 3, 10.0),
        _observation("fold_0", 3, 10.0),
        _observation("fold_0", 3, 10.0),
        _observation("fold_0", 3, 10.0),
        _observation("fold_0", 2, 4.0),
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=3, metric_name="realized_net_pnl"
    )

    assert result["per_period"][0]["best_metric"] == pytest.approx(10.0)
    assert result["per_period"][0]["best_value"] == 3
    by_value = {row["value"]: row for row in result["per_parameter_value"]}
    assert by_value[3]["periods_observed"] == 1
    assert by_value[3]["mean_metric"] == pytest.approx(10.0)


def test_stability_reports_unknown_when_a_subperiod_never_sampled_the_winner():
    """A rank for a sub-period in which the selected value never ran would be
    invented, so the sub-period is reported as unobserved and the win rate is
    computed over the sub-periods that can speak."""
    observations = [
        _observation("fold_0", 3, 100.0),
        _observation("fold_0", 2, 50.0),
        _observation("fold_1", 2, 80.0),
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=3, metric_name="realized_net_pnl"
    )

    assert result["n_periods"] == 2
    assert result["n_periods_selected_observed"] == 1
    unobserved = [row for row in result["per_period"] if not row["selected_observed"]]
    assert len(unobserved) == 1
    assert unobserved[0]["selected_metric"] is None
    assert unobserved[0]["selected_rank"] is None
    assert result["selected_win_rate"] == pytest.approx(1.0)
    assert "selected_value_rank_in_every_subperiod" in result["undefined_metrics"]


def test_stability_without_observations_is_unknown_not_a_pass():
    result = subperiod_parameter_stability(
        [], parameter="window", metric_name="realized_net_pnl"
    )

    assert result["status"] == "unknown"
    assert result["selected_win_rate"] is None
    assert result["selected_wins_n_periods"] is None
    assert result["winner_agreement"] == "unknown"
    assert result["undefined_metrics"] == ["subperiod_parameter_stability"]


def test_stability_ignores_unreadable_observations_rather_than_guessing():
    observations = [
        _observation("fold_0", 3, 100.0),
        _observation("fold_0", 2, float("nan")),
        {"period": "fold_1", "params": {}, "metric": 1.0},          # no axis
        {"period": "fold_1", "metric": 1.0},                        # no params
        {"period": "", "params": {"window": 2}, "metric": 1.0},     # no period
        {"period": "fold_1", "params": {"window": None}, "metric": 1.0},
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=3, metric_name="realized_net_pnl"
    )

    assert result["n_parameter_values"] == 1
    assert [row["period"] for row in result["per_period"]] == ["fold_0"]


def test_stability_falls_back_to_the_pooled_best_value_when_none_is_selected():
    observations = [
        _observation("fold_0", 2, 90.0),
        _observation("fold_0", 3, 10.0),
        _observation("fold_1", 2, 90.0),
        _observation("fold_1", 3, 10.0),
    ]

    result = subperiod_parameter_stability(
        observations, parameter="window", metric_name="realized_net_pnl"
    )

    assert result["selected_value"] == 2
    assert result["selected_value_source"] == "best_mean_across_subperiods"


def test_stability_takes_the_selected_axis_value_from_the_chosen_parameters():
    observations = [
        _observation("fold_0", 3, 10.0),
        _observation("fold_0", 2, 90.0),
        _observation("fold_1", 3, 10.0),
        _observation("fold_1", 2, 90.0),
    ]

    result = subperiod_parameter_stability(
        observations,
        parameter="window",
        selected_parameters={"window": 3, "other": 7},
        metric_name="realized_net_pnl",
    )

    assert result["selected_value"] == 3
    assert result["selected_value_source"] == "declared"
    assert result["winner_agreement"] == "unstable"


def test_stability_is_unknown_when_the_selected_value_never_ran():
    observations = [_observation("fold_0", 2, 5.0)]

    result = subperiod_parameter_stability(
        observations, parameter="window", selected_value=9, metric_name="realized_net_pnl"
    )

    assert result["status"] == "unknown"
    assert "never observed" in result["reason"]


def test_stability_supports_a_minimized_metric():
    observations = [
        _observation("fold_0", 3, 0.01),
        _observation("fold_0", 2, 0.40),
        _observation("fold_1", 3, 0.02),
        _observation("fold_1", 2, 0.30),
    ]

    result = subperiod_parameter_stability(
        observations,
        parameter="window",
        direction="minimize",
        selected_value=3,
        metric_name="p_value",
    )

    assert result["per_period"][0]["best_value"] == 3
    assert result["winner_agreement"] == "stable"

    with pytest.raises(ValueError, match="maximize"):
        subperiod_parameter_stability(
            observations, parameter="window", direction="sideways"
        )


# ─── trial → sub-period adapters ────────────────────────────────────────────


def _trade(**kwargs):
    from shared.models import Trade, TradeExit

    exits = [
        TradeExit(time=when, price=100.0, quantity=1.0, pnl=1.0)
        for when in kwargs.pop("exit_times", ["2024-01-05T00:00:00+00:00"])
    ]
    return Trade(
        id=1,
        symbol="AAPL",
        direction="LONG",
        entry_time="2024-01-01T00:00:00+00:00",
        entry_price=100.0,
        quantity=1.0,
        exits=exits,
        pnl=kwargs.pop("pnl", 10.0),
        **kwargs,
    )


def test_a_result_is_attributed_to_its_last_exit():
    """Partial exits: the result the sub-period produced is the one the final
    exit realized, not the first one."""
    from runner.optimizer import _trade_realization_time

    assert _trade_realization_time(
        _trade(exit_times=["2024-01-05T00:00:00+00:00", "2024-02-20T00:00:00+00:00"])
    ) == "2024-02-20T00:00:00+00:00"


def test_a_trade_without_an_exit_falls_back_to_its_entry():
    from runner.optimizer import _trade_realization_time

    assert _trade_realization_time(_trade(exit_times=[])) == "2024-01-01T00:00:00+00:00"
    assert _trade_realization_time(_trade(exit_times=[""])) == "2024-01-01T00:00:00+00:00"


def test_net_pnl_is_after_fees_and_an_unreadable_one_is_never_a_flat_zero():
    """Fees are part of the realized result, and an unreadable PnL must not be
    charged to a sub-period as a real (zero) performance."""
    from runner.optimizer import _trade_net_pnl

    assert _trade_net_pnl(_trade(pnl=10.0, net_pnl=7.0, fees=3.0)) == pytest.approx(7.0)
    assert _trade_net_pnl(_trade(pnl=10.0, fees=3.0)) == pytest.approx(7.0)
    assert _trade_net_pnl(_trade(pnl=10.0)) == pytest.approx(10.0)
    assert math.isnan(_trade_net_pnl(_trade(pnl="not-a-number")))
    assert math.isnan(_trade_net_pnl(_trade(pnl=10.0, net_pnl="not-a-number")))


def test_an_unreadable_pnl_is_dropped_rather_than_charged_to_a_subperiod():
    from runner.optimizer import _trade_net_pnl

    subperiods = [
        {"label": "fold_0", "start": "2024-01-01T00:00:00+00:00", "end": "2024-03-01T00:00:00+00:00"}
    ]

    realized = realized_pnl_by_subperiod(
        [("2024-01-05T00:00:00+00:00", _trade_net_pnl(_trade(pnl="not-a-number")))],
        subperiods,
    )

    # The sub-period is still reported (measured, not skipped) and carries no
    # invented result.
    assert realized == {"fold_0": 0.0}

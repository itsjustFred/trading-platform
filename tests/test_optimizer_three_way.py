"""Issue #164 stage 2: the three-way split wired into the optimizer run.

The search runs on train, every completed trial is judged on validation, the
winner is fixed from validation rank, and only then is the test holdout read.
These tests exercise that wiring end to end with a fake backtest runner.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import optuna
import pytest

import runner.optimizer as optimizer_module
from runner.optimizer import OptimizationConfig
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

TRAIN_START = "2024-01-01"
BACKTEST_END = "2024-12-31"


def _config(**overrides) -> OptimizationConfig:
    values = {
        "base_backtest_id": "base-demo",
        "target_metric": "sharpe",
        "n_trials": 2,
        "train_split_pct": 0.6,
        "validation_split_pct": 0.2,
        "test_split_pct": 0.2,
        "parameter_ranges": {"window": {"type": "int", "min": 1, "max": 2, "step": 1}},
    }
    values.update(overrides)
    return OptimizationConfig(**values)


def _write_base_backtest(backtests_dir: Path) -> None:
    result = BacktestResult(
        meta=BacktestMeta(
            id="base-demo",
            created_at="2026-03-11T09:00:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start=TRAIN_START,
            backtest_end=BACKTEST_END,
            initial_capital=100000.0,
            risk_per_trade_pct=0.01,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test"},
            parameters={"fixed": 3},
        ),
        trades=[],
        equity_curve=[],
    )
    backtests_dir.mkdir(parents=True, exist_ok=True)
    result.save(backtests_dir / "base-demo.trades.json")


def _equity_curve(start: str) -> list[EquityPoint]:
    values = [100_000.0, 101_000.0, 100_500.0, 102_000.0, 101_500.0, 103_000.0]
    day = 1 if start.startswith("2024-01") else 10
    return [
        EquityPoint(time=f"2024-02-{day + index:02d}T00:00:00+00:00", equity=value)
        for index, value in enumerate(values)
    ]


def _trial_result(
    config,
    *,
    metric: float,
    trade_count: int = 1,
    with_equity: bool = True,
) -> BacktestResult:
    trades = []
    if trade_count > 0:
        trades = [
            Trade(
                id=1,
                symbol=config.symbols[0],
                direction="LONG",
                entry_time=config.start_date,
                entry_price=100.0,
                quantity=1.0,
                exits=[
                    TradeExit(
                        time=config.end_date,
                        price=100.0 + metric,
                        quantity=1.0,
                        pnl=round(metric * 1000, 2),
                    )
                ],
                pnl=round(metric * 1000, 2),
            )
        ]
    stats = {
        "sharpe_ratio": metric,
        "sortino_ratio": metric,
        "calmar_ratio": metric,
        "total_pnl": round(metric * 1000, 2),
        "cagr": metric / 10.0,
        "win_rate": 1.0 if trade_count else 0.0,
        "profit_factor": 2.0 if trade_count else 0.0,
        "psr": 0.7 if trade_count else None,
        "p_value": metric,
        "total_return": metric / 100.0,
        "max_drawdown_pct": -0.1 if trade_count else 0.0,
    }
    return BacktestResult(
        meta=BacktestMeta(
            id="trial-demo",
            created_at="2026-03-11T09:00:00+00:00",
            engine="vectorbt",
            strategies=[config.strategy_name],
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            initial_capital=config.initial_capital,
            data={"symbols": config.symbols, "timeframes": [config.timeframe], "source": "test"},
            parameters=config.parameters,
        ),
        trades=trades,
        equity_curve=_equity_curve(config.start_date) if with_equity else [],
        stats=stats,
    )


def _three_way_periods(config: OptimizationConfig):
    split = optimizer_module._split_backtest_period(
        TRAIN_START,
        BACKTEST_END,
        train_split_pct=config.train_split_pct,
        validation_split_pct=config.validation_split_pct,
        test_split_pct=config.test_split_pct,
        embargo_days=config.embargo_days,
        embargo_pct=config.embargo_pct,
    )
    return split, {
        "train": split.train,
        "validation": split.validation,
        "test": split.test,
    }


def _patch_environment(
    monkeypatch,
    tmp_path: Path,
    *,
    config: OptimizationConfig,
    metrics: dict[str, dict[int, float]],
    trade_counts: dict[tuple[str, int], int] | None = None,
    with_equity: bool = True,
) -> dict[str, object]:
    """Fake backtest environment; returns the recorded runs and the split."""
    backtests_dir = tmp_path / "backtests"
    _write_base_backtest(backtests_dir)
    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(
        optimizer_module, "has_sufficient_segment_coverage", lambda **kwargs: True
    )
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    monkeypatch.setattr(
        optimizer_module,
        "compute_stats_for_result",
        lambda result: dict(result.stats),
    )
    real_create_study = optuna.create_study
    choices = sorted({window for segment in metrics.values() for window in segment})
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        lambda **kwargs: real_create_study(
            **kwargs, sampler=optuna.samplers.GridSampler({"window": choices})
        ),
    )

    split, periods = _three_way_periods(config)
    calls: list[dict[str, object]] = []

    def fake_run_backtest(backtest_config, strategy_path):
        segment = next(
            (
                name
                for name, period in periods.items()
                if period["start"] == backtest_config.start_date
                and period["end"] == backtest_config.end_date
            ),
            None,
        )
        if segment is None:
            raise AssertionError(
                "backtest ran on a period outside the three-way split: "
                f"{backtest_config.start_date} -> {backtest_config.end_date}"
            )
        window = int(backtest_config.parameters["window"])
        calls.append(
            {
                "segment": segment,
                "start": backtest_config.start_date,
                "end": backtest_config.end_date,
                "window": window,
            }
        )
        trade_count = (trade_counts or {}).get((segment, window), 1)
        return _trial_result(
            backtest_config,
            metric=metrics[segment][window],
            trade_count=trade_count,
            with_equity=with_equity,
        )

    monkeypatch.setattr(optimizer_module, "_run_vectorbt_backtest", fake_run_backtest)
    return {"calls": calls, "split": split}


def test_search_runs_only_train_and_test_is_read_after_selection(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    environment = _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 2.0, 2: 1.0},
            "validation": {1: 1.0, 2: 2.0},
            "test": {1: 3.0, 2: 0.5},
        },
    )
    calls = environment["calls"]
    split = environment["split"]
    assert isinstance(calls, list) and isinstance(split, optimizer_module.ThreeWaySplit)

    search_phase_runs: list[dict[str, object]] = []
    runs_when_search_ended: list[dict[str, object]] = []
    real_evaluate_on_test = optimizer_module._evaluate_top_set_on_test

    def spy_evaluate_on_test(**kwargs):
        # Snapshot what had run at the moment the test holdout is first opened.
        search_phase_runs.extend(dict(call) for call in calls)
        return real_evaluate_on_test(**kwargs)

    monkeypatch.setattr(optimizer_module, "_evaluate_top_set_on_test", spy_evaluate_on_test)

    real_create_study = optimizer_module.optuna.create_study

    def spy_create_study(**kwargs):
        study = real_create_study(**kwargs)
        real_optimize = study.optimize

        def spy_optimize(objective, **optimize_kwargs):
            result = real_optimize(objective, **optimize_kwargs)
            # The search is over here: nothing but train may have run.
            runs_when_search_ended.extend(dict(call) for call in calls)
            return result

        study.optimize = spy_optimize
        return study

    monkeypatch.setattr(optimizer_module.optuna, "create_study", spy_create_study)

    optimizer_module.run_optimization(config)

    # During the search only TRAIN runs happened.
    train_runs = [call for call in calls if call["segment"] == "train"]
    assert len(train_runs) == config.n_trials
    assert runs_when_search_ended, "the study never ran"
    assert {call["segment"] for call in runs_when_search_ended} == {"train"}
    # By the time the test holdout is opened, only train and validation ran.
    assert search_phase_runs, "test evaluation was never reached"
    assert {call["segment"] for call in search_phase_runs} == {"train", "validation"}
    assert all(call["segment"] != "test" for call in search_phase_runs)
    # The periods used are exactly the three-way split's segments.
    periods = {
        "train": (split.train["start"], split.train["end"]),
        "validation": (split.validation["start"], split.validation["end"]),
        "test": (split.test["start"], split.test["end"]),
    }
    assert {(call["start"], call["end"]) for call in calls} == set(periods.values())


def test_objective_cannot_see_the_validation_or_test_period(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    environment = _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 2.0, 2: 1.0},
            "validation": {1: 1.0, 2: 2.0},
            "test": {1: 3.0, 2: 0.5},
        },
    )
    split = environment["split"]
    assert isinstance(split, optimizer_module.ThreeWaySplit)
    captured: dict[str, object] = {}

    real_create_study = optimizer_module.optuna.create_study

    def spy_create_study(**kwargs):
        study = real_create_study(**kwargs)
        real_optimize = study.optimize

        def spy_optimize(objective, **optimize_kwargs):
            captured["objective"] = objective
            return real_optimize(objective, **optimize_kwargs)

        study.optimize = spy_optimize
        return study

    monkeypatch.setattr(optimizer_module.optuna, "create_study", spy_create_study)
    optimizer_module.run_optimization(config)

    objective = captured["objective"]
    closure_values = inspect.getclosurevars(objective).nonlocals.values()
    held_out_periods = [split.validation, split.test]
    for value in closure_values:
        assert value not in held_out_periods, (
            "the Optuna objective closed over a period it must not see: "
            f"{value!r}"
        )
    # And it did run: the closure carries the train window, not only scalars.
    assert split.train in list(closure_values)


def test_winner_is_the_validation_best_trial_even_when_test_disagrees(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 3.0, 2: 2.0},
            "validation": {1: 0.5, 2: 2.5},
            "test": {1: 99.0, 2: 1.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    assert payload["best_params"] == {"window": 2}
    assert payload["three_way"]["selection"]["winner_trial_number"] == (
        payload["three_way"]["selection"]["ranked_top_set"][0]["trial_number"]
    )
    assert payload["three_way"]["winner"]["validation_metrics"]["sharpe_ratio"] == 2.5
    # The headline test number is the WINNER's, even though the other trial
    # was far better on the holdout.
    assert payload["final_test_stats"]["sharpe_ratio"] == 1.0
    distribution = payload["three_way"]["test_distribution"]
    assert distribution["count"] == 1
    assert distribution["ranked_by_test"][0]["test_metric_value"] == 1.0


def test_top_set_size_rounds_up_and_never_test_more_than_selected(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(
        n_trials=4,
        test_top_fraction=0.5,
        parameter_ranges={"window": {"type": "int", "min": 1, "max": 4}},
    )
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0},
            "validation": {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0},
            "test": {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    assert payload["three_way"]["selection"]["top_set_size"] == 2
    assert payload["three_way"]["test_distribution"]["count"] == 2
    # Two candidates are too few for a rank correlation.
    assert payload["three_way"]["test_distribution"]["spearman_validation_to_test"] is None


def test_single_trial_still_tests_one_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(
        n_trials=1,
        parameter_ranges={"window": {"type": "int", "min": 1, "max": 1}},
    )
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={"train": {1: 1.0}, "validation": {1: 1.0}, "test": {1: 1.0}},
    )

    payload = optimizer_module.run_optimization(config)

    assert payload["three_way"]["selection"]["top_set_size"] == 1
    assert payload["three_way"]["test_distribution"]["count"] == 1
    assert payload["three_way"]["test_distribution"]["spearman_validation_to_test"] is None


def test_equal_validation_scores_break_by_lower_trial_number(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2, test_top_fraction=1.0)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 1.0, 2: 2.0},
            "validation": {1: 1.5, 2: 1.5},
            "test": {1: 1.0, 2: 2.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    selection = payload["three_way"]["selection"]
    trial_numbers = [row["trial_number"] for row in selection["ranked_top_set"]]
    assert selection["winner_trial_number"] == min(trial_numbers)
    assert "lower trial number" in selection["tie_break"]


def test_minimize_study_selects_the_lowest_validation_p_value(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(target_metric="p_value", n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 0.01, 2: 0.50},
            "validation": {1: 0.40, 2: 0.02},
            "test": {1: 0.30, 2: 0.90},
        },
    )

    payload = optimizer_module.run_optimization(config)

    assert payload["best_params"] == {"window": 2}
    assert payload["three_way"]["selection"]["study_direction"] == "minimize"
    assert payload["three_way"]["winner"]["validation_metrics"]["p_value"] == 0.02
    # The test holdout must not change that the lower validation p-value wins.
    assert payload["final_test_stats"]["p_value"] == 0.90


def test_no_trade_trial_is_not_selectable_even_with_the_best_validation(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 5.0, 2: 1.0},
            "validation": {1: 9.0, 2: 0.5},
            "test": {1: 9.0, 2: 0.5},
        },
        # Window 1 never trades on train: it is a penalty trial and must never
        # be selectable, regardless of what it does on validation.
        trade_counts={("train", 1): 0},
    )

    payload = optimizer_module.run_optimization(config)

    assert payload["best_params"] == {"window": 2}
    assert payload["three_way"]["selection"]["n_unselectable_trials"] == 1


def test_walk_forward_folds_stop_before_the_test_holdout_embargo() -> None:
    import pandas as pd

    config = _config()
    split, _ = _three_way_periods(config)

    folds = optimizer_module.walk_forward_train_validation_folds(
        split.train["start"],
        split.test["end"],
        n_splits=config.walk_forward_splits,
        train_fraction=config.train_split_pct,
        embargo_pct=config.embargo_pct,
        test_fraction=config.test_split_pct,
        embargo_days=config.embargo_days,
    )

    assert folds
    last_fold_end = pd.Timestamp(folds[-1]["test"]["end"])
    assert last_fold_end <= pd.Timestamp(split.validation["end"])
    assert last_fold_end < pd.Timestamp(split.test["start"])


def test_payload_keeps_legacy_held_out_keys_on_validation(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    environment = _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 1.0, 2: 2.0},
            "validation": {1: 3.0, 2: 4.0},
            "test": {1: 5.0, 2: 6.0},
        },
    )
    split = environment["split"]
    assert isinstance(split, optimizer_module.ThreeWaySplit)

    payload = optimizer_module.run_optimization(config)

    # Old "held-out result used to judge the trial" keys now carry validation.
    assert payload["test_period"] == split.validation
    assert payload["test_stats"]["sharpe_ratio"] == 4.0
    assert payload["validation_period"] == split.validation
    # The untouched test holdout has its own keys.
    assert payload["final_test_period"] == split.test
    assert payload["final_test_stats"]["sharpe_ratio"] == 6.0
    assert payload["three_way"]["segments"]["test"] == split.test
    # Existing keys the viewer reads stay populated.
    assert payload["train_split_pct"] == 0.6
    assert payload["train_period"] == split.train
    assert payload["oos_comparison"]["in_sample_value"] == 2.0
    assert payload["oos_status"] == "available"


def test_saved_optimization_round_trips_the_three_way_section(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 1.0, 2: 2.0},
            "validation": {1: 3.0, 2: 4.0},
            "test": {1: 5.0, 2: 6.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    (saved,) = (tmp_path / "optimizations").glob("opt_base-demo_*.json")
    saved_payload = json.loads(saved.read_text(encoding="utf-8"))
    assert saved_payload["three_way"]["selection"]["winner_trial_number"] == (
        payload["three_way"]["selection"]["winner_trial_number"]
    )
    assert saved_payload["three_way"]["segments"] == payload["three_way"]["segments"]


def test_verdict_reports_degradation_and_deflated_sharpe(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 4.0, 2: 2.0},
            "validation": {1: 1.0, 2: 3.0},
            "test": {1: 1.0, 2: 3.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    overfitting = payload["three_way"]["overfitting"]
    assert payload["best_params"] == {"window": 2}
    # Winner: train 2.0 -> validation 3.0 => (2 - 3) / |2| = -0.5.
    assert overfitting["degradation"] == pytest.approx(-0.5)
    assert overfitting["deflated_sharpe"] is not None
    assert 0.0 <= overfitting["deflated_sharpe"] <= 1.0
    assert overfitting["n_selectable_trials"] == 2
    assert overfitting["var_sharpe_per_obs"] is not None
    assert overfitting["thresholds"] == {
        "max_train_validation_degradation": 0.5,
        "min_deflated_sharpe": 0.95,
    }
    assert "checks" in overfitting and "reasons" in overfitting


def test_failing_overfitting_verdict_still_returns_the_full_result(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2, max_train_validation_degradation=0.1)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 4.0, 2: 2.0},
            "validation": {1: 1.0, 2: 1.5},
            "test": {1: 1.0, 2: 2.0},
        },
    )

    payload = optimizer_module.run_optimization(config)

    overfitting = payload["three_way"]["overfitting"]
    # 0.25 degradation against a 0.1 threshold: flagged, not blocked.
    assert overfitting["degradation"] == pytest.approx(0.25)
    assert overfitting["passed"] is False
    assert any("degradation" in reason for reason in overfitting["reasons"])
    # The result is complete: winner, per-segment metrics and test report.
    assert payload["best_params"] == {"window": 2}
    assert payload["three_way"]["winner"]["trial_number"] == (
        payload["three_way"]["selection"]["winner_trial_number"]
    )
    assert payload["final_test_stats"]["sharpe_ratio"] == 2.0
    assert payload["three_way"]["test_distribution"]["count"] == 1


def test_minimize_degradation_is_positive_when_validation_is_worse(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(target_metric="p_value", n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 0.02, 2: 0.90},
            "validation": {1: 0.50, 2: 0.95},
            "test": {1: 0.50, 2: 0.95},
        },
    )

    payload = optimizer_module.run_optimization(config)

    overfitting = payload["three_way"]["overfitting"]
    assert payload["best_params"] == {"window": 1}
    # p_value rose from 0.02 to 0.50 on validation, which is worse; the sign
    # convention makes "worse on validation" positive: (-0.02 + 0.50)/0.02.
    assert overfitting["degradation"] == pytest.approx(24.0)


def test_uncomputable_deflated_sharpe_flags_but_keeps_the_result(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(n_trials=2)
    _patch_environment(
        monkeypatch,
        tmp_path,
        config=config,
        metrics={
            "train": {1: 2.0, 2: 1.0},
            "validation": {1: 1.0, 2: 2.0},
            "test": {1: 1.0, 2: 2.0},
        },
        with_equity=False,
    )

    payload = optimizer_module.run_optimization(config)

    overfitting = payload["three_way"]["overfitting"]
    assert overfitting["deflated_sharpe"] is None
    assert overfitting["passed"] is False
    assert any("deflated Sharpe" in reason for reason in overfitting["reasons"])
    # A verdict that cannot be computed does not withhold the result.
    assert payload["best_params"] == {"window": 2}
    assert payload["final_test_stats"]

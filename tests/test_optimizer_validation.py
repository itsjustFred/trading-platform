"""Optimizer/walk-forward validation regressions (Cleanup 16-D1).

Covers:

- F13/F31-adjacent AC — the optimizer must not select a no-trade trial:
  no-trade trials must score strictly below every trading trial (explicit
  validation marker instead of the previous ``0.0`` that beat honest
  negative-Sharpe trials under ``direction="maximize"``).
- F30 — risk-parity covariance must derive asset returns from MARKET DATA
  (asset co-movement), not from per-symbol trade-PnL pseudo-equity.
"""

from __future__ import annotations

import math
from pathlib import Path

import optuna
import pandas as pd
import pytest

from runner import optimizer as optimizer_module
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.optimizer import OptimizationConfig, run_optimization


# ─── F30: covariance from market data, not trade-PnL pseudo-equity ──────────


class TestRiskParityCovarianceSource:
    def test_market_data_path_preferred_over_trade_pseudo_equity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Asset-price returns must win even when trade-based columns exist.

        Regression (F30): the trade-PnL pseudo-equity frame used to take
        precedence, so with >= 2 traded symbols the market-data path never
        ran — correlations measured strategy-trade overlap, not asset
        co-movement, and silently drove covariance weights + universe
        selection.
        """
        recorded: dict[str, pd.DataFrame] = {}

        def fake_selection(returns_frame, candidate_universes=None):
            recorded["returns"] = returns_frame
            return {
                "best_symbols": list(returns_frame.columns),
                "best_weights": {name: 1.0 / len(returns_frame.columns) for name in returns_frame.columns},
                "best_score": 1.0,
                "best_sharpe": 1.0,
                "best_volatility": 0.1,
                "best_diversification_ratio": 1.0,
                "best_avg_abs_correlation": 0.0,
            }

        monkeypatch.setattr(
            optimizer_module, "select_best_risk_parity_universe", fake_selection
        )
        monkeypatch.setattr(
            optimizer_module, "_asset_returns_frame_from_base_result",
            lambda result: pd.DataFrame({"PSEUDO": [0.01, -0.02], "PSEUDO2": [0.0, 0.03]}),
        )
        monkeypatch.setattr(
            optimizer_module,
            "_asset_returns_frame_from_market_data",
            lambda **kwargs: pd.DataFrame(
                {"GOLD1": [0.01, 0.02, -0.01], "GOLD2": [0.02, 0.01, 0.03]},
                index=pd.date_range("2024-01-01", periods=3, tz="UTC"),
            ),
        )
        monkeypatch.setattr(
            optimizer_module,
            "_run_vectorbt_backtest",
            lambda config, strategy_path: _empty_vectorbt_result(config),
        )

        base_result = _base_result(tmp_path)
        config = OptimizationConfig(
            base_backtest_id="bt_x",
            target_metric="sharpe_ratio",
            optimization_mode="risk_parity_covariance",
        )
        optimizer_module._run_risk_parity_covariance_optimization(
            config=config,
            base_result=base_result,
            strategy_identifier="golden_sma",
            strategy_path=Path("/tmp/whatever.py"),
            strategy_slug="golden_sma",
            symbols=["GOLD1", "GOLD2"],
            candidate_universes=[],
            timeframe="1h",
            initial_capital=100_000.0,
            train_period={"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-01T00:00:00+00:00"},
            test_period={"start": "2024-06-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"},
            base_parameters={},
            progress_callback=None,
        )

        columns = set(recorded["returns"].columns)
        assert columns == {"GOLD1", "GOLD2"}, (
            "covariance must be computed from asset-price returns; got the "
            f"trade-PnL pseudo-equity columns {sorted(columns)}"
        )


# ─── No-trade trials cannot win (AC 2) ──────────────────────────────────────


class TestNoTradeTrialsCannotWin:
    def test_no_trade_trial_scores_below_every_trading_trial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A trial that never trades must not beat honest negative Sharpe."""
        captured: dict[str, float] = {}

        def fake_run_vectorbt(config: BacktestConfig, strategy_path) -> object:
            return _empty_vectorbt_result(config)

        monkeypatch.setattr(optimizer_module, "_run_vectorbt_backtest", fake_run_vectorbt)
        monkeypatch.setattr(optimizer_module, "_load_base_backtest", lambda _id: _base_result(tmp_path))
        monkeypatch.setattr(
            optimizer_module,
            "_asset_returns_frame_from_base_result",
            lambda result: pd.DataFrame(),
        )
        monkeypatch.setattr(
            optimizer_module,
            "_asset_returns_frame_from_market_data",
            lambda **kwargs: pd.DataFrame({"GOLD1": [0.01], "GOLD2": [0.02]}),
        )
        monkeypatch.setattr(
            optimizer_module, "_split_backtest_period",
            lambda s, e, pct, **kwargs: (
                {"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-01T00:00:00+00:00"},
                {"start": "2024-06-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"},
            ),
        )
        monkeypatch.setattr(optimizer_module, "_resolve_strategy_reference", lambda r: ("s", Path("mock_strategy.py"), "s"))
        monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: None)
        monkeypatch.setattr(
            optimizer_module,
            "has_sufficient_segment_coverage",
            lambda **kwargs: True,
        )

        class FakeStudy:
            def __init__(self):
                self.trials: list = []

            def get_trials(self, deepcopy: bool = True, states=None):
                return list(self.trials)

            def optimize(self, objective, n_trials, callbacks=None, catch=None):
                class _Trial:
                    number = 0
                    state = optuna.trial.TrialState.COMPLETE
                    params = {"fast_length": 2}
                    value = None

                    def __init__(self):
                        self.user_attrs: dict = {}

                    def suggest_int(self, name, low, high, step=1):
                        return low

                    def suggest_float(self, name, low, high, **kwargs):
                        return low

                    def suggest_categorical(self, name, choices):
                        return choices[0]

                    def set_user_attr(self, key, value):
                        self.user_attrs[key] = value

                trial = _Trial()
                captured["no_trade_value"] = float(objective(trial))
                trial.value = captured["no_trade_value"]
                # Record the trial as COMPLETE (the real study would) so the
                # post-optimize completed-trials gate passes and the
                # no-trade exclusion in _best_trial_or_raise is what fires.
                self.trials.append(trial)

            @property
            def best_trial(self):
                raise AssertionError("study must never complete with only no-trade trials")

            @property
            def best_params(self):
                return {}

            @property
            def best_value(self):
                return None

        monkeypatch.setattr(optimizer_module.optuna, "create_study", lambda **kwargs: FakeStudy())

        config = OptimizationConfig(
            base_backtest_id="bt_x",
            target_metric="sharpe_ratio",
            n_trials=2,
            parameter_ranges={"fast_length": {"type": "int", "min": 2, "max": 10}},
        )
        with pytest.raises(RuntimeError, match="no-trade|no trades|no completed"):
            run_optimization(config, progress_callback=None)

        # Whatever the value is, it must carry the explicit no-trade penalty marker.
        assert captured["no_trade_value"] < 0, (
            "a no-trade trial must score strictly below 0 so it can never beat "
            "an honest trading trial with negative Sharpe"
        )


# ─── Shared fixture ──────────────────────────────────────────────────────────


def _empty_vectorbt_result(config: BacktestConfig):
    from shared.models import BacktestMeta, BacktestResult

    meta = BacktestMeta(
        id="trial",
        created_at="2024-01-01T00:00:00+00:00",
        engine="vectorbt",
        strategies=["s"],
        backtest_start=config.start_date,
        backtest_end=config.end_date,
        data={"symbols": list(config.symbols), "timeframes": [config.timeframe]},
        initial_capital=config.initial_capital,
    )
    return BacktestResult(meta=meta, trades=[], equity_curve=[])


def _base_result(tmp_path: Path):
    from shared.models import BacktestMeta, BacktestResult

    meta = BacktestMeta(
        id="bt_x",
        created_at="2024-01-01T00:00:00+00:00",
        engine="vectorbt",
        strategies=["golden_sma"],
        backtest_start="2024-01-01",
        backtest_end="2024-12-31",
        total_bars=100,
        data={"symbols": ["GOLD1", "GOLD2"], "timeframes": ["1h"], "data_dir": str(tmp_path)},
        initial_capital=100_000,
        parameters={"fast_length": 5, "slow_length": 20},
    )
    return BacktestResult(meta=meta, trades=[])


# ─── Mutation hardening: penalty margin + empty-study guard ─────────────────


class TestPenaltyMarginAndEmptyStudy:
    def test_penalty_is_strictly_below_worst_trading_trial(self):
        """The penalty must sit BELOW the worst trading value with margin.

        Kills the mutants that place the penalty above the worst trading
        trial (which would let a no-trade trial beat honest negative-Sharpe
        trials again) or that shrink the margin to zero.
        """
        values = [-3.0, -1.0, 0.5, 2.0]
        penalty = optimizer_module._no_trade_penalty_value(values)
        assert penalty < min(values), "penalty must be below the WORST trading value"
        assert penalty <= min(values) - 0.5, "penalty must keep a safety margin"

    def test_first_trial_penalty_is_a_large_negative_constant(self):
        assert optimizer_module._no_trade_penalty_value([]) == optimizer_module._NO_TRADE_PENALTY
        assert optimizer_module._NO_TRADE_PENALTY < -1e6, (
            "the seed penalty must be far below any plausible metric value"
        )

    def test_empty_study_raises_rather_than_selecting_failed_trials(self, tmp_path, monkeypatch):
        """A study with zero completed trials must fail loudly.

        Kills the mutant that bypasses the completed-trials guard and would
        select parameters from FAILED trials (whose params are meaningless).
        """
        import optuna

        study = optuna.create_study(direction="maximize")
        # One FAILED trial only (no completed trials).
        trial = study.ask()
        study.tell(trial, state=optuna.trial.TrialState.FAIL)

        with pytest.raises(RuntimeError, match="no completed trials"):
            optimizer_module._best_trial_or_raise(study)


class TestBestTrialExcludesNoTrade:
    """CodeRabbit round: best-trial selection must skip penalized trials."""

    def _study_with_trials(self, monkeypatch, completed_values_and_penalties):
        import optuna

        study = optuna.create_study(direction="maximize")
        for value, penalized in completed_values_and_penalties:
            trial = study.ask()
            trial.suggest_float("x", 0.0, 1.0)
            if penalized:
                trial.set_user_attr("penalty_applied", True)
            study.tell(trial, value)
        return study

    def test_all_no_trade_trials_raise_instead_of_returning_penalized_best(self):
        """A study of only no-trade trials must fail loudly, not select one.

        Kills the CodeRabbit major: previously the best-trial selection
        ignored the penalty marker, so a study that never traded handed its
        (penalized) parameter set back as the winner with a fabricated
        best_value of −1e9.
        """
        study = self._study_with_trials(
            None, [(-1e9, True), (-1e9, True)]
        )
        with pytest.raises(RuntimeError, match="no-trade"):
            optimizer_module._best_trial_or_raise(study)

    def test_best_trial_is_best_trading_trial_not_global_best(self):
        """With a penalized trial scoring highest, the best TRADING one wins.

        Only possible through a resumed/foreign study where a penalized trial
        holds a high value; the selection must still skip it.
        """
        study = self._study_with_trials(
            None, [(5.0, True), (0.5, False), (0.1, False)]
        )
        best = optimizer_module._best_trial_or_raise(study)
        assert best.value == pytest.approx(0.5)


# ─── #82: per-metric study direction (p_value minimizes) ─────────────────────


class TestMetricDirection:
    """The study direction must match metric semantics (#82).

    ``p_value`` is a significance p-value: smaller = more significant.
    Maximizing it selected the most INSIGNIFICANT trials; the frontend
    already treats it as lower-is-favorable.
    """

    def test_study_direction_matches_metric_semantics(self):
        assert optimizer_module._study_direction("p_value") == "minimize"
        assert optimizer_module._study_direction("sharpe_ratio") == "maximize"
        assert optimizer_module._study_direction("sharpe") == "maximize"
        assert optimizer_module._study_direction("total_pnl") == "maximize"
        assert optimizer_module._study_direction("net_profit") == "maximize"

    def test_unknown_metric_fails_loudly(self):
        with pytest.raises(ValueError, match="Unknown target metric"):
            optimizer_module._study_direction("not_a_metric")

    def test_run_optimization_creates_minimize_study_for_p_value(self, monkeypatch, tmp_path):
        """The created study's direction must follow the metric (#82)."""
        captured: dict[str, str] = {}

        real_create_study = optimizer_module.optuna.create_study

        def spy_create_study(**kwargs):
            captured["direction"] = kwargs.get("direction", "")
            return real_create_study(**kwargs)

        monkeypatch.setattr(optimizer_module.optuna, "create_study", spy_create_study)
        monkeypatch.setattr(
            optimizer_module, "_run_vectorbt_backtest", lambda *a, **k: _empty_vectorbt_result(a[0])
        )
        monkeypatch.setattr(optimizer_module, "_load_base_backtest", lambda _id: _base_result(tmp_path))
        monkeypatch.setattr(
            optimizer_module,
            "_asset_returns_frame_from_base_result",
            lambda result: pd.DataFrame(),
        )
        monkeypatch.setattr(
            optimizer_module,
            "_asset_returns_frame_from_market_data",
            lambda **kwargs: pd.DataFrame({"GOLD1": [0.01], "GOLD2": [0.02]}),
        )
        monkeypatch.setattr(
            optimizer_module,
            "_split_backtest_period",
            lambda s, e, pct, **kwargs: (
                {"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-01T00:00:00+00:00"},
                {"start": "2024-06-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"},
            ),
        )
        monkeypatch.setattr(
            optimizer_module, "_resolve_strategy_reference", lambda r: ("s", Path("mock_strategy.py"), "s")
        )
        monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: None)
        monkeypatch.setattr(optimizer_module, "has_sufficient_segment_coverage", lambda **kwargs: True)

        config = OptimizationConfig(
            base_backtest_id="bt_x",
            target_metric="p_value",
            n_trials=1,
            parameter_ranges={"fast_length": {"type": "int", "min": 2, "max": 10}},
        )
        with pytest.raises(RuntimeError):
            run_optimization(config, progress_callback=None)

        assert captured["direction"] == "minimize"

    def test_minimize_direction_selects_lowest_trading_trial(self):
        """A minimize-study's best trading trial is the MINIMUM (#82)."""
        study = self._minimize_study_with_trials([(0.5, False), (0.01, False), (0.2, True)])
        best = optimizer_module._best_trial_or_raise(study, direction="minimize")
        assert best.value == pytest.approx(0.01)

    @staticmethod
    def _minimize_study_with_trials(completed_values_and_penalties):
        import optuna

        study = optuna.create_study(direction="minimize")
        for value, penalized in completed_values_and_penalties:
            trial = study.ask()
            trial.suggest_float("x", 0.0, 1.0)
            if penalized:
                trial.set_user_attr("penalty_applied", True)
            study.tell(trial, value)
        return study

    def test_no_trade_penalty_is_a_ceiling_for_minimize(self):
        """For minimize-metrics the no-trade penalty must sit ABOVE every
        plausible value (p-values live in [0, 1]), not below."""
        penalty = optimizer_module._no_trade_penalty_value([0.4, 0.6], direction="minimize")
        assert penalty == pytest.approx(1.6)
        assert optimizer_module._no_trade_penalty_value([], direction="minimize") == pytest.approx(
            optimizer_module._NO_TRADE_PENALTY_MAXIMIZE_METRICS
        )

    def test_top_trials_rank_lowest_first_for_minimize(self):
        """The top-trials payload lists a minimize-study's BEST (lowest) first."""
        import optuna

        study = optuna.create_study(direction="minimize")
        for value in (0.5, 0.01, 0.2):
            trial = study.ask()
            trial.suggest_float("x", 0.0, 1.0)
            study.tell(trial, value)
        payload = optimizer_module._top_trials_payload(study.trials, limit=10, direction="minimize")
        values = [trial["value"] for trial in payload]
        assert values == sorted(values)
        assert values[0] == pytest.approx(0.01)

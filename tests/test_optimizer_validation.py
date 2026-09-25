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

import pandas as pd
import pytest

from runner import optimizer as optimizer_module
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.optimizer import OptimizationConfig, run_optimization


class TestExecutionContextPropagation:
    def test_base_backtest_falls_back_to_sqlite_by_id(self, tmp_path, monkeypatch):
        expected = _base_result(tmp_path)
        monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", tmp_path / "empty")
        monkeypatch.setattr(
            optimizer_module, "load_backtest_result", lambda _backtest_id: expected
        )

        assert optimizer_module._load_base_backtest("bt_x") is expected

    def test_missing_execution_context_fails_closed(self, tmp_path):
        result = _base_result(tmp_path)
        result.meta.data.pop("execution_context")

        with pytest.raises(ValueError, match="execution_context"):
            optimizer_module._execution_context_from_result(result)

    def test_optimizer_config_preserves_risk_data_and_modeling_context(self, tmp_path):
        result = _base_result(tmp_path)
        context = optimizer_module._execution_context_from_result(result)

        config = optimizer_module._optimizer_backtest_config(
            strategy_slug="golden_sma",
            symbols=["GOLD1"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-06-01",
            initial_capital=100000.0,
            parameters={"fast_length": 3},
            context=context,
        )

        assert config.risk_per_trade_pct == 0.01
        assert config.max_position_exposure_pct == 0.05
        assert config.max_leverage == 1.0
        assert config.modeling_resolution == "open_close"
        assert config.signal_exit_fill == "close"
        assert config.data_dir == str(tmp_path)
        assert config.data_source == ""

    def test_context_fingerprint_changes_with_strategy_source(self, tmp_path):
        strategy_path = tmp_path / "strategy.py"
        strategy_path.write_text("VALUE = 1\n", encoding="utf-8")
        first = optimizer_module._context_fingerprint({}, strategy_path)
        strategy_path.write_text("VALUE = 2\n", encoding="utf-8")
        second = optimizer_module._context_fingerprint({}, strategy_path)

        assert first != second


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
        captured_config: dict[str, object] = {}

        def _capture_config(config, strategy_path):
            captured_config.update(
                {
                    "risk_per_trade_pct": config.risk_per_trade_pct,
                    "max_position_exposure_pct": config.max_position_exposure_pct,
                    "max_leverage": config.max_leverage,
                    "data_dir": config.data_dir,
                    "data_source": config.data_source,
                }
            )
            return _empty_vectorbt_result(config)

        monkeypatch.setattr(
            optimizer_module,
            "_run_vectorbt_backtest",
            _capture_config,
        )

        base_result = _base_result(tmp_path)
        config = OptimizationConfig(
            base_backtest_id="bt_x",
            target_metric="sharpe_ratio",
            optimization_mode="risk_parity_covariance",
        )
        result = optimizer_module._run_risk_parity_covariance_optimization(
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
            execution_context=base_result.meta.data["execution_context"],
            context_fingerprint="context-fp",
            progress_callback=None,
        )

        columns = set(recorded["returns"].columns)
        assert columns == {"GOLD1", "GOLD2"}, (
            "covariance must be computed from asset-price returns; got the "
            f"trade-PnL pseudo-equity columns {sorted(columns)}"
        )
        assert captured_config["risk_per_trade_pct"] == 0.01
        assert captured_config["max_position_exposure_pct"] == 0.05
        assert captured_config["max_leverage"] == 1.0
        assert captured_config["data_dir"] == str(tmp_path)
        assert result["execution_context_fingerprint"] == "context-fp"
        assert result["execution_context"]["risk_config"]["risk_per_trade_pct"] == 0.01


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
            lambda s, e, pct: (
                {"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-01T00:00:00+00:00"},
                {"start": "2024-06-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"},
            ),
        )
        strategy_path = tmp_path / "s.py"
        strategy_path.write_text("def generate_signals():\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(
            optimizer_module,
            "_resolve_strategy_reference",
            lambda _result: ("s", strategy_path, "s"),
        )
        monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: None)
        monkeypatch.setattr(
            optimizer_module,
            "has_ohlcv_bars_in_range",
            lambda **kwargs: True,
        )

        class FakeStudy:
            def __init__(self):
                self.trials: list = []

            def get_trials(self, deepcopy: bool = True, states=None):
                return list(self.trials)

            def optimize(self, objective, n_trials, callbacks=None, catch=None):
                class _Trial:
                    def suggest_int(self, name, low, high, step=1):
                        return low

                    def suggest_float(self, name, low, high, **kwargs):
                        return low

                    def suggest_categorical(self, name, choices):
                        return choices[0]

                    def set_user_attr(self, key, value):
                        setattr(self, key, value)

                trial = _Trial()
                captured["no_trade_value"] = float(objective(trial))

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
        with pytest.raises(
            RuntimeError,
            match="no-trade|no trades|no completed|No optimization trial",
        ):
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
        data={
            "symbols": ["GOLD1", "GOLD2"],
            "timeframes": ["1h"],
            "execution_context": {
                "engine": "vectorbt",
                "data_dir": str(tmp_path),
                "data_source": "",
                "symbols": ["GOLD1", "GOLD2"],
                "timeframe": "1h",
                "initial_capital": 100_000.0,
                "risk_config": {
                    "risk_per_trade_pct": 0.01,
                    "max_position_exposure_pct": 0.05,
                    "max_leverage": 1.0,
                    "modeling_resolution": "open_close",
                    "signal_exit_fill": "close",
                    "warmup_start": "",
                },
            },
        },
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

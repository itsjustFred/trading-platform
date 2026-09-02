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
            lambda s, e, pct: (
                {"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-01T00:00:00+00:00"},
                {"start": "2024-06-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"},
            ),
        )
        monkeypatch.setattr(optimizer_module, "_resolve_strategy_reference", lambda r: ("s", Path("/tmp/s.py"), "s"))
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

            def optimize(self, objective, n_trials, callbacks=None):
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

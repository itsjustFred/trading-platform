from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import optuna
import pytest

import runner.optimizer as optimizer_module
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit


def _write_base_backtest(backtests_dir: Path) -> None:
    result = BacktestResult(
        meta=BacktestMeta(
            id="base-demo",
            created_at="2026-03-11T09:00:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"], "source": "test"},
            parameters={"fixed": 3},
        ),
        trades=[],
        equity_curve=[],
    )
    result.save(backtests_dir / "base-demo.trades.json")


def _write_risk_parity_base_backtest(backtests_dir: Path) -> None:
    trades: list[Trade] = []
    for index, symbol in enumerate(["SPY", "TLT", "GLD"], start=1):
        for day in range(1, 6):
            pnl = float((6 - day) * (10 if symbol != "GLD" else 3))
            trades.append(
                Trade(
                    id=len(trades) + 1,
                    symbol=symbol,
                    direction="LONG",
                    entry_time=f"2024-01-0{day}T00:00:00+00:00",
                    entry_price=100.0 + index,
                    quantity=1.0,
                    exits=[
                        TradeExit(
                            time=f"2024-01-0{day}T01:00:00+00:00",
                            price=101.0,
                            quantity=1.0,
                            pnl=pnl,
                        )
                    ],
                    pnl=pnl,
                )
            )
    result = BacktestResult(
        meta=BacktestMeta(
            id="rp-base",
            created_at="2026-03-11T09:00:00+00:00",
            engine="vectorbt",
            strategies=["risk_parity"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
            initial_capital=100000.0,
            data={"symbols": ["SPY", "TLT", "GLD"], "timeframes": ["1d"], "source": "test"},
            parameters={"rebalance_period": 21},
        ),
        trades=trades,
        equity_curve=[],
    )
    result.save(backtests_dir / "rp-base.trades.json")


def _trial_result(config, *, metric: float, trade_count: int) -> BacktestResult:
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
        "sortino_ratio": round(metric * 0.9, 4),
        "calmar_ratio": round(metric * 0.5, 4),
        "total_pnl": round(metric * 1000, 2),
        "cagr": round(metric * 0.1, 6),
        "win_rate": 1.0 if trade_count > 0 else 0.0,
        "profit_factor": 2.0 if trade_count > 0 else 0.0,
        "psr": 0.7 if trade_count > 0 else None,
        "p_value": 0.05,
        "total_return": round(metric * 0.05, 6),
        "max_drawdown_pct": -0.1 if trade_count > 0 else 0.0,
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
        equity_curve=[],
        stats=stats,
    )


def test_run_optimization_saves_best_params_and_oos_stats(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    optimizations_dir = tmp_path / "optimizations"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir)

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", optimizations_dir)
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
    original_create_study = optuna.create_study
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        lambda direction="maximize": original_create_study(
            direction=direction,
            sampler=optuna.samplers.GridSampler({"window": [1, 2, 3, 4]}),
        ),
    )

    calls = []

    def fake_run_backtest(config, strategy_path):
        calls.append(
            (
                config.start_date,
                config.end_date,
                dict(config.parameters),
                Path(strategy_path),
            )
        )
        window = int(config.parameters["window"])
        is_train = config.start_date.startswith("2024-01-01T00:00:00")
        train_metrics = {1: 0.0, 2: 1.0, 3: 2.5, 4: 1.8}
        test_metrics = {1: 0.0, 2: 0.5, 3: 1.7, 4: 1.2}
        metric = train_metrics[window] if is_train else test_metrics[window]
        trade_count = 0 if window == 1 else 1
        return _trial_result(config, metric=metric, trade_count=trade_count)

    monkeypatch.setattr(optimizer_module, "_run_vectorbt_backtest", fake_run_backtest)

    progress_updates: list[tuple[float, str]] = []
    payload = optimizer_module.run_optimization(
        optimizer_module.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=4,
            train_split_pct=0.7,
            parameter_ranges={"window": {"type": "int", "min": 1, "max": 4, "step": 1}},
        ),
        progress_callback=lambda percent, message: progress_updates.append((percent, message)),
    )

    assert payload["best_params"] == {"window": 3}
    assert payload["effective_best_parameters"] == {"fixed": 3, "window": 3}
    assert payload["train_stats"]["sharpe_ratio"] == 2.5
    assert payload["test_stats"]["sharpe_ratio"] == 1.7
    assert len(payload["top_trials"]) == 4
    assert payload["top_trials"][0]["params"] == {"window": 3}
    assert any(trial["penalty_applied"] for trial in payload["top_trials"])
    assert len(calls) == 5
    assert progress_updates[0] == (0.0, "Starting Optuna optimization.")
    assert any("✅ Trial" in message for _, message in progress_updates)
    assert progress_updates[-1][0] == 100.0
    assert progress_updates[-1][1].startswith("Optimization finished:")

    saved_files = list(optimizations_dir.glob("opt_base-demo_*.json"))
    assert len(saved_files) == 1
    saved_payload = json.loads(saved_files[0].read_text(encoding="utf-8"))
    assert saved_payload["best_params"] == {"window": 3}
    assert saved_payload["test_stats"]["sharpe_ratio"] == 1.7


def test_run_optimization_propagates_backtest_errors(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir)

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    original_create_study = optuna.create_study
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        lambda direction="maximize": original_create_study(
            direction=direction,
            sampler=optuna.samplers.GridSampler({"window": [2]}),
        ),
    )
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        lambda config, strategy_path: (_ for _ in ()).throw(RuntimeError("strategy blew up")),
    )

    with pytest.raises(RuntimeError, match="strategy blew up"):
        optimizer_module.run_optimization(
            optimizer_module.OptimizationConfig(
                base_backtest_id="base-demo",
                target_metric="sharpe",
                n_trials=1,
                train_split_pct=0.7,
                parameter_ranges={"window": {"type": "int", "min": 2, "max": 2, "step": 1}},
            )
        )


def test_run_risk_parity_covariance_optimization_saves_weights(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    optimizations_dir = tmp_path / "optimizations"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_risk_parity_base_backtest(backtests_dir)

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", optimizations_dir)
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "risk_parity.py",
            slug="risk_parity",
        ),
    )
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        lambda config, strategy_path: _trial_result(config, metric=1.25, trade_count=1),
    )
    monkeypatch.setattr(
        optimizer_module,
        "compute_stats_for_result",
        lambda result: dict(result.stats),
    )

    payload = optimizer_module.run_optimization(
        optimizer_module.OptimizationConfig(
            base_backtest_id="rp-base",
            target_metric="sharpe",
            n_trials=5,
            train_split_pct=0.7,
            optimization_mode="risk_parity_covariance",
            symbol_universes=[["SPY", "TLT"], ["SPY", "GLD"], ["SPY", "TLT", "GLD"]],
        )
    )

    assert payload["optimization_mode"] == "risk_parity_covariance"
    assert payload["target_metric"] == "risk_parity_score"
    assert set(payload["best_symbols"]) <= {"SPY", "TLT", "GLD"}
    assert payload["effective_best_parameters"]["risk_parity_weights"]
    assert sum(payload["effective_best_parameters"]["risk_parity_weights"].values()) == pytest.approx(1.0)
    assert payload["risk_parity"]["evaluated_universes"]
    saved_files = list(optimizations_dir.glob("rp_cov_rp-base_*.json"))
    assert len(saved_files) == 1

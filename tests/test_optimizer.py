from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import optuna
import pandas as pd
import pytest
from pydantic import ValidationError

import runner.optimizer as optimizer_module
from data_tools.auto_download import has_ohlcv_bars_in_range
from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit


def _optimization_config(*, train_split_pct: float) -> optimizer_module.OptimizationConfig:
    return optimizer_module.OptimizationConfig(
        base_backtest_id="base-demo",
        target_metric="sharpe",
        n_trials=1,
        train_split_pct=train_split_pct,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 2, "step": 1}},
    )


def test_optimization_config_requires_held_out_test_segment():
    with pytest.raises(ValidationError) as exc_info:
        _optimization_config(train_split_pct=1.0)

    assert exc_info.value.errors()[0]["loc"] == ("train_split_pct",)
    assert exc_info.value.errors()[0]["type"] == "less_than_equal"


@pytest.mark.parametrize("train_split_pct", [0.1, 0.7])
def test_optimization_config_accepts_current_train_test_splits(train_split_pct: float):
    assert _optimization_config(train_split_pct=train_split_pct).train_split_pct == train_split_pct


def test_optimization_config_enforces_inclusive_train_split_upper_bound():
    assert _optimization_config(train_split_pct=0.95).train_split_pct == pytest.approx(0.95)

    with pytest.raises(ValidationError) as exc_info:
        _optimization_config(train_split_pct=0.96)

    assert exc_info.value.errors()[0]["loc"] == ("train_split_pct",)


def test_split_backtest_period_returns_positive_non_overlapping_segments():
    train_period, test_period = optimizer_module._split_backtest_period(
        "2024-01-01",
        "2024-01-10",
        0.7,
    )

    train_start = datetime.fromisoformat(train_period["start"])
    train_end = datetime.fromisoformat(train_period["end"])
    test_start = datetime.fromisoformat(test_period["start"])
    test_end = datetime.fromisoformat(test_period["end"])

    assert train_start < train_end < test_start < test_end


def test_split_backtest_period_rejects_range_too_short_for_both_segments():
    with pytest.raises(ValueError, match="empty train or test period"):
        optimizer_module._split_backtest_period(
            "2024-01-01T00:00:00+00:00",
            "2024-01-01T00:00:00.000001+00:00",
            0.7,
        )


def _write_base_backtest(
    backtests_dir: Path,
    *,
    symbols: list[str] | None = None,
    data_dir: Path | None = None,
    session: dict[str, str] | None = None,
) -> None:
    data = {
        "symbols": symbols or ["AAPL"],
        "timeframes": ["1d"],
        "source": "test",
    }
    if data_dir is not None:
        data["data_dir"] = str(data_dir)
    if session is not None:
        data["session"] = session
    result = BacktestResult(
        meta=BacktestMeta(
            id="base-demo",
            created_at="2026-03-11T09:00:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
            initial_capital=100000.0,
            risk_per_trade_pct=0.01,
            data=data,
            parameters={"fixed": 3},
        ),
        trades=[],
        equity_curve=[],
    )
    result.save(backtests_dir / "base-demo.trades.json")


def _write_ohlcv_bars(data_dir: Path, symbol: str, timestamps: list[str]) -> None:
    path = data_dir / "ohlcv" / "test" / symbol / "1d.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * len(timestamps),
            "high": [101.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": [100.5] * len(timestamps),
            "volume": [1000.0] * len(timestamps),
        }
    ).to_parquet(path, index=False)


def test_has_ohlcv_bars_in_range_uses_exact_valid_utc_timestamps(tmp_path: Path):
    _write_ohlcv_bars(
        tmp_path,
        "AAPL",
        ["invalid", "2024-01-02T00:00:00+00:00", "2024-01-09T00:00:00+00:00"],
    )

    assert has_ohlcv_bars_in_range(
        symbol="aapl",
        timeframe="1d",
        start="2024-01-01T00:00:00+00:00",
        end="2024-01-05T00:00:00+00:00",
        data_dir=tmp_path,
    )
    assert has_ohlcv_bars_in_range(
        symbol="AAPL",
        timeframe="1d",
        start="2024-01-08T00:00:00+00:00",
        end="2024-01-10T00:00:00+00:00",
        data_dir=tmp_path,
    )
    assert not has_ohlcv_bars_in_range(
        symbol="AAPL",
        timeframe="1d",
        start="2024-01-03T00:00:00+00:00",
        end="2024-01-08T00:00:00+00:00",
        data_dir=tmp_path,
    )


def test_has_ohlcv_bars_in_range_does_not_treat_glob_metacharacters_as_wildcards(
    tmp_path: Path,
):
    _write_ohlcv_bars(
        tmp_path,
        "AAPL",
        ["2024-01-02T00:00:00+00:00"],
    )

    assert not has_ohlcv_bars_in_range(
        symbol="AA*L",
        timeframe="1d",
        start="2024-01-01T00:00:00+00:00",
        end="2024-01-05T00:00:00+00:00",
        data_dir=tmp_path,
    )


def _forbid_call(message: str, exc_type: type[Exception] = AssertionError):
    """Return a callable that raises if the patched function is invoked."""

    def _forbidden(*args, **kwargs):
        raise exc_type(message)

    return _forbidden


def _allow_bar_coverage_preflight(monkeypatch) -> None:
    monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(optimizer_module, "has_sufficient_segment_coverage", lambda **kwargs: True)


def _write_risk_parity_base_backtest(backtests_dir: Path, *, data_dir: Path | None = None) -> None:
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
            risk_per_trade_pct=0.01,
            data={
                "symbols": ["SPY", "TLT", "GLD"],
                "timeframes": ["1d"],
                "source": "test",
                **({"data_dir": str(data_dir)} if data_dir is not None else {}),
            },
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


def test_run_optimization_rejects_empty_test_bar_segment_before_study(
    monkeypatch,
    tmp_path: Path,
):
    backtests_dir = tmp_path / "backtests"
    data_dir = tmp_path / "market-data"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir, data_dir=data_dir)
    # #86: dense train coverage (01-01..01-07 daily) so the TRAIN gate
    # passes and the TEST gate (01-07..01-10, empty) is the one that fails.
    _write_ohlcv_bars(
        data_dir,
        "AAPL",
        [f"2024-01-0{d}T00:00:00+00:00" for d in range(1, 8)],
    )

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    ensure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        optimizer_module,
        "ensure_backtest_data",
        lambda **kwargs: ensure_calls.append(kwargs),
    )
    study_calls: list[dict[str, object]] = []

    def forbidden_create_study(**kwargs):
        study_calls.append(kwargs)
        raise AssertionError("Optuna study started before exact bar coverage validation")

    monkeypatch.setattr(optimizer_module.optuna, "create_study", forbidden_create_study)
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        _forbid_call("Backtest started before exact bar coverage validation"),
    )

    config = _optimization_config(train_split_pct=0.7)
    with pytest.raises(ValueError, match=r"test.*AAPL 1d"):
        optimizer_module.run_optimization(config)

    assert study_calls == []
    assert len(ensure_calls) == 1
    assert ensure_calls[0]["symbols"] == ["AAPL"]
    assert ensure_calls[0]["start"] == "2024-01-01"
    assert ensure_calls[0]["end"] == "2024-01-10"


def test_run_optimization_rejects_empty_train_bar_segment_before_study(
    monkeypatch,
    tmp_path: Path,
):
    backtests_dir = tmp_path / "backtests"
    data_dir = tmp_path / "market-data"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir, data_dir=data_dir)
    _write_ohlcv_bars(data_dir, "AAPL", ["2024-01-09T00:00:00+00:00"])

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    monkeypatch.setattr(optimizer_module, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        _forbid_call("Optuna study started before train coverage validation"),
    )

    config = _optimization_config(train_split_pct=0.7)
    with pytest.raises(ValueError, match=r"train.*AAPL 1d"):
        optimizer_module.run_optimization(config)


def test_run_optimization_checks_every_candidate_universe_symbol_before_study(
    monkeypatch,
    tmp_path: Path,
):
    backtests_dir = tmp_path / "backtests"
    data_dir = tmp_path / "market-data"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir, data_dir=data_dir)
    # #86: AAPL dense across the full window (passes both gates);
    # MSFT dense in train only (01-01..01-07) so the TEST gate names MSFT.
    _write_ohlcv_bars(
        data_dir,
        "AAPL",
        [f"2024-01-{d:02d}T00:00:00+00:00" for d in range(1, 11)],
    )
    _write_ohlcv_bars(
        data_dir, "MSFT", [f"2024-01-0{d}T00:00:00+00:00" for d in range(1, 8)]
    )

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    ensure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        optimizer_module,
        "ensure_backtest_data",
        lambda **kwargs: ensure_calls.append(kwargs),
    )
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        _forbid_call("Optuna study started before candidate coverage validation"),
    )

    config = optimizer_module.OptimizationConfig(
        base_backtest_id="base-demo",
        target_metric="sharpe",
        n_trials=1,
        train_split_pct=0.7,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 2}},
        symbol_universes=[["AAPL", "MSFT"]],
    )
    with pytest.raises(ValueError, match=r"test.*MSFT 1d"):
        optimizer_module.run_optimization(config)

    assert len(ensure_calls) == 1
    assert ensure_calls[0]["symbols"] == ["AAPL", "MSFT"]


def test_run_optimization_saves_best_params_and_oos_stats(monkeypatch, tmp_path: Path):
    _patch_optimizer_env(monkeypatch, tmp_path, grid_choices=[1, 2, 3, 4], pass_through_stats=True)

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

    saved_files = list((tmp_path / "optimizations").glob("opt_base-demo_*.json"))
    assert len(saved_files) == 1
    saved_payload = json.loads(saved_files[0].read_text(encoding="utf-8"))
    assert saved_payload["best_params"] == {"window": 3}
    assert saved_payload["test_stats"]["sharpe_ratio"] == 1.7


def test_run_optimization_rejects_legacy_base_before_study(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir()
    result = BacktestResult(
        meta=BacktestMeta(
            id="legacy-demo",
            created_at="2024-01-01T00:00:00+00:00",
            engine="vectorbt",
            strategies=["example_sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
            initial_capital=100_000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"]},
        ),
        trades=[],
    )
    result.save(backtests_dir / "legacy-demo.trades.json")
    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        _forbid_call("study must not be created for a legacy artifact"),
    )

    with pytest.raises(ValueError, match="legacy artifact.*risk_per_trade_pct"):
        optimizer_module.run_optimization(
            optimizer_module.OptimizationConfig(
                base_backtest_id="legacy-demo",
                target_metric="sharpe",
                n_trials=1,
                parameter_ranges={"window": {"type": "int", "min": 2, "max": 2}},
            )
        )


def test_run_optimization_propagates_explicit_base_risk_to_real_config(
    monkeypatch, tmp_path: Path
):
    _patch_optimizer_env(monkeypatch, tmp_path, grid_choices=[2], pass_through_stats=True)
    propagated: list[float | None] = []

    def fake_run_backtest(config, strategy_path):
        propagated.append(config.risk_per_trade_pct)
        return _trial_result(config, metric=1.0, trade_count=1)

    monkeypatch.setattr(optimizer_module, "_run_vectorbt_backtest", fake_run_backtest)

    optimizer_module.run_optimization(
        optimizer_module.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=1,
            train_split_pct=0.7,
            parameter_ranges={"window": {"type": "int", "min": 2, "max": 2}},
        )
    )

    assert propagated and all(value == pytest.approx(0.01) for value in propagated)


def test_run_optimization_passes_explicit_session_to_coverage_gate(
    monkeypatch, tmp_path: Path
):
    session = {"start": "14:30", "end": "19:30"}
    _patch_optimizer_env(
        monkeypatch,
        tmp_path,
        grid_choices=[2],
        pass_through_stats=True,
        session=session,
    )
    coverage_calls: list[dict[str, object]] = []

    def fake_coverage(**kwargs):
        coverage_calls.append(kwargs)
        return True

    monkeypatch.setattr(optimizer_module, "has_sufficient_segment_coverage", fake_coverage)
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        lambda config, strategy_path: _trial_result(config, metric=1.0, trade_count=1),
    )

    optimizer_module.run_optimization(
        optimizer_module.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=1,
            parameter_ranges={"window": {"type": "int", "min": 2, "max": 2}},
        )
    )

    assert coverage_calls
    assert all(call["session"] == session for call in coverage_calls)


def test_run_optimization_propagates_backtest_errors(monkeypatch, tmp_path: Path):
    _patch_optimizer_env(monkeypatch, tmp_path, grid_choices=[2])
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        _forbid_call("strategy blew up", RuntimeError),
    )

    config = optimizer_module.OptimizationConfig(
        base_backtest_id="base-demo",
        target_metric="sharpe",
        n_trials=1,
        train_split_pct=0.7,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 2, "step": 1}},
    )
    with pytest.raises(RuntimeError) as exc_info:
        optimizer_module.run_optimization(config)
    assert "No optimization trial completed successfully" in str(exc_info.value)
    assert "strategy blew up" in str(exc_info.value.__cause__)


def test_run_risk_parity_covariance_optimization_saves_weights(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    optimizations_dir = tmp_path / "optimizations"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_risk_parity_base_backtest(backtests_dir, data_dir=backtests_dir)

    # Cleanup 16-D1 (F30): covariance returns come from market data first.
    # Seed tradable bars for the universe so the market-data path succeeds
    # without network access.
    for symbol in ("SPY", "TLT", "GLD"):
        _write_ohlcv_bars(
            backtests_dir,
            symbol,
            [f"2024-01-{day:02d}T00:00:00+00:00" for day in range(1, 11)],
        )
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
    weights = payload["effective_best_parameters"]["risk_parity_weights"].values()
    assert sum(weights) == pytest.approx(1.0)
    assert payload["risk_parity"]["evaluated_universes"]
    saved_files = list(optimizations_dir.glob("rp_cov_rp-base_*.json"))
    assert len(saved_files) == 1


def test_no_trade_trials_lose_against_honest_negative_trials(monkeypatch, tmp_path: Path):
    """A no-trade trial must not return penalty 0.0 and beat honest negatives."""
    _patch_optimizer_env(monkeypatch, tmp_path, grid_choices=[1, 2], pass_through_stats=True)

    def fake_run_backtest(config, strategy_path):
        window = int(config.parameters["window"])
        # window=1 -> no trades (honest metric would be very negative)
        # window=2 -> trades with a small but real loss
        if window == 1:
            return _trial_result(config, metric=-50.0, trade_count=0)
        return _trial_result(config, metric=-0.5, trade_count=1)

    monkeypatch.setattr(optimizer_module, "_run_vectorbt_backtest", fake_run_backtest)

    payload = optimizer_module.run_optimization(
        optimizer_module.OptimizationConfig(
            base_backtest_id="base-demo",
            target_metric="sharpe",
            n_trials=2,
            train_split_pct=0.7,
            parameter_ranges={"window": {"type": "int", "min": 1, "max": 2, "step": 1}},
        ),
    )

    assert payload["best_params"] == {"window": 2}
    assert payload["best_value"] == -0.5
    penalties = {
        trial["params"]["window"]: trial["penalty_applied"]
        for trial in payload["top_trials"]
    }
    assert penalties == {1: True, 2: False}
    no_trade_trial = next(t for t in payload["top_trials"] if t["params"]["window"] == 1)
    assert no_trade_trial["value"] is None or no_trade_trial["value"] < -0.5


def _patch_grid_sampler(monkeypatch, choices: list[int]) -> None:
    """Pin optuna.create_study to a deterministic GridSampler over choices."""
    original_create_study = optuna.create_study
    monkeypatch.setattr(
        optimizer_module.optuna,
        "create_study",
        lambda direction="maximize": original_create_study(
            direction=direction,
            sampler=optuna.samplers.GridSampler({"window": choices}),
        ),
    )


def _patch_optimizer_env(
    monkeypatch,
    tmp_path: Path,
    *,
    grid_choices: list[int],
    pass_through_stats: bool = False,
    session: dict[str, str] | None = None,
) -> None:
    """Shared optimizer test environment: base backtest on disk, redirected
    output dirs, bar-coverage preflight allowed, strategy entry resolved to
    the tmp strategy, and a deterministic GridSampler."""
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir, session=session)
    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    _allow_bar_coverage_preflight(monkeypatch)
    monkeypatch.setattr(
        optimizer_module,
        "find_strategy_entry",
        lambda _: SimpleNamespace(
            file_path=tmp_path / "strategies" / "example_sma.py",
            slug="example_sma",
        ),
    )
    if pass_through_stats:
        monkeypatch.setattr(
            optimizer_module,
            "compute_stats_for_result",
            lambda result: dict(result.stats),
        )
    _patch_grid_sampler(monkeypatch, grid_choices)


def test_metric_reaching_no_trade_penalty_floor_fails_loudly(monkeypatch, tmp_path: Path):
    """Pre-mortem hardening: the floor only steers correctly while honest metric
    values rank above it; a metric that reaches it must raise instead of
    silently inverting the no-trade steering."""
    _patch_optimizer_env(monkeypatch, tmp_path, grid_choices=[2], pass_through_stats=True)
    monkeypatch.setattr(
        optimizer_module,
        "_run_vectorbt_backtest",
        lambda config, strategy_path: _trial_result(
            config, metric=optimizer_module.NO_TRADE_PENALTY - 1.0, trade_count=1
        ),
    )

    config = optimizer_module.OptimizationConfig(
        base_backtest_id="base-demo",
        target_metric="sharpe",
        n_trials=1,
        train_split_pct=0.7,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 2, "step": 1}},
    )
    with pytest.raises(
        RuntimeError, match="No optimization trial completed successfully"
    ) as exc_info:
        optimizer_module.run_optimization(config)
    # The floor violation is preserved as the trial's failure reason through
    # the all-failed guard, instead of silently steering the study.
    assert "at or below the no-trade penalty floor" in str(exc_info.value)


def test_all_trials_failing_raises_informative_error(monkeypatch, tmp_path: Path):
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _write_base_backtest(backtests_dir)

    monkeypatch.setattr(optimizer_module, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(optimizer_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    _allow_bar_coverage_preflight(monkeypatch)
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
        _forbid_call("strategy blew up", RuntimeError),
    )

    config = optimizer_module.OptimizationConfig(
        base_backtest_id="base-demo",
        target_metric="sharpe",
        n_trials=1,
        train_split_pct=0.7,
        parameter_ranges={"window": {"type": "int", "min": 2, "max": 2, "step": 1}},
    )
    with pytest.raises(RuntimeError, match="No optimization trial completed successfully"):
        optimizer_module.run_optimization(config)


class TestRiskAdjustedObjective:
    """C3: raw targets carry mandatory trade-count/drawdown haircuts."""

    def test_lucky_sparse_run_loses_to_robust_run(self) -> None:
        from runner.optimizer import _risk_adjusted_metric_value

        lucky, _ = _risk_adjusted_metric_value(
            5000.0,
            {"total_trades": 3, "max_drawdown_pct": -0.40},
            metric_key="total_pnl",
            direction="maximize",
        )
        robust, _ = _risk_adjusted_metric_value(
            3000.0,
            {"total_trades": 120, "max_drawdown_pct": -0.05},
            metric_key="total_pnl",
            direction="maximize",
        )
        assert robust > lucky

    def test_full_credit_leaves_value_untouched(self) -> None:
        from runner.optimizer import _risk_adjusted_metric_value

        value, terms = _risk_adjusted_metric_value(
            100.0,
            {"total_trades": 30, "max_drawdown_pct": 0.0},
            metric_key="win_rate",
            direction="maximize",
        )
        assert value == 100.0
        assert terms == {"sparsity_factor": 1.0, "drawdown_factor": 1.0}

    def test_losing_value_passes_through_raw(self) -> None:
        from runner.optimizer import _risk_adjusted_metric_value

        # A multiplicative haircut would compress losers toward zero and let
        # them outrank small winners — losers keep their raw value.
        value, _ = _risk_adjusted_metric_value(
            -50.0,
            {"total_trades": 5, "max_drawdown_pct": -0.5},
            metric_key="total_pnl",
            direction="maximize",
        )
        assert value == -50.0

    def test_risk_adjusted_metrics_pass_through(self) -> None:
        from runner.optimizer import _risk_adjusted_metric_value

        for key in ("sharpe_ratio", "sortino_ratio", "calmar_ratio", "cagr"):
            value, _ = _risk_adjusted_metric_value(
                1.5,
                {"total_trades": 2, "max_drawdown_pct": -0.9},
                metric_key=key,
                direction="maximize",
            )
            assert value == 1.5

    def test_percent_scaled_drawdown_fails_loud(self) -> None:
        import pytest

        from runner.optimizer import _risk_adjusted_metric_value

        # Pre-mortem T3: percent-scale drawdown must raise, not silently
        # zero the trial via the fraction clamp.
        with pytest.raises(ValueError, match="percent-scaled"):
            _risk_adjusted_metric_value(
                100.0,
                {"total_trades": 30, "max_drawdown_pct": -40.0},
                metric_key="total_pnl",
                direction="maximize",
            )

    def test_embargo_separates_train_and_test(self) -> None:
        from datetime import datetime

        train, test = optimizer_module._split_backtest_period(
            "2024-01-01", "2024-01-10", 0.7, embargo_days=1
        )
        test_start = datetime.fromisoformat(test["start"])
        train_end = datetime.fromisoformat(train["end"])
        assert (test_start - train_end).days >= 1

    def test_zero_embargo_keeps_adjacent_segments(self) -> None:
        train, test = optimizer_module._split_backtest_period(
            "2024-01-01", "2024-01-10", 0.7, embargo_days=0
        )
        assert train["end"] < test["start"]


class TestGridHardening:
    """C3: validated fan-out width + stale-cell invalidation helpers."""

    def test_max_workers_validation(self) -> None:
        from runner.backtest_grid import _validate_max_workers

        assert _validate_max_workers(4) == 4
        assert _validate_max_workers(None) == 1
        with pytest.raises(ValueError, match=">= 1"):
            _validate_max_workers(0)
        with pytest.raises(ValueError, match="integer"):
            _validate_max_workers("many")
        assert _validate_max_workers(1000) == 64

    def test_stale_cell_mismatch_recomputes(self, tmp_path: Path) -> None:
        from runner.backtest_grid import (
            CellRequest,
            _existing_cell_matches_request,
        )
        from shared.models import BacktestMeta, BacktestResult

        meta = BacktestMeta(
            id="cell-1",
            created_at="2026-01-01T00:00:00+00:00",
            engine="vectorbt",
            strategies=["sma"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
            initial_capital=100000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"]},
            parameters={"fast": 2},
        )
        result = BacktestResult(meta=meta, trades=[], equity_curve=[])
        request = CellRequest(
            strategy="sma",
            symbol="AAPL",
            timeframe="1d",
            start="2024-01-01",
            end="2024-01-10",
            engine="vectorbt",
            capital=100000.0,
            risk_pct=0.01,
            params={"fast": 2},
            output_dir=tmp_path,
        )
        assert _existing_cell_matches_request(result, request, "2024-01-01", "2024-01-10") is True
        stale = CellRequest(
            strategy="sma",
            symbol="AAPL",
            timeframe="1d",
            start="2024-01-01",
            end="2024-01-10",
            engine="vectorbt",
            capital=100000.0,
            risk_pct=0.01,
            params={"fast": 3},
            output_dir=tmp_path,
        )
        assert _existing_cell_matches_request(result, stale, "2024-01-01", "2024-01-10") is False


class TestMatrixOverviewContracts:
    """C3: no cross-capital aggregation in the strategy overview."""

    def test_overview_has_no_sums_or_means(self) -> None:
        from runner.backtest_matrix import build_strategy_overview

        rows = [
            {
                "strategy": "s",
                "strategy_name": "s",
                "status": "ok",
                "symbol": "A",
                "timeframe": "1d",
                "run_id": "r1",
                "sharpe_ratio": 1.0,
                "net_profit_pct": 5.0,
                "total_trades": 10,
                "total_pnl": 100.0,
            },
            {
                "strategy": "s",
                "strategy_name": "s",
                "status": "ok",
                "symbol": "B",
                "timeframe": "1d",
                "run_id": "r2",
                "sharpe_ratio": 2.0,
                "net_profit_pct": 7.0,
                "total_trades": 20,
                "total_pnl": 200.0,
            },
        ]
        (entry,) = build_strategy_overview(rows)
        assert "total_pnl" not in entry
        assert "avg_sharpe" not in entry
        assert entry["best_sharpe"] == 2.0
        assert entry["total_trades"] == 30

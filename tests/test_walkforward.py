"""Tests for walk-forward analysis module."""
from __future__ import annotations

import json
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner import walkforward as wf_module
from shared.models import BacktestMeta, BacktestResult, EquityPoint


class TestWalkforwardSignature:
    """Tests for function signature and defaults."""

    def test_walkforward_basic_call(self) -> None:
        """Test that walkforward function can be called and returns expected structure."""
        # Verify function signature
        sig = inspect.signature(wf_module.run_walkforward)
        params = list(sig.parameters.keys())
        assert "strategy" in params
        assert "engine" in params
        assert "backtest_ids" in params
        assert "train_months" in params
        assert "test_months" in params
        assert "n_trials" in params

    def test_walkforward_config_defaults(self) -> None:
        """Test walk-forward configuration defaults."""
        sig = inspect.signature(wf_module.run_walkforward)

        train_default = sig.parameters["train_months"].default
        test_default = sig.parameters["test_months"].default
        step_default = sig.parameters["step_months"].default
        trials_default = sig.parameters["n_trials"].default

        assert train_default == 12, "Default train_months should be 12 months"
        assert test_default == 3, "Default test_months should be 3 months"
        assert step_default == 1, "Default step_months should be 1 month"
        assert trials_default == 100, "Default n_trials should be 100"


class TestWalkforwardOutputStructure:
    """Tests for output JSON structure validation."""

    def test_walkforward_output_structure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test the expected output structure of walkforward with mocked optimizer."""
        # Mock the optimizer to return predictable results
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.5,
                "oos_sharpe": 1.2,
                "oos_max_drawdown": 0.15,
                "weights": {"strat1": 0.6, "strat2": 0.4},
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1", "strat2"],
            train_months=12,
            test_months=3,
            n_trials=10,
        )

        assert "backtest_ids" in result
        assert "config" in result
        assert "folds" in result
        assert "summary" in result
        assert "created_at" in result

        # Verify backtest_ids are preserved
        assert result["backtest_ids"] == ["strat1", "strat2"]

        # Verify config is preserved
        assert result["config"]["train_months"] == 12
        assert result["config"]["test_months"] == 3
        assert result["config"]["step_months"] == 1
        assert result["config"]["n_trials"] == 10

        # Verify fold metrics
        assert len(result["folds"]) > 0
        assert "fold" in result["folds"][0]
        assert "is_sharpe" in result["folds"][0]
        assert "oos_sharpe" in result["folds"][0]
        assert "oos_max_drawdown" in result["folds"][0]

    def test_walkforward_output_has_all_required_fields(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that output has all required fields for summary calculation."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 2.0,
                "oos_sharpe": 1.8,
                "oos_max_drawdown": 0.10,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["test_strat"],
            train_months=6,
            test_months=2,
        )

        # Verify summary structure
        assert "summary" in result
        assert "avg_oos_sharpe" in result["summary"]
        assert "n_profitable_folds" in result["summary"]
        assert "profitable_fold_pct" in result["summary"]


class TestWalkforwardSummaryMetrics:
    """Tests for summary metrics calculation."""

    def test_summary_metrics_profitable_fold(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test summary metrics with a profitable fold (positive OOS Sharpe)."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.5,
                "oos_sharpe": 1.2,  # Positive Sharpe
                "oos_max_drawdown": 0.15,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1", "strat2"],
            train_months=12,
            test_months=3,
        )

        # With positive OOS Sharpe, fold should be profitable
        assert result["summary"]["avg_oos_sharpe"] == 1.2
        assert result["summary"]["n_profitable_folds"] == 1
        assert result["summary"]["profitable_fold_pct"] == 100.0

    def test_summary_metrics_unprofitable_fold(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test summary metrics with an unprofitable fold (negative OOS Sharpe)."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.5,
                "oos_sharpe": -0.5,  # Negative Sharpe
                "oos_max_drawdown": 0.30,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1", "strat2"],
            train_months=12,
            test_months=3,
        )

        # With negative OOS Sharpe, fold should not be profitable
        assert result["summary"]["avg_oos_sharpe"] == -0.5
        assert result["summary"]["n_profitable_folds"] == 0
        assert result["summary"]["profitable_fold_pct"] == 0.0

    def test_summary_metrics_zero_oos_sharpe(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test summary metrics with zero OOS Sharpe (boundary case)."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.0,
                "oos_sharpe": 0.0,  # Zero Sharpe - not profitable (need > 0)
                "oos_max_drawdown": 0.20,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1", "strat2"],
            train_months=12,
            test_months=3,
        )

        # Zero is not > 0, so not profitable
        assert result["summary"]["avg_oos_sharpe"] == 0.0
        assert result["summary"]["n_profitable_folds"] == 0
        assert result["summary"]["profitable_fold_pct"] == 0.0

    def test_summary_metrics_null_oos_sharpe(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test summary metrics when OOS Sharpe is None."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.5,
                # No OOS metrics returned
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1", "strat2"],
            train_months=12,
            test_months=3,
        )

        # When oos_sharpe is None, it should be handled gracefully
        assert result["summary"]["avg_oos_sharpe"] is None
        assert result["summary"]["n_profitable_folds"] == 0
        assert result["summary"]["profitable_fold_pct"] == 0.0


class TestWalkforwardFileOutput:
    """Tests for file output verification."""

    def test_walkforward_creates_output_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that walkforward creates a JSON output file."""
        # Create a mock output directory
        output_dir = tmp_path / "walkforward_results"

        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.0,
                "oos_sharpe": 0.8,
                "oos_max_drawdown": 0.15,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["test_strat"],
            train_months=6,
            test_months=2,
            output_dir=output_dir,
        )

        # Verify output directory was created
        assert output_dir.exists()

        # Verify JSON file was created
        json_files = list(output_dir.glob("wf_*.json"))
        assert len(json_files) == 1

        # Verify file contents match returned result
        saved_content = json.loads(json_files[0].read_text())
        assert saved_content["backtest_ids"] == result["backtest_ids"]
        assert saved_content["config"] == result["config"]
        assert saved_content["folds"] == result["folds"]
        assert saved_content["summary"] == result["summary"]

    def test_walkforward_file_naming_format(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that output file uses correct naming format with timestamp."""
        output_dir = tmp_path / "walkforward_results"

        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"sharpe": 1.0, "oos_sharpe": 0.5}

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        wf_module.run_walkforward(
            backtest_ids=["strat1"],
            train_months=12,
            test_months=3,
            output_dir=output_dir,
        )

        json_files = list(output_dir.glob("wf_*.json"))
        assert len(json_files) == 1

        filename = json_files[0].stem
        # Should match pattern: wf_YYYYMMDD_HHMMSS
        assert filename.startswith("wf_")
        # Extract timestamp part - format is YYYYMMDD_HHMMSS (15 chars total)
        ts_part = filename.split("_", 1)[1] if "_" in filename else ""
        assert len(ts_part) == 15  # YYYYMMDD_HHMMSS format (8 + 1 + 6 = 15)


class TestWalkforwardEdgeCases:
    """Tests for edge cases and error handling."""

    def test_walkforward_multiple_backtests(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test walkforward with multiple backtest IDs."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "sharpe": 1.8,
                "oos_sharpe": 1.4,
                "oos_max_drawdown": 0.12,
            }

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        backtest_ids = ["strat_a", "strat_b", "strat_c", "strat_d"]
        result = wf_module.run_walkforward(
            backtest_ids=backtest_ids,
            train_months=12,
            test_months=3,
            n_trials=50,
            output_dir=tmp_path / "walkforward_results",
        )

        assert result["backtest_ids"] == backtest_ids
        assert len(result["folds"]) == 1  # Current implementation has single fold

    def test_walkforward_custom_parameters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test walkforward with custom parameters."""
        captured_kwargs: dict[str, Any] = {}

        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {"sharpe": 1.0, "oos_sharpe": 0.9}

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        wf_module.run_walkforward(
            backtest_ids=["custom_strat"],
            train_months=24,
            test_months=6,
            step_months=2,
            n_trials=200,
            output_dir=tmp_path / "walkforward_results",
        )

        # Verify parameters are passed correctly to optimizer
        assert captured_kwargs["train_months"] == 24
        assert captured_kwargs["test_months"] == 6
        assert captured_kwargs["n_trials"] == 200

    def test_walkforward_created_at_format(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that created_at uses ISO format with timezone."""
        def mock_optimize(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"sharpe": 1.0, "oos_sharpe": 0.7}

        monkeypatch.setattr(wf_module, "optimize_portfolio_allocation", mock_optimize)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1"],
            output_dir=tmp_path / "walkforward_results",
        )

        # Verify ISO format with timezone
        created_at = result["created_at"]
        assert isinstance(created_at, str)
        # Should be parseable as ISO format
        parsed_dt = datetime.fromisoformat(created_at)
        assert parsed_dt.tzinfo is not None  # Should have timezone info


def _fake_result(
    config: BacktestConfig,
    strategy_path: str,
    call_index: int,
    equity_curve: list[EquityPoint],
    stats: dict,
) -> BacktestResult:
    """Shared BacktestResult factory for the fake walk-forward engines."""
    return BacktestResult(
        meta=BacktestMeta(
            id=f"{Path(strategy_path).stem}-{call_index}",
            created_at="2026-01-01T00:00:00+00:00",
            engine="fake",
            strategies=[Path(strategy_path).stem],
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            initial_capital=config.initial_capital,
            data={"symbols": config.symbols, "timeframes": [config.timeframe], "source": "test"},
            parameters=config.parameters,
        ),
        trades=[],
        equity_curve=equity_curve,
        stats=stats,
    )


class _FakeEngine:
    name = "fake"

    def __init__(self) -> None:
        self.configs: list[BacktestConfig] = []

    def validate_config(self, config: BacktestConfig) -> list[str]:
        return []

    def run(self, config: BacktestConfig, strategy_path: str, progress_callback=None) -> BacktestResult:
        self.configs.append(config)
        is_oos = len(self.configs) % 2 == 0
        sharpe = 0.5 if is_oos else 1.5
        drawdown = -0.08 if is_oos else -0.03
        initial = config.initial_capital
        periods = 24
        returns = [0.004, -0.001, 0.003, 0.002] if is_oos else [0.007, -0.002, 0.005, 0.004]
        timestamps = pd.date_range(config.start_date, config.end_date, periods=periods)
        equity = initial
        equities = []
        for index in range(periods):
            if index > 0:
                equity *= 1.0 + returns[index % len(returns)]
            equities.append(equity)
        equity_points = [
            EquityPoint(time=timestamp.isoformat(), equity=equity)
            for timestamp, equity in zip(timestamps, equities, strict=True)
        ]
        return _fake_result(
            config,
            strategy_path,
            len(self.configs),
            equity_points,
            {
                "sharpe_ratio": sharpe,
                "max_drawdown_pct": drawdown,
                "total_trades": 2,
            },
        )


def test_strategy_walkforward_builds_rolling_folds_and_summary(tmp_path: Path) -> None:
    engine = _FakeEngine()
    result = wf_module.run_walkforward(
        strategy="strategies/example.py",
        engine=engine,
        symbols=["SPY"],
        timeframe="1d",
        start="2020-01-01",
        end="2021-10-01",
        train_months=12,
        test_months=3,
        step_months=3,
        output_dir=tmp_path / "walkforward_results",
        params={"window": 10},
    )

    assert result["mode"] == "strategy"
    assert result["summary"]["fold_count"] == 3
    assert result["summary"]["avg_oos_sharpe"] > 0
    assert result["summary"]["worst_oos_drawdown"] >= 0
    assert result["summary"]["profitable_fold_pct"] == 100.0
    assert len(result["folds"]) == 3
    assert len(engine.configs) == 6
    assert engine.configs[0].start_date.startswith("2020-01-01")
    assert engine.configs[1].start_date.startswith("2021-01-01")
    assert result["folds"][0]["train_start"].startswith("2020-01-01")
    assert result["folds"][0]["test_start"].startswith("2021-01-01")
    saved = json.loads(Path(result["output_path"]).read_text(encoding="utf-8"))
    assert saved == result


class _FoldRecordingEngine(_FakeEngine):
    """Like _FakeEngine but returns the caller's stats and no equity curve, so
    the canonical-stats recomputation (F15) has a stale value to override."""

    def __init__(self, *, stats: dict | None = None) -> None:
        super().__init__()
        self.stats = dict(stats or {})

    def run(self, config: BacktestConfig, strategy_path: str, progress_callback=None) -> BacktestResult:
        self.configs.append(config)
        return _fake_result(config, strategy_path, len(self.configs), [], dict(self.stats))


def test_walkforward_passes_correct_kwargs_to_attach_daily_equity(tmp_path, monkeypatch):
    """Regression (F14): attach_daily_equity must be called with start=/end=."""
    captured: dict = {}

    def fake_attach(result, *, start=None, end=None):
        captured["start"] = start
        captured["end"] = end
        return result

    monkeypatch.setattr(wf_module, "attach_daily_equity", fake_attach)
    engine = _FoldRecordingEngine(stats={"sharpe_ratio": 1.0})
    wf_module.run_walkforward(
        strategy="strategies/example.py",
        engine=engine,
        symbols=["SPY"],
        timeframe="1d",
        start="2020-01-01",
        end="2021-01-31",
        train_months=12,
        test_months=1,
        step_months=12,
        output_dir=tmp_path / "wf_out",
    )
    assert len(engine.configs) == 2
    assert captured["start"] == "2021-01-01T00:00:00+00:00"
    assert captured["end"] == "2021-01-31T23:59:59.999999+00:00"


def test_walkforward_prefers_freshly_computed_stats(tmp_path):
    """Regression (F15): canonical stats must not be overridden by stale engine stats."""
    engine = _FoldRecordingEngine(stats={"total_trades": 99})
    result = wf_module.run_walkforward(
        strategy="strategies/example.py",
        engine=engine,
        symbols=["SPY"],
        timeframe="1d",
        start="2020-01-01",
        end="2021-01-31",
        train_months=12,
        test_months=1,
        step_months=12,
        output_dir=tmp_path / "wf_out",
    )
    # The engine fabricates total_trades=99 but returns no trades; the canonical
    # computation must win over the stale engine-provided value.
    assert result["folds"][0]["is_stats"]["total_trades"] == 0
    assert result["folds"][0]["oos_stats"]["total_trades"] == 0
    assert "sharpe_ratio" in result["folds"][0]["is_stats"]
class TestWindowBuilderGuards:
    """Mutation hardening: fold-builder guards and canonical metric keys."""

    def test_empty_window_set_raises(self):
        """A range too short for a single fold must fail loudly.

        Kills the mutant that removed the empty-windows guard: an empty fold
        list would silently produce a payload with fold_count 0.
        """
        from runner.walkforward import _build_windows

        with pytest.raises(ValueError, match="No walk-forward folds"):
            _build_windows(
                start="2024-01-01",
                end="2024-02-01",
                train_months=12,
                test_months=3,
                step_months=1,
            )

    def test_is_sharpe_reads_canonical_sharpe_ratio_key(self):
        """Fold metrics must read the canonical sharpe_ratio first.

        Kills the mutant that reads the non-canonical 'sharpe' alias first:
        when a stats dict carries both, the canonical key must win (F15
        merge-order semantics at the metric level).
        """
        from runner.walkforward import _metric

        stats = {"sharpe_ratio": 1.5, "sharpe": 99.0}
        assert _metric(stats, "sharpe_ratio", "sharpe") == 1.5

"""Tests for walk-forward analysis module."""
from __future__ import annotations

import json
import inspect
from datetime import datetime
from typing import Any

import pytest

from runner import walkforward as wf_module


class TestWalkforwardSignature:
    """Tests for function signature and defaults."""

    def test_walkforward_basic_call(self) -> None:
        """Test that walkforward function can be called and returns expected structure."""
        # Verify function signature
        sig = inspect.signature(wf_module.run_walkforward)
        params = list(sig.parameters.keys())
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

        # Patch DATA_DIR to use our temp directory
        monkeypatch.setattr(wf_module, "DATA_DIR", tmp_path)

        result = wf_module.run_walkforward(
            backtest_ids=["test_strat"],
            train_months=6,
            test_months=2,
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
        monkeypatch.setattr(wf_module, "DATA_DIR", tmp_path)

        wf_module.run_walkforward(
            backtest_ids=["strat1"],
            train_months=12,
            test_months=3,
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
        monkeypatch.setattr(wf_module, "DATA_DIR", tmp_path)

        backtest_ids = ["strat_a", "strat_b", "strat_c", "strat_d"]
        result = wf_module.run_walkforward(
            backtest_ids=backtest_ids,
            train_months=12,
            test_months=3,
            n_trials=50,
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
        monkeypatch.setattr(wf_module, "DATA_DIR", tmp_path)

        wf_module.run_walkforward(
            backtest_ids=["custom_strat"],
            train_months=24,
            test_months=6,
            step_months=2,
            n_trials=200,
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
        monkeypatch.setattr(wf_module, "DATA_DIR", tmp_path)

        result = wf_module.run_walkforward(
            backtest_ids=["strat1"],
        )

        # Verify ISO format with timezone
        created_at = result["created_at"]
        assert isinstance(created_at, str)
        # Should be parseable as ISO format
        parsed_dt = datetime.fromisoformat(created_at)
        assert parsed_dt.tzinfo is not None  # Should have timezone info
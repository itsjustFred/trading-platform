"""Tests for live_runner.preflight."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from live_runner.preflight import (
    LivePreflightResult,
    _validate_deployments,
    run_live_preflight,
)
from shared.models import LiveAccountConfig, LiveDeploymentConfig


class TestPreflightValidator:
    """Tests for _validate_deployments function."""

    def test_valid_deployment_passes(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ]
        errors, warnings = _validate_deployments(accounts, deployments)
        assert errors == []
        assert warnings == []

    def test_deployment_missing_account_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="unknown_acc",
                symbols=["AAPL"],
                timeframe="1h",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("unknown account_id" in e for e in errors)

    def test_deployment_no_symbols_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=[],
                timeframe="1h",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("no symbols configured" in e for e in errors)

    def test_deployment_no_timeframe_fails(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="",
            )
        ]
        errors, _ = _validate_deployments(accounts, deployments)
        assert any("no timeframe configured" in e for e in errors)

    def test_discord_channel_warning(self):
        accounts = [LiveAccountConfig(id="acc1", broker="alpaca")]
        deployments = [
            LiveDeploymentConfig(
                strategy_name="test_strat",
                account_id="acc1",
                symbols=["AAPL"],
                timeframe="1h",
                signal_source="discord",
                signal_config={},
            )
        ]
        _, warnings = _validate_deployments(accounts, deployments)
        assert any("channel_id" in w for w in warnings)


class TestRunLivePreflight:
    """Integration-style tests for run_live_preflight."""

    def test_missing_config_raises_error(self, tmp_path: Path):
        config = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            run_live_preflight(config, check_connectivity=False)

    def test_empty_config_returns_no_accounts(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")
        result = run_live_preflight(config, check_connectivity=False)
        assert result.ok is True
        assert result.accounts == []

    def test_account_without_credentials_fails(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "alpaca"
deployments: []
""")
        result = run_live_preflight(config, check_connectivity=False)
        assert result.ok is False
        assert len(result.accounts) == 1
        assert result.accounts[0].ok is False
        assert any("Missing Alpaca credentials" in e for e in result.accounts[0].errors)

    def test_alpaca_paper_mode_ok(self, tmp_path: Path, monkeypatch):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "alpaca"
deployments: []
""")
        accounts_env = tmp_path / ".accounts.env"
        accounts_env.write_text("""
ALPACA_API_KEY=test_key
ALPACA_SECRET_KEY=test_secret
ALPACA_PAPER=true
""")
        result = run_live_preflight(config, accounts_env_path=accounts_env, check_connectivity=False)
        assert result.ok is True
        assert result.accounts[0].ok is True
        assert result.accounts[0].paper is True

    def test_ctrader_missing_credentials_fails(self, tmp_path: Path):
        config = tmp_path / "live_deployments.yaml"
        config.write_text("""
accounts:
  - id: "acc1"
    broker: "ctrader"
    ctid: "12345"
deployments: []
""")
        # Pass empty env to ensure no credentials are found
        result = run_live_preflight(config, check_connectivity=False, env={})
        assert result.ok is False
        assert result.accounts[0].ok is False
        assert any("Missing cTrader credentials" in e for e in result.accounts[0].errors)


class TestPreflightCLI:
    """Tests for the CLI entry point."""

    def test_main_returns_zero_on_ok(self, tmp_path: Path, monkeypatch):
        from live_runner import preflight as preflight_module

        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")

        with patch.object(preflight_module, "run_live_preflight") as mock_run:
            mock_run.return_value = LivePreflightResult(
                ok=True,
                config_path=str(config),
                accounts_env_path="",
                accounts=[],
            )
            exit_code = preflight_module.main(["--config", str(config)])
            assert exit_code == 0

    def test_main_returns_one_on_fail(self, tmp_path: Path, monkeypatch):
        from live_runner import preflight as preflight_module

        config = tmp_path / "live_deployments.yaml"
        config.write_text("accounts: []\ndeployments: []\n")

        with patch.object(preflight_module, "run_live_preflight") as mock_run:
            mock_run.return_value = LivePreflightResult(
                ok=False,
                config_path=str(config),
                accounts_env_path="",
                accounts=[],
                deployment_errors=["some error"],
            )
            exit_code = preflight_module.main(["--config", str(config)])
            assert exit_code == 1


class TestPreflightResultSerialization:
    """Tests that preflight results serialize to JSON correctly."""

    def test_result_to_jsonable(self):
        from live_runner.preflight import LivePreflightAccountResult, _result_to_jsonable

        account_result = LivePreflightAccountResult(
            account_id="acc1",
            broker="alpaca",
            ok=True,
            paper=True,
            connected=True,
            balance=10000.0,
            equity=10050.0,
            positions_count=2,
            errors=[],
            warnings=["test warning"],
        )
        result = LivePreflightResult(
            ok=True,
            config_path="/tmp/config.yaml",
            accounts_env_path="/tmp/.accounts.env",
            accounts=[account_result],
        )
        jsonable = _result_to_jsonable(result)
        assert jsonable["ok"] is True
        assert len(jsonable["accounts"]) == 1
        assert jsonable["accounts"][0]["broker"] == "alpaca"
        assert jsonable["accounts"][0]["warnings"] == ["test warning"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

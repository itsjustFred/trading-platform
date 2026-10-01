from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from live_runner.config_parser import parse_live_deployments_config


def test_parse_live_deployments_yaml_valid(tmp_path: Path):
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text(
        """
accounts:
  - id: alpaca-paper
    broker: alpaca

deployments:
  - strategy_name: my_strategy
    account_id: alpaca-paper
    symbols: [AAPL, MSFT]
    timeframe: 1h
""".strip()
        + "\n",
        encoding="utf-8",
    )

    accounts, deployments = parse_live_deployments_config(config_path)

    assert len(accounts) == 1
    assert accounts[0].id == "alpaca-paper"
    assert accounts[0].broker == "alpaca"

    assert len(deployments) == 1
    assert deployments[0].strategy_name == "my_strategy"
    assert deployments[0].account_id == "alpaca-paper"
    assert deployments[0].symbols == ["AAPL", "MSFT"]
    assert deployments[0].timeframe == "1h"


def test_a_missing_config_file_is_a_startup_error(tmp_path: Path):
    """A config path that does not exist must raise, not parse as an empty platform.

    `_validated_config_file` raises `FileNotFoundError` before any reading happens.
    Removing that check leaves every other test green, because they all write a real
    file first - so the case of a *typo in the configured path* is untested.

    This is the shape of the failure that matters at start-up: the daemon is pointed
    at `live_deployments.production.yaml`, the name is wrong, and without the check
    `yaml.safe_load` is never reached but the caller gets whatever a missing read
    produces instead of a clear "not found". An operator seeing an empty account list
    has no way to tell a typo from a genuinely empty config.

    A directory is included too: `is_file()` is false for it, so it must not be read
    either.
    """
    missing = tmp_path / "live_deployments.production.yaml"
    assert not missing.exists()

    with pytest.raises(FileNotFoundError, match="not found"):
        parse_live_deployments_config(missing)

    # A directory is not a config file either.
    as_directory = tmp_path / "config-dir"
    as_directory.mkdir()
    with pytest.raises(FileNotFoundError, match="not found"):
        parse_live_deployments_config(as_directory)

    # And the message names the path that was asked for, so the typo is visible.
    with pytest.raises(FileNotFoundError) as excinfo:
        parse_live_deployments_config(missing)
    assert "live_deployments.production.yaml" in str(excinfo.value), (
        "the error does not name the missing path: " + str(excinfo.value)
    )


def test_parse_live_deployments_warns_for_missing_account(tmp_path: Path):
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text(
        """
accounts:
  - id: ctrader-live
    broker: ctrader
    ctid: "12345"

deployments:
  - strategy_name: orphan_strategy
    account_id: unknown-account
    symbols: [EURUSD]
    timeframe: 15m
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with patch("live_runner.config_parser.logger.warning") as warning_mock:
        _, deployments = parse_live_deployments_config(config_path)

    assert len(deployments) == 1
    warning_mock.assert_called_once()
    _, kwargs = warning_mock.call_args
    assert kwargs["extra"]["data"]["account_id"] == "unknown-account"

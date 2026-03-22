from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


def test_parse_live_deployments_warns_for_missing_account(tmp_path: Path):
    config_path = tmp_path / "live_deployments.yaml"
    config_path.write_text(
        """
accounts:
  - id: ctrader-live
    broker: ctrader

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

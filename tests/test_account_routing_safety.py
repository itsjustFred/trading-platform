from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import live_runner.daemon as daemon
from live_runner.config_parser import parse_live_deployments_config
from shared.models import LiveAccountConfig, LiveDeploymentConfig


def _write_config(path: Path, content: str) -> Path:
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def test_live_config_rejects_duplicate_internal_account_ids(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: shared-account
    broker: alpaca
  - id: shared-account
    broker: ctrader
    ctid: "12345"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="duplicate account id.*shared-account"):
        parse_live_deployments_config(config_path)


def test_live_config_rejects_duplicate_ctrader_broker_identifier(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: strategy-a-demo
    broker: ctrader
    ctid: "12345"
  - id: strategy-b-demo
    broker: ctrader
    ctid: "12345"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="cTrader broker account.*12345"):
        parse_live_deployments_config(config_path)


def test_live_config_rejects_numerically_equivalent_ctrader_broker_identifier(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: strategy-a-demo
    broker: ctrader
    ctid: "00123"
  - id: strategy-b-demo
    broker: ctrader
    ctid: "123"

deployments: []
        """,
    )

    with pytest.raises(ValueError, match="cTrader broker account.*123"):
        parse_live_deployments_config(config_path)


def test_live_config_allows_distinct_identifiers_within_one_ctrader_account(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "live_deployments.yaml",
        """
accounts:
  - id: local-strategy-account
    broker: ctrader
    broker_login: "67890"
    ctid: "12345"

deployments: []
        """,
    )

    accounts, deployments = parse_live_deployments_config(config_path)

    assert deployments == []
    assert accounts == [
        LiveAccountConfig(
            id="local-strategy-account",
            broker="ctrader",
            broker_login="67890",
            ctid="12345",
        )
    ]


def test_daemon_fails_closed_before_adapter_creation_for_duplicate_ctrader_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [
        LiveAccountConfig(id="strategy-a-demo", broker="ctrader", ctid="12345"),
        LiveAccountConfig(id="strategy-b-demo", broker="ctrader", ctid="12345"),
    ]
    deployments = [
        LiveDeploymentConfig(
            strategy_name="fx_breakout",
            account_id="strategy-a-demo",
            symbols=["EURUSD"],
            timeframe="15m",
        )
    ]
    adapter_creations: list[str] = []

    async def _load_state():
        return accounts, deployments

    def _create_adapter(account, **_kwargs):
        adapter_creations.append(account.id)
        return None

    async def _stop_after_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_load_db_accounts_and_deployments", _load_state)
    monkeypatch.setattr(daemon, "_create_adapter_for_account", _create_adapter)
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            daemon.sync_deployments_loop(
                {},
                {},
                {},
                accounts_env=Path("/nonexistent/accounts.env"),
                interval_seconds=0,
            )
        )

    assert adapter_creations == []

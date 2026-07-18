from __future__ import annotations

from pathlib import Path

import yaml

from live_runner.account_routing import validate_live_account_routes
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveDeploymentConfig

logger = get_logger("live-runner")


def parse_live_deployments_config(
    config_path: str | Path,
) -> tuple[list[LiveAccountConfig], list[LiveDeploymentConfig]]:
    path = Path(config_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(
            "Live deployment config must be a YAML mapping with 'accounts' and 'deployments'."
        )

    raw_accounts = payload.get("accounts", [])
    raw_deployments = payload.get("deployments", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("Live deployment config field 'accounts' must be a list.")
    if not isinstance(raw_deployments, list):
        raise ValueError("Live deployment config field 'deployments' must be a list.")

    accounts = [LiveAccountConfig.model_validate(item) for item in raw_accounts]
    deployments = [LiveDeploymentConfig.model_validate(item) for item in raw_deployments]
    validate_live_account_routes(accounts)

    account_ids = {account.id for account in accounts}
    for deployment in deployments:
        if deployment.account_id not in account_ids:
            logger.warning(
                "Live deployment references unknown account_id",
                extra={
                    "data": {
                        "strategy_name": deployment.strategy_name,
                        "account_id": deployment.account_id,
                    }
                },
            )

    return accounts, deployments

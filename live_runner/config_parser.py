from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from live_runner.account_routing import validate_live_account_routes
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveDeploymentConfig
from shared.trailing import validate_live_deployment_trailing_override

logger = get_logger("live-runner")

_UNEXPANDED_ENV_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _expand_env_value(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR``/``${VAR}`` in *value* using *env* (no silent mute)."""
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return str(env.get(key, match.group(0)))

    return pattern.sub(_replace, value)


def expand_env_mapping(payload: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively expand env references in a parsed config mapping (#81).

    Uses *env* (file-first overlay when the caller passes accounts-env
    values), falling back to ``os.environ``.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    if isinstance(payload, dict):
        return {key: expand_env_mapping(item, source) for key, item in payload.items()}
    if isinstance(payload, list):
        return [expand_env_mapping(item, source) for item in payload]
    if isinstance(payload, str):
        return _expand_env_value(payload, source)
    return payload


def assert_no_unexpanded_env(payload: Any, *, context: str = "live deployment config") -> None:
    """Fail closed on unexpanded ``${VAR}`` references (#81).

    A literal ``"${DISCORD_BOT_X_CHANNEL_ID}"`` never matches a real channel
    — that is a startup error, never a silent mute.
    """
    leftovers: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            for match in _UNEXPANDED_ENV_PATTERN.findall(node):
                leftovers.append(match)

    _walk(payload)
    if leftovers:
        raise ValueError(
            f"Unexpanded environment reference(s) {sorted(set(leftovers))} in "
            f"{context}; set them via environment or .accounts.env instead of "
            "shipping a literal '${...}' channel id."
        )


def _validated_config_file(config_path: str | Path) -> Path:
    path = Path(config_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Live deployment config not found: {config_path}")
    return path


def parse_live_deployments_config(
    config_path: str | Path,
    *,
    env_values: Mapping[str, str] | None = None,
) -> tuple[list[LiveAccountConfig], list[LiveDeploymentConfig]]:
    path = _validated_config_file(config_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # #81: env expansion at parse time (centrally here); unexpanded refs
    # are a startup error, never a silent mute.
    payload = expand_env_mapping(payload, env_values)
    assert_no_unexpanded_env(payload)
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

    account_brokers = {account.id: account.broker for account in accounts}
    account_ids = set(account_brokers)
    for deployment in deployments:
        # #150/#167 fail closed: live trailing support is per broker. An
        # unknown account has no broker, which supports mode "none" only.
        validate_live_deployment_trailing_override(
            deployment.signal_config,
            context=f"live deployment '{deployment.strategy_name}'",
            broker=account_brokers.get(deployment.account_id),
        )
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

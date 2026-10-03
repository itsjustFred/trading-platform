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

logger = get_logger("live-runner")

_UNEXPANDED_ENV_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
# The existing pattern insists on the closing brace, so an *unclosed* reference is invisible to
# it. Measured: `${FOO` survived expansion, passed this guard, and delivered its literal to a
# Discord channel_id - which is the silent mute #81 exists to prevent.
#
# The scope is deliberately narrow. `${}`, `${1BAD}` and `${9}` are also malformed, but they were
# recorded as ambiguous - a config could plausibly contain them as text - and that decision is
# left alone. An unclosed brace has no such reading: there is no text that is `${FOO`. Requiring
# the identifier run to end at a boundary keeps a hyphen out of it too, so `${VAR-NAME}` is still
# not this pattern's business.
#
# Bare `$NAME` remains the separate, still-open question (backlog item 4), because `$USD` and
# `$SPX` collide with real currency codes and tickers.
_UNCLOSED_ENV_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?=$|[^\w}-])")


def _expand_env_value(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR``/``${VAR}`` in *value* using *env*, and say when it could not.

    The expansion behaviour is deliberately unchanged - an unresolved reference is left in
    place exactly as before - but it is now **recorded** instead of silent. That matters
    because the value left behind is the failure mode this exists for: a channel id of
    ``${DISCORD_BOT_X_CHANNEL_ID}`` matches no channel, and the notification is dropped with
    nothing anywhere saying the reference never resolved.

    Note what this deliberately does *not* do: it does not refuse the value. An unresolved
    ``$UNSET`` and a deliberate literal ``$USD`` in free text both survive as the same string,
    so nothing downstream can tell them apart - not this function, and not a report of which
    keys failed to resolve. Refusing either would break a legitimate config; accepting both is
    why the reference is logged rather than rejected.

    Resolution is tested with ``key in env`` rather than truthiness, so a variable that is
    set-but-empty is *resolved* (to "") and is not reported as missing.
    """
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        if key not in env:
            unresolved.append(key)
            return match.group(0)
        return str(env[key])

    expanded = pattern.sub(_replace, value)
    if unresolved:
        # One line per value, not per occurrence: a config that repeats a reference should
        # not produce a log line per repetition.
        logger.warning(
            "config reference(s) not present in the environment and left unexpanded: %s",
            ", ".join(sorted(set(unresolved))),
        )
    return expanded


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
    malformed: list[str] = []

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
            for match in _UNCLOSED_ENV_PATTERN.findall(node):
                malformed.append(match)

    _walk(payload)
    if malformed:
        raise ValueError(
            f"Unclosed environment reference(s) {sorted(set(malformed))} in {context}: a '$' "
            "followed by '{' is always meant to be a reference, and this one has no closing "
            "brace. Add the '}' rather than setting a variable - nothing was expanded."
        )
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

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from live_runner.alpaca_adapter import resolve_alpaca_credentials
from live_runner.config_parser import parse_live_deployments_config
from live_runner.ctrader_adapter import DEFAULT_ACCOUNTS_ENV
from shared.models import LiveAccountConfig, LiveDeploymentConfig


@dataclass(frozen=True)
class LivePreflightAccountResult:
    account_id: str
    broker: str
    ok: bool
    paper: bool | None = None
    demo: bool | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LivePreflightResult:
    ok: bool
    config_path: str
    accounts_env_path: str
    accounts: list[LivePreflightAccountResult]
    deployment_errors: list[str] = field(default_factory=list)
    deployment_warnings: list[str] = field(default_factory=list)


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {
        str(key): str(value).strip()
        for key, value in values.items()
        if key and isinstance(value, str)
    }


def _parse_bool_env(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_ctrader_credentials(
    env_values: Mapping[str, str],
) -> tuple[str, str, str, str, bool] | None:
    app_id = str(env_values.get("CTRADER_APP_ID", "")).strip()
    app_secret = str(env_values.get("CTRADER_APP_SECRET", "")).strip()
    access_token = str(env_values.get("CTRADER_ACCESS_TOKEN", "")).strip()
    refresh_token = str(env_values.get("CTRADER_REFRESH_TOKEN", "")).strip()
    if not app_id or not app_secret or not access_token:
        return None
    demo = _parse_bool_env(env_values.get("CTRADER_DEMO"), default=True)
    return app_id, app_secret, access_token, refresh_token, demo


def _preflight_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
) -> LivePreflightAccountResult:
    errors: list[str] = []
    warnings: list[str] = []
    paper: bool | None = None
    demo: bool | None = None

    if account.broker == "alpaca":
        credentials = resolve_alpaca_credentials(
            account_id=account.id,
            broker_login=account.broker_login,
            env=env_values,
        )
        if credentials is None:
            errors.append(
                "Missing Alpaca credentials for account. Configure default "
                "ALPACA_API_KEY/ALPACA_SECRET_KEY or a matching ACC{N} block."
            )
        else:
            _, _, paper = credentials
            if not paper:
                warnings.append("Alpaca account resolves to live mode, not paper mode.")

    elif account.broker == "ctrader":
        credentials = _resolve_ctrader_credentials(env_values)
        if credentials is None:
            errors.append(
                "Missing cTrader credentials. Configure CTRADER_APP_ID, "
                "CTRADER_APP_SECRET, and CTRADER_ACCESS_TOKEN."
            )
        else:
            _, _, _, refresh_token, demo = credentials
            if not demo:
                warnings.append("cTrader account resolves to live mode, not demo mode.")
            if not refresh_token:
                warnings.append("CTRADER_REFRESH_TOKEN is missing; token refresh cannot be verified.")
        account_identifier = str(account.ctid or account.broker_login or account.id).strip()
        try:
            if int(account_identifier) <= 0:
                errors.append(
                    "cTrader account id must be a positive numeric cTID trader account id."
                )
        except ValueError:
            errors.append("cTrader account id must be numeric via ctid, broker_login, or id.")

    return LivePreflightAccountResult(
        account_id=account.id,
        broker=account.broker,
        ok=not errors,
        paper=paper,
        demo=demo,
        errors=errors,
        warnings=warnings,
    )


def _validate_deployments(
    accounts: Sequence[LiveAccountConfig],
    deployments: Sequence[LiveDeploymentConfig],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    account_ids = {account.id for account in accounts}

    for deployment in deployments:
        label = f"{deployment.strategy_name} ({deployment.account_id})"
        if deployment.account_id not in account_ids:
            errors.append(f"{label}: references unknown account_id.")
        if not deployment.symbols:
            errors.append(f"{label}: has no symbols configured.")
        if not str(deployment.timeframe).strip():
            errors.append(f"{label}: has no timeframe configured.")
        if deployment.signal_source == "discord":
            channel_id = str(deployment.signal_config.get("channel_id") or "").strip()
            if not channel_id:
                warnings.append(f"{label}: Discord deployment has no channel_id in signal_config.")

    return errors, warnings


def run_live_preflight(
    config_path: str | Path,
    *,
    accounts_env_path: str | Path = DEFAULT_ACCOUNTS_ENV,
    env: Mapping[str, str] | None = None,
) -> LivePreflightResult:
    config = Path(config_path)
    accounts_env = Path(accounts_env_path)
    accounts, deployments = parse_live_deployments_config(config)
    env_values = dict(env) if env is not None else _load_env_file(accounts_env)

    account_results = [
        _preflight_account(account, env_values=env_values)
        for account in accounts
    ]
    deployment_errors, deployment_warnings = _validate_deployments(accounts, deployments)

    ok = all(result.ok for result in account_results) and not deployment_errors
    return LivePreflightResult(
        ok=ok,
        config_path=str(config),
        accounts_env_path=str(accounts_env),
        accounts=account_results,
        deployment_errors=deployment_errors,
        deployment_warnings=deployment_warnings,
    )


def _result_to_jsonable(result: LivePreflightResult) -> dict[str, Any]:
    return asdict(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run non-ordering live trading preflight checks.")
    parser.add_argument(
        "--config",
        default="live_deployments.yaml",
        help="Path to live deployments YAML.",
    )
    parser.add_argument(
        "--accounts-env",
        default=str(DEFAULT_ACCOUNTS_ENV),
        help="Path to .accounts.env-style credential file.",
    )
    args = parser.parse_args(argv)

    result = run_live_preflight(args.config, accounts_env_path=args.accounts_env)
    print(json.dumps(_result_to_jsonable(result), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

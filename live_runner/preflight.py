from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from live_runner.account_routing import ctrader_account_identifier
from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from live_runner.config_parser import parse_live_deployments_config
from live_runner.ctrader_adapter import DEFAULT_ACCOUNTS_ENV, CTraderLiveAdapter
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveDeploymentConfig

logger = get_logger("live-runner")


@dataclass(frozen=True)
class LivePreflightAccountResult:
    account_id: str
    broker: str
    ok: bool
    paper: bool | None = None
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Upper-case instrument names the broker reports for this account, when
    # the broker was asked. None means "not checked" - which is not the
    # same as "everything is tradeable", and callers must not read it so.
    tradeable_symbols: set[str] | None = None


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


_TRUE_BOOL_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_BOOL_TOKENS = frozenset({"0", "false", "no", "n", "off"})


def _parse_bool_env(name: str, value: str | None, *, default: bool) -> bool:
    """Strictly parse a boolean env var.

    Missing value -> default. Recognized true/false tokens (case-insensitive,
    stripped) parse accordingly. Anything else raises so a malformed value can
    never implicitly select live trading.
    """
    if value is None:
        return default
    token = value.strip().lower()
    if token in _TRUE_BOOL_TOKENS:
        return True
    if token in _FALSE_BOOL_TOKENS:
        return False
    valid = ", ".join(sorted(_TRUE_BOOL_TOKENS | _FALSE_BOOL_TOKENS))
    raise ValueError(
        f"Invalid boolean value for {name}: {value!r}. Valid values are: {valid}."
    )


def _resolve_ctrader_credentials(
    env_values: Mapping[str, str],
) -> tuple[str, str, str, str, bool] | None:
    app_id = str(env_values.get("CTRADER_APP_ID", "")).strip()
    app_secret = str(env_values.get("CTRADER_APP_SECRET", "")).strip()
    access_token = str(env_values.get("CTRADER_ACCESS_TOKEN", "")).strip()
    refresh_token = str(env_values.get("CTRADER_REFRESH_TOKEN", "")).strip()
    if not app_id or not app_secret or not access_token:
        return None
    demo = _parse_bool_env("CTRADER_DEMO", env_values.get("CTRADER_DEMO"), default=True)
    return app_id, app_secret, access_token, refresh_token, demo


def _run_broker_connectivity_check(
    adapter: AlpacaLiveAdapter | CTraderLiveAdapter,
    *,
    broker_label: str,
    errors: list[str],
    warnings: list[str] | None = None,
) -> tuple[bool, float | None, float | None, int | None, set[str] | None]:
    """Connect once, read everything the preflight needs, then disconnect.

    The tradeable-symbol read used to be a second `asyncio.run` *after* the
    connectivity check had already disconnected, so each account cost a second
    connect/disconnect round-trip and the same broker client was driven from two
    different event loops. Reading it here, before the `finally` disconnects, reuses
    the connection that was just proven and the loop that was just used.

    The symbol read stays best-effort: it is wrapped separately so that an unreadable
    symbol list reports a warning and leaves the result as None ("not checked") rather
    than failing a preflight that already established the account connects.
    """
    connected = False
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    tradeable: set[str] | None = None

    async def _check() -> tuple[float | None, float | None, int, set[str] | None]:
        nonlocal connected, tradeable
        try:
            await adapter.connect()
            fetched_balance, fetched_equity = await adapter.get_balance()
            positions = await adapter.get_positions()
            connected = True
            # Only cTrader has a *per-account* tradeable-symbol list, so only
            # cTrader defines the method. Calling it unconditionally meant every
            # Alpaca preflight raised AttributeError here - and because the Alpaca
            # caller passes no `warnings` list, that error was discarded and the
            # result stayed None with nothing recorded. A guaranteed failure that
            # reports nothing is worse than not attempting the read, and it made
            # the docstring's "leaves the result as None" claim true by accident
            # rather than by design.
            symbol_reader = getattr(adapter, "tradeable_symbol_names", None)
            if symbol_reader is None:
                if warnings is not None:
                    warnings.append(
                        f"{adapter.__class__.__name__} does not expose a per-account "
                        "tradeable symbol list; symbols were not checked"
                    )
            else:
                try:
                    tradeable = set(await symbol_reader())
                except Exception as exc:
                    if warnings is not None:
                        warnings.append(
                            f"Could not read the account's tradeable symbol list: {exc!r}"
                        )
            return fetched_balance, fetched_equity, len(positions), tradeable
        finally:
            with contextlib.suppress(Exception):
                await adapter.disconnect()

    try:
        balance, equity, positions_count, tradeable = asyncio.run(_check())
    except Exception as exc:
        errors.append(f"{broker_label} connectivity check failed: {type(exc).__name__}: {exc}")
    return connected, balance, equity, positions_count, tradeable


def _preflight_alpaca_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    errors: list[str],
    warnings: list[str],
) -> tuple[bool | None, bool | None, float | None, float | None, int | None]:
    credentials = resolve_alpaca_credentials(
        account_id=account.id,
        broker_login=account.broker_login,
        env=env_values,
    )
    paper: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    if credentials is None:
        errors.append(
            "Missing Alpaca credentials for account. Configure default "
            "ALPACA_API_KEY/ALPACA_SECRET_KEY or a matching ACC{N} block."
        )
    else:
        api_key, secret_key, paper = credentials
        if not paper:
            warnings.append("Alpaca account resolves to live mode, not paper mode.")
        if check_connectivity:
            adapter = AlpacaLiveAdapter(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper,
                account_id=account.id,
            )
            (
                connected,
                balance,
                equity,
                positions_count,
                _tradeable_symbols,
            ) = _run_broker_connectivity_check(
                adapter,
                broker_label="Alpaca",
                errors=errors,
            )
    return paper, connected, balance, equity, positions_count


def _ctrader_preflight_credentials(
    env_values: Mapping[str, str],
    errors: list[str],
) -> tuple[str, str, str, str, bool] | None:
    try:
        return _resolve_ctrader_credentials(env_values)
    except ValueError as exc:
        errors.append(str(exc))
        return None


def _validate_ctrader_account_identifier(account: LiveAccountConfig, errors: list[str]) -> str:
    account_identifier = ctrader_account_identifier(account)
    try:
        if int(account_identifier) <= 0:
            errors.append(
                "cTrader account id must be a positive numeric cTID trader account id."
            )
    except ValueError:
        errors.append("cTrader account id must be numeric via ctid, broker_login, or id.")
    return account_identifier


def _preflight_ctrader_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    accounts_env_path: Path,
    errors: list[str],
    warnings: list[str],
) -> tuple[
    bool | None, bool | None, float | None, float | None, int | None, set[str] | None
]:
    credentials = _ctrader_preflight_credentials(env_values, errors)
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    tradeable_symbols: set[str] | None = None
    if credentials is None:
        if not errors:
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
    account_identifier = _validate_ctrader_account_identifier(account, errors)
    if check_connectivity and credentials is not None and not errors:
        app_id, app_secret, access_token, refresh_token, demo = credentials
        logger.info(
            "cTrader preflight using accounts file for credential read/refresh",
            extra={
                "data": {
                    "account_id": account.id,
                    "accounts_env_path": str(accounts_env_path),
                    "refresh_target_path": str(accounts_env_path),
                }
            },
        )
        adapter = CTraderLiveAdapter(
            app_id=app_id,
            app_secret=app_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_identifier,
            demo=demo,
            account_config_id=account.id,
            accounts_env_path=accounts_env_path,
        )
        (
            connected,
            balance,
            equity,
            positions_count,
            tradeable_symbols,
        ) = _run_broker_connectivity_check(
            adapter,
            broker_label="cTrader",
            errors=errors,
            warnings=warnings,
        )
        # Best effort on purpose, and handled inside the check: an unreadable symbols
        # list must not fail a preflight that already proved the account connects.
        # None is carried through as "not checked", so the deployment check below
        # stays quiet rather than guessing.
    return (
        demo,
        connected,
        balance,
        equity,
        positions_count,
        tradeable_symbols,
    )


def _preflight_account(
    account: LiveAccountConfig,
    *,
    env_values: Mapping[str, str],
    check_connectivity: bool,
    accounts_env_path: Path,
) -> LivePreflightAccountResult:
    errors: list[str] = []
    warnings: list[str] = []
    paper: bool | None = None
    demo: bool | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None
    positions_count: int | None = None
    tradeable_symbols: set[str] | None = None

    if account.broker == "alpaca":
        (
            paper,
            connected,
            balance,
            equity,
            positions_count,
        ) = _preflight_alpaca_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            errors=errors,
            warnings=warnings,
        )

    elif account.broker == "ctrader":
        (
            demo,
            connected,
            balance,
            equity,
            positions_count,
            tradeable_symbols,
        ) = _preflight_ctrader_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            accounts_env_path=accounts_env_path,
            errors=errors,
            warnings=warnings,
        )

    return LivePreflightAccountResult(
        account_id=account.id,
        broker=account.broker,
        ok=not errors,
        paper=paper,
        demo=demo,
        connected=connected,
        balance=balance,
        equity=equity,
        positions_count=positions_count,
        errors=errors,
        warnings=warnings,
        tradeable_symbols=tradeable_symbols,
    )


def _validate_deployments(
    accounts: Sequence[LiveAccountConfig],
    deployments: Sequence[LiveDeploymentConfig],
    account_results: Sequence[LivePreflightAccountResult] = (),
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    account_ids = {account.id for account in accounts}
    tradeable_by_account = {
        result.account_id: result.tradeable_symbols
        for result in account_results
        if result.tradeable_symbols is not None
    }

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
        # A symbol the account cannot trade is not an error: cTrader resolves
        # ids per account and the order is refused at submit time anyway. It
        # is a warning because the failure is otherwise invisible until a
        # signal arrives, at which point every order for that symbol is
        # rejected with nothing to explain why. With no broker answer (None)
        # nothing is claimed.
        tradeable = tradeable_by_account.get(deployment.account_id)
        if tradeable is not None:
            unavailable = sorted(
                {
                    str(symbol).strip().upper()
                    for symbol in deployment.symbols
                    if str(symbol).strip().upper() not in tradeable
                }
            )
            if unavailable:
                warnings.append(
                    f"{label}: account cannot trade {', '.join(unavailable)}; "
                    "every order for those symbols would be refused at submit"
                )

    return errors, warnings


def run_live_preflight(
    config_path: str | Path,
    *,
    accounts_env_path: str | Path = DEFAULT_ACCOUNTS_ENV,
    env: Mapping[str, str] | None = None,
    check_connectivity: bool = False,
) -> LivePreflightResult:
    config = Path(config_path)
    accounts_env = Path(accounts_env_path)
    try:
        accounts, deployments = parse_live_deployments_config(config)
    except ValueError as exc:
        # B3: invalid account definitions (e.g. non-routable ctrader ids)
        # report as a failed preflight, never as an unhandled crash.
        logger.warning(
            "Live deployment config invalid",
            extra={"data": {"config_path": str(config), "error": str(exc)}},
        )
        return LivePreflightResult(
            ok=False,
            config_path=str(config),
            accounts_env_path=str(accounts_env),
            accounts=[],
            deployment_errors=[str(exc)],
        )
    logger.info(
        "Running live preflight",
        extra={
            "data": {
                "config_path": str(config),
                "accounts_env_path": str(accounts_env),
                "check_connectivity": check_connectivity,
            }
        },
    )
    env_values = dict(env) if env is not None else _load_env_file(accounts_env)

    account_results = [
        _preflight_account(
            account,
            env_values=env_values,
            check_connectivity=check_connectivity,
            accounts_env_path=accounts_env,
        )
        for account in accounts
    ]
    deployment_errors, deployment_warnings = _validate_deployments(
        accounts, deployments, account_results
    )

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
    parser.add_argument(
        "--check-connectivity",
        action="store_true",
        help="Connect to brokers and read balance/positions without placing orders.",
    )
    args = parser.parse_args(argv)

    result = run_live_preflight(
        args.config,
        accounts_env_path=args.accounts_env,
        check_connectivity=args.check_connectivity,
    )
    print(json.dumps(_result_to_jsonable(result), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

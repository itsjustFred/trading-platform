from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.base_adapter import BaseLiveAdapter
from live_runner.ctrader_adapter import CTraderLiveAdapter
from shared.database import list_live_accounts, list_live_deployments, save_live_balance
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveBalanceRecord, LiveDeploymentConfig

logger = get_logger("live-runner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
BALANCE_POLL_INTERVAL_SECONDS = 60
DEPLOYMENT_POLL_INTERVAL_SECONDS = 10
_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")


def _parse_bool_env(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_accounts_env_values(accounts_env: Path) -> dict[str, str]:
    loaded = dotenv_values(accounts_env)
    result: dict[str, str] = {}
    for key, value in loaded.items():
        if key and isinstance(value, str):
            result[key] = value.strip()
    return result


def _resolve_alpaca_credentials(
    account: LiveAccountConfig, env_values: Mapping[str, str]
) -> tuple[str, str, bool] | None:
    default_api_key = str(env_values.get("ALPACA_API_KEY", "")).strip()
    default_secret_key = str(env_values.get("ALPACA_SECRET_KEY", "")).strip()
    if default_api_key and default_secret_key:
        paper = _parse_bool_env(str(env_values.get("ALPACA_PAPER", "")).strip(), default=True)
        return default_api_key, default_secret_key, paper

    indexes = sorted(
        {
            int(match.group("index"))
            for key in env_values
            if (match := _ALPACA_ACC_RE.match(str(key)))
        }
    )
    if not indexes:
        return None

    blocks: list[dict[str, str | bool]] = []
    for idx in indexes:
        api_key = str(env_values.get(f"ALPACA_API_KEY_ACC{idx}", "")).strip()
        secret_key = str(env_values.get(f"ALPACA_SECRET_KEY_ACC{idx}", "")).strip()
        if not api_key or not secret_key:
            continue

        endpoint = str(env_values.get(f"ALPACA_ENDPOINT_ACC{idx}", "")).strip().lower()
        paper = "paper" in endpoint if endpoint else _parse_bool_env(
            str(env_values.get(f"ALPACA_PAPER_ACC{idx}", "")).strip(),
            default=True,
        )
        blocks.append(
            {
                "api_key": api_key,
                "secret_key": secret_key,
                "paper": paper,
                "account_id_hint": str(env_values.get(f"ALPACA_ACCOUNT_ID_ACC{idx}", "")).strip(),
                "login_hint": (
                    str(env_values.get(f"ALPACA_BROKER_LOGIN_ACC{idx}", "")).strip()
                    or str(env_values.get(f"ALPACA_ACCOUNT_NUMBER_ACC{idx}", "")).strip()
                ),
            }
        )

    if not blocks:
        return None

    normalized_account_id = account.id.strip().lower()
    normalized_login = str(account.broker_login or "").strip().lower()
    for block in blocks:
        account_id_hint = str(block.get("account_id_hint", "")).strip().lower()
        if account_id_hint and account_id_hint == normalized_account_id:
            return (
                str(block["api_key"]),
                str(block["secret_key"]),
                bool(block["paper"]),
            )
        login_hint = str(block.get("login_hint", "")).strip().lower()
        if login_hint and normalized_login and login_hint == normalized_login:
            return (
                str(block["api_key"]),
                str(block["secret_key"]),
                bool(block["paper"]),
            )

    if len(blocks) > 1:
        logger.warning(
            "Multiple Alpaca credential blocks found; falling back to first block",
            extra={"data": {"account_id": account.id}},
        )

    first = blocks[0]
    return (
        str(first["api_key"]),
        str(first["secret_key"]),
        bool(first["paper"]),
    )


def _create_adapter_for_account(
    account: LiveAccountConfig, *, accounts_env_values: Mapping[str, str] | None = None
) -> BaseLiveAdapter | None:
    env_values = accounts_env_values or os.environ
    if account.broker == "alpaca":
        credentials = _resolve_alpaca_credentials(account, env_values)
        if credentials is None:
            logger.warning(
                "Missing Alpaca credentials for account",
                extra={"data": {"account_id": account.id}},
            )
            return None
        api_key, secret_key, paper = credentials
        try:
            return AlpacaLiveAdapter(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper,
                account_id=account.id,
            )
        except Exception as exc:
            logger.warning(
                "Could not initialize alpaca adapter",
                extra={"data": {"account_id": account.id, "error": str(exc)}},
            )
            return None
    if account.broker == "ctrader":
        try:
            return CTraderLiveAdapter(account_config_id=account.id)
        except Exception as exc:
            logger.warning(
                "Could not initialize ctrader adapter",
                extra={"data": {"account_id": account.id, "error": str(exc)}},
            )
            return None

    logger.warning(
        "Broker not implemented in daemon",
        extra={"data": {"account_id": account.id, "broker": account.broker}},
    )
    return None


async def _run_adapter(account_id: str, adapter: BaseLiveAdapter) -> None:
    logger.info(
        "Starting adapter execution listener",
        extra={
            "data": {
                "account_id": account_id,
                "adapter": type(adapter).__name__,
            }
        },
    )
    await adapter.listen_executions()


async def _poll_balances(
    adapters: dict[str, BaseLiveAdapter], *, interval_seconds: int = BALANCE_POLL_INTERVAL_SECONDS
) -> None:
    while True:
        snapshot_time = datetime.now(timezone.utc)
        logger.debug(
            "Polling balances for adapters",
            extra={"data": {"adapter_count": len(adapters)}},
        )
        for account_id, adapter in list(adapters.items()):
            try:
                balance, equity = await adapter.get_balance()
                record = LiveBalanceRecord(
                    account_id=account_id,
                    timestamp=snapshot_time,
                    balance=float(balance),
                    equity=float(equity),
                )
                await asyncio.to_thread(save_live_balance, record)
                logger.debug(
                    "Saved live balance snapshot",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "balance": float(balance),
                            "equity": float(equity),
                            "snapshot_time": snapshot_time.isoformat(),
                        }
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Live balance polling failed",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "adapter": type(adapter).__name__,
                            "error": str(exc),
                        }
                    },
                )
        await asyncio.sleep(interval_seconds)


def _deployment_task_name(account_id: str, kind: str) -> str:
    return f"deployment-{kind}-{account_id}"


def _cleanup_finished_listener_tasks(active_tasks: dict[str, asyncio.Task[None]]) -> None:
    for account_id, task in list(active_tasks.items()):
        if not task.done():
            continue
        active_tasks.pop(account_id, None)
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            logger.warning(
                "Deployment listener task finished with error",
                extra={"data": {"account_id": account_id, "error": str(exception)}},
            )


def _cancel_inactive_tasks(
    active_tasks: dict[str, asyncio.Task[None]], active_account_ids: set[str]
) -> None:
    for account_id in list(active_tasks.keys()):
        if account_id in active_account_ids:
            continue
        task = active_tasks.pop(account_id, None)
        if task is None or task.done():
            continue
        task.cancel()
        logger.info(
            "Stopped adapter execution listener",
            extra={"data": {"account_id": account_id, "status": "inactive"}},
        )


def _start_missing_tasks(
    adapters: dict[str, BaseLiveAdapter],
    active_tasks: dict[str, asyncio.Task[None]],
    active_account_ids: set[str],
) -> None:
    for account_id in sorted(active_account_ids):
        running_task = active_tasks.get(account_id)
        if running_task is not None and not running_task.done():
            continue
        adapter = adapters.get(account_id)
        if adapter is None:
            logger.warning(
                "Active deployment has no adapter",
                extra={"data": {"account_id": account_id}},
            )
            continue
        task = asyncio.create_task(
            _run_adapter(account_id, adapter),
            name=_deployment_task_name(account_id, "listener"),
        )
        active_tasks[account_id] = task
        logger.info(
            "Started adapter execution listener",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                }
            },
        )


def _normalize_deployment_status(status: str) -> str:
    normalized_status = str(status or "").upper()
    if normalized_status in {"ACTIVE", "PAUSED", "STOPPED"}:
        return normalized_status
    return "UNKNOWN"


def _log_deployment_status_observations(
    deployments: list[LiveDeploymentConfig], previous_statuses: dict[str, str]
) -> None:
    current_statuses: dict[str, str] = {}
    paused_deployments: list[str] = []
    stopped_deployments: list[str] = []
    for deployment in deployments:
        deployment_id = str(deployment.id)
        status = _normalize_deployment_status(deployment.status)
        current_statuses[deployment_id] = status
        previous_status = previous_statuses.get(deployment_id)
        if previous_status != status:
            logger.info(
                "Observed deployment status transition",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "strategy_name": deployment.strategy_name,
                        "previous_status": previous_status,
                        "status": status,
                    }
                },
            )
        if status == "PAUSED":
            paused_deployments.append(deployment_id)
        elif status == "STOPPED":
            stopped_deployments.append(deployment_id)
        elif status == "UNKNOWN":
            logger.warning(
                "Observed deployment with unknown status",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "strategy_name": deployment.strategy_name,
                        "status": deployment.status,
                    }
                },
            )

    removed_deployment_ids = sorted(set(previous_statuses) - set(current_statuses))
    for deployment_id in removed_deployment_ids:
        logger.info(
            "Live deployment removed from DB state",
            extra={"data": {"deployment_id": deployment_id}},
        )
    previous_statuses.clear()
    previous_statuses.update(current_statuses)

    if paused_deployments or stopped_deployments:
        logger.debug(
            "Observed non-active deployments",
            extra={
                "data": {
                    "paused_deployment_ids": sorted(paused_deployments),
                    "stopped_deployment_ids": sorted(stopped_deployments),
                }
            },
        )


async def _load_db_accounts_and_deployments(
) -> tuple[list[LiveAccountConfig], list[LiveDeploymentConfig]]:
    accounts_task = asyncio.to_thread(list_live_accounts)
    deployments_task = asyncio.to_thread(list_live_deployments)
    accounts, deployments = await asyncio.gather(accounts_task, deployments_task)
    return accounts, deployments


async def _ensure_adapter_for_active_account(
    account_id: str,
    account_lookup: dict[str, LiveAccountConfig],
    active_adapters: dict[str, BaseLiveAdapter],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    if account_id in active_adapters:
        return
    account = account_lookup.get(account_id)
    if account is None:
        logger.warning(
            "Active deployment references unknown account",
            extra={"data": {"account_id": account_id}},
        )
        return

    adapter = _create_adapter_for_account(account, accounts_env_values=accounts_env_values)
    if adapter is None:
        return

    try:
        await adapter.connect()
    except Exception as exc:
        logger.warning(
            "Adapter connect failed for active deployment account",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                    "error": str(exc),
                }
            },
        )
        return

    active_adapters[account_id] = adapter
    logger.info(
        "Adapter started for active deployment account",
        extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
    )


async def sync_deployments_loop(
    active_adapters: dict[str, BaseLiveAdapter],
    active_tasks: dict[str, asyncio.Task[None]],
    *,
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
    interval_seconds: int = DEPLOYMENT_POLL_INTERVAL_SECONDS,
) -> None:
    known_deployment_statuses: dict[str, str] = {}
    while True:
        try:
            load_dotenv(accounts_env, override=True)
            accounts_env_values = _load_accounts_env_values(accounts_env)
            accounts, deployments = await _load_db_accounts_and_deployments()
            account_lookup = {account.id: account for account in accounts}
            _cleanup_finished_listener_tasks(active_tasks)
            _log_deployment_status_observations(deployments, known_deployment_statuses)

            active_account_ids = {
                deployment.account_id
                for deployment in deployments
                if _normalize_deployment_status(deployment.status) == "ACTIVE"
            }
            logger.debug(
                "Evaluating deployment activation state",
                extra={
                    "data": {
                        "active_account_ids": sorted(active_account_ids),
                        "known_adapters": sorted(active_adapters.keys()),
                    }
                },
            )

            for account_id in sorted(active_account_ids):
                await _ensure_adapter_for_active_account(
                    account_id,
                    account_lookup,
                    active_adapters,
                    accounts_env_values=accounts_env_values,
                )

            _cancel_inactive_tasks(active_tasks, active_account_ids)
            _start_missing_tasks(active_adapters, active_tasks, active_account_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Deployment sync iteration failed",
                extra={"data": {"error": str(exc)}},
            )
        await asyncio.sleep(interval_seconds)


async def run_daemon(
    *,
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
) -> None:
    load_dotenv(accounts_env, override=True)
    logger.info(
        "Starting live daemon",
        extra={"data": {"accounts_env": str(accounts_env)}},
    )
    active_adapters: dict[str, BaseLiveAdapter] = {}
    active_listener_tasks: dict[str, asyncio.Task[None]] = {}
    sync_task = asyncio.create_task(
        sync_deployments_loop(
            active_adapters,
            active_listener_tasks,
            accounts_env=accounts_env,
        ),
        name="deployment-sync-loop",
    )
    balance_task = asyncio.create_task(_poll_balances(active_adapters), name="balance-poller")
    try:
        await asyncio.gather(sync_task, balance_task)
    except asyncio.CancelledError:
        logger.info("Live daemon cancelled")
        raise
    except Exception:
        logger.exception("Live daemon crashed")
        raise
    finally:
        logger.info(
            "Stopping live daemon",
            extra={"data": {"active_listener_tasks": len(active_listener_tasks)}},
        )


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.base_adapter import BaseLiveAdapter
from live_runner.ctrader_adapter import CTraderLiveAdapter
from shared.database import list_live_accounts, list_live_deployments, save_live_balance
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveBalanceRecord

logger = get_logger("live-runner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
BALANCE_POLL_INTERVAL_SECONDS = 60
DEPLOYMENT_POLL_INTERVAL_SECONDS = 10


def _create_adapter_for_account(account: LiveAccountConfig) -> BaseLiveAdapter | None:
    if account.broker == "alpaca":
        try:
            return AlpacaLiveAdapter(account_id=account.id)
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


def _build_adapters_from_accounts() -> dict[str, BaseLiveAdapter]:
    adapter_map: dict[str, BaseLiveAdapter] = {}
    for account in list_live_accounts():
        adapter = _create_adapter_for_account(account)
        if adapter is not None:
            adapter_map[account.id] = adapter
    return adapter_map


async def _connect_existing_adapters(
    adapters: dict[str, BaseLiveAdapter],
) -> dict[str, BaseLiveAdapter]:
    if not adapters:
        return {}

    connect_results = await asyncio.gather(
        *(adapter.connect() for adapter in adapters.values()), return_exceptions=True
    )
    running_adapters: dict[str, BaseLiveAdapter] = {}
    for account_id, adapter, result in zip(
        adapters.keys(), adapters.values(), connect_results, strict=True
    ):
        if isinstance(result, Exception):
            logger.warning(
                "Adapter connect failed",
                extra={
                    "data": {
                        "adapter": type(adapter).__name__,
                        "account_id": account_id,
                        "error": str(result),
                    }
                },
            )
            continue
        running_adapters[account_id] = adapter
    return running_adapters


async def _ensure_adapters_for_accounts(adapters: dict[str, BaseLiveAdapter]) -> None:
    accounts = await asyncio.to_thread(list_live_accounts)
    for account in accounts:
        if account.id in adapters:
            continue
        adapter = _create_adapter_for_account(account)
        if adapter is None:
            continue
        try:
            await adapter.connect()
        except Exception as exc:
            logger.warning(
                "Adapter connect failed for new DB account",
                extra={
                    "data": {
                        "account_id": account.id,
                        "adapter": type(adapter).__name__,
                        "error": str(exc),
                    }
                },
            )
            continue
        adapters[account.id] = adapter
        logger.info(
            "Connected adapter for new DB account",
            extra={"data": {"account_id": account.id, "adapter": type(adapter).__name__}},
        )


async def _run_adapter(adapter: BaseLiveAdapter) -> None:
    await adapter.listen_executions()


def _adapter_account_id(adapter: BaseLiveAdapter) -> str:
    account_id = str(getattr(adapter, "account_id", "") or "").strip()
    if account_id:
        return account_id
    config_id = str(getattr(adapter, "account_config_id", "") or "").strip()
    if config_id:
        return config_id
    return type(adapter).__name__


async def _poll_balances(
    adapters: dict[str, BaseLiveAdapter], *, interval_seconds: int = BALANCE_POLL_INTERVAL_SECONDS
) -> None:
    while True:
        snapshot_time = datetime.now(timezone.utc)
        for account_id, adapter in list(adapters.items()):
            account_id = _adapter_account_id(adapter)
            try:
                balance, equity = await adapter.get_balance()
                record = LiveBalanceRecord(
                    account_id=account_id,
                    timestamp=snapshot_time,
                    balance=float(balance),
                    equity=float(equity),
                )
                await asyncio.to_thread(save_live_balance, record)
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
            "Stopped deployment listener task",
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
            _run_adapter(adapter),
            name=_deployment_task_name(account_id, "listener"),
        )
        active_tasks[account_id] = task
        logger.info(
            "Started deployment listener task",
            extra={"data": {"account_id": account_id}},
        )


def _active_account_ids_from_deployments() -> set[str]:
    deployments = list_live_deployments()
    return {
        deployment.account_id
        for deployment in deployments
        if str(deployment.status).upper() == "ACTIVE"
    }


async def _manage_live_deployments(
    adapters: dict[str, BaseLiveAdapter],
    active_tasks: dict[str, asyncio.Task[None]],
) -> None:
    while True:
        await _ensure_adapters_for_accounts(adapters)
        _cleanup_finished_listener_tasks(active_tasks)
        active_account_ids = await asyncio.to_thread(_active_account_ids_from_deployments)
        _cancel_inactive_tasks(active_tasks, active_account_ids)
        _start_missing_tasks(adapters, active_tasks, active_account_ids)
        await asyncio.sleep(DEPLOYMENT_POLL_INTERVAL_SECONDS)


async def run_daemon(
    *,
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
) -> None:
    load_dotenv(accounts_env)
    adapters = _build_adapters_from_accounts()
    running_adapters = await _connect_existing_adapters(adapters)

    if not running_adapters:
        logger.warning(
            "No live adapters connected at startup; waiting for DB accounts/deployments"
        )

    active_listener_tasks: dict[str, asyncio.Task[None]] = {}
    manager_task = asyncio.create_task(
        _manage_live_deployments(running_adapters, active_listener_tasks),
        name="deployment-manager",
    )
    balance_task = asyncio.create_task(_poll_balances(running_adapters), name="balance-poller")
    await asyncio.gather(manager_task, balance_task)


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()

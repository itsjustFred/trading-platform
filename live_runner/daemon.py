from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from live_runner.base_adapter import BaseLiveAdapter
from live_runner.ctrader_adapter import CTraderLiveAdapter
from live_runner.service_discovery import discover_discord_bots_from_env
from live_runner.signal_sources import DiscordSignalSource, wait_for_candle_close
from risk import RiskConfig, RiskEngine, TradeSignal
from scheduler.scheduler import ServiceScheduler
from shared.database import (
    create_service,
    list_live_accounts,
    list_live_deployments,
    list_services,
    save_live_balance,
    update_service_status,
)
from shared.logging import get_logger
from shared.models import (
    LiveAccountConfig,
    LiveBalanceRecord,
    LiveDeploymentConfig,
    LiveSignal,
    Service,
    ServiceStatus,
    ServiceType,
)

logger = get_logger("live-runner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
BALANCE_POLL_INTERVAL_SECONDS = 60
DEPLOYMENT_POLL_INTERVAL_SECONDS = 10
SERVICE_SYNC_INTERVAL_SECONDS = 60


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


def _accounts_env_mtime(accounts_env: Path) -> int | None:
    try:
        return accounts_env.stat().st_mtime_ns
    except OSError:
        return None


def _discover_and_register_discord_services(*, log_summary: bool = True) -> list[Service]:
    discovered = discover_discord_bots_from_env()
    discovered_names = [str(item.get("name") or "").strip().upper() for item in discovered]
    if log_summary:
        if discovered_names:
            logger.info(
                "Discovered %s Discord bots: %s",
                len(discovered_names),
                ", ".join(discovered_names),
            )
        else:
            logger.info("Discovered 0 Discord bots")

    existing_services = list_services(service_type=ServiceType.DISCORD_BOT)
    existing_names = {str(service.name or "").strip().upper() for service in existing_services}
    for item in discovered:
        bot_name = str(item.get("name") or "").strip().upper()
        if not bot_name or bot_name in existing_names:
            continue
        token_env_key = str(item.get("token_env_key") or "").strip()
        channel_id = str(item.get("channel_id") or "").strip()
        try:
            created = create_service(
                Service(
                    name=bot_name,
                    type=ServiceType.DISCORD_BOT,
                    status=ServiceStatus.RUNNING,
                    config={
                        "token_env_key": token_env_key,
                        "channel_id": channel_id,
                    },
                    created_at=datetime.now(timezone.utc),
                )
            )
            existing_names.add(created.name.upper())
        except ValueError:
            continue
    return list_services(service_type=ServiceType.DISCORD_BOT)


def _set_all_services_running() -> list[Service]:
    services = list_services()
    for service in services:
        if service.status == ServiceStatus.RUNNING:
            continue
        update_service_status(str(service.id), ServiceStatus.RUNNING)
    return list_services()


def _service_task_name(service_id: str) -> str:
    return f"service-discord-{service_id}"


def _service_config_string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    return str(value).strip() if value is not None else ""


def _resolve_ctrader_credentials(
    env_values: Mapping[str, str],
) -> tuple[str, str, str, bool] | None:
    app_id = str(env_values.get("CTRADER_APP_ID", "")).strip()
    app_secret = str(env_values.get("CTRADER_APP_SECRET", "")).strip()
    access_token = str(env_values.get("CTRADER_ACCESS_TOKEN", "")).strip()
    if not app_id or not app_secret or not access_token:
        return None
    raw_demo_value = env_values.get("CTRADER_DEMO")
    demo_value = raw_demo_value.strip() if isinstance(raw_demo_value, str) else None
    demo = _parse_bool_env(demo_value, default=True)
    return app_id, app_secret, access_token, demo


def _coerce_live_float(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _coerce_live_int(
    value: object,
    *,
    field_name: str,
    minimum: int | None = None,
) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _first_metadata_float(metadata: Mapping[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        return _coerce_live_float(value, field_name=key)
    return None


def _risk_config_for_deployment(deployment: LiveDeploymentConfig) -> RiskConfig:
    settings = deployment.settings or {}
    risk_per_trade_raw = settings.get(
        "max_risk_per_trade",
        settings.get("risk_per_trade_pct", 0.01),
    )
    max_total_exposure_raw = settings.get("max_total_exposure", 0.05)
    max_positions_raw = settings.get("max_positions", 5)
    sl_multiplier_raw = settings.get("default_sl_atr_multiplier", 2.0)

    return RiskConfig(
        max_risk_per_trade=_coerce_live_float(
            risk_per_trade_raw,
            field_name="max_risk_per_trade",
            minimum=0.0,
        ),
        max_total_exposure=_coerce_live_float(
            max_total_exposure_raw,
            field_name="max_total_exposure",
            minimum=0.0,
        ),
        max_positions=_coerce_live_int(max_positions_raw, field_name="max_positions", minimum=1),
        default_sl_atr_multiplier=_coerce_live_float(
            sl_multiplier_raw,
            field_name="default_sl_atr_multiplier",
            minimum=0.0,
        ),
    )


def _deployment_wait_for_candle_close(
    deployment: LiveDeploymentConfig, env_values: Mapping[str, str]
) -> bool:
    configured = deployment.signal_config or {}
    if "wait_candle_close" in configured:
        return _parse_bool_env(str(configured.get("wait_candle_close")), default=False)
    if "wait_for_candle_close" in configured:
        return _parse_bool_env(str(configured.get("wait_for_candle_close")), default=False)
    return _parse_bool_env(str(env_values.get("DISCORD_WAIT_FOR_CANDLE_CLOSE", "")), default=True)


def _deployment_signal_timeframe(deployment: LiveDeploymentConfig) -> str:
    configured = deployment.signal_config or {}
    configured_tf = str(configured.get("timeframe") or deployment.timeframe).strip()
    if configured_tf:
        return configured_tf
    return deployment.timeframe


def _deployment_has_discord_channel(deployment: LiveDeploymentConfig) -> bool:
    configured = deployment.signal_config or {}
    return bool(str(configured.get("channel_id") or "").strip())


def size_live_signal_for_submission(
    signal: LiveSignal,
    deployment: LiveDeploymentConfig,
    *,
    account_balance: float,
    current_exposure: float = 0.0,
    pip_value: float | None = None,
) -> LiveSignal:
    metadata: dict[str, object] = dict(signal.metadata or {})
    stop_loss = _first_metadata_float(metadata, ("stop_loss", "sl_price", "stop_price"))
    take_profit = _first_metadata_float(metadata, ("take_profit", "tp_price"))
    resolved_timeframe = (
        str(metadata.get("timeframe") or deployment.timeframe).strip() or deployment.timeframe
    )
    pip = pip_value if pip_value is not None else metadata.get("pip_value", 1.0)
    resolved_pip = _coerce_live_float(pip, field_name="pip_value", minimum=0.0)

    trade_signal = TradeSignal(
        action=signal.action,
        symbol=signal.symbol,
        entry_price=float(signal.theoretical_price),
        stop_loss=stop_loss,
        take_profit=take_profit,
        timeframe=resolved_timeframe,
        metadata=dict(metadata),
    )
    sized_order = RiskEngine(_risk_config_for_deployment(deployment)).calculate_position_size(
        signal=trade_signal,
        account_balance=float(account_balance),
        pip_value=resolved_pip,
        current_exposure=float(current_exposure),
    )

    metadata["qty"] = float(sized_order.position_size)
    metadata["risk_amount"] = float(sized_order.risk_amount)
    metadata["risk_percent"] = float(sized_order.risk_percent)
    metadata["risk_reward_ratio"] = (
        float(sized_order.risk_reward_ratio)
        if sized_order.risk_reward_ratio is not None
        else None
    )
    metadata["risk_timeframe"] = resolved_timeframe
    if stop_loss is None and "resolved_stop_loss" in sized_order.signal.metadata:
        metadata["resolved_stop_loss"] = float(sized_order.signal.metadata["resolved_stop_loss"])
    return signal.model_copy(update={"metadata": metadata})


async def submit_order_with_risk(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    current_exposure: float = 0.0,
    account_balance: float | None = None,
    pip_value: float | None = None,
) -> str:
    resolved_balance = account_balance
    if resolved_balance is None:
        _, equity = await adapter.get_balance()
        resolved_balance = float(equity)

    sized_signal = size_live_signal_for_submission(
        signal,
        deployment,
        account_balance=resolved_balance,
        current_exposure=current_exposure,
        pip_value=pip_value,
    )
    return await adapter.submit_order(sized_signal)


def _trade_signal_to_live_signal(
    signal: TradeSignal,
    deployment: LiveDeploymentConfig,
) -> LiveSignal:
    metadata: dict[str, object] = dict(signal.metadata or {})
    if signal.stop_loss is not None:
        metadata["stop_loss"] = float(signal.stop_loss)
    if signal.take_profit is not None:
        metadata["take_profit"] = float(signal.take_profit)
    metadata["timeframe"] = signal.timeframe
    metadata["source"] = "discord"
    return LiveSignal(
        id=uuid.uuid4(),
        deployment_id=str(deployment.id),
        symbol=signal.symbol,
        action="buy" if signal.action in {"buy", "long"} else "sell",
        theoretical_price=float(signal.entry_price),
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


async def _run_discord_signal_listener_for_account(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployments: list[LiveDeploymentConfig],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    active_discord_deployments = [
        deployment
        for deployment in deployments
        if deployment.account_id == account_id
        and _normalize_deployment_status(deployment.status) == "ACTIVE"
        and str(deployment.signal_source or "internal").lower() == "discord"
    ]
    if not active_discord_deployments:
        logger.info(
            "No active discord deployments for account; discord listener idle",
            extra={"data": {"account_id": account_id}},
        )
        return

    channel_to_deployments: dict[str, list[LiveDeploymentConfig]] = {}
    for deployment in active_discord_deployments:
        configured_channel = str(
            deployment.signal_config.get("channel_id") or accounts_env_values.get("DISCORD_CHANNEL_ID", "")
        ).strip()
        if not configured_channel:
            logger.warning(
                "Discord deployment missing channel id",
                extra={"data": {"deployment_id": str(deployment.id), "account_id": account_id}},
            )
            continue
        channel_to_deployments.setdefault(configured_channel, []).append(deployment)

    if not channel_to_deployments:
        logger.warning(
            "No valid discord channels configured for active discord deployments",
            extra={"data": {"account_id": account_id}},
        )
        return

    source = DiscordSignalSource(
        token=accounts_env_values.get("DISCORD_BOT_TOKEN", ""),
        channel_ids=set(channel_to_deployments.keys()),
        timeframe=_deployment_signal_timeframe(next(iter(active_discord_deployments))),
    )
    await source.connect()
    try:
        async for trade_signal in source.listen_signals():
            channel_id = str(trade_signal.metadata.get("discord_channel_id", "")).strip()
            deployments_for_channel = channel_to_deployments.get(channel_id, [])
            if not deployments_for_channel:
                logger.debug(
                    "Skipping discord signal from unmapped channel",
                    extra={"data": {"account_id": account_id, "channel_id": channel_id}},
                )
                continue

            for deployment in deployments_for_channel:
                deployment_symbols = {item.upper() for item in deployment.symbols}
                if deployment_symbols and trade_signal.symbol.upper() not in deployment_symbols:
                    continue

                signal_timeframe = _deployment_signal_timeframe(deployment)
                wait_enabled = _deployment_wait_for_candle_close(deployment, accounts_env_values)
                normalized_signal = TradeSignal(
                    action=trade_signal.action,
                    symbol=trade_signal.symbol,
                    entry_price=trade_signal.entry_price,
                    stop_loss=trade_signal.stop_loss,
                    take_profit=trade_signal.take_profit,
                    timeframe=signal_timeframe,
                    metadata={**trade_signal.metadata, "wait_for_candle_close": wait_enabled},
                )
                if wait_enabled:
                    await wait_for_candle_close(normalized_signal, signal_timeframe)

                live_signal = _trade_signal_to_live_signal(normalized_signal, deployment)
                try:
                    broker_order_id = await submit_order_with_risk(adapter, deployment, live_signal)
                except Exception as exc:
                    logger.warning(
                        "Discord signal submission failed",
                        extra={
                            "data": {
                                "account_id": account_id,
                                "deployment_id": str(deployment.id),
                                "symbol": live_signal.symbol,
                                "error": str(exc),
                            }
                        },
                    )
                    continue

                logger.info(
                    "Discord signal submitted",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "deployment_id": str(deployment.id),
                            "signal_id": str(live_signal.id),
                            "broker_order_id": broker_order_id,
                            "symbol": live_signal.symbol,
                            "action": live_signal.action,
                        }
                    },
                )
                break
            else:
                logger.debug(
                    "Skipping discord signal with symbol not covered by deployment",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "channel_id": channel_id,
                            "symbol": trade_signal.symbol,
                        }
                    },
                )
    finally:
        await source.disconnect()


def _create_adapter_for_account(
    account: LiveAccountConfig, *, accounts_env_values: Mapping[str, str] | None = None
) -> BaseLiveAdapter | None:
    env_values = accounts_env_values or os.environ
    if account.broker == "alpaca":
        credentials = resolve_alpaca_credentials(
            account_id=account.id,
            broker_login=account.broker_login,
            env=env_values,
        )
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
        credentials = _resolve_ctrader_credentials(env_values)
        if credentials is None:
            logger.warning(
                "Missing cTrader credentials for account",
                extra={"data": {"account_id": account.id}},
            )
            return None
        app_id, app_secret, access_token, demo = credentials
        account_identifier = str(account.ctid or account.broker_login or account.id).strip()
        try:
            return CTraderLiveAdapter(
                app_id=app_id,
                app_secret=app_secret,
                access_token=access_token,
                account_id=account_identifier,
                demo=demo,
                account_config_id=account.id,
            )
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


async def _disconnect_adapter(
    adapter: BaseLiveAdapter,
    *,
    account_id: str,
    reason: str,
) -> None:
    disconnect_method = getattr(adapter, "disconnect", None)
    if not callable(disconnect_method):
        return
    try:
        result = disconnect_method()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.warning(
            "Adapter disconnect failed",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                    "reason": reason,
                    "error": str(exc),
                }
            },
        )


async def _disconnect_inactive_adapters(
    active_adapters: dict[str, BaseLiveAdapter],
    active_account_ids: set[str],
) -> None:
    inactive_account_ids = sorted(set(active_adapters) - set(active_account_ids))
    for account_id in inactive_account_ids:
        adapter = active_adapters.pop(account_id, None)
        if adapter is None:
            continue
        await _disconnect_adapter(
            adapter,
            account_id=account_id,
            reason="inactive_deployment",
        )
        logger.info(
            "Stopped adapter for inactive account",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                }
            },
        )


async def _run_discord_listener_with_retries(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployments_by_account: dict[str, list[LiveDeploymentConfig]],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    backoff_seconds = 2
    while True:
        try:
            deployments = deployments_by_account.get(account_id, [])
            await _run_discord_signal_listener_for_account(
                account_id,
                adapter,
                deployments,
                accounts_env_values=accounts_env_values,
            )
            backoff_seconds = 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Discord listener loop failed; retrying",
                extra={
                    "data": {
                        "account_id": account_id,
                        "retry_in_seconds": backoff_seconds,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)


def _resolve_discord_bot_runtime(
    service: Service,
    *,
    accounts_env_values: Mapping[str, str],
) -> tuple[str, str, str]:
    config = service.config if isinstance(service.config, dict) else {}
    token_env_key = _service_config_string(config, "token_env_key")
    if not token_env_key:
        canonical_name = re.sub(r"[^A-Z0-9_]+", "_", str(service.name or "").upper())
        token_env_key = f"DISCORD_BOT_{canonical_name}_TOKEN"
    token = str(accounts_env_values.get(token_env_key) or os.environ.get(token_env_key, "")).strip()
    channel_id = _service_config_string(config, "channel_id")
    return token_env_key, token, channel_id


async def _run_discord_bot_service_listener(service: Service, source: DiscordSignalSource) -> None:
    backoff_seconds = 2
    while True:
        try:
            async for _signal in source.listen_signals():
                pass
            if source.is_paused:
                return
            await asyncio.sleep(1)
            backoff_seconds = 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Discord bot listener failed; retrying",
                extra={
                    "data": {
                        "service_id": str(service.id),
                        "service_name": service.name,
                        "retry_in_seconds": backoff_seconds,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)


def _cleanup_finished_discord_bot_tasks(discord_bot_tasks: dict[str, asyncio.Task[None]]) -> None:
    for service_id, task in list(discord_bot_tasks.items()):
        if not task.done():
            continue
        discord_bot_tasks.pop(service_id, None)
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            logger.warning(
                "Discord bot task finished with error",
                extra={"data": {"service_id": service_id, "error": str(exception)}},
            )


async def _stop_discord_bot_service(
    service_id: str,
    *,
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
    paused: bool = False,
) -> None:
    task = active_discord_bot_tasks.pop(service_id, None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    source = active_discord_bots.pop(service_id, None)
    if source is not None:
        if paused:
            await source.pause()
        else:
            await source.disconnect(failed=False)


async def _sync_discord_bot_services(
    services: list[Service],
    *,
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
    accounts_env_values: Mapping[str, str],
) -> None:
    _cleanup_finished_discord_bot_tasks(active_discord_bot_tasks)

    discord_services = [
        service for service in services if service.type == ServiceType.DISCORD_BOT
    ]
    service_by_id = {str(service.id): service for service in discord_services}
    running_service_ids = {
        str(service.id)
        for service in discord_services
        if service.status == ServiceStatus.RUNNING
    }

    for service_id in sorted(set(active_discord_bots) - running_service_ids):
        service = service_by_id.get(service_id)
        await _stop_discord_bot_service(
            service_id,
            active_discord_bots=active_discord_bots,
            active_discord_bot_tasks=active_discord_bot_tasks,
            paused=bool(service and service.status == ServiceStatus.PAUSED),
        )

    for service in discord_services:
        service_id = str(service.id)
        if service.status != ServiceStatus.RUNNING:
            continue
        token_env_key, token, channel_id = _resolve_discord_bot_runtime(
            service,
            accounts_env_values=accounts_env_values,
        )
        if not token or not channel_id:
            logger.warning(
                "Discord service missing runtime credentials",
                extra={
                    "data": {
                        "service_id": service_id,
                        "service_name": service.name,
                        "token_env_key": token_env_key,
                        "has_token": bool(token),
                        "has_channel_id": bool(channel_id),
                    }
                },
            )
            await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
            continue

        source = active_discord_bots.get(service_id)
        expected_channel_ids = {channel_id}
        if source is None or source.token != token or source.channel_ids != expected_channel_ids:
            if source is not None:
                await _stop_discord_bot_service(
                    service_id,
                    active_discord_bots=active_discord_bots,
                    active_discord_bot_tasks=active_discord_bot_tasks,
                )
            source = DiscordSignalSource(
                bot_name=service.name,
                token=token,
                channel_id=channel_id,
                service_id=service_id,
            )
            active_discord_bots[service_id] = source

        running_task = active_discord_bot_tasks.get(service_id)
        if running_task is None or running_task.done():
            task = asyncio.create_task(
                _run_discord_bot_service_listener(service, source),
                name=_service_task_name(service_id),
            )
            active_discord_bot_tasks[service_id] = task
            logger.info("Started Discord bot %s", service.name)

        running_task = active_discord_bot_tasks.get(service_id)
        if source.is_connected or (running_task is not None and not running_task.done()):
            continue
        try:
            await source.reconnect()
            await asyncio.to_thread(
                update_service_status,
                service_id,
                ServiceStatus.RUNNING,
                last_activity=datetime.now(timezone.utc),
            )
            logger.info(
                "Reconnected Discord bot %s",
                service.name,
            )
        except Exception as exc:
            logger.warning(
                "Discord bot reconnect failed",
                extra={
                    "data": {
                        "service_id": service_id,
                        "service_name": service.name,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)


async def sync_services_loop(
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
    scheduler: ServiceScheduler,
    *,
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
    interval_seconds: int = SERVICE_SYNC_INTERVAL_SECONDS,
) -> None:
    accounts_env_values = _load_accounts_env_values(accounts_env)
    accounts_env_mtime = _accounts_env_mtime(accounts_env)
    while True:
        try:
            current_mtime = _accounts_env_mtime(accounts_env)
            if current_mtime != accounts_env_mtime:
                accounts_env_values = _load_accounts_env_values(accounts_env)
                accounts_env_mtime = current_mtime
                await asyncio.to_thread(_discover_and_register_discord_services, log_summary=False)

            services = await asyncio.to_thread(list_services)
            await scheduler.sync_services(services)
            await _sync_discord_bot_services(
                services,
                active_discord_bots=active_discord_bots,
                active_discord_bot_tasks=active_discord_bot_tasks,
                accounts_env_values=accounts_env_values,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Service sync iteration failed",
                extra={"data": {"error": str(exc)}},
            )
        await asyncio.sleep(interval_seconds)


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


def _cleanup_finished_discord_tasks(discord_tasks: dict[str, asyncio.Task[None]]) -> None:
    for account_id, task in list(discord_tasks.items()):
        if not task.done():
            continue
        discord_tasks.pop(account_id, None)
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            logger.warning(
                "Discord listener task finished with error",
                extra={"data": {"account_id": account_id, "error": str(exception)}},
            )


def _cancel_inactive_discord_tasks(
    discord_tasks: dict[str, asyncio.Task[None]],
    active_discord_account_ids: set[str],
) -> None:
    for account_id in list(discord_tasks.keys()):
        if account_id in active_discord_account_ids:
            continue
        task = discord_tasks.pop(account_id, None)
        if task is None or task.done():
            continue
        task.cancel()
        logger.info(
            "Stopped discord signal listener",
            extra={"data": {"account_id": account_id, "status": "inactive"}},
        )


def _start_missing_discord_tasks(
    adapters: dict[str, BaseLiveAdapter],
    discord_tasks: dict[str, asyncio.Task[None]],
    active_discord_account_ids: set[str],
    deployments_by_account: dict[str, list[LiveDeploymentConfig]],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    for account_id in sorted(active_discord_account_ids):
        running_task = discord_tasks.get(account_id)
        if running_task is not None and not running_task.done():
            continue
        adapter = adapters.get(account_id)
        if adapter is None:
            continue
        task = asyncio.create_task(
            _run_discord_listener_with_retries(
                account_id,
                adapter,
                deployments_by_account,
                accounts_env_values=accounts_env_values,
            ),
            name=_deployment_task_name(account_id, "discord"),
        )
        discord_tasks[account_id] = task
        logger.info(
            "Started discord signal listener",
            extra={"data": {"account_id": account_id}},
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
            logger.info(
                "Active deployment has no adapter yet; waiting for next adapter-connect retry",
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

    active_adapters[account_id] = adapter

    try:
        await adapter.connect()
    except Exception as exc:
        logger.info(
            "Adapter connect not ready for active deployment account; will retry",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                    "error": str(exc),
                }
            },
        )
        return

    logger.info(
        "Adapter started for active deployment account",
        extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
    )


async def sync_deployments_loop(
    active_adapters: dict[str, BaseLiveAdapter],
    active_tasks: dict[str, asyncio.Task[None]],
    active_discord_tasks: dict[str, asyncio.Task[None]],
    *,
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
    interval_seconds: int = DEPLOYMENT_POLL_INTERVAL_SECONDS,
) -> None:
    known_deployment_statuses: dict[str, str] = {}
    accounts_env_values = _load_accounts_env_values(accounts_env)
    accounts_env_mtime = _accounts_env_mtime(accounts_env)
    while True:
        try:
            # Read credential file directly without repeatedly mutating process env.
            current_mtime = _accounts_env_mtime(accounts_env)
            if current_mtime != accounts_env_mtime:
                accounts_env_values = _load_accounts_env_values(accounts_env)
                accounts_env_mtime = current_mtime
            accounts, deployments = await _load_db_accounts_and_deployments()
            deployments_by_account: dict[str, list[LiveDeploymentConfig]] = {}
            for deployment in deployments:
                deployments_by_account.setdefault(deployment.account_id, []).append(deployment)
            account_lookup = {account.id: account for account in accounts}
            _cleanup_finished_listener_tasks(active_tasks)
            _cleanup_finished_discord_tasks(active_discord_tasks)
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

            await _disconnect_inactive_adapters(active_adapters, active_account_ids)

            active_discord_account_ids = {
                deployment.account_id
                for deployment in deployments
                if _normalize_deployment_status(deployment.status) == "ACTIVE"
                and str(deployment.signal_source or "internal").lower() == "discord"
                and _deployment_has_discord_channel(deployment)
            }

            for account_id in sorted(active_account_ids):
                await _ensure_adapter_for_active_account(
                    account_id,
                    account_lookup,
                    active_adapters,
                    accounts_env_values=accounts_env_values,
                )

            _cancel_inactive_tasks(active_tasks, active_account_ids)
            _start_missing_tasks(active_adapters, active_tasks, active_account_ids)
            _cancel_inactive_discord_tasks(active_discord_tasks, active_discord_account_ids)
            _start_missing_discord_tasks(
                active_adapters,
                active_discord_tasks,
                active_discord_account_ids,
                deployments_by_account,
                accounts_env_values=accounts_env_values,
            )
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
    logger.info(
        "Starting live daemon",
        extra={"data": {"accounts_env": str(accounts_env)}},
    )
    active_adapters: dict[str, BaseLiveAdapter] = {}
    active_listener_tasks: dict[str, asyncio.Task[None]] = {}
    active_discord_tasks: dict[str, asyncio.Task[None]] = {}
    active_discord_bots: dict[str, DiscordSignalSource] = {}
    active_discord_bot_tasks: dict[str, asyncio.Task[None]] = {}
    scheduler = ServiceScheduler()

    await asyncio.to_thread(_discover_and_register_discord_services, log_summary=True)
    await asyncio.to_thread(_set_all_services_running)
    await scheduler.start()

    sync_task = asyncio.create_task(
        sync_deployments_loop(
            active_adapters,
            active_listener_tasks,
            active_discord_tasks,
            accounts_env=accounts_env,
        ),
        name="deployment-sync-loop",
    )
    service_sync_task = asyncio.create_task(
        sync_services_loop(
            active_discord_bots,
            active_discord_bot_tasks,
            scheduler,
            accounts_env=accounts_env,
        ),
        name="service-sync-loop",
    )
    balance_task = asyncio.create_task(_poll_balances(active_adapters), name="balance-poller")
    try:
        await asyncio.gather(sync_task, service_sync_task, balance_task)
    except asyncio.CancelledError:
        logger.info("Live daemon cancelled")
        raise
    except Exception:
        logger.exception("Live daemon crashed")
        raise
    finally:
        if not service_sync_task.done():
            service_sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await service_sync_task
        for task in list(active_discord_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(active_discord_bot_tasks.values()):
            if not task.done():
                task.cancel()
        for source in list(active_discord_bots.values()):
            with contextlib.suppress(Exception):
                await source.disconnect(failed=False)
        with contextlib.suppress(Exception):
            await scheduler.stop()
        logger.info(
            "Stopping live daemon",
            extra={
                "data": {
                    "active_listener_tasks": len(active_listener_tasks),
                    "active_discord_tasks": len(active_discord_tasks),
                    "active_discord_bot_tasks": len(active_discord_bot_tasks),
                }
            },
        )


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()

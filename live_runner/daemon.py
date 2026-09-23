from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone, timedelta
from pathlib import Path

import pandas as pd

from live_runner.account_routing import (
    ctrader_account_identifier,
    validate_live_account_routes,
)
from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from live_runner.base_adapter import EXIT_ONLY_RISK_SIZING_MODE, BaseLiveAdapter
from live_runner.credential_env import (
    accounts_env_mtime,
    load_accounts_env_values,
    resolve_env_value,
)
from live_runner.ctrader_adapter import CTraderAuthTerminalError, CTraderLiveAdapter
from live_runner.service_discovery import discover_discord_bots_from_env
from live_runner.signal_sources import (
    DiscordSignalSource,
    signal_is_fresh,
    wait_for_candle_close,
)
from live_runner.signal_sources.discord_source import DiscordAuthenticationError
from live_runner.stockkit_routing import (
    infer_stockkit_route,
    validate_stockkit_deployment_route,
)
from risk import RiskConfig, RiskEngine, TradeSignal
from risk.models import realized_risk_amount, realized_risk_percent
from scheduler.scheduler import ServiceScheduler
from shared.database import (
    create_service,
    get_live_deployment,
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
from shared.sentry import capture_exception, initialize_sentry
from signal_bots.registry import get_signal_processor

initialize_sentry("live-runner")
logger = get_logger("live-runner")

_ACCOUNT_NOT_AUTHORIZED_LOG = "Account %s not authorized for token, skipping"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
BALANCE_POLL_INTERVAL_SECONDS = 60
DEPLOYMENT_POLL_INTERVAL_SECONDS = 10
SERVICE_SYNC_INTERVAL_SECONDS = 60
_DISCORD_AUTH_BLOCKED_ACCOUNTS: set[str] = set()
_DISCORD_AUTH_BLOCKED_REASONS: dict[str, str] = {}
_DISCORD_SERVICE_AUTH_BLOCKED_IDS: set[str] = set()
_DISCORD_SERVICE_AUTH_BLOCKED_REASONS: dict[str, str] = {}
DISCORD_RECONNECT_RETRY_FLOOR_SECONDS = 30
_DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE: dict[str, datetime] = {}
_CTRADER_AUTH_BLOCKED_ACCOUNTS: set[str] = set()
_CTRADER_AUTH_BLOCKED_REASONS: dict[str, str] = {}
_CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES: dict[str, str] = {}


def _is_terminal_discord_runtime_error(exc: BaseException) -> bool:
    if isinstance(exc, DiscordAuthenticationError):
        return True
    message = str(exc).strip().lower()
    if not message:
        return False
    return any(
        marker in message
        for marker in (
            "improper token has been passed",
            "invalid token",
            "token is invalid",
            "unauthorized",
            "authentication failed",
            "missing discord_bot_token",
            "missing discord_channel_id",
        )
    )


def _is_terminal_ctrader_runtime_error(exc: BaseException) -> bool:
    if isinstance(exc, CTraderAuthTerminalError):
        return True
    message = str(exc).strip().lower()
    if not message:
        return False
    return any(
        marker in message
        for marker in (
            "trading account is not authorized",
            "account not authorized",
            "not authorized",
            "invalid token",
            "token invalid",
            "unauthorized",
        )
    )


def _is_ctrader_pending_deferred_error(exc: BaseException) -> bool:
    message = str(exc).strip().lower()
    if not message:
        return False
    if "deferred" not in message:
        return False
    return message.startswith("(15")


def _block_discord_service(service_id: str, exc: BaseException | str) -> None:
    reason = str(exc).strip() or "Discord authentication failed"
    _DISCORD_SERVICE_AUTH_BLOCKED_IDS.add(service_id)
    _DISCORD_SERVICE_AUTH_BLOCKED_REASONS[service_id] = reason


def _adapter_is_ready(adapter: BaseLiveAdapter) -> bool:
    connected = getattr(adapter, "_connected", None)
    authenticated = getattr(adapter, "_authenticated", None)
    if connected is False:
        return False
    if authenticated is False:
        return False
    return True


def _parse_bool_env(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_TRUE_BOOL_TOKENS = {"1", "true", "yes", "y", "on"}
_FALSE_BOOL_TOKENS = {"0", "false", "no", "n", "off"}


def _parse_strict_bool_env(name: str, value: str | None, *, default: bool = True) -> bool:
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


def _raw_bool_text(value: object) -> str | None:
    """Normalize a config value to text for boolean parsing.

    None means "not configured" so the caller's default applies; an empty
    string is deliberately NOT collapsed to None — a typo'd/blank value must
    surface as a strict-parse error instead of silently enabling the default.
    """
    if value is None:
        return None
    return str(value)


def _load_accounts_env_values(accounts_env: Path) -> dict[str, str]:
    return load_accounts_env_values(accounts_env)


def _accounts_env_mtime(accounts_env: Path) -> int | None:
    return accounts_env_mtime(accounts_env)


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


def _recover_failed_services() -> list[Service]:
    """Retry FAILED services on startup; never resurrect PAUSED/STOPPED ones.

    PAUSED/STOPPED encode operator intent and must survive a daemon restart.
    FAILED is system state (e.g. transient auth/network) and is safe to retry —
    but bounded (B2): each resuscitated service starts with its reconnect
    floor armed, so a still-broken credential backs off instead of hot-looping
    from the first tick. Terminal auth failures re-block immediately.
    """
    services = list_services()
    for service in services:
        if service.status != ServiceStatus.FAILED:
            continue
        update_service_status(str(service.id), ServiceStatus.RUNNING)
        _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE[str(service.id)] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=DISCORD_RECONNECT_RETRY_FLOOR_SECONDS)
        )
    return list_services()


def _service_task_name(service_id: str) -> str:
    return f"service-discord-{service_id}"


def _service_config_string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    return str(value).strip() if value is not None else ""


def _resolve_ctrader_credentials(
    env_values: Mapping[str, str],
) -> tuple[str, str, str, str, bool] | None:
    app_id = str(env_values.get("CTRADER_APP_ID", "")).strip()
    app_secret = str(env_values.get("CTRADER_APP_SECRET", "")).strip()
    access_token = str(env_values.get("CTRADER_ACCESS_TOKEN", "")).strip()
    refresh_token = str(env_values.get("CTRADER_REFRESH_TOKEN", "")).strip()
    if not app_id or not app_secret or not access_token:
        return None
    raw_demo_value = env_values.get("CTRADER_DEMO")
    demo_value = raw_demo_value.strip() if isinstance(raw_demo_value, str) else None
    demo = _parse_strict_bool_env("CTRADER_DEMO", demo_value, default=True)
    return app_id, app_secret, access_token, refresh_token, demo


def _ctrader_credentials_signature(env_values: Mapping[str, str]) -> str:
    credentials = _resolve_ctrader_credentials(env_values)
    if credentials is None:
        return "missing"
    app_id, app_secret, access_token, refresh_token, demo = credentials
    payload = "\0".join((app_id, app_secret, access_token, refresh_token, str(demo)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ctrader_credentials_signature_safe(env_values: Mapping[str, str]) -> str:
    """Credential signature that survives a strict-parse config error.

    A typo'd boolean in `.accounts.env` must not abort the deployment sync
    iteration for every account (the loop's only handler is the generic
    `except Exception`). A stable error signature instead blocks rotation —
    the account is skipped when its adapter is (re)created, which surfaces
    the invalid value through the per-account error log.
    """
    try:
        return _ctrader_credentials_signature(env_values)
    except ValueError:
        return "invalid-credentials"


def _refresh_ctrader_blocked_state_for_accounts(
    accounts: list[LiveAccountConfig],
    env_values: Mapping[str, str],
) -> set[str]:
    changed_account_ids: set[str] = set()
    for account in accounts:
        if account.broker != "ctrader":
            continue
        account_id = account.id
        signature = _ctrader_credentials_signature_safe(env_values)
        previous = _CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES.get(account_id)
        _CTRADER_ACCOUNT_CREDENTIAL_SIGNATURES[account_id] = signature
        if previous is None or previous == signature:
            continue
        changed_account_ids.add(account_id)
        if account_id in _CTRADER_AUTH_BLOCKED_ACCOUNTS:
            logger.info(
                "cTrader account credentials changed; clearing auth block",
                extra={"data": {"account_id": account_id}},
            )
        _CTRADER_AUTH_BLOCKED_ACCOUNTS.discard(account_id)
        _CTRADER_AUTH_BLOCKED_REASONS.pop(account_id, None)
    return changed_account_ids


def _clear_ctrader_blocked_state(account_id: str) -> None:
    _CTRADER_AUTH_BLOCKED_ACCOUNTS.discard(account_id)
    _CTRADER_AUTH_BLOCKED_REASONS.pop(account_id, None)


def _block_ctrader_account(account_id: str, exc: BaseException | str) -> None:
    if not isinstance(exc, CTraderAuthTerminalError) and not _is_terminal_ctrader_runtime_error(exc):
        return
    reason = str(exc).strip() or "cTrader authorization failed"
    _CTRADER_AUTH_BLOCKED_ACCOUNTS.add(account_id)
    _CTRADER_AUTH_BLOCKED_REASONS[account_id] = reason


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


def _deployment_allows_short(deployment: LiveDeploymentConfig) -> bool:
    settings = deployment.settings or {}
    configured = deployment.signal_config or {}
    value = configured.get("allow_short", settings.get("allow_short", False))
    return _parse_strict_bool_env("allow_short", _raw_bool_text(value), default=False)


def _deployment_wait_for_candle_close(
    deployment: LiveDeploymentConfig, env_values: Mapping[str, str]
) -> bool:
    configured = deployment.signal_config or {}
    if "wait_candle_close" in configured:
        return _parse_strict_bool_env(
            "wait_candle_close", _raw_bool_text(configured.get("wait_candle_close")), default=False
        )
    if "wait_for_candle_close" in configured:
        return _parse_strict_bool_env(
            "wait_for_candle_close",
            _raw_bool_text(configured.get("wait_for_candle_close")),
            default=False,
        )
    return _parse_strict_bool_env(
        "DISCORD_WAIT_FOR_CANDLE_CLOSE",
        env_values.get("DISCORD_WAIT_FOR_CANDLE_CLOSE"),
        default=True,
    )


def _deployment_signal_timeframe(deployment: LiveDeploymentConfig) -> str:
    configured = deployment.signal_config or {}
    configured_tf = str(configured.get("timeframe") or deployment.timeframe).strip()
    if configured_tf:
        return configured_tf
    return deployment.timeframe


def _deployment_has_discord_channel(deployment: LiveDeploymentConfig) -> bool:
    configured = deployment.signal_config or {}
    return bool(str(configured.get("channel_id") or "").strip())


def _deployment_signal_processor_name(deployment: LiveDeploymentConfig) -> str:
    configured = deployment.signal_config or {}
    settings = deployment.settings or {}
    return str(
        configured.get("signal_processor") or settings.get("signal_processor") or ""
    ).strip().lower()


def _deployment_matches_stockkit_route(
    deployment: LiveDeploymentConfig,
    signal_metadata: Mapping[str, object],
) -> bool:
    route = infer_stockkit_route(
        signal_source_id=deployment.signal_source_id,
        signal_config=deployment.signal_config,
        settings=deployment.settings,
        metadata=signal_metadata,
    )
    if not route:
        return True
    try:
        validate_stockkit_deployment_route(deployment)
    except ValueError as exc:
        logger.warning(
            "Blocking misrouted StockKit deployment",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "error": str(exc),
                }
            },
        )
        return False
    signal_route = infer_stockkit_route(metadata=signal_metadata)
    return not signal_route or signal_route == route


def _build_processor_market_data(
    signal: TradeSignal,
    *,
    timeframe: str,
) -> pd.DataFrame:
    metadata = signal.metadata or {}
    candidate = metadata.get("market_data")
    if isinstance(candidate, pd.DataFrame) and not candidate.empty:
        return candidate.copy()
    if isinstance(candidate, list) and candidate:
        frame = pd.DataFrame(candidate)
        if not frame.empty:
            return frame

    # Fail closed: signal processors compute ATR/volatility/trend filters. Feeding
    # them fabricated OHLCV approves live trades on invented context.
    raise ValueError(
        "Signal processor requires market data; refusing to fabricate synthetic "
        "OHLCV for a live trading decision"
    )


def _apply_signal_processor(
    signal: TradeSignal,
    deployment: LiveDeploymentConfig,
) -> TradeSignal:
    processor_name = _deployment_signal_processor_name(deployment)
    if not processor_name:
        return signal
    configured = deployment.signal_config or {}
    processor_config_payload = configured.get("signal_processor_config")
    processor_config = (
        dict(processor_config_payload)
        if isinstance(processor_config_payload, Mapping)
        else {}
    )
    processor = get_signal_processor(processor_name, processor_config)
    timeframe = _deployment_signal_timeframe(deployment)
    market_data = _build_processor_market_data(signal, timeframe=timeframe)
    raw_signal = {
        "symbol": signal.symbol,
        "action": signal.action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_price": signal.entry_price,
        "signal_price": signal.entry_price,
        "timeframe": signal.timeframe,
        "metadata": dict(signal.metadata or {}),
    }
    processed = processor.process_signal(raw_signal=raw_signal, market_data=market_data)
    metadata = dict(signal.metadata or {})
    metadata.update(dict(processed.metadata or {}))
    metadata["signal_processor"] = processor_name
    return TradeSignal(
        action=processed.action,
        symbol=processed.symbol,
        entry_price=float(processed.entry_price),
        stop_loss=processed.stop_loss,
        take_profit=processed.take_profit,
        timeframe=processed.timeframe or signal.timeframe,
        metadata=metadata,
    )


def size_live_signal_for_submission(
    signal: LiveSignal,
    deployment: LiveDeploymentConfig,
    *,
    account_balance: float,
    current_exposure: float = 0.0,
    pip_value: float | None = None,
    current_positions: int | None = None,
) -> LiveSignal:
    metadata: dict[str, object] = dict(signal.metadata or {})
    if current_positions is not None:
        metadata["current_positions"] = int(current_positions)
    stop_loss = _first_metadata_float(
        metadata, ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
    )
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
        metadata={**metadata, "enforce_stop_direction": True},
    )
    sized_order = RiskEngine(_risk_config_for_deployment(deployment)).calculate_position_size(
        signal=trade_signal,
        account_balance=float(account_balance),
        pip_value=resolved_pip,
        current_exposure=float(current_exposure),
    )

    metadata["qty"] = float(sized_order.position_size)
    metadata["explicit_qty"] = float(sized_order.position_size)
    # #84: live metadata carries the realized stop-loss amount (differs from
    # the policy figure on notional-cap).
    metadata["risk_amount"] = float(realized_risk_amount(sized_order))
    metadata["risk_percent"] = float(realized_risk_percent(sized_order, account_balance))
    metadata["risk_reward_ratio"] = (
        float(sized_order.risk_reward_ratio)
        if sized_order.risk_reward_ratio is not None
        else None
    )
    metadata["risk_timeframe"] = resolved_timeframe
    if stop_loss is None and "resolved_stop_loss" in sized_order.signal.metadata:
        metadata["resolved_stop_loss"] = float(sized_order.signal.metadata["resolved_stop_loss"])
    return signal.model_copy(update={"metadata": metadata})


def _resolve_exit_only_target_qty(
    positions: Iterable[Mapping[str, object]],
    symbol: str,
) -> float:
    """Return the total quantity to flatten for ``symbol``.

    Hedging accounts (cTrader default) can hold several lots of the same
    symbol as separate positions; netting accounts fold them into one row.
    Both shapes must flatten the whole exposure, so the magnitudes of all
    matching rows are summed — shorts count too (a negative qty is an open
    short that an exit-only close must buy back, not a residual to ignore).
    Rows that cannot be parsed are skipped (fail-closed: the close then
    covers less, and a residual row is still visible on the broker), never
    crash the close.
    """
    target_qty = 0.0
    for position in positions:
        position_symbol = str(position.get("symbol") or "").strip().upper()
        if position_symbol != symbol.upper():
            continue
        try:
            resolved_qty = _coerce_live_float(position.get("qty"), field_name="qty")
        except ValueError:
            continue
        target_qty += abs(resolved_qty)
    return target_qty


def _exit_only_close_action(positions: Iterable[Mapping[str, object]], symbol: str) -> str:
    """Return the order side that flattens ``symbol``'s net exposure.

    A net-long book closes with ``sell``; a net-short book closes with
    ``buy`` (the short must be bought back). A flat book (or rows that all
    fail to parse) returns ``sell`` — the caller rejects the order on a
    zero target quantity anyway, and ``sell`` keeps the historical default
    for the rows that do parse.
    """
    net_qty = 0.0
    for position in positions:
        position_symbol = str(position.get("symbol") or "").strip().upper()
        if position_symbol != symbol.upper():
            continue
        try:
            net_qty += _coerce_live_float(position.get("qty"), field_name="qty")
        except ValueError:
            continue
    return "buy" if net_qty < 0 else "sell"


async def _metadata_for_exit_only_order(
    signal: LiveSignal,
    adapter: BaseLiveAdapter,
    metadata: dict[str, object],
) -> dict[str, object]:
    get_positions = getattr(adapter, "get_positions", None)
    if not callable(get_positions):
        raise ValueError("Exit-only signal requires broker adapter with get_positions support")
    positions = await get_positions()
    target_qty = _resolve_exit_only_target_qty(positions, signal.symbol)
    if target_qty <= 0:
        raise ValueError(
            f"Exit-only signal received for {signal.symbol} but no position is open"
        )
    metadata["explicit_qty"] = float(target_qty)
    metadata["risk_sizing_mode"] = EXIT_ONLY_RISK_SIZING_MODE
    # A short position must be closed with a BUY; keep the close side derived
    # from the broker book instead of trusting the signal's action. The
    # adapters read signal.action (not metadata), so the signal itself is
    # re-stamped below by the caller's model_copy.
    close_action = _exit_only_close_action(positions, signal.symbol)
    metadata["action"] = close_action
    metadata["signal_action"] = signal.action
    return metadata


def _count_open_broker_positions(positions: Iterable[Mapping[str, object]]) -> int:
    open_positions_count = 0
    for position in positions:
        try:
            position_qty = float(position.get("qty") or 0.0)
        except (TypeError, ValueError):
            position_qty = 0.0
        if abs(position_qty) > 0:
            open_positions_count += 1
    return open_positions_count


async def _broker_exposure_estimate(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
) -> tuple[int | None, float]:
    """Estimate the open risk already at stake on the broker.

    Unit contract: the estimate is a fraction of account balance (risk-at-stake),
    the same unit as ``RiskConfig.max_total_exposure`` — NOT a quantity or a
    notional. Each open position was sized at most ``max_risk_per_trade`` of
    balance (enforced by ``submit_order_with_risk`` -> risk engine), so the
    worst-case open risk is ``count * max_risk_per_trade``. The estimate is
    clamped to ``max_total_exposure``: position rows fetched between snapshots
    must not fabricate budget overflow beyond the configured cap.
    """
    open_positions_count: int | None = None
    broker_exposure = 0.0
    get_positions = getattr(adapter, "get_positions", None)
    if callable(get_positions):
        positions = await get_positions()
        open_positions_count = _count_open_broker_positions(positions)
        # Conservative estimate: each open position was sized at max_risk_per_trade.
        risk_config = _risk_config_for_deployment(deployment)
        broker_exposure = min(
            open_positions_count * risk_config.max_risk_per_trade,
            risk_config.max_total_exposure,
        )
    return open_positions_count, broker_exposure


async def _resolve_account_balance(
    adapter: BaseLiveAdapter,
    account_balance: float | None,
) -> float:
    if account_balance is None:
        _, equity = await adapter.get_balance()
        return float(equity)
    return account_balance


async def submit_order_with_risk(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    current_exposure: float = 0.0,
    account_balance: float | None = None,
    pip_value: float | None = None,
) -> str:
    metadata = dict(signal.metadata or {})
    if signal.action == "sell" and bool(metadata.get("exit_only")):
        # Exit-only orders close an existing position at the broker-observed
        # quantity. They bypass risk sizing and the entry fail-closed SL/TP
        # guard: they reduce exposure instead of opening a new position.
        metadata = await _metadata_for_exit_only_order(signal, adapter, metadata)
        # The close side is derived from the broker book (a short must be
        # bought back), so the stamped action wins over the signal's.
        update: dict[str, object] = {"metadata": metadata}
        close_action = metadata.get("action")
        if close_action in {"buy", "sell"} and close_action != signal.action:
            update["action"] = close_action
        # C1/B2: the broker-resolved flatten qty is binding — pass it
        # explicitly so the adapter never re-sizes.
        return await adapter.submit_order(
            signal.model_copy(update=update),
            qty=float(metadata["explicit_qty"]),
        )
    # Feed broker truth into sizing so max_positions / max_total_exposure actually
    # bind. Without this, every signal is sized as if the account were flat.
    open_positions_count, broker_exposure = await _broker_exposure_estimate(
        adapter, deployment
    )
    resolved_balance = await _resolve_account_balance(adapter, account_balance)
    sized_signal = size_live_signal_for_submission(
        signal,
        deployment,
        account_balance=resolved_balance,
        current_exposure=max(float(current_exposure), broker_exposure),
        pip_value=pip_value,
        current_positions=open_positions_count,
    )
    # Sizing may have resolved a fallback stop (ATR/percent). Persist that
    # resolution so the broker order carries the stop sizing actually assumed.
    # C1 (owner decision): a missing take profit FAILS — no invented 1.0-RR
    # bracket from theoretical_price. The guard below rejects it.
    sized_metadata = dict(sized_signal.metadata or {})
    if "resolved_stop_loss" in sized_metadata:
        metadata["stop_loss"] = float(sized_metadata["resolved_stop_loss"])
    stop_loss = _first_metadata_float(
        metadata, ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
    )
    take_profit = _first_metadata_float(metadata, ("take_profit", "tp_price"))
    # Fail closed: sizing divides the risk budget by the stop distance, so an
    # order without SL/TP would be sized on an invented distance and open an
    # unbounded position. Both broker adapters enforce the same guard. The
    # ONLY exception is an explicit unbounded-take-profit policy flag set by
    # the originating processor (C1 owner decision: bounded TP or explicit
    # unbounded flag — RocketFuel exits on the opposite signal).
    unbounded_tp = (
        str(metadata.get("take_profit_policy") or "")
        == "unbounded_exit_on_opposite_signal"
    )
    missing: list[str] = []
    if stop_loss is None:
        missing.append("stop loss")
    if take_profit is None and not unbounded_tp:
        missing.append("take profit")
    if missing:
        raise ValueError(
            "Refusing to submit live order without "
            f"{' and '.join(missing)} (deployment_id={deployment.id}, "
            f"symbol={signal.symbol}); opening an unbounded position is "
            "not allowed."
        )
    if take_profit is None and unbounded_tp:
        logger.warning(
            "Submitting live order with explicitly unbounded take profit "
            "(exit on opposite signal)",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "symbol": signal.symbol,
                    "take_profit_policy": metadata.get("take_profit_policy"),
                }
            },
        )
    metadata["stop_loss"] = float(stop_loss)
    if take_profit is not None:
        metadata["take_profit"] = float(take_profit)
    else:
        metadata.pop("take_profit", None)
    # Overlay the resolved SL/TP onto the SIZED metadata. The sized values for
    # qty/explicit_qty/risk_* must win: letting incoming metadata re-override
    # them would let a signal's self-declared qty bypass the risk sizing
    # entirely. All other (non-sizing) incoming keys are preserved.
    sizing_keys = frozenset({
        "qty",
        "explicit_qty",
        "risk_amount",
        "risk_percent",
        "risk_reward_ratio",
        "risk_timeframe",
        "current_positions",
        # Raw broker-unit overrides from the (untrusted) payload: on entries
        # the risk-sized 'qty' must reach the adapter unshadowed (#79).
        "volume",
        "ctrader_volume",
    })
    preserved_metadata = {
        key: value for key, value in metadata.items() if key not in sizing_keys
    }
    final_metadata = {**sized_metadata, **preserved_metadata}
    sized_signal = sized_signal.model_copy(update={"metadata": final_metadata})
    # C1/B2: the risk-sized qty is binding — pass it explicitly so the
    # adapter sizes exactly zero times (never with default config).
    return await adapter.submit_order(
        sized_signal, qty=float(sized_signal.metadata["qty"])
    )


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
    metadata["source"] = str(metadata.get("source") or "discord")
    # C1: producer-stable idempotency key for non-Discord signals so a
    # redelivery returns the canonical row instead of a second broker order.
    # Content-stable (same logical signal → same key); distinct signals
    # differ by price/levels/time. Discord redeliveries keep using their
    # discord_message_id.
    if not metadata.get("discord_message_id") and not metadata.get("idempotency_key"):
        key_material = "|".join(
            [
                str(deployment.id),
                str(signal.symbol).upper(),
                str(signal.action).lower(),
                repr(float(signal.entry_price)),
                repr(signal.stop_loss),
                repr(signal.take_profit),
                str(signal.timeframe),
                str(metadata.get("signal_timestamp") or metadata.get("timestamp") or ""),
            ]
        )
        metadata["idempotency_key"] = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]
    if signal.action in {"sell", "short"} and not _deployment_allows_short(deployment):
        metadata["exit_only"] = True
        metadata["risk_sizing_mode"] = EXIT_ONLY_RISK_SIZING_MODE
    # Whitelist at the conversion site itself (B-2): never default an unmapped
    # action to SELL. The TradeSignal Literal gates construction today, but a
    # future loosening (e.g. adding "close") must not silently become a live
    # short — reject it here with a structured reason.
    if signal.action in {"buy", "long"}:
        live_action = "buy"
    elif signal.action in {"sell", "short"}:
        live_action = "sell"
    else:
        raise ValueError(
            f"Unsupported signal action at conversion: {signal.action!r} "
            f"(symbol={signal.symbol}); refusing to "
            "default it to a live order side."
        )
    return LiveSignal(
        id=uuid.uuid4(),
        deployment_id=str(deployment.id),
        symbol=signal.symbol,
        action=live_action,
        theoretical_price=float(signal.entry_price),
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def _active_discord_deployments_for_account(
    account_id: str,
    deployments: list[LiveDeploymentConfig],
) -> list[LiveDeploymentConfig]:
    return [
        deployment
        for deployment in deployments
        if deployment.account_id == account_id
        and _normalize_deployment_status(deployment.status) == "ACTIVE"
        and str(deployment.signal_source or "internal").lower() == "discord"
    ]


def _deployment_is_currently_active(deployment: LiveDeploymentConfig) -> bool:
    """Re-check deployment status against the DB right before submitting.

    The listener holds a snapshot taken when the sync loop last ran. If an
    operator pauses or stops one deployment on an account whose other Discord
    deployments stay active, the stale snapshot would keep the paused
    deployment trading until the next poll. The DB read here is the
    authority at the moment an order is actually dispatched.
    """
    try:
        current = get_live_deployment(str(deployment.id))
    except Exception as exc:
        logger.warning(
            "Deployment status recheck failed; refusing to trade from stale snapshot",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "snapshot_status": deployment.status,
                    "error": str(exc),
                }
            },
        )
        return False
    if current is None:
        logger.info(
            "Deployment removed from DB; refusing to trade from stale snapshot",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "snapshot_status": deployment.status,
                }
            },
        )
        return False
    if _normalize_deployment_status(current.status) != "ACTIVE":
        logger.info(
            "Deployment is not ACTIVE; refusing to trade from stale snapshot",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "snapshot_status": deployment.status,
                    "current_status": current.status,
                }
            },
        )
        return False
    return True


def _group_discord_deployments_by_channel(
    account_id: str,
    deployments: list[LiveDeploymentConfig],
    *,
    accounts_env_values: Mapping[str, str],
) -> dict[str, list[LiveDeploymentConfig]]:
    channel_to_deployments: dict[str, list[LiveDeploymentConfig]] = {}
    for deployment in deployments:
        configured_channel = str(
            deployment.signal_config.get("channel_id")
            or accounts_env_values.get("DISCORD_CHANNEL_ID", "")
        ).strip()
        if not configured_channel:
            logger.warning(
                "Discord deployment missing channel id",
                extra={"data": {"deployment_id": str(deployment.id), "account_id": account_id}},
            )
            continue
        # #81: a literal "${...}" channel id never matches a real channel —
        # warn loudly (startup-grade error surfaced per deployment) instead
        # of staying silently mute.
        if "${" in configured_channel:
            logger.warning(
                "Discord deployment has unexpanded channel id; set the "
                "referenced environment variable instead of shipping '${...}'",
                extra={
                    "data": {
                        "deployment_id": str(deployment.id),
                        "account_id": account_id,
                        "channel_id": configured_channel,
                    }
                },
            )
            continue
        channel_to_deployments.setdefault(configured_channel, []).append(deployment)
    return channel_to_deployments


def _deployment_service_name(deployment: LiveDeploymentConfig) -> str:
    config = deployment.signal_config if isinstance(deployment.signal_config, dict) else {}
    return str(
        deployment.signal_source_id
        or _service_config_string(config, "service_name")
        or ""
    ).strip()


def _resolve_account_discord_token(
    accounts_env_values: Mapping[str, str],
    deployment: LiveDeploymentConfig,
) -> str:
    service_name = _deployment_service_name(deployment)
    token_env_key = ""
    if service_name:
        canonical_name = re.sub(r"[^A-Z0-9_]+", "_", service_name.upper())
        token_env_key = f"DISCORD_BOT_{canonical_name}_TOKEN"
    discord_token = ""
    if token_env_key:
        _resolved = resolve_env_value(accounts_env_values, token_env_key)
        if _resolved:
            discord_token = _resolved
    if not discord_token:
        discord_token = resolve_env_value(accounts_env_values, "DISCORD_BOT_TOKEN")
    return discord_token


def _deployment_covers_discord_symbol(
    deployment: LiveDeploymentConfig,
    symbol: str,
) -> bool:
    deployment_symbols = {item.upper() for item in deployment.symbols}
    return not deployment_symbols or symbol.upper() in deployment_symbols


def _resolve_discord_service_trusted_author_ids(
    service: Service,
    *,
    accounts_env_values: Mapping[str, str],
) -> set[str]:
    """Standalone Discord bot services configure their allowlist directly in
    the service config; env vars may supply the same value."""
    config = service.config if isinstance(service.config, dict) else {}
    direct = _trusted_author_ids_from_config(
        config.get("trusted_author_ids") or config.get("trusted_bot_ids")
    )
    if direct:
        return direct
    env_key = _service_config_string(config, "trusted_author_ids_env_key") or (
        "DISCORD_TRUSTED_AUTHOR_IDS"
    )
    raw_env = resolve_env_value(accounts_env_values, env_key)
    if not raw_env:
        return set()
    return _trusted_author_ids_from_config(
        item.strip() for item in raw_env.split(",") if item.strip()
    )


async def _submit_normalized_discord_signal(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    trade_signal: TradeSignal,
    *,
    signal_timeframe: str,
    wait_enabled: bool,
) -> None:
    # B2 (owner decision): stale signals trade immediately on outdated
    # information — discard anything older than one deployment-timeframe
    # candle instead.
    if not signal_is_fresh(trade_signal, signal_timeframe):
        logger.info(
            "Skipping stale discord signal: older than one timeframe candle",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                    "signal_timeframe": signal_timeframe,
                }
            },
        )
        return
    normalized_signal = TradeSignal(
        action=trade_signal.action,
        symbol=trade_signal.symbol,
        entry_price=trade_signal.entry_price,
        stop_loss=trade_signal.stop_loss,
        take_profit=trade_signal.take_profit,
        timeframe=signal_timeframe,
        metadata={**trade_signal.metadata, "wait_for_candle_close": wait_enabled},
    )
    try:
        normalized_signal = _apply_signal_processor(normalized_signal, deployment)
    except ValueError as exc:
        logger.warning(
            "Signal processor rejected discord signal",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                    "error": str(exc),
                }
            },
        )
        return
    if wait_enabled:
        await wait_for_candle_close(
            normalized_signal,
            normalized_signal.timeframe or signal_timeframe,
        )

    # Re-check after the candle-close wait: the wait can span minutes, and the
    # pre-dispatch snapshot was taken before it. A deployment paused during the
    # wait window must not trade at the candle-close price — and neither must
    # a signal that AGED OUT during the wait (B2 price/age revalidation).
    if not _deployment_is_currently_active(deployment):
        logger.info(
            "Skipping discord signal: deployment left ACTIVE during candle-close wait",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                    "snapshot_status": deployment.status,
                }
            },
        )
        return
    if not signal_is_fresh(normalized_signal, signal_timeframe):
        logger.info(
            "Skipping discord signal: aged out during candle-close wait",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                }
            },
        )
        return

    try:
        live_signal = _trade_signal_to_live_signal(normalized_signal, deployment)
    except ValueError:
        # Strict-boolean config errors (e.g. typo'd allow_short) must skip this
        # deployment without dropping the listener loop or sibling deployments.
        logger.exception(
            "Invalid deployment boolean config; skipping deployment",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                }
            },
        )
        return
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
        return

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


def _deployment_accepts_signal_author(
    deployment: LiveDeploymentConfig,
    signal_author_id: str,
) -> bool:
    """Per-deployment author gate applied at dispatch time.

    The listener's merged allowlist is a coarse source-level filter (any author
    trusted by any deployment on the channel passes ingestion). Before an order
    is submitted, the signal must also satisfy the target deployment's own
    allowlist so a deployment cannot trade on an author another deployment
    trusted.
    """
    allowlist = _deployment_trusted_author_ids(deployment)
    if not allowlist:
        # The listener refuses to start without any allowlist, so a deployment
        # without its own one inherits the channel-merged set; refusing here
        # would dead-lock signals for deployments relying on that merge.
        return True
    if not signal_author_id:
        # No author metadata (e.g. internal replay): the ingestion gate already
        # decided authorship for live Discord signals; do not re-block here.
        return True
    return signal_author_id in allowlist


def _trusted_author_ids_from_config(value: object) -> set[str]:
    """Normalize a configured trusted-author allowlist.

    Accepts a list of IDs or a single ID string; empty/None yields an empty
    set. IDs are compared case-insensitively (see discord_source gate).
    """
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, Mapping):
        return set()
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return {
            str(item).strip().lower()
            for item in value
            if str(item).strip()
        }
    return set()


def _deployment_trusted_author_ids(
    deployment: LiveDeploymentConfig,
    *,
    accounts_env_values: Mapping[str, str] | None = None,
) -> set[str]:
    """Return this deployment's trusted-author allowlist (may be empty).

    Only `trusted_author_ids` is documented (docs/signal_bots.md); the other
    spellings are accepted for operator convenience but are not advertised.

    #81: IDs stay out of git — per-deployment env
    ``DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS`` (service name canonicalized
    like the token key), fallback ``DISCORD_TRUSTED_AUTHOR_IDS``.
    """
    configured = deployment.signal_config or {}
    settings = deployment.settings or {}
    direct = _trusted_author_ids_from_config(
        configured.get("trusted_author_ids")
        or configured.get("trusted_bot_ids")
        or settings.get("trusted_author_ids")
        or settings.get("trusted_bot_ids")
    )
    if direct:
        return direct
    if accounts_env_values is not None:
        env_source: Mapping[str, str] = accounts_env_values
    else:
        env_source = os.environ
    service_name = _deployment_service_name(deployment)
    if service_name:
        canonical = re.sub(r"[^A-Z0-9_]+", "_", service_name.upper())
        per_service_key = f"DISCORD_BOT_{canonical}_TRUSTED_AUTHOR_IDS"
        per_service = _trusted_author_ids_from_config(
            _split_env_id_list(env_source.get(per_service_key, ""))
        )
        if per_service:
            return per_service
    return _trusted_author_ids_from_config(
        _split_env_id_list(env_source.get("DISCORD_TRUSTED_AUTHOR_IDS", ""))
    )


def _split_env_id_list(raw: object) -> set[str]:
    """Split a comma/whitespace-separated env ID list into an ID set."""
    if not isinstance(raw, str) or not raw.strip():
        return set()
    return {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}


def _deployment_discord_auth_scheme(deployment: LiveDeploymentConfig) -> str:
    """Return this deployment's discord auth scheme (#81: raw|bot, default raw)."""
    configured = deployment.signal_config if isinstance(deployment.signal_config, dict) else {}
    settings = deployment.settings if isinstance(deployment.settings, dict) else {}
    raw = str(
        configured.get("discord_auth_scheme")
        or settings.get("discord_auth_scheme")
        or os.environ.get("DISCORD_AUTH_SCHEME", "raw")
    ).strip().lower()
    if raw not in ("raw", "bot"):
        raise ValueError(
            f"Unknown discord_auth_scheme {raw!r}; expected 'raw' or 'bot'."
        )
    return raw


async def _dispatch_discord_trade_signal(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployments_for_channel: list[LiveDeploymentConfig],
    trade_signal: TradeSignal,
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    # B-11: one signal may execute on every matching deployment. Stopping after
    # the first match silently starved every other ACTIVE deployment sharing the
    # channel/symbol — the one that traded was picked by DB row order.
    signal_author_id = str(trade_signal.metadata.get("discord_author_id", "")).strip().lower()
    dispatched_to_any = False
    for deployment in deployments_for_channel:
        if not _deployment_is_currently_active(deployment):
            continue
        if not _deployment_matches_stockkit_route(deployment, trade_signal.metadata):
            continue
        if not _deployment_covers_discord_symbol(deployment, trade_signal.symbol):
            continue
        if not _deployment_accepts_signal_author(deployment, signal_author_id):
            logger.debug(
                "Skipping discord signal: author not in deployment allowlist",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "discord_author_id": signal_author_id,
                    }
                },
            )
            continue

        try:
            wait_enabled = _deployment_wait_for_candle_close(deployment, accounts_env_values)
        except ValueError:
            # A typo'd boolean must skip the deployment, not kill the listener
            # loop or other deployments on the same account. It also must not
            # default into immediate market orders.
            logger.exception(
                "Invalid deployment boolean config; skipping deployment",
                extra={
                    "data": {
                        "account_id": account_id,
                        "strategy_name": deployment.strategy_name,
                    }
                },
            )
            return

        dispatched_to_any = True
        await _submit_normalized_discord_signal(
            account_id,
            adapter,
            deployment,
            trade_signal,
            signal_timeframe=_deployment_signal_timeframe(deployment),
            wait_enabled=wait_enabled,
        )
    if not dispatched_to_any:
        logger.debug(
            "Skipping discord signal with symbol not covered by deployment",
            extra={
                "data": {
                    "account_id": account_id,
                    "channel_id": str(trade_signal.metadata.get("discord_channel_id", "")).strip(),
                    "symbol": trade_signal.symbol,
                }
            },
        )


async def _run_discord_signal_listener_for_account(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployments: list[LiveDeploymentConfig],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    active_discord_deployments = _active_discord_deployments_for_account(account_id, deployments)
    if not active_discord_deployments:
        logger.info(
            "No active discord deployments for account; discord listener idle",
            extra={"data": {"account_id": account_id}},
        )
        return

    channel_to_deployments = _group_discord_deployments_by_channel(
        account_id,
        active_discord_deployments,
        accounts_env_values=accounts_env_values,
    )
    if not channel_to_deployments:
        logger.warning(
            "No valid discord channels configured for active discord deployments",
            extra={"data": {"account_id": account_id}},
        )
        return

    # Resolve a real account token (account/user tokens, not a generic bot token).
    primary_deployment = next(iter(active_discord_deployments))
    # The trusted-author allowlist is the ingestion source gate (Decision
    # Record): authors trusted by ANY deployment on the polled channels are
    # merged into one allowlist. Without an explicit allowlist the listener
    # refuses to start — an open gate must never trade.
    merged_trusted_author_ids: set[str] = set()
    for deployment in channel_to_deployments.values():
        for channel_deployment in deployment:
            merged_trusted_author_ids |= _deployment_trusted_author_ids(
                channel_deployment, accounts_env_values=accounts_env_values
            )
    if not merged_trusted_author_ids:
        logger.warning(
            "Discord listener not started: no trusted_author_ids configured "
            "for any active discord deployment",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_ids": sorted(
                        str(deployment.id) for deployment in active_discord_deployments
                    ),
                }
            },
        )
        return
    source = DiscordSignalSource(
        token=_resolve_account_discord_token(
            accounts_env_values,
            primary_deployment,
        ),
        channel_ids=set(channel_to_deployments.keys()),
        timeframe=_deployment_signal_timeframe(primary_deployment),
        trusted_author_ids=merged_trusted_author_ids,
        auth_scheme=_deployment_discord_auth_scheme(primary_deployment),
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

            await _dispatch_discord_trade_signal(
                account_id,
                adapter,
                deployments_for_channel,
                trade_signal,
                accounts_env_values=accounts_env_values,
            )
    finally:
        await source.disconnect()


def _create_adapter_for_account(
    account: LiveAccountConfig,
    *,
    accounts_env_values: Mapping[str, str] | None = None,
    accounts_env_path: Path | str | None = None,
) -> BaseLiveAdapter | None:
    # C1: an explicitly passed (even empty) mapping wins over os.environ —
    # a falsy-but-present mapping must not fall through to process env.
    env_values = accounts_env_values if accounts_env_values is not None else os.environ
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
        app_id, app_secret, access_token, refresh_token, demo = credentials
        account_identifier = ctrader_account_identifier(account)
        try:
            return CTraderLiveAdapter(
                app_id=app_id,
                app_secret=app_secret,
                access_token=access_token,
                refresh_token=refresh_token,
                account_id=account_identifier,
                demo=demo,
                account_config_id=account.id,
                accounts_env_path=accounts_env_path or DEFAULT_ACCOUNTS_ENV,
                connect_max_attempts=1,
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
        _clear_ctrader_blocked_state(account_id)
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
            if _is_terminal_discord_runtime_error(exc):
                _DISCORD_AUTH_BLOCKED_ACCOUNTS.add(account_id)
                _DISCORD_AUTH_BLOCKED_REASONS[account_id] = str(exc)
                logger.warning(
                    "Discord listener auth failed; stopping retries for account "
                    "until credentials change",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "error": str(exc),
                        }
                    },
                )
                return
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
    token = resolve_env_value(accounts_env_values, token_env_key)
    channel_id = _service_config_string(config, "channel_id")
    return token_env_key, token, channel_id


async def _run_discord_bot_service_listener(service: Service, source: DiscordSignalSource) -> None:
    backoff_seconds = 2
    service_id = str(service.id)
    while True:
        try:
            async for _signal in source.listen_signals():
                if source.is_paused:
                    return
            await asyncio.sleep(1)
            backoff_seconds = 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_terminal_discord_runtime_error(exc):
                _block_discord_service(service_id, exc)
                logger.warning(
                    "Discord bot auth failed; stopping retries",
                    extra={
                        "data": {
                            "service_id": service_id,
                            "service_name": service.name,
                            "error": str(exc),
                        }
                    },
                )
                await asyncio.to_thread(
                    update_service_status,
                    service_id,
                    ServiceStatus.FAILED,
                )
                return
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
    _DISCORD_SERVICE_AUTH_BLOCKED_IDS.discard(service_id)
    _DISCORD_SERVICE_AUTH_BLOCKED_REASONS.pop(service_id, None)


def _running_discord_service_ids(discord_services: list[Service]) -> set[str]:
    return {
        str(service.id)
        for service in discord_services
        if service.status == ServiceStatus.RUNNING
    }


def _prune_stale_discord_service_blocks(
    discord_services: list[Service],
    running_service_ids: set[str],
) -> None:
    for service_id in list(_DISCORD_SERVICE_AUTH_BLOCKED_IDS):
        if service_id in running_service_ids:
            continue
        _DISCORD_SERVICE_AUTH_BLOCKED_IDS.discard(service_id)
        _DISCORD_SERVICE_AUTH_BLOCKED_REASONS.pop(service_id, None)


async def _stop_inactive_discord_bots(
    discord_services: list[Service],
    running_service_ids: set[str],
    *,
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
) -> None:
    service_by_id = {str(service.id): service for service in discord_services}
    for service_id in sorted(set(active_discord_bots) - running_service_ids):
        service = service_by_id.get(service_id)
        await _stop_discord_bot_service(
            service_id,
            active_discord_bots=active_discord_bots,
            active_discord_bot_tasks=active_discord_bot_tasks,
            paused=bool(service and service.status == ServiceStatus.PAUSED),
        )


def _is_discord_service_reconnect_blocked(service_id: str) -> bool:
    until = _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.get(service_id)
    if until is None:
        return False
    if datetime.now(timezone.utc) >= until:
        _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.pop(service_id, None)
        return False
    return True


async def _reconnect_discord_bot_source(
    service: Service,
    source: DiscordSignalSource,
    service_id: str,
    *,
    running_task: asyncio.Task[None] | None,
) -> None:
    if source.is_connected or (running_task is not None and not running_task.done()):
        return
    if _is_discord_service_reconnect_blocked(service_id):
        retry_not_before = _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.get(service_id)
        logger.debug(
            "Discord bot reconnect deferred; inside transient retry backoff",
            extra={
                "data": {
                    "service_id": service_id,
                    "service_name": service.name,
                    "retry_not_before": retry_not_before.isoformat()
                    if retry_not_before
                    else None,
                }
            },
        )
        return
    try:
        await source.reconnect()
        await asyncio.to_thread(
            update_service_status,
            service_id,
            ServiceStatus.RUNNING,
            last_activity=datetime.now(timezone.utc),
        )
        _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.pop(service_id, None)
        logger.info(
            "Reconnected Discord bot %s",
            service.name,
        )
    except Exception as exc:
        if _is_terminal_discord_runtime_error(exc):
            _block_discord_service(service_id, exc)
            logger.warning(
                "Discord bot auth failed; skipping reconnect until credentials change",
                extra={
                    "data": {
                        "service_id": service_id,
                        "service_name": service.name,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
            return
        # Transient failure: keep the service recoverable. The next sync tick
        # retries after the backoff floor instead of the old behavior of
        # writing FAILED once and never re-arming recovery (B-5).
        _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE[service_id] = datetime.now(
            timezone.utc
        ) + timedelta(seconds=DISCORD_RECONNECT_RETRY_FLOOR_SECONDS)
        logger.warning(
            "Discord bot reconnect failed; will retry after backoff",
            extra={
                "data": {
                    "service_id": service_id,
                    "service_name": service.name,
                    "retry_in_seconds": DISCORD_RECONNECT_RETRY_FLOOR_SECONDS,
                    "error": str(exc),
                }
            },
        )


async def _ensure_discord_bot_source(
    service: Service,
    token: str,
    channel_id: str,
    *,
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
    trusted_author_ids: set[str] | None = None,
    auth_scheme: str = "raw",
) -> None:
    service_id = str(service.id)
    source = active_discord_bots.get(service_id)
    expected_channel_ids = {channel_id}
    if (
        source is None
        or source.token != token
        or source.channel_ids != expected_channel_ids
        or getattr(source, "auth_scheme", "raw") != auth_scheme
    ):
        _DISCORD_SERVICE_AUTH_BLOCKED_IDS.discard(service_id)
        _DISCORD_SERVICE_AUTH_BLOCKED_REASONS.pop(service_id, None)
        if source is not None:
            await _stop_discord_bot_service(
                service_id,
                active_discord_bots=active_discord_bots,
                active_discord_bot_tasks=active_discord_bot_tasks,
            )
        # #81: the source gate requires a non-empty allowlist — fail closed
        # here instead of constructing a muted source.
        if not trusted_author_ids:
            raise ValueError(
                f"Discord service {service.name!r} has no trusted_author_ids; "
                "refusing to ingest signals from unverified authors."
            )
        source = DiscordSignalSource(
            bot_name=service.name,
            token=token,
            channel_id=channel_id,
            service_id=service_id,
            trusted_author_ids=trusted_author_ids,
            auth_scheme=auth_scheme,
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
    await _reconnect_discord_bot_source(
        service,
        source,
        service_id,
        running_task=running_task,
    )


async def _sync_running_discord_bot(
    service: Service,
    *,
    active_discord_bots: dict[str, DiscordSignalSource],
    active_discord_bot_tasks: dict[str, asyncio.Task[None]],
    accounts_env_values: Mapping[str, str],
) -> None:
    service_id = str(service.id)
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
        return

    trusted_author_ids = _resolve_discord_service_trusted_author_ids(
        service, accounts_env_values=accounts_env_values
    )
    if not trusted_author_ids:
        logger.warning(
            "Discord service missing trusted_author_ids; refusing open gate",
            extra={"data": {"service_id": service_id, "service_name": service.name}},
        )
        await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
        return
    service_config = service.config if isinstance(service.config, dict) else {}
    auth_scheme = str(
        service_config.get("discord_auth_scheme")
        or os.environ.get("DISCORD_AUTH_SCHEME", "raw")
    ).strip().lower()
    if auth_scheme not in ("raw", "bot"):
        logger.warning(
            "Discord service has unknown discord_auth_scheme",
            extra={"data": {"service_id": service_id, "auth_scheme": auth_scheme}},
        )
        await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
        return

    await _ensure_discord_bot_source(
        service,
        token,
        channel_id,
        active_discord_bots=active_discord_bots,
        active_discord_bot_tasks=active_discord_bot_tasks,
        trusted_author_ids=trusted_author_ids,
        auth_scheme=auth_scheme,
    )


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
    running_service_ids = _running_discord_service_ids(discord_services)
    _prune_stale_discord_service_blocks(discord_services, running_service_ids)

    await _stop_inactive_discord_bots(
        discord_services,
        running_service_ids,
        active_discord_bots=active_discord_bots,
        active_discord_bot_tasks=active_discord_bot_tasks,
    )

    for service in discord_services:
        if service.status != ServiceStatus.RUNNING:
            continue
        if str(service.id) in _DISCORD_SERVICE_AUTH_BLOCKED_IDS:
            continue
        try:
            await _sync_running_discord_bot(
                service,
                active_discord_bots=active_discord_bots,
                active_discord_bot_tasks=active_discord_bot_tasks,
                accounts_env_values=accounts_env_values,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One misconfigured service (e.g. missing allowlist) must not
            # starve its siblings for the rest of the tick.
            logger.warning(
                "Discord service sync failed; continuing with siblings",
                extra={
                    "data": {
                        "service_id": str(service.id),
                        "service_name": service.name,
                        "error": str(exc),
                    }
                },
            )
            continue


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
                if _DISCORD_SERVICE_AUTH_BLOCKED_IDS:
                    logger.info(
                        "Credentials changed; clearing blocked Discord bot services",
                        extra={
                            "data": {
                                "service_ids": sorted(_DISCORD_SERVICE_AUTH_BLOCKED_IDS),
                            }
                        },
                    )
                _DISCORD_SERVICE_AUTH_BLOCKED_IDS.clear()
                _DISCORD_SERVICE_AUTH_BLOCKED_REASONS.clear()

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


def _log_terminal_balance_auth_failure(
    account_id: str,
    adapter: BaseLiveAdapter,
    exc: BaseException,
) -> None:
    logger.warning(
        _ACCOUNT_NOT_AUTHORIZED_LOG,
        account_id,
        extra={
            "data": {
                "account_id": account_id,
                "adapter": type(adapter).__name__,
                "error": str(exc),
            }
        },
    )


async def _handle_terminal_balance_error(
    adapters: dict[str, BaseLiveAdapter],
    account_id: str,
    adapter: BaseLiveAdapter,
    exc: BaseException,
) -> None:
    _block_ctrader_account(account_id, exc)
    adapters.pop(account_id, None)
    _log_terminal_balance_auth_failure(account_id, adapter, exc)
    await _disconnect_adapter(
        adapter,
        account_id=account_id,
        reason="terminal_auth_failure",
    )


async def _poll_adapter_balance(
    adapters: dict[str, BaseLiveAdapter],
    account_id: str,
    adapter: BaseLiveAdapter,
    snapshot_time: datetime,
) -> None:
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
    except CTraderAuthTerminalError as exc:
        await _handle_terminal_balance_error(adapters, account_id, adapter, exc)
    except Exception as exc:
        if _is_ctrader_pending_deferred_error(exc):
            logger.debug(
                "Skipping cTrader balance snapshot while request is pending",
                extra={
                    "data": {
                        "account_id": account_id,
                        "adapter": type(adapter).__name__,
                        "error": str(exc),
                    }
                },
            )
            return
        if _is_terminal_ctrader_runtime_error(exc):
            await _handle_terminal_balance_error(adapters, account_id, adapter, exc)
            return
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


#: Per-account balance timeout (B2): one hung broker must not stall the tick.
BALANCE_POLL_TIMEOUT_SECONDS = 30.0


async def _poll_balances(
    adapters: dict[str, BaseLiveAdapter], *, interval_seconds: int = BALANCE_POLL_INTERVAL_SECONDS
) -> None:
    while True:
        snapshot_time = datetime.now(timezone.utc)
        logger.debug(
            "Polling balances for adapters",
            extra={"data": {"adapter_count": len(adapters)}},
        )
        # B2: fan out with gather (per-account timestamp preserved) instead
        # of sequential awaits — one slow broker no longer blocks all
        # accounts. return_exceptions keeps one failure from killing the tick.
        ready = [
            (account_id, adapter)
            for account_id, adapter in list(adapters.items())
            if _adapter_is_ready(adapter)
        ]

        async def _poll_one(account_id: str, adapter: BaseLiveAdapter) -> None:
            try:
                await asyncio.wait_for(
                    _poll_adapter_balance(adapters, account_id, adapter, snapshot_time),
                    timeout=BALANCE_POLL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Live balance polling timed out",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "adapter": type(adapter).__name__,
                            "timeout_seconds": BALANCE_POLL_TIMEOUT_SECONDS,
                        }
                    },
                )

        await asyncio.gather(
            *(_poll_one(account_id, adapter) for account_id, adapter in ready),
            return_exceptions=True,
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
    for account_id in list(_DISCORD_AUTH_BLOCKED_ACCOUNTS):
        if account_id in active_discord_account_ids:
            continue
        _DISCORD_AUTH_BLOCKED_ACCOUNTS.discard(account_id)
        _DISCORD_AUTH_BLOCKED_REASONS.pop(account_id, None)


def _start_missing_discord_tasks(
    adapters: dict[str, BaseLiveAdapter],
    discord_tasks: dict[str, asyncio.Task[None]],
    active_discord_account_ids: set[str],
    deployments_by_account: dict[str, list[LiveDeploymentConfig]],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    for account_id in sorted(active_discord_account_ids):
        if account_id in _DISCORD_AUTH_BLOCKED_ACCOUNTS:
            continue
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
        if account_id in _CTRADER_AUTH_BLOCKED_ACCOUNTS:
            continue
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
        if not _adapter_is_ready(adapter):
            logger.info(
                "Adapter not connected yet; waiting for next adapter-connect retry",
                extra={
                    "data": {
                        "account_id": account_id,
                        "adapter": type(adapter).__name__,
                    }
                },
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
    accounts_env_path: Path | str | None = None,
) -> None:
    if account_id in _CTRADER_AUTH_BLOCKED_ACCOUNTS:
        return
    adapter = active_adapters.get(account_id)
    if adapter is not None:
        if _adapter_is_ready(adapter):
            return
    else:
        account = account_lookup.get(account_id)
        if account is None:
            logger.warning(
                "Active deployment references unknown account",
                extra={"data": {"account_id": account_id}},
            )
            return
        try:
            adapter = _create_adapter_for_account(
                account,
                accounts_env_values=accounts_env_values,
                accounts_env_path=accounts_env_path,
            )
        except ValueError as exc:
            # Strict-boolean config errors (e.g. a typo'd ALPACA_PAPER_ACC{N})
            # must skip this account, not propagate through asyncio.gather and
            # abort the whole deployment sync iteration every cycle.
            logger.exception(
                "Invalid credential config; skipping account",
                extra={"data": {"account_id": account_id, "error": str(exc)}},
            )
            return
        if adapter is None:
            return
        active_adapters[account_id] = adapter

    try:
        await adapter.connect()
    except Exception as exc:
        if _is_terminal_ctrader_runtime_error(exc):
            _block_ctrader_account(account_id, exc)
            active_adapters.pop(account_id, None)
            await _disconnect_adapter(
                adapter,
                account_id=account_id,
                reason="terminal_auth_failure",
            )
            logger.warning(
                _ACCOUNT_NOT_AUTHORIZED_LOG,
                account_id,
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


async def _connect_active_accounts(
    account_ids: set[str],
    account_lookup: dict[str, LiveAccountConfig],
    active_adapters: dict[str, BaseLiveAdapter],
    *,
    accounts_env_values: Mapping[str, str],
    accounts_env_path: Path | str | None = None,
) -> None:
    # B2: per-account isolation — one failing connect must not fail the
    # whole tick (bare gather propagated the first exception).
    results = await asyncio.gather(
        *(
            _ensure_adapter_for_active_account(
                account_id,
                account_lookup,
                active_adapters,
                accounts_env_values=accounts_env_values,
                accounts_env_path=accounts_env_path,
            )
            for account_id in sorted(account_ids)
        ),
        return_exceptions=True,
    )
    for account_id, result in zip(sorted(account_ids), results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.warning(
                "Adapter connect failed; will retry",
                extra={
                    "data": {"account_id": account_id, "error": str(result)}
                },
            )


async def _rotate_changed_ctrader_adapters(
    changed_account_ids: set[str],
    active_adapters: dict[str, BaseLiveAdapter],
    active_tasks: dict[str, asyncio.Task[None]],
    active_discord_tasks: dict[str, asyncio.Task[None]],
) -> None:
    cancelled_tasks: list[asyncio.Task[None]] = []
    for account_id in sorted(changed_account_ids):
        for tasks in (active_tasks, active_discord_tasks):
            task = tasks.pop(account_id, None)
            if task is not None and not task.done():
                task.cancel()
                cancelled_tasks.append(task)

    if cancelled_tasks:
        await asyncio.gather(*cancelled_tasks, return_exceptions=True)

    for account_id in sorted(changed_account_ids):
        adapter = active_adapters.pop(account_id, None)
        if adapter is None:
            continue
        await _disconnect_adapter(
            adapter,
            account_id=account_id,
            reason="credentials_changed",
        )
        logger.info(
            "Rotated cTrader adapter after credential change",
            extra={"data": {"account_id": account_id}},
        )


def _clear_discord_auth_blocked_accounts() -> None:
    if _DISCORD_AUTH_BLOCKED_ACCOUNTS:
        logger.info(
            "Credentials changed; clearing blocked Discord listener accounts",
            extra={"data": {"account_ids": sorted(_DISCORD_AUTH_BLOCKED_ACCOUNTS)}},
        )
    _DISCORD_AUTH_BLOCKED_ACCOUNTS.clear()
    _DISCORD_AUTH_BLOCKED_REASONS.clear()


def _group_deployments_by_account(
    deployments: list[LiveDeploymentConfig],
) -> dict[str, list[LiveDeploymentConfig]]:
    deployments_by_account: dict[str, list[LiveDeploymentConfig]] = {}
    for deployment in deployments:
        deployments_by_account.setdefault(deployment.account_id, []).append(deployment)
    return deployments_by_account


def _deployment_active_account_ids(
    deployments: list[LiveDeploymentConfig],
) -> set[str]:
    return {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) == "ACTIVE"
    }


def _discord_active_account_ids(
    deployments: list[LiveDeploymentConfig],
) -> set[str]:
    return {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) == "ACTIVE"
        and str(deployment.signal_source or "internal").lower() == "discord"
        and _deployment_has_discord_channel(deployment)
    }


async def _disconnect_blocked_adapters(
    active_adapters: dict[str, BaseLiveAdapter],
    blocked_account_ids: Iterable[str],
) -> None:
    for blocked_account_id in sorted(blocked_account_ids):
        adapter = active_adapters.pop(blocked_account_id, None)
        if adapter is not None:
            await _disconnect_adapter(
                adapter,
                account_id=blocked_account_id,
                reason="terminal_auth_blocked",
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
                # cTrader blocks are cleared below only for accounts whose
                # selected credential signature actually changed.
                _clear_discord_auth_blocked_accounts()
            accounts, deployments = await _load_db_accounts_and_deployments()
            validate_live_account_routes(accounts)
            changed_ctrader_account_ids = _refresh_ctrader_blocked_state_for_accounts(
                accounts, accounts_env_values
            )
            await _rotate_changed_ctrader_adapters(
                changed_ctrader_account_ids,
                active_adapters,
                active_tasks,
                active_discord_tasks,
            )
            deployments_by_account = _group_deployments_by_account(deployments)
            account_lookup = {account.id: account for account in accounts}
            _cleanup_finished_listener_tasks(active_tasks)
            _cleanup_finished_discord_tasks(active_discord_tasks)
            _log_deployment_status_observations(deployments, known_deployment_statuses)

            active_account_ids = _deployment_active_account_ids(deployments)
            await _disconnect_blocked_adapters(
                active_adapters, active_account_ids & _CTRADER_AUTH_BLOCKED_ACCOUNTS
            )
            logger.debug(
                "Evaluating deployment activation state",
                extra={
                    "data": {
                        "active_account_ids": sorted(active_account_ids),
                        "known_adapters": sorted(active_adapters.keys()),
                    }
                },
            )
            runnable_account_ids = set(active_account_ids) - _CTRADER_AUTH_BLOCKED_ACCOUNTS

            # B2: disconnect against runnable (blocked-excluded) ids — a
            # blocked account must never keep a live adapter via the
            # active-id set.
            await _disconnect_inactive_adapters(active_adapters, runnable_account_ids)

            active_discord_account_ids = _discord_active_account_ids(deployments)

            await _connect_active_accounts(
                runnable_account_ids,
                account_lookup,
                active_adapters,
                accounts_env_values=accounts_env_values,
                accounts_env_path=accounts_env,
            )

            _cancel_inactive_tasks(active_tasks, runnable_account_ids)
            _start_missing_tasks(active_adapters, active_tasks, runnable_account_ids)
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
    await asyncio.to_thread(_recover_failed_services)
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
    except Exception as exc:
        logger.exception("Live daemon crashed")
        with contextlib.suppress(Exception):
            capture_exception(
                exc,
                service_name="live-runner",
                context={"component": "live_daemon", "phase": "run_daemon"},
            )
        raise
    finally:
        # B2: cancel EVERYTHING (sync + balance loops leaked before) and
        # await the cancels — fire-and-forget cancel() leaves pending tasks
        # and "Task was destroyed" warnings behind.
        for task in (sync_task, service_sync_task, balance_task):
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(
                sync_task, service_sync_task, balance_task, return_exceptions=True
            )
        for task in list(active_listener_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(active_discord_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(active_discord_bot_tasks.values()):
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(
                *list(active_listener_tasks.values()),
                *list(active_discord_tasks.values()),
                *list(active_discord_bot_tasks.values()),
                return_exceptions=True,
            )
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

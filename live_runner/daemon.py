from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import math
import os
import re
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from live_runner.account_routing import (
    ctrader_account_identifier,
    validate_live_account_routes,
)
from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials
from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    BaseLiveAdapter,
    LivePipValue,
    LiveQuote,
    coerce_positive_live_qty,
    is_order_not_submitted,
    is_unbounded_tp,
    validate_live_quote_prices,
)
from live_runner.broker_order_conflicts import enforce_broker_order_conflict_halts
from live_runner.credential_env import (
    accounts_env_mtime,
    discord_bot_env_canonical_name,
    discord_bot_env_key,
    load_accounts_env_values,
    resolve_accounts_env_path,
    resolve_env_value,
)
from live_runner.ctrader_adapter import CTraderAuthTerminalError, CTraderLiveAdapter
from live_runner.deployment_policy import (
    normalize_deployment_status,
    signal_source_can_trade,
    untradeable_signal_source_reason,
)
from live_runner.exit_signals import (
    DEFAULT_QTY_TOLERANCE,
    EXIT_POSITION_SIDE_KEY,
    FULL_EXIT,
    ExitBelowStepError,
    ExitInFlightError,
    ExitInstruction,
    LedgerMismatchError,
    NothingToExitError,
    broker_position_qty,
    ensure_ledgers_match_broker,
    exit_instruction_from_metadata,
    exit_intent_metadata,
    is_exit_intent,
    is_exit_order,
    resolve_exit_order,
    usable_qty_tolerance,
)
from live_runner.kill_switch import (
    PENDING_CANCEL_ESCALATION_PASSES,
    KillSwitchMode,
    KillSwitchState,
    LiveHaltError,
    enforce_live_kill_switch,
    read_kill_switch_state,
    refuse_entry_while_halted,
)
from live_runner.loss_breaker import (
    LossBreakerBreachError,
    account_deployment_trading_pnls,
    account_loss_breaker_limits,
    account_loss_breaker_scope,
    deployment_loss_breaker_limits,
    observe_account_loss_breaker_equity,
    refuse_entry_if_loss_breaker_tripped,
    set_accounts_env_file,
)
from live_runner.promotion import (
    deployment_signal_processor_name,
    enforce_promotion_gate,
    is_real_money,
)
from live_runner.service_discovery import discover_discord_bots_from_env
from live_runner.signal_archive import (
    SignalDecisionRecorder,
    get_signal_archive,
    market_features_from_frame,
    record_signal_decision,
)
from live_runner.signal_sources import (
    DiscordSignalSource,
    signal_is_fresh,
    wait_for_candle_close,
)
from live_runner.signal_sources.discord_source import DiscordAuthenticationError
from live_runner.stockkit_routing import (
    infer_stockkit_route,
    is_stockkit_marked,
    resolve_deployment_stockkit_route,
    validate_stockkit_deployment_route,
)
from live_runner.trailing_stops import (
    LiveTrailingStopManager,
    adapter_live_trailing_modes,
    run_trailing_stops_loop,
    validate_adapter_trailing_timeframe,
)
from risk import RiskConfig, RiskEngine, TradeSignal
from risk.live import (
    entry_deviation_sl_fraction,
    entry_gate_settings,
    resolve_live_levels,
)
from risk.models import realized_risk_amount, realized_risk_percent
from scheduler.scheduler import ServiceScheduler
from shared.alerts import SHUTDOWN_FLUSH_SECONDS, close_alert_sink, install_alert_sink
from shared.constants import normalize_timeframe
from shared.database import (
    LIVE_DISPATCH_ENTRY_STATUSES,
    LIVE_DISPATCH_EXIT_STATUSES,
    LIVE_SUBMISSION_CLAIM_TTL_SECONDS,
    claim_live_deployment_submission,
    clear_live_deployment_stopped_enforced,
    create_service,
    flag_live_ledger_reconciliation,
    get_account_net_filled_qty_by_deployment,
    get_live_account,
    get_live_deployment,
    get_service,
    list_account_unfilled_exit_orders,
    list_live_accounts,
    list_live_deployment_unfilled_exit_orders,
    list_live_deployments,
    list_services,
    list_stopped_enforced_deployment_ids,
    mark_live_deployment_stopped_enforced,
    release_live_deployment_submission,
    renew_live_deployment_submission,
    save_live_balance,
    update_service_status,
)
from shared.fill_keys import canonical_utc_timestamp
from shared.logging import get_logger
from shared.lot_grid import floor_qty_to_step
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
from shared.trailing import (
    ANY_BROKER,
    TRAILING_SL_CONFIG_METADATA_KEY,
    validate_live_deployment_trailing_mode,
    validate_trailing_sl_config,
)
from signal_bots.registry import get_signal_processor

initialize_sentry("live-runner")
logger = get_logger("live-runner")

_ACCOUNT_NOT_AUTHORIZED_LOG = "Account %s not authorized for token, skipping"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
BALANCE_POLL_INTERVAL_SECONDS = 60
DEPLOYMENT_POLL_INTERVAL_SECONDS = 10
# A broker-side ``pending_cancel`` order normally settles within seconds. A STOPPED
# deployment stays unstamped while one is listed (it could still fill); after
# PENDING_CANCEL_ESCALATION_PASSES consecutive enforcement passes (~1 minute at the
# poll interval) the WARNING becomes an ERROR so a stuck cancel reaches an operator.
# The kill switch's sweep applies the same rule to its own per-account counter.
_PENDING_CANCEL_PASSES: dict[str, int] = {}
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


def _is_terminal_ctrader_runtime_error(exc: BaseException | str) -> bool:
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


def _adapter_is_demo(adapter: BaseLiveAdapter) -> bool:
    """Whether this adapter provably routes to a demo/paper account.

    Fail closed on every uncertainty: an unknown adapter type, a missing flag,
    or a flag that is not literally ``True`` is not demo. cTrader calls it
    ``demo`` and Alpaca ``paper``.

    Deliberately reads the adapter and not ``LiveAccountConfig.is_demo``: the
    account row's flag is not populated by ``ensure_live_account``, so gating on
    it would reject every account. The adapter is also the object that actually
    sends orders, so its own flag is the closer one to the risk.
    """
    if isinstance(adapter, AlpacaLiveAdapter):
        return getattr(adapter, "paper", None) is True
    if isinstance(adapter, CTraderLiveAdapter):
        return getattr(adapter, "demo", None) is True
    # Unknown adapter types still have to prove it, so a double or a future
    # adapter cannot slip through by being unrecognised. Only a literal True on
    # either documented flag counts.
    return (
        getattr(adapter, "demo", None) is True
        or getattr(adapter, "paper", None) is True
    )


def _trailing_eligible_adapters(
    adapters: Mapping[str, BaseLiveAdapter],
) -> dict[str, BaseLiveAdapter]:
    """Adapters whose broker-side stops the trailing loop (#167) may amend.

    The same bar every order has to clear: connected, and provably
    demo/paper. The real-money hard block therefore also covers stop
    amendments, not only new orders.
    """
    return {
        account_id: adapter
        for account_id, adapter in list(adapters.items())
        if _adapter_is_ready(adapter) and _adapter_is_demo(adapter)
    }


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


def _accounts_env_file(accounts_env: Path | str | None) -> Path:
    """Resolve a named-or-default accounts env file at call time.

    The three loops below used to freeze ``accounts_env: Path = DEFAULT_ACCOUNTS_ENV`` in their
    signatures, which bound the path at import time and ignored every later redirect of the module
    constant. Resolving here keeps the default honest.
    """
    return resolve_accounts_env_path(accounts_env, default=DEFAULT_ACCOUNTS_ENV)


def _load_accounts_env_values(accounts_env: Path) -> dict[str, str]:
    return load_accounts_env_values(accounts_env)


def _accounts_env_mtime(accounts_env: Path) -> int | None:
    return accounts_env_mtime(accounts_env)


def _discover_and_register_discord_services(
    accounts_env: Path | str | None = None, *, log_summary: bool = True
) -> list[Service]:
    # The same file the rest of the daemon resolved: discovery that read the module default instead
    # would discover bots from a file the operator redirected away from.
    discovered = discover_discord_bots_from_env(accounts_env)
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
        # The write is conditional on the row still not being PAUSED, so a
        # pause that landed after the list read wins. Only arm the reconnect
        # floor when the service actually moved to RUNNING.
        recovered = update_service_status(
            str(service.id),
            ServiceStatus.RUNNING,
            respect_operator_intent=True,
        )
        if recovered is None or recovered.status != ServiceStatus.RUNNING:
            logger.info(
                "Not recovering a service whose operator paused it",
                extra={"data": {"service_id": str(service.id)}},
            )
            continue
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
    if not isinstance(exc, CTraderAuthTerminalError) and not _is_terminal_ctrader_runtime_error(
        exc
    ):
        return
    reason = str(exc).strip() or "cTrader authorization failed"
    _CTRADER_AUTH_BLOCKED_ACCOUNTS.add(account_id)
    _CTRADER_AUTH_BLOCKED_REASONS[account_id] = reason


def _coerce_live_float(
    value: Any,
    *,
    field_name: str,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _coerce_live_int(
    value: Any,
    *,
    field_name: str,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{field_name} must be an integer")
    parsed = int(numeric)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _risk_config_for_deployment(deployment: LiveDeploymentConfig) -> RiskConfig:
    settings = deployment.settings or {}
    risk_per_trade_raw = settings.get(
        "max_risk_per_trade",
        settings.get("risk_per_trade_pct", 0.01),
    )
    max_total_exposure_raw = settings.get("max_total_exposure", 0.05)
    max_positions_raw = settings.get("max_positions", 5)
    sl_multiplier_raw = settings.get("default_sl_atr_multiplier", 2.0)
    fallback_stop_pct_raw = settings.get("fallback_stop_pct", 0.02)
    max_notional_raw = settings.get("max_notional_per_trade", 0.20)
    allow_fallback_raw = settings.get("allow_fallback_stop", True)
    reject_missing_raw = settings.get("reject_missing_stop", False)

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
        fallback_stop_pct=_coerce_live_float(
            fallback_stop_pct_raw,
            field_name="fallback_stop_pct",
            minimum=0.0,
        ),
        max_notional_per_trade=_coerce_live_float(
            max_notional_raw,
            field_name="max_notional_per_trade",
            minimum=0.0,
        ),
        allow_fallback_stop=allow_fallback_raw,
        reject_missing_stop=reject_missing_raw,
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
    """The deployment's own signal timeframe, resolved by the model."""
    return deployment.signal_timeframe


def _deployment_has_discord_channel(deployment: LiveDeploymentConfig) -> bool:
    configured = deployment.signal_config or {}
    return bool(str(configured.get("channel_id") or "").strip())


def _deployment_signal_processor_name(deployment: LiveDeploymentConfig) -> str:
    # One definition, shared with the promotion gate: the gate must hash the
    # processor that is actually applied here.
    return deployment_signal_processor_name(deployment)


def _live_account_broker_login(account_id: str) -> str:
    """Return the broker-reported account number for a local account id.

    Alpaca reports an account number (e.g. ``PA35HB6T7815``) and cTrader a
    ctid; both live in ``broker_login``/``ctid``. Routing checks compare against
    that broker identity because the local id is a different identifier space.
    Returns an empty string when the account is unknown, which makes routing
    validation fail closed.
    """
    try:
        account = get_live_account(str(account_id))
    except Exception as exc:  # except-kind: money-decision
        # `exception()` and not `error()`: this is an unexpected failure on the
        # routing path, and the traceback is the only thing that says whether the
        # account lookup itself blew up or something below it did. The empty
        # string returned here makes routing fail closed, so without the
        # traceback the operator sees a refusal with no cause.
        logger.exception(
            "Could not resolve broker identity for account",
            extra={"data": {"account_id": account_id, "error": repr(exc)}},
        )
        return ""
    if account is None:
        return ""
    return str(getattr(account, "broker_login", "") or getattr(account, "ctid", "") or "")


def _deployment_matches_stockkit_route(
    deployment: LiveDeploymentConfig,
    signal_metadata: Mapping[str, object],
    *,
    account_broker_login: str | None = None,
    accounts_env_values: Mapping[str, str] | None = None,
) -> bool:
    marked = is_stockkit_marked(deployment)
    route = infer_stockkit_route(
        signal_source_id=deployment.signal_source_id,
        signal_config=deployment.signal_config,
        settings=deployment.settings,
        metadata=signal_metadata,
    )
    if not route and not marked:
        return True
    try:
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login=account_broker_login,
            accounts_env_values=accounts_env_values,
        )
        if marked:
            # The explicit marking is authoritative over inference (#121); it was
            # validated just above, so this cannot raise a new refusal.
            route = resolve_deployment_stockkit_route(deployment)
    except ValueError as exc:
        logger.warning(
            "Blocking misrouted StockKit deployment",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "stockkit_marked": marked,
                    "error": str(exc),
                }
            },
        )
        if marked:
            # #121 decision 5: a marked StockKit deployment whose route is unknown
            # or unresolvable is a broken service, not one skipped signal.
            _mark_discord_deployment_service_unhealthy(deployment, str(exc))
        return False
    signal_route = infer_stockkit_route(metadata=signal_metadata)
    return not signal_route or signal_route == route


def _mark_discord_deployment_service_unhealthy(
    deployment: LiveDeploymentConfig, reason: str
) -> None:
    """Mark the deployment's Discord service FAILED, never overriding PAUSE/STOP.

    Best effort: the refusal itself already happened, so a lookup or write
    failure here is logged and swallowed rather than tearing down the fan-out.
    """
    try:
        service_id = _service_id_for_discord_deployment(deployment)
        if service_id is None:
            logger.error(
                "StockKit route refused but the deployment has no unique service row "
                "to mark unhealthy",
                extra={"data": {"deployment_id": str(deployment.id), "reason": reason}},
            )
            return
        update_service_status(
            service_id, ServiceStatus.FAILED, respect_operator_intent=True
        )
        logger.error(
            "Marked Discord service unhealthy: StockKit route cannot be resolved",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "service_id": service_id,
                    "reason": reason,
                }
            },
        )
    except Exception:  # except-kind: loop-survival
        logger.exception(
            "Could not mark the Discord service unhealthy after a StockKit route refusal",
            extra={"data": {"deployment_id": str(deployment.id)}},
        )


def _timeframe_age_budget_seconds(timeframe: str) -> float:
    # Through `normalize_timeframe`, not a bare `.lower()`. It used to lower-case only, so `M1`/`m1`
    # missed the `1m` entry and fell through to the 300.0 default: the same 1-minute candle got a
    # 600s effective staleness budget under `M1` and 300s under `1m`, because the daemon's caller
    # computes `max(300.0, 2.0 * budget)`. `normalize_timeframe` is what every other consumer uses -
    # `execution_timing._timeframe_seconds` routes through it - so the two halves of the same signal
    # path disagreed on what `M1` means. Two spellings of one timeframe must not buy two policies.
    normalized = normalize_timeframe(timeframe)
    seconds_by_timeframe = {
        "1m": 60.0,
        "5m": 300.0,
        "15m": 900.0,
        "30m": 1800.0,
        "1h": 3600.0,
        "4h": 14400.0,
        "1d": 86400.0,
        "1w": 604800.0,
    }
    return seconds_by_timeframe.get(normalized, 300.0)


def _validate_signal_age_before_wait(
    signal: TradeSignal,
    deployment: LiveDeploymentConfig,
    *,
    timeframe: str,
    require_producer_timestamp: bool,
) -> None:
    """Fail closed on a signal that is already too old to trade.

    The candle-close wait can span minutes, so a signal that arrives stale (or
    with no producer timestamp at all) would otherwise be executed at the
    candle-close price on information that is no longer valid. The budget scales
    with the deployment's signal timeframe unless the operator pinned
    ``max_signal_age_seconds``.

    ``require_producer_timestamp`` is a required keyword, not a default, so a
    new caller has to state its policy instead of inheriting one. It is passed
    by :func:`_submit_normalized_discord_signal`, which is already *the*
    Discord path -- the source is known at the call site, so the gate must not
    re-derive it from ``metadata["source"]``. It used to: the Discord producer
    writes ``"discord"`` at parse time and then overwrites it with
    ``f"discord_{bot_name.lower()}"`` (``discord_nitro``), so an exact
    comparison against ``"discord"`` never matched production and the
    fail-closed branch was dead code. A signal whose age cannot be established
    was then passed on to the processor and to ``submit_order`` unvalidated for
    every timeframe :func:`signal_is_fresh` cannot parse (``tick``, ``daily``,
    ``m5``), because that gate returns "fresh" when it has no budget at all.
    """
    metadata = signal.metadata or {}
    received_raw = str(metadata.get("received_at") or "").strip()
    if not received_raw:
        if require_producer_timestamp:
            raise ValueError("Discord signal is missing received_at")
        return
    try:
        received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Discord signal has an invalid received_at timestamp") from exc
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    configured = (deployment.signal_config or {}).get("max_signal_age_seconds")
    if configured is None:
        max_age = max(300.0, 2.0 * _timeframe_age_budget_seconds(timeframe))
    else:
        # Through the shared coercer: a boolean or a non-finite value is a config
        # error, not a budget. `float(True)` silently means one second.
        max_age = _coerce_live_float(configured, field_name="max_signal_age_seconds")
    if max_age <= 0:
        raise ValueError("max_signal_age_seconds must be positive")
    age_seconds = (datetime.now(timezone.utc) - received_at).total_seconds()
    if age_seconds < -300:
        raise ValueError("Discord signal timestamp is more than 5 minutes in the future")
    if age_seconds > max_age:
        raise ValueError(
            f"Discord signal is stale: age={age_seconds:.1f}s exceeds {max_age:.1f}s"
        )


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
    *,
    adapter: BaseLiveAdapter | None = None,
    decision: SignalDecisionRecorder | None = None,
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
    if decision is not None:
        # Issue #308: when the signal carries an OHLCV frame
        # (``signal.metadata["market_data"]``), record the cheap signal-time
        # features it supports. Production Discord signals do not carry one
        # today — market features are computed offline from historical OHLCV
        # by ``message_created_at`` instead. Best effort: a feature failure
        # must never change the trading decision.
        try:
            decision.set_features(market_features_from_frame(market_data))
        except Exception:  # except-kind: loop-survival
            logger.debug("Signal archive market features failed", exc_info=True)
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
    # #150 fail closed at signal time: a processed signal that asks for a
    # trailing mode the live path cannot execute through THIS adapter is
    # rejected here — the signal is not traded, instead of running with the
    # requested trailing silently dropped. Live support is per broker and
    # needs the adapter's stop-amendment capability (#167); without an
    # adapter only mode "none" passes. The caller logs the refusal and skips
    # the signal.
    validate_trailing_sl_config(
        (processed.metadata or {}).get(TRAILING_SL_CONFIG_METADATA_KEY),
        supported=adapter_live_trailing_modes(adapter),
        context=(
            f"live deployment '{deployment.strategy_name}' ({deployment.id}) "
            f"signal processor '{processor_name}'"
        ),
    )
    validate_adapter_trailing_timeframe(
        (processed.metadata or {}).get(TRAILING_SL_CONFIG_METADATA_KEY),
        timeframe,
        adapter=adapter,
        context=(
            f"live deployment '{deployment.strategy_name}' ({deployment.id}) "
            f"signal processor '{processor_name}'"
        ),
    )
    if decision is not None:
        processed_metadata = processed.metadata or {}
        trailing_config = processed_metadata.get(TRAILING_SL_CONFIG_METADATA_KEY)
        decision.set_features(
            {
                "processor_atr": processed_metadata.get("atr"),
                "supertrend_green_at_entry": processed_metadata.get(
                    "supertrend_green_at_entry"
                ),
                "slippage_vs_signal": processed_metadata.get("slippage_vs_signal"),
                "trailing_mode": (
                    trailing_config.get("mode")
                    if isinstance(trailing_config, Mapping)
                    else None
                ),
            }
        )
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
    """Size a live entry with the deployment's risk config.

    Levels come from :func:`risk.live.resolve_live_levels`, the one resolver the
    submission path also uses (#125): entry is metadata ``entry_price`` else
    ``theoretical_price``, SL/TP the shared metadata aliases. Only trade
    descriptors (levels, ATR, timeframe) are read from the untrusted payload.

    Args:
        pip_value: Trusted account-currency P&L of one qty unit per 1.0 price
            move (#146), from the broker or the caller. Never read from signal
            metadata. ``None`` means 1.0, which is only right for a USD-quoted
            share/coin in a USD account; :func:`prepare_order_with_risk`, the
            only production caller, always passes a broker-resolved value.
        current_positions: Open-position count from the broker. ``None`` when
            the broker cannot report positions; the payload's
            ``current_positions`` is never used in its place.

    The returned metadata carries the engine's quantity/risk figures and
    ``resolved_stop_loss``: the stop the engine actually sized on (the signal
    stop, or the fallback it resolved). Any payload value under that key is
    overwritten so it cannot diverge from the sized distance.
    """
    metadata: dict[str, object] = dict(signal.metadata or {})
    entry_price, stop_loss, take_profit = resolve_live_levels(signal)
    # Broker truth only. A payload count is dropped, not forwarded.
    metadata.pop("current_positions", None)
    trusted_positions = 0
    if current_positions is not None:
        trusted_positions = _coerce_live_int(
            current_positions, field_name="current_positions", minimum=0
        )
        metadata["current_positions"] = trusted_positions
    resolved_timeframe = (
        str(metadata.get("timeframe") or deployment.timeframe).strip() or deployment.timeframe
    )
    resolved_pip = (
        1.0
        if pip_value is None
        else _coerce_live_float(pip_value, field_name="pip_value", minimum=0.0)
    )

    trade_signal = TradeSignal(
        action=signal.action,
        symbol=signal.symbol,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        timeframe=resolved_timeframe,
        # Whitelist what the engine may read: the payload's ATR is a trade
        # descriptor; position state is broker truth.
        metadata={
            "atr": metadata.get("atr"),
            "current_positions": trusted_positions,
            "enforce_stop_direction": True,
        },
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
    sized_stop = sized_order.signal.stop_loss
    if sized_stop is not None:
        metadata["resolved_stop_loss"] = float(sized_stop)
    else:
        metadata.pop("resolved_stop_loss", None)
    return signal.model_copy(update={"metadata": metadata})


#: Gate settings and their defaults are validated by
#: :func:`risk.live.entry_gate_settings` (#125).

#: How far a quote time may lie in the future before it is treated as a clock
#: or feed fault rather than skew between our clock and the broker's.
ENTRY_QUOTE_FUTURE_SKEW_SECONDS = 2.0


def _utc_now() -> datetime:
    """The gate's clock (a seam so tests can freeze it)."""
    return datetime.now(timezone.utc)

#: Float slack for the gate comparison. Prices are decimal, so a quote exactly
#: at the threshold (e.g. (100 - 99.6) / 4) must not be rejected by binary
#: rounding noise (0.10000000000000142 > 0.1). 1e-9 of the stop distance is far
#: below any tradable price increment.
_ENTRY_DEVIATION_TOLERANCE = 1e-9

#: Metadata keys the entry-deviation gate writes. Always all of them, so a
#: payload value under one of these names can never survive to persistence.
_ENTRY_QUOTE_METADATA_KEYS = (
    "entry_quote_bid",
    "entry_quote_ask",
    "entry_quote_side",
    "entry_quote_price",
    "entry_quote_time",
    "entry_quote_source",
    "entry_quote_time_source",
    "entry_quote_age_seconds",
    "max_entry_quote_age_seconds",
    "entry_deviation_sl_fraction",
    "entry_deviation_direction",
    "max_entry_deviation_sl_fraction",
    "entry_gate_rejection_rule",
)

#: Rejection rules of the entry-deviation gate, checked in this order. Quote
#: freshness comes first: a quote that is not current says nothing about where
#: the market is, so no price rule may be judged on it. The crossed-level rules
#: come next: they name the more specific problem when the quote has also moved
#: further than the deviation limit.
ENTRY_GATE_RULE_QUOTE_TIME_MISSING = "quote_time_missing"
ENTRY_GATE_RULE_QUOTE_TIME_INVALID = "quote_time_invalid"
ENTRY_GATE_RULE_QUOTE_TIME_IN_FUTURE = "quote_time_in_future"
ENTRY_GATE_RULE_QUOTE_STALE = "quote_stale"
ENTRY_GATE_RULE_AT_STOP_LOSS = "quote_at_or_beyond_stop_loss"
ENTRY_GATE_RULE_AT_TAKE_PROFIT = "quote_at_or_beyond_take_profit"
ENTRY_GATE_RULE_TOWARD_STOP_LOSS = "deviation_toward_stop_loss"
ENTRY_GATE_RULE_AWAY_FROM_STOP_LOSS = "deviation_away_from_stop_loss"


def _entry_gate_rejection_rule(
    action: str,
    *,
    quote_price: float,
    stop_loss: float,
    take_profit: float | None,
    deviation: float,
    max_fraction: float,
) -> str | None:
    """Which gate rule, if any, rejects a quote (#125, revised decision)."""
    buy = action == "buy"
    if (quote_price <= stop_loss) if buy else (quote_price >= stop_loss):
        return ENTRY_GATE_RULE_AT_STOP_LOSS
    if take_profit is not None and (
        (quote_price >= take_profit) if buy else (quote_price <= take_profit)
    ):
        return ENTRY_GATE_RULE_AT_TAKE_PROFIT
    if abs(deviation) - max_fraction > _ENTRY_DEVIATION_TOLERANCE:
        return (
            ENTRY_GATE_RULE_TOWARD_STOP_LOSS
            if deviation > 0
            else ENTRY_GATE_RULE_AWAY_FROM_STOP_LOSS
        )
    return None


def _quote_time_rule(
    timestamp: object, *, now: datetime, max_age_seconds: float
) -> tuple[str | None, float | None]:
    """Freshness verdict and age in seconds for a quote time (#125 review).

    Fails closed: a missing time, anything but a timezone-aware datetime, a
    time beyond the clock-skew allowance in the future, or a quote older than
    the limit all reject.
    """
    if timestamp is None:
        return ENTRY_GATE_RULE_QUOTE_TIME_MISSING, None
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        return ENTRY_GATE_RULE_QUOTE_TIME_INVALID, None
    age = (now - timestamp).total_seconds()
    if -age > ENTRY_QUOTE_FUTURE_SKEW_SECONDS:
        return ENTRY_GATE_RULE_QUOTE_TIME_IN_FUTURE, age
    if age > max_age_seconds:
        return ENTRY_GATE_RULE_QUOTE_STALE, age
    return None, age


def _validated_quote_prices(quote: object, *, symbol: str) -> tuple[float, float]:
    """The entry gate's quote check (#125).

    The rules live in :func:`~live_runner.base_adapter.validate_live_quote_prices`
    because cTrader's conversion chain (#146) validates the very same shape to
    size an entry from it; only the wording is this layer's.
    """
    return validate_live_quote_prices(quote, symbol=symbol, label="Broker quote")


# Upper bound for one quote fetch of the entry-deviation gate (#125). Above the
# adapters' own limits (cTrader: subscribe 10 s + quote 5 s), so it only cuts off
# a call that has no bound of its own (Alpaca's REST quote).
ENTRY_QUOTE_FETCH_TIMEOUT_SECONDS = 20.0


async def _check_entry_deviation(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float | None,
) -> dict[str, object]:
    """Gate a live entry against the current broker quote (#125).

    The signal entry is compared with the side the market order fills
    against (ask for a buy, bid for a sell), as a fraction of the SL distance.
    It rejects (revised maintainer decision) when the quote is

    * at or beyond the SL (buy: ask <= SL, sell: bid >= SL),
    * at or beyond the TP, when one is set (buy: ask >= TP, sell: bid <= TP),
    * further than ``max_entry_deviation_sl_fraction`` of the SL distance from
      the entry in EITHER direction: toward the stop the setup may be invalid,
      away from it the order chases and a fixed stop risks more than sized.

    Before any price rule the quote must be current: a missing or invalid
    broker time, a time beyond ``ENTRY_QUOTE_FUTURE_SKEW_SECONDS`` in the
    future, or a quote older than ``max_entry_quote_age_seconds`` rejects (an
    after-hours "latest" quote can be hours old).

    The quote is a gate only: sizing has already run on the signal levels and
    is not touched.

    Fails closed: no quote support, a fetch error, or an unusable quote all
    reject the entry. Returns the quote/deviation figures to persist on the
    signal.
    """
    gate_settings = entry_gate_settings(deployment.settings)
    max_fraction = gate_settings.max_deviation_sl_fraction
    max_age = gate_settings.max_quote_age_seconds
    log_data: dict[str, object] = {
        "deployment_id": str(deployment.id),
        "account_id": deployment.account_id,
        "symbol": signal.symbol,
        "action": signal.action,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "max_entry_deviation_sl_fraction": max_fraction,
        "max_entry_quote_age_seconds": max_age,
    }
    get_quote = getattr(adapter, "get_quote", None)
    try:
        if not callable(get_quote):
            raise NotImplementedError(f"{type(adapter).__name__} has no get_quote")
        # Bounded here as well as in the adapters: a stalled broker call must
        # not stall dispatch. A late quote would fail the freshness rule anyway.
        quote = await asyncio.wait_for(
            get_quote(signal.symbol), timeout=ENTRY_QUOTE_FETCH_TIMEOUT_SECONDS
        )
    except Exception as exc:  # except-kind: money-decision
        logger.warning(
            "Live quote unavailable; refusing the entry (deviation gate fails closed)",
            extra={"data": {**log_data, "error": str(exc)}},
        )
        raise ValueError(
            f"Refusing live entry for {signal.symbol}: no live broker quote for the "
            f"entry deviation gate ({type(exc).__name__}: {exc})."
        ) from exc
    try:
        bid, ask = _validated_quote_prices(quote, symbol=signal.symbol)
    except ValueError as exc:
        logger.warning(
            "Live quote unusable; refusing the entry (deviation gate fails closed)",
            extra={"data": {**log_data, "error": str(exc)}},
        )
        raise
    side = "ask" if signal.action == "buy" else "bid"
    quote_price = ask if signal.action == "buy" else bid
    deviation = entry_deviation_sl_fraction(
        signal.action,
        entry_price=entry_price,
        stop_loss=stop_loss,
        quote_price=quote_price,
    )
    quote_time = quote.timestamp if isinstance(quote, LiveQuote) else None
    time_rule, quote_age = _quote_time_rule(
        quote_time, now=_utc_now(), max_age_seconds=max_age
    )
    record: dict[str, object] = {
        "entry_quote_bid": bid,
        "entry_quote_ask": ask,
        "entry_quote_side": side,
        "entry_quote_price": quote_price,
        "entry_quote_time": quote_time.isoformat() if isinstance(quote_time, datetime) else None,
        "entry_quote_source": str(quote.source or type(adapter).__name__),
        "entry_quote_time_source": str(quote.timestamp_source or ""),
        "entry_quote_age_seconds": quote_age,
        "max_entry_quote_age_seconds": max_age,
        # Signed: positive = toward the stop, negative = away from it.
        "entry_deviation_sl_fraction": deviation,
        "entry_deviation_direction": (
            "toward_stop_loss"
            if deviation > 0
            else "away_from_stop_loss"
            if deviation < 0
            else "none"
        ),
        "max_entry_deviation_sl_fraction": max_fraction,
        "entry_gate_rejection_rule": None,
    }
    rule = time_rule or _entry_gate_rejection_rule(
        signal.action,
        quote_price=quote_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        deviation=deviation,
        max_fraction=max_fraction,
    )
    if rule is not None:
        record["entry_gate_rejection_rule"] = rule
        logger.warning(
            "Live entry deviation gate rejected the signal",
            extra={"data": {**log_data, **record}},
        )
        raise ValueError(
            f"Refusing live entry for {signal.symbol}: entry deviation gate rejected "
            f"({rule}): signed deviation {deviation:.4f} of the SL distance "
            f"(positive = toward the stop), limit +/-{max_fraction}; quote time "
            f"{record['entry_quote_time']!r}, age {quote_age!r}s, limit {max_age}s "
            f"(entry={entry_price}, stop={stop_loss}, take_profit={take_profit}, "
            f"{side}={quote_price})."
        )
    return record


# Upper bound for resolving one pip value (#146): the account and asset lookups
# plus up to MAX_CONVERSION_HOPS conversion quotes, each bounded by the adapter.
PIP_VALUE_FETCH_TIMEOUT_SECONDS = 60.0

#: Metadata keys the pip-value resolution writes. Always all of them, so a
#: payload value under one of these names can never survive to persistence
#: or reach the adapter (cTrader reads ``pip_value_units_per_qty``).
_PIP_VALUE_METADATA_KEYS = (
    "pip_value",
    "pip_value_source",
    "pip_value_units_per_qty",
    "pip_value_conversion_rate",
    "pip_value_quote_asset",
    "pip_value_account_asset",
    "pip_value_conversion_symbols",
    "pip_value_conversion_quote_times",
    "pip_value_conversion_quote_ages_seconds",
)


def _pip_value_record(pip_value: float, *, source: str) -> dict[str, object]:
    record: dict[str, object] = dict.fromkeys(_PIP_VALUE_METADATA_KEYS)
    record["pip_value"] = float(pip_value)
    record["pip_value_source"] = source
    return record


async def _resolve_live_pip_value(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
) -> tuple[float, dict[str, object]]:
    """The broker's pip value for a live entry, failing closed (#146).

    The value is the account-currency P&L of one adapter qty unit per 1.0
    price move (:class:`~live_runner.base_adapter.LivePipValue`). It never
    comes from signal metadata. With an implicit 1.0, cTrader FX entries were
    sized ~1000x too large (a 0.036 USD budget became a 36.18 USD loss at the
    stop on the demo).

    Refuses the entry, with a logged reason, when the adapter cannot provide
    one, the call fails or times out, the value is not finite and positive, or
    a conversion quote it was derived from is missing a time, in the future or
    older than the deployment's ``max_entry_quote_age_seconds`` (the same
    freshness rule as the entry gate). Returns the value and the record to
    persist on the signal.
    """
    log_data: dict[str, object] = {
        "deployment_id": str(deployment.id),
        "account_id": deployment.account_id,
        "symbol": signal.symbol,
    }

    def _refuse(reason: str) -> ValueError:
        logger.warning(
            "Pip value unavailable; refusing the entry (sizing fails closed)",
            extra={"data": {**log_data, "reason": reason}},
        )
        return ValueError(
            f"Refusing live entry for {signal.symbol}: no trusted pip value in account "
            f"currency ({reason})."
        )

    get_pip_value = getattr(adapter, "get_pip_value", None)
    try:
        if not callable(get_pip_value):
            raise NotImplementedError(f"{type(adapter).__name__} has no get_pip_value")
        resolved = await asyncio.wait_for(
            get_pip_value(signal.symbol), timeout=PIP_VALUE_FETCH_TIMEOUT_SECONDS
        )
    except Exception as exc:  # except-kind: money-decision
        raise _refuse(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(resolved, LivePipValue):
        raise _refuse(f"adapter returned {type(resolved).__name__}, not LivePipValue")
    try:
        value = _coerce_live_float(resolved.value, field_name="pip value")
        units_per_qty = _coerce_live_float(resolved.units_per_qty, field_name="units per qty")
        rate = _coerce_live_float(resolved.conversion_rate, field_name="conversion rate")
    except ValueError as exc:
        raise _refuse(str(exc)) from exc
    if value <= 0 or units_per_qty <= 0 or rate <= 0:
        raise _refuse(
            f"pip value {value!r}, units per qty {units_per_qty!r} and conversion rate "
            f"{rate!r} must all be > 0"
        )
    if not math.isclose(value, units_per_qty * rate, rel_tol=1e-9, abs_tol=0.0):
        raise _refuse(
            f"pip value {value!r} != units per qty {units_per_qty!r} * conversion rate {rate!r}"
        )
    converts = resolved.quote_asset != resolved.account_asset or not math.isclose(
        rate, 1.0, rel_tol=1e-12
    )
    if converts and not resolved.conversion_quotes:
        raise _refuse(f"conversion rate {rate!r} came without the quotes it was derived from")
    max_age = entry_gate_settings(deployment.settings).max_quote_age_seconds
    now = _utc_now()
    quote_times: list[str] = []
    quote_ages: list[float | None] = []
    for index, quote in enumerate(resolved.conversion_quotes):
        hop = (
            resolved.conversion_symbols[index]
            if index < len(resolved.conversion_symbols)
            else f"hop {index}"
        )
        quote_time = quote.timestamp if isinstance(quote, LiveQuote) else None
        rule, age = _quote_time_rule(quote_time, now=now, max_age_seconds=max_age)
        if rule is not None:
            raise _refuse(
                f"conversion quote {hop} rejected ({rule}): time {quote_time!r}, "
                f"age {age!r}s, limit {max_age}s"
            )
        quote_times.append(quote_time.isoformat() if isinstance(quote_time, datetime) else "")
        quote_ages.append(age)
    record = _pip_value_record(value, source=str(resolved.source or type(adapter).__name__))
    record.update(
        {
            "pip_value_units_per_qty": units_per_qty,
            "pip_value_conversion_rate": rate,
            "pip_value_quote_asset": str(resolved.quote_asset),
            "pip_value_account_asset": str(resolved.account_asset),
            "pip_value_conversion_symbols": list(resolved.conversion_symbols),
            "pip_value_conversion_quote_times": quote_times,
            "pip_value_conversion_quote_ages_seconds": quote_ages,
        }
    )
    logger.info(
        "Resolved live pip value",
        extra={"data": {**log_data, **record}},
    )
    return value, record


def _position_qty_tolerance(adapter: BaseLiveAdapter, symbol: str) -> float:
    """The instrument's quantity precision, or the strict default.

    An adapter that cannot say (no method, an error, a nonsensical value) gets
    the strict default: an unknown precision must never widen the match.
    """
    method = getattr(adapter, "position_qty_tolerance", None)
    if not callable(method):
        return DEFAULT_QTY_TOLERANCE
    try:
        raw = method(symbol)
    except Exception as exc:  # except-kind: money-decision
        logger.warning(
            "Could not read the instrument quantity precision; using the strict default",
            extra={"data": {"symbol": symbol, "error": str(exc)}},
        )
        return DEFAULT_QTY_TOLERANCE
    tolerance = usable_qty_tolerance(raw)
    if tolerance == DEFAULT_QTY_TOLERANCE and raw != DEFAULT_QTY_TOLERANCE:
        logger.warning(
            "Adapter reported an unusable quantity precision; using the strict default",
            extra={"data": {"symbol": symbol, "reported": repr(raw)}},
        )
    return tolerance


def _round_exit_qty_to_adapter_step(
    adapter: BaseLiveAdapter, symbol: str, qty: float

) -> float:
    """``qty`` floored to the adapter's exit quantity step for ``symbol``.

    An adapter that names no step is left to its own conversion (both real
    adapters name one and floor exits again themselves). Raises
    :class:`ExitBelowStepError` when less than one step remains.
    """
    method = getattr(adapter, "exit_qty_step", None)
    if not callable(method):
        return float(qty)
    step = float(method(symbol))
    rounded = floor_qty_to_step(qty, step)
    if rounded <= 0.0:
        raise ExitBelowStepError(
            f"Exit for {symbol} ignored: the requested {qty:g} is below the "
            f"instrument's quantity step {step:g}; closing one step would close more "
            "than this deployment owns or was asked to close.",
            requested_qty=qty,
            qty_step=step,
        )
    return rounded


def _log_exit_below_step(
    account_id: str, deployment: LiveDeploymentConfig, symbol: str, exc: ExitBelowStepError
) -> None:
    logger.warning(
        "Exit signal ignored: the exit is below the instrument's quantity step",
        extra={
            "data": {
                "account_id": account_id,
                "deployment_id": str(deployment.id),
                "symbol": symbol,
                "requested_qty": exc.requested_qty,
                "qty_step": exc.qty_step,
                "reason": str(exc),
            }
        },
    )


def _describe_exit_rows(
    rows: Iterable[Mapping[str, Any]], deployment: LiveDeploymentConfig
) -> str:
    return ", ".join(
        f"signal {row.get('signal_id')} / order {row.get('broker_order_id') or '?'}"
        + (
            ""
            if str(row.get("deployment_id") or deployment.id) == str(deployment.id)
            else f" of deployment {row.get('deployment_id')}"
        )
        for row in rows
    )


async def _ensure_no_exit_in_flight(
    adapter: BaseLiveAdapter, deployment: LiveDeploymentConfig, symbol: str
) -> list[dict[str, Any]]:
    """Refuse while an earlier exit of THIS deployment may still be working.

    Returns the still-working exits of OTHER deployments on the account (empty
    when none), which the caller subtracts from the broker position it sizes
    against.

    Deployments on one account share a netted broker position. This
    deployment's own earlier exit blocks a new one: sizing off a ledger without
    its fills would close the same units twice. Another deployment's working
    exit must not drop this exit (a message fans out to every deployment, and
    with the market closed the first exit is always still working when the
    second is processed); its unfilled quantity is reserved instead.

    Earlier exits whose persisted fills cover what they asked for are done. Any
    other one is checked against the broker's working orders, by broker order id
    or by client order id (the signal id; the broker id can be missing when the
    response was lost). An adapter that cannot list its working orders cannot
    prove an earlier exit is over, so that refuses too. Raises
    :class:`ExitInFlightError`.
    """
    own = await asyncio.to_thread(
        list_live_deployment_unfilled_exit_orders, str(deployment.id), symbol
    )
    others = await asyncio.to_thread(
        _list_other_deployments_unfilled_exits, deployment, symbol
    )
    pending = [*own, *others]
    if not pending:
        return []
    working_order_ids = getattr(adapter, "working_order_ids", None)
    if not callable(working_order_ids):
        raise ExitInFlightError(
            f"Refusing exit for {symbol}: a previous exit on this account is not "
            "known to be finished and this broker adapter cannot list its working "
            "orders to prove it."
        )
    try:
        working = {str(order_id) for order_id in await working_order_ids()}
    except Exception as exc:  # except-kind: money-decision
        raise ExitInFlightError(
            f"Refusing exit for {symbol}: a previous exit on this account is not "
            f"known to be finished and the broker's working orders could not be read "
            f"({exc})."
        ) from exc
    blocking = [
        row
        for row in pending
        if (row.get("broker_order_id") and str(row["broker_order_id"]) in working)
        or str(row.get("signal_id") or "") in working
    ]
    own_blocking = [
        row
        for row in blocking
        if str(row.get("deployment_id") or deployment.id) == str(deployment.id)
    ]
    if own_blocking:
        raise ExitInFlightError(
            f"Refusing exit for {symbol}: a previous exit of this deployment is "
            f"still working at the broker ({_describe_exit_rows(own_blocking, deployment)}); "
            "the ledger and the broker position do not include its fills yet."
        )
    return [row for row in blocking if row not in own_blocking]


def _reserved_by_other_exits(
    rows: Iterable[Mapping[str, Any]], *, close_action: str, symbol: str, deployment_id: str
) -> float:
    """Unfilled quantity of other deployments' working exits that close the same side.

    An exit row without a readable requested quantity cannot be sized: it raises,
    so the caller refuses rather than guessing how much is already spoken for.
    """
    reserved = 0.0
    for row in rows:
        action = str(row.get("action") or "").strip().lower()
        if action and action != close_action:
            continue
        requested = row.get("requested_qty")
        if requested is None or not math.isfinite(float(requested)):
            raise ExitInFlightError(
                f"Refusing exit for {symbol}: the working exit of deployment "
                f"{row.get('deployment_id')} (signal {row.get('signal_id')}) has no "
                "readable quantity, so what remains of the position for deployment "
                f"{deployment_id} cannot be told."
            )
        reserved += max(float(requested) - float(row.get("filled_qty") or 0.0), 0.0)
    return reserved


def _list_other_deployments_unfilled_exits(
    deployment: LiveDeploymentConfig, symbol: str
) -> list[dict[str, Any]]:
    return list_account_unfilled_exit_orders(
        deployment.account_id, symbol, exclude_deployment_id=str(deployment.id)
    )


#: Exits being prepared and submitted, per running event loop, account and symbol.
_EXIT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _exit_lock(account_id: str, symbol: str) -> asyncio.Lock:
    """The lock serialising exits of every deployment on one account and symbol.

    Held from sizing through submission (which persists the signal), so a second
    exit cannot size or pass :func:`_ensure_no_exit_in_flight` in the window where
    the first has read the broker position but not yet persisted its order. The
    in-flight guard covers what is already persisted; this covers dispatchers of
    this process racing before that.
    """
    locks = _EXIT_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault((str(account_id), str(symbol).upper()), asyncio.Lock())


async def _alert_ledger_mismatch(
    deployment: LiveDeploymentConfig,
    symbol: str,
    exc: LedgerMismatchError,
    *,
    own_qty: float,
    exit_qty: float,
) -> None:
    """Operator alert (ERROR, which Sentry turns into an event) for a ledger gap.

    The exit is still submitted, capped at the broker position (#121 decision 2).
    The residual risk is stated in the alert: on a netted account, a broker-side
    close made for another deployment lowers the broker position, and the cap
    then closes part of that deployment's share.

    A mismatch means a close the ledgers never saw happened - a flatten or a
    manual close no adapter could attribute (#144 decision). The ERROR alert is
    the surface; the durable ``ledger_reconciliation_required`` audit flag is
    what an operator and the periodic broker reconciliation (#330) act on.
    """
    await asyncio.to_thread(
        flag_live_ledger_reconciliation,
        account_id=deployment.account_id,
        symbol=symbol,
        cause="exit_ledger_mismatch",
        detail={
            "deployment_id": str(deployment.id),
            "own_ledger_qty": float(own_qty),
            "exit_qty_before_step": float(exit_qty),
            "reason": str(exc),
            **exc.log_data(),
        },
    )
    logger.error(
        "ALERT: deployment ledgers do not match the broker position; submitting "
        "the exit capped at the broker position. If another deployment's "
        "position was closed broker-side, this exit may close part of that "
        "deployment's share; reconcile the ledgers",
        extra={
            "data": {
                "alert": "exit_ledger_mismatch",
                "account_id": deployment.account_id,
                "deployment_id": str(deployment.id),
                "symbol": symbol,
                "own_ledger_qty": float(own_qty),
                "exit_qty_before_step": float(exit_qty),
                "reason": str(exc),
                **exc.log_data(),
            }
        },
    )


async def prepare_exit_intent_order(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    instruction: ExitInstruction | None = None,
) -> tuple[LiveSignal, float]:
    """Size an exit against this deployment's own position (#121).

    Every exit goes through here: a parsed exit/close message (its instruction is
    read from the metadata) and a plain or momentum-reversal SELL on a deployment
    that cannot short (``instruction`` is a full exit). The quantity is the
    deployment's net persisted fills for the symbol (the ledger), capped at the
    broker position, times the instruction's fraction, so another deployment's
    share of a netted position stays open. The close side comes from that
    position, never from the message. No earlier exit of this deployment and
    symbol may still be working. When the ledgers of the deployments on the
    account do not add up to the broker position (or the own quantity exceeds
    it), the capped exit still goes out and an ERROR alert is logged.

    Raises :class:`NothingToExitError` when there is nothing of this
    deployment's to close (the caller logs and ignores it),
    :class:`ExitInFlightError` while an earlier exit may be working, and
    ``ValueError`` when a close would be ambiguous or would extend exposure.
    Nothing here places an order.

    Deliberately independent of the entry path (no risk sizing, no SL/TP guard).
    This function checks no deployment status itself: the caller's status gate decides
    ("ACTIVE or PAUSED for a close", #126), so an exit on a PAUSED deployment does get
    here and is sized like any other - a pause stops new positions, not closes.
    """
    metadata = dict(signal.metadata or {})
    if instruction is None:
        instruction = exit_instruction_from_metadata(metadata)
    get_positions = getattr(adapter, "get_positions", None)
    if not callable(get_positions):
        raise ValueError("Exit signal requires a broker adapter with get_positions support")
    # One exit at a time per deployment and symbol: a second exit sized while
    # the first is still working would size off a ledger without its fills.
    other_working = await _ensure_no_exit_in_flight(adapter, deployment, signal.symbol)
    positions = list(await get_positions())
    ledgers = await asyncio.to_thread(
        get_account_net_filled_qty_by_deployment, deployment.account_id, signal.symbol
    )
    ledger_qty = float(ledgers.get(str(deployment.id), 0.0))
    broker_qty = broker_position_qty(positions, signal.symbol)
    # Other deployments' exits still working at the broker are not in the broker
    # position yet but will leave it: their unfilled quantity is spoken for and
    # this exit may only close what remains (floored at 0).
    reserved = 0.0
    sized_positions = positions
    effective_cap = abs(broker_qty)
    if other_working and broker_qty != 0.0:
        reserved = _reserved_by_other_exits(
            other_working,
            close_action="sell" if broker_qty > 0 else "buy",
            symbol=signal.symbol,
            deployment_id=str(deployment.id),
        )
        effective_cap = max(abs(broker_qty) - reserved, 0.0)
        if reserved > 0.0 and effective_cap > 0.0:
            sized_positions = [
                {**position, "qty": math.copysign(effective_cap, broker_qty)}
                if str(position.get("symbol") or "").strip().upper() == signal.symbol.upper()
                and float(position.get("qty") or 0.0) != 0.0
                else position
                for position in positions
            ]
    # Resolved against the real position first, so "nothing of this deployment's
    # to close" and ambiguous closes are reported as such.
    close_action, target_qty = resolve_exit_order(
        symbol=signal.symbol,
        ledger_qty=ledger_qty,
        broker_positions=positions,
        instruction=instruction,
    )
    if reserved > 0.0 and effective_cap <= 0.0:
        raise ExitInFlightError(
            f"Refusing exit for {signal.symbol}: the whole broker position "
            f"({broker_qty:g}) is already spoken for by exits of other deployments "
            f"on account {deployment.account_id} that are still working "
            f"({_describe_exit_rows(other_working, deployment)}); nothing is left "
            "for this deployment to close, so its position stays open."
        )
    if sized_positions is not positions:
        close_action, target_qty = resolve_exit_order(
            symbol=signal.symbol,
            ledger_qty=ledger_qty,
            broker_positions=sized_positions,
            instruction=instruction,
        )
    # An exit closes only the own ledger quantity, capped at the broker position
    # (#121 decision 2), here reduced by what other working exits will take.
    # When the ledgers do not explain the broker position, or the own quantity
    # exceeds the cap, the capped exit is still submitted, but an operator must
    # hear about it: a close the ledgers never saw (a stop or target leg, a
    # flatten, a manual close) happened.
    tolerance = _position_qty_tolerance(adapter, signal.symbol)
    try:
        ensure_ledgers_match_broker(
            symbol=signal.symbol,
            ledgers=ledgers,
            broker_qty=broker_qty,
            tolerance=tolerance,
        )
    except LedgerMismatchError as exc:
        await _alert_ledger_mismatch(
            deployment, signal.symbol, exc, own_qty=ledger_qty, exit_qty=target_qty
        )
    else:
        if abs(ledger_qty) > effective_cap + tolerance:
            # Offsetting ledgers of other deployments hide the overhang from
            # the total, or other working exits leave less than this
            # deployment's share; either way the own quantity exceeds the cap.
            await _alert_ledger_mismatch(
                deployment,
                signal.symbol,
                LedgerMismatchError(
                    "own ledger quantity exceeds the broker position"
                    + (
                        f" less {reserved:g} reserved by other deployments' working exits"
                        if reserved
                        else ""
                    ),
                    ledgers=ledgers,
                    broker_qty=broker_qty,
                    tolerance=tolerance,
                ),
                own_qty=ledger_qty,
                exit_qty=target_qty,
            )
    # Never round an exit up: a partial closes at most its fraction, a full exit
    # at most the own quantity (#121 review). Done here, before persistence, so
    # the stored qty is the one sent.
    target_qty = _round_exit_qty_to_adapter_step(adapter, signal.symbol, target_qty)
    matching_positions = [
        position
        for position in positions
        if str(position.get("symbol") or "").strip().upper() == signal.symbol.upper()
    ]
    for key in (
        "qty",
        "explicit_qty",
        "ctrader_volume",
        "volume",
        "position_id",
        "position_ids",
        "position_count",
        # An exit closes; it never carries the entry bracket.
        "stop_loss",
        "take_profit",
        "sl_price",
        "tp_price",
        "stop_price",
        "resolved_stop_loss",
    ):
        metadata.pop(key, None)
    # Stamp the instruction actually executed, so the persisted signal records
    # every exit the same way whichever producer it came from. A side the
    # executed instruction does not name must not survive from the payload.
    metadata.pop(EXIT_POSITION_SIDE_KEY, None)
    # A close is sized from the broker book, so neither the entry gate's nor
    # the pip value's record can describe it. Both are dropped here for the
    # same reason the entry path drops them: a payload value under one of these
    # names may never survive to persistence or reach the adapter. Leaving them
    # would let an untrusted payload wedge the close - cTrader's submit-time
    # scale guard reads pip_value_units_per_qty and refuses the order when it
    # does not match the symbol's current scale (#146, #125).
    for key in (*_ENTRY_QUOTE_METADATA_KEYS, *_PIP_VALUE_METADATA_KEYS):
        metadata.pop(key, None)
    metadata.update(exit_intent_metadata(instruction))
    metadata["qty"] = float(target_qty)
    metadata["explicit_qty"] = float(target_qty)
    metadata["exit_only"] = True
    metadata["risk_sizing_mode"] = EXIT_ONLY_RISK_SIZING_MODE
    metadata["exit_ledger_qty"] = float(ledger_qty)
    metadata["position_count"] = len(matching_positions)
    position_ids: list[int] = []
    for position in matching_positions:
        raw_position_id = position.get("position_id", position.get("positionId"))
        if raw_position_id is None:
            continue
        try:
            position_id = int(raw_position_id)
        except (TypeError, ValueError):
            continue
        if position_id > 0:
            position_ids.append(position_id)
    if len(matching_positions) == 1 and len(position_ids) == 1:
        metadata["position_id"] = position_ids[0]
    metadata["action"] = close_action
    metadata["signal_action"] = signal.action
    prepared = signal.model_copy(update={"metadata": metadata, "action": close_action})
    return prepared, float(target_qty)


class UnreadableBrokerPositionQtyError(ValueError):
    """A broker position row whose quantity cannot be read (#263).

    The open-position count gates entries: it drives ``max_positions`` and the
    risk-at-stake exposure estimate. A row that does not state a finite
    quantity says nothing about whether the account is flat, so the count has to
    refuse instead of defaulting it to zero - reading it as "no position"
    would open a new one on top of a cap that may already be full.
    """

    def __init__(
        self,
        message: str,
        *,
        symbol: str = "",
        qty: object = None,
        row_index: int = -1,
    ) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.qty = qty
        self.row_index = row_index

    def log_data(self) -> dict[str, object]:
        """Structured fields for the operator alert (the raw value stringified)."""
        return {
            "position_symbol": self.symbol,
            "position_qty_raw": str(self.qty),
            "position_row_index": self.row_index,
        }


def _broker_position_qty(position: Mapping[str, object], row_index: int) -> float:
    """The quantity one broker position row states; a bad value raises (#263).

    ``0.0``/``"0"`` is a *readable* flat row and is fine. Anything else that is
    not a finite number - a value that cannot be parsed at all, ``nan``/``inf``,
    a boolean, or a missing/``None`` quantity - raises
    :class:`UnreadableBrokerPositionQtyError`: the row does not say what it is
    worth, and "no position" is a guess this path may not make. The exit path
    already reads a broker quantity the same way
    (:func:`live_runner.exit_signals.broker_position_qty`).

    Alpaca's adapter marks a row whose REST quantity string it could not parse
    with ``qty_unparseable`` (its displayed ``qty`` stays the coerced ``0.0``),
    which is the only thing that tells an unreadable row from a flat one -
    ``live_runner.trailing_stops`` and the adapter's own flatten path already
    read that marker, and so does this one.
    """
    symbol = str(position.get("symbol") or "").strip().upper()

    def _unreadable(detail: str, raw: object) -> UnreadableBrokerPositionQtyError:
        return UnreadableBrokerPositionQtyError(
            f"Broker position for {symbol!r} {detail}; refusing to read the account "
            "as flat",
            symbol=symbol,
            qty=raw,
            row_index=row_index,
        )

    if position.get("qty_unparseable"):
        raw = position.get("qty_raw")
        raise _unreadable(f"has an unparseable qty ({raw!r})", raw)
    raw = position.get("qty")
    if raw is None:
        raise _unreadable("states no qty", raw)
    if isinstance(raw, bool):
        raise _unreadable(f"has a boolean qty ({raw!r})", raw)
    try:
        # `raw` is broker JSON of unknown type: a TypeError/ValueError here is the
        # "unreadable" signal handled below, not a typing bug.
        qty = float(raw)  # type: ignore[arg-type]
    # ArithmeticError covers OverflowError and the decimal library's
    # DecimalException: `float()` raises those rather than a TypeError or
    # ValueError for an int too large for a float and a Decimal it cannot parse.
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise _unreadable(f"has an unparseable qty ({raw!r})", raw) from exc
    if not math.isfinite(qty):
        raise _unreadable(f"has a non-finite qty ({raw!r})", raw)
    return qty


def _count_open_broker_positions(positions: Iterable[Mapping[str, object]]) -> int:
    """Count the broker positions that carry an open quantity (#263).

    The count feeds the entry gates, so a row this cannot read must stop the
    count rather than drop out of it: :class:`UnreadableBrokerPositionQtyError`
    propagates and the caller refuses the entry (see
    :func:`prepare_order_with_risk`).
    """
    open_positions_count = 0
    for row_index, position in enumerate(positions):
        if abs(_broker_position_qty(position, row_index)) > 0:
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

    Raises :class:`UnreadableBrokerPositionQtyError` when a position row
    states no readable quantity (#263): an unknown row must not be read as
    flat, so the caller cannot size an entry against it.
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


async def prepare_order_with_risk(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    current_exposure: float = 0.0,
    account_balance: float | None = None,
    pip_value: float | None = None,
) -> tuple[LiveSignal, float]:
    """Size and validate an order, returning exactly what to submit.

    Split out from :func:`submit_order_with_risk` so a caller can separate the
    two phases that matter for a submission lease: everything here happens
    before the broker can see an order, so a failure is safe to retry; the
    adapter call the caller makes next is not. The submission path claims its
    lease *after* this returns, so a plain validation or sizing refusal cannot
    lock the deployment out for the lease TTL.

    Nothing in here places an order. It does read broker state (positions,
    balance) because sizing has to see broker truth, and, for entries, the
    live quote for the entry-deviation gate (#125), which may reject.

    ``pip_value`` is a trusted per-symbol pip value from the caller (#146:
    account-currency P&L of one qty unit per 1.0 price move). When it is
    ``None`` (production) the adapter's :meth:`get_pip_value` supplies it from
    broker data, and an entry whose pip value cannot be determined is refused.
    A ``pip_value`` in signal metadata is never read.

    Raises :class:`UnreadableBrokerPositionQtyError` (after an operator alert)
    when a broker position row states no readable quantity (#263): the entry
    is refused rather than sized as if the account were flat.
    """
    metadata = dict(signal.metadata or {})
    if is_exit_order(signal.action, metadata):
        # Every exit closes only this deployment's own ledger quantity (#121),
        # never the whole netted broker position: a parsed exit/close message
        # (its instruction is in the metadata, whatever the action placeholder
        # says) and a plain or momentum-reversal SELL on a deployment that cannot
        # short (a full exit). Exits bypass risk sizing and the entry SL/TP guard
        # because they reduce exposure. The close side comes from the position (a
        # short is bought back), and the resolved qty is binding (C1/B2) so the
        # adapter never re-sizes.
        return await prepare_exit_intent_order(
            adapter,
            deployment,
            signal,
            instruction=None if is_exit_intent(metadata) else FULL_EXIT,
        )
    # Kill switch (#300). The persisted halt is checked at the last chokepoint
    # every entry order passes through, and it is checked *after* the exit branch
    # above: closing a position must never be blocked by a halt, or the feature
    # would trap an operator in the exposure it exists to stop adding to. It runs
    # before any broker state is read, so a halted account costs no round-trip and
    # an unreadable halt state refuses the entry instead of trading on a guess.
    await refuse_entry_while_halted(
        deployment.account_id,
        deployment_id=str(deployment.id),
        symbol=signal.symbol,
    )
    # #150 fail closed at the last chokepoint before the broker: an entry
    # signal carrying a trailing config this adapter cannot execute (#167:
    # per-broker table + the adapter's stop-amendment capability) is refused —
    # never submitted untrailed. This gate also covers signals that reach
    # submission without a signal processor (the processor path is gated in
    # _apply_signal_processor).
    validate_trailing_sl_config(
        metadata.get(TRAILING_SL_CONFIG_METADATA_KEY),
        supported=adapter_live_trailing_modes(adapter),
        context=f"live deployment '{deployment.strategy_name}' ({deployment.id}) order submission",
    )
    validate_adapter_trailing_timeframe(
        metadata.get(TRAILING_SL_CONFIG_METADATA_KEY),
        _deployment_signal_timeframe(deployment),
        adapter=adapter,
        context=f"live deployment '{deployment.strategy_name}' ({deployment.id}) order submission",
    )
    # Promotion gate (#165). Every entry order of every deployment - Discord
    # and internal alike - is prepared here, so this is the one place the daemon
    # enforces it. The verdict comes from the adapter, the same source of truth
    # as the demo-only block in `_ensure_adapter_for_active_account`: an adapter
    # that is not provably demo/paper is real money. Exit-only orders returned
    # above: closing a position must never be blocked by a missing promotion.
    # Raises StrategyNotPromotedError before any broker state is read.
    enforce_promotion_gate(
        deployment, real_money=is_real_money(_adapter_is_demo(adapter))
    )
    # Feed broker truth into sizing so max_positions / max_total_exposure actually
    # bind. Without this, every signal is sized as if the account were flat.
    try:
        open_positions_count, broker_exposure = await _broker_exposure_estimate(
            adapter, deployment
        )
    except UnreadableBrokerPositionQtyError as exc:
        # #263 fail closed on the money path: a broker position whose quantity
        # cannot be read is not "no position". Taking the entry decision anyway
        # would size it as though the account were flat, so max_positions and
        # max_total_exposure would not bind and a new position could open on top
        # of a cap that is already full. Nothing is submitted and an operator
        # has to look (ERROR is a Sentry event).
        logger.error(
            "ALERT: broker position quantity is unreadable; refusing the entry "
            "signal because the open-position count cannot be trusted",
            extra={
                "data": {
                    "alert": "broker_position_qty_unreadable",
                    "account_id": deployment.account_id,
                    "deployment_id": str(deployment.id),
                    "signal_symbol": signal.symbol,
                    "reason": str(exc),
                    **exc.log_data(),
                }
            },
        )
        raise
    resolved_balance = await _resolve_account_balance(adapter, account_balance)
    # Loss circuit breaker (#301): per-account and per-deployment daily-loss /
    # drawdown limits, checked at the same chokepoint every entry passes
    # through and *after* the exit branch above, so closing a position is never
    # blocked - PAUSED semantics, exits stay allowed. The breaker observes
    # BROKER equity through the adapter's separate equity path (``get_equity``):
    # sizing stays on ``get_balance`` semantics (#267 decision - realized
    # balance), and a caller-supplied ``account_balance`` must never reach the
    # persisted equity series. An adapter without ``get_equity`` (a test
    # double) falls back to the balance tuple's equity slot. The limits are
    # resolved FIRST: with both scopes disabled the breaker measures nothing,
    # so no equity read (a cTrader unrealized-P&L request per entry) and no
    # deployment-P&L lookup happens at all. A failed equity read refuses the
    # entry: unreadable equity is never "no loss".
    breaker_equity: float | None = None
    deployment_equity: float | None = None
    breaker_config_invalid = False
    deployment_breaker_enabled = False
    try:
        # Both limits resolve eagerly: ``or`` would short-circuit and a
        # malformed deployment setting would slip through as a raw ValueError
        # instead of the gate's distinct config refusal.
        account_breaker_enabled = account_loss_breaker_limits(
            deployment.account_id
        ).enabled
        deployment_breaker_enabled = deployment_loss_breaker_limits(
            deployment
        ).enabled
        breaker_scopes_enabled = account_breaker_enabled or deployment_breaker_enabled
    except ValueError:
        # A malformed LOSS_BREAKER_* value is a configuration error, not a
        # breach: still call the gate so it raises the distinct "loss breaker
        # configuration invalid" refusal instead of the generic handler. No
        # equity or P&L read happens - the gate refuses before either matters.
        breaker_scopes_enabled = True
        breaker_config_invalid = True
    if breaker_scopes_enabled and not breaker_config_invalid:
        get_equity = getattr(adapter, "get_equity", None)
        if get_equity is not None:
            try:
                breaker_equity = float(await get_equity())
            except Exception as exc:  # except-kind: money-decision
                raise LossBreakerBreachError(
                    f"Refusing a live entry: account equity for "
                    f"'{deployment.account_id}' could not be read ({exc!r}); the "
                    "loss breaker cannot measure a loss without it.",
                    scope=account_loss_breaker_scope(deployment.account_id),
                    account_id=deployment.account_id,
                    deployment_id=str(deployment.id),
                    state_unreadable=True,
                ) from exc
        else:
            _, broker_equity = await adapter.get_balance()
            breaker_equity = float(broker_equity)
        if deployment_breaker_enabled:
            # The deployment scope measures the deployment's own P&L:
            # synthetic equity = account equity - every OTHER deployment's
            # P&L. ``None`` (unmeasurable) fails closed inside the gate.
            pnls = await account_deployment_trading_pnls(
                adapter, deployment.account_id
            )
            if pnls is not None:
                total_pnl = sum(pnls.values())
                deployment_equity = (
                    breaker_equity - total_pnl + pnls.get(str(deployment.id), 0.0)
                )
    if breaker_scopes_enabled:
        await asyncio.to_thread(
            refuse_entry_if_loss_breaker_tripped,
            deployment,
            equity=breaker_equity if breaker_equity is not None else 0.0,
            deployment_equity=deployment_equity,
            symbol=signal.symbol,
        )
    if pip_value is None:
        resolved_pip, pip_metadata = await _resolve_live_pip_value(adapter, deployment, signal)
    else:
        resolved_pip = _coerce_live_float(pip_value, field_name="pip_value", minimum=0.0)
        pip_metadata = _pip_value_record(resolved_pip, source="caller")
        # The override is a trusted channel that bypasses the broker lookup and
        # its freshness checks (#146); make every use visible in the logs.
        logger.warning(
            "Caller-supplied pip_value override in use; the broker pip value is NOT consulted",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "symbol": signal.symbol,
                    "pip_value": resolved_pip,
                }
            },
        )
    sized_signal = size_live_signal_for_submission(
        signal,
        deployment,
        account_balance=resolved_balance,
        current_exposure=max(float(current_exposure), broker_exposure),
        pip_value=resolved_pip,
        current_positions=open_positions_count,
    )
    try:
        sized_qty = coerce_positive_live_qty(
            (sized_signal.metadata or {}).get("qty"), field_name="live entry qty"
        )
    except ValueError as exc:
        raise ValueError(
            "Live entry quantity must be finite and positive; refusing to submit a zero-size order."
        ) from exc
    sized_signal = sized_signal.model_copy(
        update={
            "metadata": {
                **(sized_signal.metadata or {}),
                "qty": sized_qty,
                "explicit_qty": sized_qty,
            }
        }
    )
    # One resolver for sizing and submission (#125): the broker order carries
    # exactly the entry/SL/TP precedence the size was computed from.
    # Sizing may have resolved a fallback stop (ATR/percent); that is the
    # only level it may add, and the broker order carries it.
    # C1 (owner decision): a missing take profit FAILS — no invented 1.0-RR
    # bracket from theoretical_price. The guard below rejects it.
    sized_metadata = dict(sized_signal.metadata or {})
    entry_price, stop_loss, take_profit = resolve_live_levels(signal)
    sized_stop_raw = sized_metadata.get("resolved_stop_loss")
    sized_stop = (
        None
        if sized_stop_raw is None
        else _coerce_live_float(sized_stop_raw, field_name="resolved_stop_loss")
    )
    if stop_loss is None:
        stop_loss = sized_stop
    elif sized_stop is not None and not math.isclose(
        sized_stop, stop_loss, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(
            f"Sized stop {sized_stop!r} differs from the resolved signal stop "
            f"{stop_loss!r} (deployment_id={deployment.id}, symbol={signal.symbol}); "
            "refusing to submit a bracket the size was not computed for."
        )
    # Fail closed: sizing divides the risk budget by the stop distance, so an
    # order without SL/TP would be sized on an invented distance and open an
    # unbounded position. Both broker adapters enforce the same guard. The
    # ONLY exception is an explicit unbounded-take-profit policy flag set by
    # the originating processor (C1 owner decision: bounded TP or explicit
    # unbounded flag — RocketFuel exits on the opposite signal).
    unbounded_tp = is_unbounded_tp(metadata)
    missing: list[str] = []
    if stop_loss is None:
        missing.append("stop loss")
    if take_profit is None and not unbounded_tp:
        missing.append("take profit")
    if missing or stop_loss is None:
        raise ValueError(
            "Refusing to submit live order without "
            f"{' and '.join(missing)} (deployment_id={deployment.id}, "
            f"symbol={signal.symbol}); opening an unbounded position is "
            "not allowed."
        )
    # Entry-deviation gate (#125) against the live broker quote. Runs last,
    # after every local refusal, so a bad signal never costs a quote request.
    quote_metadata = await _check_entry_deviation(
        adapter,
        deployment,
        signal,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
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
    metadata["entry_price"] = float(entry_price)
    metadata["stop_loss"] = float(stop_loss)
    metadata["resolved_stop_loss"] = float(stop_loss)
    if take_profit is not None:
        metadata["take_profit"] = float(take_profit)
    else:
        metadata.pop("take_profit", None)
    for key in (*_ENTRY_QUOTE_METADATA_KEYS, *_PIP_VALUE_METADATA_KEYS):
        metadata.pop(key, None)
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
    final_metadata = {
        key: value
        for key, value in {**sized_metadata, **preserved_metadata}.items()
        if key not in {"volume", "ctrader_volume"}
    }
    # The gate's and the pip value's figures are written last: a payload value
    # under one of these keys can neither shadow nor survive them.
    final_metadata.update(quote_metadata)
    final_metadata.update(pip_metadata)
    # theoretical_price carries the resolved entry so an adapter that places
    # SL/TP as distances from it (cTrader) uses the distance that was sized.
    sized_signal = sized_signal.model_copy(
        update={"metadata": final_metadata, "theoretical_price": float(entry_price)}
    )
    # C1/B2: the risk-sized qty is binding — pass it explicitly so the
    # adapter sizes exactly zero times (never with default config).
    return sized_signal, float(sized_signal.metadata["qty"])


async def submit_order_with_risk(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    current_exposure: float = 0.0,
    account_balance: float | None = None,
    pip_value: float | None = None,
) -> str:
    """Prepare an order and submit it, for callers with no lease to protect."""
    prepared_signal, prepared_qty = await prepare_order_with_risk(
        adapter,
        deployment,
        signal,
        current_exposure=current_exposure,
        account_balance=account_balance,
        pip_value=pip_value,
    )
    # The same last-moment halt gate the leased submission path runs: this
    # caller has no claim to protect, but a halt written while the order was
    # being sized must still stop it. Entries only - an exit is never blocked.
    if not is_exit_order(signal.action, signal.metadata or {}):
        await refuse_entry_while_halted(
            deployment.account_id,
            deployment_id=str(deployment.id),
            symbol=signal.symbol,
        )
    return await adapter.submit_order(prepared_signal, qty=prepared_qty)


def _mint_signal_idempotency_key(
    signal: TradeSignal, deployment: LiveDeploymentConfig, metadata: dict[str, object]
) -> None:
    """Mint a producer-stable idempotency key for non-Discord signals (C1).

    A redelivery returns the canonical row instead of a second broker
    order. Content-stable (same logical signal → same key); distinct
    signals differ by price/levels/time. Discord redeliveries keep using
    their discord_message_id. CodeRabbit round: only mint from real
    producer identity — an empty timestamp slot would make distinct
    timestamp-less signals share one key and wrongly dedupe, so leave it
    unset instead (the execution layer derives a fill-content key
    downstream).
    """
    if metadata.get("discord_message_id") or metadata.get("idempotency_key"):
        return
    producer_stamp = metadata.get("signal_timestamp") or metadata.get("timestamp")
    if not producer_stamp:
        return
    # The same producer event can be redelivered with its timestamp spelled differently (``Z``
    # vs ``+00:00``, aware vs naive, another UTC offset). Key on the instant, not the spelling,
    # without rounding it: distinct instants - even a microsecond apart - keep distinct keys.
    # Text that is not a time keeps its verbatim form.
    raw_stamp = str(producer_stamp)
    canonical_stamp = canonical_utc_timestamp(producer_stamp) or raw_stamp

    def _key(stamp: str) -> str:
        key_material = "|".join(
            [
                str(deployment.id),
                str(signal.symbol).upper(),
                str(signal.action).lower(),
                repr(float(signal.entry_price)),
                repr(signal.stop_loss),
                repr(signal.take_profit),
                str(signal.timeframe),
                stamp,
            ]
        )
        return hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]

    metadata["idempotency_key"] = _key(canonical_stamp)
    if canonical_stamp != raw_stamp:
        # Signals persisted before #127 were keyed on the verbatim spelling. A replay of one of
        # them right after the upgrade (a restart is exactly when producers replay) must still
        # find that row instead of placing a second broker order, so the pre-#127 key travels
        # along as a lookup-only fallback. It is never stored as the row's own key.
        # Bound: this only dedupes a replay that carries the SAME spelling as the stored
        # pre-#127 row. A replay re-spelled differently (``Z`` stored, ``+00:00`` replayed)
        # misses it; closing that needs a data migration of stored keys and is not done here.
        metadata["legacy_idempotency_key"] = _key(raw_stamp)


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
    _mint_signal_idempotency_key(signal, deployment, metadata)
    if is_exit_intent(metadata):
        # A parsed exit is reduce-only whatever the deployment allows: on a
        # long+short deployment a plain sell is a real short, an exit is not
        # (#121). Validated here so a malformed instruction fails before sizing.
        exit_instruction_from_metadata(metadata)
        metadata["exit_only"] = True
        metadata["risk_sizing_mode"] = EXIT_ONLY_RISK_SIZING_MODE
        for key in ("stop_loss", "take_profit"):
            metadata.pop(key, None)
    elif signal.action in {"sell", "short"} and not _deployment_allows_short(deployment):
        metadata["exit_only"] = True
        metadata["risk_sizing_mode"] = EXIT_ONLY_RISK_SIZING_MODE
    # Whitelist at the conversion site itself (B-2): never default an unmapped
    # action to SELL. The TradeSignal Literal gates construction today, but a
    # future loosening (e.g. adding "close") must not silently become a live
    # short — reject it here with a structured reason.
    live_action: Literal["buy", "sell"]
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


#: Statuses a deployment may open a new position from. Only ACTIVE: a pause or a stop
#: blocks entries in every wording of the rule.
#:
#: Derived from the shared tuple in ``shared/database.py`` so this gate, the atomic
#: submission claim's SQL and the conflict halt's exempt set always widen together.
ENTRY_DISPATCH_STATUSES = frozenset(LIVE_DISPATCH_ENTRY_STATUSES)

#: Statuses a deployment may close a position from. PAUSED is included on purpose (#126
#: decision): "stop opening new positions" (the PAUSED row of the "Operator safety
#: controls" comment in `base_adapter.py`) must not also stop a
#: deployment closing what it already holds, or a pause forced by a broker-order-id
#: conflict would leave the known position open until its stop - exactly the exposure
#: the conflict could not explain. STOPPED keeps its own cancel-and-flatten enforcement
#: and is absent: a stopped deployment is closed by the sweeps, not by signal.
EXIT_DISPATCH_STATUSES = frozenset(LIVE_DISPATCH_EXIT_STATUSES)


def _dispatch_statuses(*, is_exit: bool) -> frozenset[str]:
    """The statuses one signal may be dispatched from."""
    return EXIT_DISPATCH_STATUSES if is_exit else ENTRY_DISPATCH_STATUSES


def _signal_is_exit(
    action: str,
    metadata: Mapping[str, object] | None,
    deployment: LiveDeploymentConfig,
) -> bool:
    """Whether this deployment will dispatch the signal as a close (#126).

    :func:`live_runner.exit_signals.is_exit_order` alone is not the whole answer: a
    plain SELL on a deployment that cannot short is a full exit too, and that is
    decided per deployment, in :func:`_trade_signal_to_live_signal` (which stamps
    ``exit_only``) - *after* the dispatch gates have already run. Answering it the
    same way here is what keeps "dispatched as an exit" and "gated as an exit" from
    drifting apart, and therefore what lets a pause reject entries while still
    closing a position a plain sell was about to close.
    """
    if is_exit_order(action, metadata):
        return True
    closes_only = str(action or "").strip().lower() in {"sell", "short"}
    return closes_only and not _deployment_allows_short(deployment)


def _dispatchable_discord_deployments_for_account(
    account_id: str,
    deployments: list[LiveDeploymentConfig],
) -> list[LiveDeploymentConfig]:
    """Deployments a signal on this account can still reach for a close.

    PAUSED deployments stay in the fan-out so an exit message can reach their
    broker: they are gated per signal instead (`_deployment_can_dispatch`), which
    is what lets a paused deployment close a position while opening none. STOPPED
    deployments are excluded - their exposure is closed by the stop sweeps.
    """
    return [
        deployment
        for deployment in deployments
        if deployment.account_id == account_id
        and _normalize_deployment_status(deployment.status) in EXIT_DISPATCH_STATUSES
        # The same set the untradeable-row warning reads (#242): a source is
        # routed because this process can run it, and for no other reason.
        and signal_source_can_trade(deployment.signal_source)
    ]


def _primary_discord_deployment(deployments: list[LiveDeploymentConfig]) -> LiveDeploymentConfig:
    """The deployment whose configuration drives the account's Discord listener.

    One listener serves every deployment of the account, so one deployment's service
    name, token, auth scheme, timeframe and ``service_id`` are the listener's. They
    therefore come from an ACTIVE deployment whenever the account has one: a freshly
    created deployment is PAUSED, and this list is ordered newest first, so taking the
    head unconditionally would let a brand-new paused bot's token and timeframe decide
    how an older ACTIVE bot's channel is polled - a listener that can miss that
    channel's signals, or 401 against a bot that was never in it. An account whose
    deployments are all paused keeps one too (#126): it still needs a listener to hear
    exits, and its own deployment is the only credential scope it has.
    """
    for wanted in ("ACTIVE", "PAUSED"):
        for deployment in deployments:
            if _normalize_deployment_status(deployment.status) == wanted:
                return deployment
    return next(iter(deployments))


def _deployment_can_dispatch(
    deployment: LiveDeploymentConfig, *, is_exit: bool = False
) -> bool:
    """Re-check deployment status against the DB right before dispatching.

    The listener holds a snapshot taken when the sync loop last ran. If an
    operator pauses or stops one deployment on an account whose other Discord
    deployments stay active, the stale snapshot would keep the paused
    deployment trading until the next poll. The DB read here is the
    authority at the moment an order is actually dispatched.

    ``is_exit`` widens the accepted statuses to :data:`EXIT_DISPATCH_STATUSES`: an exit
    closes a position the deployment already holds, so a PAUSED deployment still
    answers one, while a STOPPED one does not (deny-by-default keeps a status the
    daemon does not know out of both sets).
    """
    allowed = _dispatch_statuses(is_exit=is_exit)
    try:
        current = get_live_deployment(str(deployment.id))
    except Exception as exc:  # except-kind: money-decision
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
    if _normalize_deployment_status(current.status) not in allowed:
        logger.info(
            "Deployment is not in a dispatchable status; refusing to trade from stale "
            "snapshot",
            extra={
                "data": {
                    "deployment_id": str(deployment.id),
                    "account_id": deployment.account_id,
                    "snapshot_status": deployment.status,
                    "current_status": current.status,
                    "signal_kind": "exit" if is_exit else "entry",
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
) -> tuple[str, str]:
    """Return the deployment's Discord token and the key it came from.

    The source key matters as much as the token: a token found under a per-bot
    key is configured with that bot's auth scheme, while one falling back to the
    global ``DISCORD_BOT_TOKEN`` is configured with the global scheme. Sending a
    per-bot scheme with a global token is a 401.
    """
    service_name = _deployment_service_name(deployment)
    token_env_key = ""
    if service_name:
        canonical_name = discord_bot_env_canonical_name(service_name)
        token_env_key = discord_bot_env_key(canonical_name, "TOKEN")
    if token_env_key:
        per_bot_token = resolve_env_value(accounts_env_values, token_env_key)
        if per_bot_token:
            return per_bot_token, token_env_key
    global_token = resolve_env_value(accounts_env_values, "DISCORD_BOT_TOKEN")
    if global_token:
        return global_token, "DISCORD_BOT_TOKEN"
    return "", ""


def _resolve_account_discord_credentials(
    accounts_env_values: Mapping[str, str],
    deployment: LiveDeploymentConfig,
) -> tuple[str, str]:
    """Resolve a deployment's Discord token and auth scheme together.

    Both must be keyed by the same service name. Resolving them separately let
    the token fall back to `signal_config["service_name"]` while the scheme read
    `signal_source_id` alone, so an id-less deployment found its token but sent a
    `Bot` header for an account token and got a 401. Returning both from one
    place makes that split unrepresentable.
    """
    token, token_source_key = _resolve_account_discord_token(
        accounts_env_values, deployment
    )
    # An operator who pinned the scheme on the deployment has said so out loud;
    # that beats any inferred scope. Otherwise the credential decides.
    override = _deployment_discord_auth_scheme(deployment)
    if override:
        return token, override
    if not token:
        return "", _resolve_discord_auth_scheme(
            _deployment_service_name(deployment), accounts_env_values
        )
    # The scheme is a property of the credential, not of the deployment, so it
    # must be read from the scope the token actually came from. Only the global
    # fallback needs special handling: a per-bot token was found under a key
    # derived from this deployment's service name, which is exactly the scope
    # `_resolve_discord_auth_scheme` already reads.
    if token_source_key == "DISCORD_BOT_TOKEN":
        return token, _resolve_discord_auth_scheme("", accounts_env_values)
    return token, _resolve_discord_auth_scheme(
        _deployment_service_name(deployment), accounts_env_values
    )


def _deployment_covers_discord_symbol(
    deployment: LiveDeploymentConfig,
    symbol: str,
) -> bool:
    deployment_symbols = {item.upper() for item in deployment.symbols}
    return not deployment_symbols or symbol.upper() in deployment_symbols


def _split_env_id_list(raw: object) -> set[str]:
    """Split a comma/whitespace-separated env ID list into an ID set."""
    if not isinstance(raw, str) or not raw.strip():
        return set()
    return {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}


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
    env_key = _service_config_string(config, "trusted_author_ids_env_key")
    if not env_key:
        # Per-bot first, matching DISCORD_BOT_<NAME>_TOKEN / _CHANNEL_ID. Without
        # it every bot falls back to the single shared DISCORD_TRUSTED_AUTHOR_IDS,
        # so each one trusts the *other* bots' authors: RocketFuel's bot posting
        # in Nitro's channel would be accepted and could trade Nitro. The shared
        # var stays as the fallback for single-bot setups.
        # Through the canonicalisation helper, as `discord_bot_env_canonical_name`
        # requires of every per-bot key. Building it by hand meant a service named
        # `NITRO-BOT` looked for a variable the operator could never have set, found
        # nothing, and silently fell back to the shared list - i.e. it fell open to
        # exactly the cross-bot trust the per-bot var exists to prevent, with no
        # log line. The deployment path already did this, so one variable resolved
        # two different ways in the same file.
        env_key = discord_bot_env_key(service.name, "TRUSTED_AUTHOR_IDS")
    # Presence is tested against the mapping directly, not via
    # `resolve_env_value`, which returns "" for both an absent key and a
    # present-but-blank one - so a truthiness test cannot tell "this bot trusts
    # nobody" from "not configured", and the empty case used to fall through to
    # the shared list. That widened Nitro to all three bots' authors, and
    # inconsistently: `...,` was truthy and failed closed while `` was falsy and
    # opened up, so the safe configuration was unreachable.
    #
    # Deliberately does not consult os.environ. This is an allowlist, and an
    # inherited process variable must not silently become one.
    if accounts_env_values is not None and env_key in accounts_env_values:
        per_bot = str(accounts_env_values.get(env_key) or "").strip()
        return _trusted_author_ids_from_config(_split_env_id_list(per_bot))
    raw_env = resolve_env_value(accounts_env_values, "DISCORD_TRUSTED_AUTHOR_IDS")
    if not raw_env:
        return set()
    return _trusted_author_ids_from_config(_split_env_id_list(raw_env))


async def _renew_submission_claim_until_cancelled(token: str) -> None:
    """Keep a submission lease alive while the broker call is in flight.

    A fixed 30 s lease used to expire under a slow submit, and a second
    dispatcher could then take it and submit a duplicate. Renewal runs at a
    third of the TTL, so a single missed beat is harmless. Losing the lease is
    logged loudly but does not cancel the in-flight call: the order may already
    be at the broker, and killing the coroutine would not recall it.
    """
    interval = max(LIVE_SUBMISSION_CLAIM_TTL_SECONDS / 3.0, 0.05)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await asyncio.to_thread(
                renew_live_deployment_submission, token
            )
        except Exception as exc:  # except-kind: loop-survival
            # Keep trying: a transient DB error must not be what lets the lease
            # expire under an in-flight order.
            logger.error(
                "Could not renew the live submission lease; retrying next interval",
                extra={"data": {"token": token, "error": repr(exc)}},
            )
            continue
        if not renewed:
            logger.error(
                "Live submission lease was lost while the order was in flight; "
                "the outcome must be reconciled before the next submission",
                extra={"data": {"token": token}},
            )
            return


async def _prepare_discord_signal_for_submission(
    account_id: str,
    deployment: LiveDeploymentConfig,
    trade_signal: TradeSignal,
    *,
    signal_timeframe: str,
    wait_enabled: bool,
    adapter: BaseLiveAdapter | None = None,
    decision: SignalDecisionRecorder | None = None,
) -> TradeSignal | None:
    """Apply the pre-wait gates and, when enabled, the candle-close wait.

    Returns the normalised signal to submit, or ``None`` when a gate refused it.
    Every refusal is logged with the account/deployment/symbol it applies to and
    returns ``None`` rather than raising: these signals are dispatched in a
    fan-out loop, and letting one deployment's bad config propagate would tear
    down the account's whole listener and lose the rest of the poll batch.

    Whether this dispatch is a close is decided once, from the parsed message, and
    carried into every status gate below. A pause has to block an entry and still
    let the deployment close what it holds (#126 decision), so no gate below can ask
    "is this deployment ACTIVE" alone.
    """
    try:
        is_exit = _signal_is_exit(trade_signal.action, trade_signal.metadata, deployment)
    except ValueError:
        # A strict boolean this deployment cannot parse (e.g. a typo'd allow_short)
        # means nothing may be dispatched to it. Same handling as the conversion-time
        # check: skip the deployment, and never default an unreadable config into an
        # order.
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
        return None
    # B2 (owner decision): stale signals trade immediately on outdated
    # information — discard anything older than one deployment-timeframe
    # candle instead.
    if not signal_is_fresh(trade_signal, signal_timeframe):
        # A dropped exit leaves a position open: a warning, not routine info.
        log_stale = logger.warning if is_exit_intent(trade_signal.metadata) else logger.info
        log_stale(
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
        if decision is not None:
            decision.refused("stale_signal", detail=signal_timeframe)
        return None
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
        # Fail closed on an already-stale signal BEFORE the candle-close wait:
        # the wait can span minutes, so a signal older than the timeframe-aware
        # budget would otherwise be executed at the candle-close price on
        # information that has already expired.
        _validate_signal_age_before_wait(
            normalized_signal,
            deployment,
            timeframe=signal_timeframe,
            # This function is the Discord dispatch path; the source is known
            # here, so the gate is told rather than made to sniff metadata.
            require_producer_timestamp=True,
        )
        if not is_exit_intent(normalized_signal.metadata):
            # Entry processors (ATR/SL/TP, BUY-only gates) shape entries. An exit
            # closes what exists; Nitro's BUY-only processor would refuse every
            # close on a Nitro deployment (#121).
            normalized_signal = _apply_signal_processor(
                normalized_signal, deployment, adapter=adapter, decision=decision
            )
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
        if decision is not None:
            decision.refused("signal_gate_rejected", detail=str(exc))
        return None
    if wait_enabled:
        try:
            await wait_for_candle_close(
                normalized_signal,
                normalized_signal.timeframe or signal_timeframe,
            )
        except ValueError as exc:
            # An unrecognised timeframe is a configuration error, not a reason to
            # trade immediately: `wait_for_candle_close` refuses rather than
            # silently skipping the wait. Skip this deployment's signal and keep
            # the listener alive - letting it propagate would tear down the whole
            # account's source and lose the rest of the poll batch.
            logger.warning(
                "Skipping discord signal: candle-close wait refused the timeframe",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "symbol": trade_signal.symbol,
                        "signal_timeframe": signal_timeframe,
                        "error": str(exc),
                    }
                },
            )
            if decision is not None:
                decision.refused("candle_close_wait_refused", detail=str(exc))
            return None

    # Re-check after the candle-close wait: the wait can span minutes, and the
    # pre-dispatch snapshot was taken before it. A deployment paused during the
    # wait window must not trade at the candle-close price — and neither must
    # a signal that AGED OUT during the wait (B2 price/age revalidation).
    if not _deployment_can_dispatch(deployment, is_exit=is_exit):
        logger.info(
            "Skipping discord signal: deployment is not in a dispatchable status "
            "after the candle-close wait",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                    "snapshot_status": deployment.status,
                }
            },
        )
        if decision is not None:
            decision.refused("deployment_inactive_after_wait")
        return None
    if not signal_is_fresh(normalized_signal, signal_timeframe):
        log_aged = logger.warning if is_exit_intent(normalized_signal.metadata) else logger.info
        log_aged(
            "Skipping discord signal: aged out during candle-close wait",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": trade_signal.symbol,
                }
            },
        )
        if decision is not None:
            decision.refused("signal_aged_out_during_wait")
        return None
    return normalized_signal


async def _submit_normalized_discord_signal(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    trade_signal: TradeSignal,
    *,
    signal_timeframe: str,
    wait_enabled: bool,
) -> None:
    # Issue #308: one archive decision record per (signal, deployment). The
    # recorder is created here — the one place that knows all three — and
    # threaded explicitly; every terminal path below records exactly once.
    decision = SignalDecisionRecorder(
        trade_signal=trade_signal,
        account_id=account_id,
        deployment=deployment,
    )
    try:
        prepared = await _prepare_discord_signal_for_submission(
            account_id,
            deployment,
            trade_signal,
            signal_timeframe=signal_timeframe,
            wait_enabled=wait_enabled,
            adapter=adapter,
            decision=decision,
        )
        if prepared is None:
            return
        normalized_signal = prepared

        try:
            live_signal = _trade_signal_to_live_signal(normalized_signal, deployment)
        except ValueError as exc:
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
            decision.refused("invalid_signal_config", detail=str(exc))
            return

        is_exit = _signal_is_exit(live_signal.action, live_signal.metadata, deployment)
        if is_exit:
            # Exits of every deployment on this account and symbol go one after the
            # other, from sizing to the persisted order (see ``_exit_lock``).
            async with _exit_lock(account_id, live_signal.symbol):
                await _prepare_claim_and_submit_signal(
                    account_id,
                    adapter,
                    deployment,
                    normalized_signal,
                    live_signal,
                    is_exit=True,
                    decision=decision,
                )
            return
        await _prepare_claim_and_submit_signal(
            account_id,
            adapter,
            deployment,
            normalized_signal,
            live_signal,
            is_exit=False,
            decision=decision,
        )
    except Exception as exc:  # except-kind: loop-survival
        # Issue #308: an unexpected failure must still leave a decision
        # record — "one per (signal, deployment)" is only true if the escape
        # path records too. The exception is re-raised unchanged.
        decision.unknown(
            "unhandled_error", detail=f"{type(exc).__name__}: {exc}"
        )
        raise


async def _prepare_claim_and_submit_signal(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    normalized_signal: TradeSignal,
    live_signal: LiveSignal,
    *,
    is_exit: bool,
    decision: SignalDecisionRecorder | None = None,
) -> None:
    # Prepare before claiming. Sizing and the fail-closed SL/TP validation run
    # here, and every failure in them happens before an order can reach the
    # broker. Claiming first (as this used to) held the lease for the whole TTL
    # on a plain validation refusal, locking the deployment out for no reason.
    try:
        prepared_signal, prepared_qty = await prepare_order_with_risk(
            adapter, deployment, live_signal
        )
    except NothingToExitError as exc:
        # #121 decision 1: no position -> ignore and log. Not an error. A broker
        # position that is all somebody else's is worth a warning: either this
        # deployment never held it, or its ledger missed a fill.
        log = logger.warning if exc.broker_qty else logger.info
        log(
            "Exit signal ignored: nothing of this deployment's to close",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": live_signal.symbol,
                    "broker_qty": exc.broker_qty,
                    "reason": str(exc),
                }
            },
        )
        if decision is not None:
            decision.ignored("nothing_to_exit", detail=str(exc))
        return
    except ExitBelowStepError as exc:
        _log_exit_below_step(account_id, deployment, live_signal.symbol, exc)
        if decision is not None:
            decision.ignored("exit_below_step", detail=str(exc))
        return
    except ExitInFlightError as exc:
        # An error, not a skipped signal: nothing was sent and this deployment's
        # position stays open (a later exit signal is sized once the earlier
        # exit is over, but this one is not queued). An operator has to look.
        logger.error(
            "Exit signal refused: a previous exit of this deployment is still "
            "working, the other deployments' working exits leave nothing to "
            "close, or no earlier exit can be shown to be finished; nothing was "
            "sent to the broker and the position stays open",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": live_signal.symbol,
                    "reason": str(exc),
                }
            },
        )
        if decision is not None:
            decision.refused("exit_in_flight", detail=str(exc))
        return
    except LiveHaltError as exc:
        # The kill switch (#300) refused this entry. Nothing was sent, which is
        # the halt working; reported as its own reason rather than as a generic
        # refusal, so an operator sees the halt they just set instead of hunting
        # for a validation error.
        logger.warning(
            "Entry refused by the kill switch; nothing was sent to the broker",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": live_signal.symbol,
                    "halt_mode": exc.mode.value if exc.mode is not None else None,
                    "halt_state_unreadable": exc.state_unreadable,
                    "reason": str(exc),
                }
            },
        )
        return
    except LossBreakerBreachError as exc:
        # The loss circuit breaker (#301) refused this entry. Nothing was sent,
        # which is the breaker working; reported distinctly so an operator sees
        # the tripped scope instead of hunting for a validation error. The
        # breaker already logged the latch alert; this is the per-signal record.
        logger.warning(
            "Entry refused by the loss circuit breaker; nothing was sent to the broker",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": live_signal.symbol,
                    "breaker_scope": exc.scope,
                    "breaker_kind": exc.kind,
                    "breaker_state_unreadable": exc.state_unreadable,
                    "reason": str(exc),
                }
            },
        )
        return
    except Exception as exc:  # except-kind: money-decision
        if is_exit:
            # A refused exit leaves this deployment's position open: an operator
            # has to look at it, so it is an error, not a skipped signal (#121).
            logger.error(
                "Exit signal refused before submission; nothing was sent to the "
                "broker and the position stays open",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "symbol": live_signal.symbol,
                        "action": live_signal.action,
                        "error": str(exc),
                    }
                },
            )
            if decision is not None:
                decision.refused("exit_pre_submission_error", detail=str(exc))
            return
        logger.warning(
            "Discord signal refused before submission; nothing was sent to the broker",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": live_signal.symbol,
                    "error": str(exc),
                }
            },
        )
        if decision is not None:
            decision.refused("pre_submission_error", detail=str(exc))
        return

    # Kill switch (#300), read again at the last moment before the broker sees
    # anything. The gate in `prepare_order_with_risk` ran before the broker
    # reads, so an operator who halted the account while this entry was being
    # sized must not have the order land anyway - that window is exactly when the
    # halt is being used. Nothing has been claimed at this point, so a refusal
    # costs no submission lease. Exits skip it for the same reason they skip the
    # gate in `prepare_order_with_risk`: a halt must never trap an open position.
    if not is_exit:
        try:
            await refuse_entry_while_halted(
                account_id,
                deployment_id=str(deployment.id),
                symbol=live_signal.symbol,
            )
        except LiveHaltError as exc:
            logger.warning(
                "Entry refused by the kill switch at the submission gate; nothing was "
                "sent to the broker and the submission lease was never claimed",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "symbol": live_signal.symbol,
                        "halt_mode": exc.mode.value if exc.mode is not None else None,
                        "halt_state_unreadable": exc.state_unreadable,
                        "reason": str(exc),
                    }
                },
            )
            return

    # Atomic submission claim. The status re-checks above ran against a
    # snapshot; between them and the broker call an operator can pause the
    # deployment. The claim turns "still dispatchable" into a single atomic database
    # fact taken immediately before submission, so a PAUSE either wins the race
    # (the claim fails and nothing is sent) or is already in effect while this
    # dispatcher finishes the order it had already claimed. For an exit the claim
    # also passes a PAUSED row, because a pause does not forbid closing what is
    # held (#126 decision) - it still refuses any status the daemon does not know.
    claim_token = uuid.uuid4().hex
    claimed = await asyncio.to_thread(
        claim_live_deployment_submission,
        str(deployment.id),
        claim_token,
        allow_paused=is_exit,
    )
    if not claimed:
        logger.info(
            "Skipping discord signal: deployment no longer ACTIVE at submission claim",
            extra={
                "data": {
                    "account_id": account_id,
                    "deployment_id": str(deployment.id),
                    "symbol": normalized_signal.symbol,
                    "snapshot_status": deployment.status,
                }
            },
        )
        if decision is not None:
            decision.refused("submission_claim_failed")
        return

    # The claim itself awaited the database, so a halt written while it ran is
    # still unseen by the gate above. Read the halt one last time now that the
    # lease is held: a refusal releases the lease (the order never reached the
    # broker, so nothing is in flight) and this is the last checkpoint before
    # `adapter.submit_order`. Exits skip it for the same reason as above.
    if not is_exit:
        try:
            await refuse_entry_while_halted(
                account_id,
                deployment_id=str(deployment.id),
                symbol=live_signal.symbol,
            )
        except LiveHaltError as exc:
            try:
                await asyncio.to_thread(
                    release_live_deployment_submission, claim_token
                )
            except Exception:  # except-kind: loop-survival
                # The lease expires on its own; never let the cleanup turn a
                # handled refusal into an exception that aborts the fan-out.
                logger.exception("Could not release the submission lease")
            logger.warning(
                "Entry refused by the kill switch after the submission claim; "
                "released the lease and nothing was sent to the broker",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "symbol": live_signal.symbol,
                        "halt_mode": exc.mode.value if exc.mode is not None else None,
                        "halt_state_unreadable": exc.state_unreadable,
                        "reason": str(exc),
                    }
                },
            )
            return

    renew_task = asyncio.create_task(
        _renew_submission_claim_until_cancelled(claim_token),
        name=f"submission-claim-renewal-{deployment.id}",
    )
    try:
        try:
            broker_order_id = await adapter.submit_order(
                prepared_signal, qty=prepared_qty
            )
        except Exception as exc:  # except-kind: money-decision
            if is_order_not_submitted(exc):
                # The adapter marked the failure as provably pre-request: the
                # order never left the process, so the lease can be released
                # and the next signal is not locked out for the TTL.
                try:
                    await asyncio.to_thread(
                        release_live_deployment_submission, claim_token
                    )
                except Exception:  # except-kind: loop-survival
                    # The lease expires on its own. Never let the cleanup turn a
                    # handled refusal into an exception that aborts the fan-out
                    # to the remaining deployments.
                    logger.exception("Could not release the submission lease")
                if isinstance(exc, ExitBelowStepError):
                    # The adapter's own floor found less than one step to close:
                    # an ignored exit, like the daemon's own check, not a failure.
                    _log_exit_below_step(
                        account_id, deployment, prepared_signal.symbol, exc
                    )
                    if decision is not None:
                        decision.ignored("exit_below_step", detail=str(exc))
                    return
                # A refused exit leaves the position open (#121): error, so an
                # operator sees it. A refused entry opened nothing: warning.
                log = logger.error if is_exit else logger.warning
                log(
                    (
                        "Exit signal was refused before the broker request; "
                        "released the submission lease and the position stays open"
                        if is_exit
                        else "Discord signal was refused before the broker request; "
                        "released the submission lease"
                    ),
                    extra={
                        "data": {
                            "account_id": account_id,
                            "deployment_id": str(deployment.id),
                            "symbol": prepared_signal.symbol,
                            "action": prepared_signal.action,
                            "exit": is_exit,
                            "error": str(exc),
                        }
                    },
                )
                if decision is not None:
                    decision.refused(
                        "broker_refused_pre_request"
                        if not is_exit
                        else "exit_refused_pre_request",
                        detail=str(exc),
                    )
                return
            # **Do not release the lease.** The broker may have accepted the
            # order and only the response was lost; releasing would let the next
            # signal submit a duplicate before anyone reconciled. The lease is
            # left to expire, which the drain reports as INDETERMINATE rather
            # than DRAINED.
            logger.error(
                "Discord signal submission failed with an unknown outcome; "
                "holding the submission lease until it expires and requiring "
                "reconciliation before the next submit",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "symbol": prepared_signal.symbol,
                        "action": prepared_signal.action,
                        "exit": is_exit,
                        "error": str(exc),
                    }
                },
            )
            if decision is not None:
                decision.unknown(
                    "broker_submit_outcome_unknown", detail=str(exc)
                )
            return
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task

    try:
        await asyncio.to_thread(release_live_deployment_submission, claim_token)
    except Exception:  # except-kind: loop-survival
        # The order is already at the broker, so the lease expiring on its own
        # is harmless. A cleanup failure must not abort the fan-out or skip the
        # "submitted" log line below.
        logger.exception(
            "Could not release the submission lease; it expires on its own",
            extra={"data": {"deployment_id": str(deployment.id)}},
        )
    logger.info(
        "Discord signal submitted",
        extra={
            "data": {
                "account_id": account_id,
                "deployment_id": str(deployment.id),
                "signal_id": str(live_signal.id),
                "broker_order_id": broker_order_id,
                "symbol": prepared_signal.symbol,
                "action": prepared_signal.action,
                "exit": is_exit,
            }
        },
    )
    if decision is not None:
        decision.traded(
            detail=str(broker_order_id) if broker_order_id is not None else None,
            broker_order_id=(
                str(broker_order_id) if broker_order_id is not None else None
            ),
        )


def _deployment_accepts_signal_author(
    deployment: LiveDeploymentConfig,
    signal_author_id: str,
    *,
    accounts_env_values: Mapping[str, str] | None = None,
) -> bool:
    """Per-deployment author gate applied at dispatch time.

    The listener's merged allowlist is a coarse source-level filter (any author
    trusted by any deployment on the channel passes ingestion). Before an order
    is submitted, the signal must also satisfy the target deployment's own
    allowlist so a deployment cannot trade on an author another deployment
    trusted.

    `accounts_env_values` must be threaded in. The call used to omit it, so the
    resolver fell back to `os.environ` - and `.accounts.env` is never installed
    there, only read into a mapping. The allowlist therefore resolved empty, the
    empty case was treated as "no restriction", and this gate was **inert**: it
    accepted every author. An empty allowlist now rejects every author. The one
    defence against a deployment trading on another bot's author was not running,
    silently, in the documented production config.
    """
    allowlist = _deployment_trusted_author_ids(
        deployment, accounts_env_values=accounts_env_values
    )
    if not allowlist:
        # Fail closed. An empty allowlist means "this deployment trusts nobody": either
        # the operator set a blank `DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS` (documented as
        # trust-nobody) or nothing resolved at all. It must NOT mean "accept anyone": the
        # listener's channel-merged set contains every deployment's authors, so letting an
        # allowlist-less deployment inherit it would let bot Y's author trade on bot X's
        # deployment - exactly what the per-bot allowlist exists to prevent. A deployment
        # that should trade must carry its own allowlist.
        logger.warning(
            "Skipping discord signal: deployment has no trusted author allowlist "
            "(unset or blank means it trusts nobody)",
            extra={"data": {"deployment_id": str(deployment.id or "")}},
        )
        return False
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
    # No fallback to os.environ, deliberately.
    #
    # `.accounts.env` is read into a mapping and never installed into the process
    # environment, so an `os.environ` fallback resolves nothing here: the
    # allowlist comes back empty, and `_deployment_accepts_signal_author` treats
    # empty as "no restriction" and returns True. That is the exact shape of the
    # inert-gate bug fixed in 8d8a3c3, one layer up - a gate that resolves
    # nothing and reports success. The service path refuses os.environ for the
    # same reason; this one now does too.
    #
    # All three production call sites pass the mapping, so this branch is
    # unreachable today. It is here to be loud if a future caller forgets, rather
    # than quietly permissive.
    if accounts_env_values is None:
        logger.error(
            "Discord author allowlist requested without the accounts env; "
            "refusing to fall back to the process environment, which never "
            "contains it",
            extra={"data": {"deployment_id": str(deployment.id or "")}},
        )
        return set()
    env_source: Mapping[str, str] = accounts_env_values
    service_name = _deployment_service_name(deployment)
    if service_name:
        # Through the canonicalisation helper, as its own docstring requires of
        # every per-bot key. This one was built by hand, so a name that kept its
        # punctuation would look for a variable no operator could set and fall
        # through to the shared list.
        per_service_key = discord_bot_env_key(service_name, "TRUSTED_AUTHOR_IDS")
        # Presence, not truthiness. A blank per-bot var means "this bot trusts
        # nobody", and it must NOT fall through to the shared list: that widened
        # Nitro to all three bots' authors, so the fail-closed configuration was
        # unreachable from here - while the *service* path, fixed in 626e485,
        # correctly returned empty for the same input. Same allowlist, two
        # answers, on the path all three live deployments use.
        if per_service_key in env_source:
            return _trusted_author_ids_from_config(
                _split_env_id_list(env_source.get(per_service_key, ""))
            )
    return _trusted_author_ids_from_config(
        _split_env_id_list(env_source.get("DISCORD_TRUSTED_AUTHOR_IDS", ""))
    )


DEFAULT_DISCORD_AUTH_SCHEME = "raw"
_DISCORD_AUTH_SCHEME_VOCABULARY = ("raw", "account", "bot")


def _resolve_discord_auth_scheme(
    bot_name: str,
    accounts_env_values: Mapping[str, str],
) -> str:
    """Resolve the Discord credential type for a bot service.

    File-first like every other credential: ``DISCORD_BOT_<NAME>_AUTH_SCHEME``
    then the global ``DISCORD_AUTH_SCHEME``, each falling back to the process
    environment, defaulting to ``raw`` (the recorded operator default — the
    unconfigured case is an account/user credential, not a bot one). ``raw`` and
    ``account`` name the same credential kind, so both are accepted. Bot and
    account tokens are not interchangeable, so an unknown value must never be
    guessed: it raises. The per-bot key is built with the same canonicalisation
    as the token key, otherwise a punctuated service name would read a key
    nobody can configure.
    """
    canonical_name = discord_bot_env_canonical_name(bot_name)
    per_bot_keys = (
        (discord_bot_env_key(canonical_name, "AUTH_SCHEME"),) if canonical_name else ()
    )
    for key in (*per_bot_keys, "DISCORD_AUTH_SCHEME"):
        resolved = resolve_env_value(accounts_env_values, key).strip().lower()
        if not resolved:
            continue
        if resolved not in _DISCORD_AUTH_SCHEME_VOCABULARY:
            raise ValueError(
                f"Unsupported Discord auth_scheme {resolved!r} for {bot_name or 'global'}; "
                f"expected one of {', '.join(_DISCORD_AUTH_SCHEME_VOCABULARY)}."
            )
        return resolved
    return DEFAULT_DISCORD_AUTH_SCHEME


def _deployment_discord_auth_scheme(deployment: LiveDeploymentConfig) -> str:
    """Return this deployment's explicit auth-scheme override, if any.

    The scheme is fundamentally a property of the *credential*, not of the
    deployment, so an unconfigured deployment returns ``""`` and the caller
    falls through to the credential-scoped resolution in
    :func:`_resolve_discord_auth_scheme`. Only an operator who pinned
    ``discord_auth_scheme`` on the deployment or its settings overrides that.
    """
    configured = deployment.signal_config if isinstance(deployment.signal_config, dict) else {}
    settings = deployment.settings if isinstance(deployment.settings, dict) else {}
    raw = str(
        configured.get("discord_auth_scheme")
        or settings.get("discord_auth_scheme")
        or ""
    ).strip().lower()
    if not raw:
        return ""
    if raw not in _DISCORD_AUTH_SCHEME_VOCABULARY:
        raise ValueError(
            f"Unknown discord_auth_scheme {raw!r}; expected one of "
            f"{', '.join(_DISCORD_AUTH_SCHEME_VOCABULARY)}."
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
    # Resolve the broker-reported account number once: routing must be validated
    # against broker identity, not the local account id.
    account_broker_login = _live_account_broker_login(account_id)
    for deployment in deployments_for_channel:
        # The cheap, deployment-local filters first: they need only this deployment's
        # own configuration, so a deployment that cannot be selected for this signal at
        # all is never asked for the settings that decide its exit classification.
        if not _deployment_matches_stockkit_route(
            deployment,
            trade_signal.metadata,
            account_broker_login=account_broker_login,
            accounts_env_values=accounts_env_values,
        ):
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="stockkit_route_mismatch",
            )
            continue
        if not _deployment_covers_discord_symbol(deployment, trade_signal.symbol):
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="symbol_not_covered",
            )
            continue
        if not _deployment_accepts_signal_author(
            deployment, signal_author_id, accounts_env_values=accounts_env_values
        ):
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
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="author_not_allowed",
                detail=signal_author_id or None,
            )
            continue
        # Classified per deployment, not once for the fan-out: a plain SELL closes on a
        # deployment that cannot short and is a short entry on one that can (#121), so a
        # single answer here would drop a close on one of them (#126 decision).
        try:
            signal_is_exit = _signal_is_exit(
                trade_signal.action, trade_signal.metadata, deployment
            )
        except ValueError as exc:
            # A config this deployment cannot read costs *this* deployment only. Returning
            # here would drop the signal for every other deployment on the channel -
            # including their closes, which is the money path (#311 review: an unparseable
            # `allow_short` on a deployment that is not even on this symbol used to stop
            # a valid sibling's close).
            logger.exception(
                "Invalid deployment boolean config; skipping deployment",
                extra={
                    "data": {
                        "account_id": account_id,
                        "deployment_id": str(deployment.id),
                        "strategy_name": deployment.strategy_name,
                    }
                },
            )
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="invalid_deployment_config",
                detail=str(exc),
            )
            continue
        if not _deployment_can_dispatch(deployment, is_exit=signal_is_exit):
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="deployment_not_active",
            )
            continue

        try:
            wait_enabled = _deployment_wait_for_candle_close(deployment, accounts_env_values)
        except ValueError as exc:
            # A typo'd boolean must skip the deployment, not kill the listener
            # loop or other deployments on the same account. It also must not
            # default into immediate market orders. Same shape as the
            # classification refusal above: `continue`, never `return`.
            logger.exception(
                "Invalid deployment boolean config; skipping deployment",
                extra={
                    "data": {
                        "account_id": account_id,
                        "strategy_name": deployment.strategy_name,
                    }
                },
            )
            record_signal_decision(
                trade_signal,
                account_id=account_id,
                deployment=deployment,
                outcome="refused",
                reason="invalid_deployment_config",
                detail=str(exc),
            )
            continue

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


def _service_id_for_discord_deployment(deployment: LiveDeploymentConfig) -> str | None:
    """The service row a deployment's Discord listener should report through.

    Joins on the deployment's service name, canonicalised the same way service
    discovery names the rows it creates, so both sides derive the key from one
    rule instead of two that can drift.

    Returns None when there is no unique match. It deliberately does not create a
    row, and does not fall back to "the first discord service": two services for
    one bot, or none, must not silently attach this listener to the wrong bot's
    status. No observability is recoverable; a wrong status is not.
    """
    name = discord_bot_env_canonical_name(_deployment_service_name(deployment))
    if not name:
        return None
    matches = [
        service
        for service in list_services(service_type=ServiceType.DISCORD_BOT)
        if discord_bot_env_canonical_name(service.name) == name
    ]
    if len(matches) != 1:
        return None
    return str(matches[0].id)


async def _run_discord_signal_listener_for_account(
    account_id: str,
    adapter: BaseLiveAdapter,
    deployments: list[LiveDeploymentConfig],
    *,
    accounts_env_values: Mapping[str, str],
) -> None:
    dispatchable_deployments = _dispatchable_discord_deployments_for_account(
        account_id, deployments
    )
    if not dispatchable_deployments:
        logger.info(
            "No dispatchable discord deployments for account; discord listener idle",
            extra={"data": {"account_id": account_id}},
        )
        return

    channel_to_deployments = _group_discord_deployments_by_channel(
        account_id,
        dispatchable_deployments,
        accounts_env_values=accounts_env_values,
    )
    if not channel_to_deployments:
        logger.warning(
            "No valid discord channels configured for dispatchable discord deployments",
            extra={"data": {"account_id": account_id}},
        )
        return

    # Resolve a real account token (account/user tokens, not a generic bot token).
    # Not simply the head of the list: see `_primary_discord_deployment`.
    primary_deployment = _primary_discord_deployment(dispatchable_deployments)
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
                        str(deployment.id) for deployment in dispatchable_deployments
                    ),
                }
            },
        )
        return
    # Token and auth scheme must come from the same credential scope, so they
    # are resolved together (see `_resolve_account_discord_credentials`).
    discord_token, discord_auth_scheme = _resolve_account_discord_credentials(
        accounts_env_values,
        primary_deployment,
    )
    source = DiscordSignalSource(
        token=discord_token,
        channel_ids=set(channel_to_deployments.keys()),
        timeframe=_deployment_signal_timeframe(primary_deployment),
        trusted_author_ids=merged_trusted_author_ids,
        auth_scheme=discord_auth_scheme,
        # Without this the deployment path could not report a mis-set allowlist
        # at all: `_set_service_status` returns immediately when service_id is
        # empty, so a5a76ad's streak alert had nowhere to write and the wrong
        # allowlist stayed invisible on the path that all three live deployments
        # actually use. Resolved, never created - a deployment with no matching
        # service row simply gets no observability, which is the safe direction.
        service_id=_service_id_for_discord_deployment(primary_deployment),
        account_id=account_id,
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
                record_signal_decision(
                    trade_signal,
                    account_id=account_id,
                    deployment=None,
                    outcome="refused",
                    reason="unmapped_channel",
                    detail=channel_id or None,
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
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Could not initialize alpaca adapter",
                extra={"data": {"account_id": account.id, "error": str(exc)}},
            )
            return None
    if account.broker == "ctrader":
        ctrader_credentials = _resolve_ctrader_credentials(env_values)
        if ctrader_credentials is None:
            logger.warning(
                "Missing cTrader credentials for account",
                extra={"data": {"account_id": account.id}},
            )
            return None
        app_id, app_secret, access_token, refresh_token, demo = ctrader_credentials
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
                accounts_env_path=_accounts_env_file(accounts_env_path or None),
                connect_max_attempts=1,
            )
        except Exception as exc:  # except-kind: money-decision
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
    except Exception as exc:  # except-kind: loop-survival
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
        except Exception as exc:  # except-kind: loop-survival
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
        canonical_name = discord_bot_env_canonical_name(service.name)
        token_env_key = discord_bot_env_key(canonical_name, "TOKEN")
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
        except Exception as exc:  # except-kind: loop-survival
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
                    respect_operator_intent=True,
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
            respect_operator_intent=True,
        )
        _DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.pop(service_id, None)
        logger.info(
            "Reconnected Discord bot %s",
            service.name,
        )
    except Exception as exc:  # except-kind: loop-survival
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
            await asyncio.to_thread(
            update_service_status,
            service_id,
            ServiceStatus.FAILED,
            respect_operator_intent=True,
        )
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
    # The caller's list is a snapshot taken before this tick's work. Re-read the
    # row so a bot paused in the meantime is not started and then stopped a tick
    # later; PAUSED is operator intent, not a transient state.
    current = await asyncio.to_thread(get_service, service_id)
    if current is None or current.status != ServiceStatus.RUNNING:
        logger.info(
            "Discord service is no longer RUNNING; refusing to start it",
            extra={"data": {"service_id": service_id}},
        )
        return
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
        await asyncio.to_thread(
            update_service_status,
            service_id,
            ServiceStatus.FAILED,
            respect_operator_intent=True,
        )
        return

    trusted_author_ids = _resolve_discord_service_trusted_author_ids(
        service, accounts_env_values=accounts_env_values
    )
    if not trusted_author_ids:
        logger.warning(
            "Discord service missing trusted_author_ids; refusing open gate",
            extra={"data": {"service_id": service_id, "service_name": service.name}},
        )
        await asyncio.to_thread(
            update_service_status,
            service_id,
            ServiceStatus.FAILED,
            respect_operator_intent=True,
        )
        return
    service_config = service.config if isinstance(service.config, dict) else {}
    # The scheme belongs to the credential, so it is resolved from the same
    # per-bot scope the token key uses; an explicit per-service override still
    # wins. `_resolve_discord_auth_scheme` validates the vocabulary and raises on
    # an unknown value rather than guessing between account and bot tokens.
    try:
        override = str(service_config.get("discord_auth_scheme") or "").strip().lower()
        if override and override not in _DISCORD_AUTH_SCHEME_VOCABULARY:
            # An explicit override is not exempt from the vocabulary: a typo must
            # fail the service, not reach the source and be rejected there with a
            # different error (or, worse, be guessed).
            raise ValueError(
                f"Unsupported Discord auth_scheme {override!r} for {service.name!r}; "
                f"expected one of {', '.join(_DISCORD_AUTH_SCHEME_VOCABULARY)}."
            )
        auth_scheme = override or _resolve_discord_auth_scheme(
            service.name, accounts_env_values
        )
    except ValueError as exc:
        logger.warning(
            "Discord service has unknown discord_auth_scheme",
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
            respect_operator_intent=True,
        )
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
        except Exception as exc:  # except-kind: loop-survival
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
    accounts_env: Path | str | None = None,
    interval_seconds: int = SERVICE_SYNC_INTERVAL_SECONDS,
) -> None:
    accounts_env = _accounts_env_file(accounts_env)
    accounts_env_values = _load_accounts_env_values(accounts_env)
    accounts_env_mtime = _accounts_env_mtime(accounts_env)
    while True:
        # Fail closed (#126): a deployment with an unresolved broker order id conflict
        # stays halted. This is the tick that re-pauses it if a status write, a restore
        # or an older build left it trading, so the halt is never only the one write
        # that happened when the conflict was recorded.
        #
        # First in the iteration and inside its own `try`, because the rest of the tick
        # can fail on its own: service discovery, a bad service row or a credential
        # reload raised before this point would skip the re-halt entirely, and a warning
        # about a failed service sync is not something anyone reads as "a deployment
        # with an untracked broker order went back to trading".
        try:
            halted = await asyncio.to_thread(enforce_broker_order_conflict_halts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: loop-survival
            # The halt is the safety property. Failing to write it must be loud, and it
            # must not stop this loop from going on to keep the services in sync.
            logger.error(
                "ALERT: could not enforce the broker order id conflict halts; an "
                "untracked broker order may exist and a deployment may be trading",
                extra={
                    "data": {
                        "alert": "live_broker_order_id_conflict_enforce_failed",
                        "error": str(exc),
                    }
                },
            )
        else:
            if halted:
                # The alert itself is ERROR, from the enforcement: this tick only records
                # that the halt was re-asserted.
                logger.info(
                    "Re-halted %d deployment(s) for unresolved broker order id "
                    "conflicts (deployment_ids=%s)",
                    len(halted),
                    [row["deployment_id"] for row in halted],
                    extra={"data": {"halted": halted}},
                )
        try:
            current_mtime = _accounts_env_mtime(accounts_env)
            if current_mtime != accounts_env_mtime:
                accounts_env_values = _load_accounts_env_values(accounts_env)
                accounts_env_mtime = current_mtime
                await asyncio.to_thread(
                    _discover_and_register_discord_services, accounts_env, log_summary=False
                )
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
        except Exception as exc:  # except-kind: loop-survival
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
    current_deployments: Callable[[], list[LiveDeploymentConfig]] | None = None,
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
        # Loss circuit breaker (#301): advance the account and deployment
        # scopes with the equity just read, so a breach latches and alerts on
        # the poll even when no entry signal arrives. Deployment scopes need
        # each deployment's own trading P&L (account equity minus the OTHER
        # deployments' P&L); ``None`` skips them this round while the account
        # scope still observes, and the entry gate fails closed on what it
        # cannot measure. Off the event loop - it is several SQLite
        # round-trips per scope plus broker quote/pip calls. A failure is
        # logged and never reaches the poll's balance handling.
        deployments = current_deployments() if current_deployments is not None else ()
        # The breaker observes equity-with-unrealized-P&L through the adapter's
        # separate equity path (``get_equity``), NOT the balance snapshot's
        # equity slot: on cTrader that slot is realized balance by design
        # (#267 - sizing stays on balance). An adapter without ``get_equity``
        # (a test double) falls back to the tuple's equity slot. A failed
        # equity read is logged distinctly here and skips only the breaker
        # observation - the entry gate re-checks and fails closed.
        get_equity = getattr(adapter, "get_equity", None)
        try:
            breaker_equity = (
                float(await get_equity())
                if get_equity is not None
                else float(equity)
            )
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "Loss breaker equity read failed; skipping breaker observation "
                "this poll (the entry gate still fails closed)",
                extra={
                    "data": {
                        "account_id": account_id,
                        "adapter": type(adapter).__name__,
                        "error": str(exc),
                    }
                },
            )
            breaker_equity = None
        if breaker_equity is not None:
            # The ACCOUNT scope is observed FIRST, before the deployment-P&L
            # computation: on cTrader that marking can take seconds per open
            # symbol inside the poll timeout, and observing it only afterwards
            # would let a slow P&L starve the between-entries alert entirely.
            # The second call below re-observes the same account equity
            # (idempotent) and adds the deployment scopes.
            await asyncio.to_thread(
                observe_account_loss_breaker_equity,
                account_id,
                breaker_equity,
                now=snapshot_time,
            )
            deployment_pnls = await account_deployment_trading_pnls(adapter, account_id)
            await asyncio.to_thread(
                observe_account_loss_breaker_equity,
                account_id,
                breaker_equity,
                now=snapshot_time,
                deployments=deployments,
                deployment_pnls=deployment_pnls,
            )
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
    except Exception as exc:  # except-kind: loop-survival
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
    adapters: dict[str, BaseLiveAdapter],
    *,
    interval_seconds: int = BALANCE_POLL_INTERVAL_SECONDS,
    current_deployments: Callable[[], list[LiveDeploymentConfig]] | None = None,
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
                    _poll_adapter_balance(
                        adapters,
                        account_id,
                        adapter,
                        snapshot_time,  # noqa: B023 - _poll_one is gathered and awaited within this loop iteration
                        current_deployments=current_deployments,
                    ),
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


#: How often unknown commissions are backfilled (#124). Alpaca posts crypto fees once a day, so an
#: hourly pass prices a posted day within the hour and costs a handful of REST calls.
COMMISSION_BACKFILL_INTERVAL_SECONDS = 3600
#: One hung broker must not stall the pass for the others.
COMMISSION_BACKFILL_TIMEOUT_SECONDS = 120.0


async def _backfill_commissions_once(adapters: dict[str, BaseLiveAdapter]) -> None:
    """Run every ready adapter's commission backfill once; a failure is logged, never raised."""
    ready = [
        (account_id, adapter)
        for account_id, adapter in list(adapters.items())
        if _adapter_is_ready(adapter)
    ]

    async def _backfill_one(account_id: str, adapter: BaseLiveAdapter) -> None:
        try:
            updated = await asyncio.wait_for(
                adapter.backfill_commissions(), timeout=COMMISSION_BACKFILL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Commission backfill failed; commissions stay unknown until the next pass",
                extra={
                    "data": {
                        "account_id": account_id,
                        "adapter": type(adapter).__name__,
                        "error": str(exc) or type(exc).__name__,
                    }
                },
            )
            return
        if updated:
            logger.info(
                "Backfilled commissions",
                extra={"data": {"account_id": account_id, "executions": updated}},
            )

    await asyncio.gather(*(_backfill_one(account_id, adapter) for account_id, adapter in ready))


async def _backfill_commissions_loop(
    adapters: dict[str, BaseLiveAdapter],
    *,
    interval_seconds: int = COMMISSION_BACKFILL_INTERVAL_SECONDS,
) -> None:
    while True:
        await _backfill_commissions_once(adapters)
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


# #242: the status/source vocabulary is shared with both preflights (they must
# never disagree about what "ACTIVE" and "tradeable" mean). Kept under the name
# every call site in this module already uses.
_normalize_deployment_status = normalize_deployment_status


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


#: Halt modes that can only be enforced through an adapter (see
#: :func:`_accounts_needing_halt_enforcement`).
_HALT_MODES_NEEDING_AN_ADAPTER = frozenset(
    {KillSwitchMode.ORDERS, KillSwitchMode.FLATTEN}
)


def _accounts_needing_halt_enforcement(
    halt_state: KillSwitchState,
    deployments: list[LiveDeploymentConfig],
    registered_account_ids: Iterable[str] = (),
) -> set[str]:
    """Accounts whose halt needs a connected adapter to be enforced.

    The mirror image of :func:`_account_ids_needing_stopped_enforcement`. A mode
    that has to touch the broker (``orders``, ``flatten``) can only run through an
    adapter, and an account whose deployments are all PAUSED or STOPPED is
    otherwise never connected - so its halt would be reported as unenforceable on
    every tick while its positions stayed open, which is the STOPPED machinery's
    own lesson.

    Registered accounts are candidates as well as deployed ones, so a halt on (or
    a global halt over) an account with no deployment row is still enforced
    rather than silently skipped: the account is registered, the operator halted
    it, and their broker-side position is the thing being halted.

    Retention is never permission: the accounts returned here are added to the
    retained set so an adapter exists for the sweep, and only the runnable set
    gets listener tasks, so this cannot make an account tradeable. An unreadable
    halt state answers ``entries`` - which needs no adapter - so a broken read
    connects nothing extra and still refuses every entry.
    """
    candidates = {deployment.account_id for deployment in deployments} | {
        str(account_id) for account_id in registered_account_ids
    }
    return {
        account_id
        for account_id in candidates
        if halt_state.mode_for_account(account_id) in _HALT_MODES_NEEDING_AN_ADAPTER
    }


def _account_ids_needing_stopped_enforcement(
    deployments: list[LiveDeploymentConfig],
    already_enforced: set[str],
) -> set[str]:
    """Accounts holding a STOPPED deployment whose cancel/flatten is still due.

    These accounts need a connected adapter because the adapter is what performs
    the sweep. Connecting only runnable accounts meant an account whose
    deployments were all STOPPED never got one, so the stop was retried forever
    and never actually enforced.

    Returns account ids only; whether they may *trade* is decided separately
    from the runnable set, so connecting for enforcement never grants trading
    permission.
    """
    return {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) == "STOPPED"
        # The persisted stamp is authoritative: a deployment resumed and re-stopped
        # between ticks has no stamp (the resume cleared it) while this process's
        # ledger can still list it, and trusting the ledger alone would leave its
        # account without an adapter, so the stop would never be enforced.
        and (
            deployment.stopped_enforced_at is None
            or str(deployment.id) not in already_enforced
        )
    }


def _stopped_enforcement_refusal(
    account_lookup: Mapping[str, LiveAccountConfig] | None,
    account_id: str,
) -> str | None:
    """Why this account can never get an adapter, or None when it can."""
    if account_lookup is None:
        return None
    account = account_lookup.get(account_id)
    if account is None:
        return "account is not in the database"
    return None


async def _enforce_stopped_deployments(
    deployments: list[LiveDeploymentConfig],
    active_adapters: dict[str, BaseLiveAdapter],
    *,
    already_enforced: set[str],
    account_lookup: Mapping[str, LiveAccountConfig] | None = None,
) -> None:
    """Make STOPPED mean what an operator expects: nothing open, nothing resting.

    For each STOPPED deployment: cancel working orders (leaving attached SL/TP
    alone), flatten positions, then sweep protections orphaned by the flatten.
    Enforced once per transition, not every tick, so a persistently stopped
    deployment does not spam the broker.

    A deployment is recorded as enforced **only when every sweep reports
    ``ControlOutcome.complete``**. `completed > 0` is not success: it is equally
    true of a sweep that closed one of three positions, and recording that as
    stopped is how open exposure survives behind a dashboard that says
    "stopped". A partial sweep is logged with its outstanding items and retried
    on the next tick. The orphaned-protection sweep counts too: a stop that
    outlived its position can still trigger and open a new one.

    The cancel is the one sweep that does not gate the next step: a surviving
    order - or a cancel call that failed outright - still lets the flatten run,
    because leaving positions open is worse than leaving an order resting. It
    does gate the stamp.

    The stamp is also withheld while the cancel sweep reports ids still in the
    broker's ``pending_cancel`` state (``ControlOutcome.pending_cancel``): such an
    order can still fill in a cancel/fill race, so the pass logs a WARNING naming
    them and retries, escalating to ERROR after
    ``PENDING_CANCEL_ESCALATION_PASSES`` consecutive passes. A fill is flattened
    by the next pass before the stamp. cTrader never reports any, so it is unchanged.
    """
    stopped = [
        deployment
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) == "STOPPED"
    ]
    stopped_ids = {str(deployment.id) for deployment in stopped}
    # Forget deployments that are no longer stopped, so a later STOPPED
    # transition enforces again instead of being skipped as "already done".
    left_stopped = {
        deployment_id
        for deployment_id in already_enforced
        if deployment_id not in stopped_ids
    }
    already_enforced.difference_update(left_stopped)
    # The pending_cancel escalation count belongs to one STOPPED episode. Drop it for
    # any deployment that is no longer STOPPED or was deleted - including one that
    # never got stamped, which the ledger above does not track - so the count cannot
    # grow without bound or carry over into a later STOPPED episode.
    for deployment_id in list(_PENDING_CANCEL_PASSES):
        if deployment_id not in stopped_ids:
            del _PENDING_CANCEL_PASSES[deployment_id]
    # The persisted stamp is authoritative, not this process's ledger: a deployment
    # that was resumed and stopped again between two ticks was never observed
    # leaving STOPPED, so the ledger still lists it while its stamp (cleared by the
    # resume, transactionally) is gone and its positions are open again.
    already_enforced.difference_update(
        {
            str(deployment.id)
            for deployment in stopped
            if deployment.stopped_enforced_at is None
        }
    )
    for deployment_id in sorted(left_stopped):
        try:
            await asyncio.to_thread(
                clear_live_deployment_stopped_enforced, deployment_id
            )
        except Exception as exc:  # pragma: no cover - defensive  # except-kind: money-decision
            logger.warning(
                "Could not clear a stale STOPPED enforcement stamp",
                extra={"data": {"deployment_id": deployment_id, "error": repr(exc)}},
            )

    # cancel/flatten act on the whole broker account, not on one deployment's
    # positions. If another deployment on the same account is ACTIVE or PAUSED,
    # flattening would close positions that deployment still owns.
    position_owning_accounts = {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) in {"ACTIVE", "PAUSED"}
    }

    for deployment in stopped:
        deployment_id = str(deployment.id)
        if deployment.stopped_enforced_at is not None:
            already_enforced.add(deployment_id)
            continue

        if deployment.account_id in position_owning_accounts:
            logger.error(
                "Refusing to flatten a shared account: another deployment on it is "
                "still ACTIVE or PAUSED and its positions would be closed too",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )
            continue

        adapter = active_adapters.get(deployment.account_id)
        if adapter is None:
            block_reason = _stopped_enforcement_refusal(
                account_lookup, deployment.account_id
            )
            if block_reason is not None:
                logger.error(
                    "Stopped deployment's account can never get an adapter, so the "
                    "cancel/flatten can never run; flatten it at the broker by hand. "
                    "Reason: %s",
                    block_reason,
                    extra={
                        "data": {
                            "deployment_id": deployment_id,
                            "account_id": deployment.account_id,
                        }
                    },
                )
            else:
                logger.info(
                    "Stopped deployment has no active adapter yet; will retry cancel/flatten",
                    extra={
                        "data": {
                            "deployment_id": deployment_id,
                            "account_id": deployment.account_id,
                        }
                    },
                )
            continue

        cancel_error: Exception | None = None
        try:
            cancelled = (
                await adapter.cancel_open_orders()
                if adapter.supports_cancel_orders
                else None
            )
        except NotImplementedError:
            cancelled = None
        except Exception as exc:  # except-kind: money-decision
            # Still flatten. The cancel failing says nothing about whether the
            # positions can be closed - the Alpaca flatten cancels its own
            # per-symbol orders - and leaving exposure open to protect a
            # cosmetic ordering rule is the wrong trade.
            cancelled = None
            cancel_error = exc
            logger.error(
                "Failed to cancel open orders for stopped deployment; flattening "
                "anyway",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "error": repr(exc),
                    }
                },
            )

        # A partial cancel must not stop the flatten: closing the positions is
        # the money-safety action, and the remaining order is retried on the
        # next tick. It does, however, prevent the enforced stamp - the account
        # may still have something resting.
        cancel_ok = cancelled is not None and cancelled.complete
        if cancel_error is not None:
            pass  # already logged above
        elif cancelled is None:
            logger.error(
                "Adapter cannot cancel orders; flattening anyway and keeping the "
                "deployment unenforced",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "adapter": type(adapter).__name__,
                    }
                },
            )
        elif not cancel_ok:
            logger.error(
                "Order cancel was incomplete for stopped deployment; flattening "
                "anyway and retrying the cancel: %s",
                cancelled.describe(),
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )

        try:
            flattened = (
                await adapter.close_all_positions() if adapter.supports_flatten else None
            )
        except NotImplementedError:
            flattened = None
        except Exception as exc:  # except-kind: money-decision
            logger.error(
                "Failed to flatten positions for stopped deployment",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "error": repr(exc),
                    }
                },
            )
            continue

        if flattened is None:
            logger.error(
                "Adapter cannot flatten positions; stopped deployment is NOT flat",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "adapter": type(adapter).__name__,
                    }
                },
            )
            continue
        if not flattened.complete:
            logger.error(
                "Flatten was incomplete for stopped deployment; keeping it "
                "unenforced and retrying: %s",
                flattened.describe(),
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )
            continue

        # The account is flat. Sweep the protections the flatten detached.
        # This is not merely cosmetic: a stop that outlived its position can
        # still trigger, and a triggered sell-stop with nothing to sell opens a
        # short. So a sweep that *failed* keeps the deployment unenforced and
        # retries; only an adapter that has no such concept may stamp without
        # one.
        orphan_sweep_unsupported = False
        try:
            orphaned = await adapter.cancel_orphaned_protections()
        except NotImplementedError:
            orphaned = None
            orphan_sweep_unsupported = True
        except Exception as exc:  # except-kind: money-decision
            logger.error(
                "Failed to sweep protections orphaned by the flatten; the account "
                "is flat but a resting protection could still trigger",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "error": repr(exc),
                    }
                },
            )
            orphaned = None
        if orphan_sweep_unsupported:
            logger.warning(
                "Adapter cannot identify orphaned protections; the account is flat "
                "but protection orders may still be listed by the broker",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "adapter": type(adapter).__name__,
                    }
                },
            )
        elif orphaned is not None and not orphaned.complete:
            logger.error(
                "Orphaned-protection sweep was incomplete; the account is flat but "
                "protections may still be resting: %s",
                orphaned.describe(),
                extra={"data": {"deployment_id": deployment_id}},
            )

        # Every sweep must be complete before the deployment counts as
        # enforced. The account is flat either way - the money-safety part is
        # done - but a surviving order means the next tick must try again.
        if not cancel_ok:
            logger.error(
                "Not recording STOPPED enforcement: the order cancel did not "
                "complete, so orders may still be resting. Retrying next tick: %s",
                (
                    cancelled.describe()
                    if cancelled is not None
                    else (
                        f"cancel failed: {cancel_error!r}"
                        if cancel_error is not None
                        else "adapter cannot cancel open orders"
                    )
                ),
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )
            continue
        # ``not cancel_ok`` continued above, and ``cancel_ok`` is
        # ``cancelled is not None and cancelled.complete``: the outcome is
        # non-None here. mypy cannot follow that conjunction through the
        # ``continue``.
        if cancelled.pending_cancel:  # type: ignore[union-attr]
            passes = _PENDING_CANCEL_PASSES.get(deployment_id, 0) + 1
            _PENDING_CANCEL_PASSES[deployment_id] = passes
            log = (
                logger.error
                if passes >= PENDING_CANCEL_ESCALATION_PASSES
                else logger.warning
            )
            log(
                "Not recording STOPPED enforcement: order(s) %s still pending_cancel "
                "and may yet fill (pass %d). Retrying next tick",
                sorted(cancelled.pending_cancel),  # type: ignore[union-attr]  # non-None per cancel_ok
                passes,
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                        "pending_cancel": sorted(cancelled.pending_cancel),  # type: ignore[union-attr]  # non-None per cancel_ok
                        "passes": passes,
                    }
                },
            )
            continue
        _PENDING_CANCEL_PASSES.pop(deployment_id, None)
        if not orphan_sweep_unsupported and (
            orphaned is None or not orphaned.complete
        ):
            logger.error(
                "Not recording STOPPED enforcement: the orphaned-protection sweep "
                "did not complete. Retrying next tick",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )
            continue

        try:
            stamped = await asyncio.to_thread(
                mark_live_deployment_stopped_enforced, deployment_id
            )
        except Exception as exc:  # pragma: no cover - defensive  # except-kind: money-decision
            logger.warning(
                "Could not record STOPPED enforcement for a deployment; retrying next tick",
                extra={"data": {"deployment_id": deployment_id, "error": repr(exc)}},
            )
            continue
        if not stamped:
            # Refused by the database: the deployment left STOPPED, or a submission
            # lease (live or expired-but-unreleased) says an order may still fill.
            # Either way the sweep does not count yet; the next tick runs it again.
            logger.warning(
                "Not recording STOPPED enforcement: the deployment is no longer STOPPED "
                "or a submission lease is outstanding, so an in-flight order could still "
                "fill. Retrying next tick",
                extra={
                    "data": {
                        "deployment_id": deployment_id,
                        "account_id": deployment.account_id,
                    }
                },
            )
            continue
        already_enforced.add(deployment_id)
        logger.warning(
            "Enforced STOPPED deployment: orders cancelled and positions flattened",
            extra={
                "data": {
                    "deployment_id": deployment_id,
                    "account_id": deployment.account_id,
                    "cancelled": cancelled.describe(),  # type: ignore[union-attr]  # non-None per cancel_ok
                    "flattened": flattened.describe(),
                }
            },
        )


async def _load_db_accounts_and_deployments(
) -> tuple[list[LiveAccountConfig], list[LiveDeploymentConfig]]:
    accounts_task = asyncio.to_thread(list_live_accounts)
    deployments_task = asyncio.to_thread(list_live_deployments)
    accounts, deployments = await asyncio.gather(accounts_task, deployments_task)
    # #150: a stored deployment whose trailing override live cannot run is still
    # loaded (so it stays listed, stoppable and enforceable), but every signal
    # it produces is refused by the live gates. Say so loudly at load time. The
    # same holds for a mode that only comes from the bot's own config default:
    # it is not visible in the deployment's config, so the processor that would
    # emit it is built here and asked (its config file is a few KB).
    account_brokers = {account.id: account.broker for account in accounts}
    for deployment in deployments:
        try:
            validate_live_deployment_trailing_mode(
                deployment,
                context=f"live deployment '{deployment.strategy_name}' ({deployment.id})",
                broker=account_brokers.get(deployment.account_id, ANY_BROKER),
                timeframe=_deployment_signal_timeframe(deployment),
            )
        except ValueError as exc:
            logger.error(
                "Stored live deployment has an unsupported trailing mode; its entry "
                "signals will be refused: %s",
                exc,
                extra={"data": {"deployment_id": str(deployment.id)}},
            )
        except Exception as exc:  # except-kind: loop-survival
            # Anything else reading the deployment's trailing mode can raise. It
            # must not cost the whole deployment list its sync tick: the
            # signal-time gates still refuse every signal this deployment makes,
            # so a stored row is surfaced here and traded nowhere.
            logger.error(
                "Stored live deployment's trailing mode could not be checked; its entry "
                "signals will be refused: %s",
                exc,
                extra={"data": {"deployment_id": str(deployment.id)}},
            )
    return accounts, deployments


def _warn_untradeable_active_deployments(
    deployments: Sequence[LiveDeploymentConfig],
    already_warned: dict[str, str] | None = None,
) -> None:
    """Alert (ERROR, which Sentry turns into an event) per untradeable ACTIVE deployment (#242).

    An ``internal`` deployment (or any signal source other than ``discord``)
    has no live strategy runner behind it: it can be ACTIVE, hold an adapter and
    an execution listener, and still never place an order. The daemon does not
    block on this -- the operator stops or repoints the row -- but silence makes
    an untradeable ACTIVE deployment look live.

    The definition of "untradeable" is ``live_runner.deployment_policy``'s,
    so the daemon log and both preflights cannot drift apart.

    ``already_warned`` is the operator's memory of what this process already
    reported, keyed by deployment id: the same reason for the same row is not
    repeated on every sync tick (a daemon polls forever), but a row that becomes
    untradeable, stops being it, or changes how it is untradeable is reported
    again. Pass None to log every row unconditionally (what the unit tests do).
    """
    still_untradeable: dict[str, str] = {}
    for deployment in deployments:
        if _normalize_deployment_status(deployment.status) != "ACTIVE":
            continue
        reason = untradeable_signal_source_reason(deployment.signal_source)
        if reason is None:
            continue
        deployment_id = str(deployment.id)
        # A change of state, never a repeat of one already reported: the reason
        # also encodes the signal source, so repointing a row re-warns it.
        if already_warned is not None and already_warned.get(deployment_id) == reason:
            still_untradeable[deployment_id] = reason
            continue
        signal_source = str(deployment.signal_source or "internal").lower()
        logger.error(
            "ACTIVE live deployment cannot trade: %s. It will never place an order; "
            "stop it or move it to a signal source the daemon runs.",
            f"{deployment.strategy_name} ({deployment.id}, account "
            f"{deployment.account_id}): {reason}",
            extra={
                "data": {
                    "deployment_id": deployment_id,
                    "account_id": deployment.account_id,
                    "strategy_name": deployment.strategy_name,
                    "signal_source": signal_source,
                    "untradeable_reason": reason,
                    # #242 asks for an alert, not just a log line. In this daemon the
                    # alert *is* the ERROR record: Sentry's LoggingIntegration sends
                    # event_level=ERROR (where a DSN is configured), and this field
                    # rides in the record's data -- a log field and Sentry extra,
                    # not a Sentry tag.
                    "alert": "untradeable_active_deployment",
                }
            },
        )
        still_untradeable[deployment_id] = reason
    if already_warned is not None:
        # Drop rows that are no longer ACTIVE/untradeable, so a row that comes
        # back to it is alerted again instead of staying quiet.
        already_warned.clear()
        already_warned.update(still_untradeable)


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
    account = account_lookup.get(account_id)
    if account is None:
        logger.error(
            "Refusing to construct an adapter for an unknown account",
            extra={"data": {"account_id": account_id}},
        )
        stale = active_adapters.pop(account_id, None)
        if stale is not None:
            await _disconnect_adapter(
                stale,
                account_id=account_id,
                reason="unknown_account",
            )
        return
    adapter = active_adapters.get(account_id)
    if adapter is not None and not _adapter_is_demo(adapter):
        logger.error(
            "Existing adapter is not in demo/paper mode; refusing continued use",
            extra={
                "data": {
                    "account_id": account_id,
                    "adapter": type(adapter).__name__,
                }
            },
        )
        active_adapters.pop(account_id, None)
        await _disconnect_adapter(
            adapter,
            account_id=account_id,
            reason="non_demo_adapter",
        )
        return
    if adapter is not None:
        if _adapter_is_ready(adapter):
            return
    else:
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
        if not _adapter_is_demo(adapter):
            logger.error(
                "Refusing a newly created adapter that is not in demo/paper mode",
                extra={
                    "data": {
                        "account_id": account_id,
                        "adapter": type(adapter).__name__,
                    }
                },
            )
            return
        active_adapters[account_id] = adapter

    try:
        await adapter.connect()
    except Exception as exc:  # except-kind: money-decision
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
    # `results` is one entry per `account_ids` element: `asyncio.gather`
    # returns one result per awaitable it was given, in order. `strict=True`
    # turns "the two got out of step" into an error instead of into weights
    # read off the wrong account.
    for account_id, result in zip(sorted(account_ids), results, strict=True):
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


def _deployment_dispatchable_account_ids(
    deployments: list[LiveDeploymentConfig],
) -> set[str]:
    """Accounts whose deployments can still be dispatched to.

    PAUSED is included (#126 decision): a paused deployment still closes what it
    holds, and a close needs the account's adapter and its Discord listener - the
    same two things an entry needs. An account whose deployments are all paused is
    therefore still connected; nothing trades from it, because every status gate
    below the task level refuses an entry.
    """
    return {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) in EXIT_DISPATCH_STATUSES
    }


def _discord_dispatchable_account_ids(
    deployments: list[LiveDeploymentConfig],
) -> set[str]:
    """Accounts whose listener has to stay up so an exit can reach their broker.

    The ACTIVE-only version of this set stopped the whole listener when an account
    was paused, which dropped every exit on it - the reason the #126 decision
    widened the dispatch statuses in the first place.
    """
    return {
        deployment.account_id
        for deployment in deployments
        if _normalize_deployment_status(deployment.status) in EXIT_DISPATCH_STATUSES
        and signal_source_can_trade(deployment.signal_source)
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
    accounts_env: Path | str | None = None,
    interval_seconds: int = DEPLOYMENT_POLL_INTERVAL_SECONDS,
    deployment_snapshot: list[LiveDeploymentConfig] | None = None,
) -> None:
    accounts_env = _accounts_env_file(accounts_env)
    known_deployment_statuses: dict[str, str] = {}
    # #242: what this process has already alerted about, keyed by deployment id, so
    # an untradeable ACTIVE row is reported on the first tick of every start and
    # again whenever it changes, but not on every tick of a loop that runs for days.
    already_warned_untradeable: dict[str, str] = {}
    # Seeded from the database, not empty. Every restart used to re-run cancel
    # and flatten for deployments that were already stopped, because this set
    # was process-local while the stamp it mirrors is persisted.
    try:
        enforced_stopped_deployments = await asyncio.to_thread(
            list_stopped_enforced_deployment_ids
        )
    except Exception as exc:  # pragma: no cover - defensive  # except-kind: money-decision
        # Fail toward re-enforcing rather than toward skipping it: an empty
        # ledger repeats work, a wrong one would leave a stopped deployment with
        # open positions.
        enforced_stopped_deployments = set()
        logger.warning(
            "Could not read the persisted STOPPED enforcement ledger; "
            "enforcement will be re-run for every stopped deployment this tick",
            extra={"data": {"error": repr(exc)}},
        )
    accounts_env_values = _load_accounts_env_values(accounts_env)
    accounts_env_mtime = _accounts_env_mtime(accounts_env)
    # One halt state for the process, refreshed in place once per tick (#300). The
    # halts are re-read every time - that is what makes a halt written while
    # nothing was running, or across a restart, take effect - while the sweep
    # ledger it carries (which account was already swept in which mode) has to
    # outlive a tick, or a flat account would be re-flattened every 10 s.
    halt_state = read_kill_switch_state()
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
            # The halt is read first, before any adapter work: it decides which
            # accounts need one (a mode that has to touch the broker) and the
            # entry gate asks for it on every order. Never raises - an
            # unreadable state answers 'halted in entries mode'.
            await asyncio.to_thread(halt_state.refresh)
            accounts, deployments = await _load_db_accounts_and_deployments()
            # #242: an ACTIVE row nothing can trade gets a broker adapter and an
            # execution listener, so it looks live in the log and the dashboard.
            _warn_untradeable_active_deployments(
                deployments, already_warned=already_warned_untradeable
            )
            if deployment_snapshot is not None:
                # Shared with the trailing-stop loop (#167) so it never has to
                # query deployments itself. Replaced in place: the reader
                # holds this same list object.
                deployment_snapshot[:] = deployments
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

            dispatchable_account_ids = _deployment_dispatchable_account_ids(deployments)
            await _disconnect_blocked_adapters(
                active_adapters, dispatchable_account_ids & _CTRADER_AUTH_BLOCKED_ACCOUNTS
            )
            logger.debug(
                "Evaluating deployment activation state",
                extra={
                    "data": {
                        "active_account_ids": sorted(dispatchable_account_ids),
                        "known_adapters": sorted(active_adapters.keys()),
                    }
                },
            )
            runnable_account_ids = set(dispatchable_account_ids) - _CTRADER_AUTH_BLOCKED_ACCOUNTS

            # A STOPPED deployment can only be flattened by an adapter, and a PAUSED one
            # can only close what it holds with one, so an account whose deployments are
            # all PAUSED or STOPPED still needs one (#126). The retained set is the union
            # of runnable and enforcement accounts; only an ACTIVE deployment accepts an
            # entry, and that is checked per signal (`_deployment_can_dispatch`) rather
            # than by which tasks run, so the task sets below use runnable alone and
            # connecting for enforcement never grants permission.
            enforcement_account_ids = _account_ids_needing_stopped_enforcement(
                deployments,
                enforced_stopped_deployments,
            ) - _CTRADER_AUTH_BLOCKED_ACCOUNTS
            # A halt in a mode that has to touch the broker needs an adapter for
            # the same reason (#300). Retention only: the runnable set below is
            # what may trade.
            halt_enforcement_account_ids = _accounts_needing_halt_enforcement(
                halt_state, deployments, (account.id for account in accounts)
            ) - _CTRADER_AUTH_BLOCKED_ACCOUNTS
            retained_account_ids = (
                runnable_account_ids | enforcement_account_ids | halt_enforcement_account_ids
            )

            # B2: disconnect against retained (blocked-excluded) ids — a
            # blocked account must never keep a live adapter via either path.
            await _disconnect_inactive_adapters(active_adapters, retained_account_ids)

            active_discord_account_ids = _discord_dispatchable_account_ids(deployments)

            await _connect_active_accounts(
                retained_account_ids,
                account_lookup,
                active_adapters,
                accounts_env_values=accounts_env_values,
                accounts_env_path=accounts_env,
            )

            _cancel_inactive_tasks(active_tasks, runnable_account_ids)
            _start_missing_tasks(active_adapters, active_tasks, runnable_account_ids)

            # STOPPED means "cancel entries, flatten, sweep orphaned
            # protections", enforced here rather than only in the status
            # endpoint so it also holds when another operator changed the status
            # or the daemon restarted. It runs after the connect because the
            # adapter is what performs the sweep.
            await _enforce_stopped_deployments(
                deployments,
                active_adapters,
                already_enforced=enforced_stopped_deployments,
                account_lookup=account_lookup,
            )
            # The kill switch (#300), enforced on the same tick it is read. This
            # is the second of its two checkpoints: the entry gate refuses new
            # entries, and this makes the modes that have to touch the broker
            # actually touch it - a halt written while the daemon was down is
            # enforced by the first tick after it starts. Same account set the
            # STOPPED sweep above just used, plus every connected adapter.
            kill_switch_report = await enforce_live_kill_switch(
                active_adapters,
                halt_enforcement_account_ids | retained_account_ids | set(active_adapters),
                state=halt_state,
            )
            if kill_switch_report.modes:
                logger.warning(
                    "Kill switch state enforced",
                    extra={"data": {"report": kill_switch_report.describe()}},
                )
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
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "Deployment sync iteration failed",
                extra={"data": {"error": str(exc)}},
            )
        await asyncio.sleep(interval_seconds)


def _start_balance_poller(
    active_adapters: dict[str, BaseLiveAdapter],
    deployment_snapshot: list[LiveDeploymentConfig],
) -> asyncio.Task[None]:
    """Start the balance-poll task for ``run_daemon``.

    ``current_deployments`` feeds the loss breaker's per-deployment scopes
    (#301): without it the poll would only ever observe account scopes and a
    deployment breaker could never trip between entries.
    """
    return asyncio.create_task(
        _poll_balances(
            active_adapters,
            current_deployments=lambda: list(deployment_snapshot),
        ),
        name="balance-poller",
    )


async def run_daemon(
    *,
    accounts_env: Path | str | None = None,
) -> None:
    # Ops alert channel (#304): ERROR records and explicit `alert=` records are
    # pushed to OPS_ALERT_WEBHOOK_URL when configured; unset disables it and the
    # daemon behaves exactly as before. Installed here rather than at import so
    # tools that merely import daemon helpers (--help, smoke scripts, tests)
    # stay free of the worker thread and webhook side effects. Fire-and-forget:
    # it cannot block or raise into the trading paths.
    install_alert_sink()
    try:
        accounts_env = _accounts_env_file(accounts_env)
        logger.info(
            "Starting live daemon",
            extra={"data": {"accounts_env": str(accounts_env)}},
        )
        # The loss breaker resolves LOSS_BREAKER_* env file-first through the same
        # accounts file the daemon was started with; without this a per-account
        # override in a non-default --accounts-env file would be silently ignored.
        set_accounts_env_file(accounts_env)
        active_adapters: dict[str, BaseLiveAdapter] = {}
        active_listener_tasks: dict[str, asyncio.Task[None]] = {}
        active_discord_tasks: dict[str, asyncio.Task[None]] = {}
        active_discord_bots: dict[str, DiscordSignalSource] = {}
        active_discord_bot_tasks: dict[str, asyncio.Task[None]] = {}
        scheduler = ServiceScheduler()

        try:
            await asyncio.to_thread(
                _discover_and_register_discord_services, accounts_env, log_summary=True
            )
            await asyncio.to_thread(_recover_failed_services)
            await scheduler.start()

            deployment_snapshot: list[LiveDeploymentConfig] = []
            sync_task = asyncio.create_task(
                sync_deployments_loop(
                    active_adapters,
                    active_listener_tasks,
                    active_discord_tasks,
                    accounts_env=accounts_env,
                    deployment_snapshot=deployment_snapshot,
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
            balance_task = _start_balance_poller(active_adapters, deployment_snapshot)
            trailing_task = asyncio.create_task(
                run_trailing_stops_loop(
                    LiveTrailingStopManager(),
                    eligible_adapters=lambda: _trailing_eligible_adapters(
                        active_adapters
                    ),
                    current_deployments=lambda: list(deployment_snapshot),
                ),
                name="trailing-stops-loop",
            )
            commission_task = asyncio.create_task(
                _backfill_commissions_loop(active_adapters), name="commission-backfill"
            )
        except Exception:  # except-kind: money-decision
            # A startup failure (broken DB, bad config) propagates out of
            # asyncio.run as a traceback, not an ERROR record — log one here so
            # the alert sink can page on the very failure an operator needs.
            logger.exception("Live daemon failed to start")
            raise
        try:
            await asyncio.gather(
                sync_task, service_sync_task, balance_task, trailing_task, commission_task
            )
        except asyncio.CancelledError:
            logger.info("Live daemon cancelled")
            raise
        except Exception as exc:  # except-kind: money-decision
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
            for task in (
                sync_task,
                service_sync_task,
                balance_task,
                trailing_task,
                commission_task,
            ):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(
                    sync_task,
                    service_sync_task,
                    balance_task,
                    trailing_task,
                    commission_task,
                    return_exceptions=True,
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
            # #308: flush the raw-signal archive last - records still queued by
            # the just-cancelled listeners must reach disk before exit. Off the
            # event loop: close() joins the writer thread.
            try:
                await asyncio.to_thread(get_signal_archive().close, timeout=5.0)
            except Exception:  # except-kind: loop-survival
                logger.warning(
                    "Signal archive close failed during shutdown",
                    exc_info=True,
                )
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
    finally:
        # Flush queued alerts (bounded): the shutdown/crash/startup-failure
        # ERROR records are the ones most likely to be sitting in the queue.
        # Covers startup failures too, where the task-cleanup finally above
        # never ran.
        with contextlib.suppress(Exception):
            close_alert_sink(timeout=SHUTDOWN_FLUSH_SECONDS)


def main() -> None:
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()

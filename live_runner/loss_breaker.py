"""Loss circuit breaker for live trading (#301).

Two breakers, measured on an equity series per scope:

* **Account scope** (``account:<account_id>``) - the broker account's equity,
  exactly as the adapter reports it (for cTrader that includes unrealized
  P&L).
* **Deployment scope** (``deployment:<deployment_id>``) - the deployment's
  OWN trading P&L, not the whole account's. Broker positions on a netted
  account cannot be attributed to a deployment, so the scope observes a
  synthetic equity ``V = account_equity - sum(other deployments' P&L)``:
  account equity minus every other deployment's realized+unrealized P&L
  (reconstructed from the persisted fill ledger and marked to market, see
  :func:`account_deployment_trading_pnls`). ``V`` moves only when THIS
  deployment's P&L moves, on the same account-equity scale, so the same
  fractions apply: a deployment that loses 6 % of account equity while the
  rest of the account gains 3 % still trips its own 5 % daily breaker.

* **Daily loss** — equity is down ``daily_loss_fraction`` (default 5 %) of the
  start-of-day equity. "Start of day" is the first equity observation of the
  UTC day; the breaker re-bases automatically at the UTC day boundary.
* **Overall drawdown** — equity is down ``max_drawdown_fraction`` (default
  20 %) from the peak equity ever observed. The peak rises with every
  observation and is re-based to the equity at reset time only by the
  explicit operator reset
  (``scripts.live_loss_breaker reset-drawdown``) - keeping the old peak
  would re-trip on the next observation.

Both run per account (``account:<account_id>`` scope, every deployment on the
account) and per deployment (``deployment:<deployment_id>`` scope, that
deployment's own P&L as described above). On breach the breaker blocks NEW
entries only - exits are never blocked, the same PAUSED semantics a manual
pause has. A breach latches and alerts once per episode through the
``data.alert`` log path (#331 turns that into a webhook sink); an unreadable
state, unreadable equity or unmeasurable deployment P&L fails closed and
refuses entries.

Configuration, most specific first:

* deployment ``settings`` keys ``loss_breaker_enabled``,
  ``loss_breaker_daily_loss_fraction``, ``loss_breaker_max_drawdown_fraction``
* per-account env ``LOSS_BREAKER_<ACCOUNT>_ENABLED`` /
  ``LOSS_BREAKER_<ACCOUNT>_DAILY_LOSS_FRACTION`` /
  ``LOSS_BREAKER_<ACCOUNT>_MAX_DRAWDOWN_FRACTION``
  (``<ACCOUNT>`` is the account id canonicalised like ``DISCORD_BOT_<NAME>``)
* global env ``LOSS_BREAKER_ENABLED`` / ``LOSS_BREAKER_DAILY_LOSS_FRACTION`` /
  ``LOSS_BREAKER_MAX_DRAWDOWN_FRACTION``
* defaults: enabled, 0.05 daily, 0.20 drawdown

Env values resolve file-first through ``.accounts.env`` (same overlay the
credential path uses) and are re-read when the file changes.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_runner.base_adapter import validate_live_quote_prices
from live_runner.credential_env import load_accounts_env_values
from shared.database import (
    get_account_deployment_trading_flows,
    latch_live_loss_breaker_breach,
    observe_live_loss_breaker,
    reset_live_loss_breaker_drawdown,
)
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig, LiveLossBreakerRecord

logger = get_logger("live-runner")

DEFAULT_DAILY_LOSS_FRACTION = 0.05
DEFAULT_MAX_DRAWDOWN_FRACTION = 0.20

_ENV_PREFIX = "LOSS_BREAKER_"
_ENV_ENABLED = f"{_ENV_PREFIX}ENABLED"
_ENV_DAILY = f"{_ENV_PREFIX}DAILY_LOSS_FRACTION"
_ENV_DRAWDOWN = f"{_ENV_PREFIX}MAX_DRAWDOWN_FRACTION"

_SETTINGS_ENABLED = "loss_breaker_enabled"
_SETTINGS_DAILY = "loss_breaker_daily_loss_fraction"
_SETTINGS_DRAWDOWN = "loss_breaker_max_drawdown_fraction"

_ACCOUNTS_ENV = Path(__file__).resolve().parent.parent / ".accounts.env"


def account_loss_breaker_scope(account_id: str) -> str:
    """The breaker scope covering every deployment on ``account_id``."""
    return f"account:{account_id}"


def deployment_loss_breaker_scope(deployment_id: str) -> str:
    """The breaker scope covering one deployment alone."""
    return f"deployment:{deployment_id}"


class LossBreakerBreachError(RuntimeError):
    """A new entry was refused because a loss breaker tripped (or cannot be read).

    Carries the scope/kind so a caller can report the breaker rather than a
    generic refusal, and ``state_unreadable`` so an operator can tell "the
    breaker fired" from "the breaker state could not be read" - both refuse
    entries, but only one is a trading-loss event.
    """

    def __init__(
        self,
        message: str,
        *,
        scope: str,
        kind: str | None = None,
        account_id: str | None = None,
        deployment_id: str | None = None,
        state_unreadable: bool = False,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.kind = kind
        self.account_id = account_id
        self.deployment_id = deployment_id
        self.state_unreadable = state_unreadable


# ─── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LossBreakerLimits:
    """Validated breaker thresholds of one scope; fractions of equity in (0, 1]."""

    enabled: bool
    daily_loss_fraction: float
    max_drawdown_fraction: float


def _strict_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{field_name} must be boolean, got {value!r}")


def _strict_fraction(value: object, *, field_name: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number in (0, 1], got {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a number in (0, 1], got {value!r}") from exc
    if not math.isfinite(parsed) or not 0.0 < parsed <= 1.0:
        raise ValueError(f"{field_name} must be in (0, 1], got {value!r}")
    return parsed


def _env_lookup(env_values: Mapping[str, str], key: str) -> str | None:
    """The configured value of ``key``, or None when unset (present-empty stays empty)."""
    if key in env_values:
        return env_values[key]
    return None


# ─── Env resolution (.accounts.env file-first, read fresh each call) ──────────

def _accounts_env_values() -> dict[str, str]:
    """``.accounts.env`` overlaid with the process env, read fresh each call.

    No cache: the overlay is one small file read, callers are a once-a-minute
    poll and rare entry checks, and a stale cache would pin thresholds an
    operator just changed.
    """
    return load_accounts_env_values(_ACCOUNTS_ENV)


def _env_key(account_id: str | None, suffix: str) -> str | None:
    """Per-account env key, or None when the account id cannot form one."""
    if account_id is None:
        return f"{_ENV_PREFIX}{suffix}"
    canonical = re.sub(r"[^A-Za-z0-9_]+", "_", str(account_id).strip().upper()).strip("_")
    if not canonical:
        return None
    return f"{_ENV_PREFIX}{canonical}_{suffix}"


def _env_bool(
    env_values: Mapping[str, str], account_id: str | None, suffix: str, *, default: bool
) -> bool:
    for key in (
        _env_key(account_id, suffix),
        _env_key(None, suffix),
    ):
        if key is None:
            continue
        raw = _env_lookup(env_values, key)
        if raw is None:
            continue
        token = raw.strip().lower()
        if token in {"1", "true", "yes", "y", "on"}:
            return True
        if token in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(
            f"Invalid boolean value for {key}: {raw!r}. Valid values are: "
            "0, 1, true, false, yes, no, on, off."
        )
    return default


def _env_fraction(
    env_values: Mapping[str, str], account_id: str | None, suffix: str, *, default: float
) -> float:
    for key in (
        _env_key(account_id, suffix),
        _env_key(None, suffix),
    ):
        if key is None:
            continue
        raw = _env_lookup(env_values, key)
        if raw is None:
            continue
        return _strict_fraction(raw, field_name=key)
    return default


def account_loss_breaker_limits(
    account_id: str,
    *,
    env_values: Mapping[str, str] | None = None,
) -> LossBreakerLimits:
    """The account scope's limits: per-account env, then global env, then defaults."""
    env = env_values if env_values is not None else _accounts_env_values()
    return LossBreakerLimits(
        enabled=_env_bool(env, account_id, "ENABLED", default=True),
        daily_loss_fraction=_env_fraction(
            env, account_id, "DAILY_LOSS_FRACTION", default=DEFAULT_DAILY_LOSS_FRACTION
        ),
        max_drawdown_fraction=_env_fraction(
            env, account_id, "MAX_DRAWDOWN_FRACTION", default=DEFAULT_MAX_DRAWDOWN_FRACTION
        ),
    )


def deployment_loss_breaker_limits(
    deployment: LiveDeploymentConfig,
    *,
    env_values: Mapping[str, str] | None = None,
) -> LossBreakerLimits:
    """The deployment scope's limits: its ``settings`` override the account's.

    Only keys actually present in ``settings`` override; an absent key inherits
    the account-level value, so a deployment can tighten one breaker without
    restating the other.
    """
    account_limits = account_loss_breaker_limits(
        deployment.account_id, env_values=env_values
    )
    settings = deployment.settings or {}
    enabled = account_limits.enabled
    if _SETTINGS_ENABLED in settings:
        enabled = _strict_bool(settings[_SETTINGS_ENABLED], field_name=_SETTINGS_ENABLED)
    daily = account_limits.daily_loss_fraction
    if _SETTINGS_DAILY in settings:
        daily = _strict_fraction(settings[_SETTINGS_DAILY], field_name=_SETTINGS_DAILY)
    drawdown = account_limits.max_drawdown_fraction
    if _SETTINGS_DRAWDOWN in settings:
        drawdown = _strict_fraction(
            settings[_SETTINGS_DRAWDOWN], field_name=_SETTINGS_DRAWDOWN
        )
    return LossBreakerLimits(
        enabled=enabled,
        daily_loss_fraction=daily,
        max_drawdown_fraction=drawdown,
    )


# ─── Observation, evaluation, alerting ────────────────────────────────────────


def _breach_detail(
    *,
    kind: str,
    scope: str,
    account_id: str,
    equity: float,
    day_start_equity: float,
    peak_equity: float,
    threshold: float,
    now: datetime,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "scope": scope,
        "account_id": account_id,
        "equity": equity,
        "day_start_equity": day_start_equity,
        "peak_equity": peak_equity,
        "threshold_fraction": threshold,
        "daily_loss_fraction": (
            (day_start_equity - equity) / day_start_equity
            if day_start_equity > 0
            else None
        ),
        "drawdown_fraction": (
            (peak_equity - equity) / peak_equity if peak_equity > 0 else None
        ),
        "observed_at": now.isoformat(),
    }


def _latch_and_alert(
    *,
    scope: str,
    kind: str,
    account_id: str,
    record: LiveLossBreakerRecord,
    limits: LossBreakerLimits,
    now: datetime,
    deployment_id: str | None = None,
) -> None:
    """Latch ``kind`` on ``scope`` and alert once per episode on transition."""
    equity = float(record.last_equity or 0.0)
    threshold = (
        limits.daily_loss_fraction if kind == "daily" else limits.max_drawdown_fraction
    )
    detail = _breach_detail(
        kind=kind,
        scope=scope,
        account_id=account_id,
        equity=equity,
        day_start_equity=record.day_start_equity,
        peak_equity=record.peak_equity,
        threshold=threshold,
        now=now,
    )
    try:
        transitioned = latch_live_loss_breaker_breach(scope, kind, detail, now)
    except Exception:
        # The latch write failed; the in-memory evaluation below still refuses
        # this entry, and the next observation retries the latch.
        logger.exception(
            "Could not persist loss breaker breach latch",
            extra={"data": {"scope": scope, "kind": kind}},
        )
        transitioned = True
    if transitioned:
        logger.error(
            "Loss circuit breaker tripped: %s scope %s (%s) - new entries are "
            "blocked, exits stay allowed; reset with "
            "`python -m scripts.live_loss_breaker reset-drawdown` for the "
            "drawdown breaker (the daily breaker resets at the UTC day boundary)",
            kind,
            scope,
            account_id,
            extra={
                "data": {
                    "alert": "loss_breaker_breach",
                    "scope": scope,
                    "kind": kind,
                    "account_id": account_id,
                    "deployment_id": deployment_id,
                    "equity": equity,
                    "day_start_equity": record.day_start_equity,
                    "peak_equity": record.peak_equity,
                    "threshold_fraction": threshold,
                    "daily_loss_fraction": detail["daily_loss_fraction"],
                    "drawdown_fraction": detail["drawdown_fraction"],
                }
            },
        )


def _observe_scope(
    *,
    scope: str,
    account_id: str,
    equity: float,
    now: datetime,
    seed_record: LiveLossBreakerRecord | None = None,
) -> LiveLossBreakerRecord:
    """One equity observation for ``scope``; deployment scopes seed from the account row."""
    seed: dict[str, float] = {}
    if seed_record is not None:
        seed = {
            "seed_day_start_equity": seed_record.day_start_equity,
            "seed_peak_equity": seed_record.peak_equity,
        }
    return observe_live_loss_breaker(scope, account_id, equity, now, **seed)


def _evaluate_kinds(
    *,
    scope: str,
    record: LiveLossBreakerRecord,
    account_id: str,
    limits: LossBreakerLimits,
    now: datetime,
    deployment_id: str | None = None,
) -> list[str]:
    """The breached kinds on ``record``, latching and alerting each.

    Each kind latches independently: an equity can be both 5 % under the
    day-start baseline AND 20 % under the peak, and latching only one would
    silently clear the other at the UTC rollover. A latched flag refuses even
    when the current equity recovered: the daily latch holds until the UTC
    rollover, the drawdown latch until the operator reset - that is what
    "reset rules are explicit" means.
    """
    if record.day_start_equity <= 0 or record.peak_equity <= 0:
        # A non-positive baseline cannot be a real equity; treating it as a
        # breach would be wrong, treating it as readable would be worse.
        raise LossBreakerBreachError(
            f"Loss breaker state for {scope} holds a non-positive equity "
            f"baseline (day_start={record.day_start_equity}, "
            f"peak={record.peak_equity}); refusing entries until it is reset.",
            scope=scope,
            account_id=account_id,
            deployment_id=deployment_id,
            state_unreadable=True,
        )
    current_equity = float(record.last_equity or 0.0)
    daily_loss = (record.day_start_equity - current_equity) / record.day_start_equity
    drawdown = (record.peak_equity - current_equity) / record.peak_equity
    kinds: list[str] = []
    if record.daily_breached or daily_loss >= limits.daily_loss_fraction:
        kinds.append("daily")
    if record.drawdown_breached or drawdown >= limits.max_drawdown_fraction:
        kinds.append("drawdown")
    for kind in kinds:
        _latch_and_alert(
            scope=scope,
            kind=kind,
            account_id=account_id,
            record=record,
            limits=limits,
            now=now,
            deployment_id=deployment_id,
        )
    return kinds


def _gate_scope(
    *,
    scope: str,
    account_id: str,
    equity: float,
    now: datetime,
    limits: LossBreakerLimits,
    deployment_id: str | None = None,
    symbol: str | None = None,
    seed_record: LiveLossBreakerRecord | None = None,
) -> LiveLossBreakerRecord:
    """Observe ``scope`` and refuse the entry when its breaker is breached.

    Observes (the equity series must advance for a scope that is checked, or a
    later re-enable would start blind); evaluates only when ``limits`` enables
    the breaker. The caller decides which scopes reach this function - both
    disabled scopes return before any observation. Returns the observed record
    so a deployment scope can seed its baseline from the account scope's.
    """
    try:
        record = _observe_scope(
            scope=scope,
            account_id=account_id,
            equity=equity,
            now=now,
            seed_record=seed_record,
        )
        kinds = (
            _evaluate_kinds(
                scope=scope,
                record=record,
                account_id=account_id,
                limits=limits,
                now=now,
                deployment_id=deployment_id,
            )
            if limits.enabled
            else []
        )
    except LossBreakerBreachError:
        raise
    except Exception as exc:
        # Fail closed: a breaker state that cannot be read is never "all
        # clear". The cheap failure is refusing entries; exits never reach
        # this function.
        logger.error(
            "Loss breaker state is unreadable; refusing entries until it can be read",
            extra={
                "data": {
                    "alert": "loss_breaker_state_unreadable",
                    "scope": scope,
                    "account_id": account_id,
                    "deployment_id": deployment_id,
                    "error": repr(exc),
                }
            },
        )
        raise LossBreakerBreachError(
            f"Refusing a live entry: the loss breaker state for {scope} could "
            f"not be read ({exc!r}); an unreadable state is never 'all clear'.",
            scope=scope,
            account_id=account_id,
            deployment_id=deployment_id,
            state_unreadable=True,
        ) from exc
    if kinds:
        current_equity = float(record.last_equity or equity)
        raise LossBreakerBreachError(
            f"Refusing a live entry: the {'+'.join(kinds)} loss breaker tripped on "
            f"{scope} "
            f"(account '{account_id}'"
            + (f", deployment {deployment_id}, symbol {symbol}" if deployment_id else "")
            + f"): equity {current_equity} vs day-start "
            f"{record.day_start_equity} / peak {record.peak_equity}.",
            scope=scope,
            kind="+".join(kinds),
            account_id=account_id,
            deployment_id=deployment_id,
        )
    return record


async def account_deployment_trading_pnls(
    adapter: Any,
    account_id: str,
) -> dict[str, float] | None:
    """Trading P&L per deployment on ``account_id``, in account currency.

    Realized plus unrealized P&L reconstructed from the persisted fill ledger
    (:func:`shared.database.get_account_deployment_trading_flows`) and marked
    to market with the adapter's current quotes and pip values. For each
    deployment and symbol::

        pnl = pip_value * (cash + net_qty * mark_price) - commission

    where ``cash`` is net cash paid (negative) or received (positive) over all
    fills, ``net_qty`` the signed open quantity and ``mark_price`` the
    conservative side of the book (bid for a long, ask for a short). Deployments
    without fills are absent - their P&L is 0.

    Returns ``None`` when ANY deployment's P&L cannot be measured exactly - an
    untrusted fill price, a missing or unusable quote or pip value, or an
    unreadable ledger. Callers treat ``None`` as "deployment scopes cannot be
    evaluated" and fail closed: an unmeasurable P&L is never "no loss".
    """
    try:
        flows = await asyncio.to_thread(get_account_deployment_trading_flows, account_id)
    except Exception:
        logger.exception(
            "Deployment trading flows are unreadable; deployment loss breaker "
            "scopes cannot be measured",
            extra={"data": {"account_id": account_id}},
        )
        return None
    if not flows:
        return {}
    symbols = sorted({symbol for dep in flows.values() for symbol in dep})
    pip_values: dict[str, float] = {}
    marks: dict[str, tuple[float, float]] = {}
    for symbol in symbols:
        try:
            pip_value = await adapter.get_pip_value(symbol)
            value = float(pip_value.value)
        except Exception as exc:
            logger.warning(
                "Deployment P&L is unmeasurable: no pip value for %s (%r)",
                symbol,
                exc,
                extra={"data": {"account_id": account_id, "symbol": symbol}},
            )
            return None
        if not math.isfinite(value) or value <= 0:
            logger.warning(
                "Deployment P&L is unmeasurable: pip value for %s is %r",
                symbol,
                value,
                extra={"data": {"account_id": account_id, "symbol": symbol}},
            )
            return None
        pip_values[symbol] = value
    open_symbols = {
        symbol
        for dep in flows.values()
        for symbol, flow in dep.items()
        if flow["net_qty"] != 0.0
    }
    for symbol in symbols:
        if symbol not in open_symbols:
            continue
        try:
            quote = await adapter.get_quote(symbol)
            bid, ask = validate_live_quote_prices(
                quote, symbol=symbol, label="Loss breaker deployment P&L mark"
            )
        except Exception as exc:
            logger.warning(
                "Deployment P&L is unmeasurable: no usable quote for %s (%r)",
                symbol,
                exc,
                extra={"data": {"account_id": account_id, "symbol": symbol}},
            )
            return None
        marks[symbol] = (bid, ask)
    pnls: dict[str, float] = {}
    for deployment_id, dep_flows in flows.items():
        total = 0.0
        for symbol, flow in dep_flows.items():
            if flow["unpriced_fills"]:
                logger.warning(
                    "Deployment P&L is unmeasurable: %d untrusted fill price(s) "
                    "for %s on deployment %s",
                    flow["unpriced_fills"],
                    symbol,
                    deployment_id,
                    extra={
                        "data": {
                            "account_id": account_id,
                            "symbol": symbol,
                            "deployment_id": deployment_id,
                        }
                    },
                )
                return None
            mark = 0.0
            if flow["net_qty"] != 0.0:
                bid, ask = marks[symbol]
                mark = bid if flow["net_qty"] > 0 else ask
            total += (
                pip_values[symbol]
                * (flow["cash"] + flow["net_qty"] * mark)
                - flow["commission"]
            )
        pnls[deployment_id] = total
    return pnls


def refuse_entry_if_loss_breaker_tripped(
    deployment: LiveDeploymentConfig,
    *,
    equity: float,
    deployment_equity: float | None = None,
    now: datetime | None = None,
    env_values: Mapping[str, str] | None = None,
    symbol: str | None = None,
) -> None:
    """The entry gate: refuse when the account or deployment breaker is tripped.

    Called from ``prepare_order_with_risk`` after the exit branch, so exits are
    never blocked. ``equity`` is the BROKER account equity the caller resolved
    for the breaker - never a caller-supplied sizing balance, which would
    poison the persisted equity series. ``deployment_equity`` is the
    deployment scope's synthetic equity (account equity minus the other
    deployments' P&L, see :func:`account_deployment_trading_pnls`); ``None``
    means it could not be measured, which fails closed when the deployment
    breaker is enabled and skips the scope when it is disabled.
    """
    observed_at = now or datetime.now(timezone.utc)
    equity_value = float(equity)
    if not math.isfinite(equity_value) or equity_value <= 0:
        # Unreadable equity fails closed: there is no balance to measure a
        # loss against, so the breaker cannot prove it is safe to add risk.
        raise LossBreakerBreachError(
            f"Refusing a live entry: account equity {equity!r} for "
            f"'{deployment.account_id}' is not a usable positive finite number; "
            "the loss breaker cannot measure a loss without it.",
            scope=account_loss_breaker_scope(deployment.account_id),
            account_id=deployment.account_id,
            deployment_id=str(deployment.id),
            state_unreadable=True,
        )
    account_limits = account_loss_breaker_limits(
        deployment.account_id, env_values=env_values
    )
    deployment_limits = deployment_loss_breaker_limits(
        deployment, env_values=env_values
    )
    if not (account_limits.enabled or deployment_limits.enabled):
        return
    account_id = deployment.account_id
    account_scope = account_loss_breaker_scope(account_id)
    deployment_scope = deployment_loss_breaker_scope(str(deployment.id))
    # The account scope is observed first even when its own breaker is
    # disabled: its record is the baseline a deployment scope seeds from.
    account_record = _gate_scope(
        scope=account_scope,
        account_id=account_id,
        equity=equity_value,
        now=observed_at,
        limits=account_limits,
        deployment_id=str(deployment.id),
        symbol=symbol,
    )
    if deployment_equity is None:
        if deployment_limits.enabled:
            # The deployment scope measures the deployment's own P&L; without
            # it there is nothing to measure against, and "cannot measure" is
            # never "no loss".
            raise LossBreakerBreachError(
                f"Refusing a live entry: the deployment loss breaker for "
                f"{deployment_scope} cannot measure the deployment's own "
                "trading P&L (unreadable ledger, quote or pip value); an "
                "unmeasurable deployment scope is never 'all clear'.",
                scope=deployment_scope,
                account_id=account_id,
                deployment_id=str(deployment.id),
                state_unreadable=True,
            )
        return
    _gate_scope(
        scope=deployment_scope,
        account_id=account_id,
        equity=float(deployment_equity),
        now=observed_at,
        limits=deployment_limits,
        deployment_id=str(deployment.id),
        symbol=symbol,
        seed_record=account_record,
    )


def observe_account_loss_breaker_equity(
    account_id: str,
    equity: float,
    *,
    now: datetime | None = None,
    deployments: Iterable[LiveDeploymentConfig] = (),
    deployment_pnls: Mapping[str, float] | None = None,
    env_values: Mapping[str, str] | None = None,
) -> None:
    """Balance-poll hook: advance every breaker scope on ``account_id``.

    Called once per balance snapshot with the equity just read from the broker.
    Observing (not just checking) is what makes the breakers trip between
    entries: the latch and the alert fire on the poll, and the next entry is
    refused. A failure here is logged, never raised - the entry gate re-checks
    everything itself, so a missed observation cannot fail open.

    ``deployment_pnls`` maps deployment id to its trading P&L
    (:func:`account_deployment_trading_pnls`); each deployment scope observes
    ``equity - sum(all deployment P&L) + own P&L`` - the account equity with
    every OTHER deployment's P&L subtracted out, so the scope moves only with
    its own deployment's P&L. ``None`` means the P&L could not be measured:
    deployment scopes are skipped this round (the entry gate still refuses
    entries it cannot measure) while the account scope is always observed.
    """
    observed_at = now or datetime.now(timezone.utc)
    deployment_list = list(deployments)
    try:
        equity_value = float(equity)
    except (TypeError, ValueError, OverflowError):
        equity_value = math.nan
    if not math.isfinite(equity_value) or equity_value <= 0:
        logger.error(
            "Loss breaker observation skipped: account equity is not a usable "
            "positive finite number",
            extra={
                "data": {
                    "alert": "loss_breaker_equity_unreadable",
                    "account_id": account_id,
                    "equity": repr(equity),
                }
            },
        )
        return
    account_scope = account_loss_breaker_scope(account_id)
    try:
        account_limits = account_loss_breaker_limits(
            account_id, env_values=env_values
        )
        # The account scope is always observed: deployment scopes seed their
        # baseline from its record even when the account breaker itself is
        # disabled. A breach latches and alerts inside _evaluate_kinds; the
        # poll itself never refuses anything - the entry gate does that.
        account_record = _observe_scope(
            scope=account_scope,
            account_id=account_id,
            equity=equity_value,
            now=observed_at,
        )
        scopes: list[tuple[str, LiveLossBreakerRecord, LossBreakerLimits, str | None]] = [
            (account_scope, account_record, account_limits, None)
        ]
        total_pnl = (
            sum(float(pnl) for pnl in deployment_pnls.values())
            if deployment_pnls is not None
            else None
        )
        if total_pnl is None and any(
            str(d.account_id) == str(account_id) for d in deployment_list
        ):
            logger.warning(
                "Deployment loss breaker scopes skipped this poll: deployment "
                "P&L is unmeasurable; the entry gate still fails closed",
                extra={"data": {"account_id": account_id}},
            )
        for deployment in deployment_list:
            if str(deployment.account_id) != str(account_id):
                continue
            if total_pnl is None:
                # Deployment P&L is unmeasurable this round: skip the scope
                # rather than observe it on account equity it does not own.
                # The entry gate refuses entries it cannot measure.
                continue
            own_pnl = float(deployment_pnls.get(str(deployment.id), 0.0))  # type: ignore[union-attr]
            scope_equity = equity_value - total_pnl + own_pnl
            limits = deployment_loss_breaker_limits(deployment, env_values=env_values)
            dep_record = _observe_scope(
                scope=deployment_loss_breaker_scope(str(deployment.id)),
                account_id=account_id,
                equity=scope_equity,
                now=observed_at,
                seed_record=account_record,
            )
            scopes.append(
                (
                    deployment_loss_breaker_scope(str(deployment.id)),
                    dep_record,
                    limits,
                    str(deployment.id),
                )
            )
        for scope, record, limits, deployment_id in scopes:
            if not limits.enabled:
                continue
            try:
                _evaluate_kinds(
                    scope=scope,
                    record=record,
                    account_id=account_id,
                    limits=limits,
                    now=observed_at,
                    deployment_id=deployment_id,
                )
            except LossBreakerBreachError as exc:
                # A non-positive stored baseline: the entry gate refuses on it;
                # the poll reports and moves on to the next scope.
                logger.error(
                    "Loss breaker state holds a non-positive equity baseline",
                    extra={
                        "data": {
                            "account_id": account_id,
                            "scope": scope,
                            "error": str(exc),
                        }
                    },
                )
    except Exception:
        logger.exception(
            "Loss breaker observation failed; entries still fail closed at the gate",
            extra={"data": {"account_id": account_id, "scope": account_scope}},
        )


def reset_drawdown_breaker(
    scope: str,
    *,
    equity: float,
    now: datetime | None = None,
) -> LiveLossBreakerRecord | None:
    """Operator reset of the drawdown breaker for ``scope`` (CLI-facing).

    Thin wrapper over ``shared.database.reset_live_loss_breaker_drawdown`` so
    the CLI and tests share one entry point. Returns the new state, or None
    when the scope has no row yet.
    """
    return reset_live_loss_breaker_drawdown(
        scope, equity, now or datetime.now(timezone.utc)
    )

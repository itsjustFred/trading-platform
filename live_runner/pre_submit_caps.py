"""Last-line hard caps enforced immediately before a broker submit (#302).

Sizing bugs have shipped before (cTrader ~1000x too large, #146; a 100x-off
fallback scale, #184). Every gate upstream of this point is part of, or fed by,
the sizing path; this module is the independent check that runs after sizing,
after the submission claim, and immediately before ``adapter.submit_order``.
It is wired into the daemon's two submit chokepoints (``submit_order_with_risk``
and ``_prepare_claim_and_submit_signal``); direct submits such as
``scripts/live_smoke.py`` and ``live_runner/test_signal_trigger.py`` bypass it.

Only the broker facts are re-derived here — the live quote (with its freshness
checked against ``max_entry_quote_age_seconds``), the pip value and the
balance. The quantity and the price levels are the ones sizing resolved and
are taken from the sized signal, never re-invented from payload metadata.

It refuses — fail closed, with an ERROR alert and an audit record — when

* the quantity exceeds ``pre_submit_max_qty`` (when configured),
* the order notional exceeds ``pre_submit_max_notional_equity_fraction`` of
  account equity,
* the resolved entry price deviates from the live broker quote by more than
  ``pre_submit_max_price_deviation_fraction``, or
* the risk at the stop exceeds ``pre_submit_max_risk_multiple`` times the
  deployment's configured per-trade risk (``max_risk_per_trade`` of equity).

Any failure to verify (no quote, a stale or missing quote time, no pip value,
unreadable balance, missing stop, invalid cap config) refuses the order.

Whether an order is an exit is decided by the daemon's own classification
(:func:`live_runner.exit_signals.is_exit_order`), never by a payload marker:
an entry that claims ``risk_sizing_mode == "exit_only"`` is still capped, and
the entry path strips the marker before it can reach the adapter. Exit orders
are never checked: they reduce exposure, and a cap that could block a flatten
would be a safety bug, not a safety feature.

Caps are configured per deployment in ``settings`` (a deployment binds one
account, so this is the per-account scope) with optional per-symbol overrides
in ``pre_submit_symbol_caps``. The defaults below are conservative backstops,
not trading policy; the tuned values are an open maintainer decision.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, NoReturn

from live_runner.base_adapter import (
    BaseLiveAdapter,
    LiveQuote,
    coerce_positive_live_qty,
    live_quote_time_rule,
    validate_live_quote_prices,
)
from live_runner.exit_signals import is_exit_order
from risk.live import entry_gate_settings, resolve_live_levels
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig, LiveSignal

if TYPE_CHECKING:
    from risk.config import RiskConfig

logger = get_logger("live-runner")

#: Conservative defaults (#302). These are backstops against sizing bugs, not
#: trading policy: they sit well above what a correctly sized order can reach
#: (the sizing notional cap defaults to 0.20 of equity) so only a broken size
#: trips them. The tuned values are an open maintainer decision.
DEFAULT_PRE_SUBMIT_MAX_NOTIONAL_EQUITY_FRACTION = 1.0
DEFAULT_PRE_SUBMIT_MAX_PRICE_DEVIATION_FRACTION = 0.02
DEFAULT_PRE_SUBMIT_MAX_RISK_MULTIPLE = 2.0

#: No universal absolute quantity default exists (1 share, 1 lot and 0.01 lot
#: are different instruments), so the qty cap is unset unless configured; the
#: notional cap is the instrument-agnostic backstop.
DEFAULT_PRE_SUBMIT_MAX_QTY: float | None = None

#: Upper bound for the three broker reads the check needs. Above the adapters'
#: own limits (cTrader quote 5 s + subscribe 10 s, pip-value hops), so it only
#: cuts off a call that has no bound of its own. A timeout refuses the order.
PRE_SUBMIT_CAPS_FETCH_TIMEOUT_SECONDS = 30.0

_MAX_QTY_KEY = "pre_submit_max_qty"
_NOTIONAL_KEY = "pre_submit_max_notional_equity_fraction"
_DEVIATION_KEY = "pre_submit_max_price_deviation_fraction"
_RISK_MULTIPLE_KEY = "pre_submit_max_risk_multiple"
_SYMBOL_CAPS_KEY = "pre_submit_symbol_caps"
_CAP_KEYS = (_MAX_QTY_KEY, _NOTIONAL_KEY, _DEVIATION_KEY, _RISK_MULTIPLE_KEY)


class PreSubmitCapViolation(ValueError):
    """An order refused by the pre-submit hard caps (#302).

    A ``ValueError`` so every existing refusal path treats it as a clean
    pre-request failure; callers wrap the check in ``order_not_submitted`` so
    the submission lease is released rather than held for the TTL.
    """


@dataclass(frozen=True)
class PreSubmitCapSettings:
    """Validated cap settings for one deployment (and optionally one symbol)."""

    max_qty: float | None
    max_notional_equity_fraction: float
    max_price_deviation_fraction: float
    max_risk_multiple: float


def _cap_number(raw: object, *, key: str) -> float:
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{key} must be a number, got {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite, got {raw!r}")
    return value


def _parse_cap_overrides(
    raw: object, *, context: str, strict_keys: bool = True
) -> dict[str, float | None]:
    """The four cap keys present in one settings (sub)mapping.

    ``strict_keys`` rejects unknown keys: a per-symbol override map holds
    only cap keys, so a typo there must not be silently ignored. The
    deployment-wide settings mapping holds every other policy key too, so
    there only the cap keys are read.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{context} must be a mapping, got {type(raw).__name__}")
    if strict_keys:
        unknown = sorted(set(raw) - set(_CAP_KEYS))
        if unknown:
            raise ValueError(f"{context} has unknown cap keys: {unknown}")
    else:
        # Deployment settings hold every policy key, so only cap-prefixed
        # names are policed: a typo like pre_submit_max_qtyy must not sit
        # silently unenforced.
        unknown = sorted(
            key
            for key in raw
            if str(key).startswith("pre_submit") and key not in _CAP_KEYS + (_SYMBOL_CAPS_KEY,)
        )
        if unknown:
            raise ValueError(f"{context} has unknown cap keys: {unknown}")
    overrides: dict[str, float | None] = {}
    for key in _CAP_KEYS:
        if key not in raw or raw[key] is None:
            continue
        overrides[key] = _cap_number(raw[key], key=key)
    return overrides


def _require_positive_caps(overrides: Mapping[str, float | None], *, context: str) -> None:
    """The same ``> 0`` rule :func:`pre_submit_cap_settings` enforces at submit.

    Run at creation too: a nonpositive cap stored in settings would otherwise
    sit there until the first order of the deployment is refused by it.
    """
    for key, value in overrides.items():
        if value is not None and value <= 0:
            raise ValueError(f"{context}: {key} must be > 0, got {value!r}")


def validate_pre_submit_cap_settings(settings: Mapping[str, Any] | None) -> None:
    """Validate a deployment's pre-submit cap settings at creation time.

    Mirrors ``entry_gate_settings`` (#125): a malformed cap must be refused
    when the deployment is written, not discovered at the first order. The
    full resolution runs here — type, finiteness and positivity — for the
    deployment-wide keys and every per-symbol override.
    """
    values = settings or {}
    base = _parse_cap_overrides(values, context="pre-submit cap settings", strict_keys=False)
    _require_positive_caps(base, context="pre-submit cap settings")
    symbol_caps = values.get(_SYMBOL_CAPS_KEY)
    if symbol_caps is None:
        return
    if not isinstance(symbol_caps, Mapping):
        raise ValueError(f"{_SYMBOL_CAPS_KEY} must be a mapping, got {type(symbol_caps).__name__}")
    seen_symbols: set[str] = set()
    for symbol, overrides in symbol_caps.items():
        canonical = str(symbol or "").strip().upper()
        if not canonical:
            raise ValueError(f"{_SYMBOL_CAPS_KEY} has an empty symbol key")
        if canonical in seen_symbols:
            raise ValueError(
                f"{_SYMBOL_CAPS_KEY} has duplicate symbol {symbol!r} "
                "(symbol keys match case-insensitively)"
            )
        seen_symbols.add(canonical)
        context = f"{_SYMBOL_CAPS_KEY}[{symbol!r}]"
        _require_positive_caps(_parse_cap_overrides(overrides, context=context), context=context)


def pre_submit_cap_settings(
    settings: Mapping[str, Any] | None, *, symbol: str
) -> PreSubmitCapSettings:
    """Resolve the effective caps for ``symbol`` under ``settings``.

    ``settings`` is the deployment's ``settings`` mapping (trusted config),
    never signal metadata. ``pre_submit_symbol_caps`` entries override the
    deployment-wide keys for that symbol only; symbol keys match case-
    insensitively on the upper-cased symbol.

    Raises ``ValueError`` naming the offending key on any invalid value —
    the caller fails closed.
    """
    values = settings or {}
    base = _parse_cap_overrides(values, context="pre-submit cap settings", strict_keys=False)

    overrides: dict[str, float | None] = {}
    symbol_caps = values.get(_SYMBOL_CAPS_KEY)
    if symbol_caps is not None:
        if not isinstance(symbol_caps, Mapping):
            raise ValueError(
                f"{_SYMBOL_CAPS_KEY} must be a mapping, got {type(symbol_caps).__name__}"
            )
        wanted = str(symbol or "").strip().upper()
        for raw_symbol, raw_overrides in symbol_caps.items():
            if str(raw_symbol or "").strip().upper() == wanted:
                overrides = _parse_cap_overrides(
                    raw_overrides, context=f"{_SYMBOL_CAPS_KEY}[{raw_symbol!r}]"
                )
                break

    def _resolve(key: str, default: float | None) -> float | None:
        value = overrides.get(key, base.get(key))
        if value is None:
            return default
        return value

    caps = PreSubmitCapSettings(
        max_qty=_resolve(_MAX_QTY_KEY, DEFAULT_PRE_SUBMIT_MAX_QTY),
        max_notional_equity_fraction=float(
            _resolve(_NOTIONAL_KEY, DEFAULT_PRE_SUBMIT_MAX_NOTIONAL_EQUITY_FRACTION)
        ),
        max_price_deviation_fraction=float(
            _resolve(_DEVIATION_KEY, DEFAULT_PRE_SUBMIT_MAX_PRICE_DEVIATION_FRACTION)
        ),
        max_risk_multiple=float(
            _resolve(_RISK_MULTIPLE_KEY, DEFAULT_PRE_SUBMIT_MAX_RISK_MULTIPLE)
        ),
    )
    for key, value in (
        (_MAX_QTY_KEY, caps.max_qty),
        (_NOTIONAL_KEY, caps.max_notional_equity_fraction),
        (_DEVIATION_KEY, caps.max_price_deviation_fraction),
        (_RISK_MULTIPLE_KEY, caps.max_risk_multiple),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"{key} must be > 0, got {value!r}")
    return caps


def _is_exit_order(signal: LiveSignal) -> bool:
    """The daemon's own exit classification, never a payload marker.

    ``is_exit_order`` is the same test ``prepare_order_with_risk`` dispatches
    sizing on, so "sized as an exit" and "exempt from the caps" cannot drift
    apart — and a payload ``risk_sizing_mode``/``exit_only`` on an entry
    cannot switch the caps off.
    """
    return is_exit_order(signal.action, signal.metadata)


async def _record_refusal_audit(data: Mapping[str, Any]) -> None:
    """Append the refusal to the live audit trail; never masks the refusal."""
    try:
        from shared.database import record_live_control_audit

        await asyncio.to_thread(
            record_live_control_audit,
            action="pre_submit_cap_refusal",
            target_type="live_deployment",
            target_id=str(data.get("deployment_id") or ""),
            actor="pre-submit-hard-caps",
            succeeded=False,
            detail=dict(data),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Could not write the pre-submit cap audit record; the refusal stands",
            extra={"data": {"error": repr(exc)}},
        )


async def enforce_pre_submit_caps(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    qty: float,
    risk_config: RiskConfig,
) -> None:
    """Refuse an order that breaks the hard caps; return when it may submit.

    Runs after sizing and the submission claim, immediately before the
    broker call. ``qty`` is the binding quantity the adapter will submit.
    ``risk_config`` is the deployment's :class:`RiskConfig` — the caller
    resolves it so this module never re-reads sizing policy.

    Raises :class:`PreSubmitCapViolation` (a ``ValueError``) on any refusal or
    any failure to verify. Exit orders return immediately.
    """
    if _is_exit_order(signal):
        return

    async def _fail(rule: str, reason: str, figures: Mapping[str, Any] | None = None) -> NoReturn:
        data: dict[str, Any] = {
            "alert": "pre_submit_cap_refusal",
            "rule": rule,
            "account_id": deployment.account_id,
            "deployment_id": str(deployment.id),
            "symbol": signal.symbol,
            "action": signal.action,
            "qty": qty,
            **dict(figures or {}),
        }
        logger.error(
            f"ALERT: pre-submit hard cap refused a live order ({rule}); nothing "
            "was sent to the broker",
            extra={"data": data},
        )
        await _record_refusal_audit(data)
        raise PreSubmitCapViolation(
            f"Refusing live {signal.action} order for {signal.symbol} "
            f"(deployment {deployment.id}): pre-submit hard cap '{rule}' — {reason}."
        )

    try:
        caps = pre_submit_cap_settings(deployment.settings, symbol=signal.symbol)
    except ValueError as exc:
        await _fail("invalid_cap_config", str(exc))
    try:
        max_quote_age = entry_gate_settings(deployment.settings).max_quote_age_seconds
    except ValueError as exc:
        await _fail("invalid_entry_gate_config", str(exc))

    try:
        qty_value = coerce_positive_live_qty(qty, field_name="pre-submit order qty")
    except ValueError as exc:
        await _fail("invalid_qty", str(exc))

    base_figures: dict[str, Any] = {
        "max_qty": caps.max_qty,
        "max_notional_equity_fraction": caps.max_notional_equity_fraction,
        "max_price_deviation_fraction": caps.max_price_deviation_fraction,
        "max_risk_multiple": caps.max_risk_multiple,
    }
    if caps.max_qty is not None and qty_value > caps.max_qty:
        await _fail(
            "qty_cap",
            f"qty {qty_value!r} exceeds the configured cap {caps.max_qty!r}",
            {**base_figures, "cap": caps.max_qty},
        )

    # Everything below needs broker truth. Any failure to obtain or validate
    # it refuses the order: an unverifiable cap is a failed cap.
    try:
        entry_price, stop_loss, _take_profit = resolve_live_levels(signal)
    except ValueError as exc:
        await _fail("unresolved_levels", str(exc))
    if stop_loss is None:
        await _fail(
            "missing_stop_loss",
            "the order carries no stop loss, so the risk at the stop cannot be "
            "verified",
            {**base_figures, "entry_price": entry_price},
        )

    quote_task = asyncio.create_task(adapter.get_quote(signal.symbol))
    pip_task = asyncio.create_task(adapter.get_pip_value(signal.symbol))
    balance_task = asyncio.create_task(adapter.get_balance())
    try:
        quote, pip_value, (_balance, equity) = await asyncio.wait_for(
            asyncio.gather(quote_task, pip_task, balance_task),
            timeout=PRE_SUBMIT_CAPS_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # gather propagates the first failure but leaves the sibling reads
        # running; cancel them so a hung broker call cannot outlive the
        # refusal (wait_for already cancelled them on a timeout).
        for task in (quote_task, pip_task, balance_task):
            task.cancel()
        await asyncio.gather(quote_task, pip_task, balance_task, return_exceptions=True)
        await _fail(
            "broker_facts_unavailable",
            "could not fetch the live quote, pip value and balance needed to "
            f"verify the caps ({type(exc).__name__}: {exc})",
        )

    try:
        bid, ask = validate_live_quote_prices(quote, symbol=signal.symbol)
        quote_price = ask if signal.action == "buy" else bid
        pip = float(pip_value.value)
        equity_value = float(equity)
    except (ValueError, TypeError, AttributeError) as exc:
        await _fail("broker_facts_unusable", str(exc))
    if not math.isfinite(pip) or pip <= 0:
        await _fail(
            "broker_facts_unusable", f"pip value is not positive and finite ({pip!r})"
        )
    if not math.isfinite(equity_value):
        await _fail(
            "broker_facts_unusable", f"account equity is not finite ({equity!r})"
        )

    figures: dict[str, Any] = {
        **base_figures,
        "qty": qty_value,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "quote_bid": bid,
        "quote_ask": ask,
        "quote_price": quote_price,
        "pip_value": pip,
        "account_equity": equity_value,
    }

    # Quote freshness, the same rule the entry-deviation gate applies (#125):
    # the caps compare against a live quote, so a stale cached spot — or one
    # with no usable broker time — must not pass.
    quote_time = quote.timestamp if isinstance(quote, LiveQuote) else None
    time_rule, quote_age = live_quote_time_rule(
        quote_time, now=datetime.now(timezone.utc), max_age_seconds=max_quote_age
    )
    if time_rule is not None:
        await _fail(
            "stale_quote",
            f"the broker quote is not current ({time_rule}): time {quote_time!r}, "
            f"age {quote_age!r}s, limit {max_quote_age}s",
            {
                **figures,
                "quote_time": (
                    quote_time.isoformat() if isinstance(quote_time, datetime) else None
                ),
                "quote_age_seconds": quote_age,
                "max_quote_age_seconds": max_quote_age,
            },
        )

    # Price deviation: the resolved entry against the side the order fills
    # against, as a fraction of the live quote. Absolute — a quote that ran
    # away in either direction means the sized levels no longer describe it.
    deviation = abs(entry_price - quote_price) / quote_price
    if deviation > caps.max_price_deviation_fraction:
        await _fail(
            "price_deviation",
            f"entry {entry_price!r} deviates from the live quote {quote_price!r} "
            f"by {deviation:.6f}, above the cap "
            f"{caps.max_price_deviation_fraction!r}",
            {**figures, "price_deviation_fraction": deviation},
        )

    # Notional in account currency: qty * price * pip_value (pip_value is the
    # account-currency P&L of one qty unit per 1.0 price move).
    notional = qty_value * quote_price * pip
    max_notional = caps.max_notional_equity_fraction * equity_value
    if notional > max_notional:
        await _fail(
            "notional_cap",
            f"order notional {notional!r} exceeds the cap {max_notional!r} "
            f"({caps.max_notional_equity_fraction!r} of equity {equity_value!r})",
            {**figures, "notional": notional, "max_notional": max_notional},
        )

    # Risk at the stop vs the deployment's configured per-trade risk.
    risk_at_stop = qty_value * abs(entry_price - stop_loss) * pip
    configured_risk = float(risk_config.max_risk_per_trade) * equity_value
    max_risk = caps.max_risk_multiple * configured_risk
    if risk_at_stop > max_risk:
        await _fail(
            "stop_risk",
            f"risk at the stop {risk_at_stop!r} exceeds "
            f"{caps.max_risk_multiple!r}x the configured risk {configured_risk!r}",
            {
                **figures,
                "risk_at_stop": risk_at_stop,
                "configured_risk": configured_risk,
                "max_risk": max_risk,
            },
        )

    logger.debug(
        "Pre-submit hard caps passed",
        extra={"data": {**figures, "deviation_fraction": deviation}},
    )

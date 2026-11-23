"""Last-line hard caps enforced immediately before a broker submit (#302).

Sizing bugs have shipped before (cTrader ~1000x too large, #146; a 100x-off
fallback scale, #184). Every gate upstream of this point is part of, or fed by,
the sizing path; this module is the independent check that runs after sizing,
after the submission claim, and immediately before ``adapter.submit_order``.
It is wired into the daemon's two submit chokepoints (``submit_order_with_risk``
and ``_prepare_claim_and_submit_signal``); the direct ``adapter.submit_order``
calls in ``scripts/live_smoke.py`` (:596 entry, :1481 close) and
``live_runner/test_signal_trigger.py`` bypass it, while live_smoke's own
``submit_order_with_risk`` calls (:702, :1130, :1570) do run the caps.

Only the broker facts are re-derived here — the live quote (with its freshness
checked against ``max_entry_quote_age_seconds``), the pip value and the
account equity. The quantity and the price levels are the ones sizing resolved and
are taken from the sized signal, never re-invented from payload metadata.

It refuses — fail closed, with an ERROR alert and an audit record — when

* the quantity exceeds ``pre_submit_max_qty`` (when configured),
* the order notional exceeds an explicitly configured
  ``pre_submit_max_notional_equity_fraction`` of account equity,
* the resolved entry price deviates from the live broker quote by more than
  ``pre_submit_max_price_deviation_fraction``, or
* the risk at the stop exceeds ``pre_submit_max_risk_multiple`` times the
  deployment's configured per-trade risk (``max_risk_per_trade`` of equity).

This module adds no notional ceiling below the broker's maximum leverage
for the account and asset (#240, maintainer decision on #329): an order
that would need more leverage than the broker allows is not refused — its
quantity is **clamped down to the broker maximum** (floored to the
instrument's quantity step) and the remaining caps run against the clamped
size. An undeterminable broker maximum fails closed to 1x. The clamp is a
backstop against a broken size, not the platform's notional policy —
sizing still caps order notional at ``max_notional_per_trade`` (default
0.20, ``RiskConfig`` refuses values > 1) until #240 decides otherwise.

The leverage ceiling is MT5-style **free margin** — the same rule the
native engine applies since #355 (#359, the live half of #347): free
margin is account equity minus the margin the broker already holds
against open positions (``adapter.get_used_margin`` — cTrader sums the
broker-reported ``ProtoOAPosition.usedMargin``, Alpaca reads
``account.initial_margin``), and the notional ceiling is
``free_margin * max_leverage``. Used margin that cannot be determined
fails closed like every other broker fact: an understated figure would
overstate free margin, so the order is refused rather than clamped
against a guessed ceiling. When free margin is exhausted the clamp
leaves less than one quantity step and the order is refused
(``broker_leverage_cap``) — live has no "skip the order" the way the
backtest engine does, so exhaustion surfaces as a refusal, not a silent
drop.

Any failure to verify (no quote, a stale or missing quote time, no pip value,
unreadable equity, missing stop, invalid cap config) refuses the order.

Whether an order is an exit is decided by the daemon's own classification
(:func:`live_runner.exit_signals.is_exit_order`), never by a payload marker:
an entry that claims ``risk_sizing_mode == "exit_only"`` is still capped, and
the entry path strips the marker before it can reach the adapter. Exit orders
are never checked: they reduce exposure, and a cap that could block a flatten
would be a safety bug, not a safety feature.

Caps are configured per deployment in ``settings`` (a deployment binds one
account, so this is the per-account scope) with optional per-symbol overrides
in ``pre_submit_symbol_caps``. ``pre_submit_max_notional_equity_fraction``
unset means the notional ceiling is the broker's maximum leverage (#240);
set explicitly it stays a hard refuse cap — an operator's deliberate tighter
limit, never a platform default below the broker maximum.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, NoReturn, overload

from live_runner.base_adapter import (
    BaseLiveAdapter,
    LiveQuote,
    coerce_positive_live_qty,
    live_quote_time_rule,
    validate_live_quote_prices,
)
from live_runner.exit_signals import is_exit_order, round_exit_qty_down
from risk.live import entry_gate_settings, resolve_live_levels
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig, LiveSignal

if TYPE_CHECKING:
    from risk.config import RiskConfig

logger = get_logger("live-runner")

#: Conservative defaults (#302), accepted by the maintainer on 2026-11-21:
#: risk multiple 2.0 and price deviation 2%, overridable per symbol. These
#: are backstops against sizing bugs, not trading policy; they sit well above
#: what a correctly sized order can reach so only a broken size trips them.
DEFAULT_PRE_SUBMIT_MAX_PRICE_DEVIATION_FRACTION = 0.02
DEFAULT_PRE_SUBMIT_MAX_RISK_MULTIPLE = 2.0

#: ``None``: no fixed equity-fraction default. The notional ceiling is the
#: broker's maximum leverage for the account and asset (#240), failing closed
#: to 1x when the broker maximum cannot be determined; exceeding it clamps
#: the quantity instead of refusing. An explicit configured fraction remains
#: a hard refuse cap.
DEFAULT_PRE_SUBMIT_MAX_NOTIONAL_EQUITY_FRACTION: float | None = None

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
    max_notional_equity_fraction: float | None
    max_price_deviation_fraction: float
    max_risk_multiple: float


def _cap_number(raw: Any, *, key: str) -> float:
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
            if (
                str(key).startswith("pre_submit")
                and key not in (*_CAP_KEYS, _SYMBOL_CAPS_KEY)
            )
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

    @overload
    def _resolve(key: str, default: float) -> float: ...
    @overload
    def _resolve(key: str, default: float | None) -> float | None: ...
    def _resolve(key: str, default: float | None) -> float | None:
        value = overrides.get(key, base.get(key))
        if value is None:
            return default
        return value

    notional_fraction = _resolve(
        _NOTIONAL_KEY, DEFAULT_PRE_SUBMIT_MAX_NOTIONAL_EQUITY_FRACTION
    )
    caps = PreSubmitCapSettings(
        max_qty=_resolve(_MAX_QTY_KEY, DEFAULT_PRE_SUBMIT_MAX_QTY),
        max_notional_equity_fraction=(
            None if notional_fraction is None else float(notional_fraction)
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


async def _record_cap_audit(
    data: Mapping[str, Any], *, action: str, succeeded: bool
) -> None:
    """Append a cap decision to the live audit trail; never masks it."""
    try:
        from shared.database import record_live_control_audit

        await asyncio.to_thread(
            record_live_control_audit,
            action=action,
            target_type="live_deployment",
            target_id=str(data.get("deployment_id") or ""),
            actor="pre-submit-hard-caps",
            succeeded=succeeded,
            detail=dict(data),
        )
    except Exception as exc:  # except-kind: loop-survival  # pragma: no cover - defensive
        logger.error(
            "Could not write the pre-submit cap audit record; the decision stands",
            extra={"data": {"error": repr(exc)}},
        )


async def _record_refusal_audit(data: Mapping[str, Any]) -> None:
    await _record_cap_audit(data, action="pre_submit_cap_refusal", succeeded=False)


async def _safe_max_leverage(adapter: BaseLiveAdapter, symbol: str) -> float | None:
    """The broker's maximum leverage, or ``None`` when it cannot be determined.

    A leverage lookup failure degrades to the 1x ceiling (#240) — it never
    refuses the order and never guesses a higher ceiling.
    """
    try:
        value = await adapter.get_max_leverage(symbol)
    except Exception as exc:  # except-kind: money-decision
        logger.warning(
            "Max leverage lookup raised; treating it as unknown (1x)",
            extra={"data": {"symbol": symbol, "error": repr(exc)}},
        )
        return None
    if value is None:
        return None
    try:
        leverage = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(leverage) or leverage <= 0:
        return None
    return leverage


async def _safe_used_margin(adapter: BaseLiveAdapter) -> float | None:
    """The broker's used margin, or ``None`` when it cannot be determined.

    Unlike the leverage lookup — where ``None`` degrades to a conservative
    1x ceiling — ``None`` here refuses the order: an understated used margin
    would overstate free margin and lift the leverage ceiling, the unsafe
    direction. A missing ``get_used_margin`` (an adapter that cannot report
    margin) lands here too and fails closed the same way.
    """
    try:
        method = getattr(adapter, "get_used_margin", None)
        if not callable(method):
            return None
        value = await method()
    except Exception as exc:  # except-kind: money-decision
        logger.debug(
            "Used-margin lookup raised; treating it as unknown (refuse)",
            extra={"data": {"error": repr(exc)}},
        )
        return None
    if value is None:
        return None
    try:
        margin = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(margin) or margin < 0:
        return None
    return margin


def _adapter_qty_step(adapter: BaseLiveAdapter, symbol: str) -> float | None:
    """The instrument's quantity step in qty units, when the adapter names one.

    Both real adapters expose it as ``exit_qty_step`` — the step is a property
    of the instrument, not of the order side. An adapter without one leaves
    its own submit-time conversion to floor the clamped quantity.
    """
    method = getattr(adapter, "exit_qty_step", None)
    if not callable(method):
        return None
    try:
        step = float(method(symbol))
    except (TypeError, ValueError, OverflowError):
        return None
    return step if math.isfinite(step) and step > 0 else None


async def _verify_broker_facts(
    *,
    adapter: BaseLiveAdapter,
    signal: LiveSignal,
    qty_value: float,
    entry_price: float,
    stop_loss: float,
    caps: PreSubmitCapSettings,
    max_quote_age: float,
    base_figures: Mapping[str, Any],
    fail: Callable[..., Awaitable[NoReturn]],
) -> dict[str, Any]:
    """Fetch and validate every broker fact the caps need; return ``figures``.

    Covers the bounded broker reads (quote, pip value, balance, leverage,
    used margin), their usability checks, quote freshness and the entry
    price-deviation rule. Every failure goes through ``fail`` — the caller's
    refusal path — so an unverifiable cap is a failed cap.
    """
    quote_task = asyncio.create_task(adapter.get_quote(signal.symbol))
    pip_task = asyncio.create_task(adapter.get_pip_value(signal.symbol))
    # Equity — not the balance tuple's equity slot — is what the caps judge:
    # on cTrader that slot is realized balance by design (#267, sizing stays
    # on it), while ``get_equity`` is the mark-to-market figure (#301) the
    # free-margin ceiling needs (#302/#359). On adapters whose balance tuple
    # already carries true equity the default ``get_equity`` returns the same
    # value.
    equity_task = asyncio.create_task(adapter.get_equity())
    leverage_task = asyncio.create_task(_safe_max_leverage(adapter, signal.symbol))
    margin_task = asyncio.create_task(_safe_used_margin(adapter))
    try:
        quote, pip_value, equity, max_leverage, used_margin = (
            await asyncio.wait_for(
                asyncio.gather(
                    quote_task, pip_task, equity_task, leverage_task, margin_task
                ),
                timeout=PRE_SUBMIT_CAPS_FETCH_TIMEOUT_SECONDS,
            )
        )
    except Exception as exc:  # except-kind: money-decision
        # gather propagates the first failure but leaves the sibling reads
        # running; cancel them so a hung broker call cannot outlive the
        # refusal (wait_for already cancelled them on a timeout). The
        # leverage and margin reads never raise — they degrade to None.
        for task in (
            quote_task,
            pip_task,
            equity_task,
            leverage_task,
            margin_task,
        ):
            task.cancel()
        await asyncio.gather(
            quote_task, pip_task, equity_task, leverage_task, margin_task,
            return_exceptions=True,
        )
        await fail(
            "broker_facts_unavailable",
            "could not fetch the live quote, pip value and equity needed to "
            f"verify the caps ({type(exc).__name__}: {exc})",
        )

    try:
        bid, ask = validate_live_quote_prices(quote, symbol=signal.symbol)
        quote_price = ask if signal.action == "buy" else bid
        pip = float(pip_value.value)
        equity_value = float(equity)
    except (ValueError, TypeError, AttributeError) as exc:
        await fail("broker_facts_unusable", str(exc))
    if not math.isfinite(pip) or pip <= 0:
        await fail(
            "broker_facts_unusable", f"pip value is not positive and finite ({pip!r})"
        )
    if not math.isfinite(equity_value):
        await fail(
            "broker_facts_unusable", f"account equity is not finite ({equity!r})"
        )
    if used_margin is None:
        # Fail closed like every other broker fact (#359): an understated
        # used margin would overstate free margin and lift the leverage
        # ceiling — the unsafe direction.
        await fail(
            "broker_facts_unavailable",
            "the broker's used margin could not be determined, so the "
            "free-margin leverage ceiling cannot be verified",
        )

    # The broker's maximum leverage for the account and asset (#240); an
    # undeterminable maximum fails closed to 1x — never a guessed ceiling.
    broker_leverage = max_leverage if max_leverage is not None else 1.0
    # #359/#347: MT5-style free margin — equity minus the margin the broker
    # already holds against open positions. May be negative on an
    # over-margined account; the ceiling below then clamps to nothing and
    # the order is refused, matching the backtest rule.
    free_margin = equity_value - used_margin
    # Price deviation: the resolved entry against the side the order fills
    # against, as a fraction of the live quote. Absolute — a quote that ran
    # away in either direction means the sized levels no longer describe it.
    deviation = abs(entry_price - quote_price) / quote_price
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
        "used_margin": used_margin,
        "free_margin": free_margin,
        "broker_max_leverage": max_leverage,
        "effective_max_leverage": broker_leverage,
        "price_deviation_fraction": deviation,
    }

    # Quote freshness, the same rule the entry-deviation gate applies (#125):
    # the caps compare against a live quote, so a stale cached spot — or one
    # with no usable broker time — must not pass.
    quote_time = quote.timestamp if isinstance(quote, LiveQuote) else None
    time_rule, quote_age = live_quote_time_rule(
        quote_time, now=datetime.now(timezone.utc), max_age_seconds=max_quote_age
    )
    if time_rule is not None:
        await fail(
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

    if deviation > caps.max_price_deviation_fraction:
        await fail(
            "price_deviation",
            f"entry {entry_price!r} deviates from the live quote {quote_price!r} "
            f"by {deviation:.6f}, above the cap "
            f"{caps.max_price_deviation_fraction!r}",
            {**figures, "price_deviation_fraction": deviation},
        )
    return figures


async def enforce_pre_submit_caps(
    adapter: BaseLiveAdapter,
    deployment: LiveDeploymentConfig,
    signal: LiveSignal,
    *,
    qty: float,
    risk_config: RiskConfig,
) -> float:
    """Check the hard caps; return the quantity that may be submitted.

    Runs after sizing and the submission claim, immediately before the
    broker call. ``qty`` is the binding quantity the adapter will submit.
    ``risk_config`` is the deployment's :class:`RiskConfig` — the caller
    resolves it so this module never re-reads sizing policy.

    Returns ``qty`` unchanged, or a smaller quantity clamped to what free
    margin affords at the broker's maximum leverage (#240/#359: clamp,
    never refuse, when a correctly sized order would need more leverage
    than the broker allows). Raises
    :class:`PreSubmitCapViolation` (a ``ValueError``) on any refusal or any
    failure to verify. Exit orders return ``qty`` immediately.
    """
    if _is_exit_order(signal):
        return qty

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

    figures = await _verify_broker_facts(
        adapter=adapter,
        signal=signal,
        qty_value=qty_value,
        entry_price=entry_price,
        stop_loss=stop_loss,
        caps=caps,
        max_quote_age=max_quote_age,
        base_figures=base_figures,
        fail=_fail,
    )
    quote_price = float(figures["quote_price"])
    pip = float(figures["pip_value"])
    equity_value = float(figures["account_equity"])
    used_margin = figures["used_margin"]
    free_margin = float(figures["free_margin"])
    broker_leverage = float(figures["effective_max_leverage"])
    max_leverage = figures["broker_max_leverage"]
    deviation = float(figures["price_deviation_fraction"])

    # Notional in account currency: qty * price * pip_value (pip_value is the
    # account-currency P&L of one qty unit per 1.0 price move).
    notional = qty_value * quote_price * pip
    if caps.max_notional_equity_fraction is not None:
        # An explicitly configured fraction is a hard refuse cap — the
        # operator's deliberate limit, which may sit below the broker maximum.
        max_notional = caps.max_notional_equity_fraction * equity_value
        if notional > max_notional:
            await _fail(
                "notional_cap",
                f"order notional {notional!r} exceeds the cap {max_notional!r} "
                f"({caps.max_notional_equity_fraction!r} of equity {equity_value!r})",
                {**figures, "notional": notional, "max_notional": max_notional},
            )

    # The platform imposes no notional ceiling below the broker maximum
    # (#240): an order that would need more leverage than the broker allows
    # is clamped down to what FREE MARGIN affords — equity minus used
    # margin, times the broker maximum (#359, the live half of the #347
    # backtest rule) — floored to the instrument's quantity step, never
    # refused for leverage alone. The remaining caps then run against the
    # clamped size.
    leverage_ceiling = broker_leverage * free_margin
    if notional > leverage_ceiling:
        max_qty_leverage = leverage_ceiling / (quote_price * pip)
        step = _adapter_qty_step(adapter, signal.symbol)
        try:
            clamped_qty = (
                max_qty_leverage
                if step is None
                else round_exit_qty_down(max_qty_leverage, step)
            )
        except ValueError:
            clamped_qty = 0.0
        if not math.isfinite(clamped_qty) or clamped_qty <= 0:
            await _fail(
                "broker_leverage_cap",
                f"free margin {free_margin!r} at leverage {broker_leverage!r} "
                "allows less than one quantity step of this order; nothing "
                "compliant can be submitted",
                {
                    **figures,
                    "notional": notional,
                    "leverage_ceiling": leverage_ceiling,
                    "max_qty_at_broker_leverage": max_qty_leverage,
                    "qty_step": step,
                },
            )
        clamp_data: dict[str, Any] = {
            "alert": "pre_submit_leverage_clamp",
            "account_id": deployment.account_id,
            "deployment_id": str(deployment.id),
            "symbol": signal.symbol,
            "action": signal.action,
            "qty_before_clamp": qty_value,
            "qty_after_clamp": clamped_qty,
            "notional_before_clamp": notional,
            "broker_max_leverage": max_leverage,
            "effective_max_leverage": broker_leverage,
            "used_margin": used_margin,
            "free_margin": free_margin,
            "leverage_ceiling": leverage_ceiling,
            "qty_step": step,
            # The broker maximum is an upper bound (cTrader's tiered leverage
            # is highest at the smallest volume); the broker enforces the
            # lower tier that applies at the order's actual size.
            "broker_max_leverage_is_upper_bound": True,
        }
        logger.warning(
            "Pre-submit caps clamped the order qty to the broker's maximum "
            "leverage; the clamped size is what will be submitted",
            extra={"data": clamp_data},
        )
        await _record_cap_audit(
            clamp_data, action="pre_submit_leverage_clamp", succeeded=True
        )
        qty_value = clamped_qty
        notional = qty_value * quote_price * pip
        figures["qty"] = qty_value
        figures["qty_before_clamp"] = clamp_data["qty_before_clamp"]
        figures["leverage_clamped"] = True

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
    return qty_value

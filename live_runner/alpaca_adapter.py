from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import pandas as pd
from alpaca.trading.enums import OrderClass
from alpaca.trading.requests import ReplaceOrderRequest, StopLossRequest, TakeProfitRequest

from live_runner.alpaca_fees import (
    COMMISSION_BACKFILL_LOOKBACK,
    candidate_fee_days,
    day_start_utc,
    fee_day_is_posted,
    fetch_account_activities,
    plan_commission_backfill,
)
from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    EXIT_REPROTECTION_METADATA_KEY,
    BaseLiveAdapter,
    ControlOutcome,
    LivePipValue,
    LiveQuote,
    ProtectiveStop,
    coerce_positive_live_qty,
    is_unbounded_tp,
    order_not_submitted,
)
from live_runner.credential_env import parse_bool_env, resolve_env_value
from live_runner.exit_signals import ExitBelowStepError, round_exit_qty_down
from live_runner.symbol_class import canonical_crypto_symbol, classify_symbol
from shared.alpaca_symbols import classify_alpaca_symbol, crypto_pair_key
from shared.database import (
    BROKER_CLOSE_REASON_ALPACA_LEG,
    BROKER_CLOSE_REASON_ALPACA_STOP_LEG,
    BROKER_CLOSE_REASON_ALPACA_TARGET_LEG,
    apply_live_execution_commissions,
    broker_close_signal_id,
    clear_live_signal_metadata_key,
    find_latest_filled_live_entry_signals,
    flag_live_ledger_reconciliation,
    get_live_signal_broker_order_id,
    get_live_signal_filled_qty,
    get_live_signal_filled_qty_for_broker_order,
    get_live_signal_metadata,
    get_live_signal_reconciliation_cursor,
    list_live_executions_for_broker_orders,
    list_live_executions_missing_commission,
    list_live_signal_ids_for_reconciliation,
    list_pending_exit_reprotections,
    record_live_broker_side_close_fill,
    save_live_execution,
    save_live_execution_up_to_cumulative,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.fill_keys import canonical_utc_timestamp
from shared.logging import get_logger
from shared.models import (
    PRICE_ABSENT,
    PRICE_DERIVED,
    PRICE_OBSERVED,
    PRICE_SENT_BAD,
    LiveExecution,
    LiveSignal,
)

logger = get_logger("live-runner")

_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")

_SL_METADATA_KEYS = ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
_TP_METADATA_KEYS = ("take_profit", "tp_price")
#: Newest orders requested per lookup of held bracket stop legs (Alpaca's maximum is 500).
_ALPACA_HELD_LEG_LOOKUP_LIMIT = 500

#: Alpaca rejects sub-penny prices: two decimals at or above $1, four below
#: ("sub-penny increment does not fulfill minimum pricing criteria", HTTP 422).
_ALPACA_TICK_AT_OR_ABOVE_ONE = Decimal("0.01")
_ALPACA_TICK_BELOW_ONE = Decimal("0.0001")

_EXTENDED_HOURS_BRACKET_MESSAGE = (
    "Extended-hours entry with mandatory SL/TP is not supported: "
    "the broker rejects bracket orders with extended_hours enabled "
    "(signal_id={signal_id}, symbol={symbol})."
)


def _round_bracket_price_toward_entry(price: float, *, entry_price: float) -> float:
    """Round a bracket leg price onto Alpaca's tick, toward the entry.

    Toward the entry, because sizing divided the risk budget by the unrounded
    stop distance: a stop rounded away from the entry would lose more than the
    budget. The take profit uses the same direction so it is never placed
    beyond the level the signal asked for.
    """
    value = Decimal(repr(float(price)))
    tick = _ALPACA_TICK_AT_OR_ABOVE_ONE if value >= 1 else _ALPACA_TICK_BELOW_ONE
    rounding = ROUND_CEILING if float(price) < float(entry_price) else ROUND_FLOOR
    return float(value.quantize(tick, rounding=rounding))


def _round_bracket_levels_in_place(
    metadata: dict[str, Any], *, entry_price: float, signal: LiveSignal
) -> None:
    """Rewrite every SL/TP alias in *metadata* onto Alpaca's tick, toward the entry.

    A level on the wrong side of the entry is refused here, before persistence,
    mirroring the cTrader adapter's ``stop_outward``/``take_outward`` guard: a
    buy's take profit below the entry is an instantly marketable sell limit that
    would flatten the position at once, and sizing only checks the stop side.
    """
    if signal.action not in {"buy", "sell"}:
        raise ValueError(
            f"Unsupported action {signal.action!r} for a bracket entry "
            f"(signal_id={signal.id})."
        )
    buy = signal.action == "buy"
    for canonical_key, keys in (
        ("stop_loss", _SL_METADATA_KEYS),
        ("take_profit", _TP_METADATA_KEYS),
    ):
        level = _resolve_optional_float(metadata, keys)
        if level is None or not math.isfinite(level) or level <= 0:
            # Missing/invalid levels are refused by _resolve_broker_sl_tp.
            continue
        is_stop = canonical_key == "stop_loss"
        # Distance from the entry in the direction the leg must lie: below the
        # entry for a buy's stop and a sell's take profit, above otherwise.
        outward = (entry_price - level) if (buy == is_stop) else (level - entry_price)
        if outward <= 0:
            raise ValueError(
                f"{canonical_key} {level!r} for a {signal.action} of {signal.symbol} is "
                f"on the wrong side of the entry {entry_price!r}; refusing to submit "
                f"(signal_id={signal.id})."
            )
        rounded = _round_bracket_price_toward_entry(level, entry_price=entry_price)
        if (rounded - entry_price) * (level - entry_price) <= 0:
            raise ValueError(
                f"{canonical_key} {level!r} for {signal.symbol} is within one tick "
                f"of the entry {entry_price!r}; refusing to submit "
                f"(signal_id={signal.id})."
            )
        for key in keys:
            if key in metadata:
                metadata[key] = rounded
        metadata[canonical_key] = rounded


def _floor_to_whole_shares(qty: float) -> float:
    """Alpaca accepts fractional quantities only for simple orders.

    A bracket/OTO entry with a fractional qty is rejected ("fractional orders
    must have order_class=simple", HTTP 422). Floor, never round up: rounding
    up would exceed the risk-sized quantity.
    """
    return float(math.floor(float(qty) + 1e-9))


def _normalize_broker_order_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"none", "null", "false"}:
        return None
    try:
        uuid_value = UUID(normalized)
    except (AttributeError, TypeError, ValueError):
        return None
    if uuid_value.int == 0:
        return None
    return str(uuid_value)


def _control_order_id(value: Any) -> str | None:
    """An order id usable for a control call, without assuming a UUID shape.

    `_normalize_broker_order_id` is deliberately strict because it guards the
    submission path against junk. Control calls only need to name an order the
    broker just returned, and a fake client (or a broker that changes its id
    format) must not be reported as "unidentifiable" for that reason.
    """
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"none", "null", "false"}:
        return None
    return normalized


def _order_attr(order: Any, name: str, default: Any = None) -> Any:
    if isinstance(order, Mapping):
        return order.get(name, default)
    return getattr(order, name, default)


def _order_legs(order: Any) -> list[Any]:
    legs = _order_attr(order, "legs", None)
    return list(legs) if isinstance(legs, (list, tuple)) else []


def _enum_text(raw: Any) -> str:
    """Lower-case text of an alpaca-py enum or plain string, without any ``Enum.`` prefix."""
    text = str(getattr(raw, "value", raw) or "").strip().lower()
    return text.rsplit(".", 1)[-1]


def _order_status(order: Any) -> str:
    return _enum_text(_order_attr(order, "status", ""))


def _order_symbol(order: Any) -> str:
    return str(_order_attr(order, "symbol", "") or "").strip().upper()


def _order_side(order: Any) -> str:
    """The order's side as ``buy``/``sell``, whether it arrives as an enum or a string."""
    return _enum_text(_order_attr(order, "side", ""))


def _order_type(order: Any) -> str:
    """The order's type (``stop``, ``limit``, ...), from an enum or a string."""
    raw = _order_attr(order, "order_type", None)
    if raw is None or raw == "":
        raw = _order_attr(order, "type", "")
    text = str(getattr(raw, "value", raw) or "").strip().lower()
    return text.rsplit(".", 1)[-1]


def _order_class_text(order: Any) -> str:
    """The order's class (``bracket``, ``oco``, ...), from an enum or a string."""
    return _enum_text(_order_attr(order, "order_class", ""))


#: Statuses that mean the order can no longer work or reserve quantity. Anything
#: else - ``new``, ``accepted``, ``partially_filled``, ``held``, ``pending_*`` -
#: still counts as live for protection purposes (a ``pending_cancel`` order can
#: still fill in a cancel/fill race).
_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
)

#: Order classes whose parent is an *entry*, not protection: their legs belong
#: to a pending or just-filled entry and are handled through the filled-parent
#: path, never as standalone reducing orders.
_ENTRY_ORDER_CLASSES = frozenset({"bracket", "oto", "mleg"})


def _distinct_levels(levels: list[float | None]) -> list[float]:
    """The distinct readable prices in ``levels`` (1e-6 absolute tolerance)."""
    distinct: list[float] = []
    for level in levels:
        if level is None:
            continue
        if not any(
            math.isclose(level, kept, rel_tol=0.0, abs_tol=1e-6) for kept in distinct
        ):
            distinct.append(level)
    return distinct


def _single_level(levels: list[float | None]) -> float | None:
    """The one level shared by ``levels``, or ``None`` when absent or ambiguous."""
    distinct = _distinct_levels(levels)
    return distinct[0] if len(distinct) == 1 else None


def _exit_protection_kind(candidate: Mapping[str, Any]) -> str | None:
    """``stop``/``tp`` for a working reducing order, ``None`` when unrecognised."""
    order_type = str(candidate.get("order_type") or "")
    if "stop" in order_type:
        return "stop"
    if order_type == "limit":
        return "tp"
    return None


def _exit_protection_signature(
    candidates: list[dict[str, Any]],
) -> tuple[list[str], float | None, float | None, float | None]:
    """What an exit release depends on: cancel ids and the re-place levels.

    Compared again inside ``_order_book_lock`` so a stop trailing moved (or a
    leg replaced) between planning and release refuses the exit instead of
    cancelling an order the plan no longer describes.
    """
    stops = [c for c in candidates if _exit_protection_kind(c) == "stop"]
    take_profits = [c for c in candidates if _exit_protection_kind(c) == "tp"]
    return (
        sorted({cid for c in candidates for cid in c["cancel_ids"]}),
        _single_level([c["stop_price"] for c in stops]),
        _single_level([c["limit_price"] for c in stops if c["limit_price"] is not None]),
        _single_level([c["limit_price"] for c in take_profits]),
    )


async def _clear_exit_reprotection_marker(
    signal_id: str, symbol: str, reason: str
) -> None:
    """Best-effort ``exit_reprotection`` marker clear; failure only means replay re-verifies."""
    try:
        await asyncio.to_thread(
            clear_live_signal_metadata_key,
            signal_id,
            EXIT_REPROTECTION_METADATA_KEY,
        )
    except Exception as exc:
        logger.debug(
            "Alpaca exit re-protection marker for %s could not be cleared (%s): %r",
            symbol,
            reason,
            exc,
        )


def _is_definite_broker_rejection(exc: BaseException) -> bool:
    """Whether ``exc`` proves the broker answered and refused the order.

    alpaca-py raises ``APIError`` for an HTTP response; a timeout or connection
    error leaves the outcome unknown - the order may be working at the broker.
    """
    try:
        from alpaca.common.exceptions import APIError
    except ImportError:
        return False
    return isinstance(exc, APIError)


def _bracket_leg_key(value: Any) -> str | None:
    """A bracket leg's order id as a map key, with or without a UUID shape.

    Alpaca order ids are UUIDs and `_normalize_broker_order_id` proves that. The looser text
    form keeps the registration side (submit response, nested reconcile legs, a trailed
    stop's replacement id) from silently dropping the mapping for a broker - or a fake
    client - that answers with another id shape. The stream path still requires the strict
    UUID form before it consults the map, so a non-UUID leg id is learned but its fill is
    refused as unattributable rather than recorded - the one direction that cannot lose
    money.
    """
    normalized = _normalize_broker_order_id(value)
    if normalized is not None:
        return normalized
    text_value = str(value or "").strip()
    if not text_value or text_value.lower() in {"none", "null", "false"}:
        return None
    return text_value


#: Event label a bracket leg's fill is keyed by. One label for BOTH paths that record a leg fill
#: (the trade-update stream and the reconciliation backfill): the dedupe key must be derivable
#: identically from either, or the same leg fill could be stored twice.
_LEG_FILL_EVENT_LABEL = "leg"


#: Marker the re-placed exit protection's ``client_order_id`` carries (#350):
#: ``{entry_signal_id}-reprotect-{exit_signal_id}`` and its shorter forms.
#: Never a signal UUID, so a fill naming it is attributed through the embedded
#: entry id - verified against the signal row, never trusted blindly.
_REPROTECT_CLIENT_ORDER_MARKER = "-reprotect"


def _reprotect_entry_candidate(client_order_id: str) -> str | None:
    """The UUID a ``-reprotect`` client order id leads with, or ``None``.

    ``{entry}-reprotect-{exit}``, ``{entry}-reprotect`` and ``{exit}-reprotect``
    all lead with a UUID; whether it names the *entry* is for the caller to
    verify - the bare form is ambiguous by construction.
    """
    candidate = client_order_id.split(_REPROTECT_CLIENT_ORDER_MARKER, 1)[0].strip()
    try:
        UUID(candidate)
    except (AttributeError, TypeError, ValueError):
        return None
    return candidate


def _bracket_leg_close_reason(leg: Any) -> str:
    """Which kind of bracket leg this is, for the close signal's audit metadata."""
    order_type = _order_type(leg)
    if "stop" in order_type:
        return BROKER_CLOSE_REASON_ALPACA_STOP_LEG
    if order_type == "limit":
        return BROKER_CLOSE_REASON_ALPACA_TARGET_LEG
    return BROKER_CLOSE_REASON_ALPACA_LEG


def _stop_candidates(order: Any) -> Iterator[Any]:
    """Orders that may be a position's working stop: the order, and a filled parent's held legs.

    A leg of a parent that has not filled belongs to a pending entry and protects nothing
    that is open yet; amending it would leave the position unprotected while the log says
    the stop was trailed. A dead (cancelled/filled/expired) leg is not a protection either.
    The exception is a working OCO parent (#145): its legs *are* the position's
    exit protection (the pair an exit re-places after a partial close), so its
    live legs count like a filled bracket's held ones.
    """
    yield order
    status = _order_status(order)
    if status == "filled":
        for leg in _order_legs(order):
            if _order_status(leg) == "held":
                yield leg
        return
    if _order_class_text(order) == "oco":
        for leg in _order_legs(order):
            if _order_status(leg) not in _TERMINAL_ORDER_STATUSES:
                yield leg


def _protective_stop_of(
    candidate: Any, parent: Any, reducing_sides: Mapping[str, str]
) -> ProtectiveStop | None:
    symbol = _order_symbol(candidate) or _order_symbol(parent)
    if not symbol or reducing_sides.get(symbol) != _order_side(candidate):
        return None
    if _order_type(candidate) != "stop":
        return None
    order_id = _control_order_id(_order_attr(candidate, "id"))
    stop_price = _to_float(_order_attr(candidate, "stop_price", None), default=0.0)
    if order_id is None or not math.isfinite(stop_price) or stop_price <= 0:
        return None
    qty = _to_float(_order_attr(candidate, "qty", None), default=0.0)
    return ProtectiveStop(
        order_id=order_id,
        symbol=symbol,
        side=_order_side(candidate),
        stop_price=stop_price,
        qty=qty if math.isfinite(qty) and qty > 0 else None,
    )


def _alpaca_timeframe(value: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    normalized = str(value or "").strip().lower()
    units = {
        "1m": (1, TimeFrameUnit.Minute),
        "5m": (5, TimeFrameUnit.Minute),
        "15m": (15, TimeFrameUnit.Minute),
        "30m": (30, TimeFrameUnit.Minute),
        "1h": (1, TimeFrameUnit.Hour),
        "1d": (1, TimeFrameUnit.Day),
    }
    if normalized not in units:
        raise ValueError(f"Unsupported Alpaca market-data timeframe: {value!r}")
    amount, unit = units[normalized]
    return TimeFrame(amount=amount, unit=unit), normalized


# Markets are closed for most of the calendar. A US equity session is 6.5 hours
# on five days a week, so one calendar day buys ~27% of a trading day and a week
# buys ~5/7 of the sessions. Sizing an intraday lookback by the bare bar duration
# therefore under-requests by ~5.2x: 200 30-minute bars need ~6000 trading
# minutes, which is ~21 calendar days, not the 220 hours (9 days) a `*2.2`
# multiplier buys. The old value returned a short history, and the `tail()` then
# shipped it - silently changing every lookback-dependent indicator relative to
# the backtest. 6.5 covers the session fraction, weekends and a holiday week;
# the widening retry below covers sparse IEX data on top of it.
_CALENDAR_TO_TRADING_SLACK = 6.5
_DAILY_CALENDAR_TO_TRADING_SLACK = 1.6


def _alpaca_market_data_start(
    timeframe: str,
    limit: int,
    *,
    slack_multiplier: float = 1.0,
) -> datetime:
    normalized = str(timeframe or "").strip().lower()
    bars = max(int(limit), 1) * max(float(slack_multiplier), 1.0)
    if normalized.endswith("m"):
        # The bar duration is part of the request: `limit` bars of 30 minutes is
        # 6000 trading minutes, not 200. Omitting it was the other half of the
        # short-history bug, on top of the calendar/trading slack.
        try:
            bar_minutes = float(normalized[:-1])
        except ValueError:
            bar_minutes = 1.0
        return datetime.now(timezone.utc) - timedelta(
            minutes=bars * bar_minutes * _CALENDAR_TO_TRADING_SLACK
        )
    if normalized.endswith("h"):
        try:
            bar_hours = float(normalized[:-1])
        except ValueError:
            bar_hours = 1.0
        return datetime.now(timezone.utc) - timedelta(
            hours=bars * bar_hours * _CALENDAR_TO_TRADING_SLACK
        )
    return datetime.now(timezone.utc) - timedelta(
        days=bars * _DAILY_CALENDAR_TO_TRADING_SLACK
    )


def _parse_bool_env(name: str, value: str | None, *, default: bool = True) -> bool:
    """Strict boolean parsing shared with live_runner.credential_env.

    A typo'd value raises instead of silently selecting live/paper mode.
    """
    return parse_bool_env(name, value, default=default)


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_finite(value: Any) -> float | None:
    """``value`` as a finite, positive float, or ``None`` when it is missing or unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(Decimal(str(value).strip()))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _is_sent(raw: Any) -> bool:
    return raw is not None and not (isinstance(raw, str) and not raw.strip())


def _resolve_fill_price(
    event_prices: tuple[Any, ...],
    *,
    order_avg_price: Any,
    event_qty: Any,
    cumulative_qty: float,
) -> tuple[float, str]:
    """This fill's price and its ``actual_price_observed`` verdict.

    The event's own prices come first; the first finite positive one is ``observed``.

    The order's ``filled_avg_price`` is the average over EVERY fill of the order, so it is this
    fill's price only when this fill is the whole order: the event states its own quantity and that
    equals the order's cumulative quantity. Then it is used, labelled ``derived`` - never
    ``observed``. Otherwise it is not this fill's price at all (fills of 1@100 and 1@104 average to
    102, which is neither), and borrowing it would hand `_derive_remainder_price` a fake leg
    (review of #143).

    Without a usable price the stored price is ``0.0`` with ``sent_bad`` (a price was sent and is
    unusable) or ``absent`` (none was sent), as the cTrader adapter records it.
    """
    sent = False
    for raw in event_prices:
        if not _is_sent(raw):
            continue
        sent = True
        price = _positive_finite(raw)
        if price is not None:
            return price, PRICE_OBSERVED
    own_qty = _positive_finite(event_qty)
    single_fill = (
        own_qty is not None
        and cumulative_qty > 0.0
        and math.isclose(own_qty, cumulative_qty, rel_tol=1e-9, abs_tol=1e-12)
    )
    if single_fill and _is_sent(order_avg_price):
        sent = True
        average = _positive_finite(order_avg_price)
        if average is not None:
            return average, PRICE_DERIVED
    return 0.0, PRICE_SENT_BAD if sent else PRICE_ABSENT


def _known_commission(order: Any) -> float | None:
    """``0.0`` for an equity order (commission-free on Alpaca), ``None`` (unknown) otherwise.

    Alpaca reports no commission on the order or the trade update. Crypto fees are posted end of day
    as ``CFEE`` activities and priced later by `AlpacaLiveAdapter.backfill_commissions`; options and
    an unreadable asset class are unknown too. Maintainer decision on #124: ``0.0`` only for a known
    zero, NULL for everything else.
    """
    raw = _order_attr(order, "asset_class", None)
    asset_class = _enum_text(raw)
    return 0.0 if asset_class == "us_equity" else None


def _is_parseable_qty(value: Any) -> bool:
    """Whether ``value`` yields a finite float quantity."""
    try:
        return math.isfinite(float(Decimal(str(value))))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _position_qty(position: Mapping[str, Any]) -> float:
    """The broker position's signed quantity, refusing to guess on a bad value.

    ``_to_float(..., default=0.0)`` maps an *unparseable* ``qty`` onto "flat",
    which would let a position with a malformed quantity be silently skipped as
    "no position" — the flatten would then report success and a STOPPED
    deployment would be stamped enforced while real exposure stayed open.

    :func:`get_positions` marks such a row with ``qty_unparseable`` (the
    displayed ``qty`` is still the coerced ``0.0`` so ordinary consumers keep
    working). This helper is the risk-path reader: it raises on that marker, and
    on a non-finite quantity, rather than returning a misleading zero.
    """
    if position.get("qty_unparseable"):
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has an unparseable "
            f"qty ({position.get('qty_raw')!r}); refusing to treat it as flat"
        )
    if "qty" not in position:
        return 0.0
    value = position.get("qty")
    if value is None:
        return 0.0
    try:
        qty = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has an unparseable "
            f"qty ({value!r}); refusing to treat it as flat"
        ) from exc
    if not math.isfinite(qty):
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has a non-finite "
            f"qty ({value!r}); refusing to treat it as flat"
        )
    return qty


def _is_flat(qty: float) -> bool:
    """Whether a broker quantity means "no position".

    Comparing a quantity to ``0.0`` with ``==`` is a float-equality bug (Sonar
    S1244): a position left with a sub-unit residue would read as open forever,
    and a quantity a hair above zero would never be treated as closed. The
    tolerance is absolute and far below any tradable unit (brokers quote to at
    most 1e-9), so a real position cannot be mistaken for flat.

    Non-finite values are not flat - ``_position_qty`` already refuses them, and
    a NaN that slipped through must not read as "nothing open".
    """
    return math.isclose(qty, 0.0, rel_tol=0.0, abs_tol=1e-12)


def _resolve_optional_float(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {key}: {value!r}") from exc
        return parsed
    return None


_DATE_TIME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}")


def _broker_fill_timestamp(*candidates: Any) -> datetime | None:
    """The first usable broker timestamp among ``candidates``, as an aware UTC datetime.

    The caller lists them most to least specific (the fill event's own stamp, then the order's
    ``filled_at``, then its ``updated_at``). ``None`` when none parses: a missing or malformed
    stamp is never replaced with the wall clock, because the execution ledger orders and windows
    PnL by this column and a fabricated time would be stored with nothing to correct it later.
    """
    for candidate in candidates:
        # A string must carry a time of day: ``fromisoformat`` also reads a bare date as midnight,
        # which would be an invented instant. Alpaca always sends full RFC 3339.
        if isinstance(candidate, str) and _DATE_TIME_PREFIX.match(candidate.strip()) is None:
            continue
        stamp = canonical_utc_timestamp(candidate)
        if stamp is not None:
            return datetime.fromisoformat(stamp)
    return None


def _alpaca_fill_dedupe_key(
    *,
    execution_id: str,
    broker_order_id: str,
    event: str,
    cumulative_filled_qty: float,
    raw_timestamp: Any,
    filled_qty: float,
) -> str | None:
    """The dedupe key for one Alpaca trade-update fill, or ``None`` when none can be derived.

    In order of preference:

    1. ``alpaca:exec:{execution_id}`` - the broker's own per-fill id.
    2. ``alpaca:order:{id}:{event}:cum:{cumulative}`` - the order's cumulative filled quantity
       strictly increases per fill, so two equal-sized partial fills in the same instant still
       get different keys, and a redelivery (same cumulative) gets the same one.
    3. ``alpaca:order:{id}:{event}:{canonical timestamp}:{qty}`` - only when the update really
       carries a timestamp. Spellings of one instant (naive, aware, offset, ``Z``) collapse; the
       precision is not rounded.

    ``None`` when none of the three exists. The old code substituted ``now()`` for a missing
    timestamp, which made every redelivery a brand-new key and stored it again.
    """
    if execution_id:
        return f"alpaca:exec:{execution_id}"
    if cumulative_filled_qty > 0.0:
        return f"alpaca:order:{broker_order_id}:{event}:cum:{cumulative_filled_qty:.8f}"
    stamp = canonical_utc_timestamp(raw_timestamp)
    if stamp is None:
        return None
    return f"alpaca:order:{broker_order_id}:{event}:{stamp}:{filled_qty}"


def _default_alpaca_credentials(values: Mapping[str, str]) -> tuple[str, str, bool] | None:
    api_key = str(values.get("ALPACA_API_KEY", "")).strip()
    secret_key = str(values.get("ALPACA_SECRET_KEY", "")).strip()
    if api_key and secret_key:
        raw_paper = values.get("ALPACA_PAPER")
        paper = _parse_bool_env(
            "ALPACA_PAPER",
            str(raw_paper).strip() if raw_paper is not None else None,
            default=True,
        )
        return api_key, secret_key, paper
    return None


def _alpaca_acc_indexes(values: Mapping[str, str]) -> list[int]:
    return sorted(
        {
            int(match.group("index"))
            for key in values
            if (match := _ALPACA_ACC_RE.match(str(key)))
        }
    )


def _alpaca_credential_block(
    values: Mapping[str, str],
    idx: int,
) -> dict[str, str | bool] | None:
    """Parse one ACC{idx} credential block.

    Returns None when the block has no complete key/secret pair; raises
    ValueError when its paper flag fails the strict parse.
    """
    api_key = str(values.get(f"ALPACA_API_KEY_ACC{idx}", "")).strip()
    secret_key = str(values.get(f"ALPACA_SECRET_KEY_ACC{idx}", "")).strip()
    if not api_key or not secret_key:
        return None

    endpoint = str(values.get(f"ALPACA_ENDPOINT_ACC{idx}", "")).strip().lower()
    if endpoint:
        paper = "paper" in endpoint
    else:
        raw_paper = values.get(f"ALPACA_PAPER_ACC{idx}")
        paper = _parse_bool_env(
            f"ALPACA_PAPER_ACC{idx}",
            str(raw_paper).strip() if raw_paper is not None else None,
            default=True,
        )
    return {
        "source_index": f"ACC{idx}",
        "api_key": api_key,
        "secret_key": secret_key,
        "paper": paper,
        "account_id_hint": str(values.get(f"ALPACA_ACCOUNT_ID_ACC{idx}", "")).strip(),
        "login_hint": (
            str(values.get(f"ALPACA_BROKER_LOGIN_ACC{idx}", "")).strip()
            or str(values.get(f"ALPACA_ACCOUNT_NUMBER_ACC{idx}", "")).strip()
        ),
    }


def _alpaca_credential_blocks(
    values: Mapping[str, str],
    indexes: list[int],
) -> list[dict[str, str | bool]]:
    blocks: list[dict[str, str | bool]] = []
    for idx in indexes:
        block = _alpaca_credential_block(values, idx)
        if block is not None:
            blocks.append(block)
    return blocks


def _alpaca_block_credentials(block: dict[str, str | bool]) -> tuple[str, str, bool]:
    return (
        str(block["api_key"]),
        str(block["secret_key"]),
        bool(block["paper"]),
    )


def _matching_alpaca_block(
    blocks: list[dict[str, str | bool]],
    normalized_account_id: str,
    normalized_login: str,
) -> tuple[str, str, bool] | None:
    for block in blocks:
        account_id_hint = str(block.get("account_id_hint", "")).strip().lower()
        if account_id_hint and account_id_hint == normalized_account_id:
            return _alpaca_block_credentials(block)
        login_hint = str(block.get("login_hint", "")).strip().lower()
        if login_hint and normalized_login and login_hint == normalized_login:
            return _alpaca_block_credentials(block)
    return None


def _alpaca_blocks_have_hints(blocks: list[dict[str, str | bool]]) -> bool:
    return any(
        str(block.get("account_id_hint", "")).strip() or str(block.get("login_hint", "")).strip()
        for block in blocks
    )


def resolve_alpaca_credentials(
    *,
    account_id: str = "",
    broker_login: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, bool] | None:
    """Resolve Alpaca credentials from env (default or ACC{N} blocks)."""
    values = dict(env) if env is not None else dict(os.environ)

    indexes = _alpaca_acc_indexes(values)
    blocks: list[dict[str, str | bool]] = []
    if indexes:
        blocks = _alpaca_credential_blocks(values, indexes)

    normalized_account_id = str(account_id or "").strip().lower()
    normalized_login = str(broker_login or "").strip().lower()
    if blocks:
        matched = _matching_alpaca_block(blocks, normalized_account_id, normalized_login)
        if matched is not None:
            return matched
        if (normalized_account_id or normalized_login) and _alpaca_blocks_have_hints(
            blocks
        ):
            logger.warning(
                "No exact Alpaca credential block match; refusing ambiguous fallback",
                extra={
                    "data": {
                        "account_id": account_id,
                        "broker_login": broker_login,
                    }
                },
            )
            return None

    # Default pair applies only when the mapping is still unique: the legacy
    # single-account setup (no ACC{N} blocks, or exactly one block that the
    # account did not identify itself against). With two or more credential
    # blocks there is no unique mapping, so an unmatched account must never
    # silently route through another account's keys.
    if len(blocks) > 1:
        logger.warning(
            "No unique Alpaca credential block mapping; refusing ambiguous fallback",
            extra={
                "data": {
                    "account_id": account_id,
                    "broker_login": broker_login,
                    "candidate_blocks": len(blocks),
                    "hints_present": _alpaca_blocks_have_hints(blocks),
                }
            },
        )
        return None

    default_credentials = _default_alpaca_credentials(values)
    if default_credentials is not None:
        return default_credentials

    if not blocks:
        return None
    # Single remaining block with an unidentified account: the legacy
    # single-account setup. Keep it working, but make it visible — this is
    # the one path where an account can trade through keys it did not map to.
    logger.warning(
        "Unidentified account using the only configured Alpaca credential block",
        extra={
            "data": {
                "account_id": account_id,
                "broker_login": broker_login,
                "block_source_index": str(blocks[0].get("source_index", "")),
            }
        },
    )
    return _alpaca_block_credentials(blocks[0])


#: Crypto quote currencies Alpaca lists besides USD. A slash-less symbol
#: ending in one of these (``ETHBTC``) is a non-USD pair (#146).
_NON_USD_CRYPTO_QUOTES = ("USDT", "USDC", "BTC", "ETH")


def _alpaca_quote_asset(symbol: str) -> str:
    """Quote currency of an Alpaca symbol: USD for equities and ``*/USD``."""
    if "/" in symbol:
        return symbol.rsplit("/", 1)[1] or "unknown"
    for quote in _NON_USD_CRYPTO_QUOTES:
        if len(symbol) > len(quote) + 1 and symbol.endswith(quote):
            return quote
    return "USD"


class AlpacaLiveAdapter(BaseLiveAdapter):
    """Live adapter for Alpaca trading + trade update streaming."""

    broker_name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        account_id: str = "",
        env_values: Mapping[str, str] | None = None,
    ):
        # C1: file-first credential precedence — explicit args win, then
        # .accounts.env via credential_env, then os.environ for ABSENT keys.
        # A present-but-empty file value stays empty (never shadowed).
        self.api_key = api_key or resolve_env_value(env_values, "ALPACA_API_KEY")
        self.secret_key = secret_key or resolve_env_value(env_values, "ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            logger.warning(
                "Alpaca adapter falling back to resolved-env credentials",
                extra={"data": {"account_id": account_id}},
            )
        self.paper = (
            _parse_bool_env(
                "ALPACA_PAPER",
                resolve_env_value(env_values, "ALPACA_PAPER") or None,
                default=True,
            )
            if paper is None
            else paper
        )
        self.account_id = account_id
        self._trading_client: Any | None = None
        self._trading_stream: Any | None = None
        self._historical_data_client: Any | None = None
        self._crypto_data_client: Any | None = None
        # #78: subscribe exactly once per stream instance. Re-subscribing
        # the same (or a reconnected) stream duplicates callbacks. The
        # subscribed stream object itself is tracked (not its id(): CPython
        # reuses addresses after GC, so a stale id could skip subscribing a
        # NEW stream — a silent mute after disconnect/reconnect).
        self._subscribed_stream: Any | None = None
        self._connected = False
        # #167: serialises stop amendments with the sweeps that cancel orders
        # (see `_order_book_lock`). Bound to the loop that first uses it.
        self._order_book_lock_state: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
        # #144: bracket leg order id -> the id of the signal whose position the leg closes.
        # A leg is a broker-side child order: Alpaca gives it its own order id, so a fill of it
        # names no signal of ours and would otherwise be dropped (`client_order_id` is not a
        # signal), leaving the broker position smaller than every ledger on the account and
        # #142's guard alerting on every later exit for that symbol.
        # Learned from the parent order's nested legs whenever one is seen: at submit, on
        # every reconciliation pass - which is the adapter's only one and runs on connect,
        # before the trading stream consumes events, so a restart re-learns the mapping -
        # and at once in `amend_protective_stop`, because a trailed stop's replacement is
        # a NEW leg order id that would otherwise stay unattributable until the next pass.
        # #350: the same map also carries the re-placed exit protection of a partial
        # exit - its OCO legs, or the lone stop/take-profit order id itself - keyed to
        # the ENTRY signal id embedded in its ``-reprotect`` client order id.
        self._bracket_leg_order_id_to_signal_id: dict[str, str] = {}
        # Broker order ids of re-protect fills already flagged
        # ``ledger_reconciliation_required`` this process (dedupe, like cTrader's).
        self._flagged_reprotect_order_ids: set[str] = set()

        if not self.api_key or not self.secret_key:
            raise ValueError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

    async def connect(self) -> None:
        """Initialize REST and websocket clients, and verify account access."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
            from alpaca.trading.stream import TradingStream
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        self._trading_client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        self._trading_stream = TradingStream(self.api_key, self.secret_key, paper=self.paper)
        self._historical_data_client = StockHistoricalDataClient(
            self.api_key, self.secret_key
        )
        try:
            self._trading_client.get_account()
        except Exception:
            await self.disconnect()
            raise
        self._connected = True
        logger.info(
            "Connected Alpaca live adapter",
            extra={"data": {"account_id": self.account_id, "paper": self.paper}},
        )
        # This is the ONLY reconciliation trigger on this adapter: Alpaca has no periodic pass
        # and no `_reconciliation_required` retry queue the way cTrader does
        # (`ctrader_adapter._run_pending_reconciliation`). Every refusal to store a fill that
        # lacks a usable timestamp therefore recovers at the next connect, so the warnings
        # must say so rather than promise a pass.
        try:
            await self.reconcile_executions()
        except Exception as exc:
            logger.warning(
                "Alpaca execution reconciliation failed; fills may be missing until retry",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )
        # #145: a crash between an exit's protection release and its re-place
        # leaves a persisted marker; replay it now that the order book is
        # readable again. Best-effort per symbol inside; never blocks connect.
        try:
            await self._recover_exit_reprotections()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Alpaca exit re-protection recovery failed; markers stay pending",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    async def _fetch_reconciliation_page(
        self,
        before: tuple[datetime, str] | None,
        max_signals: int,
    ) -> list[str]:
        if before is None:
            return await asyncio.to_thread(
                list_live_signal_ids_for_reconciliation,
                self.account_id,
                max_signals,
            )
        return await asyncio.to_thread(
            list_live_signal_ids_for_reconciliation,
            self.account_id,
            max_signals,
            before=before,
        )

    def _register_bracket_legs(self, parent_signal_id: str, order: Any) -> int:
        """Learn which bracket leg order ids belong to this parent signal (#144).

        The broker reports a parent order's legs nested in it, and a leg carries only its own
        order id: nothing else connects a leg's fill to the signal whose position it closes.
        Returns how many leg ids were learned.
        """
        registered = 0
        for leg in _order_legs(order):
            leg_key = _bracket_leg_key(_order_attr(leg, "id"))
            if leg_key is None:
                continue
            self._bracket_leg_order_id_to_signal_id[leg_key] = str(parent_signal_id)
            registered += 1
        return registered

    async def _fetch_parent_order_with_legs(self, signal_id: str) -> Any:
        """Fetch the parent order for a signal, with its bracket legs nested in it.

        ``get_order_by_client_id`` takes no ``nested`` argument in alpaca-py 0.44.0, and the
        runbook records that a bracket's legs are only reported back nested in the parent
        (``docs/live-deploy-runbook.md``): fetching by client id alone therefore returns no
        legs, and the leg-order-id -> signal map this adapter depends on would never be
        re-learned after a restart - the #144 defect itself, since the stream drops a leg
        fill whose client order id names no signal. The persisted broker order id is used
        with ``GetOrderByIdRequest(nested=True)`` first; only a signal submitted before that
        id was persisted falls back to the client-id fetch, and an alpaca-py without the
        nested filter degrades to the flat ``get_order_by_id``.
        """
        stored_order_id = await asyncio.to_thread(get_live_signal_broker_order_id, signal_id)
        if stored_order_id:
            try:
                from alpaca.trading.requests import GetOrderByIdRequest
            except ImportError:
                # An alpaca-py without the nested filter request: the flat fetch below
                # still carries the parent's own fill. `ImportError`, not
                # `ModuleNotFoundError`: a missing NAME inside an importable
                # module raises the former, and the fallback must still fire.
                GetOrderByIdRequest = None  # type: ignore[assignment]
            if GetOrderByIdRequest is not None:
                try:
                    return await asyncio.to_thread(
                        self._trading_client.get_order_by_id,
                        stored_order_id,
                        GetOrderByIdRequest(nested=True),
                    )
                except TypeError:
                    # An alpaca-py whose get_order_by_id takes no filter: ask for the
                    # flat order instead.
                    pass
            return await asyncio.to_thread(
                self._trading_client.get_order_by_id, stored_order_id
            )
        return await asyncio.to_thread(
            self._trading_client.get_order_by_client_id, signal_id
        )

    def _parent_signal_id_for_broker_order(self, broker_order_id: str | None) -> str | None:
        """The signal whose bracket leg this broker order id names, or ``None``."""
        if not broker_order_id:
            return None
        return self._bracket_leg_order_id_to_signal_id.get(str(broker_order_id))

    async def _verified_reprotect_entry(self, candidate: str | None) -> str | None:
        """The entry signal a ``-reprotect`` client order id attributes to.

        The leading UUID is trusted only when the signal row exists and is not
        itself exit-only or a broker-close bookkeeping row: the bare
        ``{exit}-reprotect`` form leads with the *exit* signal's id, and
        booking a close against it would sign the fill by the wrong action.
        Anything unverifiable attributes to nobody - the caller flags it.
        """
        if not candidate:
            return None
        try:
            UUID(candidate)
        except (AttributeError, TypeError, ValueError):
            return None
        try:
            metadata = await asyncio.to_thread(get_live_signal_metadata, candidate)
        except Exception as exc:
            logger.debug(
                "Alpaca re-protect entry lookup failed for %s: %r", candidate, exc
            )
            return None
        if not isinstance(metadata, dict):
            return None
        if metadata.get("exit_only") or metadata.get("broker_side_close"):
            return None
        if (
            str(metadata.get("risk_sizing_mode") or "").strip().lower()
            == EXIT_ONLY_RISK_SIZING_MODE
        ):
            return None
        return candidate

    def _flag_reprotect_fill_unattributed(
        self,
        order: Any,
        client_order_id: Any,
        broker_order_id: str,
        *,
        cause: str = "alpaca_reprotect_fill_unattributed",
    ) -> None:
        """Flag a fill that names no entry signal (#350 on #277).

        Same surface as cTrader's unattributable closes: the durable
        ``ledger_reconciliation_required`` audit row plus an ERROR alert; the
        fill is never guessed into a ledger. Once per broker order id per
        process - redeliveries and the reconcile scan see the same order again.
        The id is marked only AFTER the audit write lands: a failed write must
        not suppress the flag for the rest of the process.
        """
        if broker_order_id in self._flagged_reprotect_order_ids:
            return
        symbol = _order_symbol(order)
        flag_live_ledger_reconciliation(
            account_id=self.account_id,
            symbol=symbol,
            cause=cause,
            detail={
                "client_order_id": str(client_order_id or ""),
                "broker_order_id": broker_order_id,
                "order_type": _order_type(order),
            },
        )
        self._flagged_reprotect_order_ids.add(broker_order_id)
        logger.error(
            "ALERT: Alpaca fill could not be attributed to an entry signal; "
            "the account ledger needs reconciliation",
            extra={
                "data": {
                    "alert": "ledger_reconciliation_required",
                    "account_id": self.account_id,
                    "symbol": symbol,
                    "cause": cause,
                    "client_order_id": str(client_order_id or ""),
                    "broker_order_id": broker_order_id,
                }
            },
        )

    async def _handle_reprotect_trade_update(
        self,
        data: Any,
        order: Any,
        client_order_id: Any,
        *,
        broker_order_id: str,
        event: str,
    ) -> bool:
        """Book or flag a fill of re-placed exit protection; ``True`` when handled.

        Reached only when the leg map missed and ``client_order_id`` is not a
        signal UUID - the ``-reprotect`` forms of #350. A verified entry id
        books the fill against that signal's ledger and learns the order id
        for the next event; anything else is flagged, never dropped.
        """
        coid_text = str(client_order_id or "")
        if _REPROTECT_CLIENT_ORDER_MARKER not in coid_text:
            return False
        entry_signal_id = await self._verified_reprotect_entry(
            _reprotect_entry_candidate(coid_text)
        )
        if entry_signal_id is None:
            self._flag_reprotect_fill_unattributed(
                order, client_order_id, broker_order_id
            )
            return True
        self._bracket_leg_order_id_to_signal_id[broker_order_id] = entry_signal_id
        await self._record_bracket_leg_fill(
            entry_signal_id,
            data,
            order,
            broker_order_id=broker_order_id,
            event=event,
        )
        return True

    async def _register_reprotect_attribution(
        self,
        client: Any,
        plan: Mapping[str, Any],
        order: Any,
        *,
        nested_lookup: bool,
    ) -> None:
        """Attribute a re-placed protection order's fills to the entry signal.

        Every fillable order id is learned like a bracket leg's: an OCO
        parent's own id (it IS the take-profit - its fill is a separate fill
        from the stop leg's, never a container echo), each leg's id, and a
        lone stop/take-profit's own id.
        """
        entry_signal_id = str(plan.get("entry_signal_id") or "").strip()
        if not entry_signal_id:
            return
        order_key = _bracket_leg_key(_order_attr(order, "id"))
        if order_key is not None:
            self._bracket_leg_order_id_to_signal_id[order_key] = entry_signal_id
        self._register_bracket_legs(entry_signal_id, order)
        if _order_legs(order) or not nested_lookup:
            return
        # An OCO submit response may not nest its legs; re-read the order
        # with ``nested=True`` so the leg ids are learned now rather than
        # at the next connect. Until they are, a leg fill whose id was not
        # learned attributes through its own client order id or is flagged
        # for reconciliation - never guessed into a ledger.
        broker_order_id = _normalize_broker_order_id(_order_attr(order, "id"))
        if broker_order_id is None:
            return
        try:
            from alpaca.trading.requests import GetOrderByIdRequest
        except ImportError:
            return
        try:
            fetched = await asyncio.to_thread(
                client.get_order_by_id,
                broker_order_id,
                GetOrderByIdRequest(nested=True),
            )
        except Exception:
            return
        if fetched is None:
            return
        fetched_key = _bracket_leg_key(_order_attr(fetched, "id"))
        if fetched_key is not None:
            self._bracket_leg_order_id_to_signal_id[fetched_key] = entry_signal_id
        self._register_bracket_legs(entry_signal_id, fetched)

    async def _reconcile_bracket_leg_fill(self, parent_signal_id: str, leg: Any) -> bool:
        """Backfill one bracket leg's missed fill against the parent signal's ledger (#144).

        The leg's own broker cumulative is the authority. Only the slice still missing for that
        leg order is persisted, so a leg the stream already saw in full - or in part - is not
        counted twice, and a second pass adds nothing.
        """
        leg_order_id = _bracket_leg_key(_order_attr(leg, "id"))
        if leg_order_id is None:
            return False
        if _order_status(leg) not in {"filled", "partially_filled"}:
            return False
        leg_cumulative_qty = _to_float(_order_attr(leg, "filled_qty", None), default=0.0)
        if not math.isfinite(leg_cumulative_qty) or leg_cumulative_qty <= 0.0:
            return False
        close_signal_id = broker_close_signal_id(parent_signal_id)
        recorded_qty = await asyncio.to_thread(
            get_live_signal_filled_qty_for_broker_order, close_signal_id, leg_order_id
        )
        missing_qty = leg_cumulative_qty - recorded_qty
        if missing_qty <= 0.0:
            return False
        fill_ts = _broker_fill_timestamp(
            _order_attr(leg, "filled_at", None), _order_attr(leg, "updated_at", None)
        )
        if fill_ts is None:
            logger.warning(
                "Alpaca bracket leg has filled quantity but neither a usable filled_at nor "
                "updated_at; not persisting the missed close with an invented time. reconciliation "
                "runs only when the adapter connects, so the next adapter connect retries it",
                extra={
                    "data": {
                        "account_id": self.account_id,
                        "parent_signal_id": parent_signal_id,
                        "leg_order_id": leg_order_id,
                        "leg_filled_qty": leg_cumulative_qty,
                        "leg_recorded_qty": recorded_qty,
                    }
                },
            )
            return False
        actual_price, price_observed = _resolve_fill_price(
            (None, None),
            order_avg_price=_order_attr(leg, "filled_avg_price", None),
            event_qty=missing_qty,
            cumulative_qty=leg_cumulative_qty,
        )
        persisted_qty = await asyncio.to_thread(
            record_live_broker_side_close_fill,
            parent_signal_id=parent_signal_id,
            close_reason=_bracket_leg_close_reason(leg),
            filled_qty=missing_qty,
            actual_price=actual_price,
            actual_price_observed=price_observed,
            commission=_known_commission(leg),
            timestamp=fill_ts,
            broker_order_id=leg_order_id,
            # The leg's own cumulative and average let the insert cap the booking at
            # what the leg has actually filled and derive the missing slice's price
            # when this backfill covers only part of it.
            cumulative_filled_qty=leg_cumulative_qty,
            cumulative_avg_price=_positive_finite(
                _order_attr(leg, "filled_avg_price", None)
            ),
            dedupe_key=_alpaca_fill_dedupe_key(
                execution_id="",
                broker_order_id=leg_order_id,
                event=_LEG_FILL_EVENT_LABEL,
                cumulative_filled_qty=leg_cumulative_qty,
                raw_timestamp=None,
                filled_qty=missing_qty,
            )
            or "",
            close_side=_order_side(leg),
        )
        if persisted_qty <= 0.0:
            return False
        logger.info(
            "Reconciled missed Alpaca bracket leg close fill",
            extra={
                "data": {
                    "parent_signal_id": parent_signal_id,
                    "close_signal_id": broker_close_signal_id(parent_signal_id),
                    "leg_order_id": leg_order_id,
                    "close_reason": _bracket_leg_close_reason(leg),
                    "filled_qty": persisted_qty,
                    "leg_cumulative_qty": leg_cumulative_qty,
                    "leg_recorded_qty": recorded_qty,
                }
            },
        )
        return True

    async def _reconcile_signal_fill(self, signal_id: str) -> int:
        """Backfill this signal's missed fills; returns how many fills were recovered.

        The order's own fill first, then - since #144 - the fills of the bracket legs that hang
        off it: both are recovered from one fetch of the parent order.
        """
        try:
            order = await self._fetch_parent_order_with_legs(signal_id)
        except Exception:
            # Unknown to this account or not found.
            return 0
        recovered = 0
        # #144: the parent order carries its nested legs, so a restart (or a rebuild after a
        # reconnect) re-learns which leg orders close this signal's position - even for a parent
        # whose own fill is already recorded, which returns below without fetching anything else.
        self._register_bracket_legs(signal_id, order)
        broker_filled_qty = _to_float(
            getattr(order, "filled_qty", None), default=0.0
        )
        if not math.isfinite(broker_filled_qty):
            logger.warning(
                "Alpaca order reports a non-finite filled_qty; not reconciling it",
                extra={
                    "data": {
                        "signal_id": signal_id,
                        "filled_qty": str(getattr(order, "filled_qty", None)),
                    }
                },
            )
            return recovered
        local_filled_qty = await asyncio.to_thread(
            get_live_signal_filled_qty, signal_id
        )
        missing_filled_qty = broker_filled_qty - local_filled_qty
        broker_order_id = _normalize_broker_order_id(getattr(order, "id", None))
        # `filled_avg_price` is the broker's average over the order's WHOLE cumulative quantity.
        # Without it there is no price to record, and a fill at 0.0 is not a fill (#124): persist
        # nothing, and the next reconciliation pass retries.
        cumulative_avg_price = (
            _positive_finite(getattr(order, "filled_avg_price", None))
            if missing_filled_qty > 0
            else None
        )
        fill_ts = (
            _broker_fill_timestamp(
                getattr(order, "filled_at", None), getattr(order, "updated_at", None)
            )
            if missing_filled_qty > 0
            else None
        )
        # #144: the order's own fill is booked FIRST and a leg's close only after it,
        # because a leg closes the position that fill opened: booking a leg against a
        # parent whose own fill could not be recovered drives the deployment's ledger
        # below zero (the close insert refuses it outright, but the leg would then stay
        # unbooked until the next connect - this function used to return early on exactly
        # that, leaving an already-booked leg behind).
        parent_fill_pending = missing_filled_qty > 0
        parent_fill_unrecoverable = parent_fill_pending and (
            cumulative_avg_price is None or fill_ts is None or broker_order_id is None
        )
        if parent_fill_pending and broker_order_id is not None:
            if cumulative_avg_price is None:
                logger.warning(
                    "Alpaca order has filled quantity but no usable filled_avg_price; not "
                    "persisting the missed fill, the next reconciliation pass retries",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "signal_id": signal_id,
                            "broker_order_id": broker_order_id,
                            "filled_avg_price": str(getattr(order, "filled_avg_price", None)),
                            "missing_filled_qty": missing_filled_qty,
                        }
                    },
                )
            elif fill_ts is None:
                logger.warning(
                    "Alpaca order has filled quantity but neither a usable filled_at nor "
                    "updated_at; not persisting the missed fill with an invented time. "
                    "reconciliation runs only when the adapter connects, so the next "
                    "adapter connect retries it",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "signal_id": signal_id,
                            "broker_order_id": broker_order_id,
                            "filled_at": str(getattr(order, "filled_at", None)),
                            "updated_at": str(getattr(order, "updated_at", None)),
                            "missing_filled_qty": missing_filled_qty,
                        }
                    },
                )
            else:
                execution = LiveExecution(
                    signal_id=UUID(signal_id),
                    # Placeholder: the save derives the remainder's own price from the
                    # cumulative average inside its transaction, because part of the order
                    # may already be recorded at a different price, and sets the verdict
                    # (`derived`, or `derived_unavailable` with the quantity still
                    # recorded at 0.0).
                    actual_price=cumulative_avg_price,
                    actual_price_observed=PRICE_DERIVED,
                    filled_qty=missing_filled_qty,
                    commission=_known_commission(order),
                    timestamp=fill_ts,
                    broker_order_id=broker_order_id,
                    dedupe_key=(
                        f"alpaca:order:{broker_order_id}:reconciled:{broker_filled_qty}"
                    ),
                )
                persisted_qty = await asyncio.to_thread(
                    save_live_execution_up_to_cumulative,
                    execution,
                    broker_filled_qty,
                    cumulative_avg_price=cumulative_avg_price,
                )
                if persisted_qty > 0.0:
                    logger.info(
                        "Reconciled missed Alpaca fill",
                        extra={
                            "data": {
                                "signal_id": signal_id,
                                "broker_order_id": broker_order_id,
                                "filled_qty": persisted_qty,
                                "broker_cumulative_filled_qty": broker_filled_qty,
                                "local_cumulative_filled_qty": local_filled_qty,
                            }
                        },
                    )
                    recovered += 1
        if not parent_fill_unrecoverable:
            for leg in _order_legs(order):
                if await self._reconcile_bracket_leg_fill(signal_id, leg):
                    recovered += 1
        return recovered

    async def reconcile_executions(self, *, max_signals: int = 200) -> int:
        """Backfill missing fill quantity for recent signals in this Alpaca account.

        Recovers fills that completed while the daemon was down or crashed between
        broker submit and stream-event persistence. Uses the client_order_id == signal.id
        convention. Broker order quantities are cumulative, while local executions are
        stored as event deltas, so only the positive cumulative difference is persisted.

        Since #144 one pass also recovers the fills of a bracket's stop/target legs, which
        close the signal's position without any order of ours. Returns the number of signals
        with at least one recovered fill - a signal counted once however many fills it needed,
        because a leg's fill and the order's own are backfilled from the same fetch.
        """
        if self._trading_client is None:
            return 0
        recovered = 0
        before: tuple[datetime, str] | None = None
        while True:
            signal_ids = await self._fetch_reconciliation_page(before, max_signals)
            if not signal_ids:
                break
            for signal_id in signal_ids:
                if await self._reconcile_signal_fill(signal_id):
                    recovered += 1
            if len(signal_ids) < max_signals:
                break
            next_before = await asyncio.to_thread(
                get_live_signal_reconciliation_cursor, signal_ids[-1]
            )
            if next_before is None or next_before == before:
                break
            before = next_before
        try:
            await self._reconcile_reprotect_orders()
        except Exception as exc:
            logger.warning(
                "Alpaca re-protect order reconciliation failed; leg fills may be "
                "missing until the next connect",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )
        return recovered

    async def _reconcile_reprotect_orders(self) -> int:
        """Re-learn and backfill fills of re-placed exit protection (#350 on #277).

        The ``-reprotect`` orders are not signals, so the per-signal pass never
        sees them: this scan finds them by client order id on the ``status=all``
        nested listing - bounded like ``_list_filled_parents_with_held_legs`` -
        attributes their legs (or the lone order itself) to the verified entry
        signal, and backfills each filled slice once. Fills that verify no
        entry are flagged ``ledger_reconciliation_required``, never dropped.
        """
        client = self._trading_client
        if client is None:
            return 0
        get_orders = getattr(client, "get_orders", None)
        if not callable(get_orders):
            return 0
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            nested=True,
            limit=_ALPACA_HELD_LEG_LOOKUP_LIMIT,
        )
        result = await asyncio.to_thread(get_orders, filter=request)
        if not isinstance(result, (list, tuple)):
            raise RuntimeError(
                f"Alpaca order list has an unrecognised shape ({type(result).__name__}); "
                "refusing to treat it as complete"
            )
        if len(result) >= _ALPACA_HELD_LEG_LOOKUP_LIMIT:
            logger.warning(
                "Alpaca returned %s orders, the page limit; re-protect orders "
                "older than the page are not seen by this pass",
                len(result),
                extra={"data": {"account_id": self.account_id}},
            )
        recovered = 0
        for order in result:
            client_order_id = _order_attr(order, "client_order_id")
            coid_text = str(client_order_id or "")
            if _REPROTECT_CLIENT_ORDER_MARKER not in coid_text:
                continue
            entry_signal_id = await self._verified_reprotect_entry(
                _reprotect_entry_candidate(coid_text)
            )
            legs = _order_legs(order)
            if _order_class_text(order) == "oco":
                # Alpaca's OCO parent IS the take-profit order; the stop is
                # its only leg. Both are fillable orders with their own fills,
                # so both are reconciled - a filled parent is a real close,
                # not a container echo of its leg.
                targets = [order, *legs]
            else:
                targets = legs if legs else [order]
            if entry_signal_id is None:
                for item in targets:
                    item_status = _order_status(item)
                    item_filled = _to_float(
                        _order_attr(item, "filled_qty", None), default=0.0
                    )
                    if (
                        item_status in {"filled", "partially_filled"}
                        and math.isfinite(item_filled)
                        and item_filled > 0.0
                    ):
                        flag_key = (
                            _bracket_leg_key(_order_attr(item, "id"))
                            or _control_order_id(_order_attr(item, "id"))
                            or coid_text
                        )
                        self._flag_reprotect_fill_unattributed(
                            item, client_order_id, flag_key
                        )
                continue
            for item in targets:
                leg_key = _bracket_leg_key(_order_attr(item, "id"))
                if leg_key is not None:
                    self._bracket_leg_order_id_to_signal_id[leg_key] = entry_signal_id
                if await self._reconcile_bracket_leg_fill(entry_signal_id, item):
                    recovered += 1
        return recovered

    async def backfill_commissions(self, *, now: datetime | None = None) -> int:
        """Price this account's still-unknown crypto commissions from posted ``CFEE`` activities.

        Crypto fees are posted end of day, so fills are stored with a NULL commission and this pass
        - run periodically by the daemon - fills them in once their trade date has been posted
        (``CFEE_POSTING_DELAY``) and while they are inside ``COMMISSION_BACKFILL_LOOKBACK``. It
        reads ``FILL``, ``CFEE`` and ``FEE`` activities over REST (alpaca-py has no activities
        method), allocates each (pair, trade date)'s fee as documented in `live_runner.alpaca_fees`,
        and
        writes a group's commissions all or nothing. Every group that cannot be priced honestly
        keeps NULL and is logged; nothing is ever written as ``0.0``.

        Returns the number of executions whose commission was set. Never raises for broker data or
        a failed request: those are logged and retried on the next pass.
        """
        client = self._trading_client
        if client is None:
            return 0
        now = now or datetime.now(timezone.utc)
        rows = await asyncio.to_thread(
            list_live_executions_missing_commission,
            self.account_id,
            since=now - COMMISSION_BACKFILL_LOOKBACK,
            until=now,
        )
        candidates: list[dict[str, Any]] = []
        skipped: dict[str, list[str]] = {}
        for row in rows:
            kind = classify_alpaca_symbol(row["symbol"])
            if kind != "crypto":
                reason = (
                    f"not a crypto pair (symbol {row['symbol']!r} classifies as {kind}); no CFEE "
                    "applies and nothing else prices it"
                )
            elif not all(
                fee_day_is_posted(day, now) for day in candidate_fee_days(row["timestamp"])
            ):
                reason = "its trade date's CFEE is not posted yet; retried on a later pass"
            else:
                candidates.append(row)
                continue
            skipped.setdefault(reason, []).append(str(row["execution_id"]))
        for reason, execution_ids in skipped.items():
            logger.info(
                "Alpaca commission backfill: skipping %s execution(s) with unknown commission: "
                "%s: %s",
                len(execution_ids),
                reason,
                ", ".join(execution_ids),
                extra={"data": {"account_id": self.account_id, "execution_ids": execution_ids}},
            )
        if not candidates:
            return 0

        days = {day for row in candidates for day in candidate_fee_days(row["timestamp"])}
        window_start = day_start_utc(min(days)) - timedelta(days=1)
        fill_window_end = min(day_start_utc(max(days)) + timedelta(days=2), now)
        try:
            fills = await asyncio.to_thread(
                fetch_account_activities, client, "FILL", after=window_start, until=fill_window_end
            )
            cfees = await asyncio.to_thread(
                fetch_account_activities, client, "CFEE", after=window_start, until=now
            )
            usd_fees = await asyncio.to_thread(
                fetch_account_activities, client, "FEE", after=window_start, until=now
            )
        except Exception as exc:
            logger.warning(
                "Alpaca commission backfill: reading account activities failed; commissions stay "
                "unknown until the next pass",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )
            return 0

        candidate_pairs = {crypto_pair_key(row["symbol"]) for row in candidates}
        order_ids = sorted(
            {
                str(item.get("order_id") or "").strip()
                for item in fills
                if crypto_pair_key(item.get("symbol")) in candidate_pairs
            }
            - {""}
        )
        local_rows = await asyncio.to_thread(
            list_live_executions_for_broker_orders, self.account_id, order_ids
        )
        plans, unplaced = plan_commission_backfill(
            candidates=candidates,
            fill_activities=fills,
            cfee_activities=cfees,
            fee_activities=usd_fees,
            local_rows=local_rows,
            now=now,
        )
        for message in unplaced:
            logger.warning(
                "Alpaca commission backfill: leaving commission unknown (CFEE not attributable): "
                "%s",
                message,
                extra={"data": {"account_id": self.account_id}},
            )
        updated = 0
        for plan in plans:
            if plan.problem is not None:
                logger.warning(
                    "Alpaca commission backfill: leaving the CFEE of %s on %s unallocated, "
                    "commissions stay unknown: %s",
                    plan.pair,
                    plan.day.isoformat(),
                    plan.problem,
                    extra={"data": {"account_id": self.account_id}},
                )
                continue
            applied = await asyncio.to_thread(
                apply_live_execution_commissions, self.account_id, plan.commissions
            )
            updated += applied
            logger.info(
                "Alpaca commission backfill: priced %s execution(s) of %s on %s from CFEE",
                applied,
                plan.pair,
                plan.day.isoformat(),
                extra={"data": {"account_id": self.account_id, "commissions": plan.commissions}},
            )
        return updated

    async def disconnect(self) -> None:
        stream = self._trading_stream
        self._trading_stream = None
        self._subscribed_stream = None
        self._trading_client = None
        self._historical_data_client = None
        self._crypto_data_client = None
        self._connected = False
        if stream is None:
            return

        stop_ws = getattr(stream, "stop_ws", None)
        stop = getattr(stream, "stop", None)
        try:
            if callable(stop_ws):
                result = stop_ws()
                if inspect.isawaitable(result):
                    await result
            elif callable(stop):
                result = stop()
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.warning(
                "Failed to stop Alpaca stream cleanly",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    def _load_alpaca_order_types(
        self,
    ) -> tuple[Any, Any, Any]:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        return OrderSide, TimeInForce, MarketOrderRequest

    async def _recover_existing_signal_order(
        self,
        trading_client: Any,
        canonical_signal_id: str,
    ) -> str:
        raw_existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, canonical_signal_id
        )
        if raw_existing_order_id:
            existing_order_id = _normalize_broker_order_id(raw_existing_order_id)
            if existing_order_id is None:
                raise RuntimeError(
                    "Alpaca persisted broker order id is invalid; refusing duplicate submission."
                )
            return existing_order_id

        try:
            recovered_order = await asyncio.to_thread(
                trading_client.get_order_by_client_id,
                canonical_signal_id,
            )
        except Exception as exc:
            raise RuntimeError(
                "Alpaca canonical signal already exists and broker recovery failed; "
                "refusing possible duplicate submission."
            ) from exc
        recovered_order_id = _normalize_broker_order_id(getattr(recovered_order, "id", None))
        if recovered_order_id is None:
            raise RuntimeError(
                "Alpaca canonical signal already exists without a recoverable broker "
                "order id; refusing possible duplicate submission."
            )
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id,
            canonical_signal_id,
            recovered_order_id,
        )
        if not persisted:
            raise RuntimeError("Failed to persist recovered Alpaca broker order id.")
        logger.info(
            "Recovered accepted Alpaca order without resubmitting",
            extra={
                "data": {
                    "signal_id": canonical_signal_id,
                    "broker_order_id": recovered_order_id,
                }
            },
        )
        return recovered_order_id

    def _resolve_broker_sl_tp(self, signal: LiveSignal) -> tuple[float, float]:
        """Resolve absolute SL/TP prices from signal metadata.

        Fail closed: sizing divided the risk budget by the stop distance, so an
        order without SL/TP would open an unbounded position. Exit-only orders
        (risk_sizing_mode == 'exit_only') close an existing position, reduce
        exposure and legitimately carry no SL/TP — the guard applies to
        opening orders only.
        """
        metadata = signal.metadata or {}
        stop_loss = _resolve_optional_float(metadata, _SL_METADATA_KEYS)
        take_profit = _resolve_optional_float(metadata, _TP_METADATA_KEYS)
        exit_only = (
            str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE
        )
        # C1: explicit unbounded-take-profit policy (processor-declared) —
        # stop-only order, never an invented bracket.
        unbounded_tp = not exit_only and take_profit is None and is_unbounded_tp(metadata)
        missing = [
            name
            for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit))
            if value is None
        ]
        if missing and not exit_only and not (unbounded_tp and missing == ["take_profit"]):
            raise ValueError(
                "Refusing to submit Alpaca order without "
                f"{' and '.join(missing)} (signal_id={signal.id}, "
                f"symbol={signal.symbol}); opening an unbounded position is not allowed."
            )
        if missing:
            if unbounded_tp and stop_loss is not None:
                # CodeRabbit round: the early return must not bypass the
                # finite/positive validation below — an invalid stop would
                # reach StopLossRequest unchecked.
                if math.isnan(stop_loss) or math.isinf(stop_loss) or stop_loss <= 0:
                    raise ValueError(
                        f"Invalid stop_loss price for {signal.symbol}: {stop_loss!r}; "
                        "refusing to submit."
                    )
                return float(stop_loss), float("nan")
            return float("nan"), float("nan")
        for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit)):
            if math.isnan(value) or math.isinf(value) or value <= 0:
                raise ValueError(
                    f"Invalid {name} price for {signal.symbol}: {value!r}; "
                    "refusing to submit."
                )
        return float(stop_loss), float(take_profit)

    def _normalize_bracket_entry(self, signal: LiveSignal, qty: float) -> tuple[LiveSignal, float]:
        """Bring an entry onto the grid Alpaca accepts for bracket/OTO orders.

        Runs before the signal row is persisted (B-8), so a refusal here never
        leaves a persisted signal without a broker order. The persisted
        metadata carries exactly the qty and SL/TP that are submitted.
        Exit-only orders are simple orders and pass through unchanged.
        """
        metadata = dict(signal.metadata or {})
        if str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE:
            return signal, qty
        kind = classify_alpaca_symbol(signal.symbol)
        if kind == "crypto":
            raise ValueError(
                f"Alpaca does not support bracket/OTO orders for crypto ({signal.symbol}); "
                f"refusing to submit before persistence (signal_id={signal.id})."
            )
        if kind != "us_equity":
            # Fail closed: an unclassifiable symbol (a pair outside CRYPTO_QUOTES such as
            # BTC-EUR, an option, a malformed symbol) could be a crypto/non-equity that Alpaca
            # rejects only after the row is persisted (B-8).
            raise ValueError(
                f"Cannot verify {signal.symbol} is a US equity; Alpaca bracket/OTO orders are "
                f"only supported for US equities, refusing to submit before persistence "
                f"(signal_id={signal.id})."
            )
        if metadata.get("extended_hours"):
            raise ValueError(
                _EXTENDED_HOURS_BRACKET_MESSAGE.format(
                    signal_id=signal.id, symbol=signal.symbol
                )
            )
        whole_qty = _floor_to_whole_shares(qty)
        if whole_qty < 1:
            raise ValueError(
                f"Risk-sized qty {qty!r} for {signal.symbol} is below one whole share; "
                "Alpaca bracket orders require whole shares, and rounding up would "
                f"exceed the risk budget (signal_id={signal.id})."
            )
        if whole_qty != qty:
            logger.info(
                "Floored Alpaca bracket entry qty to whole shares",
                extra={
                    "data": {
                        "signal_id": str(signal.id),
                        "symbol": signal.symbol,
                        "risk_sized_qty": qty,
                        "submitted_qty": whole_qty,
                    }
                },
            )
        metadata["qty"] = whole_qty
        metadata["explicit_qty"] = whole_qty
        entry_price = float(signal.theoretical_price)
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError(
                f"Invalid reference price for {signal.symbol}: "
                f"{signal.theoretical_price!r}; refusing to submit."
            )
        _round_bracket_levels_in_place(metadata, entry_price=entry_price, signal=signal)
        normalized = signal.model_copy(update={"metadata": metadata})
        # Missing/NaN/inf/non-positive levels are refused here, not after
        # persistence, so a refusal never leaves a signal row without an order.
        self._resolve_broker_sl_tp(normalized)
        return normalized, whole_qty

    def _build_order_request(
        self,
        signal: LiveSignal,
        *,
        qty: float,
        order_side: Any,
        time_in_force: Any,
        market_request_cls: Any,
        client_order_id: str,
    ) -> Any:
        qty = coerce_positive_live_qty(qty, field_name="Alpaca order qty")
        if signal.action not in {"buy", "sell"}:
            # Fail closed: an unmapped action must never fall through to SELL.
            raise ValueError(f"Unsupported signal action: {signal.action}")
        stop_loss_price, take_profit_price = self._resolve_broker_sl_tp(signal)
        extended_hours = bool((signal.metadata or {}).get("extended_hours"))
        if signal.action == "buy":
            side = order_side.BUY
        else:
            side = order_side.SELL
        exit_only = (
            str((signal.metadata or {}).get("risk_sizing_mode") or "")
            == EXIT_ONLY_RISK_SIZING_MODE
        )
        if extended_hours and not exit_only:
            # The broker rejects bracket orders with extended_hours enabled
            # ("bracket order has non-zero extended_hours value", HTTP 422), and
            # OTO bracket legs cannot execute outside regular hours anyway.
            # Fail closed rather than silently dropping the SL/TP the risk
            # sizing assumed: the caller must choose bounded-hours bracket or
            # naked extended-hours — and naked entries are not allowed.
            raise ValueError(
                _EXTENDED_HOURS_BRACKET_MESSAGE.format(
                    signal_id=signal.id, symbol=signal.symbol
                )
            )
        # C1: explicit unbounded policy rides as OTO (entry + stop leg,
        # no take-profit leg) instead of an invented bracket.
        unbounded_tp = (
            not exit_only
            and isinstance(take_profit_price, float)
            and math.isnan(take_profit_price)
            and is_unbounded_tp(signal.metadata)
        )
        if unbounded_tp:
            order_class = OrderClass.OTO
            take_profit_leg = None
        else:
            order_class = OrderClass.SIMPLE if exit_only else OrderClass.BRACKET
            take_profit_leg = (
                None if exit_only else TakeProfitRequest(limit_price=take_profit_price)
            )
        return market_request_cls(
            time_in_force=time_in_force.DAY,
            extended_hours=False,
            order_class=order_class,
            take_profit=take_profit_leg,
            stop_loss=None if exit_only else StopLossRequest(stop_price=stop_loss_price),
            symbol=signal.symbol,
            qty=qty,
            side=side,
            client_order_id=client_order_id,
        )

    async def _persist_submitted_order_id(
        self,
        canonical_signal_id: str,
        broker_order_id: str,
    ) -> None:
        normalized_order_id = _normalize_broker_order_id(broker_order_id)
        if normalized_order_id is None:
            raise RuntimeError("Alpaca order response has no valid broker order id.")
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, canonical_signal_id, normalized_order_id
        )
        if not persisted:
            raise RuntimeError("Failed to persist Alpaca broker order id.")

    async def _require_exit_position(self, signal: LiveSignal, qty: float) -> float:
        """Fail closed unless an exit-only order provably reduces a live position.

        Alpaca closes a position with an ordinary opposing market order, so an
        exit-only order whose position has already gone would silently OPEN one -
        and because the exit-only path drops the mandatory SL/TP legs it would
        open it unbounded. The same is true of an order larger than the position:
        the excess becomes a new naked position on the other side.

        Returns the quantity to submit, capped at the broker's current position
        and rounded down to the quantity step (#121 review).
        Capping rather than refusing is deliberate: the order's intent is to
        close what exists, and refusing would leave the exposure in place. The
        cap is logged because a size mismatch means the daemon's book moved
        under us.
        """
        if not (
            str((signal.metadata or {}).get("risk_sizing_mode") or "")
            == EXIT_ONLY_RISK_SIZING_MODE
        ):
            return qty
        symbol = str(signal.symbol or "").strip().upper()
        quantities = [
            quantity
            for position in await self.get_positions()
            if str(position.get("symbol") or "").strip().upper() == symbol
            and not _is_flat(quantity := _position_qty(position))
        ]
        if not quantities:
            raise ValueError(
                "Refusing Alpaca exit-only order for "
                f"{symbol} (action={signal.action}, signal_id={signal.id}): "
                "no live broker position to close. An exit-only order without a "
                "position would OPEN one, and without its mandatory SL/TP."
            )
        if any(quantity > 0 for quantity in quantities) and any(
            quantity < 0 for quantity in quantities
        ):
            raise ValueError(
                f"Refusing Alpaca exit-only order for {symbol}: the broker book "
                "holds mixed long and short positions, so no single close is "
                "unambiguous."
            )
        close_action = "sell" if quantities[0] > 0 else "buy"
        if signal.action != close_action:
            position_side = "long" if close_action == "sell" else "short"
            raise ValueError(
                f"Refusing Alpaca exit-only order for {symbol}: a {signal.action} "
                f"does not close a {position_side} position (its close side is "
                f"{close_action!r}); it would open or extend exposure instead of "
                "reducing it."
            )
        position_qty = abs(quantities[0])
        if qty > position_qty:
            logger.warning(
                "Capping Alpaca exit-only order to the broker position; the "
                "requested quantity exceeded it and the excess would open a new "
                "naked position",
                extra={
                    "data": {
                        "signal_id": str(signal.id),
                        "symbol": symbol,
                        "requested_qty": qty,
                        "broker_position_qty": position_qty,
                    }
                },
            )
            qty = position_qty
        return self._floor_exit_qty(symbol, qty)

    def _floor_exit_qty(self, symbol: str, qty: float) -> float:
        """An exit qty rounded DOWN to Alpaca's quantity step (#121 review).

        A partial of a fractional position (1/3 of 0.5 = 0.1666...) has more
        decimals than Alpaca accepts; rounding it to nearest could close more
        than asked. Raises :class:`ExitBelowStepError` below one step.
        """
        step = self.exit_qty_step(symbol)
        floored = round_exit_qty_down(qty, step)
        if floored <= 0.0:
            raise ExitBelowStepError(
                f"Alpaca exit for {symbol} ignored: the requested qty {qty:g} is below "
                f"the quantity step {step:g}.",
                requested_qty=qty,
                qty_step=step,
            )
        return floored

    # ── Exit on a protected position (#145) ─────────────────────────────────
    #
    # Every Alpaca entry is a bracket, and its stop/take-profit legs reserve the
    # position's shares: a separate exit order is refused with "insufficient
    # qty". An exit-only order therefore has to release the protection first.
    # The ordering is the fail-closed part:
    #
    # * Before the exit submit, any failure restores the protection for the
    #   *current* position and refuses the exit - the account never ends up
    #   with less protection than it had.
    # * After the submit, the remainder is re-protected at the levels the
    #   cancelled orders carried: the original SL/TP, or the current trailing
    #   level when trailing amended the broker stop (maintainer decision on
    #   #145). A failed re-place is a CRITICAL alert plus the persisted
    #   ``exit_reprotection`` marker, which ``connect`` replays.

    async def _symbol_position_qty(self, symbol: str) -> float | None:
        """The signed broker quantity for ``symbol``, ``None`` when flat."""
        for position in await self.get_positions():
            if str(position.get("symbol") or "").strip().upper() != symbol:
                continue
            qty = _position_qty(position)
            return None if _is_flat(qty) else qty
        return None

    def _collect_exit_protection(
        self,
        order: Any,
        *,
        symbol: str,
        side: str,
        found: dict[str, dict[str, Any]],
        ancestors: tuple[Any, ...] = (),
    ) -> None:
        """Record ``order`` (and its live legs) as exit protection, when it is.

        Only working orders on the reducing ``side`` of ``symbol`` count.
        Market orders are skipped: a working market sell/buy is an exit in
        flight, not bracket protection - it frees shares itself and must never
        be cancelled for one. Entry parents (``bracket``/``oto``/``mleg``) are
        skipped with their legs while they work: a pending entry protects
        nothing that is open yet. Legs are collected only under a *filled*
        parent (the held stop leg of a filled bracket) or a working ``oco``
        parent (the re-placed protection of an earlier partial exit, #145).
        Alpaca's OCO parent IS the take-profit order - the ``oco`` class marks
        the pair, the parent carries the limit price and the stop is its only
        leg - so the parent is a candidate like any other working order, and
        cancelling it cancels the pair.
        """
        order_class = _order_class_text(order)
        legs = _order_legs(order)
        if order_class not in _ENTRY_ORDER_CLASSES:
            self._add_exit_protection_candidate(
                order,
                symbol=symbol,
                side=side,
                found=found,
                ancestors=ancestors,
            )
        status = _order_status(order)
        if status != "filled" and order_class != "oco":
            return
        for leg in legs:
            self._collect_exit_protection(
                leg,
                symbol=symbol,
                side=side,
                found=found,
                ancestors=(*ancestors, order),
            )

    @staticmethod
    def _add_exit_protection_candidate(
        order: Any,
        *,
        symbol: str,
        side: str,
        found: dict[str, dict[str, Any]],
        ancestors: tuple[Any, ...] = (),
    ) -> None:
        parent = ancestors[-1] if ancestors else None
        item_symbol = _order_symbol(order)
        if not item_symbol and parent is not None:
            item_symbol = _order_symbol(parent)
        if item_symbol != symbol:
            return
        item_side = _order_side(order)
        if not item_side and parent is not None:
            item_side = _order_side(parent)
        if item_side != side:
            return
        if _order_status(order) in _TERMINAL_ORDER_STATUSES:
            return
        order_type = _order_type(order)
        if "market" in order_type:
            return
        order_id = _control_order_id(_order_attr(order, "id"))
        if order_id is None or order_id in found:
            return
        # A leg is cancelled through its own id; a still-working ancestor (the
        # OCO container an exit re-places, #145) goes too, while a terminal one
        # (a filled bracket parent) cannot be cancelled and is skipped.
        cancel_ids = {order_id}
        for ancestor in ancestors:
            if _order_status(ancestor) in _TERMINAL_ORDER_STATUSES:
                continue
            ancestor_id = _control_order_id(_order_attr(ancestor, "id"))
            if ancestor_id is not None:
                cancel_ids.add(ancestor_id)
        qty = _to_float(_order_attr(order, "qty", None), default=0.0)
        found[order_id] = {
            "order_id": order_id,
            "cancel_ids": cancel_ids,
            "order_type": order_type,
            "stop_price": _positive_finite(_order_attr(order, "stop_price", None)),
            "limit_price": _positive_finite(_order_attr(order, "limit_price", None)),
            "qty": qty if math.isfinite(qty) and qty > 0 else None,
        }

    async def _exit_protection_candidates(
        self, client: Any, symbol: str, side: str
    ) -> list[dict[str, Any]]:
        """Every working order reserving ``symbol``'s quantity on ``side``.

        The ``status=open`` listing sees top-level orders (a filled bracket's
        take-profit leg, a working OCO parent); the ``status=all`` listing of
        filled parents sees the ``held`` stop leg that no open listing returns
        (#174). Both are needed: missing one leaves quantity reserved and the
        exit rejected, or protection uncancelled and later orphaned.
        """
        orders = await self._list_open_orders(client, nested=True)
        orders.extend(
            await self._list_filled_parents_with_held_legs(client, {symbol})
        )
        found: dict[str, dict[str, Any]] = {}
        for order in orders:
            self._collect_exit_protection(
                order, symbol=symbol, side=side, found=found
            )
        return list(found.values())

    async def _exit_protection_plan(
        self, signal: LiveSignal, qty: float
    ) -> dict[str, Any] | None:
        """The protection an exit-only order must release, or ``None``.

        ``None`` means "submit exactly as before": the signal is not
        exit-only, there is no live position, or nothing reserves its
        quantity. A *partial* exit on protected quantity must be able to name
        the levels to re-place, so several distinct stop or take-profit prices
        - protection that cannot be attributed to one tranche - refuse the
        exit here, inside ``order_not_submitted``, before any broker write.
        """
        metadata = signal.metadata or {}
        if str(metadata.get("risk_sizing_mode") or "") != EXIT_ONLY_RISK_SIZING_MODE:
            return None
        symbol = str(signal.symbol or "").strip().upper()
        client = self._trading_client
        if client is None:
            return None
        position_qty = await self._symbol_position_qty(symbol)
        if position_qty is None:
            return None
        side = "sell" if position_qty > 0 else "buy"
        candidates = await self._exit_protection_candidates(client, symbol, side)
        if not candidates:
            return None
        entry_signal_id = await self._entry_signal_for_position(signal, symbol)
        plan: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "position_qty": abs(position_qty),
            "exit_qty": qty,
            "entry_signal_id": entry_signal_id,
            "reprotect_qty": max(abs(position_qty) - qty, 0.0),
            "order_ids": sorted(
                {cid for c in candidates for cid in c["cancel_ids"]}
            ),
            "stop_price": None,
            "stop_limit_price": None,
            "take_profit_price": None,
            # Every candidate verbatim: when the levels are ambiguous the
            # scalar fields above stay ``None``, but the recovery marker and
            # the restore path still know exactly which orders protected the
            # position and at which levels/quantities (#350 review).
            "protection": [
                {
                    "kind": _exit_protection_kind(c),
                    "order_type": c["order_type"],
                    "stop_price": c["stop_price"],
                    "limit_price": c["limit_price"],
                    "qty": c["qty"],
                }
                for c in candidates
            ],
        }
        stops = [c for c in candidates if _exit_protection_kind(c) == "stop"]
        take_profits = [c for c in candidates if _exit_protection_kind(c) == "tp"]
        unclassified = [
            c for c in candidates if _exit_protection_kind(c) is None
        ]
        if _is_flat(plan["reprotect_qty"]):
            # The exit covers the whole broker position: nothing is re-placed,
            # so ambiguous levels cannot hurt - but the recovery marker still
            # gets the one unambiguous level, never a guess. ``stop_limit_price``
            # is set here too: the in-lock signature always derives it from the
            # candidates, and leaving it ``None`` refused every full exit over
            # a stop-limit protection as "the working protection changed".
            plan["stop_price"] = _single_level([c["stop_price"] for c in stops])
            plan["stop_limit_price"] = _single_level(
                [c["limit_price"] for c in stops if c["limit_price"] is not None]
            )
            plan["take_profit_price"] = _single_level(
                [c["limit_price"] for c in take_profits]
            )
            return plan
        problems: list[str] = []
        if unclassified:
            problems.append(
                "unrecognised order type(s) "
                + ",".join(sorted({c["order_type"] or "?" for c in unclassified}))
            )
        for name, group, key in (
            ("stop_loss", stops, "stop_price"),
            ("take_profit", take_profits, "limit_price"),
        ):
            levels = [c[key] for c in group]
            if any(level is None for level in levels):
                problems.append(f"{name} level unreadable")
            distinct = _distinct_levels(levels)
            if len(distinct) > 1:
                problems.append(f"ambiguous {name} levels {sorted(distinct)}")
        if problems:
            raise ValueError(
                f"Refusing Alpaca partial exit for {symbol} "
                f"(signal_id={signal.id}): the working protection cannot be "
                "re-placed unambiguously ("
                + "; ".join(problems)
                + "). Nothing was cancelled and the exit was not submitted."
            )
        plan["stop_price"] = _single_level([c["stop_price"] for c in stops])
        plan["take_profit_price"] = _single_level(
            [c["limit_price"] for c in take_profits]
        )
        plan["stop_limit_price"] = _single_level(
            [c["limit_price"] for c in stops if c["limit_price"] is not None]
        )
        # The re-placed OCO/GTC legs reject fractional quantities, and entries
        # are whole shares: floor the exit so the remainder stays a whole-share
        # position the protection can cover. Refuse before any broker write
        # when flooring leaves less than one share to exit, or when the broker
        # position itself is fractional so no whole-share exit leaves a whole
        # remainder (#350 review).
        exit_qty = _floor_to_whole_shares(plan["exit_qty"])
        if exit_qty < 1.0:
            raise ValueError(
                f"Refusing Alpaca partial exit for {symbol} "
                f"(signal_id={signal.id}): the requested qty {qty:g} floors "
                "below one whole share, and the remainder could not be "
                "re-protected. Nothing was cancelled and the exit was not "
                "submitted."
            )
        remainder = plan["position_qty"] - exit_qty
        nearest = round(remainder)
        if not _is_flat(remainder) and (
            abs(remainder - nearest) > self.position_qty_tolerance(symbol)
            or nearest < 1.0
        ):
            raise ValueError(
                f"Refusing Alpaca partial exit for {symbol} "
                f"(signal_id={signal.id}): position {plan['position_qty']:g} "
                f"minus the whole-share exit {exit_qty:g} leaves a fractional "
                f"remainder {remainder:g} that Alpaca OCO/GTC orders cannot "
                "protect. Nothing was cancelled and the exit was not "
                "submitted."
            )
        plan["exit_qty"] = exit_qty
        plan["reprotect_qty"] = max(remainder, 0.0)
        return plan

    async def _entry_signal_for_position(
        self, signal: LiveSignal, symbol: str
    ) -> str | None:
        """The entry signal whose position this exit reduces, best effort.

        The re-protect OCO's ``client_order_id`` embeds it so a later broker-side
        close of the remainder can be booked against the parent entry's ledger
        (#277's ``record_live_broker_side_close_fill``) instead of becoming an
        unattributable fill. ``None`` when no filled entry signal is known -
        attribution is then simply absent, never guessed.

        For a position scaled in by several entries this picks the *latest*
        filled entry of the deployment: the deployment ledger is the same
        either way (fills are summed per deployment+symbol), but the signal id
        embedded in the client order id - and therefore the ``parent_signal_id``
        a broker-side close is booked against - is an arbitrary one of them.
        """
        deployment_id = str(getattr(signal, "deployment_id", "") or "").strip()
        if not deployment_id:
            return None
        try:
            found = await asyncio.to_thread(
                find_latest_filled_live_entry_signals, [deployment_id], [symbol]
            )
        except Exception as exc:
            logger.debug(
                "Alpaca exit re-protection: entry-signal lookup failed for %s: %r",
                symbol,
                exc,
            )
            return None
        entry = found.get(symbol)
        return str(entry.get("signal_id")) if entry else None

    async def _working_ids_for_symbol(self, client: Any, symbol: str) -> set[str]:
        """Ids of every working order and leg on ``symbol`` (post-cancel re-read).

        Both listings are needed: the ``status=open`` page sees top-level orders
        and OCO legs, the ``status=all`` page of filled parents sees a held
        bracket stop leg that no open listing returns (#174).
        """
        orders = await self._list_open_orders(client, nested=True)
        orders.extend(
            await self._list_filled_parents_with_held_legs(client, {symbol})
        )
        ids: set[str] = set()
        for order in orders:
            for item in (order, *_order_legs(order)):
                order_id = _control_order_id(_order_attr(item, "id"))
                if order_id is not None:
                    ids.add(order_id)
        return ids

    def _load_exit_order_types(self) -> tuple[Any, Any, Any, Any, Any]:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import (
                LimitOrderRequest,
                StopLimitOrderRequest,
                StopOrderRequest,
            )
        except ImportError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        return (
            OrderSide,
            TimeInForce,
            LimitOrderRequest,
            StopOrderRequest,
            StopLimitOrderRequest,
        )

    def _exit_protection_request(
        self, plan: Mapping[str, Any], qty: float, *, signal_id: str | None
    ) -> Any | None:
        """The order re-protecting ``qty`` shares at the plan's recorded levels.

        SL+TP together go as ONE ``oco`` order: it is Alpaca's order class for
        an exit pair on an existing position, so the two legs share the quantity
        reservation exactly like the bracket they replace - two standalone
        orders would each reserve ``qty`` and be refused. A lone stop or take
        profit (the protection that existed had only that leg) goes as a plain
        order. ``GTC`` keeps the protection working past the session, never
        less protected than the bracket it replaces.
        """
        order_side, time_in_force, limit_request_cls, stop_request_cls, stop_limit_request_cls = (
            self._load_exit_order_types()
        )
        side = order_side.SELL if plan["side"] == "sell" else order_side.BUY
        stop_price = plan.get("stop_price")
        take_profit_price = plan.get("take_profit_price")
        # ``{entry_signal_id}-reprotect-{exit_signal_id}`` is the attribution
        # contract for broker-side closes of the remainder: whichever side
        # merges second (#350 vs #277) parses the entry id out of the
        # client_order_id and books the leg fill against that parent signal's
        # ledger instead of dropping it as unattributable. The exit id keeps
        # the key unique across repeated partial exits of one position.
        entry_signal_id = plan.get("entry_signal_id")
        if signal_id and entry_signal_id:
            client_order_id = f"{entry_signal_id}-reprotect-{signal_id}"
        elif signal_id:
            client_order_id = f"{signal_id}-reprotect"
        elif entry_signal_id:
            client_order_id = f"{entry_signal_id}-reprotect"
        else:
            client_order_id = None
        if stop_price is not None and take_profit_price is not None:
            stop_loss_kwargs: dict[str, Any] = {"stop_price": stop_price}
            if plan.get("stop_limit_price") is not None:
                stop_loss_kwargs["limit_price"] = plan["stop_limit_price"]
            # For OCO the exit limit price lives on take_profit.limit_price;
            # the request-level limit_price is optional and left unset.
            return limit_request_cls(
                symbol=plan["symbol"],
                qty=qty,
                side=side,
                time_in_force=time_in_force.GTC,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
                stop_loss=StopLossRequest(**stop_loss_kwargs),
                client_order_id=client_order_id,
            )
        if stop_price is not None:
            stop_kwargs: dict[str, Any] = {
                "symbol": plan["symbol"],
                "qty": qty,
                "side": side,
                "time_in_force": time_in_force.GTC,
                "stop_price": stop_price,
                "client_order_id": client_order_id,
            }
            if plan.get("stop_limit_price") is not None:
                # A lone stop-limit leg goes back as a stop-limit, not a plain
                # stop: re-placing it as a stop would widen the trigger into a
                # market order the original protection never was.
                return stop_limit_request_cls(
                    **stop_kwargs, limit_price=plan["stop_limit_price"]
                )
            return stop_request_cls(**stop_kwargs)
        if take_profit_price is not None:
            return limit_request_cls(
                symbol=plan["symbol"],
                qty=qty,
                side=side,
                time_in_force=time_in_force.GTC,
                order_class=OrderClass.SIMPLE,
                limit_price=take_profit_price,
                client_order_id=client_order_id,
            )
        return None

    async def _place_exit_protection(
        self,
        client: Any,
        plan: Mapping[str, Any],
        *,
        qty: float,
        signal_id: str | None,
        reason: str,
    ) -> bool:
        """Place the recorded SL/TP for ``qty`` shares; ``True`` when complete.

        A missing stop is CRITICAL - live unprotected exposure, the same
        severity ``_restore_stop`` uses. A missing take profit alone is ERROR:
        the position keeps its stop, but the exit the strategy chose no longer
        has its target. Either way the persisted marker stays, so the next
        ``connect`` recovery retries what is still missing.
        """
        if _is_flat(qty):
            return True
        request = self._exit_protection_request(plan, qty, signal_id=signal_id)
        if request is None:
            logger.error(
                "Alpaca exit re-protection for %s has no recorded levels "
                "(%s); the remainder's protection cannot be restored",
                plan["symbol"],
                reason,
                extra={"data": {"symbol": plan["symbol"], "qty": qty, "reason": reason}},
            )
            return False
        try:
            order = await asyncio.to_thread(client.submit_order, order_data=request)
        except Exception as exc:
            if plan.get("stop_price") is not None:
                logger.critical(
                    "Alpaca exit re-protection failed for %s; the remaining "
                    "position is UNPROTECTED (%s)",
                    plan["symbol"],
                    reason,
                    extra={
                        "data": {
                            "symbol": plan["symbol"],
                            "qty": qty,
                            "stop_price": plan.get("stop_price"),
                            "take_profit_price": plan.get("take_profit_price"),
                            "error": repr(exc),
                        }
                    },
                )
            else:
                logger.error(
                    "Alpaca exit re-protection failed for %s; the remaining "
                    "position keeps no take-profit leg (%s)",
                    plan["symbol"],
                    reason,
                    extra={
                        "data": {
                            "symbol": plan["symbol"],
                            "qty": qty,
                            "error": repr(exc),
                        }
                    },
                )
            return False
        # #350 on #277: the re-placed protection's fills are broker-side closes
        # of the remainder - learn their order ids now so a leg fill reaching
        # the stream before the next connect is booked against the entry's
        # ledger instead of dropped as unattributable.
        await self._register_reprotect_attribution(
            client,
            plan,
            order,
            nested_lookup=(
                getattr(request, "take_profit", None) is not None
                and getattr(request, "stop_loss", None) is not None
            ),
        )
        logger.info(
            "Re-placed Alpaca exit protection",
            extra={
                "data": {
                    "symbol": plan["symbol"],
                    "side": plan["side"],
                    "qty": qty,
                    "stop_price": plan.get("stop_price"),
                    "take_profit_price": plan.get("take_profit_price"),
                    "reason": reason,
                }
            },
        )
        return True

    async def _restore_exit_protection(
        self,
        client: Any,
        plan: Mapping[str, Any],
        *,
        reason: str,
        signal_id: str | None = None,
    ) -> bool:
        """Put the recorded protection back on the *current* position.

        Used when the exit was refused after its protection was already
        cancelled: the position is still whole, so the stop and take profit are
        re-placed for whatever quantity the broker reports now.
        """
        try:
            current_qty = await self._symbol_position_qty(str(plan["symbol"]))
        except Exception as exc:  # pragma: no cover - defensive
            logger.critical(
                "Alpaca exit %s: could not re-read the position to restore its "
                "protection; the position may be unprotected",
                reason,
                extra={"data": {"symbol": plan.get("symbol"), "error": repr(exc)}},
            )
            return False
        if current_qty is None:
            logger.warning(
                "Alpaca exit %s: the position is already flat; nothing to restore",
                reason,
                extra={"data": {"symbol": plan.get("symbol")}},
            )
            return True
        if plan.get("protection"):
            return await self._restore_recorded_candidates(
                client,
                plan,
                position_qty=abs(current_qty),
                reason=reason,
                signal_id=signal_id,
            )
        return await self._place_exit_protection(
            client,
            plan,
            qty=abs(current_qty),
            signal_id=signal_id,
            reason=reason,
        )

    async def _restore_recorded_candidates(
        self,
        client: Any,
        plan: Mapping[str, Any],
        *,
        position_qty: float,
        reason: str,
        signal_id: str | None,
    ) -> bool:
        """Re-place every recorded protection candidate on the current position.

        Used when the plan's scalar levels are ambiguous (pyramided entries
        working several stops or take-profits): each candidate goes back at
        its own level and tranche quantity. A stop and a take-profit covering
        the same shares must share ONE order's quantity reservation - two
        standalone orders would each reserve the full tranche and the second
        would be refused - so candidates are paired by quantity into ``oco``
        orders; an unpaired candidate goes back as a lone order while the
        position's reservation budget lasts. A stop that cannot be rebuilt is
        CRITICAL: the position would otherwise sit unprotected.
        """
        symbol = str(plan["symbol"])
        side = str(plan["side"])
        budget = abs(position_qty)
        placed = True

        def _entry_qty(entry: Mapping[str, Any], partner: Mapping[str, Any] | None) -> float | None:
            for source in (entry, partner):
                if source is None:
                    continue
                qty = source.get("qty")
                if isinstance(qty, (int, float)) and math.isfinite(qty) and qty > 0:
                    return float(qty)
            return None

        entries = [
            dict(item)
            for item in (plan.get("protection") or [])
            if isinstance(item, Mapping)
        ]
        stops = [e for e in entries if e.get("kind") == "stop"]
        take_profits = [e for e in entries if e.get("kind") == "tp"]
        # Greedy quantity pairing: each stop claims the unused take-profit
        # closest to its own tranche size, so equal-size pyramided tranches
        # rebuild as the OCO pairs they were.
        pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        lone_tps: list[Mapping[str, Any]] = []
        for stop in stops:
            best_index = None
            best_diff = math.inf
            stop_qty = _entry_qty(stop, None)
            for index, tp in enumerate(take_profits):
                if tp.get("_paired"):
                    continue
                tp_qty = _entry_qty(tp, None)
                if stop_qty is None or tp_qty is None:
                    diff = 0.0
                else:
                    diff = abs(stop_qty - tp_qty)
                if diff < best_diff:
                    best_diff = diff
                    best_index = index
            if best_index is None:
                continue
            tp = take_profits[best_index]
            tp["_paired"] = True
            pairs.append((stop, tp))
        lone_tps = [tp for tp in take_profits if not tp.get("_paired")]

        async def _submit_one(
            sub_plan: dict[str, Any], qty: float, *, index: int, has_stop: bool
        ) -> bool:
            nonlocal budget
            # Each restored order needs its own client order id: the
            # ``-reprotect`` suffix still leads with the entry UUID, so
            # attribution survives the index.
            tag = f"{signal_id}-{index}" if signal_id else f"restore-{index}"
            request = self._exit_protection_request(
                sub_plan, qty, signal_id=tag
            )
            if request is None:
                (logger.critical if has_stop else logger.error)(
                    "Alpaca exit %s: recorded protection for %s cannot be "
                    "rebuilt (no usable level); the position may be unprotected",
                    reason,
                    symbol,
                    extra={
                        "data": {
                            "symbol": symbol,
                            "qty": qty,
                            "plan": dict(sub_plan),
                        }
                    },
                )
                return False
            try:
                order = await asyncio.to_thread(client.submit_order, order_data=request)
            except Exception as exc:
                (logger.critical if has_stop else logger.error)(
                    "Alpaca exit %s: restoring recorded protection for %s "
                    "failed (%r); the position may be unprotected",
                    reason,
                    symbol,
                    exc,
                    extra={
                        "data": {
                            "symbol": symbol,
                            "qty": qty,
                            "stop_price": sub_plan.get("stop_price"),
                            "take_profit_price": sub_plan.get("take_profit_price"),
                        }
                    },
                )
                return False
            await self._register_reprotect_attribution(
                client,
                sub_plan,
                order,
                nested_lookup=(
                    getattr(request, "take_profit", None) is not None
                    and getattr(request, "stop_loss", None) is not None
                ),
            )
            budget = max(budget - qty, 0.0)
            return True

        index = 0
        for stop, tp in pairs:
            stop_qty = _entry_qty(stop, tp)
            tp_qty = _entry_qty(tp, stop)
            qtys = [q for q in (stop_qty, tp_qty) if q is not None]
            # The tranche's recorded size is capped by what the position's
            # reservation budget still holds: the position may have shrunk
            # since the candidates were recorded.
            qty = min(min(qtys) if qtys else budget, budget)
            if qty <= 0:
                logger.critical(
                    "ALERT: Alpaca exit %s: recorded protection tranche for %s "
                    "has no reservation budget left; it is NOT restored",
                    reason,
                    symbol,
                    extra={"data": {"symbol": symbol, "budget": budget}},
                )
                placed = False
                continue
            sub_plan = {
                "symbol": symbol,
                "side": side,
                "stop_price": stop.get("stop_price"),
                "stop_limit_price": (
                    stop.get("limit_price")
                    if "stop" in str(stop.get("order_type") or "")
                    else None
                ),
                "take_profit_price": tp.get("limit_price"),
                "entry_signal_id": plan.get("entry_signal_id"),
            }
            index += 1
            if not await _submit_one(sub_plan, qty, index=index, has_stop=True):
                placed = False
        lone_stops = [
            e
            for e in entries
            if e.get("kind") == "stop"
            and all(e is not s for s, _tp in pairs)
        ]
        for entry, has_stop in [(e, True) for e in lone_stops] + [
            (e, False) for e in lone_tps
        ]:
            entry_qty = _entry_qty(entry, None)
            qty = min(entry_qty if entry_qty is not None else budget, budget)
            if qty <= 0:
                (logger.critical if has_stop else logger.error)(
                    "ALERT: Alpaca exit %s: recorded %s protection for %s "
                    "has no reservation budget left; it is NOT restored",
                    reason,
                    "stop" if has_stop else "take-profit",
                    symbol,
                    extra={"data": {"symbol": symbol, "budget": budget}},
                )
                placed = False
                continue
            if entry.get("kind") == "stop":
                sub_plan = {
                    "symbol": symbol,
                    "side": side,
                    "stop_price": entry.get("stop_price"),
                    "stop_limit_price": entry.get("limit_price"),
                    "take_profit_price": None,
                    "entry_signal_id": plan.get("entry_signal_id"),
                }
            else:
                sub_plan = {
                    "symbol": symbol,
                    "side": side,
                    "stop_price": None,
                    "stop_limit_price": None,
                    "take_profit_price": entry.get("limit_price"),
                    "entry_signal_id": plan.get("entry_signal_id"),
                }
            index += 1
            if not await _submit_one(sub_plan, qty, index=index, has_stop=has_stop):
                placed = False
        for entry in entries:
            if entry.get("kind") not in {"stop", "tp"}:
                logger.error(
                    "Alpaca exit %s: recorded protection candidate for %s has "
                    "unrecognised kind %r; it cannot be rebuilt",
                    reason,
                    symbol,
                    entry.get("kind"),
                    extra={"data": {"symbol": symbol, "entry": dict(entry)}},
                )
                placed = False
        return placed

    async def _submit_exit_releasing_protection(
        self,
        client: Any,
        request: Any,
        plan: dict[str, Any],
        *,
        signal_id: str,
    ) -> Any:
        """Cancel the protection, submit the exit, re-protect the remainder.

        Runs under ``_order_book_lock`` so a flatten or orphan sweep cannot
        interleave with the release. Every failure before the exit submit
        restores the protection for the current position and re-raises (the
        exit is then refused, never half-done); a failure to re-place after a
        successful submit alerts CRITICAL and leaves the persisted marker for
        the next ``connect``.
        """
        symbol = plan["symbol"]
        async with self._order_book_lock():
            # The plan was computed before the signal row was persisted, outside
            # this lock: trailing may have replaced the stop, or a leg changed,
            # since. Re-collect inside the lock and refuse BEFORE the first
            # cancel when the book no longer matches the plan - cancelling an
            # order the plan does not describe could leave the position with
            # less protection than the restore path knows how to rebuild.
            fresh_candidates = await self._exit_protection_candidates(
                client, symbol, plan["side"]
            )
            fresh_signature = _exit_protection_signature(fresh_candidates)
            planned_signature = (
                list(plan["order_ids"]),
                plan.get("stop_price"),
                plan.get("stop_limit_price"),
                plan.get("take_profit_price"),
            )
            if fresh_signature != planned_signature:
                raise RuntimeError(
                    f"Alpaca exit for {symbol}: the working protection changed "
                    "while the exit was being prepared "
                    f"(planned {planned_signature}, now {fresh_signature}); "
                    "refusing the exit order before any cancel."
                )
            try:
                cancel_errors: list[str] = []
                for order_id in plan["order_ids"]:
                    try:
                        await self._cancel_one(client, order_id)
                    except Exception as exc:
                        # A parent cancel can cascade onto legs already gone;
                        # the re-read below - not this error - decides whether
                        # anything still reserves the quantity.
                        cancel_errors.append(f"{order_id}:{exc}")
                still_working = sorted(
                    set(plan["order_ids"])
                    & await self._working_ids_for_symbol(client, symbol)
                )
                if still_working:
                    raise RuntimeError(
                        f"Alpaca exit for {symbol}: protection orders still "
                        f"working after cancel: {still_working} "
                        f"(cancel errors: {cancel_errors or 'none'})"
                    )
                current_qty = await self._symbol_position_qty(symbol)
                same_side = current_qty is not None and (
                    (current_qty > 0) == (plan["side"] == "sell")
                )
                if (
                    not same_side
                    or abs(abs(current_qty) - plan["position_qty"])
                    > self.position_qty_tolerance(symbol)
                ):
                    raise RuntimeError(
                        f"Alpaca exit for {symbol}: the position changed while "
                        "its protection was being released "
                        f"(was {plan['position_qty']:g}, now "
                        f"{current_qty!r}); refusing the exit order."
                    )
            except Exception as exc:
                await self._restore_exit_protection(
                    client, plan, reason=f"release failed: {exc}", signal_id=signal_id
                )
                raise
            try:
                order = await asyncio.to_thread(
                    client.submit_order, order_data=request
                )
            except Exception as exc:
                # An HTTP rejection proves the broker never took the order; a
                # timeout or connection error does not - the exit may be
                # working. The restore protects whatever position remains
                # either way, but the log must not claim a clean refusal when
                # the outcome is unknown.
                reason = f"exit submit failed: {exc}"
                if not _is_definite_broker_rejection(exc):
                    reason = (
                        "exit submit outcome UNKNOWN - the broker may have "
                        f"accepted the order ({exc})"
                    )
                    logger.warning(
                        "Alpaca exit submit outcome is unknown for %s "
                        "(signal_id=%s); restoring protection for the current "
                        "position and surfacing the ambiguity",
                        symbol,
                        signal_id,
                        extra={"data": {"symbol": symbol, "error": repr(exc)}},
                    )
                await self._restore_exit_protection(
                    client, plan, reason=reason, signal_id=signal_id
                )
                raise
            status = _order_status(order)
            if status in {"rejected", "canceled", "expired", "done_for_day", "replaced"}:
                await self._restore_exit_protection(
                    client,
                    plan,
                    reason=f"exit order {status} on submission",
                    signal_id=signal_id,
                )
                raise RuntimeError(
                    f"Alpaca exit order for {symbol} was {status} on submission "
                    f"(signal_id={signal_id})."
                )
            replaced = await self._place_exit_protection(
                client,
                plan,
                qty=plan["reprotect_qty"],
                signal_id=signal_id,
                reason="partial exit",
            )
            if replaced:
                await _clear_exit_reprotection_marker(
                    signal_id, symbol, "re-protection placed"
                )
            return order

    async def _recover_exit_reprotection(self, row: Mapping[str, Any]) -> None:
        """Replay one persisted ``exit_reprotection`` marker, if still needed."""
        signal_id = str(row.get("signal_id") or "")
        symbol = str(row.get("symbol") or "").strip().upper()
        marker = row.get("marker") or {}
        side = str(marker.get("side") or "")
        if not symbol or side not in {"buy", "sell"}:
            logger.error(
                "Alpaca exit re-protection marker is unusable; clearing it - "
                "it can never be replayed",
                extra={"data": {"signal_id": signal_id, "symbol": symbol}},
            )
            await _clear_exit_reprotection_marker(
                signal_id, symbol or "?", "unusable marker"
            )
            return
        position_qty = await self._symbol_position_qty(symbol)
        if position_qty is None:
            # Flat: the exit completed and nothing is left to protect.
            await _clear_exit_reprotection_marker(
                signal_id, symbol, "position flat"
            )
            return
        reducing_side = "sell" if position_qty > 0 else "buy"
        if reducing_side != side:
            logger.error(
                "ALERT: Alpaca exit re-protection marker for %s names side %s "
                "but the open position is on the other side; the marker is "
                "stale and cleared - reconcile manually if a remainder exists",
                symbol,
                side,
                extra={"data": {"signal_id": signal_id, "symbol": symbol}},
            )
            await _clear_exit_reprotection_marker(
                signal_id, symbol, "side mismatch"
            )
            return
        client = self._trading_client
        if client is None:
            raise RuntimeError("Alpaca trading client is unavailable")
        candidates = await self._exit_protection_candidates(client, symbol, side)
        kinds = {_exit_protection_kind(c) for c in candidates}
        recorded = [
            dict(item)
            for item in (marker.get("protection") or [])
            if isinstance(item, Mapping)
        ]
        if recorded:
            # The marker kept every candidate verbatim (ambiguous levels):
            # recovery works at kind granularity like the scalar path - a
            # working kind counts however its price moved (trailing), and a
            # partially present book is alerted, never guessed at.
            needed = {
                item.get("kind") for item in recorded
                if item.get("kind") in {"stop", "tp"}
            }
            if not needed:
                logger.error(
                    "ALERT: Alpaca exit re-protection marker for %s recorded "
                    "protection candidates with no recognisable kind; the "
                    "position's protection cannot be verified or rebuilt - "
                    "reconcile manually. The marker is cleared",
                    symbol,
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "symbol": symbol,
                            "marker": dict(marker),
                        }
                    },
                )
                await _clear_exit_reprotection_marker(
                    signal_id, symbol, "unrecognised recorded protection"
                )
                return
            missing = needed - kinds
            if not missing:
                await _clear_exit_reprotection_marker(
                    signal_id, symbol, "protection already present"
                )
                return
            if needed & kinds:
                logger.error(
                    "ALERT: Alpaca exit re-protection for %s is incomplete at "
                    "restart (missing %s while sibling protection works); "
                    "the remainder is partially protected - reconcile manually",
                    symbol,
                    ",".join(sorted(missing)),
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "symbol": symbol,
                            "marker": dict(marker),
                        }
                    },
                )
                return
            plan = {
                "symbol": symbol,
                "side": side,
                "entry_signal_id": marker.get("entry_signal_id"),
                "protection": recorded,
            }
            placed = await self._restore_recorded_candidates(
                client,
                plan,
                position_qty=abs(position_qty),
                reason="restart recovery",
                signal_id=signal_id,
            )
            if placed:
                await _clear_exit_reprotection_marker(
                    signal_id, symbol, "recovery re-placed protection"
                )
            return
        missing_stop = (
            marker.get("stop_price") is not None and "stop" not in kinds
        )
        missing_tp = (
            marker.get("take_profit_price") is not None and "tp" not in kinds
        )
        if not missing_stop and not missing_tp:
            if not kinds and (
                marker.get("stop_price") is None
                and marker.get("take_profit_price") is None
            ):
                # The marker never recorded levels (an ambiguous full exit)
                # and nothing protects the position now: there is nothing
                # this recovery can place, so alert and leave it for review.
                logger.error(
                    "ALERT: Alpaca exit re-protection marker for %s recorded "
                    "no levels and the position has no working protection; "
                    "reconcile manually. The marker is cleared - it can never "
                    "place anything and would otherwise pile up ahead of "
                    "recoverable markers",
                    symbol,
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "symbol": symbol,
                            "marker": dict(marker),
                        }
                    },
                )
                await _clear_exit_reprotection_marker(
                    signal_id, symbol, "no recorded levels"
                )
                return
            # Whatever protection exists satisfies the marker: a stop at a
            # different price (trailing moved it) still counts - recovery
            # never moves a live stop back to a recorded level.
            await _clear_exit_reprotection_marker(
                signal_id, symbol, "protection already present"
            )
            return
        if (missing_stop or missing_tp) and ("stop" in kinds or "tp" in kinds):
            # Exactly one leg is missing while its sibling works: adding it as
            # a standalone order would double-reserve the quantity, and
            # replacing the sibling to rebuild an OCO would open a protection
            # gap. Alert instead of guessing; the operator decides.
            logger.error(
                "ALERT: Alpaca exit re-protection for %s is incomplete at "
                "restart (missing %s while the sibling leg works); the "
                "remainder is partially protected - reconcile manually",
                symbol,
                "stop" if missing_stop else "take-profit",
                extra={
                    "data": {
                        "signal_id": signal_id,
                        "symbol": symbol,
                        "marker": dict(marker),
                    }
                },
            )
            return
        plan = {
            "symbol": symbol,
            "side": side,
            "stop_price": marker.get("stop_price"),
            "stop_limit_price": marker.get("stop_limit_price"),
            "take_profit_price": marker.get("take_profit_price"),
            "entry_signal_id": marker.get("entry_signal_id"),
        }
        placed = await self._place_exit_protection(
            client,
            plan,
            qty=abs(position_qty),
            signal_id=signal_id,
            reason="restart recovery",
        )
        if placed:
            await _clear_exit_reprotection_marker(
                signal_id, symbol, "recovery re-placed protection"
            )

    async def _recover_exit_reprotections(self) -> None:
        """Replay persisted exit re-protection markers after a restart (#145).

        A crash between an exit's protection release and its re-place leaves a
        remainder that may be unprotected; the marker stamped before the first
        broker write records the levels to restore. Only the newest marker per
        symbol is replayed, protection is only ever *added* (a live stop at a
        different price - e.g. moved by trailing - is never moved back), and a
        marker whose position is flat is spent.
        """
        try:
            rows = await asyncio.to_thread(
                list_pending_exit_reprotections, self.account_id
            )
        except Exception as exc:
            logger.warning(
                "Alpaca exit re-protection recovery could not list markers; "
                "they stay pending for the next connect",
                extra={"data": {"account_id": self.account_id, "error": repr(exc)}},
            )
            return
        by_symbol: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                by_symbol.setdefault(symbol, row)
        for symbol, row in by_symbol.items():
            try:
                await self._recover_exit_reprotection(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.critical(
                    "Alpaca exit re-protection recovery failed for %s; the "
                    "remainder may be unprotected",
                    symbol,
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "signal_id": row.get("signal_id"),
                            "symbol": symbol,
                            "error": repr(exc),
                        }
                    },
                )

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        # Everything up to and including the request build is provably before
        # the broker sees an order, so a failure here is marked and the caller
        # can safely retry instead of treating the outcome as unknown. The
        # `trading_client.submit_order` call below is deliberately outside.
        with order_not_submitted():
            if not self._connected or self._trading_client is None:
                await self.connect()
            trading_client = self._trading_client
            if trading_client is None:
                raise RuntimeError("Alpaca trading client is unavailable after connect.")

            # B-8: validate the cheap in-memory facts BEFORE the first persistence.
            # A signal row persisted without a broker order wedges every redelivery
            # on the duplicate-persisted recovery path.
            if signal.action not in {"buy", "sell"}:
                raise ValueError(f"Unsupported action '{signal.action}'")

            # C1/B2: size exactly once upstream (deployment RiskConfig); the
            # incoming qty is binding. No default-config sizing here — sizing
            # with defaults silently diverged from the deployment policy, and
            # max(float(qty), 0.0) let NaN through to MarketOrderRequest.
            if qty is None:
                raise ValueError(
                    "Alpaca submit_order requires an explicit risk-sized qty; "
                    "size exactly once upstream and pass it as binding "
                    f"(signal_id={signal.id}, symbol={signal.symbol})."
                )
            try:
                qty_value = coerce_positive_live_qty(qty, field_name="Alpaca order qty")
            except ValueError as exc:
                raise ValueError(
                    "Alpaca order qty must be finite and positive, "
                    f"got {qty!r} (signal_id={signal.id}, symbol={signal.symbol})."
                ) from exc
            qty = qty_value

            # Must precede save_live_signal: an exit-only order with no position to
            # close is refused before the signal row is persisted, and an oversized
            # one is capped so it cannot open a new naked position.
            qty = await self._require_exit_position(signal, qty)

            # #145: bracket legs reserve the position's shares, so an exit on a
            # protected position is rejected with "insufficient qty". Discover
            # the protection now (read-only) and stamp the recovery marker into
            # the persisted metadata BEFORE any broker write: a crash between
            # the release and the re-place is then replayable on connect.
            exit_plan = await self._exit_protection_plan(signal, qty)
            if exit_plan is not None:
                planned_exit_qty = exit_plan.get("exit_qty")
                if planned_exit_qty is not None and planned_exit_qty != qty:
                    # The plan floored the exit to whole shares so the
                    # remainder stays re-protectable; the submitted qty, the
                    # persisted metadata and the marker all carry the floored
                    # value, exactly like a bracket entry's normalized qty.
                    logger.info(
                        "Floored Alpaca exit qty to whole shares so the "
                        "remainder stays protectable",
                        extra={
                            "data": {
                                "signal_id": str(signal.id),
                                "symbol": signal.symbol,
                                "requested_qty": qty,
                                "submitted_qty": planned_exit_qty,
                            }
                        },
                    )
                    qty = float(planned_exit_qty)
                metadata = dict(signal.metadata or {})
                metadata[EXIT_REPROTECTION_METADATA_KEY] = {
                    key: exit_plan[key]
                    for key in (
                        "side",
                        "stop_price",
                        "stop_limit_price",
                        "take_profit_price",
                        "position_qty",
                        "exit_qty",
                        "reprotect_qty",
                        "entry_signal_id",
                        "protection",
                    )
                }
                # The persisted qty is the one submitted (the plan may have
                # floored it to whole shares), same invariant bracket entries
                # keep through _normalize_bracket_entry.
                metadata["qty"] = float(qty)
                metadata["explicit_qty"] = float(qty)
                signal = signal.model_copy(update={"metadata": metadata})

            order_side, time_in_force, market_request_cls = (
                self._load_alpaca_order_types()
            )

            signal, qty = self._normalize_bracket_entry(signal, qty)

            canonical_signal_id, created = await asyncio.to_thread(
                save_live_signal, signal
            )
            if not created:
                return await self._recover_existing_signal_order(
                    trading_client, canonical_signal_id
                )

            request = self._build_order_request(
                signal,
                qty=qty,
                order_side=order_side,
                time_in_force=time_in_force,
                market_request_cls=market_request_cls,
                client_order_id=canonical_signal_id,
            )
        if exit_plan is None:
            order = await asyncio.to_thread(trading_client.submit_order, order_data=request)
        else:
            order = await self._submit_exit_releasing_protection(
                trading_client, request, exit_plan, signal_id=canonical_signal_id
            )
        # #144: a bracket's legs are child orders with their own broker order ids, and Alpaca
        # reports them nested in the parent it just created. Learning them here means a leg's
        # fill is attributed to this signal even if it arrives before the first reconciliation
        # pass (which runs on connect).
        self._register_bracket_legs(canonical_signal_id, order)
        broker_order_id = _normalize_broker_order_id(getattr(order, "id", None))
        if broker_order_id is None:
            raise RuntimeError(
                "Alpaca order response carried no valid positive broker order id "
                f"(signal_id={signal.id}, symbol={signal.symbol})."
            )
        await self._persist_submitted_order_id(canonical_signal_id, broker_order_id)
        logger.info(
            "Submitted Alpaca live order",
            extra={
                "data": {
                    "signal_id": canonical_signal_id,
                    "broker_order_id": broker_order_id,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "qty": qty,
                }
            },
        )
        return broker_order_id

    async def get_balance(self) -> tuple[float, float]:
        if not self._connected or self._trading_client is None:
            await self.connect()
        account = await asyncio.to_thread(self._trading_client.get_account)
        balance = _to_float(getattr(account, "cash", None), default=0.0)
        equity = _to_float(getattr(account, "equity", None), default=balance)
        return balance, equity

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        """One share (or coin) moves the USD account by 1.0 per 1.0 USD move (#146).

        Alpaca qty is in shares/coins and every supported symbol is quoted in
        USD, so the value is 1.0 once the account itself is in USD. Refuses
        (the daemon then refuses the entry) when the account currency is not
        USD, or the symbol is a pair quoted in anything else (``ETH/BTC``,
        ``BTC/USDT``): those would need a conversion this adapter does not do.
        """
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required for an Alpaca pip value")
        quote_asset = _alpaca_quote_asset(normalized_symbol)
        if quote_asset != "USD":
            raise ValueError(
                f"Alpaca symbol {normalized_symbol} is quoted in {quote_asset}, not USD; "
                "refusing to size it without a conversion rate."
            )
        if not self._connected or self._trading_client is None:
            await self.connect()
        account = await asyncio.to_thread(self._trading_client.get_account)
        currency = str(getattr(account, "currency", "") or "").strip().upper()
        if currency != "USD":
            raise ValueError(
                f"Alpaca account currency is {currency or 'unknown'!r}, not USD; "
                "refusing to size with a 1.0 USD pip value."
            )
        return LivePipValue(
            value=1.0,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="alpaca_usd_unit",
        )

    async def get_market_data(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Recent bars, refusing to return a shorter history than requested.

        A short history is not cosmetic: indicators would be computed over fewer
        bars than the backtest used, so the live strategy would quietly behave
        differently from the one that was validated.
        """
        from alpaca.common.enums import Sort
        from alpaca.data.requests import StockBarsRequest

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required for Alpaca market data")
        # Raises UnclassifiableSymbolError (a ValueError) rather than guessing the feed.
        is_crypto = classify_symbol(normalized_symbol) == "crypto"
        if is_crypto:
            normalized_symbol = canonical_crypto_symbol(normalized_symbol)
        resolved_timeframe, _normalized = _alpaca_timeframe(timeframe)
        bounded_limit = max(1, min(int(limit), 1000))
        if not self._connected or self._historical_data_client is None:
            raise RuntimeError(
                "Alpaca adapter must be connected before requesting market data"
            )

        def _fetch_crypto(slack_multiplier: float) -> pd.DataFrame:
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest

            if self._crypto_data_client is None:
                self._crypto_data_client = CryptoHistoricalDataClient(
                    self.api_key, self.secret_key
                )
            request = CryptoBarsRequest(
                symbol_or_symbols=[normalized_symbol],
                timeframe=resolved_timeframe,
                start=_alpaca_market_data_start(
                    timeframe, bounded_limit, slack_multiplier=slack_multiplier
                ),
                limit=1000,
                sort=Sort.DESC,
            )
            return self._crypto_data_client.get_crypto_bars(request).df

        def _fetch(slack_multiplier: float) -> pd.DataFrame:
            if is_crypto:
                return _fetch_crypto(slack_multiplier)
            request = StockBarsRequest(
                symbol_or_symbols=[normalized_symbol],
                timeframe=resolved_timeframe,
                start=_alpaca_market_data_start(
                    timeframe, bounded_limit, slack_multiplier=slack_multiplier
                ),
                limit=1000,
                adjustment="raw",
                feed="iex",
                # Newest first. The response is capped at `limit`, so an
                # ascending request over a widened window would return the
                # oldest bars in it. Sorted back to ascending below.
                sort=Sort.DESC,
            )
            return self._historical_data_client.get_stock_bars(request).df

        frame = await asyncio.to_thread(_fetch, 1.0)
        if frame is not None and len(frame.index) < bounded_limit:
            widened = await asyncio.to_thread(_fetch, 4.0)
            if widened is not None and len(widened.index) > len(frame.index):
                frame = widened
        if frame is not None and 0 < len(frame.index) < bounded_limit:
            raise ValueError(
                f"Alpaca returned only {len(frame.index)} of {bounded_limit} requested "
                f"{timeframe} bars for {normalized_symbol}; refusing to compute "
                "indicators on a shorter history than requested"
            )
        if frame is None or frame.empty:
            raise ValueError(
                f"Alpaca returned no {timeframe} market data for {normalized_symbol}"
            )
        frame = frame.reset_index()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        required = {"open", "high", "low", "close"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(
                "Alpaca market data is missing columns: " + ", ".join(sorted(missing))
            )
        if "timestamp" not in frame.columns:
            raise ValueError("Alpaca market data is missing timestamp")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        frame = frame.tail(bounded_limit).reset_index(drop=True)
        if frame.empty:
            raise ValueError(
                f"Alpaca returned no usable {timeframe} bars for {normalized_symbol}"
            )
        newest = pd.Timestamp(frame["timestamp"].iloc[-1])
        now = pd.Timestamp.now(tz="UTC")
        # Intraday bars can legitimately be days old across a market closure: a
        # Friday close to a Tuesday open is ~89 hours once a holiday Monday is
        # included, and IEX has no pre/post session to shorten the gap. 72 hours
        # rejected that valid data; a week still catches a feed that has stopped.
        max_age = (
            timedelta(days=7)
            if str(timeframe).strip().lower() == "1d"
            else timedelta(days=5)
        )
        if newest < now - max_age:
            raise ValueError(
                f"Alpaca market data is stale for {normalized_symbol}: "
                f"newest bar {newest.isoformat()} exceeds {max_age}"
            )
        return frame

    async def get_quote(self, symbol: str) -> LiveQuote:
        """Latest top-of-book quote for the #125 entry-deviation gate.

        Same IEX feed as :meth:`get_market_data`. A missing quote or a missing
        or non-numeric side raises instead of returning an invented price; the
        daemon additionally rejects zero, non-finite and crossed quotes.
        """
        from alpaca.data.requests import StockLatestQuoteRequest

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required for an Alpaca quote")
        if not self._connected or self._historical_data_client is None:
            raise RuntimeError("Alpaca adapter must be connected before requesting a quote")
        request = StockLatestQuoteRequest(symbol_or_symbols=[normalized_symbol], feed="iex")
        response = await asyncio.to_thread(
            self._historical_data_client.get_stock_latest_quote, request
        )
        quote = response.get(normalized_symbol) if isinstance(response, Mapping) else None
        if quote is None:
            raise ValueError(f"Alpaca returned no quote for {normalized_symbol}")
        prices: dict[str, float] = {}
        for side in ("bid_price", "ask_price"):
            raw = getattr(quote, side, None)
            try:
                if raw is None or isinstance(raw, bool):
                    raise TypeError(side)
                prices[side] = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"Alpaca quote for {normalized_symbol} has no usable {side}: {raw!r}"
                ) from exc
        # The quote's own time, never now(): an after-hours "latest" quote can
        # be hours old, and the daemon's freshness rule must see that.
        timestamp = getattr(quote, "timestamp", None)
        return LiveQuote(
            bid=prices["bid_price"],
            ask=prices["ask_price"],
            timestamp=timestamp if isinstance(timestamp, datetime) else None,
            source="alpaca_iex_latest_quote",
            timestamp_source="broker",
        )

    # ── Operator safety controls ──────────────────────────────────────────────────────────────
    #
    # Ordering matters more than the calls themselves. `cancel_open_orders` must never cancel
    # the SL/TP legs attached to a live position: the flatten runs after it, and if the
    # flatten fails the position would survive with every risk control removed. Alpaca has no
    # single "cancel detached orders" endpoint, so this classifies each working order by what
    # it would do to the *current* book:
    #
    #   * an order on a flat symbol cannot protect anything - cancel it (it is either an
    #     entry, or a protection orphaned by an earlier close);
    #   * an order on an open position whose side reduces that position is a protection -
    #     leave it;
    #   * anything else would open or extend exposure - cancel it.
    #
    # The flatten cannot follow that rule to the letter: Alpaca reserves the quantity of a
    # reducing-side order, so a working SL/TP makes `close_position` fail with "insufficient
    # qty available". `close_all_positions` therefore cancels each symbol's protections itself,
    # confirms the cancellation by re-read, closes, and restores a stop if the close is
    # refused - see `_flatten_symbol`.
    #
    # After the flatten, every symbol is flat, so `cancel_orphaned_protections` removes the
    # legs that outlived their position.

    @property
    def supports_cancel_orders(self) -> bool:
        return True

    @property
    def supports_flatten(self) -> bool:
        return True

    async def _require_trading_client(self) -> Any:
        if not self._connected or self._trading_client is None:
            await self.connect()
        client = self._trading_client
        if client is None:
            raise RuntimeError("Alpaca trading client is unavailable after connect.")
        return client

    def _order_book_lock(self) -> asyncio.Lock:
        """Lock held while this adapter replaces or cancels protective orders (#167).

        A trailing-stop replace gives the stop a new order id. Without the lock it could land
        between a sweep's order listing and its cancels: the sweep would then try to cancel the
        replaced id and miss the new one, which could survive a flatten as an orphaned stop
        (an orphaned sell stop opens a short). Holding one lock for the replace and for every
        sweep makes each sweep see either the old stop or the new one, never a half-state.
        """
        loop = asyncio.get_running_loop()
        state = getattr(self, "_order_book_lock_state", None)
        if state is None or state[0] is not loop:
            state = (loop, asyncio.Lock())
            self._order_book_lock_state = state
        return state[1]

    #: Alpaca's maximum page size for the orders listing (its default is 50).
    _OPEN_ORDERS_LIMIT = 500

    async def _list_open_orders(self, client: Any, *, nested: bool = False) -> list[Any]:
        """Every working order, or a hard failure - never a silent empty list.

        Asks for the maximum page (the default of 50 would silently drop orders on
        a busy account) and fails when the page comes back full, since orders may
        then be missing. ``nested`` returns bracket legs under their parent.
        """
        orders = await self._list_open_orders_page(client, nested=nested)
        if len(orders) >= self._OPEN_ORDERS_LIMIT:
            raise RuntimeError(
                f"Alpaca returned {len(orders)} open orders, the page limit; "
                "refusing to treat the list as complete"
            )
        return orders

    async def _list_open_orders_page(self, client: Any, *, nested: bool) -> list[Any]:
        get_orders = getattr(client, "get_orders", None)
        if not callable(get_orders):
            raise RuntimeError(
                "Alpaca client cannot list orders; refusing to report a clean sweep"
            )
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=self._OPEN_ORDERS_LIMIT,
                nested=nested,
            )
            result = await asyncio.to_thread(get_orders, filter=request)
        except ModuleNotFoundError:
            result = await asyncio.to_thread(get_orders)
        except TypeError:
            # A client double (or an older alpaca-py) without the `filter`
            # keyword. Only retried here; an error raised *inside* get_orders is
            # not a TypeError from the call shape and propagates.
            result = await asyncio.to_thread(get_orders)
        if result is None:
            raise RuntimeError("Alpaca returned no order list; refusing to report a clean sweep")
        if isinstance(result, (list, tuple)):
            return list(result)
        for key in ("orders", "order"):
            value = _order_attr(result, key, None)
            if isinstance(value, (list, tuple)):
                return list(value)
        raise RuntimeError(
            f"Alpaca order list has an unrecognised shape ({type(result).__name__}); "
            "refusing to report a clean sweep"
        )

    async def working_order_ids(self) -> set[str]:
        """Ids of every working (open) order: broker ids and client order ids.

        Used before sizing an exit, to tell whether an earlier exit is still
        working (#121). The client order id is the canonical signal id, which
        names an order whose broker id was never persisted. Nested legs are
        included. Fails rather than returning an empty set it cannot vouch for.
        """
        client = await self._require_trading_client()
        ids: set[str] = set()
        for order in await self._list_open_orders(client, nested=True):
            for item in (order, *_order_legs(order)):
                for name in ("id", "client_order_id"):
                    order_id = _control_order_id(_order_attr(item, name))
                    if order_id is not None:
                        ids.add(order_id)
        return ids

    def exit_qty_step(self, symbol: str) -> float:
        """Alpaca's smallest quantity step (9 decimals): what an exit is floored to."""
        return 1e-9

    def position_qty_tolerance(self, symbol: str) -> float:
        """Half of Alpaca's smallest quantity step (9 decimals, fractional shares).

        Ledger sums and the broker position agree to that step or not at all;
        half a step absorbs float summation noise without hiding a real share.
        """
        return 0.5e-9

    @staticmethod
    def _protection_order_ids(
        orders: list[Any], protection_side_by_symbol: dict[str, str]
    ) -> set[str]:
        """Ids of orders that must survive the entry-cancel sweep.

        Two shapes mean "protection": a leg nested under a working parent, and
        an order whose side reduces an open position. The second covers the case
        where the parent has already filled and only the legs are returned.
        """
        protected: set[str] = set()
        for order in orders:
            for leg in _order_legs(order):
                leg_id = _control_order_id(_order_attr(leg, "id"))
                if leg_id is not None:
                    protected.add(leg_id)
            symbol = _order_symbol(order)
            expected_side = protection_side_by_symbol.get(symbol)
            raw_side = _order_attr(order, "side", "")
            side = str(getattr(raw_side, "value", raw_side) or "").strip().lower()
            if expected_side and side == expected_side:
                order_id = _control_order_id(_order_attr(order, "id"))
                if order_id is not None:
                    protected.add(order_id)
        return protected

    async def _protection_sides_by_symbol(self) -> dict[str, str]:
        """The side that would *reduce* each open position."""
        sides: dict[str, str] = {}
        for position in await self.get_positions():
            symbol = str(position.get("symbol") or "").strip().upper()
            qty = _position_qty(position)
            if not symbol or _is_flat(qty):
                continue
            sides[symbol] = "sell" if qty > 0 else "buy"
        return sides

    async def _cancel_one(self, client: Any, order_id: str) -> None:
        cancel = getattr(client, "cancel_order_by_id", None)
        if not callable(cancel):
            cancel = getattr(client, "cancel_order", None)
        if not callable(cancel):
            raise RuntimeError("Alpaca client cannot cancel an order by id")
        await asyncio.to_thread(cancel, order_id)

    async def cancel_open_orders(self) -> ControlOutcome:
        """Cancel exposure-opening orders; see :meth:`_cancel_open_orders_locked`."""
        async with self._order_book_lock():
            return await self._cancel_open_orders_locked()

    async def _cancel_open_orders_locked(self) -> ControlOutcome:
        """Cancel working orders that could open or extend exposure.

        SL/TP legs on a live position are deliberately left alone: the flatten
        that follows is the step that removes the position, and a failure there
        must not leave it naked. A refused cancel does not stop the rest, and
        every refusal/unreadable id is reported so a partial sweep cannot be
        mistaken for a complete one.
        """
        client = await self._require_trading_client()
        orders = await self._list_open_orders(client)
        protected = self._protection_order_ids(
            orders, await self._protection_sides_by_symbol()
        )
        completed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        cancel_accepted: set[str] = set()
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                unidentified.append(f"order:{_order_attr(order, 'id', None)!r}")
                continue
            if order_id in protected:
                continue
            # An order an earlier pass already cancelled is still pending_cancel. The
            # repeat cancel is kept (it costs nothing and may nudge a stuck cancel),
            # but a refusal of it is not a dropped cancel: the order is already on its
            # way out. The re-read below still decides - a settled order is gone, one
            # still pending_cancel is reported as such, anything else is a failure.
            was_pending_cancel = _order_status(order) == "pending_cancel"
            try:
                await self._cancel_one(client, order_id)
                cancel_accepted.add(order_id)
            except Exception as exc:
                if was_pending_cancel:
                    cancel_accepted.add(order_id)
                    logger.info(
                        "Alpaca refused a repeat cancel of pending_cancel order %s: %s. "
                        "Verifying on re-read.",
                        order_id,
                        exc,
                    )
                    continue
                failed.append(f"{order_id}:{exc}")
                logger.warning(
                    "Alpaca refused to cancel order %s: %s. Continuing the sweep.",
                    order_id,
                    exc,
                )
                continue
            completed += 1

        # A cancel the broker accepted must actually disappear from the working
        # book. Re-reading also catches an order whose cancel was silently
        # dropped, which a count alone would report as done.
        try:
            remaining = await self._list_open_orders(client)
        except Exception as exc:
            return ControlOutcome(
                completed=completed,
                unidentified=tuple(unidentified),
                failed=tuple(failed) + (f"verify:{exc}",),
            )
        # Alpaca keeps listing a cancel it accepted as ``pending_cancel`` until the
        # cancel settles, so that status on an id this sweep cancelled without error
        # is treated as an accepted request, not a dropped cancel. It is NOT proof
        # the order can no longer fill: a pending_cancel order may still execute in
        # a cancel/fill race. Those ids are returned in ``pending_cancel`` and the
        # daemon refuses to stamp a STOPPED deployment enforced while any remain
        # (fail closed); it retries until the re-read shows them cancelled or filled,
        # and the flatten handles a fill. Only ids this sweep cancelled count; an
        # order that was never ours to cancel and sits in ``pending_cancel`` is not
        # vouched for.
        def accepted_pending_cancel(order_id: str | None, order: Any) -> bool:
            return order_id in cancel_accepted and _order_status(order) == "pending_cancel"

        still_working = {
            order_id
            for order in remaining
            if (order_id := _control_order_id(_order_attr(order, "id"))) not in protected
            and not accepted_pending_cancel(order_id, order)
        }
        still_working.discard(None)
        failed.extend(sorted(still_working))
        pending_cancel = sorted(
            {
                order_id
                for order in remaining
                if (order_id := _control_order_id(_order_attr(order, "id")))
                is not None
                and order_id not in protected
                and accepted_pending_cancel(order_id, order)
            }
        )
        return ControlOutcome(
            completed=completed,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
            pending_cancel=tuple(pending_cancel),
        )

    @staticmethod
    def _best_stop_price(orders: list[Any], symbol: str) -> float | None:
        """The stop price to restore if a close is refused.

        Taken from the protection about to be cancelled, so the position is
        restored to the stop the strategy chose rather than one invented from a
        stale quote. A nested stop leg is checked too, because a bracket's stop
        is sometimes only visible there.
        """
        for order in orders:
            if _order_symbol(order) != symbol:
                continue
            for candidate in (order, *_order_legs(order)):
                candidate_symbol = _order_symbol(candidate)
                if candidate_symbol not in {"", symbol}:
                    continue
                price = _to_float(
                    _order_attr(candidate, "stop_price", None), default=0.0
                )
                if price > 0:
                    return price
                nested = _order_attr(candidate, "stop_loss", None)
                nested_price = _to_float(
                    _order_attr(nested, "stop_price", None), default=0.0
                )
                if nested_price > 0:
                    return nested_price
        return None

    def _load_stop_order_types(self) -> tuple[Any, Any, Any]:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import StopOrderRequest
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        return OrderSide, TimeInForce, StopOrderRequest

    @staticmethod
    def _has_working_stop(orders: list[Any], symbol: str, side: str) -> bool:
        """Whether a stop that actually protects this position is still working.

        The side matters. A buy stop on a long is an entry for a different intent,
        not a protection: treating it as one after the position's own sell stop was
        cancelled reports the position as protected while it is naked. Only a stop
        on the reducing side counts, and a bracket's leg counts only if that leg
        itself is on the reducing side.
        """
        wanted = "buy" if str(side).strip().lower() == "buy" else "sell"
        for order in orders:
            if _order_symbol(order) != symbol:
                continue
            for candidate in (order, *_order_legs(order)):
                candidate_symbol = _order_symbol(candidate)
                if candidate_symbol not in {"", symbol}:
                    continue
                order_type = str(_order_attr(candidate, "order_type", "") or "").lower()
                if "stop" not in order_type:
                    continue
                if _order_side(candidate) == wanted:
                    return True
        return False

    async def _restore_stop(
        self,
        client: Any,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None,
    ) -> bool:
        """Put a stop back on a position whose close was refused.

        The protections were cancelled to unblock the close, so a refusal would
        otherwise leave the position naked. Restoring the stop the strategy had
        is the only acceptable outcome short of a fill; failing to do so is
        logged at CRITICAL because it means live unprotected exposure.

        Returns whether the position is protected afterwards, which is also true
        when a stop still works at the broker. Re-submitting in that case would
        leave two stops on one position, and the second becomes an orphan the
        moment the first fills - an orphaned sell-stop opens a short.
        """
        try:
            # nested=True so an OCO leg's stop (#145) counts as working here.
            working = await self._list_open_orders(client, nested=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not re-read orders before restoring a stop; restoring anyway",
                extra={"data": {"symbol": symbol, "error": repr(exc)}},
            )
            working = None
        if working is not None and self._has_working_stop(working, symbol, side):
            logger.warning(
                "Not restoring a stop: one still works at the broker",
                extra={"data": {"symbol": symbol}},
            )
            return True
        if stop_price is None:
            logger.critical(
                "Cannot restore a stop: the cancelled orders carried no stop price, "
                "so the position is unprotected until the next retry",
                extra={"data": {"symbol": symbol, "qty": qty}},
            )
            return False
        try:
            order_side, time_in_force, stop_request_cls = self._load_stop_order_types()
            request = stop_request_cls(
                symbol=symbol,
                qty=abs(qty),
                side=order_side.SELL if side == "sell" else order_side.BUY,
                time_in_force=time_in_force.DAY,
                stop_price=stop_price,
            )
            await asyncio.to_thread(client.submit_order, order_data=request)
        except Exception as exc:
            logger.critical(
                "Failed to restore a stop on a position whose close was refused; "
                "the position is unprotected",
                extra={
                    "data": {
                        "symbol": symbol,
                        "qty": qty,
                        "stop_price": stop_price,
                        "error": repr(exc),
                    }
                },
            )
            return False
        logger.warning(
            "Restored a stop after the broker refused to close the position",
            extra={
                "data": {"symbol": symbol, "qty": qty, "stop_price": stop_price}
            },
        )
        return True

    async def _abandon_flatten(
        self,
        client: Any,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None,
        reason: str,
        *,
        removed_protection: bool,
    ) -> str:
        """Report why a flatten stopped, restoring the stop if one was removed.

        Once a protection has been cancelled the position is unprotected, so
        every early exit after that point has to put a stop back before the
        failure is reported.
        """
        if removed_protection:
            if not await self._restore_stop(client, symbol, qty, side, stop_price):
                return f"{reason};NO-STOP-RESTORED"
        return reason

    async def _flatten_symbol(self, client: Any, symbol: str, qty: float) -> str | None:
        """Flatten one symbol; see :meth:`_flatten_symbol_locked`."""
        async with self._order_book_lock():
            return await self._flatten_symbol_locked(client, symbol, qty)

    async def _flatten_symbol_locked(
        self, client: Any, symbol: str, qty: float
    ) -> str | None:
        """Cancel this symbol's working orders, then close its position.

        Returns ``None`` on success, or a short reason for the failure.

        The cancel is not optional. Alpaca reserves the quantity of every
        reducing-side order, so `close_position` is refused with "insufficient
        qty available" while the SL/TP legs are still working. That is the one
        place where "keep the protections until the flatten succeeds" cannot
        hold - the protections are exactly what blocks the flatten - so the stop
        is re-placed on every failure after a protection was removed.
        """
        side = "sell" if qty > 0 else "buy"
        orders = await self._list_open_orders(client)
        on_symbol = [order for order in orders if _order_symbol(order) == symbol]
        stop_price = self._best_stop_price(on_symbol, symbol)

        cancelled_any = False
        for order in on_symbol:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                return await self._abandon_flatten(
                    client,
                    symbol,
                    qty,
                    side,
                    stop_price,
                    f"unreadable-order:{_order_attr(order, 'id', None)!r}",
                    removed_protection=cancelled_any,
                )
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:
                return await self._abandon_flatten(
                    client,
                    symbol,
                    qty,
                    side,
                    stop_price,
                    f"cancel:{order_id}:{exc}",
                    removed_protection=cancelled_any,
                )
            cancelled_any = True

        # A cancel the broker accepted but did not apply still reserves the
        # quantity, and the close would then be refused. Confirm by re-read.
        try:
            remaining_orders = await self._list_open_orders(client)
        except Exception as exc:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                f"verify:{exc}",
                removed_protection=cancelled_any,
            )
        still_working = [
            _control_order_id(_order_attr(order, "id"))
            for order in remaining_orders
            if _order_symbol(order) == symbol
        ]
        if still_working:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                "orders-still-working:"
                + ",".join(str(order_id) for order_id in still_working),
                removed_protection=cancelled_any,
            )

        try:
            await asyncio.to_thread(client.close_position, symbol)
        except Exception as exc:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                f"close:{exc}",
                removed_protection=cancelled_any,
            )
        return None

    async def close_all_positions(self) -> ControlOutcome:
        """Flatten every open position, one symbol at a time.

        `close_all_positions(cancel_orders=False)` cannot do this job: it does
        not cancel the reducing-side legs, Alpaca reserves their quantity, and
        the close is refused - so a stopped deployment with a working SL/TP
        would never actually flatten. Each symbol is therefore handled on its
        own: cancel its working orders, confirm by re-read, close, and restore a
        stop if the close is refused.
        """
        client = await self._require_trading_client()
        positions = [
            position
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        ]
        if not positions:
            return ControlOutcome(completed=0)

        failed: list[str] = []
        for position in positions:
            symbol = str(position.get("symbol") or "").strip().upper()
            qty = _position_qty(position)
            if not symbol or _is_flat(qty):
                continue
            problem = await self._flatten_symbol(client, symbol, qty)
            if problem is not None:
                failed.append(f"{symbol}:{problem}")

        # The broker's acknowledgement is not proof the account is flat. A
        # position that is still open must keep the deployment unenforced.
        remaining = [
            position
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        ]
        for position in remaining:
            symbol = str(position.get("symbol") or "?")
            if not any(item.startswith(f"{symbol}:") for item in failed):
                failed.append(symbol)
        return ControlOutcome(
            completed=len(positions) - len(remaining),
            failed=tuple(failed),
        )

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        """Cancel orders on flat symbols; see :meth:`_cancel_orphaned_protections_locked`."""
        async with self._order_book_lock():
            return await self._cancel_orphaned_protections_locked()

    async def _cancel_orphaned_protections_locked(self) -> ControlOutcome:
        """Cancel working orders left on symbols with no open position.

        Called after a successful flatten, when every symbol is flat: what
        remains are the SL/TP legs detached from the positions they protected.
        An order on a symbol that still has a position is never touched, so this
        cannot disarm a live position.
        """
        client = await self._require_trading_client()
        orders = await self._list_open_orders(client)
        open_symbols = {
            str(position.get("symbol") or "").strip().upper()
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        }
        completed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                unidentified.append(f"order:{_order_attr(order, 'id', None)!r}")
                continue
            if _order_symbol(order) in open_symbols:
                continue
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:
                failed.append(f"{order_id}:{exc}")
                continue
            completed += 1
        return ControlOutcome(
            completed=completed,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    # ── Stop amendment (live trailing stops, issue #167) ──────────────────────────────────────
    #
    # Alpaca's order replace (PATCH /v2/orders/{id}) is atomic from the position's point of
    # view: the original order keeps working until the replacement is accepted, and stays in
    # force when the replace is refused. That is what makes the capability safe to declare -
    # the stop is never cancelled by this adapter in order to move it. The replace holds
    # `_order_book_lock`, like every sweep that cancels orders, so a flatten never races it.

    @property
    def supports_stop_amendment(self) -> bool:
        return True

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        """Whether :meth:`get_market_data` can serve ``timeframe`` (no retry can change that)."""
        try:
            _alpaca_timeframe(timeframe)
        except ValueError:
            return False
        return True

    async def list_protective_stops(self) -> list[ProtectiveStop]:
        """Working plain stop orders on the reducing side of each open position.

        Only ``stop`` orders count. A stop-limit or a broker-native trailing stop is not
        something a ``stop_price`` replace can move safely, so it is not offered for
        amendment; the caller then reports the position as having no amendable stop.
        """
        client = await self._require_trading_client()
        reducing_sides = await self._protection_sides_by_symbol()
        if not reducing_sides:
            return []
        orders = await self._list_open_orders(client, nested=True)
        orders.extend(await self._list_filled_parents_with_held_legs(client, set(reducing_sides)))
        stops: dict[str, ProtectiveStop] = {}
        for order in orders:
            for candidate in _stop_candidates(order):
                stop = _protective_stop_of(candidate, order, reducing_sides)
                if stop is not None:
                    stops[stop.order_id] = stop
        return list(stops.values())

    async def _list_filled_parents_with_held_legs(
        self, client: Any, symbols: set[str]
    ) -> list[Any]:
        """Filled bracket parents whose stop leg is still ``held``.

        Observed on Alpaca paper (2026-11-16): once a bracket entry fills, the stop leg
        stays ``held`` and is absent from every ``status=open`` listing, nested or flat.
        It is only reachable as a leg of the filled parent in a ``status=all`` listing,
        and it can be replaced like any working stop. Only parents of symbols with an
        open position are requested. A failure to list is not swallowed: a missing stop
        would be reported as "no stop" for a position that does have one.
        """
        get_orders = getattr(client, "get_orders", None)
        if not symbols or not callable(get_orders):
            return []
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=sorted(symbols),
            nested=True,
            limit=_ALPACA_HELD_LEG_LOOKUP_LIMIT,
        )
        result = await asyncio.to_thread(get_orders, filter=request)
        if not isinstance(result, (list, tuple)):
            raise RuntimeError(
                f"Alpaca order list has an unrecognised shape ({type(result).__name__}); "
                "refusing to report a clean sweep"
            )
        if len(result) >= _ALPACA_HELD_LEG_LOOKUP_LIMIT:
            # The listing is newest-first: a full page may have cut off the filled parent
            # that owns the held stop leg, and "no stop" would then be a lie.
            raise RuntimeError(
                f"Alpaca returned {len(result)} orders, the page limit; "
                "refusing to treat the list as complete"
            )
        return [
            order
            for order in result
            if _order_status(order) == "filled"
            and any(_order_status(leg) == "held" for leg in _order_legs(order))
        ]

    def round_stop_price(self, stop: ProtectiveStop, price: float) -> float:
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"Invalid stop price for {stop.symbol}: {price!r}; refusing to amend."
            )
        tick = _ALPACA_TICK_AT_OR_ABOVE_ONE if value >= 1.0 else _ALPACA_TICK_BELOW_ONE
        # Away from the market: never tighter than the computed stop.
        rounding = ROUND_FLOOR if stop.side == "sell" else ROUND_CEILING
        # Float noise is removed first (101.96999999999998 is 101.97): flooring
        # the raw float would otherwise give away a whole tick to a rounding
        # artefact of the stop arithmetic.
        rounded = float(Decimal(repr(round(value, 8))).quantize(tick, rounding=rounding))
        if rounded <= 0:
            raise ValueError(
                f"Stop price for {stop.symbol} rounds to {rounded!r}; refusing to amend."
            )
        return rounded

    async def amend_protective_stop(
        self, stop: ProtectiveStop, new_stop_price: float
    ) -> ProtectiveStop:
        price = self.round_stop_price(stop, new_stop_price)
        client = await self._require_trading_client()
        replace = getattr(client, "replace_order_by_id", None)
        if not callable(replace):
            raise RuntimeError("Alpaca client cannot replace an order by id")
        async with self._order_book_lock():
            replaced = await asyncio.to_thread(
                replace, stop.order_id, order_data=ReplaceOrderRequest(stop_price=price)
            )
        new_order_id = _control_order_id(_order_attr(replaced, "id"))
        confirmed_price = _to_float(_order_attr(replaced, "stop_price", None), default=0.0)
        if new_order_id is None or not math.isclose(
            confirmed_price, price, rel_tol=0.0, abs_tol=1e-9
        ):
            # Not a confirmed amendment. The broker's own state decides what is in force; the
            # caller re-reads it on its next pass instead of trusting this response.
            raise RuntimeError(
                f"Alpaca did not confirm the stop replacement for {stop.symbol} "
                f"(order_id={new_order_id!r}, stop_price={confirmed_price!r}, "
                f"requested={price!r})"
            )
        # #144: replacing an order gives it a NEW broker order id, so the leg-order-id ->
        # signal map this adapter learned for the old id is stale from this moment on. The
        # replaced order here is the bracket's stop leg whenever the pre-replacement id was
        # learned as a leg, so its fill is re-learned against the same parent signal here
        # rather than waiting for the next adapter connect (the only other pass).
        previous_leg_key = _bracket_leg_key(stop.order_id)
        new_leg_key = _bracket_leg_key(new_order_id)
        parent_signal_id = (
            self._bracket_leg_order_id_to_signal_id.pop(previous_leg_key, None)
            if previous_leg_key is not None
            else None
        )
        if parent_signal_id is not None and new_leg_key is not None:
            self._bracket_leg_order_id_to_signal_id[new_leg_key] = parent_signal_id
            logger.info(
                "Amended Alpaca stop leg re-registered against its parent signal",
                extra={
                    "data": {
                        "parent_signal_id": parent_signal_id,
                        "previous_leg_order_id": str(stop.order_id),
                        "leg_order_id": str(new_order_id),
                        "symbol": stop.symbol,
                    }
                },
            )
        return ProtectiveStop(
            order_id=new_order_id,
            symbol=stop.symbol,
            side=stop.side,
            stop_price=confirmed_price,
            qty=stop.qty,
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with pricing and unrealized PnL."""
        if not self._connected or self._trading_client is None:
            await self.connect()
        rows = await asyncio.to_thread(self._trading_client.get_all_positions)
        payload: list[dict[str, Any]] = []
        for row in rows:
            raw_qty = getattr(row, "qty", None)
            entry: dict[str, Any] = {
                "symbol": str(getattr(row, "symbol", "")).strip().upper(),
                "qty": _to_float(raw_qty, default=0.0),
                "entry_price": _to_float(getattr(row, "avg_entry_price", None), default=0.0),
                "current_price": _to_float(getattr(row, "current_price", None), default=0.0),
                "unrealized_pnl": _to_float(getattr(row, "unrealized_pl", None), default=0.0),
            }
            if raw_qty is not None and not _is_parseable_qty(raw_qty):
                # Keep the coercion for display consumers, but remember that the
                # broker's value could not be read: the risk paths that decide
                # whether an account is flat must fail closed rather than treat
                # it as a zero position.
                entry["qty_unparseable"] = True
                entry["qty_raw"] = raw_qty
            payload.append(entry)
        return payload

    def _trade_update_fill_order(self, data: Any) -> tuple[str, Any, Any] | None:
        """The ``(event, order, client_order_id_text)`` of a fill event, or ``None``.

        Split from the signal resolution below because a bracket leg's fill is a fill event whose
        client order id names no signal of ours (#144): the order id decides that case, so the
        event has to be readable before any client order id is interpreted.
        """
        event = str(getattr(data, "event", "")).lower()
        if event not in {"fill", "partial_fill"}:
            return None

        order = getattr(data, "order", None)
        if order is None:
            logger.warning("Trade update without order payload")
            return None

        client_order_id = getattr(order, "client_order_id", "")
        if not client_order_id:
            logger.warning("Trade update missing client_order_id")
            return None
        return event, order, client_order_id

    async def _record_bracket_leg_fill(
        self,
        parent_signal_id: str,
        data: Any,
        order: Any,
        *,
        broker_order_id: str,
        event: str,
    ) -> None:
        """Book one bracket leg fill against the parent signal's deployment ledger (#144).

        The dedupe key is derived from the leg's own broker order id and its cumulative filled
        quantity - the ONE derivation this path and the reconciliation pass below share, so a fill
        the stream stored is a skip there rather than a second row, whichever of the two sees the
        fill first. Alpaca's per-fill ``execution_id`` is deliberately NOT used for a leg: the
        reconciliation pass reads the leg off the parent order, where no execution id exists, and
        two derivations for one fill would let the same fill be stored twice.
        """
        skip, filled_qty_value = self._resolve_trade_update_qty(
            data,
            order,
            event=event,
            client_order_id=_order_attr(order, "client_order_id"),
        )
        if skip:
            return
        broker_cumulative_filled_qty = _to_float(
            getattr(order, "filled_qty", None), default=0.0
        )
        # The event's OWN quantity, not the cumulative fallback `_resolve_trade_update_qty` may
        # have substituted: only that proves this fill is the whole leg.
        event_qty = getattr(data, "qty", None)
        if event_qty is None:
            event_qty = getattr(data, "filled_qty", None)
        actual_price, price_observed = _resolve_fill_price(
            (getattr(data, "fill_price", None), getattr(data, "price", None)),
            order_avg_price=getattr(order, "filled_avg_price", None),
            event_qty=event_qty,
            cumulative_qty=broker_cumulative_filled_qty,
        )
        filled_qty = _to_float(filled_qty_value)
        raw_fill_ts = getattr(data, "timestamp", None)
        fill_ts = _broker_fill_timestamp(
            raw_fill_ts, getattr(order, "filled_at", None), getattr(order, "updated_at", None)
        )
        execution_id = str(getattr(data, "execution_id", "") or "")
        dedupe_key = _alpaca_fill_dedupe_key(
            # Empty on purpose: see the docstring - the leg's id and cumulative quantity are the
            # identity a reconciliation pass can reproduce without an execution id.
            execution_id="",
            broker_order_id=broker_order_id,
            event=_LEG_FILL_EVENT_LABEL,
            cumulative_filled_qty=broker_cumulative_filled_qty,
            raw_timestamp=raw_fill_ts,
            filled_qty=filled_qty,
        )
        if dedupe_key is None:
            logger.warning(
                "Alpaca bracket leg fill carries no execution id, no cumulative leg quantity and "
                "no usable timestamp; refusing to derive a dedupe key that could count it twice "
                "or drop a sibling. reconcile_executions backfills it from the parent order",
                extra={
                    "data": {
                        "account_id": self.account_id,
                        "parent_signal_id": parent_signal_id,
                        "leg_order_id": broker_order_id,
                        "event": event,
                    }
                },
            )
            return
        if fill_ts is None:
            logger.warning(
                "Alpaca bracket leg fill carries no usable timestamp on the event or its order; "
                "no execution row was stored (never an invented time), so the deployment's "
                "ledger still counts the closed shares until the next adapter connect runs "
                "reconciliation and the leg reports a fill time",
                extra={
                    "data": {
                        "account_id": self.account_id,
                        "parent_signal_id": parent_signal_id,
                        "leg_order_id": broker_order_id,
                        "event": event,
                        "execution_id": execution_id,
                        "filled_qty": filled_qty,
                    }
                },
            )
            return
        persisted_qty = await asyncio.to_thread(
            record_live_broker_side_close_fill,
            parent_signal_id=parent_signal_id,
            close_reason=_bracket_leg_close_reason(order),
            filled_qty=filled_qty,
            actual_price=actual_price,
            actual_price_observed=price_observed,
            commission=_known_commission(order),
            timestamp=fill_ts,
            broker_order_id=broker_order_id,
            # The leg's broker cumulative caps the booking inside the insert: a
            # delayed event whose slice a reconcile pass already covered is skipped
            # there rather than double-counted here.
            cumulative_filled_qty=(
                broker_cumulative_filled_qty
                if broker_cumulative_filled_qty > 0.0
                else None
            ),
            cumulative_avg_price=_positive_finite(
                getattr(order, "filled_avg_price", None)
            ),
            dedupe_key=dedupe_key,
            # The leg's own side, cross-checked against the side the parent's action implies: a
            # leg that would extend the position is refused, not recorded.
            close_side=_order_side(order),
        )
        logger.info(
            (
                "Persisted Alpaca bracket leg close fill"
                if persisted_qty > 0.0
                else "No new Alpaca bracket leg close fill persisted "
                "(duplicate or refused; the reason is logged by the insert path)"
            ),
            extra={
                "data": {
                    "parent_signal_id": parent_signal_id,
                    "close_signal_id": broker_close_signal_id(parent_signal_id),
                    "leg_order_id": broker_order_id,
                    "close_reason": _bracket_leg_close_reason(order),
                    "event": event,
                    "filled_qty": filled_qty,
                    "persisted_qty": persisted_qty,
                    "actual_price": actual_price,
                    "actual_price_observed": price_observed,
                    "broker_cumulative_filled_qty": broker_cumulative_filled_qty,
                }
            },
        )

    def _resolve_trade_update_qty(
        self,
        data: Any,
        order: Any,
        *,
        event: str,
        client_order_id: Any,
    ) -> tuple[bool, Any]:
        filled_qty_value = getattr(data, "qty", None)
        if filled_qty_value is None:
            filled_qty_value = getattr(data, "filled_qty", None)
        if filled_qty_value is None:
            # Per-event quantity. `order.filled_qty` is CUMULATIVE in alpaca-py and
            # must not be persisted per partial-fill event (double counting).
            if event == "partial_fill":
                logger.warning(
                    "Partial fill without per-event qty; skipping to avoid "
                    "double-counting cumulative order.filled_qty",
                    extra={"data": {"client_order_id": str(client_order_id)}},
                )
                return True, None
            filled_qty_value = getattr(order, "filled_qty", None)
        return False, filled_qty_value

    async def _persist_execution_fill(
        self,
        execution: LiveExecution,
        broker_cumulative_filled_qty: float,
    ) -> float:
        if broker_cumulative_filled_qty > 0.0:
            return await asyncio.to_thread(
                save_live_execution_up_to_cumulative,
                execution,
                broker_cumulative_filled_qty,
            )
        persisted = await asyncio.to_thread(save_live_execution, execution)
        return execution.filled_qty if persisted else 0.0

    async def _on_trade_update(self, data: Any) -> None:
        try:
            fill = self._trade_update_fill_order(data)
            if fill is None:
                return
            event, order, client_order_id = fill
            raw_broker_order_id = getattr(order, "id", None)
            broker_order_id = _normalize_broker_order_id(raw_broker_order_id)
            if broker_order_id is None:
                logger.warning(
                    "Trade update missing valid broker order id",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "client_order_id": str(client_order_id),
                            "broker_order_id": str(raw_broker_order_id or ""),
                        }
                    },
                )
                return

            # #144: a bracket leg's fill closes this signal's position. Booked BEFORE the
            # client_order_id path below, because a leg may carry the parent's client order id -
            # and booking that against the parent signal would ADD the leg's quantity to the
            # long side (the ledger signs by the parent signal's action) instead of reducing
            # it, leaving the ledger further from the broker position than before.
            parent_signal_id = self._parent_signal_id_for_broker_order(broker_order_id)
            if parent_signal_id is not None:
                await self._record_bracket_leg_fill(
                    parent_signal_id,
                    data,
                    order,
                    broker_order_id=broker_order_id,
                    event=event,
                )
                return

            try:
                signal_uuid = UUID(str(client_order_id))
            except ValueError:
                if await self._handle_reprotect_trade_update(
                    data,
                    order,
                    client_order_id,
                    broker_order_id=broker_order_id,
                    event=event,
                ):
                    return
                logger.warning(
                    "Trade update client_order_id is not a UUID",
                    extra={"data": {"client_order_id": str(client_order_id)}},
                )
                return

            # A UUID client order id that names no persisted signal is not a
            # signal fill we may book: Alpaca gives each OCO leg its own
            # UUID-shaped client order id, so a re-placed stop leg whose id
            # registration was lost arrives here looking exactly like a signal
            # fill. Persisting it would write an execution against a signal
            # that does not exist; flag the account for reconciliation instead
            # (#277's never-guess rule).
            signal_metadata = await asyncio.to_thread(
                get_live_signal_metadata, str(signal_uuid)
            )
            if not isinstance(signal_metadata, dict):
                self._flag_reprotect_fill_unattributed(
                    order,
                    client_order_id,
                    broker_order_id,
                    cause="alpaca_unattributed_order_fill",
                )
                return

            skip, filled_qty_value = self._resolve_trade_update_qty(
                data, order, event=event, client_order_id=client_order_id
            )
            if skip:
                return

            broker_cumulative_filled_qty = _to_float(
                getattr(order, "filled_qty", None), default=0.0
            )
            # The event's OWN quantity, not the cumulative fallback `_resolve_trade_update_qty`
            # may have substituted: only that proves this fill is the whole order.
            event_qty = getattr(data, "qty", None)
            if event_qty is None:
                event_qty = getattr(data, "filled_qty", None)
            # The quantity is still recorded when no price is usable - a fill the database never
            # saw is how a duplicate order gets submitted - but at 0.0 with a verdict that says so.
            actual_price, price_observed = _resolve_fill_price(
                (getattr(data, "fill_price", None), getattr(data, "price", None)),
                order_avg_price=getattr(order, "filled_avg_price", None),
                event_qty=event_qty,
                cumulative_qty=broker_cumulative_filled_qty,
            )

            filled_qty = _to_float(filled_qty_value)
            # alpaca-py has no commission field on TradeUpdate or Order; a crypto fee is posted end
            # of day and backfilled, so here it is unknown unless it is a known zero (#124).
            commission = _known_commission(order)
            raw_fill_ts = getattr(data, "timestamp", None)
            fill_ts = _broker_fill_timestamp(
                raw_fill_ts, getattr(order, "filled_at", None), getattr(order, "updated_at", None)
            )

            execution_id = str(getattr(data, "execution_id", "") or "")
            dedupe_key = _alpaca_fill_dedupe_key(
                execution_id=execution_id,
                broker_order_id=broker_order_id,
                event=event,
                cumulative_filled_qty=broker_cumulative_filled_qty,
                raw_timestamp=raw_fill_ts,
                filled_qty=filled_qty,
            )
            if dedupe_key is None:
                logger.warning(
                    "Alpaca fill carries no execution id, no cumulative order quantity and no "
                    "usable timestamp; refusing to derive a dedupe key that could count it "
                    "twice or drop a sibling. reconcile_executions backfills it from the order",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "client_order_id": str(client_order_id),
                            "broker_order_id": broker_order_id,
                            "event": event,
                        }
                    },
                )
                return

            if fill_ts is None:
                logger.warning(
                    "Alpaca fill carries no usable timestamp on the event or its order; no "
                    "execution row was stored (never an invented time), so the local filled "
                    "quantity understates the broker's until the next adapter connect runs "
                    "reconciliation and the order reports a fill time",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "client_order_id": str(client_order_id),
                            "broker_order_id": broker_order_id,
                            "event": event,
                            "execution_id": execution_id,
                            "filled_qty": filled_qty,
                            "broker_cumulative_filled_qty": broker_cumulative_filled_qty,
                        }
                    },
                )
                return

            execution = LiveExecution(
                signal_id=signal_uuid,
                actual_price=actual_price,
                actual_price_observed=price_observed,
                filled_qty=filled_qty,
                commission=commission,
                timestamp=fill_ts,
                broker_order_id=broker_order_id,
                dedupe_key=dedupe_key,
            )
            persisted_qty = await self._persist_execution_fill(
                execution, broker_cumulative_filled_qty
            )
            logger.info(
                (
                    "Persisted Alpaca execution fill"
                    if persisted_qty > 0.0
                    else "Skipped duplicate Alpaca execution fill"
                ),
                extra={
                    "data": {
                        "signal_id": str(execution.signal_id),
                        "execution_id": str(execution.id),
                        "event": event,
                        "actual_price": actual_price,
                        "actual_price_observed": price_observed,
                        "commission": commission,
                        "filled_qty": filled_qty,
                        "persisted_qty": persisted_qty,
                        "broker_cumulative_filled_qty": broker_cumulative_filled_qty,
                        "broker_order_id": broker_order_id,
                    }
                }
            )
        except Exception as exc:
            logger.warning(
                "Failed to process trade update",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    async def listen_executions(self) -> None:
        if not self._connected or self._trading_stream is None:
            await self.connect()

        backoff_seconds = 2
        while True:
            try:
                if self._trading_stream is None:
                    from alpaca.trading.stream import TradingStream

                    self._trading_stream = TradingStream(
                        self.api_key, self.secret_key, paper=self.paper
                    )
                # #78: subscribe exactly once per stream instance —
                # re-subscribing on every reconnect fires duplicate fill
                # callbacks (double-counted PnL/exposure).
                if self._subscribed_stream is not self._trading_stream:
                    self._trading_stream.subscribe_trade_updates(self._on_trade_update)
                    self._subscribed_stream = self._trading_stream
                # NOTE: TradingStream.run is blocking sync; it occupies one
                # executor thread for the stream lifetime (no asyncio-native
                # alternative in alpaca-py). The thread is released when run
                # returns/raises; on error the stream is stopped below before
                # discard so a healthy stream never leaks a thread per retry.
                await asyncio.to_thread(self._trading_stream.run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Alpaca trade stream disconnected, retrying",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "error": str(exc),
                            "retry_in_seconds": backoff_seconds,
                        }
                    },
                )
                stream = self._trading_stream
                self._trading_stream = None
                if self._subscribed_stream is stream:
                    self._subscribed_stream = None
                if stream is not None:
                    stop_ws = getattr(stream, "stop_ws", None)
                    stop = getattr(stream, "stop", None)
                    try:
                        if callable(stop_ws):
                            result = stop_ws()
                            if inspect.isawaitable(result):
                                await result
                        elif callable(stop):
                            result = stop()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        pass
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
            else:
                # #78: a clean run() return must not spin — back off before
                # re-subscribing (reconnect storm) and reset the backoff.
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = 2

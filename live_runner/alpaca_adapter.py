from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, NamedTuple
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
from live_runner.exit_signals import ExitBelowStepError
from live_runner.symbol_class import canonical_crypto_symbol, classify_symbol
from shared import venue_calendar
from shared.alpaca_symbols import classify_alpaca_symbol, crypto_pair_key
from shared.constants import DATA_DIR
from shared.database import (
    BROKER_CLOSE_REASON_ALPACA_LEG,
    BROKER_CLOSE_REASON_ALPACA_STOP_LEG,
    BROKER_CLOSE_REASON_ALPACA_TARGET_LEG,
    apply_live_execution_commissions,
    broker_close_signal_id,
    get_live_signal_broker_order_id,
    get_live_signal_filled_qty,
    get_live_signal_filled_qty_for_broker_order,
    get_live_signal_reconciliation_cursor,
    list_live_executions_for_broker_orders,
    list_live_executions_missing_commission,
    list_live_signal_ids_for_reconciliation,
    record_live_broker_side_close_fill,
    save_live_execution,
    save_live_execution_up_to_cumulative,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.fill_keys import canonical_utc_timestamp
from shared.logging import get_logger
from shared.lot_grid import floor_qty_to_step
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

#: Pages of ``_ALPACA_HELD_LEG_LOOKUP_LIMIT`` newest orders scanned back when a held-leg page
#: comes back full. A full page means the account's history reaches further back than one
#: page, not that the page was cut off (an account trading one bracket a day fills a page in
#: under two years); the budget is what stops a sweep from paging forever.
_ALPACA_HELD_LEG_PAGE_LIMIT = 20

#: States in which a bracket leg still rests at the broker: ``held`` is the OCO-inactive
#: leg of a filled parent, ``pending_cancel`` one whose cancel has not settled yet and may
#: still fill (#198). A leg whose cancel has settled says so in the listing.
_RESTING_LEG_STATUSES = frozenset({"held", "pending_cancel"})

#: Terminal states in which an order left the book without executing. Only these let a
#: refuse-cancel be excused: a ``filled``/``partially_filled`` order executed (a stop that
#: fills on a position that is already flat opens a short), a ``done_for_day`` order is only
#: out for the session and resumes with the next one, and a row nobody could read proves
#: nothing. alpaca-py 0.44.0's ``OrderStatus`` defines no "done" - its end-of-day state is
#: ``done_for_day`` - so there is nothing here for a shorter spelling to match. Verified on
#: paper 2026-11-20: the leg Alpaca removes as the OCO sibling of a cancelled take profit
#: is still readable by id, as ``canceled``.
_GONE_WITHOUT_EXECUTING = frozenset({"canceled", "expired", "rejected"})

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
    up would exceed the risk-sized quantity. The shared lot-grid floor's
    decimal noise band (1e-6 of a step) can return a value a hair ABOVE a
    real fraction that close to the next whole share -- at most 1e-6 of a
    share, below Alpaca's quantity resolution (#278).
    """
    return floor_qty_to_step(qty, 1.0)


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


def _bracket_leg_close_reason(leg: Any) -> str:
    """Which kind of bracket leg this is, for the close signal's audit metadata."""
    order_type = _order_type(leg)
    if order_type == "stop":
        return BROKER_CLOSE_REASON_ALPACA_STOP_LEG
    if order_type == "limit":
        return BROKER_CLOSE_REASON_ALPACA_TARGET_LEG
    return BROKER_CLOSE_REASON_ALPACA_LEG


def _stop_candidates(order: Any) -> Iterator[Any]:
    """Orders that may be a position's working stop: the order, and a filled parent's held legs.

    A leg of a parent that has not filled belongs to a pending entry and protects nothing
    that is open yet; amending it would leave the position unprotected while the log says
    the stop was trailed. A dead (cancelled/filled/expired) leg is not a protection either.
    """
    yield order
    if _order_status(order) != "filled":
        return
    for leg in _order_legs(order):
        if _order_status(leg) == "held":
            yield leg


def _page_back_cursor(orders: list[Any]) -> datetime | None:
    """The ``until`` that asks for the page before a newest-first listing's oldest row.

    The cursor goes *past* that timestamp by a microsecond, never before it: ``until``
    bounds the window at the old end, so a cursor short of the oldest row would omit the
    orders submitted in the gap, and the filled parent that owns the resting leg can be
    one of them. A microsecond past it instead asks for the same rows again under every
    inclusive/exclusive reading, and a row that comes back twice is deduplicated by id -
    unlike a row that was skipped, which cannot be recovered. None when a row carries no
    readable timestamp, which leaves the caller unable to page back.
    """
    oldest: datetime | None = None
    for order in orders:
        stamp = _order_attr(order, "submitted_at", None)
        if isinstance(stamp, datetime):
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if oldest is None or stamp < oldest:
                oldest = stamp
    return oldest + timedelta(microseconds=1) if oldest is not None else None


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


#: Nominal bar spacing per supported market-data timeframe, for the
#: session-aware staleness limit in ``_raise_if_market_data_stale``.
_ALPACA_TIMEFRAME_STEP_MINUTES = {
    "1m": 1.0,
    "5m": 5.0,
    "15m": 15.0,
    "30m": 30.0,
    "1h": 60.0,
    "1d": 1440.0,
}


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


def _order_filled_qty(order: Any) -> float | None:
    """What the broker reports an order as filled, or ``None`` when it cannot be read.

    A terminal state is not proof of "left without executing" on its own: a sell stop
    that partly fills and is then cancelled is ``canceled`` *with* a quantity. So an
    excusal needs this number, and a number nobody could read answers "unknown" -
    the same fail-closed answer the rest of this file gives a row it cannot read.
    """
    value = _to_float(_order_attr(order, "filled_qty", None), default=float("nan"))
    return value if math.isfinite(value) else None


class _RestingLeg(NamedTuple):
    """A bracket leg that still rests, and the filled parent it rests under.

    The parent travels with the leg because a leg can arrive without its own
    ``symbol``: this file's other order readers fall back to the parent's
    (``_protective_stop_of``, ``_best_stop_price`` and ``_has_working_stop`` all
    accept an empty leg symbol), and a symbol nobody could read is not "no
    position" - it is a position the sweep cannot place.
    """

    leg: Any
    parent: Any


class _LivePosition(NamedTuple):
    """The broker's current position on a symbol.

    ``flat`` is True only for a readable zero (or no row at all): a listing that
    failed, or a quantity that cannot be read, leaves the position unknown - with
    ``stop_qty``/``stop_side`` unset - so a caller can neither skip a position it
    cannot see nor size an order from a quantity it could not read. The two stop
    fields are the quantity and side of the stop that protects the position: the
    *reducing* side's, so a long position's stop is a sell.
    """

    flat: bool
    stop_qty: float | None = None
    stop_side: str | None = None


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
        self._bracket_leg_order_id_to_signal_id: dict[str, str] = {}

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
        except Exception:  # except-kind: money-decision
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
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Alpaca execution reconciliation failed; fills may be missing until retry",
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
        trading_client = self._trading_client
        if trading_client is None:
            raise RuntimeError("Alpaca adapter is not connected; cannot fetch the parent order")
        stored_order_id = await asyncio.to_thread(get_live_signal_broker_order_id, signal_id)
        if stored_order_id:
            try:
                from alpaca.trading.requests import GetOrderByIdRequest
            except ImportError:
                # An alpaca-py without the nested filter request: the flat fetch below
                # still carries the parent's own fill. `ImportError`, not
                # `ModuleNotFoundError`: a missing NAME inside an importable
                # module raises the former, and the fallback must still fire.
                GetOrderByIdRequest = None  # type: ignore[assignment,misc]
            if GetOrderByIdRequest is not None:
                try:
                    return await asyncio.to_thread(
                        trading_client.get_order_by_id,
                        stored_order_id,
                        GetOrderByIdRequest(nested=True),
                    )
                except TypeError:
                    # An alpaca-py whose get_order_by_id takes no filter: ask for the
                    # flat order instead.
                    pass
            return await asyncio.to_thread(
                trading_client.get_order_by_id, stored_order_id
            )
        return await asyncio.to_thread(trading_client.get_order_by_client_id, signal_id)

    def _parent_signal_id_for_broker_order(self, broker_order_id: str | None) -> str | None:
        """The signal whose bracket leg this broker order id names, or ``None``."""
        if not broker_order_id:
            return None
        return self._bracket_leg_order_id_to_signal_id.get(str(broker_order_id))

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
        except Exception:  # except-kind: money-decision
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
        except Exception as exc:  # except-kind: money-decision
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
        except Exception as exc:  # except-kind: loop-survival
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
        except Exception as exc:  # except-kind: money-decision
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
        # `missing` handled every None above: it either raised or returned, so
        # both are prices here. Re-stated as a check rather than a suppression:
        # mypy cannot correlate the `missing` scan with the loop below or with
        # the return, and an explicit refusal keeps the invariant visible (and
        # fails closed with a message instead of a TypeError) if the control
        # flow above is ever reordered.
        if stop_loss is None or take_profit is None:
            raise ValueError(
                "Refusing to submit Alpaca order with a missing level "
                f"(signal_id={signal.id}, symbol={signal.symbol}); "
                "opening an unbounded position is not allowed."
            )
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
        floored = floor_qty_to_step(qty, step)
        if floored <= 0.0:
            raise ExitBelowStepError(
                f"Alpaca exit for {symbol} ignored: the requested qty {qty:g} is below "
                f"the quantity step {step:g}.",
                requested_qty=qty,
                qty_step=step,
            )
        return floored

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
        order = await asyncio.to_thread(trading_client.submit_order, order_data=request)
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
        # Re-read after the await: `connect()` is what establishes the client, and
        # mypy drops member narrowing across it, so without this a failed connect
        # would raise AttributeError instead of saying the adapter is not ready.
        trading_client = self._trading_client
        if trading_client is None:
            raise RuntimeError(
                "Alpaca adapter must be connected before requesting the account"
            )
        account = await asyncio.to_thread(trading_client.get_account)
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
        trading_client = self._trading_client
        if trading_client is None:
            raise RuntimeError(
                "Alpaca adapter must be connected before requesting the account"
            )
        account = await asyncio.to_thread(trading_client.get_account)
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
        from alpaca.data.enums import Adjustment, DataFeed
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
        # Bound after the guard and used by the closures below: mypy does not
        # carry a member narrowing into a nested function, so reaching through
        # `self` there would need a suppression (and would read the attribute
        # twice, which a concurrent disconnect could turn into None).
        historical_client = self._historical_data_client

        def _fetch_crypto(slack_multiplier: float) -> pd.DataFrame:
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest

            crypto_client = self._crypto_data_client
            if crypto_client is None:
                crypto_client = CryptoHistoricalDataClient(
                    self.api_key, self.secret_key
                )
                self._crypto_data_client = crypto_client
            request = CryptoBarsRequest(
                symbol_or_symbols=[normalized_symbol],
                timeframe=resolved_timeframe,
                start=_alpaca_market_data_start(
                    timeframe, bounded_limit, slack_multiplier=slack_multiplier
                ),
                limit=1000,
                sort=Sort.DESC,
            )
            # `get_crypto_bars` is declared `BarSet | RawData`: the RawData
            # branch only happens when the client was built with
            # `use_raw_data=True`, which this adapter never does (see the
            # construction above), so the answer is the BarSet and `.df` is on
            # it. Stated because mypy cannot see that construction flag.
            return crypto_client.get_crypto_bars(request).df  # type: ignore[union-attr]

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
                # alpaca-py's own enum members, not the wire strings: the
                # request model declares these fields as `Adjustment`/`DataFeed`,
                # so a misspelled or deleted member is a type error instead of a
                # runtime validation failure buried in a broker call. Both are
                # `str` enums, so the serialised request is byte-identical.
                adjustment=Adjustment.RAW,
                feed=DataFeed.IEX,
                # Newest first. The response is capped at `limit`, so an
                # ascending request over a widened window would return the
                # oldest bars in it. Sorted back to ascending below.
                sort=Sort.DESC,
            )
            return historical_client.get_stock_bars(request).df

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
        self._raise_if_market_data_stale(
            normalized_symbol, timeframe, is_crypto, newest, now
        )
        return frame

    @staticmethod
    def _raise_if_market_data_stale(
        symbol: str,
        timeframe: str,
        is_crypto: bool,
        newest: pd.Timestamp,
        now: pd.Timestamp,
    ) -> None:
        """Closed-vs-stale check driven by the venue calendar (#133).

        A bar gap while the market is CLOSED is expected, not stale; the same
        gap while a session is OPEN means the feed stopped. When the venue
        cannot be resolved the legacy flat-age rule applies as a fallback.
        """
        max_age = (
            timedelta(days=7)
            if str(timeframe).strip().lower() == "1d"
            else timedelta(days=5)
        )
        try:
            calendar = venue_calendar.resolve_venue_calendar(
                symbol=symbol,
                timeframe=timeframe,
                asset_class="crypto" if is_crypto else "equity",
                source="alpaca",
                data_dir=str(DATA_DIR),
            )
        except Exception:  # except-kind: loop-survival
            logger.warning(
                "Venue calendar resolution failed for %s; using flat staleness age",
                symbol,
                exc_info=True,
            )
            calendar = None
        if calendar is None:
            if newest < now - max_age:
                raise ValueError(
                    f"Alpaca market data is stale for {symbol}: "
                    f"newest bar {newest.isoformat()} exceeds {max_age}"
                )
            return
        state = calendar.session_state(now)
        if state == "unknown":
            # The calendar cannot speak for ``now`` (e.g. an exchange-session
            # cache whose fetched range does not reach it): same flat-age
            # fallback as an unresolved venue — never assume closed.
            if newest < now - max_age:
                raise ValueError(
                    f"Alpaca market data is stale for {symbol}: "
                    f"newest bar {newest.isoformat()} exceeds {max_age}"
                )
            return
        if state == "closed":
            # Legitimate closures (weekends, holidays) can stack to several
            # days; beyond that the feed itself is presumed dead.
            if newest < now - timedelta(days=10):
                raise ValueError(
                    f"Alpaca market data is stale for {symbol}: "
                    f"newest bar {newest.isoformat()} is over 10 days old "
                    "while the venue reports closed"
                )
            return
        step_minutes = _ALPACA_TIMEFRAME_STEP_MINUTES.get(
            str(timeframe).strip().lower(), 60.0
        )
        # While a session is open the newest bar should be at most ~one
        # trading day behind; three bar-steps floor keeps tiny timeframes
        # from tripping on ordinary bar jitter. The 780-minute floor is
        # roughly two NYSE sessions — 1440 trading minutes would let a dead
        # equity feed go unnoticed for nearly four sessions.
        limit_minutes = max(3.0 * step_minutes, 780.0)
        trading_age = calendar.trading_minutes_between(newest, now)
        if trading_age > limit_minutes:
            raise ValueError(
                f"Alpaca market data is stale for {symbol}: "
                f"newest bar {newest.isoformat()} is {trading_age:.0f} "
                f"trading minutes old while the venue is open "
                f"(limit {limit_minutes:.0f})"
            )

    async def get_quote(self, symbol: str) -> LiveQuote:
        """Latest top-of-book quote for the #125 entry-deviation gate.

        Same IEX feed as :meth:`get_market_data`. A missing quote or a missing
        or non-numeric side raises instead of returning an invented price; the
        daemon additionally rejects zero, non-finite and crossed quotes.
        """
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required for an Alpaca quote")
        if not self._connected or self._historical_data_client is None:
            raise RuntimeError("Alpaca adapter must be connected before requesting a quote")
        request = StockLatestQuoteRequest(
            symbol_or_symbols=[normalized_symbol],
            # The alpaca-py `DataFeed` member, not the wire string, for the same
            # reason as `get_market_data` above; `DataFeed` is a `str` enum, so
            # the request this sends is unchanged.
            feed=DataFeed.IEX,
        )
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
            except Exception as exc:  # except-kind: money-decision
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
        except Exception as exc:  # except-kind: money-decision
            return ControlOutcome(
                completed=completed,
                unidentified=tuple(unidentified),
                failed=(*failed, f"verify:{exc}"),
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

        # `_control_order_id` returns ``str | None``, so the None case is filtered
        # in the comprehension (as in `pending_cancel` below) instead of being
        # discarded from the finished set, which the checker cannot model.
        still_working = {
            order_id
            for order in remaining
            if (order_id := _control_order_id(_order_attr(order, "id"))) is not None
            and order_id not in protected
            and not accepted_pending_cancel(order_id, order)
        }
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
        is sometimes only visible there. The book is already scoped to *symbol*
        by the caller, and a held leg placed on it through its parent carries no
        symbol of its own - so an empty top-level symbol is this symbol's order,
        not another symbol's.
        """
        for order in orders:
            if _order_symbol(order) not in {"", symbol}:
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
        itself is on the reducing side. The book is already scoped to *symbol*
        by the caller, and a held leg placed on it through its parent carries no
        symbol of its own - so an empty top-level symbol is this symbol's order,
        not another symbol's.
        """
        wanted = "buy" if str(side).strip().lower() == "buy" else "sell"
        for order in orders:
            if _order_symbol(order) not in {"", symbol}:
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

    async def _live_position(self, symbol: str) -> _LivePosition:
        """The broker's position on *symbol* as it stands right now.

        Only a *readable, zero* quantity counts as flat, and only a readable,
        non-zero one counts as a position to size a stop from. A listing that
        cannot be read, or a row whose quantity cannot be read, says "unknown"
        and is reported as such (logged, and neither flat nor sized): a restore
        that skipped on evidence it does not have would leave a live position
        naked, and one that guessed a quantity would sell a position that is no
        longer there.
        """
        try:
            positions = await self.get_positions()
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Could not re-read positions before restoring a stop; the restore "
                "is not sized from a position nobody could read",
                extra={"data": {"symbol": symbol, "error": repr(exc)}},
            )
            return _LivePosition(flat=False)
        for position in positions:
            if str(position.get("symbol") or "").strip().upper() != symbol:
                continue
            if position.get("qty_unparseable"):
                return _LivePosition(flat=False)
            qty = _position_qty(position)
            if _is_flat(qty):
                return _LivePosition(flat=True)
            return _LivePosition(
                flat=False, stop_qty=abs(qty), stop_side="sell" if qty > 0 else "buy"
            )
        return _LivePosition(flat=True)

    async def _restore_stop(
        self, client: Any, symbol: str, stop_price: float | None
    ) -> bool:
        """Put a stop back on a position whose close was refused.

        The protections were cancelled to unblock the close, so a refusal would
        otherwise leave the position naked. Restoring the stop the strategy had
        is the only acceptable outcome short of a fill; failing to do so is
        logged at CRITICAL because it means live unprotected exposure.

        Returns whether the position is protected afterwards, which is also true
        when a stop still works at the broker or the position is already flat.
        Re-submitting next to a working stop would leave two stops on one
        position, and the second becomes an orphan the moment the first fills -
        an orphaned sell-stop opens a short. The same rule covers a working book
        nobody could re-read: it cannot show that no stop works, so the restore is
        refused rather than guessed, and the position is unprotected until the
        next enforcement retry.

        The stop is sized and sided from the position as the broker reports it
        *now*, not from the snapshot the flatten started from: the leg that was
        cancelled to unblock the close can have filled on its way out, and a stop
        sized from the old quantity oversells what is left - into a short when it
        triggers. A position the broker will not report is not sized either: the
        restore refuses and says so rather than placing an order for a quantity
        nobody read.
        """
        live = await self._live_position(symbol)
        # The position may already be flat: the stop that was cancelled to unblock the
        # close can have been the thing that closed it (a cancel accepted and then the
        # stop filling, or a race with the fill). Restoring a stop onto a flat position
        # then creates the orphan this file's sweeps exist to remove - a triggered
        # sell-stop with nothing to sell opens a short.
        if live.flat:
            logger.warning(
                "Not restoring a stop: the broker reports the position flat",
                extra={"data": {"symbol": symbol}},
            )
            return True
        if live.stop_qty is None or live.stop_side is None:
            logger.critical(
                "Cannot restore a stop: the broker's live position could not be read, "
                "so the position is unprotected until the next retry",
                extra={"data": {"symbol": symbol}},
            )
            return False
        try:
            working = await self._working_book_on_symbol(client, symbol)
        except Exception as exc:  # except-kind: money-decision
            # Not restoring is the bounded failure. The re-read is what proves no stop
            # still works, and a book nobody could read proves nothing - the held-leg
            # lookup inside it is paged, so it is also the part that can fail. Restoring
            # anyway would place a second stop beside whatever the broker kept: two
            # stops reduce a position twice, and the extra one is an orphan the moment
            # the first fills. The position is then unprotected until the next
            # enforcement retry, which is the same state every other refusal below
            # reports - and the sweep takes no unbounded risk.
            logger.critical(
                "Not restoring a stop: the working book could not be re-read, so it "
                "cannot be shown that no stop still works",
                extra={"data": {"symbol": symbol, "error": repr(exc)}},
            )
            return False
        if self._has_working_stop(working, symbol, live.stop_side):
            logger.warning(
                "Not restoring a stop: one still works at the broker",
                extra={"data": {"symbol": symbol}},
            )
            return True
        if stop_price is None:
            logger.critical(
                "Cannot restore a stop: the cancelled orders carried no stop price, "
                "so the position is unprotected until the next retry",
                extra={"data": {"symbol": symbol, "qty": live.stop_qty}},
            )
            return False
        try:
            order_side, time_in_force, stop_request_cls = self._load_stop_order_types()
            request = stop_request_cls(
                symbol=symbol,
                qty=live.stop_qty,
                side=order_side.SELL
                if live.stop_side == "sell"
                else order_side.BUY,
                time_in_force=time_in_force.DAY,
                stop_price=stop_price,
            )
            await asyncio.to_thread(client.submit_order, order_data=request)
        except Exception as exc:  # except-kind: money-decision
            logger.critical(
                "Failed to restore a stop on a position whose close was refused; "
                "the position is unprotected",
                extra={
                    "data": {
                        "symbol": symbol,
                        "qty": live.stop_qty,
                        "stop_price": stop_price,
                        "error": repr(exc),
                    }
                },
            )
            return False
        logger.warning(
            "Restored a stop after the broker refused to close the position",
            extra={
                "data": {
                    "symbol": symbol,
                    "qty": live.stop_qty,
                    "stop_price": stop_price,
                }
            },
        )
        return True

    async def _abandon_flatten(
        self,
        client: Any,
        symbol: str,
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
            if not await self._restore_stop(client, symbol, stop_price):
                return f"{reason};NO-STOP-RESTORED"
        return reason

    async def _flatten_symbol(self, client: Any, symbol: str) -> str | None:
        """Flatten one symbol; see :meth:`_flatten_symbol_locked`."""
        async with self._order_book_lock():
            return await self._flatten_symbol_locked(client, symbol)

    async def _flatten_symbol_locked(self, client: Any, symbol: str) -> str | None:
        """Cancel this symbol's working orders, then close its position.

        Returns ``None`` on success, or a short reason for the failure.

        The cancel is not optional. Alpaca reserves the quantity of every
        reducing-side order - a bracket's stop leg included, even though only the
        filled parent lists it - so `close_position` is refused with "insufficient
        qty available" while the SL/TP legs are still working. That is the one
        place where "keep the protections until the flatten succeeds" cannot
        hold - the protections are exactly what blocks the flatten - so the stop
        is re-placed on every failure after a protection was removed.

        Nothing here is sized from the position the flatten was handed: every
        decision - which orders still rest, and the quantity and side a restored
        stop needs - is read back from the broker, because the leg that fills on
        its way out is exactly the case this sweep has to survive.
        """
        on_symbol = await self._working_book_on_symbol(client, symbol)
        stop_price = self._best_stop_price(on_symbol, symbol)

        cancelled_any = False
        for order in on_symbol:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                return await self._abandon_flatten(
                    client,
                    symbol,
                    stop_price,
                    f"unreadable-order:{_order_attr(order, 'id', None)!r}",
                    removed_protection=cancelled_any,
                )
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:  # except-kind: money-decision
                # The broker's OCO may already have removed this leg with the
                # sibling just cancelled. A refused cancel of an order that is no
                # longer resting is not a dropped cancel; treating it as one
                # would fail - and restore a stop on - every bracket flatten.
                if await self._cancel_already_gone(client, order_id):
                    cancelled_any = True
                    continue
                return await self._abandon_flatten(
                    client,
                    symbol,
                    stop_price,
                    f"cancel:{order_id}:{exc}",
                    removed_protection=cancelled_any,
                )
            cancelled_any = True

        # A cancel the broker accepted but did not apply still reserves the
        # quantity, and the close would then be refused. Confirm by re-read,
        # held legs included: a stop leg the broker kept listing is exactly what
        # must stop this flatten from closing a position it did not dis-arm.
        try:
            remaining_orders = await self._working_book_on_symbol(client, symbol)
        except Exception as exc:  # except-kind: money-decision
            return await self._abandon_flatten(
                client,
                symbol,
                stop_price,
                f"verify:{exc}",
                removed_protection=cancelled_any,
            )
        still_working = [
            _control_order_id(_order_attr(order, "id"))
            for order in remaining_orders
        ]
        if still_working:
            return await self._abandon_flatten(
                client,
                symbol,
                stop_price,
                "orders-still-working:"
                + ",".join(str(order_id) for order_id in still_working),
                removed_protection=cancelled_any,
            )

        try:
            await asyncio.to_thread(client.close_position, symbol)
        except Exception as exc:  # except-kind: money-decision
            return await self._abandon_flatten(
                client,
                symbol,
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
            problem = await self._flatten_symbol(client, symbol)
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
        cannot disarm a live position - and a leg's symbol is read from the
        parent it rests under when the leg itself carries none, because an empty
        symbol would read as "no position" and cancel a live position's stop.
        A bracket's held stop leg is in scope
        (#245): it is absent from every ``status=open`` listing, so a sweep that
        only read those would leave exactly the leg that can still trigger - and
        a triggered sell-stop with nothing to sell opens a short.

        The legs are looked up account-wide. A bracket's take profit names the symbol a
        `held` stop leg rests on, but an OTO entry - which the adapter submits for an
        unbounded take profit - has a stop leg and no take-profit sibling, so the symbols
        the open listing names are not enough. The lookup's 20-page budget bounds the
        history it reads, and it is the one caller that passes ``symbols=None``: these
        helpers have no default scope, so no caller can reach one by omission.

        The cancels are then verified by re-reading the book, as the flatten verifies
        its own: the broker's acknowledgement is not proof the leg is gone, and Alpaca
        keeps a cancel it accepted listed as ``pending_cancel`` until it settles - a leg
        that can still fill. Everything this sweep cancelled that is still resting is
        reported as a failure, so the sweep is incomplete and the daemon keeps the
        deployment unenforced instead of stamping it stopped over a leg that can still
        open a short.
        """
        client = await self._require_trading_client()
        open_orders = await self._list_open_orders(client)
        open_symbols = {
            str(position.get("symbol") or "").strip().upper()
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        }
        legs = await self._list_resting_legs(client, symbols=None)
        # A bracket leg can arrive without its own symbol, and an empty one reads as
        # "no position on this symbol": the sweep would then cancel the stop leg of a
        # live position, which is the one thing this sweep promises never to do. The
        # parent the leg rests under is what names the symbol, so the leg's symbol is
        # resolved from it before anything is cancelled.
        leg_symbols = {
            leg_id: _order_symbol(resting.leg) or _order_symbol(resting.parent)
            for resting in legs
            if (leg_id := _control_order_id(_order_attr(resting.leg, "id"))) is not None
        }
        orders = self._merge_held_legs(open_orders, legs)
        completed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        cancelled: list[str] = []
        cancelled_symbols: dict[str, str] = {}
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                unidentified.append(f"order:{_order_attr(order, 'id', None)!r}")
                continue
            symbol = _order_symbol(order) or leg_symbols.get(order_id, "")
            if symbol in open_symbols:
                continue
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:  # except-kind: money-decision
                # The broker's OCO may already have removed this leg with the
                # sibling just cancelled; a refusal for an order that is no
                # longer resting is not a failure the daemon must retry forever.
                if await self._cancel_already_gone(client, order_id):
                    continue
                failed.append(f"{order_id}:{exc}")
                continue
            cancelled.append(order_id)
            cancelled_symbols[order_id] = symbol
            completed += 1
        if cancelled:
            # A held leg is only ever visible under its filled parent, so the
            # verification has to read the whole working book, not the open
            # listing: the one order this sweep exists to remove is the one the
            # open listing cannot show.
            #
            # Scoped to the symbols this sweep actually cancelled rather than read
            # account-wide a second time: the daemon runs this sweep every sync tick
            # and retries while it is incomplete, so two 20-page account-wide scans
            # per pass is what turns a long history into a rate limit, and a rate
            # limit fails the sweep closed - the retry then pays the same price. A
            # leg is found under the filled parent of its own symbol, so those
            # symbols are enough. An id whose symbol could not be resolved has no
            # such bound, and the sweep reads account-wide rather than assume it.
            #
            # The bound is the symbols, not the count of cancels: one symbol can
            # carry several cancels - both legs of an orphaned bracket, or two
            # brackets on one symbol - and counting ids instead of the symbols they
            # sit on would fall back to the account-wide re-read on exactly the
            # ordinary bracket sweep. So only an unresolved symbol, which names no
            # bound at all, widens the scope.
            verify_symbols: set[str] | None = {
                symbol for symbol in cancelled_symbols.values() if symbol
            }
            if any(not symbol for symbol in cancelled_symbols.values()):
                verify_symbols = None
            try:
                remaining = await self._list_working_book(
                    client, symbols=verify_symbols
                )
            except Exception as exc:  # except-kind: money-decision
                failed.append(f"verify:{exc}")
            else:
                still_resting = {
                    _control_order_id(_order_attr(order, "id")) for order in remaining
                }
                still_resting.discard(None)
                failed.extend(
                    f"{order_id}:still-working"
                    for order_id in cancelled
                    if order_id in still_resting
                )
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
        orders = await self._list_open_orders(client)
        orders.extend(await self._list_filled_parents_with_held_legs(client, set(reducing_sides)))
        stops: dict[str, ProtectiveStop] = {}
        for order in orders:
            for candidate in _stop_candidates(order):
                stop = _protective_stop_of(candidate, order, reducing_sides)
                if stop is not None:
                    stops[stop.order_id] = stop
        return list(stops.values())

    async def _list_filled_parents_with_held_legs(
        self, client: Any, symbols: set[str] | None
    ) -> list[Any]:
        """Filled bracket parents that still have a bracket leg resting at the broker.

        Observed on Alpaca paper (2026-11-16): once a bracket entry fills, the stop leg
        stays ``held`` and is absent from every ``status=open`` listing, nested or flat.
        It is only reachable as a leg of the filled parent in a ``status=all`` listing,
        and it can be replaced like any working stop. ``symbols`` scopes the lookup (the
        positions being acted on); ``None`` looks account-wide, which only the orphan sweep
        may ask for: its whole job is symbols that no longer have a position, and an OTO
        entry's stop leg has no take-profit sibling that ``status=open`` would name. The
        20-page budget bounds even the account-wide history, and there is no default
        scope, so no caller reaches an account-wide scan by omission.

        The listing is newest-first and capped at 500 rows, so one call is only the
        newest 500 orders of the account's whole history - a mature account fills that
        page with terminal orders. A full page therefore pages back over the history
        until a page comes back not full (the end of the history) rather than failing:
        an account that trades a bracket per day reaches 500 orders within two years,
        and a flatten that then failed would leave the position open. The 20-page
        budget is the end of that, and a 20th full page fails with it: a history of
        exactly 10,000 orders cannot be told apart from a longer one, and either way
        the pages past it hold orders this lookup cannot vouch for. A failure to list
        is not swallowed either way: a missing stop would be reported as "no stop" for
        a position that does have one.
        """
        get_orders = getattr(client, "get_orders", None)
        if not callable(get_orders):
            return []
        if symbols is not None and not symbols:
            return []
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        collected: dict[str, Any] = {}
        until: datetime | None = None
        for _ in range(_ALPACA_HELD_LEG_PAGE_LIMIT):
            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                symbols=sorted(symbols) if symbols is not None else None,
                nested=True,
                limit=_ALPACA_HELD_LEG_LOOKUP_LIMIT,
                until=until,
            )
            result = await asyncio.to_thread(get_orders, filter=request)
            if not isinstance(result, (list, tuple)):
                raise RuntimeError(
                    f"Alpaca order list has an unrecognised shape ({type(result).__name__}); "
                    "refusing to report a clean sweep"
                )
            for order in result:
                row_key = _control_order_id(_order_attr(order, "id"))
                collected[row_key or f"row-{len(collected)}"] = order
            if len(result) < _ALPACA_HELD_LEG_LOOKUP_LIMIT:
                return self._parents_with_resting_legs(collected.values())
            until = _page_back_cursor(list(result))
            if until is None:
                raise RuntimeError(
                    "Alpaca's order rows carry no submitted timestamp, so the next page "
                    "cannot be asked for; refusing to treat the list as complete"
                )
        raise RuntimeError(
            f"Alpaca's order history reached "
            f"{_ALPACA_HELD_LEG_PAGE_LIMIT * _ALPACA_HELD_LEG_LOOKUP_LIMIT} orders "
            "without an empty page; refusing to treat the list as complete"
        )

    @staticmethod
    def _parents_with_resting_legs(orders: Any) -> list[Any]:
        """Filled bracket parents that still have a bracket leg resting at the broker."""
        return [
            order
            for order in orders
            if _order_status(order) == "filled"
            and any(_order_status(leg) in _RESTING_LEG_STATUSES for leg in _order_legs(order))
        ]

    async def _list_resting_legs(
        self, client: Any, *, symbols: set[str] | None
    ) -> list[_RestingLeg]:
        """The bracket legs themselves, the ones no ``status=open`` listing shows (#245).

        A leg in ``pending_cancel`` still rests: its cancel has not settled and it can
        still fill, so a sweep that dropped it would report a clean book while the leg
        was still reserving quantity (#198). A leg whose cancel did settle says so in
        the listing and is not resting. Each leg comes back with the parent it rests
        under, so a caller can resolve a symbol the leg itself does not carry.
        """
        parents = await self._list_filled_parents_with_held_legs(client, symbols)
        return [
            _RestingLeg(leg=leg, parent=parent)
            for parent in parents
            for leg in _order_legs(parent)
            if _order_status(leg) in _RESTING_LEG_STATUSES
        ]

    async def _list_working_book(
        self, client: Any, *, symbols: set[str] | None
    ) -> list[Any]:
        """Every order still resting at the broker, or a hard failure - never a partial book.

        ``status=open`` is not the book on a bracketed position: the stop leg stays
        ``held`` and is absent from every open listing, so a sweep built on it alone
        cannot see the leg - the one order that can still trigger on the position. That
        is the probe's ``orders-still-working``/``NO-STOP-RESTORED`` result: ``held``
        legs of filled parents are added here. The open listing itself is account-wide;
        ``symbols`` scopes the held-leg lookup, and every caller filters the result it
        gets back.
        """
        orders = await self._list_open_orders(client)
        legs = await self._list_resting_legs(client, symbols=symbols)
        return self._merge_held_legs(orders, legs)

    async def _working_book_on_symbol(self, client: Any, symbol: str) -> list[Any]:
        """The working book filtered to *symbol*, held legs placed by their parent.

        A held leg can arrive without a symbol of its own, and the parent it rests
        under is what names the symbol, exactly as the orphan sweep resolves it:
        filtered by the leg's own symbol such a leg reads as "another symbol",
        survives the cancel, its stop goes on reserving the position's quantity,
        and the close is then refused for that reservation - a flatten that cannot
        finish. The leg lookup is scoped to *symbol* already, so a leg without a
        symbol of its own belongs to this flatten; one whose parent names another
        symbol is still left out. A leg is placed by object identity as well as
        by id: the lookup above was scoped to this symbol's parents, so a leg it
        returns belongs here even when its id cannot be read - and dropping it
        would let the flatten close over a protection nobody verified.
        """
        open_orders = await self._list_open_orders(client)
        legs = await self._list_resting_legs(client, symbols={symbol})
        orders = self._merge_held_legs(open_orders, legs)
        leg_symbols = {
            leg_id: _order_symbol(resting.leg) or _order_symbol(resting.parent)
            for resting in legs
            if (leg_id := _control_order_id(_order_attr(resting.leg, "id"))) is not None
        }
        leg_symbols_by_object = {
            id(resting.leg): _order_symbol(resting.leg) or _order_symbol(resting.parent)
            for resting in legs
        }
        on_symbol = []
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            resolved = (
                _order_symbol(order)
                or leg_symbols_by_object.get(id(order))
                or leg_symbols.get(order_id or "", "")
            )
            if resolved == symbol:
                on_symbol.append(order)
        return on_symbol

    @staticmethod
    def _merge_held_legs(
        open_orders: list[Any], legs: Iterable[_RestingLeg]
    ) -> list[Any]:
        """One entry per resting order: the open listing plus the legs it cannot show.

        A broker can list the same leg both ways, and an entry listed twice would hide
        which orders actually remain. An unreadable id cannot be matched, so it is always
        kept - the sweep still has to report it rather than silently drop it.
        """
        orders = list(open_orders)
        listed = {
            order_id
            for order_id in (
                _control_order_id(_order_attr(order, "id")) for order in orders
            )
            if order_id is not None
        }
        for resting in legs:
            leg = resting.leg
            leg_id = _control_order_id(_order_attr(leg, "id"))
            if leg_id is None or leg_id not in listed:
                orders.append(leg)
                if leg_id is not None:
                    listed.add(leg_id)
        return orders

    async def _cancel_already_gone(self, client: Any, order_id: str) -> bool:
        """Whether a refused cancel was for an order that is no longer resting.

        A bracket is an OCO pair: cancelling one leg cancels the other at the broker,
        so the second cancel of the pair is refused for an order the broker has already
        removed (paper probe 2026-11-20: the sibling the take profit took with it reads
        back by id as ``canceled``). That refusal is not a dropped cancel - the read
        below is the proof - and a sweep that reported it as one would fail, and in the
        flatten restore a stop on, every real bracket.

        "Not resting" is not itself the proof: an order also leaves the book by executing,
        and a stop that fills on a position that is already flat opens a short. So the id
        is read back and only an order that left *without executing* counts - in a
        terminal state, and with nothing of its own filled. A broker that cannot find the
        order, a row nobody could read or a quantity nobody could read proves nothing,
        and answers "not gone" so the refusal stands.

        The read is by id, not a fresh listing: a terminal state already proves the order
        is not resting, and the book a re-list would fetch is the page-limited nested
        history - up to 20 requests of 500 rows - which on a mature account is what turns
        one refused bracket cancel into a rate limit, and a rate limit fails the sweep
        closed. A sweep with several refusals must not pay that price per refusal.
        """
        get_order = getattr(client, "get_order_by_id", None)
        if not callable(get_order):
            return False
        try:
            order = await asyncio.to_thread(get_order, order_id)
        except Exception:  # fail closed: a read that raises proves nothing  # except-kind: money-decision
            return False
        if _order_status(order) not in _GONE_WITHOUT_EXECUTING:
            return False
        filled_qty = _order_filled_qty(order)
        return filled_qty is not None and filled_qty <= 0.0

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
        # Re-read after the await, for the same reason as `get_balance`: a
        # `connect()` that left the client unset must raise, not AttributeError.
        trading_client = self._trading_client
        if trading_client is None:
            raise RuntimeError(
                "Alpaca adapter must be connected before listing positions"
            )
        rows = await asyncio.to_thread(trading_client.get_all_positions)
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
                logger.warning(
                    "Trade update client_order_id is not a UUID",
                    extra={"data": {"client_order_id": str(client_order_id)}},
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
        except Exception as exc:  # except-kind: money-decision
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
                # Bound above in the `is None` branch and re-assigned on
                # reconnect, so mypy still sees `Any | None` here even though the
                # `is None` check just established it.
                await asyncio.to_thread(self._trading_stream.run)  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # except-kind: loop-survival
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
                    except Exception as exc:  # except-kind: loop-survival
                        logger.warning(
                            "Alpaca trading stream stop failed during disconnect",
                            extra={"data": {"error": str(exc)}},
                        )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
            else:
                # #78: a clean run() return must not spin — back off before
                # re-subscribing (reconnect storm) and reset the backoff.
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = 2

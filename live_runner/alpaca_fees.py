"""Alpaca crypto fee (``CFEE``) backfill: turning account activities into per-execution commissions.

Why a backfill (#124). Alpaca equities are commission-free, but crypto fills pay a maker/taker fee,
and Alpaca neither reports it on the order nor on the trade update (alpaca-py 0.44 has no commission
field anywhere). The fee appears only as a ``CFEE`` account activity, and per the docs
(https://docs.alpaca.markets/docs/crypto-fees) it is "calculated and posted end of day", "charged on
the credited crypto asset/fiat (what you receive)". The activities reference adds that a fee's
``created_at`` "is typically the day after the trade date (in UTC)", while its ``date`` is the trade
date. So at fill time the commission is unknown (stored NULL) and this module prices it later.

What the docs do NOT say, and how each gap is closed here:

* **Per order or aggregated.** The fee is charged "per trade", but a ``CFEE`` carries no order id -
  only ``date``, ``symbol`` (``ETHUSD``), ``qty`` (asset fee, negative) or ``net_amount`` (USD fee,
  negative). Whether Alpaca posts one entry per fill or one per day and pair is not documented, so
  both are handled the same way: the fees of one (pair, trade date, kind) are summed and that total
  is **allocated pro rata by received quantity** across the account's fills of the pair that day -
  base-asset quantity for buys (whose fee is in the base asset), USD notional for sells (whose fee
  is in USD). The group total is exact; the per-order split is exact for a single order and
  otherwise ignores the maker/taker rate difference between orders. Each order's share is spread
  over its local rows by ``filled_qty``.
* **Which "day".** Not documented whether ``date`` is the UTC or the New York day. A fill whose UTC
  and New York dates agree is unambiguous; a fill between UTC midnight and New York midnight could
  be either, and every group it could belong to is left NULL.
* **USD conversion.** A base-asset fee is valued at the price of the fill that received the asset:
  it is that much of the bought asset, paid for at that price. A non-USD quote (``ETH/BTC``) would
  need a second price this module does not have, so it is left NULL.

Everything else that cannot be priced honestly **fails closed**: the group's executions keep a NULL
commission and the reason is logged; nothing is ever written as ``0.0``. In particular a group is
refused when the pair traded on an order this account never recorded (a manual trade shares the
day's fee), when local and broker quantities or sides disagree, when any row of it is already
priced, when an order spans two fee days, when a fee entry is not ``executed`` or cannot be
classified, when a USD ``FEE`` entry names the pair, when a side has fills but no fee of its kind
(or a fee but no fills), or when the fee exceeds ``MAX_PLAUSIBLE_FEE_RATE`` of what was received.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

#: A trade date's fees are looked up only once this long has passed since its UTC midnight: the
#: whole trade day, plus the following UTC day on which the docs say the fee is typically created.
CFEE_POSTING_DELAY = timedelta(days=2)
#: Unknown commissions older than this are left alone; they stay NULL ("net PnL incomplete").
COMMISSION_BACKFILL_LOOKBACK = timedelta(days=30)
#: Alpaca's highest documented crypto rate is 0.25% (tier 1 taker). A fee above 1% of what was
#: received is not a fee this module understands.
MAX_PLAUSIBLE_FEE_RATE = Decimal("0.01")
ACTIVITY_PAGE_SIZE = 100
MAX_ACTIVITY_PAGES = 200

_NEW_YORK = ZoneInfo("America/New_York")
_QTY_REL_TOL = 1e-9
_QTY_ABS_TOL = 1e-12


def pair_key(symbol: Any) -> str:
    """``BTC/USD`` and ``BTCUSD`` (as a ``CFEE`` spells it) to one comparable key."""
    return str(symbol or "").replace("/", "").strip().upper()


def split_crypto_pair(symbol: Any) -> tuple[str, str] | None:
    """``("BTC", "USD")`` for ``BTC/USD``; ``None`` when the symbol is not a ``BASE/QUOTE`` pair."""
    parts = str(symbol or "").strip().upper().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def candidate_fee_days(moment: datetime) -> frozenset[date]:
    """The trade dates a fill at ``moment`` could be booked under: its UTC and New York dates."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return frozenset({moment.astimezone(timezone.utc).date(), moment.astimezone(_NEW_YORK).date()})


def day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=timezone.utc)


def fee_day_is_posted(day: date, now: datetime) -> bool:
    return now >= day_start_utc(day) + CFEE_POSTING_DELAY


def activity_timestamp(moment: datetime) -> str:
    """The ``YYYY-MM-DDTHH:MM:SSZ`` form the activities endpoint accepts for ``after``/``until``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_account_activities(
    client: Any, activity_type: str, *, after: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Every ``activity_type`` activity created in ``(after, until)``, following ``page_token``.

    ``GET /v2/account/activities/{activity_type}`` over alpaca-py's generic REST ``get`` - the SDK
    has no activities method. Ascending, so ``page_token`` (the last id of a page) continues after
    it. Raises on any malformed page or a token that does not advance, so a partial listing is never
    mistaken for a complete one.
    """
    items: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    token: str | None = None
    for _ in range(MAX_ACTIVITY_PAGES):
        params: dict[str, Any] = {
            "after": activity_timestamp(after),
            "until": activity_timestamp(until),
            "direction": "asc",
            "page_size": ACTIVITY_PAGE_SIZE,
        }
        if token is not None:
            params["page_token"] = token
        page = client.get(f"/account/activities/{activity_type}", params)
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise ValueError(
                f"Alpaca {activity_type} activities response is not a list of objects: "
                f"{type(page).__name__}"
            )
        items.extend(page)
        if len(page) < ACTIVITY_PAGE_SIZE:
            return items
        token = str(page[-1].get("id") or "").strip()
        if not token or token in seen_tokens:
            raise ValueError(f"Alpaca {activity_type} activities pagination did not advance")
        seen_tokens.add(token)
    raise ValueError(
        f"Alpaca {activity_type} activities exceeded {MAX_ACTIVITY_PAGES} pages; refusing a "
        "partial listing"
    )


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return number


def _parse_activity_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing transaction_time")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


@dataclass(frozen=True)
class BrokerFill:
    order_id: str
    pair: str
    side: str
    qty: Decimal
    price: Decimal
    at: datetime

    @property
    def days(self) -> frozenset[date]:
        return candidate_fee_days(self.at)

    @property
    def received(self) -> Decimal:
        """What the fill credited: the base asset on a buy, USD notional on a sell."""
        return self.qty if self.side == "buy" else self.qty * self.price


def parse_fill_activity(raw: dict[str, Any]) -> BrokerFill:
    pair = pair_key(raw.get("symbol"))
    if not pair:
        raise ValueError("missing symbol")
    side = str(raw.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported side {raw.get('side')!r}")
    order_id = str(raw.get("order_id") or "").strip()
    if not order_id:
        raise ValueError("missing order_id")
    qty = _decimal(raw.get("qty"))
    price = _decimal(raw.get("price"))
    if qty <= 0 or price <= 0:
        raise ValueError(f"non-positive qty {qty} or price {price}")
    return BrokerFill(
        order_id=order_id,
        pair=pair,
        side=side,
        qty=qty,
        price=price,
        at=_parse_activity_time(raw.get("transaction_time")),
    )


@dataclass(frozen=True)
class FeeTotals:
    """A (pair, trade date)'s posted fees, or why they cannot be used."""

    asset: Decimal = Decimal(0)
    usd: Decimal = Decimal(0)
    problem: str | None = None


@dataclass
class _FeeIndex:
    totals: dict[tuple[str, date], FeeTotals] = field(default_factory=dict)
    broken: str | None = None

    def get(self, pair: str, day: date) -> FeeTotals:
        if self.broken is not None:
            return FeeTotals(problem=self.broken)
        return self.totals.get((pair, day), FeeTotals())

    def _fail(self, pair: str, day: date, problem: str) -> None:
        self.totals[(pair, day)] = FeeTotals(problem=problem)

    def add_cfee(self, raw: dict[str, Any]) -> None:
        try:
            day = date.fromisoformat(str(raw.get("date") or "").strip())
        except ValueError:
            self.broken = f"a CFEE entry has no usable date ({raw.get('date')!r})"
            return
        pair = pair_key(raw.get("symbol"))
        if not pair:
            self.broken = f"a CFEE entry for {day} names no symbol"
            return
        current = self.totals.get((pair, day), FeeTotals())
        if current.problem is not None:
            return
        status = str(raw.get("status") or "").strip().lower()
        if status != "executed":
            self._fail(pair, day, f"a CFEE entry has status {raw.get('status')!r}, not 'executed'")
            return
        try:
            qty = _decimal(raw.get("qty", "0"))
            net_amount = _decimal(raw.get("net_amount", "0"))
        except ValueError as exc:
            self._fail(pair, day, f"a CFEE entry is unreadable: {exc}")
            return
        if qty < 0 and net_amount == 0:
            self.totals[(pair, day)] = FeeTotals(asset=current.asset - qty, usd=current.usd)
        elif net_amount < 0 and qty == 0:
            self.totals[(pair, day)] = FeeTotals(asset=current.asset, usd=current.usd - net_amount)
        else:
            self._fail(
                pair,
                day,
                f"a CFEE entry is neither an asset fee nor a USD fee (qty={raw.get('qty')!r}, "
                f"net_amount={raw.get('net_amount')!r})",
            )

    def add_usd_fee(self, raw: dict[str, Any]) -> None:
        pair = pair_key(raw.get("symbol"))
        if not pair:
            # A USD fee that names no symbol (a regulatory or account fee) is not a crypto fee.
            return
        try:
            day = date.fromisoformat(str(raw.get("date") or "").strip())
        except ValueError:
            self.broken = f"a FEE entry for {pair} has no usable date ({raw.get('date')!r})"
            return
        self._fail(
            pair,
            day,
            f"a USD FEE entry ({raw.get('activity_sub_type')!r}) names the pair; whether it is "
            "part of the crypto fee is not documented",
        )


def build_fee_index(cfees: list[dict[str, Any]], usd_fees: list[dict[str, Any]]) -> _FeeIndex:
    index = _FeeIndex()
    for raw in cfees:
        index.add_cfee(raw)
    for raw in usd_fees:
        index.add_usd_fee(raw)
    return index


@dataclass(frozen=True)
class GroupPlan:
    """The outcome for one (pair, trade date): commissions to write, or the reason not to."""

    pair: str
    day: date
    commissions: dict[str, float]
    problem: str | None = None


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=_QTY_REL_TOL, abs_tol=_QTY_ABS_TOL)


@dataclass
class _Inputs:
    fills_by_order: dict[str, list[BrokerFill]]
    fills_by_pair: dict[str, list[BrokerFill]]
    rows_by_order: dict[str, list[dict[str, Any]]]
    fees: _FeeIndex
    broken_pairs: dict[str, str]


def _group_problem(inputs: _Inputs, pair: str, day: date) -> tuple[str | None, list[BrokerFill]]:
    if pair in inputs.broken_pairs:
        return inputs.broken_pairs[pair], []
    group_fills = [fill for fill in inputs.fills_by_pair.get(pair, []) if day in fill.days]
    for fill in group_fills:
        if len(fill.days) > 1:
            return (
                f"order {fill.order_id} filled at {fill.at.isoformat()}, which is a different "
                "date in UTC and in New York; which day's CFEE carries it is not documented",
                [],
            )
    for order_id in {fill.order_id for fill in group_fills}:
        rows = inputs.rows_by_order.get(order_id, [])
        if not rows:
            return (
                f"the pair also traded on order {order_id}, which this account never recorded "
                "(a manual or foreign trade shares the day's CFEE)",
                [],
            )
        order_fills = inputs.fills_by_order[order_id]
        if any(fill.days != frozenset({day}) for fill in order_fills):
            return f"order {order_id} has fills on more than one fee day", []
        sides = {fill.side for fill in order_fills}
        actions = {row["action"] for row in rows}
        if len(sides) != 1 or actions != sides:
            return (
                f"order {order_id} is a {sorted(sides)} at the broker but {sorted(actions)} "
                "locally",
                [],
            )
        if any(pair_key(row["symbol"]) != pair for row in rows):
            return f"order {order_id} is recorded locally under another symbol", []
        quotes = {(split_crypto_pair(row["symbol"]) or ("", ""))[1] for row in rows}
        if quotes != {"USD"}:
            return f"the pair is not quoted in USD ({sorted(quotes)}); no USD conversion", []
        if any(row["commission"] is not None for row in rows):
            return f"order {order_id} already has a known commission on part of its rows", []
        local_qty = sum(float(row["filled_qty"]) for row in rows)
        broker_qty = float(sum(fill.qty for fill in order_fills))
        if not _close(local_qty, broker_qty):
            return (
                f"order {order_id} has {local_qty} recorded locally but {broker_qty} at the broker",
                [],
            )
    return None, group_fills


def _allocate(
    inputs: _Inputs, pair: str, day: date, group_fills: list[BrokerFill]
) -> GroupPlan:
    fees = inputs.fees.get(pair, day)
    if fees.problem is not None:
        return GroupPlan(pair, day, {}, fees.problem)
    buys = [fill for fill in group_fills if fill.side == "buy"]
    sells = [fill for fill in group_fills if fill.side == "sell"]
    received_buy = sum((fill.received for fill in buys), Decimal(0))
    received_sell = sum((fill.received for fill in sells), Decimal(0))
    for label, fills, fee, received in (
        ("base-asset", buys, fees.asset, received_buy),
        ("USD", sells, fees.usd, received_sell),
    ):
        if fills and fee <= 0:
            return GroupPlan(pair, day, {}, f"no {label} CFEE is posted for the day's fills")
        if fee > 0 and not fills:
            return GroupPlan(pair, day, {}, f"a {label} CFEE is posted but nothing received it")
        if fills and fee > received * MAX_PLAUSIBLE_FEE_RATE:
            return GroupPlan(
                pair,
                day,
                {},
                f"the {label} CFEE {fee} exceeds {MAX_PLAUSIBLE_FEE_RATE:%} of the {received} "
                "received; refusing an implausible fee",
            )

    usd_by_order: dict[str, Decimal] = defaultdict(Decimal)
    for fill in buys:
        usd_by_order[fill.order_id] += fees.asset * fill.received / received_buy * fill.price
    for fill in sells:
        usd_by_order[fill.order_id] += fees.usd * fill.received / received_sell

    commissions: dict[str, float] = {}
    for order_id, order_usd in usd_by_order.items():
        rows = inputs.rows_by_order[order_id]
        order_qty = sum(float(row["filled_qty"]) for row in rows)
        for row in rows:
            share = float(row["filled_qty"]) / order_qty
            commissions[row["execution_id"]] = float(order_usd) * share
    return GroupPlan(pair, day, commissions)


def plan_commission_backfill(
    *,
    candidates: list[dict[str, Any]],
    fill_activities: list[dict[str, Any]],
    cfee_activities: list[dict[str, Any]],
    fee_activities: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[GroupPlan], list[str]]:
    """Plan the commissions to backfill: one `GroupPlan` per (pair, trade date) to resolve.

    ``candidates`` are the account's crypto executions with an unknown commission; ``local_rows``
    are every execution the account recorded on the orders the broker reports for those pairs.
    Returns the plans plus messages for candidates that could not be put in any group.
    """
    pairs = {pair_key(row["symbol"]) for row in candidates}
    fills_by_order: dict[str, list[BrokerFill]] = defaultdict(list)
    fills_by_pair: dict[str, list[BrokerFill]] = defaultdict(list)
    broken_pairs: dict[str, str] = {}
    for raw in fill_activities:
        raw_pair = pair_key(raw.get("symbol"))
        if raw_pair and raw_pair not in pairs:
            continue
        try:
            fill = parse_fill_activity(raw)
        except ValueError as exc:
            problem = f"a FILL activity ({raw.get('id')!r}) is unreadable: {exc}"
            for pair in [raw_pair] if raw_pair else pairs:
                broken_pairs[pair] = problem
            continue
        fills_by_order[fill.order_id].append(fill)
        fills_by_pair[fill.pair].append(fill)

    rows_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in local_rows:
        rows_by_order[row["broker_order_id"]].append(row)
    inputs = _Inputs(
        fills_by_order=fills_by_order,
        fills_by_pair=fills_by_pair,
        rows_by_order=rows_by_order,
        fees=build_fee_index(cfee_activities, fee_activities),
        broken_pairs=broken_pairs,
    )

    unplaced: list[str] = []
    groups: set[tuple[str, date]] = set()
    for row in candidates:
        order_fills = fills_by_order.get(row["broker_order_id"], [])
        if not order_fills:
            unplaced.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): no FILL "
                "activity found for its order"
            )
            continue
        days = {day for fill in order_fills for day in fill.days}
        if len(days) != 1:
            unplaced.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): its fills map "
                f"to more than one possible CFEE date {sorted(str(day) for day in days)}"
            )
            continue
        groups.add((pair_key(row["symbol"]), next(iter(days))))

    plans: list[GroupPlan] = []
    for pair, day in sorted(groups):
        if not fee_day_is_posted(day, now):
            continue
        problem, group_fills = _group_problem(inputs, pair, day)
        if problem is not None:
            plans.append(GroupPlan(pair, day, {}, problem))
            continue
        plans.append(_allocate(inputs, pair, day, group_fills))
    return plans, unplaced

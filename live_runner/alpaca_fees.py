"""Alpaca crypto fee (``CFEE``) backfill: turning account activities into per-execution commissions.

Why a backfill (#124). Alpaca equities are commission-free, but crypto fills pay a maker/taker fee,
and Alpaca neither reports it on the order nor on the trade update (alpaca-py 0.44 has no commission
field anywhere). The fee appears only as a ``CFEE`` account activity. Real account data (#149)
showed that the earlier assumption - ``CFEE.date`` is the trade date, posted end of day - is wrong:
``date`` is the New York calendar date of ``created_at``, i.e. the *posting* date, and the trade
date is not on the activity at all. Fees are observed posted in batches: per-fill asset fees within
seconds of each other, and delayed batches hours later (a ~51 h posting delay was seen over a
weekend). So at fill time the commission is unknown (stored NULL) and this module prices it later.

What the data shows, and how each fact is used here:

* **Posting batches.** ``CFEE`` entries are clustered by ``created_at``: entries created within
  ``CFEE_BATCH_GAP`` of each other are one posting batch. A batch's fee total covers the fills
  whose ``transaction_time`` falls after the previous batch's latest ``created_at`` and up to its
  own - "the fills since the previous batch". Fills after the last batch's ``created_at`` have no
  posted fee yet and are retried on a later pass, never refused.
* **Two fee kinds.** Asset fees (on buys) carry ``symbol:"BTCUSD"`` and a negative ``qty`` in the
  base asset. USD fees (on sells) carry only a negative ``net_amount`` and *no* ``symbol``, so
  they cannot be attributed to a pair individually: they form a per-account, per-batch USD pool.
  The pool is allocated to a pair only when exactly one crypto pair has sells in the batch window;
  otherwise the USD part fails closed while asset fees are still priced.
* **Which "window".** Batch boundaries are posting instants, not trade days, so the UTC-vs-New
  York date ambiguity of the old model does not exist: a fill belongs to the earliest batch
  posted at or after its ``transaction_time``.

Within a (pair, batch) group the fee totals are **allocated pro rata by received quantity** -
base-asset quantity for buys (whose fee is in the base asset), USD notional for sells (whose fee
is in USD) - across the account's fills of the pair in the window. Each order's share is spread
over its local rows by ``filled_qty``. A base-asset fee is valued at the price of the fill that
received the asset; a non-USD quote (``ETH/BTC``) has no USD conversion here and fails closed.

Everything else that cannot be priced honestly **fails closed**, scoped per (pair, batch): the
affected executions keep a NULL commission and the reason is logged; nothing is ever written as
``0.0``. A group is refused when the pair traded on an order this account never recorded (a manual
trade shares the batch's fee), when local and broker quantities or sides disagree, when any row
of it is already priced, when an order's fills span two batches, when a fee entry is not
``executed`` or cannot be classified, when a USD ``FEE`` entry names the pair, when a side has
fills but no fee of its kind (or a fee but no fills on that side), or when the fee exceeds
``MAX_PLAUSIBLE_FEE_RATE`` of what was received. Pricing is per side: a batch whose USD pool
cannot be attributed still prices each pair's asset fees.

The first fetched batch is special: its predecessor (the batch before the fetch horizon) is
unknown, so its coverage window reaches before the fetched history. The caller probes for an
earlier ``CFEE`` batch (and, when there is none, for fills before the ``FILL`` fetch bound) and
passes ``first_batch_open`` when either may exist; an open first batch fails closed whenever it
would price fills - a >``COMMISSION_BACKFILL_LOOKBACK`` posting stall can therefore leave a
batch unpriced rather than risk assigning fills to the wrong batch.

Two more guards keep a partially-posted or misattributed batch from being written:

* **Settle guard.** A batch is priced only once ``now - batch.end > CFEE_BATCH_GAP``: while a
  posting run is still receiving entries its totals are partial, and pricing them would write a
  fee that can never be corrected (the group is then refused as already priced). Until then the
  batch's fills stay pending and are retried on a later pass.
* **Per-fill consistency.** #149 observed one ``CFEE`` entry per fill, with the asset fee's
  ``price`` equal to the fill price at 2 dp. Before a side is priced, the batch's fee entries
  for that side must match the side's fills in the window one-for-one: the same count, and for
  asset fees the same 2 dp prices. A batch whose entries cannot be paired with the fills it
  claims to cover (an interleaved delayed posting, an aggregate entry, a missing entry) fails
  closed for that side instead of spreading a fee over the wrong fills.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.alpaca_symbols import crypto_pair_key, split_crypto_pair

#: ``CFEE`` entries created within this gap of each other are one posting batch. Real batches
#: post per-fill entries within seconds; distinct posting runs are observed hours to days apart.
CFEE_BATCH_GAP = timedelta(minutes=15)
#: Unknown commissions older than this are left alone; they stay NULL ("net PnL incomplete").
COMMISSION_BACKFILL_LOOKBACK = timedelta(days=30)
#: How far before the earliest candidate the ``CFEE`` listing reaches: far enough back to find
#: the batch preceding the ones that can cover candidate fills, so each batch's window start is
#: known. A posting stall longer than this leaves the first fetched batch unpriceable.
CFEE_LOOKBACK_MARGIN = COMMISSION_BACKFILL_LOOKBACK
#: Broker fill times and local execution timestamps describe the same fill; this skew widens
#: the candidate-time range when deciding which batches can hold a candidate's fee.
FILL_WINDOW_SKEW = timedelta(days=1)
#: Alpaca's highest documented crypto rate is 0.25% (tier 1 taker). A fee above 1% of what was
#: received is not a fee this module understands.
MAX_PLAUSIBLE_FEE_RATE = Decimal("0.01")
ACTIVITY_PAGE_SIZE = 100
MAX_ACTIVITY_PAGES = 200
#: Lower bound for the boundary probes: the account's first activity is always after this.
ACTIVITY_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: ``CFEE``/``FEE`` ``after``/``until`` filter by New York calendar day (#149), not by instant,
#: so every window is widened by one day and re-filtered client-side on the real timestamp.
ACTIVITY_WINDOW_OVERLAP = timedelta(days=1)

_QTY_REL_TOL = 1e-9
_QTY_ABS_TOL = 1e-12


def pair_key(symbol: Any) -> str:
    """``BTC/USD`` and ``BTCUSD`` (as a ``CFEE`` spells it) to one comparable key."""
    return crypto_pair_key(symbol)


def activity_timestamp(moment: datetime) -> str:
    """The ``YYYY-MM-DDTHH:MM:SSZ`` form the activities endpoint accepts for ``after``/``until``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_account_activities(
    client: Any,
    activity_type: str,
    *,
    after: datetime,
    until: datetime,
    direction: str = "asc",
    page_size: int = ACTIVITY_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Every ``activity_type`` activity created in ``(after, until)``, following ``page_token``.

    ``GET /v2/account/activities/{activity_type}`` over alpaca-py's generic REST ``get`` - the SDK
    has no activities method. ``direction`` orders by id; ``page_token`` (the last id of a page)
    continues after it in the same direction. Raises on any malformed page or a token that does
    not advance, so a partial listing is never mistaken for a complete one.
    """
    items: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    token: str | None = None
    for _ in range(MAX_ACTIVITY_PAGES):
        params: dict[str, Any] = {
            "after": activity_timestamp(after),
            "until": activity_timestamp(until),
            "direction": direction,
            "page_size": page_size,
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
        if len(page) < page_size:
            return items
        token = str(page[-1].get("id") or "").strip()
        if not token or token in seen_tokens:
            raise ValueError(f"Alpaca {activity_type} activities pagination did not advance")
        seen_tokens.add(token)
    raise ValueError(
        f"Alpaca {activity_type} activities exceeded {MAX_ACTIVITY_PAGES} pages; refusing a "
        "partial listing"
    )


def _activity_page(client: Any, activity_type: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    page = client.get(f"/account/activities/{activity_type}", params)
    if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
        raise ValueError(
            f"Alpaca {activity_type} activities response is not a list of objects: "
            f"{type(page).__name__}"
        )
    return page


def has_earlier_account_activity(
    client: Any,
    activity_type: str,
    *,
    before: datetime,
    field: str = "created_at",
) -> bool:
    """Whether any ``activity_type`` activity has a timestamp strictly before ``before``.

    One request, no pagination: ascending from the epoch, the first item is the account's
    oldest activity inside the window, and it is before ``before`` exactly when an earlier one
    exists. ``until`` is widened by ``ACTIVITY_WINDOW_OVERLAP`` because ``CFEE``/``FEE`` filter
    by New York calendar day; the returned item's real timestamp is checked client-side, so a
    same-day item newer than ``before`` cannot produce a false positive. An activity without a
    usable timestamp raises, so the caller fails closed.
    """
    page = _activity_page(
        client,
        activity_type,
        {
            "after": activity_timestamp(ACTIVITY_EPOCH),
            "until": activity_timestamp(before + ACTIVITY_WINDOW_OVERLAP),
            "direction": "asc",
            "page_size": 1,
        },
    )
    if not page:
        return False
    return _parse_activity_time(page[0].get(field)) < before


class _CryptoFillScanUnknown:
    """The crypto-fill scan could not prove the earliest fill; callers fail closed."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CRYPTO_FILL_SCAN_UNKNOWN"


#: ``earliest_crypto_fill_at`` answer when the scan cannot finish honestly - an item whose
#: symbol or crypto timestamp cannot be classified, pagination that stalls, or
#: ``MAX_ACTIVITY_PAGES`` reached. The first CFEE batch's window is then treated as open,
#: exactly as the unfinished probe answered before.
CRYPTO_FILL_SCAN_UNKNOWN = _CryptoFillScanUnknown()


def earliest_crypto_fill_at(
    client: Any,
    *,
    until: datetime,
    page_size: int = ACTIVITY_PAGE_SIZE,
) -> datetime | _CryptoFillScanUnknown | None:
    """The earliest crypto ``FILL`` ``transaction_time`` in the listing up to ``until``.

    Pages ``FILL`` ascending from the epoch and keeps the minimum ``transaction_time`` over
    crypto symbols; equity fills cannot sit in a crypto CFEE batch's coverage and are
    skipped. ``None`` means the scan completed and found no crypto fill.
    ``CRYPTO_FILL_SCAN_UNKNOWN`` means it could not prove the earliest - a missing symbol,
    an unreadable crypto timestamp, pagination that stalls, or ``MAX_ACTIVITY_PAGES``
    reached - so the caller must fail closed. Bounded by ``MAX_ACTIVITY_PAGES`` requests;
    a found datetime never changes and ``CRYPTO_FILL_SCAN_UNKNOWN`` fails closed either
    way, so a caller that asks on every pass should memoize those - but ``None`` is not
    fixed history (a fill posted after the scan can still predate the caller's bound
    later) and must be rescanned (the trading path shares Alpaca's ~200 requests/minute
    budget, and the daemon's ``wait_for`` cannot stop a ``to_thread`` scan).
    """
    earliest: datetime | None = None
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_ACTIVITY_PAGES):
        params: dict[str, Any] = {
            "after": activity_timestamp(ACTIVITY_EPOCH),
            "until": activity_timestamp(until),
            "direction": "asc",
            "page_size": page_size,
        }
        if token is not None:
            params["page_token"] = token
        page = _activity_page(client, "FILL", params)
        if not page:
            return earliest
        for raw in page:
            symbol_text = str(raw.get("symbol") or "").strip()
            if not symbol_text:
                return CRYPTO_FILL_SCAN_UNKNOWN
            if split_crypto_pair(symbol_text) is None:
                continue
            try:
                moment = _parse_activity_time(raw.get("transaction_time"))
            except ValueError:
                return CRYPTO_FILL_SCAN_UNKNOWN
            if earliest is None or moment < earliest:
                earliest = moment
        if len(page) < page_size:
            return earliest
        token = str(page[-1].get("id") or "").strip()
        if not token or token in seen_tokens:
            return CRYPTO_FILL_SCAN_UNKNOWN
        seen_tokens.add(token)
    return CRYPTO_FILL_SCAN_UNKNOWN


def has_earlier_crypto_fill(
    client: Any,
    *,
    before: datetime,
    page_size: int = ACTIVITY_PAGE_SIZE,
) -> bool:
    """Whether any crypto ``FILL`` activity has ``transaction_time`` strictly before ``before``.

    The first fetched ``CFEE`` batch's coverage window is open-ended, so an unfetched *crypto*
    fill inside it would make the batch's fee totals cover fills the per-fill consistency check
    never sees. Equity fills are irrelevant - the planner ignores them - so this scans ``FILL``
    ascending from the epoch and classifies each item's symbol instead of trusting the
    single oldest-activity probe. ``until`` is widened by ``ACTIVITY_WINDOW_OVERLAP`` for the
    same reason as the other activity listings; the client-side ``< before`` check makes the
    extra day harmless. Every branch that cannot prove the earliest fill - a missing or
    unreadable symbol/timestamp, stalled pagination, the page cap - answers True, so an
    unfinished scan fails closed.
    """
    scan = earliest_crypto_fill_at(
        client, until=before + ACTIVITY_WINDOW_OVERLAP, page_size=page_size
    )
    if isinstance(scan, _CryptoFillScanUnknown):
        return True
    return scan is not None and scan < before


def select_activity_window(
    items: list[dict[str, Any]],
    *,
    after: datetime,
    until: datetime,
    field: str = "created_at",
) -> list[dict[str, Any]]:
    """Client-side ``(after, until]`` filter with dedupe by id for a widened listing.

    Day-granular ``CFEE``/``FEE`` filtering can return items outside the requested instants;
    this keeps exactly the intended window. An item whose timestamp cannot be parsed is kept
    so the downstream classifier marks the index broken instead of silently dropping it.
    """
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        activity_id = str(raw.get("id") or "").strip()
        if activity_id:
            if activity_id in seen:
                continue
            seen.add(activity_id)
        try:
            moment = _parse_activity_time(raw.get(field))
        except ValueError:
            selected.append(raw)
            continue
        if after < moment <= until:
            selected.append(raw)
    return selected


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return number


_PRICE_CENT = Decimal("0.01")


def _price_cents(value: Any) -> Decimal | None:
    """A price at the 2 dp precision ``CFEE`` entries report it (#149); ``None`` if unusable."""
    try:
        return _decimal(value).quantize(_PRICE_CENT)
    except (ValueError, InvalidOperation):
        return None


def _parse_activity_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing activity timestamp")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def cfee_created_at(raw: dict[str, Any]) -> datetime:
    try:
        return _parse_activity_time(raw.get("created_at"))
    except ValueError as exc:
        raise ValueError(
            f"CFEE entry has no usable created_at ({raw.get('created_at')!r})"
        ) from exc


@dataclass(frozen=True)
class BrokerFill:
    order_id: str
    pair: str
    side: str
    qty: Decimal
    price: Decimal
    at: datetime

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


@dataclass
class _FeeBatch:
    """One posting run of ``CFEE`` activities and the fill window it covers.

    ``end`` is the latest ``created_at`` in the cluster; the batch covers fills with
    ``start < transaction_time <= end`` where ``start`` is the previous batch's ``end``
    (unbounded for the first fetched batch - see ``open_start``).
    """

    end: datetime
    open_start: bool
    asset: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    pair_usd: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    usd_pool: Decimal = Decimal(0)
    #: Per-fill consistency evidence (#149 posts one entry per fill): entry counts per pair and
    #: the asset entries' 2 dp prices (``None`` when the entry's price is unusable).
    asset_entries: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    asset_prices: dict[str, list[Decimal | None]] = field(
        default_factory=lambda: defaultdict(list)
    )
    pair_usd_entries: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    usd_pool_entries: int = 0
    #: ``None`` key = a problem that could concern any pair in the batch; a pair key scopes it.
    problems: dict[str | None, str] = field(default_factory=dict)


def _cfee_symbol(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(pair, problem)`` for a CFEE's symbol: ``(None, None)`` means a symbol-less entry."""
    symbol_text = str(raw.get("symbol") or "").strip()
    if not symbol_text:
        return None, None
    if split_crypto_pair(symbol_text) is None:
        return None, (
            f"a CFEE entry names a non-crypto symbol ({raw.get('symbol')!r}); whether its fee "
            "is a crypto fee is not documented"
        )
    return pair_key(symbol_text), None


def _add_cfee(batch: _FeeBatch, raw: dict[str, Any]) -> None:
    pair, symbol_problem = _cfee_symbol(raw)
    if symbol_problem is not None:
        batch.problems[None] = symbol_problem
        return
    status = str(raw.get("status") or "").strip().lower()
    if status != "executed":
        batch.problems[pair] = f"a CFEE entry has status {raw.get('status')!r}, not 'executed'"
        return
    try:
        qty = _decimal(raw.get("qty", "0"))
        net_amount = _decimal(raw.get("net_amount", "0"))
    except ValueError as exc:
        batch.problems[pair] = f"a CFEE entry is unreadable: {exc}"
        return
    if qty < 0 and net_amount == 0:
        if pair is None:
            batch.problems[None] = (
                "a CFEE asset fee names no symbol; it cannot be attributed to a pair"
            )
        else:
            batch.asset[pair] -= qty
            batch.asset_entries[pair] += 1
            batch.asset_prices[pair].append(_price_cents(raw.get("price")))
    elif net_amount < 0 and qty == 0:
        if pair is None:
            batch.usd_pool -= net_amount
            batch.usd_pool_entries += 1
        else:
            batch.pair_usd[pair] -= net_amount
            batch.pair_usd_entries[pair] += 1
    else:
        batch.problems[pair] = (
            "a CFEE entry is neither an asset fee nor a USD fee "
            f"(qty={raw.get('qty')!r}, net_amount={raw.get('net_amount')!r})"
        )


@dataclass
class _FeeBatches:
    batches: list[_FeeBatch]
    broken: str | None = None


def build_fee_batches(
    cfee_activities: list[dict[str, Any]], fee_activities: list[dict[str, Any]]
) -> _FeeBatches:
    """Cluster ``CFEE`` activities into posting batches and total their fees per pair.

    Symbol-less USD fees collect into each batch's ``usd_pool``; USD fees naming a crypto pair
    collect into ``pair_usd``. ``FEE`` activities that name a crypto pair poison the batch whose
    window contains their ``created_at`` - whether such a fee is part of the crypto fee is not
    documented. A ``CFEE`` without a usable ``created_at`` cannot be placed in any batch, so the
    whole index is marked broken instead of guessing.
    """
    result = _FeeBatches(batches=[])
    timed: list[tuple[datetime, dict[str, Any]]] = []
    for raw in cfee_activities:
        try:
            moment = cfee_created_at(raw)
        except ValueError as exc:
            result.broken = str(exc)
            return result
        timed.append((moment, raw))
    timed.sort(key=lambda item: item[0])
    for moment, raw in timed:
        if result.batches and moment - result.batches[-1].end <= CFEE_BATCH_GAP:
            batch = result.batches[-1]
        else:
            batch = _FeeBatch(end=moment, open_start=False)
            result.batches.append(batch)
        batch.end = moment
        _add_cfee(batch, raw)
    for raw in fee_activities:
        pair, _problem = _cfee_symbol(raw)
        if pair is None:
            # A USD fee that names no crypto pair (a regulatory or account fee) is not a crypto fee.
            continue
        try:
            moment = _parse_activity_time(raw.get("created_at"))
        except ValueError:
            result.broken = (
                f"a FEE entry for {pair} has no usable created_at ({raw.get('created_at')!r})"
            )
            return result
        for batch in result.batches:
            if moment <= batch.end:
                batch.problems[pair] = (
                    f"a USD FEE entry ({raw.get('activity_sub_type')!r}) names the pair; whether "
                    "it is part of the crypto fee is not documented"
                )
                break
        # A FEE posted after the last batch covers fills whose fee batch is not posted yet.
    return result


def fee_fill_window_start(
    timestamps: list[datetime], cfee_activities: list[dict[str, Any]]
) -> datetime | None:
    """The earliest ``FILL`` fetch bound covering every batch that could hold a candidate's fee.

    ``timestamps`` are the candidates' execution times (naive values are read as UTC). Returns
    ``None`` when no posted batch can cover any candidate yet - every candidate is simply retried
    on a later pass. Raises ValueError when a ``CFEE`` activity has no usable ``created_at``.

    The batch ends come from ``build_fee_batches`` itself, so the ``FILL`` fetch bound can never
    drift from the windows the planner later prices. ``cfee_activities`` may include a batch the
    settle guard later drops as still posting; that only widens the fetch, never narrows it.
    """
    batches = build_fee_batches(cfee_activities, [])
    if batches.broken is not None:
        raise ValueError(batches.broken)
    ends = [batch.end for batch in batches.batches]
    if not ends:
        return None
    moments = [
        moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
        for moment in timestamps
    ]
    low = min(moments) - FILL_WINDOW_SKEW
    high = max(moments) + FILL_WINDOW_SKEW
    relevant = False
    starts: list[datetime] = []
    previous: datetime | None = None
    for index, end in enumerate(ends):
        start = previous
        if end >= low and (start is None or start < high):
            relevant = True
            if index > 0 and start is not None:
                starts.append(start)
        previous = end
    if not relevant:
        return None
    # The open first batch fails closed whenever it is relevant, so it never widens the fetch.
    return min([low, *starts])


@dataclass(frozen=True)
class GroupPlan:
    """The outcome for one (pair, CFEE batch): commissions to write, or why some stayed NULL.

    ``problem`` describes what could not be priced; ``commissions`` may still be non-empty -
    the asset and USD sides are priced independently, so an unattributable USD pool does not
    block a pair's asset fees. ``warnings`` notes residual risks the maintainer accepted
    (the USD side is matched by entry count only, so a delayed posting that swapped batches
    could still misattribute a plausible total).
    """

    pair: str
    end: datetime
    commissions: dict[str, float]
    problem: str | None = None
    warnings: tuple[str, ...] = ()


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=_QTY_REL_TOL, abs_tol=_QTY_ABS_TOL)


def _batch_index(ends: list[datetime], at: datetime) -> int | None:
    """The batch covering a fill at ``at``: the first whose ``end`` is at or after it."""
    index = bisect.bisect_left(ends, at)
    return index if index < len(ends) else None


@dataclass
class _Inputs:
    fills_by_order: dict[str, list[BrokerFill]]
    fills_by_pair: dict[str, list[BrokerFill]]
    rows_by_order: dict[str, list[dict[str, Any]]]
    fees: _FeeBatches
    # The value carries the per-pair message: ``_group_problem`` returns it
    # alongside the (empty) fill list. Every entry is the reason the pair was
    # taken out of play, so a pair is only "clean" by being absent here.
    broken_pairs: dict[str, str]
    sell_pairs: dict[int, set[str]]


def _group_problem(
    inputs: _Inputs,
    pair: str,
    batch: _FeeBatch,
    window_start: datetime | None,
    group_fills: list[BrokerFill],
) -> tuple[str | None, list[BrokerFill]]:
    if pair in inputs.broken_pairs:
        # Fail closed: a stored ``None`` would read as "no problem" further down
        # (the caller only skips a pair on a non-None problem), so an entry that
        # carries no reason still refuses the pair rather than backfilling it.
        reason = inputs.broken_pairs[pair] or (
            f"the pair {pair} is broken without a recorded reason"
        )
        return reason, []

    def in_window(fill: BrokerFill) -> bool:
        return fill.at <= batch.end and (window_start is None or fill.at > window_start)

    for order_id in {fill.order_id for fill in group_fills}:
        rows = inputs.rows_by_order.get(order_id, [])
        if not rows:
            return (
                f"the pair also traded on order {order_id}, which this account never recorded "
                "(a manual or foreign trade shares the batch's CFEE)",
                [],
            )
        order_fills = inputs.fills_by_order[order_id]
        if any(not in_window(fill) for fill in order_fills):
            return (
                f"order {order_id} has fills outside this CFEE batch's window "
                "(its fills span more than one posting batch)",
                [],
            )
        if any(fill.pair != pair for fill in order_fills):
            return f"order {order_id} is recorded at the broker under another pair", []
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
        if not local_qty > 0:
            return f"order {order_id} has no positive recorded quantity ({local_qty})", []
        broker_qty = float(sum(fill.qty for fill in order_fills))
        if not _close(local_qty, broker_qty):
            return (
                f"order {order_id} has {local_qty} recorded locally but {broker_qty} at the broker",
                [],
            )
    return None, group_fills


def _pool_owner(inputs: _Inputs, batch_index: int) -> str | None:
    """The pair a batch's USD pool belongs to, a sentinel, or ``None`` when there is no pool."""
    batch = inputs.fees.batches[batch_index]
    if batch.usd_pool <= 0:
        return None
    sellers = inputs.sell_pairs.get(batch_index, set())
    if len(sellers) == 1:
        return next(iter(sellers))
    if not sellers:
        return "orphan"
    return "ambiguous"


def _asset_entries_match(batch: _FeeBatch, pair: str, buys: list[BrokerFill]) -> bool:
    """One asset-fee entry per buy fill, each at the fill's 2 dp price (#149's observed shape)."""
    prices = batch.asset_prices.get(pair, [])
    if batch.asset_entries.get(pair, 0) != len(buys) or len(prices) != len(buys):
        return False
    buy_prices = [_price_cents(fill.price) for fill in buys]
    if any(price is None for price in prices) or any(
        price is None for price in buy_prices
    ):
        return False
    return sorted(p for p in prices if p is not None) == sorted(
        p for p in buy_prices if p is not None
    )


def _usd_entries_match(
    batch: _FeeBatch, pair: str, owner: str | None, sells: list[BrokerFill]
) -> bool:
    """One USD-fee entry per sell fill the priced USD fee is meant to cover."""
    expected = batch.pair_usd_entries.get(pair, 0)
    if owner == pair:
        expected += batch.usd_pool_entries
    return expected == len(sells)


def _allocate(
    inputs: _Inputs,
    pair: str,
    batch_index: int,
    group_fills: list[BrokerFill],
) -> GroupPlan:
    batch = inputs.fees.batches[batch_index]
    end = batch.end
    buys = [fill for fill in group_fills if fill.side == "buy"]
    sells = [fill for fill in group_fills if fill.side == "sell"]
    received_buy = sum((fill.received for fill in buys), Decimal(0))
    received_sell = sum((fill.received for fill in sells), Decimal(0))
    asset_fee = batch.asset.get(pair, Decimal(0))
    usd_fee = batch.pair_usd.get(pair, Decimal(0))
    pool_problem: str | None = None
    owner = _pool_owner(inputs, batch_index)
    if sells and batch.usd_pool > 0:
        if owner == pair:
            usd_fee += batch.usd_pool
        elif owner == "ambiguous":
            sellers = sorted(inputs.sell_pairs[batch_index])
            pool_problem = (
                f"the batch's USD CFEE pool cannot be attributed: {len(sellers)} crypto pairs "
                f"({', '.join(sellers)}) sold in its window"
            )

    problems: list[str] = []
    warnings: list[str] = []
    usd_by_order: dict[str, Decimal] = defaultdict(Decimal)
    if buys:
        if asset_fee <= 0:
            problems.append("no base-asset CFEE is posted for the batch's fills")
        elif asset_fee > received_buy * MAX_PLAUSIBLE_FEE_RATE:
            problems.append(
                f"the base-asset CFEE {asset_fee} exceeds {MAX_PLAUSIBLE_FEE_RATE:%} of the "
                f"{received_buy} received; refusing an implausible fee"
            )
        elif not _asset_entries_match(batch, pair, buys):
            problems.append(
                "the batch's base-asset CFEE entries do not match the buy fills one-for-one "
                "(one entry per fill at the fill's 2 dp price); refusing to spread a fee that "
                "may cover other fills"
            )
        else:
            for fill in buys:
                usd_by_order[fill.order_id] += (
                    asset_fee * fill.received / received_buy * fill.price
                )
    elif asset_fee > 0:
        problems.append(
            "a base-asset CFEE is posted but the pair has no buys in the batch window"
        )
    if sells:
        if pool_problem is not None:
            problems.append(pool_problem)
        elif usd_fee <= 0:
            problems.append("no USD CFEE is posted for the batch's sells")
        elif usd_fee > received_sell * MAX_PLAUSIBLE_FEE_RATE:
            problems.append(
                f"the USD CFEE {usd_fee} exceeds {MAX_PLAUSIBLE_FEE_RATE:%} of the "
                f"{received_sell} received; refusing an implausible fee"
            )
        elif not _usd_entries_match(batch, pair, owner, sells):
            problems.append(
                "the batch's USD CFEE entries do not match the sell fills one-for-one; "
                "refusing to spread a fee that may cover other fills"
            )
        else:
            for fill in sells:
                usd_by_order[fill.order_id] += usd_fee * fill.received / received_sell
            warnings.append(
                f"the USD CFEE {usd_fee} was matched to the {len(sells)} sell fill(s) by "
                "entry count only; a delayed posting that swapped batches could still "
                "misattribute it"
            )
    elif batch.pair_usd.get(pair, Decimal(0)) > 0:
        problems.append(
            "a USD CFEE naming the pair is posted but the pair has no sells in the batch window"
        )

    commissions: dict[str, float] = {}
    for order_id, order_usd in usd_by_order.items():
        rows = inputs.rows_by_order[order_id]
        order_qty = sum(float(row["filled_qty"]) for row in rows)
        if not order_qty > 0:
            # Defence in depth: `_group_problem` refuses this earlier; never divide by it.
            return GroupPlan(
                pair,
                end,
                {},
                f"order {order_id} has no positive recorded quantity ({order_qty})",
            )
        for row in rows:
            share = float(row["filled_qty"]) / order_qty
            commissions[row["execution_id"]] = float(order_usd) * share
    return GroupPlan(
        pair,
        end,
        commissions,
        "; ".join(problems) if problems else None,
        tuple(warnings),
    )


def _plan_group(inputs: _Inputs, pair: str, batch_index: int) -> GroupPlan:
    batch = inputs.fees.batches[batch_index]
    end = batch.end

    def fail(problem: str) -> GroupPlan:
        return GroupPlan(pair, end, {}, problem)

    if inputs.fees.broken is not None:
        return fail(inputs.fees.broken)
    window_start = (
        inputs.fees.batches[batch_index - 1].end if batch_index > 0 else None
    )
    group_fills = [
        fill
        for fill in inputs.fills_by_pair.get(pair, [])
        if fill.at <= end and (window_start is None or fill.at > window_start)
    ]
    if batch.open_start:
        return fail(
            "the first fetched CFEE batch's coverage window reaches before the fetched "
            "activity history; which earlier fills it covers is unknown"
        )
    problem = batch.problems.get(None) or batch.problems.get(pair)
    if problem is not None:
        return fail(problem)
    problem, _fills = _group_problem(inputs, pair, batch, window_start, group_fills)
    if problem is not None:
        return fail(problem)
    try:
        return _allocate(inputs, pair, batch_index, group_fills)
    except ArithmeticError as exc:
        # One unallocatable group must not abort the pass for every other group.
        return fail(f"allocation failed: {exc!r}")


def plan_commission_backfill(
    *,
    candidates: list[dict[str, Any]],
    fill_activities: list[dict[str, Any]],
    cfee_activities: list[dict[str, Any]],
    fee_activities: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
    now: datetime,
    first_batch_open: bool = False,
) -> tuple[list[GroupPlan], list[str], list[str]]:
    """Plan the commissions to backfill: one `GroupPlan` per (pair, CFEE batch) to resolve.

    ``candidates`` are the account's crypto executions with an unknown commission; ``local_rows``
    are every execution the account recorded on the orders the broker reports for those pairs.
    ``now`` is the pass's clock: a batch whose latest ``created_at`` is within ``CFEE_BATCH_GAP``
    of it is still posting, so it is treated as unposted - its fills stay pending for a later
    pass rather than being priced from partial totals. ``first_batch_open`` asserts that the
    first fetched ``CFEE`` batch's coverage window reaches before the fetched history (an earlier
    batch or earlier fills may exist), so it fails closed whenever it would price fills. Returns
    the plans, messages for candidates that could not be put in any group, and messages for
    candidates whose fee batch is simply not posted yet (retried on a later pass).
    """
    pairs = {pair_key(row["symbol"]) for row in candidates}
    fills_by_order: dict[str, list[BrokerFill]] = defaultdict(list)
    fills_by_pair: dict[str, list[BrokerFill]] = defaultdict(list)
    broken_pairs: dict[str, str] = {}
    for raw in fill_activities:
        symbol_text = str(raw.get("symbol") or "").strip()
        if not symbol_text:
            problem = "a FILL activity names no symbol; it could belong to any pair"
            for pair in pairs:
                broken_pairs[pair] = problem
            continue
        if split_crypto_pair(symbol_text) is None:
            # An equity or unrecognised symbol is not a crypto fill.
            continue
        try:
            fill = parse_fill_activity(raw)
        except ValueError as exc:
            problem = f"a FILL activity ({raw.get('id')!r}) is unreadable: {exc}"
            for pair in pairs:
                broken_pairs[pair] = problem
            continue
        fills_by_order[fill.order_id].append(fill)
        fills_by_pair[fill.pair].append(fill)

    rows_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in local_rows:
        rows_by_order[row["broker_order_id"]].append(row)
    fees = build_fee_batches(cfee_activities, fee_activities)
    # Settle guard: a batch still receiving entries has partial totals; leave it unposted so its
    # fills stay pending instead of being priced from an incomplete fee.
    fees.batches = [
        batch for batch in fees.batches if now - batch.end > CFEE_BATCH_GAP
    ]
    if first_batch_open and fees.batches:
        fees.batches[0].open_start = True
    ends = [batch.end for batch in fees.batches]
    sell_pairs: dict[int, set[str]] = defaultdict(set)
    for pair, fills in fills_by_pair.items():
        for fill in fills:
            if fill.side != "sell":
                continue
            index = _batch_index(ends, fill.at)
            if index is not None:
                sell_pairs[index].add(pair)
    inputs = _Inputs(
        fills_by_order=fills_by_order,
        fills_by_pair=fills_by_pair,
        rows_by_order=rows_by_order,
        fees=fees,
        broken_pairs=broken_pairs,
        sell_pairs=sell_pairs,
    )

    unplaced: list[str] = []
    pending: list[str] = []
    groups: set[tuple[str, int]] = set()
    for row in candidates:
        order_fills = fills_by_order.get(row["broker_order_id"], [])
        if not order_fills:
            unplaced.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): no FILL "
                "activity found for its order"
            )
            continue
        indices: set[int] = set()
        has_pending = False
        for fill in order_fills:
            index = _batch_index(ends, fill.at)
            if index is None:
                has_pending = True
            else:
                indices.add(index)
        if has_pending and indices:
            unplaced.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): its fills "
                "span a posted CFEE batch and fills whose fee batch is not posted yet"
            )
            continue
        if has_pending:
            pending.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): its fills "
                "are newer than the latest posted CFEE batch; retried on a later pass"
            )
            continue
        if len(indices) != 1:
            unplaced.append(
                f"execution {row['execution_id']} (order {row['broker_order_id']}): its fills "
                "map to more than one CFEE batch"
            )
            continue
        groups.add((pair_key(row["symbol"]), next(iter(indices))))

    plans: list[GroupPlan] = []
    for pair, index in sorted(groups, key=lambda item: (item[0], fees.batches[item[1]].end)):
        plans.append(_plan_group(inputs, pair, index))
    for index, batch in enumerate(fees.batches):
        if batch.usd_pool > 0 and not sell_pairs.get(index):
            unplaced.append(
                f"CFEE batch posted {batch.end.isoformat()}: a USD fee pool of {batch.usd_pool} "
                "has no crypto sells in its window to receive it"
            )
    return plans, unplaced, pending

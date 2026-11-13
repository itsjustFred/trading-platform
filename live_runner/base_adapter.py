from __future__ import annotations

import math
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from shared.logging import get_logger
from shared.models import LiveSignal

#: Marker in signal metadata (`risk_sizing_mode`) for exit-only flatten
#: orders. Shared constant (pre-mortem PM1): producer (daemon) and consumers
#: (adapters) must never compare divergent spellings again.
EXIT_ONLY_RISK_SIZING_MODE = "exit_only"

#: Explicit unbounded-take-profit policy (C1 owner decision). Set ONLY by
#: the originating processor (RocketFuel exits on the opposite signal) —
#: never inferred. Matched via :func:`is_unbounded_tp`, not string
#: comparison scattered across daemon + adapters (pre-mortem PM2).
UNBOUNDED_TP_POLICY = "unbounded_exit_on_opposite_signal"

#: Attribute set on an exception that provably never reached the broker.
_ORDER_NOT_SUBMITTED_ATTR = "_order_not_submitted"


@contextmanager
def order_not_submitted() -> Iterator[None]:
    """Mark exceptions raised inside as raised *before* any order request.

    Adapters wrap the part of ``submit_order`` that runs before the call which
    places the order. Anything raised inside is provably pre-request - local
    validation, a missing client, a volume floor, a failed symbol lookup - and
    nothing can have reached the broker, so a caller may safely retry or release
    its submission lease.

    The exception type and message are preserved; only the marker is added. That
    keeps every existing caller and assertion working, and keeps the failure
    loud in the same way it was before.

    Anything raised *outside* the block - including a failure of the submit call
    itself - is deliberately left unmarked: the broker may have accepted the
    order and only the response was lost.
    """
    try:
        yield
    except BaseException as exc:
        try:
            setattr(exc, _ORDER_NOT_SUBMITTED_ATTR, True)
        except Exception:  # pragma: no cover - exotic exception types
            # An exception that refuses attributes degrades to "outcome
            # unknown", which is the safe default for every caller.
            pass
        raise


def is_order_not_submitted(exc: BaseException) -> bool:
    """Whether an adapter proved the failure happened before any broker write."""
    return bool(getattr(exc, _ORDER_NOT_SUBMITTED_ATTR, False))


def is_unbounded_tp(metadata: object) -> bool:
    """Whether metadata declares the explicit unbounded-TP policy."""
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("take_profit_policy") or "") == UNBOUNDED_TP_POLICY


logger = get_logger("live-runner")

#: Decimal places assumed when the broker sends no `moneyDigits` at all.
DEFAULT_MONEY_DIGITS = 2

#: Widest `moneyDigits` this codebase will scale by. Beyond it the field is treated as
#: unreadable rather than trusted, because `10**digits` stops being a sane divisor.
MAX_MONEY_DIGITS = 12


def _money_digits_fallback(reason: str, raw: Any) -> int:
    logger.warning(
        "%s (value=%r); assuming %d decimal places",
        reason,
        raw if isinstance(raw, (int, float, str, bool, type(None))) else type(raw).__name__,
        DEFAULT_MONEY_DIGITS,
    )
    return DEFAULT_MONEY_DIGITS


def money_digits_from_trader(trader: Any) -> int:
    """The broker's decimal-place count for a `ProtoOATrader`, honouring a genuine zero.

    **Why this is not `int(getattr(trader, "moneyDigits", 2) or 2)`.** `moneyDigits` is a
    protobuf `uint32` *with presence*, so an unset field reads back as `0` - the same value a
    broker sends for an account with no minor units. The `or 2` collapse therefore cannot tell

    - "the broker sent no `moneyDigits`", where 2 is the right conventional default, from
    - "the broker sent a genuine 0", where dividing the balance by 100 makes it 100x too small.

    `HasField` distinguishes them, so it is asked first. The range guard below the old line
    already admitted zero, which is what made the collapse a contradiction rather than a
    judgement call: the guard accepted a value the line above it had already replaced.

    **Non-protobuf payloads.** A `SimpleNamespace` double (tests, replay adapters) has no
    `HasField`, and there the attribute being present at all is the only signal available, so
    its value is taken as sent. A *missing* attribute still means "not sent". This is the
    documented fallback, not a second opinion about the wire.

    **Out-of-range values.** Anything outside ``0..MAX_MONEY_DIGITS`` is not a decimal count
    this code can scale by, so it falls back to the default rather than raising: the balance
    path is used by monitoring and refusing to report one is worse than reporting it at the
    conventional scale. Every fallback is logged here at WARNING, so a broker that stops sending
    ``moneyDigits`` (or sends nonsense) is visible rather than silently mis-scaled.
    """
    sentinel = object()
    raw = getattr(trader, "moneyDigits", sentinel)
    if raw is sentinel or raw is None:
        return _money_digits_fallback("moneyDigits is not present", raw)

    has_field = getattr(trader, "HasField", None)
    if has_field is not None:
        try:
            if not bool(has_field("moneyDigits")):
                # The field is not on the wire at all: this is the "not sent" case, and no
                # value read from the attribute is meaningful.
                return _money_digits_fallback("moneyDigits was not sent", raw)
        except (TypeError, ValueError):
            # A payload that cannot answer HasField is treated like one without presence.
            pass

    try:
        digits = int(raw)
    except (TypeError, ValueError, OverflowError):
        return _money_digits_fallback("moneyDigits is not an integer", raw)
    if isinstance(raw, bool) or not (0 <= digits <= MAX_MONEY_DIGITS):
        return _money_digits_fallback("moneyDigits is out of range", raw)
    return digits


def money_scale(money_digits: int) -> int:
    """The divisor that converts a broker money field into account currency."""
    return 10**money_digits


def commission_cost(signed_cash_flow: float) -> float:
    """``live_executions.commission`` convention: a charge is a POSITIVE cost.

    Net PnL is ``gross - commission`` everywhere (viewer, ``net_pnl``), so every adapter must
    store what the fill cost as a positive number and a rebate as a negative one. A broker that
    reports a signed cash flow (cTrader: ``ProtoOADeal.commission`` is negative for a charge)
    is converted with this helper. ``0`` stays ``0.0`` (never ``-0.0``).
    """
    if not signed_cash_flow or not math.isfinite(signed_cash_flow):
        return 0.0
    return -float(signed_cash_flow)


@dataclass(frozen=True)
class LiveQuote:
    """A broker top-of-book quote, in price units of the symbol.

    Used by the daemon's entry-deviation gate (#125) only: it decides whether a
    signal may still be submitted, and is never a sizing input. Adapters return
    what the broker sent; the daemon validates it (finite, positive, not
    crossed) and fails closed on anything else.

    ``timestamp`` must be timezone-aware. The daemon rejects a quote whose time
    is missing, invalid, in the future or older than the deployment's
    ``max_entry_quote_age_seconds``. ``timestamp_source`` says where the time
    came from: ``"broker"`` (the quote's own time, the default) or
    ``"receive"`` (the local time the adapter received it, for brokers whose
    stream does not send a time). Adapters never substitute ``now()`` for a
    broker time that should have been there. ``source`` names the feed so a
    persisted quote can be traced back (#134).
    """

    bid: float
    ask: float
    timestamp: datetime | None = None
    source: str = ""
    timestamp_source: str = "broker"


def validate_live_quote_prices(
    quote: object, *, symbol: str, label: str = "Broker quote"
) -> tuple[float, float]:
    """Validated ``(bid, ask)`` of a broker quote, failing closed (#125, #146).

    One implementation for every layer that turns a broker quote into a number
    it is willing to gate or size on: the daemon's entry-deviation gate and
    cTrader's quote->account conversion chain both size real orders from such a
    quote, and both must refuse the same shapes - a payload that is not a
    :class:`LiveQuote`, a missing side, a non-numeric side, a non-finite or
    non-positive side, or a crossed book. Two copies of a safety-value parser
    drift: one layer gets a fix the other does not, and a quote that sizes an
    entry is accepted by one and rejected by the other.

    ``label`` names the layer so the refusal still says which one refused.
    """
    if not isinstance(quote, LiveQuote):
        raise ValueError(
            f"{label} for {symbol}: no usable quote ({type(quote).__name__}); "
            "refusing to trade on a number the broker did not send."
        )
    sides: list[float] = []
    for name, value in (("bid", quote.bid), ("ask", quote.ask)):
        if value is None or isinstance(value, bool):
            raise ValueError(f"{label} for {symbol} is unusable: missing {name} side ({value!r})")
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{label} for {symbol} is unusable: {name} is not numeric ({value!r})"
            ) from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError(
                f"{label} for {symbol} is unusable: {name} is not positive and finite ({value!r})"
            )
        sides.append(price)
    bid, ask = sides
    if bid > ask:
        raise ValueError(f"{label} for {symbol} is crossed: bid={bid!r} > ask={ask!r}")
    return bid, ask


@dataclass(frozen=True)
class LivePipValue:
    """What one qty unit of a symbol gains or loses per 1.0 price move (#146).

    ``value`` is the ``pip_value`` the risk engine sizes with: account-currency
    P&L of ONE adapter qty unit when the price moves by 1.0 (one whole price
    unit, not one pip), so ``qty = risk_amount / (stop_distance * value)`` and
    the position's notional is ``qty * price * value``. It is
    ``units_per_qty * conversion_rate``:

    * ``units_per_qty``: base-asset units one qty unit stands for (cTrader:
      0.01 lot, e.g. 1,000 EUR for EURUSD; Alpaca: one share).
    * ``conversion_rate``: account-currency value of one unit of the symbol's
      quote currency (1.0 when they are the same).

    ``conversion_quotes`` are the live quotes the rate was derived from, one
    per conversion hop. The daemon checks each one against the deployment's
    quote-age limit and refuses the entry when one is stale, so a broker that
    converts must return them. ``source`` names where the figures came from so
    a persisted signal can be traced back.
    """

    value: float
    units_per_qty: float
    conversion_rate: float
    quote_asset: str
    account_asset: str
    source: str
    conversion_symbols: tuple[str, ...] = ()
    conversion_quotes: tuple[LiveQuote, ...] = ()


@dataclass(frozen=True)
class ControlOutcome:
    """What a cancel or flatten sweep actually achieved, including what it did not.

    A bare count cannot answer the only question the caller has - "is the account flat and
    disarmed?" - because `completed > 0` is equally true of a sweep that closed one of three
    positions. Reporting that as success is how a stopped deployment ends up with open exposure
    and a dashboard that says `stopped`.

    So the outcome carries the three ways a sweep can fall short of complete:

    ``completed``     items the broker accepted (orders cancelled, positions closed)
    ``unidentified``  items the adapter could see but could not address - no id, no volume, an
                      unreadable status. Not an error to retry blindly; the payload is the
                      problem, so it is named rather than skipped.
    ``failed``        items the broker refused, or where the request raised

    ``complete`` is the contract: a caller may only treat the account as stopped/flat when the
    sweep was complete. A partial sweep must be retried or escalated, never recorded as enforced.
    An empty sweep is complete (there was nothing to do), which is why the flag is not
    ``completed > 0``.
    """

    completed: int = 0
    unidentified: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every item the adapter could see was acted on successfully."""
        return not self.unidentified and not self.failed

    def describe(self) -> str:
        """A one-line summary for logs and operator messages."""
        if self.complete:
            return f"{self.completed} completed, nothing outstanding"
        return (
            f"{self.completed} completed, {len(self.unidentified)} unidentifiable "
            f"{sorted(self.unidentified)}, {len(self.failed)} refused {sorted(self.failed)}"
        )


def coerce_positive_live_qty(value: object, *, field_name: str = "qty") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return parsed


class BaseLiveAdapter(ABC):
    """Abstract broker adapter for live order routing and fill streaming."""

    @abstractmethod
    async def connect(self) -> None:
        """Initialize broker REST/stream clients and validate connectivity."""

    @abstractmethod
    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        """Submit an order for the given signal and return broker order id.

        ``qty`` is the trusted quantity channel: callers that have already run
        risk sizing (the daemon's ``submit_order_with_risk``) pass the sized
        quantity explicitly so adapters never have to trust signal metadata.
        Live submissions must provide a finite, positive quantity.
        """

    @abstractmethod
    async def listen_executions(self) -> None:
        """Block and listen for execution updates, persisting fills to storage."""

    @abstractmethod
    async def get_balance(self) -> tuple[float, float]:
        """Return current account (balance, equity)."""

    async def get_quote(self, symbol: str) -> LiveQuote:
        """Return the current broker bid/ask for ``symbol``.

        Raises NotImplementedError when the broker cannot provide one. The daemon treats that
        (and any other failure) as "no quote", which rejects the entry: the deviation gate
        fails closed rather than submitting an order it could not check.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot provide a live quote")

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        """Return the account-currency value of one qty unit per 1.0 price move.

        The risk engine divides the risk budget by ``stop_distance * value``,
        so a wrong value scales the size by the same factor (#146: an implicit
        1.0 oversized cTrader FX entries ~1000x). It must come from the broker
        or trusted configuration, never from signal metadata. Raises
        NotImplementedError when the adapter cannot provide it; the daemon
        treats that (and any other failure) as "unknown" and refuses the entry.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot provide a pip value")

    # ── Operator safety controls ──────────────────────────────────────────────────────────────
    #
    # Status semantics built on top of these:
    #   PAUSED  -> stop opening new positions; orders and positions are left untouched, and the
    #              deployment can be resumed.
    #   STOPPED -> stop opening new positions, cancel every open order, then flatten every open
    #              position. Deliberately not implicit in PAUSED, so pausing never liquidates.
    #
    # Adapters that cannot honour a control leave it unsupported so the caller fails loudly
    # instead of reading a silent no-op as "nothing was open".

    @property
    def supports_cancel_orders(self) -> bool:
        """Whether :meth:`cancel_open_orders` is implemented for this broker."""
        return False

    @property
    def supports_flatten(self) -> bool:
        """Whether :meth:`close_all_positions` is implemented for this broker."""
        return False

    async def cancel_open_orders(self) -> ControlOutcome:
        """Cancel every working order for this account.

        Raises NotImplementedError when the broker cannot do this, so a caller cannot mistake a
        silent no-op for "nothing was open".
        """
        raise NotImplementedError(f"{type(self).__name__} cannot cancel open orders")

    async def close_all_positions(self) -> ControlOutcome:
        """Flatten every open position for this account.

        Raises NotImplementedError when the broker cannot do this.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot close positions")

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        """Cancel position-protection orders whose position no longer exists.

        A cleanliness control, not a safety one: an orphaned protection cannot fill, it only
        holds an order slot and misleads an operator reading the account. Adapters raise
        NotImplementedError when they cannot tell an orphan from a live position's stop, so the
        caller reports the account as unswept instead of claiming a clean zero.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot identify orphaned protections"
        )

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

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


def is_unbounded_tp(metadata: object) -> bool:
    """Whether metadata declares the explicit unbounded-TP policy."""
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("take_profit_policy") or "") == UNBOUNDED_TP_POLICY


#: Decimal places assumed when the broker sends no `moneyDigits` at all.
DEFAULT_MONEY_DIGITS = 2

#: Widest `moneyDigits` this codebase will scale by. Beyond it the field is treated as
#: unreadable rather than trusted, because `10**digits` stops being a sane divisor.
MAX_MONEY_DIGITS = 12


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
    conventional scale, and the caller logs the fallback.
    """
    sentinel = object()
    raw = getattr(trader, "moneyDigits", sentinel)
    if raw is sentinel or raw is None:
        return DEFAULT_MONEY_DIGITS

    has_field = getattr(trader, "HasField", None)
    if has_field is not None:
        try:
            if not bool(has_field("moneyDigits")):
                # The field is not on the wire at all: this is the "not sent" case, and no
                # value read from the attribute is meaningful.
                return DEFAULT_MONEY_DIGITS
        except (TypeError, ValueError):
            # A payload that cannot answer HasField is treated like one without presence.
            pass

    try:
        digits = int(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MONEY_DIGITS
    if isinstance(raw, bool) or not (0 <= digits <= MAX_MONEY_DIGITS):
        return DEFAULT_MONEY_DIGITS
    return digits


def money_scale(money_digits: int) -> int:
    """The divisor that converts a broker money field into account currency."""
    return 10**money_digits


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

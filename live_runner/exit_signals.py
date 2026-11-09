"""Exit-only signals: the representation and the sizing rule (#121).

An exit/close message ("Exit long AAPL @ 100", "Trim half AAPL") is not an entry.
It must never open or extend a position, so it travels as its own kind of
signal, marked by ``metadata["signal_intent"] == "exit"``, and is sized by a rule
that only ever *reduces* exposure. A plain or momentum-reversal SELL on a
deployment that cannot short is an exit too (:data:`FULL_EXIT`) and is sized by
the same rule:

* The close side comes from the position, never from the message. The signal's
  ``action`` is only a placeholder that :func:`resolve_exit_order` replaces.
* An exit closes **only the deployment's own quantity**: its net persisted fills
  for the symbol (the ledger). Another deployment's share of the same netted
  broker position stays open.
* The ledgers of every deployment on the account must add up to the broker
  position (:func:`ensure_ledgers_match_broker`), or the exit is refused: a
  close none of them saw makes every own quantity untrustworthy. The close is
  still capped at the broker position as a last line of defence.
* A partial exit closes that fraction of the own (capped) quantity. A fraction
  that rounds a whole-unit position down to nothing is refused, never rounded up.
* No position to close -> :class:`NothingToExitError`: the caller ignores the
  signal and logs it; nothing reaches the broker.

Everything here is pure and keyed on ``signal_intent`` rather than on the order
side or the deployment status, so the exit path can be dispatched on its own,
independently of the entry path (#126 lets PAUSED deployments accept exits).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from shared.logging import get_logger

logger = get_logger("live-runner")

SIGNAL_INTENT_KEY = "signal_intent"
SIGNAL_INTENT_EXIT = "exit"
EXIT_FRACTION_KEY = "exit_fraction"
EXIT_POSITION_SIDE_KEY = "exit_position_side"
EXIT_POSITION_SIDES = frozenset({"long", "short"})

# Tolerance for "this quantity is a whole number of units". Quantities are read
# back from SQLite REAL sums and broker JSON, so an exact integer test would
# misclassify 10.000000000000002.
_WHOLE_UNIT_TOLERANCE = 1e-9


#: Quantity tolerance when the adapter cannot name its instrument's precision.
#: Strict on purpose: an unknown precision must not widen what "reconciles".
DEFAULT_QTY_TOLERANCE = 1e-9


class NothingToExitError(ValueError):
    """The deployment holds nothing to close; the exit is ignored, not failed.

    ``broker_qty`` is the signed broker position for the symbol (0.0 when flat).
    A non-zero one means somebody else's position is open: worth a warning.
    """

    def __init__(self, message: str, *, broker_qty: float = 0.0) -> None:
        super().__init__(message)
        self.broker_qty = float(broker_qty)


class LedgerMismatchError(ValueError):
    """The account's ledgers do not add up to the broker position.

    A close happened that no ledger saw (a bracket's stop or target leg, a
    flatten, a manual close), so no deployment's own quantity can be trusted.
    """

    def __init__(
        self,
        message: str,
        *,
        ledgers: Mapping[str, float],
        broker_qty: float,
        tolerance: float,
    ) -> None:
        super().__init__(message)
        self.ledgers = dict(ledgers)
        self.broker_qty = float(broker_qty)
        self.tolerance = float(tolerance)

    def log_data(self) -> dict[str, object]:
        return {
            "ledger_qty_by_deployment": dict(self.ledgers),
            "ledger_qty_total": float(math.fsum(self.ledgers.values())),
            "broker_qty": self.broker_qty,
            "qty_tolerance": self.tolerance,
        }


class ExitInFlightError(ValueError):
    """An earlier exit of the same deployment and symbol may still be working."""


@dataclass(frozen=True, slots=True)
class ExitInstruction:
    """What an exit message asked for: a fraction of the own position.

    ``position_side`` is the side the message named ("Exit *long* AAPL"), or
    ``None`` when it named none. It is a cross-check only: the close side always
    comes from the position, and a mismatch refuses the exit.
    """

    fraction: float
    position_side: str | None = None

    def __post_init__(self) -> None:
        fraction = self.fraction
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError(f"Exit fraction must be a number, got {fraction!r}")
        if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
            raise ValueError(
                f"Exit fraction must be in (0, 1], got {fraction!r}; refusing to "
                "guess how much of the position to close."
            )
        if self.position_side is not None and self.position_side not in EXIT_POSITION_SIDES:
            raise ValueError(
                f"Exit position side must be 'long', 'short' or absent, got "
                f"{self.position_side!r}"
            )


#: A plain or momentum-reversal SELL on a deployment that cannot short: close all
#: of this deployment's own quantity. No side is named, so the close side comes
#: from the own position (#76: a short is bought back).
FULL_EXIT = ExitInstruction(fraction=1.0)


def is_exit_intent(metadata: Mapping[str, object] | None) -> bool:
    """True when the signal is a parsed exit/close instruction.

    Deliberately not ``exit_only``: that flag also marks a plain sell on a
    deployment that cannot short, which carries no instruction in its metadata
    and is sized as :data:`FULL_EXIT`. Use :func:`is_exit_order` to ask whether a
    signal is any kind of exit.
    """
    return str((metadata or {}).get(SIGNAL_INTENT_KEY) or "") == SIGNAL_INTENT_EXIT


def is_exit_order(action: str, metadata: Mapping[str, object] | None) -> bool:
    """True for every signal the daemon sizes as an exit.

    That is a parsed exit/close instruction, or a SELL flagged ``exit_only`` (a
    plain or momentum-reversal sell on a deployment that cannot short). The same
    test the daemon's sizing dispatches on, so "logged as an exit" and "sized as
    an exit" cannot drift apart.
    """
    if is_exit_intent(metadata):
        return True
    return str(action or "").strip().lower() == "sell" and bool(
        (metadata or {}).get("exit_only")
    )


def exit_instruction_from_metadata(metadata: Mapping[str, object]) -> ExitInstruction:
    """Read the instruction back from signal metadata, failing closed."""
    if not is_exit_intent(metadata):
        raise ValueError("Signal is not an exit instruction")
    if EXIT_FRACTION_KEY not in metadata:
        raise ValueError(
            "Exit signal carries no exit_fraction; refusing to guess how much to close."
        )
    raw_fraction = metadata[EXIT_FRACTION_KEY]
    if isinstance(raw_fraction, bool) or not isinstance(raw_fraction, (int, float)):
        raise ValueError(f"Exit fraction must be a number, got {raw_fraction!r}")
    raw_side = metadata.get(EXIT_POSITION_SIDE_KEY)
    side = None if raw_side is None else str(raw_side)
    return ExitInstruction(fraction=float(raw_fraction), position_side=side)


def exit_intent_metadata(instruction: ExitInstruction) -> dict[str, object]:
    """The metadata a producer stamps on an exit signal."""
    metadata: dict[str, object] = {
        SIGNAL_INTENT_KEY: SIGNAL_INTENT_EXIT,
        EXIT_FRACTION_KEY: float(instruction.fraction),
        # Same flags the daemon uses for every reduce-only order: the adapters
        # key on risk_sizing_mode to build a plain close without SL/TP legs.
        "exit_only": True,
        "risk_sizing_mode": "exit_only",
    }
    if instruction.position_side is not None:
        metadata[EXIT_POSITION_SIDE_KEY] = instruction.position_side
    return metadata


def _broker_quantity(positions: Iterable[Mapping[str, object]], symbol: str) -> float | None:
    """The signed broker position for ``symbol``, or ``None`` when flat."""
    wanted = symbol.strip().upper()
    quantities: list[float] = []
    for position in positions:
        if str(position.get("symbol") or "").strip().upper() != wanted:
            continue
        raw = position.get("qty")
        try:
            quantity = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Exit signal for {wanted} received an invalid broker position "
                f"quantity {raw!r}."
            ) from exc
        if not math.isfinite(quantity):
            raise ValueError(
                f"Exit signal for {wanted} received a non-finite broker position."
            )
        if quantity == 0:
            continue
        quantities.append(quantity)
    if not quantities:
        return None
    if len(quantities) > 1:
        raise ValueError(
            f"Exit signal for {wanted} has {len(quantities)} broker positions; "
            "refusing an ambiguous close."
        )
    return quantities[0]


def _apply_fraction(own_qty: float, fraction: float) -> float:
    if fraction >= 1.0:
        return own_qty
    raw = own_qty * fraction
    if abs(own_qty - round(own_qty)) <= _WHOLE_UNIT_TOLERANCE:
        # A whole-unit position stays whole: round DOWN, so a partial can never
        # close more than it was asked to.
        return float(math.floor(raw + _WHOLE_UNIT_TOLERANCE))
    return raw


def resolve_exit_order(
    *,
    symbol: str,
    ledger_qty: float,
    broker_positions: Iterable[Mapping[str, object]],
    instruction: ExitInstruction,
) -> tuple[str, float]:
    """Return ``(close_action, qty)`` for an exit instruction.

    ``ledger_qty`` is the deployment's own signed net filled quantity for the
    symbol. Raises :class:`NothingToExitError` when there is nothing to close and
    ``ValueError`` when closing would be ambiguous or would extend exposure.
    """
    if not math.isfinite(ledger_qty):
        raise ValueError(f"Exit signal for {symbol}: own ledger quantity is not finite.")
    broker_qty = _broker_quantity(broker_positions, symbol)
    if broker_qty is None:
        raise NothingToExitError(
            f"Exit signal for {symbol} ignored: no broker position is open."
        )
    if abs(ledger_qty) <= _WHOLE_UNIT_TOLERANCE:
        raise NothingToExitError(
            f"Exit signal for {symbol} ignored: this deployment holds no own "
            f"quantity (broker position {broker_qty} belongs to someone else).",
            broker_qty=broker_qty,
        )
    if (ledger_qty > 0) != (broker_qty > 0):
        raise ValueError(
            f"Refusing exit for {symbol}: this deployment's own position "
            f"({ledger_qty}) and the broker position ({broker_qty}) are on "
            "opposite sides, so a close would extend the broker exposure."
        )
    own_side = "long" if ledger_qty > 0 else "short"
    if instruction.position_side is not None and instruction.position_side != own_side:
        raise ValueError(
            f"Refusing exit for {symbol}: the message asked to exit a "
            f"{instruction.position_side} position, but this deployment holds a "
            f"{own_side} one."
        )
    own_qty = abs(ledger_qty)
    if own_qty > abs(broker_qty):
        logger.warning(
            "Own ledger quantity exceeds the broker position; closing at most the "
            "broker quantity",
            extra={
                "data": {
                    "symbol": symbol,
                    "ledger_qty": ledger_qty,
                    "broker_qty": broker_qty,
                }
            },
        )
        own_qty = abs(broker_qty)
    qty = _apply_fraction(own_qty, instruction.fraction)
    if qty <= 0:
        raise ValueError(
            f"Refusing partial exit for {symbol}: {instruction.fraction:.4g} of "
            f"{own_qty} rounds down to nothing."
        )
    close_action = "sell" if ledger_qty > 0 else "buy"
    return close_action, float(qty)


def broker_position_qty(positions: Iterable[Mapping[str, object]], symbol: str) -> float:
    """The signed broker position for ``symbol``; 0.0 when flat.

    Same reading and the same refusals (unparseable, several rows) as the exit
    sizing itself.
    """
    quantity = _broker_quantity(positions, symbol)
    return 0.0 if quantity is None else quantity


def usable_qty_tolerance(raw: object) -> float:
    """``raw`` when it is a finite, non-negative number, else the strict default."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_QTY_TOLERANCE
    value = float(raw)
    if not math.isfinite(value) or value < 0.0:
        return DEFAULT_QTY_TOLERANCE
    return value


def ensure_ledgers_match_broker(
    *,
    symbol: str,
    ledgers: Mapping[str, float],
    broker_qty: float,
    tolerance: float,
) -> None:
    """Refuse when the account's ledgers do not explain the broker position.

    ``ledgers`` maps every deployment on the account to its signed net filled
    quantity for ``symbol``. Their sum must equal the signed broker position
    within ``tolerance`` (the instrument's quantity precision). Only then is a
    deployment's own quantity evidence of what it holds; otherwise a close the
    ledgers never saw has happened, and an exit sized off them could close
    another deployment's shares. Raises :class:`LedgerMismatchError`.
    """
    total = math.fsum(float(value) for value in ledgers.values())
    if (
        not math.isfinite(tolerance)
        or tolerance < 0.0
        or not math.isfinite(total)
        or not math.isfinite(broker_qty)
        or abs(total - broker_qty) > tolerance
    ):
        listing = ", ".join(
            f"{deployment_id}={float(value):g}" for deployment_id, value in sorted(ledgers.items())
        ) or "none"
        raise LedgerMismatchError(
            f"Refusing exit for {symbol}: the ledgers of the deployments on this "
            f"account add up to {total:g} ({listing}), but the broker position is "
            f"{broker_qty:g} (tolerance {tolerance:g}). A close the ledgers never saw "
            "(a stop or target leg, a flatten, a manual close) would make this exit "
            "close another deployment's shares; reconcile before exiting.",
            ledgers=ledgers,
            broker_qty=broker_qty,
            tolerance=tolerance,
        )

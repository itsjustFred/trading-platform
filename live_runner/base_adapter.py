from __future__ import annotations

import math
from abc import ABC, abstractmethod

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


def is_exit_only_metadata(metadata: object) -> bool:
    """Whether metadata is system-stamped as an exit-only (flatten) order.

    This is the SINGLE definition of the exit-only contract, deliberately
    resting on the producer's stamp alone and never on ``signal.action``.

    The close side is derived from the broker book by the daemon
    (``daemon._exit_only_close_action``): closing a SHORT is a BUY. So an
    action test inverts the meaning on exactly the orders that matter -- every
    stop-loss and take-profit on a short would be refused while long exits
    passed. The action is a consequence of the position side, not an input.

    The dangerous case -- exit-only metadata with no broker position, which
    would OPEN one -- is not this function's job. It is refused where the
    position is resolved (daemon ``_metadata_for_exit_only_order``; cTrader
    ``_position_id_for_exit``; Alpaca ``_require_exit_position``), so that the
    refusal is broker-derived and observable.
    """
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE


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

    # ── Operator safety controls ────────────────────────────────────────────
    #
    # Status semantics implemented on top of these:
    #   PAUSED  -> stop opening new positions; open orders and positions are
    #               left untouched, and the deployment can be resumed.
    #   STOPPED -> stop opening new positions, cancel every open order, then
    #               flatten every open position. Deliberately NOT implicit in
    #               PAUSED, so pausing can never liquidate by surprise.
    #
    # Adapters that cannot honour a control must leave it unsupported so the
    # daemon fails loudly and reports the deployment as incompletely stopped,
    # rather than silently pretending the account is flat.

    @property
    def supports_cancel_orders(self) -> bool:
        """Whether :meth:`cancel_open_orders` is implemented for this broker."""
        return False

    @property
    def supports_flatten(self) -> bool:
        """Whether :meth:`close_all_positions` is implemented for this broker."""
        return False

    async def cancel_open_orders(self) -> int:
        """Cancel every open order for this account; return how many were cancelled.

        Raises NotImplementedError when the broker cannot do this, so callers
        must not treat a silent no-op as "nothing was open".
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot cancel open orders"
        )

    async def close_all_positions(self) -> int:
        """Flatten every open position for this account; return how many closed.

        Raises NotImplementedError when the broker cannot do this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot close positions"
        )

    async def cancel_orphaned_protections(self) -> int:
        """Cancel position-protection orders whose position no longer exists.

        A cleanliness control, not a safety one: an orphaned protection cannot
        fill, it only holds an order slot and misleads an operator reading the
        account. Adapters raise NotImplementedError when they cannot tell an
        orphan from a live position's stop, so the caller reports the account
        as unswept instead of claiming a clean zero.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot identify orphaned protections"
        )

    @abstractmethod
    async def get_balance(self) -> tuple[float, float]:
        """Return current account (balance, equity)."""

    async def get_market_data(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int = 200,
    ):
        """Return recent OHLCV rows for signal processors.

        Adapters that cannot provide broker market data must leave this
        unsupported so callers fail closed instead of fabricating indicators.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide OHLCV market data"
        )

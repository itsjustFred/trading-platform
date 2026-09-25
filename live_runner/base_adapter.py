from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import LiveSignal


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
        When ``qty`` is None (direct operator scripts), the adapter falls back
        to sizing the signal with the shared default risk configuration.
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

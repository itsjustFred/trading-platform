from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import LiveSignal

#: Marker in signal metadata (`risk_sizing_mode`) for exit-only flatten
#: orders. Shared constant (pre-mortem PM1): producer (daemon) and consumers
#: (adapters) must never compare divergent spellings again.
EXIT_ONLY_RISK_SIZING_MODE = "exit_only"


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

    @abstractmethod
    async def get_balance(self) -> tuple[float, float]:
        """Return current account (balance, equity)."""

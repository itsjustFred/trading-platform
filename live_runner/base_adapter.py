from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import LiveSignal


class BaseLiveAdapter(ABC):
    """Abstract broker adapter for live order routing and fill streaming."""

    @abstractmethod
    async def connect(self) -> None:
        """Initialize broker REST/stream clients and validate connectivity."""

    @abstractmethod
    async def submit_order(self, signal: LiveSignal) -> str:
        """Submit an order for the given signal and return broker order id."""

    @abstractmethod
    async def listen_executions(self) -> None:
        """Block and listen for execution updates, persisting fills to storage."""

    @abstractmethod
    async def get_balance(self) -> tuple[float, float]:
        """Return current account (balance, equity)."""

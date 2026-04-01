from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from risk.models import TradeSignal


class BaseSignalSource(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Initialize source connections/resources."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close source connections/resources."""

    @abstractmethod
    async def listen_signals(self) -> AsyncIterator[TradeSignal]:
        """Yield parsed trade signals from this source."""

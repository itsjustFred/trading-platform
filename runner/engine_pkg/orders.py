"""Order and position dataclasses plus the native order-type enum."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from shared.models import TradeExit


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


@dataclass
class Order:
    symbol: str
    direction: str
    order_type: OrderType
    created_index: int
    created_time: str
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    stop_distance: float | None = None
    take_profit: float | None = None
    take_profit_levels: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Position:
    symbol: str
    direction: str
    entry_index: int
    entry_time: str
    entry_price: float
    order_type: str
    quantity: float
    remaining_qty: float
    stop_loss: float | None = None
    take_profit_levels: list[dict[str, Any]] = field(default_factory=list)
    exits: list[TradeExit] = field(default_factory=list)
    mae: float = 0.0
    mfe: float = 0.0

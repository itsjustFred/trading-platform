from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class TradeSignal:
    action: Literal["buy", "sell", "long", "short"]
    symbol: str
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    timeframe: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = str(self.action).strip().lower()
        if normalized not in {"buy", "sell", "long", "short"}:
            raise ValueError(f"Unsupported action '{self.action}'")
        self.action = normalized  # type: ignore[assignment]
        self.symbol = str(self.symbol).strip().upper()
        if not self.symbol:
            raise ValueError("symbol is required")
        self.entry_price = float(self.entry_price)
        if self.entry_price <= 0:
            raise ValueError("entry_price must be > 0")
        self.stop_loss = float(self.stop_loss) if self.stop_loss is not None else None
        self.take_profit = float(self.take_profit) if self.take_profit is not None else None
        self.timeframe = str(self.timeframe).strip()
        if not self.timeframe:
            raise ValueError("timeframe is required")
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class SizedOrder:
    signal: TradeSignal
    position_size: float
    risk_amount: float
    risk_percent: float
    risk_reward_ratio: float | None

    def __post_init__(self) -> None:
        self.position_size = float(self.position_size)
        self.risk_amount = float(self.risk_amount)
        self.risk_percent = float(self.risk_percent)
        self.risk_reward_ratio = (
            float(self.risk_reward_ratio) if self.risk_reward_ratio is not None else None
        )

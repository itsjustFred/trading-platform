from __future__ import annotations

import math
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
        if self.symbol is None or str(self.symbol).strip() == "":
            raise ValueError("symbol is required")
        self.symbol = str(self.symbol).strip().upper()
        self.entry_price = float(self.entry_price)
        if not math.isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("entry_price must be a finite number > 0")
        self.stop_loss = float(self.stop_loss) if self.stop_loss is not None else None
        if self.stop_loss is not None and not math.isfinite(self.stop_loss):
            raise ValueError("stop_loss must be finite")
        self.take_profit = float(self.take_profit) if self.take_profit is not None else None
        if self.take_profit is not None and not math.isfinite(self.take_profit):
            raise ValueError("take_profit must be finite")
        if self.timeframe is None or str(self.timeframe).strip() == "":
            raise ValueError("timeframe is required")
        self.timeframe = str(self.timeframe).strip()
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
        for _name, _value in (
            ("position_size", self.position_size),
            ("risk_amount", self.risk_amount),
            ("risk_percent", self.risk_percent),
        ):
            if not math.isfinite(_value):
                raise ValueError(f"{_name} must be finite")
        if self.risk_reward_ratio is not None and not math.isfinite(self.risk_reward_ratio):
            raise ValueError("risk_reward_ratio must be finite")
        if self.risk_amount < 0:
            raise ValueError("risk_amount must be >= 0")
        if self.risk_percent < 0:
            raise ValueError("risk_percent must be >= 0")

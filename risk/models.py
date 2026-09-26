from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal


def _finite_float(value: Any, field_name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite")
    return numeric


def _optional_finite_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, field_name)


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{field_name} must be a boolean")


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
class RiskDecision:
    signal_id: str
    accepted: bool
    quantity: float
    risk_amount: float
    risk_percent: float
    risk_distance: float | None
    stop_price_used: float | None
    take_profit_used: float | None
    rejection_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or not self.signal_id.strip():
            raise ValueError("signal_id must be a non-empty string")
        self.signal_id = self.signal_id.strip()
        self.accepted = _parse_bool(self.accepted, "accepted")
        self.quantity = _finite_float(self.quantity, "quantity")
        self.risk_amount = _finite_float(self.risk_amount, "risk_amount")
        self.risk_percent = _finite_float(self.risk_percent, "risk_percent")
        self.risk_distance = _optional_finite_float(self.risk_distance, "risk_distance")
        self.stop_price_used = _optional_finite_float(
            self.stop_price_used,
            "stop_price_used",
        )
        self.take_profit_used = _optional_finite_float(
            self.take_profit_used,
            "take_profit_used",
        )
        self.rejection_reason = (
            str(self.rejection_reason) if self.rejection_reason is not None else None
        )
        self.metadata = dict(self.metadata or {})


@dataclass(slots=True)
class SizedOrder:
    signal: TradeSignal
    position_size: float
    risk_amount: float
    risk_percent: float
    risk_reward_ratio: float | None
    decision: RiskDecision | None = None

    def __post_init__(self) -> None:
        self.position_size = float(self.position_size)
        self.risk_amount = float(self.risk_amount)
        self.risk_percent = float(self.risk_percent)
        self.risk_reward_ratio = (
            float(self.risk_reward_ratio) if self.risk_reward_ratio is not None else None
        )


def realized_risk_amount(order: SizedOrder) -> float:
    """Realized stop-loss amount for a sized order (#84).

    ``risk_amount``/``risk_percent`` intentionally keep the policy values
    (validation-reference parity contract); on notional-cap the position is
    smaller than the policy asked for, and the truth lives in the decision
    metadata as ``realized_risk_amount``. Consumers (stats, reports,
    validation parity, live metadata) must read this, never the policy
    figure, or they overstate risk on every capped fill.
    """
    decision = getattr(order, "decision", None)
    metadata = getattr(decision, "metadata", None) or {}
    realized = metadata.get("realized_risk_amount")
    if realized is not None:
        try:
            value = float(realized)
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(value) and value >= 0:
                return value
    return float(order.risk_amount)


def realized_risk_percent(order: SizedOrder, account_balance: float) -> float:
    """Realized risk as a fraction of equity (#84).

    The engine reports no realized percent (only the policy figure plus the
    realized amount), so it is derived here: realized amount over the sizing
    balance. Falls back to the policy percent on zero/unknown balance.
    """
    try:
        balance = float(account_balance)
    except (TypeError, ValueError):
        balance = 0.0
    if balance > 0:
        return float(realized_risk_amount(order)) / balance
    return float(order.risk_percent)

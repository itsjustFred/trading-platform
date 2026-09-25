from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4


def _finite_float(value: Any, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite")
    return numeric


def _finite_positive(value: Any, field_name: str) -> float:
    numeric = _finite_float(value, field_name)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return numeric


def _finite_nonnegative(value: Any, field_name: str) -> float:
    numeric = _finite_float(value, field_name)
    if numeric < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return numeric


def _finite_fraction(value: Any, field_name: str) -> float:
    numeric = _finite_float(value, field_name)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return numeric


def _optional_finite_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, field_name)


def _optional_finite_positive(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _finite_positive(value, field_name)


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
    identity: str = field(default_factory=lambda: uuid4().hex, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized = str(self.action).strip().lower()
        if normalized not in {"buy", "sell", "long", "short"}:
            raise ValueError(f"Unsupported action '{self.action}'")
        self.action = normalized  # type: ignore[assignment]
        self.symbol = str(self.symbol).strip().upper()
        if not self.symbol:
            raise ValueError("symbol is required")
        self.entry_price = _finite_positive(self.entry_price, "entry_price")
        self.stop_loss = (
            _optional_finite_positive(self.stop_loss, "stop_loss")
        )
        self.take_profit = (
            _optional_finite_positive(self.take_profit, "take_profit")
        )
        self.timeframe = str(self.timeframe).strip()
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if self.identity is None:
            raise ValueError("identity must be non-empty")
        self.identity = str(self.identity).strip()
        if not self.identity:
            raise ValueError("identity must be non-empty")
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
        self.quantity = _finite_nonnegative(self.quantity, "quantity")
        self.risk_amount = _finite_nonnegative(self.risk_amount, "risk_amount")
        self.risk_percent = _finite_fraction(self.risk_percent, "risk_percent")
        self.risk_distance = (
            None
            if self.risk_distance is None
            else _optional_finite_positive(self.risk_distance, "risk_distance")
        )
        self.stop_price_used = _optional_finite_positive(
            self.stop_price_used,
            "stop_price_used",
        )
        self.take_profit_used = _optional_finite_positive(
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
        self.position_size = _finite_nonnegative(self.position_size, "position_size")
        self.risk_amount = _finite_nonnegative(self.risk_amount, "risk_amount")
        self.risk_percent = _finite_fraction(self.risk_percent, "risk_percent")
        self.risk_reward_ratio = (
            None
            if self.risk_reward_ratio is None
            else _finite_nonnegative(self.risk_reward_ratio, "risk_reward_ratio")
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

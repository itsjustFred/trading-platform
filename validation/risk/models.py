"""Pydantic models for risk-sizing validation results."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.models import Trade


class SizingModel(str, Enum):
    """Enumeration of supported position-sizing models."""

    FIXED_RISK = "fixed_risk"
    PERCENT_RISK = "percent_risk"


class RiskModelConfig(BaseModel):
    """High-level configuration for a risk-sizing model."""

    model_config = ConfigDict(frozen=False)

    model: SizingModel = SizingModel.FIXED_RISK
    risk_amount: float | None = None
    risk_percent: float | None = None


def enforce_minimum_position_size(position_size: float, min_position_size: float) -> float:
    """Validate a computed position size against the configured minimum (DV-B48).

    A size of ``0.0`` is always rejected: it means the sizing produced no
    position at all (typically lot rounding floored a sub-lot size), which
    must surface as a failure instead of a trivially "matching" empty
    profile.

    Raises
    ------
    ValueError
        When *position_size* is below *min_position_size* or zero.  Failing
        closed replaces the previous silent flooring to 0, which produced a
        zero-size profile that trivially "matched" in the risk comparison
        instead of surfacing the undersized position.
    """
    if position_size <= 0.0:
        raise ValueError(
            f"computed position size {position_size} is not positive "
            "(lot rounding must not silently produce an empty position)"
        )
    if position_size < min_position_size:
        raise ValueError(
            f"computed position size {position_size} is below the "
            f"minimum position size {min_position_size}"
        )
    return position_size


class RiskSizingInput(BaseModel):
    """Input parameters for a single position-sizing calculation."""

    model_config = ConfigDict(frozen=False)

    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    direction: str = "LONG"
    trade: Trade | None = None
    account_balance: float | None = None


class TradeRiskProfile(BaseModel):
    """Risk characteristics of a single trade after sizing."""

    model_config = ConfigDict(frozen=False)

    trade_id: int
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    direction: str
    position_size: float
    risk_amount: float
    risk_percent: float
    stop_distance: float
    risk_reward_ratio: float | None = None


class RiskSizingResult(BaseModel):
    """Aggregated risk sizing result across one or more trades."""

    model_config = ConfigDict(frozen=False)

    profiles: list[TradeRiskProfile] = Field(default_factory=list)
    total_risk_amount: float = 0.0
    total_risk_percent: float = 0.0

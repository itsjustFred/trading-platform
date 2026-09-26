"""Pydantic models for risk-sizing validation results."""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.models import Trade


def _validate_optional_positive(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and > 0")


def _validate_optional_fraction(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{field_name} must be finite and between 0 and 1")


def _validate_optional_nonnegative(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and >= 0")


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

    @model_validator(mode="after")
    def _validate_risk_percent(self) -> "RiskModelConfig":
        _validate_optional_nonnegative(self.risk_amount, "risk_amount")
        _validate_optional_fraction(self.risk_percent, "risk_percent")
        return self


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

    @model_validator(mode="after")
    def _validate_values(self) -> "TradeRiskProfile":
        _validate_optional_positive(self.entry_price, "entry_price")
        _validate_optional_positive(self.stop_loss, "stop_loss")
        _validate_optional_positive(self.take_profit, "take_profit")
        _validate_optional_nonnegative(self.position_size, "position_size")
        _validate_optional_nonnegative(self.risk_amount, "risk_amount")
        _validate_optional_fraction(self.risk_percent, "risk_percent")
        _validate_optional_nonnegative(self.stop_distance, "stop_distance")
        _validate_optional_nonnegative(self.risk_reward_ratio, "risk_reward_ratio")
        return self


class RiskSizingResult(BaseModel):
    """Aggregated risk sizing result across one or more trades."""

    model_config = ConfigDict(frozen=False)

    profiles: list[TradeRiskProfile] = Field(default_factory=list)
    total_risk_amount: float = 0.0
    total_risk_percent: float = 0.0

    @model_validator(mode="after")
    def _validate_values(self) -> "RiskSizingResult":
        _validate_optional_nonnegative(self.total_risk_amount, "total_risk_amount")
        _validate_optional_nonnegative(self.total_risk_percent, "total_risk_percent")
        return self

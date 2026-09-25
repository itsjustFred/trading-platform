"""Dedicated percent-risk sizing model for strategy validation."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, model_validator

from validation.risk.models import (
    RiskSizingInput,
    RiskSizingResult,
    TradeRiskProfile,
    enforce_minimum_position_size,
)


class PercentRiskConfig(BaseModel):
    """Configuration for percent-risk position sizing."""

    model_config = ConfigDict(frozen=False)

    risk_percent: float
    min_position_size: float = 0.0
    max_position_size: float | None = None
    round_lots: bool = True
    lot_size: float = 1.0

    @model_validator(mode="after")
    def _validate_finite_config(self) -> "PercentRiskConfig":
        values = {
            "risk_percent": self.risk_percent,
            "min_position_size": self.min_position_size,
            "lot_size": self.lot_size,
            "max_position_size": self.max_position_size,
        }
        for name, value in values.items():
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        return self



class PercentRiskModel:
    """Standalone percent-risk sizing calculator.

    Computes position size from a percentage of the account balance
    and the distance between entry price and stop-loss.
    """

    def __init__(self, config: PercentRiskConfig):
        self.config = config

    def validate(self, input: RiskSizingInput) -> None:
        """Validate that required fields are present and valid.

        If ``stop_loss`` is missing on ``input`` but a ``trade`` with
        ``meta`` is provided, the stop is extracted from
        ``trade.meta['stop_loss']`` (mirroring the adapter behaviour).

        Raises:
            ValueError: If ``entry_price``, ``stop_loss``, or
                ``account_balance`` are missing or otherwise invalid.
        """
        if not math.isfinite(self.config.risk_percent):
            raise ValueError("risk_percent must be finite")
        if self.config.risk_percent <= 0:
            raise ValueError("risk_percent must be > 0")
        if self.config.risk_percent > 1:
            raise ValueError("risk_percent must be between 0 and 1")
        if not math.isfinite(self.config.min_position_size) or self.config.min_position_size < 0:
            raise ValueError("min_position_size must be finite and >= 0")
        if not math.isfinite(self.config.lot_size) or self.config.lot_size < 0:
            raise ValueError("lot_size must be finite and >= 0")
        if self.config.max_position_size is not None and (
            not math.isfinite(self.config.max_position_size)
            or self.config.max_position_size <= 0
        ):
            raise ValueError("max_position_size must be finite and > 0")
        if not math.isfinite(input.entry_price) or input.entry_price <= 0:
            raise ValueError("entry_price is required and must be finite and > 0")

        stop_loss = input.stop_loss
        if stop_loss is None and input.trade is not None:
            meta = input.trade.meta or {}
            stop_loss = meta.get("stop_loss")

        if stop_loss is None:
            raise ValueError("stop_loss is required")

        stop_loss = float(stop_loss)
        if not math.isfinite(stop_loss) or stop_loss <= 0:
            raise ValueError("stop_loss must be finite and > 0")

        if input.take_profit is not None and (
            not math.isfinite(input.take_profit) or input.take_profit <= 0
        ):
            raise ValueError("take_profit must be finite and > 0")

        input.stop_loss = stop_loss

        if input.account_balance is None:
            raise ValueError("account_balance is required")

        if not math.isfinite(input.account_balance) or input.account_balance <= 0:
            raise ValueError("account_balance must be > 0")

        if self.config.risk_percent <= 0:
            raise ValueError("risk_percent must be > 0")

    def size(self, input: RiskSizingInput) -> RiskSizingResult:
        """Calculate a single percent-risk sizing profile.

        Returns:
            RiskSizingResult: Aggregated result containing exactly one
            :class:`TradeRiskProfile`.
        """
        self.validate(input)

        entry_price = float(input.entry_price)
        stop_loss = float(input.stop_loss)
        take_profit = (
            float(input.take_profit) if input.take_profit is not None else None
        )
        direction = input.direction or "LONG"
        account_balance = float(input.account_balance)

        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == 0:
            raise ValueError(
                "stop_distance must be > 0 (entry_price != stop_loss)"
            )

        risk_amount = account_balance * self.config.risk_percent
        position_size = risk_amount / stop_distance

        if self.config.round_lots and self.config.lot_size > 0:
            position_size = (
                math.floor(position_size / self.config.lot_size) * self.config.lot_size
            )

        enforce_minimum_position_size(position_size, self.config.min_position_size)

        max_size = self.config.max_position_size
        if max_size is not None and position_size > max_size:
            position_size = max_size

        risk_per_trade = position_size * stop_distance

        risk_reward_ratio = None
        if take_profit is not None and stop_distance > 0:
            reward_distance = abs(take_profit - entry_price)
            if reward_distance > 0:
                risk_reward_ratio = reward_distance / stop_distance

        trade_id = 0
        if input.trade is not None:
            trade_id = input.trade.id

        risk_percent_of_balance = 0.0
        if account_balance > 0:
            risk_percent_of_balance = risk_per_trade / account_balance

        profile = TradeRiskProfile(
            trade_id=trade_id,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=direction,
            position_size=position_size,
            risk_amount=risk_per_trade,
            risk_percent=risk_percent_of_balance,
            stop_distance=stop_distance,
            risk_reward_ratio=risk_reward_ratio,
        )

        return RiskSizingResult(
            profiles=[profile],
            total_risk_amount=risk_per_trade,
            total_risk_percent=risk_percent_of_balance,
        )

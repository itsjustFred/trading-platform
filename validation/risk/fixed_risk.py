"""Dedicated fixed-risk sizing model for strategy validation."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from validation.risk.models import RiskSizingInput, RiskSizingResult, TradeRiskProfile


class FixedRiskConfig(BaseModel):
    """Configuration for fixed-risk position sizing."""

    model_config = ConfigDict(frozen=False)

    risk_amount: float
    min_position_size: float = 0.0
    max_position_size: float | None = None
    round_lots: bool = True
    lot_size: float = 1.0


class FixedRiskModel:
    """Standalone fixed-risk sizing calculator.

    Computes position size from a fixed monetary risk amount and the
    distance between entry price and stop-loss.
    """

    def __init__(self, config: FixedRiskConfig):
        self.config = config

    def validate(self, input: RiskSizingInput) -> None:
        """Validate that required fields are present.

        If ``stop_loss`` is missing on ``input`` but a ``trade`` with
        ``meta`` is provided, the stop is extracted from
        ``trade.meta['stop_loss']`` (mirroring the adapter behaviour).

        Raises:
            ValueError: If ``entry_price`` or ``stop_loss`` are missing
                or otherwise invalid.
        """
        if not math.isfinite(input.entry_price):
            raise ValueError("entry_price is required and must be finite")

        stop_loss = input.stop_loss
        if stop_loss is None and input.trade is not None:
            meta = input.trade.meta or {}
            stop_loss = meta.get("stop_loss")

        if stop_loss is None:
            raise ValueError("stop_loss is required")

        stop_loss = float(stop_loss)
        if not math.isfinite(stop_loss):
            raise ValueError("stop_loss must be finite")

        # Write the resolved stop-loss back so size() can read it directly.
        input.stop_loss = stop_loss

    def size(self, input: RiskSizingInput) -> RiskSizingResult:
        """Calculate a single fixed-risk sizing profile.

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

        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == 0:
            raise ValueError(
                "stop_distance must be > 0 (entry_price != stop_loss)"
            )

        position_size = self.config.risk_amount / stop_distance

        if self.config.round_lots and self.config.lot_size > 0:
            position_size = (
                math.floor(position_size / self.config.lot_size) * self.config.lot_size
            )

        if position_size < self.config.min_position_size:
            # DV-B48: fail closed.  Silently flooring to 0 produced a
            # zero-size profile that trivially "matched" in the risk
            # comparison instead of surfacing the undersized position.
            raise ValueError(
                f"computed position size {position_size} is below the "
                f"minimum position size {self.config.min_position_size}"
            )

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

        risk_percent = 0.0
        if input.account_balance is not None and input.account_balance > 0:
            risk_percent = risk_per_trade / input.account_balance

        profile = TradeRiskProfile(
            trade_id=trade_id,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=direction,
            position_size=position_size,
            risk_amount=risk_per_trade,
            risk_percent=risk_percent,
            stop_distance=stop_distance,
            risk_reward_ratio=risk_reward_ratio,
        )

        return RiskSizingResult(
            profiles=[profile],
            total_risk_amount=risk_per_trade,
            total_risk_percent=risk_percent,
        )

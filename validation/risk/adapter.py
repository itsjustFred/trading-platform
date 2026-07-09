"""RiskAdapter for strategy validation.

Wraps the existing RiskEngine and exposes validation-oriented helpers to
convert ``Trade`` objects into ``TradeSignal`` objects and compute
risk-sizing profiles without modifying ``risk/engine.py``.

Behaviour with ``None`` stop-loss:
- The adapter delegates to ``RiskEngine``, which synthesises a fallback
  stop distance (ATR-based if ``atr`` is present in metadata, otherwise
  ``fallback_stop_pct`` of the entry price).
- The resolved stop is written back to the signal so that the caller can
  inspect it in the resulting ``TradeRiskProfile``.
"""

from __future__ import annotations

import math
from typing import Any

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import SizedOrder, TradeSignal
from shared.models import Trade
from validation.reference import ValidationReference
from validation.risk.fixed_risk import FixedRiskConfig, FixedRiskModel
from validation.risk.models import RiskSizingInput, RiskSizingResult, SizingModel, TradeRiskProfile
from validation.risk.percent_risk import PercentRiskConfig, PercentRiskModel


def _trade_to_signal(
    trade: Trade,
    symbol: str,
    timeframe: str,
    account_balance: float | None = None,
) -> TradeSignal:
    """Build a ``TradeSignal`` from a canonical ``Trade``.

    ``stop_loss`` and ``take_profit`` are read from direct attributes if they
    exist; otherwise they are read from ``trade.meta``.
    """
    action = "buy" if trade.direction == "LONG" else "sell"

    # Prefer direct attribute, fall back to meta dict for backward
    # compatibility with importers that store SL/TP inside meta.
    stop_loss = getattr(trade, "stop_loss", None)
    if stop_loss is None and trade.meta:
        stop_loss = trade.meta.get("stop_loss")

    take_profit = getattr(trade, "take_profit", None)
    if take_profit is None and trade.meta:
        take_profit = trade.meta.get("take_profit")

    metadata: dict[str, Any] = dict(trade.meta) if trade.meta else {}
    if account_balance is not None:
        metadata["account_balance"] = float(account_balance)

    return TradeSignal(
        action=action,
        symbol=symbol,
        entry_price=float(trade.entry_price),
        stop_loss=float(stop_loss) if stop_loss is not None else None,
        take_profit=float(take_profit) if take_profit is not None else None,
        timeframe=timeframe,
        metadata=metadata,
    )


class RiskAdapter:
    """Adapter that surfaces RiskEngine calculations for validation use-cases."""

    def __init__(
        self,
        engine: RiskEngine | None = None,
        pip_value: float = 1.0,
    ):
        self.engine = engine or RiskEngine()
        self.pip_value = float(pip_value)

    def trade_to_signal(
        self,
        trade: Trade,
        symbol: str,
        timeframe: str,
        account_balance: float | None = None,
    ) -> TradeSignal:
        """Convert a ``Trade`` into a ``TradeSignal``.

        Convenience wrapper around :func:`_trade_to_signal`.
        """
        return _trade_to_signal(trade, symbol, timeframe, account_balance)

    @staticmethod
    def _stop_distance(entry_price: float, stop_loss: float) -> float:
        return abs(float(entry_price) - float(stop_loss))

    @staticmethod
    def _risk_reward_ratio(
        entry_price: float,
        stop_loss: float,
        take_profit: float | None,
    ) -> float | None:
        if take_profit is None:
            return None
        risk_distance = abs(float(entry_price) - float(stop_loss))
        if risk_distance <= 0 or not math.isfinite(risk_distance):
            return None
        reward_distance = abs(float(take_profit) - float(entry_price))
        if reward_distance <= 0 or not math.isfinite(reward_distance):
            return None
        return reward_distance / risk_distance

    def calculate_trade_risk(
        self,
        trade: Trade,
        symbol: str,
        timeframe: str,
        account_balance: float,
        current_exposure: float = 0.0,
    ) -> TradeRiskProfile:
        """Compute a full risk profile for a single trade.

        If the trade has no explicit stop-loss, the underlying
        ``RiskEngine`` fills in a fallback stop.  The resolved value is
        reflected in the returned profile.

        Raises:
            ValueError: Raised by ``RiskEngine`` when inputs are invalid
                (e.g. non-positive ``account_balance`` or indistinguishable
                entry / stop prices).
        """
        signal = self.trade_to_signal(trade, symbol, timeframe, account_balance)
        sized_order = self.engine.calculate_position_size(
            signal=signal,
            account_balance=account_balance,
            pip_value=self.pip_value,
            current_exposure=current_exposure,
        )

        # After sizing the signal may contain a fallback-resolved stop.
        resolved_stop = signal.stop_loss
        stop_distance = (
            self._stop_distance(signal.entry_price, resolved_stop)
            if resolved_stop is not None
            else 0.0
        )

        rr = (
            self._risk_reward_ratio(
                signal.entry_price,
                resolved_stop,
                signal.take_profit,
            )
            if resolved_stop is not None
            else None
        )
        if rr is None and sized_order.risk_reward_ratio is not None:
            rr = sized_order.risk_reward_ratio

        return TradeRiskProfile(
            trade_id=trade.id,
            entry_price=trade.entry_price,
            stop_loss=resolved_stop,
            take_profit=signal.take_profit,
            direction=trade.direction,
            position_size=sized_order.position_size,
            risk_amount=sized_order.risk_amount,
            risk_percent=sized_order.risk_percent,
            stop_distance=stop_distance,
            risk_reward_ratio=rr,
        )

    def size(
        self,
        input: RiskSizingInput,
        model: SizingModel = SizingModel.FIXED_RISK,
        config: FixedRiskConfig | PercentRiskConfig | None = None,
    ) -> RiskSizingResult:
        """Calculate position sizing using a dedicated risk model.

        Args:
            input: Sizing parameters (entry, stop, account balance, etc.).
            model: Which sizing model to apply.
            config: Model-specific configuration.  If ``None`` a default
                config is created.

        Raises:
            TypeError: If ``config`` does not match the selected ``model``.
        """
        if model == SizingModel.PERCENT_RISK:
            if config is None:
                config = PercentRiskConfig(risk_percent=0.01)
            if not isinstance(config, PercentRiskConfig):
                raise TypeError(
                    "config must be PercentRiskConfig for percent_risk model"
                )
            return PercentRiskModel(config).size(input)

        if config is None:
            config = FixedRiskConfig(risk_amount=100.0)
        if not isinstance(config, FixedRiskConfig):
            raise TypeError("config must be FixedRiskConfig for fixed_risk model")
        return FixedRiskModel(config).size(input)

    def calculate_validation_reference_risk(
        self,
        reference: ValidationReference,
        account_balance: float,
        current_exposure: float = 0.0,
    ) -> RiskSizingResult:
        """Compute ``TradeRiskProfile`` for every trade in a reference.

        Aggregated totals are simple sums of per-trade ``risk_amount``
        and ``risk_percent``.
        """
        profiles: list[TradeRiskProfile] = []
        total_risk_amount = 0.0
        total_risk_percent = 0.0

        for trade in reference.trades:
            profile = self.calculate_trade_risk(
                trade=trade,
                symbol=reference.symbol,
                timeframe=reference.timeframe,
                account_balance=account_balance,
                current_exposure=current_exposure,
            )
            profiles.append(profile)
            total_risk_amount += profile.risk_amount
            total_risk_percent += profile.risk_percent

        return RiskSizingResult(
            profiles=profiles,
            total_risk_amount=total_risk_amount,
            total_risk_percent=total_risk_percent,
        )

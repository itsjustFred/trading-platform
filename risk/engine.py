from __future__ import annotations

import math
from dataclasses import dataclass

from risk.config import RiskConfig
from risk.models import RiskDecision, SizedOrder, TradeSignal


@dataclass(slots=True)
class RiskEngine:
    config: RiskConfig

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    def _fallback_stop_loss(self, signal: TradeSignal) -> float:
        atr = signal.metadata.get("atr")
        if atr is not None:
            distance = abs(float(atr)) * self.config.default_sl_atr_multiplier
        else:
            # Percent-of-price fallback. An absolute (pip-based) distance under-scales
            # on high-priced assets and produces drastically oversized positions.
            distance = signal.entry_price * self.config.fallback_stop_pct
        if distance <= 0 or not math.isfinite(distance):
            distance = signal.entry_price * 0.02
        if signal.action in {"buy", "long"}:
            return signal.entry_price - distance
        return signal.entry_price + distance

    @staticmethod
    def _risk_reward_ratio(signal: TradeSignal, stop_loss: float) -> float | None:
        if signal.take_profit is None:
            return None
        risk_distance = abs(signal.entry_price - stop_loss)
        if risk_distance <= 0:
            return None
        reward_distance = abs(float(signal.take_profit) - signal.entry_price)
        return reward_distance / risk_distance

    @staticmethod
    def _stop_matches_direction(signal: TradeSignal, stop_loss: float) -> bool:
        if signal.action in {"buy", "long"}:
            return stop_loss < signal.entry_price
        return stop_loss > signal.entry_price

    @staticmethod
    def _signal_id(signal: TradeSignal) -> str:
        explicit = signal.metadata.get("signal_id") or signal.metadata.get("source_signal_id")
        if explicit:
            return str(explicit)
        stop = "none" if signal.stop_loss is None else f"{float(signal.stop_loss):.12g}"
        take_profit = (
            "none" if signal.take_profit is None else f"{float(signal.take_profit):.12g}"
        )
        return (
            "legacy:"
            f"{signal.symbol}:{signal.timeframe}:{signal.action}:"
            f"{signal.entry_price:.12g}:{stop}:{take_profit}"
        )

    def _decision(
        self,
        signal: TradeSignal,
        *,
        signal_id: str,
        accepted: bool,
        quantity: float,
        risk_amount: float,
        risk_percent: float,
        risk_distance: float | None,
        stop_price_used: float | None,
        rejection_reason: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            signal_id=signal_id,
            accepted=accepted,
            quantity=quantity,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            risk_distance=risk_distance,
            stop_price_used=stop_price_used,
            take_profit_used=signal.take_profit,
            rejection_reason=rejection_reason,
            metadata={
                "max_risk_per_trade": self.config.max_risk_per_trade,
                "max_total_exposure": self.config.max_total_exposure,
                "max_positions": self.config.max_positions,
                **dict(metadata or {}),
            },
        )

    def _sized_order(
        self,
        signal: TradeSignal,
        *,
        position_size: float,
        risk_amount: float,
        risk_percent: float,
        risk_reward_ratio: float | None,
        decision: RiskDecision,
    ) -> SizedOrder:
        return SizedOrder(
            signal=signal,
            position_size=position_size,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            risk_reward_ratio=risk_reward_ratio,
            decision=decision,
        )

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account_balance: float,
        pip_value: float,
        current_exposure: float,
    ) -> SizedOrder:
        balance = float(account_balance)
        if balance <= 0:
            raise ValueError("account_balance must be > 0")
        pip = float(pip_value)
        if pip <= 0:
            raise ValueError("pip_value must be > 0")
        exposure = max(0.0, float(current_exposure))
        current_positions = int(float(signal.metadata.get("current_positions", 0) or 0))
        decision_signal_id = self._signal_id(signal)

        if current_positions >= self.config.max_positions:
            return self._sized_order(
                signal=signal,
                position_size=0.0,
                risk_amount=0.0,
                risk_percent=0.0,
                risk_reward_ratio=None,
                decision=self._decision(
                    signal,
                    signal_id=decision_signal_id,
                    accepted=False,
                    quantity=0.0,
                    risk_amount=0.0,
                    risk_percent=0.0,
                    risk_distance=None,
                    stop_price_used=signal.stop_loss,
                    rejection_reason="max_positions",
                    metadata={"current_positions": current_positions},
                ),
            )

        remaining_exposure = max(0.0, self.config.max_total_exposure - exposure)
        risk_percent = min(self.config.max_risk_per_trade, remaining_exposure)
        if risk_percent <= 0:
            return self._sized_order(
                signal=signal,
                position_size=0.0,
                risk_amount=0.0,
                risk_percent=0.0,
                risk_reward_ratio=None,
                decision=self._decision(
                    signal,
                    signal_id=decision_signal_id,
                    accepted=False,
                    quantity=0.0,
                    risk_amount=0.0,
                    risk_percent=0.0,
                    risk_distance=None,
                    stop_price_used=signal.stop_loss,
                    rejection_reason="max_total_exposure",
                    metadata={"current_exposure": exposure},
                ),
            )

        stop_loss = signal.stop_loss
        stop_source = "signal"
        if stop_loss is None:
            stop_source = (
                "fallback_atr"
                if signal.metadata.get("atr") is not None
                else "fallback_percent"
            )
            stop_loss = self._fallback_stop_loss(signal)
            signal.stop_loss = stop_loss
            signal.metadata["resolved_stop_loss"] = stop_loss
        enforce_stop_direction = bool(signal.metadata.get("enforce_stop_direction", False))
        if enforce_stop_direction and not self._stop_matches_direction(signal, stop_loss):
            risk_distance = abs(signal.entry_price - stop_loss)
            return self._sized_order(
                signal=signal,
                position_size=0.0,
                risk_amount=0.0,
                risk_percent=0.0,
                risk_reward_ratio=None,
                decision=self._decision(
                    signal,
                    signal_id=decision_signal_id,
                    accepted=False,
                    quantity=0.0,
                    risk_amount=0.0,
                    risk_percent=0.0,
                    risk_distance=risk_distance,
                    stop_price_used=stop_loss,
                    rejection_reason="bad_stop_direction",
                    metadata={
                        "current_exposure": exposure,
                        "enforce_stop_direction": enforce_stop_direction,
                        "stop_source": stop_source,
                    },
                ),
            )

        risk_distance = abs(signal.entry_price - stop_loss)
        if risk_distance <= 0 or not math.isfinite(risk_distance):
            raise ValueError("entry_price and stop_loss must differ for risk sizing")

        risk_amount = balance * risk_percent
        raw_size = risk_amount / (risk_distance * pip)
        position_size = max(0.0, raw_size)
        risk_reward_ratio = self._risk_reward_ratio(signal, stop_loss)

        return self._sized_order(
            signal=signal,
            position_size=position_size,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            risk_reward_ratio=risk_reward_ratio,
            decision=self._decision(
                signal,
                signal_id=decision_signal_id,
                accepted=True,
                quantity=position_size,
                risk_amount=risk_amount,
                risk_percent=risk_percent,
                risk_distance=risk_distance,
                stop_price_used=stop_loss,
                metadata={
                    "current_exposure": exposure,
                    "pip_value": pip,
                    "stop_source": stop_source,
                },
            ),
        )

    def apply_trailing_stop(
        self,
        signal: TradeSignal,
        *,
        market_price: float,
    ) -> float | None:
        price = float(market_price)
        if price <= 0:
            raise ValueError("market_price must be > 0")

        trailing_cfg = signal.metadata.get("trailing_sl_config")
        if not isinstance(trailing_cfg, dict):
            return signal.stop_loss

        mode = str(trailing_cfg.get("mode") or "").strip().lower()
        if mode != "percent":
            return signal.stop_loss

        percent = float(trailing_cfg.get("percent") or 0.0)
        if percent <= 0 or percent >= 1:
            raise ValueError("trailing_sl_config.percent must be between 0 and 1")

        action = signal.action
        current_stop = signal.stop_loss
        if action in {"buy", "long"}:
            candidate = price * (1.0 - percent)
            if current_stop is None or candidate > current_stop:
                signal.stop_loss = candidate
        else:
            candidate = price * (1.0 + percent)
            if current_stop is None or candidate < current_stop:
                signal.stop_loss = candidate

        return signal.stop_loss

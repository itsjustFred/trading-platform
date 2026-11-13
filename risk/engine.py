from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from risk.config import RiskConfig
from risk.models import RiskDecision, SizedOrder, TradeSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskEngine:
    config: RiskConfig

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    def _fallback_stop_loss(self, signal: TradeSignal) -> float:
        # C1: finite-guard ATR — NaN/negative/garbage values are rejected,
        # never silently healed (abs() + 0.02 fallback hid bad inputs).
        atr = signal.metadata.get("atr")
        if atr is not None:
            try:
                atr_value = float(atr)
            except (TypeError, ValueError):
                raise ValueError(
                    f"ATR value {atr!r} is not a number; refusing to size "
                    "on an uninterpretable stop distance."
                )
            if not math.isfinite(atr_value) or atr_value <= 0:
                raise ValueError(
                    f"ATR value {atr!r} is not finite and positive; refusing "
                    "to size on an uninterpretable stop distance."
                )
            distance = atr_value * self.config.default_sl_atr_multiplier
        else:
            # Percent-of-price fallback. An absolute (pip-based) distance under-scales
            # on high-priced assets and produces drastically oversized positions.
            distance = signal.entry_price * self.config.fallback_stop_pct
        if distance <= 0 or not math.isfinite(distance):
            raise ValueError(
                "Fallback stop distance is not finite and positive "
                f"(got {distance!r}); refusing to size on it."
            )
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
        cached = signal.metadata.get("_risk_legacy_signal_id")
        if cached:
            return str(cached)
        stop = "none" if signal.stop_loss is None else f"{float(signal.stop_loss):.12g}"
        take_profit = (
            "none" if signal.take_profit is None else f"{float(signal.take_profit):.12g}"
        )
        signal_id = (
            "legacy:"
            f"{signal.symbol}:{signal.timeframe}:{signal.action}:"
            f"{signal.entry_price:.12g}:{stop}:{take_profit}:{signal.identity}"
        )
        signal.metadata["_risk_legacy_signal_id"] = signal_id
        return signal_id

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

    @staticmethod
    def _finite_number(value: object, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be numeric") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field_name} must be finite")
        return parsed

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account_balance: float,
        pip_value: float,
        current_exposure: float,
        *,
        allow_fallback_stop: bool | None = None,
        reject_missing_stop: bool | None = None,
        max_notional_per_trade: float | None = None,
    ) -> SizedOrder:
        """Size one signal against the configured risk limits.

        Unit contract (all limits are fractions of ``account_balance``):
        - ``current_exposure``: aggregate open risk, fraction of balance.
        - ``config.max_total_exposure``: aggregate open-risk ceiling, fraction.
        - ``config.max_risk_per_trade``: maximum loss on this trade, fraction.
        - ``config.max_notional_per_trade``: default notional ceiling for the
          resulting position, as a fraction of balance. The position's notional
          is ``position_size * entry_price * pip_value`` in account currency.
        - ``pip_value``: account-currency P&L of one ``position_size`` unit per
          1.0 price move (1.0 for shares in the account currency; cTrader
          EURUSD on a USD account: 1000 per 0.01 lot).
        - ``max_notional_per_trade``: optional caller-specific ceiling, used by
          engines whose leverage policy is independent of the risk config.

        The returned ``SizedOrder.position_size`` is in account-currency units
        of the traded symbol (shares/lots); it is the only quantity in that
        unit domain. Caller-supplied state (``current_positions``,
        ``current_exposure``) must come from the portfolio/broker source of
        truth, never from untrusted signal payloads.
        """
        balance = self._finite_number(account_balance, "account_balance")
        if balance <= 0:
            raise ValueError("account_balance must be > 0")
        pip = self._finite_number(pip_value, "pip_value")
        if pip <= 0:
            raise ValueError("pip_value must be > 0")
        notional_limit = (
            self.config.max_notional_per_trade
            if max_notional_per_trade is None
            else self._finite_number(max_notional_per_trade, "max_notional_per_trade")
        )
        if notional_limit <= 0:
            raise ValueError("max_notional_per_trade must be > 0")
        exposure = self._finite_number(current_exposure, "current_exposure")
        if exposure < 0:
            raise ValueError("current_exposure must be >= 0")
        raw_positions = signal.metadata.get("current_positions", 0) or 0
        if isinstance(raw_positions, bool):
            raise ValueError("current_positions must be an integer")
        try:
            numeric_positions = float(raw_positions)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("current_positions must be an integer") from exc
        if not math.isfinite(numeric_positions) or not numeric_positions.is_integer():
            raise ValueError("current_positions must be an integer")
        current_positions = int(numeric_positions)
        if current_positions < 0:
            raise ValueError("current_positions must be >= 0")
        decision_signal_id = self._signal_id(signal)
        fallback_allowed = (
            self.config.allow_fallback_stop
            if allow_fallback_stop is None
            else RiskConfig._bool(allow_fallback_stop, "allow_fallback_stop")
        )
        if reject_missing_stop is None:
            reject_missing_stop = self.config.reject_missing_stop
        else:
            reject_missing_stop = RiskConfig._bool(reject_missing_stop, "reject_missing_stop")

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
        if stop_loss is not None and (
            not math.isfinite(float(stop_loss)) or float(stop_loss) <= 0
        ):
            raise ValueError("stop_loss must be finite and > 0")
        stop_source = "signal"
        if stop_loss is None and (reject_missing_stop or not fallback_allowed):
            logger.warning(
                "Rejecting %s/%s: no signal stop_loss and fallback stops are disabled",
                signal.symbol,
                signal.timeframe,
            )
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
                    stop_price_used=None,
                    rejection_reason="missing_stop",
                    metadata={
                        "current_exposure": exposure,
                        "stop_source": "missing",
                        "fallback_stop_allowed": False,
                    },
                ),
            )
        if stop_loss is None:
            stop_source = (
                "fallback_atr"
                if signal.metadata.get("atr") is not None
                else "fallback_percent"
            )
            # C1: a fallback stop is an explicit, logged event — sizing on
            # an invented distance must never be silent.
            logger.warning(
                "Sizing on %s fallback stop (no signal stop provided)",
                stop_source,
                extra={"data": {"symbol": signal.symbol, "stop_source": stop_source}},
            )
            stop_loss = self._fallback_stop_loss(signal)
            signal.stop_loss = stop_loss
            signal.metadata["resolved_stop_loss"] = stop_loss
        enforce_stop_direction = bool(signal.metadata.get("enforce_stop_direction", False))
        if enforce_stop_direction and not self._stop_matches_direction(signal, stop_loss):
            risk_distance: float | None = abs(signal.entry_price - stop_loss)
            if risk_distance <= 0 or not math.isfinite(risk_distance):
                risk_distance = None
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
        max_notional = balance * notional_limit
        # #146: ``pip`` is the account-currency value of one size unit per 1.0
        # price move, so one unit's notional in account currency is
        # ``entry_price * pip`` (cTrader EURUSD, USD account: 1.10 * 1000 =
        # 1,100 USD per 0.01 lot). Comparing ``size * entry_price`` alone made
        # the cap 1000x too loose there. pip == 1.0 (shares) is unchanged.
        notional_per_unit = signal.entry_price * pip
        capped_by_notional = False
        if position_size * notional_per_unit > max_notional:
            position_size = max_notional / notional_per_unit
            capped_by_notional = True
        # risk_amount/risk_percent stay the policy figures sizing was asked for
        # (they anchor the validation-reference parity contract); the realized
        # stop-loss amount of a notional-capped position is reported here.
        realized_risk_amount = (
            position_size * risk_distance * pip if capped_by_notional else None
        )
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
                    "fallback_stop_allowed": fallback_allowed and not reject_missing_stop,
                    "stop_price_provenance": stop_source,
                    "max_notional_per_trade": notional_limit,
                    "capped_by_notional": capped_by_notional,
                    **(
                        {"realized_risk_amount": realized_risk_amount}
                        if realized_risk_amount is not None
                        else {}
                    ),
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
            # C1: unsupported trailing modes (e.g. supertrend_flip) are an
            # explicit warned no-op — never a silent one.
            logger.warning(
                "Unsupported trailing stop mode %r; leaving stop unchanged",
                mode,
                extra={"data": {"symbol": signal.symbol, "mode": mode}},
            )
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

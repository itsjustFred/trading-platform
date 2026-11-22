from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# All exposure values are FRACTIONS OF ACCOUNT EQUITY in [0, 1]:
#   - max_risk_per_trade: maximum loss on a single trade (as a fraction of
#     account balance) if the stop is hit.
#   - max_total_exposure: aggregate open risk across all positions (same unit).
#   - max_notional_per_trade: upper bound on a single position's notional value
#     as a fraction of account balance (leverage guard).
# Sizing quantities (SizedOrder.position_size) are in ACCOUNT-CURRENCY UNITS of
# the traded symbol (shares/lots). Never mix the two unit domains; conversions
# happen only inside RiskEngine.calculate_position_size.
UNIT_FRACTION = "fraction_of_balance"
UNIT_QUANTITY = "account_currency_units_of_symbol"


@dataclass(slots=True)
class RiskConfig:
    max_risk_per_trade: float = 0.01
    max_total_exposure: float = 0.05
    max_positions: int = 5
    default_sl_atr_multiplier: float = 2.0
    fallback_stop_pct: float = 0.02
    max_notional_per_trade: float = 0.20
    allow_fallback_stop: bool = True
    reject_missing_stop: bool = False

    def __post_init__(self) -> None:
        self.max_risk_per_trade = self._finite_float(
            self.max_risk_per_trade, "max_risk_per_trade"
        )
        self.max_total_exposure = self._finite_float(
            self.max_total_exposure, "max_total_exposure"
        )
        self.max_positions = self._positive_int(self.max_positions, "max_positions")
        self.default_sl_atr_multiplier = self._finite_float(
            self.default_sl_atr_multiplier, "default_sl_atr_multiplier"
        )
        self.fallback_stop_pct = self._finite_float(
            self.fallback_stop_pct, "fallback_stop_pct"
        )
        self.max_notional_per_trade = self._finite_float(
            self.max_notional_per_trade, "max_notional_per_trade"
        )
        self.allow_fallback_stop = self._bool(self.allow_fallback_stop, "allow_fallback_stop")
        self.reject_missing_stop = self._bool(self.reject_missing_stop, "reject_missing_stop")

        if not 0 < self.max_risk_per_trade <= 1:
            raise ValueError("max_risk_per_trade must be > 0 and <= 1")
        if not 0 < self.max_total_exposure <= 1:
            raise ValueError("max_total_exposure must be > 0 and <= 1")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be > 0")
        if self.default_sl_atr_multiplier <= 0:
            raise ValueError("default_sl_atr_multiplier must be > 0")
        if not (0 < self.fallback_stop_pct < 1):
            raise ValueError("fallback_stop_pct must be between 0 and 1")
        if not 0 < self.max_notional_per_trade <= 1:
            raise ValueError("max_notional_per_trade must be > 0 and <= 1")

    @staticmethod
    def _finite_float(value: Any, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be numeric") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field_name} must be finite")
        return parsed

    @staticmethod
    def _positive_int(value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{field_name} must be an integer")
        return int(numeric)

    @staticmethod
    def _bool(value: object, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise ValueError(f"{field_name} must be boolean")

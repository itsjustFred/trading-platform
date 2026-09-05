from __future__ import annotations

from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        self.max_risk_per_trade = float(self.max_risk_per_trade)
        self.max_total_exposure = float(self.max_total_exposure)
        self.max_positions = int(self.max_positions)
        self.default_sl_atr_multiplier = float(self.default_sl_atr_multiplier)
        self.fallback_stop_pct = float(self.fallback_stop_pct)
        self.max_notional_per_trade = float(self.max_notional_per_trade)

        if self.max_risk_per_trade <= 0:
            raise ValueError("max_risk_per_trade must be > 0")
        if self.max_total_exposure <= 0:
            raise ValueError("max_total_exposure must be > 0")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be > 0")
        if self.default_sl_atr_multiplier <= 0:
            raise ValueError("default_sl_atr_multiplier must be > 0")
        if not (0 < self.fallback_stop_pct < 1):
            raise ValueError("fallback_stop_pct must be between 0 and 1")
        if self.max_notional_per_trade <= 0:
            raise ValueError("max_notional_per_trade must be > 0")

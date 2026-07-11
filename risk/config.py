from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class RiskConfig:
    max_risk_per_trade: float = 0.01
    max_total_exposure: float = 0.05
    max_positions: int = 5
    default_sl_atr_multiplier: float = 2.0
    fallback_stop_pct: float = 0.02

    def __post_init__(self) -> None:
        self.max_risk_per_trade = float(self.max_risk_per_trade)
        if not math.isfinite(self.max_risk_per_trade):
            raise ValueError("max_risk_per_trade must be finite")
        self.max_total_exposure = float(self.max_total_exposure)
        if not math.isfinite(self.max_total_exposure):
            raise ValueError("max_total_exposure must be finite")
        if isinstance(self.max_positions, bool) or not float(self.max_positions).is_integer():
            raise ValueError("max_positions must be an integral value")
        self.max_positions = int(self.max_positions)
        self.default_sl_atr_multiplier = float(self.default_sl_atr_multiplier)
        if not math.isfinite(self.default_sl_atr_multiplier):
            raise ValueError("default_sl_atr_multiplier must be finite")
        self.fallback_stop_pct = float(self.fallback_stop_pct)
        if not math.isfinite(self.fallback_stop_pct):
            raise ValueError("fallback_stop_pct must be finite")

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

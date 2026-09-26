"""Paper-readiness models for strategy validation.

Defines the canonical data structures used by the readiness validator
and the resulting report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from validation.comparison.tolerances import finite_tolerance


class PaperReadinessLevel(str, Enum):
    """Aggregated readiness classification."""

    NOT_READY = "NOT_READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    READY = "READY"


class ReadinessCheck(BaseModel):
    """A single paper-readiness check with its result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str = ""
    passed: bool = False
    severity: Literal["error", "warning", "info"] = "info"
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ReadinessReport(BaseModel):
    """Complete paper-readiness report produced by :class:`ReadinessValidator`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    level: PaperReadinessLevel = PaperReadinessLevel.NOT_READY
    checks: list[ReadinessCheck] = Field(default_factory=list)
    passed: bool = False
    errors: int = 0
    warnings: int = 0
    recommendations: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReadinessConfig(BaseModel):
    """Configurable thresholds for the readiness validator."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_missing_trades: int = 0
    max_extra_trades: int = 0
    max_mismatched_trades: int = 0
    max_error_count: int = 0

    require_all_goldens_pass: bool = True

    # Mandatory artifacts. When a required artifact is missing the gate emits an
    # error-level check so it can never report READY on an incomplete validation.
    require_trade_report: bool = True
    require_regression: bool = True
    require_risk_report: bool = True
    require_stats: bool = True

    max_position_size_diff_pct: float = 0.01
    max_stop_distance_diff_pct: float = 0.01

    stats_win_rate_tolerance_pct: float = 0.01
    stats_net_profit_tolerance_pct: float = 0.01
    # #83: Sharpe gate — inputs must be canonical rf=0 stats on BOTH sides
    # (as produced by validation/stats/calculator.py); the gate compares
    # stored fields and cannot re-derive the risk-free rate. Runner-produced
    # stats must be recomputed with risk_free_rate=0.0 before validation.
    # Pass when the absolute drift fits max(10% relative, 0.1 absolute).
    stats_sharpe_tolerance_pct: float = 0.10
    stats_sharpe_tolerance_abs: float = 0.1
    # #83: max-drawdown gate — 2pp absolute on max_drawdown_pct.
    stats_max_drawdown_tolerance_pp: float = 0.02

    @model_validator(mode="after")
    def _validate_tolerances(self) -> "ReadinessConfig":
        for name in (
            "max_position_size_diff_pct",
            "max_stop_distance_diff_pct",
            "stats_win_rate_tolerance_pct",
            "stats_net_profit_tolerance_pct",
            "stats_sharpe_tolerance_pct",
            "stats_sharpe_tolerance_abs",
            "stats_max_drawdown_tolerance_pp",
        ):
            value = finite_tolerance(getattr(self, name), name)
            setattr(self, name, value)
        return self

"""Paper-readiness models for strategy validation.

Defines the canonical data structures used by the readiness validator
and the resulting report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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



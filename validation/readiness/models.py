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
    #: Whether this check could be evaluated at all. `True` for every check that has an answer,
    #: including a failing one - a failure is an answer. `False` means the check had nothing to
    #: compare and is therefore excluded from the counts that decide readiness.
    #:
    #: Added for the zero-expectation Sharpe case, where the relative term of the tolerance
    #: vanishes and there is no scale to be proportionate to. It is `True` everywhere by default
    #: and is only ever `False` when `ReadinessConfig` opts in, so existing reports aggregate
    #: identically.
    applicable: bool = True


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
    # Pass when the absolute drift fits max(10% relative, 0.05 absolute).
    #
    # The absolute floor was 0.1, and it is the floor that governs whenever the expectation
    # is small - which is the common case, not an edge case: 97.9% of the backtest corpus
    # stores `sharpe_ratio` exactly 0.0. At 0.1 the gate was ~15x looser in relative terms
    # at zero than at a typical Sharpe of 1.5, so a drift of 0.09 against an expectation of
    # 0.15 - a 60% proportional regression - passed.
    #
    # Halved to 0.05 because it is the cheapest fix with a measured scope: modelling the gate
    # as reference-vs-platform over the 1,028 corpus pairs that actually differ, 0.05 flips
    # **7** of 2,655 rows and 0.02 flips 9, so 0.05 captures most of the tightening at the
    # smallest blast radius. Reversible: this is a config default, not logic.
    #
    # Deliberately NOT changed here: the fact that `expected == 0` should arguably be
    # "not comparable" rather than pass or fail. That is a policy question, it would remove
    # 1,572 of 2,655 rows (59.2%) from the verdict - all of which pass today - and
    # `ReadinessCheck` has no state to express it. Tracked in the backlog, not decided here.
    stats_sharpe_tolerance_pct: float = 0.10
    stats_sharpe_tolerance_abs: float = 0.05
    # #83 follow-up: what to do when the *expected* Sharpe is exactly zero, where the relative
    # term vanishes and only the absolute floor applies. This is the open policy question in the
    # backlog, and it is exposed here as a switch rather than left implicit in the arithmetic.
    #
    # `False` (the default) preserves today's behaviour exactly: a zero expectation is compared
    # against the floor and can pass or fail like any other value.
    #
    # `True` would mark the check not-applicable instead, on the reasoning that an expectation of
    # zero carries no scale to be proportionate to. It is NOT enabled, because it would remove
    # 1,572 of 2,655 rows (59.2%) from the verdict - all of which pass today - while 1,019 of the
    # 1,083 genuinely comparable rows already fail. That is a change to what the platform
    # certifies, not a bug fix, and it belongs to whoever owns that policy.
    #
    # The field exists so that turning it on later is a one-word config change rather than a code
    # change, and so that the current behaviour is a decision on the record instead of an
    # accident of arithmetic.
    stats_sharpe_zero_expectation_is_not_comparable: bool = False
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

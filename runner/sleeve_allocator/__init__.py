"""Portfolio sleeve allocator (issue #307).

Phase 1: sleeve-returns loader, eligibility filter, 1/N and inverse-vol
baselines, and a monthly walk-forward allocation harness that always reports
both baselines. No live wiring, no Optuna — later phases add ERC/HRP
objectives, shrinkage, constraints and promotion-gate integration.
"""
from .eligibility import EligibilityConfig, EligibilityResult, filter_eligible_sleeves
from .harness import (
    AllocationConfig,
    AllocationResult,
    RebalanceRecord,
    curve_metrics,
    walk_forward_allocation,
)
from .loader import (
    SleeveMetadata,
    SleeveReturns,
    TradePeriod,
    align_sleeve_returns,
    load_sleeve_returns,
)
from .objectives import BASELINE_OBJECTIVES, SUPPORTED_OBJECTIVES, solve_weights

__all__ = [
    "BASELINE_OBJECTIVES",
    "SUPPORTED_OBJECTIVES",
    "AllocationConfig",
    "AllocationResult",
    "EligibilityConfig",
    "EligibilityResult",
    "RebalanceRecord",
    "SleeveMetadata",
    "SleeveReturns",
    "TradePeriod",
    "align_sleeve_returns",
    "curve_metrics",
    "filter_eligible_sleeves",
    "load_sleeve_returns",
    "solve_weights",
    "walk_forward_allocation",
]

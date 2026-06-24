"""Trade comparison engine for strategy validation."""

from __future__ import annotations

from validation.comparison.comparison import (
    FieldComparisonResult,
    MatchedTrade,
    TradeComparisonConfig,
    TradeComparisonResult,
    TradeComparator,
)
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    FieldDelta,
    TradeDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import MatchKey, MatchResult, TradeMatcher

__all__ = [
    "DeltaReportBuilder",
    "FieldComparisonResult",
    "FieldDelta",
    "MatchedTrade",
    "MatchKey",
    "MatchResult",
    "TradeComparisonConfig",
    "TradeComparisonResult",
    "TradeComparator",
    "TradeDelta",
    "TradeDeltaReport",
    "TradeMatcher",
]

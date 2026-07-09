"""Anomaly detection for strategy validation.

Detects missing trades, extra trades, mismatched trades and duplicate
matches after the matcher and comparator have run.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.models import Trade
from validation.comparison.comparison import (
    FieldComparisonResult,
    MatchedTrade,
    TradeComparisonResult,
)
from validation.comparison.matcher import TradeMatcher
from validation.reference import ValidationReference


class AnomalyType(str, Enum):
    MISSING_TRADE = "MISSING_TRADE"
    EXTRA_TRADE = "EXTRA_TRADE"
    MISMATCHED_TRADE = "MISMATCHED_TRADE"
    DUPLICATE_MATCH = "DUPLICATE_MATCH"


class TradeAnomaly(BaseModel):
    """A single detected anomaly in the trade comparison."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    anomaly_type: AnomalyType
    expected_trade: Trade | None = None
    actual_trade: Trade | None = None
    matched_trade: MatchedTrade | None = None
    field_mismatches: list[FieldComparisonResult] = Field(default_factory=list)
    severity: Literal["info", "warning", "error"]
    message: str


class AnomalyDetector:
    """Detect anomalies in a validation run.

    Works on the output of :class:`TradeMatcher` and optionally
    :class:`TradeComparator`.
    """

    @classmethod
    def detect(
        cls,
        reference: ValidationReference,
        platform_trades: list[Trade],
        comparison_results: list[TradeComparisonResult] | None = None,
        **match_kwargs: Any,
    ) -> list[TradeAnomaly]:
        """Run all detection methods and return aggregated anomalies."""
        anomalies: list[TradeAnomaly] = []

        ref_trades = reference.trades

        anomalies.extend(cls.detect_missing(ref_trades, platform_trades, **match_kwargs))
        anomalies.extend(cls.detect_extra(ref_trades, platform_trades, **match_kwargs))

        if comparison_results:
            anomalies.extend(cls.detect_mismatches(comparison_results))

        match_result = TradeMatcher.match(ref_trades, platform_trades, **match_kwargs)
        matched_trades = [
            MatchedTrade(reference=ref, actual=act, key=key)
            for ref, act, key in match_result.matched
        ]
        anomalies.extend(cls.detect_duplicates(matched_trades))

        return anomalies

    @classmethod
    def detect_missing(
        cls,
        reference_trades: list[Trade],
        platform_trades: list[Trade],
        **match_kwargs: Any,
    ) -> list[TradeAnomaly]:
        """Return anomalies for reference trades with no platform pair."""
        result = TradeMatcher.match(reference_trades, platform_trades, **match_kwargs)
        anomalies: list[TradeAnomaly] = []
        for trade in result.missing:
            msg = (
                f"Reference trade {trade.symbol} {trade.direction} "
                f"at {trade.entry_time} (id={trade.id}) is missing in platform trades"
            )
            anomalies.append(
                TradeAnomaly(
                    anomaly_type=AnomalyType.MISSING_TRADE,
                    expected_trade=trade,
                    severity="warning",
                    message=msg,
                )
            )
        return anomalies

    @classmethod
    def detect_extra(
        cls,
        reference_trades: list[Trade],
        platform_trades: list[Trade],
        **match_kwargs: Any,
    ) -> list[TradeAnomaly]:
        """Return anomalies for platform trades with no reference pair."""
        result = TradeMatcher.match(reference_trades, platform_trades, **match_kwargs)
        anomalies: list[TradeAnomaly] = []
        for trade in result.extra:
            msg = (
                f"Platform trade {trade.symbol} {trade.direction} "
                f"at {trade.entry_time} (id={trade.id}) has no matching reference trade"
            )
            anomalies.append(
                TradeAnomaly(
                    anomaly_type=AnomalyType.EXTRA_TRADE,
                    actual_trade=trade,
                    severity="info",
                    message=msg,
                )
            )
        return anomalies

    @classmethod
    def detect_mismatches(
        cls,
        comparison_results: list[TradeComparisonResult],
    ) -> list[TradeAnomaly]:
        """Turn comparison failures into anomalies."""
        anomalies: list[TradeAnomaly] = []
        for comp in comparison_results:
            if comp.is_match:
                continue
            field_mismatches = [
                comp.field_results[name]
                for name in comp.mismatches
                if name in comp.field_results
            ]
            severity = cls._severity_for_mismatches(field_mismatches)
            ref = comp.matched_trade.reference
            msg = (
                f"Matched trade {ref.symbol} {ref.direction} "
                f"at {ref.entry_time} has mismatches: "
                f"{', '.join(comp.mismatches)}"
            )
            anomalies.append(
                TradeAnomaly(
                    anomaly_type=AnomalyType.MISMATCHED_TRADE,
                    matched_trade=comp.matched_trade,
                    field_mismatches=field_mismatches,
                    severity=severity,
                    message=msg,
                )
            )
        return anomalies

    @classmethod
    def detect_duplicates(
        cls,
        matches: list[MatchedTrade],
    ) -> list[TradeAnomaly]:
        """Detect reference or actual trades used in more than one match."""
        anomalies: list[TradeAnomaly] = []
        ref_seen: dict[int, list[MatchedTrade]] = {}
        act_seen: dict[int, list[MatchedTrade]] = {}

        for mt in matches:
            ref_seen.setdefault(mt.reference.id, []).append(mt)
            act_seen.setdefault(mt.actual.id, []).append(mt)

        for trade_id, mts in ref_seen.items():
            if len(mts) > 1:
                trade = mts[0].reference
                msg = (
                    f"Reference trade {trade.symbol} {trade.direction} "
                    f"at {trade.entry_time} (id={trade_id}) is matched {len(mts)} times"
                )
                anomalies.append(
                    TradeAnomaly(
                        anomaly_type=AnomalyType.DUPLICATE_MATCH,
                        expected_trade=trade,
                        matched_trade=mts[0],
                        severity="error",
                        message=msg,
                    )
                )

        for trade_id, mts in act_seen.items():
            if len(mts) > 1:
                trade = mts[0].actual
                msg = (
                    f"Platform trade {trade.symbol} {trade.direction} "
                    f"at {trade.entry_time} (id={trade_id}) is matched {len(mts)} times"
                )
                anomalies.append(
                    TradeAnomaly(
                        anomaly_type=AnomalyType.DUPLICATE_MATCH,
                        actual_trade=trade,
                        matched_trade=mts[0],
                        severity="error",
                        message=msg,
                    )
                )

        return anomalies

    @staticmethod
    def _severity_for_mismatches(
        field_mismatches: list[FieldComparisonResult],
    ) -> Literal["info", "warning", "error"]:
        """Map field mismatches to an overall anomaly severity."""
        error_fields = {"direction", "pnl"}
        for fm in field_mismatches:
            if fm.field in error_fields:
                return "error"
        return "warning"

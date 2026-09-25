"""Trade comparison engine for strategy validation.

Compares matched trades from reference and actual backtests with configurable
tolerances for prices, times, volumes, PnL, stop-loss and take-profit levels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared.models import Trade
from validation.comparison.matcher import MatchKey
from validation.comparison.tolerances import finite_tolerance, relative_difference


def _normalize_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp and return a timezone-aware UTC datetime."""
    normalized = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_MATCH_SUFFIX = " (match)"
_MISMATCH_SUFFIX = " (mismatch)"


def _pct_diff(a: float, b: float) -> float:
    """Return relative drift using the expected value as the denominator."""
    return relative_difference(a, b)


def _abs_diff(a: float, b: float) -> float:
    return abs(a - b)


def _time_diff_seconds(a: str, b: str) -> float:
    """Absolute difference between two ISO-8601 timestamps in seconds."""
    return abs((_normalize_timestamp(a) - _normalize_timestamp(b)).total_seconds())


@dataclass
class TradeComparisonConfig:
    """Tolerances used by ``TradeComparator``."""

    entry_time_seconds: float = 60.0
    exit_time_seconds: float = 60.0
    entry_price_pct: float = 0.0001
    exit_price_pct: float = 0.0001
    volume_pct: float = 0.01
    position_size_pct: float = 0.01
    pnl_pct: float = 0.001
    pnl_abs: float = 0.01
    sl_price_pct: float = 0.0001
    tp_price_pct: float = 0.0001

    def __post_init__(self) -> None:
        for name in (
            "entry_time_seconds",
            "exit_time_seconds",
            "entry_price_pct",
            "exit_price_pct",
            "volume_pct",
            "position_size_pct",
            "pnl_pct",
            "pnl_abs",
            "sl_price_pct",
            "tp_price_pct",
        ):
            value = finite_tolerance(getattr(self, name), name)
            setattr(self, name, value)


@dataclass
class MatchedTrade:
    """A reference trade paired with the corresponding actual trade."""

    reference: Trade
    actual: Trade
    key: MatchKey


@dataclass
class FieldComparisonResult:
    """Result of comparing a single field between reference and actual trade."""

    field: str
    expected: Any
    actual: Any
    tolerance: Any
    passed: bool
    message: str


@dataclass
class TradeComparisonResult:
    """Complete comparison result for a matched pair of trades."""

    matched_trade: MatchedTrade
    field_results: dict[str, FieldComparisonResult] = field(default_factory=dict)
    is_match: bool = False
    mismatches: list[str] = field(default_factory=list)


class TradeComparator:
    """Compare a matched reference/actual trade pair against tolerances.

    The comparator extracts canonical fields from both sides and produces a
    ``TradeComparisonResult`` that contains per-field pass/fail information
    and an overall ``is_match`` flag.
    """

    def __init__(self, config: TradeComparisonConfig | None = None) -> None:
        self.config = config if config is not None else TradeComparisonConfig()

    def compare(self, matched_trade: MatchedTrade) -> TradeComparisonResult:
        """Compare the fields of *matched_trade* and return the result."""
        ref = matched_trade.reference
        act = matched_trade.actual

        field_results: dict[str, FieldComparisonResult] = {}

        # direction
        field_results["direction"] = self._compare_direction(ref.direction, act.direction)

        # times
        field_results["entry_time"] = self._compare_time(
            ref.entry_time, act.entry_time, self.config.entry_time_seconds, "entry_time"
        )
        field_results["exit_time"] = self._compare_time(
            ref.last_exit_time, act.last_exit_time, self.config.exit_time_seconds, "exit_time"
        )

        # prices
        field_results["entry_price"] = self._compare_pct(
            ref.entry_price,
            act.entry_price,
            self.config.entry_price_pct,
            "entry_price",
        )

        exit_price_ref = ref.exits[-1].price if ref.exits else None
        exit_price_act = act.exits[-1].price if act.exits else None
        field_results["exit_price"] = self._compare_optional_pct(
            exit_price_ref, exit_price_act, self.config.exit_price_pct, "exit_price"
        )

        # volume / quantity
        field_results["volume"] = self._compare_pct(
            ref.quantity, act.quantity, self.config.volume_pct, "volume"
        )

        # position_size
        field_results["position_size"] = self._compare_optional_pct(
            ref.position_size,
            act.position_size,
            self.config.position_size_pct,
            "position_size",
        )

        # pnl
        field_results["pnl"] = self._compare_pnl(
            ref.pnl, act.pnl, self.config.pnl_pct, self.config.pnl_abs
        )

        # stop_loss / take_profit (#85): resolve from Position-level fields
        # first, with meta.* only as fallback. Native trades carry their stops
        # in Position.* (the engine path), so a meta-only read saw None on BOTH
        # sides and certified "both None (match)" without ever comparing a stop.
        # None vs a real stop is a mismatch, never a match.
        sl_ref = self._resolve_stop_level(ref, "stop_loss", ("stop_loss", "sl_price", "stop_price"))
        sl_act = self._resolve_stop_level(act, "stop_loss", ("stop_loss", "sl_price", "stop_price"))
        field_results["stop_loss"] = self._compare_optional_pct(
            sl_ref, sl_act, self.config.sl_price_pct, "stop_loss"
        )

        # take_profit
        tp_ref = self._resolve_stop_level(ref, "take_profit", ("take_profit", "tp_price"))
        tp_act = self._resolve_stop_level(act, "take_profit", ("take_profit", "tp_price"))
        field_results["take_profit"] = self._compare_optional_pct(
            tp_ref, tp_act, self.config.tp_price_pct, "take_profit"
        )

        mismatches = [name for name, res in field_results.items() if not res.passed]
        is_match = len(mismatches) == 0

        return TradeComparisonResult(
            matched_trade=matched_trade,
            field_results=field_results,
            is_match=is_match,
            mismatches=mismatches,
        )

    def _compare_direction(self, expected: str, actual: str) -> FieldComparisonResult:
        passed = expected == actual
        return FieldComparisonResult(
            field="direction",
            expected=expected,
            actual=actual,
            tolerance=None,
            passed=passed,
            message=f"Direction: expected {expected!r}, got {actual!r}"
            + (_MATCH_SUFFIX if passed else _MISMATCH_SUFFIX),
        )

    def _compare_time(
        self, expected: str, actual: str, tolerance: float, field_name: str
    ) -> FieldComparisonResult:
        diff = _time_diff_seconds(expected, actual)
        passed = diff <= tolerance
        return FieldComparisonResult(
            field=field_name,
            expected=expected,
            actual=actual,
            tolerance=tolerance,
            passed=passed,
            message=f"{field_name} difference is {diff:.3f}s (tolerance {tolerance}s)"
            + (_MATCH_SUFFIX if passed else _MISMATCH_SUFFIX),
        )

    def _compare_pct(
        self, expected: float, actual: float, tolerance: float, field_name: str
    ) -> FieldComparisonResult:
        diff = _pct_diff(expected, actual)
        passed = diff <= tolerance
        return FieldComparisonResult(
            field=field_name,
            expected=expected,
            actual=actual,
            tolerance=tolerance,
            passed=passed,
            message=f"{field_name} relative difference is {diff:.6f} (tolerance {tolerance:.6f})"
            + (_MATCH_SUFFIX if passed else _MISMATCH_SUFFIX),
        )

    def _resolve_stop_level(self, trade: Trade, attr: str, keys: tuple[str, ...]) -> float | None:
        """Resolve a stop level from canonical fields and legacy metadata."""
        def _finite(value: Any) -> float | None:
            if value is None:
                return None
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            return numeric if math.isfinite(numeric) else None

        direct = _finite(getattr(trade, attr, None))
        if direct is not None:
            return direct

        metadata = trade.meta or {}
        sources: list[dict[str, Any]] = [metadata]
        for nested_key in ("risk_math", "risk", "execution"):
            nested = metadata.get(nested_key)
            if isinstance(nested, dict):
                sources.append(nested)

        legacy_keys = tuple(keys) + (
            "final_stop_loss",
            "final_take_profit",
            "signal_stop_loss",
            "signal_take_profit",
            "initial_stop_loss",
            "initial_take_profit",
        )
        for source in sources:
            for key in legacy_keys:
                value = _finite(source.get(key))
                if value is not None:
                    return value
        return None

    def _compare_optional_pct(
        self,
        expected: float | None,
        actual: float | None,
        tolerance: float,
        field_name: str,
    ) -> FieldComparisonResult:
        if expected is None and actual is None:
            return FieldComparisonResult(
                field=field_name,
                expected=expected,
                actual=actual,
                tolerance=tolerance,
                passed=True,
                message=f"{field_name}: both None (match)",
            )
        if expected is None or actual is None:
            return FieldComparisonResult(
                field=field_name,
                expected=expected,
                actual=actual,
                tolerance=tolerance,
                passed=False,
                message=f"{field_name}: expected {expected!r}, got {actual!r} (mismatch)",
            )
        return self._compare_pct(expected, actual, tolerance, field_name)

    def _compare_pnl(
        self,
        expected: float,
        actual: float,
        tolerance_pct: float,
        tolerance_abs: float,
    ) -> FieldComparisonResult:
        diff_pct = _pct_diff(expected, actual)
        diff_abs = _abs_diff(expected, actual)
        # Both bands must hold (DV-B35): a relative diff of 1.0 (one side
        # exactly zero) is never rescued by the absolute band and vice versa.
        passed_pct = diff_pct <= tolerance_pct
        passed_abs = diff_abs <= tolerance_abs
        passed = passed_pct and passed_abs
        return FieldComparisonResult(
            field="pnl",
            expected=expected,
            actual=actual,
            tolerance=(tolerance_pct, tolerance_abs),
            passed=passed,
            message=f"PnL relative diff {diff_pct:.6f} (tol {tolerance_pct:.6f}), "
            f"absolute diff {diff_abs:.6f} (tol {tolerance_abs:.6f})"
            + (_MATCH_SUFFIX if passed else _MISMATCH_SUFFIX),
        )

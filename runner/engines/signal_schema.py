"""Canonical signal schema utilities for engine and viewer translation.

This module centralizes normalization of heterogeneous strategy signal outputs
into canonical v2 execution and drawing schemas while preserving backward
compatibility with existing strategies.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from shared.models import (
    DrawingSchema,
    ExitLeg,
    SignalExecutionSchema,
    SignalOrder,
    VALID_DIRECTIONS,
)


CANONICAL_SIGNAL_SCHEMA_VERSION = "2.0"


def _normalize_direction(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in VALID_DIRECTIONS:
        return raw

    # Keep buy/sell compatibility while canonicalizing to long/short.
    if raw in {"BUY", "BULL", "BULLISH", "1"}:
        return "LONG"
    if raw in {"SELL", "BEAR", "BEARISH", "-1"}:
        return "SHORT"
    return "LONG"


def _extract_direction_series(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
) -> pd.Series | None:
    if "direction" in signals.columns:
        return signals["direction"]
    for candidate in ("direction", "side", "position_side"):
        series = _indicator_series(indicators, candidate)
        if series is not None:
            return series
    return None


def _extract_order_type_series(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
) -> pd.Series | None:
    if "order_type" in signals.columns:
        return signals["order_type"]
    for candidate in ("order_type", "entry_order_type", "order_kind"):
        series = _indicator_series(indicators, candidate)
        if series is not None:
            return series
    return None


def _extract_size_series(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
) -> tuple[str, pd.Series | None]:
    # Canonical precedence.
    if "position_size_pct" in signals.columns:
        return "PCT_POSITION", signals["position_size_pct"]
    if "position_qty" in signals.columns:
        return "ABS_QTY", signals["position_qty"]
    if "position_notional" in signals.columns:
        return "NOTIONAL", signals["position_notional"]
    if "risk_pct" in signals.columns:
        return "RISK_PCT", signals["risk_pct"]

    aliases = (
        ("position_size_pct", "PCT_POSITION"),
        ("size_pct", "PCT_POSITION"),
        ("position_qty", "ABS_QTY"),
        ("qty", "ABS_QTY"),
        ("position_notional", "NOTIONAL"),
        ("risk_pct", "RISK_PCT"),
    )
    for alias, size_model in aliases:
        series = _indicator_series(indicators, alias)
        if series is not None:
            return size_model, series
    return "PCT_POSITION", None


def _extract_price_series(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
    *,
    kind: str,
) -> pd.Series | None:
    mapping = {
        "limit": ("limit_price", "entry_limit", "limit"),
        "stop": ("stop_price", "entry_stop", "stop"),
    }
    candidates = mapping[kind]

    for candidate in candidates:
        if candidate in signals.columns:
            return signals[candidate]
    for candidate in candidates:
        series = _indicator_series(indicators, candidate)
        if series is not None:
            return series
    return None


def _extract_exit_series(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
    candidates: tuple[str, ...],
) -> pd.Series | None:
    for candidate in candidates:
        if candidate in signals.columns:
            return signals[candidate]
    for candidate in candidates:
        series = _indicator_series(indicators, candidate)
        if series is not None:
            return series
    return None


def _safe_quantity_pct(raw_value: Any, *, fallback: float | None) -> float | None:
    numeric = _numeric_or_none(raw_value)
    if numeric is None:
        return fallback
    if numeric > 1:
        numeric = numeric / 100.0
    if numeric <= 0:
        return fallback
    return min(numeric, 1.0)


def _extract_tp_legs(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
    *,
    entry_idx: int,
) -> list[ExitLeg]:
    legs: list[ExitLeg] = []

    # Prefer explicit multi-target TP levels if present.
    for level in range(1, 5):
        price_series = _extract_exit_series(
            signals,
            indicators,
            (f"tp{level}_price", f"take_profit_{level}", f"tp{level}"),
        )
        if price_series is None:
            continue

        price = _numeric_or_none(price_series.iloc[entry_idx])
        if price is None or price <= 0:
            continue

        qty_series = _extract_exit_series(
            signals,
            indicators,
            (f"tp{level}_qty_pct", f"tp{level}_size_pct", f"tp{level}_qty"),
        )
        qty_pct = _safe_quantity_pct(
            qty_series.iloc[entry_idx] if qty_series is not None else None,
            fallback=None,
        )

        legs.append(
            ExitLeg(
                kind="TP",
                quantity_pct=qty_pct,
                price=price,
                priority=10 + level,
                label=f"tp{level}",
            )
        )

    if legs:
        return legs

    single_tp_series = _extract_exit_series(
        signals,
        indicators,
        ("take_profit", "tp_price", "tp"),
    )
    if single_tp_series is None:
        return []

    single_tp = _numeric_or_none(single_tp_series.iloc[entry_idx])
    if single_tp is None or single_tp <= 0:
        return []

    return [
        ExitLeg(
            kind="TP",
            quantity_pct=1.0,
            price=single_tp,
            priority=10,
            label="tp",
        )
    ]


def _extract_sl_leg(
    signals: pd.DataFrame,
    indicators: dict[str, Any],
    *,
    entry_idx: int,
) -> ExitLeg | None:
    sl_series = _extract_exit_series(
        signals,
        indicators,
        ("stop_loss", "sl_price", "stop"),
    )
    if sl_series is None:
        return None

    sl_price = _numeric_or_none(sl_series.iloc[entry_idx])
    if sl_price is None or sl_price <= 0:
        return None

    return ExitLeg(
        kind="SL",
        quantity_pct=1.0,
        price=sl_price,
        priority=5,
        label="sl",
    )


def _indicator_series(indicators: dict[str, Any], key: str) -> pd.Series | None:
    row = indicators.get(key)
    if row is None:
        return None
    if isinstance(row, pd.Series):
        return row
    if isinstance(row, tuple) and row:
        maybe_series = row[0]
        if isinstance(maybe_series, pd.Series):
            return maybe_series
    return None


def _numeric_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _infer_order_type(
    order_type_value: Any,
    *,
    limit_price: float | None,
    stop_price: float | None,
) -> str:
    raw = str(order_type_value or "").strip().upper().replace("-", "_")
    if raw in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
        return raw
    if raw == "STOPLIMIT":
        return "STOP_LIMIT"

    # Fallback inference from price fields.
    if stop_price is not None and limit_price is not None:
        return "STOP_LIMIT"
    if stop_price is not None:
        return "STOP"
    if limit_price is not None:
        return "LIMIT"
    return "MARKET"


def _safe_size_value(
    raw_size: Any,
    *,
    size_model: str,
    fallback: float,
) -> float:
    numeric = _numeric_or_none(raw_size)
    if numeric is None:
        return fallback

    if size_model in {"PCT_POSITION", "RISK_PCT"}:
        if numeric > 1:
            numeric = numeric / 100.0
        if numeric <= 0:
            return fallback
        return min(numeric, 1.0)

    if numeric <= 0:
        return fallback
    return numeric


def build_execution_schema_for_entry(
    *,
    entry_idx: int,
    signals: pd.DataFrame,
    indicators: dict[str, Any],
    default_direction: str = "LONG",
    default_size_value: float = 1.0,
) -> SignalExecutionSchema:
    """Build canonical execution schema for one entry bar.

    This is intentionally conservative and backward-compatible: when signals
    don't provide advanced order fields, a MARKET open with full position size
    is emitted.
    """

    direction_series = _extract_direction_series(signals, indicators)
    order_type_series = _extract_order_type_series(signals, indicators)
    size_model, size_series = _extract_size_series(signals, indicators)
    limit_series = _extract_price_series(signals, indicators, kind="limit")
    stop_series = _extract_price_series(signals, indicators, kind="stop")

    direction_raw = (
        direction_series.iloc[entry_idx] if direction_series is not None else default_direction
    )
    direction = _normalize_direction(direction_raw)

    limit_price = _numeric_or_none(limit_series.iloc[entry_idx] if limit_series is not None else None)
    stop_price = _numeric_or_none(stop_series.iloc[entry_idx] if stop_series is not None else None)

    order_type_raw = (
        order_type_series.iloc[entry_idx] if order_type_series is not None else None
    )
    order_type = _infer_order_type(
        order_type_raw,
        limit_price=limit_price,
        stop_price=stop_price,
    )

    raw_size = size_series.iloc[entry_idx] if size_series is not None else None
    size_value = _safe_size_value(raw_size, size_model=size_model, fallback=default_size_value)

    entry_order = SignalOrder(
        action="OPEN",
        direction=direction,
        order_type=order_type,
        size_model=size_model,
        size_value=size_value,
        limit_price=limit_price,
        stop_price=stop_price,
        label=f"{direction}_{order_type}",
    )

    tp_legs = _extract_tp_legs(signals, indicators, entry_idx=entry_idx)
    sl_leg = _extract_sl_leg(signals, indicators, entry_idx=entry_idx)

    return SignalExecutionSchema(
        schema_version=CANONICAL_SIGNAL_SCHEMA_VERSION,
        direction=direction,
        entry=entry_order,
        tp_legs=tp_legs,
        sl_leg=sl_leg,
        meta={
            "derived": True,
            "entry_idx": int(entry_idx),
        },
    )


def normalize_drawing_schema(
    payload: dict[str, Any] | DrawingSchema | None,
) -> DrawingSchema:
    """Normalize drawing payload into canonical schema type."""
    if payload is None:
        return DrawingSchema(schema_version=CANONICAL_SIGNAL_SCHEMA_VERSION)
    if isinstance(payload, DrawingSchema):
        return payload
    if isinstance(payload, dict):
        return DrawingSchema.from_dict(payload)
    raise TypeError("Drawing schema payload must be dict, DrawingSchema, or None.")

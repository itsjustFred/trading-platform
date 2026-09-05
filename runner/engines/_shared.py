"""Shared serialization helpers for engine adapters."""

from __future__ import annotations

from typing import Any

import pandas as pd


def serialize_meta_value(value: Any) -> Any | None:
    """Serialize a single indicator/meta value for JSON-safe storage.

    Returns ``None`` for values that carry no information (None, NaN) so
    callers can drop the key entirely. Numeric floats are rounded to 6
    decimals; numpy scalars are unwrapped via ``item()``.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)

    if isinstance(value, (int, str, bool)):
        return value

    if pd.isna(value):
        return None

    return str(value)


def resolve_max_position_pct(explicit: float | None, asset_class: str) -> float:
    """Cap on notional as a fraction of equity; asset-class default 20% (100% equities)."""
    if explicit is not None and explicit > 0:
        return float(explicit)
    if asset_class in {"equity", "equities", "etf", "etfs", "stocks"}:
        return 1.0
    return 0.20


def resolve_max_leverage(explicit: float | None) -> float:
    """Leverage ceiling applied to risk-sized notional; default 2.0x."""
    if explicit is not None and explicit > 0:
        return float(explicit)
    return 2.0

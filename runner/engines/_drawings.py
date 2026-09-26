"""Drawing normalization shared by engine adapters.

Strategies may return a list of drawing dicts (marker/line); adapters
normalize them into the viewer's drawing contract with UTC ISO timestamps.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from runner.engines._shared import serialize_meta_value as _serialize_meta_value

DrawingList = list[dict[str, Any]]


def _normalize_drawings(drawings: Any, *, symbol: str) -> DrawingList:
    if drawings is None:
        return []
    if not isinstance(drawings, list):
        raise TypeError("drawings must be a list of drawing dictionaries")

    normalized: DrawingList = []
    for idx, raw_drawing in enumerate(drawings, start=1):
        if not isinstance(raw_drawing, dict):
            raise TypeError(f"drawing #{idx} must be a dict")

        drawing_type = str(raw_drawing.get("type", "")).strip()
        if drawing_type not in {"marker", "line"}:
            raise ValueError(
                f"drawing #{idx} has unsupported type {drawing_type!r}; use 'marker' or 'line'"
            )

        normalized_drawing: dict[str, Any] = {}
        for key, value in raw_drawing.items():
            serialized = _serialize_meta_value(value)
            if serialized is not None:
                normalized_drawing[str(key)] = serialized

        normalized_drawing["type"] = drawing_type
        normalized_drawing.setdefault("symbol", symbol)

        if drawing_type == "marker":
            normalized_drawing["time"] = _normalize_drawing_time(
                raw_drawing.get("time"), field_name="time", drawing_index=idx
            )
        else:
            normalized_drawing["start_time"] = _normalize_drawing_time(
                raw_drawing.get("start_time"), field_name="start_time", drawing_index=idx
            )
            normalized_drawing["end_time"] = _normalize_drawing_time(
                raw_drawing.get("end_time"), field_name="end_time", drawing_index=idx
            )
            if "val1" not in raw_drawing or "val2" not in raw_drawing:
                raise ValueError(f"drawing #{idx} line drawings require val1 and val2")
            try:
                normalized_drawing["val1"] = float(raw_drawing["val1"])
                normalized_drawing["val2"] = float(raw_drawing["val2"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"drawing #{idx} line values must be numeric") from exc

        normalized.append(normalized_drawing)

    return normalized


def _normalize_drawing_time(value: Any, *, field_name: str, drawing_index: int) -> str:
    if value is None:
        raise ValueError(f"drawing #{drawing_index} missing required field {field_name!r}")

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, pd.Timestamp):
        timestamp = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
        return timestamp.isoformat()

    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"drawing #{drawing_index} field {field_name!r} must be finite")
        magnitude = abs(numeric)
        if magnitude >= 1e17:
            unit = "ns"
        elif magnitude >= 1e14:
            unit = "us"
        elif magnitude >= 1e11:
            unit = "ms"
        else:
            unit = "s"
        return pd.Timestamp(numeric, unit=unit, tz="UTC").isoformat()

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"drawing #{drawing_index} field {field_name!r} must not be empty")
        try:
            numeric = float(raw)
        except ValueError:
            numeric = None
        if numeric is not None and math.isfinite(numeric):
            return _normalize_drawing_time(
                numeric,
                field_name=field_name,
                drawing_index=drawing_index,
            )
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(
                f"drawing #{drawing_index} field {field_name!r} is not a valid timestamp"
            )
        return pd.Timestamp(parsed).isoformat()

    raise TypeError(f"drawing #{drawing_index} field {field_name!r} must be a timestamp-like value")

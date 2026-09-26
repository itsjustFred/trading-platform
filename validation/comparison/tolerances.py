"""Finite numeric and tolerance helpers for validation comparisons."""

from __future__ import annotations

import math
from typing import Any


def finite_float(value: Any, name: str) -> float:
    """Return *value* as a finite float or raise ``ValueError``."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def finite_tolerance(value: Any, name: str, *, optional: bool = False) -> float | None:
    """Validate a finite, non-negative tolerance."""
    if value is None and optional:
        return None
    number = finite_float(value, name)
    if number < 0.0:
        raise ValueError(f"{name} must be >= 0")
    return number


def relative_difference(expected: float, actual: float) -> float:
    """Return relative drift using the expected value as the denominator."""
    expected_float = finite_float(expected, "expected")
    actual_float = finite_float(actual, "actual")
    if expected_float == 0.0:
        return 0.0 if actual_float == 0.0 else 1.0
    return abs(expected_float - actual_float) / abs(expected_float)

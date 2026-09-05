"""Shared timestamp formatting for the native engine.

Single source of truth for the ISO-UTC encoding used by trade timestamps,
equity-curve points, and indicator-series points. The viewer compares these
strings, so they must come from one formatter (pre-mortem PM-3).
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def to_iso_utc(value: Any) -> str:
    """Format a timestamp-like value as a UTC ISO-8601 string."""
    ts = pd.Timestamp(value, tz="UTC") if not isinstance(value, pd.Timestamp) else value
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.isoformat()

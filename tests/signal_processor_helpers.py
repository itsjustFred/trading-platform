"""Shared fixtures/data helpers for the signal-processor test suites."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sample_signal_market_data(rows: int = 80) -> "pd.DataFrame":
    """Deterministic OHLCV frame shared by the signal-processor test suites."""
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    base = np.linspace(90.0, 110.0, rows)
    close = base + np.sin(np.linspace(0.0, 4.0, rows))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(rows, 10_000.0),
        }
    )

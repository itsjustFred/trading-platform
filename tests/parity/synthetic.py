"""Seeded synthetic intraday sessions for the parity tests."""

from __future__ import annotations

import random

import pandas as pd


def synthetic_sessions(seed: int, days: int = 24) -> pd.DataFrame:
    """78-bar sessions with a volatile open and occasional gaps at the second candle."""
    rng = random.Random(seed)
    rows = []
    stamps = []
    price = 1000.0
    session_start = pd.Timestamp("2025-01-06 09:30", tz="America/New_York")
    for d in range(days):
        day = session_start + pd.Timedelta(days=d)
        if day.dayofweek >= 5:
            continue
        price *= 1 + rng.gauss(0, 0.004)
        sigma = 0.0012
        for k in range(78):
            open_ = price
            if k == 1 and rng.random() < 0.35:
                open_ *= 1 + rng.choice([-1, 1]) * rng.uniform(0.003, 0.008)
            elif k > 1 and rng.random() < 0.03:
                open_ *= 1 + rng.choice([-1, 1]) * rng.uniform(0.002, 0.006)
            move = rng.gauss(0, sigma * (3 if k < 3 else 1))
            close = open_ * (1 + move)
            span = abs(rng.gauss(0, sigma)) * open_
            high = max(open_, close) + span * rng.random()
            low = min(open_, close) - span * rng.random()
            rows.append((round(open_, 3), round(high, 3), round(low, 3), round(close, 3)))
            stamps.append((day + pd.Timedelta(minutes=5 * k)).tz_convert("UTC"))
            price = close
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": 100.0,
        }
    )

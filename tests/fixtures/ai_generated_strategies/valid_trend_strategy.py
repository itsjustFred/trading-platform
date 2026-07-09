from __future__ import annotations

import pandas as pd


def get_name() -> str:
    return "Valid Trend Strategy"


def get_parameters() -> dict[str, int]:
    return {"fast": 10, "slow": 30}


def generate_signals(df: pd.DataFrame, fast: int = 10, slow: int = 30) -> tuple[pd.Series, pd.Series]:
    close = df["close"].astype(float)
    fast_ma = close.rolling(window=fast, min_periods=fast).mean()
    slow_ma = close.rolling(window=slow, min_periods=slow).mean()
    entries = (fast_ma > slow_ma).fillna(False)
    exits = (fast_ma < slow_ma).fillna(False)
    return entries, exits

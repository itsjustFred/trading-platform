from __future__ import annotations

import pandas as pd


def get_name() -> str:
    return "Intentionally Broken Strategy"


def get_parameters() -> dict[str, float]:
    return {"threshold": 0.0}


def generate_signals(df: pd.DataFrame, threshold: float = 0.0) -> tuple[pd.Series, pd.Series]:
    close = df["close"].astype(float)
    momentum = close.diff().fillna(0.0)
    entries = (momentum > threshold) & (momentum.shift(1).fillna(0.0) <= threshold)
    exits = (momentum < -threshold) & (momentum.shift(1).fillna(0.0) >= -threshold)
    return entries.fillna(False), exits.fillna(False)

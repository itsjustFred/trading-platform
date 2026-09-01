"""Negative control: dangerous code the validator must reject (Cleanup 17-B)."""

from __future__ import annotations

import pandas as pd


def get_name() -> str:
    return "Uses Eval"


def get_parameters() -> dict[str, int]:
    return {"fast": 10}


def generate_signals(df: pd.DataFrame, fast: int = 10) -> tuple[pd.Series, pd.Series]:
    close = df["close"].astype(float)
    momentum = close.diff().fillna(0.0)
    entries = momentum > eval("0.0")  # noqa: S307
    exits = momentum < 0.0
    return entries, exits

"""
Example Strategy: SMA Crossover

Simple moving average crossover strategy for testing the runner pipeline.
Long when fast SMA crosses above slow SMA, exit when it crosses below.
"""

STRATEGY_META = {
    "name": "SMA Crossover",
    "version": "1.0.0",
    "parameters": {"fast": 10, "slow": 30},
    "tags": {
        "type": ["trend-following"],
        "source": "example",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast_period = params.get("fast", 10)
    slow_period = params.get("slow", 30)

    close = df["close"]
    fast_sma = close.rolling(fast_period).mean()
    slow_sma = close.rolling(slow_period).mean()

    prev_fast = fast_sma.shift(1)
    prev_slow = slow_sma.shift(1)

    entries = ((prev_fast <= prev_slow) & (fast_sma > slow_sma)).fillna(False)
    exits = ((prev_fast >= prev_slow) & (fast_sma < slow_sma)).fillna(False)

    indicators_dict = {
        "SMA_Fast": fast_sma,
        "SMA_Slow": slow_sma,
        "Signal_Strength": pd.Series("High", index=df.index),
    }
    return entries, exits, indicators_dict

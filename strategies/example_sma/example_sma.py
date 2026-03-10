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

    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_indices = entries[entries].index.tolist()
        exit_indices = exits[exits].index.tolist()

        if entry_indices:
            first_entry_idx = entry_indices[0]
            entry_time = pd.Timestamp(df.loc[first_entry_idx, "timestamp"]).isoformat()
            drawings.append(
                {
                    "type": "marker",
                    "time": entry_time,
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#58a6ff",
                    "text": "SMA Cross",
                }
            )

            matching_exits = [idx for idx in exit_indices if idx >= first_entry_idx]
            if matching_exits:
                first_exit_idx = matching_exits[0]
                exit_time = pd.Timestamp(df.loc[first_exit_idx, "timestamp"]).isoformat()
                drawings.append(
                    {
                        "type": "marker",
                        "time": exit_time,
                        "position": "aboveBar",
                        "shape": "circle",
                        "color": "#f59e0b",
                        "text": "SMA Exit",
                    }
                )
                drawings.append(
                    {
                        "type": "line",
                        "start_time": entry_time,
                        "end_time": exit_time,
                        "val1": float(fast_sma.iloc[first_entry_idx]),
                        "val2": float(fast_sma.iloc[first_exit_idx]),
                        "color": "#58a6ff",
                        "title": "Fast SMA",
                    }
                )

    return entries, exits, indicators_dict, drawings

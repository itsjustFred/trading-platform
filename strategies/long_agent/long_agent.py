"""Generated strategy."""

STRATEGY_META = {'name': 'Demo Agent',
 'version': '1.0.0',
 'parameters': {'fast': 10, 'slow': 30},
 'tags': {'type': ['momentum'],
          'source': 'test',
          'asset_class': ['equities'],
          'complexity': 'simple'}}

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast_period = params.get("fast", 10)
    slow_period = params.get("slow", 30)
    fast = df["close"].rolling(fast_period).mean()
    slow = df["close"].rolling(slow_period).mean()
    entries = ((fast > slow) & (fast.shift(1) <= slow.shift(1))).fillna(False)
    exits = ((fast < slow) & (fast.shift(1) >= slow.shift(1))).fillna(False)
    indicators_dict = {"fast_sma": fast, "slow_sma": slow}
    drawings: list[dict] = []
    return entries, exits, indicators_dict, drawings

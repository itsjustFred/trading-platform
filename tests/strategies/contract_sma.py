"""Deterministic SMA-crossover strategy used by the engine conformance tests.

Same shape as ``strategies/example_sma`` but with SMA semantics so the
bundled trend/consolidation fixture produces several crossover round-trips.
"""

STRATEGY_META = {
    "name": "contract_sma",
    "version": "1.0",
    "parameters": {"fast_length": 5, "slow_length": 20},
    "tags": {"type": ["sma", "crossover", "trend", "moving_average"]},
}

import pandas as pd


def generate_signals(df, params):
    fast_length = params.get("fast_length", 5)
    slow_length = params.get("slow_length", 20)

    close = df["close"]

    fast = close.rolling(fast_length).mean()
    slow = close.rolling(slow_length).mean()

    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    indicators_dict = {"fast": fast, "slow": slow}
    drawings = []

    return entries, exits, indicators_dict, drawings

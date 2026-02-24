"""
Example Strategy: SMA Crossover

Simple moving average crossover strategy for testing the runner pipeline.
Long when fast SMA crosses above slow SMA, exit when it crosses below.

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/example_sma.py \
        --symbols AAPL --timeframe 1h --start 2024-01-01 --end 2025-12-31
"""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    Generate entry/exit signals based on SMA crossover.

    Args:
        df: OHLCV DataFrame with columns: timestamp, open, high, low, close, volume
        params: Strategy parameters
            - fast: Fast SMA period (default: 10)
            - slow: Slow SMA period (default: 30)

    Returns:
        DataFrame with 'entries' and 'exits' boolean columns
    """
    fast_period = params.get("fast", 10)
    slow_period = params.get("slow", 30)

    close = df["close"]
    fast_sma = close.rolling(fast_period).mean()
    slow_sma = close.rolling(slow_period).mean()

    # Crossover signals
    prev_fast = fast_sma.shift(1)
    prev_slow = slow_sma.shift(1)

    entries = (prev_fast <= prev_slow) & (fast_sma > slow_sma)
    exits = (prev_fast >= prev_slow) & (fast_sma < slow_sma)

    return pd.DataFrame({
        "entries": entries.fillna(False),
        "exits": exits.fillna(False),
    })

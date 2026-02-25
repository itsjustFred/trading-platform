"""
Strategy: Trend Following (Multi-Timeframe Momentum)

CTA-style trend following using multiple SMA timeframes for trend confirmation
and ATR-based position sizing / trailing stops.

Original: desenyon/trend_following_cta.py (QuantConnect)
Adapted: vectorbt signal format for individual equities

Logic:
    - Fast trend: SMA(20) vs SMA(50) — short-term momentum
    - Slow trend: SMA(50) vs SMA(200) — long-term confirmation
    - Entry LONG: fast SMA cross up AND slow SMA confirms (50 > 200)
    - Exit: fast SMA crosses below slow SMA OR ATR trailing stop hit
    - ATR trailing stop: entry_price - atr_mult × ATR(14)

Parameters:
    - fast_sma: 20
    - mid_sma: 50
    - slow_sma: 200
    - atr_period: 14
    - atr_stop_mult: 2.0 (trailing stop distance in ATRs)

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/trend_following.py \\
        --symbols AAPL NVDA AMZN --timeframe 1d --start 2021-01-01 --end 2025-12-31
"""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    fast = params.get("fast_sma", 20)
    mid = params.get("mid_sma", 50)
    slow = params.get("slow_sma", 200)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # SMAs
    sma_fast = close.rolling(fast).mean()
    sma_mid = close.rolling(mid).mean()
    sma_slow = close.rolling(slow).mean()

    # ATR for trailing stop
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    # Fast crossover: fast SMA crosses above mid SMA
    fast_cross_up = (sma_fast > sma_mid) & (sma_fast.shift(1) <= sma_mid.shift(1))

    # Trend confirmation: mid SMA above slow SMA
    trend_up = sma_mid > sma_slow

    # Fast cross down
    fast_cross_down = (sma_fast < sma_mid) & (sma_fast.shift(1) >= sma_mid.shift(1))

    # State machine with ATR trailing stop
    in_trade = False
    entry_price = 0.0
    trailing_stop = 0.0
    entries = []
    exits = []

    for i in range(len(df)):
        if pd.isna(sma_slow.iloc[i]):
            entries.append(False)
            exits.append(False)
            continue

        if not in_trade:
            if fast_cross_up.iloc[i] and trend_up.iloc[i]:
                in_trade = True
                entry_price = float(close.iloc[i])
                trailing_stop = entry_price - atr_stop_mult * float(atr.iloc[i])
                entries.append(True)
                exits.append(False)
            else:
                entries.append(False)
                exits.append(False)
        else:
            # Update trailing stop (only moves up)
            new_stop = float(close.iloc[i]) - atr_stop_mult * float(atr.iloc[i])
            trailing_stop = max(trailing_stop, new_stop)

            # Exit conditions: SMA cross down OR trailing stop hit
            if fast_cross_down.iloc[i] or float(close.iloc[i]) < trailing_stop:
                in_trade = False
                entries.append(False)
                exits.append(True)
            else:
                entries.append(False)
                exits.append(False)

    return pd.DataFrame({
        "entries": entries,
        "exits": exits,
    })

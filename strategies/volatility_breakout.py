"""
Strategy: Intraday Volatility Breakout

Simple breakout strategy: if price moves beyond the daily open + ATR multiplier,
enter in that direction. Stop at the open price.

Original: discord-zenomtrader-forum-strategies/Simple Intraday Volatility Breakout (NinjaTrader 8 C#)
Adapted: vectorbt signal format

Logic:
    - Calculate ATR(period) on daily-equivalent scale
    - Long entry: close > session_open + (atr_mult × ATR)
    - Short entry: close < session_open - (atr_mult × ATR)  [not used in long-only version]
    - Exit: close crosses back below session_open (stop) OR opposite signal

Parameters:
    - atr_period: 5 (ATR lookback)
    - atr_mult: 0.3 (breakout multiplier — conservative)
    - use_atr_stop: True (stop at open - 0.5*ATR instead of just open)

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/volatility_breakout.py \\
        --symbols AAPL MSFT --timeframe 1h --start 2023-06-01 --end 2025-12-31
"""

import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    atr_period = params.get("atr_period", 5)
    atr_mult = params.get("atr_mult", 0.3)

    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(atr_period).mean()

    # Session open approximation: use rolling first bar of each day
    # For hourly data, we use the open of each new day as reference
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        day = ts.dt.date
    else:
        day = pd.Series(range(len(df))) // 7  # fallback: 7 bars per session

    # Session open = first close of each new day group
    session_open = close.copy()
    prev_day = None
    current_open = close.iloc[0]
    for i in range(len(df)):
        d = day.iloc[i]
        if d != prev_day:
            current_open = float(df["open"].iloc[i])
            prev_day = d
        session_open.iloc[i] = current_open

    # Breakout levels
    upper_band = session_open + atr_mult * atr
    lower_band = session_open - atr_mult * atr

    # Long when close breaks above upper band
    entries = (close > upper_band) & (close.shift(1) <= upper_band.shift(1))

    # Exit when close drops below session open (stop) or below lower band
    exits = close < session_open

    # Clean: no entry while in trade
    in_trade = False
    clean_entries = []
    clean_exits = []

    for i in range(len(df)):
        if not in_trade and bool(entries.iloc[i]) if not pd.isna(entries.iloc[i]) else False:
            in_trade = True
            clean_entries.append(True)
            clean_exits.append(False)
        elif in_trade and (bool(exits.iloc[i]) if not pd.isna(exits.iloc[i]) else False):
            in_trade = False
            clean_entries.append(False)
            clean_exits.append(True)
        else:
            clean_entries.append(False)
            clean_exits.append(False)

    return pd.DataFrame({
        "entries": clean_entries,
        "exits": clean_exits,
    })

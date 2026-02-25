"""
Strategy: Mean Reversion (RSI + Bollinger Bands)

Mean reversion strategy using RSI oversold/overbought conditions confirmed
by Bollinger Band extremes. Buys when price is below lower BB and RSI < 30,
sells when RSI > 70 or price returns to BB middle.

Original: desenyon/spy_mean_reversion_strategy.py (QuantConnect)
Adapted: vectorbt signal format for individual stocks

Logic:
    - Bollinger Bands (20, 2.0 std)
    - RSI(14) for momentum confirmation
    - Entry LONG: close < lower_bb AND RSI < rsi_entry
    - Exit: RSI > rsi_exit OR close > middle_bb OR max_hold exceeded

Parameters:
    - bb_period: 20
    - bb_std: 2.0
    - rsi_period: 14
    - rsi_entry: 30 (buy when oversold)
    - rsi_exit: 70 (sell when overbought)
    - max_hold: 15 (force exit after N bars)

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/mean_reversion_rsi_bb.py \\
        --symbols AAPL MSFT AMZN --timeframe 1h --start 2023-06-01 --end 2025-12-31
"""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 30)
    rsi_exit = params.get("rsi_exit", 70)
    max_hold = params.get("max_hold", 15)

    close = df["close"]

    # Bollinger Bands
    sma = close.rolling(bb_period).mean()
    std = close.rolling(bb_period).std()
    upper_bb = sma + bb_std * std
    lower_bb = sma - bb_std * std

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(rsi_period).mean()
    avg_loss = loss.rolling(rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    # Raw signals
    raw_entries = (close < lower_bb) & (rsi < rsi_entry)
    raw_exits = (rsi > rsi_exit) | (close > sma)

    # State machine for clean entries/exits with max_hold
    in_trade = False
    bars_held = 0
    entries = []
    exits = []

    for i in range(len(df)):
        if not in_trade and raw_entries.iloc[i]:
            in_trade = True
            bars_held = 0
            entries.append(True)
            exits.append(False)
        elif in_trade:
            bars_held += 1
            entries.append(False)
            if raw_exits.iloc[i] or bars_held >= max_hold:
                in_trade = False
                exits.append(True)
            else:
                exits.append(False)
        else:
            entries.append(False)
            exits.append(False)

    return pd.DataFrame({
        "entries": entries,
        "exits": exits,
    })

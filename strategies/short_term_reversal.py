"""
Strategy: Short-Term Reversal

Exploits weekly mean reversion: stocks that dropped the most over 5 days tend to
bounce, and vice versa. Long the bottom performers, short (or avoid) the top.

Original: desenyon/short_term_reversal_strategy.py (QuantConnect)
Adapted: vectorbt signal format (single-symbol version)

Logic:
    - Compute 5-day return (pct_change)
    - Entry LONG when 5d return < -threshold (oversold bounce)
    - Exit after holding_period bars OR when 5d return > +threshold
    - Optional: RSI confirmation (RSI < 30 for entry)

Parameters:
    - lookback: 5 (5-day return window)
    - entry_threshold: -0.05 (buy when stock dropped 5%+ in 5 days)
    - exit_threshold: 0.03 (sell when stock gained 3%+ in 5 days)
    - rsi_period: 14
    - rsi_entry: 35 (RSI below this confirms oversold)
    - max_hold: 10 (force exit after 10 bars)

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/short_term_reversal.py \\
        --symbols AAPL NVDA AMZN --timeframe 1d --start 2021-01-01 --end 2025-12-31
"""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    lookback = params.get("lookback", 5)
    entry_threshold = params.get("entry_threshold", -0.05)
    exit_threshold = params.get("exit_threshold", 0.03)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 35)
    max_hold = params.get("max_hold", 10)

    close = df["close"]

    # 5-day return
    ret_5d = close.pct_change(lookback)

    # RSI calculation
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(rsi_period).mean()
    avg_loss = loss.rolling(rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    # Entry: 5d return below threshold AND RSI confirms oversold
    entries = (ret_5d < entry_threshold) & (rsi < rsi_entry)

    # Exit: 5d return above exit threshold (mean reverted)
    exits = ret_5d > exit_threshold

    # Force exit after max_hold bars (handled via a rolling window)
    # Track bars since last entry to enforce max hold
    in_trade = False
    bars_held = 0
    entry_list = []
    exit_list = []

    for i in range(len(df)):
        if not in_trade and entries.iloc[i]:
            in_trade = True
            bars_held = 0
            entry_list.append(True)
            exit_list.append(False)
        elif in_trade:
            bars_held += 1
            entry_list.append(False)
            if exits.iloc[i] or bars_held >= max_hold:
                in_trade = False
                exit_list.append(True)
            else:
                exit_list.append(False)
        else:
            entry_list.append(False)
            exit_list.append(False)

    return pd.DataFrame({
        "entries": entry_list,
        "exits": exit_list,
    })

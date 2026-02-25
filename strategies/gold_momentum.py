"""
Strategy: Gold Momentum (Multi-Timeframe)

Multi-timeframe momentum strategy for XAUUSD using MACD crossovers
confirmed by RSI momentum and Bollinger Band position.

Original: desenyon/gold_momentum_strategy.py (QuantConnect)
Adapted: vectorbt signal format for XAUUSD

Logic:
    - MACD(12, 26, 9) crossover for trend direction
    - RSI(14) for momentum confirmation
    - Bollinger Band(20, 2) for volatility context
    - Entry LONG: MACD line crosses above signal AND RSI > 50 AND close > BB middle
    - Exit: MACD line crosses below signal OR RSI < 40

Parameters:
    - macd_fast: 12
    - macd_slow: 26
    - macd_signal: 9
    - rsi_period: 14
    - rsi_entry: 50
    - rsi_exit: 40
    - bb_period: 20

Note: XAUUSD Dukascopy timestamps need fixing before use.
      Timestamps are Unix epoch seconds stored as datetime (wrong conversion during migration).

Usage:
    python -m runner.cli run --engine vectorbt --strategy strategies/gold_momentum.py \\
        --symbols XAUUSD --timeframe 1h --start 2021-01-01 --end 2025-12-31
"""

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    macd_fast = params.get("macd_fast", 12)
    macd_slow = params.get("macd_slow", 26)
    macd_signal_period = params.get("macd_signal", 9)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 50)
    rsi_exit = params.get("rsi_exit", 40)
    bb_period = params.get("bb_period", 20)

    close = df["close"]

    # MACD
    ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=macd_signal_period, adjust=False).mean()

    # MACD crossover
    macd_cross_up = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))
    macd_cross_down = (macd_line < signal_line) & (macd_line.shift(1) >= signal_line.shift(1))

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(rsi_period).mean()
    avg_loss = loss.rolling(rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    # Bollinger Band middle
    bb_mid = close.rolling(bb_period).mean()

    # Entry: MACD cross up + RSI momentum + above BB middle
    raw_entries = macd_cross_up & (rsi > rsi_entry) & (close > bb_mid)

    # Exit: MACD cross down OR RSI drops below exit threshold
    raw_exits = macd_cross_down | (rsi < rsi_exit)

    # State machine
    in_trade = False
    entries = []
    exits = []

    for i in range(len(df)):
        if not in_trade and raw_entries.iloc[i]:
            in_trade = True
            entries.append(True)
            exits.append(False)
        elif in_trade and raw_exits.iloc[i]:
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

import pandas as pd
import numpy as np

STRATEGY_META = {
    "name": "ema crossoiver conversion test",
    "version": "1.0.0",
    "tags": {
        "type": ["trend-following"],
        "source": "Pine Script v5 - Impulse Cross",
        "link": "",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}


def generate_signals(df, params):
    """
    Generate signals for EMA Crossover strategy.
    Entry: EMA(9) crosses above EMA(21)
    Exit: EMA(9) crosses below EMA(21)
    Stop-Loss: ATR(14) * 2.0 below entry price (added for risk management)
    """
    params = params or {}
    # Get parameters with defaults
    fast_period = params.get('fast_period', 9)
    slow_period = params.get('slow_period', 21)
    atr_period = params.get('atr_period', 14)
    atr_multiplier = params.get('atr_multiplier', 2.0)

    # Calculate EMAs
    fast_ema = df['close'].ewm(span=fast_period, adjust=False).mean()
    slow_ema = df['close'].ewm(span=slow_period, adjust=False).mean()

    # Calculate ATR for stop-loss
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=atr_period).mean()

    # Entry trigger: Fast EMA crosses above Slow EMA
    entries = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))

    # Exit trigger: Fast EMA crosses below Slow EMA
    exits = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

    # Stop-loss calculation: Entry price - ATR * multiplier
    # This provides a technical stop when the crossover exit hasn't triggered yet
    sl_price = df['close'] - (atr * atr_multiplier)

    # Prepare indicators dict for trade meta enrichment
    indicators_dict = {
        'sl_price': sl_price,
        'atr_value': atr,
        'fast_ema': fast_ema,
        'slow_ema': slow_ema,
    }

    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_times = pd.to_datetime(
            df.loc[entries[entries].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        exit_times = pd.to_datetime(
            df.loc[exits[exits].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        for ts in entry_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Entry",
                }
            )
        for ts in exit_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#ef4444",
                    "text": "Exit",
                }
            )

    return entries, exits, indicators_dict, drawings

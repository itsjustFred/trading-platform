"""
Strategy: Trend Following (Multi-Timeframe Momentum)

CTA-style trend following using multiple SMA timeframes for trend confirmation
and ATR-based trailing stops.
"""

STRATEGY_META = {
    "name": "Trend Following",
    "version": "1.0.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/trend_following_cta.py",
    "tags": {
        "type": ["momentum", "trend-following"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/trend_following_cta",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast = params.get("fast_sma", 20)
    mid = params.get("mid_sma", 50)
    slow = params.get("slow_sma", 200)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    sma_fast = close.rolling(fast).mean()
    sma_mid = close.rolling(mid).mean()
    sma_slow = close.rolling(slow).mean()

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    fast_cross_up = (sma_fast > sma_mid) & (sma_fast.shift(1) <= sma_mid.shift(1))
    trend_up = sma_mid > sma_slow
    fast_cross_down = (sma_fast < sma_mid) & (sma_fast.shift(1) >= sma_mid.shift(1))

    in_trade = False
    trailing_stop_value = 0.0
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    trailing_stop_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    for i in range(len(df)):
        if pd.isna(sma_slow.iloc[i]):
            continue

        if not in_trade:
            if bool(fast_cross_up.iloc[i]) and bool(trend_up.iloc[i]):
                atr_value = atr.iloc[i]
                entries.iloc[i] = True
                in_trade = True
                if pd.notna(atr_value):
                    trailing_stop_value = float(close.iloc[i]) - atr_stop_mult * float(atr_value)
                    sl_price.iloc[i] = trailing_stop_value
        else:
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                new_stop = float(close.iloc[i]) - atr_stop_mult * float(atr_value)
                trailing_stop_value = max(trailing_stop_value, new_stop)
                trailing_stop_at_exit.iloc[i] = trailing_stop_value

            trailing_stop_hit = float(close.iloc[i]) < trailing_stop_value
            cross_down_hit = bool(fast_cross_down.iloc[i])
            if trailing_stop_hit or cross_down_hit:
                in_trade = False
                exits.iloc[i] = True
                trailing_stop_at_exit.iloc[i] = trailing_stop_value
                if trailing_stop_hit:
                    exit_price.iloc[i] = trailing_stop_value
                    exit_reason.iloc[i] = "trailing_stop"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "sma_cross_down"
                trailing_stop_value = 0.0

    indicators_dict = {
        "SMA_Fast": sma_fast,
        "SMA_Mid": sma_mid,
        "SMA_Slow": sma_slow,
        "SMA_Fast_at_exit": sma_fast,
        "SMA_Mid_at_exit": sma_mid,
        "ATR": atr,
        "sl_price": sl_price,
        "trailing_stop_at_exit": trailing_stop_at_exit,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
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

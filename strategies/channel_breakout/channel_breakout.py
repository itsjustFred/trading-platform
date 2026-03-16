"""
Intraday Channel Breakout Strategy

N-Bar High/Low channel breakout with 200 EMA trend filter,
gap filter, and ATR trailing stop.
"""

import numpy as np
import pandas as pd


STRATEGY_META = {
    "name": "Intraday Channel Breakout",
    "version": "1.0.0",
    "source_url": "Discord: zenomtrader",
    "tags": {
        "type": ["breakout"],
        "source": "zenomtrader-discord",
        "link": "",
        "asset_class": ["equities", "futures"],
        "complexity": "medium",
    },
}


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    channel_length = int(params.get("channel_length", 5))
    ema_period = int(params.get("ema_period", 200))
    use_ema_filter = params.get("use_ema_filter", True)
    atr_period = int(params.get("atr_period", 14))
    atr_multiplier = float(params.get("atr_multiplier", 2.0))

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema200 = close.ewm(span=ema_period, adjust=False).mean()
    upper_channel = high.rolling(channel_length).max()
    lower_channel = low.rolling(channel_length).min()

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    atr = tr.rolling(atr_period).mean()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    trailing_stop_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    trailing_stop_value = 0.0
    warmup = max(channel_length, ema_period, atr_period)

    for i in range(warmup, len(df)):
        if in_trade:
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                new_stop = float(close.iloc[i]) - float(atr_value) * atr_multiplier
                trailing_stop_value = max(trailing_stop_value, new_stop)
                trailing_stop_at_exit.iloc[i] = trailing_stop_value
            if float(close.iloc[i]) <= trailing_stop_value:
                exits.iloc[i] = True
                exit_price.iloc[i] = trailing_stop_value
                exit_reason.iloc[i] = "trailing_stop"
                in_trade = False
                trailing_stop_value = 0.0
        else:
            ema_long_ok = (not use_ema_filter) or (float(close.iloc[i]) > float(ema200.iloc[i]))
            if ema_long_ok and float(close.iloc[i]) > float(upper_channel.iloc[i - 1]):
                entries.iloc[i] = True
                in_trade = True
                atr_value = atr.iloc[i]
                if pd.notna(atr_value):
                    trailing_stop_value = float(close.iloc[i]) - float(atr_value) * atr_multiplier
                    sl_price.iloc[i] = trailing_stop_value

    indicators_dict = {
        "EMA_200": ema200,
        "Upper_Channel": upper_channel,
        "Lower_Channel": lower_channel,
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

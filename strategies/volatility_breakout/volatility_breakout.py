"""
Strategy: Intraday Volatility Breakout

Simple breakout strategy: if price moves beyond the daily open + ATR multiplier,
enter long and stop at the session open.
"""

STRATEGY_META = {
    "name": "Volatility Breakout",
    "version": "1.0.0",
    "source_url": "Discord: zenomtrader",
    "tags": {
        "type": ["breakout"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/zenomtrader",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    atr_period = params.get("atr_period", 5)
    atr_mult = params.get("atr_mult", 0.3)

    high = df["high"]
    low = df["low"]
    close = df["close"]

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

    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        day = ts.dt.date
    else:
        day = pd.Series(range(len(df)), index=df.index) // 7

    session_open = close.copy()
    prev_day = None
    current_open = float(close.iloc[0])
    for i in range(len(df)):
        d = day.iloc[i]
        if d != prev_day:
            current_open = float(df["open"].iloc[i])
            prev_day = d
        session_open.iloc[i] = current_open

    upper_band = session_open + atr_mult * atr
    lower_band = session_open - atr_mult * atr
    raw_entries = (close > upper_band) & (close.shift(1) <= upper_band.shift(1))
    in_trade = False
    active_stop = np.nan
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    for i in range(len(df)):
        entry_signal = bool(raw_entries.iloc[i]) if not pd.isna(raw_entries.iloc[i]) else False
        exit_signal = in_trade and float(low.iloc[i]) <= float(active_stop)

        if not in_trade and entry_signal:
            in_trade = True
            entries.iloc[i] = True
            active_stop = float(session_open.iloc[i])
            sl_price.iloc[i] = active_stop
        elif in_trade and exit_signal:
            in_trade = False
            exits.iloc[i] = True
            exit_price.iloc[i] = active_stop
            exit_reason.iloc[i] = "stop_loss"
            active_stop = np.nan

    indicators_dict = {
        "Session_Open": session_open,
        "Session_Open_at_exit": session_open,
        "ATR": atr,
        "Upper_Band": upper_band,
        "Lower_Band": lower_band,
        "sl_price": sl_price,
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

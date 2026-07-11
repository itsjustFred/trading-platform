"""
Strategy: Intraday Volatility Breakout

MT5-style intraday breakout around the session open. The reference reports in
strategy-comparisons use both long and short breakouts, ATR-based stops, a short
ATR regime filter and fixed trading hours.
"""

STRATEGY_META = {
    "name": "Volatility Breakout",
    "version": "1.0.0",
    "source_url": "https://discord.com/channels/zenomtrader",
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
    atr_period = int(params.get("atr_period", params.get("ATR_Period", 5)))
    atr_mult = float(params.get("atr_mult", params.get("ATR_Multiplier", 0.25)))
    stop_atr_mult = float(
        params.get("stop_atr_mult", params.get("Stop_ATR_Multiplier", 0.25))
    )
    atr_short_period = int(params.get("atr_short_period", params.get("ATR_Short_Period", 1)))
    atr_long_period = int(params.get("atr_long_period", params.get("ATR_Long_Period", 5)))
    atr_regime_factor = float(
        params.get("atr_regime_factor", params.get("ATR_Regime_Factor", 1.0))
    )
    trade_direction = int(params.get("trade_direction", params.get("Trade_Direction", 0)))
    entry_time = str(params.get("entry_time", params.get("Entry_Time", "16:30")))
    max_entry_time = str(params.get("max_entry_time", params.get("Max_Entry_Time", "18:05")))
    exit_time = str(params.get("exit_time", params.get("Exit_Time", "22:55")))

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
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        day = ts.dt.date
        minute_of_day = ts.dt.hour * 60 + ts.dt.minute
    elif isinstance(df.index, pd.DatetimeIndex):
        ts = pd.Series(pd.to_datetime(df.index, utc=True), index=df.index)
        day = ts.dt.date
        minute_of_day = ts.dt.hour * 60 + ts.dt.minute
    else:
        raise ValueError("generate_signals requires a timestamp column or DatetimeIndex")

    def parse_minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    entry_minute = parse_minutes(entry_time)
    max_entry_minute = parse_minutes(max_entry_time)
    exit_minute = parse_minutes(exit_time)

    session_open = close.copy()
    prev_day = None
    current_open = float(close.iloc[0]) if len(close) > 0 else float("nan")
    for i in range(len(df)):
        d = day.iloc[i]
        if d != prev_day:
            current_open = float(df["open"].iloc[i])
            prev_day = d
        session_open.iloc[i] = current_open

    upper_band = session_open + atr_mult * atr
    lower_band = session_open - atr_mult * atr
    atr_short = tr.rolling(atr_short_period).mean()
    atr_long = tr.rolling(atr_long_period).mean()
    regime_ok = atr_short >= (atr_long * atr_regime_factor)
    entry_window = (minute_of_day >= entry_minute) & (minute_of_day <= max_entry_minute)
    exit_window = minute_of_day >= exit_minute
    allow_longs = trade_direction in {0, 1}
    allow_shorts = trade_direction in {0, -1, 2}
    raw_long_entries = (
        allow_longs
        & entry_window
        & regime_ok
        & (close > upper_band)
        & (close.shift(1) <= upper_band.shift(1))
    )
    raw_short_entries = (
        allow_shorts
        & entry_window
        & regime_ok
        & (close < lower_band)
        & (close.shift(1) >= lower_band.shift(1))
    )
    in_trade = False
    active_stop = np.nan
    active_direction = ""
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    for i in range(len(df)):
        long_entry = bool(raw_long_entries.iloc[i]) if not pd.isna(raw_long_entries.iloc[i]) else False
        short_entry = bool(raw_short_entries.iloc[i]) if not pd.isna(raw_short_entries.iloc[i]) else False
        stop_hit = False
        if in_trade and pd.notna(active_stop):
            if active_direction == "LONG":
                stop_hit = float(low.iloc[i]) <= float(active_stop)
            else:
                stop_hit = float(high.iloc[i]) >= float(active_stop)
        time_exit = in_trade and bool(exit_window.iloc[i])

        if not in_trade and (long_entry or short_entry):
            in_trade = True
            entries.iloc[i] = True
            active_direction = "LONG" if long_entry else "SHORT"
            direction.iloc[i] = active_direction
            stop_distance = float(atr.iloc[i]) * stop_atr_mult if pd.notna(atr.iloc[i]) else 0.0
            active_stop = (
                float(close.iloc[i]) - stop_distance
                if active_direction == "LONG"
                else float(close.iloc[i]) + stop_distance
            )
            sl_price.iloc[i] = active_stop
        elif in_trade and (stop_hit or time_exit):
            in_trade = False
            exits.iloc[i] = True
            exit_price.iloc[i] = active_stop if stop_hit else float(close.iloc[i])
            exit_reason.iloc[i] = "stop_loss" if stop_hit else "session_exit"
            active_stop = np.nan
            active_direction = ""

    indicators_dict = {
        "Session_Open": session_open,
        "Session_Open_at_exit": session_open,
        "ATR": atr,
        "ATR_Short": atr_short,
        "ATR_Long": atr_long,
        "ATR_Regime_OK": regime_ok,
        "Upper_Band": upper_band,
        "Lower_Band": lower_band,
        "direction": direction,
        "stop_loss": sl_price,
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

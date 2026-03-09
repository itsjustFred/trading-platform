"""
Strategy: Mean Reversion (RSI + Bollinger Bands)

Mean reversion strategy using RSI oversold/overbought conditions confirmed
by Bollinger Band extremes.
"""

STRATEGY_META = {
    "name": "Mean Reversion RSI+BB",
    "version": "1.0.0",
    "tags": {
        "type": ["mean-reversion"],
        "source": "alexisbouley2-github",
        "link": "https://github.com/alexisbouley2/spy_mean_reversion_strategy",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 30)
    rsi_exit = params.get("rsi_exit", 70)
    max_hold = params.get("max_hold", 15)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    sma = close.rolling(bb_period).mean()
    std = close.rolling(bb_period).std()
    upper_bb = sma + bb_std * std
    lower_bb = sma - bb_std * std
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(rsi_period).mean()
    avg_loss = loss.rolling(rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    raw_entries = (close < lower_bb) & (rsi < rsi_entry)
    raw_exits = (rsi > rsi_exit) | (close > sma)

    in_trade = False
    bars_held = 0
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    bars_held_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    active_stop = np.nan

    for i in range(len(df)):
        if not in_trade and bool(raw_entries.iloc[i]):
            in_trade = True
            bars_held = 0
            entries.iloc[i] = True
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            bars_held += 1
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            rsi_exit_hit = bool(rsi.iloc[i] > rsi_exit) if pd.notna(rsi.iloc[i]) else False
            mean_reversion_hit = (
                bool(close.iloc[i] > sma.iloc[i]) if pd.notna(sma.iloc[i]) else False
            )
            time_exit_hit = bars_held >= max_hold
            if stop_hit or rsi_exit_hit or mean_reversion_hit or time_exit_hit:
                in_trade = False
                exits.iloc[i] = True
                bars_held_at_exit.iloc[i] = bars_held
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif rsi_exit_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "rsi_exit"
                elif mean_reversion_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "mean_reversion_to_mid"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "time_exit"
                active_stop = np.nan

    indicators_dict = {
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "BB_Mid": sma,
        "BB_Mid_at_exit": sma,
        "BB_Upper": upper_bb,
        "BB_Lower": lower_bb,
        "ATR": atr,
        "sl_price": sl_price,
        "bars_held_at_exit": bars_held_at_exit,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    return entries, exits, indicators_dict

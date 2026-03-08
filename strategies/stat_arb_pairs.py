"""
Statistical Arbitrage Pairs Trading Strategy

Simplified pairs trading: Z-score of a spread proxy. Long-only for vectorbt compatibility.
"""

import numpy as np
import pandas as pd


STRATEGY_META = {
    "name": "Statistical Arbitrage Pairs",
    "version": "1.0.0",
    "tags": {
        "type": ["mean-reversion", "pairs-trading"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/quantconnect-strategies",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    lookback = int(params.get("lookback", 60))
    entry_zscore = float(params.get("entry_zscore", 2.0))
    exit_zscore = float(params.get("exit_zscore", 0.5))
    stop_zscore = float(params.get("stop_zscore", 4.0))
    atr_period = int(params.get("atr_period", 14))
    atr_stop_mult = float(params.get("atr_stop_mult", 2.0))

    close = df["close"]
    high = df["high"]
    low = df["low"]
    log_returns = np.log(close / close.shift(1)).fillna(0.0)
    cum_ret = log_returns.cumsum()
    rolling_mean = cum_ret.rolling(lookback).mean()
    rolling_std = cum_ret.rolling(lookback).std().replace(0, np.nan)
    zscore = (cum_ret - rolling_mean) / rolling_std
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_long = False
    active_stop = np.nan

    for i in range(lookback, len(df)):
        z_value = zscore.iloc[i]
        if pd.isna(z_value):
            continue
        if in_long:
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            mean_reversion_hit = z_value >= -exit_zscore
            zscore_stop_hit = z_value <= -stop_zscore
            if stop_hit or mean_reversion_hit or zscore_stop_hit:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif mean_reversion_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "mean_reversion"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "zscore_stop"
                in_long = False
                active_stop = np.nan
        elif z_value <= -entry_zscore:
            entries.iloc[i] = True
            in_long = True
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop

    indicators_dict = {
        "ZScore": zscore,
        "ZScore_at_exit": zscore,
        "Rolling_Mean": rolling_mean,
        "Rolling_Std": rolling_std,
        "ATR": atr,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    return entries, exits, indicators_dict

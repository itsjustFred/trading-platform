"""
Strategy: Cross-Asset Momentum (Time-Series)

Time-series momentum applied across multiple asset classes.
Each asset is evaluated independently: go long when momentum is positive.
"""

STRATEGY_META = {
    "name": "Cross-Asset Momentum",
    "version": "1.0.0",
    "tags": {
        "type": ["momentum", "cross-asset"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/CrossAssetMomentumAlpha",
        "asset_class": ["multi-asset", "etf"],
        "complexity": "complex",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    lookbacks = params.get("lookbacks", [21, 63, 126, 252])
    weights = params.get("weights", [0.4, 0.3, 0.2, 0.1])
    vol_window = params.get("vol_window", 60)
    dd_exit = params.get("dd_exit", -0.08)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    composite = pd.Series(0.0, index=df.index)
    for lookback, weight in zip(lookbacks, weights):
        composite += close.pct_change(lookback).fillna(0) * weight

    vol = close.pct_change().rolling(vol_window).std() * np.sqrt(252)
    vol_adj_mom = composite / vol.replace(0, np.nan)
    rolling_max = close.rolling(252, min_periods=1).max()
    drawdown = (close - rolling_max) / rolling_max
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    entry_cond = (composite > 0) & (vol_adj_mom > 0)
    exit_cond = (composite < -0.01) | (drawdown < dd_exit)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan
    warmup = max(lookbacks) + vol_window

    for i in range(warmup, len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            momentum_break = (
                bool(composite.iloc[i] < -0.01) if pd.notna(composite.iloc[i]) else False
            )
            drawdown_break = (
                bool(drawdown.iloc[i] < dd_exit) if pd.notna(drawdown.iloc[i]) else False
            )
            if stop_hit or momentum_break or drawdown_break:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif momentum_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "momentum_break"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "drawdown_break"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "Composite_Momentum": composite,
        "Composite_Momentum_at_exit": composite,
        "Vol_Adjusted_Momentum": vol_adj_mom,
        "Rolling_Drawdown": drawdown,
        "Rolling_Drawdown_at_exit": drawdown,
        "ATR": atr,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    return entries, exits, indicators_dict

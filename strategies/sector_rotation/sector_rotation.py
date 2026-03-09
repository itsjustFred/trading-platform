"""
Strategy: Sector Rotation (Relative Momentum)

Rotates allocation among US sector ETFs based on momentum ranking.
In the single-symbol adapter this becomes a momentum-filtered long strategy.
"""

STRATEGY_META = {
    "name": "Sector Rotation",
    "version": "1.0.0",
    "tags": {
        "type": ["momentum", "rotation"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/SectorMomentumAlpha",
        "asset_class": ["etf", "equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    lookback = params.get("lookback", 63)
    fast_lookback = params.get("fast_lookback", 21)
    exit_lookback = params.get("exit_lookback", 10)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    momentum = close.pct_change(lookback)
    fast_momentum = close.pct_change(fast_lookback)
    sma_200 = close.rolling(200).mean()
    short_weakness = close.pct_change(exit_lookback) < -0.03
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    entry_cond = (momentum > 0) & (fast_momentum > 0) & (close > sma_200)
    exit_cond = (momentum < 0) | short_weakness

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan

    for i in range(max(lookback, 200), len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            momentum_break = bool(momentum.iloc[i] < 0) if pd.notna(momentum.iloc[i]) else False
            weakness_hit = bool(short_weakness.iloc[i])
            if stop_hit or momentum_break or weakness_hit:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif momentum_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "momentum_break"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "short_weakness"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "Momentum_63": momentum,
        "Momentum_63_at_exit": momentum,
        "Momentum_21": fast_momentum,
        "SMA_200": sma_200,
        "Short_Weakness": short_weakness,
        "Short_Weakness_at_exit": short_weakness,
        "ATR": atr,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    return entries, exits, indicators_dict

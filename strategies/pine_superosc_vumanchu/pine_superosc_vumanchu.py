"""
Strategy: SuperOSC + VuManChu WaveTrend (Pine Script Conversion)

Combines a SuperTrend Oscillator with VuManChu WaveTrend B for entries.
Buys when SuperTrend oscillator is bullish AND WaveTrend crosses up from oversold.
"""

STRATEGY_META = {
    "name": "SuperOSC + VuManChu",
    "version": "1.0.0",
    "tags": {
        "type": ["momentum", "mean-reversion"],
        "source": "indikatoren-repo",
        "link": "https://github.com/itsjustFred/Indikatoren",
        "asset_class": ["equities", "forex"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd

from shared.indicators import presets
from shared.strategy_markers import entry_exit_markers


def generate_signals(df: pd.DataFrame, params: dict):
    sto_length = params.get("sto_length", 10)
    sto_mult = params.get("sto_mult", 8.0)
    vmc_channel = params.get("vmc_channel_len", 9)
    vmc_avg = params.get("vmc_avg_len", 12)
    vmc_ma = params.get("vmc_ma_len", 3)
    atr_sl_len = params.get("atr_sl_len", 13)
    atr_sl_mult = params.get("atr_sl_mult", 1.5)
    tp_r_mult = params.get("tp_r_mult", 2.0)

    close = df["close"]
    osc = presets.superosc_luxalgo(df["high"], df["low"], df["close"], sto_length, sto_mult)
    wt1, wt2 = presets.wavetrend_vumanchu(
        df["high"], df["low"], df["close"], vmc_channel, vmc_avg, vmc_ma
    )

    wt_cross_up = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    buy_signal = (osc > 0) & wt_cross_up & (wt2 <= -60)

    atr = presets.atr_pine_ta(df["high"], df["low"], df["close"], atr_sl_len)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    tp_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    stop_loss = 0.0
    take_profit = 0.0
    warmup = max(sto_length, vmc_channel + vmc_avg, atr_sl_len) + 5

    for i in range(warmup, len(df)):
        if not in_trade and bool(buy_signal.iloc[i]):
            risk = atr.iloc[i] * atr_sl_mult
            entries.iloc[i] = True
            in_trade = True
            stop_loss = float(close.iloc[i]) - float(risk)
            take_profit = float(close.iloc[i]) + float(risk) * tp_r_mult
            sl_price.iloc[i] = stop_loss
            tp_price.iloc[i] = take_profit
        elif in_trade:
            stop_hit = float(df["low"].iloc[i]) <= stop_loss
            tp_hit = float(df["high"].iloc[i]) >= take_profit
            if stop_hit or tp_hit:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = stop_loss
                    exit_reason.iloc[i] = "stop_loss"
                else:
                    exit_price.iloc[i] = take_profit
                    exit_reason.iloc[i] = "take_profit"
                in_trade = False

    indicators_dict = {
        "STO_Osc": osc,
        "WaveTrend_1": wt1,
        "WaveTrend_2": wt2,
        "ATR": atr,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    drawings = entry_exit_markers(df, entries, exits)

    return entries, exits, indicators_dict, drawings

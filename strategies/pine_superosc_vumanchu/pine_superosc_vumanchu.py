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


def _calc_supertrend_osc(df: pd.DataFrame, length: int, mult: float):
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    hl2 = (high + low) / 2

    tr = np.zeros(len(close))
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = pd.Series(tr, index=df.index).rolling(length).mean()

    upper = np.full(len(close), np.nan)
    lower = np.full(len(close), np.nan)
    trend = np.ones(len(close))

    atr_values = atr.values
    for i in range(length, len(close)):
        up = hl2[i] + mult * atr_values[i]
        dn = hl2[i] - mult * atr_values[i]

        if i > 0 and not np.isnan(upper[i - 1]):
            upper[i] = min(up, upper[i - 1]) if close[i - 1] < upper[i - 1] else up
            lower[i] = max(dn, lower[i - 1]) if close[i - 1] > lower[i - 1] else dn
        else:
            upper[i] = up
            lower[i] = dn

        if i > 0 and not np.isnan(upper[i - 1]) and close[i] > upper[i - 1]:
            trend[i] = 1
        elif i > 0 and not np.isnan(lower[i - 1]) and close[i] < lower[i - 1]:
            trend[i] = 0
        else:
            trend[i] = trend[i - 1] if i > 0 else 1

    spt = np.where(trend == 1, lower, upper)
    den = upper - lower
    den = np.where(np.abs(den) < 1e-10, 1e-10, den)
    osc = np.clip((close - spt) / den, -1, 1)
    return pd.Series(osc, index=df.index)


def _calc_wavetrend(df: pd.DataFrame, channel_len: int, avg_len: int, ma_len: int):
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3
    esa = hlc3.ewm(span=channel_len, adjust=False).mean()
    de = (hlc3 - esa).abs().ewm(span=channel_len, adjust=False).mean()
    ci = (hlc3 - esa) / (0.015 * de).clip(lower=1e-10)
    wt1 = ci.ewm(span=avg_len, adjust=False).mean()
    wt2 = wt1.rolling(ma_len).mean()
    return wt1, wt2


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
    osc = _calc_supertrend_osc(df, sto_length, sto_mult)
    wt1, wt2 = _calc_wavetrend(df, vmc_channel, vmc_avg, vmc_ma)

    wt_cross_up = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    buy_signal = (osc > 0) & wt_cross_up & (wt2 <= -60)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - close.shift(1)).abs(),
            (df["low"] - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_sl_len).mean()

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

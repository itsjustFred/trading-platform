"""
Strategy: Post VIX Dip Buy

Buys SPY on panic dips: when VIX spikes significantly and SPY drops,
enter a mean-reversion long trade expecting a bounce.
"""

STRATEGY_META = {
    "name": "Post VIX Dip Buy",
    "version": "1.0.0",
    "source_url": "Discord: zenomtrader",
    "tags": {
        "type": ["mean-reversion", "regime-filter"],
        "source": "zenomtrader-discord",
        "link": "",
        "asset_class": ["equities", "etf"],
        "complexity": "medium",
    },
}

import os
from pathlib import Path

import numpy as np
import pandas as pd


def _load_vix_data(start: str = None, end: str = None) -> pd.Series | None:
    from shared.constants import DATA_DIR

    ohlcv_root = DATA_DIR / "ohlcv"
    matches = list(ohlcv_root.rglob("**/VIX/1d.parquet"))
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
    return df["close"]


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def generate_signals(df: pd.DataFrame, params: dict):
    vix_threshold = params.get("vix_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 35)
    rsi_exit = params.get("rsi_exit", 60)
    sma_period = params.get("sma_period", 20)
    max_hold = params.get("max_hold", 20)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    rsi = _calc_rsi(close, rsi_period)
    sma = close.rolling(sma_period).mean()
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()
    vix = _load_vix_data()

    vix_aligned = pd.Series(np.nan, index=df.index)
    vix_data_available = pd.Series(vix is not None, index=df.index)
    if vix is not None and "timestamp" in df.columns:
        spy_dates = pd.to_datetime(df["timestamp"]).dt.date
        for i, current_date in enumerate(spy_dates):
            vix_on_date = vix.loc[vix.index.date == current_date]
            if len(vix_on_date) > 0:
                vix_aligned.iloc[i] = vix_on_date.iloc[0]
        vix_aligned = vix_aligned.ffill()
    else:
        vix_aligned = pd.Series(vix_threshold + 1, index=df.index)

    vix_fallback_used = pd.Series(vix is None, index=df.index)
    entry_cond = (vix_aligned > vix_threshold) & (rsi < rsi_entry) & (close < sma)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    bars_held_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    entry_bar = 0
    active_stop = np.nan

    for i in range(sma_period, len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            entry_bar = i
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            bars_held = i - entry_bar
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            rsi_high = rsi.iloc[i] > rsi_exit if pd.notna(rsi.iloc[i]) else False
            above_sma = close.iloc[i] > sma.iloc[i] if pd.notna(sma.iloc[i]) else False
            time_exit_hit = bars_held >= max_hold
            if stop_hit or rsi_high or above_sma or time_exit_hit:
                exits.iloc[i] = True
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif rsi_high:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "rsi_exit"
                elif above_sma:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "mean_reversion_to_sma"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "time_exit"
                active_stop = np.nan

    indicators_dict = {
        "VIX": vix_aligned,
        "VIX_at_exit": vix_aligned,
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "SMA": sma,
        "SMA_at_exit": sma,
        "ATR": atr,
        "sl_price": sl_price,
        "VIX_Data_Available": vix_data_available,
        "VIX_Fallback_Used": vix_fallback_used,
        "bars_held_at_exit": bars_held_at_exit,
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

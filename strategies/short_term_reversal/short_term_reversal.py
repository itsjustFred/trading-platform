"""
Strategy: Short-Term Reversal

Exploits weekly mean reversion: stocks that dropped the most over 5 days tend to
bounce, and vice versa.
"""

STRATEGY_META = {
    "name": "Short Term Reversal",
    "version": "1.0.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/short_term_reversal_strategy.py",
    "tags": {
        "type": ["mean-reversion"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/short_term_reversal_strategy",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    lookback = params.get("lookback", 5)
    entry_threshold = params.get("entry_threshold", -0.05)
    exit_threshold = params.get("exit_threshold", 0.03)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 35)
    max_hold = params.get("max_hold", 10)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    ret_lookback = close.pct_change(lookback)
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

    raw_entries = (ret_lookback < entry_threshold) & (rsi < rsi_entry)
    raw_exits = ret_lookback > exit_threshold

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
        if (
            not in_trade
            and bool(raw_entries.iloc[i])
            and pd.notna(atr.iloc[i])
        ):
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
            return_reversion_hit = bool(raw_exits.iloc[i])
            time_exit_hit = bars_held >= max_hold
            if stop_hit or return_reversion_hit or time_exit_hit:
                in_trade = False
                exits.iloc[i] = True
                bars_held_at_exit.iloc[i] = bars_held
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif return_reversion_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "return_reversion"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "time_exit"
                active_stop = np.nan

    indicators_dict = {
        "Return_Lookback": ret_lookback,
        "Return_Lookback_at_exit": ret_lookback,
        "RSI": rsi,
        "ATR": atr,
        "sl_price": sl_price,
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

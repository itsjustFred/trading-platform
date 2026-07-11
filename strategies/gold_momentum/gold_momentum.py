"""
Strategy: Gold Momentum (Multi-Timeframe)

Multi-timeframe momentum strategy for XAUUSD using MACD crossovers
confirmed by RSI momentum and Bollinger Band position.
"""

STRATEGY_META = {
    "name": "Gold Momentum",
    "version": "1.0.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/gold_momentum_strategy.py",
    "tags": {
        "type": ["momentum"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/zenomtrader",
        "asset_class": ["forex"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    macd_fast = params.get("macd_fast", 12)
    macd_slow = params.get("macd_slow", 26)
    macd_signal_period = params.get("macd_signal", 9)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 50)
    rsi_exit = params.get("rsi_exit", 40)
    bb_period = params.get("bb_period", 20)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=macd_signal_period, adjust=False).mean()

    macd_cross_up = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))
    macd_cross_down = (macd_line < signal_line) & (macd_line.shift(1) >= signal_line.shift(1))

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(rsi_period).mean()
    avg_loss = loss.rolling(rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    bb_mid = close.rolling(bb_period).mean()
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()
    raw_entries = macd_cross_up & (rsi > rsi_entry) & (close > bb_mid)
    raw_exits = macd_cross_down | (rsi < rsi_exit)

    in_trade = False
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
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
            entries.iloc[i] = True
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            macd_exit_hit = bool(macd_cross_down.iloc[i])
            rsi_exit_hit = bool(rsi.iloc[i] < rsi_exit) if pd.notna(rsi.iloc[i]) else False
            if stop_hit or macd_exit_hit or rsi_exit_hit:
                in_trade = False
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif macd_exit_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "macd_cross_down"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "rsi_exit"
                active_stop = np.nan

    indicators_dict = {
        "MACD_Line": macd_line,
        "MACD_Line_at_exit": macd_line,
        "MACD_Signal": signal_line,
        "MACD_Signal_at_exit": signal_line,
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "BB_Mid": bb_mid,
        "ATR": atr,
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

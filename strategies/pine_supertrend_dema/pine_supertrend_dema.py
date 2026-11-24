"""
Strategy: Supertrend + DEMA (Pine Script Conversion)

Combines Supertrend indicator with Double EMA (DEMA) filter.
Long when Supertrend flips bullish AND price > DEMA.

Exits: only the INITIAL Supertrend band is protective on native — ``sl_price``
is written on the entry bar alone, so the engine's stop never trails. The
trailed level travels in the exit bar's ``exit_price``/``trailing_stop_at_exit``,
which native honors only with ``exit_order_type="level"`` (not emitted here).
A trailed-stop hit (a low-touch, not close-triggered) or Supertrend flip-down
is therefore a market exit that fills at the next bar's open under the default
``signal_exit_fill="next_open"`` — any ``exit_price`` written on the stop-hit
bar is informational and ignored without ``exit_order_type="level"``, so the
trailed level itself is not enforced as a fill price. Native also refuses any
resolved ``use_trailing_sl=true`` parameter on this strategy even though the
parameter is not part of its signature (maintainer decision 2026-11-23).
"""

STRATEGY_META = {
    "name": "Supertrend + DEMA",
    "version": "1.1.0",
    "tags": {
        "type": ["trend-following"],
        "source": "indikatoren-repo",
        "link": "https://github.com/itsjustFred/Indikatoren",
        "asset_class": ["equities", "forex"],
        "complexity": "simple",
    },
}

import numpy as np
import pandas as pd

from shared.indicators import presets
from shared.strategy_markers import entry_exit_markers


def generate_signals(df: pd.DataFrame, params: dict):
    atr_period = params.get("atr_period", 10)
    atr_mult = params.get("atr_mult", 3.0)
    dema_length = params.get("dema_length", 9)

    close = df["close"]
    st = presets.supertrend_pine_dema(df["high"], df["low"], df["close"], atr_period, atr_mult)
    trend = st.trend
    lower_band = st.lower_band
    upper_band = st.upper_band
    dema = presets.dema_pine_ta(close, dema_length)

    trend_flip_up = (trend == 1) & (trend.shift(1) == -1)
    trend_flip_down = (trend == -1) & (trend.shift(1) == 1)
    entry_cond = trend_flip_up & (close > dema)
    exit_cond = trend_flip_down

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    trailing_stop_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan

    for i in range(atr_period, len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            active_stop = float(lower_band.iloc[i])
            sl_price.iloc[i] = active_stop
        elif in_trade:
            active_stop = max(float(active_stop), float(lower_band.iloc[i]))
            trailing_stop_at_exit.iloc[i] = active_stop
            stop_hit = float(df["low"].iloc[i]) <= active_stop
            if stop_hit or bool(exit_cond.iloc[i]):
                exits.iloc[i] = True
                trailing_stop_at_exit.iloc[i] = active_stop
                # Only the INITIAL band is protective on native: sl_price is
                # written on the entry bar alone, so the engine's stop never
                # trails. The trailed level travels in exit_price /
                # trailing_stop_at_exit, honored as a level only with
                # exit_order_type="level" (not emitted here). A trailed-stop
                # hit or flip is a MARKET exit -> next bar's open; on a stop
                # hit exit_price is informational only (ignored without
                # exit_order_type="level").
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "trailing_stop"
                else:
                    exit_reason.iloc[i] = "supertrend_flip_down"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "DEMA": dema,
        "Supertrend_Trend": trend,
        "Supertrend_Trend_at_exit": trend,
        "Supertrend_Lower": lower_band,
        "Supertrend_Upper": upper_band,
        "sl_price": sl_price,
        "trailing_stop_at_exit": trailing_stop_at_exit,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    drawings = entry_exit_markers(df, entries, exits)

    return entries, exits, indicators_dict, drawings

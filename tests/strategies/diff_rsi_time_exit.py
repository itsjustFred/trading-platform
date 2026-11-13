"""RSI mean reversion with a fixed time exit (differential tests).

Rules (re-implemented independently by the reference in
``tests/test_differential_reference_strategies.py``):

- ``rsi``: Wilder smoothing, ``alpha = 1/rsi_len`` with the recursion seeded by
  the first price change (pandas ``ewm(alpha, adjust=False, min_periods=rsi_len)``
  on gains and losses); ``rsi = 100`` when the average loss is 0.
- flat and ``rsi < oversold`` -> entry signal on that bar.
- ``hold_bars`` bars after the entry-signal bar -> exit signal (time exit);
  no entry on the exit bar itself.
- Optional ``stop_pct``: publishes ``sizing_sl_price = close*(1-stop_pct)`` on
  the entry bar. It is a SIZING-ONLY level (never a live stop). ``stop_pct=0``
  publishes nothing, so engines use their documented fallback stop.
"""

STRATEGY_META = {
    "name": "diff_rsi_time_exit",
    "version": "1.0",
    "parameters": {"rsi_len": 5, "oversold": 30.0, "hold_bars": 4, "stop_pct": 0.03},
    "tags": {"type": ["mean_reversion", "rsi", "time_exit"]},
}

import numpy as np
import pandas as pd


def generate_signals(df, params):
    rsi_len = int(params.get("rsi_len", 5))
    oversold = float(params.get("oversold", 30.0))
    hold_bars = int(params.get("hold_bars", 4))
    stop_pct = float(params.get("stop_pct", 0.03))

    close = df["close"]
    change = close.diff()
    gain = change.clip(lower=0.0)
    loss = (-change).clip(lower=0.0)
    alpha = 1.0 / rsi_len
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=rsi_len).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=rsi_len).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss != 0.0, 100.0).where(avg_loss.notna())

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sizing_sl = pd.Series(np.nan, index=df.index)
    entry_bar = None
    for i in range(len(df)):
        if entry_bar is not None:
            if i - entry_bar >= hold_bars:
                exits.iloc[i] = True
                entry_bar = None
        elif rsi.iloc[i] < oversold:
            entries.iloc[i] = True
            entry_bar = i
            if stop_pct > 0:
                sizing_sl.iloc[i] = close.iloc[i] * (1.0 - stop_pct)

    indicators = {"rsi": rsi}
    if stop_pct > 0:
        indicators["sizing_sl_price"] = sizing_sl
    return entries, exits, indicators, []

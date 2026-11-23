"""Donchian/channel breakout with an ATR stop and ATR target (differential tests).

Rules (the independent reference in ``tests/test_differential_reference_strategies.py``
re-implements exactly these in plain Python):

- ``upper[i]``  = max(high[i-entry_len .. i-1])  (prior bars only)
- ``lower[i]``  = min(low[i-exit_len .. i-1])    (prior bars only)
- ``tr[0]`` = high-low; ``tr[i]`` = max(h-l, |h-prev_close|, |l-prev_close|)
  (undefined/NaN when any of the three inputs is NaN)
- ``atr``  = simple mean of the last ``atr_len`` true ranges
- flat and ``close > upper`` and ATR valid -> entry signal; protective stop
  ``close - stop_atr*atr`` and target ``close + tp_atr*atr``
- in position and ``close < lower`` -> exit signal (channel exit)
"""

STRATEGY_META = {
    "name": "diff_donchian",
    "version": "1.0",
    "parameters": {
        "entry_len": 10,
        "exit_len": 5,
        "atr_len": 10,
        "stop_atr": 2.0,
        "tp_atr": 4.0,
    },
    "tags": {"type": ["breakout", "donchian"]},
}

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def generate_signals(df, params):
    entry_len = int(params.get("entry_len", 10))
    exit_len = int(params.get("exit_len", 5))
    atr_len = int(params.get("atr_len", 10))
    stop_atr = float(params.get("stop_atr", 2.0))
    tp_atr = float(params.get("tp_atr", 4.0))

    high, low, close = df["high"], df["low"], df["close"]
    upper = high.rolling(entry_len).max().shift(1)
    lower = low.rolling(exit_len).min().shift(1)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1, skipna=False
    )
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    atr = tr.rolling(atr_len).mean()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    in_position = False
    for i in range(len(df)):
        c = close.iloc[i]
        if in_position:
            if c < lower.iloc[i]:  # NaN compares False
                exits.iloc[i] = True
                in_position = False
        elif c > upper.iloc[i] and not np.isnan(atr.iloc[i]):
            entries.iloc[i] = True
            in_position = True

    indicators = {
        "stop_loss": close - stop_atr * atr,
        "take_profit": close + tp_atr * atr,
    }
    return entries, exits, indicators, []

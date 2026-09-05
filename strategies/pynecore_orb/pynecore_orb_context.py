"""Trade-context helper for the PyneCore SMA-crossover strategy (pynecore_orb).

Naming integrity (Cleanup #35): the strategy is an SMA crossover with an ATR
bracket — there is no opening range. ``fast_length`` is the fast SMA length.

Settlement S16: the PyneCore script brackets from ``position_avg_price``
(AND anchors TP/SL at the fill price); this context helper historically
anchored at the per-bar open AND folded ``sl_mult`` into the TP distance
(``open + atr * sl_mult * tp_mult``), which diverges from the script's
``position_avg_price + atr * tp_mult`` whenever ``sl_mult != 1``. The series
below use the script's formula (no ``sl_mult`` in the TP distance) and are
documented as per-bar-open approximations of the fill-price anchor.
"""

import pandas as pd


def build_trade_context(df: pd.DataFrame, params: dict | None = None) -> dict[str, pd.Series]:
    params = params or {}
    fast_length = int(params.get("fast_length", params.get("or_length", 5)))
    ema_period = int(params.get("ema_period", 200))
    atr_period = int(params.get("atr_period", 14))
    tp_mult = float(params.get("tp_mult", 2.0))
    sl_mult = float(params.get("sl_mult", 1.0))

    close = df["close"]
    open_price = df["open"]
    high = df["high"]
    low = df["low"]

    ema_val = close.ewm(span=ema_period, adjust=False).mean()
    fast_sma = close.rolling(fast_length).mean()
    slow_sma = close.rolling(fast_length * 4).mean()

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    # Per-bar-open approximation of the script's position_avg_price anchor.
    # TP distance follows the script: atr * tp_mult (NOT scaled by sl_mult).
    return {
        "Fast_SMA": fast_sma,
        "Slow_SMA": slow_sma,
        "EMA": ema_val,
        "ATR": atr,
        "sl_price_long": open_price - atr * sl_mult,
        "sl_price_short": open_price + atr * sl_mult,
        "tp_price_long": open_price + atr * tp_mult,
        "tp_price_short": open_price - atr * tp_mult,
        "sl_offset_at_exit": atr * sl_mult,
        "tp_offset_at_exit": atr * tp_mult,
    }

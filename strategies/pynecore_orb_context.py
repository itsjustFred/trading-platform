import pandas as pd


def build_trade_context(df: pd.DataFrame, params: dict | None = None) -> dict[str, pd.Series]:
    params = params or {}
    or_length = int(params.get("or_length", 5))
    ema_period = int(params.get("ema_period", 200))
    atr_period = int(params.get("atr_period", 14))
    tp_mult = float(params.get("tp_mult", 2.0))
    sl_mult = float(params.get("sl_mult", 1.0))

    close = df["close"]
    open_price = df["open"]
    high = df["high"]
    low = df["low"]

    ema_val = close.ewm(span=ema_period, adjust=False).mean()
    fast_sma = close.rolling(or_length).mean()
    slow_sma = close.rolling(or_length * 4).mean()

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

    return {
        "Fast_SMA": fast_sma,
        "Slow_SMA": slow_sma,
        "EMA": ema_val,
        "ATR": atr,
        "sl_price_long": open_price - atr * sl_mult,
        "sl_price_short": open_price + atr * sl_mult,
        "tp_price_long": open_price + atr * sl_mult * tp_mult,
        "tp_price_short": open_price - atr * sl_mult * tp_mult,
        "sl_offset_at_exit": atr * sl_mult,
        "tp_offset_at_exit": atr * sl_mult * tp_mult,
    }

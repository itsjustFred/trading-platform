import pandas as pd


def build_trade_context(df: pd.DataFrame, params: dict | None = None) -> dict[str, pd.Series]:
    params = params or {}
    fast_len = int(params.get("fast_len", 10))
    slow_len = int(params.get("slow_len", 30))
    atr_period = int(params.get("atr_period", 14))
    sl_mult = float(params.get("sl_mult", 2.0))
    close = df["close"]
    open_price = df["open"]
    high = df["high"]
    low = df["low"]
    fast_sma = close.rolling(fast_len).mean()
    slow_sma = close.rolling(slow_len).mean()
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
    crossunder = (fast_sma < slow_sma) & (fast_sma.shift(1) >= slow_sma.shift(1))
    exit_reason = pd.Series(index=df.index, dtype="object")
    exit_reason.loc[crossunder] = "crossunder"
    return {
        "SMA_Fast": fast_sma,
        "SMA_Slow": slow_sma,
        "SMA_Fast_at_exit": fast_sma,
        "SMA_Slow_at_exit": slow_sma,
        "ATR": atr,
        "sl_price_long": open_price - atr * sl_mult,
        "sl_offset_at_exit": atr * sl_mult,
        "exit_reason": exit_reason,
    }

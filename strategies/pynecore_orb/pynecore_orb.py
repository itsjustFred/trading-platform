"""
@pyne
SMA Crossover Strategy (PyneCore).
Fast/Slow SMA crossover with ATR-based take-profit / stop-loss.
NOTE: despite the original 'ORB' naming, this implements an SMA crossover
(fast = or_length, slow = or_length * 4), not an Opening Range Breakout.
"""

from pynecore import Series
from pynecore.lib import script, input, strategy, ta, plot, color


STRATEGY_META = {
    "name": "SMA Crossover (PyneCore)",
    "version": "1.0.0",
    "tags": {
        "type": ["trend-following"],
        "source": "zenomtrader-discord",
        "link": "",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}


@script.strategy(
    title="SMA Crossover (PyneCore)",
    shorttitle="SMA-ORB",
    overlay=True,
    default_qty_type=strategy.percent_of_equity,
    default_qty_value=100,
    initial_capital=100000,
)
def main(
    src: Series[float] = input.source("close", title="Source"),
    or_length: int = input.int(5, title="Opening Range Bars"),
    ema_period: int = input.int(200, title="EMA Trend Filter"),
    atr_period: int = input.int(14, title="ATR Period"),
    tp_mult: float = input.float(2.0, title="Take Profit ATR Mult"),
    sl_mult: float = input.float(1.0, title="Stop Loss ATR Mult"),
    use_ema_filter: bool = input.bool(True, title="Use EMA Filter"),
):
    ema_val = ta.ema(src, ema_period)
    atr_val = ta.atr(atr_period)
    fast_sma = ta.sma(src, or_length)
    slow_sma = ta.sma(src, or_length * 4)

    long_cond = ta.crossover(fast_sma, slow_sma) and (not use_ema_filter or src > ema_val)
    short_cond = ta.crossunder(fast_sma, slow_sma) and (not use_ema_filter or src < ema_val)

    if long_cond and strategy.position_size <= 0:
        strategy.entry("Long", strategy.long)

    if short_cond and strategy.position_size >= 0:
        strategy.entry("Short", strategy.short)

    if strategy.position_size > 0 and atr_val:
        tp_price = strategy.position_avg_price + atr_val * tp_mult
        sl_price = strategy.position_avg_price - atr_val * sl_mult
        strategy.exit("Long TP/SL", "Long", limit=tp_price, stop=sl_price)

    if strategy.position_size < 0 and atr_val:
        tp_price = strategy.position_avg_price - atr_val * tp_mult
        sl_price = strategy.position_avg_price + atr_val * sl_mult
        strategy.exit("Short TP/SL", "Short", limit=tp_price, stop=sl_price)

    plot(ema_val, title="EMA", color=color.blue)

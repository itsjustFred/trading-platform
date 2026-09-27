"""
@pyne
SMA Crossover — PyneCore-Format Strategie.
Einfache Trend-Following Strategie basierend auf Moving Average Crossover.
"""

from pynecore import Series
from pynecore.lib import color, input, plot, script, strategy, ta

STRATEGY_META = {
    "name": "SMA Crossover (PyneCore)",
    "version": "1.0.0",
    "tags": {
        "type": ["trend-following"],
        "source": "internal",
        "link": "",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}


@script.strategy(
    title="SMA Crossover",
    shorttitle="SMA X",
    overlay=True,
    default_qty_type=strategy.percent_of_equity,
    default_qty_value=100,
    initial_capital=100000,
)
def main(
    src: Series[float] = input.source("close", title="Source"),
    fast_len: int = input.int(10, title="Fast SMA"),
    slow_len: int = input.int(30, title="Slow SMA"),
    atr_period: int = input.int(14, title="ATR Period"),
    sl_mult: float = input.float(2.0, title="Stop Loss ATR Mult"),
):
    fast_sma = ta.sma(src, fast_len)
    slow_sma = ta.sma(src, slow_len)
    atr_val = ta.atr(atr_period)

    if ta.crossover(fast_sma, slow_sma):
        strategy.entry("Long", strategy.long)

    if strategy.position_size > 0 and atr_val:
        sl_price = strategy.position_avg_price - atr_val * sl_mult
        strategy.exit("Long SL", "Long", stop=sl_price)

    if ta.crossunder(fast_sma, slow_sma):
        strategy.close("Long")

    plot(fast_sma, title="Fast SMA", color=color.blue)
    plot(slow_sma, title="Slow SMA", color=color.red)

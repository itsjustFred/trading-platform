"""Long-only SMA(fast)/SMA(slow) crossover with tie-exact comparisons (differential tests).

``contract_sma`` compares float rolling means; on real quotes ``fast == slow`` is
common (both are means of the same quantised prices) and pandas' incremental rolling
mean then disagrees with a plain re-summation by an ulp, flipping the cross bar. This
variant keeps the SMA semantics but makes every comparison exact: prices are scaled to
integer ticks (``price_scale``) and the means are compared as
``sum_fast * slow_len  vs  sum_slow * fast_len`` (integer arithmetic in float64).

- entry: fast > slow now and fast <= slow on the previous bar
- exit : fast < slow now and fast >= slow on the previous bar
- a window that contains a NaN has no mean, so no cross can involve it
"""

STRATEGY_META = {
    "name": "diff_sma_cross",
    "version": "1.0",
    "parameters": {"fast_length": 5, "slow_length": 20, "price_scale": 100000},
    "tags": {"type": ["sma", "crossover"]},
}


def generate_signals(df, params):
    fast_len = int(params.get("fast_length", 5))
    slow_len = int(params.get("slow_length", 20))
    scale = float(params.get("price_scale", 100000))

    ticks = (df["close"] * scale).round()
    fast_sum = ticks.rolling(fast_len).sum()
    slow_sum = ticks.rolling(slow_len).sum()
    diff = fast_sum * slow_len - slow_sum * fast_len  # sign(fast mean - slow mean), exact
    prev = diff.shift(1)

    entries = (diff > 0) & (prev <= 0)
    exits = (diff < 0) & (prev >= 0)
    return entries, exits, {"fast_minus_slow": diff}, []

STRATEGY_META = {'name': 'Sample EMA Cross',
 'version': '1.0.0',
 'parameters': {'fast_length': {'type': 'int',
                                'default': 9,
                                'min': 1,
                                'description': 'Fast EMA period (short term trend)'},
                'slow_length': {'type': 'int',
                                'default': 21,
                                'min': 1,
                                'description': 'Slow EMA period (long term trend)'}},
 'tags': {'type': ['trend-following',
                   'ema',
                   'crossover',
                   'moving-average',
                   'golden-cross',
                   'death-cross']}}



def generate_signals(df, params):
    """
    Generate signals for EMA Crossover strategy.
    Entry: Fast EMA crosses above Slow EMA (Golden Cross)
    Exit: Fast EMA crosses below Slow EMA (Death Cross)

    Returns: entries, exits, indicators_dict, drawings
    """
    # Robust parameter extraction handling nested dicts from STRATEGY_META
    def _get_param(p, key, default):
        if p is None:
            return default
        val = p.get(key, default)
        # Handle case where params contains metadata dict instead of values
        if isinstance(val, dict):
            return val.get('default', default)
        return val

    # Extract EMA lengths with defaults
    fast_length = int(_get_param(params, 'fast_length', 9))
    slow_length = int(_get_param(params, 'slow_length', 21))

    # Calculate EMAs using pandas ewm
    close = df['close']
    fast_ema = close.ewm(span=fast_length, adjust=False).mean()
    slow_ema = close.ewm(span=slow_length, adjust=False).mean()

    # Crossover logic: current cross vs previous bar
    # Entry: fast crosses above slow
    entries = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
    # Exit: fast crosses below slow
    exits = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

    # Clean NaN values
    entries = entries.fillna(False)
    exits = exits.fillna(False)

    # Indicators dict with all logic-relevant values (sl_price=None per Ground Truth)
    indicators_dict = {
        'fast_ema': fast_ema,
        'slow_ema': slow_ema,
        'fast_length': fast_length,
        'slow_length': slow_length,
        'sl_price': None  # No stop loss implemented in original strategy
    }

    # Optional drawings - keep contract-compatible empty list when unused
    drawings = []

    return entries, exits, indicators_dict, drawings

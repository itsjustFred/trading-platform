STRATEGY_META = {'name': 'example_sma',
 'version': '1.0',
 'parameters': {'fast_length': 9, 'slow_length': 21},
 'tags': {'type': ['ema', 'crossover', 'trend', 'moving_average']}}

import pandas as pd

def generate_signals(df, params):
    fast_length = params.get('fast_length', 9)
    slow_length = params.get('slow_length', 21)
    
    close = df['close']
    
    fast = close.ewm(span=fast_length, adjust=False).mean()
    slow = close.ewm(span=slow_length, adjust=False).mean()
    
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    
    indicators_dict = {
        'fast': fast,
        'slow': slow
    }
    
    drawings = []
    
    return entries, exits, indicators_dict, drawings

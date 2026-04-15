STRATEGY_META = {'name': 'lab_generated_strategy',
 'version': '1.0.0',
 'parameters': {'fast_length': 9, 'slow_length': 21},
 'tags': {'type': ['trend_following', 'ema_cross', 'crossover', 'moving_average', 'vectorbt']}}

import pandas as pd
import numpy as np
from typing import Dict, List, Any, Tuple

def generate_signals(df: pd.DataFrame, params: dict) -> Tuple[pd.Series, pd.Series, Dict[str, pd.Series], List[Dict[str, Any]]]:
    """
    EMA Crossover Strategy.
    Entry: Fast EMA (9) crosses above Slow EMA (21)
    Exit: Fast EMA (9) crosses below Slow EMA (21)
    """
    fast_length = params.get('fast_length', 9)
    slow_length = params.get('slow_length', 21)
    
    close = df['close']
    fast_ema = close.ewm(span=fast_length, adjust=False).mean()
    slow_ema = close.ewm(span=slow_length, adjust=False).mean()
    
    prev_fast = fast_ema.shift(1)
    prev_slow = slow_ema.shift(1)
    
    entries = (fast_ema > slow_ema) & (prev_fast <= prev_slow)
    exits = (fast_ema < slow_ema) & (prev_fast >= prev_slow)
    
    indicators_dict = {
        'fast_ema': fast_ema,
        'slow_ema': slow_ema,
    }
    
    drawings = []
    
    return entries, exits, indicators_dict, drawings

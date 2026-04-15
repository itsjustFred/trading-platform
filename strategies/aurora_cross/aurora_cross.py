"""Auto-generated deterministic vectorbt strategy from StrategyIR."""

from __future__ import annotations

import pandas as pd

STRATEGY_META = {'name': 'Aurora Cross',
 'version': '1.0.0',
 'parameters': {'fast_period': 9, 'slow_period': 21},
 'tags': {'labels': ['trend_following', 'ema_crossover', 'impulse_cross']}}


def _resolve_window(params: dict, key: str, default: int) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(1, value)


def _cross_over(left: pd.Series, right: pd.Series) -> pd.Series:
    return ((left > right) & (left.shift(1) <= right.shift(1))).fillna(False)


def _cross_under(left: pd.Series, right: pd.Series) -> pd.Series:
    return ((left < right) & (left.shift(1) >= right.shift(1))).fillna(False)


def _calc_rsi(series: pd.Series, *, window: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def generate_signals(df: pd.DataFrame, params: dict):
    if "close" not in df.columns:
        raise ValueError("Input DataFrame must include 'close' column.")

    close = pd.to_numeric(df["close"], errors="coerce")
    indicators_dict: dict[str, pd.Series] = {}
    window_fast_ema = _resolve_window(params, 'window', 14)
    indicator_fast_ema = close.ewm(span=window_fast_ema, adjust=False).mean()
    indicators_dict['Fast EMA'] = indicator_fast_ema
    window_preisdaten = _resolve_window(params, 'window', 14)
    indicator_preisdaten = close.rolling(window=window_preisdaten, min_periods=window_preisdaten).mean()
    indicators_dict['Preisdaten'] = indicator_preisdaten
    window_slow_ema = _resolve_window(params, 'window', 14)
    indicator_slow_ema = close.ewm(span=window_slow_ema, adjust=False).mean()
    indicators_dict['Slow EMA'] = indicator_slow_ema

    entry_rules: list[pd.Series] = []
    entry_rules.append((_cross_over(indicator_fast_ema, indicator_preisdaten)).fillna(False))
    entry_rules.append(((indicator_fast_ema > indicator_preisdaten)).fillna(False))
    entry_rules.append(((indicator_fast_ema > indicator_preisdaten)).fillna(False))
    entry_rules.append(((indicator_fast_ema > indicator_preisdaten)).fillna(False))

    entries = entry_rules[0]
    for rule in entry_rules[1:]:
        entries = entries & rule
    entries = entries.fillna(False).astype(bool)

    exit_rules: list[pd.Series] = []
    exit_rules.append((_cross_under(indicator_fast_ema, indicator_preisdaten)).fillna(False))
    exit_rules.append(((indicator_fast_ema < indicator_preisdaten)).fillna(False))
    exit_rules.append(((indicator_fast_ema < indicator_preisdaten)).fillna(False))
    exit_rules.append(((indicator_fast_ema < indicator_preisdaten)).fillna(False))

    exits = exit_rules[0]
    for rule in exit_rules[1:]:
        exits = exits | rule
    exits = exits.fillna(False).astype(bool)

    drawings: list[dict[str, object]] = []
    if 'timestamp' in df.columns:
        entry_times = df.loc[entries, 'timestamp']
        for timestamp in entry_times.head(25):
            drawings.append(
                {
                    'type': 'marker',
                    'time': timestamp,
                    'position': 'belowBar',
                    'shape': 'arrowUp',
                    'color': '#16a34a',
                    'text': 'Entry',
                }
            )

        exit_times = df.loc[exits, 'timestamp']
        for timestamp in exit_times.head(25):
            drawings.append(
                {
                    'type': 'marker',
                    'time': timestamp,
                    'position': 'aboveBar',
                    'shape': 'arrowDown',
                    'color': '#dc2626',
                    'text': 'Exit',
                }
            )

    if drawings:
        return entries, exits, indicators_dict, drawings
    return entries, exits, indicators_dict

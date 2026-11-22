"""
Project-usable core port of the Pine Script strategy "Custom Swings + Supertrend".

Source file: 'Custom Swings + Supertrend.txt' (Pine Script v5 despite the .txt suffix).

This module intentionally ports the strategy's portable core:
- Supertrend flip entries
- EMA direction filter
- Supertrend-band stop placement for risk-based sizing
- optional fixed-RRR exits
- breakeven handling

It does not attempt a full 1:1 reproduction of the original Pine source's
stateful swing/BOS, MTF `request.security(...)`, session gating, or partial-exit
management. Those differences are documented in the accompanying README.
"""

STRATEGY_META = {
    "name": "Custom Swings + Supertrend",
    "version": "1.0.0",
    "tags": {
        "type": ["trend-following"],
        "source": "Pine Script v5 — DTFX Structure Swings + Supertrend Strategie",
        "asset_class": ["forex", "equities", "futures"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd

from shared.indicators import presets


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SIGNAL GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_signals(
    df: pd.DataFrame,
    st_period: int = 12,
    st_multiplier: float = 3.0,
    ema_fast: int = 21,
    ema_slow: int = 120,
    ema_filter_enabled: bool = True,
    use_rrr: bool = False,
    risk_reward: float = 2.0,
    use_breakeven: bool = True,
    breakeven_r: float = 1.0,
    use_trailing_sl: bool = False,
    trade_direction: str = "both",
) -> tuple:
    """
    Generate Long/Short signals for the Custom Swings + Supertrend strategy.

    Parameters
    ----------
    st_period : int
        ATR period for Supertrend calculation (Pine default: 12).
    st_multiplier : float
        ATR multiplier for Supertrend bands (Pine default: 3.0).
    ema_fast : int
        Fast EMA period for the EMA cross filter (Pine default: 21).
    ema_slow : int
        Slow EMA period for the EMA cross filter (Pine default: 120).
    ema_filter_enabled : bool
        When True, apply EMA cross direction filter. Long only when fast < slow
        at entry (pullback into EMA); Short only when fast > slow (inverse).
    use_rrr : bool
        If True, use fixed risk-reward ratio (risk_reward) as Take-Profit target.
        If False, exit on opposite Supertrend flip only (no fixed TP).
    risk_reward : float
        Risk-Reward ratio for TP when use_rrr=True (Pine default: 2.0).
    use_breakeven : bool
        If True, move SL to breakeven when price reaches entry ± breakevenR × R.
    breakeven_r : float
        Breakeven trigger as multiple of initial stop distance (Pine default: 1.0).
    use_trailing_sl : bool
        If True, trail the SL along the Supertrend band after TP1 hit.
    trade_direction : str
        "both"  — Long and Short trades (default)
        "long"  — Long trades only
        "short" — Short trades only

    Returns
    -------
    entries        : pd.Series[bool]  — True on bar where trade is entered
    exits          : pd.Series[bool]  — True on bar where trade is exited
    indicators_dict: dict             — All logic-relevant indicators
    drawings       : list             — Custom drawings (empty for this strategy)
    """
    # Keep compatibility with older repo tests/helpers that still call
    # generate_signals(df, params_dict) positionally.
    if isinstance(st_period, dict):
        params = st_period
        st_period = int(params.get("st_period", 12))
        st_multiplier = float(params.get("st_multiplier", st_multiplier))
        ema_fast = int(params.get("ema_fast", ema_fast))
        ema_slow = int(params.get("ema_slow", ema_slow))
        ema_filter_enabled = _coerce_bool(params.get("ema_filter_enabled", ema_filter_enabled))
        use_rrr = _coerce_bool(params.get("use_rrr", use_rrr))
        risk_reward = float(params.get("risk_reward", risk_reward))
        use_breakeven = _coerce_bool(params.get("use_breakeven", use_breakeven))
        breakeven_r = float(params.get("breakeven_r", breakeven_r))
        use_trailing_sl = _coerce_bool(params.get("use_trailing_sl", use_trailing_sl))
        trade_direction = str(params.get("trade_direction", trade_direction))

    trade_direction = str(trade_direction).strip().lower()

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    idx = df.index
    n = len(close)

    # ── Indicators ────────────────────────────────────────────────────────────
    st = presets.supertrend_custom_swings(
        df["high"], df["low"], df["close"], st_period, st_multiplier
    )
    trend_arr = st.trend.to_numpy(dtype=float)
    up_arr = st.lower_band.to_numpy(dtype=float)
    dn_arr = st.upper_band.to_numpy(dtype=float)
    atr_arr = st.atr.to_numpy(dtype=float)

    ema_fast_s = presets.ema_pine_ta(df["close"], ema_fast).to_numpy(dtype=float)
    ema_slow_s = presets.ema_pine_ta(df["close"], ema_slow).to_numpy(dtype=float)

    # ── Pre-compute Supertrend flip signals ───────────────────────────────────
    buy_signal = np.zeros(n, dtype=bool)   # trend flips UP
    sell_signal = np.zeros(n, dtype=bool)  # trend flips DOWN
    for i in range(1, n):
        if trend_arr[i] == 1 and trend_arr[i - 1] == -1:
            buy_signal[i] = True
        elif trend_arr[i] == -1 and trend_arr[i - 1] == 1:
            sell_signal[i] = True

    # ── Result arrays ─────────────────────────────────────────────────────────
    entries_arr = np.zeros(n, dtype=bool)
    exits_arr = np.zeros(n, dtype=bool)
    direction_arr = np.full(n, "", dtype=object)
    sl_price_arr = np.full(n, np.nan)
    tp_price_arr = np.full(n, np.nan)
    atr_value_arr = np.full(n, np.nan)
    trailing_stop_arr = np.full(n, np.nan)
    exit_price_arr = np.full(n, np.nan)
    exit_reason_arr = np.full(n, None, dtype=object)

    # ── Warm-up: need enough bars for all indicators ──────────────────────────
    warmup = max(st_period + 1, ema_slow + 1)

    # ── Trade-state variables ─────────────────────────────────────────────────
    in_trade = False
    trade_dir = ""       # "LONG" or "SHORT"
    entry_price = np.nan
    current_sl = np.nan
    initial_risk = np.nan
    tp_target = np.nan
    tp_hit = False       # TP1 hit flag (used for trailing SL)

    for i in range(warmup, n):
        if not in_trade:
            # ── Check for Long entry ──────────────────────────────────────────
            if trade_direction in ("both", "long") and buy_signal[i]:
                ema_ok = (not ema_filter_enabled) or (ema_fast_s[i] < ema_slow_s[i])
                if ema_ok and atr_arr[i] > 0:
                    entries_arr[i] = True
                    in_trade = True
                    trade_dir = "LONG"
                    entry_price = close[i]
                    current_sl = up_arr[i]          # Supertrend support band
                    initial_risk = abs(entry_price - current_sl)
                    if initial_risk <= 0:
                        initial_risk = entry_price * 0.01
                        current_sl = entry_price - initial_risk
                    tp_target = entry_price + risk_reward * initial_risk if use_rrr else np.nan
                    tp_hit = False
                    direction_arr[i] = "LONG"
                    sl_price_arr[i] = round(current_sl, 6)
                    atr_value_arr[i] = round(atr_arr[i], 6)
                    tp_price_arr[i] = round(tp_target, 6) if not np.isnan(tp_target) else np.nan

            # ── Check for Short entry ─────────────────────────────────────────
            elif trade_direction in ("both", "short") and sell_signal[i]:
                ema_ok = (not ema_filter_enabled) or (ema_fast_s[i] > ema_slow_s[i])
                if ema_ok and atr_arr[i] > 0:
                    entries_arr[i] = True
                    in_trade = True
                    trade_dir = "SHORT"
                    entry_price = close[i]
                    current_sl = dn_arr[i]          # Supertrend resistance band
                    initial_risk = abs(current_sl - entry_price)
                    if initial_risk <= 0:
                        initial_risk = entry_price * 0.01
                        current_sl = entry_price + initial_risk
                    tp_target = entry_price - risk_reward * initial_risk if use_rrr else np.nan
                    tp_hit = False
                    direction_arr[i] = "SHORT"
                    sl_price_arr[i] = round(current_sl, 6)
                    atr_value_arr[i] = round(atr_arr[i], 6)
                    tp_price_arr[i] = round(tp_target, 6) if not np.isnan(tp_target) else np.nan

        else:
            # ── Manage open trade ─────────────────────────────────────────────
            exited = False
            reason = None
            ep = entry_price
            sl = current_sl

            if trade_dir == "LONG":
                # Update trailing stop
                if use_trailing_sl and tp_hit:
                    new_sl = up_arr[i]  # trail with ST support
                    if new_sl > sl:
                        sl = new_sl

                # Breakeven
                if use_breakeven and initial_risk > 0:
                    be_trigger = ep + breakeven_r * initial_risk
                    if high[i] >= be_trigger and sl < ep:
                        sl = ep  # move SL to entry

                # Check TP1 hit (for trailing activation)
                if use_rrr and not np.isnan(tp_target) and not tp_hit:
                    if high[i] >= tp_target:
                        tp_hit = True

                # Check stop hit
                if low[i] <= sl:
                    exited = True
                    ep_exit = sl
                    reason = "stop_loss"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Check TP hit (fixed RRR exit)
                elif use_rrr and not np.isnan(tp_target) and high[i] >= tp_target:
                    exited = True
                    ep_exit = tp_target
                    reason = "take_profit"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Check Supertrend flip to DOWN
                elif sell_signal[i]:
                    exited = True
                    ep_exit = close[i]
                    reason = "supertrend_flip"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Update current stop
                current_sl = sl

            else:  # SHORT
                # Update trailing stop
                if use_trailing_sl and tp_hit:
                    new_sl = dn_arr[i]  # trail with ST resistance
                    if new_sl < sl:
                        sl = new_sl

                # Breakeven
                if use_breakeven and initial_risk > 0:
                    be_trigger = ep - breakeven_r * initial_risk
                    if low[i] <= be_trigger and sl > ep:
                        sl = ep  # move SL to entry

                # Check TP1 hit
                if use_rrr and not np.isnan(tp_target) and not tp_hit:
                    if low[i] <= tp_target:
                        tp_hit = True

                # Check stop hit
                if high[i] >= sl:
                    exited = True
                    ep_exit = sl
                    reason = "stop_loss"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Check TP hit
                elif use_rrr and not np.isnan(tp_target) and low[i] <= tp_target:
                    exited = True
                    ep_exit = tp_target
                    reason = "take_profit"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Check Supertrend flip to UP
                elif buy_signal[i]:
                    exited = True
                    ep_exit = close[i]
                    reason = "supertrend_flip"
                    exit_price_arr[i] = round(ep_exit, 6)

                # Update current stop
                current_sl = sl

            if exited:
                exits_arr[i] = True
                exit_reason_arr[i] = reason
                trailing_stop_arr[i] = round(current_sl, 6)
                in_trade = False
                trade_dir = ""
                entry_price = np.nan
                current_sl = np.nan
                initial_risk = np.nan
                tp_target = np.nan
                tp_hit = False
            else:
                # Keep current stop updated in the stop_loss series
                # (adapter reads stop_loss at entry_idx only, so this is for context)
                trailing_stop_arr[i] = round(current_sl, 6)

    # ── Build pandas Series ───────────────────────────────────────────────────
    entries = pd.Series(entries_arr, index=idx)
    exits = pd.Series(exits_arr, index=idx)

    trend_s = pd.Series(trend_arr.astype(float), index=idx)
    up_s = pd.Series(up_arr, index=idx)
    dn_s = pd.Series(dn_arr, index=idx)
    atr_s = pd.Series(atr_arr, index=idx)
    ema_fast_series = pd.Series(ema_fast_s, index=idx)
    ema_slow_series = pd.Series(ema_slow_s, index=idx)
    sl_series = pd.Series(sl_price_arr, index=idx)
    tp_series = pd.Series(tp_price_arr, index=idx)
    atr_val_series = pd.Series(atr_value_arr, index=idx)
    trailing_s = pd.Series(trailing_stop_arr, index=idx)
    dir_series = pd.Series(direction_arr, index=idx)
    exit_price_s = pd.Series(exit_price_arr, index=idx)
    exit_reason_s = pd.Series(exit_reason_arr, index=idx)

    indicators_dict = {
        # ── Core logic indicators (adapter uses these for risk sizing / meta) ─
        "stop_loss":             sl_series,           # reserved key → sl_price in meta
        "atr":                   atr_s,               # reserved key → ATR-based sizing
        "atr_value":             atr_val_series,       # ATR value at entry
        "tp_price":              tp_series,            # Take-profit level
        "trailing_stop_at_exit": trailing_s,           # Trailing SL at exit bar
        "direction":             dir_series,           # "LONG" / "SHORT"
        "exit_price":            exit_price_s,         # Exact exit price
        "exit_reason":           exit_reason_s,        # Why the trade exited
        # ── Display indicators ───────────────────────────────────────────────
        "Supertrend_Trend":      trend_s,
        "Supertrend_Up_Band":    up_s,
        "Supertrend_Dn_Band":    dn_s,
        "EMA_Fast":              ema_fast_series,
        "EMA_Slow":              ema_slow_series,
    }

    drawings: list = []

    return entries, exits, indicators_dict, drawings

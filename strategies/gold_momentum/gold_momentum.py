"""
Strategy: Gold Momentum (continuous-score, single-symbol proxy)

Single-symbol expression of desenyon's GoldMomentumStrategy
(gold_momentum_strategy.py): the source trades a CONTINUOUS momentum
score (trend + MACD + multi-timeframe momentum + RSI + Bollinger
position, clipped to [-1, 1]) with volatility-scaled, regime-adjusted
position sizing. Long-only proxy: enter when the score is positive,
exit when it turns non-positive.

Source fidelity
---------------
- MACD(12, 26, 9), exponential.
- RSI(14) with Wilder smoothing (MovingAverageType.Wilders).
- ATR(14) with Wilder smoothing (sizing info only, no stop by default).
- Bollinger(20, 2) middle/upper/lower bands.
- Score weights exactly as ``CalculateMomentumScore``: EMA trend
  +-0.25, MACD signal +-0.20, MACD histogram +0.10, short+medium
  momentum +-0.25, RSI bands (+0.10 / +0.15 / -0.10), Bollinger
  position (+0.05 / +0.10).

Opt-in extras (default OFF - NOT source behavior)
-------------------------------------------------
- ``use_atr_stop``: ATR-based protection stop (adapter retrofit).

Scope notes (documented, not implemented)
-----------------------------------------
The source is a TWO-LEG system (GLD + TLT) with volatility targeting
and regime multipliers; none of that is single-symbol-expressible:
- dropped TLT risk-off leg (remainder-to-treasuries allocation);
- dropped volatility scaling (15 % vol target) - sizing is engine-side;
- dropped regime multipliers (bullish_low_vol 1.2 / bullish_high_vol
  0.8 / bearish_low_vol 0.6 / bearish_high_vol 0.4 / neutral 0.7);
- dropped 40 % max-position cap (engine caps apply instead);
- hourly rolling windows are expressed in bars of the run timeframe.
"""

STRATEGY_META = {
    "name": "Gold Momentum",
    "version": "1.1.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/gold_momentum_strategy.py",
    "notes": (
        "Single-symbol proxy of desenyon's GoldMomentumStrategy. "
        "Source-exact: MACD 12/26/9, Wilder RSI14, Wilder ATR14, "
        "BB(20, 2), continuous momentum score with source weights. "
        "Dropped (not single-symbol-expressible): TLT leg, "
        "volatility targeting, regime multipliers, 40 % position cap. "
        "ATR stop is opt-in only (default OFF)."
    ),
    "tags": {
        "type": ["momentum"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/quantconnect-strategies",
        "asset_class": ["commodities", "metals"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def generate_signals(df: pd.DataFrame, params: dict):
    macd_fast = params.get("macd_fast", 12)
    macd_slow = params.get("macd_slow", 26)
    macd_signal_period = params.get("macd_signal", 9)
    rsi_period = params.get("rsi_period", 14)
    bb_period = params.get("bb_period", 20)
    bb_mult = params.get("bb_mult", 2.0)
    momentum_short = params.get("momentum_short", 20)
    momentum_medium = params.get("momentum_medium", 60)
    score_entry = params.get("score_entry", 0.0)
    score_exit = params.get("score_exit", 0.0)
    # Opt-in extra (default OFF - not source behavior).
    use_atr_stop = params.get("use_atr_stop", False)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=macd_signal_period, adjust=False).mean()
    macd_hist = macd_line - signal_line

    rsi = _wilder_rsi(close, rsi_period)
    atr = _wilder_atr(high, low, close, atr_period)

    bb_mid = close.rolling(bb_period).mean()
    bb_std = close.rolling(bb_period).std()
    bb_upper = bb_mid + bb_mult * bb_std
    bb_lower = bb_mid - bb_mult * bb_std

    # Multi-timeframe momentum. The source reads (newest - oldest) / oldest
    # over RollingWindows of 20 / 60 prices, i.e. 19- / 59-bar returns.
    short_mom = close.pct_change(momentum_short - 1)
    medium_mom = close.pct_change(momentum_medium - 1)

    # Continuous momentum score (source: CalculateMomentumScore).
    score = pd.Series(0.0, index=df.index)
    score += np.where(ema_fast > ema_slow, 0.25, -0.25)
    score += np.where(macd_line > signal_line, 0.20, -0.20)
    score += np.where(macd_hist > 0, 0.10, 0.0)
    both_up = (short_mom > 0) & (medium_mom > 0)
    both_down = (short_mom < 0) & (medium_mom < 0)
    score += np.where(both_up, 0.25, np.where(both_down, -0.25, 0.0))
    score += np.where(
        (rsi > 40) & (rsi < 60),
        0.10,
        np.where(rsi < 30, 0.15, np.where(rsi > 70, -0.10, 0.0)),
    )
    score += np.where(
        (close > bb_mid) & (close < bb_upper),
        0.05,
        np.where(close < bb_lower, 0.10, 0.0),
    )
    score = score.clip(-1, 1)

    raw_entries = score > score_entry
    raw_exits = score <= score_exit

    in_trade = False
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    active_stop = np.nan
    warmup = int(max(momentum_medium, macd_slow + macd_signal_period, bb_period, rsi_period))

    for i in range(warmup, len(df)):
        if pd.isna(score.iloc[i]):
            continue
        if not in_trade and bool(raw_entries.iloc[i]):
            in_trade = True
            entries.iloc[i] = True
            if use_atr_stop:
                atr_value = atr.iloc[i]
                if pd.notna(atr_value):
                    active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                    sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = (
                use_atr_stop
                and pd.notna(active_stop)
                and float(low.iloc[i]) <= float(active_stop)
            )
            score_exit_hit = bool(raw_exits.iloc[i])
            if stop_hit or score_exit_hit:
                in_trade = False
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "score_reversal"
                active_stop = np.nan

    indicators_dict = {
        "Momentum_Score": score,
        "Momentum_Score_at_exit": score,
        "MACD_Line": macd_line,
        "MACD_Line_at_exit": macd_line,
        "MACD_Signal": signal_line,
        "MACD_Signal_at_exit": signal_line,
        "MACD_Hist": macd_hist,
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "BB_Mid": bb_mid,
        "BB_Upper": bb_upper,
        "BB_Lower": bb_lower,
        "EMA_Fast": ema_fast,
        "EMA_Slow": ema_slow,
        "ATR": atr,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_times = pd.to_datetime(
            df.loc[entries[entries].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        exit_times = pd.to_datetime(
            df.loc[exits[exits].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        drawings.extend(
            {
                "type": "marker",
                "time": ts.isoformat(),
                "position": "belowBar",
                "shape": "arrowUp",
                "color": "#22c55e",
                "text": "Entry",
            }
            for ts in entry_times.iloc[:30]
        )
        drawings.extend(
            {
                "type": "marker",
                "time": ts.isoformat(),
                "position": "aboveBar",
                "shape": "arrowDown",
                "color": "#ef4444",
                "text": "Exit",
            }
            for ts in exit_times.iloc[:30]
        )

    return entries, exits, indicators_dict, drawings

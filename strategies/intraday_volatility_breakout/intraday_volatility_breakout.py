"""
Intraday Volatility Breakout Strategy

Vectorized conversion of the MQL5 EA IntradayVolatilityBreakout.mq5.

Logic:
- Daily ATR (shifted by 1 day) is forward-filled to the main timeframe.
- Each day an entry-setup is calculated once: open price at entry_time plus/minus
  ATR * ATR_Multiplier gives the breakout levels.
- Regime filter compares short-term ATR vs long-term ATR on daily basis.
- Entry signals fire inside the entry window (entry_time … max_entry_time) when
  the signal price breaks a level.  Only one trade per day is allowed.
- Exits are time-based at exit_time and additionally at the last bar of each day
  (stale-position safeguard).
- sl_price is emitted per entry bar as ATR-based distance from the entry close.
"""

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "intraday_volatility_breakout",
    "version": "1.0.0",
    "parameters": {
        "atr_period": 5,
        "atr_multiplier": 0.25,
        "stop_atr_multiplier": 0.25,
        "use_candle_close": True,
        "confirmation_timeframe": "15m",
        "atr_short_period": 1,
        "atr_long_period": 5,
        "atr_regime_factor": 1.0,
        "regime_filter_mode": "low",
        "trade_direction": "both",
        "reverse_mode": False,
        "fixed_risk_amount": 500.0,
        "entry_time": "16:30",
        "max_entry_time": "18:05",
        "exit_time": "22:55",
        "magic_number": 123456,
    },
    "tags": {
        "type": ["breakout", "intraday"],
        "source": "mql5-conversion",
        "complexity": "medium",
    },
}

_TF_FREQ_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
    "m1": "1min",
    "m5": "5min",
    "m15": "15min",
    "m30": "30min",
    "h1": "1H",
    "h4": "4H",
    "d1": "1D",
    "w1": "1W",
}


def _parse_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def generate_signals(df: pd.DataFrame, params: dict):
    # --- Parameter extraction ------------------------------------------------
    atr_period = int(params.get("atr_period", params.get("ATR_Period", 5)))
    atr_multiplier = float(
        params.get("atr_multiplier", params.get("ATR_Multiplier", 0.25))
    )
    stop_atr_multiplier = float(
        params.get("stop_atr_multiplier", params.get("Stop_ATR_Multiplier", 0.25))
    )
    use_candle_close = bool(
        params.get("use_candle_close", params.get("Use_Candle_Close", True))
    )
    confirmation_timeframe = str(
        params.get(
            "confirmation_timeframe",
            params.get("Confirmation_Timeframe", "15m"),
        )
    )
    atr_short_period = int(
        params.get("atr_short_period", params.get("ATR_Short_Period", 1))
    )
    atr_long_period = int(
        params.get("atr_long_period", params.get("ATR_Long_Period", 5))
    )
    atr_regime_factor = float(
        params.get("atr_regime_factor", params.get("ATR_Regime_Factor", 1.0))
    )
    regime_filter_mode = str(
        params.get(
            "regime_filter_mode",
            params.get("Regime_Filter_Mode", "low"),
        )
    ).lower()
    trade_direction = str(
        params.get("trade_direction", params.get("Trade_Direction", "both"))
    ).lower()
    reverse_mode = bool(
        params.get("reverse_mode", params.get("Reverse_Mode", False))
    )
    entry_time = str(params.get("entry_time", params.get("Entry_Time", "16:30")))
    max_entry_time = str(
        params.get("max_entry_time", params.get("Max_Entry_Time", "18:05"))
    )
    exit_time = str(params.get("exit_time", params.get("Exit_Time", "22:55")))

    close = df["close"]
    open_ = df["open"]
    idx = df.index

    # --- Time features -------------------------------------------------------
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        day = pd.Series(ts.dt.date.values, index=idx)
        minute_of_day = pd.Series(ts.dt.hour * 60 + ts.dt.minute, index=idx)
    elif hasattr(idx, "hour"):
        day = pd.Series(idx.date, index=idx)
        minute_of_day = pd.Series(idx.hour * 60 + idx.minute, index=idx)
    else:
        # Fallback: synthetic day every 100 bars
        day = pd.Series(range(len(df)) // 100, index=idx)
        minute_of_day = pd.Series(range(len(df)), index=idx)

    entry_minute = _parse_minutes(entry_time)
    max_entry_minute = _parse_minutes(max_entry_time)
    exit_minute = _parse_minutes(exit_time)

    # --- Helpers for datetime-aware resampling -------------------------------
    def _has_datetime_index():
        return "timestamp" in df.columns or hasattr(idx, "hour")

    def _to_datetime_index():
        if "timestamp" in df.columns:
            return pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        return idx

    # --- Daily indicators (resample to D1, shift 1, ffill) ------------------
    if _has_datetime_index():
        dt_idx = _to_datetime_index()
        temp_df = pd.DataFrame(
            {"open": open_.values, "high": df["high"].values, "low": df["low"].values, "close": close.values},
            index=dt_idx,
        )
        daily = (
            temp_df.resample("D")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        # Re-align daily values to main timeframe
        def _ffill_daily(daily_series):
            return daily_series.reindex(temp_df.index, method="ffill").values
    else:
        # Fallback: group by synthetic day
        ohlc = pd.DataFrame(
            {"open": open_, "high": df["high"], "low": df["low"], "close": close},
            index=idx,
        )
        daily = ohlc.groupby(day.values).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        )

        def _ffill_daily(daily_series):
            return daily_series.reindex(day.values, method="ffill").values

    prev_close_daily = daily["close"].shift(1)
    tr_daily = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - prev_close_daily).abs(),
            (daily["low"] - prev_close_daily).abs(),
        ],
        axis=1,
    ).max(axis=1)

    daily_atr = tr_daily.rolling(atr_period, min_periods=1).mean().shift(1)
    daily_atr_short = tr_daily.rolling(atr_short_period, min_periods=1).mean().shift(1)
    daily_atr_long = tr_daily.rolling(atr_long_period, min_periods=1).mean().shift(1)

    daily_atr_ffill = pd.Series(_ffill_daily(daily_atr), index=idx)
    daily_atr_short_ffill = pd.Series(_ffill_daily(daily_atr_short), index=idx)
    daily_atr_long_ffill = pd.Series(_ffill_daily(daily_atr_long), index=idx)

    # --- Regime filter -------------------------------------------------------
    threshold = daily_atr_long_ffill * atr_regime_factor
    if regime_filter_mode == "low":
        regime_ok = daily_atr_short_ffill < threshold
    else:
        regime_ok = daily_atr_short_ffill > threshold

    # --- Entry open price (first bar >= entry_time per day) -----------------
    past_entry = minute_of_day >= entry_minute
    entry_open_eligible = open_.where(past_entry)
    # Fix the entry open to the first eligible bar of the day so breakout levels
    # do not drift with subsequent bars.
    entry_open = entry_open_eligible.groupby(day.values).transform("first")

    # --- Breakout levels & stop distance -------------------------------------
    upper_level = entry_open + daily_atr_ffill * atr_multiplier
    lower_level = entry_open - daily_atr_ffill * atr_multiplier
    stop_distance = daily_atr_ffill * stop_atr_multiplier

    # --- Signal price --------------------------------------------------------
    # When Use_Candle_Close is true in MQL5 the EA waits for the close of the
    # configured confirmation timeframe.  In a bar engine we approximate that
    # with the shifted close.  When Use_Candle_Close is false the EA reacts to
    # the current market price (SYMBOL_LAST).  In a bar engine the best proxy
    # for intrabar touches is the high/low of the current bar.
    if use_candle_close:
        conf_freq = _TF_FREQ_MAP.get(confirmation_timeframe, confirmation_timeframe)
        try:
            dt_idx = _to_datetime_index()
            signal_sr = pd.Series(close.values, index=dt_idx)
            conf_close = signal_sr.resample(conf_freq).last().shift(1)
            signal_price_long = conf_close.reindex(signal_sr.index, method="ffill")
            signal_price_long = pd.Series(signal_price_long.values, index=idx)
            signal_price_short = signal_price_long
            # Fallback when data resolution is the same as or coarser than
            # confirmation timeframe (e.g. H1 data with 15m confirmation).
            # In that case the resampled Series only has values at :00 and
            # after shift(1) / reindex everything becomes NaN.
            if signal_price_long.isna().all():
                signal_price_long = close.shift(1)
                signal_price_short = signal_price_long
        except Exception:
            signal_price_long = close.shift(1)
            signal_price_short = signal_price_long
    else:
        signal_price_long = df["high"]
        signal_price_short = df["low"]

    # --- Entry window --------------------------------------------------------
    in_entry_window = (minute_of_day >= entry_minute) & (minute_of_day < max_entry_minute)

    # --- Raw entry candidates ------------------------------------------------
    long_candidates = in_entry_window & regime_ok & (signal_price_long >= upper_level)
    short_candidates = in_entry_window & regime_ok & (signal_price_short <= lower_level)

    # Trade direction
    if trade_direction == "long_only":
        short_candidates = pd.Series(False, index=idx)
    elif trade_direction == "short_only":
        long_candidates = pd.Series(False, index=idx)

    # Reverse mode (flip long / short)
    if reverse_mode:
        long_candidates, short_candidates = short_candidates.copy(), long_candidates.copy()

    # --- One trade per day ---------------------------------------------------
    any_candidate = long_candidates | short_candidates
    # cumsum within each day -> keep only the first True
    day_str = day.astype(str)
    daily_cum = any_candidate.groupby(day_str.values).cumsum()
    first_per_day = daily_cum == 1

    long_entries = long_candidates & first_per_day
    short_entries = short_candidates & first_per_day

    # If both trigger on same bar, long takes precedence (MQL5 check order)
    both_hit = long_entries & short_entries
    short_entries = short_entries & ~both_hit

    # The MQL5 EA evaluates a just-closed confirmation candle at the first tick
    # of the next confirmation bar, then submits a market order immediately.
    # The platform engines execute strategy signals on the next bar open, so
    # candle-close entries are emitted one source bar earlier to land on the
    # same execution timestamp as MT5 for M1/M15 data.
    signal_long_entries = long_entries
    signal_short_entries = short_entries
    if use_candle_close:
        signal_long_entries = long_entries.shift(-1, fill_value=False).astype(bool)
        signal_short_entries = short_entries.shift(-1, fill_value=False).astype(bool)

    entries = signal_long_entries | signal_short_entries

    # --- Direction & sl_price per entry --------------------------------------
    direction = pd.Series(index=idx, dtype=object)
    direction.loc[signal_long_entries] = "LONG"
    direction.loc[signal_short_entries] = "SHORT"

    long_sl_price = (close - stop_distance).where(long_entries)
    short_sl_price = (close + stop_distance).where(short_entries)
    sl_price = long_sl_price.combine_first(short_sl_price)
    if use_candle_close:
        sl_price = sl_price.shift(-1)

    # --- Exits (time exit + EOD safety net) ----------------------------------
    exit_time_mask = minute_of_day >= exit_minute
    # Last bar of each day
    idx_series = pd.Series(idx, index=idx)
    is_last_bar = idx_series.groupby(day_str.values).transform("last") == idx_series
    eod_exit_mask = is_last_bar
    exits = exit_time_mask | eod_exit_mask

    # --- Indicators ----------------------------------------------------------
    indicators_dict = {
        "daily_atr": daily_atr_ffill,
        "upper_level": upper_level,
        "lower_level": lower_level,
        "regime_ok": regime_ok,
        "entry_open": entry_open,
        "direction": direction,
        "sl_price": sl_price,
    }

    # --- Drawings ------------------------------------------------------------
    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_ts = pd.to_datetime(
            df.loc[entries[entries].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        exit_ts = pd.to_datetime(
            df.loc[exits[exits].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        for ts in entry_ts.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Entry",
                }
            )
        for ts in exit_ts.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#ef4444",
                    "text": "Exit",
                }
            )

    return entries, exits, indicators_dict, drawings

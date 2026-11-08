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

import logging
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STRATEGY_META = {
    "name": "intraday_volatility_breakout",
    "version": "1.0.0",
    "engine": "native",
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
        "session_timezone": "",
        "session_timezone_offset_hours": 0,
        "evaluate_on_confirmation_bar": True,
        "magic_number": 123456,
    },
    "tags": {
        "type": ["breakout", "intraday"],
        "source": "mql5-conversion",
        "complexity": "medium",
    },
}

# NOTE: "1W" is deliberately left as pandas' default weekly bucket here. This map feeds an
# in-strategy `resample(...).last().shift(1)` confirmation price, not stored bars, and the
# default (Sunday-labelled, right-closed) bucket makes that signal lag one week more than a
# Monday-start bucket would - conservative, no look-ahead. Moving it to the stored-data
# convention in `data_tools.resample` (Sunday-start, Monday-labelled) would change signal
# timing and therefore backtest results, so it is a separate, explicit decision.
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


def _resolve_session_zone(session_timezone: object, offset_hours: object):
    name = str(session_timezone or "").strip()
    offset = float(offset_hours or 0)
    if name and offset:
        raise ValueError("session_timezone_offset_hours cannot be combined with an IANA timezone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid session_timezone: {name!r}") from exc
    if offset:
        try:
            return timezone(timedelta(hours=offset))
        except (OverflowError, ValueError) as exc:
            raise ValueError("session_timezone_offset_hours must be between -24 and 24") from exc
    return timezone.utc


def _parse_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _normalize_regime_filter_mode(value) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"0", "low", "low_volatility", "low_volatility_filter"}:
        return "low"
    if normalized in {"1", "high", "high_volatility", "high_volatility_filter"}:
        return "high"
    return normalized


def _normalize_trade_direction(value) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"0", "both", "all"}:
        return "both"
    if normalized in {"1", "long", "long_only", "buy"}:
        return "long_only"
    if normalized in {"2", "-1", "short", "short_only", "sell"}:
        return "short_only"
    return normalized


def _mt5_atr(true_range: pd.Series, period: int) -> pd.Series:
    period = max(1, int(period))
    values = pd.to_numeric(true_range, errors="coerce").astype(float)
    if period == 1:
        return values.copy()

    atr = pd.Series(np.nan, index=values.index, name=values.name, dtype=float)
    valid_positions = np.flatnonzero(values.notna().to_numpy())
    if len(valid_positions) < period:
        return atr

    previous_atr = values.iloc[valid_positions[:period]].mean()
    atr.iloc[valid_positions[period - 1]] = previous_atr
    for position in valid_positions[period:]:
        previous_atr = (
            (previous_atr * (period - 1)) + values.iloc[position]
        ) / period
        atr.iloc[position] = previous_atr
    return atr


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
    regime_filter_mode = _normalize_regime_filter_mode(
        params.get(
            "regime_filter_mode",
            params.get("Regime_Filter_Mode", "low"),
        )
    )
    trade_direction = _normalize_trade_direction(
        params.get("trade_direction", params.get("Trade_Direction", "both"))
    )
    reverse_mode = bool(
        params.get("reverse_mode", params.get("Reverse_Mode", False))
    )
    entry_time = str(params.get("entry_time", params.get("Entry_Time", "16:30")))
    max_entry_time = str(
        params.get("max_entry_time", params.get("Max_Entry_Time", "18:05"))
    )
    exit_time = str(params.get("exit_time", params.get("Exit_Time", "22:55")))
    session_timezone = params.get(
        "session_timezone",
        params.get("Session_Timezone", ""),
    )
    session_timezone_offset_hours = params.get(
        "session_timezone_offset_hours",
        params.get("Session_Timezone_Offset_Hours", 0),
    )
    session_zone = _resolve_session_zone(
        session_timezone,
        session_timezone_offset_hours,
    )
    evaluate_on_confirmation_bar = bool(
        params.get(
            "evaluate_on_confirmation_bar",
            params.get("Evaluate_On_Confirmation_Bar", True),
        )
    )

    close = df["close"]
    open_ = df["open"]
    idx = df.index

    def _to_session_time(values):
        converted = pd.to_datetime(values, utc=True, errors="coerce")
        if getattr(converted, "dt", None) is None:
            raise ValueError("timestamp values must be datetime-like")
        return converted.dt.tz_convert(session_zone)

    def _index_to_session_time(values):
        converted = pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors="coerce"))
        if converted.tz is None:
            raise ValueError("datetime index must be timezone-aware")
        return converted.tz_convert(session_zone)

    # --- Time features -------------------------------------------------------
    if "timestamp" in df.columns:
        session_ts = _to_session_time(df["timestamp"])
        day = pd.Series(session_ts.dt.date.values, index=idx)
        minute_of_day = pd.Series(session_ts.dt.hour * 60 + session_ts.dt.minute, index=idx)
    elif isinstance(idx, pd.DatetimeIndex) or pd.api.types.is_datetime64_any_dtype(idx):
        session_idx = _index_to_session_time(idx)
        day = pd.Series(session_idx.date, index=idx)
        minute_of_day = pd.Series(session_idx.hour * 60 + session_idx.minute, index=idx)
    else:
        raise ValueError(
            "intraday_volatility_breakout requires a timestamp column or datetime index"
        )

    entry_minute = _parse_minutes(entry_time)
    max_entry_minute = _parse_minutes(max_entry_time)
    exit_minute = _parse_minutes(exit_time)

    # --- Helpers for datetime-aware resampling -------------------------------
    def _has_datetime_index():
        return "timestamp" in df.columns or hasattr(idx, "hour")

    def _to_datetime_index():
        if "timestamp" in df.columns:
            return _to_session_time(df["timestamp"])
        return _index_to_session_time(idx)

    # --- Daily indicators (resample to D1, shift 1, ffill) ------------------
    daily_ohlcv = params.get("daily_ohlcv")
    if isinstance(daily_ohlcv, pd.DataFrame) and not daily_ohlcv.empty:
        daily_frame = daily_ohlcv.copy()
        daily_frame.columns = [str(col).lower() for col in daily_frame.columns]
        daily_ts = pd.to_datetime(daily_frame.get("timestamp"), utc=True, errors="coerce")
        if daily_ts.isna().all():
            daily_ts = pd.to_datetime(daily_frame.index, utc=True, errors="coerce")
        daily_idx = _index_to_session_time(daily_ts).normalize()
        daily = pd.DataFrame(
            {
                "open": pd.to_numeric(daily_frame["open"], errors="coerce").values,
                "high": pd.to_numeric(daily_frame["high"], errors="coerce").values,
                "low": pd.to_numeric(daily_frame["low"], errors="coerce").values,
                "close": pd.to_numeric(daily_frame["close"], errors="coerce").values,
            },
            index=daily_idx,
        ).dropna()
        daily = daily.groupby(level=0).last().sort_index()
        align_index = _to_datetime_index() if _has_datetime_index() else pd.Index(day.values)

        def _ffill_daily(daily_series):
            return daily_series.reindex(align_index, method="ffill").values
    elif _has_datetime_index():
        dt_idx = _to_datetime_index()
        temp_df = pd.DataFrame(
            {
                "open": open_.values,
                "high": df["high"].values,
                "low": df["low"].values,
                "close": close.values,
            },
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

    daily_atr = _mt5_atr(tr_daily, atr_period).shift(1)
    daily_atr_short = _mt5_atr(tr_daily, atr_short_period).shift(1)
    daily_atr_long = _mt5_atr(tr_daily, atr_long_period).shift(1)

    daily_atr_ffill = pd.Series(_ffill_daily(daily_atr), index=idx)
    daily_atr_short_ffill = pd.Series(_ffill_daily(daily_atr_short), index=idx)
    daily_atr_long_ffill = pd.Series(_ffill_daily(daily_atr_long), index=idx)

    # --- Regime filter -------------------------------------------------------
    threshold = daily_atr_long_ffill * atr_regime_factor
    if regime_filter_mode == "low":
        regime_ok = daily_atr_short_ffill < threshold
    else:
        regime_ok = daily_atr_short_ffill > threshold

    # --- Entry open price (nearest bar to entry_time per day) ---------------
    # MT5 parity: iBarShift(_Symbol, PERIOD_M1, entry_time) with exact=false
    # returns the NEAREST bar when the entry-time bar is missing (data gap),
    # not the first bar at/after entry_time. Verified against MT5 logs
    # (2026-04-28 USATECH: 16:30/16:31 bars missing -> MT5 used the 16:29
    # open while first->= would use 16:32). Ties resolve to the earlier bar.
    # When the 16:30 bar exists this is identical to first->=.
    abs_delta = (minute_of_day - entry_minute).abs()
    is_after = (minute_of_day > entry_minute).astype(int)
    rank_key = abs_delta * 2 + is_after
    best_rank = rank_key.groupby(day.values).transform("min")
    entry_open = (
        open_.where(rank_key == best_rank).groupby(day.values).transform("first")
    )

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
    signal_price_fallback_used = pd.Series(False, index=idx)
    if use_candle_close:
        conf_freq = _TF_FREQ_MAP.get(confirmation_timeframe, confirmation_timeframe)
        try:
            dt_idx = _to_datetime_index()
            signal_sr = pd.Series(close.values, index=dt_idx)
            conf_close = signal_sr.resample(conf_freq).last().shift(1)
            signal_price_long = conf_close.reindex(signal_sr.index, method="ffill")
            signal_price_long = pd.Series(signal_price_long.values, index=idx)
            signal_price_short = signal_price_long
            if evaluate_on_confirmation_bar:
                conf_bar = signal_sr.resample(conf_freq).last()
                confirmation_eval_mask = pd.Series(
                    signal_sr.index.isin(conf_bar.index),
                    index=idx,
                )
            else:
                confirmation_eval_mask = pd.Series(True, index=idx)
            # Fallback when data resolution is the same as or coarser than
            # confirmation timeframe (e.g. H1 data with 15m confirmation).
            # In that case the resampled Series only has values at :00 and
            # after shift(1) / reindex everything becomes NaN.
            if signal_price_long.isna().all():
                signal_price_long = close.shift(1)
                signal_price_short = signal_price_long
                confirmation_eval_mask = pd.Series(True, index=idx)
                signal_price_fallback_used = pd.Series(True, index=idx)
        except Exception as exc:
            # Cleanup #35: a genuine failure of the confirmation path must be
            # visible, not silently traded. The fallback (previous bar close)
            # is a DIFFERENT signal definition, so it is marked in the
            # indicators AND logged instead of switching semantics invisibly.
            logger.warning(
                "intraday_volatility_breakout: confirmation resample failed "
                f"({type(exc).__name__}: {exc}); falling back to close.shift(1) "
                "signal prices (signal_price_fallback_used=True)"
            )
            signal_price_long = close.shift(1)
            signal_price_short = signal_price_long
            confirmation_eval_mask = pd.Series(True, index=idx)
            signal_price_fallback_used = pd.Series(True, index=idx)
    else:
        signal_price_long = df["high"]
        signal_price_short = df["low"]
        confirmation_eval_mask = pd.Series(True, index=idx)

    # --- Entry window --------------------------------------------------------
    in_entry_window = (minute_of_day >= entry_minute) & (minute_of_day < max_entry_minute)

    # --- Raw entry candidates ------------------------------------------------
    long_candidates = (
        in_entry_window
        & confirmation_eval_mask
        & regime_ok
        & (signal_price_long >= upper_level)
    )
    short_candidates = (
        in_entry_window
        & confirmation_eval_mask
        & regime_ok
        & (signal_price_short <= lower_level)
    )

    # Trade direction filter.
    #
    # MT5 parity: EvaluateBreakoutSignals / EvaluateReverseSignals check the
    # direction permission against the side ACTUALLY traded, so the filter
    # must apply after the reverse-mode mapping. (Filtering before the swap
    # permits e.g. long_only+reverse to SHORT on upper hits, while MT5 only
    # allows the fade LONG on lower hits.) With trade_direction="both" the
    # order is irrelevant.
    if reverse_mode:
        long_candidates, short_candidates = (
            short_candidates.copy(),
            long_candidates.copy(),
        )
    if trade_direction == "long_only":
        short_candidates = pd.Series(False, index=idx)
    elif trade_direction == "short_only":
        long_candidates = pd.Series(False, index=idx)

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
    entry_stop_distance = stop_distance.where(long_entries | short_entries)
    if use_candle_close:
        sl_price = sl_price.shift(-1)
        entry_stop_distance = entry_stop_distance.shift(-1)

    # --- Exits (time exit + EOD safety net) ----------------------------------
    exit_time_mask = minute_of_day >= exit_minute
    # Last bar of each day
    idx_series = pd.Series(idx, index=idx)
    is_last_bar = idx_series.groupby(day_str.values).transform("last") == idx_series
    eod_exit_mask = is_last_bar
    exits = exit_time_mask | eod_exit_mask

    # --- Signal/risk/execution contract metadata -------------------------------
    strategy_id = "intraday_volatility_breakout"
    strategy_id_series = pd.Series(index=idx, dtype=object)
    strategy_id_series.loc[entries] = strategy_id

    order_timing = pd.Series(index=idx, dtype=object)
    order_timing.loc[entries] = "next_bar_open"
    order_type = pd.Series(index=idx, dtype=object)
    order_type.loc[entries] = "market"

    entry_reference_price = signal_price_long.where(long_entries).combine_first(
        signal_price_short.where(short_entries)
    )
    if use_candle_close:
        entry_reference_price = entry_reference_price.shift(-1)

    signal_timestamps = pd.Series(idx, index=idx, dtype=object)
    if "timestamp" in df.columns:
        timestamp_values = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        signal_timestamps = pd.Series(
            timestamp_values.to_list(),
            index=idx,
            dtype=object,
        )

    signal_id = pd.Series(index=idx, dtype=object)
    for entry_idx in entries[entries].index:
        ts = signal_timestamps.loc[entry_idx]
        timestamp_part = ts.isoformat() if hasattr(ts, "isoformat") and pd.notna(ts) else str(ts)
        side = direction.loc[entry_idx]
        signal_id.loc[entry_idx] = f"{strategy_id}:{timestamp_part}:{side}"

    exit_reason = pd.Series(index=idx, dtype=object)
    exit_reason.loc[exit_time_mask] = "time_exit"
    exit_reason.loc[eod_exit_mask & ~exit_time_mask] = "end_of_day"

    # --- Indicators ----------------------------------------------------------
    indicators_dict = {
        "daily_atr": daily_atr_ffill,
        "upper_level": upper_level,
        "lower_level": lower_level,
        "regime_ok": regime_ok,
        "entry_open": entry_open,
        "direction": direction,
        "sl_price": sl_price,
        "stop_distance": entry_stop_distance,
        "signal_id": signal_id,
        "strategy_id": strategy_id_series,
        "entry_reference_price": entry_reference_price,
        "order_timing": order_timing,
        "order_type": order_type,
        "exit_reason": exit_reason,
        "signal_price": signal_price_long,
        "signal_price_fallback_used": signal_price_fallback_used,
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

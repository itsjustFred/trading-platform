"""
Strategy: Intraday Volatility Breakout (VolaBreakout).

Source-faithful port of "optimized_version_vola_breackout nt8.txt"
(Discord ID 1433816630118449322).

Source semantics (NT8, Calculate.OnBarClose, primary intraday + daily series):
- Daily ATR(5, Wilder) from the DAILY bars, PREVIOUS bar value ([1]).
- Session clock on exchange time: entries only 08:30 <= t < 15:00.
- rthOpen = open of the first in-session bar of the day.
- Levels: long = rthOpen + 0.3 * ATRprev, short = rthOpen - 0.3 * ATRprev.
- Entry when flat: close > long level -> LONG (market), close < short level
  -> SHORT (market); protective stop AT SESSION OPEN (SetStopLoss price
  rthOpen) for both sides.
- Force-close 60 min before session end (default): flat any open position
  once t >= session_end - 60 min (runs before the session gate, so it also
  fires outside the entry window).
- Session-close exit (IsExitOnSessionCloseStrategy, 30 s): flatten at the
  session end.
- Position sizing (RiskPerTrade %, RiskPerTradeMoney, BackTestBalanceStart,
  contract math) is engine-owned in this runner and intentionally not ported.

Notes on fidelity:
- The source sets ``tradingerlaubtheuteundjetzt = false`` on force-close but
  never reads it back, so re-entry later the same session is possible; this
  port replicates the effective behavior (no re-entry block) and documents it.
- The invented ATR-regime filter (atr_short/atr_long) and the MT5-style
  entry/exit clock (16:30/18:05/22:55) of the previous port are removed.
- Session clock uses America/New_York (the source uses the instrument
  TradingHours timezone; NY assumed for US intraday).
- Exit robustness: every exit signal sets BOTH exit_price and exit_reason,
  every entry sets sl_price (= rthOpen) and direction, so the adapter never
  sizes a trade or prices an exit off NaN (the NaN-PnL cascade class).
"""

STRATEGY_META = {
    "name": "Volatility Breakout",
    "version": "1.1.0",
    "source_url": "https://discord.com/channels/1230977283888648315/1342609786633584670/1433816630118449322",
    "notes": (
        "- Source: optimized_version_vola_breackout nt8.txt (daily ATR(5)[1], "
        "offset 0.3, levels rthOpen +/- 0.3*ATR, stop at session open, "
        "session 8:30-15:00, force-close 60 min before session end).\n"
        "- Removed invented ATR-regime filter and MT5-style 16:30/18:05/22:55 clock.\n"
        "- Session clock is America/New_York (source: instrument timezone).\n"
        "- Source force-close sets a flag that is never read; re-entry after "
        "force-close stays possible (effective behavior, documented).\n"
        "- Every exit sets exit_price+exit_reason; every entry sets "
        "sl_price+direction (no NaN sizing/pricing)."
    ),
    "tags": {
        "type": ["breakout"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/1230977283888648315/1342609786633584670/1433816630118449322",
        "asset_class": ["equities", "indices", "forex"],
        "complexity": "simple",
    },
}

import numpy as np
import pandas as pd


def _wilder_rma(series: pd.Series, length: int) -> pd.Series:
    return series.astype(float).ewm(alpha=1.0 / float(length), adjust=False).mean()


def _parse_minutes(value: str) -> int:
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    atr_period = int(
        params.get("daily_atr_period", params.get("atr_period", params.get("DailyATRMulti", 5)))
    )
    atr_mult = float(
        params.get("atr_mult", params.get("ATRMulti", params.get("atr_multiplier", 0.3)))
    )
    start_time = str(params.get("start_time", params.get("StartTime", "08:30")))
    end_time = str(params.get("end_time", params.get("EndTime", "15:00")))
    force_close_in_min = int(
        params.get("force_close_in_min", params.get("ForceCloseInMin", 60))
    )
    exit_on_session_close = params.get(
        "exit_on_session_close", params.get("ExitOnSessionClose", True)
    )

    n = len(df)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    if "timestamp" in df.columns:
        ts_utc = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    elif isinstance(df.index, pd.DatetimeIndex):
        ts_utc = pd.Series(
            pd.to_datetime(df.index, utc=True, errors="coerce"), index=df.index
        )
    else:
        ts_utc = pd.Series(pd.NaT, index=df.index)
    try:
        ts_ny = ts_utc.dt.tz_convert("America/New_York")
        ny_dates = ts_ny.dt.date.to_numpy()
        ny_minutes = (ts_ny.dt.hour * 60 + ts_ny.dt.minute).to_numpy()
        clock_ok = True
    except Exception:
        ny_dates = np.array([None] * n)
        ny_minutes = np.zeros(n, dtype=int)
        clock_ok = False

    start_min = _parse_minutes(start_time)
    end_min = _parse_minutes(end_time)
    force_close_min = end_min - force_close_in_min if force_close_in_min > 0 else None

    # Daily bars (NY dates) from the intraday frame for the daily ATR.
    # ATR[i] uses Wilder smoothing; the signal on date d uses the PREVIOUS
    # daily bar value (dailyATRIndicator[1]).
    date_order: list = []
    d_open: dict = {}
    d_high: dict = {}
    d_low: dict = {}
    d_close: dict = {}
    for i in range(n):
        d = ny_dates[i]
        if d is None or (isinstance(d, float) and np.isnan(d)):
            continue
        o, h, lo, c = (
            float(open_.iloc[i]),
            float(high.iloc[i]),
            float(low.iloc[i]),
            float(close.iloc[i]),
        )
        if d not in d_close:
            date_order.append(d)
            d_open[d] = o
            d_high[d] = h
            d_low[d] = lo
        else:
            d_high[d] = max(d_high[d], h)
            d_low[d] = min(d_low[d], lo)
        d_close[d] = c
    if date_order:
        dclose = pd.Series([d_close[d] for d in date_order], index=pd.Index(date_order))
        dprev = dclose.shift(1)
        dhigh = pd.Series([d_high[d] for d in date_order], index=pd.Index(date_order))
        dlow = pd.Series([d_low[d] for d in date_order], index=pd.Index(date_order))
        dtr = pd.concat(
            [dhigh - dlow, (dhigh - dprev).abs(), (dlow - dprev).abs()], axis=1
        ).max(axis=1)
        datr = _wilder_rma(dtr, atr_period).shift(1)  # previous daily bar
        atr_prev_by_date = datr.to_dict()
    else:
        atr_prev_by_date = {}

    # rthOpen per date: open of the first IN-SESSION bar.
    rth_open_by_date: dict = {}
    day_index_by_date: dict = {}
    if clock_ok:
        for i in range(n):
            d = ny_dates[i]
            if d is None or (isinstance(d, float) and np.isnan(d)):
                continue
            if d not in day_index_by_date:
                day_index_by_date[d] = len(day_index_by_date)
            m = int(ny_minutes[i])
            if d not in rth_open_by_date and start_min <= m < end_min:
                rth_open_by_date[d] = float(open_.iloc[i])

    session_open = pd.Series(np.nan, index=df.index, dtype=float)
    atr_prev = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(n):
        d = ny_dates[i]
        if d is None or (isinstance(d, float) and np.isnan(d)):
            continue
        if d in rth_open_by_date:
            session_open.iloc[i] = rth_open_by_date[d]
        v = atr_prev_by_date.get(d, np.nan)
        if pd.notna(v):
            atr_prev.iloc[i] = float(v)

    upper_band = session_open + atr_mult * atr_prev
    lower_band = session_open - atr_mult * atr_prev

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    active_direction: str | None = None
    active_stop = np.nan

    for i in range(n):
        c = float(close.iloc[i])
        lo = float(low.iloc[i])
        hi = float(high.iloc[i])
        m = int(ny_minutes[i]) if clock_ok else -1
        ro = session_open.iloc[i]
        atr_v = atr_prev.iloc[i]
        d = ny_dates[i]

        # Force-close runs before the session gate (source order): flatten an
        # open position once t >= session_end - force_close_in_min.
        if in_trade and force_close_min is not None and clock_ok and m >= force_close_min:
            exits.iloc[i] = True
            exit_price.iloc[i] = c
            exit_reason.iloc[i] = "force_close"
            in_trade = False
            active_direction = None
            active_stop = np.nan
            continue

        if in_trade:
            # Protective stop AT SESSION OPEN (stop order -> low/high check).
            if active_direction == "LONG" and pd.notna(active_stop):
                if lo <= float(active_stop):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = float(active_stop)
                    exit_reason.iloc[i] = "stop_loss"
                    in_trade = False
                    active_direction = None
                    active_stop = np.nan
                    continue
            elif active_direction == "SHORT" and pd.notna(active_stop):
                if hi >= float(active_stop):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = float(active_stop)
                    exit_reason.iloc[i] = "stop_loss"
                    in_trade = False
                    active_direction = None
                    active_stop = np.nan
                    continue
            # Session-close flatten at the last in-session bar of the day.
            if bool(exit_on_session_close) and clock_ok:
                last_in_session = start_min <= m < end_min and (
                    i == n - 1
                    or ny_dates[i + 1] != d
                    or not (start_min <= int(ny_minutes[i + 1]) < end_min)
                )
                if last_in_session:
                    exits.iloc[i] = True
                    exit_price.iloc[i] = c
                    exit_reason.iloc[i] = "session_exit"
                    in_trade = False
                    active_direction = None
                    active_stop = np.nan
                    continue
            continue

        # Flat: entries only inside the session with warmed-up daily ATR
        # (source: 6 daily bars + BarsRequiredToTrade 20).
        if not clock_ok or not (start_min <= m < end_min):
            continue
        if i < 20 or day_index_by_date.get(d, 0) < 5:
            continue
        if pd.isna(ro) or pd.isna(atr_v):
            continue
        long_level = float(ro) + atr_mult * float(atr_v)
        short_level = float(ro) - atr_mult * float(atr_v)
        if c > long_level:
            entries.iloc[i] = True
            direction.iloc[i] = "LONG"
            sl_price.iloc[i] = float(ro)
            in_trade = True
            active_direction = "LONG"
            active_stop = float(ro)
        elif c < short_level:
            entries.iloc[i] = True
            direction.iloc[i] = "SHORT"
            sl_price.iloc[i] = float(ro)
            in_trade = True
            active_direction = "SHORT"
            active_stop = float(ro)

    indicators_dict = {
        "Session_Open": session_open,
        "Session_Open_at_exit": session_open,
        "ATR": atr_prev,
        "Upper_Band": upper_band,
        "Lower_Band": lower_band,
        "direction": direction,
        "stop_loss": sl_price,
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
        for ts in entry_times.iloc[:30]:
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
        for ts in exit_times.iloc[:30]:
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

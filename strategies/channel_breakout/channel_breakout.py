"""
Intraday Channel Breakout Strategy

Source-faithful port of "Intraday Channel Breakout nt8.txt"
(NQ 5 min US Equities RTH, Discord ID 1388096847498707044).

Source semantics (NT8, Calculate.OnBarClose):
- Channel Length=5 (MAX(high,5)/MIN(low,5)), EMA(200) trend filter (default ON),
  ATR(14) x 2.0 trailing stop (default).
- Bi-directional: EnableLong/EnableShort default True. Entries are STOP orders
  at the channel (long: upBound+TickSize, short: downBound-TickSize),
  approximated here as close-breakout vs PRIOR bar channel
  (close > upper[-1] / close < lower[-1]) with next-open execution in vectorbt.
- Gap filter (default ON, MinGap 0.5 pts): positive gap -> longs only,
  negative gap -> shorts only, gap < MinGap (incl. gap==0) -> NO trades.
- Channel exit (default ON): opposite-side stop (long exits at
  downBound-TickSize, short at upBound+TickSize).
- Session-close exit (IsExitOnSessionCloseStrategy, 30s): flatten at the last
  bar of each NY session day (no overnight holds).
- EMA exit (default OFF) + fixed-$ stop (default OFF, $100) are opt-in params
  defaulting to source defaults.

vectorbt notes:
- Engine supports SHORTS via the `direction` indicator (LONG/SHORT); PnL is
  mirrored in the adapter. Shorts are therefore restored (not long-only).
- Next-open execution: signals fire on bar close, fills on next open.
- Fixed-$ stop: NT8 Currency-mode needs account/contract value unavailable in
  generate_signals; stop_loss_amount is a DOLLAR risk amount (D6) converted
  to a price distance via point_value ($/point, default 20.0 = NQ: $100
  risk ~= 5 pts). Pass the instrument's point value for other symbols.
- Trailing-stop trigger is close-based per source (Close <= trail); channel /
  fixed stops are stop orders, checked against low/high.
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


STRATEGY_META = {
    "name": "Intraday Channel Breakout",
    "version": "1.1.0",
    "source_url": "https://discord.com/channels/1230977283888648315/1383712509831155782/1388096847498707044",
    "notes": (
        "- Source: Intraday Channel Breakout nt8.txt (Length=5, EMA200 filter ON, "
        "ATR14x2.0 trailing, gap filter ON MinGap 0.5, channel exit ON, EMA exit OFF, "
        "fixed-$ stop OFF $100, session-close exit ON, longs+shorts ON).\n"
        "- vectorbt_adapter supports SHORT entries via the `direction` indicator "
        "(LONG/SHORT with mirrored PnL); shorts restored, not long-only.\n"
        "- Stop-entry orders approximated as close vs prior-bar channel with "
        "next-open execution; no intrabar stop fills.\n"
        "- Fixed-$ stop is a DOLLAR risk amount (D6): price distance = "
        "stop_loss_amount / point_value (default point_value 20.0 = NQ, "
        "so $100 ~= 5 pts). Set point_value to the traded instrument's "
        "$/point.\n"
        "- Gap/session-close use NY calendar dates (US Equities RTH); without a "
        "`timestamp` column the gap filter is bypassed (documented)."
    ),
    "tags": {
        "type": ["breakout"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/1230977283888648315/1383712509831155782/1388096847498707044",
        "asset_class": ["equities", "futures"],
        "complexity": "medium",
    },
}


def _ny_dates(df: pd.DataFrame):
    """Return NY dates array or None when no timestamp column."""
    if "timestamp" not in df.columns:
        return None, None
    ts_utc = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    try:
        ts_ny = ts_utc.dt.tz_convert("America/New_York")
    except Exception:
        logger.warning("NY timezone conversion failed; gap filter bypassed")
        return None, None
    return ts_ny.dt.date.to_numpy(), ts_ny


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    channel_length = int(params.get("channel_length", params.get("Length", 5)))
    ema_period = int(params.get("ema_period", params.get("EmaPeriod", 200)))
    use_ema_filter = params.get("use_ema_filter", params.get("UseEmaFilter", True))
    enable_long = params.get("enable_long", params.get("EnableLongTrades", True))
    enable_short = params.get("enable_short", params.get("EnableShortTrades", True))
    atr_period = int(params.get("atr_period", params.get("AtrPeriod", 14)))
    atr_multiplier = float(params.get("atr_multiplier", params.get("AtrMultiplier", 2.0)))
    use_fixed_stop = params.get(
        "use_fixed_stop_loss", params.get("UseFixedStopLoss", False)
    )
    stop_loss_amount = float(params.get("stop_loss_amount", params.get("StopLossAmount", 100.0)))
    # D6 (B-H2): stop_loss_amount is a DOLLAR risk amount, not a price
    # distance. 100 POINTS ~= $2000/contract at $20/pt instead of $100 risk
    # (~20x mis-sized). Convert via the instrument point value ($/point).
    point_value = float(params.get("point_value", params.get("PointValue", 20.0)))
    if point_value <= 0:
        raise ValueError(f"point_value must be positive ($/point), got {point_value!r}")
    fixed_stop_distance = stop_loss_amount / point_value
    use_channel_exit = params.get("use_channel_exit", params.get("UseChannelExit", True))
    use_ema_exit = params.get("use_ema_exit", params.get("UseEmaExit", False))
    use_gap_filter = params.get("use_gap_filter", params.get("UseGapFilter", True))
    min_gap_size = float(params.get("min_gap_size", params.get("MinimumGapSize", 0.5)))
    exit_on_session_close = params.get(
        "exit_on_session_close", params.get("ExitOnSessionClose", True)
    )

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float) if "open" in df.columns else close

    ema = close.ewm(span=ema_period, adjust=False).mean()
    upper_channel = high.rolling(channel_length).max()
    lower_channel = low.rolling(channel_length).min()

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    atr = tr.rolling(atr_period).mean()

    n = len(df)
    # Gap direction per bar: 1 = long only, -1 = short only, 0 = no trades.
    gap_direction = pd.Series(np.zeros(n, dtype=int), index=df.index)
    ny_dates, _ = _ny_dates(df)
    gap_filter_active = bool(use_gap_filter) and (ny_dates is not None)
    if gap_filter_active:
        dates = np.array(ny_dates)
        # first open / last close per date in encountered order
        order: list = []
        first_open: dict = {}
        last_close: dict = {}
        for i in range(n):
            d = dates[i]
            if d not in first_open:
                first_open[d] = float(open_.iloc[i])
                order.append(d)
            last_close[d] = float(close.iloc[i])
        prev_close = None
        gap_by_date: dict = {}
        for d in order:
            if prev_close is None:
                gap_by_date[d] = 0
            else:
                gap = first_open[d] - prev_close
                if abs(gap) >= min_gap_size and gap != 0:
                    gap_by_date[d] = 1 if gap > 0 else -1
                else:
                    gap_by_date[d] = 0
            prev_close = last_close[d]
        for i in range(n):
            gap_direction.iloc[i] = gap_by_date[dates[i]]

    # B-M2: last-bar-of-day markers are derived whenever a session calendar
    # is available — INDEPENDENT of the gap filter. Previously
    # is_last_of_day stayed all-False with use_gap_filter=False, so
    # exit_on_session_close=True silently held positions overnight.
    is_last_of_day = pd.Series(False, index=df.index)
    session_dates = np.array(ny_dates) if ny_dates is not None else None
    if session_dates is None and "timestamp" in df.columns and (
        bool(use_gap_filter) or bool(exit_on_session_close)
    ):
        # NY conversion failed -> fall back to UTC dates for session-close
        # only; gap stays bypassed (documented).
        try:
            ts_utc = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            session_dates = ts_utc.dt.date.to_numpy()
        except Exception:
            logger.warning("Session-date fallback failed; session-close exit disabled")
            session_dates = None
    if session_dates is not None:
        for i in range(n):
            if i == n - 1 or session_dates[i] != session_dates[i + 1]:
                is_last_of_day.iloc[i] = True

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sl_price = pd.Series(np.nan, index=df.index)
    trailing_stop_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    active_direction: str | None = None
    trailing_stop_value: float | None = None
    active_sl = np.nan
    warmup = max(channel_length, ema_period, atr_period, 20)

    for i in range(warmup, n):
        up_prev = upper_channel.iloc[i - 1]
        dn_prev = lower_channel.iloc[i - 1]
        ema_v = ema.iloc[i]
        atr_v = atr.iloc[i]
        c = float(close.iloc[i])
        lo = float(low.iloc[i])
        hi = float(high.iloc[i])
        if pd.isna(up_prev) or pd.isna(dn_prev) or pd.isna(ema_v):
            continue

        if in_trade:
            if active_direction == "LONG":
                # ATR trailing / fixed stop bookkeeping (source: trailing is
                # close-based; fixed SetStopLoss is a stop order -> low/high).
                if not use_fixed_stop and pd.notna(atr_v):
                    new_stop = c - float(atr_v) * atr_multiplier
                    if trailing_stop_value is None or new_stop > trailing_stop_value:
                        trailing_stop_value = new_stop
                    trailing_stop_at_exit.iloc[i] = trailing_stop_value
                # 1) stop hit (stop-first priority)
                if use_fixed_stop:
                    if lo <= active_sl:
                        exits.iloc[i] = True
                        exit_price.iloc[i] = float(active_sl)
                        exit_reason.iloc[i] = "fixed_stop"
                        in_trade = False
                        active_direction = None
                        trailing_stop_value = None
                        continue
                else:
                    # A-M2: close-triggered trailing exit fills at the CLOSE,
                    # not at the stop level (filling LONGs at the level above
                    # the close systematically sells too high). Stop-order
                    # legs (fixed stop on low/high, channel exit on low/high)
                    # keep level fills — only close-triggered legs move.
                    if trailing_stop_value is not None and c <= trailing_stop_value:
                        exits.iloc[i] = True
                        exit_price.iloc[i] = c
                        exit_reason.iloc[i] = "trailing_stop"
                        in_trade = False
                        active_direction = None
                        trailing_stop_value = None
                        continue
                # 2) channel exit: opposite-side stop (stop order -> low check)
                if use_channel_exit and lo <= float(dn_prev):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = float(dn_prev)
                    exit_reason.iloc[i] = "channel_exit"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
                    continue
                # 3) EMA exit (close-based per source)
                if use_ema_exit and c < float(ema_v):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = c
                    exit_reason.iloc[i] = "ema_exit"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
                    continue
                # 4) session-close flatten
                if bool(exit_on_session_close) and bool(is_last_of_day.iloc[i]):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = c
                    exit_reason.iloc[i] = "session_close"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
            else:  # SHORT
                if not use_fixed_stop and pd.notna(atr_v):
                    new_stop = c + float(atr_v) * atr_multiplier
                    if trailing_stop_value is None or new_stop < trailing_stop_value:
                        trailing_stop_value = new_stop
                    trailing_stop_at_exit.iloc[i] = trailing_stop_value
                if use_fixed_stop:
                    if hi >= active_sl:
                        exits.iloc[i] = True
                        exit_price.iloc[i] = float(active_sl)
                        exit_reason.iloc[i] = "fixed_stop"
                        in_trade = False
                        active_direction = None
                        trailing_stop_value = None
                        continue
                else:
                    # A-M2: close-triggered trailing exit fills at the CLOSE
                    # (mirror of the LONG leg) — never at the stop level.
                    if trailing_stop_value is not None and c >= trailing_stop_value:
                        exits.iloc[i] = True
                        exit_price.iloc[i] = c
                        exit_reason.iloc[i] = "trailing_stop"
                        in_trade = False
                        active_direction = None
                        trailing_stop_value = None
                        continue
                if use_channel_exit and hi >= float(up_prev):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = float(up_prev)
                    exit_reason.iloc[i] = "channel_exit"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
                    continue
                if use_ema_exit and c > float(ema_v):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = c
                    exit_reason.iloc[i] = "ema_exit"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
                    continue
                if bool(exit_on_session_close) and bool(is_last_of_day.iloc[i]):
                    exits.iloc[i] = True
                    exit_price.iloc[i] = c
                    exit_reason.iloc[i] = "session_close"
                    in_trade = False
                    active_direction = None
                    trailing_stop_value = None
        else:
            # Gap gate: 1 -> longs only, -1 -> shorts only, 0 -> no trades.
            if gap_filter_active:
                g = int(gap_direction.iloc[i])
                long_allowed = bool(enable_long) and g == 1
                short_allowed = bool(enable_short) and g == -1
            else:
                long_allowed = bool(enable_long)
                short_allowed = bool(enable_short)

            long_breakout = c > float(up_prev)
            short_breakout = c < float(dn_prev)
            long_ema_ok = (not use_ema_filter) or (c > float(ema_v))
            short_ema_ok = (not use_ema_filter) or (c < float(ema_v))

            if long_allowed and long_ema_ok and long_breakout:
                entries.iloc[i] = True
                direction.iloc[i] = "LONG"
                in_trade = True
                active_direction = "LONG"
                if use_fixed_stop:
                    active_sl = c - fixed_stop_distance
                    sl_price.iloc[i] = float(active_sl)
                    trailing_stop_value = None
                else:
                    if pd.notna(atr_v):
                        trailing_stop_value = c - float(atr_v) * atr_multiplier
                        sl_price.iloc[i] = float(trailing_stop_value)
                        active_sl = float(trailing_stop_value)
                    else:
                        trailing_stop_value = None
                        active_sl = np.nan
            elif short_allowed and short_ema_ok and short_breakout:
                entries.iloc[i] = True
                direction.iloc[i] = "SHORT"
                in_trade = True
                active_direction = "SHORT"
                if use_fixed_stop:
                    active_sl = c + fixed_stop_distance
                    sl_price.iloc[i] = float(active_sl)
                    trailing_stop_value = None
                else:
                    if pd.notna(atr_v):
                        trailing_stop_value = c + float(atr_v) * atr_multiplier
                        sl_price.iloc[i] = float(trailing_stop_value)
                        active_sl = float(trailing_stop_value)
                    else:
                        trailing_stop_value = None
                        active_sl = np.nan

    indicators_dict = {
        "EMA_200": ema,
        "Upper_Channel": upper_channel,
        "Lower_Channel": lower_channel,
        "ATR": atr,
        "sl_price": sl_price,
        "fixed_stop_distance_pts": pd.Series(
            fixed_stop_distance, index=df.index, dtype=float
        ),
        "trailing_stop_at_exit": trailing_stop_at_exit,
        "direction": direction,
        "gap_direction": gap_direction.astype(float),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    drawings: list[dict] = []
    if "timestamp" in df.columns:
        ts_all = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        long_mask = entries & (direction == "LONG")
        short_mask = entries & (direction == "SHORT")
        for ts in ts_all.loc[long_mask].dropna().iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Long",
                }
            )
        for ts in ts_all.loc[short_mask].dropna().iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#f97316",
                    "text": "Short",
                }
            )
        for ts in ts_all.loc[exits].dropna().iloc[:30]:
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

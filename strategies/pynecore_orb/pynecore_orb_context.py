"""Trade-context helper for the PyneCore opening-range breakout (pynecore_orb).

Pure pandas/numpy mirror of ``pynecore_orb.py`` (the allowed pattern for
in-process helper code: no ``pynecore`` import — the script itself runs
out-of-process via the ``pyne`` CLI). It replays the same ET-session state
machine on closes so each trade is enriched with:

- ``OR_High`` / ``OR_Low`` / ``OR_Range`` / ``breakout_long`` /
  ``breakout_short`` (entry-sampled),
- ``direction`` (``LONG``/``SHORT`` at entry bars),
- ``sl_price_long`` / ``sl_price_short`` (1%-of-price protective distance as
  an every-bar series, sampled at the fill bar; used for ``--risk-pct``
  sizing),
- ``trailing_stop_at_exit`` (exit-sampled HWM/LWM trailing level),
- ``exit_reason`` (``trailing_stop`` / ``time_exit`` at exit bars).

Approximation note: the helper replays fills at signal-bar closes while the
broker emulator fills at the next bar open, so entry/exit bar alignment — and
hence HWM paths — can differ by one bar on gap opens. Exit reasons are
best-effort mirrors of the script logic, not copies of the trade ledger.
"""

import pandas as pd


def build_trade_context(df: pd.DataFrame, params: dict | None = None) -> dict[str, pd.Series]:
    _TZ = "America/New_York"
    _OR_START_MINUTES = 9 * 60 + 30
    params = params or {}
    opening_range_minutes = int(params.get("opening_range_minutes", 30))
    breakout_buffer = float(params.get("breakout_buffer", 0.2))
    reversion_multiple = float(params.get("reversion_multiple", 0.01))
    exit_hour = int(params.get("exit_hour_ny", params.get("exit_hour", 15)))
    exit_minute = int(params.get("exit_minute_ny", params.get("exit_minute", 45)))

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    if "timestamp" in df.columns:
        ts_ny = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.tz_convert(_TZ)
        minutes_now = (ts_ny.dt.hour * 60 + ts_ny.dt.minute).to_numpy()
        day_key = ts_ny.dt.date.to_numpy()
    else:
        minutes_now = pd.Series(12 * 60, index=df.index).to_numpy()
        day_key = pd.Series(0, index=df.index).to_numpy()

    or_end = _OR_START_MINUTES + opening_range_minutes
    exit_minutes = exit_hour * 60 + exit_minute
    n = len(df)

    or_high = pd.Series(float("nan"), index=df.index)
    or_low = pd.Series(float("nan"), index=df.index)
    breakout_long = pd.Series(float("nan"), index=df.index)
    breakout_short = pd.Series(float("nan"), index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    trailing_stop_at_exit = pd.Series(float("nan"), index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    cur_day = None
    cur_or_high: float | None = None
    cur_or_low: float | None = None
    traded = False
    position = 0  # 0 flat, 1 long, -1 short (active from the bar AFTER the signal)
    pending_entry = 0  # signal-bar entry, activated next bar
    hwm: float | None = None
    lwm: float | None = None

    for i in range(n):
        if day_key[i] != cur_day:
            if position != 0:
                # B-H1 mirror: the prior session had no forced-exit bar (data
                # gap), so the script's day-reset flatten fires. Book the exit
                # on the last bar of the prior session instead of silently
                # dropping the open position. (The signal+1 carry below never
                # leaks across the session boundary, so this marker stays put.)
                exit_reason.iloc[i - 1] = "day_reset_flatten"
            cur_day = day_key[i]
            cur_or_high = None
            cur_or_low = None
            traded = False
            position = 0
            pending_entry = 0
            hwm = None
            lwm = None

        # Pending entry from the previous signal bar becomes the open position.
        if pending_entry != 0:
            position = pending_entry
            pending_entry = 0

        mins = int(minutes_now[i])

        if _OR_START_MINUTES <= mins < or_end:
            hi = float(high.iloc[i])
            lo = float(low.iloc[i])
            if cur_or_high is None:
                cur_or_high, cur_or_low = hi, lo
            else:
                cur_or_high = max(cur_or_high, hi)
                cur_or_low = min(cur_or_low, lo)

        if cur_or_high is not None:
            or_high.iloc[i] = cur_or_high
            or_low.iloc[i] = cur_or_low
            rng = cur_or_high - cur_or_low
            if rng > 0:
                breakout_long.iloc[i] = cur_or_high + rng * breakout_buffer
                breakout_short.iloc[i] = cur_or_low - rng * breakout_buffer

        price = float(close.iloc[i])

        if mins >= exit_minutes:
            if position != 0:
                exit_reason.iloc[i] = "time_exit"
                position = 0
                hwm = None
                lwm = None
            traded = True
            continue

        if position > 0:
            if hwm is None or price > hwm:
                hwm = price
            if price < hwm - hwm * reversion_multiple:
                exit_reason.iloc[i] = "trailing_stop"
                trailing_stop_at_exit.iloc[i] = hwm - hwm * reversion_multiple
                position = 0
                hwm = None
                # traded already True (set at entry)
        elif position < 0:
            if lwm is None or price < lwm:
                lwm = price
            if price > lwm + lwm * reversion_multiple:
                exit_reason.iloc[i] = "trailing_stop"
                trailing_stop_at_exit.iloc[i] = lwm + lwm * reversion_multiple
                position = 0
                lwm = None
        elif mins >= or_end and not traded and cur_or_high is not None:
            rng = cur_or_high - cur_or_low
            if rng > 0:
                if price > cur_or_high + rng * breakout_buffer:
                    direction.iloc[i] = "LONG"
                    hwm = price
                    pending_entry = 1
                    traded = True
                elif price < cur_or_low - rng * breakout_buffer:
                    direction.iloc[i] = "SHORT"
                    lwm = price
                    pending_entry = -1
                    traded = True

    # Protective distance as an every-bar series (sampled at the broker fill
    # bar, which is the signal bar +1): 1% of the bar close, matching the
    # script's initial HWM/LWM = entry price.
    sl_long = close * (1.0 - reversion_multiple)
    sl_short = close * (1.0 + reversion_multiple)

    # The broker fills entries/exits on the bar AFTER the signal bar, so the
    # adapter samples markers at signal+1. Carry each marker one bar forward
    # (harmless for non-trade bars, which are never sampled).
    # B-M5: the carry must not leak across the session boundary — a marker on
    # the last bar of day N would otherwise be sampled as a signal on the
    # first bar of day N+1 (different OR range, different session state).
    day_key_s = pd.Series(list(day_key), index=df.index)
    same_day = day_key_s.eq(day_key_s.shift(1))
    direction = direction.combine_first(direction.shift(1).where(same_day))
    exit_reason = exit_reason.combine_first(exit_reason.shift(1).where(same_day))
    trailing_stop_at_exit = trailing_stop_at_exit.combine_first(
        trailing_stop_at_exit.shift(1).where(same_day)
    )

    return {
        "OR_High": or_high,
        "OR_Low": or_low,
        "OR_Range": or_high - or_low,
        "breakout_long": breakout_long,
        "breakout_short": breakout_short,
        "direction": direction,
        "sl_price_long": sl_long,
        "sl_price_short": sl_short,
        "trailing_stop_at_exit": trailing_stop_at_exit,
        "exit_reason": exit_reason,
    }

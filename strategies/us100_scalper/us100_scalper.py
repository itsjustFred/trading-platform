"""
US100 Intraday Scalper (long-only NQ breakout scalper).

Vectorized port of the MQL5 EA "us100 intrayday scalper single-symbol mql5.txt"
(discord-zenomtrader-forum-strategies / US100 INTRADAY SCALPER):

- Midnight reference: H1 close into NY midnight (00:00 ET).
- Breakout detect 00:00-08:00 ET: any high > midnight_close + buffer -> armed.
- Entry window 08:00-16:00 ET: hourly buy-stop at prev-H1-close + entry
  offset; fill approximated when the bar high touches the stop. One fill/day.
- Protective stop at prev-H1-close - SL offset; EOD flatten at 16:00 ET.

Session mapping: the MQL5 server hours (GMT+3) 07:00 / 07-15h / 15-23h / 23:00
correspond to NY midnight / 00:00-08:00 ET / 08:00-16:00 ET / 16:00 ET; the
port expresses everything in America/New_York derived from bar timestamps.

PARAMETER CONFLICT (infos.txt vs code): infos.txt says entry 0.5 / SL 0.1 /
buffer 0.12, the .txt CODE uses EntryOffsetPercent 0.12 / StopLossPercent 0.10
/ BreakoutBufferPercent 0.12 with ReferencePrice 25000.0. THE CODE WINS:
defaults below are the code values (30 / 25 / 30 points).
"""

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "US100 Intraday Scalper",
    "version": "1.0.0",
    "source_url": "https://discord.com/channels/1230977283888648315/1453162378509418657/1453162378509418657",
    "notes": (
        "Long-only NQ scalper ported from 'us100 intrayday scalper "
        "single-symbol mql5.txt'. CONFLICT: infos.txt says entry 0.5 / SL 0.1 "
        "/ buffer 0.12; the .txt CODE uses 0.12 / 0.10 / 0.12 with "
        "ReferencePrice 25000.0 — the code values are implemented "
        "(entry 30pt / SL 25pt / buffer 30pt). MQL5 server hours map to ET: "
        "07:00 ref -> NY midnight, 07-15h detect -> 00:00-08:00 ET, 15-23h "
        "buy-stops -> 08:00-16:00 ET, 23:00 flatten -> 16:00 ET. "
        "Approximations: buy-stop fills via bar-high touch; SL exits at bar "
        "low touch; one fill per ET day. Fill pricing is engine-specific: "
        "the native engine fills the touched buy-stop at the next open, "
        "while the vectorbt engine (D2 strict) fills at the stop level "
        "itself — gap-through fills at the stop, and no touch means no "
        "fill (never a next-open fallback). EOD flatten books the bar "
        "close; a missing post-16:00-ET bar books 'day_reset_flatten' on "
        "the last bar of the ET day instead of dropping the position."
    ),
    "parameters": {
        "reference_price": 25000.0,
        "entry_offset_percent": 0.12,
        "stop_loss_percent": 0.10,
        "breakout_buffer_percent": 0.12,
    },
    "tags": {
        "type": ["breakout", "intraday", "scalper", "long-only"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/1230977283888648315/1453162378509418657/1453162378509418657",
        "asset_class": ["indices"],
        "complexity": "medium",
    },
}

_TZ = "America/New_York"
_BREAKOUT_START = 0  # 00:00 ET
_BREAKOUT_END = 8 * 60  # 08:00 ET
_ENTRY_END = 16 * 60  # 16:00 ET (flatten)


def _prev_h1_close(close: pd.Series, ts_ny: pd.Series) -> pd.Series:
    """Previous completed H1-bar close aligned to every bar.

    On 1h data this is close.shift(1); on finer data (1m) it is the close of
    the previous clock hour, matching the EA's iClose(H1, 1) semantics.
    """
    hour_key = ts_ny.dt.floor("h")
    # Epoch-int keys: tz-aware Timestamps never match a tz-naive group index,
    # so map on ints (one int per clock hour) instead of datetimes.
    hour_id = pd.Series(hour_key.astype("int64").values, index=close.index)
    hourly_last = close.groupby(hour_id).last()
    return hour_id.map(hourly_last.shift(1))


def generate_signals(df: pd.DataFrame, params: dict):
    reference_price = float(params.get("reference_price", 25000.0))
    entry_offset_percent = float(params.get("entry_offset_percent", 0.12))
    stop_loss_percent = float(params.get("stop_loss_percent", 0.10))
    breakout_buffer_percent = float(params.get("breakout_buffer_percent", 0.12))

    entry_points = reference_price * entry_offset_percent / 100.0
    stop_points = reference_price * stop_loss_percent / 100.0
    buffer_points = reference_price * breakout_buffer_percent / 100.0

    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    if "timestamp" in df.columns:
        ts_ny = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.tz_convert(_TZ)
    else:
        ts_ny = pd.to_datetime(df.index, utc=True, errors="coerce").tz_convert(_TZ)
        # Keep the tz-aware series (no .values round-trip: that strips the
        # timezone and silently shifts session windows to UTC).
        ts_ny = pd.Series(ts_ny, index=df.index)
    minutes_now = (ts_ny.dt.hour * 60 + ts_ny.dt.minute).to_numpy()
    et_day = ts_ny.dt.date.to_numpy()

    prev_h1 = _prev_h1_close(close, ts_ny)
    stop_level = prev_h1 + entry_points
    stop_loss_level = prev_h1 - stop_points

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sl_price = pd.Series(np.nan, index=df.index)
    entry_stop = pd.Series(np.nan, index=df.index)
    midnight_close = pd.Series(np.nan, index=df.index)
    armed = pd.Series(False, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    cur_day = None
    ref = np.nan
    cond = False
    traded = False
    in_trade = False
    active_sl = np.nan

    for i in range(len(df)):
        if et_day[i] != cur_day:
            if in_trade:
                # B-H1: day-reset flatten. The 16:00-ET exit never fired
                # (no post-16:00 bar in the frame, e.g. a session-data gap).
                # Book the exit on the last bar of the prior ET day at its
                # close instead of silently dropping the open position
                # (phantom trade: entry without exit).
                exits.iloc[i - 1] = True
                exit_price.iloc[i - 1] = float(close.iloc[i - 1])
                exit_reason.iloc[i - 1] = "day_reset_flatten"
            cur_day = et_day[i]
            # H1 close into NY midnight: previous bar's close at the first bar
            # of the ET day (exactly iClose(H1,1) at 07:00 server on H1 data).
            ref = float(close.iloc[i - 1]) if i > 0 else float(close.iloc[i])
            cond = False
            traded = False
            in_trade = False
            active_sl = np.nan

        midnight_close.iloc[i] = ref
        mins = int(minutes_now[i])

        if _BREAKOUT_START <= mins < _BREAKOUT_END and pd.notna(ref):
            if float(high.iloc[i]) > ref + buffer_points:
                cond = True
        armed.iloc[i] = cond

        if in_trade:
            if float(low.iloc[i]) <= active_sl:
                exits.iloc[i] = True
                exit_price.iloc[i] = float(active_sl)
                exit_reason.iloc[i] = "stop_loss"
                in_trade = False
                active_sl = np.nan
            elif mins >= _ENTRY_END:
                exits.iloc[i] = True
                exit_price.iloc[i] = float(close.iloc[i])
                exit_reason.iloc[i] = "time_exit_eod"
                in_trade = False
                active_sl = np.nan
            continue

        if (
            _BREAKOUT_END <= mins < _ENTRY_END
            and cond
            and not traded
            and pd.notna(stop_level.iloc[i])
        ):
            entry_stop.iloc[i] = float(stop_level.iloc[i])
            if float(high.iloc[i]) >= float(stop_level.iloc[i]):
                entries.iloc[i] = True
                direction.iloc[i] = "LONG"
                active_sl = float(stop_loss_level.iloc[i])
                sl_price.iloc[i] = active_sl
                traded = True
                in_trade = True

    if in_trade and len(df) > 0:
        # B-H1 edge: the frame ends mid-day with an open position (no further
        # bar to trigger the 16:00 EOD exit or a day-change flatten). Book the
        # exit on the last bar at its close instead of dropping the position
        # (phantom trade: entry without exit).
        last = len(df) - 1
        # PM11: skip when the entry fired on this very bar — a same-bar flatten
        # would book a phantom round trip (entry at the stop level, exit at the
        # close) for a position that never held. The next frame's day-reset or
        # EOD exit picks it up instead.
        if not entries.iloc[last]:
            exits.iloc[last] = True
            exit_price.iloc[last] = float(close.iloc[last])
            exit_reason.iloc[last] = "day_reset_flatten"

    _ = open_  # retained for OHLCV-shape parity; logic keys off high/low/close
    indicators_dict = {
        "midnight_close": midnight_close,
        "buffer_level": midnight_close + buffer_points,
        "prev_h1_close": prev_h1,
        "entry_stop": entry_stop,
        "direction": direction,
        "sl_price": sl_price,
        "armed": armed,
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
        for ts in entry_times.iloc[:200]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Scalp Long",
                }
            )
        for ts in exit_times.iloc[:200]:
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

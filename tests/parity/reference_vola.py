"""Independent reference for the ``volatility_breakout`` (NT8 VolaBreakout) signals.

Written from the rules in the strategy's documented contract (daily ATR(5,
Wilder)[1], levels ``session_open +/- 0.3 * ATR``, close breakout, protective
stop AT the session open, force close 60 minutes before the session end, session
close flatten, entries 08:30 <= t < 15:00 New York).  Imports nothing from the
platform.  Per-session event loop; the stateful "assumed fill" semantics of the
strategy are reproduced because they are part of the signal contract.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.parity.reference_execution import Bar, Signals

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _ny(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(NY)


def vola_signals(
    bars: list[Bar],
    *,
    atr_len: int = 5,
    mult: float = 0.3,
    start: int = 8 * 60 + 30,
    end: int = 15 * 60,
    force_close_in: int = 60,
) -> Signals:
    size = len(bars)
    sig = Signals(
        entries=[False] * size,
        exits=[False] * size,
        direction=["LONG"] * size,
        stop_loss=[None] * size,
        take_profit=[None] * size,
    )
    clock = [_ny(b.time) for b in bars]
    minute = [c.hour * 60 + c.minute for c in clock]
    day = [c.date() for c in clock]

    # per-day aggregates, Wilder ATR (alpha = 1/len, seeded by the first TR)
    order: list = []
    agg: dict = {}
    for i, b in enumerate(bars):
        d = day[i]
        if d not in agg:
            order.append(d)
            agg[d] = [b.high, b.low, b.close]
        else:
            agg[d][0] = max(agg[d][0], b.high)
            agg[d][1] = min(agg[d][1], b.low)
            agg[d][2] = b.close
    atr_for_day: dict = {}
    smoothed = None
    prev_close = None
    alpha = 1.0 / atr_len
    for d in order:
        hi, lo, cl = agg[d]
        tr = hi - lo if prev_close is None else max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        atr_for_day[d] = smoothed  # the PREVIOUS daily bar's value
        smoothed = tr if smoothed is None else alpha * tr + (1 - alpha) * smoothed
        prev_close = cl
    day_number = {d: k for k, d in enumerate(order)}

    session_open: dict = {}
    for i, b in enumerate(bars):
        if day[i] not in session_open and start <= minute[i] < end:
            session_open[day[i]] = b.open

    force_at = end - force_close_in
    in_trade = False
    side = ""
    stop = 0.0
    for i, b in enumerate(bars):
        if in_trade and minute[i] >= force_at:
            sig.exits[i] = True
            in_trade = False
            continue
        if in_trade:
            if (side == "LONG" and b.low <= stop) or (side == "SHORT" and b.high >= stop):
                sig.exits[i] = True
                in_trade = False
                continue
            last_in_session = start <= minute[i] < end and (
                i == size - 1 or day[i + 1] != day[i] or not (start <= minute[i + 1] < end)
            )
            if last_in_session:
                sig.exits[i] = True
                in_trade = False
            continue
        d = day[i]
        if not (start <= minute[i] < end) or day_number[d] < 5:
            continue
        ro, atr = session_open.get(d), atr_for_day.get(d)
        if ro is None or atr is None:
            continue
        if b.close > ro + mult * atr:
            side = "LONG"
        elif b.close < ro - mult * atr:
            side = "SHORT"
        else:
            continue
        sig.entries[i] = True
        sig.direction[i] = side
        sig.stop_loss[i] = ro
        in_trade, stop = True, ro
    return sig

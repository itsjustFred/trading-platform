"""Independent reference for the ``volatility_breakout`` (NT8 VolaBreakout) signals.

Written from the rules in the strategy's documented contract (v1.2.0, README
"Exit" section and module docstring "Exit timing"): daily ATR(5, Wilder)[1],
levels ``session_open +/- 0.3 * ATR``, close breakout, protective stop AT the
session open, force close 60 minutes before the session end, session close
flatten, entries 08:30 <= t < 15:00 New York.

Exit timing (v1.2.0).  Bars are stamped at their START and a signal exit fills at
the NEXT bar's open, so a clock exit must be raised one bar early.  A bar's END is
``minute_of_day + bar spacing``; the signal is raised on the first bar whose END
reaches the clock time:

* force close (``session_end - force_close_in``): every bar with
  ``minute + spacing >= force_at`` while in a trade;
* session close: the bar with ``minute + spacing >= session_end - spacing`` (the
  second-to-last in-session bar, filling at the last in-session bar's open).
  No entries are signalled from that bar on.  A position still open after the
  session or on a new day (data gap) is flattened on the first such bar.

The bar spacing is the most frequent positive gap between consecutive bar stamps
(ties resolved to the shorter gap).  Only the clock and the bar spacing are used,
never a later bar.

This module imports nothing from the platform.  It is a per-bar event loop; the
stateful "assumed fill" semantics of the strategy (an entry marks the position
open at the signal bar until its exit) are reproduced because they are part of the
signal contract.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.parity.reference_execution import Bar, Signals

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _ny(stamp: str) -> datetime:
    return _utc(stamp).astimezone(NY)


def _bar_spacing_minutes(times: list[datetime]) -> float:
    """Nominal bar length: the most frequent positive gap (ties -> shorter gap).

    Weekend and holiday gaps are longer steps and do not win the vote.
    Returns 0.0 when the spacing cannot be determined.
    """
    gaps: Counter = Counter()
    for earlier, later in zip(times, times[1:]):
        seconds = int((later - earlier).total_seconds())
        if seconds > 0:
            gaps[seconds] += 1
    if not gaps:
        return 0.0
    most = max(gaps.values())
    return min(seconds for seconds, count in gaps.items() if count == most) / 60.0


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
    spacing = _bar_spacing_minutes([_utc(b.time) for b in bars])

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
    session_close_at = end - spacing  # the session-close signal bar's END reaches this
    in_trade = False
    side = ""
    stop = 0.0
    entry_day = None
    for i, b in enumerate(bars):
        bar_end = minute[i] + spacing  # a bar is stamped at its start
        if in_trade and bar_end >= force_at:
            sig.exits[i] = True
            in_trade = False
            continue
        if in_trade:
            if (side == "LONG" and b.low <= stop) or (side == "SHORT" and b.high >= stop):
                sig.exits[i] = True
                in_trade = False
                continue
            in_session = start <= minute[i] < end
            session_bar = in_session and bar_end >= session_close_at
            if session_bar or not in_session or day[i] != entry_day:
                sig.exits[i] = True
                in_trade = False
            continue
        d = day[i]
        if not (start <= minute[i] < end) or day_number[d] < 5:
            continue
        if bar_end >= session_close_at:
            # an entry from the session-close bar on would be carried past the close
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
        entry_day = d
    return sig

"""Independent reference for the 5-minute Opening Range Breakout signals.

Written from the published rules (see ``strategies/zarattini_orb``), imports
nothing from the platform.  Works session by session: it first builds the
list of ET sessions, then walks them, which is a deliberately different shape
from the per-bar vectorised strategy.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.parity.reference_execution import Bar, Signals

NY = ZoneInfo("America/New_York")


def _ny(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=ZoneInfo("UTC")
    ).astimezone(NY)


def orb_signals(
    bars: list[Bar], *, atr_len: int = 14, stop_frac: float = 0.10, open_minute: int = 9 * 60 + 30
) -> Signals:
    size = len(bars)
    sig = Signals(
        entries=[False] * size,
        exits=[False] * size,
        direction=["LONG"] * size,
        stop_loss=[None] * size,
        take_profit=[None] * size,
    )
    sessions: list[list[int]] = []
    current_date = None
    for i, bar in enumerate(bars):
        day = _ny(bar.time).date()
        if day != current_date:
            sessions.append([])
            current_date = day
        sessions[-1].append(i)

    atr_prev: float | None = None
    history: list[float] = []
    prev_close: float | None = None
    for members in sessions:
        first, last = members[0], members[-1]
        sig.exits[last] = True
        stamp = _ny(bars[first].time)
        candle = bars[first]
        eligible = (
            atr_prev is not None
            and stamp.hour * 60 + stamp.minute == open_minute
            and len(members) > 1
            and candle.close != candle.open
        )
        if eligible:
            side = 1 if candle.close > candle.open else -1
            sig.entries[first] = True
            sig.direction[first] = "LONG" if side > 0 else "SHORT"
            sig.stop_loss[first] = candle.close - side * stop_frac * atr_prev
        # fold today's range into the ATR AFTER the decision (previous-day value)
        day_high = max(bars[m].high for m in members)
        day_low = min(bars[m].low for m in members)
        day_close = bars[last].close
        tr = day_high - day_low
        if prev_close is not None:
            tr = max(tr, abs(day_high - prev_close), abs(day_low - prev_close))
        prev_close = day_close
        if len(history) < atr_len:
            history.append(tr)
            if len(history) == atr_len:
                atr_prev = sum(history) / atr_len
        else:
            assert atr_prev is not None
            atr_prev = (atr_prev * (atr_len - 1) + tr) / atr_len
    return sig

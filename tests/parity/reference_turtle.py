"""Independent streaming reference for Turtle System 1 signal generation.

Written from the published rules (see ``strategies/turtle_system1`` docstring),
NOT from the strategy source: it is a bar-at-a-time event loop with rolling
deques and a small ``Position``/``Shadow`` object pair, imports nothing from the
platform, and produces the per-bar ``Signals`` the execution reference
(``reference_execution.ReferenceSimulator``) consumes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tests.parity.reference_execution import Bar, Signals


@dataclass
class _Unit:
    bar: int
    stop: float


@dataclass
class _Shadow:
    """Hypothetical single-unit trade used by the System 1 skip rule."""

    bar: int
    side: int
    ref: float
    stop: float


@dataclass
class TurtleRef:
    entry_len: int = 20
    exit_len: int = 10
    failsafe_len: int = 55
    atr_len: int = 20
    stop_n: float = 2.0
    add_n: float = 0.5
    max_units: int = 4
    skip_after_winner: bool = True
    allow_short: bool = True
    log: list[str] = field(default_factory=list)

    def run(self, bars: list[Bar]) -> Signals:
        size = len(bars)
        sig = Signals(
            entries=[False] * size,
            exits=[False] * size,
            direction=["LONG"] * size,
            stop_loss=[None] * size,
            take_profit=[None] * size,
        )
        hi_hist: deque[float] = deque(maxlen=self.failsafe_len)
        lo_hist: deque[float] = deque(maxlen=self.failsafe_len)
        trs: list[float] = []
        n_val: float | None = None
        prev_close: float | None = None

        units: list[_Unit] = []
        side = 0
        last_ref = 0.0
        last_n = 0.0
        shadow: _Shadow | None = None
        shadow_won: bool | None = None

        for j, bar in enumerate(bars):
            # true range / N (streaming)
            if prev_close is None:
                tr = bar.high - bar.low
            else:
                tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
            trs.append(tr)
            if len(trs) == self.atr_len:
                n_val = sum(trs) / self.atr_len
            elif len(trs) > self.atr_len and n_val is not None:
                n_val = (n_val * (self.atr_len - 1) + tr) / self.atr_len

            def channel(length: int) -> tuple[float, float] | None:
                if len(hi_hist) < length:
                    return None
                highs = list(hi_hist)[-length:]
                lows = list(lo_hist)[-length:]
                return max(highs), min(lows)

            c20, c10, c55 = channel(self.entry_len), channel(self.exit_len), channel(self.failsafe_len)

            def exit_channel_hit(s: int) -> bool:
                if c10 is None:
                    return False
                return bar.close < c10[1] if s > 0 else bar.close > c10[0]

            # protective stops on units signalled before this bar
            if units:
                kept = []
                for u in units:
                    if u.bar < j and ((bar.low <= u.stop) if side > 0 else (bar.high >= u.stop)):
                        continue
                    kept.append(u)
                units = kept
                if not units:
                    side = 0
            if shadow is not None and shadow.bar < j:
                touched = bar.low <= shadow.stop if shadow.side > 0 else bar.high >= shadow.stop
                if touched:
                    shadow_won = (shadow.stop - shadow.ref) * shadow.side > 0
                    shadow = None
            if shadow is not None and shadow.bar < j and exit_channel_hit(shadow.side):
                shadow_won = (bar.close - shadow.ref) * shadow.side > 0
                shadow = None
            if side != 0 and exit_channel_hit(side):
                sig.exits[j] = True
                units, side = [], 0

            if n_val is not None:
                if side != 0:
                    moved = (bar.close - last_ref) * side
                    if len(units) < self.max_units and moved >= self.add_n * last_n:
                        sig.entries[j] = True
                        sig.direction[j] = "LONG" if side > 0 else "SHORT"
                        stop = bar.close - side * self.stop_n * n_val
                        sig.stop_loss[j] = stop
                        units.append(_Unit(j, stop))
                        last_ref, last_n = bar.close, n_val
                else:
                    want = 0
                    if c20 is not None:
                        if bar.close > c20[0]:
                            want = 1
                        elif self.allow_short and bar.close < c20[1]:
                            want = -1
                    if want:
                        failsafe = c55 is not None and (
                            bar.close > c55[0] if want > 0 else bar.close < c55[1]
                        )
                        skipped = self.skip_after_winner and shadow_won is True and not failsafe
                        stop = bar.close - want * self.stop_n * n_val
                        if not skipped:
                            sig.entries[j] = True
                            sig.direction[j] = "LONG" if want > 0 else "SHORT"
                            sig.stop_loss[j] = stop
                            units = [_Unit(j, stop)]
                            side, last_ref, last_n = want, bar.close, n_val
                        if shadow is None:
                            shadow = _Shadow(j, want, bar.close, stop)

            hi_hist.append(bar.high)
            lo_hist.append(bar.low)
            prev_close = bar.close
        return sig

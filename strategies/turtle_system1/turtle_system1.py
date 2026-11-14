"""
Turtle Trading System 1 (Richard Dennis / William Eckhardt, as published by
Curtis Faith, "Way of the Turtle", and the original "Turtle Trading Rules").

Published rules implemented here (daily bars):

- ``N``  = Turtle ATR: ``N = (19 * previous N + TR) / 20`` seeded with the mean
  of the first 20 true ranges (bar 0 uses ``high - low``).
- Entry ("System 1"): the 20-day breakout.  LONG when the close exceeds the
  highest high of the prior 20 bars, SHORT when it falls below the lowest low.
- System 1 filter: a 20-day breakout is SKIPPED when the previous 20-day
  breakout (taken or not - tracked as a hypothetical single-unit trade) was a
  winner.  The 55-day breakout is the failsafe and is always taken.
- Pyramiding: add one unit for every ``0.5 N`` the market moves in favour of
  the last unit, up to ``max_units`` (4) per direction.
- Stop: ``2 N`` from each unit's entry.
- Exit: the 10-day opposite channel (close below the lowest low of the prior
  10 bars for LONG, mirrored for SHORT) closes ALL units.
- Unit size: 1 % of equity per ``N`` of price movement, i.e. a ``2 N`` stop
  risks 2 % of equity per unit -> run with ``risk_per_trade_pct=0.02``.

Deviations from the original rules (platform execution model, documented):

- Breakouts are CLOSE based (the originals are intraday stop orders at the
  channel) and fill at the NEXT bar open; stop levels are anchored on the
  signal bar's close (``close -/+ 2 N``), not on the fill price.
- Each unit keeps its own ``2 N`` stop; the original ratchets every earlier
  unit's stop to ``2 N`` below the LAST unit (``0.5 N`` pyramid step).  The
  platform cannot move the stop of an open position from a signal.
- The 10-day exit is a close-triggered signal exit (fills at that close), not
  an intrabar stop order at the channel.
- Strategy-side state assumes every signalled unit fills; if the risk engine
  sizes a unit to zero (aggregate 5 % open-risk ceiling) the strategy still
  counts it.  The parity tests pin the engine behaviour for that case.
"""

import math

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "Turtle System 1",
    "version": "1.0.0",
    "source_url": "https://www.tradingblox.com/originalturtles/originalturtlerules.htm",
    "parameters": {
        "entry_len": 20,
        "exit_len": 10,
        "failsafe_len": 55,
        "atr_len": 20,
        "stop_n": 2.0,
        "add_n": 0.5,
        "max_units": 4,
        "skip_after_winner": True,
        "allow_short": True,
    },
    "notes": (
        "- Published Turtle System 1 rules; close-based breakout with next-bar-open fills.\n"
        "- Per-unit 2N stop (no ratchet of earlier units); 10-day exit is a signal exit.\n"
        "- Run with risk_per_trade_pct=0.02 so one unit equals 1% of equity per N."
    ),
    "tags": {
        "type": ["breakout", "trend-following"],
        "source": "turtle-trading-rules",
        "asset_class": ["equities", "futures"],
        "complexity": "medium",
    },
}


def _turtle_n(high, low, close, length):
    """Turtle N: smoothed true range, ``N = ((len-1) * N_prev + TR) / len``."""
    n = len(close)
    out = [math.nan] * n
    tr = [math.nan] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    if n < length:
        return out
    seed = sum(tr[:length]) / length
    out[length - 1] = seed
    for i in range(length, n):
        out[i] = ((length - 1) * out[i - 1] + tr[i]) / length
    return out


def _prior_extreme(values, length, fn):
    """``fn`` over the ``length`` bars BEFORE bar i (NaN until available)."""
    n = len(values)
    out = [math.nan] * n
    for i in range(length, n):
        out[i] = fn(values[i - length : i])
    return out


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    entry_len = int(params.get("entry_len", 20))
    exit_len = int(params.get("exit_len", 10))
    failsafe_len = int(params.get("failsafe_len", 55))
    atr_len = int(params.get("atr_len", 20))
    stop_n = float(params.get("stop_n", 2.0))
    add_n = float(params.get("add_n", 0.5))
    max_units = int(params.get("max_units", 4))
    skip_after_winner = bool(params.get("skip_after_winner", True))
    allow_short = bool(params.get("allow_short", True))

    high = [float(v) for v in df["high"]]
    low = [float(v) for v in df["low"]]
    close = [float(v) for v in df["close"]]
    size = len(close)

    n_series = _turtle_n(high, low, close, atr_len)
    up_entry = _prior_extreme(high, entry_len, max)
    dn_entry = _prior_extreme(low, entry_len, min)
    up_fail = _prior_extreme(high, failsafe_len, max)
    dn_fail = _prior_extreme(low, failsafe_len, min)
    up_exit = _prior_extreme(high, exit_len, max)
    dn_exit = _prior_extreme(low, exit_len, min)

    entries = [False] * size
    exits = [False] * size
    direction = [None] * size
    stop_loss = [math.nan] * size

    # actual position state: list of unit stops, side, last reference/N
    units: list[dict] = []
    side = 0  # +1 long / -1 short / 0 flat
    last_ref = math.nan
    last_n = math.nan
    # hypothetical single-unit tracker for the System 1 skip rule
    hypo: dict | None = None
    last_hypo_win: bool | None = None

    def exit_hit(s, j):
        if s > 0:
            return not math.isnan(dn_exit[j]) and close[j] < dn_exit[j]
        return not math.isnan(up_exit[j]) and close[j] > up_exit[j]

    for j in range(size):
        n_j = n_series[j]
        # 1) per-unit stops on units signalled on an EARLIER bar
        if units:
            survivors = []
            for unit in units:
                if unit["bar"] < j:
                    hit = low[j] <= unit["stop"] if side > 0 else high[j] >= unit["stop"]
                    if hit:
                        continue
                survivors.append(unit)
            units = survivors
            if not units:
                side = 0
        # 2) hypothetical trade: stop, then channel exit
        if hypo is not None and hypo["bar"] < j:
            s = hypo["side"]
            stopped = low[j] <= hypo["stop"] if s > 0 else high[j] >= hypo["stop"]
            if stopped:
                last_hypo_win = (hypo["stop"] - hypo["ref"]) * s > 0
                hypo = None
        if hypo is not None and hypo["bar"] < j and exit_hit(hypo["side"], j):
            last_hypo_win = (close[j] - hypo["ref"]) * hypo["side"] > 0
            hypo = None
        # 3) signal exit closes all actual units
        if side != 0 and exit_hit(side, j):
            exits[j] = True
            units = []
            side = 0
        if math.isnan(n_j):
            continue
        # 4) pyramid add-on or fresh breakout entry
        if side != 0:
            if len(units) < max_units and (close[j] - last_ref) * side >= add_n * last_n:
                entries[j] = True
                direction[j] = "LONG" if side > 0 else "SHORT"
                stop = close[j] - side * stop_n * n_j
                stop_loss[j] = stop
                units.append({"bar": j, "stop": stop})
                last_ref, last_n = close[j], n_j
        else:
            long_break = not math.isnan(up_entry[j]) and close[j] > up_entry[j]
            short_break = allow_short and not math.isnan(dn_entry[j]) and close[j] < dn_entry[j]
            fail_long = not math.isnan(up_fail[j]) and close[j] > up_fail[j]
            fail_short = allow_short and not math.isnan(dn_fail[j]) and close[j] < dn_fail[j]
            s = 1 if long_break else (-1 if short_break else 0)
            if s != 0:
                skip = skip_after_winner and last_hypo_win is True
                failsafe = fail_long if s > 0 else fail_short
                if not skip or failsafe:
                    entries[j] = True
                    direction[j] = "LONG" if s > 0 else "SHORT"
                    stop = close[j] - s * stop_n * n_j
                    stop_loss[j] = stop
                    units = [{"bar": j, "stop": stop}]
                    side = s
                    last_ref, last_n = close[j], n_j
                if hypo is None:
                    hypo = {"bar": j, "side": s, "ref": close[j], "stop": close[j] - s * stop_n * n_j}

    idx = df.index
    indicators = {
        "N": pd.Series(n_series, index=idx),
        "upper_20": pd.Series(up_entry, index=idx),
        "lower_20": pd.Series(dn_entry, index=idx),
        "exit_high_10": pd.Series(up_exit, index=idx),
        "exit_low_10": pd.Series(dn_exit, index=idx),
        "stop_loss": pd.Series(stop_loss, index=idx),
        "direction": pd.Series(direction, index=idx, dtype="object"),
    }
    return (
        pd.Series(entries, index=idx),
        pd.Series(exits, index=idx),
        indicators,
        [],
    )

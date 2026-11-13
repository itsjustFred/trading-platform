"""Bar-spacing helpers for strategies whose clock exits must fill at a given open.

Since the 2026-11-13 owner decision a signal exit fills at the NEXT bar's open
(``signal_exit_fill="next_open"``). A strategy that wants to be flat *at* a clock
time T (for example an end-of-day flatten at 15:45) must therefore raise its exit
signal on the bar BEFORE the one that opens at T, i.e. on the first bar whose END
reaches T. That needs the bar length, which is a property of the data feed (the
modal bar spacing) - never a look at a later bar - plus the known clock schedule,
so a signal on bar ``t`` does not depend on bars after ``t``.

Usage::

    spacing = modal_bar_minutes(utc_times)
    fire = clock_exit_signal_mask(minute_of_day, spacing, exit_minute)

A missing bar cannot be anticipated: if the bar before ``T`` is absent the signal
comes on the first bar that exists from there on (the fill is then one bar after
the ideal open, never earlier).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def modal_bar_minutes(utc_times: pd.DatetimeIndex | pd.Series) -> float:
    """Most frequent positive spacing of the bars in minutes (0.0 if unknown).

    Gaps (weekends, holidays, missing bars) are larger steps and do not win the
    vote, so the result is the feed's nominal bar length.
    """
    index = pd.DatetimeIndex(pd.to_datetime(utc_times, utc=True, errors="coerce"))
    valid = index[~index.isna()]
    if len(valid) < 2:
        return 0.0
    steps = np.diff(valid.to_numpy()).astype("timedelta64[s]").astype(np.int64)
    steps = steps[steps > 0]
    if steps.size == 0:
        return 0.0
    values, counts = np.unique(steps, return_counts=True)
    return float(values[counts == counts.max()].min()) / 60.0


def clock_exit_signal_mask(
    minute_of_day: pd.Series | np.ndarray,
    bar_minutes: float,
    exit_minute: float,
) -> pd.Series | np.ndarray:
    """True on every bar whose end reaches ``exit_minute`` (minute of day).

    On such a bar a next-open exit fills at the first open at/after the exit
    time: the bar before the exit minute when it exists, the first existing bar
    otherwise. ``bar_minutes`` of 0 degenerates to the plain ``>=`` test.
    """
    return (minute_of_day + float(bar_minutes)) >= float(exit_minute)

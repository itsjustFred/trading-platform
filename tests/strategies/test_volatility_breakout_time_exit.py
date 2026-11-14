"""force_close / session_exit timing of the volatility breakout (signal level).

Signal exits fill at the NEXT bar's open, so both flattens are raised one bar
early (bar spacing + clock only) and carry no explicit exit_price.
"""

import numpy as np
import pandas as pd
import pytest

from strategies.volatility_breakout.volatility_breakout import generate_signals


def _frame(freq: str, *, days: int = 7, drop=()) -> pd.DataFrame:
    step = pd.Timedelta(freq)
    stamps = []
    for d in pd.bdate_range("2024-01-02", periods=days):
        base = pd.Timestamp(f"{d.date()} 08:00", tz="America/New_York")
        n = int(pd.Timedelta(hours=8) / step)
        stamps += [(base + step * k).tz_convert("UTC") for k in range(n)]
    df = pd.DataFrame({"timestamp": stamps})
    close = np.full(len(df), 100.0)
    ny = df["timestamp"].dt.tz_convert("America/New_York")
    last_day = ny.dt.date == ny.dt.date.iloc[-1]
    in_session = (ny.dt.hour * 60 + ny.dt.minute >= 8 * 60 + 30) & (ny.dt.hour * 60 + ny.dt.minute < 15 * 60)
    close[(last_day & in_session).to_numpy()] = 110.0  # far above rthOpen + 0.3*ATR
    df["open"] = 100.0
    df["close"] = close
    df["volume"] = 1.0
    # Last day: the 08:30 bar opens at 100 (= rthOpen = protective stop), every other
    # in-session bar trades at 110 with its low above the stop -> LONG entry, no stop.
    first_bar = last_day & (ny.dt.hour * 60 + ny.dt.minute == 8 * 60 + 30)
    df.loc[last_day & in_session & ~first_bar, "open"] = 110.0
    df["high"] = np.maximum(df["open"], df["close"]) + 1.0
    df["low"] = np.minimum(df["open"], df["close"]) - 1.0
    df.loc[last_day & in_session & ~first_bar, "low"] = 109.0
    # out-of-session bars after the session stay above the stop as well (so the
    # session-exit safeguard, not a stop, is what flattens)
    after = last_day & ~in_session & (ny.dt.hour * 60 + ny.dt.minute >= 15 * 60)
    df.loc[after, ["open", "close", "high", "low"]] = [110.0, 110.0, 111.0, 109.0]
    if drop:
        keep = ~ny.dt.strftime("%H:%M").isin(list(drop)) | ~last_day
        df = df[keep.to_numpy()].reset_index(drop=True)
    return df


def _et(df, i) -> str:
    return df.loc[i, "timestamp"].tz_convert("America/New_York").strftime("%H:%M")


@pytest.mark.parametrize(("freq", "expected"), [("1min", "13:59"), ("5min", "13:55"), ("30min", "13:30")])
def test_force_close_is_raised_on_the_bar_before_session_end_minus_60(freq, expected):
    df = _frame(freq)
    entries, exits, indicators, _ = generate_signals(df, {})
    assert entries.sum() >= 1
    exit_idx = exits[exits].index.tolist()
    assert [_et(df, i) for i in exit_idx][:1] == [expected]
    assert indicators["exit_reason"].loc[exit_idx[0]] == "force_close"
    assert pd.isna(indicators["exit_price"].loc[exit_idx[0]])


@pytest.mark.parametrize(("freq", "expected"), [("1min", "14:58"), ("5min", "14:50"), ("30min", "14:00")])
def test_session_exit_is_raised_on_the_second_to_last_in_session_bar(freq, expected):
    df = _frame(freq)
    entries, exits, indicators, _ = generate_signals(df, {"force_close_in_min": 0})
    assert entries.sum() >= 1
    exit_idx = exits[exits].index.tolist()
    assert [_et(df, i) for i in exit_idx][:1] == [expected]
    assert indicators["exit_reason"].loc[exit_idx[0]] == "session_exit"
    assert pd.isna(indicators["exit_price"].loc[exit_idx[0]])


def test_no_entry_from_the_session_exit_bar_onward():
    df = _frame("30min")
    entries, _, _, _ = generate_signals(df, {"force_close_in_min": 0})
    ny = df["timestamp"].dt.tz_convert("America/New_York")
    late = (ny.dt.hour * 60 + ny.dt.minute) >= 14 * 60
    assert not entries[late].any()


def test_position_left_open_after_the_session_is_flattened_on_the_next_bar():
    # Remove the in-session bars from 14:00 on: the session exit never gets its
    # signal bar, so the first out-of-session bar flattens (safeguard).
    df = _frame("30min", drop=("14:00", "14:30"))
    _, exits, indicators, _ = generate_signals(df, {"force_close_in_min": 0})
    exit_idx = exits[exits].index.tolist()
    assert exit_idx, "a position left open must still be flattened"
    assert indicators["exit_reason"].loc[exit_idx[0]] == "session_exit"
    assert _et(df, exit_idx[0]) == "15:00"

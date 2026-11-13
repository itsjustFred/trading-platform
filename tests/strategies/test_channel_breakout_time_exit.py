"""Session-close timing of the channel breakout under next_open fills.

Signal level, synthetic frames (no market data). A signal exit fills at the
next bar's open, so the session-close flatten is raised on the second-to-last
bar of the NY session (feed schedule + bar spacing, no look-ahead); market
exits carry no explicit exit_price.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

STRATEGY_PATH = (
    Path(__file__).parent.parent.parent / "strategies" / "channel_breakout" / "channel_breakout.py"
)

_PARAMS = dict(
    channel_length=3,
    ema_period=5,
    atr_period=5,
    use_ema_filter=False,
    use_channel_exit=False,
    use_ema_exit=False,
    use_gap_filter=False,
    exit_on_session_close=True,
)


def _load():
    spec = importlib.util.spec_from_file_location("channel_breakout_under_test", str(STRATEGY_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(days, freq, *, start_hhmm="09:30", bars=14, breakout_bar=15, truncate=None) -> pd.DataFrame:
    step = pd.Timedelta(freq)
    stamps = []
    for day in days:
        base = pd.Timestamp(f"{day} {start_hhmm}", tz="America/New_York")
        stamps += [(base + step * k).tz_convert("UTC") for k in range(bars)]
    stamps = pd.DatetimeIndex(stamps)
    n = len(stamps)
    close = np.full(n, 100.0)
    close[breakout_bar] = 102.0
    close[breakout_bar + 1 :] = 102.0 + 0.1 * np.arange(1, n - breakout_bar)
    series = pd.Series(close)
    df = pd.DataFrame(
        {"timestamp": stamps, "open": series, "high": series + 0.05, "low": series - 0.05, "close": series, "volume": 1.0}
    )
    if truncate is not None:
        df = df.iloc[:truncate].reset_index(drop=True)
    return df


def _et(df, idx) -> str:
    return df.loc[idx, "timestamp"].tz_convert("America/New_York").strftime("%m-%d %H:%M")


def test_session_close_is_raised_on_the_second_to_last_bar_of_each_session():
    module = _load()
    df = _frame(["2024-01-02", "2024-01-03"], "30min")
    entries, exits, indicators, _ = module.generate_signals(df, _PARAMS)
    assert int(entries.sum()) == 1
    (exit_idx,) = exits[exits].index.tolist()
    # day 2 has 14 bars 09:30..16:00; second-to-last starts 15:30, last bar 16:00
    assert _et(df, exit_idx) == "01-03 15:30"
    assert indicators["exit_reason"].loc[exit_idx] == "session_close"


def test_session_close_has_no_explicit_exit_price_for_market_exits():
    module = _load()
    df = _frame(["2024-01-02", "2024-01-03"], "30min")
    _, exits, indicators, _ = module.generate_signals(df, _PARAMS)
    assert indicators["exit_price"].isna().loc[exits].all()


def test_no_entry_is_signalled_from_the_session_close_bar_onward():
    module = _load()
    # breakout on the second-to-last bar of day 1 would fill at the last bar's open
    # and then be carried overnight: it must not be taken.
    df = _frame(["2024-01-02", "2024-01-03"], "30min", breakout_bar=12)
    entries, _, _, _ = module.generate_signals(df, dict(_PARAMS, channel_length=3))
    assert not entries.iloc[12:14].any()


def test_one_minute_feed_signals_the_second_to_last_minute():
    module = _load()
    df = _frame(["2024-01-02", "2024-01-03"], "1min", start_hhmm="23:00", bars=60, breakout_bar=70)
    entries, exits, _, _ = module.generate_signals(df, _PARAMS)
    assert int(entries.sum()) == 1
    # NY calendar day 01-02 ends at 23:59 (bars 23:00..23:59); day 2 starts 23:00 too,
    # but that stamp belongs to 01-03 -> its last bar is 23:59, the signal 23:58.
    (exit_idx,) = exits[exits].index.tolist()
    assert _et(df, exit_idx) == "01-03 23:58"


def test_day_ending_early_flattens_on_the_first_bar_of_the_next_session():
    module = _load()
    # day 1 normal (14 bars), day 2 enters and then the session ends 2 bars early
    # (half day) - the modal schedule never fires; the next session's first bar does.
    df = _frame(["2024-01-02", "2024-01-03", "2024-01-04"], "30min", breakout_bar=15)
    short_day = df["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%m-%d %H:%M")
    drop = short_day.isin(["01-03 15:30", "01-03 16:00"])
    df = df[~drop].reset_index(drop=True)
    _, exits, indicators, _ = module.generate_signals(df, _PARAMS)
    exit_idx = exits[exits].index.tolist()[0]  # later exits belong to the next day's trade
    assert _et(df, exit_idx) == "01-04 09:30"
    assert indicators["exit_reason"].loc[exit_idx] == "session_close"

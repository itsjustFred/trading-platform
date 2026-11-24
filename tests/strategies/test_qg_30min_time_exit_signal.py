"""Forced-exit timing of the QG-30Min ES breakout (signal level, no market data).

Signal exits fill at the NEXT bar's open, so the 14:55 ET forced exit is raised
on the first bar whose end reaches 14:55 (1m: 14:54) and carries no explicit
exit_price (a market exit has no level).
"""

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

STRATEGY_PATH = (
    Path(__file__).parent.parent.parent
    / "strategies"
    / "qg_30min_es_breakout_strategy"
    / "qg_30min_es_breakout_strategy.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("qg_30min_under_test", str(STRATEGY_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(freq: str) -> pd.DataFrame:
    # Warm-up (>= 50 bars before the entry) in front of 2024-01-02 (EST = UTC-5).
    # Warm-up bars have low == open, so breakout_long == low and no entry fires
    # there; on the target day the first in-window bar (08:00 ET) is the entry.
    entry_ts = pd.Timestamp("2024-01-02 13:00", tz="UTC")  # 08:00 ET
    start = entry_ts - 60 * pd.Timedelta(freq) - pd.Timedelta(hours=1)
    stamps = pd.date_range(start, pd.Timestamp("2024-01-02 21:30", tz="UTC"), freq=freq, inclusive="left")
    df = pd.DataFrame(
        {"timestamp": stamps, "open": 100.0, "high": 101.0, "low": 100.0, "close": 100.0, "volume": 10.0}
    )
    day = df["timestamp"] >= entry_ts
    df.loc[day, "low"] = 99.5
    df.loc[df["timestamp"] == entry_ts, "low"] = 99.0
    return df


@pytest.mark.parametrize(("freq", "expected"), [("1min", "14:54"), ("5min", "14:50"), ("30min", "14:30"), ("1h", "14:00")])
def test_forced_exit_signal_is_the_bar_before_1455(freq, expected):
    module = _load()
    df = _frame(freq)
    entries, exits, indicators, _ = module.generate_signals(df, {})
    assert entries.sum() == 1
    (exit_idx,) = exits[exits].index.tolist()
    assert df.loc[exit_idx, "timestamp"].tz_convert("America/New_York").strftime("%H:%M") == expected
    assert indicators["exit_reason"].loc[exit_idx] == "time_exit_1455_ny"
    # market exit: no explicit price (it would clash with the next-open fill)
    assert indicators["exit_price"].isna().loc[exit_idx]


def test_reason_label_follows_the_forced_exit_parameters():
    module = _load()
    df = _frame("1min")
    _, exits, indicators, _ = module.generate_signals(
        df, {"forced_exit_hour_ny": 14, "forced_exit_minute_ny": 30}
    )
    (exit_idx,) = exits[exits].index.tolist()
    assert indicators["exit_reason"].loc[exit_idx] == "time_exit_1430_ny"
    assert df.loc[exit_idx, "timestamp"].tz_convert("America/New_York").strftime("%H:%M") == "14:29"

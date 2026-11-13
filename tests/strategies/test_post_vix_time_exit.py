"""Clock-exit timing of post_vix_dip_buy (14:00 power hour / 15:50 EOD) under next_open.

Signal exits fill at the next bar's open, so the exits are raised on the first bar
whose END reaches the clock time; no explicit market-close exit_price. Synthetic
panic window with a monkeypatched VIX series - no market data needed.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shared.bar_timing import modal_bar_minutes_by_group

STRATEGY_PATH = str(
    Path(__file__).parent.parent.parent / "strategies" / "post_vix_dip_buy" / "post_vix_dip_buy.py"
)
N_DAYS = 205
START = "2023-12-25"
PANIC_IDX = 200
INTRADAY_IDX = 203  # Mon 2024-07-15


def _module():
    spec = importlib.util.spec_from_file_location("post_vix_time_exit_fixture", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vix() -> pd.Series:
    start = pd.Timestamp(START, tz="UTC")
    idx = pd.DatetimeIndex([start + pd.Timedelta(days=i) for i in range(N_DAYS)])
    vix = pd.Series(15.0, index=idx)
    vix.iloc[PANIC_IDX] = 25.0
    vix.iloc[PANIC_IDX + 1 :] = 20.0
    return vix


def _frame(freq: str, *, drop_after: str | None = None) -> pd.DataFrame:
    start = pd.Timestamp(START, tz="UTC")
    dates = [(start + pd.Timedelta(days=i)).date() for i in range(N_DAYS)]
    trend = [100.0 + 0.05 * i for i in range(N_DAYS)]
    trend[PANIC_IDX] = trend[PANIC_IDX - 1] * 0.98
    rows = []
    for i in range(N_DAYS):
        if i == INTRADAY_IDX:
            continue
        ts = (pd.Timestamp(dates[i]) + pd.Timedelta(hours=21)).tz_localize("UTC")
        rows.append((ts, trend[i], trend[i] + 0.5, trend[i] - 0.5, trend[i], 1_000_000.0))
    step = pd.Timedelta(freq)
    n_bars = int(pd.Timedelta(hours=6, minutes=30) / step)  # 09:30 .. 16:00
    ny = pd.date_range(pd.Timestamp("2024-07-15 09:30", tz="America/New_York"), periods=n_bars, freq=freq)
    for k, ts in enumerate(ny):
        if ts.hour == 9 and ts.minute < 60:
            o, h, low, c = 110.0, 110.2, 109.8, 110.0
        else:
            c = 110.5 + 0.01 * k
            o, h, low = c - 0.01, c + 0.02, c - 0.02
        if drop_after and ts.strftime("%H:%M") >= drop_after:
            continue
        rows.append((ts.tz_convert("UTC"), o, h, low, c, 1000.0))
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    if drop_after:
        # next day's daily stub bar must not trip the protective stop
        stub = df["timestamp"] == pd.Timestamp("2024-07-16 21:00", tz="UTC")
        df.loc[stub, ["open", "high", "low", "close"]] = [110.6, 110.7, 110.5, 110.6]
    return df


def _signals(freq, monkeypatch, **kwargs):
    module = _module()
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: _vix())
    df = _frame(freq, **kwargs)
    entries, exits, indicators, _ = module.generate_signals(df, {})
    return df, entries, exits, indicators


def _et(df, i) -> str:
    return df.loc[i, "timestamp"].tz_convert("America/New_York").strftime("%H:%M")


@pytest.mark.parametrize(("freq", "expected"), [("5min", "13:55"), ("15min", "13:45"), ("30min", "13:30")])
def test_power_hour_exit_is_raised_on_the_bar_before_1400(freq, expected, monkeypatch):
    df, entries, exits, indicators = _signals(freq, monkeypatch)
    assert entries.sum() >= 1
    (exit_idx,) = [i for i in exits[exits].index if df.loc[i, "timestamp"].date() == pd.Timestamp("2024-07-15").date()]
    assert _et(df, exit_idx) == expected
    assert indicators["exit_reason"].loc[exit_idx] == "scale_50pct_power_hour"
    assert pd.isna(indicators["exit_price"].loc[exit_idx])  # market exit: no level


def test_market_exits_never_carry_the_signal_bar_close(monkeypatch):
    df, _, exits, indicators = _signals("5min", monkeypatch)
    market = indicators["exit_reason"].isin(["scale_50pct_power_hour", "eod_flatten"]) & exits
    assert market.any()
    assert indicators["exit_price"][market].isna().all()


def test_position_still_open_on_a_later_day_is_flattened_on_its_first_bar(monkeypatch):
    # the 5m bars from 13:00 are missing -> neither the 14:00 nor the 15:50 signal bar
    # exists; the next NY day's first bar flattens (eod_flatten), never skipped.
    df, entries, exits, indicators = _signals("5min", monkeypatch, drop_after="13:00")
    assert entries.sum() >= 1
    exit_idx = [i for i in exits[exits].index if indicators["exit_reason"].loc[i] == "eod_flatten"]
    assert exit_idx, "stale position must be flattened"
    assert df.loc[exit_idx[0], "timestamp"].tz_convert("America/New_York").date() > pd.Timestamp("2024-07-15").date()


def test_per_group_spacing_is_taken_inside_each_day():
    ts = pd.DatetimeIndex(
        ["2024-01-02 21:00", "2024-01-03 21:00"]
        + [f"2024-01-04 {h:02d}:{m:02d}" for h in (14, 15) for m in (0, 30)]
        + ["2024-01-05 21:00"],
        tz="UTC",
    )
    groups = np.array(["a", "b", "c", "c", "c", "c", "d"])
    assert modal_bar_minutes_by_group(ts, groups).tolist() == [0.0, 0.0, 30.0, 30.0, 30.0, 30.0, 0.0]

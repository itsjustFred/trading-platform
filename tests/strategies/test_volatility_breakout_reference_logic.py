"""Reference-logic test for the source-faithful Volatility Breakout port.

Source: optimized_version_vola_breackout nt8.txt — daily ATR(5, previous bar),
levels rthOpen +/- 0.3*ATR, stop at session open, session 8:30-15:00 NY with
force-close 60 min before session end, no ATR-regime filter.

The previous version of this test encoded the invented MT5-style behavior
(16:30/18:05 entry clock, ATR-regime gate); it was rewritten with the
source-faithful fix.
"""

import pandas as pd

from strategies.volatility_breakout.volatility_breakout import generate_signals


def _bars() -> pd.DataFrame:
    # 8 NY days of 30m bars across the full UTC day (covers 8:30-15:00 NY).
    days = pd.date_range("2024-01-08", periods=8, freq="D", tz="UTC")
    rows = []
    for di, d in enumerate(days):
        idx = pd.date_range(d, periods=48, freq="30min", tz="UTC")
        base = 100.0 + di * 0.5
        px = pd.Series(base, index=range(48), dtype=float)
        rows.append(
            pd.DataFrame(
                {
                    "timestamp": idx,
                    "open": px.values,
                    "high": px.values + 1.0,
                    "low": px.values - 1.0,
                    "close": px.values,
                    "volume": 100,
                }
            )
        )
    df = pd.concat(rows, ignore_index=True)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    ny = ts.dt.tz_convert("America/New_York")
    minutes = ny.dt.hour * 60 + ny.dt.minute
    in_session = (minutes >= 8 * 60 + 30) & (minutes < 15 * 60)
    day7 = ny.dt.date == ny.iloc[6 * 48].date()
    day8 = ny.dt.date == ny.iloc[7 * 48].date()
    # Day 7: close above the long level through the session -> LONGs.
    df.loc[day7 & in_session, "close"] = 105.0
    df.loc[day7 & in_session, "high"] = 105.5
    df.loc[day7 & in_session, "low"] = 104.6
    # Day 8: close below the short level through the session -> SHORTs.
    df.loc[day8 & in_session, "close"] = 102.0
    df.loc[day8 & in_session, "high"] = 102.4
    df.loc[day8 & in_session, "low"] = 101.5
    return df


def test_source_session_entries_direction_and_session_open_stop():
    entries, exits, indicators, _ = generate_signals(_bars(), {})

    entry_idx = entries[entries].index.tolist()
    assert len(entry_idx) > 0
    ts = pd.to_datetime(_bars()["timestamp"], utc=True)
    ny = ts.dt.tz_convert("America/New_York")
    minutes = ny.dt.hour * 60 + ny.dt.minute
    # Entries only inside 8:30-15:00 NY.
    assert all(8 * 60 + 30 <= minutes.iloc[i] < 15 * 60 for i in entry_idx)

    dirs = set(indicators["direction"].iloc[entry_idx].tolist())
    assert dirs == {"LONG", "SHORT"}

    # Stop AT SESSION OPEN on every entry.
    for i in entry_idx:
        assert indicators["sl_price"].iloc[i] == indicators["Session_Open"].iloc[i]
        assert indicators["stop_loss"].iloc[i] == indicators["Session_Open"].iloc[i]

    # Bands are symmetric around the session open with the 0.3 offset.
    width = (indicators["Upper_Band"] - indicators["Lower_Band"]).dropna()
    expect = (2 * 0.3 * indicators["ATR"]).dropna()
    pd.testing.assert_series_equal(
        width.loc[expect.index], expect, check_names=False, atol=1e-9
    )
    # Bands are centred on the session open (midpoint complements width).
    mid = ((indicators["Upper_Band"] + indicators["Lower_Band"]) / 2).dropna()
    centre = indicators["Session_Open"].loc[mid.index]
    pd.testing.assert_series_equal(mid, centre, check_names=False, atol=1e-9)

    # No invented regime filter anymore.
    assert "ATR_Regime_OK" not in indicators
    assert "ATR_Short" not in indicators
    assert "ATR_Long" not in indicators

    # Every exit carries price + reason (NaN-PnL cascade guard).
    exit_idx = exits[exits].index.tolist()
    assert len(exit_idx) > 0
    assert int(indicators["exit_price"].iloc[exit_idx].isna().sum()) == 0
    assert int(pd.Series(indicators["exit_reason"].iloc[exit_idx]).isna().sum()) == 0
    reasons = set(pd.Series(indicators["exit_reason"].iloc[exit_idx]).tolist())
    assert reasons <= {"stop_loss", "force_close", "session_exit"}

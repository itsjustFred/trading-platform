"""
Unit + Integration tests for Intraday Volatility Breakout strategy.
"""

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.vectorbt_adapter import _normalize_signal_output

STRATEGY_PATH = (
    Path(__file__).parent.parent.parent
    / "strategies"
    / "intraday_volatility_breakout"
    / "intraday_volatility_breakout.py"
)


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ivb", str(STRATEGY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_df(n_bars: int = 100, start: str = "2024-01-01 15:00") -> pd.DataFrame:
    """Build a synthetic M1 dataframe covering one trading day."""
    idx = pd.date_range(start=start, periods=n_bars, freq="1min")
    np.random.seed(42)
    base = 1.1000
    noise = np.cumsum(np.random.randn(n_bars) * 0.0001)
    close = base + noise
    open_ = close + np.random.randn(n_bars) * 0.00005
    high = np.maximum(open_, close) + np.abs(np.random.randn(n_bars)) * 0.0001
    low = np.minimum(open_, close) - np.abs(np.random.randn(n_bars)) * 0.0001
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }
    )


def _two_day_breakout_df(*, direction: str) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01 00:00", periods=2880, freq="1min")
    close = np.full(len(idx), 1.1000)
    open_ = np.full(len(idx), 1.1000)
    high = np.full(len(idx), 1.1010)
    low = np.full(len(idx), 1.0990)
    entry_mask = (idx >= pd.Timestamp("2024-01-02 16:30")) & (
        idx < pd.Timestamp("2024-01-02 17:30")
    )
    after_entry_mask = idx >= pd.Timestamp("2024-01-02 17:30")
    if direction == "down":
        close[entry_mask] = np.linspace(1.1000, 1.0980, entry_mask.sum())
        close[after_entry_mask] = 1.0980
        open_[entry_mask] = close[entry_mask] + 0.00001
        open_[after_entry_mask] = 1.0980
    else:
        close[entry_mask] = np.linspace(1.1000, 1.1020, entry_mask.sum())
        close[after_entry_mask] = 1.1020
        open_[entry_mask] = close[entry_mask] - 0.00001
        open_[after_entry_mask] = 1.1020
    high[entry_mask] = close[entry_mask] + 0.0001
    high[after_entry_mask] = close[after_entry_mask] + 0.0001
    low[entry_mask] = close[entry_mask] - 0.0001
    low[after_entry_mask] = close[after_entry_mask] - 0.0001
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }
    )


# ---------------------------------------------------------------------------
# Module-load smoke test
# ---------------------------------------------------------------------------
def test_module_loads():
    mod = _load_module()
    assert hasattr(mod, "STRATEGY_META")
    assert mod.STRATEGY_META["name"] == "intraday_volatility_breakout"
    assert hasattr(mod, "generate_signals")


def test_mt5_atr_is_a_simple_moving_average_of_true_range():
    # MT5's built-in iATR is SMA(TR, period) (running sum / period), not Wilder.
    # Hand-computed: TR = 1, 2, 3, 4, 5, 10, 7 with period 5.
    mod = _load_module()
    true_range = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 7.0])

    atr = mod._mt5_atr(true_range, period=5)

    assert atr.iloc[:4].isna().all()
    assert atr.iloc[4] == pytest.approx(15.0 / 5)  # 1+2+3+4+5
    assert atr.iloc[5] == pytest.approx(24.0 / 5)  # 2+3+4+5+10
    assert atr.iloc[6] == pytest.approx(29.0 / 5)  # 3+4+5+10+7
    # Wilder would give 4.4 and 4.92 here - the value the EA logs is the SMA.
    assert atr.iloc[5] != pytest.approx(((3.0 * 4) + 10.0) / 5)


def test_mt5_atr_matches_an_ea_logged_daily_atr():
    # Real reference day: the Dukascopy-demo tester log (EURUSD H1 run) prints
    # "2025.06.09 16:30:00 Levels calculated | ... | Stop: 0.0022519999999999966",
    # so the EA's iATR(D1, 5) at shift 1 was 4 * Stop = 0.009008. The five D1 bars
    # before it (2025-06-02..06, server clock) have true ranges 0.01063, 0.00905,
    # 0.00776, 0.00903, 0.00857 - their plain mean is exactly that value.
    mod = _load_module()
    true_range = pd.Series([0.00617, 0.01740, 0.00774, 0.01063, 0.00905, 0.00776, 0.00903, 0.00857])
    logged_atr = 4 * 0.0022519999999999966

    sma = mod._mt5_atr(true_range, period=5)
    wilder = mod._wilder_atr(true_range, period=5)

    assert sma.iloc[-1] == pytest.approx(logged_atr, rel=1e-12)
    assert abs(wilder.iloc[-1] - logged_atr) / logged_atr > 0.01


def test_wilder_atr_uses_sma_seed_then_wilder_recurrence():
    mod = _load_module()
    true_range = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 7.0])

    atr_full = mod._wilder_atr(true_range, period=5)
    assert atr_full.iloc[:4].isna().all()

    atr = atr_full.dropna()
    seed = true_range.iloc[:5].mean()
    first_update = ((seed * 4) + true_range.iloc[5]) / 5

    assert atr.iloc[0] == pytest.approx(seed)
    assert atr.iloc[1] == pytest.approx(first_update)
    assert atr.iloc[2] == pytest.approx(((first_update * 4) + true_range.iloc[6]) / 5)


@pytest.mark.parametrize("atr_name", ["_mt5_atr", "_wilder_atr"])
def test_atr_short_input_stays_nan_until_a_full_window_is_available(atr_name):
    mod = _load_module()
    true_range = pd.Series([1.0, 2.0, 3.0, 4.0], name="true_range")

    atr = getattr(mod, atr_name)(true_range, period=5)

    assert atr.isna().all()
    assert atr.index.equals(true_range.index)
    assert atr.name == true_range.name


def test_mt5_atr_skips_nan_gaps_without_emitting_an_atr_for_the_gap():
    mod = _load_module()
    true_range = pd.Series([np.nan, 1.0, 2.0, 3.0, np.nan, 6.0])

    atr = mod._mt5_atr(true_range, period=3)

    assert atr.iloc[:3].isna().all()
    assert atr.iloc[3] == pytest.approx(2.0)
    assert np.isnan(atr.iloc[4])
    assert atr.iloc[5] == pytest.approx((2.0 + 3.0 + 6.0) / 3)


def test_wilder_atr_skips_nan_gaps_without_emitting_an_atr_for_the_gap():
    mod = _load_module()
    true_range = pd.Series([np.nan, 1.0, 2.0, 3.0, np.nan, 6.0])

    atr = mod._wilder_atr(true_range, period=3)

    assert atr.iloc[:3].isna().all()
    assert atr.iloc[3] == pytest.approx(2.0)
    assert np.isnan(atr.iloc[4])
    assert atr.iloc[5] == pytest.approx(((2.0 * 2) + 6.0) / 3)


@pytest.mark.parametrize("atr_name", ["_mt5_atr", "_wilder_atr"])
def test_atr_period_one_preserves_values_and_nan_gaps(atr_name):
    mod = _load_module()
    true_range = pd.Series([1.0, np.nan, "3.5"], name="true_range")

    atr = getattr(mod, atr_name)(true_range, period=1)

    expected = pd.Series([1.0, np.nan, 3.5], name="true_range")
    pd.testing.assert_series_equal(atr, expected)


# ---------------------------------------------------------------------------
# Parameter defaults
# ---------------------------------------------------------------------------
def test_default_parameters_present():
    mod = _load_module()
    params = mod.STRATEGY_META["parameters"]
    expected = {
        "atr_period": 5,
        "atr_multiplier": 0.25,
        "stop_atr_multiplier": 0.25,
        "use_candle_close": True,
        "confirmation_timeframe": "15m",
        "atr_short_period": 1,
        "atr_long_period": 5,
        "atr_regime_factor": 1.0,
        "regime_filter_mode": "low",
        "trade_direction": "both",
        "reverse_mode": False,
        "fixed_risk_amount": 500.0,
        "entry_time": "16:30",
        "max_entry_time": "18:05",
        "exit_time": "22:55",
        "session_timezone": "",
        "session_timezone_offset_hours": 0,
        "atr_smoothing": "sma",
        "broker_clock": "",
        "daily_weekend_bars": "auto",
        "daily_skip_filler_bars": True,
    }
    for key, val in expected.items():
        assert params.get(key) == val, f"param {key} expected {val}, got {params.get(key)}"


# ---------------------------------------------------------------------------
# Basic signal generation
# ---------------------------------------------------------------------------
def test_generate_signals_returns_tuple():
    mod = _load_module()
    df = _make_df(200)
    params = mod.STRATEGY_META["parameters"].copy()
    result = mod.generate_signals(df, params)
    assert isinstance(result, tuple)
    assert len(result) == 4
    entries, exits, indicators, drawings = result
    assert isinstance(entries, pd.Series)
    assert isinstance(exits, pd.Series)
    assert isinstance(indicators, dict)
    assert isinstance(drawings, list)
    assert len(entries) == len(df)
    assert len(exits) == len(df)


# ---------------------------------------------------------------------------
# One trade per day
# ---------------------------------------------------------------------------
def test_one_trade_per_day():
    mod = _load_module()
    # Create a 2-day synthetic dataset where price breaks levels multiple times
    idx = pd.date_range("2024-01-01 16:00", periods=300, freq="1min")
    # Day 1: strong uptrend inside entry window
    close = np.concatenate([
        np.linspace(1.1000, 1.1000, 30),   # before entry
        np.linspace(1.1000, 1.1020, 95),   # entry window, breaks up
        np.linspace(1.1020, 1.1020, 100),  # after max entry, flat
        np.linspace(1.1020, 1.1020, 75),   # next day before entry
    ])
    open_ = close + 0.00001
    high = close + 0.0002
    low = close - 0.0002
    df = pd.DataFrame({
        "timestamp": idx,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    })
    params = mod.STRATEGY_META["parameters"].copy()
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "16:40"
    params["exit_time"] = "20:00"
    params["regime_filter_mode"] = "low"
    params["use_candle_close"] = False

    entries, exits, indicators, _ = mod.generate_signals(df, params)
    entry_indices = entries[entries].index
    # At most one entry per day
    days = df.loc[entry_indices, "timestamp"].dt.date
    assert days.nunique() == len(entry_indices), (
        f"Expected at most one entry per day, got entries on days: {list(days)}"
    )


# ---------------------------------------------------------------------------
# Reverse mode
# ---------------------------------------------------------------------------
def test_reverse_mode_flips_signals():
    mod = _load_module()
    df = _two_day_breakout_df(direction="down")
    base_params = mod.STRATEGY_META["parameters"].copy()
    base_params["use_candle_close"] = False
    base_params["entry_time"] = "16:30"
    base_params["max_entry_time"] = "18:00"
    base_params["exit_time"] = "20:00"
    base_params["atr_regime_factor"] = 2.0
    base_params["atr_period"] = 1
    base_params["atr_long_period"] = 1

    # Normal mode: short should trigger (price drops below lower)
    params_normal = {**base_params, "reverse_mode": False}
    entries_normal, _, indicators_normal, _ = mod.generate_signals(df, params_normal)
    dirs_normal = indicators_normal["direction"]
    assert entries_normal.any()
    assert (dirs_normal == "SHORT").any(), "Expected a SHORT entry in normal mode"

    # Reverse mode: the same breakout should produce a LONG
    params_reverse = {**base_params, "reverse_mode": True}
    entries_reverse, _, indicators_reverse, _ = mod.generate_signals(df, params_reverse)
    dirs_reverse = indicators_reverse["direction"]
    assert entries_reverse.any()
    assert (dirs_reverse == "LONG").any(), "Expected a LONG entry in reverse mode"


def test_mt5_numeric_direction_and_regime_settings_are_normalized():
    mod = _load_module()
    df = _two_day_breakout_df(direction="down")
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "Trade_Direction": 0,
            "Regime_Filter_Mode": 0,
            "use_candle_close": False,
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "20:00",
            "atr_regime_factor": 2.0,
            "atr_period": 1,
            "atr_long_period": 1,
        }
    )

    entries, _, indicators, _ = mod.generate_signals(df, params)

    assert entries.any()
    assert "direction" in indicators
    assert (indicators["direction"].dropna() == "SHORT").any()


def test_session_timezone_uses_dst_aware_server_day():
    mod = _load_module()
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2018-01-04T13:30:00Z",
                    "2018-07-04T13:30:00Z",
                ],
                utc=True,
            ),
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "session_timezone": "Europe/Athens",
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "22:55",
            "use_candle_close": False,
        }
    )

    _, _, indicators, _ = mod.generate_signals(df, params)

    entry_open = indicators["entry_open"]
    # MT5 parity (iBarShift with exact=false): the entry open is the NEAREST
    # bar to 16:30 server time per day, not the first bar at/after it. Each
    # synthetic day holds a single bar, which is trivially its own nearest
    # bar — even the 15:30 (pre-entry) bar resolves to its own open.
    assert entry_open.iloc[0] == 1.0
    assert entry_open.iloc[1] == 1.0


def test_entry_open_uses_nearest_bar_on_gaps():
    mod = _load_module()
    idx = pd.to_datetime(
        [
            "2024-01-02T16:28:00Z",
            "2024-01-02T16:29:00Z",
            # 16:30 and 16:31 bars missing (data gap, cf. 2026-04-28 USATECH)
            "2024-01-02T16:32:00Z",
            "2024-01-02T16:33:00Z",
        ],
        utc=True,
    )
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [1.0, 2.0, 3.0, 4.0],
            "low": [1.0, 2.0, 3.0, 4.0],
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "22:55",
        }
    )

    _, _, indicators, _ = mod.generate_signals(df, params)

    # Nearest to 16:30 is 16:29 (1 min away beats 16:32 at 2 min) — matches
    # MT5 iBarShift, which picked the 16:29 open on 2026-04-28 USATECH.
    assert (indicators["entry_open"] == 2.0).all()


def test_entry_open_tie_prefers_earlier_bar():
    mod = _load_module()
    idx = pd.to_datetime(
        [
            "2024-01-02T16:29:00Z",
            "2024-01-02T16:31:00Z",
        ],
        utc=True,
    )
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": [1.0, 3.0],
            "high": [1.0, 3.0],
            "low": [1.0, 3.0],
            "close": [1.0, 3.0],
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "22:55",
        }
    )

    _, _, indicators, _ = mod.generate_signals(df, params)

    assert (indicators["entry_open"] == 1.0).all()


# ---------------------------------------------------------------------------
# Trade direction filters
# ---------------------------------------------------------------------------
def test_short_only_blocks_long_breakouts():
    mod = _load_module()
    df = _two_day_breakout_df(direction="up")
    params = mod.STRATEGY_META["parameters"].copy()
    params["trade_direction"] = "short_only"
    params["use_candle_close"] = False
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "18:00"
    params["exit_time"] = "20:00"
    params["atr_regime_factor"] = 2.0

    entries, _, indicators, _ = mod.generate_signals(df, params)
    active_dirs = indicators["direction"].dropna()
    assert not entries.any()
    assert active_dirs.empty


def test_reverse_long_only_fades_lower_breakout():
    # MT5 parity: EvaluateReverseSignals checks the direction permission
    # against the side actually traded — long_only + reverse may only take
    # the fade LONG on lower-level touches, never a SHORT on upper touches.
    mod = _load_module()
    df = _two_day_breakout_df(direction="up")
    params = mod.STRATEGY_META["parameters"].copy()
    params["trade_direction"] = "long_only"
    params["reverse_mode"] = True
    params["use_candle_close"] = False
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "18:00"
    params["exit_time"] = "20:00"
    params["atr_regime_factor"] = 2.0
    params["atr_period"] = 1
    params["atr_long_period"] = 1

    entries, _, indicators, _ = mod.generate_signals(df, params)
    active_dirs = indicators["direction"].dropna()
    assert not entries.any()
    assert active_dirs.empty


def test_reverse_short_only_fades_upper_breakout():
    # Mirror: short_only + reverse fades the upper breakout into a SHORT.
    mod = _load_module()
    df = _two_day_breakout_df(direction="up")
    params = mod.STRATEGY_META["parameters"].copy()
    params["trade_direction"] = "short_only"
    params["reverse_mode"] = True
    params["use_candle_close"] = False
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "18:00"
    params["exit_time"] = "20:00"
    params["atr_regime_factor"] = 2.0
    params["atr_period"] = 1
    params["atr_long_period"] = 1

    entries, _, indicators, _ = mod.generate_signals(df, params)
    active_dirs = indicators["direction"].dropna()
    assert entries.any()
    assert (active_dirs == "SHORT").all()


# ---------------------------------------------------------------------------
# sl_price presence
# ---------------------------------------------------------------------------
def test_sl_price_present_in_indicators():
    mod = _load_module()
    # Build a 2-day dataframe so daily ATR (shift 1) is available on day 2.
    # 2 days * 1440 bars/day = 2880 bars at 1-minute resolution.
    idx = pd.date_range("2024-01-01 00:00", periods=2880, freq="1min")
    close = np.concatenate([
        np.full(990, 1.1000),           # day 1 before entry (00:00-16:29)
        np.linspace(1.1000, 1.1030, 40),   # day 1 entry window (strong up)
        np.full(410, 1.1030),             # day 1 after max entry
        np.full(990, 1.1030),             # day 2 before entry
        np.linspace(1.1030, 1.1060, 40),   # day 2 entry window (strong up)
        np.full(410, 1.1060),             # day 2 after max entry
    ])
    open_ = close + 0.00001
    high = close + 0.0002
    low = close - 0.0002
    df = pd.DataFrame({
        "timestamp": idx,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    })
    params = mod.STRATEGY_META["parameters"].copy()
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "16:40"
    params["exit_time"] = "17:30"
    params["use_candle_close"] = False
    # Day 1 has a wide range, so short-term ATR == long-term ATR. Loosen the
    # regime factor to make the low-volatility filter pass on day 2.
    params["atr_regime_factor"] = 2.0
    params["atr_period"] = 1
    params["atr_long_period"] = 1
    entries, exits, indicators, _ = mod.generate_signals(df, params)
    assert "sl_price" in indicators
    sl = indicators["sl_price"]
    entry_sl = sl[entries]
    assert entry_sl.notna().any(), "Expected some non-NaN sl_price values at entry bars"


# ---------------------------------------------------------------------------
# Regime filter effect
# ---------------------------------------------------------------------------
def test_regime_filter_blocks_when_condition_not_met():
    mod = _load_module()
    idx = pd.date_range("2024-01-01 16:30", periods=60, freq="1min")
    close = np.linspace(1.1000, 1.1020, 60)
    open_ = close - 0.00001
    high = close + 0.0001
    low = close - 0.0001
    df = pd.DataFrame({
        "timestamp": idx,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    })
    params = mod.STRATEGY_META["parameters"].copy()
    params["use_candle_close"] = False
    params["entry_time"] = "16:30"
    params["max_entry_time"] = "18:00"
    params["exit_time"] = "20:00"
    # Force regime filter to high mode with extreme factor -> no trades
    params["regime_filter_mode"] = "high"
    params["atr_regime_factor"] = 1e9
    params["atr_short_period"] = 1
    params["atr_long_period"] = 5

    entries, _, _, _ = mod.generate_signals(df, params)
    assert not entries.any(), "Expected zero entries when regime filter is impossible"


# ---------------------------------------------------------------------------
# Exit coverage
# ---------------------------------------------------------------------------
def test_exit_time_and_eod_present():
    mod = _load_module()
    df = _make_df(200)
    params = mod.STRATEGY_META["parameters"].copy()
    _, exits, _, _ = mod.generate_signals(df, params)
    assert exits.any(), "Expected at least some exit signals"
    # Last bar should always be an exit (EOD safety net)
    assert exits.iloc[-1], "Expected last bar to be an exit (EOD safety)"


# ---------------------------------------------------------------------------
# Vectorbt adapter compatibility
# ---------------------------------------------------------------------------
def test_adapter_normalization():
    mod = _load_module()
    df = _make_df(200)
    params = mod.STRATEGY_META["parameters"].copy()
    entries, exits, indicators, drawings = mod.generate_signals(df, params)
    signals, norm_indicators, norm_drawings = _normalize_signal_output(
        (entries, exits, indicators, drawings),
        df.index,
        symbol="TEST",
    )
    assert "entries" in signals.columns
    assert "exits" in signals.columns
    assert len(norm_indicators) > 0
    assert isinstance(norm_drawings, list)


def test_candle_close_signal_emits_previous_bar_for_next_open_execution():
    mod = _load_module()
    idx = pd.date_range("2024-01-01 00:00", periods=3 * 24 * 4, freq="15min", tz="UTC")
    close = np.full(len(idx), 100.0)
    open_ = np.full(len(idx), 100.0)
    high = np.full(len(idx), 100.2)
    low = np.full(len(idx), 99.8)
    breakout_bar = idx.get_loc(pd.Timestamp("2024-01-03 16:45", tz="UTC"))
    close[breakout_bar] = 110.0
    open_[breakout_bar + 1] = 110.0
    close[breakout_bar + 1] = 110.0
    high[breakout_bar] = 110.2
    high[breakout_bar + 1] = 110.2

    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "use_candle_close": True,
            "confirmation_timeframe": "15m",
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "22:55",
            "atr_multiplier": 0.25,
            "atr_regime_factor": 2.0,
            "atr_period": 1,
            "atr_long_period": 1,
        }
    )

    entries, _, indicators, _ = mod.generate_signals(df, params)
    entry_times = df.loc[entries[entries].index, "timestamp"].tolist()

    assert pd.Timestamp("2024-01-03 17:00", tz="UTC") not in entry_times
    assert pd.Timestamp("2024-01-03 16:45", tz="UTC") in entry_times
    entry_idx = df.index[df["timestamp"] == pd.Timestamp("2024-01-03 16:45", tz="UTC")][0]
    assert indicators["direction"].iloc[entry_idx] == "LONG"
    assert indicators["entry_reference_price"].iloc[entry_idx] == pytest.approx(
        df["close"].iloc[entry_idx + 1]
    )
    assert pd.notna(indicators["sl_price"].iloc[entry_idx])
    assert pd.notna(indicators["stop_distance"].iloc[entry_idx])
    assert indicators["entry_reference_price"].iloc[entry_idx] == pytest.approx(
        df["close"].iloc[entry_idx + 1]
    )


def test_entry_indicators_include_signal_contract_metadata():
    mod = _load_module()
    df = _two_day_breakout_df(direction="up")
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "use_candle_close": False,
            "entry_time": "16:30",
            "max_entry_time": "18:00",
            "exit_time": "20:00",
            "atr_regime_factor": 2.0,
            "atr_period": 1,
            "atr_long_period": 1,
        }
    )

    entries, exits, indicators, _ = mod.generate_signals(df, params)

    assert entries.any()
    entry_idx = entries[entries].index[0]
    entry_timestamp = pd.Timestamp(df.loc[entry_idx, "timestamp"], tz="UTC")
    assert indicators["signal_id"].loc[entry_idx] == (
        f"intraday_volatility_breakout:{entry_timestamp.isoformat()}:LONG"
    )
    assert indicators["strategy_id"].loc[entry_idx] == "intraday_volatility_breakout"
    assert indicators["order_timing"].loc[entry_idx] == "next_bar_open"
    assert indicators["order_type"].loc[entry_idx] == "market"
    assert indicators["entry_reference_price"].loc[entry_idx] == pytest.approx(
        df.loc[entry_idx, "high"]
    )

    assert exits.any()
    exit_idx = exits[exits].index[0]
    assert indicators["exit_reason"].loc[exit_idx] == "time_exit"


def test_native_daily_ohlcv_overrides_intraday_resampled_atr():
    mod = _load_module()
    idx = pd.date_range("2024-01-01", periods=72, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": np.full(len(idx), 100.0),
            "high": np.full(len(idx), 101.0),
            "low": np.full(len(idx), 99.0),
            "close": np.full(len(idx), 100.0),
        }
    )
    daily_ohlcv = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
            "open": [100.0, 105.0, 110.0],
            "high": [110.0, 125.0, 120.0],
            "low": [100.0, 105.0, 110.0],
            "close": [105.0, 110.0, 115.0],
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update(
        {
            "atr_period": 1,
            "atr_short_period": 1,
            "atr_long_period": 1,
            "atr_regime_factor": 2.0,
        }
    )

    _, _, resampled_indicators, _ = mod.generate_signals(df, params)
    native_params = dict(params)
    native_params["daily_ohlcv"] = daily_ohlcv
    _, _, native_indicators, _ = mod.generate_signals(df, native_params)

    jan3 = df["timestamp"] == pd.Timestamp("2024-01-03 12:00", tz="UTC")
    assert resampled_indicators["daily_atr"].loc[jan3].iloc[0] == pytest.approx(2.0)
    assert native_indicators["daily_atr"].loc[jan3].iloc[0] == pytest.approx(20.0)


def test_invalid_session_timezone_fails_closed():
    mod = _load_module()
    params = mod.STRATEGY_META["parameters"].copy()
    params["session_timezone"] = "Not/A_Real_Zone"

    with pytest.raises(ValueError, match="Invalid session_timezone"):
        mod.generate_signals(_make_df(20), params)


def test_missing_timestamp_and_non_datetime_index_fails_closed():
    mod = _load_module()
    params = mod.STRATEGY_META["parameters"].copy()
    frame = _make_df(20).drop(columns=["timestamp"]).reset_index(drop=True)

    with pytest.raises(ValueError, match="timestamp column or datetime index"):
        mod.generate_signals(frame, params)


def test_fixed_utc_offset_is_supported_when_timezone_is_blank():
    mod = _load_module()

    zone = mod._resolve_session_zone("", 2)

    assert zone.utcoffset(None) == timedelta(hours=2)
    with pytest.raises(ValueError, match="cannot be combined"):
        mod._resolve_session_zone("Europe/Athens", 2)


# ---------------------------------------------------------------------------
# MT5 daily-ATR semantics: smoothing, trading-day D1 bars, broker clock
# ---------------------------------------------------------------------------
def _one_bar_per_day(rows):
    """rows: (utc timestamp, open, high, low, close) -> one bar per row."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([r[0] for r in rows], utc=True),
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
        }
    )


def _atr_params(**overrides):
    mod = _load_module()
    params = mod.STRATEGY_META["parameters"].copy()
    params.update({"atr_period": 2, "atr_long_period": 2, "use_candle_close": False})
    params.update(overrides)
    return mod, params


# Thu, Fri, a stray Saturday filler print, Mon, Tue (UTC, session clock = UTC).
_WEEKEND_ROWS = [
    ("2024-01-04 12:00", 100.0, 102.0, 98.0, 100.0),  # TR 4 (first bar: high-low)
    ("2024-01-05 12:00", 100.0, 105.0, 99.0, 104.0),  # TR max(6, 5, 1) = 6
    ("2024-01-06 12:00", 104.5, 104.5, 104.5, 104.5),  # Sat: TR 0.5 vs Fri close
    ("2024-01-08 12:00", 104.0, 106.0, 102.0, 103.0),  # Mon: TR 4 (prev close 104 or 104.5)
    ("2024-01-09 12:00", 103.0, 104.0, 102.0, 103.0),
]


def test_daily_atr_drops_weekend_rows_by_default():
    mod, params = _atr_params()
    df = _one_bar_per_day(_WEEKEND_ROWS)

    _, _, indicators, _ = mod.generate_signals(df, params)

    # Tuesday uses ATR(2) of the last two *trading-day* D1 bars: (Fri 6 + Mon 4) / 2.
    assert indicators["daily_atr"].iloc[4] == pytest.approx(5.0)
    # Monday uses (Thu 4 + Fri 6) / 2 - the Saturday row never takes a window slot.
    assert indicators["daily_atr"].iloc[3] == pytest.approx(5.0)


def test_daily_atr_keeps_weekend_rows_only_when_asked():
    mod, params = _atr_params(daily_weekend_bars="keep")
    df = _one_bar_per_day(_WEEKEND_ROWS)

    _, _, indicators, _ = mod.generate_signals(df, params)

    # (Sat 0.5 + Mon max(4, 1.5, 2.5) = 4) / 2 and (Fri 6 + Sat 0.5) / 2.
    assert indicators["daily_atr"].iloc[4] == pytest.approx(2.25)
    assert indicators["daily_atr"].iloc[3] == pytest.approx(3.25)


def test_atr_smoothing_is_selectable_and_defaults_to_mt5_sma():
    rows = [
        ("2024-01-01 12:00", 100.0, 101.0, 100.0, 100.0),  # TR 1
        ("2024-01-02 12:00", 100.0, 102.0, 100.0, 100.0),  # TR 2
        ("2024-01-03 12:00", 100.0, 103.0, 100.0, 100.0),  # TR 3
        ("2024-01-04 12:00", 100.0, 110.0, 100.0, 100.0),  # TR 10
        ("2024-01-05 12:00", 100.0, 100.0, 100.0, 100.0),
    ]
    df = _one_bar_per_day(rows)

    mod, sma_params = _atr_params(atr_period=3)
    _, _, sma, _ = mod.generate_signals(df, sma_params)
    _, wilder_params = _atr_params(atr_period=3, atr_smoothing="wilder")
    _, _, wilder, _ = mod.generate_signals(df, wilder_params)

    # Friday sees the ATR through Thursday: SMA (2+3+10)/3, Wilder (2*2+10)/3.
    assert sma["daily_atr"].iloc[4] == pytest.approx(5.0)
    assert wilder["daily_atr"].iloc[4] == pytest.approx(14.0 / 3)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"atr_smoothing": "ema"}, "atr_smoothing must be one of"),
        ({"daily_weekend_bars": "sometimes"}, "daily_weekend_bars must be one of"),
        ({"broker_clock": "Europe/Athens"}, "Unknown broker_clock"),
        ({"broker_clock": "ny_close", "session_timezone": "Europe/Athens"}, "cannot be combined"),
        ({"broker_clock": "ny_close", "session_timezone_offset_hours": 2}, "cannot be combined"),
    ],
)
def test_invalid_mt5_daily_settings_fail_closed(overrides, message):
    mod, params = _atr_params(**overrides)
    with pytest.raises(ValueError, match=message):
        mod.generate_signals(_one_bar_per_day(_WEEKEND_ROWS), params)


def test_native_daily_ohlcv_is_refused_with_a_broker_clock():
    mod, params = _atr_params(broker_clock="ny_close")
    params["daily_ohlcv"] = _one_bar_per_day(_WEEKEND_ROWS)
    with pytest.raises(ValueError, match="daily_ohlcv cannot be combined with broker_clock"):
        mod.generate_signals(_one_bar_per_day(_WEEKEND_ROWS), params)


@pytest.mark.parametrize(
    ("day", "entry_utc"),
    [
        ("2023-07-05", "14:30"),  # legacy index history: fixed UTC+2 in summer
        ("2024-04-03", "14:30"),  # transition: still UTC+2 although DST began
        ("2024-11-06", "13:30"),  # autumn-2024 switch came a week late: UTC+3
        ("2025-03-12", "13:30"),  # New York close: UTC+3 from the US switch
        ("2025-11-05", "14:30"),  # New York close: back to UTC+2
    ],
)
def test_broker_clock_places_entry_time_on_the_broker_server_clock(day, entry_utc):
    mod = _load_module()
    idx = pd.date_range(f"{day} 12:00", f"{day} 16:00", freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": np.arange(len(idx), dtype=float),
            "high": np.arange(len(idx), dtype=float) + 0.5,
            "low": np.arange(len(idx), dtype=float) - 0.5,
            "close": np.arange(len(idx), dtype=float),
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params.update({"broker_clock": "dukascopy_mt5_index", "use_candle_close": False})

    _, _, indicators, _ = mod.generate_signals(df, params)

    entry_bar = df["timestamp"] == pd.Timestamp(f"{day} {entry_utc}", tz="UTC")
    expected = df.loc[entry_bar, "open"].iloc[0]
    assert indicators["entry_open"].iloc[0] == expected


def _sunday_evening_rows(sunday: str):
    friday = (pd.Timestamp(sunday) - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    monday = (pd.Timestamp(sunday) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    tuesday = (pd.Timestamp(sunday) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    return [
        (f"{friday} 12:00", 100.0, 105.0, 99.0, 104.0),
        (f"{sunday} 21:30", 104.5, 104.5, 104.5, 104.5),  # 23:30 Sunday on a UTC+2 clock
        (f"{monday} 12:00", 104.0, 106.0, 102.0, 103.0),
        (f"{tuesday} 12:00", 103.0, 104.0, 102.0, 103.0),
    ]


@pytest.mark.parametrize(
    ("sunday", "listed"),
    [("2019-03-10", False), ("2024-03-10", True)],
)
def test_broker_clock_weekend_bars_follow_the_broker_history(sunday, listed):
    df = _one_bar_per_day(_sunday_evening_rows(sunday))
    common = {"broker_clock": "dukascopy_mt5_forex", "atr_period": 1, "atr_long_period": 1}
    mod, auto = _atr_params(**common)
    _, keep = _atr_params(**common, daily_weekend_bars="keep")
    _, drop = _atr_params(**common, daily_weekend_bars="drop")

    atr = {name: mod.generate_signals(df, p)[2]["daily_atr"].iloc[3] for name, p in
           (("auto", auto), ("keep", keep), ("drop", drop))}

    # Tuesday's ATR(1) is Monday's TR; Monday's previous close is Friday's (104) when
    # the Sunday bar is absent and the Sunday print (104.5) when the broker lists it.
    assert atr["drop"] == pytest.approx(4.0)
    assert atr["keep"] == pytest.approx(4.0)  # max(4, 1.5, 2.5)
    # Monday itself: the previous D1 bar is Sunday (TR 0.5) only if it is listed.
    monday_auto = mod.generate_signals(df, auto)[2]["daily_atr"].iloc[2]
    assert monday_auto == pytest.approx(0.5 if listed else 6.0)


def test_broker_clock_refuses_bars_inside_a_fall_back_overlap():
    mod = _load_module()
    # ny_close falls back at 2025-11-02 06:00 UTC (09:00 -> 08:00 server).
    idx = pd.date_range("2025-11-02 05:30", "2025-11-02 06:30", freq="10min", tz="UTC")
    df = pd.DataFrame({"timestamp": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    params = mod.STRATEGY_META["parameters"].copy()
    params["broker_clock"] = "ny_close"
    with pytest.raises(ValueError, match="non-monotonic server clock"):
        mod.generate_signals(df, params)


def _with_volume(df, volumes):
    out = df.copy()
    out["volume"] = volumes
    return out


def test_flat_zero_volume_filler_bars_do_not_form_d1_bars():
    # A holiday padded with flat zero-volume filler (Wed) between two trading days.
    rows = [
        ("2024-12-23 12:00", 100.0, 102.0, 98.0, 100.0),  # TR 4
        ("2024-12-24 12:00", 100.0, 105.0, 99.0, 104.0),  # TR 6
        ("2024-12-25 12:00", 104.0, 104.0, 104.0, 104.0),  # filler: TR would be 0
        ("2024-12-26 12:00", 104.0, 106.0, 102.0, 103.0),  # TR 4
        ("2024-12-27 12:00", 103.0, 104.0, 102.0, 103.0),
    ]
    df = _with_volume(_one_bar_per_day(rows), [5.0, 5.0, 0.0, 5.0, 5.0])
    mod, params = _atr_params()
    _, _, skipped, _ = mod.generate_signals(df, params)
    _, kept_params = _atr_params(daily_skip_filler_bars=False)
    _, _, kept, _ = mod.generate_signals(df, kept_params)

    # Friday: ATR(2) of the last two traded D1 bars, (6 + 4) / 2 - versus (0 + 4) / 2.
    assert skipped["daily_atr"].iloc[4] == pytest.approx(5.0)
    assert kept["daily_atr"].iloc[4] == pytest.approx(2.0)


def test_filler_needs_both_zero_volume_and_a_flat_bar():
    rows = [
        ("2024-12-23 12:00", 100.0, 102.0, 98.0, 100.0),
        ("2024-12-24 12:00", 100.0, 105.0, 99.0, 104.0),
        ("2024-12-25 12:00", 104.0, 104.0, 104.0, 104.0),  # flat but traded (volume 1)
        ("2024-12-26 12:00", 104.0, 106.0, 102.0, 103.0),
        ("2024-12-27 12:00", 103.0, 104.0, 102.0, 103.0),
    ]
    mod, params = _atr_params()
    traded_flat = _with_volume(_one_bar_per_day(rows), [5.0, 5.0, 1.0, 5.0, 5.0])
    no_volume_column = _one_bar_per_day(rows)

    for df in (traded_flat, no_volume_column):
        _, _, indicators, _ = mod.generate_signals(df, params)
        assert indicators["daily_atr"].iloc[4] == pytest.approx(2.0)  # (TR 0 + TR 4) / 2


def test_broker_clock_overlap_check_ignores_weekend_filler_bars():
    mod = _load_module()
    # Filler bars straddling the 2025-11-02 06:00 UTC New York fall-back (a Sunday),
    # then real bars on Monday.
    filler = pd.date_range("2025-11-02 05:30", "2025-11-02 06:30", freq="10min", tz="UTC")
    real = pd.date_range("2025-11-03 13:00", "2025-11-03 15:00", freq="1min", tz="UTC")
    idx = filler.append(real)
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": np.where(idx.isin(real), 1.001, 1.0),
            "low": 1.0,
            "close": 1.0,
            "volume": np.where(idx.isin(real), 3.0, 0.0),
        }
    )
    params = mod.STRATEGY_META["parameters"].copy()
    params["broker_clock"] = "ny_close"

    entries, _, _, _ = mod.generate_signals(df, params)
    assert len(entries) == len(df)


@pytest.mark.parametrize("unknown", [np.nan, "n/a", np.inf, -np.inf])
def test_non_finite_volume_is_unknown_not_filler_and_warns(unknown, caplog):
    # A flat bar whose volume is non-finite cannot be told apart from a real one-tick
    # minute, so it is kept like a bar of a feed without a volume column (ATR(2) on
    # Friday = (TR 0 + TR 4) / 2 = 2.0) - and the run says so instead of silently
    # letting the filler skip do nothing.
    rows = [
        ("2024-12-23 12:00", 100.0, 102.0, 98.0, 100.0),
        ("2024-12-24 12:00", 100.0, 105.0, 99.0, 104.0),
        ("2024-12-25 12:00", 104.0, 104.0, 104.0, 104.0),
        ("2024-12-26 12:00", 104.0, 106.0, 102.0, 103.0),
        ("2024-12-27 12:00", 103.0, 104.0, 102.0, 103.0),
    ]
    df = _with_volume(_one_bar_per_day(rows), [5.0, 5.0, unknown, 5.0, 5.0])
    mod, params = _atr_params()
    with caplog.at_level("WARNING"):
        _, _, indicators, _ = mod.generate_signals(df, params)
    assert indicators["daily_atr"].iloc[4] == pytest.approx(2.0)
    assert "non-finite volume" in caplog.text


def test_finite_volume_feed_does_not_warn(caplog):
    rows = [
        ("2024-12-23 12:00", 100.0, 102.0, 98.0, 100.0),
        ("2024-12-24 12:00", 100.0, 105.0, 99.0, 104.0),
        ("2024-12-25 12:00", 104.0, 104.0, 104.0, 104.0),
        ("2024-12-26 12:00", 104.0, 106.0, 102.0, 103.0),
    ]
    df = _with_volume(_one_bar_per_day(rows), [5.0, 5.0, 0.0, 5.0])
    mod, params = _atr_params()
    with caplog.at_level("WARNING"):
        mod.generate_signals(df, params)
    assert "non-finite volume" not in caplog.text

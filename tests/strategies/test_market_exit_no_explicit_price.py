"""Market signal exits carry no explicit exit_price (close-triggered strategies).

Under the default ``signal_exit_fill="next_open"`` a signal exit fills at the
NEXT bar's open. The strategies below used to emit ``exit_price = close`` for
their market exits — dead data that also risks being read as a level. They now
leave it unset (only real stop levels are explicit), so the exit fills at the
next bar's open.

Synthetic daily bars with regime swings - no market data needed.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_ohlcv

STRATEGIES_DIR = Path(__file__).parent.parent.parent / "strategies"
STRATEGIES = [
    "trend_following",
    "cross_asset_momentum",
    "risk_parity",
    "pine_supertrend_dema",
    "stat_arb_pairs",
    "sector_rotation",
    "gold_momentum",
    "custom_swings_supertrend",
]
N_BARS = 700


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    t = np.arange(N_BARS)
    # Regime swings (80-bar cycle, +/-40 %) on a 2 % daily random-walk base: every
    # strategy sees entries and at least two signal exits after its own warmup.
    close = 100.0 * np.exp(
        0.4 * np.sin(2 * np.pi * t / 80.0) + np.cumsum(rng.normal(0.0, 0.02, N_BARS))
    )
    open_ = np.r_[close[0], close[:-1]] * (1.0 + rng.normal(0.0, 0.002, N_BARS))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.004, N_BARS)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.004, N_BARS)))
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2022-01-01", periods=N_BARS, freq="D", tz="UTC"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1_000_000.0,
        }
    )


def _load_strategy(name: str):
    path = STRATEGIES_DIR / name / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"market_exit_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", STRATEGIES)
def test_market_exit_bars_carry_no_close_price(name):
    frame = _frame()
    _, exits, indicators, _ = _load_strategy(name).generate_signals(frame, {})
    assert exits.any(), f"{name}: synthetic bars produced no signal exit (check is vacuous)"
    explicit = indicators["exit_price"][exits].dropna()
    close = frame["close"].reindex(explicit.index)
    # Tolerance covers 6-dp rounding (custom_swings_supertrend rounds its prices).
    assert not np.isclose(explicit.values, close.values, rtol=1e-6, atol=1e-9).any(), (
        f"{name}: an exit bar carries exit_price == its close (market exit must stay unset)"
    )


@pytest.mark.parametrize("name", STRATEGIES)
def test_market_exits_fill_at_next_open(tmp_path: Path, name: str):
    """Every market signal exit fills at the bar AFTER the signal, at that
    bar's open — on the native engine, where this is observable."""
    frame = _frame()
    _write_ohlcv(tmp_path, "SYN", frame, timeframe="1d")
    payload = {
        "strategy_name": name,
        "symbols": ["SYN"],
        "timeframe": "1d",
        "start_date": str(frame["timestamp"].iloc[0].date()),
        "end_date": str(frame["timestamp"].iloc[-1].date()),
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    config = BacktestConfig(**with_zero_fees(payload))
    result = NativeAdapter().run(config, str(STRATEGIES_DIR / name / f"{name}.py"))

    assert result.trades, f"{name}: synthetic run produced no trades (check is vacuous)"

    # Expected fill bars: the bar after each signal-exit bar. A signal on the
    # last bar cannot fill next open; the engine closes it end_of_data instead
    # (reason "end_of_data", not "signal"), so it is excluded here.
    _, exits, _, _ = _load_strategy(name).generate_signals(frame, {})
    signal_bars = exits[exits].index.to_numpy()
    ts = frame["timestamp"]
    open_at = dict(zip(ts, frame["open"], strict=True))
    expected_fill_times = {
        ts.iloc[i + 1] for i in signal_bars if i + 1 < len(frame)
    }

    signal_legs = [
        leg
        for trade in result.trades
        for leg in trade.exits
        if leg.reason == "signal"
    ]
    assert signal_legs, f"{name}: no market signal-exit legs booked (check is vacuous)"
    for leg in signal_legs:
        leg_time = pd.Timestamp(leg.time)
        assert leg_time in expected_fill_times, (
            f"{name}: signal exit filled at {leg_time}, which is not the bar "
            "after a signal-exit bar"
        )
        # next_open fill = that bar's open. No spread column is configured, so
        # LONG (bid) and SHORT (ask = open + 0) both fill at the open. These
        # strategies only go LONG on this frame; the SHORT ask-side next-open
        # fill is covered engine-side by
        # tests/test_realistic_fills.py::test_short_signal_exit_pays_the_spread_at_the_next_open.
        assert leg.price == pytest.approx(float(open_at[leg_time]), rel=1e-9)

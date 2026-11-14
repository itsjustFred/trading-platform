"""Market signal exits carry no explicit exit_price (close-triggered strategies).

Under the default ``signal_exit_fill="next_open"`` an explicit ``exit_price`` equal to
the signal bar's close contradicts the next-open fill: the engine drops it, warns and
flags the trade with ``exit_price_ignored_market_close``. The strategies below used to
emit ``exit_price = close`` for their market exits. They now leave it unset (only real
stop levels are explicit), so the exit fills at the next bar's open without a warning.

Synthetic daily bars with regime swings - no market data needed.
"""

import importlib.util
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
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
WARNING_FRAGMENT = "signal bar's close"


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
def test_market_exits_fill_at_next_open_without_warning(tmp_path: Path, caplog, name: str):
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
    with caplog.at_level(logging.WARNING):
        result = VectorbtAdapter().run(config, str(STRATEGIES_DIR / name / f"{name}.py"))

    assert result.trades, f"{name}: synthetic run produced no trades (check is vacuous)"
    assert not [t for t in result.trades if t.meta.get("exit_price_ignored_market_close")]
    assert not [r for r in caplog.records if WARNING_FRAGMENT in r.getMessage()]

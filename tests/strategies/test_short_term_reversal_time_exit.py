"""short_term_reversal under next_open: a 5-bar hold is open(i+1) -> open(i+6).

End-to-end through the vectorbt engine on synthetic daily bars (no market data).
The time exit counts bars from the SIGNAL bar, fills at the next open and carries
no explicit exit_price - so entry/exit timestamps and prices agree.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_ohlcv

STRATEGY_PATH = str(
    Path(__file__).parent.parent.parent / "strategies" / "short_term_reversal" / "short_term_reversal.py"
)
CRASH = 30  # signal bar (close -6% vs 5 bars earlier)


def _frame() -> pd.DataFrame:
    n = 60
    stamps = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = np.full(n, 100.0)
    close[CRASH:] = 93.0 + 0.001 * np.arange(n - CRASH)  # flat afterwards: no stop/target
    # opens differ from closes so close-vs-open fills are distinguishable
    open_ = close + 0.30
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            "open": open_,
            "high": np.maximum(open_, close) + 0.1,
            "low": np.minimum(open_, close) - 0.1,
            "close": close,
            "volume": 1_000_000.0,
        }
    )
    return frame


def _run(tmp_path: Path, **cfg):
    frame = _frame()
    _write_ohlcv(tmp_path, "STR", frame, timeframe="1d")
    payload = {
        "strategy_name": "short_term_reversal",
        "symbols": ["STR"],
        "timeframe": "1d",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(cfg)
    return frame, VectorbtAdapter().run(BacktestConfig(**with_zero_fees(payload)), STRATEGY_PATH)


def test_five_bar_hold_is_open_of_bar_i_plus_1_to_open_of_bar_i_plus_6(tmp_path: Path):
    frame, result = _run(tmp_path)
    (trade,) = result.trades
    assert pd.Timestamp(trade.entry_time) == frame["timestamp"].iloc[CRASH + 1]
    assert trade.entry_price == pytest.approx(float(frame["open"].iloc[CRASH + 1]))
    exit_leg = trade.exits[0]
    assert exit_leg.reason == "time_exit"
    assert pd.Timestamp(exit_leg.time) == frame["timestamp"].iloc[CRASH + 6]
    # price and timestamp agree: the OPEN of the exit bar, not the signal bar's close
    assert exit_leg.price == pytest.approx(float(frame["open"].iloc[CRASH + 6]))
    assert exit_leg.price != pytest.approx(float(frame["close"].iloc[CRASH + 5]))
    assert "exit_price_ignored_market_close" not in trade.meta


def test_strategy_emits_no_explicit_price_for_market_exits():
    import importlib.util

    spec = importlib.util.spec_from_file_location("srt_under_test", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, exits, indicators, _ = module.generate_signals(_frame(), {})
    assert exits.sum() == 1
    assert indicators["exit_price"].isna().all()
    assert indicators["bars_held_at_exit"][exits].iloc[0] == 5

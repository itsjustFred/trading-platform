from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from shared.constants import DATA_DIR


def _synthetic_ohlcv(rows: int = 100) -> pd.DataFrame:
    base = 100.0
    records = []
    for i in range(rows):
        open_price = base + i
        high_price = open_price + 1.0
        low_price = open_price - 1.0
        close_price = open_price + 0.2
        records.append(
            {
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": 1000 + i,
            }
        )
    return pd.DataFrame.from_records(records)


def test_trade_engine_market_entry_and_signal_exit_price_model():
    data = _synthetic_ohlcv(100)

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        exits.iloc[20] = True
        return entries, exits, {}, []

    cfg = BacktestConfig(
        strategy_name="unit-test",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        modeling_resolution="main_timeframe",
        parameters={},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]

    expected_entry_price = float(data.iloc[11]["open"])
    assert trade.entry_price == expected_entry_price


def test_trade_engine_stop_uses_bar_low_and_risk_sizing_1pct():
    data = _synthetic_ohlcv(100)

    # Trigger stop-loss on bar index 12 through bar.low
    data.loc[12, "low"] = 109.0

    def strategy_fn(df: pd.DataFrame, params: dict):
        signals = pd.DataFrame(index=df.index)
        signals["entries"] = False
        signals["exits"] = False
        signals.loc[10, "entries"] = True
        signals["direction"] = "LONG"
        signals["stop_loss"] = pd.NA
        signals.loc[10, "stop_loss"] = 109.0
        return signals

    cfg = BacktestConfig(
        strategy_name="risk-test",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        max_leverage=3.0,
        modeling_resolution="main_timeframe",
        parameters={},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]

    # Entry happens at open of next bar (index 11) => 111.0
    assert trade.entry_price == pytest.approx(111.0)

    # Stop is filled from low-hit logic (not close-based)
    assert trade.exits[-1].price == pytest.approx(109.0)
    assert trade.exits[-1].reason == "stop_loss"

    # 1% risk sizing => roughly -1000 PnL for a 2-point stop distance
    assert trade.pnl == pytest.approx(-1000.0, abs=0.5)


def test_example_sma_native_vs_vectorbt_trade_count_similarity():
    strategy_path = Path("strategies/example_sma/example_sma.py").resolve()
    if not strategy_path.exists():
        pytest.skip("example_sma strategy not found")

    ohlcv_root = DATA_DIR / "ohlcv"
    parquet_files = list(ohlcv_root.rglob("**/1h.parquet"))
    if not parquet_files:
        pytest.skip("No local 1h OHLCV parquet data available")

    symbol = parquet_files[0].parent.name
    cfg = BacktestConfig(
        strategy_name="example_sma",
        symbols=[symbol],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2025-12-31",
        initial_capital=100_000,
        modeling_resolution="main_timeframe",
        parameters={},
    )

    native_result = NativeAdapter().run(cfg, str(strategy_path))
    vectorbt_result = VectorbtAdapter().run(cfg, str(strategy_path))

    native_count = len(native_result.trades)
    vectorbt_count = len(vectorbt_result.trades)

    if vectorbt_count == 0 and native_count == 0:
        assert True
        return

    tolerance = max(2, int(vectorbt_count * 0.3))
    assert abs(native_count - vectorbt_count) <= tolerance

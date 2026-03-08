from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import _enrich_trades_with_context, _normalize_trade_context
from runner.engines.vectorbt_adapter import VectorbtAdapter
from shared.models import Trade, TradeExit

sys.path.insert(0, str(Path(__file__).parent.parent))


REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = REPO_ROOT / "strategies"



def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _make_price_df(periods: int = 500, freq: str = "D") -> pd.DataFrame:
    timestamp = pd.date_range("2023-01-01", periods=periods, freq=freq, tz="UTC")
    close = pd.Series(range(periods), dtype=float) * 0.5 + 100.0
    return pd.DataFrame(
        {
            "timestamp": timestamp,
            "open": close + 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000,
        }
    )



def test_vectorbt_adapter_accepts_indicators_dict_and_sizes_from_sl_price(tmp_path: Path):
    strategy_path = tmp_path / "tuple_strategy.py"
    strategy_path.write_text(
        """
import pandas as pd

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[3] = True
    indicators = {
        'RSI': pd.Series([50, 25, 40, 65, 70], index=df.index),
        'sl_price': pd.Series([None, 95.0, None, None, None], index=df.index),
        'trailing_stop_at_exit': pd.Series([None, None, 99.0, 101.0, None], index=df.index),
        'exit_price': pd.Series([None, None, None, 101.0, None], index=df.index),
        'exit_reason': pd.Series([None, None, None, 'trailing_stop', None], index=df.index),
    }
    return entries, exits, indicators
"""
    )

    data_root = tmp_path / "data"
    parquet_dir = data_root / "ohlcv" / "test_source" / "equities" / "AAPL"
    parquet_dir.mkdir(parents=True)
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"),
            "open": [100, 100, 102, 104, 106],
            "high": [101, 101, 103, 105, 107],
            "low": [99, 99, 101, 103, 105],
            "close": [100, 100, 102, 104, 106],
            "volume": [10, 11, 12, 13, 14],
        }
    )
    df.to_parquet(parquet_dir / "1d.parquet", index=False)

    adapter = VectorbtAdapter()
    result = adapter.run(
        BacktestConfig(
            strategy_name="tuple_strategy",
            symbols=["AAPL"],
            timeframe="1d",
            start_date="2024-01-01",
            end_date="2024-01-05",
            initial_capital=100000.0,
            risk_per_trade_pct=0.01,
            data_dir=str(data_root),
        ),
        str(strategy_path),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.quantity == 200.0
    assert trade.pnl == 200.0
    assert trade.meta["RSI"] == 25
    assert trade.meta["sl_price"] == 95.0
    assert trade.meta["trailing_stop_at_exit"] == 101.0
    assert trade.meta["exit_price"] == 101.0
    assert trade.meta["exit_reason"] == "trailing_stop"
    assert trade.exits[0].price == 101.0
    assert trade.exits[0].reason == "trailing_stop"



def test_all_vectorbt_strategies_return_entries_exits_and_indicators_dict():
    df = _make_price_df()

    for strategy_path in sorted(STRATEGIES_DIR.glob("*.py")):
        if strategy_path.name == "__init__.py" or strategy_path.name.startswith("pynecore_"):
            continue

        module = _load_module(strategy_path)
        output = module.generate_signals(df.copy(), {})
        assert isinstance(output, tuple), strategy_path.name
        assert len(output) == 3, strategy_path.name
        entries, exits, indicators = output
        assert len(pd.Series(entries)) == len(df), strategy_path.name
        assert len(pd.Series(exits)) == len(df), strategy_path.name
        assert isinstance(indicators, dict) and indicators, strategy_path.name



def test_pynecore_trade_context_enrichment_promotes_directional_prices():
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC"),
            "open": [100, 101, 102, 103],
            "high": [101, 102, 103, 104],
            "low": [99, 100, 101, 102],
            "close": [100, 101, 102, 103],
            "volume": [1, 1, 1, 1],
        }
    )
    trade = Trade(
        id=1,
        symbol="AAPL",
        direction="SHORT",
        entry_time="2024-01-02T00:00:00+00:00",
        entry_price=101.0,
        quantity=1.0,
        exits=[TradeExit(time="2024-01-03T00:00:00+00:00", price=102.0, quantity=1.0, pnl=-1.0)],
        pnl=-1.0,
    )
    context = _normalize_trade_context(
        {
            "Fast_SMA": pd.Series([99.0, 100.0, 101.0, 102.0], index=df.index),
            "sl_price_long": pd.Series([98.0, 99.0, 100.0, 101.0], index=df.index),
            "sl_price_short": pd.Series([102.0, 103.0, 104.0, 105.0], index=df.index),
            "tp_price_long": pd.Series([104.0, 105.0, 106.0, 107.0], index=df.index),
            "tp_price_short": pd.Series([96.0, 95.0, 94.0, 93.0], index=df.index),
        },
        df.index,
    )

    _enrich_trades_with_context([trade], context, df)

    assert trade.meta["Fast_SMA"] == 100.0
    assert trade.meta["sl_price"] == 103.0
    assert trade.meta["tp_price"] == 95.0
    assert "sl_price_long" not in trade.meta
    assert "sl_price_short" not in trade.meta

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import (
    _build_drawings,
    _enrich_trades_with_context,
    _load_strategy_artifacts,
    _normalize_trade_context,
)
from runner.engines.vectorbt_adapter import VectorbtAdapter, _resolve_max_leverage
from shared.models import Trade, TradeExit
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR, iter_strategy_files

sys.path.insert(0, str(Path(__file__).parent.parent))


REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR



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
    assert trade.quantity == 142.8571
    assert trade.pnl == -142.86
    assert trade.meta["RSI"] == 25
    assert trade.meta["sl_price"] == 95.0
    assert trade.meta["trailing_stop_at_exit"] == 101.0
    assert trade.meta["exit_price"] == 101.0
    assert trade.meta["exit_reason"] == "trailing_stop"
    assert trade.meta["risk_math"]["risk_target_usd"] == 1000.0
    assert trade.meta["risk_math"]["entry_price"] == trade.entry_price
    assert trade.meta["risk_math"]["sl_price"] == 95.0
    assert trade.meta["risk_math"]["position_size"] == trade.quantity
    assert trade.meta["risk_math"]["capped_by_max_leverage"] is False
    assert trade.exits[0].price == 101.0
    assert trade.exits[0].reason == "trailing_stop"


def test_vectorbt_adapter_emits_supertrend_split_indicator_series(tmp_path: Path):
    strategy_path = tmp_path / "supertrend_strategy.py"
    strategy_path.write_text(
        """
import pandas as pd

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[4] = True
    indicators = {
        "Supertrend_Trend": pd.Series([-1, -1, 1, 1, -1, -1], index=df.index),
        "Supertrend_Lower": pd.Series([None, None, 101.0, 102.0, None, None], index=df.index),
        "Supertrend_Upper": pd.Series([103.0, 104.0, None, None, 106.0, 107.0], index=df.index),
    }
    return entries, exits, indicators
"""
    )

    data_root = tmp_path / "data"
    parquet_dir = data_root / "ohlcv" / "test_source" / "equities" / "AAPL"
    parquet_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC"),
            "open": [100, 101, 102, 103, 104, 105],
            "high": [101, 102, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103, 104],
            "close": [100, 101, 102, 103, 104, 105],
            "volume": [10, 11, 12, 13, 14, 15],
        }
    ).to_parquet(parquet_dir / "1d.parquet", index=False)

    result = VectorbtAdapter().run(
        BacktestConfig(
            strategy_name="supertrend_strategy",
            symbols=["AAPL"],
            timeframe="1d",
            start_date="2024-01-01",
            end_date="2024-01-06",
            initial_capital=1000.0,
            data_dir=str(data_root),
        ),
        str(strategy_path),
    )

    names = {indicator.name for indicator in result.indicators}
    assert "supertrend_up" in names
    assert "supertrend_down" in names

    up = next(indicator for indicator in result.indicators if indicator.name == "supertrend_up")
    down = next(indicator for indicator in result.indicators if indicator.name == "supertrend_down")
    assert any(point.get("value") is None for point in up.data)
    assert any(point.get("value") is None for point in down.data)



def test_all_vectorbt_strategies_return_entries_exits_and_indicators_dict():
    df = _make_price_df()

    for strategy_path in iter_strategy_files(STRATEGIES_DIR):
        if strategy_path.name.startswith("pynecore_"):
            continue
        if strategy_path.name == "tiingo_sentiment.py":
            # Blocked skeleton: generate_signals raises NotImplementedError by
            # design (no news feed); covered by its own blocked-behavior test.
            continue

        module = _load_module(strategy_path)
        output = module.generate_signals(df.copy(), {})
        assert isinstance(output, tuple), strategy_path.name
        assert len(output) in {3, 4}, strategy_path.name
        entries, exits, indicators = output[:3]
        assert len(pd.Series(entries)) == len(df), strategy_path.name
        assert len(pd.Series(exits)) == len(df), strategy_path.name
        assert isinstance(indicators, dict), strategy_path.name
        assert indicators, strategy_path.name
        if len(output) == 4:
            assert isinstance(output[3], list), strategy_path.name


def test_vectorbt_adapter_merges_strategy_meta_parameters(tmp_path: Path):
    strategy_path = tmp_path / "meta_params_strategy.py"
    strategy_path.write_text(
        """
import pandas as pd

STRATEGY_META = {
    "name": "Meta Params",
    "parameters": {"fast": 2, "slow": 4},
}

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[0] = True
    exits.iloc[-1] = True
    indicators = {
        "configured_fast": pd.Series([params["fast"]] * len(df), index=df.index),
        "configured_slow": pd.Series([params["slow"]] * len(df), index=df.index),
    }
    return entries, exits, indicators
"""
    )

    data_root = tmp_path / "data"
    parquet_dir = data_root / "ohlcv" / "test_source" / "equities" / "AAPL"
    parquet_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [100, 101, 102],
            "volume": [10, 11, 12],
        }
    ).to_parquet(parquet_dir / "1d.parquet", index=False)

    result = VectorbtAdapter().run(
        BacktestConfig(
            strategy_name="meta_params_strategy",
            symbols=["AAPL"],
            timeframe="1d",
            start_date="2024-01-01",
            end_date="2024-01-03",
            initial_capital=1000.0,
            data_dir=str(data_root),
            parameters={"slow": 6},
        ),
        str(strategy_path),
    )

    assert result.meta.parameters == {"fast": 2, "slow": 6}
    assert result.trades[0].meta["configured_fast"] == 2
    assert result.trades[0].meta["configured_slow"] == 6


def test_vectorbt_adapter_uses_intrabar_take_profit_and_tracks_timeframes(tmp_path: Path):
    strategy_path = tmp_path / "intrabar_strategy.py"
    strategy_path.write_text(
        """
import pandas as pd

STRATEGY_META = {"name": "Intrabar Strategy"}

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[0] = True
    exits.iloc[2] = True
    indicators = {
        "tp_price": pd.Series([105.0, None, None], index=df.index),
        "sl_price": pd.Series([95.0, None, None], index=df.index),
    }
    return entries, exits, indicators
"""
    )

    data_root = tmp_path / "data"
    symbol_dir = data_root / "ohlcv" / "test_source" / "equities" / "AAPL"
    symbol_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=3, freq="1h", tz="UTC"),
            "open": [100, 101, 110],
            "high": [101, 111, 112],
            "low": [99, 100, 109],
            "close": [100, 110, 111],
            "volume": [10, 11, 12],
        }
    ).to_parquet(symbol_dir / "1h.parquet", index=False)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01T01:15:00Z",
                    "2024-01-01T01:30:00Z",
                    "2024-01-01T02:15:00Z",
                ],
                utc=True,
            ),
            "open": [100.5, 104.0, 104.5],
            "high": [104.5, 105.5, 105.0],
            "low": [100.0, 103.5, 104.0],
            "close": [104.0, 105.0, 104.8],
            "volume": [1, 1, 1],
        }
    ).to_parquet(symbol_dir / "1m.parquet", index=False)

    result = VectorbtAdapter().run(
        BacktestConfig(
            strategy_name="intrabar_strategy",
            symbols=["AAPL"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=1000.0,
            data_dir=str(data_root),
            modeling_resolution="1m",
        ),
        str(strategy_path),
    )

    assert result.meta.data["timeframes"] == ["1h", "1m"]
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[0].reason == "take_profit"
    assert trade.exits[0].price == 105.0
    assert trade.exits[0].time == "2024-01-01T01:30:00+00:00"
    assert trade.meta["modeling_resolution"] == "1m"
    assert trade.pnl == 39.6


def test_vectorbt_adapter_persists_custom_drawings(tmp_path: Path):
    strategy_path = tmp_path / "drawing_strategy.py"
    strategy_path.write_text(
        """
import pandas as pd

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[3] = True
    indicators = {"Fast": pd.Series([1, 2, 3, 4, 5], index=df.index)}
    drawings = [
        {
            "type": "marker",
            "time": df.loc[1, "timestamp"],
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#3fb950",
            "text": "Entry",
        },
        {
            "type": "line",
            "start_time": df.loc[1, "timestamp"],
            "end_time": df.loc[3, "timestamp"],
            "val1": 100.0,
            "val2": 104.0,
            "color": "#58a6ff",
        },
    ]
    return entries, exits, indicators, drawings
"""
    )

    data_root = tmp_path / "data"
    parquet_dir = data_root / "ohlcv" / "test_source" / "equities" / "AAPL"
    parquet_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"),
            "open": [100, 101, 102, 103, 104],
            "high": [101, 102, 103, 104, 105],
            "low": [99, 100, 101, 102, 103],
            "close": [100, 101, 102, 103, 104],
            "volume": [10, 11, 12, 13, 14],
        }
    ).to_parquet(parquet_dir / "1d.parquet", index=False)

    result = VectorbtAdapter().run(
        BacktestConfig(
            strategy_name="drawing_strategy",
            symbols=["AAPL"],
            timeframe="1d",
            start_date="2024-01-01",
            end_date="2024-01-05",
            initial_capital=1000.0,
            data_dir=str(data_root),
        ),
        str(strategy_path),
    )

    assert len(result.drawings) == 2
    assert result.drawings[0]["type"] == "marker"
    assert result.drawings[0]["symbol"] == "AAPL"
    assert result.drawings[0]["time"] == "2024-01-02T00:00:00+00:00"
    assert result.drawings[1]["type"] == "line"
    assert result.drawings[1]["start_time"] == "2024-01-02T00:00:00+00:00"
    assert result.drawings[1]["end_time"] == "2024-01-04T00:00:00+00:00"
    assert result.trades[0].meta["risk_math"]["sizing_mode"] == "full_equity"
    assert result.trades[0].meta["risk_math"]["position_size"] == result.trades[0].quantity


def test_default_max_leverage_is_unlevered():
    assert _resolve_max_leverage(None) == 2.0
    assert _resolve_max_leverage(1.0) == 1.0



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


def test_pynecore_helper_namespace_supports_build_drawings(tmp_path: Path):
    strategy_path = tmp_path / "pyne_drawings.py"
    strategy_path.write_text(
        """
STRATEGY_META = {"name": "Pyne Drawings"}
"""
    )
    helper_path = tmp_path / "pyne_drawings_context.py"
    helper_path.write_text(
        """
def build_trade_context(df, params):
    return {"Fast": df["close"]}

def build_drawings(df, params, trades, symbol):
    return [
        {
            "type": "marker",
            "time": df.loc[0, "timestamp"],
            "position": "aboveBar",
            "shape": "circle",
            "color": "#f59e0b",
            "text": f"{symbol}:{len(trades)}",
        }
    ]
"""
    )

    strategy_name, context_builder, drawings_builder = _load_strategy_artifacts(
        str(strategy_path),
        "fallback",
    )
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC"),
            "close": [100.0, 101.0],
        }
    )
    trade = Trade(
        id=1,
        symbol="SPY",
        direction="LONG",
        entry_time="2024-01-01T00:00:00+00:00",
        entry_price=100.0,
        quantity=1.0,
        exits=[TradeExit(time="2024-01-02T00:00:00+00:00", price=101.0, quantity=1.0, pnl=1.0)],
        pnl=1.0,
    )

    drawings = _build_drawings(drawings_builder, df, {}, [trade], "SPY")

    assert strategy_name == "Pyne Drawings"
    assert context_builder is not None
    assert len(drawings) == 1
    assert drawings[0]["text"] == "SPY:1"
    assert drawings[0]["symbol"] == "SPY"

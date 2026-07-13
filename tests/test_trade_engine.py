from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow.compute as pc
import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from shared.constants import DATA_DIR


def _parse_date_bound(value: str, *, is_end: bool) -> pd.Timestamp:
    raw = str(value or "").strip()
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid date filter value: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if is_end and "T" not in raw and ":" not in raw:
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return pd.Timestamp(parsed)


def _to_utc_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _time_bounds(path: Path) -> tuple[datetime | None, datetime | None]:
    try:
        table = pq.read_table(path, columns=["timestamp"])
    except Exception:
        return None, None
    if table.num_rows == 0:
        return None, None
    column = table["timestamp"]
    try:
        min_scalar = pc.min(column)
        max_scalar = pc.max(column)
    except Exception:
        return None, None
    start = _to_utc_datetime(min_scalar.as_py() if min_scalar is not None else None)
    end = _to_utc_datetime(max_scalar.as_py() if max_scalar is not None else None)
    return start, end


def _has_coverage(path: Path, *, start: str, end: str) -> bool:
    lower, upper = _time_bounds(path)
    if lower is None or upper is None:
        return False
    return lower <= _parse_date_bound(start, is_end=False).to_pydatetime() and upper >= _parse_date_bound(
        end, is_end=True
    ).to_pydatetime()


def _to_timestamp_utc(value: object) -> pd.Timestamp:
    dt = _to_utc_datetime(value)
    if dt is None:
        raise ValueError(f"Invalid date filter value: {value!r}")
    return pd.Timestamp(dt)


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
    assert trade.meta["execution_model"] == {
        "market_order_timing": "next_bar_open",
        "entry_price_side": "open",
        "stop_trigger_side": "bar_low_high",
        "take_profit_trigger_side": "bar_high_low",
        "same_bar_order": "stop_first",
        "intrabar_resolution": None,
        "lower_timeframe_replay": False,
    }
    assert trade.meta["entry_fill"] == {
        "order_type": "market",
        "price_side": "open",
        "timestamp": trade.entry_time,
        "price": trade.entry_price,
    }


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


def test_trade_engine_accepts_sl_price_alias_for_native_stops_and_risk_sizing():
    data = _synthetic_ohlcv(100)
    data.loc[12, "low"] = 109.0

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        indicators = {
            "direction": pd.Series("LONG", index=df.index),
            "sl_price": pd.Series(pd.NA, index=df.index),
        }
        indicators["sl_price"].iloc[10] = 109.0
        return entries, exits, indicators, []

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
    assert trade.entry_price == pytest.approx(111.0)
    assert trade.exits[-1].price == pytest.approx(109.0)
    assert trade.exits[-1].reason == "stop_loss"
    assert trade.pnl == pytest.approx(-1000.0, abs=0.5)


def test_trade_engine_can_anchor_market_stop_to_fill_price_with_stop_distance():
    data = _synthetic_ohlcv(100)
    data.loc[11, "open"] = 120.0
    data.loc[11, "high"] = 121.0
    data.loc[11, "low"] = 119.0
    data.loc[11, "close"] = 120.2
    data.loc[12, "low"] = 118.0

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        indicators = {
            "direction": pd.Series("LONG", index=df.index),
            "sl_price": pd.Series(pd.NA, index=df.index),
            "stop_distance": pd.Series(pd.NA, index=df.index),
        }
        indicators["sl_price"].iloc[10] = 108.2
        indicators["stop_distance"].iloc[10] = 2.0
        return entries, exits, indicators, []

    cfg = BacktestConfig(
        strategy_name="risk-test",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        max_leverage=10.0,
        modeling_resolution="main_timeframe",
        parameters={},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(120.0)
    assert trade.exits[-1].price == pytest.approx(118.0)
    assert trade.exits[-1].reason == "stop_loss"
    assert trade.pnl == pytest.approx(-1000.0, abs=0.5)


def test_trade_engine_can_configure_same_bar_take_profit_priority():
    data = _synthetic_ohlcv(100)
    data.loc[12, "high"] = 113.0
    data.loc[12, "low"] = 109.0

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        indicators = {
            "direction": pd.Series("LONG", index=df.index),
            "stop_loss": pd.Series(pd.NA, index=df.index),
            "take_profit": pd.Series(pd.NA, index=df.index),
        }
        indicators["stop_loss"].iloc[10] = 109.0
        indicators["take_profit"].iloc[10] = 112.0
        return entries, exits, indicators, []

    cfg = BacktestConfig(
        strategy_name="execution-test",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        max_leverage=3.0,
        modeling_resolution="main_timeframe",
        parameters={"same_bar_order": "take_profit_first"},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].price == pytest.approx(112.0)
    assert trade.exits[-1].reason == "tp"
    assert trade.meta["execution_model"]["same_bar_order"] == "take_profit_first"
    assert trade.pnl > 0


def test_example_sma_native_vs_vectorbt_trade_count_similarity():
    strategy_path = Path("strategies/example_sma/example_sma.py").resolve()
    if not strategy_path.exists():
        pytest.skip("example_sma strategy not found")

    ohlcv_root = DATA_DIR / "ohlcv"
    parquet_files = list(ohlcv_root.rglob("**/1h.parquet"))
    if not parquet_files:
        pytest.skip("No local 1h OHLCV parquet data available")

    selected = next(
        (
            path
            for path in parquet_files
            if _has_coverage(path, start="2024-01-01", end="2025-12-31")
        ),
        None,
    )
    if selected is None:
        pytest.skip("No local 1h OHLCV parquet data covering 2024-01-01..2025-12-31")

    symbol = selected.parent.name
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

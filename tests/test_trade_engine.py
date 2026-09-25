from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.contract import parse_date_bound
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from shared.constants import DATA_DIR


def _to_utc_datetime(value: object) -> datetime | None:
    """Coerce a parquet scalar into a tz-aware datetime (test-local, shared
    by ``_time_bounds`` only; date-bound parsing itself goes through the
    contract)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    return None


@pytest.mark.parametrize("exposure", [0.0, -0.01, 1.01])
def test_backtest_config_rejects_position_exposure_outside_fraction_range(exposure: float):
    with pytest.raises(ValueError):
        BacktestConfig(
            strategy_name="bounds",
            symbols=["TEST"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            max_position_exposure_pct=exposure,
        )


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
    # Conformance (Cleanup 16-A, F9): the coverage check uses the SHARED
    # contract parser instead of a private re-implementation, so the test's
    # window arithmetic can never drift from the engines'.
    return lower <= parse_date_bound(start, is_end=False).to_pydatetime() and upper >= parse_date_bound(
        end, is_end=True
    ).to_pydatetime()


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

    cfg = BacktestConfig(risk_per_trade_pct=0.01,
        strategy_name="unit-test",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        modeling_resolution="main_timeframe",
        parameters={"fees": "zero"},
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
        parameters={"fees": "zero"},
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
        parameters={"fees": "zero"},
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
        parameters={"fees": "zero"},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(120.0)
    assert trade.exits[-1].price == pytest.approx(118.0)
    assert trade.exits[-1].reason == "stop_loss"
    assert trade.pnl == pytest.approx(-1000.0, abs=0.5)


def test_trade_engine_configured_same_bar_take_profit_priority_falls_back_to_stop_first():
    """Cleanup 16-A Decision Record: the stop-first tie-break is contractual.

    A configured ``same_bar_order = "take_profit_first"`` is no longer honored:
    without intrabar data the stop wins the same-bar ambiguity, and the
    configured-but-unsupported request stays visible in the audit metadata as
    ``stop_first``.

    The ambiguity bar must NOT be the fill bar, and neither level may sit
    inside the fill bar's default range: since A-H1 the scoped fill-bar
    recheck exits a just-opened position on the fill bar itself (bar 11
    here has high 112), which would preempt the same-bar ambiguity this
    test pins. TP is 113 so the fill bar cannot trigger it; the ambiguity
    bar (12, high 114 / low 109) then hits both levels and F8 stop-first
    decides.
    """
    data = _synthetic_ohlcv(100)
    data.loc[12, "high"] = 114.0
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
        indicators["take_profit"].iloc[10] = 113.0
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
        parameters={"fees": "zero", "same_bar_order": "take_profit_first"},
    )

    engine = TradeEngine(cfg, symbol="TEST")
    result = engine.run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].price == pytest.approx(109.0)
    assert trade.exits[-1].reason == "stop_loss"
    assert trade.meta["execution_model"]["same_bar_order"] == "stop_first"
    assert trade.pnl < 0


def test_example_sma_native_vs_vectorbt_smoke_on_local_data():
    """Smoke test on whatever local 1h dataset exists (Decision Record 2026-08-31).

    Named explicitly as a SMOKE test: on real/local data the engines use
    different signal-exit fill conventions (ox-alpha F10), so exact parity
    is not promised here. The deterministic-fixture parity test with exact
    trade-count equality lives in tests/test_engine_parity.py.

    Guards that were previously vacuous:
    - an empty store no longer passes the test (both engines returning zero
      trades is a failure, not "assert True");
    - divergence is capped at one trade, not 30%.
    """
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
    cfg = BacktestConfig(risk_per_trade_pct=0.01,
        strategy_name="example_sma",
        symbols=[symbol],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2025-12-31",
        initial_capital=100_000,
        modeling_resolution="main_timeframe",
        parameters={"fees": "zero"},
    )

    native_result = NativeAdapter().run(cfg, str(strategy_path))
    vectorbt_result = VectorbtAdapter().run(cfg, str(strategy_path))

    native_count = len(native_result.trades)
    vectorbt_count = len(vectorbt_result.trades)

    assert (native_count, vectorbt_count) != (0, 0), (
        f"Both engines produced zero trades on {symbol}/1h — the smoke test "
        "must exercise at least one engine, not pass vacuously"
    )
    assert abs(native_count - vectorbt_count) <= 1, (
        f"Trade-count divergence on real data must be at most one trade: "
        f"native={native_count}, vectorbt={vectorbt_count} on {symbol}/1h"
    )


class TestIssueC2ExposureMath:
    """C2: exposure counts stop-defined risk only; pending counts for limits."""

    def _engine(self):
        from runner.engine import TradeEngine

        return TradeEngine(
            BacktestConfig(
                strategy_name="t",
                symbols=["X"],
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-01-02",
                initial_capital=100_000.0,
                risk_per_trade_pct=0.01,
                parameters={"fees": "zero"},
            ),
            symbol="X",
        )

    def test_stopless_open_leg_fails_closed(self) -> None:
        from runner.engine_pkg.orders import Position

        engine = self._engine()
        engine.equity = 100_000.0
        engine.positions.append(
            Position(
                symbol="X",
                direction="LONG",
                entry_index=0,
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=100.0,
                order_type="market",
                quantity=500.0,
                remaining_qty=500.0,
                stop_loss=None,
            )
        )
        with pytest.raises(ValueError, match="stopless open position"):
            engine._current_risk_exposure(100.0)

    def test_stopless_pending_order_fails_closed(self) -> None:
        from runner.engine_pkg.orders import Order, OrderType

        engine = self._engine()
        engine.equity = 100_000.0
        engine.pending_orders.append(
            Order(
                symbol="X",
                direction="LONG",
                order_type=OrderType.MARKET,
                created_index=0,
                created_time="2024-01-01T00:00:00+00:00",
                quantity=500.0,
                stop_loss=None,
            )
        )
        with pytest.raises(ValueError, match="stopless pending order"):
            engine._current_risk_exposure(100.0)

    def test_stopped_legs_contribute_stop_distance(self) -> None:
        from runner.engine_pkg.orders import Position

        engine = self._engine()
        engine.equity = 100_000.0
        engine.positions.append(
            Position(
                symbol="X",
                direction="LONG",
                entry_index=0,
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=100.0,
                order_type="market",
                quantity=500.0,
                remaining_qty=500.0,
                stop_loss=98.0,
            )
        )
        assert engine._current_risk_exposure(100.0) == pytest.approx(0.01)

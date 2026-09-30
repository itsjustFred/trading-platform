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


def test_sizing_only_stop_survives_fill_and_is_used_for_risk_metadata():
    data = _synthetic_ohlcv(6)

    def strategy_fn(df: pd.DataFrame, params: dict):
        signals = pd.DataFrame(index=df.index)
        signals["entries"] = False
        signals["exits"] = False
        signals["sizing_sl_price"] = float("nan")
        signals["stop_distance"] = float("nan")
        signals.loc[0, "entries"] = True
        signals.loc[2, "exits"] = True
        signals.loc[0, "sizing_sl_price"] = 99.5
        signals.loc[0, "stop_distance"] = 0.5
        return signals

    cfg = BacktestConfig(
        strategy_name="sizing-only-stop",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-01",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero"},
    )

    result = TradeEngine(cfg, symbol="TEST").run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.stop_loss is None
    assert trade.meta["sizing_stop_loss"] == pytest.approx(99.5)
    assert trade.quantity == pytest.approx(666.6666667)
    assert trade.r_multiple is not None


def test_sizing_only_stop_equal_to_fill_is_skipped():
    data = _synthetic_ohlcv(4)

    def strategy_fn(df: pd.DataFrame, params: dict):
        signals = pd.DataFrame(index=df.index)
        signals["entries"] = False
        signals["exits"] = False
        signals["sizing_sl_price"] = float("nan")
        signals.loc[0, "entries"] = True
        signals.loc[0, "sizing_sl_price"] = 101.0
        return signals

    cfg = BacktestConfig(
        strategy_name="invalid-sizing-stop",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-01",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero"},
    )

    result = TradeEngine(cfg, symbol="TEST").run(data, strategy_fn)

    assert result.trades == []


def test_trade_engine_mae_includes_signal_exit_bar_excursion():
    records = []
    for index in range(25):
        low = 80.0 if index == 10 else 99.0
        close = 105.0 if index == 10 else 101.0
        records.append(
            {
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=index),
                "open": 100.0,
                "high": 110.0,
                "low": low,
                "close": close,
                "volume": 1000,
            }
        )
    data = pd.DataFrame.from_records(records)

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[1] = True
        exits.iloc[10] = True
        return entries, exits, {}, []

    cfg = BacktestConfig(
        strategy_name="mae-exit-bar",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-02",
        initial_capital=100_000,
        # Merge resolution: the engine now refuses to size without an explicit
        # risk setting, so the fixture states one — the same
        # `risk_per_trade_pct=0.01` every sibling sizing test in this file uses.
        # The MAE/MFE assertions below are computed from the resulting
        # `trade.quantity`, so the risk setting does not weaken them.
        risk_per_trade_pct=0.01,
        # ...and declares a no-cost run, as the engine now also refuses to size
        # without an explicit fee/slippage setting. Every sibling sizing test in
        # this file already uses `fees="zero"`.
        parameters={"fees": "zero"},
    )

    result = TradeEngine(cfg, symbol="TEST").run(data, strategy_fn)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.mae == pytest.approx((80.0 - trade.entry_price) * trade.quantity)
    assert trade.mfe == pytest.approx((110.0 - trade.entry_price) * trade.quantity)


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
    assert trade.risk_amount == pytest.approx(1000.0, abs=0.5)
    assert trade.risk_percent == pytest.approx(0.01, abs=1e-6)
    assert trade.meta["stop_loss"] == pytest.approx(109.0)


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


def test_stop_distance_only_order_gets_fill_anchored_live_stop():
    data = _synthetic_ohlcv(100)
    data.loc[12, "low"] = 109.0

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        indicators = {
            "direction": pd.Series("LONG", index=df.index),
            "stop_distance": pd.Series(pd.NA, index=df.index),
        }
        indicators["stop_distance"].iloc[10] = 2.0
        return entries, exits, indicators, []

    cfg = BacktestConfig(
        strategy_name="stop-distance-only",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero"},
    )

    result = TradeEngine(cfg, symbol="TEST").run(data, strategy_fn)

    assert len(result.trades) == 1
    assert result.trades[0].stop_loss == pytest.approx(109.0)


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
    # Merge resolution: the ambiguity bar is 12 (high 114 / low 109), as the
    # docstring above requires. The other side of the merge raised bar 11 to
    # high 113 as well, which would have put TP 113 inside the fill bar's range
    # and triggered the very fill-bar recheck (A-H1) this test needs to avoid —
    # a fixture that contradicts the test's own stated contract.
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
    # Repo-relative, not CWD-relative.
    #
    # `Path("strategies/...")` resolves against the process working directory, so this
    # test skipped for a *different reason* depending on where pytest was invoked.
    # Measured: from the repo root the skip read "No local 1h OHLCV parquet data
    # available"; run from /tmp against an absolute test path it read "example_sma
    # strategy not found" - a tracked file that is always present.
    #
    # The data skips below are legitimate, this one was not: the strategy is a git-tracked
    # file, so its absence is a defect rather than a condition. And on a machine that
    # *does* have 1h data, invoking pytest from anywhere but the repo root would have
    # skipped this smoke test for good, silently, with exit 0.
    strategy_path = Path(__file__).resolve().parents[1] / "strategies" / "example_sma" / "example_sma.py"
    assert strategy_path.exists(), f"git-tracked strategy is missing: {strategy_path}"

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

    def test_excluded_pending_order_is_not_double_counted(self) -> None:
        from runner.engine_pkg.orders import Order, OrderType

        engine = self._engine()
        engine.equity = 100_000.0
        first = Order(
            symbol="X",
            direction="LONG",
            order_type=OrderType.MARKET,
            created_index=0,
            created_time="2024-01-01T00:00:00+00:00",
            quantity=500.0,
            stop_loss=99.0,
        )
        second = Order(
            symbol="X",
            direction="LONG",
            order_type=OrderType.MARKET,
            created_index=0,
            created_time="2024-01-01T00:00:00+00:00",
            quantity=500.0,
            stop_loss=99.0,
        )
        engine.pending_orders.extend([first, second])

        assert engine._current_risk_exposure(100.0) == pytest.approx(0.01)
        assert engine._current_risk_exposure(100.0, exclude_order=first) == pytest.approx(0.005)

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


def _wide_bar_run(strategy_name: str, *, stop=None, take_profit=None, exit_index: int = 20):
    """One entry, and a single bar whose range sits well outside any exit level.

    Bar 10 is wide (high 130, low 70) around an entry near 100. That separation is
    the whole point: if an exit level coincided with a bar extreme, clamping and not
    clamping would be indistinguishable.

    Stop and take-profit travel on the signals frame - `entries`, `exits`,
    `direction`, `stop_loss` - which is how the engine's own tests supply them. The
    first attempt at this test put them in the strategy's third return value, which
    is *params*, so no stop was ever set and the position signal-exited: a path that
    is deliberately unclamped. The test passed the wrong way and reported a
    full-bar MAE as if it were a bug in the clamp.
    """
    records = []
    for index in range(25):
        records.append(
            {
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=index),
                "open": 100.0,
                "high": 130.0 if index == 10 else 101.0,
                "low": 70.0 if index == 10 else 99.0,
                "close": 100.0,
                "volume": 1000,
            }
        )
    data = pd.DataFrame.from_records(records)

    def strategy_fn(df: pd.DataFrame, params: dict):
        signals = pd.DataFrame(index=df.index)
        signals["entries"] = False
        signals["exits"] = False
        signals["direction"] = "LONG"
        signals["stop_loss"] = pd.NA
        signals["take_profit"] = pd.NA
        signals.loc[1, "entries"] = True
        if stop is not None:
            signals.loc[1, "stop_loss"] = stop
        if take_profit is not None:
            signals.loc[1, "take_profit"] = take_profit
        else:
            signals.loc[exit_index, "exits"] = True
        return signals

    cfg = BacktestConfig(
        strategy_name=strategy_name,
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-02",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        max_leverage=3.0,
        modeling_resolution="main_timeframe",
        parameters={"fees": "zero"},
    )
    return TradeEngine(cfg, symbol="TEST").run(data, strategy_fn)


def test_a_take_profit_exit_is_not_credited_with_the_bars_high():
    """MFE stops at the fill, not at a price the position never held.

    The bar's high is 130 and the position exits at a 120 take-profit. It never
    traded above 120, so an MFE of 130 is fiction - and it is fiction on precisely
    the trades that worked, which biases comparison toward whichever strategy
    happened to exit inside a wide bar.
    """
    result = _wide_bar_run("tp-excursion", take_profit=120.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].price == pytest.approx(120.0)
    assert trade.mfe == pytest.approx(
        (120.0 - trade.entry_price) * trade.quantity
    ), f"MFE reached {trade.mfe}, beyond the 120.0 fill"


def test_a_stop_exit_is_not_credited_with_the_bars_low():
    """The mirror: MAE stops at the stop, not below it.

    A stop-out closes at the stop level, so the loss taken is the stop distance.
    Attributing the bar's low reports a loss larger than the one that occurred, on
    exactly the trades that failed.
    """
    result = _wide_bar_run("stop-excursion", stop=90.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "stop_loss"
    assert trade.exits[-1].price == pytest.approx(90.0)
    assert trade.mae == pytest.approx(
        (90.0 - trade.entry_price) * trade.quantity
    ), f"MAE reached {trade.mae}, beyond the 90.0 stop fill"


def test_a_position_that_stays_open_keeps_full_bar_recording():
    """The clamp is for exits only; a position held through the bar sees all of it."""
    result = _wide_bar_run("open-excursion")

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.mfe == pytest.approx(
        (130.0 - trade.entry_price) * trade.quantity
    ), "a position held through the bar must still record the bar's high"
    assert trade.mae == pytest.approx((70.0 - trade.entry_price) * trade.quantity)


def _excursion_run(take_profit=None, stop=None, direction="LONG", tps=None, hi=115.0, lo=90.0):  # noqa: C901
    """One entry, one wide bar, and the exit kind under test.

    `direction` is a parameter because the short side of the clamp is a different
    code path - it uses the ask-side bar and adds the spread to the fill - and every
    excursion test before this one was LONG-only. A mutation that broke the short
    branch by one spread passed the whole file.
    """
    records = [
        {
            "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i),
            "open": 100.0,
            "high": hi if i == 10 else 101.0,
            "low": lo if i == 10 else 99.0,
            "close": 100.0,
            "volume": 1000,
        }
        for i in range(25)
    ]
    data = pd.DataFrame(records)

    def strategy_fn(df: pd.DataFrame, params: dict):
        signals = pd.DataFrame(index=df.index)
        signals["entries"] = False
        signals["exits"] = False
        signals["direction"] = direction
        signals["stop_loss"] = pd.NA
        signals["take_profit"] = pd.NA
        signals.loc[1, "entries"] = True
        if stop is not None:
            signals.loc[1, "stop_loss"] = stop
        # A ladder arrives as numbered `tpN` columns, which is how the engine
        # collects them; a list in `take_profit` is not a shape it understands.
        for level in range(1, 5):
            signals[f"tp{level}"] = pd.NA
        for name, value in (tps or {}).items():
            signals[name] = float(value)
        if take_profit is not None:
            signals.loc[1, "take_profit"] = take_profit
        else:
            signals.loc[20, "exits"] = True
        return signals

    cfg = BacktestConfig(
        strategy_name="excursion-side",
        symbols=["TEST"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-02",
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        max_leverage=3.0,
        modeling_resolution="main_timeframe",
        parameters={"fees": "zero"},
    )
    return TradeEngine(cfg, symbol="TEST").run(data, strategy_fn).trades[0]


def test_a_take_profit_cap_leaves_the_adverse_side_alone():
    """Capping the favourable side must not flatten the adverse one.

    The first version clamped the bar symmetrically to the fill, which capped *both*
    sides. On this bar (90 to 115, entry near 100) a take-profit at 110 correctly
    reported MFE of +10 per unit and then reported MAE of -1 per unit instead of the
    bar's -10. Every stop-out likewise reported a near-zero MFE, so every trade looked
    like a monotone excursion and the two metrics stopped carrying independent
    information - the bias traded places rather than going away.
    """
    trade = _excursion_run(take_profit=110.0)

    assert trade.mfe == pytest.approx(
        (110.0 - trade.entry_price) * trade.quantity
    ), "the take-profit fill must cap the favourable side"
    assert trade.mae == pytest.approx(
        (90.0 - trade.entry_price) * trade.quantity
    ), (
        f"MAE is {trade.mae / trade.quantity:+.1f} per unit; the bar's low alone is "
        f"{90.0 - trade.entry_price:+.1f}. A take-profit exit says nothing about the "
        f"adverse side, so capping it erases real excursion."
    )


def test_a_stop_cap_leaves_the_favourable_side_alone():
    """The mirror: the stop caps MAE, and the bar's high is still the MFE."""
    trade = _excursion_run(stop=95.0)

    assert trade.mae == pytest.approx(
        (95.0 - trade.entry_price) * trade.quantity
    ), "the stop fill must cap the adverse side"
    assert trade.mfe == pytest.approx(
        (115.0 - trade.entry_price) * trade.quantity
    ), (
        f"MFE is {trade.mfe / trade.quantity:+.1f} per unit; the bar's high alone is "
        f"{115.0 - trade.entry_price:+.1f}. A stop-out does not mean the position "
        f"never went favourably."
    )


def test_a_short_take_profit_caps_only_the_favourable_side():
    """The short branch is separate code and had no coverage at all.

    It reads the ask-side bar via `_ask_bar` and adds the spread to the fill, so a
    mutation there is invisible to every LONG test. A bar whose low is 90 and high
    115 against a short entry near 100: taking profit at 90 caps the favourable
    excursion at that level, and the adverse side still comes from the high.
    """
    trade = _excursion_run(take_profit=90.0, direction="SHORT", hi=115.0, lo=90.0)

    # A short profits as the price falls, so the TP at 90 is the favourable cap.
    assert trade.mfe <= trade.quantity * (trade.entry_price - 90.0) + 1e-6, (
        f"MFE is {trade.mfe / trade.quantity:+.2f} per unit, beyond the take-profit "
        f"fill the position actually exited at"
    )
    assert trade.mae == pytest.approx(
        (trade.entry_price - 115.0) * trade.quantity
    ), (
        f"MAE is {trade.mae / trade.quantity:+.2f} per unit; a short's adverse side "
        f"is the bar's high, which the take-profit exit says nothing about"
    )


def test_a_short_stop_caps_only_the_adverse_side():
    """The short stop path, which adds the spread to the fill."""
    trade = _excursion_run(stop=115.0, direction="SHORT", hi=115.0, lo=90.0)

    assert trade.mae >= trade.quantity * (trade.entry_price - 115.0) - 1e-6, (
        f"MAE is {trade.mae / trade.quantity:+.2f} per unit, worse than the stop the "
        f"position exited at; the stop must cap the adverse side"
    )
    assert trade.mfe == pytest.approx(
        (trade.entry_price - 90.0) * trade.quantity
    ), (
        f"MFE is {trade.mfe / trade.quantity:+.2f} per unit; the bar's low alone is "
        f"{trade.entry_price - 90.0:+.2f}, and a stop-out does not erase it"
    )


def test_a_multi_level_ladder_is_credited_with_the_furthest_level_reached():
    """Capping at the *first* pending level understated MFE five-fold.

    The first version clamped to `pending_tp[0]`, but the close loop fills every
    reached level at its own price. A ladder at 105 and 115 that fills both is
    credited with 115; clamping to 105 reported less than half the real excursion,
    on exactly the trades the cap was added to describe correctly.
    """
    trade = _excursion_run(
        tps={"tp1": 105.0, "tp2": 115.0}, hi=130.0, lo=99.5
    )

    reached = {e.reason for e in trade.exits}
    assert "tp1" in reached and "tp2" in reached, (
        f"the fixture must fill both levels, got {reached}"
    )
    assert trade.mfe == pytest.approx(
        (115.0 - trade.entry_price) * trade.quantity
    ), (
        f"MFE is {trade.mfe / trade.quantity:+.1f} per unit; both levels filled, so "
        f"the furthest one reached (115) is the cap, not the first pending (105)"
    )


def test_no_test_resolves_a_repo_path_against_the_working_directory() -> None:
    """Repo paths in tests must be derived from `__file__`, never from the CWD.

    AGENTS.md requires scripts and paths to be portable across Linux, Windows and macOS, and
    a CWD-relative path is the opposite: it resolves against wherever pytest happened to be
    invoked from.

    This is not hypothetical. `Path("strategies/example_sma/example_sma.py")` in the smoke
    test above meant that test skipped for a **different reason** depending on the working
    directory - measured, "No local 1h OHLCV parquet data available" from the repo root
    against "example_sma strategy not found" from `/tmp`, for a git-tracked file that is
    always present. On a machine that *did* have the data, invoking pytest from anywhere but
    the root would have skipped it for good, silently, with exit 0.

    A static pin, because the defect is a *spelling*: `Path("strategies/...")` looks
    identical to the correct form until the CWD moves. And a pin on the pattern rather than
    on that one line, so the next instance is caught here.

    `tests/test_destructive_scripts_safety.py:355` is excluded, and the exclusion is narrow
    and stated rather than a file allowlist: a `Path(...)` built inside a
    `monkeypatch.setattr(...)` call is a **value handed to a replaced dependency**, not a
    location. There the path is never touched - `_run_strategy` is itself patched to raise
    - so requiring it to be repo-relative would be requiring the wrong thing. Everything
    outside such a call is scanned, including string literals inside them.
    """
    import ast
    from pathlib import Path as _Path

    repo_dirs = ("strategies", "data", "runner", "viewer", "scripts", "shared",
                 "signal_bots", "live_runner", "risk", "data_tools", "validation")
    offenders: list[str] = []

    for path in sorted(_Path(__file__).resolve().parent.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - would fail collection anyway
            continue
        # `Path(...)` anywhere inside a `monkeypatch.setattr(...)` call is a stand-in
        # value for a replaced dependency, not a filesystem location.
        patched: set[int] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setattr"):
                continue
            for inner in ast.walk(node):
                patched.add(id(inner))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "Path"):
                continue
            if id(node) in patched:
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                head = arg.value.replace("\\", "/").split("/")[0]
                if head in repo_dirs and not arg.value.startswith("/"):
                    offenders.append(f"{path.name}:{node.lineno} Path({arg.value!r})")

    assert not offenders, (
        "these paths resolve against the working directory, so the tests skip or fail "
        "depending on where pytest was invoked. Use Path(__file__).resolve().parents[N]:\n  "
        + "\n  ".join(offenders)
    )

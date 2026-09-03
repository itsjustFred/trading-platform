"""Behavior regression tests for the native TradeEngine money path.

Covers Cleanup 16-D1 findings on ``runner/engine.py``:

- F20 — signal lookup must merge the OHLCV row with the signal row
  *element-wise* (aligned by label), never by vertical concatenation.
- F21 — sizing equity must exclude unrealized mark-to-market PnL of open
  positions (realized cash only), so positions cannot be sized against
  paper profits.
- F22 — engine-level risk policy (``max_positions``) must come from the
  risk configuration, not silently from strategy ``parameters``.
- F23 — order-type sizing consistency: MARKET orders size once at fill;
  LIMIT/STOP orders size at creation (documented policy: size exactly once,
  at the price known at fill time).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **overrides) -> BacktestConfig:
    payload = {
        "strategy_name": "probe",
        "symbols": ["PROBE"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


def _linear_frame(n: int = 5, start: float = 100.0) -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    closes = [start + i for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


# ─── F20: element-wise signal merge ──────────────────────────────────────────


class TestSignalMergeElementWise:
    """Signal rows must overlay the OHLCV row by label, not stack vertically."""

    def test_signal_named_close_does_not_shadow_ohlcv_close(self):
        """An indicator called 'close' must not corrupt the fill-price lookup.

        With a vertical concat the merged row carries 'close' twice and the
        OHLCV value becomes ambiguous; entry sizing then reads the wrong
        price. The element-wise overlay keeps ``row["close"]`` authoritative.
        """
        frame = _linear_frame()
        strategy = lambda df, params: (  # noqa: E731
            pd.Series([True] + [False] * (len(df) - 1), index=df.index),
            pd.Series([False] * len(df), index=df.index),
            {"close": pd.Series([9.99] * len(df), index=df.index)},
        )

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

        assert engine.closed_trades, "strategy must open one position"
        trade = engine.closed_trades[0]
        assert trade.entry_price == pytest.approx(frame.iloc[1]["open"]), (
            "entry fill must use the next bar's OHLCV open, not the indicator"
        )

    def test_merged_row_length_matches_ohlcv_row(self):
        """The merge must never widen the row (vertical concat artifact)."""
        frame = _linear_frame()
        captured: dict[str, pd.Series] = {}

        original_extract = TradeEngine._extract_levels

        def spy(self, row: pd.Series):
            captured["row"] = row
            return original_extract(self, row)

        strategy = lambda df, params: (  # noqa: E731
            pd.Series([True] + [False] * (len(df) - 1), index=df.index),
            pd.Series([False] * len(df), index=df.index),
            {"close": pd.Series([1.0] * len(df), index=df.index),
             "low": pd.Series([2.0] * len(df), index=df.index)},
        )

        TradeEngine._extract_levels = spy  # type: ignore[method-assign]
        try:
            engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
            engine.run(frame, strategy)
        finally:
            TradeEngine._extract_levels = original_extract  # type: ignore[method-assign]

        row = captured["row"]
        ohlcv_labels = [str(label) for label in frame.iloc[0].index if label != "timestamp"]
        for label in ohlcv_labels:
            assert isinstance(row[label], float), (
                f"label {label!r} must resolve to a single scalar value "
                "(element-wise merge), got a non-scalar"
            )


# ─── F21: sizing equity excludes unrealized PnL ──────────────────────────────


class TestSizingEquityExcludesUnrealizedPnl:
    """Positions must be sized from realized cash, not mark-to-market equity."""

    @staticmethod
    def _run_two_entry_scenario(pump_price: float):
        """Shared two-entry scenario: entries on bars 0 and 2, bar 2 pumped
        (or dumped) to ``pump_price`` so the open position carries a large
        unrealized PnL when entry 2 is sized."""
        frame = _linear_frame(6, start=100.0)
        frame.loc[2, "high"] = max(pump_price, 100.5) if pump_price > 100 else frame.loc[2, "high"]
        frame.loc[2, "low"] = min(pump_price, 99.5) if pump_price < 100 else frame.loc[2, "low"]
        frame.loc[2, "close"] = pump_price
        frame.loc[3, "close"] = pump_price
        frame.loc[3, "open"] = pump_price

        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)
        assert len(engine.closed_trades) >= 2
        return engine.closed_trades[1].quantity

    def test_second_entry_sized_on_realized_not_mark_to_market(self):
        """While position 1 rides an open profit, position 2 must not grow.

        Deterministic setup: enter long at 100 (bar 1). Price rises to 130
        (unrealized +30/qty). A second entry on bar 3 sizes from the same
        capital base as the first — the paper profit is not spendable.
        """
        # Sizing capital is realized cash (100k, unchanged — no trade closed),
        # so the second entry (fill 130) must size 100000/130 ≈ 769.23.
        # Mark-to-market sizing would use equity 100k + 30*1000 = 160k → 1230.77.
        assert self._run_two_entry_scenario(130.0) == pytest.approx(
            100_000.0 / 130.0, rel=1e-9
        ), (
            "the second entry must size from realized cash (100k), not from "
            "mark-to-market equity (160k would be MTM)"
        )

    def test_unrealized_loss_does_not_shrink_sizing(self):
        """The mirror case: open losses must not shrink new position sizes."""
        # Sizing capital is realized cash (100k), so the second entry (fill 70)
        # sizes 100000/70 ≈ 1428.57. Mark-to-market sizing would use equity
        # 100k − 30*1000 = 40k → 571.43 (paper losses shrinking new sizes).
        assert self._run_two_entry_scenario(70.0) == pytest.approx(
            100_000.0 / 70.0, rel=1e-9
        ), (
            "an unrealized drawdown must not reduce the cash available for sizing"
        )


# ─── F22: max_positions comes from risk policy, not strategy params ─────────


class TestMaxPositionsPolicy:
    def test_strategy_param_max_positions_is_ignored(self):
        """Strategy `parameters` must not silently set engine risk policy."""
        config = _config(Path("/tmp"), parameters={"max_positions": 2})
        engine = TradeEngine(config, symbol="PROBE")
        assert engine._risk_engine.config.max_positions != 2

    def test_default_policy_applies_when_param_leaks_in(self):
        """The policy constant must be the single source of truth, not params."""
        config = _config(Path("/tmp"), parameters={"max_positions": 3})
        engine = TradeEngine(config, symbol="PROBE")
        assert engine._risk_engine.config.max_positions == 5, (
            "engine risk policy must come from the engine constant, not from "
            "strategy parameters (a strategy could otherwise silently loosen "
            "or tighten concurrency limits)"
        )


# ─── F23: consistent sizing semantics across order types ────────────────────


class TestOrderTypeSizingSemantics:
    """Documented policy (16-D1): every order is sized exactly once, at fill.

    MARKET fills at next-bar open → sized from the fill price.
    LIMIT/STOP fill at their trigger price → sized from that trigger price.
    The pre-fill creation sizing must not survive into the position.
    """

    def _limit_order_engine(self, limit_price: float) -> tuple[TradeEngine, pd.DataFrame]:
        frame = _linear_frame(6)
        # Bar 2 dips through the limit so a LONG limit at 100.5 fills on bar 2.
        frame.loc[2, "low"] = limit_price - 1.0
        frame.loc[2, "open"] = limit_price + 0.5

        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")

        original_fill = engine._fill_pending_orders

        def create_limit(row: pd.Series, i: int, direction: str) -> None:
            from runner.engine import Order, OrderType

            qty = engine._calculate_position_size(
                entry_price=limit_price,
                stop_loss_price=None,
                direction=direction,
            )
            engine.pending_orders.append(
                Order(
                    symbol="PROBE",
                    direction=direction,
                    order_type=OrderType.LIMIT,
                    created_index=i,
                    created_time=engine._to_iso(row["timestamp"]),
                    quantity=qty,
                    limit_price=limit_price,
                )
            )

        def patched_fill(row: pd.Series, i: int) -> None:
            if not engine.pending_orders:
                if bool(entries.iloc[i]):
                    create_limit(row, i, "LONG")
                return
            original_fill(row, i)

        engine._fill_pending_orders = patched_fill  # type: ignore[method-assign]
        engine.run(frame, strategy)
        return engine, frame

    def test_limit_order_is_resized_to_trigger_price_at_fill(self):
        engine, frame = self._limit_order_engine(limit_price=100.5)
        assert engine.closed_trades, "limit order must fill"
        trade = engine.closed_trades[0]
        expected_qty = 100_000.0 / 100.5
        assert trade.quantity == pytest.approx(expected_qty, rel=1e-6), (
            "LIMIT orders must be sized at fill from the trigger price, "
            "not keep the creation-price estimate"
        )

    def test_stop_order_is_resized_to_trigger_price_at_fill(self):
        """STOP orders (short side, trigger above) size at the trigger price."""
        frame = _linear_frame(6)
        # Bar 2 rallies through the stop of a SHORT stop order at 101.5.
        frame.loc[2, "high"] = 102.5
        frame.loc[2, "open"] = 101.0

        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)  # noqa: E731

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        original_fill = engine._fill_pending_orders
        from runner.engine import Order, OrderType

        stop_price = 101.5

        def create_stop(row: pd.Series, i: int) -> None:
            qty = engine._calculate_position_size(
                entry_price=100.0,  # deliberately wrong (creation estimate)
                stop_loss_price=None,
                direction="SHORT",
            )
            engine.pending_orders.append(
                Order(
                    symbol="PROBE",
                    direction="SHORT",
                    order_type=OrderType.STOP,
                    created_index=i,
                    created_time=engine._to_iso(row["timestamp"]),
                    quantity=qty,
                    stop_price=stop_price,
                )
            )

        def patched_fill(row: pd.Series, i: int) -> None:
            if not engine.pending_orders:
                if bool(entries.iloc[i]):
                    create_stop(row, i)
                return
            original_fill(row, i)

        engine._fill_pending_orders = patched_fill  # type: ignore[method-assign]
        engine.run(frame, strategy)

        assert engine.closed_trades, "stop order must fill"
        trade = engine.closed_trades[0]
        assert trade.quantity == pytest.approx(100_000.0 / stop_price, rel=1e-6), (
            "STOP orders must be sized at fill from the trigger price"
        )

    def test_market_order_without_stop_keeps_documented_fallback_sizing(self):
        """Stopless MARKET entries are the documented exception: no re-size.

        Without a stop there is no fill-time risk input, so the creation-time
        (signal-bar close) estimate stands — pinned here so a future change
        to that policy has to update this test deliberately.
        """
        frame = _linear_frame(4)
        entries = pd.Series([True] + [False] * 3, index=frame.index)
        exits = pd.Series([False] * 4, index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)  # noqa: E731

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

        assert engine.closed_trades
        trade = engine.closed_trades[0]
        # Fill happens on bar 1 (open 101); stopless sizing was computed on
        # bar 0 close (100) → 100000/100 = 1000 shares.
        assert trade.quantity == pytest.approx(100_000.0 / 100.0, rel=1e-6)


# ─── Mutation hardening: reporting equity curve ─────────────────────────────


class TestReportingEquityCurve:
    def test_curve_includes_unrealized_mark_to_market(self):
        """Reporting equity must move with open-position MTM (F21 report side).

        Kills the mutants that drop unrealized PnL from the reporting equity:
        a MID-curve bar (position still open) must sit at
        capital + realized + unrealized — realized-only would stay flat at
        capital until the trade closes.
        """
        frame = _linear_frame(6)
        entries = pd.Series([True] + [False] * 5, index=frame.index)
        exits = pd.Series([False] * 6, index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)  # noqa: E731

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

        # Position filled on bar 1 at open=101; qty = 100000/101.
        qty = engine.closed_trades[0].quantity
        curve = engine.equity_curve
        # Bar index 2 (close=102): unrealized = (102 - 101) * qty.
        expected_bar2 = 100_000.0 + (102.0 - 101.0) * qty
        assert curve[2].equity == pytest.approx(round(expected_bar2, 2), rel=1e-6), (
            "mid-curve reporting equity must include unrealized MTM PnL "
            "(realized-only would still be flat at 100000 here)"
        )

    def test_curve_starts_exactly_at_initial_capital(self):
        """Before any fill the curve must sit exactly at initial capital."""
        frame = _linear_frame(3)
        entries = pd.Series([True] + [False] * 2, index=frame.index)
        exits = pd.Series([False] * 3, index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)  # noqa: E731

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

        assert engine.equity_curve[0].equity == pytest.approx(100_000.0), (
            "bar-0 equity must be exactly the initial capital (no seeding drift)"
        )


class TestSignalMergeOhlcvAuthority:
    """CodeRabbit round: OHLCV values are authoritative over signal values."""

    def test_signal_value_never_overwrites_non_na_ohlcv_value(self):
        """A signal column matching an OHLCV label must not change its value.

        combine_first takes the left (OHLCV) value whenever it is non-NaN, so
        the only path for a signal value into an OHLCV label is a NaN bar
        value — and `_prepare`/`dropna` guarantees those never reach the loop.
        Pinned here so a future merge change cannot silently invert authority.
        """
        frame = _linear_frame(4)
        strategy = lambda df, params: (  # noqa: E731  # noqa: E731
            pd.Series([True] + [False] * 3, index=df.index),
            pd.Series([False] * 4, index=df.index),
            {"close": pd.Series([9.99] * 4, index=df.index),
             "high": pd.Series([-1.0] * 4, index=df.index)},
        )

        captured: dict[str, pd.Series] = {}
        original = TradeEngine._extract_levels

        def spy(self, row):
            captured["row"] = row
            return original(self, row)

        TradeEngine._extract_levels = spy  # type: ignore[method-assign]
        try:
            engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
            engine.run(frame, strategy)
        finally:
            TradeEngine._extract_levels = original  # type: ignore[method-assign]

        row = captured["row"]
        assert row["close"] == pytest.approx(frame.iloc[0]["close"]), (
            "the signal 'close' must not overwrite the OHLCV close"
        )
        assert row["high"] == pytest.approx(frame.iloc[0]["high"]), (
            "the signal 'high' must not overwrite the OHLCV high"
        )
        # The engine's direction is resolved from the merged row via
        # _resolve_direction, which defaults to LONG for missing labels —
        # signal-only columns still surface, but never at the cost of an
        # OHLCV value.
        assert row.get("direction", "LONG") in ("LONG", "SHORT")

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

import logging
from pathlib import Path

import pandas as pd
import pytest

from runner.engine import Position, TradeEngine
from runner.engines.base import BacktestConfig
from tests.helpers import with_zero_fees

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **overrides) -> BacktestConfig:
    payload = {
        "strategy_name": "probe",
        "symbols": ["PROBE"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


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

    def test_signal_named_close_does_not_shadow_ohlcv_close(self, monkeypatch):
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

    def test_merged_row_length_matches_ohlcv_row(self, monkeypatch):
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

        monkeypatch.setattr(TradeEngine, "_extract_levels", spy)
        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

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

        # max_leverage=100 keeps the #347 free-margin clamp non-binding so the
        # assertion isolates the sizing BASIS (realized vs mark-to-market),
        # which is what this scenario pins; the clamp itself is covered by
        # TestFreeMarginLeverageCap.
        engine = TradeEngine(
            _config(Path("/tmp"), max_leverage=100.0), symbol="PROBE"
        )
        engine.run(frame, strategy)
        assert len(engine.closed_trades) >= 2
        return engine.closed_trades[1].quantity

    def test_second_entry_sized_on_realized_not_mark_to_market(self):
        """While position 1 rides an open profit, position 2 must not grow.

        Deterministic setup: enter long at 100 (bar 1). Price rises to 130
        (unrealized +30/qty). A second entry on bar 3 sizes from the same
        capital base as the first — the paper profit is not spendable.
        """
        # C1: explicit risk sizing (1%, 2% fallback stop). Sizing capital is
        # realized cash (100k, unchanged — no trade closed), so the second
        # entry (fill 130, stop distance 2.6) sizes 1000/2.6 ≈ 384.62.
        # Mark-to-market sizing would use equity 100k + 30*500 = 115k → 442.31.
        assert self._run_two_entry_scenario(130.0) == pytest.approx(
            1000.0 / 2.6, rel=1e-9
        ), (
            "the second entry must size from realized cash (100k), not from "
            "mark-to-market equity (115k would be MTM)"
        )

    def test_unrealized_loss_does_not_shrink_sizing(self):
        """The mirror case: open losses must not shrink new position sizes."""
        # C1: explicit risk sizing. Sizing capital is realized cash (100k),
        # so the second entry (fill 70, stop distance 1.4) sizes 1000/1.4.
        # Mark-to-market sizing would use equity 100k − 30*500 = 85k → 607.14
        # (paper losses shrinking new sizes).
        assert self._run_two_entry_scenario(70.0) == pytest.approx(
            1000.0 / 1.4, rel=1e-9
        ), (
            "an unrealized drawdown must not reduce the cash available for sizing"
        )

    # ─── #262: sizing WHILE a position is open ──────────────────────────────
    # The two tests above only stay green on either basis: their first entry
    # fills at the next bar's open and the paper PnL of the open position
    # first appears in equity on the very bar the second entry is sized —
    # which is *after* that bar's sizing call. The scenarios below put a real
    # unrealized mark on the open position BEFORE the second entry is sized,
    # so only the realized-cash basis can pass them.

    @staticmethod
    def _run_open_position_scenario(
        mark: float, tmp_path: Path, parameters: dict | None = None
    ) -> list:
        """Two entries where the first is still OPEN and marked at ``mark``.

        Bar 0 signals entry 1 (fill = bar 1 open, 101); bar 1 closes at
        ``mark`` with entry 1 riding, so the bar-2 sizing call sees an open
        position carrying real paper PnL. No bar trades through the 2%
        fallback stop, so nothing is ever realized and the second entry is
        sized while entry 1 is still open.
        """
        frame = _linear_frame(6, start=100.0)
        # Bar 1 is entry 1's fill bar: keep its open (101) as the fill price
        # but mark the close at ``mark`` so the open position carries paper
        # PnL when the next bar is sized.
        frame.loc[1, "high"] = max(mark, 101.0) + 0.5
        frame.loc[1, "low"] = min(mark, 101.0) - 0.5
        frame.loc[1, "close"] = mark
        # Bar 2 signals entry 2 (sized from its close) and bar 3 fills it at
        # its open — the same price, so creation and fill sizing agree.
        for bar in (2, 3):
            frame.loc[bar, "open"] = mark
            frame.loc[bar, "high"] = mark + 0.5
            frame.loc[bar, "low"] = mark - 0.5
            frame.loc[bar, "close"] = mark

        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)

        # max_leverage=100 keeps the #347 free-margin clamp non-binding so the
        # assertions isolate the sizing BASIS (realized vs mark-to-market);
        # the clamp itself is covered by TestFreeMarginLeverageCap.
        engine = TradeEngine(
            _config(
                tmp_path, parameters=parameters or {}, max_leverage=100.0
            ),
            symbol="PROBE",
        )
        engine.run(frame, strategy)
        assert len(engine.closed_trades) >= 2, "both entries must have filled"
        return engine.closed_trades

    def test_open_paper_profit_does_not_grow_next_entry(self, tmp_path: Path):
        """An OPEN position's paper profit must not compound into the next size.

        Entry 1 fills at 101 for 500 shares and rides to a 105 close, so
        mark-to-market equity is 102k when entry 2 is sized at 105 on bar 2.
        Realized cash is still 100k (nothing has closed), so the second entry
        risks 1% of 100k over the 2% fallback stop (2.1) = 476.19 shares.
        Mark-to-market sizing would risk 1% of 102k = 485.71 shares.
        """
        trades = self._run_open_position_scenario(105.0, tmp_path)
        assert trades[1].quantity == pytest.approx(1000.0 / 2.1, rel=1e-9), (
            "an open position's unrealized profit is not spendable: the second "
            "entry must size from realized cash (100k), not mark-to-market (102k)"
        )

    def test_open_paper_loss_does_not_shrink_next_entry(self, tmp_path: Path):
        """An OPEN position's paper loss must not deflate the next size.

        Entry 1 rides down to a 99.5 close (paper −750 on 500 shares) while
        entry 2 is sized at 99.5 on bar 2. Realized cash stays 100k, so the
        second entry risks 1% of 100k over the 1.99 fallback stop = 502.51
        shares; mark-to-market sizing would risk 1% of 99.25k = 498.74.
        """
        trades = self._run_open_position_scenario(99.5, tmp_path)
        assert trades[1].quantity == pytest.approx(1000.0 / 1.99, rel=1e-9), (
            "an open position's unrealized loss is not a realized loss: the "
            "second entry must size from realized cash (100k), not 99.25k"
        )

    def test_recorded_equity_at_entry_is_the_sizing_basis(self, tmp_path: Path):
        """``equity_at_entry`` (the shared-replay coefficient denominator)
        must record the realized basis the order was SIZED from, not the
        mark-to-market reporting equity — otherwise the multi-symbol replay
        rescales a realized-cash quantity by a mark-to-market denominator."""
        trades = self._run_open_position_scenario(105.0, tmp_path)
        for trade in trades:
            assert trade.meta["equity_at_entry"] == pytest.approx(
                100_000.0, rel=1e-9
            ), (
                "equity_at_entry must be the realized sizing basis (100k); the "
                "mark-to-market figure (102k with entry 1 riding) is reporting "
                "equity, not the basis the quantity was computed from"
            )

    def test_recorded_basis_follows_the_surviving_quantity(self, tmp_path: Path):
        """``equity_at_entry`` must be the basis the surviving quantity was
        sized from, not whatever the ledger reads at creation time.

        Entry 2 is a fallback-sized MARKET order with no stop; like every
        other order it is re-sized at its fill on bar 3 (89), by which point
        entry 1 has stopped out on that same bar and the realized basis reads
        98.9k (the 90 stop gaps through the 89 open and fills there). Recording
        the creation-time 100k would hand the shared-equity replay a
        coefficient ``quantity / equity_at_entry`` that no longer matches the
        quantity it is re-scaling.
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 100.0, 89.0, 89.0, 89.0],
                "high": [100.5, 100.5, 100.5, 89.5, 89.5, 89.5],
                "low": [99.5, 99.5, 99.5, 88.0, 88.5, 88.5],
                "close": [100.0, 100.0, 100.0, 89.0, 89.0, 89.0],
                "volume": [1000.0] * 6,
            }
        )
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        # Entry 1 carries a real 10-point stop (hit on bar 3); entry 2 has no
        # stop at all, so it is sized on the 2% fallback and keeps its
        # creation-time quantity at fill.
        blanks: list[float | None] = [None] * len(frame)
        stop_loss = pd.Series([90.0, *blanks[1:]], index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {"stop_loss": stop_loss})

        engine = TradeEngine(_config(tmp_path), symbol="PROBE")
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 2, "both entries must have filled"
        first = engine.closed_trades[0]
        assert first.quantity == pytest.approx(100.0, rel=1e-9), (
            "entry 1 risks 1% of 100k over its 10-point stop = 100 shares"
        )
        assert first.exits[0].reason == "stop_loss", "entry 1 stops out on bar 3"
        second = engine.closed_trades[1]
        assert second.quantity == pytest.approx(989.0 / 1.78, rel=1e-9), (
            "entry 2 is re-sized at its fill like every other order: 1% of "
            "the 98.9k fill-time basis over the 2% fallback stop of the 89 "
            "fill (1.78) = 555.62 shares"
        )
        assert second.meta["equity_at_entry"] == pytest.approx(98_900.0, rel=1e-9), (
            "the basis must be the one the surviving quantity was sized from "
            "(98.9k at the fill, after entry 1's stop gapped through 90 to "
            "the 89 open); the creation-time 100k would corrupt the replay's "
            "per-unit coefficient"
        )

    def test_resized_order_records_its_fill_time_basis(self, tmp_path: Path):
        """The mirror rule: an order that IS re-sized at fill records the
        fill-time basis, because that is the ledger its surviving quantity was
        computed from.

        Entry 2 carries an explicit ``stop_distance`` (5), so its MARKET fill
        re-sizes it from the fill bar (89) — by which point entry 1 has stopped
        out on that same bar and the realized basis reads 98.9k (the 90 stop
        gaps through the 89 open and fills there, −1,100 not −1,000). Its
        quantity is therefore 1% of 98.9k over the 5-point stop = 197.8 shares,
        and the basis must be 98.9k (the creation-time 100k would no longer
        match).
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=7, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 100.0, 89.0, 89.0, 89.0, 89.0],
                "high": [100.5, 100.5, 100.5, 89.5, 89.5, 89.5, 89.5],
                "low": [99.5, 99.5, 99.5, 88.0, 88.5, 88.5, 88.5],
                "close": [100.0, 100.0, 100.0, 89.0, 89.0, 89.0, 89.0],
                "volume": [1000.0] * 7,
            }
        )
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        blanks: list[float | None] = [None] * len(frame)
        stop_loss = pd.Series([90.0, *blanks[1:]], index=frame.index)
        stop_distance = pd.Series([*blanks[:2], 5.0, *blanks[3:]], index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {"stop_loss": stop_loss, "stop_distance": stop_distance})

        engine = TradeEngine(_config(tmp_path), symbol="PROBE")
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 2, "both entries must have filled"
        second = engine.closed_trades[1]
        assert second.quantity == pytest.approx(197.8, rel=1e-9), (
            "the fill-time re-size uses the realized basis in force on the "
            "fill bar: 1% of 98.9k over the 5-point stop = 197.8 shares"
        )
        assert second.meta["equity_at_entry"] == pytest.approx(98_900.0, rel=1e-9), (
            "a re-sized order records the basis its quantity was computed "
            "from (98.9k at fill), not the creation-time figure (100k)"
        )

    def test_resized_limit_order_records_its_fill_time_basis(self, tmp_path: Path):
        """The mirror rule for a LIMIT order, which is re-sized at fill too.

        Entry 1 is a MARKET order with a 5-point stop (95) that fills at 100
        for 200 shares; a LONG LIMIT at 98 (stop 96.5) is created on bar 1
        while the realized cash is still 100k, and fills on bar 2 — the bar
        entry 1 stops out on, so the ledger reads 99k by then. The LIMIT is
        re-sized from its trigger price against that fill-time basis: 1% of
        99,000 over the 1.5-point stop = 660 shares, so the recorded basis
        must be 99,000. Leaving the creation-time figure (100k) on the order
        hands the shared-equity replay a coefficient ``quantity /
        equity_at_entry`` that no longer matches the quantity it re-scales.
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 97.0, 96.0, 96.0, 96.0],
                "high": [100.5, 100.5, 97.5, 96.5, 96.5, 96.5],
                "low": [99.5, 99.5, 94.5, 96.0, 96.0, 96.0],
                "close": [100.0, 100.0, 96.0, 96.0, 96.0, 96.0],
                "volume": [1000.0] * 6,
            }
        )
        entries = pd.Series([True] + [False] * 5, index=frame.index)
        exits = pd.Series([False] * len(frame), index=frame.index)
        blanks: list[float | None] = [None] * len(frame)
        stop_loss = pd.Series([95.0, *blanks[1:]], index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {"stop_loss": stop_loss})

        engine = TradeEngine(_config(tmp_path), symbol="PROBE")
        original_fill = engine._fill_pending_orders
        # The engine's own order factory emits MARKET orders only, so the
        # LIMIT leg is created here (sized from its trigger price, the
        # pending-order estimate that the fill-time re-size replaces).

        def create_limit(row: pd.Series, i: int) -> None:
            from runner.engine import Order, OrderType

            engine.pending_orders.append(
                Order(
                    symbol="PROBE",
                    direction="LONG",
                    order_type=OrderType.LIMIT,
                    created_index=i,
                    created_time=engine._to_iso(row["timestamp"]),
                    quantity=engine._calculate_position_size(
                        entry_price=98.0,
                        stop_loss_price=96.5,
                        direction="LONG",
                    ),
                    limit_price=98.0,
                    stop_loss=96.5,
                )
            )

        def patched_fill(row: pd.Series, i: int) -> None:
            if i == 1:
                create_limit(row, i)
            original_fill(row, i)

        engine._fill_pending_orders = patched_fill  # type: ignore[method-assign]
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 2, "entry 1 and the limit leg"
        first = engine.closed_trades[0]
        assert first.quantity == pytest.approx(200.0, rel=1e-9), (
            "entry 1 risks 1% of 100k over its 5-point stop = 200 shares"
        )
        assert first.exits[0].reason == "stop_loss", "entry 1 stops out on bar 2"
        limit = engine.closed_trades[1]
        assert limit.entry_price == pytest.approx(98.0, rel=1e-9), "limit fills at its trigger"
        assert limit.quantity == pytest.approx(660.0, rel=1e-9), (
            "the re-size uses the fill-time ledger: 1% of 99,000 over the "
            "1.5-point stop = 660 shares, where the creation-time 100k would "
            "have given 666.67"
        )
        assert limit.meta["equity_at_entry"] == pytest.approx(99_000.0, rel=1e-9), (
            "a re-sized LIMIT order records the fill-time basis (99k after "
            "entry 1's stop), not the creation-time figure (100k)"
        )

    def test_aggregate_exposure_denominator_is_realized_cash(self, tmp_path: Path):
        """The aggregate-exposure fraction must use the realized denominator.

        Two open positions each risk 2% of 100k over a 5-point stop, so 0.04
        of the 0.05 aggregate budget is already committed — while both ride an
        8,000 paper profit (mark-to-market equity 108k). Entry 3 is sized on
        the bar after the pump: with the realized denominator the remainder is
        0.05 − 0.04 = 0.01 → 1,000 risk over the 5-point stop = 200 shares.
        With a mark-to-market denominator the committed fraction reads
        4,000/108,000 = 0.037, leaving 0.013 → 1,400 risk = 280 shares.
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=9, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0],
                "high": [100.5, 100.5, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0],
                "low": [99.5, 99.5, 99.5, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0],
                "close": [100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0],
                "volume": [1000.0] * 9,
            }
        )
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[1] = True
        entries.iloc[3] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        stop_distance = pd.Series([5.0] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {"stop_distance": stop_distance})

        # max_leverage=100 keeps the #347 free-margin clamp non-binding so the
        # assertion isolates the realized-cash exposure DENOMINATOR; the
        # clamp itself is covered by TestFreeMarginLeverageCap.
        config = _config(tmp_path, risk_per_trade_pct=0.02, max_leverage=100.0)
        engine = TradeEngine(config, symbol="PROBE")
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 3, "all three entries must have filled"
        for trade in engine.closed_trades[:2]:
            assert trade.quantity == pytest.approx(400.0, rel=1e-9), (
                "each of the first two entries risks 2% of 100k over the "
                "5-point stop = 400 shares"
            )
        assert engine.closed_trades[2].quantity == pytest.approx(200.0, rel=1e-9), (
            "the third entry must see the aggregate exposure already committed "
            "against realized cash (0.04), leaving 0.01 risk = 200 shares; the "
            "mark-to-market figure would leave 0.013 = 280 shares"
        )

    def test_partial_exit_fees_are_not_spendable_while_owed(self, tmp_path: Path):
        """A partial exit's gross PnL is realized, but the fee model has only
        been paid for the legs it has already taken — so the next entry may
        not size on cash that is still owed to it.

        Entry 1 (500 shares @ 100, fee_rate 0.001) takes half off at 105: bar
        2 OPENS at 106, beyond the level, so the rung gaps through and fills
        at the open — 250 x 6 = 1,500 gross. By the time entry 2 is sized on
        the next bar the fee model has incurred, and nothing else:

        - the entry leg on the ORIGINAL 500 shares, 0.001 x 100 x 500 = 50,
        - the exit leg of the 250 shares that closed, 0.001 x 106 x 250
          = 26.5.

        The exit leg of the 250 still-open shares is a future cost the
        position has not incurred, so it is NOT held back. Basis = 100,000 +
        1,500 − 76.5 = 101,423.5, and entry 2 risks 1% of that over the 2%
        fallback stop (2.1).
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 106.0, 105.0, 105.0, 105.0],
                "high": [100.5, 100.5, 106.0, 105.5, 105.5, 105.5],
                "low": [99.5, 99.5, 99.0, 104.5, 104.5, 104.5],
                "close": [100.0, 100.0, 106.0, 105.0, 105.0, 105.0],
                "volume": [1000.0] * 6,
            }
        )
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[3] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        # A 50% take-profit at 105 (plus a second level that is never
        # reached, so the ladder leaves a remainder open) on entry 1 only.
        blanks: list[float | None] = [None] * len(frame)
        tp1 = pd.Series([105.0, *blanks[1:]], index=frame.index)
        tp1_pct = pd.Series([0.5, *blanks[1:]], index=frame.index)
        tp2 = pd.Series([200.0, *blanks[1:]], index=frame.index)
        tp2_pct = pd.Series([0.5, *blanks[1:]], index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries,
        exits,
        {"tp1": tp1, "tp1_pct": tp1_pct, "tp2": tp2, "tp2_pct": tp2_pct},
        )

        # ``fee_rate`` takes precedence over the ``fees="zero"`` default that
        # ``with_zero_fees`` merges in (resolve_fee_model's documented order),
        # so this run is priced and the owed-fee branch is exercised.
        config = _config(tmp_path, parameters={"fee_rate": 0.001})
        engine = TradeEngine(config, symbol="PROBE")
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 2, "both entries must have filled"
        first = engine.closed_trades[0]
        assert first.quantity == pytest.approx(500.0, rel=1e-9), (
            "entry 1 risks 1% of 100k over the 2% fallback stop (2.0) = 500 shares"
        )
        assert [leg.reason for leg in first.exits] == ["tp1", "end_of_data"], (
            "the first rung must close half of entry 1 on bar 2, leaving the "
            "remainder open (the second rung is never reached)"
        )
        assert [leg.quantity for leg in first.exits] == pytest.approx([250.0, 250.0]), (
            "the 50% rung closes 250 of the 500 shares"
        )
        second = engine.closed_trades[1]
        assert second.meta["equity_at_entry"] == pytest.approx(101_423.5, rel=1e-9), (
            "the sizing basis is realized cash less the fees already incurred: "
            "100,000 + 1,500 gross − 50 entry − 26.5 exit = 101,423.5"
        )
        assert second.quantity == pytest.approx(1014.235 / 2.1, rel=1e-9), (
            "entry 2 sizes from 101,423.5, not from the gross 101,550 the "
            "ledger holds before any fee is charged, and not from 101,397 "
            "which would wrongly hold back the remainder's future exit fee"
        )

    def test_entry_fee_is_owed_before_the_first_exit(self, tmp_path: Path):
        """A position with NO exit yet still owes its entry leg, so the next
        entry may not size on cash the fee model has already been paid.

        Same shape as ``test_open_paper_profit_does_not_grow_next_entry`` but
        priced (fee_rate 0.001): entry 1 is re-sized at its 101 fill, so it
        keeps 1% of 100k over the 2% fallback stop of the fill (2.02) =
        495.05 shares and its entry leg is 0.001 x 101 x 495.05 = 50.0. The
        basis entry 2 is sized from is therefore 100,000 − 50 = 99,950, and
        entry 2 (fill 105, fallback distance 2.1) risks 1% of that =
        475.95 shares — where a run that held nothing back would still size
        from the full 100,000.
        """
        trades = self._run_open_position_scenario(
            105.0, tmp_path, parameters={"fee_rate": 0.001}
        )
        entry_qty = 1000.0 / 2.02
        entry_fee = 0.001 * 101.0 * entry_qty
        assert trades[0].quantity == pytest.approx(entry_qty, rel=1e-9)
        assert trades[1].quantity == pytest.approx(
            (0.01 * (100_000.0 - entry_fee)) / 2.1, rel=1e-9
        ), (
            "an open position's entry leg is already owed: the basis is "
            f"100,000 − {entry_fee} , not the gross 100,000"
        )
        assert trades[1].meta["equity_at_entry"] == pytest.approx(
            round(100_000.0 - entry_fee, 2), rel=1e-9
        )

    def test_finalized_fees_are_not_held_back_twice(self, tmp_path: Path):
        """A closed trade's fee is in the ledger, so it is not held back again.

        Entry 1 (200 shares @ 100, fee_rate 0.001) stops out at 95 on bar 2:
        the leg books −1,000 gross and finalization backs out the round-trip
        fee 0.001 x (100 + 95) x 200 = 39. Entry 2 is sized on bar 3, when the
        only ledger entries are those, so its basis is 100,000 − 1,000 − 39 =
        98,961 — its entry leg is no longer owed, and holding it back a second
        time would read 98,941.
        """
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
                "open": [100.0, 100.0, 95.5, 96.0, 96.0, 96.0],
                "high": [100.5, 100.5, 96.0, 96.5, 96.5, 96.5],
                "low": [99.5, 99.5, 94.5, 95.5, 95.5, 95.5],
                "close": [100.0, 100.0, 95.0, 96.0, 96.0, 96.0],
                "volume": [1000.0] * 6,
            }
        )
        entries = pd.Series([True, False, False, True, False, False], index=frame.index)
        exits = pd.Series([False] * len(frame), index=frame.index)
        blanks: list[float | None] = [None] * len(frame)
        stop_loss = pd.Series([95.0, *blanks[1:]], index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {"stop_loss": stop_loss})

        config = _config(tmp_path, parameters={"fee_rate": 0.001})
        engine = TradeEngine(config, symbol="PROBE")
        engine.run(frame, strategy)

        assert len(engine.closed_trades) == 2, "both entries must have filled"
        first = engine.closed_trades[0]
        assert first.quantity == pytest.approx(200.0, rel=1e-9), (
            "entry 1 risks 1% of 100k over its 5-point stop = 200 shares"
        )
        assert first.exits[0].reason == "stop_loss", "entry 1 stops out on bar 2"
        assert first.fees == pytest.approx(39.0, rel=1e-9), (
            "the round-trip fee is 0.001 x (100 + 95) x 200 = 39"
        )
        second = engine.closed_trades[1]
        assert second.meta["equity_at_entry"] == pytest.approx(98_961.0, rel=1e-9), (
            "the basis is the net realized cash 100,000 − 1,000 − 39; the "
            "closed trade's entry leg must not be held back a second time "
            "(which would read 98,941)"
        )
        assert second.quantity == pytest.approx(989.61 / 1.92, rel=1e-9), (
            "entry 2 sizes from 98,961: 1% of it over the 2% fallback stop "
            "(1.92) = 515.421875 shares"
        )


# ─── #347: leverage/margin check on free margin ─────────────────────────────


class TestFreeMarginLeverageCap:
    """#347 (decisions on #267/#240/#329): the leverage/margin check is
    MT5-style — an order's margin must fit FREE MARGIN (equity − used
    margin), sizing itself stays on realized cash (D2), and an order that
    would need more leverage than the broker maximum is clamped down
    (floored to the lot step) instead of refused. An unknown broker maximum
    means 1x."""

    @staticmethod
    def _engine(tmp_path: Path, *, parameters: dict | None = None, **overrides):
        engine = TradeEngine(
            _config(tmp_path, parameters=parameters or {}, **overrides),
            symbol="PROBE",
        )
        engine._refresh_sizing_equity()
        return engine

    @staticmethod
    def _open_position(
        engine: TradeEngine,
        *,
        entry_price: float = 100.0,
        qty: float = 1000.0,
        direction: str = "LONG",
        stop_loss: float | None = None,
    ) -> None:
        engine.positions.append(
            Position(
                symbol="PROBE",
                direction=direction,
                entry_index=0,
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=entry_price,
                order_type="market",
                quantity=qty,
                remaining_qty=qty,
                stop_loss=(
                    stop_loss
                    if stop_loss is not None
                    else entry_price * (1.02 if direction == "SHORT" else 0.98)
                ),
            )
        )
        engine._refresh_sizing_equity()

    def test_free_margin_is_equity_minus_used_margin(self, tmp_path: Path):
        engine = self._engine(tmp_path, max_leverage=2.0)
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # Marked at the order price 100: equity = 100k (no unrealized PnL),
        # used margin = 1000 * 100 / 2 = 50k → free margin = 50k.
        assert engine._free_margin(100.0) == pytest.approx(50_000.0)

    def test_leverage_cap_uses_free_margin_not_sizing_cash(self, tmp_path: Path):
        engine = self._engine(tmp_path, max_leverage=2.0)
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # The risk target wants 1000 / 0.1 = 10,000 shares; the realized-cash
        # cap would allow 200k notional (2,000 shares), but free margin only
        # affords 50k * 2 / 100 = 1,000 shares → clamped to 1,000.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == pytest.approx(1_000.0)

    def test_unrealized_profit_raises_equity_but_margin_still_binds(
        self, tmp_path: Path
    ):
        engine = self._engine(tmp_path, max_leverage=2.0)
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # New order priced at 120: mark-to-market equity = 100k + 20k = 120k,
        # used margin = 1000 * 120 / 2 = 60k → free margin = 60k, so the cap
        # is 60k * 2 / 120 = 1,000 shares. The paper profit lifts equity but
        # the position's own margin grows with the same mark.
        qty = engine._calculate_position_size(
            entry_price=120.0,
            stop_loss_price=119.9,
            direction="LONG",
        )
        assert qty == pytest.approx(1_000.0)

    def test_unrealized_loss_tightens_free_margin(self, tmp_path: Path):
        engine = self._engine(tmp_path, max_leverage=2.0)
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # New order priced at 80: equity = 100k − 20k = 80k, used margin =
        # 1000 * 80 / 2 = 40k → free margin = 40k → cap = 40k * 2 / 80 =
        # 1,000 shares. The realized-cash cap would have allowed 2,500.
        qty = engine._calculate_position_size(
            entry_price=80.0,
            stop_loss_price=79.9,
            direction="LONG",
        )
        assert qty == pytest.approx(1_000.0)

    def test_no_free_margin_clamps_to_zero(self, tmp_path: Path):
        engine = self._engine(tmp_path, max_leverage=2.0)
        # Tight stop keeps aggregate exposure under the cap so the zero
        # below comes from the leverage check, not the exposure rejection.
        self._open_position(
            engine, entry_price=100.0, qty=4000.0, stop_loss=99.9
        )
        # Used margin = 4000 * 100 / 2 = 200k > equity 100k → free margin is
        # negative; the broker refuses and the engine clamps to zero.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == 0.0

    def test_zero_free_margin_skip_is_logged(
        self, tmp_path: Path, caplog
    ):
        """A clamp to zero (no free margin) must not drop the order
        silently — it is skipped with a warning like the fill-time skips."""
        engine = self._engine(tmp_path, max_leverage=1.0)
        # At 1x the whole notional is margin: 1000 * 100 / 1 = 100k = equity,
        # so free margin is exactly zero for the next order.
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        row = pd.Series(
            {
                "close": 100.0,
                "timestamp": pd.Timestamp("2024-01-02T00:00:00Z"),
                "stop_loss": 99.0,
                "stop_distance": 1.0,
            }
        )
        with caplog.at_level(logging.WARNING):
            engine._create_order(row, 1, "LONG")
        assert engine.pending_orders == []
        assert any(
            "Skipping" in record.getMessage()
            and "sized quantity" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]

    def _fill_row(self) -> pd.Series:
        return pd.Series(
            {
                "timestamp": pd.Timestamp("2024-01-02T00:00:00Z"),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 1000.0,
            }
        )

    def test_fill_time_limit_zero_size_skip_is_logged(
        self, tmp_path: Path, caplog
    ):
        """A pending LIMIT sized >0 at creation but clamped to zero at fill
        (free margin ran out) must not be dropped silently."""
        from runner.engine import Order, OrderType

        engine = self._engine(tmp_path, max_leverage=1.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty > 0, "sized while free margin remains"
        engine.pending_orders.append(
            Order(
                symbol="PROBE",
                direction="LONG",
                order_type=OrderType.LIMIT,
                created_index=0,
                created_time="2024-01-01T00:00:00+00:00",
                quantity=qty,
                limit_price=100.0,
                stop_loss=99.0,
            )
        )
        # Consume all margin before the fill: at 1x, 1000 * 100 = 100k of
        # margin against 100k equity leaves free margin at zero.
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        with caplog.at_level(logging.WARNING):
            engine._fill_pending_orders(self._fill_row(), 1)
        assert engine.pending_orders == [], "the zero-size fill is cancelled"
        assert not engine.positions[1:], "no second position opens"
        assert any(
            "fill-time sized quantity" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]

    def test_fill_time_market_zero_size_skip_is_logged(
        self, tmp_path: Path, caplog
    ):
        """Same for a MARKET order re-sized at fill from its sizing stop."""
        from runner.engine import Order, OrderType

        engine = self._engine(tmp_path, max_leverage=1.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty > 0
        engine.pending_orders.append(
            Order(
                symbol="PROBE",
                direction="LONG",
                order_type=OrderType.MARKET,
                created_index=0,
                created_time="2024-01-01T00:00:00+00:00",
                quantity=qty,
                stop_loss=99.0,
            )
        )
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        with caplog.at_level(logging.WARNING):
            engine._fill_pending_orders(self._fill_row(), 1)
        assert engine.pending_orders == []
        assert len(engine.positions) == 1, "only the margin-consuming position"
        assert any(
            "fill-time sized quantity" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]

    def test_unknown_broker_maximum_means_1x(self, tmp_path: Path):
        engine = self._engine(tmp_path)
        assert engine._max_leverage == 1.0
        self._open_position(engine, entry_price=100.0, qty=500.0)
        # At 1x the position's whole notional is margin: used = 50k,
        # free = 50k → cap = 50k * 1 / 100 = 500 shares.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == pytest.approx(500.0)

    def test_leverage_clamp_floors_to_lot_step(self, tmp_path: Path):
        engine = self._engine(
            tmp_path,
            max_leverage=2.0,
            parameters={"lot_step": 100.0},
        )
        self._open_position(engine, entry_price=100.0, qty=1050.0)
        # used margin = 1050 * 100 / 2 = 52,500 → free = 47,500 → raw cap =
        # 950 shares; floored to the 100-share lot step → 900.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == pytest.approx(900.0)

    def test_lot_step_floor_does_not_shrink_a_size_leverage_allows(
        self, tmp_path: Path
    ):
        """The floor applies only when leverage is the binding cap: a risk
        size between the floored and raw cap keeps its exact value."""
        engine = self._engine(
            tmp_path,
            risk_per_trade_pct=0.0092,
            max_leverage=2.0,
            parameters={"lot_step": 100.0},
        )
        self._open_position(engine, entry_price=100.0, qty=1050.0)
        # Risk target = 0.92% * 100k / |100 - 99| = 920 shares; the raw
        # leverage cap is 950 (floored 900) — leverage does not bind, so the
        # size stays 920, not the floored 900.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(920.0)

    def test_leverage_clamp_floors_with_the_shared_lot_grid(
        self, tmp_path: Path
    ):
        """The free-margin clamp must floor through ``shared.lot_grid``
        (#365/#278), not a private ``floor(x / step + eps)``: an exact
        3-step cap on a 0.1 grid is ``0.3``, not ``0.30000000000000004`` —
        and the risk-engine path never re-floors, so a private floor's
        off-grid float would reach the order."""
        # Balance 30 at 1x with no open position: free margin = 30, so the
        # leverage cap is exactly 30 * 1 / 100 = 0.3 — three 0.1 steps.
        risk_engine = self._engine(
            tmp_path,
            initial_capital=30.0,
            max_leverage=1.0,
            parameters={"lot_step": 0.1},
        )
        # 1% of 30 = 0.3 risk over a ~0.1 stop distance → ~3 shares pre-cap.
        qty = risk_engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == 0.3

        fixed_risk = self._engine(
            tmp_path,
            initial_capital=30.0,
            max_leverage=1.0,
            parameters={
                "fixed_risk_amount": 30.0,
                "tick_value": 1.0,
                "tick_size": 1.0,
                "lot_step": 0.1,
            },
        )
        # 30 / ~0.1 ≈ 300 shares pre-cap; the same 0.3 leverage cap binds.
        qty = fixed_risk._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.9,
            direction="LONG",
        )
        assert qty == 0.3

    def test_exposure_cap_stays_on_realized_cash(self, tmp_path: Path):
        engine = self._engine(
            tmp_path, max_leverage=10.0, max_position_exposure_pct=0.4
        )
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # Mark the open position at 110: reporting equity reads 110k while
        # realized sizing cash stays 100k. The per-position exposure cap is
        # a sizing rule on realized cash (D2): 0.4 * 100k / 100 = 400
        # shares — sizing it off ``self.equity`` would wrongly give 440.
        # (At 10x free margin allows ~9,000, so the exposure cap binds.)
        engine.equity = 110_000.0
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(400.0)

    def test_fixed_risk_exposure_cap_stays_on_realized_cash(
        self, tmp_path: Path
    ):
        """The same D2 invariant on the ``fixed_risk_amount`` leg: there the
        risk engine is bypassed, so the exposure cap inside
        ``_apply_notional_caps`` is the ONLY realized-cash ceiling — a
        mutant reading ``self.equity`` would silently resize the MT5-parity
        fixed-risk strategies this branch exists for."""
        engine = self._engine(
            tmp_path,
            max_leverage=10.0,
            max_position_exposure_pct=0.4,
            parameters={
                "fixed_risk_amount": 5000.0,
                "tick_value": 1.0,
                "tick_size": 1.0,
            },
        )
        self._open_position(engine, entry_price=100.0, qty=1000.0)
        # Fixed size = 5000 / |100 - 99| = 5,000 shares; the exposure cap on
        # realized cash is 0.4 * 100k / 100 = 400 — sizing it off
        # ``self.equity`` (110k with the open mark) would give 440. At 10x
        # free margin allows ~9,000, so only the exposure cap binds.
        engine.equity = 110_000.0
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(400.0)

    def test_nan_free_margin_fails_closed(self, tmp_path: Path):
        """A NaN free margin must clamp to zero: ``nan <= 0`` is False, so
        dropping the ``isfinite`` check would let ``min(qty, nan)`` keep
        the uncapped size. Here a NaN entry mark on the open position
        poisons mark-to-market equity inside ``_free_margin``."""
        engine = self._engine(
            tmp_path,
            max_leverage=10.0,
            parameters={
                "fixed_risk_amount": 5000.0,
                "tick_value": 1.0,
                "tick_size": 1.0,
            },
        )
        self._open_position(engine, entry_price=float("nan"), qty=1000.0)
        assert engine._free_margin_quantity_cap(100.0) == 0.0
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == 0.0

    def test_short_position_margin_and_pnl_signs(self, tmp_path: Path):
        engine = self._engine(tmp_path, max_leverage=2.0)
        self._open_position(
            engine, entry_price=100.0, qty=1000.0, direction="SHORT"
        )
        # New SHORT order priced at 80: the open short is up 20 → equity =
        # 120k; used margin = 1000 * 80 / 2 = 40k → free = 80k → cap =
        # 80k * 2 / 80 = 2,000 shares.
        qty = engine._calculate_position_size(
            entry_price=80.0,
            stop_loss_price=80.1,
            direction="SHORT",
        )
        assert qty == pytest.approx(2_000.0)

    def test_fixed_risk_branch_clamps_on_free_margin(self, tmp_path: Path):
        engine = self._engine(
            tmp_path,
            max_leverage=2.0,
            parameters={
                "fixed_risk_amount": 500.0,
                "tick_value": 1.0,
                "tick_size": 1.0,
            },
        )
        self._open_position(engine, entry_price=100.0, qty=2000.0)
        # Fixed size = 500 / 1 = 500 shares; used margin = 2000 * 100 / 2 =
        # 100k = equity → free margin 0 → clamped to 0.
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == 0.0

    def test_fixed_risk_leverage_opt_out_keeps_uncapped_size(self, tmp_path: Path):
        engine = self._engine(
            tmp_path,
            max_leverage=2.0,
            parameters={
                "fixed_risk_amount": 500.0,
                "tick_value": 1.0,
                "tick_size": 1.0,
                "cap_fixed_risk_by_leverage": False,
            },
        )
        self._open_position(engine, entry_price=100.0, qty=2000.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(500.0)

    def test_sizing_target_still_uses_realized_cash(self, tmp_path: Path):
        """D2 unchanged: only the leverage check moved to free margin — the
        risk target itself must still be 1% of realized cash (100k), not of
        mark-to-market equity (101k with the open profit)."""
        engine = self._engine(tmp_path, max_leverage=10.0)
        self._open_position(engine, entry_price=100.0, qty=100.0)
        # The order prices at 110 while the position entered at 100, so
        # mark-to-market equity is 101k — but the risk target must still be
        # 1% of realized cash: 1,000 / |110 - 109| = 1,000 shares, not the
        # 1,010 a mark-to-market basis would produce. At 10x the free-margin
        # cap (~9,000 shares) does not bind.
        qty = engine._calculate_position_size(
            entry_price=110.0,
            stop_loss_price=109.0,
            direction="LONG",
        )
        assert qty == pytest.approx(1_000.0)

    def test_run_clamps_second_entry_on_free_margin(self, tmp_path: Path):
        """End-to-end: with the default unknown-broker 1x, a second entry
        sized while the first rides is clamped to what free margin affords.

        1.5% risk: entry 1 is re-sized at its 101 fill to 1500 / (0.02 x 101)
        = 742.57 shares — 75k notional, so free margin at the 105 mark is
        100k − 75k = 25k → cap = 238.10 shares at 1x, far below the 714.29
        the 1.5%-of-realized-cash risk target on the 105 fill would want.
        """
        frame = _linear_frame(6, start=100.0)
        for bar in (1, 2, 3):
            frame.loc[bar, "open"] = 105.0
            frame.loc[bar, "high"] = 105.5
            frame.loc[bar, "low"] = 104.5
            frame.loc[bar, "close"] = 105.0
        frame.loc[1, "open"] = 101.0  # entry 1's fill bar keeps its open
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)

        engine = TradeEngine(
            _config(tmp_path, risk_per_trade_pct=0.015), symbol="PROBE"
        )
        engine.run(frame, strategy)

        assert len(engine.closed_trades) >= 2, "both entries must have filled"
        assert engine.closed_trades[0].quantity == pytest.approx(
            1500.0 / 2.02, rel=1e-9
        )
        assert engine.closed_trades[1].quantity == pytest.approx(
            25_000.0 / 105.0, rel=1e-9
        ), (
            "the second entry is clamped to free margin (25k at 1x), not "
            "the 714.29 shares the realized-cash risk target would want"
        )


class TestSizingEquityBasis:
    def test_equity_at_entry_records_the_sizing_basis_not_mark_to_market(self):
        """cubic #190: the replay derives quantity / equity_at_entry, so the
        recorded basis must be the realized cash the entry was sized from."""
        frame = _linear_frame(6, start=100.0)
        frame.loc[2, "high"] = 130.0
        frame.loc[2, "close"] = 130.0
        frame.loc[3, "close"] = 130.0
        frame.loc[3, "open"] = 130.0
        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[0] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * len(frame), index=frame.index)
        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, lambda df, params: (entries, exits))

        assert len(engine.closed_trades) >= 2
        # Open +30/qty profit from entry 1 must not leak into the basis.
        assert engine.closed_trades[1].meta["equity_at_entry"] == pytest.approx(100_000.0)

    def test_partial_exit_cash_is_net_of_fees_still_owed(self):
        """cubic #190: gross partial proceeds are not spendable while the
        fees already incurred on the position are unsettled. The owed amount
        is the entry leg on the full quantity plus the exit fee of each leg
        that actually closed; the open remainder's future exit fee is a
        future cost and is not held back."""
        from runner.engine import Order, OrderType

        engine = TradeEngine(
            _config(Path("/tmp"), parameters={"fee_rate": 0.001}), symbol="PROBE"
        )
        order = Order(
            symbol="PROBE",
            direction="LONG",
            order_type=OrderType.MARKET,
            created_index=0,
            created_time="2024-01-01T00:00:00+00:00",
            quantity=100.0,
        )
        engine._open_position(order, 100.0, "2024-01-01T01:00:00+00:00", 1)
        position = engine.positions[0]
        engine._close_partial(position, 110.0, 50.0, "2024-01-01T02:00:00+00:00", "tp1")

        gross = 50.0 * 10.0
        # Owed = the entry leg on the full position plus the exit fee of the
        # leg that actually closed; the still-open remainder's future exit fee
        # is not held back (the #267 realized-cash rule).
        owed = 100.0 * 100.0 * 0.001 + 110.0 * 50.0 * 0.001
        assert engine._sizing_equity == pytest.approx(100_000.0 + gross - owed)


# ─── F22: max_positions comes from risk policy, not strategy params ─────────


class TestFixedRiskViaRiskEngine:
    """#279: ``fixed_risk_amount`` sizing must go through
    ``RiskEngine.calculate_position_size`` (via ``risk_amount_override``) so
    ``max_positions``, the aggregate ``max_total_exposure`` ceiling and the
    engine's input validation apply — the fixed-risk branch used to return
    before the risk engine and skip all three. Quantities must stay identical
    where the caps never bound (the MT5-parity sizing protocol)."""

    @staticmethod
    def _engine(tmp_path: Path, *, parameters: dict | None = None, **overrides):
        engine = TradeEngine(
            _config(tmp_path, parameters=parameters or {}, **overrides),
            symbol="PROBE",
        )
        engine._refresh_sizing_equity()
        return engine

    @staticmethod
    def _open_position(
        engine: TradeEngine,
        *,
        entry_price: float = 100.0,
        qty: float = 100.0,
        stop_loss: float | None = None,
    ) -> None:
        engine.positions.append(
            Position(
                symbol="PROBE",
                direction="LONG",
                entry_index=0,
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=entry_price,
                order_type="market",
                quantity=qty,
                remaining_qty=qty,
                stop_loss=stop_loss if stop_loss is not None else entry_price * 0.98,
            )
        )
        engine._refresh_sizing_equity()

    @staticmethod
    def _fixed_params(**extra) -> dict:
        params = {"fixed_risk_amount": 500.0, "tick_value": 1.0, "tick_size": 1.0}
        params.update(extra)
        return params

    def test_fixed_risk_respects_max_positions(self, tmp_path: Path):
        """With ``fixed_risk_max_positions`` configured, five open positions
        fill the limit: the fixed-risk branch must not size a sixth order —
        today it bypasses the engine and does."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            max_leverage=100.0,
            fixed_risk_max_positions=5,
        )
        for _ in range(5):
            self._open_position(engine)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == 0.0

    def test_fixed_risk_pending_orders_count_toward_max_positions(
        self, tmp_path: Path
    ):
        """C2 parity with the pct path: pending orders are committed intent,
        so 4 positions + 1 pending order already fills the limit."""
        from runner.engine import Order, OrderType

        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            max_leverage=100.0,
            fixed_risk_max_positions=5,
        )
        for _ in range(4):
            self._open_position(engine)
        engine.pending_orders.append(
            Order(
                symbol="PROBE",
                direction="LONG",
                order_type=OrderType.MARKET,
                created_index=0,
                created_time="2024-01-01T00:00:00+00:00",
                quantity=10.0,
                stop_loss=99.0,
            )
        )
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == 0.0

    def test_fixed_risk_rejects_when_aggregate_exposure_is_spent(
        self, tmp_path: Path
    ):
        """max_total_exposure (5% of realized cash) was invisible to the
        fixed-risk branch: two positions already carrying 12% aggregate risk
        must reject the next entry, not size it."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        # Each position risks |100 - 98| * 3000 = 6,000 → 0.06 of 100k; two
        # of them are 0.12 > 0.05. High leverage keeps the margin cap out of
        # the way so only the exposure ceiling can produce the zero.
        self._open_position(engine, qty=3000.0, stop_loss=98.0)
        self._open_position(engine, qty=3000.0, stop_loss=98.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == 0.0

    def test_fixed_risk_clamps_to_remaining_exposure_budget(
        self, tmp_path: Path
    ):
        """The aggregate ceiling clamps like the pct path does: with 1% of
        exposure already used, only 4% of balance remains as risk budget, so
        a $5,000 fixed risk sizes as $4,000 — 4,000 shares at a 1.0 distance,
        not the 5,000 the override alone would give."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=5000.0),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        # |100 - 99.5| * 2000 = 1,000 risk → 0.01 exposure; remaining = 0.04.
        self._open_position(engine, qty=2000.0, stop_loss=99.5)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(4_000.0)

    def test_fixed_risk_validates_sizing_equity(self, tmp_path: Path):
        """Engine input validation now applies: a non-positive realized-cash
        basis must raise instead of sizing against it."""
        engine = self._engine(tmp_path, parameters=self._fixed_params(), max_leverage=100.0)
        engine._sizing_equity = -1.0
        with pytest.raises(ValueError, match="account_balance"):
            engine._calculate_position_size(
                entry_price=100.0,
                stop_loss_price=99.0,
                direction="LONG",
            )

    def test_fixed_risk_still_sizes_without_risk_pct(self, tmp_path: Path):
        """Regression guard: fixed_risk_amount is the sizing protocol for
        MT5-parity runs whose configs never set risk_per_trade_pct — routing
        through the engine must not make the pct config a prerequisite."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            risk_per_trade_pct=0.0,
            max_leverage=100.0,
        )
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(500.0)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -500.0])
    def test_fixed_risk_rejects_non_finite_or_non_positive_amount(
        self, tmp_path: Path, bad: float
    ):
        """A configured ``fixed_risk_amount`` that is NaN/inf/< 0 is a config
        bug: it must raise, not silently fall through to pct sizing (inf
        would otherwise size without bound). ``0.0`` is different — it is
        the documented "off" idiom and falls through to pct sizing."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=bad),
            max_leverage=100.0,
        )
        with pytest.raises(ValueError, match="fixed_risk_amount"):
            engine._calculate_position_size(
                entry_price=100.0,
                stop_loss_price=99.0,
                direction="LONG",
            )

    def test_fixed_risk_zero_amount_is_off(self, tmp_path: Path):
        """``fixed_risk_amount: 0.0`` is the documented "off" idiom (#170 on
        main): the order falls through to pct sizing exactly like an absent
        key — it must not raise and must not size off zero."""
        zero_engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=0.0),
            risk_per_trade_pct=0.01,
            max_leverage=100.0,
        )
        pct_engine = self._engine(
            tmp_path,
            parameters={"tick_value": 1.0, "tick_size": 1.0},
            risk_per_trade_pct=0.01,
            max_leverage=100.0,
        )
        qty_zero = zero_engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        qty_pct = pct_engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty_zero == pytest.approx(qty_pct)
        assert qty_zero > 0.0

    # ── bundle-46 review: exposure units, configurable limits, declared risk ──

    @staticmethod
    def _fx_params(**extra) -> dict:
        # EURUSD-like golden tick metadata: pip = tick_value / tick_size = 1e5.
        params = {
            "fixed_risk_amount": 5000.0,
            "tick_value": 1.0,
            "tick_size": 0.00001,
            "cap_fixed_risk_by_leverage": False,
        }
        params.update(extra)
        return params

    def test_fixed_risk_exposure_is_measured_in_account_currency_for_fx(
        self, tmp_path: Path
    ):
        """pip = 1e5: four positions each risking $4,000 on a 100k account are
        16% aggregate risk — the 5% ceiling must reject the fifth order. The
        unscaled ``|entry - stop| * qty`` sum saw 1.6e-06 and sized it fully."""
        engine = self._engine(
            tmp_path,
            parameters=self._fx_params(),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        # qty * |1.10 - 1.09| * 1e5 = 4 * 0.01 * 1e5 = $4,000 each.
        for _ in range(4):
            self._open_position(engine, entry_price=1.10, qty=4.0, stop_loss=1.09)
        qty = engine._calculate_position_size(
            entry_price=1.10,
            stop_loss_price=1.09,
            direction="LONG",
        )
        assert qty == 0.0

    def test_fixed_risk_exposure_with_sub_unit_pip_does_not_overstate(
        self, tmp_path: Path
    ):
        """pip = 0.1: one position really risking $500 (0.5%) must leave 4.5%
        of budget — the unscaled sum saw the whole 5% and rejected the second
        order outright."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(
                fixed_risk_amount=1000.0, tick_value=0.1, tick_size=1.0
            ),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        # qty * |100 - 99| * 0.1 = 5000 * 1 * 0.1 = $500.
        self._open_position(engine, qty=5000.0, stop_loss=99.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        # Unclamped: 1000 / (1 * 0.1) = 10,000 units.
        assert qty == pytest.approx(10_000.0)

    def test_fixed_risk_exposure_uses_the_declared_stop_distance(
        self, tmp_path: Path
    ):
        """A position sized on ``stop_distance`` carries its declared per-unit
        risk: re-measuring it with ``|entry - stop|`` would understate the
        exposure and oversize the next order."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=4000.0),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        position = Position(
            symbol="PROBE",
            direction="LONG",
            entry_index=0,
            entry_time="2024-01-01T00:00:00+00:00",
            entry_price=100.0,
            order_type="market",
            quantity=1000.0,
            remaining_qty=1000.0,
            stop_loss=99.0,  # |entry - stop| = 1, but the declared distance was 2
            meta={"sizing_risk_per_unit": 2.0},
        )
        engine.positions.append(position)
        engine._refresh_sizing_equity()
        # Declared risk = 1000 * 2.0 = $2,000 → 0.02 exposure; remaining 3%
        # → budget $3,000 → qty 3,000 at distance 1 (not the $4,000 a
        # |entry - stop| re-measurement of 0.01 exposure would allow).
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(3_000.0)

    def test_stop_distance_only_estimate_is_not_vetoed_by_pct_caps(
        self, tmp_path: Path
    ):
        """#279: a ``stop_distance``-only order has no stop at creation, so
        its creation-time estimate lands on the pct path — but the
        fill-time re-size applies the fixed-risk branch, whose aggregate
        and position caps are OFF by default. The pct engine's own 5% /
        5-position defaults must not veto the estimate.

        Two open fixed-risk FX positions each carry $5,000 of declared
        risk (``sizing_risk_per_unit`` = distance x pip = 0.01 x 1e5 =
        1,000 per unit, qty 5) — 10% combined on the 100k realized cash,
        over the pct engine's 5% default. On main the same estimate
        sized normally (its exposure figure never saw the declared
        per-unit risk); the estimate must still size here."""
        engine = self._engine(
            tmp_path,
            parameters=self._fx_params(),
            max_leverage=100.0,
        )
        for _ in range(2):
            engine.positions.append(
                Position(
                    symbol="PROBE",
                    direction="LONG",
                    entry_index=0,
                    entry_time="2024-01-01T00:00:00+00:00",
                    entry_price=1.10,
                    order_type="market",
                    quantity=5.0,
                    remaining_qty=5.0,
                    stop_loss=1.09,
                    meta={"sizing_risk_per_unit": 1_000.0},
                )
            )
        engine._refresh_sizing_equity()
        qty = engine._calculate_position_size(
            entry_price=1.10,
            stop_loss_price=None,
            direction="LONG",
            stop_distance=0.01,
        )
        assert qty > 0.0, (
            "the creation-time estimate must not be vetoed by the pct "
            "engine's caps: the fill-time re-size applies the fixed-risk "
            "branch, whose caps default to off"
        )
        assert engine._last_sizing is not None
        assert engine._last_sizing.decision is not None
        assert engine._last_sizing.decision.rejection_reason is None

    def test_stop_distance_only_fixed_risk_entries_match_main(
        self, tmp_path: Path
    ):
        """#279 end-to-end: three concurrent ``stop_distance``-only
        fixed-risk entries each declare $5,000 risk — 15% combined on a
        100k account, far over the pct engine's 5% default that vetoed
        the creation-time estimate. Main sizes all three at the
        fixed-risk quantity (500 units = 5000 / 10 x tick_size/tick_value);
        the trade count and quantities must equal it."""
        n = 10
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
                "open": [100.0] * n,
                "high": [100.5] * n,
                "low": [99.5] * n,
                "close": [100.0] * n,
                "volume": [1000.0] * n,
            }
        )
        entries = pd.Series([False] * n, index=frame.index)
        entries.iloc[0] = True
        entries.iloc[1] = True
        entries.iloc[2] = True
        exits = pd.Series([False] * n, index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits, {
            "stop_distance": pd.Series([10.0] * n, index=df.index),
        })

        engine = TradeEngine(
            _config(
                tmp_path,
                parameters={
                    "fixed_risk_amount": 5000.0,
                    "tick_value": 1.0,
                    "tick_size": 1.0,
                    "cap_fixed_risk_by_leverage": False,
                },
                max_leverage=100.0,
            ),
            symbol="PROBE",
        )
        engine.run(frame, strategy)

        # Open positions are closed at end of data, so the run's trade
        # count is the closed-trade count.
        assert len(engine.closed_trades) == 3, (
            "all three entries must fill: the pct engine's default caps "
            "are off for the fixed-risk protocol"
        )
        for trade in engine.closed_trades:
            assert trade.quantity == pytest.approx(500.0), (
                "each position risks the declared $5,000 over the 10-point "
                "stop_distance: 5000 / 10 x (1 / 1) = 500 units, the same "
                "quantity main's fixed-amount branch produced"
            )

    def test_fixed_risk_amount_above_equity_sizes_like_main(
        self, tmp_path: Path
    ):
        """#279: ``fixed_risk_amount`` larger than the sizing equity is a
        legitimate state (only equity <= 0 raises) — main and the MT5 EA
        size it and let the margin/leverage clamp apply afterwards. The
        reported ``risk_percent`` saturates at 1.0; the declared dollars
        and the quantity are unchanged."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(
                fixed_risk_amount=5000.0, cap_fixed_risk_by_leverage=False
            ),
            max_leverage=100.0,
        )
        engine._sizing_equity = 3_000.0
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(5_000.0), (
            "5000 / |100 - 99| x (1 / 1) = 5,000 units — the same "
            "quantity main's inline fixed-amount formula produced"
        )
        sized = engine._last_sizing
        assert sized is not None
        assert sized.risk_amount == pytest.approx(5_000.0)
        assert sized.risk_percent == pytest.approx(1.0)

    def test_fixed_risk_limits_are_configurable(self, tmp_path: Path):
        """``fixed_risk_max_total_exposure`` / ``fixed_risk_max_positions``
        tune the caps for the fixed-risk protocol only; ``None`` means
        UNCAPPED (the settled parity-safe default)."""
        engine = self._engine(
            tmp_path,
            parameters=self._fx_params(),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.20,
            fixed_risk_max_positions=10,
        )
        # 4 * $4,000 = $16,000 = 16%: over the 5% default but inside the
        # configured 20% — remaining 4% → $4,000 budget → qty 4.0 at
        # distance 0.01 and pip 1e5 (the default would reject outright).
        for _ in range(4):
            self._open_position(engine, entry_price=1.10, qty=4.0, stop_loss=1.09)
        qty = engine._calculate_position_size(
            entry_price=1.10,
            stop_loss_price=1.09,
            direction="LONG",
        )
        assert qty == pytest.approx(4.0), (
            "clamped to the configured 20% budget: 0.04 * 100k / (0.01 * 1e5)"
        )

    def test_fixed_risk_max_positions_configurable(self, tmp_path: Path):
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            max_leverage=100.0,
            fixed_risk_max_positions=7,
        )
        for _ in range(6):
            self._open_position(engine)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(500.0), "7th position fits a limit of 7"

    def test_fixed_risk_defaults_are_uncapped(self, tmp_path: Path):
        """Settled parity-safe default: ``None`` on both
        ``fixed_risk_max_*`` fields means NO aggregate exposure ceiling and
        NO position cap — the MT5 EA the protocol mirrors applies neither.
        Six positions already past the old 5-position/5% limits must still
        size the full declared amount."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=5000.0),
            max_leverage=100.0,
        )
        # Each position risks |100 - 90| * 5000 = $50,000 → 6 x 50% aggregate
        # risk on a 100k account — absurdly over the old 5% ceiling, and a
        # 6th concurrent position over the old 5-position cap.
        for _ in range(6):
            self._open_position(engine, qty=5000.0, stop_loss=90.0)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(5_000.0), (
            "uncapped default: the declared $5,000 risk sizes in full "
            "(5000 / 1.0 distance, pip 1.0)"
        )

    def test_fixed_risk_exposure_counts_current_stop_after_breakeven(
        self, tmp_path: Path
    ):
        """A stop trailed to breakeven must not keep charging the declared
        per-unit risk: the position's real open risk is ~0, so the exposure
        budget is whole again for the next order."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=5000.0),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        # Sized risking $2/unit (declared distance 2, sizing stop 98); the
        # stop has since trailed to breakeven at the 100 entry.
        engine.positions.append(
            Position(
                symbol="PROBE",
                direction="LONG",
                entry_index=0,
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=100.0,
                order_type="market",
                quantity=1000.0,
                remaining_qty=1000.0,
                stop_loss=100.0,
                meta={
                    "sizing_risk_per_unit": 2.0,
                    "sizing_stop_loss": 98.0,
                },
            )
        )
        engine._refresh_sizing_equity()
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        # Breakeven stop → ~0 open risk → the full 5% budget ($5,000) is
        # available: 5000 / 1.0 = 5,000 units. Counting the stale declared
        # $2,000 (2%) would leave only 3% → 3,000.
        assert qty == pytest.approx(5_000.0)

    def test_fixed_risk_size_uses_the_mt5_order_of_operations(
        self, tmp_path: Path
    ):
        """Floor flips: ``a / (b * (tv / ts))`` vs ``(a / b) * (ts / tv)``
        differ by an ulp — the accepted size is recomputed in the MT5 order
        so the pre-grid value stays bit-identical to legacy when unclamped.
        Since #278 the shared ``_floor_to_lot_grid`` snap band (1e-6 of a
        step, exact decimal) absorbs that ulp, so the two orders now agree
        on the grid result; the recompute is retained for the parity
        protocol's pre-grid value."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(
                fixed_risk_amount=228.0,
                tick_value=1.0,
                tick_size=0.00001,
                lot_step=0.01,
                min_lot=0.01,
                max_lot=1000.0,
                cap_fixed_risk_by_leverage=False,
            ),
            max_leverage=100.0,
        )
        qty = engine._calculate_position_size(
            entry_price=1.10,
            stop_loss_price=1.10 - 1e-5,
            direction="LONG",
            stop_distance=1e-5,
        )
        # MT5 order: (228.0 / 1e-5) * (1e-5 / 1.0) = 227.99999999999997,
        # one ulp below the 228.0 grid point; the engine's
        # ``risk / (distance * pip_value)`` form gives
        # 228.00000000000003, one ulp above. Both sit inside the shared
        # grid floor's 1e-6-of-a-step snap band (#278), so the floored
        # result is 228.0 either way — the snap band, not the operation
        # order, decides this edge now.
        assert qty == pytest.approx(228.0)

    def test_fixed_risk_size_without_grid_is_the_exact_mt5_product(
        self, tmp_path: Path
    ):
        """The same config with ``lot_step = 0`` (no broker grid): nothing
        floors or snaps the size, so the quantity IS the MT5-order
        recompute bit-for-bit — ``(228.0 / 1e-5) * (1e-5 / 1.0)`` =
        227.99999999999997, one ulp below the engine's
        ``risk / (distance * pip)`` order (228.00000000000003). This pins
        the MT5-order recompute itself, which the grid case above can no
        longer discriminate."""
        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(
                fixed_risk_amount=228.0,
                tick_value=1.0,
                tick_size=0.00001,
                cap_fixed_risk_by_leverage=False,
            ),
            max_leverage=100.0,
        )
        qty = engine._calculate_position_size(
            entry_price=1.10,
            stop_loss_price=1.10 - 1e-5,
            direction="LONG",
            stop_distance=1e-5,
        )
        assert qty == 227.99999999999997

    def test_fixed_risk_clamped_size_is_equity_proportional(
        self, tmp_path: Path
    ):
        """A_5 exposure-clamp-bound fixed-risk quantity is a function of the
        sizing equity, so the shared-equity replay must re-scale it; an
        unclamped one stays absolute-$."""
        from runner.engine import Order, OrderType

        engine = self._engine(
            tmp_path,
            parameters=self._fixed_params(fixed_risk_amount=5000.0),
            max_leverage=100.0,
            fixed_risk_max_total_exposure=0.05,
        )
        self._open_position(engine, qty=2000.0, stop_loss=99.5)
        qty = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert qty == pytest.approx(4_000.0)  # clamped: 4% of 100k budget
        order = Order(
            symbol="PROBE",
            direction="LONG",
            order_type=OrderType.MARKET,
            created_index=0,
            created_time="2024-01-01T00:00:00+00:00",
            quantity=qty,
            stop_loss=99.0,
        )
        engine._record_fixed_risk_sizing(
            order,
            entry_price=100.0,
            stop_loss_price=99.0,
            stop_distance=None,
        )
        assert order._equity_proportional is True
        assert order._sizing_risk_per_unit == pytest.approx(1.0)

        unclamped = self._engine(
            tmp_path,
            parameters=self._fixed_params(),
            max_leverage=100.0,
        )
        unclamped_qty = unclamped._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
        )
        assert unclamped_qty == pytest.approx(500.0)
        order2 = Order(
            symbol="PROBE",
            direction="LONG",
            order_type=OrderType.MARKET,
            created_index=0,
            created_time="2024-01-01T00:00:00+00:00",
            quantity=unclamped_qty,
            stop_loss=99.0,
        )
        unclamped._record_fixed_risk_sizing(
            order2,
            entry_price=100.0,
            stop_loss_price=99.0,
            stop_distance=None,
        )
        assert order2._equity_proportional is False
        assert order2._sizing_risk_per_unit == pytest.approx(1.0)


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
    LIMIT/STOP/STOP_LIMIT fill at their level (or the bar open on a stop
    gap-through) → sized from that fill price.
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
        engine, _frame = self._limit_order_engine(limit_price=100.5)
        assert engine.closed_trades, "limit order must fill"
        trade = engine.closed_trades[0]
        # C1: explicit risk sizing (1% of 100k = 1000 risk, 2% fallback stop
        # on the 100.5 trigger → distance 2.01).
        expected_qty = 1000.0 / 2.01
        assert trade.quantity == pytest.approx(expected_qty, rel=1e-6), (
            "LIMIT orders must be sized at fill from the trigger price, "
            "not keep the creation-price estimate"
        )

    def test_stop_order_is_resized_to_trigger_price_at_fill(self):
        """STOP orders (short side, trigger above) size at the trigger price."""
        frame = _linear_frame(6)
        # Bar 2 rallies through the stop of a SHORT stop order at 101.5.
        # Open 101.7 stays above the stop so this is an intrabar touch, not
        # a gap-through-open fill (covered by TestPendingStopGapPricing).
        frame.loc[2, "high"] = 102.5
        frame.loc[2, "open"] = 101.7

        entries = pd.Series([False] * len(frame), index=frame.index)
        entries.iloc[1] = True
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
        # C1: explicit risk sizing (1000 risk, 2% fallback stop on the
        # 101.5 trigger → distance 2.03).
        assert trade.quantity == pytest.approx(1000.0 / 2.03, rel=1e-6), (
            "STOP orders must be sized at fill from the trigger price"
        )

    def test_market_order_without_stop_sizes_on_the_fill_price(self):
        """Stopless MARKET entries size on the FILL via the documented fallback.

        16-D1 documented an exception ("a MARKET order without any stop has no
        fill-time risk input, so its creation-time estimate stands") and
        pinned it here. That exception is superseded, for two documented
        reasons:

        1. the premise is false — a stopless order DOES have a documented
           fill-time risk input, the fallback stop (`allow_fallback_stop` /
           `RiskConfig.fallback_stop_pct`, documented in
           `docs/live-risk-consistency.md`, and in
           `docs/cross-engine-backtest-contract.md` §4 as "a documented
           default stop distance ... 2% of entry"), which the risk engine
           derives from the entry price it is handed — i.e. from the fill.
        2. keeping it breached a documented cap. The notional cap is
           `equity * leverage / fill`, but the creation sizing came from the
           signal bar's close, so any gap between them sized a position
           beyond the cap (the #189 differential repro: 103.4k notional on a
           100k equity / 1x cap).

        The class policy above ("MARKET fills at next-bar open → sized from
        the fill price") therefore holds for every MARKET order.
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
        # Fill happens on bar 1 (open 101); C1 risk sizing uses the fallback
        # distance 2% * 101 = 2.02 of the FILL, not 2% of bar 0's close:
        # 1000 risk / 2.02.
        assert trade.entry_price == pytest.approx(101.0)
        assert trade.quantity == pytest.approx(1000.0 / 2.02, rel=1e-6)
        assert trade.quantity * trade.entry_price <= 100_000.0 * (1.0 + 1e-12)

    def test_market_order_without_stop_sizes_on_the_fill_after_a_gap(self):
        """Same rule across a gap: the risk distance follows the fill, not the
        signal close. Signal close 100, fill 120 → 1000 risk / (2% * 120)."""
        frame = _linear_frame(4)
        frame.loc[1, ["open", "high", "low", "close"]] = [120.0, 120.5, 119.5, 120.0]
        entries = pd.Series([True] + [False] * 3, index=frame.index)
        exits = pd.Series([False] * 4, index=frame.index)
        strategy = lambda df, params: (  # noqa: E731
        entries, exits)  # noqa: E731

        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

        assert engine.closed_trades
        trade = engine.closed_trades[0]
        assert trade.entry_price == pytest.approx(120.0)
        assert trade.quantity == pytest.approx(1000.0 / 2.4, rel=1e-6)


# ─── #288: pending orders never fill on their creation bar ──────────────────


class TestPendingOrderCreationBarGuard:
    """LIMIT/STOP/STOP_LIMIT share the MARKET rule: a pending order may only
    fill on a bar strictly after the bar whose close produced its signal.
    Filling on the creation bar would spend that bar's own range — lookahead.

    The native engine only creates MARKET orders today (#339's explicit exit
    order types will use these branches), so the guard is pinned directly on
    ``_fill_pending_orders``.
    """

    @staticmethod
    def _engine(tmp_path: Path, **param_overrides) -> TradeEngine:
        return TradeEngine(
            _config(tmp_path, parameters=dict(param_overrides)), symbol="PROBE"
        )

    @staticmethod
    def _order(order_type, direction: str, created_index: int, **kwargs):
        from runner.engine import Order

        return Order(
            symbol="PROBE",
            direction=direction,
            order_type=order_type,
            created_index=created_index,
            created_time="2024-01-01T01:00:00+00:00",
            quantity=10.0,
            **kwargs,
        )

    def test_limit_long_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        order = self._order(
            OrderType.LIMIT,
            "LONG",
            created_index=1,
            limit_price=101.5,
            stop_loss=100.0,
        )
        engine.pending_orders.append(order)

        # Bar 1 is the creation bar: low 100.5 <= 101.5 touches the limit,
        # but the order must survive untouched.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        # Bar 2's low (101.5) still touches the limit -> fills one bar later.
        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        position = engine.positions[0]
        assert position.entry_index == 2
        assert position.entry_price == pytest.approx(101.5)

    def test_limit_short_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        order = self._order(
            OrderType.LIMIT,
            "SHORT",
            created_index=1,
            limit_price=101.0,
            stop_loss=102.0,
        )
        engine.pending_orders.append(order)

        # Bar 1 high (101.5) >= 101.0 touches the short limit -> guarded.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        # Bar 2 high (102.5) still trades through -> fills at the limit.
        # Bar 2 opens at 102.0, above the 101.0 sell limit, so a real fill
        # would price-improve to the open; LIMIT deliberately keeps the
        # conservative level fill (#371).
        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.0)

    def test_stop_long_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        # Bar 2 must not gap through the buy stop in this guard test: open
        # 100.8 is below the 101.0 stop, so the intrabar touch fills at the
        # level. Gap-through-open pricing is covered by TestPendingStopGapPricing.
        frame.loc[2, "open"] = 100.8
        frame.loc[2, "low"] = 100.7
        order = self._order(
            OrderType.STOP,
            "LONG",
            created_index=1,
            stop_price=101.0,
            stop_loss=100.0,
        )
        engine.pending_orders.append(order)

        # Bar 1 high (101.5) >= 101.0 triggers the stop -> guarded.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        # Bar 2 high (102.5) still trades through -> fills at the stop.
        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.0)

    def test_stop_short_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        frame.loc[2, "low"] = 100.0  # only bar 2 trades down through the stop
        order = self._order(
            OrderType.STOP,
            "SHORT",
            created_index=1,
            stop_price=100.6,
            stop_loss=102.0,
        )
        engine.pending_orders.append(order)

        # Bar 1 low (100.5) <= 100.6 triggers the short stop -> guarded.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(100.6)

    def test_stop_limit_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        # Bar 2 keeps the stop trigger (high 102.5 >= 101.0) but only dips
        # to the limit when its low is forced down.
        frame.loc[2, "low"] = 100.5
        order = self._order(
            OrderType.STOP_LIMIT,
            "LONG",
            created_index=1,
            stop_price=101.0,
            limit_price=100.8,
            stop_loss=99.0,
        )
        engine.pending_orders.append(order)

        # Bar 1: high 101.5 triggers AND low 100.5 <= 100.8 reaches the
        # limit — both legs fire on the creation bar -> guarded.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(100.8)

    def test_stop_limit_short_cannot_fill_on_creation_bar(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(4)
        # Only bar 2 dips through the sell stop (low 100.0 <= 100.6) while
        # its high still reaches the limit. Bar 2 opens at 102.0, above the
        # 100.6 stop, so the gap-through-open rule does not apply.
        frame.loc[2, "low"] = 100.0
        order = self._order(
            OrderType.STOP_LIMIT,
            "SHORT",
            created_index=1,
            stop_price=100.6,
            limit_price=101.0,
            stop_loss=102.0,
        )
        engine.pending_orders.append(order)

        # Bar 1: low 100.5 <= 100.6 triggers AND high 101.5 >= 101.0
        # reaches the limit — both legs fire on the creation bar -> guarded.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert engine.positions == []
        assert engine.pending_orders == [order]

        engine._fill_pending_orders(frame.iloc[2], 2)
        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.0)

    @pytest.mark.parametrize(
        "fixed_amount,expected",
        [(500.0, False), (None, True)],
        ids=["fixed_risk_amount", "percentage_sizing"],
    )
    def test_limit_fill_site_flag_follows_fill_time_sizing(
        self, tmp_path, fixed_amount, expected
    ):
        """The LIMIT/STOP fill-site re-size must refresh `_equity_proportional`
        (#288 / #170 review finding): the flag describes the branch that sized
        the FINAL quantity, not the creation-time estimate. The order is
        pre-seeded `_equity_proportional = False` (a value `_create_order`
        never writes for these params) so BOTH parametrizations fail if the
        fill-site update is dropped — without the seed the percentage case
        would pass on the `getattr(..., True)` default alone."""
        from runner.engine import OrderType

        params = {} if fixed_amount is None else {"fixed_risk_amount": fixed_amount}
        engine = self._engine(tmp_path, **params)
        frame = _linear_frame(4)
        order = self._order(
            OrderType.LIMIT,
            "LONG",
            created_index=0,
            limit_price=101.5,
            stop_loss=100.0,
        )
        order._equity_proportional = False
        engine.pending_orders.append(order)

        # created_index=0 -> bar 1 is already a post-creation bar.
        engine._fill_pending_orders(frame.iloc[1], 1)
        assert len(engine.positions) == 1
        assert (
            engine.positions[0].meta["equity_proportional_sizing"] is expected
        )


class TestPendingStopGapPricing:
    """#371: pending STOP/STOP_LIMIT fills honour gap-through-open pricing.

    A bar that opens through a stop fills at the open (the worse price for a
    stop-market entry), never at the trigger level. STOP_LIMIT applies the
    same open fill only when the bar opens through the stop AND the resulting
    limit is marketable at that open; otherwise the limit price stands.
    Plain LIMIT orders keep the conservative level fill (no favourable-gap
    price improvement).
    """

    @staticmethod
    def _engine(tmp_path: Path, **param_overrides) -> TradeEngine:
        return TradeEngine(
            _config(tmp_path, parameters=dict(param_overrides)), symbol="PROBE"
        )

    @staticmethod
    def _order(order_type, direction: str, created_index: int, **kwargs):
        from runner.engine import Order

        return Order(
            symbol="PROBE",
            direction=direction,
            order_type=order_type,
            created_index=created_index,
            created_time="2024-01-01T01:00:00+00:00",
            quantity=10.0,
            **kwargs,
        )

    @staticmethod
    def _fill(engine: TradeEngine, frame: pd.DataFrame, idx: int = 1) -> None:
        engine._fill_pending_orders(frame.iloc[idx], idx)

    def test_stop_long_gap_up_fills_at_open(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 105.0
        frame.loc[1, "high"] = 106.0
        frame.loc[1, "low"] = 104.0
        frame.loc[1, "close"] = 105.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "LONG",
                created_index=0,
                stop_price=101.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(105.0)

    def test_stop_short_gap_down_fills_at_open(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 95.0
        frame.loc[1, "high"] = 96.0
        frame.loc[1, "low"] = 94.0
        frame.loc[1, "close"] = 95.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                stop_loss=110.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(95.0)

    def test_stop_long_no_gap_fills_at_trigger(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 100.8
        frame.loc[1, "high"] = 101.5
        frame.loc[1, "low"] = 100.7
        frame.loc[1, "close"] = 101.2
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "LONG",
                created_index=0,
                stop_price=101.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.0)

    def test_stop_short_no_gap_fills_at_trigger(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 101.0
        frame.loc[1, "high"] = 101.5
        frame.loc[1, "low"] = 100.0
        frame.loc[1, "close"] = 100.5
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "SHORT",
                created_index=0,
                stop_price=100.6,
                stop_loss=102.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(100.6)

    def test_stop_limit_long_gap_with_marketable_limit_fills_at_open(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 102.0
        frame.loc[1, "high"] = 104.0
        frame.loc[1, "low"] = 101.5
        frame.loc[1, "close"] = 103.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "LONG",
                created_index=0,
                stop_price=101.0,
                limit_price=103.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(102.0)

    def test_stop_limit_long_gap_with_unmarketable_limit_fills_at_limit(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 104.0
        frame.loc[1, "high"] = 105.0
        frame.loc[1, "low"] = 102.5
        frame.loc[1, "close"] = 103.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "LONG",
                created_index=0,
                stop_price=101.0,
                limit_price=103.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(103.0)

    def test_stop_limit_short_gap_with_marketable_limit_fills_at_open(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 99.0
        frame.loc[1, "high"] = 99.5
        frame.loc[1, "low"] = 98.5
        frame.loc[1, "close"] = 99.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                limit_price=98.0,
                stop_loss=105.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(99.0)

    def test_stop_limit_short_gap_with_unmarketable_limit_fills_at_limit(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 97.5
        frame.loc[1, "high"] = 98.5
        frame.loc[1, "low"] = 97.0
        frame.loc[1, "close"] = 98.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                limit_price=98.0,
                stop_loss=105.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(98.0)

    def test_stop_limit_long_no_gap_fills_at_limit(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 100.5
        frame.loc[1, "high"] = 102.5
        frame.loc[1, "low"] = 100.8
        frame.loc[1, "close"] = 101.5
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "LONG",
                created_index=0,
                stop_price=101.0,
                limit_price=102.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(102.0)

    def test_stop_limit_short_no_gap_fills_at_limit(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 101.0
        frame.loc[1, "high"] = 101.5
        frame.loc[1, "low"] = 98.5
        frame.loc[1, "close"] = 100.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                limit_price=99.0,
                stop_loss=105.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(99.0)

    def test_limit_long_favorable_gap_stays_at_limit(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path)
        frame = _linear_frame(3)
        frame.loc[1, "open"] = 100.5
        frame.loc[1, "high"] = 101.0
        frame.loc[1, "low"] = 100.0
        frame.loc[1, "close"] = 100.8
        engine.pending_orders.append(
            self._order(
                OrderType.LIMIT,
                "LONG",
                created_index=0,
                limit_price=101.5,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.5)

    # ── Maintainer default 2026-11-23: a gap-through open fill is marketable,
    # so it pays the same spread a MARKET order at that open pays — a LONG
    # lifts the ask (open + spread), a SHORT hits the bid open. Intrabar
    # level fills and resting-limit fills carry no spread.

    @staticmethod
    def _spread_frame() -> pd.DataFrame:
        frame = _linear_frame(3)
        frame["spread_price"] = 0.5
        return frame

    def test_stop_long_gap_up_fills_at_open_plus_spread(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 105.0
        frame.loc[1, "high"] = 106.0
        frame.loc[1, "low"] = 104.0
        frame.loc[1, "close"] = 105.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "LONG",
                created_index=0,
                stop_price=101.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(105.5)

    def test_stop_long_intrabar_touch_keeps_stop_price_despite_spread(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 100.8
        frame.loc[1, "high"] = 101.5
        frame.loc[1, "low"] = 100.7
        frame.loc[1, "close"] = 101.2
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "LONG",
                created_index=0,
                stop_price=101.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.0)

    def test_stop_short_gap_down_fills_at_bid_open_despite_spread(self, tmp_path):
        """Sell-side symmetry: a MARKET SHORT also fills at the bid open."""
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 95.0
        frame.loc[1, "high"] = 96.0
        frame.loc[1, "low"] = 94.0
        frame.loc[1, "close"] = 95.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                stop_loss=110.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(95.0)

    def test_stop_limit_long_gap_marketable_fills_at_open_plus_spread(
        self, tmp_path
    ):
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 102.0
        frame.loc[1, "high"] = 104.0
        frame.loc[1, "low"] = 101.5
        frame.loc[1, "close"] = 103.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "LONG",
                created_index=0,
                stop_price=101.0,
                limit_price=103.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(102.5)

    def test_stop_limit_short_gap_marketable_fills_at_bid_open(self, tmp_path):
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 99.0
        frame.loc[1, "high"] = 99.5
        frame.loc[1, "low"] = 98.5
        frame.loc[1, "close"] = 99.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                limit_price=98.0,
                stop_loss=105.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(99.0)

    def test_stop_limit_long_open_equal_to_stop_fills_at_open_plus_spread(
        self, tmp_path
    ):
        """Boundary: ``open == stop`` is marketable at the open (``>=``), so
        the fill is the open — plus the ask spread for a LONG — not the
        limit. Mutating ``>=`` to ``>`` flips this to the limit price."""
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 101.0
        frame.loc[1, "high"] = 102.0
        frame.loc[1, "low"] = 100.5
        frame.loc[1, "close"] = 101.5
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "LONG",
                created_index=0,
                stop_price=101.0,
                limit_price=103.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(101.5)

    def test_stop_limit_short_open_equal_to_stop_fills_at_open(self, tmp_path):
        """SHORT boundary mirror: ``open == stop`` fills at the bid open."""
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 100.0
        frame.loc[1, "high"] = 100.5
        frame.loc[1, "low"] = 99.0
        frame.loc[1, "close"] = 99.5
        engine.pending_orders.append(
            self._order(
                OrderType.STOP_LIMIT,
                "SHORT",
                created_index=0,
                stop_price=100.0,
                limit_price=98.0,
                stop_loss=105.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        assert engine.positions[0].entry_price == pytest.approx(100.0)

    def test_gap_filled_stop_is_resized_from_the_spread_adjusted_open(
        self, tmp_path
    ):
        """Risk-based quantity must be computed from the actual gap fill —
        open + spread for a LONG — not the bare open or the stop level."""
        from runner.engine import OrderType

        engine = self._engine(tmp_path, spread_price_col="spread_price")
        frame = self._spread_frame()
        frame.loc[1, "open"] = 105.0
        frame.loc[1, "high"] = 106.0
        frame.loc[1, "low"] = 104.0
        frame.loc[1, "close"] = 105.0
        engine.pending_orders.append(
            self._order(
                OrderType.STOP,
                "LONG",
                created_index=0,
                stop_price=101.0,
                stop_loss=100.0,
            )
        )

        self._fill(engine, frame)

        assert len(engine.positions) == 1
        position = engine.positions[0]
        assert position.entry_price == pytest.approx(105.5)
        # 1% of 100k = 1000 risk over |105.5 - 100| = 5.5 per unit.
        assert position.quantity == pytest.approx(1000.0 / 5.5, rel=1e-6)


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

    def test_signal_value_never_overwrites_non_na_ohlcv_value(self, monkeypatch):
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
        original_extract = TradeEngine._extract_levels

        def spy(self, row):
            captured["row"] = row
            return original_extract(self, row)

        monkeypatch.setattr(TradeEngine, "_extract_levels", spy)
        engine = TradeEngine(_config(Path("/tmp")), symbol="PROBE")
        engine.run(frame, strategy)

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

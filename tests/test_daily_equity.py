"""Tests for runner.daily_equity module."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from runner.daily_equity import attach_daily_equity, build_daily_equity_curve
from runner.stats import compute_stats_for_result
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit


def make_result(
    trades=None,
    equity_curve=None,
    initial_capital=100000.0,
    backtest_start="2024-01-01",
    backtest_end="2024-02-01",
):
    """Helper to create a BacktestResult."""
    return BacktestResult(
        meta=BacktestMeta(
            id="test",
            created_at=datetime.now(timezone.utc).isoformat(),
            engine="vectorbt",
            strategies=["demo"],
            initial_capital=initial_capital,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
        ),
        trades=trades or [],
        equity_curve=equity_curve or [],
        stats={},
    )


def make_trade(
    entry_time="2024-01-15T10:00:00Z",
    exit_time="2024-01-15T15:00:00Z",
    pnl=500.0,
    symbol="AAPL",
    direction="LONG",
):
    """Helper to create a Trade."""
    return Trade(
        id=1,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=150.0,
        quantity=10.0,
        exits=[TradeExit(time=exit_time, price=155.0, quantity=10.0, pnl=pnl)],
        pnl=pnl,
    )


class TestBuildDailyEquityCurve:
    """Tests for build_daily_equity_curve function."""

    def test_empty_result(self):
        """Test with no trades and no equity curve."""
        result = make_result()
        points, returns = build_daily_equity_curve(result)
        assert points == []
        assert returns == []

    def test_no_trades_no_equity(self):
        """Test with no trades but existing equity curve."""
        result = make_result(
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=101000.0),
            ]
        )
        points, returns = build_daily_equity_curve(result)
        assert points == []
        assert returns == []

    def test_single_trade_single_day(self):
        """Test single trade within one day."""
        result = make_result(trades=[make_trade()])
        points, returns = build_daily_equity_curve(result)

        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert points[0].equity == 100000.0
        assert points[-1].equity == 100500.0
        assert len(returns) == 32
        january_return = next(item for item in returns if item["date"] == "2024-01-15")
        assert abs(january_return["return"] - 0.005) < 1e-10
        # Union (branch side): the trade's exit day is exactly one point of the
        # curve. The branch asserted a 2-point curve with points[1] on the trade
        # day; the resolved contract fills every calendar day of the backtest
        # window, so the same property is asserted against the trade-day point
        # rather than a positional index.
        assert len(points) == 33
        trade_day = [point for point in points if point.time.startswith("2024-01-15")]
        assert len(trade_day) == 1
        assert trade_day[0].equity == 100500.0

    def test_multiple_trades_same_day(self):
        """Test multiple trades aggregated to same day."""
        trade1 = make_trade(pnl=500.0)
        trade2 = make_trade(pnl=300.0, exit_time="2024-01-15T16:00:00Z")
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert points[0].equity == 100000.0
        assert points[-1].equity == 100800.0
        assert len(returns) == 32
        # Union (branch side): the two same-day trades aggregate into one
        # calendar point, so the curve is the filled window, not two points.
        assert len(points) == 33

    def test_trades_different_days(self):
        """Test trades on different days create separate equity points."""
        trade1 = make_trade(
            entry_time="2024-01-15T10:00:00Z",
            exit_time="2024-01-15T15:00:00Z",
            pnl=500.0,
        )
        trade2 = make_trade(
            entry_time="2024-01-16T10:00:00Z",
            exit_time="2024-01-16T15:00:00Z",
            pnl=300.0,
        )
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert len(returns) == 32
        assert points[0].equity == 100000.0
        assert points[-1].equity == 100800.0
        assert any(
            point.time.startswith("2024-01-15") and point.equity == 100500.0
            for point in points
        )
        assert any(
            point.time.startswith("2024-01-16") and point.equity == 100800.0
            for point in points
        )
        # Union (branch side): this subsumes the branch's positional
        # points[1]/points[2] checks — the filled curve carries each trade day's
        # equity on that day, which the two assertions above pin.
        assert len(points) == 33

    def test_trades_sorted_chronologically(self):
        """Test that output is sorted by date regardless of trade order."""
        trade1 = make_trade(
            entry_time="2024-01-16T10:00:00Z",
            exit_time="2024-01-16T15:00:00Z",
            pnl=300.0,
        )
        trade2 = make_trade(
            entry_time="2024-01-15T10:00:00Z",
            exit_time="2024-01-15T15:00:00Z",
            pnl=500.0,
        )
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert any(point.time.startswith("2024-01-15") for point in points)
        assert any(point.time.startswith("2024-01-16") for point in points)

    def test_skips_non_finite_pnl(self):
        """Test that non-finite PnL values are skipped."""
        trade1 = make_trade(pnl=500.0)
        trade2 = Trade(
            id=2, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[
                TradeExit(
                    time="2024-01-15T15:00:00Z",
                    price=155.0,
                    quantity=10.0,
                    pnl=float("nan"),
                )
            ],
            pnl=float("nan"),
        )
        trade3 = Trade(
            id=3, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[
                TradeExit(
                    time="2024-01-15T15:00:00Z",
                    price=155.0,
                    quantity=10.0,
                    pnl=float("inf"),
                )
            ],
            pnl=float("inf"),
        )
        result = make_result(trades=[trade1, trade2, trade3])
        points, returns = build_daily_equity_curve(result)

        assert points[0].equity == 100000.0
        assert points[-1].equity == 100500.0
        assert len(returns) == 32
        # Union (branch side): only the valid trade contributes, so the filled
        # window still starts flat and ends at its net PnL.
        assert len(points) == 33

    def test_skips_trades_without_valid_exit_time(self):
        """Test that trades with invalid exit times are skipped."""
        trade1 = make_trade()
        trade2 = Trade(
            id=2, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[TradeExit(time="invalid-time", price=155.0, quantity=10.0, pnl=300.0)],
            pnl=300.0,
        )
        trade3 = Trade(
            id=3, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[],
            pnl=200.0,
        )
        result = make_result(trades=[trade1, trade2, trade3])
        points, returns = build_daily_equity_curve(result)

        assert points[0].equity == 100000.0
        assert points[-1].equity == 100500.0
        assert len(returns) == 32
        # Union (branch side): only the valid trade contributes, so the filled
        # window still starts flat and ends at its net PnL.
        assert len(points) == 33

    def test_daily_return_calculation(self):
        """Test daily return is calculated correctly."""
        trade1 = make_trade(pnl=5000.0)  # 5% return
        result = make_result(trades=[trade1])
        points, returns = build_daily_equity_curve(result)

        assert len(returns) == 32
        january_return = next(item for item in returns if item["date"] == "2024-01-15")
        assert abs(january_return["return"] - 0.05) < 1e-10
        assert january_return["equity"] == 105000.0


    def test_daily_equity_uses_net_pnl_after_fees(self):
        trade = make_trade(pnl=500.0)
        trade.fees = 25.0
        trade.net_pnl = 475.0

        points, returns = build_daily_equity_curve(make_result(trades=[trade]))

        assert points[-1].equity == pytest.approx(100475.0)
        # Union (branch side): this used to read `returns[-1]`, which under the
        # resolved calendar-filled contract is the last day of the backtest
        # window (a zero-return day), not the trade day. The 0.00475 return
        # belongs to 2024-01-15.
        trade_day_return = next(item for item in returns if item["date"] == "2024-01-15")
        assert trade_day_return["return"] == pytest.approx(0.00475)
        assert trade_day_return["equity"] == pytest.approx(100475.0)

    def test_first_trade_loss_is_included_in_return_and_drawdown(self):
        trade = make_trade(pnl=-1100.0)
        trade.net_pnl = -1100.0
        result = make_result(
            trades=[trade],
            equity_curve=[
                EquityPoint(time="2024-01-15T15:00:00Z", equity=98_900.0)
            ],
            backtest_start="2024-01-15",
            backtest_end="2024-01-16",
        )

        attach_daily_equity(result)
        stats = compute_stats_for_result(result)

        assert result.equity_curve[0].equity == pytest.approx(100_000.0)
        assert result.equity_curve[-1].equity == pytest.approx(98_900.0)
        assert stats["total_return"] == pytest.approx(-0.011)
        assert stats["max_drawdown_pct"] == pytest.approx(-0.011)


class TestAttachDailyEquity:
    """Tests for attach_daily_equity function."""

    def test_no_trades_no_equity_curve(self):
        """Test with no trades and no equity curve."""
        result = make_result()
        attach_daily_equity(result)
        assert result.meta.data["equity_frequency"] == "unknown"
        assert result.stats["daily_return_count"] == 0

    def test_trades_but_no_existing_equity_curve(self):
        """Test building daily equity from trades when none exists."""
        result = make_result(trades=[make_trade(pnl=500.0)])
        attach_daily_equity(result)

        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 32
        assert len(result.equity_curve) == 33
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 100500.0

    def test_aggregates_reconciling_engine_curve_to_daily_points(self):
        """An engine curve that reconciles with the trades is kept, compacted
        to one point per CALENDAR day.

        The old literal for this test was 4 points / 3 returns — one point per
        *observed* day. That premise is the defect fix (a) closes: the engine
        curve observes 2024-01-10, 01-20 and 02-01, so the +0.1% move into
        01-10 was booked as a single day even though it covers nine calendar
        days of holding, and `daily_return_count` described a 3-day series for a
        32-day window. Every skipped day is now an explicit flat point, exactly
        as `build_daily_equity_curve` already reported it, so the same window
        yields 32 points and 31 returns while the engine's own equity levels
        (100100.0 on 01-10, 100300.0 on 01-20) are still the ones published.
        """
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-10T00:00:00+00:00", equity=100100.0),
                EquityPoint(time="2024-01-20T00:00:00+00:00", equity=100300.0),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=100500.0),
            ]
        )
        attach_daily_equity(result)

        # One point per calendar day of 2024-01-01..2024-02-01, inclusive.
        assert len(result.equity_curve) == 32
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 100500.0
        # The engine's own observed levels survive the compaction.
        assert result.equity_curve[9].equity == 100100.0
        assert result.equity_curve[19].equity == 100300.0
        # A day the engine did not observe is flat, not a silent omission.
        assert result.equity_curve[15].equity == 100100.0
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 31
        # The +0.1% move is attributed to the day it happened, and the days it
        # did not happen are zero, so the series is not annualised as if the
        # whole nine-day move were one day.
        returns_by_day = {
            item["date"]: item["return"] for item in result.stats["daily_returns"]
        }
        assert returns_by_day["2024-01-10"] == pytest.approx(0.001)
        assert returns_by_day["2024-01-09"] == pytest.approx(0.0)
        assert returns_by_day["2024-01-11"] == pytest.approx(0.0)

    def test_open_position_drawdown_is_kept_from_engine_curve(self):
        """The engine's mark-to-market dip survives, on a calendar-day series.

        The old literal asserted a bare ``[100000.0, 99000.0, 100500.0]`` and
        ``daily_return_count == 2``: the -1% open-position drawdown on 01-05 was
        carried by ONE return that actually spans four calendar days, so the
        daily series described a 2-day window. Fix (a) fills the skipped days
        flat, which keeps the drawdown (the point of this test) and makes the
        daily series describe the window it claims to.
        """
        result = make_result(
            trades=[
                make_trade(
                    entry_time="2024-01-10T10:00:00Z",
                    exit_time="2024-01-10T15:00:00Z",
                    pnl=500.0,
                )
            ],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-05T00:00:00+00:00", equity=99000.0),
                EquityPoint(time="2024-01-10T00:00:00+00:00", equity=100500.0),
            ],
        )

        attach_daily_equity(result)
        stats = compute_stats_for_result(result)

        assert [point.equity for point in result.equity_curve] == [
            100000.0,  # 01-01 opening point
            100000.0,  # 01-02 flat
            100000.0,  # 01-03 flat
            100000.0,  # 01-04 flat
            99000.0,   # 01-05 the engine's mark-to-market drawdown
            99000.0,   # 01-06 flat
            99000.0,   # 01-07 flat
            99000.0,   # 01-08 flat
            99000.0,   # 01-09 flat
            100500.0,  # 01-10 the trade's exit
        ]
        assert result.stats["daily_return_count"] == 9
        assert stats["total_return"] == pytest.approx(0.005)
        assert stats["max_drawdown_pct"] == pytest.approx(-0.01)

    def test_rejects_engine_curve_that_does_not_reconcile_with_trades(self):
        """A curve whose final equity contradicts the trades is thrown away and
        rebuilt from realized PnL over the full backtest window."""
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-20T00:00:00+00:00", equity=105000.0),
            ],
        )

        attach_daily_equity(result)

        # Union (branch side): this used to assert a two-point curve and a single
        # daily return. The resolved contract fills the whole 2024-01-01..2024-02-01
        # window, so the same intent (the bogus 105000.0 point is gone and the
        # curve ends at the trade's net PnL) is asserted over the filled curve.
        equities = [point.equity for point in result.equity_curve]
        assert len(result.equity_curve) == 33
        assert equities[0] == 100000.0
        assert equities[-1] == 100500.0
        assert set(equities) == {100000.0, 100500.0}
        assert result.stats["daily_return_count"] == 32

    def test_replaces_equity_curve_when_daily_has_more_points(self):
        """The daily curve must replace a two-point endpoint-only engine curve.

        Merge resolution: this is the same fixture and the same intent as
        `test_rejects_endpoint_only_curve_with_matching_endpoints` on the other
        side of the merge, which already pinned the calendar-filled answer (33
        points). The branch's literal (4 points) described the older
        event-day-only curve. Both are kept; both now assert the filled curve.

        NOTE for the source resolution: this fixture's engine curve *reconciles*
        with the trades (101000.0 == 100000 + 500 + 300 + 200), so any fast path
        that adopts a reconciling engine curve before the density check in
        `_preserves_engine_curve` will hand back the 2-point curve and fail both
        tests. That is the guard working as intended: an endpoint-only curve must
        not be preserved.
        """
        trade1 = make_trade(
            entry_time="2024-01-10T10:00:00Z", exit_time="2024-01-10T15:00:00Z", pnl=500.0
        )
        trade2 = make_trade(
            entry_time="2024-01-15T10:00:00Z", exit_time="2024-01-15T15:00:00Z", pnl=300.0
        )
        trade3 = make_trade(
            entry_time="2024-01-20T10:00:00Z", exit_time="2024-01-20T15:00:00Z", pnl=200.0
        )
        result = make_result(
            trades=[trade1, trade2, trade3],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=101000.0),
            ]
        )
        attach_daily_equity(result)

        assert len(result.equity_curve) == 33
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 101000.0
        # Each trade day carries that day's cumulative net PnL.
        assert result.equity_curve[10].equity == 100500.0
        assert result.equity_curve[15].equity == 100800.0
        assert result.equity_curve[20].equity == 101000.0
        assert result.stats["daily_return_count"] == 32

    def test_does_not_preserve_single_point_engine_curve(self):
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0)],
        )

        attach_daily_equity(result)

        assert len(result.equity_curve) > 1
        assert result.equity_curve[-1].equity == pytest.approx(100500.0)
        assert result.meta.data["equity_frequency"] == "daily"

    def test_does_not_preserve_stale_engine_curve(self):
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=100500.0),
            ],
        )

        attach_daily_equity(result)

        assert result.equity_curve[-1].equity == pytest.approx(100500.0)
        assert len(result.equity_curve) > 2
        assert result.meta.data["equity_frequency"] == "daily"

    @pytest.mark.parametrize(
        "preserved_curve",
        [
            [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0)],
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=100500.0),
            ],
        ],
    )
    def test_daily_equity_rejects_single_point_and_stale_preservation(
        self, preserved_curve
    ):
        from runner.daily_equity import _preserves_engine_curve

        daily_timestamps = pd.date_range("2024-01-01", periods=32, freq="D", tz="UTC")
        daily_curve = [
            EquityPoint(
                time=timestamp.isoformat(),
                equity=100500.0 if timestamp == daily_timestamps[-1] else 100000.0,
            )
            for timestamp in daily_timestamps
        ]

        assert not _preserves_engine_curve(preserved_curve, daily_curve)

    def test_rejects_sparse_existing_equity_curve(self):
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-10T00:00:00+00:00", equity=101000.0),
                EquityPoint(time="2024-01-20T00:00:00+00:00", equity=102000.0),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=105000.0),
            ]
        )
        attach_daily_equity(result)

        assert len(result.equity_curve) == 33
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 100500.0
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 32

    def test_rejects_endpoint_only_curve_with_matching_endpoints(self):
        trade1 = make_trade(
            entry_time="2024-01-10T10:00:00Z", exit_time="2024-01-10T15:00:00Z", pnl=500.0
        )
        trade2 = make_trade(
            entry_time="2024-01-15T10:00:00Z", exit_time="2024-01-15T15:00:00Z", pnl=300.0
        )
        trade3 = make_trade(
            entry_time="2024-01-20T10:00:00Z", exit_time="2024-01-20T15:00:00Z", pnl=200.0
        )
        result = make_result(
            trades=[trade1, trade2, trade3],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=101000.0),
            ]
        )
        attach_daily_equity(result)

        assert len(result.equity_curve) == 33
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 101000.0
        assert result.meta.data["equity_frequency"] == "daily"

    def test_preserves_dense_business_day_curve_within_daily_boundaries(self):
        timestamps = pd.date_range("2024-01-02", "2024-01-31", freq="B", tz="UTC")
        curve = [
            EquityPoint(
                time=timestamp.isoformat(),
                equity=100500.0 if timestamp == timestamps[-1] else 100000.0,
            )
            for timestamp in timestamps
        ]
        result = make_result(trades=[make_trade()], equity_curve=curve)

        attach_daily_equity(result)

        assert result.equity_curve == curve
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 32

    def test_preserves_dense_curve_across_nyse_holiday(self):
        timestamps = pd.date_range("2024-12-20", "2025-01-05", freq="D", tz="UTC")
        curve = [
            EquityPoint(time=timestamp.isoformat(), equity=100000.0)
            for timestamp in timestamps
            if timestamp.date() != pd.Timestamp("2024-12-25").date()
        ]
        trade = make_trade(
            entry_time="2024-12-20T10:00:00Z",
            exit_time="2024-12-20T15:00:00Z",
            pnl=0.0,
        )
        result = make_result(
            trades=[trade],
            equity_curve=curve,
            backtest_start="2024-12-20",
            backtest_end="2025-01-05",
        )

        attach_daily_equity(result)

        assert result.equity_curve == curve

    def test_start_end_params_accepted(self):
        """Test that start and end parameters are accepted."""
        result = make_result(trades=[make_trade()])
        # Should not raise
        attach_daily_equity(result, start="2024-01-01", end="2024-02-01")
        assert result.meta.data["equity_frequency"] == "daily"

    def test_window_scopes_preserved_engine_curve(self):
        """Regression (pre-mortem 16-B #7): with start/end active, a preserved
        engine-provided curve must be scoped to the same window as the daily
        returns — otherwise the curve and stats describe different periods."""
        result = make_result(
            trades=[
                make_trade(
                    entry_time="2024-01-10T10:00:00Z",
                    exit_time="2024-01-10T15:00:00Z",
                    pnl=500.0,
                ),
                make_trade(
                    entry_time="2024-02-10T10:00:00Z",
                    exit_time="2024-02-10T15:00:00Z",
                    pnl=700.0,
                ),
            ],
            # Engine-provided per-bar curve spanning two months
            equity_curve=[
                EquityPoint(time=f"2024-01-{day:02d}T00:00:00+00:00", equity=100000.0 + day)
                for day in range(1, 32)
            ]
            + [
                EquityPoint(time=f"2024-02-{day:02d}T00:00:00+00:00", equity=101000.0 + day)
                for day in range(1, 30)
            ],
        )
        attach_daily_equity(result, start="2024-01-01", end="2024-01-31")

        # The preserved curve covers January only — never February.
        assert result.equity_curve
        assert all(point.time < "2024-02-01" for point in result.equity_curve)
        # And the daily returns describe the same window as the curve.
        assert result.stats["daily_return_count"] == 31
        assert result.meta.data["daily_return_count"] == 31

    def test_window_excludes_out_of_window_trades_from_daily_returns(self):
        """Trades outside the window must not feed the daily aggregation."""
        result = make_result(
            trades=[
                make_trade(
                    entry_time="2024-01-10T10:00:00Z",
                    exit_time="2024-01-10T15:00:00Z",
                    pnl=500.0,
                ),
                make_trade(
                    entry_time="2024-02-10T10:00:00Z",
                    exit_time="2024-02-10T15:00:00Z",
                    pnl=700.0,
                ),
            ],
        )
        attach_daily_equity(result, start="2024-01-01", end="2024-01-31")

        assert result.stats["daily_return_count"] == 31
        january_return = next(
            item for item in result.stats["daily_returns"] if item["date"] == "2024-01-10"
        )
        assert january_return["equity"] == pytest.approx(100500.0)
        assert result.equity_curve[-1].equity == pytest.approx(100500.0)

    def test_empty_trades_with_existing_equity_curve(self):
        """Test with no trades but existing multi-point equity curve."""
        result = make_result(
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=101000.0),
            ]
        )
        attach_daily_equity(result)

        # Should mark as daily since curve exists
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 0


class TestEngineCurveGapFill:
    """Fix (a): a multi-day move must never be booked as a single day.

    The engine-curve path used to compact the curve to ``last_by_day`` and
    derive returns from consecutive *observed* days. The calendar fill it
    replaces emits every day of the window, so the engine path silently had a
    different — and gap-dependent — daily series.
    """

    @staticmethod
    def _result(curve, *, pnl=1000.0, exit_time="2024-01-09T15:00:00Z"):
        return make_result(
            trades=[
                make_trade(
                    entry_time="2024-01-02T10:00:00Z",
                    exit_time=exit_time,
                    pnl=pnl,
                )
            ],
            equity_curve=curve,
            backtest_start="2024-01-01",
            backtest_end="2024-01-10",
        )

    def test_gap_days_are_emitted_as_flat_points(self):
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-05T00:00:00+00:00", equity=103000.0),
                EquityPoint(time="2024-01-09T00:00:00+00:00", equity=101000.0),
            ]
        )

        points, returns = _daily_points_from_engine_curve(
            result, start=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )

        # 2024-01-01 opening point plus one point per calendar day through the
        # last observed day: 10 points, 9 daily returns, one per elapsed day.
        assert [point.time[:10] for point in points] == [
            f"2024-01-{day:02d}" for day in range(1, 10)
        ]
        # The engine's own levels are still the ones published.
        assert points[4].equity == 103000.0
        assert points[8].equity == 101000.0
        # Every skipped day is an explicit zero return, so the series describes
        # the window instead of collapsing 8 calendar days into 2 returns.
        assert [item["return"] for item in returns] == [
            0.0,
            0.0,
            0.0,
            pytest.approx(0.03),
            0.0,
            0.0,
            0.0,
            pytest.approx(-2000.0 / 103000.0),
        ]
        # One return per distinct calendar day, and the days are contiguous.
        assert [item["date"] for item in returns] == [
            f"2024-01-{day:02d}" for day in range(2, 10)
        ]

    def test_multi_day_move_is_not_annualised_as_one_day(self):
        """The reported daily statistics must not scale with the skipped days.

        The defect: a curve observing only 01-05, 01-08 and 01-09 produced the
        returns ``[+3.00%, -1.94%]`` and a ``mean x 252`` annualisation of
        ~133%, where the mean over the eight calendar days those two moves
        actually span gives ~33%. The error was ~2x here and grew with the gap,
        so the same backtest scored differently depending on how many days the
        engine curve happened to skip.
        """
        from runner.daily_equity import _daily_points_from_engine_curve
        from runner.stats import _sharpe_manual

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-05T00:00:00+00:00", equity=103000.0),
                EquityPoint(time="2024-01-09T00:00:00+00:00", equity=101000.0),
            ]
        )

        _points, returns = _daily_points_from_engine_curve(
            result, start=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        observed = [item["return"] for item in returns]
        # The series the old compaction produced: one return per OBSERVED day.
        collapsed = [0.03, -2000.0 / 103000.0]

        def _annualised_mean(series):
            return sum(series) / len(series) * 252

        assert len(observed) == 8, "one return per calendar day, not per observation"
        assert observed != collapsed
        assert _annualised_mean(observed) == pytest.approx(0.3333, abs=1e-3)
        assert _annualised_mean(collapsed) == pytest.approx(1.3334, abs=1e-3)
        # A real Sharpe ratio, not only a mean, moves by the same factor: the
        # gap-collapsed series is a *different strategy*, not a coarser view of
        # the same one.
        assert _sharpe_manual(observed, 0.0) < _sharpe_manual(collapsed, 0.0)

    def test_gap_before_the_first_observation_is_filled(self):
        """The flat period before the engine's first point is a gap too."""
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-04T00:00:00+00:00", equity=100200.0),
                EquityPoint(time="2024-01-06T00:00:00+00:00", equity=100500.0),
            ],
            pnl=500.0,
            exit_time="2024-01-06T15:00:00Z",
        )

        points, returns = _daily_points_from_engine_curve(
            result, start=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )

        assert [point.time[:10] for point in points] == [
            f"2024-01-{day:02d}" for day in range(1, 7)
        ]
        assert [item["return"] for item in returns] == [
            0.0,
            0.0,
            pytest.approx(0.002),
            0.0,
            pytest.approx(300.0 / 100200.0),
        ]

    def test_dense_engine_curve_is_not_gap_filled(self):
        """Contiguous days gain nothing from the filler: no point is invented."""
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time=f"2024-01-{day:02d}T00:00:00+00:00", equity=100000.0)
                for day in range(1, 5)
            ]
            + [EquityPoint(time="2024-01-05T00:00:00+00:00", equity=100500.0)]
            + [
                EquityPoint(time=f"2024-01-{day:02d}T00:00:00+00:00", equity=100500.0)
                for day in range(6, 10)
            ],
            pnl=500.0,
            exit_time="2024-01-05T15:00:00Z",
        )

        points, _returns = _daily_points_from_engine_curve(
            result, start=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )

        # 2024-01-01 is consumed as the opening point, then 01-02..01-09.
        assert [point.time[:10] for point in points] == [
            f"2024-01-{day:02d}" for day in range(1, 10)
        ]

    def test_endpoint_only_curve_is_still_rejected(self):
        """The gap fill must not resurrect the endpoint-only curve.

        ``MIN_RECONCILING_ENGINE_CURVE_POINTS`` guards a different failure: a
        2-point curve describes the same total return the calendar fill already
        describes, so adopting it would publish a curve with no path at all.
        Filling gaps must not become a way around that gate.
        """
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-09T00:00:00+00:00", equity=101000.0),
            ]
        )

        assert _daily_points_from_engine_curve(result) is None


class TestReconciliationTolerance:
    """Fix (b): the gate is derived from the arithmetic, not a round number."""

    @staticmethod
    def _many_trades(count: int, *, pnl: float = 100.0):
        trades = []
        for index in range(count):
            day = (index % 27) + 2
            trade = make_trade(
                entry_time=f"2024-01-{day:02d}T10:00:00Z",
                exit_time=f"2024-01-{day:02d}T15:00:00Z",
                pnl=pnl,
            )
            trade.net_pnl = pnl
            trades.append(trade)
        return trades

    @staticmethod
    def _curve(trades, final_equity: float):
        # Three points: an endpoint-only curve is rejected before the
        # reconciliation is even reached, so the fixture has to carry a path.
        return [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
            EquityPoint(
                time="2024-01-15T00:00:00+00:00", equity=(100000.0 + final_equity) / 2
            ),
            EquityPoint(time="2024-01-28T00:00:00+00:00", equity=final_equity),
        ]

    def test_cent_level_reconciliation_drift_is_accepted(self):
        """Per-trade cent rounding must not discard the engine's curve.

        The gate used to be ``max(0.05, |initial| * 1e-9)`` — a flat $0.05 on a
        $100k account. Twenty trades whose stored ``net_pnl`` sit six tenths of
        a cent away from what the engine's own equity path accumulated drift by
        $0.12, which the old gate rejected: the mark-to-market path the function
        exists to preserve was silently thrown away for a rounding artefact.
        """
        from runner.daily_equity import (
            _daily_points_from_engine_curve,
            _reconciliation_tolerance,
        )

        count = 20
        trades = self._many_trades(count)
        drift = count * 0.006
        final = 100000.0 + count * 100.0 - drift
        result = make_result(
            trades=trades, equity_curve=self._curve(trades, final)
        )

        assert drift > 0.05, "the fixture must exceed the old flat five-cent gate"
        assert drift < _reconciliation_tolerance(
            expected_final=100000.0 + count * 100.0, trade_count=count
        )

        reconciled = _daily_points_from_engine_curve(result)

        assert reconciled is not None, (
            "a $0.12 reconciliation drift over 20 trades is cent-level "
            "rounding, not a contradiction; the curve must be preserved"
        )
        assert reconciled[0][-1].equity == pytest.approx(final)

    def test_genuine_contradiction_is_still_rejected(self):
        """The widened bound must not admit a curve the trades cannot reach."""
        from runner.daily_equity import (
            _daily_points_from_engine_curve,
            _reconciliation_tolerance,
        )

        count = 20
        trades = self._many_trades(count)
        expected = 100000.0 + count * 100.0
        # $4500 above what 20 x $100 of net PnL can produce.
        contradiction = expected + 4500.0
        assert (
            abs(contradiction - expected)
            > 20 * _reconciliation_tolerance(
                expected_final=expected, trade_count=count
            )
        )
        result = make_result(
            trades=trades, equity_curve=self._curve(trades, contradiction)
        )

        assert _daily_points_from_engine_curve(result) is None

    def test_tolerance_is_cent_floor_plus_one_cent_per_trade(self):
        """The bound is ``max(cent floor, float64 drift) + n * one cent``."""
        from runner.daily_equity import (
            EQUITY_CENT,
            _reconciliation_tolerance,
        )

        # One trade: a whole cent of quantisation plus a whole cent of drift.
        assert _reconciliation_tolerance(
            expected_final=100000.0, trade_count=1
        ) == pytest.approx(2 * EQUITY_CENT)
        # A hundred trades: the per-trade term dominates at a realistic account
        # size, and it is derived from the rounding, not from a round number.
        hundred = _reconciliation_tolerance(
            expected_final=100000.0, trade_count=100
        )
        assert hundred == pytest.approx(101 * EQUITY_CENT)
        assert hundred == pytest.approx(1.01)
        assert hundred > 0.05
        # Monotone in the trade count, and independent of the account size in
        # the range where cent quantisation is the binding limit.
        assert hundred > _reconciliation_tolerance(
            expected_final=100000.0, trade_count=10
        )
        assert _reconciliation_tolerance(
            expected_final=1e9, trade_count=10
        ) == pytest.approx(
            _reconciliation_tolerance(expected_final=1e5, trade_count=10)
        )

    def test_tolerance_carries_the_relative_float64_term_for_absurd_accounts(self):
        """At $1e15 the float64 accumulation term, not the cent floor, binds.

        That is what the relative epsilon is for: cent quantisation does not
        grow with the account, but float64 summation error does.
        """
        from runner.daily_equity import (
            EQUITY_CENT,
            _reconciliation_tolerance,
        )

        tolerance = _reconciliation_tolerance(
            expected_final=1e15, trade_count=1_000_000
        )

        # 8 * 1e6 * 2**-52 * 1e15 = ~1.776, far above the cent floor.
        assert tolerance == pytest.approx(
            8.0 * 1e6 * 2.0**-52 * 1e15 + 1e6 * EQUITY_CENT
        )
        assert tolerance > EQUITY_CENT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

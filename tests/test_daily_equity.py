"""Tests for runner.daily_equity module."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from runner.daily_equity import build_daily_equity_curve, attach_daily_equity
from runner.stats import compute_stats_for_result
from shared.models import BacktestResult, BacktestMeta, EquityPoint, Trade, TradeExit


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

        assert len(points) == 2
        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert points[0].equity == 100000.0
        assert points[1].time == "2024-01-15T00:00:00+00:00"
        assert points[1].equity == 100500.0
        assert len(returns) == 1
        assert returns[0]["date"] == "2024-01-15"
        assert abs(returns[0]["return"] - 0.005) < 1e-10

    def test_multiple_trades_same_day(self):
        """Test multiple trades aggregated to same day."""
        trade1 = make_trade(pnl=500.0)
        trade2 = make_trade(pnl=300.0, exit_time="2024-01-15T16:00:00Z")
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert len(points) == 2
        assert points[0].equity == 100000.0
        assert points[1].equity == 100800.0

    def test_trades_different_days(self):
        """Test trades on different days create separate equity points."""
        trade1 = make_trade(entry_time="2024-01-15T10:00:00Z", exit_time="2024-01-15T15:00:00Z", pnl=500.0)
        trade2 = make_trade(entry_time="2024-01-16T10:00:00Z", exit_time="2024-01-16T15:00:00Z", pnl=300.0)
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert len(points) == 3
        assert points[0].equity == 100000.0
        assert points[1].equity == 100500.0
        assert points[2].equity == 100800.0
        assert points[1].time == "2024-01-15T00:00:00+00:00"
        assert points[2].time == "2024-01-16T00:00:00+00:00"

    def test_trades_sorted_chronologically(self):
        """Test that output is sorted by date regardless of trade order."""
        trade1 = make_trade(entry_time="2024-01-16T10:00:00Z", exit_time="2024-01-16T15:00:00Z", pnl=300.0)
        trade2 = make_trade(entry_time="2024-01-15T10:00:00Z", exit_time="2024-01-15T15:00:00Z", pnl=500.0)
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert points[1].time == "2024-01-15T00:00:00+00:00"
        assert points[2].time == "2024-01-16T00:00:00+00:00"

    def test_skips_non_finite_pnl(self):
        """Test that non-finite PnL values are skipped."""
        import math
        trade1 = make_trade(pnl=500.0)
        trade2 = Trade(
            id=2, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[TradeExit(time="2024-01-15T15:00:00Z", price=155.0, quantity=10.0, pnl=float("nan"))],
            pnl=float("nan"),
        )
        trade3 = Trade(
            id=3, symbol="AAPL", direction="LONG",
            entry_time="2024-01-15T10:00:00Z", entry_price=150.0, quantity=10.0,
            exits=[TradeExit(time="2024-01-15T15:00:00Z", price=155.0, quantity=10.0, pnl=float("inf"))],
            pnl=float("inf"),
        )
        result = make_result(trades=[trade1, trade2, trade3])
        points, returns = build_daily_equity_curve(result)

        assert len(points) == 2
        assert points[0].equity == 100000.0
        assert points[1].equity == 100500.0

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

        assert len(points) == 2
        assert points[0].equity == 100000.0
        assert points[1].equity == 100500.0

    def test_daily_return_calculation(self):
        """Test daily return is calculated correctly."""
        trade1 = make_trade(pnl=5000.0)  # 5% return
        result = make_result(trades=[trade1])
        points, returns = build_daily_equity_curve(result)

        assert len(returns) == 1
        assert abs(returns[0]["return"] - 0.05) < 1e-10
        assert returns[0]["equity"] == 105000.0

    def test_daily_equity_uses_net_pnl_after_fees(self):
        trade = make_trade(pnl=500.0)
        trade.fees = 25.0
        trade.net_pnl = 475.0

        points, returns = build_daily_equity_curve(make_result(trades=[trade]))

        assert points[-1].equity == pytest.approx(100475.0)
        assert returns[-1]["return"] == pytest.approx(0.00475)

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
        assert result.stats["daily_return_count"] == 1
        assert len(result.equity_curve) == 2
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[1].equity == 100500.0

    def test_aggregates_reconciling_engine_curve_to_daily_points(self):
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

        assert len(result.equity_curve) == 4
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[-1].equity == 100500.0
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 3

    def test_open_position_drawdown_is_kept_from_engine_curve(self):
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

        assert [point.equity for point in result.equity_curve] == [100000.0, 99000.0, 100500.0]
        assert result.stats["daily_return_count"] == 2
        assert stats["total_return"] == pytest.approx(0.005)
        assert stats["max_drawdown_pct"] == pytest.approx(-0.01)

    def test_rejects_engine_curve_that_does_not_reconcile_with_trades(self):
        result = make_result(
            trades=[make_trade(pnl=500.0)],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-20T00:00:00+00:00", equity=105000.0),
            ],
        )

        attach_daily_equity(result)

        assert [point.equity for point in result.equity_curve] == [100000.0, 100500.0]
        assert result.stats["daily_return_count"] == 1

    def test_replaces_equity_curve_when_daily_has_more_points(self):
        """Test that daily curve replaces when it has more points."""
        # Build a case where daily curve would have multiple points
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
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=105000.0),
            ]
        )
        attach_daily_equity(result)

        assert len(result.equity_curve) == 4
        assert result.equity_curve[0].equity == 100000.0
        assert result.equity_curve[1].equity == 100500.0
        assert result.equity_curve[2].equity == 100800.0
        assert result.equity_curve[3].equity == 101000.0

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
        assert result.stats["daily_return_count"] == 1
        assert result.meta.data["daily_return_count"] == 1

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

        assert result.stats["daily_return_count"] == 1
        (only_return,) = result.stats["daily_returns"]
        assert only_return["date"] == "2024-01-10"
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
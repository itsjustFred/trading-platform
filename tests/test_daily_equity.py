"""Tests for runner.daily_equity module."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import runner.daily_equity as daily_equity_module
from runner.daily_equity import (
    _net_pnl_before,
    _trade_net_pnl,
    _window_starting_equity,
    attach_daily_equity,
    build_daily_equity_curve,
)
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

    def test_multiple_trades_same_day(self):
        """Test multiple trades aggregated to same day."""
        trade1 = make_trade(pnl=500.0)
        trade2 = make_trade(pnl=300.0, exit_time="2024-01-15T16:00:00Z")
        result = make_result(trades=[trade1, trade2])
        points, returns = build_daily_equity_curve(result)

        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert points[-1].equity == 100800.0
        assert len(returns) == 32

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

        assert points[-1].equity == 100500.0
        assert len(returns) == 32

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

        assert points[-1].equity == 100500.0
        assert len(returns) == 32

    def test_daily_return_calculation(self):
        """Test daily return is calculated correctly."""
        trade1 = make_trade(pnl=5000.0)  # 5% return
        result = make_result(trades=[trade1])
        points, returns = build_daily_equity_curve(result)

        assert len(returns) == 32
        january_return = next(item for item in returns if item["date"] == "2024-01-15")
        assert abs(january_return["return"] - 0.05) < 1e-10
        assert january_return["equity"] == 105000.0

    def test_curve_starts_at_inception_before_first_loss(self):
        """The curve must open at the starting equity, not at the first loss."""
        result = make_result(
            trades=[
                make_trade(exit_time="2024-01-15T15:00:00Z", pnl=-100.0),
                make_trade(exit_time="2024-01-16T15:00:00Z", pnl=200.0),
            ]
        )

        points, returns = build_daily_equity_curve(result)

        assert points[0].time == "2024-01-01T00:00:00+00:00"
        assert points[0].equity == 100_000.0
        by_date = {daily["date"]: daily for daily in returns}
        assert by_date["2024-01-15"]["equity"] == 99_900.0
        assert by_date["2024-01-16"]["equity"] == 100_100.0
        assert by_date["2024-01-15"]["return"] == pytest.approx(-0.001)
        assert by_date["2024-01-16"]["return"] == pytest.approx(200 / 99_900)

    def test_exit_day_uses_utc_date(self):
        """A non-UTC exit belongs to its UTC day, not its local day."""
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T23:30:00-05:00", pnl=500.0)]
        )

        points, returns = build_daily_equity_curve(result)

        by_date = {daily["date"]: daily for daily in returns}
        assert by_date["2024-01-15"]["equity"] == 100_000.0
        assert by_date["2024-01-16"]["equity"] == 100_500.0
        assert by_date["2024-01-16"]["return"] == pytest.approx(0.005)

    def test_naive_exit_time_is_treated_as_utc(self, monkeypatch):
        """A naive timestamp must not be interpreted in the host timezone."""

        class NonUtcDateTime(datetime):
            @classmethod
            def fromisoformat(cls, value):
                parsed = datetime.fromisoformat(value)
                return cls(
                    parsed.year,
                    parsed.month,
                    parsed.day,
                    parsed.hour,
                    parsed.minute,
                    parsed.second,
                    parsed.microsecond,
                    parsed.tzinfo,
                )

            def astimezone(self, tz=None):
                if self.tzinfo is None:
                    raise AssertionError("naive timestamps must not use the host timezone")
                return super().astimezone(tz)

        monkeypatch.setattr(daily_equity_module, "datetime", NonUtcDateTime)
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T23:30:00", pnl=500.0)]
        )

        points, returns = build_daily_equity_curve(result)

        by_date = {daily["date"]: daily for daily in returns}
        assert by_date["2024-01-15"]["equity"] == 100_500.0
        assert by_date["2024-01-15"]["return"] == pytest.approx(0.005)

    @pytest.mark.parametrize("initial_capital", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_nonfinite_initial_capital(self, initial_capital):
        result = make_result(initial_capital=initial_capital)

        with pytest.raises(ValueError, match="initial_capital must be finite"):
            build_daily_equity_curve(result)

    def test_rejects_nonfinite_starting_equity(self):
        result = make_result(trades=[make_trade()])

        with pytest.raises(ValueError, match="starting equity must be finite"):
            build_daily_equity_curve(result, starting_equity=float("inf"))

    def test_rejects_daily_balance_overflow(self):
        """Two 1e308 wins overflow the daily balance; that must not pass silently."""
        result = make_result(
            trades=[
                make_trade(exit_time="2024-01-15T10:00:00Z", pnl=1e308),
                make_trade(exit_time="2024-01-15T15:00:00Z", pnl=1e308),
            ]
        )

        with pytest.raises(ValueError, match="Daily equity accumulation is non-finite"):
            build_daily_equity_curve(result)


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

    def test_empty_result_clears_stale_daily_fields(self):
        """A re-attach on an empty result must not leave stale daily fields."""
        result = make_result()
        result.stats["daily_returns"] = [{"stale": True}]
        result.meta.data["daily_return_count"] = 7

        attach_daily_equity(result)

        assert result.equity_curve == []
        assert result.stats["daily_return_count"] == 0
        assert result.stats["daily_returns"] == []
        assert "daily_return_count" not in result.meta.data

    def test_fallback_clears_stale_daily_returns(self):
        """The no-daily-points fallback keeps the count and the list consistent."""
        result = make_result(
            equity_curve=[EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0)]
        )
        result.stats["daily_returns"] = [{"stale": True}]

        attach_daily_equity(result)

        assert result.meta.data["equity_frequency"] == "unknown"
        assert result.stats["daily_return_count"] == 0
        assert result.stats["daily_returns"] == []

    def test_generated_curve_normalizes_inception_close_collision(self):
        """An inception instant at the day close must not duplicate timestamps."""
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T23:59:59.999999Z", pnl=500.0)]
        )

        attach_daily_equity(result, start="2024-01-15T23:59:59.999999Z")

        assert result.equity_curve[0].time == "2024-01-15T23:59:59.999999+00:00"
        assert result.equity_curve[0].equity == 100_000.0
        assert result.equity_curve[1].time == "2024-01-16T00:00:00+00:00"
        assert result.equity_curve[1].equity == 100_500.0
        times = [point.time for point in result.equity_curve]
        assert times == sorted(times)
        assert len(times) == len(set(times))

    def test_partial_end_bound_uses_requested_instant(self):
        """The last close must not overshoot an explicit end instant."""
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T10:00:00Z", pnl=500.0)]
        )

        attach_daily_equity(result, end="2024-01-15T12:00:00Z")

        assert result.equity_curve[-1].time == "2024-01-15T12:00:00+00:00"
        assert result.equity_curve[-1].equity == 100_500.0

    def test_zero_width_window_fails_loudly(self):
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T12:00:00Z", pnl=500.0)]
        )

        with pytest.raises(ValueError, match="start and end bounds must differ"):
            attach_daily_equity(
                result,
                start="2024-01-15T12:00:00Z",
                end="2024-01-15T12:00:00Z",
            )

    def test_invalid_window_bound_fails_loudly(self):
        result = make_result(trades=[make_trade()])

        with pytest.raises(ValueError, match="start bound is invalid"):
            attach_daily_equity(result, start="not-a-date")

        with pytest.raises(ValueError, match="end bound is invalid"):
            attach_daily_equity(result, end="not-a-date")

    def test_reversed_window_fails_loudly(self):
        result = make_result(trades=[make_trade()])

        with pytest.raises(ValueError, match="start bound must not exceed end bound"):
            attach_daily_equity(result, start="2024-02-01", end="2024-01-01")

    def test_net_pnl_cutoff_controls_inclusive_boundary(self):
        cutoff = datetime.fromisoformat("2024-01-15T15:00:00+00:00")
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T15:00:00Z", pnl=500.0)]
        )

        assert _net_pnl_before(result, cutoff, inclusive=False) == 0.0
        assert _net_pnl_before(result, cutoff, inclusive=True) == 500.0

    def test_trade_exactly_at_window_start_is_counted_once(self):
        """A trade exiting exactly at the window start is window PnL, not prior."""
        result = make_result(
            trades=[make_trade(exit_time="2024-02-01T00:00:00Z", pnl=700.0)]
        )

        attach_daily_equity(
            result,
            start="2024-02-01T00:00:00Z",
            end="2024-02-28T23:59:59Z",
        )

        assert result.equity_curve[0].equity == 100_000.0
        assert result.equity_curve[-1].equity == 100_700.0
        assert result.stats["daily_returns"][0] == {
            "date": "2024-02-01",
            "return": pytest.approx(0.007),
            "equity": 100_700.0,
        }

    def test_window_rebases_starting_equity_from_prior_trades(self):
        """A windowed curve must start from the equity at the window start."""
        prior_trade = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0)
        window_trade.id = 2
        result = make_result(trades=[prior_trade, window_trade])

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_500.0
        assert result.equity_curve[-1].equity == 101_200.0
        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-02-10"]["return"] == pytest.approx(700 / 100_500)

    def test_window_rebases_from_net_pnl_not_gross_engine_point(self):
        """The reconciliation must prefer the net ledger over a gross engine point."""
        prior_trade = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        prior_trade.fees = 10.0
        prior_trade.net_pnl = 490.0
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0)
        window_trade.id = 2
        result = make_result(
            trades=[prior_trade, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_500.0)
            ],
        )

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_490.0
        assert result.equity_curve[-1].equity == 101_190.0
        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-02-10"]["return"] == pytest.approx(700 / 100_490)

    def test_reconciliation_includes_trade_at_engine_point(self):
        """A trade exiting exactly at an engine point is part of that point."""
        first_trade = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        second_trade = make_trade(exit_time="2024-01-20T15:00:00Z", pnl=200.0)
        second_trade.id = 2
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=300.0)
        window_trade.id = 3
        result = make_result(
            trades=[first_trade, second_trade, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_500.0)
            ],
        )

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_700.0
        assert result.equity_curve[-1].equity == 101_000.0
        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-02-10"]["return"] == pytest.approx(300 / 100_700)

    def test_window_ignores_nonfinite_prior_engine_point(self):
        prior_trade = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0)
        window_trade.id = 2
        result = make_result(
            trades=[prior_trade, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=float("nan"))
            ],
        )

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_500.0
        assert result.equity_curve[-1].equity == 101_200.0

    def test_window_starting_equity_reconciles_within_one_cent(self):
        """An engine point within one cent of the net ledger is trusted."""
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")

        for engine_equity in (100_500.0, 100_500.005, 100_499.995):
            result = make_result(
                trades=[prior],
                equity_curve=[
                    EquityPoint(time="2024-01-10T15:00:00+00:00", equity=engine_equity)
                ],
            )

            assert _window_starting_equity(result, start) == pytest.approx(engine_equity)

    def test_window_starting_equity_carries_post_reconciliation_pnl(self):
        """A prior trade after the engine point must reach the window start.

        The engine point agrees within one cent, so the reconciled point is
        trusted as the running-equity anchor.  The trade that exits *after* it
        is still prior to the window, and its $250 only arrives at the window
        start through the reconciliation term.  Dropping that term silently
        rebases every window return onto an equity $250 too small.
        """
        anchored = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        between_point_and_window = make_trade(exit_time="2024-01-20T15:00:00Z", pnl=250.0)
        between_point_and_window.id = 2
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=125.0)
        window_trade.id = 3
        result = make_result(
            trades=[anchored, between_point_and_window, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_500.0)
            ],
        )
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")

        # Engine point 100_500 == initial 100_000 + 500 through the point, so the
        # one-cent agreement holds and the anchor is the engine equity.  The
        # window start is the ledger's 100_750, not the anchor's 100_500.
        assert _window_starting_equity(result, start) == pytest.approx(100_750.0)

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_750.0
        assert result.equity_curve[-1].equity == 100_875.0
        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-02-10"]["return"] == pytest.approx(125 / 100_750)

    def test_window_starting_equity_between_engine_point_and_window_uses_net_pnl(self):
        """Both a cent-agreeing anchor and a post-anchor trade, counted once each.

        The engine closes one point at a net +$499.99 (one cent below the
        ledger), then a further trade nets +$100.02 before the window opens.
        Trusting the anchor must still add the later trade, and the trade that
        is *at* the anchor must not be counted twice.
        """
        anchored = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        anchored.fees = 0.01
        anchored.net_pnl = 499.99
        later = make_trade(exit_time="2024-01-25T15:00:00Z", pnl=100.0)
        later.fees = 0.0
        later.net_pnl = 100.02
        later.id = 2
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=7.0)
        window_trade.id = 3
        result = make_result(
            trades=[anchored, later, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_499.99)
            ],
        )
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")

        assert _window_starting_equity(result, start) == pytest.approx(100_600.01)

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-02-10"]["return"] == pytest.approx(7.0 / 100_600.01)

    def test_window_starting_equity_rejects_drift_beyond_one_cent(self):
        """Beyond one cent the engine point is ignored and the ledger wins."""
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")
        result = make_result(
            trades=[prior],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_500.02)
            ],
        )

        assert _window_starting_equity(result, start) == pytest.approx(100_500.0)

    def test_window_starting_equity_cent_tolerance_is_absolute_on_high_priced_account(self):
        """One cent stays one cent: a relative tolerance would hide $0.02 here."""
        initial_capital = 1_000_000_000_000.0
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")
        result = make_result(
            trades=[prior],
            initial_capital=initial_capital,
            equity_curve=[
                EquityPoint(
                    time="2024-01-10T15:00:00+00:00",
                    equity=initial_capital + 500.02,
                )
            ],
        )

        assert _window_starting_equity(result, start) == pytest.approx(
            initial_capital + 500.0
        )

    def test_high_priced_account_endpoint_drift_beyond_one_cent_is_rejected(self):
        """A 50-cent drift on a trillion-dollar account must not be preserved."""
        initial_capital = 1_000_000_000_000.0
        daily_final = initial_capital + 500.0
        timestamps = pd.date_range("2024-01-01", periods=31, freq="D", tz="UTC")
        curve = [
            EquityPoint(
                time=timestamp.isoformat(),
                equity=daily_final - 0.50 if timestamp == timestamps[-1] else initial_capital,
            )
            for timestamp in timestamps
        ]
        result = make_result(
            trades=[make_trade(exit_time="2024-01-31T00:00:00Z", pnl=500.0)],
            initial_capital=initial_capital,
            equity_curve=curve,
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
        )

        attach_daily_equity(result)

        assert result.equity_curve != curve
        assert result.equity_curve[-1].equity == daily_final

    def test_endpoint_drift_within_one_cent_is_preserved(self):
        """The one-cent tolerance is intentional: half a cent is acceptable."""
        initial_capital = 1_000_000_000_000.0
        daily_final = initial_capital + 500.0
        timestamps = pd.date_range("2024-01-01", periods=31, freq="D", tz="UTC")
        curve = [
            EquityPoint(
                time=timestamp.isoformat(),
                equity=daily_final - 0.005 if timestamp == timestamps[-1] else initial_capital,
            )
            for timestamp in timestamps
        ]
        result = make_result(
            trades=[make_trade(exit_time="2024-01-31T00:00:00Z", pnl=500.0)],
            initial_capital=initial_capital,
            equity_curve=curve,
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
        )

        attach_daily_equity(result)

        assert result.equity_curve == curve

    def test_preserved_curve_ending_before_last_trade_is_rejected(self):
        """A curve that stops before the last trade cannot describe the window."""
        timestamps = pd.date_range("2024-01-01", "2024-01-27", freq="D", tz="UTC")
        curve = [
            EquityPoint(time=timestamp.isoformat(), equity=100_000.0)
            for timestamp in timestamps
        ]
        result = make_result(
            trades=[
                make_trade(exit_time="2024-01-05T15:00:00Z", pnl=500.0),
                make_trade(exit_time="2024-01-29T15:00:00Z", pnl=-500.0),
            ],
            equity_curve=curve,
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
        )

        attach_daily_equity(result)

        assert result.equity_curve != curve
        assert result.equity_curve[-1].time == "2024-01-31T23:59:59.999999+00:00"

    def test_net_pnl_falls_back_to_gross_minus_fees(self):
        """With no stored net PnL the ledger fee must be *subtracted*.

        A gross trade (+$120) whose $20 fee is inverted into +$20 turns the
        winner into a $140 winner, and a losing gross trade (-$50) with a $5
        fee becomes a $45 winner.  Both flips are visible in the win/loss
        split, so a `+` where the money path needs a `-` cannot pass.
        """
        winner = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=120.0)
        winner.fees = 20.0
        winner.net_pnl = None
        loser = make_trade(exit_time="2024-01-11T15:00:00Z", pnl=-50.0)
        loser.fees = 5.0
        loser.net_pnl = None
        loser.id = 2

        assert _trade_net_pnl(winner) == 100.0
        assert _trade_net_pnl(loser) == -55.0

        attach_daily_equity(result := make_result(trades=[winner, loser]), end="2024-01-31")

        # 100_000 + 100 - 55: fees reduce equity, they never add to it.
        assert result.equity_curve[-1].equity == 100_045.0
        by_date = {daily["date"]: daily for daily in result.stats["daily_returns"]}
        assert by_date["2024-01-10"]["equity"] == 100_100.0
        assert by_date["2024-01-11"]["equity"] == 100_045.0

    def test_net_pnl_fee_fallback_respects_the_day_bucket_and_cutoff(self):
        """The fee-aware net value is what the window ledger is built from.

        The only exercise of `pnl - fees` also has to reach the prior-PnL
        reconstruction: a fee-inflated prior balance would drift the window
        start by exactly twice the fee.
        """
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        prior.fees = 12.0
        prior.net_pnl = None
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0)
        window_trade.id = 2
        result = make_result(trades=[prior, window_trade])
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")

        # Ledger: 100_000 + (500 - 12).  An inverted fee gives 100_512 instead.
        assert _window_starting_equity(result, start) == pytest.approx(100_488.0)
        assert _net_pnl_before(result, start) == pytest.approx(488.0)

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_488.0
        assert result.equity_curve[-1].equity == 101_188.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Tests for runner.daily_equity module."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import runner.daily_equity as daily_equity_module
from runner.daily_equity import (
    _net_pnl_before,
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
    id=1,
):
    """Helper to create a Trade."""
    return Trade(
        id=id,
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

    def test_daily_bucket_uses_net_pnl_not_gross(self):
        """The daily curve is a net ledger: fees must be subtracted (#84)."""
        trade = make_trade(pnl=500.0)
        trade.fees = 8.0
        trade.net_pnl = 492.0
        result = make_result(trades=[trade])

        points, returns = build_daily_equity_curve(result)

        by_date = {daily["date"]: daily for daily in returns}
        assert by_date["2024-01-15"]["equity"] == 100_492.0
        assert points[-1].equity == 100_492.0

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

    @pytest.mark.parametrize("initial_capital", [0.0, -1.0, -100_000.0])
    def test_rejects_nonpositive_initial_capital(self, initial_capital):
        """A zero or negative base makes every return undefined, not just small.

        `equity / initial_capital` is a division by zero at 0 and a sign flip
        below it, so this must be refused rather than published as a curve.
        """
        result = make_result(initial_capital=initial_capital, trades=[make_trade()])

        with pytest.raises(ValueError, match="initial_capital must be finite and > 0"):
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

        assert not _preserves_engine_curve(preserved_curve, daily_curve, [])

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
        result.meta.data["daily_return_count"] = 7

        attach_daily_equity(result)

        assert result.meta.data["equity_frequency"] == "unknown"
        assert "daily_return_count" not in result.meta.data
        assert result.stats["daily_return_count"] == 0
        assert result.stats["daily_returns"] == []

    def test_sparse_reconciling_curve_is_carried_through_the_window_end(self):
        """The adopted engine curve must describe the requested window.

        The density gate rejects this curve (it stops 26 days before the window
        ends), so the mark-to-market fallback adopts it — and the fallback must
        then carry the flat tail to the window end. Otherwise the published
        daily series would cover five days of a thirty-one day window.
        """
        curve = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-03T00:00:00+00:00", equity=100_300.0),
            EquityPoint(time="2024-01-05T00:00:00+00:00", equity=100_500.0),
        ]
        result = make_result(
            trades=[make_trade(exit_time="2024-01-05T15:00:00Z", pnl=500.0)],
            equity_curve=curve,
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
        )

        attach_daily_equity(result)

        assert result.equity_curve[0].equity == 100_000.0
        # 01-02 is a skipped day, so it is carried flat at the previous equity.
        assert [point.time[:10] for point in result.equity_curve[:3]] == [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
        ]
        assert result.equity_curve[1].equity == 100_000.0
        assert result.equity_curve[2].equity == 100_300.0
        assert result.equity_curve[-1].time == "2024-01-31T00:00:00+00:00"
        assert result.equity_curve[-1].equity == 100_500.0
        # Every calendar day of the window after the opening is a return.
        assert result.stats["daily_return_count"] == 30
        assert result.stats["daily_returns"][-1]["return"] == 0.0

    def test_fallback_clears_stale_meta_count_on_a_daily_curve(self):
        """A fallback that keeps a daily curve reports zero returns in meta too."""
        curve = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-02T00:00:00+00:00", equity=100_000.0),
        ]
        result = make_result(equity_curve=curve)
        result.meta.data["daily_return_count"] = 7

        attach_daily_equity(result)

        assert result.meta.data["equity_frequency"] == "daily"
        assert result.meta.data["daily_return_count"] == 0
        assert result.stats["daily_return_count"] == 0

    def test_fallback_coalesces_duplicate_curve_timestamps(self):
        """The fallback must publish a strictly increasing curve."""
        result = make_result(
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_100.0),
            ]
        )

        attach_daily_equity(result)

        assert [(point.time, point.equity) for point in result.equity_curve] == [
            ("2024-01-01T00:00:00+00:00", 100_100.0)
        ]

    def test_fallback_rejects_nonfinite_engine_equity(self):
        """A corrupt engine curve must not be published."""
        result = make_result(
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=float("inf")),
            ]
        )

        with pytest.raises(ValueError, match="non-finite equity"):
            attach_daily_equity(result)

    def test_fallback_scopes_existing_curve_to_the_requested_window(self):
        """A window without trades must not publish the unwindowed curve."""
        curve = [
            EquityPoint(time=f"2024-02-{day:02d}T00:00:00+00:00", equity=100_000.0 + day)
            for day in range(1, 10)
        ]
        result = make_result(
            trades=[make_trade(exit_time="2024-01-05T15:00:00Z")],
            equity_curve=curve,
        )

        attach_daily_equity(result, start="2024-02-01", end="2024-02-05")

        assert result.equity_curve == curve[:5]
        assert result.meta.data["equity_frequency"] == "daily"
        assert result.stats["daily_return_count"] == 0

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

    def test_meta_end_instant_clamps_the_last_close(self):
        """A meta end carrying a time is a boundary, not a whole day."""
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T10:00:00Z", pnl=500.0)],
            backtest_end="2024-01-15T12:00:00Z",
        )

        attach_daily_equity(result)

        assert result.equity_curve[-1].time == "2024-01-15T12:00:00+00:00"

    def test_meta_end_instant_excludes_later_trades_from_the_last_day(self):
        """A trade after the meta end instant must not feed the last close."""
        result = make_result(
            trades=[
                make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0),
                make_trade(exit_time="2024-01-15T15:00:00Z", pnl=700.0, id=2),
            ],
            backtest_end="2024-01-15T12:00:00Z",
        )

        attach_daily_equity(result)

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

    def test_meta_start_rebases_starting_equity_like_an_explicit_window(self):
        """The window can come from ``meta.backtest_start`` alone."""
        prior_trade = make_trade(exit_time="2024-01-05T15:00:00Z", pnl=500.0)
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0, id=2)
        result = make_result(
            trades=[prior_trade, window_trade],
            backtest_start="2024-02-01",
            backtest_end="2024-02-28",
        )

        attach_daily_equity(result)

        assert result.equity_curve[0].equity == 100_500.0
        assert result.equity_curve[-1].equity == 101_200.0

    def test_window_rebases_starting_equity_from_prior_trades(self):
        """A windowed curve must start from the equity at the window start."""
        prior_trade = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0, id=2)
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
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0, id=2)
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
        second_trade = make_trade(exit_time="2024-01-20T15:00:00Z", pnl=200.0, id=2)
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=300.0, id=3)
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
        window_trade = make_trade(exit_time="2024-02-10T15:00:00Z", pnl=700.0, id=2)
        result = make_result(
            trades=[prior_trade, window_trade],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=float("nan"))
            ],
        )

        attach_daily_equity(result, start="2024-02-01", end="2024-02-28")

        assert result.equity_curve[0].equity == 100_500.0
        assert result.equity_curve[-1].equity == 101_200.0

    @pytest.mark.parametrize("engine_equity", [100_500.0, 100_500.005, 100_499.995])
    def test_window_starting_equity_reconciles_within_one_cent(self, engine_equity):
        """An engine point within one cent of the net ledger is trusted."""
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")
        result = make_result(
            trades=[prior],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=engine_equity)
            ],
        )

        # rel=0: pytest's default rel=1e-6 would allow 0.1 currency units of
        # slack here, which is exactly the tolerance under test.
        assert _window_starting_equity(result, start) == pytest.approx(
            engine_equity, rel=0, abs=1e-9
        )

    def test_window_starting_equity_rejects_drift_beyond_one_cent(self):
        """Beyond one cent the engine point is ignored and the ledger wins.

        The drift is 1.5 cents on purpose: exactly 0.02 is not distinguishable
        from a two-cent tolerance because 100_500.02 - 100_500.0 rounds just
        above 0.02 in binary floating point.
        """
        prior = make_trade(exit_time="2024-01-10T15:00:00Z", pnl=500.0)
        start = datetime.fromisoformat("2024-02-01T00:00:00+00:00")
        result = make_result(
            trades=[prior],
            equity_curve=[
                EquityPoint(time="2024-01-10T15:00:00+00:00", equity=100_500.015)
            ],
        )

        assert _window_starting_equity(result, start) == pytest.approx(
            100_500.0, rel=0, abs=1e-9
        )

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
            initial_capital + 500.0, rel=0, abs=1e-6
        )

    def _assert_endpoint_drift(self, *, drift, preserved, initial_capital):
        """Attach a 31-day curve whose endpoint is off by ``drift`` and check the outcome."""
        daily_final = initial_capital + 500.0
        timestamps = pd.date_range("2024-01-01", periods=31, freq="D", tz="UTC")
        curve = [
            EquityPoint(
                time=timestamp.isoformat(),
                equity=daily_final - drift if timestamp == timestamps[-1] else initial_capital,
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

        if preserved:
            assert result.equity_curve == curve
        else:
            assert result.equity_curve != curve
            assert result.equity_curve[-1].equity == daily_final

    @pytest.mark.parametrize(
        ("drift", "preserved"),
        [
            (0.005, True),  # within one cent: the engine curve wins
            (0.015, False),  # beyond one cent: the net ledger wins
            (0.50, False),  # a relative tolerance would have accepted this
        ],
    )
    def test_endpoint_drift_is_judged_by_an_absolute_one_cent(self, drift, preserved):
        """One cent is absolute, also on a trillion-dollar account."""
        self._assert_endpoint_drift(
            drift=drift, preserved=preserved, initial_capital=1_000_000_000_000.0
        )

    @pytest.mark.parametrize(
        ("drift", "preserved"),
        [
            (0.0, True),
            (0.0049, True),  # just inside the cent
            (0.0051, True),  # the other side of the same rounding step
            (0.0099, True),  # still inside
            (0.0101, False),  # just outside: the ledger wins
        ],
    )
    def test_fractional_endpoint_drift_pins_both_sides_of_one_cent(
        self, drift, preserved
    ):
        """Pin the one-cent cliff itself, not just points far from it.

        The ledger endpoint is quantised to the cent, so the comparison must
        resolve drift at a fraction of a cent.  Running the same case at 0.0049
        and 0.0101 pins the boundary from both sides on an account whose
        absolute size does not wash the fraction out.
        """
        self._assert_endpoint_drift(
            drift=drift, preserved=preserved, initial_capital=100_000.0
        )

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

    def test_curve_frequency_parses_mixed_resolution_timestamps(self):
        """A daily curve mixing second and microsecond forms is still daily.

        The generated curve is exactly this shape: the inception point carries
        second resolution and every close carries microseconds.  pandas infers a
        single format from the first element unless told ``format="mixed"``, so
        without it every close becomes NaT, the median spacing turns into NaT
        and the frequency silently degrades to ``"unknown"``.
        """
        points = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-02T23:59:59.999999+00:00", equity=100_100.0),
            EquityPoint(time="2024-01-03T23:59:59.999999+00:00", equity=100_200.0),
        ]

        assert daily_equity_module._curve_frequency(points) == "daily"

    def test_curve_frequency_parses_mixed_resolution_intraday_points(self):
        """The mixed-format path must also resolve sub-daily spacing."""
        points = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-01T00:15:00.500000+00:00", equity=100_010.0),
            EquityPoint(time="2024-01-01T00:30:00.500000+00:00", equity=100_020.0),
        ]

        assert daily_equity_module._curve_frequency(points) == "intraday"

    def test_strictly_increasing_curve_rejects_shift_that_leaves_the_window(self):
        """A colliding timestamp cannot be shifted past ``end_bound``.

        The microsecond shift exists to make duplicate timestamps strictly
        increasing, but it must never push a point outside the requested
        window.  Here the only point sits exactly on the bound, so the shift
        would overshoot by one microsecond and the curve must be rejected.
        """
        inception = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        end_bound = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        curve = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0)]

        assert daily_equity_module._strictly_increasing_curve(
            curve, inception, end_bound
        ) is None

    def test_strictly_increasing_curve_keeps_shift_that_stays_inside_the_window(self):
        """One microsecond of headroom is enough for the shift to be allowed.

        Same curve as the rejection case, so the two tests pin the boundary
        itself rather than any other difference between them.
        """
        inception = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        end_bound = datetime.fromisoformat("2024-01-01T00:00:00.000001+00:00")
        curve = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0)]

        normalized = daily_equity_module._strictly_increasing_curve(
            curve, inception, end_bound
        )

        assert normalized is not None
        assert normalized[-1].time == "2024-01-01T00:00:00.000001+00:00"

    def test_daily_returns_equity_matches_the_published_curve_close(self):
        """Both daily views must report the same, cent-quantised equity.

        ``stats["daily_returns"][i]["equity"]`` and the curve close for the same
        instant are two representations of one number.  Rounding only the curve
        lets them drift up to half a cent apart, which reads as accounting
        noise to anything comparing the two.
        """
        # Sub-cent daily gains so the rounding actually has something to do.
        trades = [
            make_trade(exit_time=f"2024-01-{day:02d}T15:00:00Z", pnl=0.004, id=day)
            for day in range(1, 31)
        ]
        result = make_result(trades=trades)

        points, returns = build_daily_equity_curve(result)

        assert len(returns) == len(points) - 1
        for day_return, point in zip(returns, points[1:], strict=True):
            assert day_return["equity"] == point.equity

    def test_daily_returns_equity_is_cent_quantised(self):
        """Every reported equity is a whole number of cents."""
        result = make_result(
            trades=[
                make_trade(exit_time="2024-01-15T15:00:00Z", pnl=1234.5678),
                make_trade(exit_time="2024-01-16T15:00:00Z", pnl=987.6543, id=2),
            ]
        )

        _, returns = build_daily_equity_curve(result)

        assert all(
            day_return["equity"] == round(day_return["equity"], 2)
            for day_return in returns
        )
        assert returns[14]["equity"] == 101_234.57

    def test_fractional_starting_equity_is_quantised_like_the_closes(self):
        """A fractional opening balance is reported to the cent, not raw.

        ``starting_equity`` comes from the reconstructed window start and is
        generally not a whole number of cents.  It enters the curve at the same
        precision as every close, so the opening point does not carry more
        digits than the rest of the series.
        """
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T15:00:00Z", pnl=0.0)]
        )

        points, returns = build_daily_equity_curve(result, starting_equity=100_000.005)

        # 100_000.005 is the nearest-cent rounding case: it lands on .01.
        assert points[0].equity == 100_000.01
        assert all(
            day_return["equity"] == round(day_return["equity"], 2)
            for day_return in returns
        )
        by_date = {daily["date"]: daily for daily in returns}
        # A zero-PnL day reports the same quantised balance and a flat return.
        assert by_date["2024-01-14"]["equity"] == points[0].equity
        assert by_date["2024-01-14"]["return"] == pytest.approx(0.0, abs=0.0)
        assert by_date["2024-01-01"]["equity"] == points[0].equity

    def test_daily_returns_reconstruct_the_curve_endpoint(self):
        """The reported equity series must land on the published curve closes.

        This pins the *relationship* between the two published views rather than
        their individual values: ``daily_returns[i]["equity"]`` and the matching
        curve close describe the same instant and must carry the same number.
        Quantising one side without the other breaks this.
        """
        trades = [
            make_trade(exit_time=f"2024-01-{day:02d}T15:00:00Z", pnl=0.004, id=day)
            for day in range(1, 31)
        ]
        result = make_result(trades=trades)

        points, returns = build_daily_equity_curve(result)

        # One point per reported day, plus the opening point.
        assert len(points) == len(returns) + 1
        for day_return, point in zip(returns, points[1:], strict=True):
            assert day_return["equity"] == point.equity
        assert returns[-1]["equity"] == points[-1].equity

        # The per-day factors are computed from the raw ledger, so compounding
        # them reconstructs the ledger's own move.  The equity series is that
        # same move quantised to the cent, so the two agree to within the
        # rounding the curve can introduce on the opening balance.
        compounded = 1.0
        for day_return in returns:
            if day_return["return"] is not None:
                compounded *= 1.0 + day_return["return"]
        ledger_factor = returns[-1]["equity"] / points[0].equity

        # One cent of endpoint difference, expressed against the opening
        # balance, plus the fp noise the compounding multiply can accumulate.
        one_cent = 0.01 / points[0].equity
        assert compounded == pytest.approx(
            ledger_factor, rel=0, abs=one_cent + abs(ledger_factor) * 1e-12
        )

    def test_published_curve_never_overshoots_the_end_instant(self):
        """A generated curve must stay inside the requested window end.

        Drives the ``end_bound`` guard through the public entry point: when the
        end instant lands just short of the day close, the normalised closes
        must not be shifted past it.
        """
        result = make_result(
            trades=[make_trade(exit_time="2024-01-15T15:00:00Z", pnl=500.0)],
            backtest_end="2024-01-15T23:59:59.999998+00:00",
        )

        attach_daily_equity(result)

        end_bound = datetime.fromisoformat("2024-01-15T23:59:59.999998+00:00")
        assert all(
            datetime.fromisoformat(point.time) <= end_bound
            for point in result.equity_curve
        )
        assert result.equity_curve[-1].time == "2024-01-15T23:59:59.999998+00:00"


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

    def test_a_first_day_move_in_the_engine_curve_is_not_dropped(self):
        """The opening baseline must not swallow a real first-day close.

        A curve whose first observation already carries day one's PnL (the
        engine stamps its bars at the start of the bar) used to be skipped as
        "on or before the baseline day", so the move vanished from the series
        while the endpoint still reconciled.
        """
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_500.0),
                EquityPoint(time="2024-01-03T00:00:00+00:00", equity=100_700.0),
                EquityPoint(time="2024-01-05T00:00:00+00:00", equity=101_000.0),
            ],
            pnl=1000.0,
            exit_time="2024-01-05T15:00:00Z",
        )

        points, returns = _daily_points_from_engine_curve(
            result, start=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )

        assert points[0].equity == 100_000.0
        assert points[1].equity == 100_500.0
        assert returns[0]["date"] == "2024-01-01"
        assert returns[0]["return"] == pytest.approx(0.005)

    def test_the_series_covers_the_whole_window_not_just_the_observations(self):
        """A sparse curve must not shorten the reported window.

        The calendar fill emits one return per day of the window. If the engine
        path stopped at its last observation, the same window would publish a
        different number of daily returns — and Sharpe's denominator would
        depend on where the engine happened to stop observing.
        """
        from runner.daily_equity import _daily_points_from_engine_curve

        result = self._result(
            [
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
                EquityPoint(time="2024-01-05T00:00:00+00:00", equity=100_500.0),
                EquityPoint(time="2024-01-06T00:00:00+00:00", equity=101_000.0),
            ],
            pnl=1000.0,
            exit_time="2024-01-06T15:00:00Z",
        )

        points, returns = _daily_points_from_engine_curve(
            result,
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 10, 23, 59, 59, 999999, tzinfo=timezone.utc),
        )

        # Opening point plus one point per calendar day through the window end.
        assert [point.time[:10] for point in points] == [
            f"2024-01-{day:02d}" for day in range(1, 11)
        ]
        assert [item["date"] for item in returns] == [
            f"2024-01-{day:02d}" for day in range(2, 11)
        ]
        # The flat tail carries the last known equity, not a guess.
        assert points[-1].equity == 101_000.0
        assert returns[-1]["return"] == 0.0

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

    def test_tolerance_is_one_cent_per_trade(self):
        """The bound is ``max(n * one cent, float64 drift)``."""
        from runner.daily_equity import (
            EQUITY_CENT,
            _reconciliation_tolerance,
        )

        # One trade: exactly one cent, the same absolute cliff the preserved
        # curve gate applies. A separate flat cent on top would double-count
        # the first trade and admit a 1.5-cent drift on a single-trade run.
        assert _reconciliation_tolerance(
            expected_final=100000.0, trade_count=1
        ) == pytest.approx(EQUITY_CENT)
        # A hundred trades: the per-trade term dominates at a realistic account
        # size, and it is derived from the rounding, not from a round number.
        hundred = _reconciliation_tolerance(
            expected_final=100000.0, trade_count=100
        )
        assert hundred == pytest.approx(100 * EQUITY_CENT)
        assert hundred == pytest.approx(1.0)
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
        """At $1e15 the float64 accumulation term, not the cent term, binds.

        That is what the relative epsilon is for: cent quantisation does not
        grow with the account, but float64 summation error does.
        """
        from runner.daily_equity import (
            EQUITY_CENT,
            _reconciliation_tolerance,
        )

        tolerance = _reconciliation_tolerance(
            expected_final=1e15, trade_count=10
        )

        # 8 * 10 * 2**-52 * 1e15 = ~17.76, far above ten cents.
        assert tolerance == pytest.approx(
            8.0 * 10 * 2.0**-52 * 1e15
        )
        assert tolerance > EQUITY_CENT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

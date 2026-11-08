"""Tests for runner.stats — performance metrics calculation."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from runner.stats import (
    _daily_returns_from_equity_curve,
    _max_consecutive,
    _max_drawdown_pct_manual,
    _resolve_backtest_period_bounds,
    compute_stats,
    compute_stats_for_result,
)
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit


def _trade(pnl: float, holding_bars: int | None = None) -> Trade:
    """Helper: minimal trade with given PnL."""
    return Trade(
        id=1, symbol="X", direction="LONG",
        entry_time="2021-01-01T00:00:00Z", entry_price=100.0,
        quantity=10,
        exits=[TradeExit(time="2021-01-02T00:00:00Z", price=100 + pnl / 10, quantity=10)],
        pnl=pnl,
        holding_bars=holding_bars,
    )


def _equity_curve_from_returns(returns: list[float], initial_capital: float = 100000.0) -> list[EquityPoint]:
    points = []
    equity = initial_capital
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    points.append(EquityPoint(time=start.isoformat(), equity=equity))
    for index, ret in enumerate(returns, start=1):
        equity *= 1 + ret
        points.append(EquityPoint(time=(start + timedelta(days=index)).isoformat(), equity=equity))
    return points


class TestComputeStats:
    def test_empty_trades(self):
        stats = compute_stats([])
        assert stats["total_trades"] == 0
        assert stats["win_rate"] == 0.0
        assert stats["risk_free_rate"] == pytest.approx(0.0)

    def test_empty_trades_emit_supplied_risk_free_rate(self):
        stats = compute_stats([], risk_free_rate=0.0375)
        assert stats["risk_free_rate"] == pytest.approx(0.0375)

    def test_all_winners(self):
        trades = [_trade(100), _trade(200), _trade(50)]
        stats = compute_stats(trades)
        assert stats["total_trades"] == 3
        assert stats["winners"] == 3
        assert stats["losers"] == 0
        assert stats["win_rate"] == 1.0
        assert stats["total_pnl"] == 350.0
        assert stats["profit_factor"] is None  # no losses → inf → None

    def test_mixed_trades(self):
        trades = [_trade(100), _trade(-50), _trade(200), _trade(-30), _trade(-20)]
        stats = compute_stats(trades)
        assert stats["total_trades"] == 5
        assert stats["winners"] == 2
        assert stats["losers"] == 3
        assert abs(stats["win_rate"] - 0.4) < 0.01
        assert stats["total_pnl"] == 200.0
        assert stats["avg_winner"] == 150.0
        assert stats["avg_loser"] == round(-100 / 3, 2)
        # Profit factor: 300 / 100 = 3.0
        assert stats["profit_factor"] == 3.0

    def test_holding_bars(self):
        trades = [_trade(10, holding_bars=5), _trade(-5, holding_bars=3)]
        stats = compute_stats(trades)
        assert stats["avg_holding_bars"] == 4.0

    def test_holding_bars_none_ignored(self):
        trades = [_trade(10, holding_bars=None), _trade(-5, holding_bars=3)]
        stats = compute_stats(trades)
        assert stats["avg_holding_bars"] == 3.0

    def test_with_equity_curve(self):
        trades = [_trade(1000), _trade(-500), _trade(2000)]
        equity = [
            EquityPoint(time="2021-01-01", equity=100000),
            EquityPoint(time="2021-01-02", equity=101000),
            EquityPoint(time="2021-01-03", equity=100500),
            EquityPoint(time="2021-01-04", equity=102500),
        ]
        stats = compute_stats(trades, equity_curve=equity)
        assert stats["max_drawdown_pct"] != 0  # QuantStats returns negative
        assert "sharpe_ratio" in stats
        assert "sortino_ratio" in stats
        assert "calmar_ratio" in stats
        assert "cagr" in stats
        assert "total_return" in stats
        assert "max_drawdown_duration" in stats

    def test_p_value_defaults_to_one_without_equity_returns(self):
        stats = compute_stats([_trade(100), _trade(-25)])
        assert stats["p_value"] == 1.0

    def test_p_value_is_computed_from_daily_returns(self):
        equity = _equity_curve_from_returns(
            [0.010, 0.012, 0.009, 0.014, 0.011, 0.013, 0.008, 0.012, 0.010, 0.011, 0.009, 0.013]
        )
        stats = compute_stats([], equity_curve=equity)
        assert 0.0 <= stats["p_value"] <= 1.0
        assert stats["p_value"] < 0.05

    def test_cagr_uses_elapsed_time_for_sparse_equity_curve(self):
        equity = [
            EquityPoint(time="2023-01-01T00:00:00+00:00", equity=100000.0),
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=110000.0),
        ]

        stats = compute_stats([], equity_curve=equity)

        assert 0.09 <= stats["cagr"] <= 0.11
        assert stats["total_return"] == 0.1

    def test_cagr_annualizes_with_trade_period_bounds(self):
        # #84: calendar-window CAGR — the equity endpoints (2020→2022),
        # not the clustered trade span (2021→2023).
        equity = [
            EquityPoint(time="2020-01-01T00:00:00+00:00", equity=100000.0),
            EquityPoint(time="2022-01-01T00:00:00+00:00", equity=121000.0),
        ]
        trades = [
            Trade(
                id=1,
                symbol="X",
                direction="LONG",
                entry_time="2021-01-01T00:00:00+00:00",
                entry_price=100.0,
                quantity=1,
                exits=[
                    TradeExit(
                        time="2023-01-01T00:00:00+00:00",
                        price=121.0,
                        quantity=1,
                        pnl=21000.0,
                    )
                ],
                pnl=21000.0,
            )
        ]

        stats = compute_stats(trades, equity_curve=equity)

        expected = (121000.0 / 100000.0) ** (365.25 / 731) - 1
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-3, abs_tol=1e-3)

    def test_cagr_handles_mixed_fractional_curve_timestamps(self):
        # Daily equity curves mix second-resolution inception points with
        # microsecond-resolution closes; without format="mixed" every close
        # coerces to NaT and the CAGR window silently falls back.
        equity = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-02T23:59:59.999999+00:00", equity=99_000.0),
            EquityPoint(time="2024-01-03T23:59:59.999999+00:00", equity=101_000.0),
        ]

        stats = compute_stats([], equity_curve=equity)

        expected = (101_000.0 / 100_000.0) ** (365.25 / 3.0) - 1.0
        assert stats["cagr"] == pytest.approx(expected, rel=0, abs=1e-6)

    def test_cagr_uses_fractional_days_from_trade_bounds(self):
        # #84: calendar window — 2024 is a leap year (366 days), not the
        # 365.75-day trade span.
        equity = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
            EquityPoint(time="2025-01-01T00:00:00+00:00", equity=110000.0),
        ]
        trades = [
            Trade(
                id=1,
                symbol="X",
                direction="LONG",
                entry_time="2024-01-01T12:00:00+00:00",
                entry_price=100.0,
                quantity=1,
                exits=[
                    TradeExit(
                        time="2025-01-01T06:00:00+00:00",
                        price=110.0,
                        quantity=1,
                        pnl=10000.0,
                    )
                ],
                pnl=10000.0,
            )
        ]

        stats = compute_stats(trades, equity_curve=equity)

        expected = (110000.0 / 100000.0) ** (365.25 / 366) - 1
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-3, abs_tol=1e-3)

    def test_trade_stats_are_net_of_fees(self):
        # #84: gross pnl flatters win-rate/profit-factor/total.
        def _net_trade(gross: float, fees: float) -> Trade:
            trade = _trade(gross)
            trade.fees = fees
            trade.net_pnl = gross - fees
            return trade

        trades = [_net_trade(100, 5), _net_trade(-50, 5), _net_trade(8, 10)]
        stats = compute_stats(trades)
        # Net: +95, -55, -2 → 1 winner, 2 losers, total +38.
        assert stats["winners"] == 1
        assert stats["losers"] == 2
        assert stats["total_pnl"] == 38.0
        assert stats["profit_factor"] == pytest.approx(95.0 / 57.0, abs=1e-3)

    def test_calmar_ratio_zeroed_for_tiny_drawdown(self):
        equity = _equity_curve_from_returns([0.0200, -0.0005, 0.0150, 0.0100])

        stats = compute_stats([], equity_curve=equity)

        assert abs(stats["max_drawdown_pct"]) < 0.001
        assert stats["calmar_ratio"] == 0.0

    def test_intraday_sharpe_uses_daily_returns_not_bar_density(self):
        start = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
        equity: list[EquityPoint] = []
        value = 100000.0
        for day in range(30):
            for bar in range(24):
                if bar == 23:
                    value *= 1.001
                elif bar % 7 == 0:
                    value *= 0.99995
                else:
                    value *= 1.00002
                equity.append(
                    EquityPoint(
                        time=(start + timedelta(days=day, minutes=bar)).isoformat(),
                        equity=value,
                    )
                )

        stats = compute_stats([], equity_curve=equity, risk_free_rate=0.04)
        daily_returns = _daily_returns_from_equity_curve(equity)

        assert stats["total_return"] > 0
        assert stats["sharpe_ratio"] > 0
        assert len(daily_returns) == 29

    def test_emit_supplied_risk_free_rate(self):
        stats = compute_stats([_trade(100)], risk_free_rate=0.0375)
        assert stats["risk_free_rate"] == pytest.approx(0.0375)


class TestMaxConsecutiveLosses:
    def test_no_losses(self):
        assert _max_consecutive([10, 20, 30], positive=False) == 0

    def test_all_losses(self):
        assert _max_consecutive([-10, -20, -30], positive=False) == 3

    def test_mixed(self):
        assert _max_consecutive([10, -5, -3, 20, -1, -2, -3, 10], positive=False) == 3

    def test_empty(self):
        assert _max_consecutive([], positive=False) == 0

    def test_consecutive_wins(self):
        assert _max_consecutive([10, 20, 30, -5, 10], positive=True) == 3


class TestMaxDrawdown:
    def test_no_drawdown(self):
        assert _max_drawdown_pct_manual([100, 110, 120, 130]) == 0.0

    def test_simple_drawdown(self):
        dd = _max_drawdown_pct_manual([100, 110, 100, 120])
        assert abs(dd - (10 / 110)) < 0.001  # ~0.0909

    def test_single_point(self):
        assert _max_drawdown_pct_manual([100]) == 0.0


class TestComputeStatsForResult:
    def test_convenience_wrapper(self):
        result = BacktestResult(
            meta=BacktestMeta(
                id="test",
                created_at="2026-01-01T00:00:00Z",
                engine="vectorbt",
                strategies=["test"],
                backtest_start="2021-01-01",
                backtest_end="2021-12-31",
                initial_capital=100000,
            ),
            trades=[_trade(100), _trade(-50)],
            equity_curve=[
                EquityPoint(time="2021-01-01", equity=100000),
                EquityPoint(time="2021-01-02", equity=100100),
                EquityPoint(time="2021-01-03", equity=100050),
            ],
        )
        stats = compute_stats_for_result(result)
        assert stats["total_trades"] == 2
        assert stats["total_pnl"] == 50.0


class TestTotalReturnDefinitionConsistency:
    """Mutation hardening (F17): total_return is the equity-endpoint ratio."""

    def test_total_return_uses_equity_endpoints_not_daily_product(self):
        """Intraday dips must not change total_return.

        The daily-resampled product only sees the LAST point per day, so an
        intraday drawdown that recovers (100k → 90k → 111k same day) would
        produce a different number than the documented equity-endpoint
        definition. Pin the endpoint ratio.
        """
        equity = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-01T12:00:00+00:00", equity=90_000.0),
            EquityPoint(time="2024-01-01T23:00:00+00:00", equity=111_000.0),
            EquityPoint(time="2024-01-02T00:00:00+00:00", equity=121_000.0),
        ]
        stats = compute_stats([], equity_curve=equity)
        assert stats["total_return"] == pytest.approx(0.21, rel=1e-9), (
            "total_return must be the equity-endpoint ratio (121k/100k - 1), "
            "not the daily-resampled compounded product (0.09)"
        )


class TestCalmarAndQsFallback:
    """Mutation hardening (F17): Calmar definition + QS-without-daily-series."""

    def test_calmar_uses_cagr_not_observation_annualized_return(self):
        """Calmar must divide elapsed-time CAGR by max drawdown.

        Kills the mutant that swaps CAGR for an observation-count annualized
        total return — the two diverge whenever the equity curve's cadence
        differs from 252 points/year.
        """
        # 2 years with a genuine drawdown (peak → -20% trough → recovery → pump)
        equity = [
            EquityPoint(time="2022-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2022-07-01T00:00:00+00:00", equity=80_000.0),
            EquityPoint(time="2023-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=150_000.0),
        ]
        stats = compute_stats([], equity_curve=equity)
        assert stats["max_drawdown_pct"] != 0, "fixture must contain a drawdown"
        expected_calmar = stats["cagr"] / abs(stats["max_drawdown_pct"])
        # Output values are rounded (calmar 4dp, cagr 6dp); allow the
        # recomputed ratio to differ by the rounding slack.
        assert stats["calmar_ratio"] == pytest.approx(expected_calmar, abs=2e-4), (
            "calmar_ratio must equal cagr / |max_drawdown_pct| (elapsed-time "
            "definition, shared by both code paths)"
        )

    def test_qs_installed_but_unparseable_times_still_yields_sharpe(self):
        """With QS installed but timestamps unresolvable, Sharpe must not be 0.

        Kills the mutant that disables the manual fall-through: the fallback
        computes Sharpe/Sortino on raw period returns instead of silently
        leaving 0.0.
        """
        equity = [
            EquityPoint(time=f"not-a-timestamp-{i}", equity=100_000.0 * (1.001**i))
            for i in range(40)
        ]
        stats = compute_stats([], equity_curve=equity)
        assert stats["sharpe_ratio"] != 0.0, (
            "unparseable equity timestamps must fall back to the manual Sharpe "
            "path (raw period returns), not silently publish 0.0"
        )


class TestPeriodBounds:
    """The annualization window must cover the whole configured end day."""

    def test_date_only_end_is_extended_to_end_of_day(self):
        start_dt, end_dt = _resolve_backtest_period_bounds(
            equity_curve=[],
            trades=[],
            backtest_start="2024-01-01",
            backtest_end="2024-12-31",
        )

        assert start_dt.isoformat() == "2024-01-01T00:00:00+00:00"
        assert end_dt.isoformat() == "2024-12-31T23:59:59.999999+00:00"

    def test_explicit_end_instant_is_not_extended(self):
        _, end_dt = _resolve_backtest_period_bounds(
            equity_curve=[],
            trades=[],
            backtest_start="2024-01-01",
            backtest_end="2024-12-31T12:00:00+00:00",
        )

        assert end_dt.isoformat() == "2024-12-31T12:00:00+00:00"

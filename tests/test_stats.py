"""Tests for runner.stats — performance metrics calculation."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from runner.stats import (
    _daily_returns_from_equity_curve,
    _daily_returns_series_from_equity_curve,
    _max_consecutive,
    _max_drawdown_pct_manual,
    _period_risk_free_rate,
    _resolve_backtest_period_bounds,
    _sharpe_manual,
    _sortino_manual,
    compute_stats,
    compute_stats_for_result,
)
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

# `compute_stats` publishes `cagr` rounded to six decimal places
# (runner/stats.py: `cagr=round(float(cagr), 6)`), so an exact comparison against
# the unrounded expectation can be off by up to 5e-7. Sit just above that
# rounding floor instead of at it: the tolerance still separates the equity
# window from the trade window (>=1e-5 apart in every CAGR fixture here), which
# the previous 1e-3 tolerance could not.
CAGR_PAYLOAD_TOLERANCE = 1e-6


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


class TestQuantStatsManualParity:
    returns = [0.012, -0.008, 0.004, -0.011, 0.006, 0.002, -0.003, 0.009]

    def test_period_risk_free_rate_uses_compound_conversion(self):
        # Merge resolution: the branch's compound per-period conversion is
        # kept — it is what makes `_sharpe_manual`/`_sortino_manual` bit-identical
        # to quantstats (ADR D5: one definition per metric on both paths), so
        # the symbol is imported at module level again.
        assert _period_risk_free_rate(0.04) == pytest.approx(math.pow(1.04, 1 / 252) - 1)

    def test_manual_sharpe_matches_quantstats_definition(self):
        import pandas as pd

        qs = pytest.importorskip("quantstats.stats")

        series = pd.Series(self.returns, index=pd.date_range("2024-01-01", periods=len(self.returns)))
        expected = float(qs.sharpe(series, rf=0.04, periods=252))

        assert _sharpe_manual(self.returns, 0.04) == pytest.approx(expected, rel=1e-12)

    def test_manual_sortino_matches_quantstats_definition(self):
        import pandas as pd

        qs = pytest.importorskip("quantstats.stats")

        series = pd.Series(self.returns, index=pd.date_range("2024-01-01", periods=len(self.returns)))
        expected = float(qs.sortino(series, rf=0.04, periods=252))

        assert _sortino_manual(self.returns, 0.04) == pytest.approx(expected, rel=1e-12)


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

    def test_trade_metrics_use_net_pnl_after_fees(self):
        winner = _trade(100)
        winner.fees = 20.0
        winner.net_pnl = 80.0
        loser = _trade(-50)
        loser.fees = 10.0
        loser.net_pnl = -60.0

        stats = compute_stats([winner, loser])

        assert stats["winners"] == 1
        assert stats["losers"] == 1
        assert stats["total_pnl"] == 20.0
        assert stats["avg_winner"] == 80.0
        assert stats["avg_loser"] == -60.0
        assert stats["profit_factor"] == pytest.approx(80.0 / 60.0, rel=1e-4)

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
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-9, abs_tol=CAGR_PAYLOAD_TOLERANCE)

    def test_cagr_prefers_equity_period_over_trade_bounds(self):
        """Same contract as `test_cagr_annualizes_with_trade_period_bounds`.

        Merge resolution: the two sides of the merge added this scenario under
        different names. Both keep the shared 731-day equity window, so both are
        retained as separate tests rather than one shadowing the other.
        """
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
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-9, abs_tol=CAGR_PAYLOAD_TOLERANCE)

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

        # 2024-01-01T00:00Z -> 2025-01-01T00:00Z is 366 calendar days.
        expected = (110000.0 / 100000.0) ** (365.25 / 366) - 1
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-9, abs_tol=CAGR_PAYLOAD_TOLERANCE)

    def test_cagr_uses_equity_period_when_available(self):
        """The equity window, not the 365.75-day trade span, drives CAGR.

        Merge resolution: the other side of the merge asserted a 365-day year
        for this window. 2024 is a leap year, so the equity endpoints are 366
        days apart and the trade span is 365.75 — neither is 365.0, so that
        literal could not describe any window this fixture produces. The 366-day
        expectation is kept, and it still proves the equity period is preferred:
        the trade bounds would give 365.75.
        """
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
        assert math.isclose(stats["cagr"], expected, rel_tol=1e-9, abs_tol=CAGR_PAYLOAD_TOLERANCE)

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

    def test_trade_stats_subtract_stored_fees_when_net_pnl_is_absent(self):
        """The gross-minus-fees fallback must subtract, not add, the fee.

        The stored-net path above cannot catch an inverted operator: these
        trades deliberately carry ``net_pnl=None`` so the ledger arithmetic is
        the only thing that decides the winner/loser split.  With a fee of 5 a
        gross -3 trade is a -8 loser; adding the fee would make it a +2 winner
        and flip both the win rate and the total.
        """
        gross_winner = _trade(30.0)
        gross_winner.fees = 10.0
        gross_winner.net_pnl = None
        gross_winner.id = 1
        gross_loser = _trade(-3.0)
        gross_loser.fees = 5.0
        gross_loser.net_pnl = None
        gross_loser.id = 2

        stats = compute_stats([gross_winner, gross_loser])

        # Net: +20, -8 → one winner, one loser, total +12.  With the fee added
        # instead of subtracted the figures would be +40 / +2 (two winners,
        # total +42, profit_factor infinite).
        assert stats["winners"] == 1
        assert stats["losers"] == 1
        assert stats["total_trades"] == 2
        assert stats["total_pnl"] == 12.0
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["avg_winner"] == pytest.approx(20.0)
        assert stats["avg_loser"] == pytest.approx(-8.0)
        assert stats["profit_factor"] == pytest.approx(2.5)
        assert stats["max_consecutive_wins"] == 1
        assert stats["max_consecutive_losses"] == 1

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
        # One return per trading day, INCLUDING the first: the curve's first
        # observation is that day's opening equity, so resampling to the last
        # observation per day must not drop the day it belongs to. The old
        # expectation of 29 pinned exactly that loss.
        assert len(daily_returns) == 30

    def test_emit_supplied_risk_free_rate(self):
        stats = compute_stats([_trade(100)], risk_free_rate=0.0375)
        assert stats["risk_free_rate"] == pytest.approx(0.0375)


class TestFirstTradingDayReturn:
    """Three trading days must produce three daily returns, the first included.

    The daily equity build writes an inception point at the window start
    (00:00) and a close at 23:59:59.999999 of every day. Resampling to the last
    observation of each day dropped that inception point whenever the same day
    also carried a close, so the first day's return never reached the series: a
    first-day loss vanished, Sharpe and Sortino came out too high, and the size
    of the error depended on whether day one happened to be a loss.
    """

    @staticmethod
    def _curve() -> list[EquityPoint]:
        """The exact layout `build_daily_equity_curve` emits."""
        return [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-01T23:59:59.999999+00:00", equity=99_000.0),
            EquityPoint(time="2024-01-02T23:59:59.999999+00:00", equity=99_990.0),
            EquityPoint(time="2024-01-03T23:59:59.999999+00:00", equity=100_989.9),
        ]

    @staticmethod
    def _curve_with_a_first_day_loss() -> list[EquityPoint]:
        """-2% on day one, then +3% and -1%: both ratios are well defined."""
        return [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-01T23:59:59.999999+00:00", equity=98_000.0),
            EquityPoint(time="2024-01-02T23:59:59.999999+00:00", equity=100_940.0),
            EquityPoint(time="2024-01-03T23:59:59.999999+00:00", equity=99_930.6),
        ]

    def test_three_trading_days_produce_three_returns_including_the_first_loss(self):
        series = _daily_returns_series_from_equity_curve(self._curve())

        assert series is not None
        assert len(series) == 3, "the first trading day's return is missing"
        assert series.iloc[0] == pytest.approx(-0.01)
        assert series.iloc[1] == pytest.approx(0.01)
        assert series.iloc[2] == pytest.approx(0.01)

    def test_a_day_with_a_single_observation_gains_no_synthetic_return(self):
        """Only a day that carries an opening AND a close has a return."""
        curve = [
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-02T23:59:59.999999+00:00", equity=101_000.0),
            EquityPoint(time="2024-01-03T23:59:59.999999+00:00", equity=102_000.0),
        ]

        series = _daily_returns_series_from_equity_curve(curve)

        assert series is not None
        assert len(series) == 2
        assert series.iloc[0] == pytest.approx(0.01)

    def test_sharpe_and_sortino_drop_once_the_first_day_is_counted(self):
        """The defect direction: the collapsed series overstates both ratios."""
        series = _daily_returns_series_from_equity_curve(
            self._curve_with_a_first_day_loss()
        )
        assert series is not None
        observed = [float(value) for value in series]
        # What the resample-to-last produced: the two later days only.
        collapsed = [0.03, -0.01]

        assert observed == pytest.approx([-0.02, 0.03, -0.01])
        assert _sharpe_manual(observed, 0.0) < _sharpe_manual(collapsed, 0.0)
        assert _sortino_manual(observed, 0.0) < _sortino_manual(collapsed, 0.0)

    def test_compute_stats_reports_the_sharpe_of_all_three_days(self):
        stats = compute_stats([], equity_curve=self._curve(), risk_free_rate=0.0)

        series = _daily_returns_series_from_equity_curve(self._curve())
        assert series is not None
        assert stats["equity_return_frequency"] == "daily"
        assert stats["sharpe_ratio"] == pytest.approx(
            _sharpe_manual([float(value) for value in series], 0.0), abs=1e-4
        )
        assert stats["sortino_ratio"] == pytest.approx(
            _sortino_manual([float(value) for value in series], 0.0), abs=1e-4
        )


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

    def test_qs_installed_but_unparseable_times_declare_the_ratios_undefined(self):
        """Unparseable timestamps must not publish an annualised ratio.

        **Why this test was rewritten (it pinned wrong behaviour).** The earlier
        version asserted that the unparseable curve published the SAME
        sqrt(252)-annualised Sharpe as the daily one (ADR D5 parity). That
        parity is only true if the bars happen to be daily, which is exactly
        what an unreadable clock makes unknowable: hourly bars annualised with
        252 periods per year overstate Sharpe by ~sqrt(6.5), weekly bars
        understate it. A number that looks measured but rests on a guessed
        period is worse than no number, so the ratios are now declared
        undefined (and not silently published as 0.0 without the flag).

        The p-value is a t-test on the raw observation returns and does not
        depend on the period, so it stays defined and equal to the daily one.
        """
        returns = [
            0.012, -0.008, 0.004, -0.011, 0.006, 0.002,
            -0.003, 0.009, 0.001, -0.002, 0.005, -0.001,
        ]

        def _equities():
            values = [100_000.0]
            for value in returns:
                values.append(values[-1] * (1 + value))
            return values

        start = datetime(2021, 1, 1, tzinfo=timezone.utc)
        unparseable = [
            EquityPoint(time=f"not-a-timestamp-{index}", equity=equity)
            for index, equity in enumerate(_equities())
        ]
        daily = [
            EquityPoint(
                time=(start + timedelta(days=index)).isoformat(), equity=equity
            )
            for index, equity in enumerate(_equities())
        ]

        unknown = compute_stats([], equity_curve=unparseable, risk_free_rate=0.04)
        daily_stats = compute_stats([], equity_curve=daily, risk_free_rate=0.04)

        assert daily_stats["equity_return_frequency"] == "daily"
        assert unknown["equity_return_frequency"].startswith("unknown")
        assert daily_stats["sharpe_ratio"] == pytest.approx(
            _sharpe_manual(returns, 0.04), abs=1e-4
        )
        assert "sharpe_ratio" not in daily_stats["undefined_metrics"]
        for metric in ("sharpe_ratio", "sortino_ratio", "psr"):
            assert metric in unknown["undefined_metrics"], metric
        assert unknown["sharpe_ratio"] == 0.0
        assert unknown["sortino_ratio"] == 0.0
        assert "p_value" not in unknown["undefined_metrics"]
        assert unknown["p_value"] == pytest.approx(daily_stats["p_value"], abs=1e-6)

    def test_unknown_frequency_states_why_the_period_assumption_is_unverified(self):
        """`equity_return_frequency` must name the reason, not just "unknown"."""
        unparseable = [
            EquityPoint(time=f"not-a-timestamp-{index}", equity=100_000.0 * (1.001**index))
            for index in range(40)
        ]

        stats = compute_stats([], equity_curve=unparseable, risk_free_rate=0.04)

        assert stats["equity_return_frequency"] == (
            "unknown:unparseable_equity_timestamps"
        ), (
            "the payload must say the equity curve had no readable clock, so a "
            "consumer can see the annualisation was a stated assumption rather "
            "than a fact read off the data"
        )
        # A known frequency stays a bare token, so existing consumers keep
        # matching on it.
        assert stats["undefined_metrics"] is not None
        known = _equity_curve_from_returns([0.01, -0.005, 0.004] * 4)
        assert compute_stats([], equity_curve=known)["equity_return_frequency"] == "daily"

    def test_describe_return_frequency_is_a_no_op_without_a_reason(self):
        from runner.stats import (
            UNKNOWN_RETURN_FREQUENCY,
            _describe_return_frequency,
        )

        assert _describe_return_frequency("daily", None) == "daily"
        assert _describe_return_frequency("none", None) == "none"
        assert _describe_return_frequency(UNKNOWN_RETURN_FREQUENCY, "why") == "unknown:why"

    def test_sharpe_manual_has_no_annualization_knob(self):
        """The per-period variant is unreachable, not merely unused.

        ``annualization_factor=1.0`` is what published a per-observation ratio
        under the name "Sharpe". Leaving the parameter in place (unused, with a
        default) invites the same call back, so its absence is the fix.
        """
        import inspect

        from runner.stats import _sortino_manual

        for helper in (_sharpe_manual, _sortino_manual):
            assert "annualization_factor" not in inspect.signature(helper).parameters

        # And the only ratio these helpers can produce is the annualized one:
        # the per-period variant is off by exactly sqrt(252).
        returns = [0.012, -0.008, 0.004, -0.011, 0.006, 0.002]
        annualized = _sharpe_manual(returns, 0.0)
        assert annualized == pytest.approx(
            (sum(returns) / len(returns))
            / (
                sum(
                    (value - sum(returns) / len(returns)) ** 2 for value in returns
                )
                / (len(returns) - 1)
            )
            ** 0.5
            * math.sqrt(252)
        )
        assert annualized != pytest.approx(annualized / math.sqrt(252))


class TestWithoutQuantStats:
    """The fallback path a bare install runs.

    `runner.stats` branches on `HAS_QUANTSTATS`; with quantstats installed the
    manual branch is dead code in this environment, so a regression there would
    only surface on a machine that lacks the optional dependency. These tests
    force the flag off and pin the same parity contract as the QS path.
    """

    @staticmethod
    def _compute_without_quantstats(monkeypatch, **kwargs):
        import runner.stats as stats_module

        monkeypatch.setattr(stats_module, "HAS_QUANTSTATS", False)
        return stats_module.compute_stats(**kwargs)

    def test_fallback_matches_the_quantstats_definition_for_a_daily_curve(self, monkeypatch):
        """No optional dependency must not mean a different Sharpe.

        Both branches are documented to publish the daily-resampled, 252-period
        annualized ratio. Force the flag off and compare against the manual
        helpers directly: a fallback that silently returned the default 0.0
        would pass any "is a number" assertion but not this one.
        """
        returns = [0.012, -0.008, 0.004, -0.011, 0.006, 0.002, -0.003, 0.009]
        curve = _equity_curve_from_returns(returns)

        stats = self._compute_without_quantstats(
            monkeypatch, trades=[], equity_curve=curve, risk_free_rate=0.04
        )

        assert stats["equity_return_frequency"] == "daily"
        # The payload rounds `sharpe_ratio` to four decimals (stats.py:262),
        # so the tolerance is that rounding floor, not the float equality.
        assert stats["sharpe_ratio"] == pytest.approx(
            _sharpe_manual(returns, 0.04), abs=1e-4
        )
        assert stats["sortino_ratio"] == pytest.approx(
            _sortino_manual(returns, 0.04), abs=1e-4
        )
        assert "sharpe_ratio" not in stats["undefined_metrics"]

    def test_fallback_declares_the_ratios_undefined_when_the_clock_is_unparseable(
        self, monkeypatch
    ):
        """The no-quantstats path must agree with the quantstats path.

        Same contract as the quantstats-installed test above, on the branch a
        bare install executes: no period can be read, so no annualised ratio is
        published (it used to be sqrt(252)-annualised on the raw bars, wrong for
        any non-daily spacing).
        """
        returns = [
            0.012, -0.008, 0.004, -0.011, 0.006, 0.002,
            -0.003, 0.009, 0.001, -0.002, 0.005, -0.001,
        ]
        values = [100_000.0]
        for value in returns:
            values.append(values[-1] * (1 + value))
        unparseable = [
            EquityPoint(time=f"not-a-timestamp-{index}", equity=equity)
            for index, equity in enumerate(values)
        ]

        stats = self._compute_without_quantstats(
            monkeypatch, trades=[], equity_curve=unparseable, risk_free_rate=0.04
        )

        assert stats["equity_return_frequency"] == (
            "unknown:unparseable_equity_timestamps"
        )
        for metric in ("sharpe_ratio", "sortino_ratio", "psr"):
            assert metric in stats["undefined_metrics"], metric
        assert stats["sharpe_ratio"] == 0.0
        assert stats["sortino_ratio"] == 0.0
        assert "p_value" not in stats["undefined_metrics"]

    def test_fallback_declares_metrics_undefined_for_a_one_point_curve(self, monkeypatch):
        """One observation cannot produce a ratio; the payload must say so.

        `undefined_metrics` is the machine-readable half of the contract, so an
        empty set here would let a consumer read the default 0.0 as a real
        measurement.
        """
        curve = [EquityPoint(time="2021-01-01T00:00:00+00:00", equity=100_000.0)]

        stats = self._compute_without_quantstats(
            monkeypatch, trades=[], equity_curve=curve, risk_free_rate=0.04
        )

        assert stats["undefined_metrics"] is not None
        for metric in ("sharpe_ratio", "sortino_ratio", "psr", "p_value"):
            assert metric in stats["undefined_metrics"]

    def test_fallback_without_a_curve_marks_the_whole_metric_set_undefined(self, monkeypatch):
        """No equity curve at all is the extreme case of "cannot be computed"."""
        stats = self._compute_without_quantstats(monkeypatch, trades=[], equity_curve=[])

        assert stats["equity_return_frequency"] == "none"
        assert stats["undefined_metrics"] is not None
        for metric in (
            "max_drawdown_pct",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "cagr",
            "total_return",
            "psr",
            "p_value",
        ):
            assert metric in stats["undefined_metrics"]


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


def _moments_curve(daily_equities: list[float]) -> list:
    import pandas as pd

    from shared.models import EquityPoint

    days = pd.date_range("2024-01-01", periods=len(daily_equities), freq="D", tz="UTC")
    return [
        EquityPoint(time=day.isoformat(), equity=value)
        for day, value in zip(days, daily_equities)
    ]


def test_daily_returns_moments_match_a_hand_built_series():
    """Issue #164: the moments feed the deflated Sharpe ratio."""
    from runner.stats import _daily_returns_series_from_equity_curve, daily_returns_moments

    curve = _moments_curve([100.0, 101.0, 100.5, 102.0, 101.0, 103.5, 103.0, 105.0])
    returns = _daily_returns_series_from_equity_curve(curve)
    values = [float(v) for v in returns]
    n = len(values)
    mean = sum(values) / n
    sample_std = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5

    moments = daily_returns_moments(curve)

    assert moments["count"] == n
    assert moments["sharpe_per_obs"] == pytest.approx(mean / sample_std)
    assert moments["skew"] == pytest.approx(float(returns.skew()))
    assert moments["kurtosis"] == pytest.approx(float(returns.kurtosis()) + 3.0)
    # Not annualised: far below the sqrt(252)-scaled figure.
    assert abs(moments["sharpe_per_obs"]) < abs(mean / sample_std) * 252**0.5


def test_daily_returns_moments_agree_with_the_reported_psr():
    """One trial, no selection: DSR from these moments equals the stats PSR."""
    from runner.optimization_diagnostics import deflated_sharpe_ratio
    from runner.stats import (
        _daily_returns_series_from_equity_curve,
        _probabilistic_sharpe,
        daily_returns_moments,
    )

    equities = [100.0]
    for step in [0.4, -0.2, 0.7, 0.1, -0.5, 0.9, 0.3, -0.1, 0.6, 0.2, -0.3, 0.8]:
        equities.append(equities[-1] * (1 + step / 100))
    curve = _moments_curve(equities)
    moments = daily_returns_moments(curve)
    dsr = deflated_sharpe_ratio(
        sr=moments["sharpe_per_obs"],
        t=moments["count"],
        skew=moments["skew"],
        kurt=moments["kurtosis"],
        n=1,
        var_sr=0.0,
    )
    psr = _probabilistic_sharpe(_daily_returns_series_from_equity_curve(curve), 0.0)
    assert psr is not None
    assert dsr == pytest.approx(psr)


def test_daily_returns_moments_undefined_cases_return_none():
    from runner.stats import daily_returns_moments

    assert daily_returns_moments([]) is None
    assert daily_returns_moments(_moments_curve([100.0])) is None
    assert daily_returns_moments(_moments_curve([100.0, 100.0, 100.0, 100.0, 100.0])) is None

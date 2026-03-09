"""Tests for runner.stats — performance metrics calculation."""

from datetime import datetime, timedelta, timezone
import math

from runner.stats import compute_stats, compute_stats_for_result, _max_consecutive, _max_drawdown_pct_manual
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

    def test_calmar_ratio_zeroed_for_tiny_drawdown(self):
        equity = _equity_curve_from_returns([0.0200, -0.0005, 0.0150, 0.0100])

        stats = compute_stats([], equity_curve=equity)

        assert abs(stats["max_drawdown_pct"]) < 0.001
        assert stats["calmar_ratio"] == 0.0


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

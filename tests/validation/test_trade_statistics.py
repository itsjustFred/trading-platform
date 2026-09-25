"""Tests for validation.stats.calculator — compute_trade_statistics."""

from __future__ import annotations

import math

import pytest

from shared.models import Trade, TradeExit
from validation.stats import TradeStatistics, compute_trade_statistics
from validation.stats.calculator import _annualize_factor, _trade_span_years


def _make_trade(trade_id: int, pnls: list[float]) -> Trade:
    """Helper to build a Trade with partial exits whose pnl values sum to the desired amount."""
    exits = [
        TradeExit(time=f"2024-01-{i + 1:02d}T00:00:00Z", price=100.0, quantity=1.0, pnl=p)
        for i, p in enumerate(pnls)
    ]
    total_pnl = sum(pnls)
    return Trade(
        id=trade_id,
        symbol="TEST",
        direction="LONG",
        entry_time="2024-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=1.0,
        exits=exits,
        pnl=total_pnl,
    )


# ── 1. Empty list ──────────────────────────────────────────────────────────


def test_empty_list_returns_defaults() -> None:
    stats = compute_trade_statistics([])
    assert stats == TradeStatistics()
    assert stats.total_trades == 0
    assert stats.win_rate == 0.0
    assert stats.net_profit == 0.0


# ── 2. Single trade scenarios ──────────────────────────────────────────────


def test_single_winner() -> None:
    trades = [_make_trade(1, [100.0])]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 1
    assert stats.winning_trades == 1
    assert stats.losing_trades == 0
    assert stats.win_rate == pytest.approx(1.0)
    assert stats.net_profit == pytest.approx(100.0)
    assert stats.gross_profit == pytest.approx(100.0)
    assert stats.gross_loss == pytest.approx(0.0)
    assert stats.profit_factor == float("inf")


def test_single_loser() -> None:
    trades = [_make_trade(1, [-50.0])]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 1
    assert stats.winning_trades == 0
    assert stats.losing_trades == 1
    assert stats.win_rate == pytest.approx(0.0)
    assert stats.net_profit == pytest.approx(-50.0)
    assert stats.gross_profit == pytest.approx(0.0)
    assert stats.gross_loss == pytest.approx(-50.0)
    assert stats.profit_factor == pytest.approx(0.0)
    assert stats.avg_loser == pytest.approx(-50.0)


# ── 3. Mixed scenarios ─────────────────────────────────────────────────────


def test_mixed_trades() -> None:
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-50.0]),
        _make_trade(3, [30.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 3
    assert stats.winning_trades == 2
    assert stats.losing_trades == 1
    assert stats.win_rate == pytest.approx(2 / 3)
    assert stats.net_profit == pytest.approx(80.0)
    assert stats.gross_profit == pytest.approx(130.0)
    assert stats.gross_loss == pytest.approx(-50.0)
    assert stats.profit_factor == pytest.approx(130.0 / 50.0)


def test_all_winners() -> None:
    trades = [_make_trade(i, [10.0]) for i in range(5)]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 5
    assert stats.winning_trades == 5
    assert stats.losing_trades == 0
    assert stats.win_rate == pytest.approx(1.0)
    assert stats.profit_factor == float("inf")
    assert stats.avg_winner == pytest.approx(10.0)
    assert stats.avg_loser == 0.0


def test_all_losers() -> None:
    trades = [_make_trade(i, [-20.0]) for i in range(4)]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 4
    assert stats.winning_trades == 0
    assert stats.losing_trades == 4
    assert stats.win_rate == pytest.approx(0.0)
    assert stats.profit_factor == pytest.approx(0.0)
    assert stats.avg_winner == 0.0
    assert stats.avg_loser == pytest.approx(-20.0)


# ── 4. Net / gross profit & loss ───────────────────────────────────────────


def test_net_profit_zero() -> None:
    trades = [_make_trade(1, [50.0]), _make_trade(2, [-50.0])]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(0.0)


def test_gross_profit_and_loss() -> None:
    trades = [_make_trade(1, [200.0]), _make_trade(2, [-80.0]), _make_trade(3, [50.0])]
    stats = compute_trade_statistics(trades)
    assert stats.gross_profit == pytest.approx(250.0)
    assert stats.gross_loss == pytest.approx(-80.0)


# ── 5. Win rate variants ───────────────────────────────────────────────────


def test_win_rate_half() -> None:
    trades = [_make_trade(i, [10.0] if i % 2 == 0 else [-5.0]) for i in range(10)]
    stats = compute_trade_statistics(trades)
    assert stats.win_rate == pytest.approx(0.5)


def test_win_rate_one_third() -> None:
    trades = [
        _make_trade(1, [10.0]),
        _make_trade(2, [-5.0]),
        _make_trade(3, [-5.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.win_rate == pytest.approx(1 / 3)


# ── 6. Profit factor edge cases ────────────────────────────────────────────


def test_profit_factor_normal() -> None:
    trades = [_make_trade(1, [100.0]), _make_trade(2, [-25.0])]
    stats = compute_trade_statistics(trades)
    assert stats.profit_factor == pytest.approx(4.0)


def test_profit_factor_inf_when_no_losses() -> None:
    trades = [_make_trade(1, [50.0])]
    stats = compute_trade_statistics(trades)
    assert stats.profit_factor == float("inf")


# ── 7. Drawdown (absolute & pct) ───────────────────────────────────────────


def test_max_drawdown_absolute() -> None:
    # Sequence: +100, -40, -30, +50, -20
    # Cumulative: 100, 60, 30, 80, 60
    # Peak: 100, DD: 0, 40, 70, 0, 40  -> max = 70
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-40.0]),
        _make_trade(3, [-30.0]),
        _make_trade(4, [50.0]),
        _make_trade(5, [-20.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.max_drawdown == pytest.approx(70.0)


def test_max_drawdown_pct() -> None:
    # Same sequence — max DD% = 70 / 100 = 0.7
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-40.0]),
        _make_trade(3, [-30.0]),
        _make_trade(4, [50.0]),
        _make_trade(5, [-20.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.max_drawdown_pct == pytest.approx(0.7)


def test_max_drawdown_uses_first_observed_equity_peak() -> None:
    trades = [_make_trade(i, [-10.0]) for i in range(3)]
    stats = compute_trade_statistics(trades)
    assert stats.max_drawdown == pytest.approx(30.0)
    assert stats.max_drawdown_pct == pytest.approx(2.0)


def test_max_drawdown_new_higher_peak() -> None:
    # +50, -10, +100, -30
    # Cumulative: 50, 40, 140, 110
    # Peak: 50, 50, 140, 140
    # DD: 0, 10, 0, 30 -> max_dd = 30, max_dd_pct = 30/140 ≈ 0.214
    trades = [
        _make_trade(1, [50.0]),
        _make_trade(2, [-10.0]),
        _make_trade(3, [100.0]),
        _make_trade(4, [-30.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.max_drawdown == pytest.approx(30.0)
    assert stats.max_drawdown_pct == pytest.approx(30.0 / 140.0)


# ── 8. Sharpe ratio ────────────────────────────────────────────────────────


def test_sharpe_zero_with_no_trades() -> None:
    stats = compute_trade_statistics([])
    assert stats.sharpe_ratio == 0.0


def test_sharpe_zero_with_single_trade() -> None:
    trades = [_make_trade(1, [10.0])]
    stats = compute_trade_statistics(trades)
    assert stats.sharpe_ratio == 0.0


def test_sharpe_constant_returns() -> None:
    # All returns identical → std = 0 → sharpe = 0
    trades = [_make_trade(i, [10.0]) for i in range(5)]
    stats = compute_trade_statistics(trades)
    assert stats.sharpe_ratio == 0.0


def test_sharpe_mixed_trades() -> None:
    # returns: 100, -50, 30, -20, 40
    # mean = 20, variance = ((80^2 + (-70)^2 + 10^2 + (-40)^2 + 20^2) / 5) = 13400/5 = 2680
    # std = sqrt(2680) ≈ 51.7687
    # sharpe = (20 / 51.7687) * sqrt(252) ≈ 6.133
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-50.0]),
        _make_trade(3, [30.0]),
        _make_trade(4, [-20.0]),
        _make_trade(5, [40.0]),
    ]
    stats = compute_trade_statistics(trades)
    expected = (20.0 / math.sqrt(2680.0)) * math.sqrt(252.0)
    assert stats.sharpe_ratio == pytest.approx(expected, rel=1e-4)


# ── 9. Sortino ratio ───────────────────────────────────────────────────────


def test_sortino_zero_with_no_downside() -> None:
    trades = [_make_trade(i, [10.0]) for i in range(5)]
    stats = compute_trade_statistics(trades)
    assert stats.sortino_ratio == 0.0


def test_sortino_zero_with_single_trade() -> None:
    trades = [_make_trade(1, [-10.0])]
    stats = compute_trade_statistics(trades)
    assert stats.sortino_ratio == 0.0


def test_sortino_mixed_trades() -> None:
    # returns: 100, -50, 30, -20, 40
    # mean = 20
    # downside = [-50, -20], downside_var = (2500 + 400)/2 = 1450, downside_std = sqrt(1450)
    # sortino = (20 / sqrt(1450)) * sqrt(252)
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-50.0]),
        _make_trade(3, [30.0]),
        _make_trade(4, [-20.0]),
        _make_trade(5, [40.0]),
    ]
    stats = compute_trade_statistics(trades)
    expected = (20.0 / math.sqrt(1450.0)) * math.sqrt(252.0)
    assert stats.sortino_ratio == pytest.approx(expected, rel=1e-4)


# ── 10. Recovery factor ────────────────────────────────────────────────────


def test_recovery_factor_normal() -> None:
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-40.0]),
        _make_trade(3, [-30.0]),
        _make_trade(4, [50.0]),
    ]
    stats = compute_trade_statistics(trades)
    # net = 80, max_dd = 70
    assert stats.recovery_factor == pytest.approx(80.0 / 70.0)


def test_recovery_factor_inf_when_no_drawdown() -> None:
    trades = [_make_trade(1, [10.0])]
    stats = compute_trade_statistics(trades)
    assert stats.recovery_factor == float("inf")


# ── 11. Multi-exit trades ──────────────────────────────────────────────────


def test_multi_exit_trade() -> None:
    # One trade with two partial exits: +30 and +20
    trade = _make_trade(1, [30.0, 20.0])
    stats = compute_trade_statistics([trade])
    assert stats.total_trades == 1
    assert stats.winning_trades == 1
    assert stats.net_profit == pytest.approx(50.0)
    assert stats.gross_profit == pytest.approx(50.0)


def test_multi_exit_mixed_exit_pnls() -> None:
    # One trade: +60, -20  → net +40
    trade = _make_trade(1, [60.0, -20.0])
    stats = compute_trade_statistics([trade])
    assert stats.net_profit == pytest.approx(40.0)
    assert stats.winning_trades == 1
    assert stats.gross_profit == pytest.approx(40.0)


# ── 12. Trade with no exits ────────────────────────────────────────────────


def test_trade_with_no_exits() -> None:
    trade = Trade(
        id=1,
        symbol="TEST",
        direction="LONG",
        entry_time="2024-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=1.0,
        exits=[],
        pnl=0.0,
    )
    stats = compute_trade_statistics([trade])
    assert stats.total_trades == 1
    assert stats.winning_trades == 0
    assert stats.losing_trades == 0
    assert stats.net_profit == 0.0


# ── 13. Average metrics ────────────────────────────────────────────────────


def test_avg_winner_and_loser() -> None:
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [200.0]),
        _make_trade(3, [-50.0]),
        _make_trade(4, [-30.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.avg_winner == pytest.approx(150.0)
    assert stats.avg_loser == pytest.approx(-40.0)
    assert stats.avg_trade == pytest.approx((300.0 - 80.0) / 4)


# ── 14. Largest winner / loser ─────────────────────────────────────────────


def test_largest_winner_and_loser() -> None:
    trades = [
        _make_trade(1, [100.0]),
        _make_trade(2, [-200.0]),
        _make_trade(3, [50.0]),
        _make_trade(4, [-20.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.largest_winner == pytest.approx(100.0)
    assert stats.largest_loser == pytest.approx(-200.0)


# ── 15. Approx / float tolerances ──────────────────────────────────────────


def test_float_tolerance_approx() -> None:
    trades = [
        _make_trade(1, [33.3333]),
        _make_trade(2, [-16.6667]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(16.6666, abs=1e-3)
    assert stats.win_rate == pytest.approx(0.5, abs=1e-6)


# ── 16. Break-even trades (pnl == 0) ───────────────────────────────────────


def test_break_even_trade_not_counted_as_winner_or_loser() -> None:
    trades = [
        _make_trade(1, [10.0]),
        _make_trade(2, [0.0]),
        _make_trade(3, [-5.0]),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 3
    assert stats.winning_trades == 1
    assert stats.losing_trades == 1
    assert stats.win_rate == pytest.approx(1 / 3)


# ── 17. Fees / net PnL (DV-B46) ────────────────────────────────────────────


def _make_fee_trade(trade_id: int, exit_pnl: float, fees: float | None, net_pnl: float | None = None) -> Trade:
    exits = [TradeExit(time="2024-01-01T00:00:00Z", price=100.0, quantity=1.0, pnl=exit_pnl)]
    total = exit_pnl
    return Trade(
        id=trade_id,
        symbol="TEST",
        direction="LONG",
        entry_time="2024-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=1.0,
        exits=exits,
        pnl=total,
        fees=fees,
        net_pnl=net_pnl,
    )


def test_net_pnl_preferred_over_gross_exits() -> None:
    trades = [
        _make_fee_trade(1, 100.0, fees=2.0, net_pnl=98.0),
        _make_fee_trade(2, -50.0, fees=2.0, net_pnl=-52.0),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(46.0)
    assert stats.gross_profit == pytest.approx(100.0)
    assert stats.gross_loss == pytest.approx(-50.0)
    assert stats.avg_trade == pytest.approx(23.0)


def test_fees_subtracted_from_gross_when_no_net() -> None:
    trades = [_make_fee_trade(1, 100.0, fees=2.0)]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(98.0)
    assert stats.gross_profit == pytest.approx(100.0)


def test_net_aggregates_follow_net_winner_classification() -> None:
    trades = [
        _make_fee_trade(1, 1.0, fees=5.0),
        _make_fee_trade(2, 10.0, fees=1.0),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.winning_trades == 1
    assert stats.losing_trades == 1
    assert stats.profit_factor == pytest.approx(9.0 / 4.0)
    assert stats.avg_winner == pytest.approx(9.0)
    assert stats.avg_loser == pytest.approx(-4.0)
    assert stats.gross_profit == pytest.approx(11.0)
    assert stats.gross_loss == pytest.approx(0.0)


    trades = [_make_fee_trade(1, 10.0, fees=1.0)]
    stats = compute_trade_statistics(trades)
    assert stats.winning_trades == 1
    assert stats.losing_trades == 0
    assert stats.win_rate == pytest.approx(1.0)


def test_fees_can_turn_gross_winner_into_net_loser() -> None:
    trades = [_make_fee_trade(1, 1.0, fees=5.0)]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(-4.0)
    assert stats.winning_trades == 0
    assert stats.losing_trades == 1


def test_no_fees_means_gross_equals_net() -> None:
    trades = [_make_fee_trade(1, 100.0, fees=None)]
    stats = compute_trade_statistics(trades)
    assert stats.net_profit == pytest.approx(100.0)


def test_break_even_counted_in_denominator() -> None:
    """DV-B46 (denominator skew): a break-even trade counts in total_trades."""
    trades = [
        _make_fee_trade(1, 10.0, fees=None),
        _make_fee_trade(2, 0.0, fees=None),
        _make_fee_trade(3, -5.0, fees=None),
    ]
    stats = compute_trade_statistics(trades)
    assert stats.total_trades == 3
    assert stats.win_rate == pytest.approx(1 / 3)


# ── Mutation hardening (cycle 1: A-L4 annualization + Sharpe guards) ───────


def _make_dated_trade(trade_id: int, entry: str, exit_: str, pnl: float) -> Trade:
    return Trade(
        id=trade_id,
        symbol="TEST",
        direction="LONG",
        entry_time=entry,
        entry_price=100.0,
        quantity=1.0,
        exits=[TradeExit(time=exit_, price=100.0, quantity=1.0, pnl=pnl)],
        pnl=pnl,
    )


def test_sharpe_two_trades_is_nonzero() -> None:
    """The n<2 Sharpe guard must not swallow a 2-trade series."""
    trades = [_make_trade(1, [100.0]), _make_trade(2, [-50.0])]
    stats = compute_trade_statistics(trades)
    # mean=25, population std=75, undatable span -> sqrt(252) fallback.
    expected = (25.0 / 75.0) * math.sqrt(252.0)
    assert stats.sharpe_ratio == pytest.approx(expected, rel=1e-4)


def test_sharpe_uses_trade_frequency_annualization() -> None:
    """#84: dated trades Sharpe on the daily-resampled net series + sqrt(252).

    12 monthly exits over the Jan-01 -> Dec-02 calendar span: flat days
    contribute 0.0, like the daily-resampled equity definition in
    runner/stats.py.
    """
    import statistics

    pnls = [100.0, -50.0] * 6
    trades = [
        _make_dated_trade(
            i + 1, f"2024-{i + 1:02d}-01T00:00:00Z", f"2024-{i + 1:02d}-02T00:00:00Z", p
        )
        for i, p in enumerate(pnls)
    ]
    stats = compute_trade_statistics(trades)
    daily = [0.0] * 337  # Jan-01 .. Dec-02 inclusive
    for i, p in enumerate(pnls):
        # Exit dates: 2024-MM-02; day offsets from Jan-01 within the span.
        import datetime as _dt

        exit_day = (_dt.date(2024, i + 1, 2) - _dt.date(2024, 1, 1)).days
        daily[exit_day] += p
    expected = (statistics.fmean(daily) / statistics.pstdev(daily)) * math.sqrt(252.0)
    assert stats.sharpe_ratio == pytest.approx(expected, rel=1e-4)


def test_sharpe_falls_back_for_denser_than_daily_trades() -> None:
    """#84: 300 same-span exits collapse to 2 daily observations + sqrt(252)."""
    pnls = [10.0, -9.0] * 150
    trades = [
        _make_dated_trade(
            i + 1, "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", p
        )
        for i, p in enumerate(pnls)
    ]
    stats = compute_trade_statistics(trades)
    # All exits land on Jan-02: daily series [0.0, 150.0].
    expected = (75.0 / 75.0) * math.sqrt(252.0)
    assert stats.sharpe_ratio == pytest.approx(expected, rel=1e-4)


# ── 18. Trade-span counts distinct dated trades (coderabbit cycle 2) ─────────


def test_span_needs_two_dated_trades_not_two_timestamps() -> None:
    """One trade with entry+exit dated is a SINGLE dated trade -> span None.

    Regression: ``_trade_span_years`` counted timestamps, so a lone trade
    contributing both ``entry_time`` and ``last_exit_time`` produced a span
    (its own holding time) and ``_annualize_factor`` annualized 1 trade over
    it instead of falling back to sqrt(252).
    """
    trade = _make_dated_trade(1, "2024-01-01T00:00:00Z", "2024-07-01T00:00:00Z", 100.0)
    assert _trade_span_years([trade]) is None
    assert _annualize_factor([trade]) == pytest.approx(math.sqrt(252.0))


def test_span_two_dated_trades_still_spans() -> None:
    """#84: the annualization contract is daily-resample + sqrt(252)."""
    trades = [
        _make_dated_trade(1, "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", 10.0),
        _make_dated_trade(2, "2024-07-01T00:00:00Z", "2024-07-02T00:00:00Z", -5.0),
    ]
    span = _trade_span_years(trades)
    assert span is not None and span > 0
    assert _annualize_factor(trades) == pytest.approx(math.sqrt(252.0))

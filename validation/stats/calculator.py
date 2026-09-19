"""Trade statistics calculator — pure functional metrics from a list of Trades.

This is the single canonical statistics implementation for the validation
framework (the parallel ``validation/statistics/`` module was removed).

Mathematical conventions (kept explicit so results are reproducible):

* **Standard deviation:** *population* std (divide by ``n``), risk-free rate 0.
* **Sharpe:** ``mean(returns) / population_std(returns) * sqrt(P)`` where the
  "returns" series is the per-trade realized PnL sequence and ``P`` is the
  number of trades per year derived from the trade timestamps
  (``n_trades / span_years``) — compatible with ``runner/stats.py``, which
  annualizes daily equity returns with 252 periods. When the timestamps
  cannot support a trade-frequency annualization (fewer than 2 dated
  trades, zero span, or more than one trade per day on average — e.g.
  intraday or synthetic fixtures), it falls back to ``sqrt(252)``.
* **Sortino:** ``mean(returns) / downside_deviation * sqrt(P)`` where the
  downside deviation divides the sum of squared *negative* returns by the count
  of *negative* returns only (same negatives-only convention as
  ``runner/stats.py`` ``_sortino_manual``).
* **Drawdown:** absolute is in account currency; ``max_drawdown_pct`` is a
  fraction (0..1) of the running peak.
* **PnL basis (DV-B46):** win/loss classification, ``net_profit``, Sharpe,
  Sortino, drawdown, ``avg_trade`` and the largest winner/loser use the
  *net* per-trade PnL (``net_pnl`` when set, else gross PnL minus ``fees``,
  else the exit PnL sum).  ``gross_profit``/``gross_loss``/``avg_winner``/
  ``avg_loser`` stay gross so they remain comparable with MT5's
  Bruttogewinn/Bruttoverlust labels.
"""

from __future__ import annotations

import logging
import math
from typing import List

from pydantic import BaseModel

from shared.models import Trade, TradeExit

logger = logging.getLogger(__name__)

# Tolerance for the "net_pnl consistent with exit PnLs" invariant check: a
# few float ulps of drift are fine, a systematic fee double count is not.
_NET_PNL_EPSILON = 1e-6

# Absolute tolerance for "is this float effectively zero" guards (S1244):
# exact ``== 0.0`` on floats is fragile (accumulated rounding can leave a
# 1e-16 residue), while a 1e-12 band is far below any economically meaningful
# PnL/std magnitude in this module, so pinned test expectations are unchanged.
_ZERO_TOL = 1e-12


class TradeStatistics(BaseModel):
    """Canonical trade-based performance statistics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    recovery_factor: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    avg_trade: float = 0.0
    largest_winner: float = 0.0
    largest_loser: float = 0.0


def _realized_pnl(trade: Trade) -> float:
    """Sum PnL over all exits; no exits → 0."""
    if not trade.exits:
        return 0.0
    return sum((exit_.pnl or 0.0) for exit_ in trade.exits)


def _net_realized_pnl(trade: Trade) -> float:
    """Net realized PnL for one trade after fees (DV-B46).

    Preference order: explicit ``net_pnl`` (the trade already carries the
    net figure), else gross PnL minus ``fees``, else the sum of exit PnLs
    (fee-free trades and normalized inputs where fees are already deducted
    from the exit PnLs).

    ``net_pnl`` is only trusted when it is consistent with the exit PnLs:
    importers must store exit PnLs *gross of fees* whenever ``fees`` is set,
    so ``net_pnl == sum(exit PnLs) - fees``.  A trade that violates this
    invariant (fee-inclusive exit PnLs *plus* a fees field) would double
    count, so it falls back to the recomputed gross-minus-fees figure.
    """
    gross = _realized_pnl(trade)
    recomputed = gross - float(trade.fees) if trade.fees is not None else gross
    if trade.net_pnl is not None:
        net = float(trade.net_pnl)
        if trade.fees is None or abs(net - recomputed) <= _NET_PNL_EPSILON:
            return net
        logger.warning(
            "Trade %s: net_pnl %s inconsistent with exit PnLs gross %s and "
            "fees %s; using recomputed %s",
            trade.id,
            net,
            gross,
            trade.fees,
            recomputed,
        )
    return recomputed


def _compute_drawdowns(realized_pnls: List[float]) -> tuple[float, float]:
    """Return (max_drawdown_absolute, max_drawdown_pct) from trade PnLs."""
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0

    for pnl in realized_pnls:
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = dd / peak
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return max_dd, max_dd_pct


def _trade_span_years(trades: List[Trade]) -> float | None:
    """Calendar span covered by the trades, in years (None when undatable)."""
    stamps: list[pd.Timestamp] = []
    dated_trades = 0
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return None
    for trade in trades:
        trade_dated = False
        for raw in (trade.entry_time, trade.last_exit_time):
            if raw is None:
                continue
            ts = pd.to_datetime(str(raw), utc=True, errors="coerce")
            if not pd.isna(ts):
                stamps.append(ts)
                # One trade = one dated trade even when it contributes
                # both an entry and an exit timestamp (A-L4 annualizes
                # per TRADE, n_trades / span_years).
                trade_dated = True
        if trade_dated:
            dated_trades += 1
    if dated_trades < 2:
        return None
    span_days = (max(stamps) - min(stamps)).total_seconds() / 86400.0
    if span_days <= 0:
        return None
    return span_days / 365.25


def _annualize_factor(trades: List[Trade]) -> float:
    """Trades-per-year annualization factor (A-L4).

    ``sqrt(n_trades / span_years)`` — the trade-based analogue of the
    ``sqrt(252)`` daily annualization in ``runner/stats.py``. Falls back to
    ``sqrt(252)`` when the timestamps cannot support it (undatable, zero
    span, or denser than one trade per day — the per-trade Sharpe has no
    defined annualization there, so the legacy daily-trade assumption
    applies).
    """
    span_years = _trade_span_years(trades)
    if span_years is None or span_years <= 0:
        return math.sqrt(252)
    periods_per_year = len(trades) / span_years
    if not 1.0 <= periods_per_year <= 252.0:
        return math.sqrt(252)
    return math.sqrt(periods_per_year)


def _sharpe(returns: List[float], annualize_factor: float | None = None) -> float:
    """Annualized Sharpe ratio (population std, risk-free rate = 0)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_return = sum(returns) / n
    variance = sum((r - mean_return) ** 2 for r in returns) / n
    std = math.sqrt(variance) if variance > 0 else 0.0
    if math.isclose(std, 0.0, abs_tol=_ZERO_TOL):
        return 0.0
    factor = annualize_factor if annualize_factor is not None else math.sqrt(252)
    return (mean_return / std) * factor


def _sortino(returns: List[float], annualize_factor: float | None = None) -> float:
    """Annualized Sortino ratio (downside deviation, risk-free rate = 0)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_return = sum(returns) / n
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_var = sum(r**2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
    if math.isclose(downside_std, 0.0, abs_tol=_ZERO_TOL):
        return 0.0
    factor = annualize_factor if annualize_factor is not None else math.sqrt(252)
    return (mean_return / downside_std) * factor


def compute_trade_statistics(trades: List[Trade]) -> TradeStatistics:
    """Compute all trade-based statistics from a list of Trade objects."""
    if not trades:
        return TradeStatistics()

    realized_pnls = [_net_realized_pnl(t) for t in trades]
    # Gross figures stay gross (fees excluded) so they remain comparable with
    # MT5's Bruttogewinn/Bruttoverlust labels (DV-B46).
    gross_pnls = [_realized_pnl(t) for t in trades]

    total_trades = len(trades)
    winning_trades = sum(1 for p in realized_pnls if p > 0)
    losing_trades = sum(1 for p in realized_pnls if p < 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

    net_profit = sum(realized_pnls)
    gross_profit = sum(p for p in gross_pnls if p > 0)
    gross_loss = sum(p for p in gross_pnls if p < 0)

    if not math.isclose(gross_loss, 0.0, abs_tol=_ZERO_TOL):
        profit_factor = abs(gross_profit / gross_loss)
    else:
        profit_factor = float("inf")

    max_drawdown, max_drawdown_pct = _compute_drawdowns(realized_pnls)

    annualize = _annualize_factor(trades)
    sharpe_ratio = _sharpe(realized_pnls, annualize)
    sortino_ratio = _sortino(realized_pnls, annualize)

    if not math.isclose(max_drawdown, 0.0, abs_tol=_ZERO_TOL):
        recovery_factor = net_profit / max_drawdown
    else:
        recovery_factor = float("inf")

    avg_winner = gross_profit / winning_trades if winning_trades > 0 else 0.0
    avg_loser = gross_loss / losing_trades if losing_trades > 0 else 0.0
    avg_trade = net_profit / total_trades if total_trades > 0 else 0.0

    largest_winner = max((p for p in realized_pnls if p > 0), default=0.0)
    largest_loser = min((p for p in realized_pnls if p < 0), default=0.0)

    return TradeStatistics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        net_profit=net_profit,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        recovery_factor=recovery_factor,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
        avg_trade=avg_trade,
        largest_winner=largest_winner,
        largest_loser=largest_loser,
    )

"""
Strategy comparison logic — ported from custom_lean_integration/compare_strategies.py.

Adapted for the Universal Trade Format used by trading-platform.
Computes combined equity curves, drawdown correlation, and side-by-side stats
for 2+ backtests.
"""

from __future__ import annotations

import math
from typing import Any


def compute_returns(values: list[float]) -> list[float]:
    """Period-over-period returns from an equity series."""
    if len(values) < 2:
        return []
    return [
        (values[i] - values[i - 1]) / values[i - 1] if values[i - 1] != 0 else 0.0
        for i in range(1, len(values))
    ]


def compute_drawdown_pct(values: list[float]) -> list[float]:
    """Drawdown percentage series from equity values."""
    if not values:
        return []
    peak = values[0]
    dd = []
    for v in values:
        if v > peak:
            peak = v
        dd.append((v - peak) / peak if peak > 0 else 0.0)
    return dd


def pearson_correlation(x: list[float], y: list[float]) -> float:
    """Pearson correlation coefficient.

    Guards against mismatched lengths, non-finite values, zero variance and
    empty inputs — all of which would otherwise produce a silently wrong or
    crashing coefficient.
    """
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    if not all(math.isfinite(v) for v in x) or not all(math.isfinite(v) for v in y):
        return 0.0
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)


def _align_series(
    series_a: list[list], series_b: list[list]
) -> tuple[list[str], list[float], list[float]]:
    """Align two [time, value] series to common timestamps."""
    dict_a = dict(series_a)
    dict_b = dict(series_b)
    common = sorted(set(dict_a.keys()) & set(dict_b.keys()))
    return common, [dict_a[d] for d in common], [dict_b[d] for d in common]


def compute_combined_equity(
    dates: list[str],
    vals_a: list[float],
    vals_b: list[float],
    start_capital: float = 100000,
) -> list[list]:
    """50/50 combined equity curve from two aligned series."""
    if not dates:
        return []
    norm_a = [v / vals_a[0] if vals_a[0] > 0 else 1.0 for v in vals_a]
    norm_b = [v / vals_b[0] if vals_b[0] > 0 else 1.0 for v in vals_b]
    return [
        [d, round(start_capital * (0.5 * norm_a[i] + 0.5 * norm_b[i]), 2)]
        for i, d in enumerate(dates)
    ]


def compute_combined_stats(
    dates: list[str],
    vals_a: list[float],
    vals_b: list[float],
    combined_equity: list[list],
) -> dict[str, Any]:
    """Stats for the combined 50/50 portfolio."""
    combined_vals = [v for _, v in combined_equity]
    if not combined_vals:
        return {"errors": ["No overlapping dates"]}

    combined_rets = compute_returns(combined_vals)

    # Sharpe (annualized, ~252 trading days)
    if combined_rets:
        mean_r = sum(combined_rets) / len(combined_rets)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in combined_rets) / len(combined_rets))
        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0
        mean_r = 0.0

    # Max drawdown
    dd_series = compute_drawdown_pct(combined_vals)
    max_dd = min(dd_series) if dd_series else 0.0

    # Correlations
    dd_a = compute_drawdown_pct(vals_a)
    dd_b = compute_drawdown_pct(vals_b)
    dd_corr = pearson_correlation(dd_a, dd_b)

    returns_a = compute_returns(vals_a)
    returns_b = compute_returns(vals_b)
    return_corr = pearson_correlation(returns_a, returns_b)

    # Net return
    net_return = (combined_vals[-1] - combined_vals[0]) / combined_vals[0] if combined_vals else 0.0

    # Sortino
    if combined_rets:
        downside = [r for r in combined_rets if r < 0]
        downside_std = math.sqrt(sum(r**2 for r in downside) / len(downside)) if downside else 0.0
        sortino = (mean_r / downside_std * math.sqrt(252)) if downside_std > 0 else 0.0
    else:
        sortino = 0.0

    return {
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown": round(max_dd, 4),
        "net_return_pct": round(net_return * 100, 3),
        "correlation_drawdown": round(dd_corr, 4),
        "correlation_returns": round(return_corr, 4),
        "overlapping_days": len(dates),
        "start_date": dates[0] if dates else None,
        "end_date": dates[-1] if dates else None,
    }


def compare_backtests(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compare exactly two backtest results.

    Parameters
    ----------
    strategies : list of dicts, each with keys:
        - name: str
        - equity_curve: [[time_str, value], ...]
        - drawdown_curve: [[time_str, dd_pct], ...]  (optional, computed if missing)
        - stats: dict
        - trades: dict (summary)

    Returns
    -------
    Structured comparison result with combined view. The combined 50/50 view
    is defined for exactly two strategies; any other count is reported as an
    explicit error instead of silently blending the first two.
    """
    combined_view: dict[str, Any] = {
        "combined_equity_curve": [],
        "combined_stats": {},
        "errors": [],
    }

    if len(strategies) != 2:
        combined_view["errors"].append(
            f"compare_backtests requires exactly 2 strategies, got {len(strategies)}"
        )
        # No combined 50/50 blend when the arity is wrong: silently blending
        # the first two is exactly the behavior this guard replaces.
        return {
            "strategies": [
                {
                    "name": s.get("name", "Unknown"),
                    "equity_curve": s.get("equity_curve", []),
                    "drawdown_curve": s.get("drawdown_curve", []),
                    "stats": s.get("stats", {}),
                    "trades": s.get("trades", {}),
                }
                for s in strategies
            ],
            "combined_view": combined_view,
        }

    if len(strategies) >= 2:
        eq_a = strategies[0].get("equity_curve", [])
        eq_b = strategies[1].get("equity_curve", [])

        if eq_a and eq_b:
            dates, vals_a, vals_b = _align_series(eq_a, eq_b)
            if not dates:
                combined_view["errors"].append("No overlapping dates between strategies")
            else:
                combined_eq = compute_combined_equity(dates, vals_a, vals_b)
                combined_stats = compute_combined_stats(dates, vals_a, vals_b, combined_eq)
                combined_view["combined_equity_curve"] = combined_eq
                combined_view["combined_stats"] = combined_stats
        else:
            if not eq_a:
                combined_view["errors"].append(
                    f"No equity curve for {strategies[0].get('name', 'Strategy A')}"
                )
            if not eq_b:
                combined_view["errors"].append(
                    f"No equity curve for {strategies[1].get('name', 'Strategy B')}"
                )

    return {
        "strategies": [
            {
                "name": s.get("name", "Unknown"),
                "equity_curve": s.get("equity_curve", []),
                "drawdown_curve": s.get("drawdown_curve", []),
                "stats": s.get("stats", {}),
                "trades": s.get("trades", {}),
            }
            for s in strategies
        ],
        "combined_view": combined_view,
    }

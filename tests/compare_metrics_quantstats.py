"""
Metrics Validation: runner.stats vs QuantStats
================================================
Vergleicht Sharpe, Sortino, Calmar und Max Drawdown der eigenen Implementierung
mit QuantStats anhand mehrerer synthetischer Szenarien.

Aufruf:  python tests/compare_metrics_quantstats.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import quantstats as qs

from runner.stats import _daily_returns, _max_drawdown_pct, _sharpe, _sortino, compute_stats
from shared.models import EquityPoint, Trade, TradeExit

RISK_FREE_RATE = 0.04  # annual


# ─── Helpers ─────────────────────────────────────────────────────────────────

def equity_to_returns_series(equities: list[float], dates: pd.DatetimeIndex | None = None) -> pd.Series:
    """Convert equity list to daily returns pd.Series."""
    arr = np.array(equities, dtype=float)
    rets = arr[1:] / arr[:-1] - 1
    if dates is not None:
        idx = dates[1:]
    else:
        idx = pd.date_range("2020-01-02", periods=len(rets), freq="B")
    return pd.Series(rets, index=idx)


def make_equity_curve_objs(equities: list[float]) -> list[EquityPoint]:
    dates = pd.date_range("2020-01-01", periods=len(equities), freq="B")
    return [EquityPoint(time=str(d.date()), equity=float(e)) for d, e in zip(dates, equities)]


def make_dummy_trade(pnl: float) -> Trade:
    return Trade(
        id=1, symbol="TEST", direction="LONG",
        entry_time="2020-01-01T00:00:00Z", entry_price=100.0,
        quantity=1,
        exits=[TradeExit(time="2020-01-02T00:00:00Z", price=100 + pnl, quantity=1)],
        pnl=pnl,
    )


# ─── Scenarios ────────────────────────────────────────────────────────────────

np.random.seed(42)

SCENARIOS: dict[str, list[float]] = {}

# Scenario A: Steady upward trend, minimal volatility
_start = 100_000.0
_A = [_start]
for _ in range(251):
    _A.append(_A[-1] * (1 + 0.0003))  # +0.03% per day
SCENARIOS["A_steady_growth"] = _A

# Scenario B: Realistic volatile strategy with drawdowns
_B = [100_000.0]
np.random.seed(42)
_daily = np.random.normal(0.0005, 0.012, 251)  # mean 0.05%/day, std 1.2%
for r in _daily:
    _B.append(_B[-1] * (1 + r))
SCENARIOS["B_realistic_volatile"] = _B

# Scenario C: Stress – significant losses, deep drawdown
_C = [100_000.0]
np.random.seed(7)
_stress = np.random.normal(-0.001, 0.025, 251)  # negative drift
for r in _stress:
    _C.append(max(_C[-1] * (1 + r), 1.0))  # floor at 1
SCENARIOS["C_stress_losses"] = _C

# Scenario D: Simple textbook case (easy to verify manually)
SCENARIOS["D_textbook_simple"] = [100.0, 102.0, 101.0, 105.0, 103.0, 108.0]


# ─── Compute and compare ──────────────────────────────────────────────────────

def compare_scenario(name: str, equities: list[float]) -> dict:
    daily_rf = RISK_FREE_RATE / 252
    returns_list = _daily_returns(equities)
    returns_series = equity_to_returns_series(equities)

    # ── Our implementation ────────────────────────────────────────────────────
    our_sharpe = _sharpe(returns_list, RISK_FREE_RATE)
    our_sortino = _sortino(returns_list, RISK_FREE_RATE)
    our_mdd = _max_drawdown_pct(equities)   # returns fraction 0-1

    # Calmar as computed in compute_stats()
    total_return = (equities[-1] / equities[0]) - 1 if equities[0] > 0 else 0
    n_points = len(equities)
    our_annualized_return = (1 + total_return) ** (252 / max(n_points, 1)) - 1
    # BUG CANDIDATE: divides by max_drawdown_pct/100
    our_calmar_bugged = (
        our_annualized_return / abs(our_mdd / 100) if our_mdd != 0 else 0
    )
    # CORRECT Calmar: annualized_return / abs(max_drawdown_as_fraction)
    our_calmar_fixed = (
        our_annualized_return / abs(our_mdd) if our_mdd != 0 else 0
    )

    # ── QuantStats ────────────────────────────────────────────────────────────
    # NOTE: QuantStats expects the ANNUAL rf — it deannualizes internally.
    # Passing daily_rf (rf/252) would be double-deannualized → effectively ~0.
    qs_sharpe   = float(qs.stats.sharpe(returns_series, rf=RISK_FREE_RATE, periods=252, annualize=True))
    qs_sortino  = float(qs.stats.sortino(returns_series, rf=RISK_FREE_RATE, periods=252, annualize=True))
    qs_mdd      = float(qs.stats.max_drawdown(returns_series))  # returns negative fraction
    qs_calmar   = float(qs.stats.calmar(returns_series))        # CAGR / abs(max_drawdown)

    # qs max_drawdown is negative (e.g. -0.0909); convert to positive for comparison
    qs_mdd_pos = abs(qs_mdd)

    return {
        "scenario": name,
        "n_points": len(equities),
        # Sharpe
        "our_sharpe": our_sharpe,
        "qs_sharpe": qs_sharpe,
        "sharpe_diff": our_sharpe - qs_sharpe,
        # Sortino
        "our_sortino": our_sortino,
        "qs_sortino": qs_sortino,
        "sortino_diff": our_sortino - qs_sortino,
        # Max Drawdown (as fraction)
        "our_mdd": our_mdd,
        "qs_mdd": qs_mdd_pos,
        "mdd_diff": our_mdd - qs_mdd_pos,
        # Calmar (bugged)
        "our_calmar_bugged": our_calmar_bugged,
        # Calmar (fixed)
        "our_calmar_fixed": our_calmar_fixed,
        "qs_calmar": qs_calmar,
        "calmar_diff_bugged": our_calmar_bugged - qs_calmar,
        "calmar_diff_fixed": our_calmar_fixed - qs_calmar,
    }


def fmt(v: float, decimals: int = 4) -> str:
    if math.isnan(v) or math.isinf(v):
        return str(v)
    return f"{v:.{decimals}f}"


def print_results(results: list[dict]) -> None:
    col_w = 22

    header = (
        f"{'Metric':<25}"
        f"{'Our Impl':>{col_w}}"
        f"{'QuantStats':>{col_w}}"
        f"{'Diff':>{col_w}}"
        f"{'Status':>10}"
    )

    for r in results:
        print(f"\n{'='*95}")
        print(f"  Scenario: {r['scenario']}  (n_points={r['n_points']})")
        print(f"{'='*95}")
        print(header)
        print("-" * 95)

        def row(metric, our, qs_val, diff, threshold=0.01):
            ok = "✓ OK" if abs(diff) <= threshold else "✗ DIFF"
            print(f"  {metric:<23}{fmt(our):>{col_w}}{fmt(qs_val):>{col_w}}{fmt(diff):>{col_w}}{ok:>10}")

        row("Sharpe Ratio",    r["our_sharpe"],        r["qs_sharpe"],  r["sharpe_diff"])
        row("Sortino Ratio",   r["our_sortino"],       r["qs_sortino"], r["sortino_diff"])
        row("Max Drawdown",    r["our_mdd"],           r["qs_mdd"],     r["mdd_diff"])

        diff_bugged = r["calmar_diff_bugged"]
        diff_fixed  = r["calmar_diff_fixed"]
        ok_bugged = "✓ OK" if abs(diff_bugged) <= 0.01 else "✗ BUG"
        ok_fixed  = "✓ OK" if abs(diff_fixed)  <= 0.01 else "✗ DIFF"

        print(
            f"  {'Calmar (bugged /100)':<23}"
            f"{fmt(r['our_calmar_bugged']):>{col_w}}"
            f"{fmt(r['qs_calmar']):>{col_w}}"
            f"{fmt(diff_bugged):>{col_w}}"
            f"{ok_bugged:>10}"
        )
        print(
            f"  {'Calmar (fixed)':<23}"
            f"{fmt(r['our_calmar_fixed']):>{col_w}}"
            f"{fmt(r['qs_calmar']):>{col_w}}"
            f"{fmt(diff_fixed):>{col_w}}"
            f"{ok_fixed:>10}"
        )


if __name__ == "__main__":
    print("\n" + "=" * 95)
    print("  METRICS COMPARISON: runner.stats vs QuantStats")
    print("  Risk-free rate (annual): 4%")
    print("=" * 95)

    all_results = []
    for scenario_name, equities in SCENARIOS.items():
        r = compare_scenario(scenario_name, equities)
        all_results.append(r)

    print_results(all_results)

    # ── Extra: Sharpe formula deep-dive ──────────────────────────────────────
    print("\n\n" + "=" * 95)
    print("  DEEP-DIVE: Sharpe variance (Population N vs Sample N-1)")
    print("=" * 95)
    for scenario_name, equities in list(SCENARIOS.items())[:2]:
        daily_rf = RISK_FREE_RATE / 252
        rl = _daily_returns(equities)
        if len(rl) < 2:
            continue
        excess = [r - daily_rf for r in rl]
        mean_e = sum(excess) / len(excess)
        # Population std (N)
        var_n  = sum((r - mean_e) ** 2 for r in excess) / len(excess)
        std_n  = math.sqrt(var_n)
        sharpe_n = (mean_e / std_n) * math.sqrt(252) if std_n > 0 else 0
        # Sample std (N-1)
        var_n1 = sum((r - mean_e) ** 2 for r in excess) / (len(excess) - 1)
        std_n1 = math.sqrt(var_n1)
        sharpe_n1 = (mean_e / std_n1) * math.sqrt(252) if std_n1 > 0 else 0

        s = equity_to_returns_series(equities)
        qs_s = float(qs.stats.sharpe(s, rf=daily_rf, periods=252, annualize=True))

        print(f"\n  {scenario_name}:")
        print(f"    Sharpe (N,  pop-var):  {sharpe_n:.6f}  ← our implementation")
        print(f"    Sharpe (N-1, smp-var): {sharpe_n1:.6f}")
        print(f"    QuantStats:            {qs_s:.6f}")

    # ── Sortino deep-dive ─────────────────────────────────────────────────────
    print("\n\n" + "=" * 95)
    print("  DEEP-DIVE: Sortino – downside definition differences")
    print("=" * 95)
    for scenario_name, equities in list(SCENARIOS.items())[:2]:
        daily_rf = RISK_FREE_RATE / 252
        rl = _daily_returns(equities)
        if len(rl) < 2:
            continue
        mean_r = sum(rl) / len(rl)

        # Our impl: downside = r < 0
        downside_neg = [r for r in rl if r < 0]
        if len(downside_neg) >= 2:
            dv_neg = sum(r**2 for r in downside_neg) / len(downside_neg)
            ds_neg = math.sqrt(dv_neg)
            sortino_neg = ((mean_r - daily_rf) / ds_neg) * math.sqrt(252) if ds_neg > 0 else 0
        else:
            sortino_neg = 0.0

        # Alternative: downside = r < rf
        downside_rf = [r for r in rl if r < daily_rf]
        if len(downside_rf) >= 2:
            dv_rf = sum((r - daily_rf)**2 for r in downside_rf) / len(downside_rf)
            ds_rf = math.sqrt(dv_rf)
            sortino_rf = ((mean_r - daily_rf) / ds_rf) * math.sqrt(252) if ds_rf > 0 else 0
        else:
            sortino_rf = 0.0

        s = equity_to_returns_series(equities)
        qs_s = float(qs.stats.sortino(s, rf=daily_rf, periods=252, annualize=True))

        our_s = _sortino(rl, RISK_FREE_RATE)

        print(f"\n  {scenario_name}:")
        print(f"    Our Sortino (r<0):     {our_s:.6f}  ← our implementation")
        print(f"    Sortino (r<0, fixed):  {sortino_neg:.6f}")
        print(f"    Sortino (r<rf):        {sortino_rf:.6f}")
        print(f"    QuantStats:            {qs_s:.6f}")

    print("\n\nDone.")

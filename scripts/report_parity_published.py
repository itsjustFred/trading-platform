"""Print the platform / reference / buy-and-hold tables of the published-strategy parity report.

Runs the platform engine and the independent reference
(``tests/parity/ref_published.py``) on the committed fixtures and prints
markdown.  Usage: ``python scripts/report_parity_published.py``.
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from runner.stats import compute_stats_for_result  # noqa: E402
from tests.parity.published_compare import bars_of, compare  # noqa: E402
from tests.parity.published_harness import REPO_ROOT, load_fixture, run_engine  # noqa: E402
from tests.parity.ref_published import (  # noqa: E402
    SimResult, connors_target, faber_target, gem_targets, simulate,
)

STRAT = REPO_ROOT / "strategies"
FABER = STRAT / "faber_tactical_asset_allocation" / "faber_tactical_asset_allocation.py"
CONNORS = STRAT / "connors_rsi2_mean_reversion" / "connors_rsi2_mean_reversion.py"
GEM = STRAT / "antonacci_global_equity_momentum" / "gem_dual_momentum.py"


def metrics(equity: pd.Series) -> dict:
    """CAGR / vol / Sharpe(rf=0) / maxDD from a daily equity series."""
    equity = equity.astype(float)
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    rets = equity.pct_change().dropna()
    peak = equity.cummax()
    return {
        "total_return_pct": (equity.iloc[-1] / equity.iloc[0] - 1) * 100,
        "cagr_pct": ((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100,
        "vol_pct": rets.std() * math.sqrt(252) * 100,
        "sharpe": rets.mean() / rets.std() * math.sqrt(252) if rets.std() else float("nan"),
        "maxdd_pct": ((equity / peak - 1).min()) * 100,
    }


def marked_series(ref: SimResult, capital: float = 100_000.0) -> pd.Series:
    s = pd.Series(ref.marked)
    s.index = pd.to_datetime(s.index)
    return s


def buy_hold(frame: pd.DataFrame, capital: float = 100_000.0) -> pd.Series:
    # bought at the first bar's open... use close-to-close from first close
    s = pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["timestamp"].dt.tz_localize(None)))
    return capital * s / s.iloc[0]


def with_cash_leg(ref: SimResult, irx: pd.DataFrame) -> pd.Series:
    """Reference equity where flat days earn the previous day's 13-week T-bill yield / 252."""
    marked = marked_series(ref)
    held = pd.Series(False, index=marked.index)
    for t in ref.trades:
        # daily approximation: held from the entry day through the exit day (the exit now fills at that day's open, so
        # the exit day's open-to-close part would earn cash; ignored, it is a reference-only estimate)
        held.loc[(held.index > pd.Timestamp(t.entry_day)) & (held.index <= pd.Timestamp(t.exit_day))] = True
        held.loc[pd.Timestamp(t.entry_day)] = True
    y = pd.Series(irx["close"].to_numpy(), index=pd.to_datetime(irx["timestamp"].dt.tz_localize(None)))
    y = y.reindex(marked.index, method="ffill").shift(1).fillna(0.0) / 100.0 / 252.0
    raw = marked.pct_change().fillna(0.0)
    r = raw.where(held, y)
    return 100_000.0 * (1 + r).cumprod()


def platform_stats(run) -> dict:
    stats = compute_stats_for_result(run.result)
    return {k: stats.get(k) for k in ("total_trades", "total_return", "cagr", "max_drawdown_pct", "sharpe_ratio", "win_rate")}


def fmt(m: dict) -> str:
    return " | ".join(f"{m[k]:.2f}" for k in ("total_return_pct", "cagr_pct", "vol_pct", "sharpe", "maxdd_pct"))


def run_single(strategy, frame, target_fn, tmp, fee=0.0):
    frame = frame.reset_index(drop=True)
    bars = bars_of(frame)
    ref = simulate({"x": bars}, {"x": target_fn(bars)}, fee_rate=fee)
    run = run_engine(strategy, {"SYN": frame}, tmp_path=Path(tmp), start=bars[0].day, end=bars[-1].day, fee_rate=fee)
    return frame, ref, run, compare(run, ref, {"x": "SYN"})


def main() -> None:
    irx = load_fixture("irx")
    rows = []
    print("### scenario table (reference metrics on daily marked equity; Sharpe rf=0)\n")
    print("| Scenario | trades eng/ref | diffs | final eq eng-curve | final eq ref | total ret % | CAGR % | vol % | Sharpe | maxDD % |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for label, fix, fn, strat, start in [
        ("Faber SMA10 S&P500 price 1928-2025", "gspc", faber_target, FABER, None),
        ("Faber SMA10 S&P500 price 1928-2012", "gspc", faber_target, FABER, "2012-12-31"),
        ("Faber SMA10 S&P500 TR 1988-2012", "sp500tr", faber_target, FABER, "2012-12-31"),
        ("Faber SMA10 SPY adj. 1993-2025", "spy", faber_target, FABER, None),
        ("Connors RSI2 SPY adj. 1993-2025", "spy", connors_target, CONNORS, None),
    ]:
        frame = load_fixture(fix)
        if start:
            frame = frame[frame["timestamp"] <= start]
        with tempfile.TemporaryDirectory() as tmp:
            frame, ref, run, diffs = run_single(strat, frame, fn, tmp)
        m = metrics(marked_series(ref))
        bh = metrics(buy_hold(frame))
        ps = platform_stats(run)
        print(f"| {label} (strategy) | {len(run.trades)}/{len(ref.trades)} | {len(diffs)} | {run.curve[-1][1]:,.2f} | {ref.final_equity:,.2f} | {fmt(m)} |")
        print(f"| {label} (buy&hold) | | | | | {fmt(bh)} |")
        print(f"|   platform compute_stats | | | | | total_return={ps['total_return']}, cagr={ps['cagr']}, maxDD={ps['max_drawdown_pct']}, sharpe={ps['sharpe_ratio']}, win_rate={ps['win_rate']} |")
        if strat == FABER:
            cash = metrics(with_cash_leg(ref, irx.pipe(lambda d: d)))
            print(f"| {label} (strategy + T-bill cash leg, reference only) | | | | | {fmt(cash)} |")
        time_in = sum((pd.Timestamp(t.exit_day) - pd.Timestamp(t.entry_day)).days for t in ref.trades) / max((frame['timestamp'].iloc[-1].tz_localize(None) - frame['timestamp'].iloc[0].tz_localize(None)).days, 1)
        print(f"|   time in market (calendar) | | | | | {time_in*100:.1f} % |")
    # GEM
    params = {"us_symbol": "SPY", "non_us_symbol": "VEU", "bond_symbol": "AGG", "cash_symbol": "BIL"}
    import importlib.util
    spec = importlib.util.spec_from_file_location("gem_port", GEM)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    for non_us in ("VEU", "EFA"):
        params["non_us_symbol"] = non_us
        frames = {n: load_fixture(n) for n in ("SPY", non_us, "AGG", "BIL")}
        attached = mod.attach_peer_closes(frames, params)
        start = "2007-05-30"
        with tempfile.TemporaryDirectory() as tmp:
            run = run_engine(GEM, {s: attached[s] for s in ("SPY", non_us, "AGG")}, tmp_path=Path(tmp), start=start, end="2025-12-30", params=params)
        slots = (("us", "SPY"), ("non_us", non_us), ("bond", "AGG"), ("cash", "BIL"))
        full = {slot: bars_of(frames[sym]) for slot, sym in slots}
        targets = gem_targets(full)
        traded = {slot: [b for b in full[slot] if b.day >= start] for slot in targets}
        ref = simulate(traded, targets)
        diffs = compare(run, ref, {"us": "SPY", "non_us": non_us, "bond": "AGG"}, qty_abs=5.1e-5, rel=1e-6)
        m = metrics(marked_series(ref))
        spy = frames["SPY"]; spy = spy[spy["timestamp"] >= start]
        bh = metrics(buy_hold(spy))
        ps = platform_stats(run)
        by = {s: sum(1 for t in ref.trades if t.symbol == s) for s in ("us", "non_us", "bond")}
        print(f"| GEM SPY/{non_us}/AGG/BIL 2007-2025 | {len(run.trades)}/{len(ref.trades)} | {len(diffs)} | {run.curve[-1][1]:,.2f} | {ref.final_equity:,.2f} | {fmt(m)} |")
        print(f"| GEM SPY buy&hold same window | | | | | {fmt(bh)} |")
        print(f"|   platform compute_stats | | | | | total_return={ps['total_return']}, cagr={ps['cagr']}, maxDD={ps['max_drawdown_pct']}, sharpe={ps['sharpe_ratio']} |")
        print(f"|   slot trades | | | | | {by} |")


if __name__ == "__main__":
    main()

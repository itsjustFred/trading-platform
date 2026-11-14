"""Trade-by-trade / equity comparison of engine runs against ``ref_published``."""
from __future__ import annotations

import math

import pandas as pd

from tests.parity.published_harness import EngineRun
from tests.parity.ref_published import Bar, SimResult

PRICE_ABS = 1e-6
QTY_REL = 1e-9
PNL_ABS = 5e-3
CURVE_ABS = 0.02


def bars_of(frame: pd.DataFrame) -> list[Bar]:
    return [
        Bar(day=ts.strftime("%Y-%m-%d"), open=float(o), close=float(c))
        for ts, o, c in zip(frame["timestamp"], frame["open"], frame["close"])
    ]


def compare(
    run: EngineRun, ref: SimResult, symbol_map: dict[str, str], *, qty_abs: float = 0.0, rel: float = 0.0
) -> list[str]:
    """Differences (empty == parity). ``symbol_map`` maps reference slot -> engine symbol.

    ``qty_abs``: absolute quantity slack.  Multi-symbol runs are replayed on a
    shared equity by the adapter, which rounds quantities to 4 decimals (and
    sizes each later trade on the rounded-PnL equity, a ~1e-7 relative drift);
    ``rel`` is the relative slack for quantities, PnL and curve in that mode and
    the PnL/curve tolerances also widen by the rounding's price exposure.
    """
    diffs: list[str] = []
    eng = sorted(run.trades, key=lambda t: (t.entry_time, t.symbol))
    exp = sorted(ref.trades, key=lambda t: (t.entry_day, symbol_map[t.symbol]))
    if len(eng) != len(exp):
        diffs.append(f"trade count: engine={len(eng)} reference={len(exp)}")
    for k, (a, b) in enumerate(zip(eng, exp)):
        tag = f"#{k} {b.symbol} {b.entry_day}"
        if (a.entry_time, a.symbol) != (b.entry_day, symbol_map[b.symbol]):
            diffs.append(f"{tag}: engine entry {a.symbol} {a.entry_time}")
            break
        for name, x, y, kw in (
            ("entry price", a.entry_price, b.entry_price, {"abs_tol": PRICE_ABS}),
            ("exit price", a.exit_price, b.exit_price, {"abs_tol": PRICE_ABS}),
            ("qty", a.quantity, b.qty, {"rel_tol": max(QTY_REL, rel), "abs_tol": qty_abs}),
            ("net pnl", a.net_pnl, b.net_pnl,
             {"rel_tol": max(1e-9, rel), "abs_tol": PNL_ABS + qty_abs * (abs(b.exit_price) + abs(b.entry_price))}),
        ):
            if not math.isclose(x, y, **kw):
                diffs.append(f"{tag}: {name} {x} != {y}")
        if a.exit_time != b.exit_day:
            diffs.append(f"{tag}: exit {a.exit_time} != {b.exit_day}")
    # each rotation carries <= qty_abs of rounding on a price <= ~1e3; accumulates per trade
    curve_tol = CURVE_ABS + qty_abs * 1e3 * max(len(eng), 1)
    matched = 0
    for stamp, equity in run.curve:
        if stamp in ref.marked:
            matched += 1
            if not math.isclose(equity, ref.marked[stamp], rel_tol=rel, abs_tol=curve_tol):
                diffs.append(f"curve[{stamp}] engine={equity} ref marked={ref.marked[stamp]}")
                if len(diffs) > 6:
                    break
    if matched < len(run.curve) - 1:
        diffs.append(f"curve: only {matched}/{len(run.curve)} engine points are reference days")
    return diffs

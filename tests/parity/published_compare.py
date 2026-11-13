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


def compare(run: EngineRun, ref: SimResult, symbol_map: dict[str, str]) -> list[str]:
    """Differences (empty == parity). ``symbol_map`` maps reference slot -> engine symbol."""
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
            ("qty", a.quantity, b.qty, {"rel_tol": QTY_REL}),
            ("net pnl", a.net_pnl, b.net_pnl, {"rel_tol": 1e-9, "abs_tol": PNL_ABS}),
        ):
            if not math.isclose(x, y, **kw):
                diffs.append(f"{tag}: {name} {x} != {y}")
        if a.exit_time != b.exit_day:
            diffs.append(f"{tag}: exit {a.exit_time} != {b.exit_day}")
    matched = 0
    for stamp, equity in run.curve:
        if stamp in ref.realized:
            matched += 1
            if not (
                math.isclose(equity, ref.realized[stamp], abs_tol=CURVE_ABS)
                or math.isclose(equity, ref.marked[stamp], abs_tol=CURVE_ABS)
            ):
                diffs.append(f"curve[{stamp}] engine={equity} ref realized={ref.realized[stamp]} marked={ref.marked[stamp]}")
                if len(diffs) > 6:
                    break
    if matched < len(run.curve) - 1:
        diffs.append(f"curve: only {matched}/{len(run.curve)} engine points are reference days")
    return diffs

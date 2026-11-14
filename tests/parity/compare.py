"""Trade-by-trade comparison of the engine against the reference simulator."""

from __future__ import annotations

import math

from tests.parity.engine_harness import EngineRun
from tests.parity.reference_execution import RefTrade

#: price/quantity/pnl tolerances.  The engine rounds nothing in trades; the
#: only slack is float summation order (1e-6 relative) and 2dp curve rounding.
QTY_REL = 1e-6
PRICE_ABS = 1e-6
PNL_ABS = 5e-3
CURVE_ABS = 0.02


def _close(a: float, b: float, *, rel: float = 0.0, abs_: float = 0.0) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


def compare_trades(engine: list[RefTrade], ref: list[RefTrade]) -> list[str]:
    """Return a list of human readable differences (empty == parity)."""
    diffs: list[str] = []
    eng = sorted(engine, key=lambda t: t.entry_time)
    exp = sorted(ref, key=lambda t: t.entry_time)
    if len(eng) != len(exp):
        diffs.append(f"trade count: engine={len(eng)} reference={len(exp)}")
    for k, (a, b) in enumerate(zip(eng, exp)):
        tag = f"#{k} {b.entry_time[:10]} {b.direction}"
        if a.entry_time != b.entry_time or a.direction != b.direction:
            diffs.append(f"{tag}: entry engine={a.entry_time}/{a.direction}")
            break  # misaligned from here on
        if not _close(a.entry_price, b.entry_price, abs_=PRICE_ABS):
            diffs.append(f"{tag}: entry price {a.entry_price} != {b.entry_price}")
        if not _close(a.quantity, b.quantity, rel=QTY_REL, abs_=1e-9):
            diffs.append(f"{tag}: qty {a.quantity} != {b.quantity}")
        if a.exit_time != b.exit_time:
            diffs.append(f"{tag}: exit time {a.exit_time} != {b.exit_time}")
        if a.exit_price is None or b.exit_price is None or not _close(
            a.exit_price, b.exit_price, abs_=PRICE_ABS
        ):
            diffs.append(f"{tag}: exit price {a.exit_price} != {b.exit_price}")
        if _reason(a.exit_reason) != _reason(b.exit_reason):
            diffs.append(f"{tag}: exit reason {a.exit_reason} != {b.exit_reason}")
        if not _close(a.net_pnl, b.net_pnl, rel=1e-6, abs_=PNL_ABS):
            diffs.append(f"{tag}: net pnl {a.net_pnl} != {b.net_pnl}")
    return diffs


def _reason(value: str) -> str:
    return "stop_loss" if value in {"stop_loss", "trailing_stop"} else value


def compare_curve(engine: list[tuple[str, float]], ref: dict[str, float]) -> list[str]:
    """Every engine equity point must equal the reference equity at that bar.

    The adapter publishes a compressed curve (points only where equity moves,
    plus a synthetic start point that is not a bar), so the reference is looked
    up by timestamp instead of by position.
    """
    diffs: list[str] = []
    matched = 0
    for stamp, equity in engine:
        if stamp not in ref:
            continue  # the synthetic window-start point
        matched += 1
        if not _close(equity, ref[stamp], abs_=CURVE_ABS):
            diffs.append(f"curve[{stamp}] engine={equity} reference={ref[stamp]}")
            if len(diffs) >= 3:
                break
    if matched < max(1, len(engine) - 2):
        diffs.append(f"curve: only {matched}/{len(engine)} engine points are reference bars")
    return diffs


def assert_parity(
    run: EngineRun, ref_trades: list[RefTrade], ref_curve: dict[str, float], label: str
):
    diffs = compare_trades(run.trades, ref_trades) + compare_curve(run.curve, ref_curve)
    assert not diffs, f"{label}: engine != reference\n" + "\n".join(diffs[:12])

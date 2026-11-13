"""Parity: platform engine vs an independent reference on Turtle System 1.

The reference (``reference_turtle.TurtleRef`` for signals,
``reference_execution.ReferenceSimulator`` for fills/stops/sizing) imports
nothing from the platform.  Compared trade by trade: entry time and price,
quantity (``risk_per_trade_pct=2 %`` of REALIZED equity over the 2N stop = one
Turtle unit), exit time/price/reason (intrabar stops, gap through the stop,
same-bar entry+stop, 10-day signal exits), net PnL, and the published
realized-equity curve.

Scenarios that hold more than one concurrent unit are marked ``xfail(strict)``
on engines that still size on mark-to-market equity instead of realized cash
(``docs/sizing-semantics.md``, ``TestSizingEquityExcludesUnrealizedPnl``): that
sizing fix is owned by PR #190.  Once it is on the base branch the strict
xfail turns into a failure that says "remove the marker".
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pandas as pd
import pytest

from tests.parity.compare import assert_parity
from tests.parity.engine_harness import REPO_ROOT, bar_list, frame_from_fixture, run_engine
from tests.parity.reference_execution import RefConfig, ReferenceSimulator
from tests.parity.reference_turtle import TurtleRef

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "turtle_system1" / "turtle_system1.py"

#: concurrent units are sized on mark-to-market equity until PR #190 lands.
MTM_SIZING_BUG = pytest.mark.xfail(
    strict=True,
    reason="engine sizes the 2nd+ concurrent unit on mark-to-market equity "
    "(realized-cash contract fixed by PR #190)",
)

SHORT_PARAMS = {
    "entry_len": 10,
    "exit_len": 5,
    "failsafe_len": 20,
    "atr_len": 10,
}


def _ref(bars, *, risk=0.02, cap=1.0, **ref_kwargs):
    signals = TurtleRef(**ref_kwargs).run(bars)
    sim = ReferenceSimulator(RefConfig(risk_pct=risk, notional_cap=cap))
    trades = sim.run(bars, signals)
    return sim, trades


def _engine_params(**overrides):
    return {k: v for k, v in overrides.items()}


def _check(tmp_path, frame, label, *, params=None, risk=0.02, **ref_kwargs):
    bars = bar_list(frame)
    start = frame["timestamp"].iloc[0].strftime("%Y-%m-%d")
    end = frame["timestamp"].iloc[-1].strftime("%Y-%m-%d")
    sim, ref_trades = _ref(bars, risk=risk, **ref_kwargs)
    run = run_engine(
        STRATEGY,
        frame,
        timeframe="1d",
        start=start,
        end=end,
        risk_pct=risk,
        position_pct=1.0,
        leverage=4.0,
        params=params,
        tmp_path=tmp_path,
    )
    assert_parity(run, ref_trades, sim.realized_by_time, label)
    return sim, ref_trades, run


# ───────────────────────── real ETF data ────────────────────────────────────


@MTM_SIZING_BUG
@pytest.mark.parametrize("fixture", ["spy_1d.csv", "gld_1d.csv"])
def test_turtle_system1_real_etf_parity(tmp_path, fixture):
    frame = frame_from_fixture(fixture)
    sim, trades, run = _check(tmp_path, frame, fixture)
    assert len(trades) > 250  # the real-data run is substantial, not a handful
    assert len(run.trades) == len(trades)


# ───────────────────────── synthetic event classes ──────────────────────────


def synthetic_frame(seed: int, n: int = 420) -> pd.DataFrame:
    """Regime-switching random walk with overnight gaps and fat ranges."""
    rng = random.Random(seed)
    close = 100.0
    drift = 0.0
    rows = []
    for i in range(n):
        if i % 60 == 0:
            drift = rng.choice([-0.01, -0.004, 0.0, 0.004, 0.01])
        gap = 0.0
        if rng.random() < 0.10:
            gap = rng.choice([-1, 1]) * rng.uniform(0.02, 0.06)
        open_ = close * (1 + gap + rng.gauss(0, 0.002))
        move = drift + rng.gauss(0, 0.012)
        new_close = open_ * (1 + move)
        span = abs(rng.gauss(0, 0.012)) * open_
        high = max(open_, new_close) + span * rng.random()
        low = min(open_, new_close) - span * rng.random()
        rows.append((open_, high, low, new_close))
        close = new_close
    ts = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [round(r[0], 4) for r in rows],
            "high": [round(r[1], 4) for r in rows],
            "low": [round(r[2], 4) for r in rows],
            "close": [round(r[3], 4) for r in rows],
            "volume": 1000.0,
        }
    )


SEEDS = list(range(1, 13))


def _stats(sim, trades, bars):
    open_by_time = {b.time: b for b in bars}
    gap_through = same_bar = 0
    for t in trades:
        leg = t.legs[-1]
        if leg.reason != "stop_loss":
            continue
        if leg.time == t.entry_time:
            same_bar += 1
        bar = open_by_time[leg.time]
        beyond = bar.open < leg.price if t.direction == "LONG" else bar.open > leg.price
        if beyond:
            gap_through += 1
    return {
        "gap_through": gap_through,
        "same_bar": same_bar,
        "wrong_side": sum("wrong side" in s for s in sim.skipped),
        "short": sum(t.direction == "SHORT" for t in trades),
        "stops": sum(t.legs[-1].reason == "stop_loss" for t in trades),
        "signal": sum(t.legs[-1].reason == "signal" for t in trades),
    }


@pytest.mark.parametrize("seed", SEEDS)
def test_turtle_single_unit_synthetic_parity(tmp_path, seed):
    """One unit at a time: every sizing input is a realized-equity input."""
    frame = synthetic_frame(seed)
    params = dict(SHORT_PARAMS, max_units=1)
    _check(tmp_path, frame, f"seed{seed}", params=params, max_units=1, **SHORT_PARAMS)


def test_turtle_single_unit_synthetic_covers_event_classes(tmp_path):
    total = {"gap_through": 0, "same_bar": 0, "wrong_side": 0, "short": 0, "stops": 0, "signal": 0}
    for seed in SEEDS:
        frame = synthetic_frame(seed)
        bars = bar_list(frame)
        sim, trades = _ref(bars, max_units=1, **SHORT_PARAMS)
        for key, value in _stats(sim, trades, bars).items():
            total[key] += value
    # the scenarios must actually exercise what they claim to
    assert total["gap_through"] >= 3, total
    assert total["same_bar"] >= 3, total
    assert total["wrong_side"] >= 1, total
    assert total["short"] >= 10, total
    assert total["stops"] >= 20 and total["signal"] >= 20, total


@MTM_SIZING_BUG
@pytest.mark.parametrize("seed", SEEDS[:6])
def test_turtle_pyramiding_synthetic_parity(tmp_path, seed):
    frame = synthetic_frame(seed)
    sim, trades, run = _check(tmp_path, frame, f"seed{seed}", params=dict(SHORT_PARAMS), **SHORT_PARAMS)
    assert trades


def test_turtle_pyramiding_reaches_four_units_and_hits_risk_cap():
    """Reference-only pin: the scenario set really stacks 4 units and trips the 5 % cap."""
    deepest = 0
    capped = 0
    for seed in SEEDS:
        bars = bar_list(synthetic_frame(seed))
        sim, trades = _ref(bars, **SHORT_PARAMS)
        by_exit: dict[str, int] = {}
        for t in trades:
            by_exit[t.legs[-1].time] = by_exit.get(t.legs[-1].time, 0) + 1
        deepest = max(deepest, max(by_exit.values(), default=0))
        capped += sum("sized to zero" in s for s in sim.skipped)
    assert deepest >= 3
    assert capped >= 1

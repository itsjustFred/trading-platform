"""Parity: platform engine vs an independent reference on the 5-minute ORB.

Fixture: Dukascopy USA500 (S&P 500 CFD) 1-minute data resampled to 5-minute
regular-session bars, 2026-03-20..2026-05-15 (38 complete sessions).  The
published study trades QQQ 2016-2023; the instrument and window differ, see
``docs/reports/parity-web-strategies.md``.

Each trade is one position per session, so no concurrent-unit sizing is
involved (the stop distance ``0.1 * ATR14`` is tiny, so the 4x leverage
ceiling binds on most entries - the tests assert that this path is hit).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from tests.parity.compare import assert_parity
from tests.parity.engine_harness import REPO_ROOT, bar_list, frame_from_fixture, run_engine
from tests.parity.reference_execution import RefConfig, ReferenceSimulator
from tests.parity.reference_orb import orb_signals

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "zarattini_orb" / "zarattini_orb.py"


def _run(tmp_path, frame, label):
    bars = bar_list(frame)
    sim = ReferenceSimulator(RefConfig(risk_pct=0.01, notional_cap=4.0))
    trades = sim.run(bars, orb_signals(bars))
    run = run_engine(
        STRATEGY,
        frame,
        timeframe="5m",
        start=frame["timestamp"].iloc[0].strftime("%Y-%m-%d"),
        end=frame["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
        risk_pct=0.01,
        position_pct=None,
        leverage=4.0,
        tmp_path=tmp_path,
    )
    assert_parity(run, trades, sim.realized_by_time, label)
    return sim, trades, run


def test_orb_real_usa500_5m_parity(tmp_path):
    frame = frame_from_fixture("usa500_5m_rth.csv.gz")
    sim, trades, run = _run(tmp_path, frame, "usa500")
    assert len(trades) == 24
    assert len(run.trades) == 24
    reasons = {t.legs[-1].reason for t in trades}
    assert reasons == {"stop_loss", "signal"}
    # the published 4x leverage ceiling binds: notional == 4 x equity on most entries
    capped = sum(1 for t in trades if t.quantity * t.entry_price > 3.9 * 100_000 * 0.8)
    assert capped >= 10


def synthetic_sessions(seed: int, days: int = 24) -> pd.DataFrame:
    """78-bar sessions with a volatile open and occasional gaps at the second candle."""
    rng = random.Random(seed)
    rows = []
    stamps = []
    price = 1000.0
    session_start = pd.Timestamp("2025-01-06 09:30", tz="America/New_York")
    for d in range(days):
        day = session_start + pd.Timedelta(days=d)
        if day.dayofweek >= 5:
            continue
        price *= 1 + rng.gauss(0, 0.004)
        sigma = 0.0012
        for k in range(78):
            open_ = price
            if k == 1 and rng.random() < 0.35:
                open_ *= 1 + rng.choice([-1, 1]) * rng.uniform(0.003, 0.008)
            elif k > 1 and rng.random() < 0.03:
                open_ *= 1 + rng.choice([-1, 1]) * rng.uniform(0.002, 0.006)
            move = rng.gauss(0, sigma * (3 if k < 3 else 1))
            close = open_ * (1 + move)
            span = abs(rng.gauss(0, sigma)) * open_
            high = max(open_, close) + span * rng.random()
            low = min(open_, close) - span * rng.random()
            rows.append((round(open_, 3), round(high, 3), round(low, 3), round(close, 3)))
            stamps.append((day + pd.Timedelta(minutes=5 * k)).tz_convert("UTC"))
            price = close
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": 100.0,
        }
    )


@pytest.mark.parametrize("seed", range(1, 9))
def test_orb_synthetic_parity(tmp_path, seed):
    frame = synthetic_sessions(seed)
    _run(tmp_path, frame, f"seed{seed}")


def test_orb_synthetic_covers_event_classes():
    gap_through = same_bar = wrong_side = shorts = longs = 0
    for seed in range(1, 9):
        frame = synthetic_sessions(seed)
        bars = bar_list(frame)
        sim = ReferenceSimulator(RefConfig(risk_pct=0.01, notional_cap=4.0))
        trades = sim.run(bars, orb_signals(bars))
        by_time = {b.time: b for b in bars}
        wrong_side += sum("wrong side" in s for s in sim.skipped)
        for t in trades:
            longs += t.direction == "LONG"
            shorts += t.direction == "SHORT"
            leg = t.legs[-1]
            if leg.reason != "stop_loss":
                continue
            same_bar += leg.time == t.entry_time
            bar = by_time[leg.time]
            gap_through += (bar.open < leg.price) if t.direction == "LONG" else (bar.open > leg.price)
    assert wrong_side >= 2
    assert same_bar >= 3
    assert gap_through >= 1
    assert longs >= 10 and shorts >= 10

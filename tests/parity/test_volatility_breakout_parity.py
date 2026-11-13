"""Parity: engine vs an independent reference on the EXISTING ``volatility_breakout``.

``volatility_breakout`` already implements the Larry-Williams style idea
(session open +/- k x prior-day range measure) as an intraday ATR breakout with a
protective stop at the session open, so it is verified here instead of adding a
duplicate Williams strategy.  Fixture: the same USA500 5-minute sessions as the
ORB test (the fixture has 38 sessions; the strategy's day-based ATR warm-up
leaves 32).
"""

from __future__ import annotations

import pytest

from tests.parity.compare import assert_parity
from tests.parity.engine_harness import REPO_ROOT, bar_list, frame_from_fixture, run_engine
from tests.parity.reference_execution import RefConfig, ReferenceSimulator
from tests.parity.reference_vola import vola_signals
from tests.parity.synthetic import synthetic_sessions

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "volatility_breakout" / "volatility_breakout.py"


def _run(tmp_path, frame, label):
    bars = bar_list(frame)
    sim = ReferenceSimulator(RefConfig(risk_pct=0.01, notional_cap=4.0))
    trades = sim.run(bars, vola_signals(bars))
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
    assert_parity(run, trades, sim.curve_by_time, label)
    return sim, trades, run


def test_volatility_breakout_real_usa500_parity(tmp_path):
    frame = frame_from_fixture("usa500_5m_rth.csv.gz")
    sim, trades, run = _run(tmp_path, frame, "usa500")
    assert len(trades) >= 20
    assert len(run.trades) == len(trades)
    assert {t.legs[-1].reason for t in trades} >= {"stop_loss", "signal"}


@pytest.mark.parametrize("seed", range(1, 9))
def test_volatility_breakout_synthetic_parity(tmp_path, seed):
    _run(tmp_path, synthetic_sessions(seed, days=26), f"seed{seed}")


def test_volatility_breakout_synthetic_covers_event_classes():
    gap_through = same_bar = wrong_side = shorts = longs = 0
    for seed in range(1, 9):
        frame = synthetic_sessions(seed, days=26)
        bars = bar_list(frame)
        sim = ReferenceSimulator(RefConfig(risk_pct=0.01, notional_cap=4.0))
        trades = sim.run(bars, vola_signals(bars))
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
            # rule A: a gap through the stop fills AT the bar's open
            gap_through += leg.time != t.entry_time and bar.open == leg.price
    assert longs >= 5 and shorts >= 5
    assert wrong_side + same_bar + gap_through >= 3

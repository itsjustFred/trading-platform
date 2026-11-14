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

import pytest

from tests.parity.compare import assert_parity
from tests.parity.engine_harness import REPO_ROOT, bar_list, frame_from_fixture, run_engine
from tests.parity.reference_execution import RefConfig, ReferenceSimulator
from tests.parity.reference_orb import orb_signals
from tests.parity.synthetic import synthetic_sessions

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "zarattini_orb" / "zarattini_orb.py"


def _run(tmp_path, frame, label, *, signal_exit_fill="next_open"):
    bars = bar_list(frame)
    sim = ReferenceSimulator(
        RefConfig(risk_pct=0.01, notional_cap=4.0, signal_exit_fill=signal_exit_fill)
    )
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
        signal_exit_fill=signal_exit_fill,
        tmp_path=tmp_path,
    )
    assert_parity(run, trades, sim.curve_by_time, label)
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
            # rule A: a gap through the stop fills AT the bar's open
            gap_through += leg.time != t.entry_time and bar.open == leg.price
    assert wrong_side >= 2
    assert same_bar >= 3
    assert gap_through >= 1
    assert longs >= 10 and shorts >= 10


@pytest.mark.parametrize("seed", [1, 2])
def test_orb_legacy_close_exit_parity(tmp_path, seed):
    """Explicit opt-in ``signal_exit_fill="close"``: flat at the session's last close."""
    sim, trades, run = _run(
        tmp_path, synthetic_sessions(seed), f"close-seed{seed}", signal_exit_fill="close"
    )
    assert any(t.legs[-1].reason == "signal" for t in trades)


def test_orb_default_signal_exit_is_next_session_open(tmp_path):
    """Rule B: the session-end signal exit fills at the NEXT bar's open (overnight)."""
    frame = synthetic_sessions(1)
    sim, trades, run = _run(tmp_path, frame, "next-open")
    by_time = {b.time: b for b in bar_list(frame)}
    signal_trades = [t for t in trades if t.legs[-1].reason == "signal"]
    assert signal_trades
    for t in signal_trades:
        leg = t.legs[-1]
        assert leg.price == by_time[leg.time].open
        assert leg.time != t.entry_time


def _tail_cut_with_deferred_exit():
    """First (seed, cut) whose truncated frame ends on a session's last bar with the
    position still open, found with the (fast) reference alone."""
    for seed in range(1, 9):
        frame = synthetic_sessions(seed)
        stamps = frame["timestamp"].dt.tz_convert("America/New_York").dt.date
        last_of_day = [k for k in range(len(frame) - 1) if stamps.iloc[k] != stamps.iloc[k + 1]]
        for k in last_of_day[3:]:
            cut = frame.iloc[: k + 1].reset_index(drop=True)
            bars = bar_list(cut)
            sim = ReferenceSimulator(RefConfig(risk_pct=0.01, notional_cap=4.0))
            sim.run(bars, orb_signals(bars))
            if sim.signal_exit_deferred_to_end:
                return cut
    raise AssertionError("no truncation exercises the deferred last-bar signal exit")


def test_orb_last_bar_signal_exit_defers_to_end_of_data(tmp_path):
    """A signal exit on the final bar has no next open: end-of-data close, flagged."""
    frame = _tail_cut_with_deferred_exit()
    sim, trades, run = _run(tmp_path, frame, "tail")
    assert sim.signal_exit_deferred_to_end and run.deferred_to_end == 1
    assert trades[-1].legs[-1].reason == "end_of_data"
    assert trades[-1].legs[-1].price == bar_list(frame)[-1].close

"""Parity: platform ``antonacci_global_equity_momentum`` vs an independent GEM reference.

The platform runs one symbol per ``generate_signals`` call, so the GEM port
reads its peers' closes from ``peer_close_<SYMBOL>`` columns plus a ``slot``
column (``attach_peer_closes``).  The test attaches them with the port's
mechanical helper (no trading logic there) and runs ONE multi-symbol engine
run; the adapter replays the three mutually exclusive slots on one shared
portfolio equity.  The reference decides on month-end closes of its own and
simulates the rotation on one shared cash balance.
"""
from __future__ import annotations

import importlib.util

import pytest

from tests.parity.published_compare import bars_of, compare
from tests.parity.published_harness import REPO_ROOT, load_fixture, run_engine
from tests.parity.ref_published import gem_targets, simulate

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "antonacci_global_equity_momentum" / "gem_dual_momentum.py"
START = "2007-05-30"


def _port():
    spec = importlib.util.spec_from_file_location("gem_port_under_test", STRATEGY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path, non_us: str, *, fee_rate=0.0, start=START):
    params = {"us_symbol": "SPY", "non_us_symbol": non_us, "bond_symbol": "AGG", "cash_symbol": "BIL"}
    frames = {name: load_fixture(name) for name in ("SPY", non_us, "AGG", "BIL")}
    attached = _port().attach_peer_closes(frames, params)
    engine_frames = {s: attached[s] for s in ("SPY", non_us, "AGG")}  # BIL is only the hurdle
    run = run_engine(
        STRATEGY, engine_frames, tmp_path=tmp_path, start=start,
        end="2025-12-30", params=params, fee_rate=fee_rate,
    )
    ref_frames = {slot: frames[sym][frames[sym]["timestamp"] >= start].reset_index(drop=True)
                  for slot, sym in (("us", "SPY"), ("non_us", non_us), ("bond", "AGG"), ("cash", "BIL"))}
    # The reference sees the same history the engine does: full series from the
    # fixture start (momentum needs the 12 months before ``start`` too).
    full = {slot: bars_of(frames[sym]) for slot, sym in
            (("us", "SPY"), ("non_us", non_us), ("bond", "AGG"), ("cash", "BIL"))}
    del ref_frames
    targets = gem_targets(full)
    traded = {slot: [b for b in full[slot] if b.day >= start] for slot in targets}
    ref = simulate(traded, targets, fee_rate=fee_rate)
    return run, ref, compare(run, ref, {"us": "SPY", "non_us": non_us, "bond": "AGG"}, qty_abs=5.1e-5, rel=1e-6)


@pytest.mark.parametrize("non_us", ["VEU", "EFA"])
def test_gem_engine_equals_reference(tmp_path, non_us):
    run, ref, diffs = _run(tmp_path, non_us)
    assert not diffs, "\n".join(diffs[:10])
    assert {t.symbol for t in run.trades} >= {"SPY", "AGG"}  # at least two slots rotated in


def test_gem_engine_equals_reference_with_costs(tmp_path):
    run, ref, diffs = _run(tmp_path, "VEU", fee_rate=0.0005)
    assert not diffs, "\n".join(diffs[:10])
    assert sum(t.fees for t in run.trades) > 0

"""Parity: platform ``faber_tactical_asset_allocation`` vs an independent Faber (2007) reference."""
from __future__ import annotations

import pytest

from tests.parity.published_compare import bars_of, compare
from tests.parity.published_harness import REPO_ROOT, load_fixture, run_engine
from tests.parity.ref_published import faber_target, simulate

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "faber_tactical_asset_allocation" / "faber_tactical_asset_allocation.py"


def _check(tmp_path, fixture, *, fee_rate=0.0, start=None, end=None):
    frame = load_fixture(fixture)
    if start:
        frame = frame[frame["timestamp"] >= start]
    if end:
        frame = frame[frame["timestamp"] <= end]
    frame = frame.reset_index(drop=True)
    bars = bars_of(frame)
    ref = simulate({"x": bars}, {"x": faber_target(bars)}, fee_rate=fee_rate)
    run = run_engine(
        STRATEGY, {"SYN": frame}, tmp_path=tmp_path,
        start=bars[0].day, end=bars[-1].day, fee_rate=fee_rate,
    )
    return run, ref, compare(run, ref, {"x": "SYN"})


@pytest.mark.parametrize("fixture", ["spy", "sp500tr", "gspc"])
def test_faber_engine_equals_reference(tmp_path, fixture):
    run, ref, diffs = _check(tmp_path, fixture)
    assert not diffs, "\n".join(diffs[:10])
    assert len(ref.trades) >= 10


def test_faber_engine_equals_reference_with_costs(tmp_path):
    run, ref, diffs = _check(tmp_path, "spy", fee_rate=0.001)
    assert not diffs, "\n".join(diffs[:10])


def test_faber_comparison_detects_a_wrong_rule(tmp_path):
    """Non-vacuity: a 9-month SMA on the engine side must NOT match the 10-month reference."""
    frame = load_fixture("spy")
    frame = frame[frame["timestamp"] >= "2005-01-01"].reset_index(drop=True)
    bars = bars_of(frame)
    ref = simulate({"x": bars}, {"x": faber_target(bars)})
    run = run_engine(
        STRATEGY, {"SYN": frame}, tmp_path=tmp_path, start=bars[0].day, end=bars[-1].day,
        params={"sma_months": 9},
    )
    assert compare(run, ref, {"x": "SYN"})

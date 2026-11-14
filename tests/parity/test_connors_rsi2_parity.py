"""Parity: platform ``connors_rsi2_mean_reversion`` vs an independent Connors/Alvarez RSI(2) reference."""
from __future__ import annotations

import pandas as pd
import pytest

from tests.parity.published_compare import bars_of, compare
from tests.parity.published_harness import REPO_ROOT, load_fixture, run_engine
from tests.parity.ref_published import connors_target, simulate, wilder_rsi

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STRATEGY = REPO_ROOT / "strategies" / "connors_rsi2_mean_reversion" / "connors_rsi2_mean_reversion.py"


def _check(tmp_path, frame, *, fee_rate=0.0):
    frame = frame.reset_index(drop=True)
    bars = bars_of(frame)
    ref = simulate({"x": bars}, {"x": connors_target(bars)}, fee_rate=fee_rate)
    run = run_engine(
        STRATEGY, {"SYN": frame}, tmp_path=tmp_path,
        start=bars[0].day, end=bars[-1].day, fee_rate=fee_rate,
    )
    return run, ref, compare(run, ref, {"x": "SYN"})


def test_connors_spy_engine_equals_reference(tmp_path):
    frame = load_fixture("spy")
    run, ref, diffs = _check(tmp_path, frame)
    assert not diffs, "\n".join(diffs[:10])
    assert len(ref.trades) > 100  # a real sample, not a handful


def test_connors_spy_engine_equals_reference_with_costs(tmp_path):
    frame = load_fixture("spy")
    frame = frame[frame["timestamp"] >= "2015-01-01"]
    run, ref, diffs = _check(tmp_path, frame, fee_rate=0.0005)
    assert not diffs, "\n".join(diffs[:10])


def test_wilder_rsi_seed_choice_is_immaterial_after_warmup():
    """The reference's SMA-seeded Wilder RSI equals the ``ewm(alpha=1/2)`` form once warmed up.

    RSI(2) forgets its seed geometrically (0.5 per bar); after 60 bars the two
    conventions agree to double precision, so the only possible disagreement
    with a charting package is in the first handful of bars.
    """
    closes = load_fixture("spy")["close"].astype(float).reset_index(drop=True)
    ref = wilder_rsi(list(closes), 2)
    delta = closes.diff()
    up = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    down = (-delta).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    pandas_rsi = 100 - 100 / (1 + up / down)
    for i in range(60, len(closes)):
        if ref[i] is None or pd.isna(pandas_rsi[i]):
            continue
        assert abs(ref[i] - pandas_rsi[i]) < 1e-9


def test_connors_comparison_detects_a_wrong_rule(tmp_path):
    """Non-vacuity: RSI(2) < 5 on the engine side must NOT match the RSI(2) < 10 reference."""
    frame = load_fixture("spy")
    frame = frame[frame["timestamp"] >= "2015-01-01"].reset_index(drop=True)
    bars = bars_of(frame)
    ref = simulate({"x": bars}, {"x": connors_target(bars)})
    run = run_engine(
        STRATEGY, {"SYN": frame}, tmp_path=tmp_path, start=bars[0].day, end=bars[-1].day,
        params={"rsi_entry": 5.0},
    )
    assert compare(run, ref, {"x": "SYN"})

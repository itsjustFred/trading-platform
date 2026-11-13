"""Engine findings surfaced by the published-strategy parity work (minimal reproductions).

Fixed: the reporting equity curve is mark-to-market on every engine, so the
drawdown of an open position reaches ``max_drawdown_pct`` (see
``docs/cross-engine-backtest-contract.md`` section 4b and
``tests/test_mtm_equity.py`` for the per-engine coverage).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from runner.stats import compute_stats_for_result
from tests.parity.published_harness import REPO_ROOT, run_engine

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

FABER = REPO_ROOT / "strategies" / "faber_tactical_asset_allocation" / "faber_tactical_asset_allocation.py"


def test_max_drawdown_includes_open_position_drawdown(tmp_path):
    close = np.concatenate([np.full(22, 100.0 + 10 * m) for m in range(12)] + [np.full(22, 230.0)] * 4)
    close[22 * 13 + 5 : 22 * 13 + 15] = 138.0  # -40 % for ten bars, back to 230 before month end
    ts = pd.bdate_range("2020-01-01", periods=len(close), tz="UTC")
    frame = pd.DataFrame(
        {"timestamp": ts, "open": close, "high": close, "low": close, "close": close, "volume": 1.0}
    )
    run = run_engine(FABER, {"SYN": frame}, tmp_path=tmp_path, start="2020-01-01", end=str(ts[-1].date()))
    held = [t for t in run.trades if t.entry_time < ts[22 * 13 + 5].strftime("%Y-%m-%d") < t.exit_time]
    assert held, "the long hold must span the crash"
    assert compute_stats_for_result(run.result)["max_drawdown_pct"] <= -0.30

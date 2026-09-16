"""Risk-parity script regressions — mutation cycle 1 (engines pipeline).

Pins the numerics of ``scripts/run_risk_parity_backtest.py`` that the
mutation cycle showed to be test-blind:

- the textbook downside deviation of the non-lean Sortino path (A-L3);
- the exact 0.7 crisis equity factor in ``adjust_for_regime``;
- the 0.2 default shrinkage in ``calculate_covariance``.

(The A-H3 rebalance-timing mutation IS caught by
``tests/strategies/test_risk_parity.py::test_rebalance_weights_effective_next_day``,
so no test is added for it here.)
"""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from scripts.run_risk_parity_backtest import (
    TICKERS,
    adjust_for_regime,
    calculate_covariance,
    _portfolio_performance_stats,
)


def _drift_series() -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=61, freq="D")
    rets = np.tile([0.002, -0.001, 0.0015, -0.0005, 0.001], 12)[:60]
    vals = 100 * np.cumprod(np.r_[1.0, 1 + rets])
    return pd.Series(vals, index=idx)


def test_sortino_uses_textbook_downside_over_all_observations():
    # A-L3: downside deviation is the mean of squared downside vs the daily
    # risk-free target over ALL observations — not the std over negative
    # returns only (which reads ~2x higher on this series).
    stats = _portfolio_performance_stats(
        portfolio_value=_drift_series(), trades=[], attribution_pnls=[]
    )
    assert stats["sortino_ratio"] == pytest.approx(19.049409439666306)


def test_crisis_cuts_equity_by_factor_0_7():
    weights = {t: 1.0 / len(TICKERS) for t in TICKERS}
    adjusted = adjust_for_regime(weights, "crisis")
    # 4 equities x0.7, GLD/TLT/IEF x1.5, rest x1.0, renormalized:
    # SPY == (0.7/11) / (11.3/11).
    assert adjusted["SPY"] == pytest.approx(0.06194690265486725)
    assert sum(adjusted.values()) == pytest.approx(1.0)


def test_covariance_default_shrinkage_is_0_2():
    assert inspect.signature(calculate_covariance).parameters["shrinkage"].default == 0.2
    rng = np.random.default_rng(3)
    returns = rng.normal(0, 0.01, size=(11, 120))
    assert np.allclose(
        calculate_covariance(returns), calculate_covariance(returns, shrinkage=0.2)
    )
    assert not np.allclose(
        calculate_covariance(returns), calculate_covariance(returns, shrinkage=0.0)
    )

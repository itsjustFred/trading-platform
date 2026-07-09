"""
Unit tests for the Risk Parity Python reimplementation.

The original QuantConnect algorithm lives in
`strategy-comparisons/risk_parity/risk_parity_strategy.py`.  A native LEAN
backtest is not practical here (missing credentials / local equity data), so
the regression surface is the fallback script
`scripts/run_risk_parity_backtest.py`.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.run_risk_parity_backtest import (
    ASSETS,
    TICKERS,
    adjust_for_regime,
    apply_tail_risk_hedge,
    apply_volatility_scaling,
    calculate_covariance,
    calculate_risk_parity_weights,
    detect_correlation_regime,
)


def test_asset_catalog_covers_tickers():
    assert set(TICKERS) == set(ASSETS.keys())
    assert "SPY" in ASSETS
    assert ASSETS["SPY"]["class"] == "equity"


def test_calculate_covariance_shape_and_positive_semi_definite():
    rng = np.random.default_rng(42)
    returns = rng.normal(0, 0.01, size=(11, 120))
    cov = calculate_covariance(returns, shrinkage=0.2)
    assert cov.shape == (11, 11)
    # Symmetric and positive semi-definite
    np.testing.assert_allclose(cov, cov.T, atol=1e-12)
    eigvals = np.linalg.eigvalsh(cov)
    assert eigvals.min() >= -1e-10


def test_risk_parity_weights_sum_to_one_and_respect_bounds():
    rng = np.random.default_rng(7)
    returns = rng.normal(0, 0.01, size=(11, 120))
    cov = calculate_covariance(returns, shrinkage=0.2)
    weights = calculate_risk_parity_weights(
        cov,
        min_weight=0.05,
        max_weight=0.25,
        tickers=TICKERS,
    )
    assert set(weights.keys()) == set(TICKERS)
    total = sum(weights.values())
    assert pytest.approx(total, abs=1e-6) == 1.0
    for w in weights.values():
        assert 0.05 - 1e-6 <= w <= 0.25 + 1e-6


def _factor_returns(n_assets: int, n_days: int, beta: float, noise: float) -> np.ndarray:
    rng = np.random.default_rng(123)
    market = rng.normal(0, 0.01, size=n_days)
    idio = rng.normal(0, noise, size=(n_assets, n_days))
    return beta * market + idio


def test_detect_correlation_regime():
    # Normal: recent period has the same factor structure as the historical period,
    # so average correlations are comparable.
    historical = _factor_returns(5, 60, beta=0.5, noise=0.008)
    recent_normal = _factor_returns(5, 30, beta=0.5, noise=0.008)
    normal_returns = np.hstack([historical, recent_normal])
    assert detect_correlation_regime(normal_returns) == "normal"

    # Crisis: recent period is driven by a single common factor (high correlation),
    # while the historical period remains diversified.
    recent_crisis = _factor_returns(5, 30, beta=0.95, noise=0.002)
    crisis_returns = np.hstack([historical, recent_crisis])
    regime = detect_correlation_regime(crisis_returns)
    assert regime in ("elevated", "crisis")


def test_adjust_for_regime_reduces_equity_in_crisis():
    weights = {t: 1.0 / len(TICKERS) for t in TICKERS}
    adjusted = adjust_for_regime(weights, "crisis")
    equity_total = sum(adjusted[t] for t in TICKERS if ASSETS[t]["class"] == "equity")
    original_equity_total = sum(weights[t] for t in TICKERS if ASSETS[t]["class"] == "equity")
    assert equity_total < original_equity_total


def test_apply_volatility_scaling_changes_weights():
    rng = np.random.default_rng(3)
    returns = rng.normal(0, 0.01, size=(11, 120))
    cov = calculate_covariance(returns, shrinkage=0.2)
    weights = {t: 1.0 / len(TICKERS) for t in TICKERS}
    scaled = apply_volatility_scaling(
        weights,
        cov,
        target_vol=0.10,
        max_leverage=1.5,
        max_weight=0.25,
        tickers=TICKERS,
    )
    # Scaling should move weights away from the equal-weight baseline
    assert any(
        not np.isclose(scaled[t], weights[t], atol=1e-6) for t in TICKERS
    )


def test_apply_tail_risk_hedge_reduces_equity_exposure():
    weights = {t: 1.0 / len(TICKERS) for t in TICKERS}
    hedged = apply_tail_risk_hedge(weights)
    equity_before = sum(weights[t] for t in TICKERS if ASSETS[t]["class"] == "equity")
    equity_after = sum(hedged[t] for t in TICKERS if ASSETS[t]["class"] == "equity")
    assert equity_after < equity_before

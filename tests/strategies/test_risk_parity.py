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
import pandas as pd
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
    first_available_trading_dates,
    latest_correlation_regime,
    run_backtest,
    weekly_correlation_regimes,
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


def test_weekly_correlation_regime_uses_latest_week_start():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-08", "2024-01-09"])
    scheduled = pd.to_datetime(["2024-01-01", "2024-01-08"])

    assert first_available_trading_dates(dates, scheduled) == [dates[0], dates[2]]
    assert latest_correlation_regime({dates[0]: "normal", dates[2]: "elevated"}, dates[3]) == "elevated"


def test_weekly_correlation_regimes_returns_scheduled_state():
    dates = pd.bdate_range("2024-01-02", periods=140)
    data = pd.DataFrame(
        np.tile(np.linspace(100.0, 120.0, len(dates)), (len(TICKERS), 1)).T,
        index=dates,
        columns=TICKERS,
    )
    returns = data.pct_change().dropna()

    regimes = weekly_correlation_regimes(returns)

    assert regimes
    assert all(date in returns.index for date in regimes)
    assert set(regimes.values()).issubset({"normal", "elevated", "crisis"})


def test_run_backtest_skips_small_rebalance_changes_and_fills_material_changes_at_close(monkeypatch):
    dates = pd.bdate_range("2023-12-29", "2024-03-15")
    dates = dates[dates != pd.Timestamp("2024-01-01")]
    close_prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    open_prices = close_prices + 1.0

    baseline = {ticker: 1.0 / len(TICKERS) for ticker in TICKERS}
    small_shift = dict(baseline)
    small_shift["SPY"] += 0.01
    small_shift["QQQ"] -= 0.01
    material_shift = dict(baseline)
    material_shift["SPY"] += 0.03
    material_shift["QQQ"] -= 0.03
    weight_sequence = iter([baseline, small_shift, material_shift])

    import scripts.run_risk_parity_backtest as risk_parity

    monkeypatch.setattr(risk_parity, "calculate_covariance", lambda *args, **kwargs: np.eye(len(TICKERS)))
    monkeypatch.setattr(risk_parity, "calculate_risk_parity_weights", lambda *args, **kwargs: next(weight_sequence))
    monkeypatch.setattr(risk_parity, "adjust_for_regime", lambda weights, regime: dict(weights))
    monkeypatch.setattr(
        risk_parity,
        "apply_volatility_scaling",
        lambda weights, cov, target_vol, max_leverage, max_weight, tickers: dict(weights),
    )
    monkeypatch.setattr(risk_parity, "apply_tail_risk_hedge", lambda weights: dict(weights))
    monkeypatch.setattr(risk_parity, "weekly_correlation_regimes", lambda returns: {})

    result = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=close_prices,
        open_prices=open_prices,
        warm_up_days=0,
    )

    trades = result["trades"]
    target_signals = result["portfolio_target_signals"]
    jan_trades = [trade for trade in trades if trade["exit_time"].startswith("2024-01-02")]
    feb_trades = [trade for trade in trades if trade["exit_time"].startswith("2024-02-01")]
    mar_trades = [trade for trade in trades if trade["exit_time"].startswith("2024-03-01")]

    assert [signal["timestamp"] for signal in target_signals] == [
        "2024-01-02T00:00:00Z",
        "2024-02-01T00:00:00Z",
        "2024-03-01T00:00:00Z",
    ]
    mar_target = target_signals[-1]
    assert mar_target["signal_id"] == "risk_parity:2024-03-01T00:00:00Z"
    assert mar_target["strategy_id"] == "risk_parity"
    assert mar_target["rebalance_threshold"] == pytest.approx(0.02)
    assert set(mar_target["target_weights"]) == set(TICKERS)
    assert mar_target["metadata"]["universe"] == list(TICKERS)

    assert len(jan_trades) == len(TICKERS)
    assert len(feb_trades) == 0
    assert len(mar_trades) == 2
    assert result["stats"]["total_trades"] == len(TICKERS) + 2
    assert result["stats"]["rebalance_attribution_win_rate"] == pytest.approx(0.0)
    assert result["stats"]["rebalance_attribution_net_profit"] == pytest.approx(0.0)

    mar_spy = next(trade for trade in mar_trades if trade["symbol"] == "SPY")
    mar_qqq = next(trade for trade in mar_trades if trade["symbol"] == "QQQ")
    assert mar_spy["meta"]["portfolio_target_signal_id"] == mar_target["signal_id"]
    assert mar_qqq["meta"]["portfolio_target_signal_id"] == mar_target["signal_id"]
    assert mar_spy["quantity"] == pytest.approx(0.03, abs=1e-6)
    assert mar_qqq["quantity"] == pytest.approx(0.03, abs=1e-6)
    assert mar_spy["entry_time"].startswith("2024-01-02")
    assert mar_qqq["entry_time"].startswith("2024-01-02")
    assert mar_spy["entry_price"] == pytest.approx(close_prices.loc[pd.Timestamp("2024-01-02"), "SPY"])
    assert mar_qqq["entry_price"] == pytest.approx(close_prices.loc[pd.Timestamp("2024-01-02"), "QQQ"])
    assert mar_spy["exit_price"] == pytest.approx(close_prices.loc[pd.Timestamp("2024-03-01"), "SPY"])
    assert mar_qqq["exit_price"] == pytest.approx(close_prices.loc[pd.Timestamp("2024-03-01"), "QQQ"])


def test_run_backtest_hedge_gated_until_sma200_ready(monkeypatch):
    """Tail hedge stays off until 200 bars since QC data start (LEAN IsReady).

    Regression test for the audit finding: with <200 bars since
    2015-08-04 the SMA(200) cannot be ready, so SPY far below its 200-day
    mean must NOT trigger the hedge (QC did not hedge 2016-02-01).
    """
    dates = pd.bdate_range("2015-08-04", periods=130)
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    # crash SPY on the last bars: price condition holds, readiness does not
    prices.loc[dates[-5:], "SPY"] = 50.0

    import scripts.run_risk_parity_backtest as risk_parity

    monkeypatch.setattr(
        risk_parity, "weekly_correlation_regimes", lambda returns: {}
    )
    result = run_backtest(
        start_date="2016-01-01",
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=len(dates) - 10,
    )
    assert result["portfolio_target_signals"], "expected at least one rebalance"
    for signal in result["portfolio_target_signals"]:
        assert signal["metadata"]["hedge_active"] is False


def test_run_backtest_rejects_warmup_longer_than_prices():
    dates = pd.bdate_range("2024-01-02", periods=3)
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)

    with pytest.raises(ValueError, match="warm_up_days"):
        run_backtest(
            start_date=str(dates[0].date()),
            end_date=str(dates[-1].date()),
            prices=prices,
            open_prices=prices,
            warm_up_days=len(prices),
        )


def test_run_backtest_risk_free_rate_lowes_sharpe_like_lean():
    """Sharpe/Sortino subtract an annual risk-free rate (LEAN parity).

    Audit finding: LEAN reports Sharpe 0.372 on a curve the reimplementation
    prices at 0.77 (RF=0). The difference is the risk-free rate LEAN
    subtracts (~4.2%/yr reproduces 0.372 on the QC order-ledger curve).
    """
    dates = pd.bdate_range("2024-01-02", periods=60)
    drift = np.linspace(100.0, 110.0, len(dates))
    drift[::7] -= 1.5  # weekly down-days so downside deviation is defined
    prices = pd.DataFrame(
        {ticker: drift for ticker in TICKERS}, index=dates,
    )

    base = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
    )
    with_rf = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
        risk_free_rate=0.05,
    )
    assert with_rf["stats"]["sharpe_ratio"] < base["stats"]["sharpe_ratio"]
    assert with_rf["stats"]["sortino_ratio"] < base["stats"]["sortino_ratio"]
    for key in ("total_return", "cagr", "max_drawdown", "total_trades"):
        assert with_rf["stats"][key] == base["stats"][key]


def test_lean_statistics_use_calendar_days_and_compounded_mean():
    """LEAN-exact stats: calendar-day sampling, (1+mean)^252 annualization.

    Verified against LEAN source (Common/Statistics/Statistics.cs): with
    weekend-flat calendar sampling the order-ledger curve prices at vol
    0.0872, exactly the reported QC value.
    """
    dates = pd.bdate_range("2024-01-02", periods=70)
    drift = np.linspace(100.0, 110.0, len(dates))
    drift[::7] -= 1.5
    prices = pd.DataFrame({ticker: drift for ticker in TICKERS}, index=dates)

    kwargs = dict(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
    )
    plain = run_backtest(**kwargs)
    lean = run_backtest(**kwargs, lean_statistics=True)
    # weekend-flat zeros dilute volatility vs trading-days-only sampling
    assert lean["stats"]["annualized_volatility"] < plain["stats"]["annualized_volatility"]
    # endpoints (return/drawdown/trades) are sampling-invariant
    for key in ("total_return", "cagr", "max_drawdown", "total_trades"):
        assert lean["stats"][key] == plain["stats"][key]

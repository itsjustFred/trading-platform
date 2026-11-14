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
    _simulate_portfolio_value,
    compounded_growth,
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
    # Two completed bars precede the first rebalance: decisions use data up to the previous close.
    dates = pd.bdate_range("2023-12-27", "2024-03-15")
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


def test_rebalance_weights_effective_next_day():
    """A-H3: Market-On-Close fills earn from the NEXT trading day.

    New weights set on the rebalance date must not capture the rebalance
    day's own return — the fill happens AT the close.
    """
    dates = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    returns = pd.DataFrame(0.0, index=dates, columns=TICKERS)
    returns["SPY"] = [0.0, 0.10, 0.0, 0.0]  # +10% on the rebalance day
    schedule = {dates[1]: {t: (1.0 if t == "SPY" else 0.0) for t in TICKERS}}
    portfolio_value = _simulate_portfolio_value(returns, 100_000.0, schedule)
    assert float(portfolio_value.iloc[-1]) == pytest.approx(100_000.0)


def test_held_weights_earn_next_day_return():
    """A-H3 complement: weights set on day T earn day T+1's return."""
    dates = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    returns = pd.DataFrame(0.0, index=dates, columns=TICKERS)
    returns["SPY"] = [0.0, 0.0, 0.10, 0.0]  # +10% the day after rebalance
    schedule = {dates[1]: {t: (1.0 if t == "SPY" else 0.0) for t in TICKERS}}
    portfolio_value = _simulate_portfolio_value(returns, 100_000.0, schedule)
    assert float(portfolio_value.iloc[-1]) == pytest.approx(110_000.0)


def test_pdf_gate_bounds_checked_in_reference_stats():
    """A-M8 (D7): the QC PDF summary gates the reimplementation snapshot.

    The top-level stats mirror the Python reimplementation (comparing them
    to themselves is always green), so the PDF summary (CAGR 7.6%, Sharpe
    0.4, Drawdown 17.7%) is stored as ``pdf_gate`` with a wide relative
    tolerance (±50%: no trade list, different LEAN methodology/fees/timing).
    A regenerated reference that drifts past the gate fails here.
    """
    import json

    ref_path = (
        Path(__file__).parent.parent.parent
        / "strategy-comparisons"
        / "risk_parity"
        / "risk_parity_reference.json"
    )
    reference = json.loads(ref_path.read_text(encoding="utf-8"))
    gate = reference["raw_metadata"]["pdf_gate"]
    assert gate["source"].endswith("risk_parity_report_quantconnect.pdf")
    tolerance = float(gate["tolerance_rel"])
    assert tolerance == pytest.approx(0.5)

    stats = reference["stats"]
    checks = {
        "cagr": (stats["cagr"], float(gate["cagr"])),
        "sharpe_ratio": (stats["sharpe_ratio"], float(gate["sharpe_ratio"])),
        "max_drawdown": (abs(stats["max_drawdown"]), float(gate["max_drawdown"])),
    }
    for name, (actual, expected) in checks.items():
        rel_diff = abs(actual - expected) / expected
        assert rel_diff <= tolerance, (
            f"{name}: reimplementation {actual} vs PDF gate {expected} "
            f"(rel. diff {rel_diff:.1%} > {tolerance:.0%})"
        )


def test_download_prices_loads_history_from_qc_start(monkeypatch):
    """A-M6: downloads start before the QC data start, not at the backtest start.

    QC's History() is unbounded by the algorithm data start, so a 200-calendar-day
    lookback margin (130 bars) precedes 2015-08-04 -> 2015-01-16.
    """
    import scripts.run_risk_parity_backtest as risk_parity

    seen: dict[str, str] = {}

    def fake_download(tickers, start, end, auto_adjust, progress):
        seen["start"] = start
        idx = pd.bdate_range("2015-08-04", periods=300)
        frame = pd.DataFrame(100.0, index=idx, columns=tickers)
        return pd.concat({"Close": frame, "Open": frame}, axis=1)

    monkeypatch.setattr(risk_parity.yf, "download", fake_download)
    risk_parity.download_prices("2016-01-01", "2016-06-01")
    assert seen["start"] == "2015-01-16"


def test_non_lean_sortino_uses_full_sample_downside():
    """A-L3: non-lean Sortino divides by the all-observation downside."""
    dates = pd.bdate_range("2024-01-02", periods=60)
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
    # Textbook downside (all observations) is smaller than the
    # negatives-only sample std, so non-lean Sortino exceeds Sharpe here
    # while the lean recipe stays LEAN-faithful (verified vs QC 0.39).
    assert plain["stats"]["sortino_ratio"] > 0.0
    lean = run_backtest(**kwargs, lean_statistics=True, risk_free_rate=0.025)
    assert lean["stats"]["sortino_ratio"] > 0.0


# --- QuantConnect parity regressions (Sleepy Tan Coyote trade-by-trade comparison) ---


def _mock_flat_pipeline(monkeypatch, weights_fn):
    """Neutralise covariance/solver/regime/hedge so only timing + accounting remain."""
    import scripts.run_risk_parity_backtest as risk_parity

    monkeypatch.setattr(risk_parity, "calculate_covariance", lambda *a, **k: np.eye(len(TICKERS)))
    monkeypatch.setattr(risk_parity, "calculate_risk_parity_weights", lambda *a, **k: weights_fn())
    monkeypatch.setattr(risk_parity, "adjust_for_regime", lambda weights, regime: dict(weights))
    monkeypatch.setattr(
        risk_parity,
        "apply_volatility_scaling",
        lambda weights, cov, target_vol, max_leverage, max_weight, tickers: dict(weights),
    )
    monkeypatch.setattr(risk_parity, "apply_tail_risk_hedge", lambda weights: dict(weights))
    monkeypatch.setattr(risk_parity, "weekly_correlation_regimes", lambda returns: {})


def _first_rebalance_after(dates, warm_up_days):
    month_starts = pd.date_range(dates[0], dates[-1], freq="MS")
    month_starts = month_starts[month_starts > dates[warm_up_days]]
    return next(d for d in dates if d >= month_starts[0])


@pytest.mark.parametrize(
    ("crash_offset", "expected_hedge"),
    [
        (0, False),  # SPY crashes ON the rebalance day: QC's 10:00 decision cannot see it
        (-1, True),  # SPY crashed the session BEFORE: visible at 10:00, hedge must fire
    ],
)
def test_hedge_decision_uses_previous_close_not_rebalance_day_close(
    monkeypatch, crash_offset, expected_hedge
):
    """QC runs Rebalance at 10:00 on daily data: SPY price/SMA200 are the PREVIOUS close.

    Hand computation (constant 100, one SPY bar crashed to 50 inside the 200-bar window):
    SMA200 = (199 * 100 + 50) / 200 = 99.75 and 50 < 0.98 * 99.75 = 97.755, so the hedge
    fires iff the crash bar is the last completed bar before the rebalance day.
    """
    dates = pd.bdate_range("2015-08-04", periods=330)
    warm_up_days = 215
    reb = _first_rebalance_after(dates, warm_up_days)
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    prices.loc[dates[dates.get_loc(reb) + crash_offset], "SPY"] = 50.0
    _mock_flat_pipeline(monkeypatch, lambda: {t: 1.0 / len(TICKERS) for t in TICKERS})

    result = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=warm_up_days,
    )

    first = result["portfolio_target_signals"][0]
    assert first["timestamp"] == reb.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert first["metadata"]["hedge_active"] is expected_hedge


def test_weekly_regime_window_ends_before_the_regime_date():
    """QC's 09:45 regime callback sees data up to the previous close only."""
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2024-01-02", periods=200)
    returns = pd.DataFrame(rng.normal(0, 0.01, (200, len(TICKERS))), index=dates, columns=TICKERS)

    regimes = weekly_correlation_regimes(returns)

    assert regimes
    for regime_date, regime in regimes.items():
        loc = returns.index.get_loc(regime_date)
        expected = detect_correlation_regime(returns.iloc[max(0, loc - 120) : loc].T.values)
        assert regime == expected


def test_rebalancing_starts_at_start_date_when_history_precedes_it(monkeypatch):
    """QC's SetWarmUp period precedes SetStartDate: first order on the first session of 2016.

    Counting ``warm_up_days`` bars from the first history row (2015-08-04) instead skipped
    the 2016-01/02/03 rebalances (first rebalance 2016-04-01 in the audit). The equity
    curve starts at the last bar before the start date with the initial cash.
    """
    dates = pd.bdate_range("2015-08-04", "2016-03-15")
    dates = dates[dates != pd.Timestamp("2016-01-01")]  # market holiday
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    _mock_flat_pipeline(monkeypatch, lambda: {t: 1.0 / len(TICKERS) for t in TICKERS})

    result = run_backtest(
        start_date="2016-01-01",
        end_date="2016-03-15",
        prices=prices,
        open_prices=prices,
        warm_up_days=150,
    )

    assert [s["timestamp"][:10] for s in result["portfolio_target_signals"]] == [
        "2016-01-04",
        "2016-02-01",
        "2016-03-01",
    ]
    curve = result["portfolio_value"]
    assert curve.index[0] == pd.Timestamp("2015-12-31")
    assert float(curve.iloc[0]) == pytest.approx(1_000_000.0)


def test_trade_gate_uses_drifted_holding_weight(monkeypatch):
    """QC gates on HoldingsValue / TotalPortfolioValue, which drifts with prices.

    Hand computation: 11 legs at 1/11. SPY +25% -> SPY value 1.25/11, portfolio
    1 + 0.25/11 = 11.25/11, drifted SPY weight = 1.25 / 11.25 = 1/9. Back to the 1/11 target
    the delta is 1/11 - 1/9 = -2/99 = -0.0202 (> 2% gate, so SPY is sold) while every other
    leg drifted to 1/11.25 = 0.0889 (delta +0.0020, kept). Comparing against the stale
    last-target weight would see delta 0 for all legs and never trade.
    """
    dates = pd.bdate_range("2023-12-27", "2024-03-15")
    dates = dates[dates != pd.Timestamp("2024-01-01")]
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    prices.loc[dates >= pd.Timestamp("2024-01-16"), "SPY"] = 125.0
    _mock_flat_pipeline(monkeypatch, lambda: {t: 1.0 / len(TICKERS) for t in TICKERS})

    result = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
    )

    by_day: dict[str, list[dict]] = {}
    for trade in result["trades"]:
        by_day.setdefault(trade["exit_time"][:10], []).append(trade)
    assert len(by_day["2024-01-02"]) == len(TICKERS)
    assert [t["symbol"] for t in by_day["2024-02-01"]] == ["SPY"]
    spy = by_day["2024-02-01"][0]
    assert spy["meta"]["previous_weight"] == pytest.approx(1 / 9, abs=1e-7)
    assert spy["meta"]["delta_weight"] == pytest.approx(-2 / 99, abs=1e-7)
    # March: the other legs sit at 1/11.25 (+0.20% off target, inside the gate), SPY is on target.
    assert "2024-03-01" not in by_day


def test_portfolio_value_is_buy_and_hold_between_rebalances():
    """Hand computation: 50% SPY, then SPY x1.25 and x0.8 (net 1.0).

    Buy-and-hold: 1 + 0.5 * (1.25 * 0.8 - 1) = 1.0 exactly. A daily-reset constant mix would
    give (1 + 0.5 * 0.25) * (1 - 0.5 * 0.2) = 1.0125.
    """
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    returns = pd.DataFrame(0.0, index=dates, columns=TICKERS)
    returns["SPY"] = [0.0, 0.25, -0.2, 0.0]
    schedule = {dates[0]: {t: (0.5 if t == "SPY" else 0.0) for t in TICKERS}}

    value = _simulate_portfolio_value(returns, 100_000.0, schedule)

    assert float(value.iloc[1]) == pytest.approx(112_500.0)  # 50k cash + 50k * 1.25
    assert float(value.iloc[-1]) == pytest.approx(100_000.0)


def test_checked_in_reference_stays_within_lean_ground_truth():
    """Parity pin vs the QuantConnect run "Sleepy Tan Coyote" (LEAN ground truth).

    The regenerated reimplementation (yfinance adjusted prices, 2026-11-13) measured
    return 0.9296 vs 0.9378, CAGR 7.58% vs 7.62%, max DD 17.85% vs 17.70%, Sharpe 0.351 vs
    0.372, vol 8.76% vs 8.7%. The tolerances are the measured gap plus headroom and are tight
    enough that any of the fixed defects (1-day look-ahead: return 0.73, DD 25%; stale gate:
    Sharpe 0.35; skipped first 3 rebalances) fails it. The residual is explained by data and
    modelling differences: no fee model (QC $2,698), fractional shares, QC sizing MOC orders
    at the previous close, and adjusted-price vendor differences.
    """
    import json

    ref_path = (
        Path(__file__).parent.parent.parent
        / "strategy-comparisons"
        / "risk_parity"
        / "risk_parity_reference.json"
    )
    reference = json.loads(ref_path.read_text(encoding="utf-8"))
    truth = reference["raw_metadata"]["lean_ground_truth"]
    stats = reference["stats"]
    assert abs(stats["total_return"] - truth["total_return"]) <= 0.03
    assert abs(stats["cagr"] - truth["cagr"]) <= 0.003
    assert abs(abs(stats["max_drawdown"]) - abs(truth["max_drawdown"])) <= 0.005
    assert abs(stats["sharpe_ratio"] - truth["sharpe_ratio"]) <= 0.03
    assert abs(stats["annualized_volatility"] - 0.087) <= 0.003


def _flat_prices(start: str, end: str, drop: str | None = None) -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    if drop:
        dates = dates[dates != pd.Timestamp(drop)]
    rng = np.random.default_rng(7)
    steps = 1.0 + rng.normal(0.0003, 0.01, size=(len(dates), len(TICKERS)))
    return pd.DataFrame(100.0 * np.cumprod(steps, axis=0), index=dates, columns=TICKERS)


def test_compounded_growth_matches_the_simulated_curve():
    """A-M9: the rebalance mark-to-market and the equity curve share one compounding."""
    prices = _flat_prices("2024-01-01", "2024-03-29")
    returns = prices.pct_change().dropna()
    start, end = returns.index[3], returns.index[40]
    growth = compounded_growth(returns, start, end)
    weights = {t: 1.0 / len(TICKERS) for t in TICKERS}
    sim = _simulate_portfolio_value(returns, 1000.0, {start: weights})
    expected = sim.loc[end] / sim.loc[start]
    got = sum(weights[t] * growth[t] for t in TICKERS)
    assert got == pytest.approx(expected, rel=1e-12)


def test_compounded_growth_refuses_non_finite_returns():
    prices = _flat_prices("2024-01-01", "2024-02-29")
    returns = prices.pct_change().dropna()
    returns.iloc[5, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite compounded growth"):
        compounded_growth(returns, returns.index[2], returns.index[10])


def test_compounded_growth_refuses_unknown_bar():
    returns = _flat_prices("2024-01-01", "2024-02-29").pct_change().dropna()
    with pytest.raises(ValueError, match="not a return bar"):
        compounded_growth(returns, pd.Timestamp("2023-12-01"), returns.index[3])


def test_rebalance_without_completed_bar_is_skipped_visibly(caplog):
    prices = _flat_prices("2024-01-31", "2024-03-15")  # first return bar is 2024-02-01
    with caplog.at_level("WARNING"):
        result = run_backtest(
            start_date="2024-01-31",
            end_date="2024-03-15",
            prices=prices,
            warm_up_days=0,
        )
    assert any("no completed return bar" in r.message for r in caplog.records)
    assert result["portfolio_target_signals"][0]["timestamp"] != "2024-02-01T00:00:00Z"


def test_short_covariance_window_is_logged_and_recorded(caplog):
    prices = _flat_prices("2023-10-02", "2024-03-15")
    with caplog.at_level("WARNING"):
        result = run_backtest(
            start_date="2024-01-01", end_date="2024-03-15", prices=prices, warm_up_days=0
        )
    first = result["portfolio_target_signals"][0]["metadata"]
    assert first["covariance_observations"] < first["covariance_lookback_days"]
    assert any("covariance window" in r.message for r in caplog.records)


def _patch_equal_weights(monkeypatch):
    import scripts.run_risk_parity_backtest as risk_parity

    baseline = {ticker: 1.0 / len(TICKERS) for ticker in TICKERS}
    monkeypatch.setattr(risk_parity, "calculate_covariance", lambda *a, **k: np.eye(len(TICKERS)))
    monkeypatch.setattr(
        risk_parity, "calculate_risk_parity_weights", lambda *a, **k: dict(baseline)
    )
    monkeypatch.setattr(risk_parity, "adjust_for_regime", lambda weights, regime: dict(weights))
    monkeypatch.setattr(
        risk_parity,
        "apply_volatility_scaling",
        lambda weights, cov, target_vol, max_leverage, max_weight, tickers: dict(weights),
    )
    monkeypatch.setattr(risk_parity, "apply_tail_risk_hedge", lambda weights: dict(weights))
    monkeypatch.setattr(risk_parity, "weekly_correlation_regimes", lambda returns: {})


def test_attribution_baseline_is_the_last_executed_rebalance_not_a_skipped_date(monkeypatch):
    """cubic #188: a scheduled rebalance dropped from the return index (a missing close)
    must not become the next rebalance's attribution baseline."""
    _patch_equal_weights(monkeypatch)
    dates = pd.bdate_range("2023-12-27", "2024-03-15")
    dates = dates[dates != pd.Timestamp("2024-01-01")]
    prices = pd.DataFrame(100.0, index=dates, columns=TICKERS)
    # QQQ: 100 on Jan 2, jumps to 110 mid-January (so the Feb 1 close differs from
    # Jan 2), then to 121 in mid-February.
    prices.loc[pd.Timestamp("2024-01-15") :, "QQQ"] = 110.0
    prices.loc[pd.Timestamp("2024-02-15") :, "QQQ"] = 121.0
    # SPY has no close on the Feb 1 rebalance date -> that bar leaves the returns.
    prices.loc[pd.Timestamp("2024-02-01"), "SPY"] = np.nan

    result = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
    )

    assert "2024-02-01T00:00:00Z" not in [
        s["timestamp"] for s in result["portfolio_target_signals"]
    ]
    # Held 1/11 of 1,000,000 in QQQ since Jan 2 (100) -> Mar 1 (121).
    expected = (121.0 - 100.0) / 100.0 * (1.0 / len(TICKERS)) * 1_000_000.0
    assert result["stats"]["rebalance_attribution_net_profit"] == pytest.approx(
        expected, abs=0.02
    )


def test_rebalance_with_a_single_prior_return_is_skipped_not_run_on_nan_weights(monkeypatch):
    """cubic #188: one completed return gives a NaN sample covariance."""
    import scripts.run_risk_parity_backtest as risk_parity

    monkeypatch.setattr(risk_parity, "weekly_correlation_regimes", lambda returns: {})
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2023-12-28", "2024-03-15")
    dates = dates[dates != pd.Timestamp("2024-01-01")]
    prices = pd.DataFrame(
        100.0 * np.cumprod(1 + rng.normal(0, 0.01, size=(len(dates), len(TICKERS))), axis=0),
        index=dates,
        columns=TICKERS,
    )

    result = run_backtest(
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        prices=prices,
        open_prices=prices,
        warm_up_days=0,
    )

    signals = result["portfolio_target_signals"]
    assert signals, "later rebalances still run"
    assert signals[0]["timestamp"] != "2024-01-02T00:00:00Z"
    for signal in signals:
        assert all(np.isfinite(w) for w in signal["target_weights"].values())
    assert np.isfinite(result["portfolio_value"]).all()

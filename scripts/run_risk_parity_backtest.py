#!/usr/bin/env python3
"""Risk Parity Backtest (Python reimplementation of QC strategy).

This script runs a close Python reimplementation of the QuantConnect
`risk_parity_strategy.py` algorithm.  It uses yfinance to download daily
adjusted close and open prices and rebalances monthly.  The resulting trades and
statistics are written to `data/backtests/` as both a raw trades JSON and a
`ValidationReference` JSON for comparison with the PDF reference report.

Note: This is a *fallback* execution path.  A native LEAN backtest would
require QuantConnect credentials and local equity data, neither of which are
available in this environment.
"""
from __future__ import annotations

import argparse
import json

# Ensure project root is importable when called directly
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.optimize import minimize

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.run_risk_parity_backtest
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("run_risk_parity_backtest")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from shared.constants import ensure_path_within_workspace
from shared.logging import get_logger

logger = get_logger("scripts.run-risk-parity-backtest")

_ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from validation.reference import ValidationReference, resolve_initial_capital

ASSETS: dict[str, dict[str, str]] = {
    "SPY": {"class": "equity", "sub": "us_large"},
    "QQQ": {"class": "equity", "sub": "us_tech"},
    "EFA": {"class": "equity", "sub": "intl_dev"},
    "EEM": {"class": "equity", "sub": "intl_em"},
    "TLT": {"class": "fixed_income", "sub": "lt_treasury"},
    "IEF": {"class": "fixed_income", "sub": "it_treasury"},
    "LQD": {"class": "fixed_income", "sub": "ig_credit"},
    "TIP": {"class": "fixed_income", "sub": "tips"},
    "GLD": {"class": "commodities", "sub": "gold"},
    "DBC": {"class": "commodities", "sub": "broad"},
    "VNQ": {"class": "real_assets", "sub": "reits"},
}

TICKERS = list(ASSETS.keys())

# First trading session of the QuantConnect data history (log line 1:
# "Algorithm starting warm up..." 2015-08-04). The LEAN SMA(200) hedge
# indicator counts its 200 ready-bars from here.
QC_DATA_START = pd.Timestamp("2015-08-04")

# QC's ``History(symbol, 130, Daily)`` is not bounded by the algorithm's data start: at the
# first rebalance (2016-01-04) LEAN still returns 130 daily bars reaching back into 2015-06.
# The covariance and correlation lookbacks (120 returns) are therefore full from the first
# rebalance; 130 trading bars are about 190 calendar days, rounded up to 200. Only the SMA(200)
# readiness gate counts from QC_DATA_START (the indicator is fed by the algorithm's own warm-up).
HISTORY_LOOKBACK_MARGIN_DAYS = 200


def price_history_start(start: str, history_start: str = "2015-08-04") -> str:
    """First calendar day of price history needed to reproduce QC's rebalances."""
    first = min(pd.Timestamp(start), pd.Timestamp(history_start))
    return (first - pd.Timedelta(days=HISTORY_LOOKBACK_MARGIN_DAYS)).strftime("%Y-%m-%d")


def download_prices(
    start: str, end: str, history_start: str = "2015-08-04"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download adjusted daily close and open prices from yfinance.

    A-M6: the download starts ``HISTORY_LOOKBACK_MARGIN_DAYS`` (200 calendar
    days) BEFORE ``history_start`` — ``price_history_start`` returns 2015-01-16
    for the default (QC data start 2015-08-04, backtest start 2016-01-01) —
    because QC's ``History(symbol, 130, Daily)`` is not bounded by the
    algorithm's data start and still reaches back into 2015-06 at the first
    rebalance.  Only the SMA(200) ready-gate counts its bars from
    ``history_start`` itself; the covariance/correlation lookback (120 returns)
    must be FULL at the first rebalance, which is what the margin buys.
    Starting the download at ``history_start`` instead leaves the first
    covariance window short (~105 of 120 returns).
    """
    effective_start = price_history_start(start, history_start)
    data = yf.download(TICKERS, start=effective_start, end=end, auto_adjust=True, progress=False)
    # yfinance returns a MultiIndex when multiple tickers
    closes = data["Close"].copy() if "Close" in data.columns else data.copy()
    opens = data["Open"].copy() if "Open" in data.columns else closes.copy()
    closes = closes.dropna(how="all")
    opens = opens.reindex(closes.index)
    return closes, opens


def calculate_covariance(returns_matrix: np.ndarray, shrinkage: float = 0.2) -> np.ndarray:
    """Calculate Ledoit-Wolf-style shrunk covariance and annualize."""
    sample_cov = np.cov(returns_matrix)
    n = sample_cov.shape[0]
    avg_var = np.trace(sample_cov) / n
    target = np.eye(n) * avg_var
    shrunk = shrinkage * target + (1.0 - shrinkage) * sample_cov
    return shrunk * 252.0


def calculate_risk_parity_weights(
    cov: np.ndarray,
    min_weight: float,
    max_weight: float,
    tickers: list[str],
) -> dict[str, float]:
    """Equal risk contribution weights via SLSQP optimisation."""
    n = len(tickers)

    def portfolio_vol(w):
        return float(np.sqrt(w @ cov @ w))

    def objective(w):
        port_vol = portfolio_vol(w)
        if port_vol == 0:
            return 0.0
        marginal = cov @ w
        rc = w * marginal / port_vol
        target_rc = np.ones(n) / n
        return float(np.sum((rc - target_rc) ** 2))

    bounds = [(min_weight, max_weight) for _ in range(n)]
    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    x0 = np.ones(n) / n
    result = minimize(
        objective,
        x0,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"maxiter": 1000},
    )
    if result.success:
        weights = result.x
    else:
        vols = np.sqrt(np.diag(cov))
        inv_vols = 1.0 / vols
        weights = inv_vols / np.sum(inv_vols)

    return {ticker: float(weights[i]) for i, ticker in enumerate(tickers)}


def detect_correlation_regime(returns_matrix: np.ndarray) -> str:
    """Classify correlation regime based on recent vs historical avg correlation."""
    recent = returns_matrix[:, -30:]
    corr_recent = np.corrcoef(recent)
    corr_full = np.corrcoef(returns_matrix)
    n = corr_recent.shape[0]
    off_diag_sum = np.sum(np.abs(corr_recent)) - n
    avg_recent = off_diag_sum / (n * n - n) if n > 1 else 0.0
    off_diag_sum_full = np.sum(np.abs(corr_full)) - n
    avg_full = off_diag_sum_full / (n * n - n) if n > 1 else 0.0

    if avg_recent > avg_full * 1.5:
        return "crisis"
    if avg_recent > avg_full * 1.2:
        return "elevated"
    return "normal"


def adjust_for_regime(weights: dict[str, float], regime: str) -> dict[str, float]:
    """Adjust weights for correlation regime."""
    adjusted = dict(weights)
    if regime == "crisis":
        for ticker in adjusted:
            asset_class = ASSETS[ticker]["class"]
            if asset_class == "equity":
                adjusted[ticker] *= 0.7
            elif ticker in ("GLD", "TLT", "IEF"):
                adjusted[ticker] *= 1.5
    elif regime == "elevated":
        for ticker in adjusted:
            if ASSETS[ticker]["class"] == "equity":
                adjusted[ticker] *= 0.9

    total = sum(adjusted.values())
    if total > 0:
        adjusted = {t: w / total for t, w in adjusted.items()}
    return adjusted


def apply_volatility_scaling(
    weights: dict[str, float],
    cov: np.ndarray,
    target_vol: float,
    max_leverage: float,
    max_weight: float,
    tickers: list[str],
) -> dict[str, float]:
    """Scale portfolio to target volatility."""
    w_array = np.array([weights[t] for t in tickers])
    port_vol = float(np.sqrt(w_array @ cov @ w_array))
    if port_vol > 0:
        scale = np.clip(target_vol / port_vol, 0.5, max_leverage)
    else:
        scale = 1.0

    scaled = {t: w * scale for t, w in weights.items()}
    for ticker in scaled:
        scaled[ticker] = min(scaled[ticker], max_weight * max_leverage)
    return scaled



def first_available_trading_dates(index: pd.DatetimeIndex, scheduled_dates: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Map scheduled calendar dates to the next available trading date."""
    dates: list[pd.Timestamp] = []
    for scheduled in scheduled_dates:
        candidates = index[index >= scheduled]
        if len(candidates):
            dates.append(candidates[0])
    return dates


def weekly_correlation_regimes(returns: pd.DataFrame) -> dict[pd.Timestamp, str]:
    """Reproduce QC's weekly correlation-regime state updates."""
    if returns.empty:
        return {}
    week_starts = first_available_trading_dates(
        returns.index,
        pd.date_range(start=returns.index[0], end=returns.index[-1], freq="W-MON"),
    )
    regimes: dict[pd.Timestamp, str] = {}
    for regime_date in week_starts:
        # The QC callback fires at 09:45 on ``regime_date``: the newest daily bar it can see is
        # the PREVIOUS session's close, so the window ends before ``regime_date``'s own return.
        loc = returns.index.get_loc(regime_date)
        if loc < 31:
            # Fewer observations than the 30-day recent window plus one: the correlation
            # is undefined, and QC's History() always supplies a full window.
            # Logged so the missing state update is visible rather than silent.
            logger.debug(
                "Weekly correlation regime for %s skipped: %d return bars precede it "
                "(need 31); the previous regime stays active",
                regime_date.date(),
                loc,
            )
            continue
        hist_start = max(0, loc - 120)
        ret_matrix = returns.iloc[hist_start:loc].T.values
        regimes[regime_date] = detect_correlation_regime(ret_matrix)
    return regimes


def latest_correlation_regime(
    regimes: dict[pd.Timestamp, str],
    rebalance_date: pd.Timestamp,
    default: str = "normal",
) -> str:
    """Return the latest weekly regime known at a rebalance date."""
    known_dates = [date for date in regimes if date <= rebalance_date]
    if not known_dates:
        return default
    return regimes[max(known_dates)]

def apply_tail_risk_hedge(weights: dict[str, float]) -> dict[str, float]:
    """Reduce risk exposure when tail risk hedge is active."""
    hedged = dict(weights)
    for ticker in hedged:
        asset_class = ASSETS[ticker]["class"]
        if asset_class == "equity":
            hedged[ticker] *= 0.3
        elif asset_class in ("commodities", "real_assets"):
            hedged[ticker] *= 0.5
        elif ticker in ("TLT", "IEF"):
            hedged[ticker] *= 1.3
        elif ticker == "GLD":
            hedged[ticker] *= 1.5
    return hedged


def compounded_growth(
    returns: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp
) -> dict[str, float]:
    """Compound the return rows between two bars (``start_date`` exclusive, ``end_date`` inclusive).

    A-M9: there must be exactly ONE mark-to-market implementation.  The
    rebalance-time value (``advance_portfolio_value``) feeds the 2% drifted-weight
    trade gate, the order sizing and the attribution PnL, while
    ``_simulate_portfolio_value`` compounds the same returns into the published
    equity curve.  Deriving the drift from raw price ratios instead would walk a
    different set of bars: ``prices.pct_change().dropna()`` deletes a bar whenever
    ANY ticker has a missing close (``download_prices`` only drops all-NaN rows and
    ``_load_local_prices`` unions unaligned per-ticker series), so a raw ratio
    silently includes moves the curve drops and the two paths disagree.

    Refuses loudly (``ValueError``) on a non-finite factor: a missing close inside
    the interval would otherwise poison every downstream PnL and size with NaN and
    still be serialised into the artifact.
    """
    if start_date not in returns.index:
        raise ValueError(
            f"growth window start {start_date.date()} is not a return bar of the price history"
        )
    if end_date not in returns.index:
        raise ValueError(
            f"growth window end {end_date.date()} is not a return bar of the price history"
        )
    window = returns.loc[(returns.index > start_date) & (returns.index <= end_date)]
    growth: dict[str, float] = {}
    for ticker in returns.columns:
        values = window[ticker].to_numpy(dtype=float)
        factor = float(np.prod(1.0 + values))
        if not np.isfinite(factor) or factor <= 0.0:
            raise ValueError(
                f"non-finite compounded growth for {ticker} between "
                f"{start_date.date()} and {end_date.date()}: the price history has a "
                "missing/invalid close inside the rebalance interval"
            )
        growth[ticker] = factor
    return growth


def _simulate_portfolio_value(
    returns: pd.DataFrame,
    initial_cash: float,
    rebalance_schedule: dict[pd.Timestamp, dict[str, float]],
) -> pd.Series:
    """Walk the returns with rebalanced weights to build the portfolio value.

    A-H3: rebalance orders are Market-On-Close — the fill happens AT the
    rebalance-day close, so the new weights earn from the NEXT trading day.
    The schedule update therefore applies AFTER the day's return is booked
    with the previously active weights (same-day application would credit
    the new portfolio with a return it was never exposed to).
    """
    # Buy-and-hold between rebalances: QC holds whole-share positions, so each leg's weight
    # drifts with its own price instead of being reset to the target every day.
    portfolio_value = pd.Series(index=returns.index, dtype=float)
    portfolio_value.iloc[0] = initial_cash
    holdings = dict.fromkeys(TICKERS, 0.0)  # position value per ticker
    cash = float(initial_cash)
    for pos, date in enumerate(returns.index):
        if pos:
            for t in TICKERS:
                holdings[t] *= 1 + float(returns[t].loc[date])
            portfolio_value.loc[date] = cash + sum(holdings.values())
        if date in rebalance_schedule:
            total = float(portfolio_value.iloc[pos])
            holdings = {t: rebalance_schedule[date].get(t, 0.0) * total for t in TICKERS}
            cash = total - sum(holdings.values())
    return portfolio_value


def _portfolio_performance_stats(
    *,
    portfolio_value: pd.Series,
    trades: list[dict[str, Any]],
    attribution_pnls: list[float],
    risk_free_rate: float = 0.0,
    lean_statistics: bool = False,
) -> dict[str, float]:
    """Derive the raw return/risk/trade statistics from the simulated value."""
    total_return = float(portfolio_value.iloc[-1] / portfolio_value.iloc[0] - 1)
    equity_net_profit = float(portfolio_value.iloc[-1] - portfolio_value.iloc[0])
    years = (portfolio_value.index[-1] - portfolio_value.index[0]).days / 365.25
    cagr = float((portfolio_value.iloc[-1] / portfolio_value.iloc[0]) ** (1 / years) - 1)

    if lean_statistics:
        # LEAN-exact recipe (Common/Statistics/Statistics.cs + PortfolioStatistics.cs):
        # performance = day-over-day equity returns on CALENDAR days (weekends
        # flat at 0), annual return = (1+daily_mean)^252-1, sample variance
        # (ddof=1). Verified against the QC order-ledger curve: vol matches
        # LEAN 0.0872 to 4 decimals; Sharpe/Sortino match with the average
        # risk-free rate (GetAverageRiskFreeRate ~2.5%/yr for 2016-2024).
        curve = portfolio_value.asfreq("D", method="ffill")
    else:
        curve = portfolio_value
    port_returns = curve.pct_change().dropna()
    if lean_statistics:
        ann_perf = float((1 + port_returns.mean()) ** 252 - 1)
    else:
        ann_perf = float(port_returns.mean() * 252)
    vol_ann = float(port_returns.std() * np.sqrt(252))
    # LEAN subtracts the average risk-free rate (US Treasury curve via the
    # risk-free interest rate model); the reimplementation defaults to 0.0
    # (pure excess over cash). Pass risk_free_rate to reproduce LEAN
    # Sharpe/Sortino one-for-one (audit calibration: ~0.025 for 2016-2024).
    excess_ann = ann_perf - risk_free_rate
    sharpe = float(excess_ann / (port_returns.std() * np.sqrt(252))) if vol_ann else 0.0
    if lean_statistics:
        # LEAN-faithful downside deviation (verified 0.3901 vs QC 0.39 at
        # rf~=0.025): sample std of the negative returns only. Kept
        # byte-identical — do not "fix" toward the textbook definition here,
        # it would diverge from LEAN.
        downside = port_returns[port_returns < 0]
        sortino = float(excess_ann / (downside.std() * np.sqrt(252))) if len(downside) and downside.std() > 0 else 0.0
    else:
        # A-L3: textbook downside deviation for the non-lean path — mean of
        # squared downside vs the daily risk-free target over ALL
        # observations (not std over negatives only), so the metric no
        # longer depends on the negative-count convention.
        daily_rf = risk_free_rate / 252.0
        downside_sq_mean = float(
            np.mean([min(0.0, float(r) - daily_rf) ** 2 for r in port_returns])
        ) if len(port_returns) else 0.0
        downside_dev = float(np.sqrt(downside_sq_mean * 252.0)) if downside_sq_mean > 0 else 0.0
        sortino = float(excess_ann / downside_dev) if downside_dev > 0 else 0.0

    rolling_max = portfolio_value.cummax()
    drawdown = (portfolio_value - rolling_max) / rolling_max
    max_drawdown = float(drawdown.min())

    winning_trades = [t for t in trades if t["pnl"] > 0]
    losing_trades = [t for t in trades if t["pnl"] < 0]
    win_rate = len(winning_trades) / len(trades) if trades else 0.0
    gross_profit = sum(t["pnl"] for t in winning_trades)
    gross_loss = sum(t["pnl"] for t in losing_trades)
    profit_factor = abs(gross_profit / gross_loss) if gross_loss else 0.0
    realized_trade_net_profit = gross_profit + gross_loss
    attribution_winners = [pnl for pnl in attribution_pnls if pnl > 0]
    attribution_losers = [pnl for pnl in attribution_pnls if pnl < 0]
    attribution_gross_profit = sum(attribution_winners)
    attribution_gross_loss = sum(attribution_losers)
    attribution_net_profit = attribution_gross_profit + attribution_gross_loss
    attribution_win_rate = (
        len(attribution_winners) / len(attribution_pnls)
        if attribution_pnls
        else 0.0
    )
    return {
        "total_return": total_return,
        "cagr": cagr,
        "annualized_volatility": vol_ann,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown": max_drawdown,
        "net_profit": equity_net_profit,
        "realized_trade_net_profit": realized_trade_net_profit,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "rebalance_attribution_net_profit": attribution_net_profit,
        "rebalance_attribution_gross_profit": attribution_gross_profit,
        "rebalance_attribution_gross_loss": attribution_gross_loss,
        "rebalance_attribution_win_rate": attribution_win_rate,
        "total_trades": len(trades),
    }


def run_backtest(
    start_date: str = "2016-01-01",
    end_date: str = "2024-12-31",
    initial_cash: float = 1_000_000.0,
    prices: pd.DataFrame | None = None,
    open_prices: pd.DataFrame | None = None,
    warm_up_days: int = 150,
    rebalance_threshold: float = 0.02,
    risk_free_rate: float = 0.0,
    lean_statistics: bool = False,
    history_start: str = "2015-08-04",
) -> dict[str, Any]:
    """Run the risk parity backtest and return result dict."""
    if prices is None:
        # A-M6: the download starts ``history_start`` minus
        # HISTORY_LOOKBACK_MARGIN_DAYS (price_history_start), so the SMA(200)
        # IsReady gate counts from the LEAN origin AND the 120-return covariance
        # window is already full at the first rebalance.
        prices, open_prices = download_prices(start_date, end_date, history_start=history_start)
    elif open_prices is None:
        open_prices = prices
    if prices.index[0] > QC_DATA_START:
        # Explicit-price path without QC history: the SMA(200) ready-gate
        # counts local bars only and goes ready late vs LEAN (documented
        # limitation — pass history from 2015-08-04 or leave prices=None to
        # download it).
        logger.warning(
            "Price history starts %s, after QC data start %s: the SMA(200) "
            "hedge gate counts local bars only and fires late vs LEAN",
            prices.index[0].date(),
            QC_DATA_START.date(),
        )
    if not 0 <= warm_up_days < len(prices):
        raise ValueError("warm_up_days must be non-negative and less than the number of price rows")
    returns = prices.pct_change().dropna()
    returns = returns[TICKERS]  # ensure column order

    lookback_corr = 120
    target_vol = 0.10
    max_leverage = 1.5
    min_weight = 0.02
    max_weight = 0.30

    # Rebalance on the first trading day of each month after warm-up
    # QC trades from SetStartDate; its SetWarmUp(150 days) is a pre-start period
    # (QC log: warm-up 2015-08-04 -> 2016-01-01, first order 2016-01-04). When the
    # supplied history reaches back before ``start_date`` that history IS the warm-up,
    # so rebalancing starts at ``start_date``. Counting ``warm_up_days`` bars from the
    # first row instead would skip the 2016-01/02/03 rebalances on the default history.
    start_ts = pd.Timestamp(start_date)
    if prices.index[0] < start_ts:
        first_rebalance_after = start_ts - pd.Timedelta(days=1)
    else:
        first_rebalance_after = prices.index[warm_up_days]
    rebalance_dates = pd.date_range(start=prices.index[0], end=prices.index[-1], freq="MS")
    rebalance_dates = rebalance_dates[rebalance_dates > first_rebalance_after]
    rebalance_dates = first_available_trading_dates(prices.index, rebalance_dates)
    regime_schedule = weekly_correlation_regimes(returns)

    current_weights: dict[str, float] = dict.fromkeys(TICKERS, 0.0)
    position_entry_prices: dict[str, float | None] = dict.fromkeys(TICKERS)
    position_entry_times: dict[str, pd.Timestamp | None] = dict.fromkeys(TICKERS)
    rebalance_schedule: dict[pd.Timestamp, dict[str, float]] = {}
    portfolio_target_signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    rebalance_attribution_pnls: list[float] = []
    portfolio_value_at_rebalance = float(initial_cash)
    last_rebalance_value_date = returns.index[0]

    def advance_portfolio_value(rebalance_date: pd.Timestamp) -> dict[str, float]:
        """Mark the previous rebalance's holdings to market through the rebalance date.

        Positions are held (buy-and-hold), so a leg's weight drifts with its own price.
        Returns the drifted weights; updates ``portfolio_value_at_rebalance``.
        """
        nonlocal portfolio_value_at_rebalance, last_rebalance_value_date
        growth = compounded_growth(returns, last_rebalance_value_date, rebalance_date)
        grown = {t: current_weights[t] * growth[t] for t in TICKERS}
        cash_weight = 1.0 - sum(current_weights.values())
        total = cash_weight + sum(grown.values())
        portfolio_value_at_rebalance *= total
        last_rebalance_value_date = rebalance_date
        return {t: grown[t] / total for t in TICKERS}

    def execution_price(ticker: str, date: pd.Timestamp) -> float:
        """Fill at the rebalance-day close (QuantConnect MarketOnClose).

        LEAN converts intraday market orders on daily resolution into
        MarketOnClose orders (see QC log 2016-01-04 warning); orders.csv
        confirms Type='Market On Close' with close prices (median 0.002%
        vs adjusted close). Never use the open.
        """
        return float(prices.at[date, ticker])

    for i, reb_date in enumerate(rebalance_dates):
        if reb_date not in returns.index or returns.index.get_loc(reb_date) < 1:
            # No completed bar before the rebalance: nothing to decide on. Skip BEFORE the
            # mark-to-market advances so no state is mutated, and make the skip visible.
            logger.warning(
                "Rebalance %s skipped: no completed return bar precedes it; the portfolio "
                "stays in cash until the next rebalance",
                reb_date.date(),
            )
            continue
        interval_start_portfolio_value = portfolio_value_at_rebalance
        drifted_weights = advance_portfolio_value(reb_date)
        current_portfolio_value = portfolio_value_at_rebalance
        # QC's Rebalance runs at 10:00 (AfterMarketOpen 30) on daily data: History() and the
        # SMA only contain bars up to the PREVIOUS close, while the MarketOnClose fill happens
        # at ``reb_date``'s close. The decision window therefore ends before ``reb_date``'s own
        # return; using it would be a one-day look-ahead (it flips the hedge in 2020-03/2022-03).
        loc = returns.index.get_loc(reb_date)
        hist_start = max(0, loc - lookback_corr)
        ret_matrix = returns.iloc[hist_start:loc].T.values
        if ret_matrix.shape[1] < lookback_corr:
            logger.warning(
                "Rebalance %s: covariance window has %d of %d return observations "
                "(short price history); weights are estimated on a thin window",
                reb_date.date(),
                ret_matrix.shape[1],
                lookback_corr,
            )

        cov = calculate_covariance(ret_matrix)
        weights = calculate_risk_parity_weights(cov, min_weight, max_weight, TICKERS)

        regime = latest_correlation_regime(regime_schedule, reb_date)
        weights = adjust_for_regime(weights, regime)
        weights = apply_volatility_scaling(
            weights, cov, target_vol, max_leverage, max_weight, TICKERS
        )

        # Tail risk hedge: SPY below 200-day SMA * 0.98.
        # LEAN gate: the SMA(200) indicator is only IsReady after 200
        # bars. With SetWarmUp(150 days) the indicator cannot fire before
        # ~2016-03 even if the price condition holds (audit: QC did NOT
        # hedge 2016-02-01 although SPY < SMA200*0.98 on raw history).
        # bars_since_data_start counts trading bars since the QC data
        # start (2015-08-04); local histories starting earlier must still
        # count from that date to reproduce IsReady faithfully.
        spy_idx = prices.index.get_loc(reb_date) - 1  # last completed bar (see above)
        bars_since_data_start = (
            int((prices.index >= QC_DATA_START).sum())
            - int((prices.index > prices.index[spy_idx]).sum())
        )
        spy_sma_ready = bars_since_data_start >= 200
        spy_window = prices["SPY"].iloc[max(0, spy_idx - 199) : spy_idx + 1]
        spy_sma = spy_window.mean()
        spy_price = prices["SPY"].iloc[spy_idx]
        hedge_active = bool(
            spy_sma_ready and len(spy_window) >= 200 and spy_price < spy_sma * 0.98
        )
        if hedge_active:
            weights = apply_tail_risk_hedge(weights)

        portfolio_target_signal_id = f"risk_parity:{reb_date.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        portfolio_target_signals.append(
            {
                "signal_id": portfolio_target_signal_id,
                "strategy_id": "risk_parity",
                "timestamp": reb_date.strftime(_ISO_UTC_FORMAT),
                "target_weights": {
                    ticker: round(float(weights[ticker]), 8) for ticker in TICKERS
                },
                "rebalance_threshold": rebalance_threshold,
                "cash_target": round(1.0 - sum(float(weights[ticker]) for ticker in TICKERS), 8),
                "metadata": {
                    "regime": regime,
                    "hedge_active": hedge_active,
                    "universe": list(TICKERS),
                    "covariance_lookback_days": lookback_corr,
                    "covariance_observations": int(ret_matrix.shape[1]),
                    "target_volatility": target_vol,
                    "max_leverage": max_leverage,
                    "min_weight": min_weight,
                    "max_weight": max_weight,
                },
            }
        )

        next_weights: dict[str, float] = {}
        for ticker in TICKERS:
            # Weight at the last rebalance (attribution) vs weight now (QC's trade gate:
            # ``Portfolio[symbol].HoldingsValue / TotalPortfolioValue``).
            held_weight = current_weights.get(ticker, 0.0)
            prev_weight = drifted_weights[ticker]
            if i > 0 and held_weight:
                attribution_price = float(prices[ticker].loc[reb_date])
                attribution_prev_price = float(prices[ticker].loc[rebalance_dates[i - 1]])
                attribution_pnl = (
                    (attribution_price - attribution_prev_price)
                    / attribution_prev_price
                    * held_weight
                    * interval_start_portfolio_value
                )
                rebalance_attribution_pnls.append(round(attribution_pnl, 2))

            delta = weights[ticker] - prev_weight
            if abs(delta) <= rebalance_threshold:
                next_weights[ticker] = prev_weight  # position kept, weight carried as drifted
                continue

            price = execution_price(ticker, reb_date)
            entry_price = position_entry_prices.get(ticker)
            entry_time = position_entry_times.get(ticker)
            if entry_price is None or entry_time is None:
                entry_price = price
                entry_time = reb_date
            direction = "LONG" if prev_weight >= 0 else "SHORT"
            if not prev_weight:
                direction = "LONG" if delta >= 0 else "SHORT"
            realizes_pnl = bool(bool(prev_weight) and abs(weights[ticker]) < abs(prev_weight))
            pnl = (
                (price - entry_price)
                / entry_price
                * prev_weight
                * current_portfolio_value
                if realizes_pnl and entry_price
                else 0.0
            )
            trades.append(
                {
                    "entry_time": entry_time.strftime(_ISO_UTC_FORMAT),
                    "symbol": ticker,
                    "exit_time": reb_date.strftime(_ISO_UTC_FORMAT),
                    "direction": direction,
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(price, 6),
                    "quantity": round(abs(delta), 6),
                    "position_size": round(abs(weights[ticker]) * current_portfolio_value, 2),
                    "pnl": round(pnl, 2),
                    "meta": {
                        "portfolio_target_signal_id": portfolio_target_signal_id,
                        "regime": regime,
                        "hedge_active": hedge_active,
                        "previous_weight": round(prev_weight, 8),
                        "target_weight": round(weights[ticker], 8),
                        "delta_weight": round(delta, 8),
                        "realizes_pnl": realizes_pnl,
                    },
                }
            )
            next_weights[ticker] = weights[ticker]
            position_entry_prices[ticker] = price
            position_entry_times[ticker] = reb_date

        current_weights = next_weights
        rebalance_schedule[reb_date] = dict(current_weights)

    # Build portfolio return series from held weights
    portfolio_value = _simulate_portfolio_value(
        returns, initial_cash, rebalance_schedule
    )
    # The equity curve starts at the last bar before ``start_date`` (QC's account starts
    # with the cash on the start date); the flat pre-start warm-up must not dilute
    # CAGR, volatility or Sharpe.
    pre_start = portfolio_value.index[portfolio_value.index < start_ts]
    if len(pre_start):
        portfolio_value = portfolio_value.loc[pre_start[-1]:]
    stats = _portfolio_performance_stats(
        portfolio_value=portfolio_value,
        trades=trades,
        attribution_pnls=rebalance_attribution_pnls,
        risk_free_rate=risk_free_rate,
        lean_statistics=lean_statistics,
    )

    stats = {
        "total_return": round(stats["total_return"], 6),
        "cagr": round(stats["cagr"], 6),
        "annualized_volatility": round(stats["annualized_volatility"], 6),
        "sharpe_ratio": round(stats["sharpe_ratio"], 6),
        "sortino_ratio": round(stats["sortino_ratio"], 6),
        "max_drawdown": round(stats["max_drawdown"], 6),
        "max_drawdown_pct": round(stats["max_drawdown"] * 100, 6),
        "net_profit": round(stats["net_profit"], 2),
        "realized_trade_net_profit": round(stats["realized_trade_net_profit"], 2),
        "gross_profit": round(stats["gross_profit"], 2),
        "gross_loss": round(stats["gross_loss"], 2),
        "profit_factor": round(stats["profit_factor"], 6),
        "win_rate": round(stats["win_rate"], 6),
        "rebalance_attribution_net_profit": round(stats["rebalance_attribution_net_profit"], 2),
        "rebalance_attribution_gross_profit": round(stats["rebalance_attribution_gross_profit"], 2),
        "rebalance_attribution_gross_loss": round(stats["rebalance_attribution_gross_loss"], 2),
        "rebalance_attribution_win_rate": round(stats["rebalance_attribution_win_rate"], 6),
        "total_trades": stats["total_trades"],
    }

    return {
        "portfolio_value": portfolio_value,
        "portfolio_target_signals": portfolio_target_signals,
        "trades": trades,
        "stats": stats,
        "initial_capital": float(initial_cash),
    }


def build_validation_reference(result: dict[str, Any]) -> ValidationReference:
    """Convert the backtest result into a ValidationReference."""
    from shared.models import Trade, TradeExit

    trades: list[Trade] = []
    for i, raw in enumerate(result["trades"], start=1):
        exit_price = raw.get("exit_price")
        if exit_price is None:
            # ValidationReference needs a closed trade; use same-day close as proxy
            exit_price = raw["entry_price"]
        exits = [
            TradeExit(
                time=raw["exit_time"],
                price=exit_price,
                quantity=raw["quantity"],
                pnl=raw.get("pnl"),
            )
        ]
        trades.append(
            Trade(
                id=i,
                symbol="PORTFOLIO",
                direction=raw["direction"],
                entry_time=raw["entry_time"],
                entry_price=raw["entry_price"],
                exits=exits,
                quantity=raw["quantity"],
                position_size=raw["position_size"],
                pnl=raw["pnl"],
                meta=raw.get("meta", {}),
            )
        )

    reference = ValidationReference(
        strategy_name="RiskParityStrategy",
        source_framework="unknown",
        symbol="PORTFOLIO",
        timeframe="1d",
        settings={
            "target_volatility": 0.10,
            "max_leverage": 1.5,
            "lookback_volatility": 60,
            "lookback_correlation": 120,
            "min_weight": 0.02,
            "max_weight": 0.30,
            "initial_capital": result.get("initial_capital"),
        },
        trades=trades,
        stats=result["stats"],
        raw_metadata={
            "note": "Python reimplementation using yfinance daily data. Not a native LEAN run due to missing credentials/local equity data.",
            "assets": TICKERS,
        },
    )
    reference.settings["initial_capital"] = resolve_initial_capital(reference)
    return reference


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Risk Parity backtest")
    parser.add_argument(
        "--output-dir",
        default="data/backtests",
        help="Directory for output files",
    )
    parser.add_argument(
        "--start",
        default="2016-01-01",
        help="Backtest start date",
    )
    parser.add_argument(
        "--end",
        default="2024-12-31",
        help="Backtest end date",
    )
    args = parser.parse_args()

    output_dir = ensure_path_within_workspace(Path(args.output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Running Risk Parity backtest (Python reimplementation)...")
    result = run_backtest(start_date=args.start, end_date=args.end)
    stats = result["stats"]
    print(json.dumps(stats, indent=2))

    # Raw trades file (satisfies harness verification pattern)
    trades_path = output_dir / "risk_parity_yfinance.trades.json"
    with trades_path.open("w", encoding="utf-8") as f:
        json.dump(result["trades"], f, indent=2, default=str)
    print(f"Wrote {trades_path}")

    # ValidationReference file
    vref = build_validation_reference(result)
    vref_path = output_dir / "risk_parity_yfinance_vref.json"
    with vref_path.open("w", encoding="utf-8") as f:
        f.write(vref.model_dump_json(indent=2))
    print(f"Wrote {vref_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

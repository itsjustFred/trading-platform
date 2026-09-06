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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from shared.constants import ensure_path_within_workspace
import yfinance as yf
from scipy.optimize import minimize

# Ensure project root is importable when called directly
import sys

_ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from validation.reference import ValidationReference

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


def download_prices(start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download adjusted daily close and open prices from yfinance."""
    data = yf.download(TICKERS, start=start, end=end, auto_adjust=True, progress=False)
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
        loc = returns.index.get_loc(regime_date)
        hist_start = max(0, loc - 120 + 1)
        ret_matrix = returns.iloc[hist_start : loc + 1].T.values
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


def _simulate_portfolio_value(
    returns: pd.DataFrame,
    initial_cash: float,
    rebalance_schedule: dict[pd.Timestamp, dict[str, float]],
) -> pd.Series:
    """Walk the returns with rebalanced weights to build the portfolio value."""
    portfolio_value = pd.Series(index=returns.index, dtype=float)
    portfolio_value.iloc[0] = initial_cash
    active_weights = {t: 0.0 for t in TICKERS}
    for date in returns.index:
        if date in rebalance_schedule:
            active_weights = rebalance_schedule[date]
        daily_ret = sum(active_weights[t] * returns[t].loc[date] for t in TICKERS)
        prev_idx = returns.index.get_loc(date) - 1
        if prev_idx >= 0:
            portfolio_value.loc[date] = portfolio_value.iloc[prev_idx] * (1 + daily_ret)
    return portfolio_value


def _portfolio_performance_stats(
    *,
    portfolio_value: pd.Series,
    trades: list[dict[str, Any]],
    attribution_pnls: list[float],
) -> dict[str, float]:
    """Derive the raw return/risk/trade statistics from the simulated value."""
    total_return = float(portfolio_value.iloc[-1] / portfolio_value.iloc[0] - 1)
    equity_net_profit = float(portfolio_value.iloc[-1] - portfolio_value.iloc[0])
    years = (portfolio_value.index[-1] - portfolio_value.index[0]).days / 365.25
    cagr = float((portfolio_value.iloc[-1] / portfolio_value.iloc[0]) ** (1 / years) - 1)

    port_returns = portfolio_value.pct_change().dropna()
    vol_ann = float(port_returns.std() * np.sqrt(252))
    sharpe = float((port_returns.mean() * 252) / (port_returns.std() * np.sqrt(252))) if vol_ann else 0.0
    downside = port_returns[port_returns < 0]
    sortino = float((port_returns.mean() * 252) / (downside.std() * np.sqrt(252))) if len(downside) and downside.std() > 0 else 0.0

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
) -> dict[str, Any]:
    """Run the risk parity backtest and return result dict."""
    if prices is None:
        prices, open_prices = download_prices(start_date, end_date)
    elif open_prices is None:
        open_prices = prices
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
    rebalance_dates = pd.date_range(start=prices.index[0], end=prices.index[-1], freq="MS")
    rebalance_dates = rebalance_dates[rebalance_dates > prices.index[warm_up_days]]
    rebalance_dates = first_available_trading_dates(prices.index, rebalance_dates)
    regime_schedule = weekly_correlation_regimes(returns)

    current_weights: dict[str, float] = {t: 0.0 for t in TICKERS}
    position_entry_prices: dict[str, float | None] = {t: None for t in TICKERS}
    position_entry_times: dict[str, pd.Timestamp | None] = {t: None for t in TICKERS}
    rebalance_schedule: dict[pd.Timestamp, dict[str, float]] = {}
    portfolio_target_signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    rebalance_attribution_pnls: list[float] = []
    portfolio_value_at_rebalance = float(initial_cash)
    last_rebalance_value_date = returns.index[0]

    def advance_portfolio_value(rebalance_date: pd.Timestamp) -> float:
        """Mark previous holdings to market through the rebalance date."""
        nonlocal portfolio_value_at_rebalance, last_rebalance_value_date
        start_loc = returns.index.get_loc(last_rebalance_value_date) + 1
        end_loc = returns.index.get_loc(rebalance_date)
        for date in returns.index[start_loc : end_loc + 1]:
            daily_ret = sum(current_weights[t] * returns[t].loc[date] for t in TICKERS)
            portfolio_value_at_rebalance *= 1 + daily_ret
        last_rebalance_value_date = rebalance_date
        return portfolio_value_at_rebalance

    def execution_price(ticker: str, date: pd.Timestamp) -> float:
        """Prefer the rebalance-day open when it is available locally."""
        if open_prices is not None and ticker in open_prices.columns and date in open_prices.index:
            price = open_prices.at[date, ticker]
            if pd.notna(price):
                return float(price)
        return float(prices.at[date, ticker])

    for i, reb_date in enumerate(rebalance_dates):
        interval_start_portfolio_value = portfolio_value_at_rebalance
        current_portfolio_value = advance_portfolio_value(reb_date)
        loc = returns.index.get_loc(reb_date)
        hist_start = max(0, loc - lookback_corr + 1)
        ret_matrix = returns.iloc[hist_start : loc + 1].T.values

        cov = calculate_covariance(ret_matrix)
        weights = calculate_risk_parity_weights(cov, min_weight, max_weight, TICKERS)

        regime = latest_correlation_regime(regime_schedule, reb_date)
        weights = adjust_for_regime(weights, regime)
        weights = apply_volatility_scaling(
            weights, cov, target_vol, max_leverage, max_weight, TICKERS
        )

        # Tail risk hedge: SPY below 200-day SMA * 0.98
        spy_idx = prices.index.get_loc(reb_date)
        spy_window = prices["SPY"].iloc[max(0, spy_idx - 199) : spy_idx + 1]
        spy_sma = spy_window.mean()
        spy_price = prices["SPY"].iloc[spy_idx]
        hedge_active = bool(spy_price < spy_sma * 0.98)
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
                    "target_volatility": target_vol,
                    "max_leverage": max_leverage,
                    "min_weight": min_weight,
                    "max_weight": max_weight,
                },
            }
        )

        for ticker in TICKERS:
            prev_weight = current_weights.get(ticker, 0.0)
            if i > 0 and prev_weight:
                attribution_price = float(prices[ticker].loc[reb_date])
                attribution_prev_price = float(prices[ticker].loc[rebalance_dates[i - 1]])
                attribution_pnl = (
                    (attribution_price - attribution_prev_price)
                    / attribution_prev_price
                    * prev_weight
                    * interval_start_portfolio_value
                )
                rebalance_attribution_pnls.append(round(attribution_pnl, 2))

            delta = weights[ticker] - prev_weight
            if abs(delta) <= rebalance_threshold:
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
            current_weights[ticker] = weights[ticker]
            position_entry_prices[ticker] = price
            position_entry_times[ticker] = reb_date

        rebalance_schedule[reb_date] = dict(current_weights)

    # Build portfolio return series from held weights
    portfolio_value = _simulate_portfolio_value(
        returns, initial_cash, rebalance_schedule
    )
    stats = _portfolio_performance_stats(
        portfolio_value=portfolio_value,
        trades=trades,
        attribution_pnls=rebalance_attribution_pnls,
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

    return ValidationReference(
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
        },
        trades=trades,
        stats=result["stats"],
        raw_metadata={
            "note": "Python reimplementation using yfinance daily data. Not a native LEAN run due to missing credentials/local equity data.",
            "assets": TICKERS,
        },
    )


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

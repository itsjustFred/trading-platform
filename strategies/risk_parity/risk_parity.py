"""
Strategy: Volatility-Regime Long (single instrument)

Single-instrument long/flat strategy. Entry only in a calm volatility
regime (annualized rolling vol below its own rolling median times a
threshold) and above a long SMA trend filter; exits on ATR stop, regime
break or trend break.

Naming integrity (Cleanup #35 / Settlement S15): this file does NOT
implement multi-asset risk parity — there is no weighting across
instruments here. The real multi-asset risk-parity allocation lives in
``scripts/run_risk_parity_backtest.py`` (+ ``runner/risk_parity_optimizer.py``).
Meta/tags below describe the implemented single-instrument behavior.
"""

STRATEGY_META = {
    "name": "Volatility-Regime Long (single instrument)",
    "version": "1.2.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/risk_parity_strategy.py",
    "notes": (
        "- Implementiert NICHT die Multi-Asset-Risk-Parity-Allokation des "
        "Originals; diese Strategie handelt ein einzelnes Instrument "
        "long/flat mit Volatilitaets-Regime- und SMA-Trendfilter.\n"
        "- 1.2.0: Regime- und Trend-Ausstiege sind Market-Exits ohne "
        "exit_price=close (Fill am naechsten Open); der ATR-Stop bleibt ein "
        "explizites Level.\n"
        "- Die echte Multi-Asset-Risk-Parity-Gewichtung liegt in "
        "scripts/run_risk_parity_backtest.py (QuantConnect-Reimplementierung) "
        "und runner/risk_parity_optimizer.py."
    ),
    "tags": {
        "type": ["regime-filter", "trend-following", "single-instrument"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/RiskParityAlpha",
        "asset_class": ["equities", "etf"],
        "complexity": "medium",
        "naming_note": (
            "slug retained as risk_parity for backtest-artifact compatibility; "
            "implementation is single-instrument volatility-regime long"
        ),
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    vol_period = params.get("vol_period", 60)
    vol_mult = params.get("vol_threshold", 1.5)
    trend_sma = params.get("trend_sma", 100)
    rebalance = params.get("rebalance_period", 21)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    returns = close.pct_change()
    vol = returns.rolling(vol_period).std() * np.sqrt(252)
    vol_median = vol.rolling(252).median()
    sma = close.rolling(trend_sma).mean()
    calm_regime = vol < (vol_median * vol_mult)
    uptrend = close > sma
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    rebalance_eligible = pd.Series(False, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan
    warmup = max(vol_period, trend_sma, 252)

    for i in range(warmup, len(df)):
        should_rebalance = i % rebalance == 0
        rebalance_eligible.iloc[i] = should_rebalance
        if not should_rebalance and not (in_trade and not calm_regime.iloc[i]):
            continue
        if not in_trade:
            if bool(calm_regime.iloc[i]) and bool(uptrend.iloc[i]):
                entries.iloc[i] = True
                in_trade = True
                atr_value = atr.iloc[i]
                if pd.notna(atr_value):
                    active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                    sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            regime_break = not bool(calm_regime.iloc[i])
            trend_break = not bool(uptrend.iloc[i])
            if stop_hit or regime_break or trend_break:
                exits.iloc[i] = True
                # Only the ATR stop is a price LEVEL (explicit exit_price). The
                # regime / trend breaks are close-triggered MARKET exits: no
                # explicit exit_price, so they fill at the next bar's open.
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif regime_break:
                    exit_reason.iloc[i] = "regime_break"
                else:
                    exit_reason.iloc[i] = "trend_break"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "Rolling_Vol": vol,
        "Rolling_Vol_at_exit": vol,
        "Vol_Median": vol_median,
        "Trend_SMA": sma,
        "Trend_SMA_at_exit": sma,
        "Calm_Regime": calm_regime,
        "Calm_Regime_at_exit": calm_regime,
        "Uptrend": uptrend,
        "Uptrend_at_exit": uptrend,
        "Rebalance_Eligible": rebalance_eligible,
        "ATR": atr,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    drawings: list[dict] = []
    if "timestamp" in df.columns:
        entry_times = pd.to_datetime(
            df.loc[entries[entries].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        exit_times = pd.to_datetime(
            df.loc[exits[exits].index, "timestamp"], utc=True, errors="coerce"
        ).dropna()
        for ts in entry_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Entry",
                }
            )
        for ts in exit_times.iloc[:30]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#ef4444",
                    "text": "Exit",
                }
            )

    return entries, exits, indicators_dict, drawings

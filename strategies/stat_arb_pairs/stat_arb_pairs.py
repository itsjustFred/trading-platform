"""
Single-Leg Z-Score Mean Reversion

Single-instrument mean reversion on the instrument's own cumulative log
return (z-scored over a rolling window). Long-only for vectorbt
compatibility.

Naming integrity (Cleanup #35 / Settlement S15): this file does NOT trade a
pair — there is no second leg, no spread between two instruments and no
market-neutral hedge. Meta/tags below describe the implemented single-leg
behavior so discovery/portfolio tooling does not route it as pairs trading.
"""

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "Single-Leg Z-Score Mean Reversion",
    "version": "1.3.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/statistical_arbitrage_pairs.py",
    "notes": (
        "- KEIN Pairs-Trading: der Z-Score wird auf den kumulierten Log-"
        "Returns des EINZELNEN Instruments berechnet (Single-Leg Mean "
        "Reversion), nicht auf einem Spread zwischen zwei Instrumenten.\n"
        "- Long-only; keine Market-Neutral-Absicherung.\n"
        "- 1.3.0: Z-Score-Ausstiege sind Market-Exits ohne exit_price=close "
        "(Fill am naechsten Open); der ATR-Stop bleibt ein explizites Level.\n"
        "- Quellen-Schwellen exakt: Lookback 60, Entry |z| >= 2.0, "
        "Mean-Reversion-Exit bei z >= -0.5, statistischer Fail-Stop bei "
        "z <= -4.0 (stop_zscore der Quelle).\n"
        "- KEIN ATR-Stop im Standard (Erfingung des Ports, entfernt "
        "2026-09-09); Opt-in via use_atr_stop=true. Die Quelle kennt "
        "kein ATR. Sizing ohne Stop faellt auf Engine-Fallbacks zurueck.\n"
        "- Dropped (nicht single-symbol-exprimierbar): zweites Bein, "
        "Kalman-Hedge-Ratio, Korrelations-/Half-Life-Validierung, "
        "Short-Spread-Seite."
    ),
    "tags": {
        "type": ["mean-reversion", "single-instrument", "z-score"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/quantconnect-strategies",
        "asset_class": ["equities"],
        "complexity": "medium",
        "naming_note": (
            "slug retained as stat_arb_pairs for backtest-artifact compatibility; "
            "implementation is single-leg z-score mean reversion, not a pair"
        ),
    },
}


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    lookback = int(params.get("lookback", 60))
    entry_zscore = float(params.get("entry_zscore", 2.0))
    exit_zscore = float(params.get("exit_zscore", 0.5))
    stop_zscore = float(params.get("stop_zscore", 4.0))
    # Opt-in extra (default OFF - not source behavior; the source knows
    # no ATR at all).
    use_atr_stop = bool(params.get("use_atr_stop", False))
    atr_period = int(params.get("atr_period", 14))
    atr_stop_mult = float(params.get("atr_stop_mult", 2.0))

    close = df["close"]
    high = df["high"]
    low = df["low"]
    log_returns = np.log(close / close.shift(1)).fillna(0.0)
    cum_ret = log_returns.cumsum()
    rolling_mean = cum_ret.rolling(lookback).mean()
    rolling_std = cum_ret.rolling(lookback).std().replace(0, np.nan)
    zscore = (cum_ret - rolling_mean) / rolling_std
    atr = None
    if use_atr_stop:
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.ewm(alpha=1.0 / atr_period, min_periods=atr_period, adjust=False).mean()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_long = False
    active_stop = np.nan

    for i in range(lookback, len(df)):
        z_value = zscore.iloc[i]
        if pd.isna(z_value):
            continue
        if in_long:
            stop_hit = (
                use_atr_stop
                and pd.notna(active_stop)
                and float(low.iloc[i]) <= float(active_stop)
            )
            mean_reversion_hit = z_value >= -exit_zscore
            zscore_stop_hit = z_value <= -stop_zscore
            if stop_hit or mean_reversion_hit or zscore_stop_hit:
                exits.iloc[i] = True
                # Only the ATR stop is a price LEVEL (explicit exit_price). The
                # z-score exits are close-triggered MARKET exits: no explicit
                # exit_price, so they fill at the next bar's open.
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif mean_reversion_hit:
                    exit_reason.iloc[i] = "mean_reversion"
                else:
                    exit_reason.iloc[i] = "zscore_stop"
                in_long = False
                active_stop = np.nan
        elif z_value <= -entry_zscore:
            entries.iloc[i] = True
            in_long = True
            if use_atr_stop and atr is not None:
                atr_value = atr.iloc[i]
                if pd.notna(atr_value):
                    active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                    sl_price.iloc[i] = active_stop

    indicators_dict = {
        "ZScore": zscore,
        "ZScore_at_exit": zscore,
        "Rolling_Mean": rolling_mean,
        "Rolling_Std": rolling_std,
        "sl_price": sl_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }
    if atr is not None:
        indicators_dict["ATR"] = atr
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

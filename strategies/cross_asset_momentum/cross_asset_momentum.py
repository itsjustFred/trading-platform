"""
Strategy: Cross-Asset Momentum (Time-Series, single-symbol proxy)

Single-symbol expression of desenyon's CrossAssetMomentumStrategy
(cross_asset_momentum.py): weighted time-series momentum across the
source lookbacks. Long-only: enter when the composite momentum score
is positive, exit when it turns non-positive.

Source fidelity
---------------
- lookbacks [21, 63, 126, 252] and weights [0.35, 0.30, 0.20, 0.15]
  (``momentum_lookback`` / ``momentum_weights`` in the source).
- Volatility window 60 bars (source: ``np.std(returns[-60:])``).
- Entry/exit on the composite score mirrors the source's long-only
  filter (``ApplyRiskParity`` keeps positive signals only).

Opt-in extras (default OFF - NOT source behavior)
-------------------------------------------------
- ``use_atr_stop``: ATR-based protection stop (adapter retrofit).
- ``use_momentum_exit``: exit on ``composite < momentum_exit``
  (default -0.01).
- ``use_drawdown_exit``: exit on rolling drawdown below ``dd_exit``
  (default -0.08).

Scope notes (documented, not implemented)
-----------------------------------------
The source is a MULTI-ASSET system; this adapter runs one symbol:
- no cross-sectional momentum ranking across assets/classes;
- no inverse-volatility risk-parity weighting, no 15 % asset / 40 %
  class caps, no 12 % volatility targeting;
- no SPY-based crash protection (defensive TLT/IEF/GLD/TIP leg);
- weekly rebalancing is approximated by daily signal bars.
"""

STRATEGY_META = {
    "name": "Cross-Asset Momentum",
    "version": "1.1.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/cross_asset_momentum.py",
    "notes": (
        "Single-symbol proxy of a multi-asset system. Source-exact: "
        "lookbacks [21, 63, 126, 252], weights [0.35, 0.30, 0.20, 0.15], "
        "vol window 60. Dropped (not single-symbol-expressible): "
        "cross-sectional ranking, risk-parity weighting + caps, vol "
        "targeting, SPY crash-protection leg. ATR stop / momentum-break "
        "exit / drawdown exit are opt-in only (default OFF)."
    ),
    "tags": {
        "type": ["momentum", "cross-asset"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/CrossAssetMomentumAlpha",
        "asset_class": ["multi-asset", "etf"],
        "complexity": "complex",
    },
}

import numpy as np
import pandas as pd


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    # QuantConnect ATR defaults to Wilder smoothing.
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def generate_signals(df: pd.DataFrame, params: dict):
    lookbacks = params.get("lookbacks", [21, 63, 126, 252])
    weights = params.get("weights", [0.35, 0.30, 0.20, 0.15])
    vol_window = params.get("vol_window", 60)
    # Opt-in extras (default OFF - not source behavior).
    use_atr_stop = params.get("use_atr_stop", False)
    use_momentum_exit = params.get("use_momentum_exit", False)
    use_drawdown_exit = params.get("use_drawdown_exit", False)
    momentum_exit = params.get("momentum_exit", -0.01)
    dd_exit = params.get("dd_exit", -0.08)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    composite = pd.Series(0.0, index=df.index)
    # `strict=True` is a real invariant here: a shorter `weights` than
    # `lookbacks` used to drop the longest legs silently, which both loses a
    # signal leg and leaves the composite's weights not summing to one.
    for lookback, weight in zip(lookbacks, weights, strict=True):
        composite += close.pct_change(lookback).fillna(0) * weight

    vol = close.pct_change().rolling(vol_window).std() * np.sqrt(252)
    vol_adj_mom = composite / vol.replace(0, np.nan)
    rolling_max = close.rolling(252, min_periods=1).max()
    drawdown = (close - rolling_max) / rolling_max
    atr = _wilder_atr(high, low, close, atr_period)

    entry_cond = (composite > 0) & (vol_adj_mom > 0)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan
    warmup = max(lookbacks) + vol_window

    for i in range(warmup, len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            if use_atr_stop:
                atr_value = atr.iloc[i]
                if pd.notna(atr_value):
                    active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                    sl_price.iloc[i] = active_stop
        elif in_trade:
            stop_hit = (
                use_atr_stop
                and pd.notna(active_stop)
                and float(low.iloc[i]) <= float(active_stop)
            )
            signal_break = not bool(entry_cond.iloc[i])
            momentum_break = (
                use_momentum_exit
                and pd.notna(composite.iloc[i])
                and bool(composite.iloc[i] < momentum_exit)
            )
            drawdown_break = (
                use_drawdown_exit
                and pd.notna(drawdown.iloc[i])
                and bool(drawdown.iloc[i] < dd_exit)
            )
            if stop_hit or signal_break or momentum_break or drawdown_break:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif momentum_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "momentum_break"
                elif drawdown_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "drawdown_break"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "signal_break"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "Composite_Momentum": composite,
        "Composite_Momentum_at_exit": composite,
        "Vol_Adjusted_Momentum": vol_adj_mom,
        "Rolling_Drawdown": drawdown,
        "Rolling_Drawdown_at_exit": drawdown,
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
        drawings.extend(
            {
                "type": "marker",
                "time": ts.isoformat(),
                "position": "belowBar",
                "shape": "arrowUp",
                "color": "#22c55e",
                "text": "Entry",
            }
            for ts in entry_times.iloc[:30]
        )
        drawings.extend(
            {
                "type": "marker",
                "time": ts.isoformat(),
                "position": "aboveBar",
                "shape": "arrowDown",
                "color": "#ef4444",
                "text": "Exit",
            }
            for ts in exit_times.iloc[:30]
        )

    return entries, exits, indicators_dict, drawings

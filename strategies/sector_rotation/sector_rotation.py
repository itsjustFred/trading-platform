"""
Strategy: Sector Rotation (single-symbol momentum proxy)

Single-symbol expression of desenyon's SectorRotationStrategy
(sector_rotation_strategy.py). The entry proxy (accepted as-is) is:
mid-term momentum positive AND short-term momentum positive AND price
above the 200-day trend filter.

Source fidelity
---------------
- Entry legs: ``momentum_3m > 0`` (63 bars), ``momentum_1m > 0``
  (21 bars), ``close > SMA(200)`` (trend_score leg).
- Default exit: trend break (``close < SMA(200)``) - the only
  source-traceable single-symbol exit leg.

Opt-in extras (default OFF - NOT source behavior)
-------------------------------------------------
- ``use_atr_stop``: ATR-based protection stop (adapter retrofit).
- ``use_momentum_exit``: exit on ``momentum_63 < 0`` alone.
- ``use_weakness_exit``: exit on ``close.pct_change(10) < -0.03``.

Scope notes (documented, not implemented)
-----------------------------------------
The source is a MULTI-SECTOR portfolio system; this adapter runs one
symbol:
- no 11-sector universe, no relative-strength ranking vs SPY;
- no regime tables (early/late_expansion, peak, contraction, trough)
  and no regime-driven base allocations;
- no RS-rank weight adjustments (1.2 / 1.0 / 0.8 / 0.5);
- no defensive fallback basket (XLP/XLV/XLU);
- monthly rebalancing is approximated by daily signal bars;
- long-only (the source is long-only across sectors as well).
"""

STRATEGY_META = {
    "name": "Sector Rotation",
    "version": "1.1.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/sector_rotation_strategy.py",
    "notes": (
        "Single-symbol proxy of a multi-sector rotation system. "
        "Source-exact entry: mom63 > 0, mom21 > 0, close > SMA200. "
        "Default exit: trend break (close < SMA200). Dropped (not "
        "single-symbol-expressible): sector universe, RS ranking, "
        "regime tables, defensive basket. ATR stop / mom63<0 exit / "
        "10d-weakness exit are opt-in only (default OFF)."
    ),
    "tags": {
        "type": ["momentum", "rotation"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/SectorMomentumAlpha",
        "asset_class": ["etf", "equities"],
        "complexity": "medium",
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
    lookback = params.get("lookback", 63)
    fast_lookback = params.get("fast_lookback", 21)
    exit_lookback = params.get("exit_lookback", 10)
    weakness_threshold = params.get("weakness_threshold", -0.03)
    # Opt-in extras (default OFF - not source behavior).
    use_atr_stop = params.get("use_atr_stop", False)
    use_momentum_exit = params.get("use_momentum_exit", False)
    use_weakness_exit = params.get("use_weakness_exit", False)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    momentum = close.pct_change(lookback)
    fast_momentum = close.pct_change(fast_lookback)
    sma_200 = close.rolling(200).mean()
    short_weakness = close.pct_change(exit_lookback) < weakness_threshold
    atr = _wilder_atr(high, low, close, atr_period)

    entry_cond = (momentum > 0) & (fast_momentum > 0) & (close > sma_200)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    active_stop = np.nan

    for i in range(max(lookback, 200), len(df)):
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
            trend_break = bool(close.iloc[i] < sma_200.iloc[i]) if pd.notna(
                sma_200.iloc[i]
            ) else False
            momentum_break = (
                use_momentum_exit
                and pd.notna(momentum.iloc[i])
                and bool(momentum.iloc[i] < 0)
            )
            weakness_hit = bool(short_weakness.iloc[i]) if use_weakness_exit else False
            if stop_hit or trend_break or momentum_break or weakness_hit:
                exits.iloc[i] = True
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif trend_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "trend_break"
                elif momentum_break:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "momentum_break"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "short_weakness"
                in_trade = False
                active_stop = np.nan

    indicators_dict = {
        "Momentum_63": momentum,
        "Momentum_63_at_exit": momentum,
        "Momentum_21": fast_momentum,
        "SMA_200": sma_200,
        "Short_Weakness": short_weakness,
        "Short_Weakness_at_exit": short_weakness,
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

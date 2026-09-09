"""
Strategy: Short-Term Reversal (single-symbol, long-only proxy)

Single-symbol expression of desenyon's ShortTermReversalStrategy
(short_term_reversal_strategy.py): buy the 5-day loser, exit on
reversal completion, fixed stop, or end of holding period.

Source fidelity
---------------
- ``lookback_period = 5``: entry on ``Return_5D < -0.05``.
- ``holding_period = 5``: time exit after 5 bars.
- Profit target +-5 % from entry (source: ``pnl_pct > 0.05`` long /
  ``pnl_pct < -0.05`` short; long-only proxy uses +5 %).
- Fixed stop +-8 % from entry (source stop-loss; long-only: -8 %).
  ``sl_price = entry_close * (1 - stop_pct)``.
- NO RSI filter (the source has no RSI).

Scope notes (documented, not implemented)
-----------------------------------------
The source is a CROSS-SECTIONAL long/short universe system; this
adapter runs one symbol, long-only:
- no 500-stock liquid-universe selection, no loser/winner ranking;
- no long 20 / short 20 portfolio, no z-score (|z| > 1.5) or
  volume-spike filters, no z-weighted sizing;
- long-only: the short leg (winners, -5 % target / +8 % stop) is dropped;
- no SPY regime filter (volatility / trend guard) and no exposure cut;
- no 30 % sector caps;
- weekly rebalance is approximated by daily signal bars.
"""

STRATEGY_META = {
    "name": "Short Term Reversal",
    "version": "1.1.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/short_term_reversal_strategy.py",
    "notes": (
        "Single-symbol long-only proxy of a cross-sectional long/short "
        "universe system. Source-exact: 5d lookback, -5 % entry, +5 % "
        "profit target, -8 % fixed stop, 5-bar hold. No RSI (source has "
        "none), no ATR stop (source uses a fixed 8 % stop). Dropped: "
        "universe ranking, short leg, z-score/volume filters, SPY regime "
        "filter, sector caps."
    ),
    "tags": {
        "type": ["mean-reversion"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/short_term_reversal_strategy",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    lookback = params.get("lookback", 5)
    entry_threshold = params.get("entry_threshold", -0.05)
    # Source profit target is +-5 % on position PnL (not on trailing
    # returns). ``exit_threshold`` is kept as a deprecated alias.
    if "profit_target" in params:
        profit_target = params.get("profit_target", 0.05)
    else:
        profit_target = params.get("exit_threshold", 0.05)
    stop_pct = params.get("stop_pct", 0.08)
    max_hold = params.get("max_hold", 5)

    close = df["close"]
    ret_5d = close.pct_change(lookback)

    raw_entries = ret_5d < entry_threshold

    in_trade = False
    bars_held = 0
    entry_price = np.nan
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    bars_held_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    for i in range(len(df)):
        if not in_trade and bool(raw_entries.iloc[i]):
            in_trade = True
            bars_held = 0
            entry_price = float(close.iloc[i])
            entries.iloc[i] = True
            sl_price.iloc[i] = entry_price * (1 - stop_pct)
        elif in_trade:
            bars_held += 1
            pnl_pct = (
                (float(close.iloc[i]) - entry_price) / entry_price
                if entry_price > 0
                else 0.0
            )
            stop_hit = pnl_pct <= -stop_pct
            profit_hit = pnl_pct >= profit_target
            time_exit_hit = bars_held >= max_hold
            if stop_hit or profit_hit or time_exit_hit:
                in_trade = False
                exits.iloc[i] = True
                bars_held_at_exit.iloc[i] = bars_held
                if stop_hit:
                    exit_price.iloc[i] = entry_price * (1 - stop_pct)
                    exit_reason.iloc[i] = "stop_loss"
                elif profit_hit:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "profit_target"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "time_exit"
                entry_price = np.nan

    indicators_dict = {
        "Return_5D": ret_5d,
        "Return_5D_at_exit": ret_5d,
        "sl_price": sl_price,
        "bars_held_at_exit": bars_held_at_exit,
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

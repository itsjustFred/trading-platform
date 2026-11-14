"""
Strategy: Trend Following (single-symbol, long-only proxy)

Single-symbol expression of desenyon's TrendFollowingCTAStrategy
(trend_following_cta.py): EMA-stack trend filter with ATR-based
protection stop and activation-gated trailing.

Source fidelity
---------------
- EMA(20) / EMA(50) / EMA(200) (source: ``self.EMA(..., 20/50/200)``).
- ATR(20) with Wilder smoothing (source: ``self.ATR(..., 20)``).
- Initial stop ``2.5 x ATR`` below entry (``atr_stop_multiplier``).
- Trailing only after ``2.0 ATR`` profit (``trailing_stop_activation``):
  ``new_stop = price - 2.5 x ATR``, ratcheted up, never down.
- Entry proxy for the source's ``|composite| > 0.5`` long trigger:
  EMA20 crosses above EMA50 while EMA50 is above EMA200.
- Exit on fast cross down (signal reversal) or stop hit. Both are
  close-triggered market exits: no explicit ``exit_price``, so under the
  default ``signal_exit_fill="next_open"`` they fill at the next bar's open.

Scope notes (documented, not implemented)
-----------------------------------------
The source is a MULTI-MARKET long/short CTA; this adapter runs one
symbol, long-only:
- no Donchian-55 breakout leg (long signal uses price vs EMA200 only);
- no composite weighting (0.25 / 0.40 / 0.35) and no weak-signal
  (``|composite| < 0.3``) scale-out;
- no volatility-targeted sizing (1 % risk per trade with 2.5-ATR stop
  is engine-side here);
- no sector-exposure caps, no correlation caps;
- no shorts (source trades both directions);
- daily bars (no 30/45-min post-open scheduling).
"""

STRATEGY_META = {
    "name": "Trend Following",
    "version": "1.2.0",
    "source_url": "https://github.com/desenyon/quantconnect-strategies/blob/main/trend_following_cta.py",
    "notes": (
        "Single-symbol long-only proxy of a multi-market long/short CTA. "
        "Source-exact: EMA 20/50/200, Wilder ATR20, 2.5xATR stop with "
        "trailing only after 2.0 ATR profit. 1.2.0: market exits (trailing "
        "stop, EMA cross-down) carry no exit_price=close and fill at the next "
        "open. Dropped (not "
        "single-symbol-expressible): Donchian breakout, composite "
        "weighting, vol targeting, sector/correlation caps, shorts."
    ),
    "tags": {
        "type": ["momentum", "trend-following"],
        "source": "desenyon-github",
        "link": "https://github.com/desenyon/trend_following_cta",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}

import numpy as np
import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    # ``fast_sma``/``mid_sma``/``slow_sma`` are deprecated aliases from the
    # SMA-based port (pre-1.1.0); the source uses EMAs.
    fast = params.get("fast_ema", params.get("fast_sma", 20))
    mid = params.get("mid_ema", params.get("mid_sma", 50))
    slow = params.get("slow_ema", params.get("slow_sma", 200))
    atr_period = params.get("atr_period", 20)
    atr_stop_mult = params.get("atr_stop_mult", 2.5)
    trailing_activation = params.get("trailing_activation", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_mid = close.ewm(span=mid, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()

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

    fast_cross_up = (ema_fast > ema_mid) & (ema_fast.shift(1) <= ema_mid.shift(1))
    trend_up = ema_mid > ema_slow
    fast_cross_down = (ema_fast < ema_mid) & (ema_fast.shift(1) >= ema_mid.shift(1))

    # Warmup: EMAs/ATR brauchen Seeding (Quelle: 220 Tage Warmup). Ohne
    # diese Schranke feuern Entries auf ungesaetteten EMAs mit NaN-ATR
    # (kein Stop fuer das Sizing).
    warmup = int(max(slow, atr_period))

    in_trade = False
    entry_price = 0.0
    trailing_stop_value = 0.0
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    trailing_stop_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    for i in range(warmup, len(df)):
        if pd.isna(ema_slow.iloc[i]):
            continue

        if not in_trade:
            if bool(fast_cross_up.iloc[i]) and bool(trend_up.iloc[i]):
                atr_value = atr.iloc[i]
                entries.iloc[i] = True
                in_trade = True
                entry_price = float(close.iloc[i])
                if pd.notna(atr_value):
                    trailing_stop_value = entry_price - atr_stop_mult * float(atr_value)
                    sl_price.iloc[i] = trailing_stop_value
        else:
            atr_value = atr.iloc[i]
            if pd.notna(atr_value) and float(atr_value) > 0 and entry_price > 0:
                # Trail ONLY after 2.0 ATR profit (source:
                # UpdateTrailingStop gated by trailing_stop_activation).
                profit_atr = (float(close.iloc[i]) - entry_price) / float(atr_value)
                if profit_atr > trailing_activation:
                    new_stop = float(close.iloc[i]) - atr_stop_mult * float(atr_value)
                    trailing_stop_value = max(trailing_stop_value, new_stop)
            trailing_stop_at_exit.iloc[i] = trailing_stop_value

            trailing_stop_hit = float(close.iloc[i]) <= trailing_stop_value
            cross_down_hit = bool(fast_cross_down.iloc[i])
            if trailing_stop_hit or cross_down_hit:
                in_trade = False
                exits.iloc[i] = True
                trailing_stop_at_exit.iloc[i] = trailing_stop_value
                # Both exit legs are close-triggered MARKET exits: no explicit
                # exit_price (a price equal to the signal bar's close would
                # contradict the default next_open fill). They fill at the next
                # bar's open. Filling at the stop level above the close would
                # systematically sell LONGs too high, which is why no level is
                # emitted here either.
                if trailing_stop_hit:
                    exit_reason.iloc[i] = "trailing_stop"
                else:
                    exit_reason.iloc[i] = "ema_cross_down"
                trailing_stop_value = 0.0
                entry_price = 0.0

    indicators_dict = {
        "EMA_Fast": ema_fast,
        "EMA_Mid": ema_mid,
        "EMA_Slow": ema_slow,
        "EMA_Fast_at_exit": ema_fast,
        "EMA_Mid_at_exit": ema_mid,
        "EMA_Slow_at_exit": ema_slow,
        "ATR": atr,
        "sl_price": sl_price,
        "trailing_stop_at_exit": trailing_stop_at_exit,
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

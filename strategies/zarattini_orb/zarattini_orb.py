"""
5-minute Opening Range Breakout, ATR-stop variant (Zarattini, Barbon, Aziz,
"A Profitable Day Trading Strategy For The U.S. Equity Market", 2024, the
"Stocks in Play" ORB:
https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/).
The earlier QQQ paper (Zarattini & Aziz, SSRN 4416622, 2023) uses a first-candle
stop and a 10R target instead; this strategy implements the ATR-stop rules.
NOT implemented: the paper's "stocks in play" selection (top-20 relative
volume names); this strategy trades one symbol (an index/ETF).

Published rules (5-minute bars, regular session only):

- Look at the FIRST 5-minute candle of the session (09:30-09:35 ET).
- Bullish first candle (close > open): go LONG at the open of the second
  candle.  Bearish (close < open): go SHORT.  Doji: no trade.
- Stop loss: 10 % of the 14-day daily ATR away from the entry price.
- No profit target; flat at the close of the session (last 5-minute bar).
- One trade per day.  Risk 1 % of equity per trade (``risk_per_trade_pct``)
  with a 4x leverage ceiling (``max_leverage``) - run with
  ``risk_per_trade_pct=0.01, max_leverage=4`` and no position cap.

Platform execution notes (documented deviations):

- The signal is produced on the first candle's close and fills at the next
  bar's open = the second candle's open, exactly the published entry.  The stop
  level is anchored on the FIRST CANDLE'S CLOSE (``close -/+ 0.1 ATR``) because
  the fill price is unknown when the signal is produced; the publication
  anchors it on the actual entry.
- Daily ATR(14) uses Wilder smoothing over session bars aggregated from the
  input frame (the paper uses the standard 14-day ATR), the PREVIOUS day's
  value (no look-ahead); a day without a full ATR window does not trade.
- The session is taken from America/New_York timestamps; days whose first
  candle is not the 09:30 bar are skipped.
"""

import math

import pandas as pd

STRATEGY_META = {
    "name": "Zarattini ORB (5 min)",
    "version": "1.0.0",
    "source_url": "https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/",
    "parameters": {
        "atr_len": 14,
        "stop_atr_frac": 0.10,
        "session_open": "09:30",
        "range_minutes": 5,
    },
    "notes": (
        "- Published 5-minute ORB: first candle direction, stop 10% of daily ATR(14), flat at close.\n"
        "- Stop anchored on the signal (first-candle) close, not the fill; one trade per day.\n"
        "- Run with risk_per_trade_pct=0.01, max_leverage=4."
    ),
    "tags": {
        "type": ["breakout", "intraday", "orb"],
        "source": "zarattini-barbon-aziz-2024",
        "asset_class": ["equities", "indices"],
        "complexity": "simple",
    },
}


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    atr_len = int(params.get("atr_len", 14))
    stop_frac = float(params.get("stop_atr_frac", 0.10))
    session_open = str(params.get("session_open", "09:30"))
    open_h, open_m = (int(x) for x in session_open.split(":"))

    stamps = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    dates = stamps.dt.date.tolist()
    minutes = (stamps.dt.hour * 60 + stamps.dt.minute).tolist()
    o = [float(v) for v in df["open"]]
    h = [float(v) for v in df["high"]]
    lo = [float(v) for v in df["low"]]
    c = [float(v) for v in df["close"]]
    size = len(c)

    # daily bars (session aggregate) in order of appearance
    day_index: dict = {}
    days: list[dict] = []
    for i in range(size):
        d = dates[i]
        if d not in day_index:
            day_index[d] = len(days)
            days.append({"high": h[i], "low": lo[i], "close": c[i], "first": i, "last": i})
        else:
            rec = days[day_index[d]]
            rec["high"] = max(rec["high"], h[i])
            rec["low"] = min(rec["low"], lo[i])
            rec["close"] = c[i]
            rec["last"] = i

    # Wilder ATR over the daily bars; value for day k is known at ITS close.
    atr = [math.nan] * len(days)
    prev_close = None
    acc: list[float] = []
    for k, rec in enumerate(days):
        tr = rec["high"] - rec["low"]
        if prev_close is not None:
            tr = max(tr, abs(rec["high"] - prev_close), abs(rec["low"] - prev_close))
        prev_close = rec["close"]
        if k < atr_len - 1:
            acc.append(tr)
        elif k == atr_len - 1:
            acc.append(tr)
            atr[k] = sum(acc) / atr_len
        else:
            atr[k] = (atr[k - 1] * (atr_len - 1) + tr) / atr_len

    entries = [False] * size
    exits = [False] * size
    direction = [None] * size
    stop_loss = [math.nan] * size

    for k, rec in enumerate(days):
        first, last = rec["first"], rec["last"]
        exits[last] = True  # flat at the close of the session
        if k == 0 or math.isnan(atr[k - 1]):
            continue
        if minutes[first] != open_h * 60 + open_m or last == first:
            continue
        if c[first] == o[first]:
            continue  # doji
        side = 1 if c[first] > o[first] else -1
        entries[first] = True
        direction[first] = "LONG" if side > 0 else "SHORT"
        stop_loss[first] = c[first] - side * stop_frac * atr[k - 1]

    idx = df.index
    daily_atr = [atr[day_index[d] - 1] if day_index[d] > 0 else math.nan for d in dates]
    indicators = {
        "daily_atr_prev": pd.Series(daily_atr, index=idx),
        "stop_loss": pd.Series(stop_loss, index=idx),
        "direction": pd.Series(direction, index=idx, dtype="object"),
    }
    return pd.Series(entries, index=idx), pd.Series(exits, index=idx), indicators, []

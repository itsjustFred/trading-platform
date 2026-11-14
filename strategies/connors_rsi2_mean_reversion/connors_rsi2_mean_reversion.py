"""
Strategy: Connors RSI(2) Mean Reversion

Faithful port of the 2-period-RSI mean-reversion rule popularised by Larry
Connors and Cesar Alvarez, "Short Term Trading Strategies That Work: A
Quantified Guide to Trading Stocks and ETFs" (TradingMarkets, 2008) —
the single-day form that became the canonical public restatement of the
rule (Connors/Alvarez FAQ, e.g. https://www.connorsresearch.com):

    Only trade when the instrument is above its 200-day SMA (long-term
    uptrend).
    BUY  when the 2-period RSI closes below 10.
    SELL when the close closes above the 5-period SMA.

Modelling decisions that the rule leaves open (documented, not invented)
----------------------------------------------------------------------
- RSI(2) is Welles Wilder's RSI with period 2: the first average
  gain/loss is the simple mean of the first 2 changes, then Wilder
  smoothing with alpha = 1/period (``avg = avg - alpha*avg + alpha*gain``).
- "Closes below 10" is read on the bar's close; the buy decision is taken
  on the signal bar and the fill happens on the NEXT bar's open (platform
  contract §1). The exit decision ("closes above the 5-period SMA") is
  likewise taken on the close; this platform fills a signal exit at the
  trigger bar's CLOSE (``signal_exit_fill="close"``), which is the
  convention the book's own close-price backtests describe.
- One position at a time: while long, further oversold readings do not
  add; the exit rule runs until it fires.
- No stop-loss and no time exit: Connors/Alvarez are explicit that stops
  cut mean-reversion trades short ("consider not using stops at all in
  mean reversion strategies"), so a protective stop would change the
  published behaviour. Sizing is engine-side.
- Cash: when flat, nothing is held. The source's universe is stocks/ETFs
  traded long-only, so there is no separate cash leg to reproduce.

Scope notes (documented, NOT implemented)
-----------------------------------------
- The book applies the rule set to a cross-sectional stock/ETF universe
  (the platform runs ONE symbol per backtest).
- The book's Chapter 9 table uses a DIFFERENT rule — the CUMULATIVE
  2-period RSI (the sum of the last two 2-period RSI readings) below 35 -
  and reports backtests on SPY 1993-2007. The single-day rule ported
  here is what third-party implementations and the author's own FAQ
  describe; the parity report compares both windows and names the rule
  difference explicitly instead of blending the two numbers.
- Volatility/regime filters, long/short symmetry and position sizing are
  not part of the rule.
"""

STRATEGY_META = {
    "name": "Connors RSI(2) Mean Reversion",
    "version": "1.0.0",
    "source_url": "https://www.connorsresearch.com/",
    "notes": (
        "Connors/Alvarez 'Short Term Trading Strategies That Work' (2008) "
        "2-period RSI rule on one symbol: long only while close > SMA(200), "
        "buy when RSI(2) < 10, sell when close > SMA(5). Wilder RSI(2) with "
        "the simple-mean seed; no stop-loss, no time exit, one position at a "
        "time. The single-day form ported here is the canonical public "
        "restatement; the book's Chapter 9 table uses the cumulative RSI(2) "
        "variant (sum of two readings below 35), which is NOT what this file "
        "implements. Entry fills at the next bar's open, signal exit fills at "
        "the trigger bar's close."
    ),
    "parameters": {
        "rsi_period": 2,
        "rsi_entry": 10.0,
        "trend_sma": 200,
        "exit_sma": 5,
    },
    "tags": {
        "type": ["mean-reversion", "rsi"],
        "source": "connors-alvarez-2008",
        "link": "https://www.connorsresearch.com/",
        "asset_class": ["equities", "etf"],
        "complexity": "simple",
        "data_basis": "price",
        "rebalance": "signal",
    },
}

import numpy as np
import pandas as pd


def wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (``New Concepts in Technical Trading Systems``).

    The first average gain/loss is the simple mean of the first
    ``period`` changes, every later value is Wilder-smoothed with
    ``alpha = 1/period``. ``period`` changes are consumed, so the first
    usable reading lands on bar ``period``.
    """
    period = int(period)
    change = close.diff()
    gain = change.clip(lower=0.0)
    loss = (-change).clip(lower=0.0)

    avg_gain = np.full(len(close), np.nan)
    avg_loss = np.full(len(close), np.nan)
    for i in range(1, len(close)):
        if i < period:
            continue
        if i == period:
            avg_gain[i] = gain.iloc[1 : period + 1].mean()
            avg_loss[i] = loss.iloc[1 : period + 1].mean()
        else:
            alpha = 1.0 / period
            avg_gain[i] = (1.0 - alpha) * avg_gain[i - 1] + alpha * gain.iloc[i]
            avg_loss[i] = (1.0 - alpha) * avg_loss[i - 1] + alpha * loss.iloc[i]

    avg_gain = pd.Series(avg_gain, index=close.index)
    avg_loss = pd.Series(avg_loss, index=close.index)

    warming_up = avg_gain.isna() | avg_loss.isna()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + ratio))
    # The classic Wilder degenerate cases, resolved explicitly instead of
    # letting 0/0 leak through as NaN or as 100:
    #   no down change at all            -> RSI = 100
    #   no up change at all              -> RSI = 0
    #   neither up nor down (flat window)-> undefined, stays NaN
    flat = (avg_gain == 0.0) & (avg_loss == 0.0)
    only_losses = (avg_gain == 0.0) & (avg_loss > 0.0)
    only_gains = (avg_gain > 0.0) & (avg_loss == 0.0)
    rsi = rsi.where(~only_gains, 100.0)
    rsi = rsi.where(~only_losses, 0.0)
    rsi = rsi.where(~flat, np.nan)
    rsi = rsi.where(~warming_up, np.nan)
    return rsi


def generate_signals(df: pd.DataFrame, params: dict):
    rsi_period = int(params.get("rsi_period", 2))
    rsi_entry = float(params.get("rsi_entry", 10.0))
    trend_sma_period = int(params.get("trend_sma", 200))
    exit_sma_period = int(params.get("exit_sma", 5))

    close = df["close"].astype(float)
    rsi = wilder_rsi(close, rsi_period)
    trend_sma = close.rolling(trend_sma_period, min_periods=trend_sma_period).mean()
    exit_sma = close.rolling(exit_sma_period, min_periods=exit_sma_period).mean()

    in_uptrend = close > trend_sma
    entry_cond = in_uptrend & (rsi < rsi_entry)
    exit_cond = close > exit_sma

    entries = pd.Series(False, index=close.index, dtype=bool)
    exits = pd.Series(False, index=close.index, dtype=bool)
    exit_reason = pd.Series(index=close.index, dtype="object")
    exit_price = pd.Series(np.nan, index=close.index, dtype=float)
    sma_at_exit = pd.Series(np.nan, index=close.index, dtype=float)
    rsi_at_exit = pd.Series(np.nan, index=close.index, dtype=float)

    warmup = max(trend_sma_period, exit_sma_period, rsi_period + 1)
    in_position = False
    for i in range(warmup, len(df)):
        if pd.isna(rsi.iloc[i]) or pd.isna(trend_sma.iloc[i]) or pd.isna(exit_sma.iloc[i]):
            continue
        if not in_position:
            if bool(entry_cond.iloc[i]):
                entries.iloc[i] = True
                in_position = True
        elif bool(exit_cond.iloc[i]):
            exits.iloc[i] = True
            exit_reason.iloc[i] = "close_above_sma5"
            exit_price.iloc[i] = float(close.iloc[i])
            sma_at_exit.iloc[i] = float(exit_sma.iloc[i])
            rsi_at_exit.iloc[i] = float(rsi.iloc[i])
            in_position = False

    indicators_dict = {
        "RSI": rsi,
        "RSI_at_exit": rsi_at_exit,
        "Trend_SMA": trend_sma,
        "Trend_SMA_at_exit": trend_sma,
        "Exit_SMA": exit_sma,
        "Exit_SMA_at_exit": sma_at_exit,
        "In_Uptrend": in_uptrend,
        "In_Uptrend_at_exit": in_uptrend,
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
                    "text": "Buy RSI(2)<10",
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
                    "text": "Sell >SMA5",
                }
            )

    return entries, exits, indicators_dict, drawings
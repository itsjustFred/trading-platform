"""
Strategy: Faber Tactical Asset Allocation (10-month SMA timing)

Faithful port of Mebane T. Faber, "A Quantitative Approach to Tactical
Asset Allocation", The Journal of Wealth Management, Spring 2007, 10(1),
69-79 (SSRN 962461; 2013 update through 2012):

    BUY RULE   Buy when monthly price > 10-month SMA.
    SELL RULE  Sell and move to cash when monthly price < 10-month SMA.

    "1. All entry and exit prices are on the day of the signal at the
        close. The model is only updated once a month on the last day of
        the month. Price fluctuations during the rest of the month are
        ignored.
     2. All data series are total return series including dividends,
        updated monthly."

Source fidelity
---------------
- 10-month simple moving average of MONTH-END closes (``sma_months``).
- The decision is taken on the LAST daily bar of each calendar month and
  is held for the whole following month (no intrabar reconsideration).
- Long-only long/flat: there is no short leg and no cash instrument in
  this port, so "move to cash" means "no position".
- No stop-loss, no take-profit, no position sizing rule of its own
  (the paper's models hold 100% of the allocation in the asset or in
  T-bills; sizing is engine-side, see below).

Scope notes (documented, NOT implemented)
-----------------------------------------
- The paper's cash leg earns 90-day T-bill interest ("cash returns are
  estimated with 90-day Treasury bills"). This platform strategy is
  long/flat in ONE instrument and cannot hold a second (cash) symbol,
  so the flat months earn nothing here. The paper's timing CAGR
  therefore includes a cash yield this port does not add; the parity
  report in ``docs/reports/parity-published-strategies.md`` decomposes
  that gap explicitly.
- The paper excludes taxes, commissions and slippage (its item 4). This
  port leaves cost handling to the engine configuration; the report
  separates the gross (``fees="zero"``) from a costed run.
- The paper's multi-asset allocation framework (US/foreign stocks,
  10y Treasuries, commodities, REITs, equally weighted timing portfolio)
  is a MULTI-SYMBOL system and is not expressed here: this file runs the
  timing rule on ONE symbol.
- Leverage variants (2x levered portfolio) are out of scope.

Platform conventions that differ from the paper
-----------------------------------------------
- The paper fills the signal on the day of the signal AT THE CLOSE.
  This platform fills an ENTRY on the next bar's open (contract §1,
  ``market_order_timing="next_bar_open"``), so an entry is executed one
  bar after the month-end signal, at the open of the first bar of the
  following month. EXITS follow ``signal_exit_fill``: the platform default is
  ``next_open`` (owner decision 2026-11-13: signal exits fill at the next bar's
  open, like entries); the paper's close fill is reproduced by running with
  ``signal_exit_fill="close"`` explicitly.
- The paper's series is a total-return series. Run this strategy on a
  dividend-adjusted (total-return) dataset to reproduce the paper; on a
  price-only dataset the buy-and-hold drag of the missing dividend yield
  shows up in every number. ``tags.data_basis`` documents the required
  basis for discovery tooling.
"""

STRATEGY_META = {
    "name": "Faber Tactical Asset Allocation (10-Month SMA Timing)",
    "version": "1.0.0",
    "source_url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461",
    "notes": (
        "Faber (2007, JWM 10(1):69-79) timing model on one symbol: buy when "
        "the month-end close > 10-month SMA of month-end closes, sell when it "
        "is below. Decision taken on the last daily bar of each month and "
        "held for the following month. Long/flat only: the source's cash leg "
        "earns 90-day T-bill interest and cannot be expressed on a single "
        "symbol, and taxes/commissions/slippage are excluded in the source. "
        "Entry fills at the next bar's open (platform convention), the source "
        "fills at the signal close. Requires a total-return (dividend "
        "adjusted) dataset to match the published numbers."
    ),
    "parameters": {
        "sma_months": 10,
        "price_basis": "total_return",
    },
    "tags": {
        "type": ["trend-following", "market-timing", "tactical-asset-allocation"],
        "source": "faber-2007-jwm",
        "link": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461",
        "asset_class": ["equities", "etf"],
        "complexity": "simple",
        "data_basis": "total_return",
        "rebalance": "monthly",
    },
}

import numpy as np
import pandas as pd


def _month_keys(timestamps: pd.Series) -> pd.PeriodIndex:
    """Calendar-month keys for a timestamp series (timezone dropped)."""
    parsed = pd.to_datetime(timestamps, utc=True, errors="coerce")
    return pd.PeriodIndex(parsed.dt.tz_localize(None), freq="M")


def month_end_flags(timestamps: pd.Series) -> pd.Series:
    """True on the last daily bar of each calendar month.

    The last bar of the frame is always a month end: with no following bar
    there is no month boundary below it.
    """
    if timestamps is None or len(timestamps) == 0:
        raise ValueError(
            "Faber timing needs a 'timestamp' column to identify month-end bars"
        )
    months = pd.Series(_month_keys(timestamps).astype(str), index=timestamps.index)
    return months.ne(months.shift(-1))


def monthly_timing_signal(close: pd.Series, timestamps: pd.Series, sma_months: int):
    """Return ``(month_end_mask, month_end_close, month_end_sma, want_long)``.

    ``want_long`` is only defined on month-end bars: it is True when the
    month-end close is above the simple moving average of the last
    ``sma_months`` month-end closes (the SMA includes the current month,
    exactly as in the source's "monthly price > 10-month SMA").
    """
    is_month_end = month_end_flags(timestamps).to_numpy()
    month_end_close = close.loc[is_month_end]
    month_end_sma = month_end_close.rolling(int(sma_months), min_periods=int(sma_months)).mean()
    want_long = pd.Series(
        (month_end_close > month_end_sma).astype(bool) & month_end_sma.notna().to_numpy(),
        index=month_end_close.index,
        dtype=bool,
    )
    return pd.Series(is_month_end, index=close.index), month_end_close, month_end_sma, want_long


def generate_signals(df: pd.DataFrame, params: dict):
    sma_months = int(params.get("sma_months", 10))

    close = df["close"].astype(float)
    timestamps = df["timestamp"] if "timestamp" in df.columns else None
    is_month_end, month_end_close, month_end_sma, want_long = monthly_timing_signal(
        close, timestamps, sma_months
    )

    # Spread the monthly values back onto the daily bar index (NaN on
    # non-month-end bars) so the indicator payload is chartable and the
    # state machine reads one aligned frame.
    sma_at_bar = pd.Series(np.nan, index=close.index, dtype=float)
    sma_at_bar.loc[month_end_sma.index] = month_end_sma.to_numpy()
    signal_at_bar = pd.Series(False, index=close.index, dtype=bool)
    signal_at_bar.loc[want_long.index] = want_long.to_numpy()

    entries = pd.Series(False, index=close.index, dtype=bool)
    exits = pd.Series(False, index=close.index, dtype=bool)
    exit_reason = pd.Series(index=close.index, dtype="object")
    sma_at_exit = pd.Series(np.nan, index=close.index, dtype=float)
    close_at_exit = pd.Series(np.nan, index=close.index, dtype=float)

    # The decision taken on a month-end bar applies from the bar AFTER it
    # (entry fill = next bar open, exit fill = this bar close), so the
    # position changes only at month boundaries.
    in_position = False
    previous_position = False
    for i in range(len(df)):
        if bool(is_month_end.iloc[i]) and not pd.isna(sma_at_bar.iloc[i]):
            in_position = bool(signal_at_bar.iloc[i])
            if in_position and not previous_position:
                # Entry fills on the NEXT bar's open (platform contract).
                entries.iloc[i] = True
            elif not in_position and previous_position:
                # Signal exit: fills at the NEXT bar's open by default
                # (signal_exit_fill="next_open"); the paper's same-bar close
                # fill needs signal_exit_fill="close".
                exits.iloc[i] = True
                exit_reason.iloc[i] = "sma_timing_below"
                sma_at_exit.iloc[i] = float(sma_at_bar.iloc[i])
                close_at_exit.iloc[i] = float(close.iloc[i])
        previous_position = in_position

    indicators_dict = {
        "SMA_10M": sma_at_bar,
        "SMA_10M_at_exit": sma_at_exit,
        "Month_End": is_month_end.astype(bool),
        "Monthly_Close": pd.Series(
            np.where(is_month_end.to_numpy(), close.to_numpy(), np.nan), index=close.index
        ).astype(float),
        "Timing_Long": signal_at_bar,
        "close_at_exit": close_at_exit,
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
                    "text": "Buy (SMA10M)",
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
                    "text": "Sell to cash",
                }
            )

    return entries, exits, indicators_dict, drawings
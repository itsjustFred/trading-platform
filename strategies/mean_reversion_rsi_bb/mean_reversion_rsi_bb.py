"""
Overnight Gap Mean Reversion (daily-expressible proxy).

Faithful daily port of the QuantConnect strategy
``overnight_gap_mean_reverse`` (alexisbouley2/quantconnect):

- Entry on the overnight gap: ``gap = open[t] - close[t-1]`` filtered by a
  sigma gate against the rolling 60-close standard deviation
  (``abs(gap) >= sigma * std(close[-60:])``), fading the gap
  (gap up -> SHORT, gap down -> LONG).
- Exit at the next open (one-bar hold).

DAILY-PROXY LIMITATION (intraday logic not expressible on 1d bars):
the source enters at 09:31 ET and liquidates at 09:45 ET on MINUTE bars.
On daily bars the entry signal fires on the gap day and the exit signal on
the next day; the platform's next-open execution convention additionally
shifts fills by one bar. P&L therefore corresponds to a 1-day-delayed fade,
not to the 14-minute 09:31 -> 09:45 window. See README and STRATEGY_META
notes. There is no stop-loss in the source; the emitted
``sizing_sl_price`` (entry-bar low for LONG / high for SHORT) is a
sizing-only structural protective level used ONLY for runner risk sizing —
never armed as a live exit (A-M3).
"""

import numpy as np
import pandas as pd


STRATEGY_META = {
    "name": "Overnight Gap Mean Reversion (Daily Proxy)",
    "version": "2.0.0",
    "source_url": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/overnight_gap_mean_reverse",
    "notes": (
        "Daily-expressible proxy of overnight_gap_mean_reverse "
        "(algorithm.py: sigma=10.0, volatility_window=60, entry 09:31 ET, "
        "liquidate 09:45 ET; variants/variant_1.py: same gap/std filter with "
        "sigma default 3.0). Entry fades the overnight gap "
        "(open vs previous close) when abs(gap) >= sigma * 60-close std; "
        "exit is the next open (one-bar hold). LIMITATION: the 09:31->09:45 "
        "intraday window cannot be expressed on 1d bars, and next-open "
        "execution shifts fills by one bar, so backtest P&L is a 1-day-delayed "
        "fade proxy. sizing_sl_price (entry-bar low/high) is a sizing-only "
        "protective level, never a live exit (A-M3). Replaces the previous generic "
        "BB/RSI implementation that was mapped to this source by mistake."
    ),
    "parameters": {
        "sigma": 10.0,
        "volatility_window": 60,
    },
    "tags": {
        "type": ["mean-reversion"],
        "source": "alexisbouley2-github",
        "link": "https://github.com/alexisbouley2/quantconnect/tree/main/strategies/overnight_gap_mean_reverse",
        "asset_class": ["equities"],
        "complexity": "medium",
    },
}


def generate_signals(df: pd.DataFrame, params: dict):
    sigma = float(params.get("sigma", 10.0))
    volatility_window = int(params.get("volatility_window", 60))

    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    # Overnight gap per source: today's open minus yesterday's close.
    prev_close = close.shift(1)
    gap = open_ - prev_close
    # Volatility per source: rolling std of closes over the window, read off
    # the history available BEFORE the gap day (shifted by one bar so the
    # gap day itself cannot leak into its own gate).
    volatility = close.shift(1).rolling(volatility_window).std()
    with np.errstate(divide="ignore", invalid="ignore"):
        deviations = gap / volatility

    valid = volatility.notna() & (volatility > 0) & gap.notna()
    raw_signal = valid & (deviations.abs() >= sigma)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sizing_sl_price = pd.Series(np.nan, index=df.index)
    gap_at_entry = pd.Series(np.nan, index=df.index)
    volatility_at_entry = pd.Series(np.nan, index=df.index)
    deviations_at_entry = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    for i in range(len(df)):
        if in_trade:
            # Daily proxy for the 09:45 liquidate: flat after one bar.
            exits.iloc[i] = True
            exit_reason.iloc[i] = "time_exit_next_open"
            in_trade = False
            continue
        if bool(raw_signal.iloc[i]):
            in_trade = True
            entries.iloc[i] = True
            is_gap_up = bool(gap.iloc[i] > 0)
            # Fade the gap: gap up -> SHORT, gap down -> LONG.
            direction.iloc[i] = "SHORT" if is_gap_up else "LONG"
            # A-M3: sizing-only structural stop (NOT a source rule and NOT a
            # live exit level): the entry bar's extreme against the fade
            # direction. Published as ``sizing_sl_price`` so engines size
            # from it without arming a live intrabar stop (a ``sl_price``
            # key would be picked up by exit detection whenever
            # modeling_resolution enables the intrabar walk).
            sizing_sl_price.iloc[i] = float(high.iloc[i]) if is_gap_up else float(low.iloc[i])
            gap_at_entry.iloc[i] = float(gap.iloc[i])
            volatility_at_entry.iloc[i] = float(volatility.iloc[i])
            deviations_at_entry.iloc[i] = float(deviations.iloc[i])

    indicators_dict = {
        "gap": gap,
        "gap_volatility": volatility,
        "gap_deviations": deviations,
        "direction": direction,
        "sizing_sl_price": sizing_sl_price,
        "gap_at_entry": gap_at_entry,
        "volatility_at_entry": volatility_at_entry,
        "deviations_at_entry": deviations_at_entry,
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
        for ts in entry_times.iloc[:200]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#22c55e",
                    "text": "Gap fade",
                }
            )
        for ts in exit_times.iloc[:200]:
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

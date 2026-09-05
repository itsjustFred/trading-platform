"""
Strategy: Post VIX Dip Buy

Buys SPY on panic dips: when VIX spikes significantly and SPY drops,
enter a mean-reversion long trade expecting a bounce.

VIX data dependency (Cleanup #35): the VIX close series is read from
``<DATA_DIR>/ohlcv/**/VIX/1d.parquet``. When the dataset is missing the
regime gate is FAIL-CLOSED: no entries are produced (the strategy does not
degrade to a VIX-less mean-reversion system). ``_load_vix_data`` honours
its ``start``/``end`` arguments so callers can request a bounded window.
"""

STRATEGY_META = {
    "name": "Post VIX Dip Buy",
    "version": "1.0.0",
    "source_url": "https://discord.com/channels/zenomtrader",
    "tags": {
        "type": ["mean-reversion", "regime-filter"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/zenomtrader",
        "asset_class": ["equities", "etf"],
        "complexity": "medium",
    },
    "data_requirements": {
        "vix": {
            "symbol": "VIX",
            "timeframe": "1d",
            "missing_data_behavior": "fail_closed",
        }
    },
}

import os
from pathlib import Path

import numpy as np
import pandas as pd


def _load_vix_data(start: str = None, end: str = None) -> pd.Series | None:
    from shared.constants import DATA_DIR

    ohlcv_root = DATA_DIR / "ohlcv"
    matches = list(ohlcv_root.rglob("**/VIX/1d.parquet"))
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")

    close = df["close"]
    if start is not None:
        start_ts = pd.Timestamp(start, tz="UTC")
        close = close[close.index >= start_ts]
    if end is not None:
        end_ts = pd.Timestamp(end, tz="UTC")
        close = close[close.index <= end_ts]
    return close


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def generate_signals(df: pd.DataFrame, params: dict):
    vix_threshold = params.get("vix_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_entry = params.get("rsi_entry", 35)
    rsi_exit = params.get("rsi_exit", 60)
    sma_period = params.get("sma_period", 20)
    max_hold = params.get("max_hold", 20)
    atr_period = params.get("atr_period", 14)
    atr_stop_mult = params.get("atr_stop_mult", 2.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    rsi = _calc_rsi(close, rsi_period)
    sma = close.rolling(sma_period).mean()
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    # VIX range is bounded by the strategy frame so the loader honours
    # start/end instead of always reading the whole dataset.
    # TZ guard (Pre-mortem strategies #5, PR #61): bar dates are matched
    # against UTC-normalized VIX daily closes. A tz-naive bar source would be
    # silently re-interpreted as UTC and shift every bar date by the local
    # offset, gating entries on the wrong day's VIX value — fail loudly
    # instead.
    if "timestamp" in df.columns:
        bar_ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        raw_ts = pd.to_datetime(df["timestamp"], errors="coerce")
        if raw_ts.notna().any() and (raw_ts.dt.tz is None):
            raise ValueError(
                "post_vix_dip_buy requires tz-aware bar timestamps "
                "(naive timestamps would be mis-dated against the UTC VIX series)"
            )
        frame_start = bar_ts.min()
        frame_end = bar_ts.max()
    elif isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        index_utc = df.index.tz_convert("UTC")
        frame_start = index_utc.min()
        frame_end = index_utc.max()
    else:
        frame_start = frame_end = None
    vix = _load_vix_data(
        start=str(frame_start.date()) if frame_start is not None else None,
        end=str(frame_end.date()) if frame_end is not None else None,
    )

    vix_data_available = pd.Series(vix is not None, index=df.index)
    vix_fallback_used = pd.Series(vix is None, index=df.index)

    # O(N) alignment: map each strategy bar to its date once, then join the
    # (date -> VIX close) mapping. The old per-bar full-index scan was
    # O(N x M) and effectively unrunnable on intraday frames.
    vix_aligned = pd.Series(np.nan, index=df.index, dtype=float)
    if vix is not None:
        vix_by_date = pd.Series(
            vix.to_numpy(), index=pd.to_datetime(vix.index, utc=True).date
        )
        vix_by_date = vix_by_date[~vix_by_date.index.duplicated(keep="last")]
        if "timestamp" in df.columns:
            bar_dates = bar_ts.dt.date
        elif isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            bar_dates = pd.Index(df.index.tz_convert("UTC").date)
        else:
            bar_dates = pd.Index([pd.NaT] * len(df))
        vix_aligned = pd.Series(
            vix_by_date.reindex(bar_dates).to_numpy(), index=df.index, dtype=float
        )

    # Fail-closed regime gate: without VIX data the gate is closed. The old
    # behaviour fabricated vix_threshold + 1 for every bar, silently turning
    # the VIX-dip system into plain RSI+SMA mean reversion.
    entry_cond = (
        vix_data_available
        & (vix_aligned > vix_threshold)
        & (rsi < rsi_entry)
        & (close < sma)
    )

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    bars_held_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    in_trade = False
    entry_bar = 0
    active_stop = np.nan

    for i in range(sma_period, len(df)):
        if not in_trade and bool(entry_cond.iloc[i]):
            entries.iloc[i] = True
            in_trade = True
            entry_bar = i
            atr_value = atr.iloc[i]
            if pd.notna(atr_value):
                active_stop = float(close.iloc[i]) - float(atr_value) * atr_stop_mult
                sl_price.iloc[i] = active_stop
        elif in_trade:
            bars_held = i - entry_bar
            stop_hit = pd.notna(active_stop) and float(low.iloc[i]) <= float(active_stop)
            rsi_high = rsi.iloc[i] > rsi_exit if pd.notna(rsi.iloc[i]) else False
            above_sma = close.iloc[i] > sma.iloc[i] if pd.notna(sma.iloc[i]) else False
            time_exit_hit = bars_held >= max_hold
            if stop_hit or rsi_high or above_sma or time_exit_hit:
                exits.iloc[i] = True
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                if stop_hit:
                    exit_price.iloc[i] = active_stop
                    exit_reason.iloc[i] = "stop_loss"
                elif rsi_high:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "rsi_exit"
                elif above_sma:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "mean_reversion_to_sma"
                else:
                    exit_price.iloc[i] = float(close.iloc[i])
                    exit_reason.iloc[i] = "time_exit"
                active_stop = np.nan

    indicators_dict = {
        "VIX": vix_aligned,
        "VIX_at_exit": vix_aligned,
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "SMA": sma,
        "SMA_at_exit": sma,
        "ATR": atr,
        "sl_price": sl_price,
        "VIX_Data_Available": vix_data_available,
        "VIX_Fallback_Used": vix_fallback_used,
        "VIX_Uncovered_Bars": int(vix_aligned.isna().sum()),
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

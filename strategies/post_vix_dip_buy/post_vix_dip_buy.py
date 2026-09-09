"""
Strategy: Post VIX Dip Buy (Post-VIX Panic Intraday Long v4 [Dip + ORB Hybrid])

Source-faithful port of "post vix dip buy pine.txt" (Pine v5,
Discord ID 1458626059988762696).

Source semantics:
- Daily regime: VIX chg% >= 10 AND VIX close >= 19 AND SPX chg% <= -1%
  (all on the PRIOR daily bar), active window 1-5 days after the panic bar,
  plus bull filter daily close > SMA200 (default ON).
- Dip leg (default): NY 9:30-10:59, close < VWAP, Wilder RSI14 < 40,
  low within supportBuffer% (1.5%) above the prior (panic) day low
  (useNearSupport default ON, requireBullCandle default OFF).
- ORB leg (default ON): 9:30-10:00 range (9:30-9:59 bars), breakout cross
  above OR-high during NY 10:00-14:00.
- Exits: TP +2% / SL -0.3% (stop-first on same-bar ambiguity), 50% scale-out
  from 14:00 NY, EOD flatten from 15:50 NY. Long-only, one position at a time.

VIX data dependency: the VIX daily close series is read from
``<DATA_DIR>/ohlcv/**/VIX/1d.parquet``. When the dataset is missing the
regime gate is FAIL-CLOSED: no entries are produced. ``_load_vix_data``
honours its ``start``/``end`` arguments so callers can request a bounded
window. SPX daily values (close/prev/SMA200/prior low) are derived from the
strategy frame itself (the source runs on the SPX/SPY chart, so the frame
symbol proxies SPX) by UTC-date resampling.

vectorbt notes:
- Next-open execution: signals fire on bar close, fills on next open.
- The 50% power-hour scale has no partial-fill representation in vectorbt;
  it is modeled as a FULL exit with reason ``scale_50pct_power_hour``
  (documented approximation).
- Daily regime dates use UTC calendar dates (matching the VIX loader's UTC
  mapping; RTH bars map identically in NY). Intraday session windows use
  America/New_York wall time.
- The invented RSI35/SMA20/ATR-stop/time-exit logic of the previous port is
  removed.
"""

STRATEGY_META = {
    "name": "Post VIX Dip Buy",
    "version": "1.1.0",
    "source_url": "https://discord.com/channels/1230977283888648315/1458625986039119982/1458626059988762696",
    "notes": (
        "- Source: post vix dip buy pine.txt (VIX chg%>=10 & VIX>=19 & SPX chg%<=-1%, "
        "1-5 day window, SMA200 bull filter; dip 9:30-10:59 close<VWAP RSI14<40 Wilder "
        "near-prior-low; ORB 9:30-10:00 range / 10:00-14:00 breakout; "
        "TP+2%/SL-0.3%, 50% scale >=14:00 NY, EOD flatten >=15:50 NY).\n"
        "- VIX missing -> FAIL-CLOSED (no entries), as before.\n"
        "- 50% power-hour scale modeled as full exit (no partials in vectorbt).\n"
        "- RSI is Wilder (RMA) per ta.rsi; VWAP is daily-reset hlc3 (volume-weighted "
        "when volume exists, else cumulative mean).\n"
        "- Removed invented RSI35/SMA20/ATR-stop/max-hold logic."
    ),
    "tags": {
        "type": ["mean-reversion", "regime-filter"],
        "source": "zenomtrader-discord",
        "link": "https://discord.com/channels/1230977283888648315/1458625986039119982/1458626059988762696",
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


def _wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI (RMA smoothing) matching Pine ta.rsi."""
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    all_zero = (avg_gain == 0) & (avg_loss == 0)
    rsi = rsi.mask(all_zero.fillna(False), 50.0)
    rsi = rsi.mask(((avg_loss == 0) & (avg_gain > 0)).fillna(False), 100.0)
    return rsi


def generate_signals(df: pd.DataFrame, params: dict = None):
    params = params or {}
    vix_spike_min = float(params.get("vix_spike_min", 10.0))
    vix_level_min = float(params.get("vix_level_min", 19.0))
    spx_drop_min = float(params.get("spx_drop_min", 1.0))
    active_days_max = int(params.get("active_days_max", 5))
    rsi_len = int(params.get("rsi_len", params.get("rsi_period", 14)))
    rsi_oversold = float(params.get("rsi_oversold", 40.0))
    profit_pct = float(params.get("profit_pct", 2.0))
    stop_pct = float(params.get("stop_pct", 0.3))
    use_htf_filter = params.get("use_htf_filter", True)
    use_near_support = params.get("use_near_support", True)
    support_buffer = float(params.get("support_buffer", 1.5))
    require_bull_candle = params.get("require_bull_candle", False)
    use_orb_mode = params.get("use_orb_mode", True)

    n = len(df)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float) if "open" in df.columns else close
    volume = df["volume"].astype(float) if "volume" in df.columns else None

    # TZ guard: bar dates are matched against the UTC VIX daily closes.
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
        bar_ts = pd.Series(index_utc, index=df.index)
        frame_start = index_utc.min()
        frame_end = index_utc.max()
    else:
        bar_ts = pd.Series(pd.NaT, index=df.index)
        frame_start = frame_end = None

    ny = pd.to_datetime(bar_ts, utc=True, errors="coerce").dt.tz_convert(
        "America/New_York"
    )
    ny_hour = ny.dt.hour.fillna(-1).astype(int)
    ny_minute = ny.dt.minute.fillna(-1).astype(int)

    # Daily frame (UTC dates, matching VIX mapping) from the strategy symbol.
    utc_dates = pd.to_datetime(bar_ts, utc=True, errors="coerce").dt.date
    utc_arr = np.asarray(utc_dates)
    daily_close_by_date: dict = {}
    daily_low_by_date: dict = {}
    date_order: list = []
    for i in range(n):
        d = utc_arr[i]
        if pd.isna(d):
            continue
        if d not in daily_close_by_date:
            date_order.append(d)
        daily_close_by_date[d] = float(close.iloc[i])  # last close wins
        li = float(low.iloc[i])
        daily_low_by_date[d] = li if d not in daily_low_by_date else min(
            daily_low_by_date[d], li
        )
    daily_close_s = pd.Series(
        [daily_close_by_date[d] for d in date_order], index=pd.Index(date_order)
    )
    daily_prev_s = daily_close_s.shift(1)
    daily_sma200_s = daily_close_s.rolling(200, min_periods=200).mean()
    daily_low_prev_s = pd.Series(
        [daily_low_by_date[d] for d in date_order], index=pd.Index(date_order)
    ).shift(1)

    # VIX daily mapped by UTC date (same convention as the previous port).
    vix = _load_vix_data(
        start=str(frame_start.date()) if frame_start is not None else None,
        end=str(frame_end.date()) if frame_end is not None else None,
    )
    vix_data_available = pd.Series(vix is not None, index=df.index)
    vix_fallback_used = pd.Series(vix is None, index=df.index)
    vix_aligned = pd.Series(np.nan, index=df.index, dtype=float)
    vix_by_date = None
    if vix is not None:
        vix_by_date = pd.Series(
            vix.to_numpy(), index=pd.to_datetime(vix.index, utc=True).date
        )
        vix_by_date = vix_by_date[~vix_by_date.index.duplicated(keep="last")]
        vix_aligned = pd.Series(
            vix_by_date.reindex(utc_dates).to_numpy(), index=df.index, dtype=float
        )

    # Per-date panic math. panic_s[d] is True when day d itself is a panic bar;
    # a bar on date d is in-regime when a panic bar occurred 1..max days earlier
    # (Pine: panicYesterday uses [1] offsets, daysSince counts 1..5).
    if vix_by_date is not None:
        vix_close_s = vix_by_date.reindex(daily_close_s.index)
        vix_prev_s = vix_close_s.shift(1)
        with np.errstate(divide="ignore", invalid="ignore"):
            vix_chg_s = (vix_close_s - vix_prev_s) / vix_prev_s * 100
            spx_chg_s = (daily_close_s - daily_prev_s) / daily_prev_s * 100
        panic_s = (
            (vix_chg_s >= vix_spike_min)
            & (vix_close_s >= vix_level_min)
            & (spx_chg_s <= -abs(spx_drop_min))
        ).fillna(False)
    else:
        panic_s = pd.Series(False, index=daily_close_s.index)

    days_since_by_date: dict = {}
    cur = None
    prev = None
    for d in date_order:
        panic_yesterday = bool(panic_s.get(prev, False)) if prev is not None else False
        if panic_yesterday:
            cur = 1
        elif cur is not None and cur < active_days_max:
            cur = cur + 1
        else:
            cur = None
        days_since_by_date[d] = cur
        prev = d

    # Broadcast per-bar regime.
    in_regime = pd.Series(False, index=df.index)
    bull_market = pd.Series(True, index=df.index)
    days_since_bar = pd.Series(np.nan, index=df.index, dtype=float)
    panic_low_bar = pd.Series(np.nan, index=df.index, dtype=float)
    sma200_bar = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(n):
        d = utc_arr[i]
        if pd.isna(d):
            continue
        ds = days_since_by_date.get(d)
        if ds is not None:
            in_regime.iloc[i] = True
            days_since_bar.iloc[i] = float(ds)
        if use_htf_filter:
            sma = daily_sma200_s.get(d, np.nan)
            dc = daily_close_by_date.get(d, np.nan)
            sma200_bar.iloc[i] = float(sma) if pd.notna(sma) else np.nan
            bull_market.iloc[i] = bool(
                pd.notna(sma) and pd.notna(dc) and dc > sma
            )
        pl = daily_low_prev_s.get(d, np.nan)
        if pd.notna(pl):
            panic_low_bar.iloc[i] = float(pl)
    active = in_regime & bull_market & vix_data_available

    # Intraday indicators.
    rsi = _wilder_rsi(close, rsi_len)
    hlc3 = (high + low + close) / 3.0
    vwap = pd.Series(np.nan, index=df.index, dtype=float)
    if volume is not None:
        vol = volume.fillna(0.0)
        for d in date_order:
            idx = np.where(utc_arr == d)[0]
            if len(idx) == 0:
                continue
            pv = (hlc3.iloc[idx].to_numpy() * vol.iloc[idx].to_numpy()).cumsum()
            vv = vol.iloc[idx].to_numpy().cumsum()
            with np.errstate(divide="ignore", invalid="ignore"):
                v = np.where(vv > 0, pv / vv, np.nan)
            nan_mask = np.isnan(v)
            if nan_mask.any():
                cm = hlc3.iloc[idx].expanding().mean().to_numpy()
                v[nan_mask] = cm[nan_mask]
            vwap.iloc[idx] = v
    else:
        for d in date_order:
            idx = np.where(utc_arr == d)[0]
            if len(idx) == 0:
                continue
            vwap.iloc[idx] = hlc3.iloc[idx].expanding().mean().to_numpy()

    near_support = (low <= panic_low_bar * (1 + support_buffer / 100)).fillna(False)

    in_early = ((ny_hour == 9) & (ny_minute >= 30)) | (ny_hour == 10)
    in_orb_session = (ny_hour >= 10) & (ny_hour < 14)
    power_hour = ny_hour >= 14
    eod = (ny_hour > 15) | ((ny_hour == 15) & (ny_minute >= 50))
    orb_form_mask = ((ny_hour == 9) & (ny_minute >= 30)).to_numpy()

    # OR range per UTC date from 9:30-9:59 NY bars.
    or_high_by_date: dict = {}
    for d in date_order:
        idx = np.where((utc_arr == d) & orb_form_mask)[0]
        if len(idx):
            or_high_by_date[d] = float(high.iloc[idx].max())
    or_high_bar = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(n):
        d = utc_arr[i]
        if pd.isna(d):
            continue
        if d in or_high_by_date:
            or_high_bar.iloc[i] = or_high_by_date[d]

    bull_candle = close > open_

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    sl_price = pd.Series(np.nan, index=df.index)
    tp_price = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")
    bars_held_at_exit = pd.Series(np.nan, index=df.index)

    in_trade = False
    entry_idx = -1
    take_profit = np.nan
    stop_loss = np.nan

    for i in range(n):
        if in_trade:
            bars_held = i - entry_idx
            sl_hit = pd.notna(stop_loss) and float(low.iloc[i]) <= float(stop_loss)
            tp_hit = pd.notna(take_profit) and float(high.iloc[i]) >= float(take_profit)
            # Stop-first on same-bar ambiguity (conservative).
            if sl_hit:
                exits.iloc[i] = True
                exit_price.iloc[i] = float(stop_loss)
                exit_reason.iloc[i] = "stop_loss"
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                continue
            if tp_hit:
                exits.iloc[i] = True
                exit_price.iloc[i] = float(take_profit)
                exit_reason.iloc[i] = "take_profit"
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                continue
            if bool(power_hour.iloc[i]):
                exits.iloc[i] = True
                exit_price.iloc[i] = float(close.iloc[i])
                exit_reason.iloc[i] = "scale_50pct_power_hour"
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                continue
            if bool(eod.iloc[i]):
                exits.iloc[i] = True
                exit_price.iloc[i] = float(close.iloc[i])
                exit_reason.iloc[i] = "eod_flatten"
                bars_held_at_exit.iloc[i] = bars_held
                in_trade = False
                continue
            continue

        # Flat: evaluate entries.
        if not bool(active.iloc[i]):
            continue
        rsi_v = rsi.iloc[i]
        if pd.isna(rsi_v):
            continue
        dip = (
            bool(in_early.iloc[i])
            and pd.notna(vwap.iloc[i])
            and float(close.iloc[i]) < float(vwap.iloc[i])
            and float(rsi_v) < rsi_oversold
        )
        if dip:
            if require_bull_candle and not bool(bull_candle.iloc[i]):
                dip = False
            if use_near_support and not bool(near_support.iloc[i]):
                dip = False
        orb = False
        if use_orb_mode and bool(in_orb_session.iloc[i]) and i > 0:
            oh = or_high_bar.iloc[i]
            oh_prev = or_high_bar.iloc[i - 1]
            if pd.notna(oh) and pd.notna(oh_prev):
                orb = float(close.iloc[i]) > float(oh) and float(
                    close.iloc[i - 1]
                ) <= float(oh_prev)
        if dip or orb:
            entries.iloc[i] = True
            in_trade = True
            entry_idx = i
            entry_price = float(close.iloc[i])
            take_profit = entry_price * (1 + profit_pct / 100)
            stop_loss = entry_price * (1 - stop_pct / 100)
            sl_price.iloc[i] = float(stop_loss)
            tp_price.iloc[i] = float(take_profit)

    indicators_dict = {
        "VIX": vix_aligned,
        "VIX_at_exit": vix_aligned,
        "RSI": rsi,
        "RSI_at_exit": rsi,
        "VWAP": vwap,
        "SMA200": sma200_bar,
        "panic_low": panic_low_bar,
        "OR_high": or_high_bar,
        "days_since_panic": days_since_bar,
        "sl_price": sl_price,
        "tp_price": tp_price,
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

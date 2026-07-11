"""
QG-30Min ES Breakout Strategy (Pine v6 conversion).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


STRATEGY_META = {
    "name": "QG-30Min ES Breakout Strategy",
    "version": "1.0.0",
    "source_url": "https://discord.com/channels/1230977283888648315/1387506825603059762/1387506825603059762",
    "notes": (
        "- Original nutzt Stop-Orders (`strategy.entry(..., stop=...)`).\n"
        "- Der vectorbt-Adapter arbeitet signalbasiert mit Next-Open-Ausfuehrung; "
        "die Konvertierung bildet daher die Signal-Logik nach, aber keine intrabar Stop-Order-Fills.\n"
        "- Original hat keinen expliziten Stop-Loss. Fuer robustes Risk-Sizing und begrenzte Tail-Risiken "
        "nutzt die Konvertierung einen technischen Schutz-Stop aus dem Entry-Bar "
        "(Long=low, Short=high) und schliesst bei Stop-Hit."
    ),
    "parameters": {
        "fract": 3.3,
        "adx_period": 45,
        "adx_smooth": 5,
        "market_open_hour_ny": 8,
        "market_open_minute_ny": 0,
        "market_close_hour_ny": 15,
        "market_close_minute_ny": 0,
        "forced_exit_hour_ny": 14,
        "forced_exit_minute_ny": 30,
    },
    "tags": {
        "type": ["breakout", "intraday"],
        "source": "tradingview-pine-discord",
        "link": "https://discord.com/channels/1230977283888648315/1387506825603059762/1387506825603059762",
        "asset_class": ["futures", "indices"],
        "complexity": "medium",
    },
}


def _wilder_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / float(length)
    return series.ewm(alpha=alpha, adjust=False).mean()


def _calc_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int,
    smooth: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    tr_smooth = _wilder_rma(tr.fillna(0.0), length)
    plus_dm_smooth = _wilder_rma(plus_dm.fillna(0.0), length)
    minus_dm_smooth = _wilder_rma(minus_dm.fillna(0.0), length)

    plus_di = 100.0 * (plus_dm_smooth / tr_smooth.replace(0.0, np.nan))
    minus_di = 100.0 * (minus_dm_smooth / tr_smooth.replace(0.0, np.nan))
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = _wilder_rma(dx.fillna(0.0), smooth)

    # Keep ADX/DI undefined through the warmup period so entry checks never
    # act on premature (near-zero) values. The first `length` bars seed the
    # Wilder RMA and the next `smooth` bars let it converge.
    warmup = length + smooth
    if len(adx) > warmup:
        adx.iloc[:warmup] = np.nan
        plus_di.iloc[:warmup] = np.nan
        minus_di.iloc[:warmup] = np.nan
    return adx, plus_di, minus_di


def generate_signals(df: pd.DataFrame, params: dict):
    fract = float(params.get("fract", 3.3))
    adx_period = int(params.get("adx_period", 45))
    adx_smooth = int(params.get("adx_smooth", 5))

    market_open_hour = int(params.get("market_open_hour_ny", 8))
    market_open_minute = int(params.get("market_open_minute_ny", 0))
    market_close_hour = int(params.get("market_close_hour_ny", 15))
    market_close_minute = int(params.get("market_close_minute_ny", 0))
    forced_exit_hour = int(params.get("forced_exit_hour_ny", 14))
    forced_exit_minute = int(params.get("forced_exit_minute_ny", 30))

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)

    if "timestamp" in df.columns:
        timestamps_utc = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    else:
        timestamps_utc = pd.to_datetime(df.index, utc=True, errors="coerce")
    timestamps_ny = timestamps_utc.dt.tz_convert("America/New_York")

    adx, plus_di, minus_di = _calc_adx(high, low, close, adx_period, adx_smooth)

    breakout_long = low + fract * (open_ - low).abs()
    breakout_short = high - fract * (high - open_).abs()

    ny_minutes = timestamps_ny.dt.hour * 60 + timestamps_ny.dt.minute
    open_minutes = market_open_hour * 60 + market_open_minute
    close_minutes = market_close_hour * 60 + market_close_minute
    forced_exit_minutes = forced_exit_hour * 60 + forced_exit_minute

    in_time = (ny_minutes >= open_minutes) & (ny_minutes < forced_exit_minutes)
    exit_now = ny_minutes >= forced_exit_minutes

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    direction = pd.Series(index=df.index, dtype="object")
    sl_price = pd.Series(np.nan, index=df.index)
    sl_price_at_exit = pd.Series(np.nan, index=df.index)
    exit_price = pd.Series(np.nan, index=df.index)
    exit_reason = pd.Series(index=df.index, dtype="object")

    in_trade = False
    active_direction: str | None = None
    active_sl = np.nan
    current_day = None
    trades_today = 0

    for i in range(len(df)):
        day = timestamps_ny.iloc[i].date()
        if current_day != day:
            current_day = day
            trades_today = 0

        if in_trade:
            long_stop_hit = active_direction == "LONG" and bool(low.iloc[i] <= active_sl)
            short_stop_hit = active_direction == "SHORT" and bool(high.iloc[i] >= active_sl)
            if long_stop_hit or short_stop_hit:
                exits.iloc[i] = True
                exit_price.iloc[i] = float(active_sl)
                sl_price_at_exit.iloc[i] = float(active_sl)
                exit_reason.iloc[i] = "stop_loss"
                in_trade = False
                active_direction = None
                active_sl = np.nan
            elif bool(exit_now.iloc[i]):
                exits.iloc[i] = True
                exit_price.iloc[i] = float(close.iloc[i])
                sl_price_at_exit.iloc[i] = float(active_sl)
                exit_reason.iloc[i] = "time_exit"
                in_trade = False
                active_direction = None
                active_sl = np.nan
            continue

        can_trade = bool(in_time.iloc[i]) and trades_today == 0
        if not can_trade:
            continue

        enter_long = bool(adx.iloc[i] < 30.0) and bool(close.iloc[i] < breakout_long.iloc[i])
        enter_short = bool(adx.iloc[i] > 30.0) and bool(close.iloc[i] < breakout_short.iloc[i])

        if enter_long:
            entries.iloc[i] = True
            direction.iloc[i] = "LONG"
            sl_price.iloc[i] = float(low.iloc[i])
            active_sl = float(low.iloc[i])
            active_direction = "LONG"
            in_trade = True
            trades_today += 1
        elif enter_short:
            entries.iloc[i] = True
            direction.iloc[i] = "SHORT"
            sl_price.iloc[i] = float(high.iloc[i])
            active_sl = float(high.iloc[i])
            active_direction = "SHORT"
            in_trade = True
            trades_today += 1

    indicators_dict = {
        "ADX": adx,
        "plusDI": plus_di,
        "minusDI": minus_di,
        "breakout_long": breakout_long,
        "breakout_short": breakout_short,
        "sl_price": sl_price,
        "sl_price_at_exit": ("exit", sl_price_at_exit),
        "direction": direction,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }

    drawings: list[dict] = []
    if "timestamp" in df.columns:
        long_times = timestamps_utc.loc[entries & (direction == "LONG")].dropna()
        short_times = timestamps_utc.loc[entries & (direction == "SHORT")].dropna()
        exit_times = timestamps_utc.loc[exits].dropna()

        for ts in long_times.iloc[:200]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "belowBar",
                    "shape": "triangleUp",
                    "color": "#22c55e",
                    "text": "Long Signal",
                }
            )
        for ts in short_times.iloc[:200]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "triangleDown",
                    "color": "#ef4444",
                    "text": "Short Signal",
                }
            )
        for ts in exit_times.iloc[:200]:
            drawings.append(
                {
                    "type": "marker",
                    "time": ts.isoformat(),
                    "position": "aboveBar",
                    "shape": "arrowDown",
                    "color": "#f59e0b",
                    "text": "Exit",
                }
            )

    return entries, exits, indicators_dict, drawings

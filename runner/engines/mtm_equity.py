"""Mark-to-market (MTM) reporting equity curve for every engine adapter.

Why this exists
---------------
Drawdown, Sharpe, Sortino, volatility and Calmar are all computed from the
persisted equity curve (``runner.stats.compute_stats``). A curve that is only
credited at trade *exit* (the old realized-only replay) is flat while a
position is open, so a 40 % crash inside one long hold reported
``max_drawdown_pct == 0`` and real Faber-style runs under-reported drawdown by
roughly a factor of two. Industry practice (MT5 reports both, QuantConnect and
every portfolio-analytics tool use equity) is to measure risk on the
mark-to-market equity: ``initial_capital + realized net PnL + unrealized PnL``.

Position *sizing* is a separate matter and is deliberately untouched: it keeps
drawing on realized cash (``TradeEngine._sizing_equity``, see
``docs/sizing-semantics.md``). This module only builds the *reporting* curve.

Semantics (one definition for native, vectorbt, pynecore, signal replay and the
shared multi-symbol replay):

* The curve has one observation per bar timestamp of the traded symbols (plus
  every entry / exit leg time), starting at ``start_time`` with the initial
  capital. Bar timestamps follow the engine convention: the observation at
  ``t`` uses the close of the bar stamped ``t``.
* At an observation, an open position contributes its already-realized exit
  legs (gross) plus ``sign * (mark - entry) * remaining_quantity`` where
  ``mark`` is the last close at or before the observation.
* Fees are recognised when the trade fully closes (exactly like
  ``TradeEngine._finalize_position``), so from the final exit onward a trade
  contributes exactly its ``net_pnl`` and the final point equals
  ``initial_capital + sum(net PnL)`` — total return is unchanged by the switch.
* Runs of unchanged equity with no open position are thinned to their end
  points (lossless for the daily resampling every ratio uses) so sub-daily data
  does not persist millions of flat points.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from shared.models import EquityPoint, Trade

logger = logging.getLogger(__name__)

#: ``meta.data`` key describing how the persisted equity curve was built.
EQUITY_CURVE_BASIS_KEY = "equity_curve_basis"
BASIS_MARK_TO_MARKET = "mark_to_market"
#: Curve credited only at trade exit (balance curve). Only for artifacts whose
#: PnL cannot be re-derived from bar closes (e.g. MT5-parity trades priced with
#: tick values and currency conversion); such artifacts must say so.
BASIS_REALIZED_EXIT_TIMED = "realized_exit_timed"
#: Some traded symbols had no bar data to mark against; their open positions
#: were carried at the entry / leg prices (equivalent to the realized-only
#: curve for those positions). Listed in ``meta.data["equity_curve_unmarked_symbols"]``.
BASIS_MARK_TO_MARKET_PARTIAL = "mark_to_market_partial"
UNMARKED_SYMBOLS_KEY = "equity_curve_unmarked_symbols"


def _to_utc(values: Sequence[str] | pd.Index) -> pd.DatetimeIndex:
    items = list(values)
    if not items:
        return pd.DatetimeIndex([], tz="UTC")
    return pd.DatetimeIndex(pd.to_datetime(items, utc=True, format="mixed"))


def closes_from_frame(frame: pd.DataFrame | None) -> pd.Series | None:
    """``timestamp``/``close`` columns of an OHLCV frame as a UTC-indexed series."""
    if frame is None or frame.empty or "close" not in frame.columns:
        return None
    if "timestamp" in frame.columns:
        index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
        values = frame["close"].to_numpy(dtype=float)
    elif isinstance(frame.index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(
            frame.index.tz_localize("UTC")
            if frame.index.tz is None
            else frame.index.tz_convert("UTC")
        )
        values = frame["close"].to_numpy(dtype=float)
    else:
        return None
    return pd.Series(values, index=index, dtype=float)


def _net_pnl(trade: Trade) -> float:
    if trade.net_pnl is not None:
        return float(trade.net_pnl)
    return float(trade.pnl or 0.0) - float(trade.fees or 0.0)


def _lookup_close(closes: Mapping[str, pd.Series], symbol: str) -> pd.Series | None:
    for key in (symbol, str(symbol).upper(), str(symbol).lower()):
        series = closes.get(key)
        if series is not None and not series.empty:
            return series
    return None


def build_mark_to_market_curve(
    trades: Sequence[Trade],
    closes: Mapping[str, pd.Series],
    initial_capital: float,
    *,
    start_time: str,
) -> tuple[list[EquityPoint], list[str]]:
    """Build the MTM reporting curve; returns ``(curve, unmarked_symbols)``.

    ``closes`` maps a symbol to its UTC-indexed close series. A traded symbol
    without a series cannot be marked; its open positions then stay at the
    entry price between legs and the symbol is returned in ``unmarked_symbols``.
    Callers pass bar series already restricted to the backtest window.
    """
    capital = float(initial_capital)
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("initial_capital must be finite and > 0")
    start_ts = pd.Timestamp(pd.to_datetime(start_time, utc=True))

    trade_list = [t for t in trades if t.exits or t.entry_time]
    entry_times = [pd.Timestamp(x) for x in _to_utc([t.entry_time for t in trade_list])]
    leg_times = [
        [pd.Timestamp(x) for x in _to_utc([leg.time for leg in t.exits])] if t.exits else []
        for t in trade_list
    ]

    traded_symbols = {t.symbol for t in trade_list}
    clean: dict[str, pd.Series] = {}
    unmarked: list[str] = []
    for symbol in sorted(traded_symbols):
        series = _lookup_close(closes, symbol)
        if series is None:
            unmarked.append(symbol)
            continue
        series = series[~series.index.duplicated(keep="last")].sort_index()
        clean[symbol] = series

    stamps: set[pd.Timestamp] = set(entry_times)
    for times in leg_times:
        stamps.update(times)
    parts = [pd.DatetimeIndex(sorted(stamps), tz="UTC")]
    parts.extend(series.index for series in clean.values())
    # Every symbol in `closes` contributes bars (not only traded ones): a flat
    # portfolio has a defined, constant equity on every bar of the window.
    for symbol, series in closes.items():
        if symbol not in clean and series is not None and not series.empty:
            parts.append(pd.DatetimeIndex(series.index))
    grid = parts[0]
    for part in parts[1:]:
        grid = grid.union(part)
    grid = grid[grid >= start_ts]
    grid = grid.sort_values().unique()

    start_point = EquityPoint(time=start_time, equity=round(capital, 2))
    if len(grid) == 0:
        return [start_point], unmarked

    pnl = np.zeros(len(grid), dtype=float)
    open_count = np.zeros(len(grid), dtype=int)
    marks: dict[str, np.ndarray] = {}
    for symbol, series in clean.items():
        marks[symbol] = series.reindex(grid, method="ffill").to_numpy(dtype=float)

    for trade, entry_ts, legs_ts in zip(trade_list, entry_times, leg_times):
        sign = 1.0 if str(trade.direction).upper() == "LONG" else -1.0
        entry_price = float(trade.entry_price)
        legs = sorted(zip(legs_ts, trade.exits), key=lambda item: item[0])
        total_qty = sum(float(leg.quantity or 0.0) for _, leg in legs)
        a = entry_idx = int(grid.searchsorted(entry_ts, side="left"))
        if not legs:
            # No exit recorded: nothing is realized; contribute net PnL from entry.
            pnl[a:] += _net_pnl(trade)
            continue
        mark = marks.get(trade.symbol)
        cum_qty = 0.0
        cum_pnl = 0.0
        last = len(legs) - 1
        final_idx = a
        for n, (leg_ts, leg) in enumerate(legs):
            li = max(int(grid.searchsorted(leg_ts, side="left")), a)
            if li > a:
                remaining = total_qty - cum_qty
                if mark is None:
                    unrealized = np.zeros(li - a)
                else:
                    seg = mark[a:li]
                    unrealized = np.where(
                        np.isnan(seg), 0.0, sign * (seg - entry_price) * remaining
                    )
                pnl[a:li] += cum_pnl + unrealized
            leg_qty = float(leg.quantity or 0.0)
            cum_qty += leg_qty
            cum_pnl += sign * (float(leg.price) - entry_price) * leg_qty
            a = li
            if n == last:
                final_idx = li
        open_count[entry_idx:final_idx] += 1
        pnl[final_idx:] += _net_pnl(trade)

    equity = capital + pnl
    keep = np.ones(len(grid), dtype=bool)
    if len(grid) > 2:
        flat = open_count == 0
        same_prev = equity[1:-1] == equity[:-2]
        same_next = equity[1:-1] == equity[2:]
        interior_flat = flat[1:-1] & flat[:-2] & flat[2:]
        keep[1:-1] = ~(same_prev & same_next & interior_flat)

    curve: list[EquityPoint] = [start_point]
    for ts, value in zip(grid[keep], equity[keep]):
        point = EquityPoint(time=pd.Timestamp(ts).isoformat(), equity=round(float(value), 2))
        if pd.Timestamp(ts) == start_ts:
            curve[0] = EquityPoint(time=start_time, equity=point.equity)
        else:
            curve.append(point)
    if unmarked:
        logger.warning(
            "MTM equity curve: no bar data for %s; their open positions are carried at the "
            "entry/leg prices (equity_curve_basis=%s)",
            ", ".join(unmarked),
            BASIS_MARK_TO_MARKET_PARTIAL,
        )
    return curve, unmarked


def describe_basis(unmarked_symbols: Sequence[str]) -> dict[str, object]:
    """``meta.data`` entries documenting how the curve was built."""
    data: dict[str, object] = {
        EQUITY_CURVE_BASIS_KEY: BASIS_MARK_TO_MARKET_PARTIAL
        if unmarked_symbols
        else BASIS_MARK_TO_MARKET
    }
    if unmarked_symbols:
        data[UNMARKED_SYMBOLS_KEY] = sorted(unmarked_symbols)
    return data

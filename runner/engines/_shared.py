"""Shared serialization helpers for engine adapters."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from shared.models import EquityPoint, Trade


def serialize_meta_value(value: Any) -> Any | None:
    """Serialize a single indicator/meta value for JSON-safe storage.

    Returns ``None`` for values that carry no information (None, NaN) so
    callers can drop the key entirely. Numeric floats are rounded to 6
    decimals; numpy scalars are unwrapped via ``item()``.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)

    if isinstance(value, (int, str, bool)):
        return value

    if pd.isna(value):
        return None

    return str(value)


def resolve_max_position_exposure_pct(explicit: float | None, asset_class: str) -> float:
    """Cap on notional as a fraction of equity; asset-class default 20% (100% equities)."""
    if explicit is not None and explicit > 0:
        return float(explicit)
    if asset_class in {"equity", "equities", "etf", "etfs", "stocks"}:
        return 1.0
    return 0.20


def resolve_max_leverage(explicit: float | None) -> float:
    """Leverage ceiling applied to risk-sized notional; default 2.0x."""
    if explicit is not None and explicit > 0:
        return float(explicit)
    return 2.0


def resolve_bar_spread(row: pd.Series, raw_col: Any) -> float:
    """Per-bar spread surcharge in price units (shared D3 helper).

    Byte-identical consolidation of ``TradeEngine._bar_spread``,
    ``_vbt_bar_spread`` and ``_pyne_bar_spread``: opt-in via a strategy
    parameter naming a data column with the spread in PRICE units. Default
    off (0.0). The configured name is normalized (strip + lowercase) the
    same way data columns are lowercased on load, so an uppercase config
    cannot silently price 0.0 (A-L6).
    """
    if not raw_col:
        return 0.0
    col = str(raw_col).strip().lower()
    if not col:
        return 0.0
    try:
        raw = row.get(col)
    except (AttributeError, KeyError):
        return 0.0
    if raw is None or pd.isna(raw):
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


#: The four quote columns a two-sided (Dukascopy-style) feed carries. Their
#: canonical ``open/high/low/close`` counterparts are the BID series.
ASK_COLUMNS = ("ask_open", "ask_high", "ask_low", "ask_close")


def _row_value(row: Any, column: str) -> float | None:
    """Finite float for ``column`` on ``row``, or ``None``."""
    try:
        raw = row.get(column)
    except (AttributeError, KeyError, TypeError):
        return None
    if raw is None or pd.isna(raw):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return value


def has_ask_side(row: Any) -> bool:
    """True when ``row`` carries usable ``ask_*`` quote columns (#472).

    A two-sided feed's canonical OHLC is the BID book and the ``ask_*``
    columns are the other side of the quote; their presence is what makes the
    engine fill and trigger on the side a real order would hit. A row without
    them (yfinance, Alpaca, dolthub, ...) has ask == bid.
    """
    return any(_row_value(row, column) is not None for column in ASK_COLUMNS)


def bar_spread_units(row: Any) -> float | None:
    """Per-bar ask-minus-bid spread in price units, or ``None``.

    Canonical OHLC is the bid-side series (``normalize_ohlcv_frame`` fills
    it from ``bid_*`` and the MT5-chart convention treats bid as the
    reference). For each canonical field with an ``ask_*`` counterpart the
    positive difference is a quoted spread observation; the median of the
    available observations is used so one stale column cannot dominate.
    Non-positive differences (ask-side canonical data where ``ask_*``
    merely repeats the canonical series) are ignored — they carry no
    spread information and must not produce a negative adjustment.
    """
    values: dict[str, float] = {}
    try:
        items = row.items() if hasattr(row, "items") else []
    except (AttributeError, TypeError):
        return None
    for key, raw in items:
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            values[str(key).lower()] = float(raw)
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    diffs: list[float] = []
    for field in ("open", "high", "low", "close"):
        ask = values.get(f"ask_{field}")
        bid = values.get(field)
        if ask is None or bid is None:
            continue
        diff = ask - bid
        if diff > 0 and pd.notna(diff):
            diffs.append(diff)
    if not diffs:
        return None
    diffs.sort()
    mid = len(diffs) // 2
    return diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2.0


def resolve_ask_high_low(row: pd.Series, spread: float) -> tuple[float | None, float | None]:
    """Per-bar ask-side high/low in price units (shared D3 helper).

    Byte-identical consolidation of ``TradeEngine._ask_bar`` and
    ``_vbt_ask_bar``: reads explicit ``ask_high``/``ask_low`` data columns
    when present, otherwise falls back to bid high/low plus ``spread``.
    Returns (None, None) when unavailable. Callers supply the spread they
    price with (native ``_bar_spread`` / ``_vbt_bar_spread``), so this helper
    stays free of engine-specific config plumbing.
    """
    ask_high = _row_value(row, "ask_high")
    ask_low = _row_value(row, "ask_low")
    if ask_high is None or ask_low is None:
        try:
            base_high = float(row["high"])
            base_low = float(row["low"])
        except (KeyError, TypeError, ValueError):
            return None, None
        if ask_high is None:
            ask_high = base_high + spread
        if ask_low is None:
            ask_low = base_low + spread
    return ask_high, ask_low


def resolve_ask_price(row: Any, field: str, spread: float) -> float | None:
    """Ask-side open/close of a bar in price units, or ``None``.

    Explicit ``ask_open``/``ask_close`` wins (a two-sided feed states the
    quote exactly). Otherwise the ask is the bid plus the caller's ``spread``
    (``spread_price_col``); when that is zero the bar carries no ask
    information at all — on a two-sided row the quoted spread is derived from
    the row itself so a feed that only ships ``ask_high``/``ask_low`` still
    prices the ask open/close, and on a single-sided row ``None`` means "ask
    is the bid".
    """
    if field not in ("open", "close"):
        raise ValueError(f"field must be 'open' or 'close', got {field!r}")
    explicit = _row_value(row, f"ask_{field}")
    if explicit is not None:
        return explicit
    try:
        base = float(row[field])
    except (KeyError, TypeError, ValueError):
        return None
    if spread > 0:
        return base + spread
    if has_ask_side(row):
        derived = bar_spread_units(row)
        if derived is not None:
            return base + derived
    return None


def _trade_equity_basis(trade: Trade, initial_capital: float) -> float:
    """Equity the trade was sized from (per-unit coefficient denominator)."""
    meta = trade.meta or {}
    risk_math = meta.get("risk_math") if isinstance(meta.get("risk_math"), dict) else {}
    for candidate in (
        meta.get("equity_at_entry"),
        risk_math.get("equity_at_entry"),
    ):
        try:
            basis = float(candidate)
        except (TypeError, ValueError):
            continue
        if basis > 0:
            return basis
    return float(initial_capital)


def _trade_stop_distance(trade: Trade) -> float | None:
    meta = trade.meta or {}
    risk_math = meta.get("risk_math") if isinstance(meta.get("risk_math"), dict) else {}
    for candidate in (risk_math.get("stop_distance"),):
        try:
            distance = float(candidate)
        except (TypeError, ValueError):
            continue
        if distance > 0:
            return distance
    return None


def replay_shared_equity(
    trades: list[Trade],
    initial_capital: float,
    start_time: str | None = None,
) -> tuple[list[Trade], list[EquityPoint], dict[str, Any]]:
    """Replay multi-symbol trades onto one running portfolio equity (D5, A-M4).

    Per-symbol engines size every trade from the FULL starting capital, so a
    3-symbol run risks N x risk_pct with no shared limits. Position size is
    linear in equity in the risk-target and position-cap branches, so each
    trade carries a per-unit coefficient ``quantity / equity_at_entry``.
    Exception (#347): the native free-margin leverage clamp judges margin
    against the engine's own mark-to-market equity and used margin, not the
    shared-equity basis alone, so a leverage-clamped trade's re-scaled
    quantity is an approximation of what a shared-account free-margin check
    would give — see the per-symbol limitation in
    ``docs/sizing-semantics.md``. This replay
    walks all trades chronologically by entry time, re-scales quantities onto
    the running shared equity, re-prices PnL/fees from the scaled quantities
    and sums open exposure across symbols into ``trade.meta["portfolio"]``.

    Scale-invariant derived metrics (r_multiple, pnl_pct) are untouched;
    qty-proportional MAE/MFE scale with the trade; fees scale per-share.
    Trades flagged ``equity_proportional_sizing=False`` (native
    fixed-amount sizing is absolute-$) keep their quantity — exposure is
    still summed and PnL still credited, only the re-scale is skipped.
    Single-symbol runs must NOT call this (identity modulo rounding — the
    adapters skip the replay when ``len(symbols) == 1``).
    """
    ordered = sorted(trades, key=lambda t: (str(t.entry_time), t.id or 0))
    shared = float(initial_capital)
    # open trade id -> (notional, risk_usd) for exposure summation
    open_ledger: dict[int, tuple[float, float]] = {}
    open_notional = 0.0
    open_risk = 0.0
    peak_notional = 0.0
    peak_exposure_pct = 0.0

    # Exit events ordered by exit time release exposure AND credit realized
    # PnL back. Processed lazily: before each entry, release every trade
    # whose last exit time is at or before this entry time. PnL is credited
    # exactly at exit time (not when the entry is processed), so simultaneous
    # entries all size from the same pre-exit equity.
    by_exit = sorted(
        ordered, key=lambda t: (str(t.last_exit_time or t.entry_time), t.id or 0)
    )
    released_idx = 0
    pending_pnl: dict[int, float] = {}

    def _release_through(cutoff: str) -> None:
        nonlocal released_idx, open_notional, open_risk, shared
        while released_idx < len(by_exit):
            candidate = by_exit[released_idx]
            exit_key = str(candidate.last_exit_time or candidate.entry_time)
            if exit_key > cutoff:
                break
            booked = open_ledger.pop(candidate.id, None)
            if booked is not None:
                open_notional -= booked[0]
                open_risk -= booked[1]
            shared += pending_pnl.pop(candidate.id, 0.0)
            released_idx += 1

    for trade in ordered:
        entry_key = str(trade.entry_time)
        _release_through(entry_key)
        basis = _trade_equity_basis(trade, initial_capital)
        orig_qty = float(trade.quantity or 0.0)
        proportional = bool((trade.meta or {}).get("equity_proportional_sizing", True))
        if proportional and basis > 0:
            unit_coeff = orig_qty / basis
            new_qty = unit_coeff * shared
        else:
            new_qty = orig_qty
        scale = (new_qty / orig_qty) if orig_qty > 0 else 0.0

        direction = str(trade.direction or "LONG").upper()
        sign = 1.0 if direction == "LONG" else -1.0
        new_pnl = 0.0
        for leg in trade.exits or []:
            leg_qty = float(leg.quantity or 0.0) * scale
            leg.quantity = round(leg_qty, 4)
            leg_pnl = sign * (float(leg.price or 0.0) - float(trade.entry_price or 0.0)) * leg_qty
            leg.pnl = round(leg_pnl, 2)
            new_pnl += leg_pnl
        trade.quantity = round(new_qty, 4)
        trade.pnl = round(new_pnl, 2)
        if trade.fees is not None:
            trade.fees = round(float(trade.fees) * scale, 2)
        if trade.net_pnl is not None:
            trade.net_pnl = round(new_pnl - float(trade.fees or 0.0), 2)
        if trade.mae is not None:
            trade.mae = round(float(trade.mae) * scale, 2)
        if trade.mfe is not None:
            trade.mfe = round(float(trade.mfe) * scale, 2)

        stop_distance = _trade_stop_distance(trade)
        trade_notional = new_qty * float(trade.entry_price or 0.0)
        # Fixed-risk trades carry the declared per-unit risk (account
        # currency) they were sized with — prefer it over re-measuring a
        # stop distance the strategy may never have used (ATR stop_distance).
        per_unit_risk = (trade.meta or {}).get("sizing_risk_per_unit")
        if per_unit_risk is not None:
            trade_risk = new_qty * float(per_unit_risk)
        else:
            trade_risk = new_qty * stop_distance if stop_distance else 0.0
        if not isinstance(trade.meta, dict):
            trade.meta = {}
        trade.meta["portfolio"] = {
            "equity_at_entry": round(shared, 2),
            "open_positions": len(open_ledger),
            "open_notional_usd": round(open_notional, 2),
            "open_risk_usd": round(open_risk, 2),
        }
        open_ledger[trade.id] = (trade_notional, trade_risk)
        open_notional += trade_notional
        open_risk += trade_risk
        pending_pnl[trade.id] = (
            float(trade.net_pnl)
            if trade.net_pnl is not None
            else new_pnl - float(trade.fees or 0.0)
        )
        if shared > 0:
            peak_notional = max(peak_notional, open_notional)
            peak_exposure_pct = max(peak_exposure_pct, open_notional / shared)

    # Credit exits that fall after the last entry (no later entry releases
    # them) so the returned shared state is exact.
    _release_through("~")

    if not ordered:
        return ordered, [], {
            "shared_equity": True,
            "symbols_traded": [],
            "peak_open_notional_usd": 0.0,
            "peak_exposure_pct": 0.0,
        }
    first_time = start_time or str(ordered[0].entry_time)
    equity_curve: list[EquityPoint] = [
        EquityPoint(time=first_time, equity=round(float(initial_capital), 2))
    ]
    running = float(initial_capital)
    for trade in sorted(ordered, key=lambda t: str(t.last_exit_time or t.entry_time)):
        net = (
            float(trade.net_pnl)
            if trade.net_pnl is not None
            else float(trade.pnl or 0.0) - float(trade.fees or 0.0)
        )
        running += net
        point = EquityPoint(
            time=str(trade.last_exit_time or trade.entry_time),
            equity=round(running, 2),
        )
        if equity_curve[-1].time == point.time:
            equity_curve[-1] = point
        else:
            equity_curve.append(point)

    summary = {
        "shared_equity": True,
        "symbols_traded": sorted({t.symbol for t in ordered}),
        "peak_open_notional_usd": round(peak_notional, 2),
        "peak_exposure_pct": round(peak_exposure_pct, 6),
    }
    return ordered, equity_curve, summary

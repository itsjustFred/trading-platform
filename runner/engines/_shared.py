"""Shared serialization helpers for engine adapters."""

from __future__ import annotations

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
    trades: list[Trade], initial_capital: float
) -> tuple[list[Trade], list[EquityPoint], dict[str, Any]]:
    """Replay multi-symbol trades onto one running portfolio equity (D5, A-M4).

    Per-symbol engines size every trade from the FULL starting capital, so a
    3-symbol run risks N x risk_pct with no shared limits. Position size is
    linear in equity in every sizing branch (risk target, position cap and
    leverage cap are all equity fractions), therefore each trade carries an
    exact per-unit coefficient ``quantity / equity_at_entry``. This replay
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
            (new_pnl - float(trade.fees or 0.0)) if trade.net_pnl is not None else new_pnl
        )
        if shared > 0:
            peak_notional = max(peak_notional, open_notional)
            peak_exposure_pct = max(peak_exposure_pct, open_notional / shared)

    # Credit exits that fall after the last entry (no later entry releases
    # them) so the returned shared state is exact.
    _release_through("~")

    equity_curve: list[EquityPoint] = []
    running = float(initial_capital)
    for trade in sorted(ordered, key=lambda t: str(t.last_exit_time or t.entry_time)):
        net = float(trade.net_pnl) if trade.net_pnl is not None else float(trade.pnl or 0.0)
        running += net
        equity_curve.append(
            EquityPoint(
                time=str(trade.last_exit_time or trade.entry_time),
                equity=round(running, 2),
            )
        )

    summary = {
        "shared_equity": True,
        "symbols_traded": sorted({t.symbol for t in ordered}),
        "peak_open_notional_usd": round(peak_notional, 2),
        "peak_exposure_pct": round(peak_exposure_pct, 6),
    }
    return ordered, equity_curve, summary

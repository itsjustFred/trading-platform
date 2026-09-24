"""
Convert LEAN Engine backtest results to the universal trade format.

Reads LEAN's native JSON output (orders + profitLoss) and produces a
BacktestResult with normalized Trade objects.

Usage:
    from runner.converters.lean_converter import convert_lean_result
    result = convert_lean_result("path/to/lean_output.json")
    result.save("output.json")
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

logger = logging.getLogger(__name__)


_UTC_SUFFIX_Z = "Z"
_UTC_SUFFIX_OFFSET = "+00:00"


def convert_lean_result(
    lean_json_path: str | Path,
    strategy_name: str | None = None,
    engine: str = "lean",
) -> BacktestResult:
    """Convert a LEAN backtest JSON to a BacktestResult."""
    path = Path(lean_json_path)
    data = json.loads(path.read_text())

    trades = _build_trades(data)
    meta = _build_meta(data, strategy_name=strategy_name, engine=engine, trades=trades)
    equity = _build_equity(data)

    return BacktestResult(meta=meta, trades=trades, equity_curve=equity)


def _order_fee(order: dict) -> float:
    """Per-order fee/commission passthrough (C6).

    Reads the payload's fee fields when present; 0.0 only when the payload
    carries no fee information (recorded as-is, not silently assumed).
    """
    for key in ("fee", "commission", "totalFee", "total_fee"):
        try:
            value = order.get(key)
        except AttributeError:
            continue
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 0.0


def _build_trades(data: dict) -> list[Trade]:
    """Pair LEAN orders into entry/exit trades."""
    orders_dict = data.get("orders", {})
    profit_loss = data.get("profitLoss", {})

    # Flatten and sort
    all_orders = []
    for oid, order in orders_dict.items():
        sym_info = order.get("symbol", {})
        sym_value = sym_info.get("value", "UNKNOWN")
        ticker = _extract_ticker(sym_value)

        all_orders.append(
            {
                "id": order.get("id"),
                "time": order.get("time", ""),
                "quantity": order.get("quantity", 0),
                "price": order.get("price", 0),
                "tag": order.get("tag", ""),
                "ticker": ticker,
                # C6: fee passthrough — per-order fee/commission when the
                # payload carries it (0.0 only when absent, recorded as is).
                "fee": _order_fee(order),
            }
        )

    all_orders.sort(key=lambda x: (x["time"], x["id"] or 0))

    # Group by ticker
    by_ticker: dict[str, list] = defaultdict(list)
    for o in all_orders:
        by_ticker[o["ticker"]].append(o)

    # P&L lookup (handle both UTC suffix variants)
    pl_lookup: dict[str, float] = {}
    for ts, pnl in profit_loss.items():
        pl_lookup[ts] = float(pnl)
        pl_lookup[ts.replace(_UTC_SUFFIX_Z, _UTC_SUFFIX_OFFSET)] = float(pnl)
        pl_lookup[ts.replace(_UTC_SUFFIX_OFFSET, _UTC_SUFFIX_Z)] = float(pnl)

    trades: list[Trade] = []

    for ticker, orders in by_ticker.items():
        # C6: direction/qty-aware pairing. Sequential pairing broke on
        # partial fills, multiple opens and scaling (exit i+1 blindly closed
        # entry i). An open leg tracks remaining qty; opposite-sign fills
        # close into it (partial allowed), same-sign fills scale the leg in
        # at the volume-weighted average price.
        open_qty = 0.0
        open_notional = 0.0
        open_entry_qty = 0.0
        open_order: dict | None = None
        open_direction = ""
        open_exits: list[dict] = []
        open_fees = 0.0

        def _close_leg() -> None:
            nonlocal open_qty, open_notional, open_entry_qty
            nonlocal open_order, open_direction, open_exits, open_fees
            assert open_order is not None
            # Entry size/VWAP are frozen at open+scale-ins; partial closes
            # only shrink the remainder (open_qty).
            qty = abs(open_entry_qty)
            entry_price = open_notional / qty if qty else 0.0
            exit_qty = sum(abs(e["quantity"]) for e in open_exits)
            exit_notional = sum(abs(e["quantity"]) * e["price"] for e in open_exits)
            avg_exit = exit_notional / exit_qty if exit_qty else entry_price
            last_exit = open_exits[-1]
            pnl = pl_lookup.get(str(last_exit["time"]))
            if pnl is None:
                if open_direction == "LONG":
                    pnl = (avg_exit - entry_price) * qty
                else:
                    pnl = (entry_price - avg_exit) * qty
            exit_reason = str(last_exit["tag"] or "")
            if exit_reason.startswith("MR_"):
                parts = exit_reason.split("_")
                if len(parts) >= 2:
                    exit_reason = parts[1]
            entry_dt = _parse_iso(str(open_order["time"]))
            trade_exits = [
                TradeExit(
                    time=_parse_iso(str(e["time"])),
                    price=round(float(e["price"]), 6),
                    quantity=round(abs(float(e["quantity"])), 4),
                    pnl=None,
                    reason=exit_reason if e is last_exit else None,
                )
                for e in open_exits
            ]
            trade = Trade(
                id=len(trades) + 1,
                symbol=ticker,
                direction=open_direction,
                entry_time=entry_dt,
                entry_price=round(entry_price, 6),
                quantity=round(qty, 4),
                exits=trade_exits,
                pnl=round(float(pnl), 2),
                fees=round(open_fees, 2),
                tags=["lean", str(open_order["tag"] or "")],
            )
            trades.append(trade)
            open_qty = 0.0
            open_notional = 0.0
            open_entry_qty = 0.0
            open_order = None
            open_direction = ""
            open_exits = []
            open_fees = 0.0

        for o in orders:
            try:
                signed_qty = float(o["quantity"] or 0.0)
            except (TypeError, ValueError):
                signed_qty = 0.0
            if signed_qty == 0:
                continue
            tag = str(o["tag"] or "")
            is_entry_tag = "Entry" in tag or tag in ("LONG", "SHORT")
            leg_direction = "LONG" if signed_qty > 0 else "SHORT"
            if open_order is None:
                if not is_entry_tag:
                    logger.warning(
                        "LEAN orphan exit without open leg (ticker %s, order %r); skipped.",
                        ticker,
                        o["id"],
                    )
                    continue
                open_order = o
                open_direction = leg_direction
                open_qty = signed_qty
                open_entry_qty = signed_qty
                open_notional = abs(signed_qty) * float(o["price"] or 0.0)
                open_fees = float(o.get("fee") or 0.0)
                continue
            if leg_direction == open_direction:
                # Scale-in: merge into the open leg at VWAP. Signs decide,
                # not tags — an entry-tagged opposite fill still closes.
                open_qty += signed_qty
                open_entry_qty += signed_qty
                open_notional += abs(signed_qty) * float(o["price"] or 0.0)
                open_fees += float(o.get("fee") or 0.0)
                continue
            # Opposite-sign fill closes (possibly partially) the open leg.
            # An oversized exit flips direction: close the matched portion,
            # reopen the remainder as a fresh leg. The entry VWAP never
            # moves on a partial close — only open_qty shrinks.
            remaining_fill = abs(signed_qty)
            while remaining_fill >= 1e-12 and open_order is not None:
                matched = min(remaining_fill, abs(open_qty))
                open_exits.append(
                    {
                        "time": o["time"],
                        "price": o["price"],
                        "quantity": -matched if open_direction == "LONG" else matched,
                        "tag": tag,
                    }
                )
                open_fees += float(o.get("fee") or 0.0) * (
                    matched / abs(signed_qty) if signed_qty else 0.0
                )
                open_qty += -matched if open_direction == "LONG" else matched
                remaining_fill -= matched
                if abs(open_qty) < 1e-12:
                    _close_leg()
            if remaining_fill >= 1e-12:
                open_order = o
                open_direction = leg_direction
                open_qty = remaining_fill if leg_direction == "LONG" else -remaining_fill
                open_entry_qty = open_qty
                open_notional = remaining_fill * float(o["price"] or 0.0)
                open_fees = float(o.get("fee") or 0.0) * (
                    remaining_fill / abs(signed_qty) if signed_qty else 0.0
                )

        # An unclosed leg at report end is an open position, not a trade —
        # recorded in meta upstream, never a fabricated closed trade.

    trades.sort(key=lambda t: t.entry_time)
    return trades


def _order_times(data: dict) -> list[str]:
    """All order timestamps in the payload (for real meta bounds)."""
    times: list[str] = []
    orders = data.get("orders", {})
    if isinstance(orders, dict):
        for order in orders.values():
            if isinstance(order, dict) and order.get("time"):
                times.append(str(order["time"]))
    return sorted(times)


def _build_meta(
    data: dict,
    strategy_name: str | None = None,
    engine: str = "lean",
    trades: list[Trade] | None = None,
) -> BacktestMeta:
    """Extract metadata from LEAN output (C6: real bounds, no fixed capital).

    Window comes from the order timestamps; the capital default is only a
    last resort when the payload carries no starting-cash figure.
    """
    from datetime import datetime, timezone

    name = strategy_name or "lean_strategy"
    now = datetime.now(timezone.utc)

    times = _order_times(data)
    backtest_start = times[0] if times else ""
    backtest_end = times[-1] if times else ""
    symbols = sorted({trade.symbol for trade in (trades or []) if trade.symbol})
    initial_capital = 100000.0
    for key in ("startingCash", "starting_cash", "initialCapital", "initial_capital"):
        try:
            candidate = float(data.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            initial_capital = candidate
            break

    return BacktestMeta(
        id=f"lean-{name}-{now.strftime('%Y%m%d-%H%M%S')}",
        created_at=now.isoformat(),
        engine=engine,
        strategies=[name],
        backtest_start=backtest_start,
        backtest_end=backtest_end,
        initial_capital=initial_capital,
        data={"timeframes": [], "symbols": symbols, "universe": "", "source": "lean"},
    )


def _build_equity(data: dict) -> list[EquityPoint]:
    """Extract equity curve if available in LEAN output."""
    # LEAN's chart data may have "Equity" series
    charts = data.get("charts", {})
    equity_chart = charts.get("Strategy Equity", {})
    series = equity_chart.get("series", {})
    equity_series = series.get("Equity", {})
    values = equity_series.get("values", [])

    points = []
    for v in values:
        ts = v.get("x")
        val = v.get("y")
        if ts is not None and val is not None:
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                points.append(EquityPoint(time=dt, equity=float(val)))
            except (ValueError, TypeError):
                continue
    return points


def _extract_ticker(sym_value: str) -> str:
    """Extract clean ticker from LEAN symbol value like 'AAPL 2T' → 'AAPL'."""
    return sym_value.split()[0] if sym_value else "UNKNOWN"


def _parse_iso(ts: str) -> str:
    """Normalize ISO timestamp to consistent format."""
    try:
        dt = datetime.fromisoformat(ts.replace(_UTC_SUFFIX_Z, _UTC_SUFFIX_OFFSET))
        return dt.isoformat()
    except (ValueError, AttributeError):
        return ts

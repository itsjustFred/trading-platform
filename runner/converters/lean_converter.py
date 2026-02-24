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


def convert_lean_result(
    lean_json_path: str | Path,
    strategy_name: str | None = None,
    engine: str = "lean",
) -> BacktestResult:
    """Convert a LEAN backtest JSON to a BacktestResult."""
    path = Path(lean_json_path)
    data = json.loads(path.read_text())

    trades = _build_trades(data)
    meta = _build_meta(data, strategy_name=strategy_name, engine=engine)
    equity = _build_equity(data)

    return BacktestResult(meta=meta, trades=trades, equity_curve=equity)


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

        all_orders.append({
            "id": order.get("id"),
            "time": order.get("time", ""),
            "quantity": order.get("quantity", 0),
            "price": order.get("price", 0),
            "tag": order.get("tag", ""),
            "ticker": ticker,
        })

    all_orders.sort(key=lambda x: (x["time"], x["id"] or 0))

    # Group by ticker
    by_ticker: dict[str, list] = defaultdict(list)
    for o in all_orders:
        by_ticker[o["ticker"]].append(o)

    # P&L lookup (handle both Z and +00:00 suffixes)
    pl_lookup: dict[str, float] = {}
    for ts, pnl in profit_loss.items():
        pl_lookup[ts] = float(pnl)
        pl_lookup[ts.replace("Z", "+00:00")] = float(pnl)
        pl_lookup[ts.replace("+00:00", "Z")] = float(pnl)

    trades: list[Trade] = []

    for ticker, orders in by_ticker.items():
        i = 0
        while i < len(orders):
            o = orders[i]
            tag = o["tag"]

            is_entry = "Entry" in tag or tag in ("LONG", "SHORT")
            if not is_entry:
                i += 1
                continue

            entry = o
            if i + 1 >= len(orders):
                i += 1
                continue
            exit_order = orders[i + 1]
            i += 2

            direction = "LONG" if entry["quantity"] > 0 else "SHORT"
            entry_price = entry["price"]
            exit_price = exit_order["price"]
            qty = abs(entry["quantity"])

            # P&L
            exit_time_str = exit_order["time"]
            pnl = pl_lookup.get(exit_time_str)
            if pnl is None:
                if direction == "LONG":
                    pnl = (exit_price - entry_price) * qty
                else:
                    pnl = (entry_price - exit_price) * qty

            # Exit reason from tag
            exit_tag = exit_order["tag"]
            exit_reason = exit_tag
            if exit_tag.startswith("MR_"):
                parts = exit_tag.split("_")
                if len(parts) >= 2:
                    exit_reason = parts[1]

            entry_dt = _parse_iso(entry["time"])
            exit_dt = _parse_iso(exit_time_str)

            trade_exit = TradeExit(
                time=exit_dt,
                price=round(exit_price, 6),
                quantity=qty,
                pnl=round(pnl, 2),
                reason=exit_reason,
            )

            trade = Trade(
                id=len(trades) + 1,
                symbol=ticker,
                direction=direction,
                entry_time=entry_dt,
                entry_price=round(entry_price, 6),
                quantity=qty,
                exits=[trade_exit],
                pnl=round(pnl, 2),
                fees=0.0,
                tags=["lean", tag],
            )
            trades.append(trade)

    trades.sort(key=lambda t: t.entry_time)
    return trades


def _build_meta(
    data: dict,
    strategy_name: str | None = None,
    engine: str = "lean",
) -> BacktestMeta:
    """Extract metadata from LEAN output."""
    from datetime import datetime, timezone
    name = strategy_name or "lean_strategy"
    now = datetime.now(timezone.utc)

    return BacktestMeta(
        id=f"lean-{name}-{now.strftime('%Y%m%d-%H%M%S')}",
        strategy=name,
        engine=engine,
        created_at=now.isoformat(),
        timeframe_start="",
        timeframe_end="",
        initial_capital=100000.0,
        symbols=[],
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
                points.append(EquityPoint(timestamp=dt, equity=float(val)))
            except (ValueError, TypeError):
                continue
    return points


def _extract_ticker(sym_value: str) -> str:
    """Extract clean ticker from LEAN symbol value like 'AAPL 2T' → 'AAPL'."""
    return sym_value.split()[0] if sym_value else "UNKNOWN"


def _parse_iso(ts: str) -> str:
    """Normalize ISO timestamp to consistent format."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, AttributeError):
        return ts

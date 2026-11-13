"""
Convert Stockkit AI Bot trade CSVs to the universal trade format.

Stockkit uses semicolon-separated CSVs with partial exit support (TP1 + Runner).

Usage:
    from runner.converters.stockkit_converter import convert_stockkit_trades
    result = convert_stockkit_trades("path/to/trades.csv", strategy_name="ram_jet")
    result.save("output.json")
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit

logger = logging.getLogger(__name__)

# Stockkit CSV columns (;-separated)
TP1_PRICE_COLUMN = "TP1 Price"
EXPECTED_HEADERS = [
    "Symbol",
    "Direction",
    "Entry Date",
    "Entry Time",
    "Entry Price",
    "Exit Date",
    "Exit Time",
    "Exit Price",
    "Qty Total",
    "Qty TP1",
    "Qty Runner",
    "TP1 Reached",
    TP1_PRICE_COLUMN,
    "SL Price",
    "Initial SL",
    "Exit Reason",
    "PnL ($)",
    "PnL (%)",
    "Slippage",
    "Position Size ($)",
]


def convert_stockkit_trades(
    csv_path: str | Path,
    strategy_name: str | None = None,
    initial_capital: float = 100000.0,
) -> BacktestResult:
    """Convert a Stockkit trade CSV to a BacktestResult."""
    path = Path(csv_path)
    rows = _read_csv(path)

    if not strategy_name:
        # Infer from filename: trades_history_ram_jet.csv → ram_jet
        name = path.stem
        if name.startswith("trades_history_"):
            strategy_name = name.replace("trades_history_", "")
        elif name.startswith("live_trades_"):
            strategy_name = name.replace("live_trades_", "")
        else:
            strategy_name = name

    trades = _build_trades(rows, strategy_name)
    symbols = sorted({t.symbol for t in trades})

    now = datetime.now(timezone.utc).isoformat()
    meta = BacktestMeta(
        id=f"stockkit-{strategy_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        created_at=now,
        engine="stockkit",
        strategies=[strategy_name],
        backtest_start=trades[0].entry_time[:10] if trades else "",
        backtest_end=trades[-1].entry_time[:10] if trades else "",
        initial_capital=initial_capital,
        data={"timeframes": ["5m"], "symbols": symbols, "universe": "", "source": "stockkit"},
    )

    return BacktestResult(meta=meta, trades=trades, equity_curve=[])


def _read_csv(path: Path) -> list[dict]:
    """Read semicolon-separated Stockkit CSV."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            rows.append(row)
    return rows


def _build_trades(rows: list[dict], strategy_name: str) -> list[Trade]:
    """Convert CSV rows to Trade objects, handling partial exits."""
    trades: list[Trade] = []

    for i, row in enumerate(rows):
        try:
            symbol = row["Symbol"].strip()
            direction = row["Direction"].strip().upper()
            entry_date = row["Entry Date"].strip()
            entry_time = row["Entry Time"].strip()
            exit_date = row["Exit Date"].strip()
            exit_time_str = row["Exit Time"].strip()
            entry_price = float(row["Entry Price"])
            exit_price = float(row["Exit Price"])
            # C6: fractional quantities — int() truncation silently shrank
            # partial fills.
            qty_total = float(row["Qty Total"])
            qty_tp1 = float(row["Qty TP1"])
            qty_runner = float(row["Qty Runner"])
            if qty_total <= 0:
                raise ValueError(f"non-positive Qty Total: {row['Qty Total']!r}")
            tp1_reached = row["TP1 Reached"].strip().lower() == "yes"
            tp1_price = float(row[TP1_PRICE_COLUMN]) if row[TP1_PRICE_COLUMN].strip() else None
            exit_reason = row["Exit Reason"].strip()
            total_pnl = float(row["PnL ($)"])
            slippage = float(row["Slippage"]) if row["Slippage"].strip() else 0.0

            entry_dt = _parse_datetime(entry_date, entry_time)
            exit_dt = _parse_datetime(exit_date, exit_time_str)

            exits: list[TradeExit] = []

            if tp1_reached and tp1_price is not None and qty_tp1 > 0:
                # Partial exit: TP1 hit, then runner closed
                # We need to estimate TP1 PnL vs runner PnL
                if direction == "LONG":
                    tp1_pnl = (tp1_price - entry_price) * qty_tp1
                    runner_pnl = (exit_price - entry_price) * qty_runner
                else:
                    tp1_pnl = (entry_price - tp1_price) * qty_tp1
                    runner_pnl = (entry_price - exit_price) * qty_runner

                exits.append(
                    TradeExit(
                        # C6: the exact TP1 time is unknown, but an empty
                        # timestamp breaks downstream sort/match worse — use
                        # the exit time as the documented estimate.
                        time=exit_dt,
                        price=round(tp1_price, 6),
                        quantity=round(qty_tp1, 4),
                        pnl=round(tp1_pnl, 2),
                        reason="TP1",
                    )
                )
                exits.append(
                    TradeExit(
                        time=exit_dt,
                        price=round(exit_price, 6),
                        quantity=round(qty_runner, 4),
                        pnl=round(runner_pnl, 2),
                        reason=exit_reason,
                    )
                )
            else:
                # Single exit
                exits.append(
                    TradeExit(
                        time=exit_dt,
                        price=round(exit_price, 6),
                        quantity=round(qty_total, 4),
                        pnl=round(total_pnl, 2),
                        reason=exit_reason,
                    )
                )

            trade = Trade(
                id=i + 1,
                symbol=symbol,
                direction=direction,
                entry_time=entry_dt,
                entry_price=round(entry_price, 6),
                quantity=round(qty_total, 4),
                exits=exits,
                pnl=round(total_pnl, 2),
                # C6: slippage is a separate cost leg, not a fee — fees
                # stay 0.0 and slippage rides in meta (as-reported PnL basis
                # preserved verbatim).
                fees=0.0,
                tags=["stockkit", strategy_name],
                meta={"slippage": round(slippage, 4)},
            )
            trades.append(trade)

        except (KeyError, ValueError) as e:
            logger.warning("Skipping row %d: %s", i + 1, e)
            continue

    return trades


def _parse_datetime(date_str: str, time_str: str) -> str:
    """Parse stockkit date/time to ISO format (C6).

    Naive values are stamped UTC (the CSV carries no zone); unparseable
    values RAISE so the caller skips the row loudly instead of smuggling a
    raw f-string downstream into sort/match.
    """
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # One retry for date-only values (midnight UTC, documented).
        try:
            dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"unparsable stockkit datetime: {date_str!r} {time_str!r}") from exc
    return dt.replace(tzinfo=timezone.utc).isoformat()

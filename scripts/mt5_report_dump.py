#!/usr/bin/env python3
"""Dump a compact MT5 XLSX trade CSV and stats JSON for agent debugging.

The CSV has one row per position exit. Trade-level totals are repeated on
every exit row so partial exits remain visible without a second file format.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of reading another tree's clock profiles. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.mt5_report_dump
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("mt5_report_dump")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time - it
# is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of scripts
# that must carry the guard. Kept deliberately, and silenced rather than removed: dropping it would
# take this script off the roster and out of the gate that keeps the guard alive.
import shared  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._safe_path import reject_traversal
from shared.atomic_io import write_text_atomically
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference

TRADE_CSV_FIELDS = [
    "trade_id",
    "position_id",
    "order_id",
    "symbol",
    "direction",
    "entry_time",
    "entry_price",
    "quantity",
    "entry_comment",
    "exit_index",
    "exit_time",
    "exit_price",
    "exit_quantity",
    "exit_pnl",
    "exit_reason",
    "exit_count",
    "pnl",
    "commission",
    "swap",
    "fees",
    "net_pnl",
    "stop_loss",
    "take_profit",
]


def _number(value: Any) -> str:
    if value is None:
        return ""
    return format(float(value), ".10g")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _joined(values: list[Any]) -> str:
    return "; ".join(str(value) for value in values if value not in (None, ""))


def trade_csv_rows(reference: ValidationReference) -> list[dict[str, str]]:
    """Return one compact CSV row per trade exit."""
    rows: list[dict[str, str]] = []
    for trade in reference.trades:
        meta = trade.meta or {}
        exit_count = len(trade.exits)
        for index, exit_ in enumerate(trade.exits, start=1):
            rows.append(
                {
                    "trade_id": str(trade.id),
                    "position_id": _text(meta.get("position_id")),
                    "order_id": _text(meta.get("order_id")),
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "entry_time": trade.entry_time,
                    "entry_price": _number(trade.entry_price),
                    "quantity": _number(trade.quantity),
                    "entry_comment": _joined(trade.tags),
                    "exit_index": str(index),
                    "exit_time": exit_.time,
                    "exit_price": _number(exit_.price),
                    "exit_quantity": _number(exit_.quantity),
                    "exit_pnl": _number(exit_.pnl),
                    "exit_reason": _text(exit_.reason),
                    "exit_count": str(exit_count),
                    "pnl": _number(trade.pnl),
                    "commission": _number(meta.get("commission")),
                    "swap": _number(meta.get("swap")),
                    "fees": _number(trade.fees),
                    "net_pnl": _number(trade.net_pnl),
                    "stop_loss": _number(trade.stop_loss),
                    "take_profit": _number(trade.take_profit),
                }
            )
    return rows


def render_trade_csv(reference: ValidationReference) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(trade_csv_rows(reference))
    return buffer.getvalue()


def stats_summary(reference: ValidationReference) -> dict[str, Any]:
    """Return the small JSON summary posted for MT5 report debugging."""
    raw_metadata = reference.raw_metadata or {}
    position_fills = raw_metadata.get("position_fills") or {}
    summary: dict[str, Any] = {
        "strategy_name": reference.strategy_name,
        "source_framework": reference.source_framework,
        "symbol": reference.symbol,
        "timeframe": reference.timeframe,
        "source_path": reference.source_path,
        "source_timezone": raw_metadata.get("source_timezone"),
        "broker_clock": raw_metadata.get("broker_clock"),
        "trade_count": len(reference.trades),
        "exit_count": sum(len(trade.exits) for trade in reference.trades),
        "stats": reference.stats,
    }
    for key in (
        "open_position_count_including_partial",
        "open_position_ids",
        "orphan_exit_count",
        "orphan_exit_ids",
    ):
        if key in position_fills:
            summary[key] = position_fills[key]
    return summary


def render_stats_json(reference: ValidationReference) -> str:
    return json.dumps(stats_summary(reference), indent=2, sort_keys=True) + "\n"


def dump_reference(
    reference: ValidationReference,
    csv_path: Path,
    json_path: Path,
) -> tuple[Path, Path]:
    """Write the CSV and JSON dump atomically and return their paths."""
    safe_csv = reject_traversal(csv_path, field="--csv")
    safe_json = reject_traversal(json_path, field="--json")
    write_text_atomically(safe_csv, render_trade_csv(reference))
    write_text_atomically(safe_json, render_stats_json(reference))
    return safe_csv, safe_json


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a compact CSV and JSON stats dump for an MT5 XLSX report."
    )
    parser.add_argument("report", type=Path, help="Path to the MT5 XLSX report.")
    parser.add_argument("--csv", type=Path, required=True, help="CSV output path.")
    parser.add_argument(
        "--json", dest="json_path", type=Path, required=True, help="JSON output path."
    )
    timezone_group = parser.add_mutually_exclusive_group()
    timezone_group.add_argument(
        "--source-timezone",
        help="IANA timezone for naive MT5 XLSX report timestamps (e.g. Europe/Athens).",
    )
    timezone_group.add_argument(
        "--broker-clock",
        help="Named broker server clock for MT5 XLSX report timestamps "
             "(e.g. dukascopy_mt5_index).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    report = reject_traversal(args.report, field="report")
    if report.suffix.lower() != ".xlsx":
        print("report: expected an .xlsx MT5 report", file=sys.stderr)
        return 2

    try:
        reference = MT5XLSXImporter().load(
            report,
            source_timezone=args.source_timezone,
            broker_clock=args.broker_clock,
        )
        csv_path, json_path = dump_reference(reference, args.csv, args.json_path)
    except (OSError, ValueError) as exc:
        print(f"mt5_report_dump: {exc}", file=sys.stderr)
        return 2

    print(f"Wrote {csv_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

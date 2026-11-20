#!/usr/bin/env python3
"""Assert that an MT5 Strategy Tester HTML report is the run we asked for.

Every silent failure in the CI flow still produces a normal-looking report:
a missing ``/portable``, a BOM in ``tester.ini``, forward slashes in
``Expert=`` (the tester falls back to the stock Moving Average sample) or a
zero-trade run. This script turns those into a loud, non-zero exit.

It reads the report without third-party dependencies (the CI job does not
install the project's Python deps) and understands both the German and the
English report labels, because the report language follows the terminal UI
language.

Usage::

    python tools/mt5_ci/inspect_report.py --report report.html \\
        --expect-expert IntradayVolatilityBreakout --min-trades 1 --json-out summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

LABELS: dict[str, tuple[str, ...]] = {
    "expert": ("Expert", "Expertenprogramm"),
    "symbol": ("Symbol",),
    "period": ("Period", "Periode"),
    "initial_deposit": ("Initial Deposit", "Ersteinlage"),
    "leverage": ("Leverage", "Hebel"),
    "total_trades": ("Total Trades", "Gesamtzahl Trades"),
    "total_deals": ("Total Deals", "Anzahl Deals"),
    "total_net_profit": ("Total Net Profit", "Nettogewinn gesamt"),
    "gross_profit": ("Gross Profit", "Bruttogewinn"),
    "gross_loss": ("Gross Loss", "Bruttoverlust"),
    "profit_factor": ("Profit Factor", "Profitfaktor"),
    "bars": ("Bars", "Balken"),
    "ticks": ("Ticks",),
    "model": ("Model", "Modell"),
}

# Column labels of the deals table, German and English.
DEAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "time": ("Time", "Zeit"),
    "deal": ("Deal",),
    "symbol": ("Symbol",),
    "type": ("Type", "Typ"),
    "direction": ("Direction", "Richtung"),
    "volume": ("Volume", "Volumen"),
    "price": ("Price", "Preis"),
    "order": ("Order", "Auftrag"),
    "commission": ("Commission", "Kommission"),
    "swap": ("Swap",),
    "profit": ("Profit", "Gewinn"),
    "balance": ("Balance", "Kontostand"),
    "comment": ("Comment", "Kommentar"),
}

# The stock EA the tester falls back to when Expert= cannot be resolved.
FALLBACK_EXPERT_MARKER = "moving average"


class _TableParser(HTMLParser):
    """Collect every table as a list of rows, each row a list of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack and self._stack[-1]:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._stack and self._stack[-1]:
                self._stack[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())


def decode_report(raw: bytes) -> str:
    """MT5 writes the report as UTF-16LE with a BOM; accept UTF-8 too."""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8", errors="replace")


def _normalise(label: str) -> str:
    return " ".join(label.replace(":", " ").split()).strip().lower()


def _label_set(names: tuple[str, ...]) -> set[str]:
    return {_normalise(name) for name in names}


def _cell_pairs(tables: list[list[list[str]]]) -> list[tuple[str, str]]:
    """Return (label, value) pairs from adjacent cells, in document order."""
    pairs: list[tuple[str, str]] = []
    for table in tables:
        for row in table:
            for index in range(len(row) - 1):
                label = row[index]
                value = row[index + 1]
                if label and value:
                    pairs.append((label, value))
    return pairs


def _lookup(pairs: list[tuple[str, str]], key: str) -> str | None:
    wanted = _label_set(LABELS[key])
    for label, value in pairs:
        if _normalise(label) in wanted:
            return value
    return None


def parse_number(text: str | None) -> float | None:
    """Parse a report number, tolerating thousands separators and locale."""
    if not text:
        return None
    cleaned = text.replace("\u00a0", "").replace(" ", "").replace("%", "")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        # "1.234.567" is grouped thousands, not a decimal.
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_deals_table(tables: list[list[list[str]]]) -> tuple[list[str], list[list[str]]] | None:
    """Locate the deals header row and return it with the rows that follow.

    MT5 writes the whole report into one table, so the header is a row in the
    middle rather than the first row of a table. The header is identified by
    its column labels (Time + Direction + Price); the Orders table above it has
    "Open Time" and no Direction column, so it cannot match.
    """
    for table in tables:
        for index, row in enumerate(table):
            normalised = [_normalise(cell) for cell in row]
            has_time = any(n in _label_set(DEAL_COLUMNS["time"]) for n in normalised)
            has_direction = any(n in _label_set(DEAL_COLUMNS["direction"]) for n in normalised)
            has_price = any(n in _label_set(DEAL_COLUMNS["price"]) for n in normalised)
            if has_time and has_direction and has_price:
                return row, table[index + 1 :]
    return None


def _column_index(header: list[str], key: str) -> int | None:
    wanted = _label_set(DEAL_COLUMNS[key])
    for index, cell in enumerate(header):
        if _normalise(cell) in wanted:
            return index
    return None


def extract_deals(tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    found = _find_deals_table(tables)
    if found is None:
        return []
    header, rows = found
    columns = {key: _column_index(header, key) for key in DEAL_COLUMNS}
    deals: list[dict[str, Any]] = []
    for row in rows:
        def cell(key: str) -> str:
            index = columns[key]
            if index is None or index >= len(row):
                return ""
            return row[index]

        if not cell("time"):
            continue
        if cell("type").strip().lower() == "balance":
            continue
        deals.append(
            {
                "time": cell("time"),
                "symbol": cell("symbol"),
                "type": cell("type"),
                "direction": cell("direction"),
                "volume": parse_number(cell("volume")),
                "price": parse_number(cell("price")),
                "order": cell("order"),
                "profit": parse_number(cell("profit")),
                "comment": cell("comment"),
            }
        )
    return deals


def summarise(report: Path) -> dict[str, Any]:
    text = decode_report(report.read_bytes())
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    pairs = _cell_pairs(parser.tables)

    summary: dict[str, Any] = {"report": str(report)}
    for key in LABELS:
        summary[key] = _lookup(pairs, key)
    summary["total_trades_value"] = parse_number(summary["total_trades"])
    summary["total_net_profit_value"] = parse_number(summary["total_net_profit"])
    deals = extract_deals(parser.tables)
    summary["deal_rows"] = len(deals)
    summary["deals"] = deals
    return summary


def check(
    summary: dict[str, Any],
    expect_expert: str | None,
    expect_symbol: str | None,
    min_trades: int,
) -> list[str]:
    """Return a list of human-readable failures (empty means the run is valid)."""
    failures: list[str] = []
    expert = (summary.get("expert") or "").strip()

    if not expert:
        failures.append("report has no Expert/Expertenprogramm field")
    elif expect_expert and expect_expert.lower() not in expert.lower():
        failures.append(f"report expert {expert!r} does not contain {expect_expert!r}")
    if expert and FALLBACK_EXPERT_MARKER in expert.lower() and (
        expect_expert is None or FALLBACK_EXPERT_MARKER not in expect_expert.lower()
    ):
        failures.append(
            f"report expert {expert!r} is the stock sample: Expert= in tester.ini "
            "was not resolved (backslashes? wrong MQL5\\Experts path?)"
        )

    if expect_symbol:
        symbol = (summary.get("symbol") or "").strip()
        if symbol.lower() != expect_symbol.lower():
            failures.append(f"report symbol {symbol!r} != expected {expect_symbol!r}")

    trades = summary.get("total_trades_value")
    if trades is None:
        failures.append("report has no Total Trades/Gesamtzahl Trades field")
    elif trades < min_trades:
        failures.append(f"report has {int(trades)} trades, expected at least {min_trades}")

    if not summary.get("deals"):
        failures.append("report has no deals table rows")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expect-expert", default=None)
    parser.add_argument("--expect-symbol", default=None)
    parser.add_argument("--min-trades", type=int, default=1)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--no-deals", action="store_true", help="omit deal rows from JSON")
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"inspect_report: report not found: {args.report}", file=sys.stderr)
        return 1

    summary = summarise(args.report)
    failures = check(summary, args.expect_expert, args.expect_symbol, args.min_trades)

    printable = dict(summary)
    if args.no_deals:
        printable.pop("deals", None)
    print(json.dumps(printable, indent=2, ensure_ascii=False))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if failures:
        for failure in failures:
            print(f"inspect_report: FAIL {failure}", file=sys.stderr)
        return 1
    print(
        "inspect_report: OK "
        f"expert={summary.get('expert')!r} symbol={summary.get('symbol')!r} "
        f"trades={int(summary.get('total_trades_value') or 0)} "
        f"deals={summary.get('deal_rows')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

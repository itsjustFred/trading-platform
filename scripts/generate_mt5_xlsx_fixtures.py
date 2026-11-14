#!/usr/bin/env python3
"""Generate the synthetic MT5 Strategy Tester XLSX fixtures.

Output: ``tests/fixtures/validation/mt5_xlsx/*.xlsx``.

Every number in these files is invented (EURUSD/H4/H1, 2024). They follow the
column layout in ``docs/mt5_xlsx_schema.md``. The expected results for each
shape are hand-computed in ``tests/validation/test_mt5_xlsx_fixture_shapes.py``
and must not be derived from importer output. If you change a row here, change
the expectations there in the same commit.

Commission and swap use MT5's signed convention (a cost is negative). Profit
(``Gewinn``) is gross of commission and swap.

Usage::

    python scripts/generate_mt5_xlsx_fixtures.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "validation" / "mt5_xlsx"

# Fixed document timestamp. Re-running the generator gives identical cell content and
# goldens; the .xlsx bytes still differ because zip entries carry the write time.
_FIXED_TIME = datetime(2024, 1, 1, 0, 0, 0)

_DEALS_HEADER = [
    "Zeit",
    "Trade",
    "Symbol",
    "Typ",
    "Richtung",
    "Volumen",
    "Preis",
    "Auftrag",
    "Kommission",
    "Swap",
    "Gewinn",
    "Kontostand",
    "Kommentar",
]
_ORDERS_HEADER = [
    "Auftrag",
    "Symbol",
    "Typ",
    "Volumen",
    "Preis",
    "S / L",
    "T / P",
    "Zeit",
    "Status",
    "Kommentar",
]


def _meta(strategy: str, symbol: str, period: str, deposit: float) -> list[list[Any]]:
    return [
        ["Strategietesterbericht"],
        ["Expertenprogramm:", strategy],
        ["Symbol:", symbol],
        ["Periode:", period],
        ["Ersteinlage:", deposit],
        ["Hebel:", "1:100"],
        [],
    ]


def _write_report(
    path: Path,
    *,
    meta: list[list[Any]],
    stats: list[list[Any]],
    orders: list[list[Any]],
    deals: list[list[Any]],
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for row in meta:
        ws.append(row)
    ws.append(["Ergebnisse"])
    for row in stats:
        ws.append(row)
    ws.append([])
    ws.append(["Orders"])
    ws.append(_ORDERS_HEADER)
    for row in orders:
        ws.append(row)
    ws.append([])
    ws.append(["Trades"])
    ws.append(_DEALS_HEADER)
    for row in deals:
        ws.append(row)
    wb.properties.creator = "trading-platform fixture generator"
    wb.properties.created = _FIXED_TIME
    wb.properties.modified = _FIXED_TIME
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _balance(time: str, number: int, balance: float) -> list[Any]:
    return [time, number, None, "balance", None, None, None, None, 0.0, 0.0, 0.0, balance, None]


def build_hedged_partial_open(path: Path) -> None:
    """Deal-numbered report: hedged long/short, split exit, partial and open position.

    Positions (scope = symbol + entry side, see ``_pairing.entry_side_scope``):

    * P1 LONG 0.10 in/out, closed by TP. Ordinary round trip.
    * P2 LONG 0.20 in, closed by two exits 0.12 + 0.08 (split exit).
    * P3 SHORT 0.10 opened while P2 is still open (hedge), closed by SL.
    * P4 SHORT 0.20 in, exit 0.12 only: ``partial``, residual 0.08, no Trade.
    * P5 LONG 0.05 in with no exit: ``open``, no Trade.

    Hand-computed values are in the test module, not here.
    """
    deals = [
        _balance("2024.01.01 00:00:00", 1, 10000.0),
        # P1
        [
            "2024.01.02 08:00:00",
            2,
            "EURUSD",
            "buy",
            "in",
            0.10,
            1.1000,
            201,
            -0.70,
            0.0,
            0.0,
            9999.30,
            "Breakout Long",
        ],
        [
            "2024.01.02 12:00:00",
            3,
            "EURUSD",
            "sell",
            "out",
            0.10,
            1.1050,
            202,
            -0.70,
            0.0,
            50.00,
            10048.60,
            "tp 1.10500",
        ],
        # P2 (split exit) and P3 (hedge short) overlap
        [
            "2024.01.03 08:00:00",
            4,
            "EURUSD",
            "buy",
            "in",
            0.20,
            1.1010,
            203,
            -1.40,
            0.0,
            0.0,
            10047.20,
            "Breakout Long",
        ],
        [
            "2024.01.03 09:00:00",
            5,
            "EURUSD",
            "sell",
            "in",
            0.10,
            1.1020,
            206,
            -0.70,
            0.0,
            0.0,
            10046.50,
            "Hedge Short",
        ],
        [
            "2024.01.03 10:00:00",
            6,
            "EURUSD",
            "buy",
            "out",
            0.10,
            1.1035,
            207,
            -0.70,
            -0.30,
            -15.00,
            10030.50,
            "sl 1.10350",
        ],
        [
            "2024.01.03 12:00:00",
            7,
            "EURUSD",
            "sell",
            "out",
            0.12,
            1.1040,
            204,
            -0.84,
            0.0,
            36.00,
            10065.66,
            "Partial TP",
        ],
        [
            "2024.01.03 13:00:00",
            8,
            "EURUSD",
            "sell",
            "out",
            0.08,
            1.1030,
            205,
            -0.56,
            0.0,
            16.00,
            10081.10,
            "tp 1.10300",
        ],
        # P4 (partial, never fully closed) and P5 (open at report end)
        [
            "2024.01.04 08:00:00",
            9,
            "EURUSD",
            "sell",
            "in",
            0.20,
            1.1000,
            208,
            -1.40,
            0.0,
            0.0,
            10079.70,
            "Breakout Short",
        ],
        [
            "2024.01.04 10:00:00",
            10,
            "EURUSD",
            "buy",
            "out",
            0.12,
            1.0980,
            209,
            -0.84,
            0.0,
            24.00,
            10102.86,
            "Partial TP",
        ],
        [
            "2024.01.04 12:00:00",
            11,
            "EURUSD",
            "buy",
            "in",
            0.05,
            1.0990,
            210,
            -0.35,
            0.0,
            0.0,
            10102.51,
            "Breakout Long",
        ],
    ]
    orders = [
        [
            201,
            "EURUSD",
            "buy",
            "0.10 / 0.10",
            1.1000,
            1.0950,
            1.1050,
            "2024.01.02 08:00:00",
            "filled",
            "Breakout Long",
        ],
        [
            202,
            "EURUSD",
            "sell",
            "0.10 / 0.10",
            1.1050,
            None,
            None,
            "2024.01.02 12:00:00",
            "filled",
            "tp 1.10500",
        ],
        [
            203,
            "EURUSD",
            "buy",
            "0.20 / 0.20",
            1.1010,
            1.0980,
            None,
            "2024.01.03 08:00:00",
            "filled",
            "Breakout Long",
        ],
        [
            204,
            "EURUSD",
            "sell",
            "0.12 / 0.12",
            1.1040,
            None,
            None,
            "2024.01.03 12:00:00",
            "filled",
            "Partial TP",
        ],
        [
            205,
            "EURUSD",
            "sell",
            "0.08 / 0.08",
            1.1030,
            None,
            None,
            "2024.01.03 13:00:00",
            "filled",
            "tp 1.10300",
        ],
        [
            206,
            "EURUSD",
            "sell",
            "0.10 / 0.10",
            1.1020,
            1.1050,
            None,
            "2024.01.03 09:00:00",
            "filled",
            "Hedge Short",
        ],
        [
            207,
            "EURUSD",
            "buy",
            "0.10 / 0.10",
            1.1035,
            None,
            None,
            "2024.01.03 10:00:00",
            "filled",
            "sl 1.10350",
        ],
        [
            208,
            "EURUSD",
            "sell",
            "0.20 / 0.20",
            1.1000,
            1.1030,
            None,
            "2024.01.04 08:00:00",
            "filled",
            "Breakout Short",
        ],
        [
            209,
            "EURUSD",
            "buy",
            "0.12 / 0.12",
            1.0980,
            None,
            None,
            "2024.01.04 10:00:00",
            "filled",
            "Partial TP",
        ],
        [
            210,
            "EURUSD",
            "buy",
            "0.05 / 0.05",
            1.0990,
            1.0950,
            None,
            "2024.01.04 12:00:00",
            "filled",
            "Breakout Long",
        ],
    ]
    # Summary: MT5's "Nettogewinn gesamt" is the sum of every out-deal profit plus
    # every commission and swap, i.e. 111.00 - 8.19 - 0.30 = 102.51.
    # "Anzahl Deals" counts the ten non-balance deals.
    stats = [
        ["Gesamtanzahl Trades", 3, "Gewonnene Trades (in % von Gesamt)", "2 (66.67%)"],
        ["Verlorene Trades (in % von Gesamt)", "1 (33.33%)", "Nettogewinn gesamt", 102.51],
        ["Anzahl Deals", 10],
    ]
    _write_report(
        path,
        meta=_meta(
            "Synthetic Hedge Partial Fixture", "EURUSD", "H1 (2024.01.01 - 2024.01.31)", 10000
        ),
        stats=stats,
        orders=orders,
        deals=deals,
    )


def build_position_id_shared(path: Path) -> None:
    """Position-numbered export: the Trade column repeats the position id.

    Some exporters write the position id in ``Trade`` instead of a unique deal
    id. The importer then groups by that id (``_pairing`` shared-id path):

    * 1001 LONG: two entries (0.10 + 0.05) and two exits (0.10 + 0.05). One trade.
    * 1002 SHORT: one entry 0.20, one exit 0.20. One trade.
    * 1003 LONG: entry 0.10, exit 0.04. Partial (residual 0.06), no trade.
    * 1004 LONG: opened while 1003 is still open, exit 0.05. One trade. Only
      the shared-id grouping keeps it apart from 1003.
    """
    deals = [
        _balance("2024.02.01 00:00:00", 1, 10000.0),
        [
            "2024.02.01 09:00:00",
            1001,
            "EURUSD",
            "buy",
            "in",
            0.10,
            1.1000,
            301,
            -0.50,
            0.0,
            0.0,
            9999.50,
            "Entry A",
        ],
        [
            "2024.02.01 10:00:00",
            1001,
            "EURUSD",
            "buy",
            "in",
            0.05,
            1.1010,
            302,
            -0.25,
            0.0,
            0.0,
            9999.25,
            "Add",
        ],
        [
            "2024.02.01 12:00:00",
            1001,
            "EURUSD",
            "sell",
            "out",
            0.10,
            1.1040,
            303,
            -0.50,
            0.0,
            40.00,
            10038.75,
            "tp 1.10400",
        ],
        [
            "2024.02.01 13:00:00",
            1001,
            "EURUSD",
            "sell",
            "out",
            0.05,
            1.1030,
            304,
            -0.25,
            0.0,
            15.00,
            10053.50,
            "tp 1.10300",
        ],
        [
            "2024.02.01 14:00:00",
            1002,
            "EURUSD",
            "sell",
            "in",
            0.20,
            1.1100,
            305,
            -1.00,
            0.0,
            0.0,
            10052.50,
            "Short",
        ],
        [
            "2024.02.01 16:00:00",
            1002,
            "EURUSD",
            "buy",
            "out",
            0.20,
            1.1080,
            306,
            -1.00,
            0.0,
            40.00,
            10091.50,
            "tp 1.10800",
        ],
        [
            "2024.02.01 17:00:00",
            1003,
            "EURUSD",
            "buy",
            "in",
            0.10,
            1.1050,
            307,
            -0.50,
            0.0,
            0.0,
            10091.00,
            "Entry C",
        ],
        [
            "2024.02.01 18:00:00",
            1003,
            "EURUSD",
            "sell",
            "out",
            0.04,
            1.1060,
            308,
            -0.20,
            0.0,
            4.00,
            10094.80,
            "Partial",
        ],
        # 1004 opens on the same side while 1003 still has 0.06 open. Grouping by
        # the shared id keeps it separate; a sequential walker would merge it.
        [
            "2024.02.01 18:30:00",
            1004,
            "EURUSD",
            "buy",
            "in",
            0.05,
            1.1070,
            309,
            -0.25,
            0.0,
            0.0,
            10094.55,
            "Overlap Long",
        ],
        [
            "2024.02.01 19:00:00",
            1004,
            "EURUSD",
            "sell",
            "out",
            0.05,
            1.1080,
            310,
            -0.25,
            0.0,
            5.00,
            10099.30,
            "tp 1.10800",
        ],
    ]
    stats = [
        ["Gesamtanzahl Trades", 3, "Gewonnene Trades (in % von Gesamt)", "3 (100.00%)"],
        ["Verlorene Trades (in % von Gesamt)", "0 (0.00%)", "Nettogewinn gesamt", 99.30],
        ["Anzahl Deals", 10],
    ]
    _write_report(
        path,
        meta=_meta(
            "Synthetic Position Id Fixture", "EURUSD", "H4 (2024.02.01 - 2024.02.29)", 10000
        ),
        stats=stats,
        orders=[],
        deals=deals,
    )


_FIXTURES = ("hedged_partial_open", "position_id_shared")

_README = """# MT5 XLSX synthetic fixtures

Generated by `scripts/generate_mt5_xlsx_fixtures.py`. Do not edit the `.xlsx`
files or the `goldens/` JSON by hand. Regenerate them with:

    python scripts/generate_mt5_xlsx_fixtures.py

All values are invented. No file here is an export from a real account.

- `hedged_partial_open.xlsx`: deal-numbered deals. Hedged long and short on
  one symbol, a split exit, a partial position and an open position.
- `position_id_shared.xlsx`: position-numbered deals (`Trade` repeats the
  position id). Split entries and exits, a partial position and an overlapping
  same-side position.

`goldens/<name>.json` is the importer output projected onto the fields that
the tests check (`net_pnl` is left out on purpose, see
`tests/validation/test_golden_reconciliation.py`). The expected values are
hand-computed in `tests/validation/test_mt5_xlsx_fixture_shapes.py`; the
golden is only a regression pin on top of those.
"""


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 10)


def project_reference(ref: Any) -> dict[str, Any]:
    """Project a ValidationReference onto the stable, hand-checked fields."""
    trades = [
        {
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_time": t.entry_time,
            "entry_price": _r(t.entry_price),
            "quantity": _r(t.quantity),
            "pnl": _r(t.pnl),
            "fees": _r(t.fees),
            "stop_loss": _r(t.stop_loss),
            "take_profit": _r(t.take_profit),
            "exit_reason": t.exit_reason,
            "tags": list(t.tags),
            "position_id": t.meta.get("position_id"),
            "exits": [
                {
                    "time": e.time,
                    "price": _r(e.price),
                    "quantity": _r(e.quantity),
                    "pnl": _r(e.pnl),
                    "reason": e.reason,
                }
                for e in t.exits
            ],
        }
        for t in ref.trades
    ]
    stats = {
        key: ref.stats[key]
        for key in (
            "total_trades",
            "winners",
            "losers",
            "win_rate",
            "total_pnl",
            "total_deals",
            "total_return",
        )
        if key in ref.stats
    }
    fills = ref.raw_metadata["position_fills"]
    fill_summary = {
        key: fills[key]
        for key in (
            "closed_position_count",
            "partial_position_count",
            "open_position_count",
            "open_only_count",
            "orphan_exit_count",
            "closed_position_ids",
            "partial_position_ids",
            "open_only_position_ids",
            "residual_position_count",
            "residual_exit_count",
        )
    }
    return {
        "strategy_name": ref.strategy_name,
        "source_framework": ref.source_framework,
        "symbol": ref.symbol,
        "timeframe": ref.timeframe,
        "trades": trades,
        "stats": {k: _r(v) if isinstance(v, float) else v for k, v in stats.items()},
        "position_fills": fill_summary,
    }


def generate() -> None:
    import json
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from validation.importers.mt5_xlsx import MT5XLSXImporter

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    golden_dir = FIXTURE_DIR / "goldens"
    golden_dir.mkdir(parents=True, exist_ok=True)

    build_hedged_partial_open(FIXTURE_DIR / "hedged_partial_open.xlsx")
    build_position_id_shared(FIXTURE_DIR / "position_id_shared.xlsx")
    (FIXTURE_DIR / "README.md").write_text(_README, encoding="utf-8")

    importer = MT5XLSXImporter()
    for name in _FIXTURES:
        xlsx = FIXTURE_DIR / f"{name}.xlsx"
        golden = golden_dir / f"{name}.json"
        projected = project_reference(importer.load(xlsx))
        golden.write_text(
            json.dumps(projected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"[OK] {xlsx.name}  ->  goldens/{golden.name}  ({len(projected['trades'])} trades)")


if __name__ == "__main__":
    generate()

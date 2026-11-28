"""Tests for scripts/mt5_report_dump.py."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts import mt5_report_dump
from validation.importers.mt5_xlsx import MT5XLSXImporter

FIXTURE = Path("tests/fixtures/validation/mt5_xlsx/hedged_partial_open.xlsx")


def test_trade_csv_has_one_row_per_exit_with_trade_totals() -> None:
    reference = MT5XLSXImporter().load(FIXTURE)

    rows = mt5_report_dump.trade_csv_rows(reference)

    assert len(rows) == 4
    partial_rows = [row for row in rows if row["trade_id"] == "2"]
    assert [row["exit_index"] for row in partial_rows] == ["1", "2"]
    assert {row["exit_count"] for row in partial_rows} == {"2"}
    assert {row["pnl"] for row in partial_rows} == {"52"}
    assert {row["fees"] for row in partial_rows} == {"2.8"}
    assert {row["net_pnl"] for row in partial_rows} == {"49.2"}
    assert [row["exit_quantity"] for row in partial_rows] == ["0.12", "0.08"]


def test_render_trade_csv_is_compact_and_headered() -> None:
    reference = MT5XLSXImporter().load(FIXTURE)

    text = mt5_report_dump.render_trade_csv(reference)
    parsed = list(csv.DictReader(text.splitlines()))

    assert parsed[0]["trade_id"] == "1"
    assert parsed[0]["direction"] == "LONG"
    assert parsed[0]["entry_time"] == "2024-01-02T08:00:00Z"
    assert parsed[0]["exit_reason"] == "take_profit"
    assert len(parsed[0]) == len(mt5_report_dump.TRADE_CSV_FIELDS)


def test_stats_summary_contains_provenance_and_importer_stats() -> None:
    reference = MT5XLSXImporter().load(FIXTURE)

    summary = mt5_report_dump.stats_summary(reference)

    assert summary["strategy_name"] == "Synthetic Hedge Partial Fixture"
    assert summary["source_framework"] == "mt5_xlsx"
    assert summary["symbol"] == "EURUSD"
    assert summary["timeframe"] == "h1"
    assert summary["source_timezone"] == "UTC"
    assert summary["broker_clock"] is None
    assert summary["trade_count"] == 3
    assert summary["exit_count"] == 4
    assert summary["stats"]["total_trades"] == 3
    assert summary["open_position_count_including_partial"] == 2


def test_main_writes_csv_and_json(tmp_path: Path) -> None:
    csv_path = tmp_path / "trades.csv"
    json_path = tmp_path / "stats.json"

    rc = mt5_report_dump.main([
        str(FIXTURE),
        "--csv", str(csv_path),
        "--json", str(json_path),
    ])

    assert rc == 0
    assert csv_path.exists()
    assert json_path.exists()
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["trade_count"] == 3
    assert summary["exit_count"] == 4


def test_main_accepts_broker_clock(tmp_path: Path) -> None:
    csv_path = tmp_path / "trades.csv"
    json_path = tmp_path / "stats.json"

    rc = mt5_report_dump.main([
        str(FIXTURE),
        "--csv", str(csv_path),
        "--json", str(json_path),
        "--broker-clock", "dukascopy_mt5_index",
    ])

    assert rc == 0
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["broker_clock"] == "dukascopy_mt5_index"
    assert summary["source_timezone"] is None


def test_main_rejects_non_xlsx_report(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text("<html></html>", encoding="utf-8")

    rc = mt5_report_dump.main([
        str(report),
        "--csv", str(tmp_path / "trades.csv"),
        "--json", str(tmp_path / "stats.json"),
    ])

    assert rc == 2

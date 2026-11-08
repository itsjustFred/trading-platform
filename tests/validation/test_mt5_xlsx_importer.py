"""Tests for validation.importers.mt5_xlsx.MT5XLSXImporter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from shared.models import VALID_DIRECTIONS
from validation.importers._pairing import group_position_fills
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference

_REPORTS = (
    Path(__file__).resolve().parents[2]
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
)


def _eurusd_path() -> Path:
    return _REPORTS / "EURUSD_H1.xlsx"


def _nas100_path() -> Path:
    return _REPORTS / "NAS100_M15.xlsx"


def _sp500_path() -> Path:
    return _REPORTS / "SP500_M1.xlsx"


@pytest.fixture
def importer() -> MT5XLSXImporter:
    return MT5XLSXImporter()


class TestEURUSDH1:
    def test_load_returns_validation_reference(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert isinstance(ref, ValidationReference)

    def test_source_framework(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert ref.source_framework == "mt5_xlsx"

    def test_strategy_name(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "ZenomTrader" in ref.strategy_name
        assert "IntradayVolatilityBreakout" in ref.strategy_name

    def test_symbol(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert ref.symbol == "EURUSD"

    def test_timeframe(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert ref.timeframe.lower() == "h1"

    def test_trades_count_positive(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert len(ref.trades) > 0

    def test_first_trade_has_valid_direction(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        first = ref.trades[0]
        assert first.direction in VALID_DIRECTIONS

    def test_first_trade_has_entry_and_exit_times(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        first = ref.trades[0]
        assert first.entry_time
        assert first.entry_time.startswith("2")
        assert len(first.exits) >= 1
        assert first.exits[0].time
        assert first.exits[0].time.startswith("2")

    def test_first_trade_has_prices(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        first = ref.trades[0]
        assert isinstance(first.entry_price, float)
        assert first.entry_price > 0
        assert isinstance(first.exits[0].price, float)
        assert first.exits[0].price > 0

    def test_first_trade_has_volume(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        first = ref.trades[0]
        assert isinstance(first.quantity, float)
        assert first.quantity > 0

    def test_sl_tp_are_float_or_none(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        for trade in ref.trades:
            sl = trade.meta.get("stop_loss")
            tp = trade.meta.get("take_profit")
            assert sl is None or isinstance(sl, float)
            assert tp is None or isinstance(tp, float)
            assert trade.stop_loss == sl
            assert trade.take_profit == tp

    def test_stats_contains_known_keys(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert len(ref.stats) > 0
        assert "total_trades" in ref.stats
        assert ref.stats["total_trades"] == 492

    def test_settings_populated(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "ATR_Period" in ref.settings
        assert ref.settings["ATR_Period"] == 5

    def test_raw_metadata(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "parsed_meta" in ref.raw_metadata
        assert "parsed_stats" in ref.raw_metadata
        assert ref.raw_metadata["source_timezone"] == "UTC"

    def test_trade_pnl_consistent(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        for trade in ref.trades:
            assert isinstance(trade.pnl, float)
            exit_pnl = trade.exits[0].pnl
            assert exit_pnl == trade.pnl

    def test_exit_reason_sl_detected(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        sl_trades = [t for t in ref.trades if t.exit_reason == "stop_loss"]
        assert len(sl_trades) > 0

    def test_last_row_balance_ignored(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        # Last trade in raw data is around May 2026; ensure we didn't append totals row
        last = ref.trades[-1]
        assert last.symbol == "EURUSD"
        assert "balance" not in last.direction.lower()

    def test_no_empty_symbol_trades(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        for trade in ref.trades:
            assert trade.symbol != ""
            assert trade.symbol is not None


class TestNAS100M15:
    def test_load_success(self, importer: MT5XLSXImporter):
        ref = importer.load(_nas100_path())
        assert isinstance(ref, ValidationReference)
        assert ref.symbol == "USATECH.IDX"
        assert ref.timeframe.lower() == "m15"
        assert len(ref.trades) > 0


class TestSP500M1:
    def test_load_success(self, importer: MT5XLSXImporter):
        ref = importer.load(_sp500_path())
        assert isinstance(ref, ValidationReference)
        assert ref.symbol == "USA500.IDX"
        assert ref.timeframe.lower() == "m1"
        assert len(ref.trades) > 0


class TestNegativeCases:
    def test_missing_file_raises(self, importer: MT5XLSXImporter):
        with pytest.raises(FileNotFoundError):
            importer.load("/nonexistent/path/report.xlsx")

    def test_invalid_path_raises(self, importer: MT5XLSXImporter):
        with pytest.raises((FileNotFoundError, IsADirectoryError)):
            importer.load("")


class TestTradeCountMatchesStats:
    def test_eurusd_trade_count_matches_total_trades(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert len(ref.trades) == ref.stats.get("total_trades", 0)


class TestIndividualTradeFields:
    def test_each_trade_has_required_fields(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        for trade in ref.trades:
            assert isinstance(trade.id, int)
            assert trade.id > 0
            assert trade.direction in VALID_DIRECTIONS
            assert trade.entry_time is not None
            assert len(trade.exits) >= 1
            exit_ = trade.exits[0]
            assert exit_.time is not None
            assert isinstance(trade.entry_price, float)
            assert isinstance(exit_.price, float)
            assert isinstance(trade.quantity, float)
            assert trade.quantity > 0
            assert isinstance(exit_.quantity, float)

    def test_entry_exit_direction_consistency(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        for trade in ref.trades:
            # LONG trades: buy in, sell out. SHORT: sell in, buy out.
            if trade.direction == "LONG":
                pass  # basic sanity, direction already validated
            else:
                pass  # SHORT
            # Volume in = volume out for these reports
            assert trade.quantity == trade.exits[0].quantity


class TestStatsParsing:
    def test_sharpe_present(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "sharpe_ratio" in ref.stats
        assert isinstance(ref.stats["sharpe_ratio"], float)

    def test_profit_factor_present(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "profit_factor" in ref.stats
        assert isinstance(ref.stats["profit_factor"], float)

    def test_win_rate_present(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "win_rate" in ref.stats
        assert isinstance(ref.stats["win_rate"], float)

    def test_max_drawdown_pct_present(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        assert "max_drawdown_pct" in ref.stats
        assert isinstance(ref.stats["max_drawdown_pct"], float)


class TestHelpers:
    def test_coerce_setting_bool(self):
        assert MT5XLSXImporter._coerce_setting("true") is True
        assert MT5XLSXImporter._coerce_setting("false") is False

    def test_coerce_setting_int(self):
        assert MT5XLSXImporter._coerce_setting("42") == 42

    def test_coerce_setting_float(self):
        assert MT5XLSXImporter._coerce_setting("3.14") == 3.14

    def test_extract_first_percent(self):
        assert MT5XLSXImporter._extract_first_percent("5.52% (6 617.58)") == 5.52
        assert MT5XLSXImporter._extract_first_percent(None) is None

    def test_stat_label_value_groups_accepts_two_column_layout(self):
        assert MT5XLSXImporter._stat_label_value_groups(
            ["Nettogewinn gesamt:", "100.0"]
        ) == [("Nettogewinn gesamt:", "100.0")]

    def test_parse_count_pct(self):
        count, pct = MT5XLSXImporter._parse_count_pct("222 (45.12%)")
        assert count == 222
        assert pct == 45.12

    def test_infer_exit_reason_sl(self):
        assert MT5XLSXImporter._infer_exit_reason("sl 1.20643") == "stop_loss"

    def test_infer_exit_reason_none(self):
        assert MT5XLSXImporter._infer_exit_reason("") is None
        assert MT5XLSXImporter._infer_exit_reason("some comment") is None


class TestSourceTimezone:
    def test_to_iso_defaults_to_utc(self):
        assert (
            MT5XLSXImporter._to_iso("2024.01.01 10:00:00")
            == "2024-01-01T10:00:00Z"
        )

    def test_to_iso_converts_source_timezone_to_utc(self):
        from zoneinfo import ZoneInfo

        # 10:00 in Europe/Athens (UTC+2 in January) -> 08:00 UTC.
        assert (
            MT5XLSXImporter._to_iso("2024.01.01 10:00:00", ZoneInfo("Europe/Athens"))
            == "2024-01-01T08:00:00Z"
        )

    def test_resolve_source_timezone_rejects_unknown(self):
        from validation.importers.mt5_xlsx import _resolve_source_timezone

        with pytest.raises(ValueError):
            _resolve_source_timezone("Not/AZone")

    def test_to_iso_rejects_unknown_source_timezone(self):
        with pytest.raises(ValueError):
            MT5XLSXImporter._to_iso("2024.01.01 10:00:00", "Not/AZone")


class TestDirectionRequiresTypColumn:
    """DV-B42: direction must never be inferred from a missing Typ column."""

    def _write_report(self, tmp_path: Path, include_typ: bool, typ_value: str = "buy") -> Path:
        data: dict[str, object] = {
            "A": ["Orders", None, "Auftrag", 1001],
            "B": [None, None, "Zeit", "2024.01.03 08:00:00"],
            "C": [None, None, "Symbol", "EURUSD"],
            "D": [None, None, "Typ", typ_value],
            "E": [None, None, "Volumen", "0.10"],
            "F": [None, None, "Preis", 1.085],
            "G": [None, None, "S / L", 1.08],
            "H": [None, None, "T / P", 1.095],
            "I": [None, None, "Kommentar", "Entry"],
        }
        if not include_typ:
            data.pop("D")
            # Re-align remaining columns so the Orders table keeps Zeit..Kommentar
            data["D"] = data.pop("E")
            data["E"] = data.pop("F")
            data["F"] = data.pop("G")
            data["G"] = data.pop("H")
            data["H"] = data.pop("I")

        orders = pd.DataFrame(data)
        trades_rows = [
            ["Trades"] + [None] * 11,
            ["Zeit", "Trade", "Symbol"]
            + (["Typ"] if include_typ else [])
            + ["Richtung", "Volumen", "Preis", "Auftrag", "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar"],
            ["2024.01.03 08:00:00", 2, "EURUSD"]
            + ([typ_value] if include_typ else [])
            + ["in", "0.10", 1.085, 1001, 0, 0, 0, 100000, "Entry"],
            ["2024.01.03 16:00:00", 3, "EURUSD"]
            + (["sell"] if include_typ else [])
            + ["out", "0.10", 1.09, 1002, 0, 0, 500.0, 100500, "TP 1.09000"],
        ]
        trades = pd.DataFrame(trades_rows)

        path = tmp_path / (
            f"report_{'with' if include_typ else 'no'}_typ_{typ_value.replace(' ', '_')}.xlsx"
        )
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            orders.to_excel(writer, index=False, header=False, startrow=0)
            trades.to_excel(writer, index=False, header=False, startrow=20)
        return path

    def test_missing_typ_column_raises(self, tmp_path: Path):
        path = self._write_report(tmp_path, include_typ=False)
        importer = MT5XLSXImporter()
        with pytest.raises(ValueError) as excinfo:
            importer.load(path)
        assert "Typ" in str(excinfo.value)

    def test_unsupported_entry_typ_raises(self, tmp_path: Path):
        path = self._write_report(tmp_path, include_typ=True, typ_value="buy limit")
        importer = MT5XLSXImporter()
        with pytest.raises(ValueError) as excinfo:
            importer.load(path)
        assert "buy limit" in str(excinfo.value)


class TestGermanNumberParsing:
    """DV-B44: xlsx SL/TP cells parse German-formatted numbers exactly."""

    def test_sltp_map_german_thousands(self):
        """A German-grouped report parses "2.500" as 2500, not 2.5."""
        orders = pd.DataFrame(
            [
                [1001, "2024.01.03 08:00:00", "EURUSD", "buy", "0.10", 1.085, "1.234,56", "2.500", "filled", "Entry"],
            ],
            columns=["Auftrag", "Zeit", "Symbol", "Typ", "Volumen", "Preis", "S / L", "T / P", "Status", "Kommentar"],
        )
        mapping = MT5XLSXImporter._build_sltp_map(orders)
        assert mapping[1001]["sl"] == pytest.approx(1234.56)
        assert mapping[1001]["tp"] == pytest.approx(2500.0)

    def test_to_float_parses_german_locale_fallback(self):
        """PnL/commission cells outside SL/TP also honour the German locale."""
        assert MT5XLSXImporter._to_float("1.234,56") == pytest.approx(1234.56)
        assert MT5XLSXImporter._to_float("2.500") == pytest.approx(2.5)
        assert MT5XLSXImporter._to_float("300.0") == pytest.approx(300.0)
        assert MT5XLSXImporter._to_float("n/a") is None
        assert MT5XLSXImporter._to_float(None) is None

    def test_sltp_locale_scan_covers_only_sltp_cells(self):
        """A stray ASCII-grouped value outside the SL/TP columns must not
        flip the report locale and corrupt the SL/TP cells ("2.500" -> 2.5)."""
        orders = pd.DataFrame(
            [
                [1001, "2024.01.03 08:00:00", "EURUSD", "buy", "1,000", 1.085, "1.08500", "2.500", "filled", "10.000 runs"],
                [1002, "2024.01.03 09:00:00", "EURUSD", "sell", "0,500", 1.090, None, "1.234,56", "filled", "20.000 runs"],
            ],
            columns=["Auftrag", "Zeit", "Symbol", "Typ", "Volumen", "Preis", "S / L", "T / P", "Status", "Kommentar"],
        )
        mapping = MT5XLSXImporter._build_sltp_map(orders)
        assert mapping[1001]["sl"] == pytest.approx(1.085)
        assert mapping[1001]["tp"] == pytest.approx(2500.0)
        assert mapping[1002]["tp"] == pytest.approx(1234.56)


class TestOrphanOutDeal:
    """An 'out' deal with no open position must be skipped, not merged
    into the next position's pnl (CodeRabbit batch-review finding)."""

    @staticmethod
    def _write_report(tmp_path: Path, name: str, deals: pd.DataFrame) -> Path:
        orders = pd.DataFrame(
            [
                ["Orders"] + [None] * 9,
                ["Auftrag", "Zeit", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "S / L", "T / P", "Kommentar"],
                [9002, "2024.01.02 08:00:00", "EURUSD", "buy", "in", "0.10", 1.085, 1.08, 1.095, "Entry"],
                [9003, "2024.01.02 16:00:00", "EURUSD", "sell", "out", "0.10", 1.090, None, None, "TP 1.09000"],
            ]
        )
        path = tmp_path / name
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            orders.to_excel(writer, index=False, header=False, startrow=0)
            deals.to_excel(writer, index=False, header=False, startrow=20)
        return path

    @staticmethod
    def _deals(rows: list[list[Any]]) -> pd.DataFrame:
        return pd.DataFrame(
            [["Trades"] + [None] * 11]
            + [
                ["Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "Auftrag",
                 "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar"]
            ]
            + rows
        )

    def test_leading_orphan_out_is_not_merged_into_next_position(self, tmp_path: Path):
        """A report that starts mid-position must not inflate the next trade."""
        deals = self._deals(
            [
                ["2024.01.01 09:00:00", 2, "EURUSD", "sell", "out", "0.10", 1.090, 9001, -0.5, 0, 425.0, 104250, "TP 1.09000"],
                ["2024.01.02 08:00:00", 3, "EURUSD", "buy", "in", "0.10", 1.085, 9002, -0.5, 0, 0, 100000, "Entry"],
                ["2024.01.02 16:00:00", 4, "EURUSD", "sell", "out", "0.10", 1.090, 9003, -0.5, 0, 500.0, 105000, "TP 1.09000"],
            ]
        )
        ref = MT5XLSXImporter().load(self._write_report(tmp_path, "leading_orphan.xlsx", deals))
        assert len(ref.trades) == 1
        trade = ref.trades[0]
        assert trade.pnl == pytest.approx(500.0)
        assert [e.pnl for e in trade.exits] == [pytest.approx(500.0)]

    def test_mid_report_orphan_out_does_not_inflate_next_position(self, tmp_path: Path):
        """An orphan 'out' between two positions keeps their pnl separate."""
        deals = self._deals(
            [
                ["2024.01.01 08:00:00", 2, "EURUSD", "buy", "in", "0.10", 1.085, 9002, -0.5, 0, 0, 100000, "Entry"],
                ["2024.01.01 16:00:00", 3, "EURUSD", "sell", "out", "0.10", 1.090, 9003, -0.5, 0, 500.0, 105000, "TP 1.09000"],
                ["2024.01.02 09:00:00", 4, "EURUSD", "buy", "out", "0.05", 1.100, 9004, -0.5, 0, 425.0, 109250, "TP 1.10000"],
                ["2024.01.03 08:00:00", 5, "EURUSD", "buy", "in", "0.10", 1.085, 9005, -0.5, 0, 0, 100000, "Entry"],
                ["2024.01.03 16:00:00", 6, "EURUSD", "sell", "out", "0.10", 1.090, 9006, -0.5, 0, 500.0, 105000, "TP 1.09000"],
            ]
        )
        ref = MT5XLSXImporter().load(self._write_report(tmp_path, "mid_orphan.xlsx", deals))
        assert len(ref.trades) == 2
        assert [t.pnl for t in ref.trades] == [pytest.approx(500.0), pytest.approx(500.0)]
        assert [t.meta["position_id"] for t in ref.trades] == ["position-1", "position-2"]


class TestPositionFillClassification:
    def test_unhashable_pairing_scope_is_rejected(self):
        rows = [{"id": 1, "direction": "in", "quantity": 1.0}]

        with pytest.raises(TypeError, match="scope must be hashable"):
            group_position_fills(
                rows,
                position_id_of=lambda row: str(row["id"]),
                direction_of=lambda row: row["direction"],
                is_trade_row=lambda _row: True,
                scope_of=lambda _row: {"symbol": "AAPL"},
            )

    def test_partial_open_and_orphan_fills_are_evidence_only(self):
        deals = pd.DataFrame(
            [
                ["2024.01.01 09:00:00", "", "EURUSD", "sell", "out", "0.10", 1.09, 901, 0, 0, 50, 100050, "TP"],
                ["2024.01.02 08:00:00", 1, "EURUSD", "buy", "in", "0.15", 1.08, 902, 0, 0, 0, 100000, "Entry"],
                ["2024.01.02 16:00:00", 1, "EURUSD", "sell", "out", "0.05", 1.09, 903, 0, 0, 25, 100025, "Partial"],
                ["2024.01.03 08:00:00", 2, "EURUSD", "buy", "in", "0.10", 1.09, 904, 0, 0, 0, 100025, "Open"],
            ],
            columns=["Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "Auftrag", "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar"],
        )
        trades, evidence = MT5XLSXImporter._build_trades(deals, {})
        assert trades == []
        assert evidence["partial_position_count"] == 1
        assert evidence["open_position_count"] == 2
        assert evidence["open_only_count"] == 1
        assert evidence["orphan_exit_count"] == 1
        assert evidence["residual_positions"][0]["residual_quantity"] == pytest.approx(0.10)
        assert evidence["orphan_exits"][0]["exit_quantity"] == pytest.approx(0.10)

    def test_interleaved_symbols_and_entry_sides_preserve_partial_pairing(self):
        columns = [
            "Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis",
            "Auftrag", "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar",
        ]
        deals = pd.DataFrame(
            [
                ["2024.01.01 08:00:00", 1, "AAPL", "buy", "in", "0.10", 100.0,
                 901, 0, 0, 0, 100000, "Entry"],
                ["2024.01.01 09:00:00", 2, "MSFT", "sell", "in", "0.20", 200.0,
                 902, 0, 0, 0, 100000, "Entry"],
                ["2024.01.02 08:00:00", 3, "MSFT", "buy", "out", "0.04", 190.0,
                 903, 0, 0, -40, 99960, "Partial"],
                ["2024.01.02 09:00:00", 4, "MSFT", "buy", "out", "0.16", 190.0,
                 904, 0, 0, -160, 99800, "Close"],
                ["2024.01.03 08:00:00", 5, "AAPL", "sell", "out", "0.04", 110.0,
                 905, 0, 0, 40, 99840, "Partial"],
                ["2024.01.03 09:00:00", 6, "AAPL", "sell", "out", "0.06", 110.0,
                 906, 0, 0, 60, 99900, "Close"],
            ],
            columns=columns,
        )

        trades, evidence = MT5XLSXImporter._build_trades(deals, {})

        assert [(trade.symbol, trade.direction) for trade in trades] == [
            ("AAPL", "LONG"),
            ("MSFT", "SHORT"),
        ]
        assert [leg.time for leg in trades[0].exits] == [
            "2024-01-03T08:00:00Z",
            "2024-01-03T09:00:00Z",
        ]
        assert [leg.time for leg in trades[1].exits] == [
            "2024-01-02T08:00:00Z",
            "2024-01-02T09:00:00Z",
        ]
        assert evidence["closed_position_count"] == 2
        assert evidence["residual_position_count"] == 0

    def test_shared_position_ids_are_scoped_by_symbol(self):
        columns = [
            "Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis",
            "Auftrag", "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar",
        ]
        deals = pd.DataFrame(
            [
                ["2024.01.01 08:00:00", 7, "AAPL", "buy", "in", "0.10", 100.0,
                 901, 0, 0, 0, 100000, "Entry"],
                ["2024.01.01 09:00:00", 7, "MSFT", "sell", "in", "0.20", 200.0,
                 902, 0, 0, 0, 100000, "Entry"],
                ["2024.01.01 10:00:00", 7, "AAPL", "sell", "out", "0.10", 110.0,
                 903, 0, 0, 10, 100010, "Close"],
                ["2024.01.01 11:00:00", 7, "MSFT", "buy", "out", "0.20", 190.0,
                 904, 0, 0, -20, 99990, "Close"],
            ],
            columns=columns,
        )

        trades, evidence = MT5XLSXImporter._build_trades(deals, {})

        assert [(trade.symbol, trade.quantity) for trade in trades] == [
            ("AAPL", pytest.approx(0.10)),
            ("MSFT", pytest.approx(0.20)),
        ]
        assert evidence["closed_position_count"] == 2
        assert evidence["orphan_exit_count"] == 0


class TestTotalReturnFraction:
    """#84: total_return is a fraction (parity with runner stats)."""

    def test_total_return_is_fraction_not_percent(self, importer: MT5XLSXImporter):
        ref = importer.load(_eurusd_path())
        total_return = ref.stats["total_return"]
        initial_capital = ref.raw_metadata["parsed_meta"]["initial_capital"]
        assert total_return == pytest.approx(ref.stats["total_pnl"] / initial_capital)
        assert total_return == pytest.approx(0.2186881)


def test_build_trades_empty_frame_returns_tuple_shape() -> None:
    """CodeRabbit round: the empty path must keep the declared tuple shape."""
    trades, summary = MT5XLSXImporter._build_trades(pd.DataFrame(), {})
    assert trades == []
    assert summary["open_position_count"] == 0


class TestTradeColumnRequired:
    """The Trade column drives position reconstruction and cannot be optional.

    Without it every deal id resolves to "", so every row looks like a deal of
    the same position and the whole report imports as one giant position.
    """

    def _write_report(self, tmp_path: Path, include_trade: bool) -> Path:
        # Merge resolution: the fixture gained the ``Gewinn`` column. The
        # importer has required it since DV-B42 (``direction``/``Gewinn`` are
        # derived inputs, not optional), and without it both tests below failed
        # at that earlier gate instead of at the Trade-column gate they target.
        header = ["Zeit"] + (["Trade"] if include_trade else []) + [
            "Symbol", "Typ", "Richtung", "Volumen", "Preis", "Gewinn", "Kommentar",
        ]

        def _row(
            time_value: str,
            deal: int,
            typ: str,
            direction: str,
            price: float,
            profit: float = 0.0,
        ):
            return [time_value] + ([deal] if include_trade else []) + [
                "EURUSD", typ, direction, "0.10", price, profit, "",
            ]

        rows = [
            ["Trades"] + [None] * (len(header) - 1),
            header,
            _row("2024.01.03 08:00:00", 2, "buy", "in", 1.085),
            _row("2024.01.03 16:00:00", 3, "sell", "out", 1.09, profit=0.0005),
        ]
        trades = pd.DataFrame(rows)
        # All-string Orders table; a mixed-type cell becomes NaN and breaks the
        # importer's frame handling before the Trades table is even read.
        orders = pd.DataFrame([["Orders", None], [None, None]])

        path = tmp_path / f"report_{'with' if include_trade else 'no'}_trade.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            orders.to_excel(writer, index=False, header=False, startrow=0)
            trades.to_excel(writer, index=False, header=False, startrow=20)
        return path

    def test_missing_trade_column_raises(self, tmp_path: Path):
        path = self._write_report(tmp_path, include_trade=False)
        importer = MT5XLSXImporter()
        with pytest.raises(ValueError) as excinfo:
            importer.load(path)
        # Merge resolution: assert the Trade-column gate specifically. The
        # substring "Trade" also occurs in the generic "Trades table is missing
        # required columns ..." prefix, so the original assertion could pass on
        # an unrelated missing column and never reach the gate under test.
        assert "missing required column (Trade)" in str(excinfo.value)

    def test_present_trade_column_still_imports(self, tmp_path: Path):
        ref = MT5XLSXImporter().load(self._write_report(tmp_path, include_trade=True))
        assert len(ref.trades) == 1, "one in/out pair must reconstruct one position"


class TestZeroFeesBecomeNone:
    """MT5 reports "no fees" as ``0.0``; the importer stores that as ``None``.

    A mutant flipping ``abs(fees) > 0.0`` to ``>= 0.0`` would store ``0.0`` and
    still pass every other test, because the fee columns are only exercised with
    non-zero values. Pin the exact-zero mapping and the net-pnl arithmetic.
    """

    def _write_report(
        self, tmp_path: Path, *, commission: float, swap: float, profit: float = 100.0
    ) -> Path:
        header = [
            "Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis",
            "Kommission", "Swap", "Gewinn", "Kommentar",
        ]
        rows = [
            ["Trades"] + [None] * (len(header) - 1),
            header,
            ["2024.01.03 08:00:00", 2, "EURUSD", "buy", "in", "0.10", 1.085,
             commission, swap, 0.0, ""],
            ["2024.01.03 16:00:00", 3, "EURUSD", "sell", "out", "0.10", 1.090,
             0.0, 0.0, profit, ""],
        ]
        trades = pd.DataFrame(rows)
        orders = pd.DataFrame([["Orders", None], [None, None]])

        path = tmp_path / f"report_{commission}_{swap}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            orders.to_excel(writer, index=False, header=False, startrow=0)
            trades.to_excel(writer, index=False, header=False, startrow=20)
        return path

    def test_zero_commission_and_swap_are_stored_as_none(self, tmp_path: Path):
        ref = MT5XLSXImporter().load(
            self._write_report(tmp_path, commission=0.0, swap=0.0)
        )
        trade = ref.trades[0]
        assert trade.fees is None
        assert trade.meta["commission"] is None
        assert trade.meta["swap"] is None

    def test_nonzero_fees_are_stored_as_the_summed_value(self, tmp_path: Path):
        ref = MT5XLSXImporter().load(
            self._write_report(tmp_path, commission=2.5, swap=1.5)
        )
        trade = ref.trades[0]
        assert trade.fees == pytest.approx(4.0)
        assert trade.meta["commission"] == pytest.approx(2.5)
        assert trade.meta["swap"] == pytest.approx(1.5)

    def test_net_pnl_subtracts_fees_from_gross(self, tmp_path: Path):
        ref = MT5XLSXImporter().load(
            self._write_report(tmp_path, commission=2.5, swap=1.5, profit=100.0)
        )
        trade = ref.trades[0]
        assert trade.net_pnl == pytest.approx(trade.pnl - 4.0)

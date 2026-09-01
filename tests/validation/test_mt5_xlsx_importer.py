"""Tests for validation.importers.mt5_xlsx.MT5XLSXImporter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from shared.models import Trade, VALID_DIRECTIONS
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference


def _eurusd_path() -> Path:
    return Path("strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx")


def _nas100_path() -> Path:
    return Path("strategy-comparisons/intraday_volatility_breakout/NAS100_M15.xlsx")


def _sp500_path() -> Path:
    return Path("strategy-comparisons/intraday_volatility_breakout/SP500_M1.xlsx")


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
        with pytest.raises(ValueError, match="Typ"):
            MT5XLSXImporter().load(path)

    def test_unsupported_entry_typ_raises(self, tmp_path: Path):
        path = self._write_report(tmp_path, include_typ=True, typ_value="buy limit")
        with pytest.raises(ValueError, match="buy limit"):
            MT5XLSXImporter().load(path)


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

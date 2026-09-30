"""Tests for validation.importers.mt5_html.MT5HTMLImporter."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from shared.models import VALID_DIRECTIONS
from validation.importers.mt5_html import MT5HTMLImporter
from validation.reference import ValidationReference


def _sample_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_html/sample_report.html"


@pytest.fixture
def importer() -> MT5HTMLImporter:
    return MT5HTMLImporter()


class TestImportFromHTML:
    def test_import_from_html(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert isinstance(ref, ValidationReference)

    def test_source_framework(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert ref.source_framework == "mt5_html"

    def test_strategy_name(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert "ZenomTrader" in ref.strategy_name
        assert "IntradayVolatilityBreakout" in ref.strategy_name

    def test_symbol(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert ref.symbol == "EURUSD"

    def test_timeframe(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert ref.timeframe.lower() == "h1"


class TestTradeCount:
    def test_trade_count(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert len(ref.trades) >= 5


class TestTradeFields:
    def test_trade_fields(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        first = ref.trades[0]
        assert first.entry_time
        assert first.entry_time.startswith("2")
        assert first.last_exit_time
        assert first.last_exit_time.startswith("2")
        assert first.direction in VALID_DIRECTIONS
        assert isinstance(first.entry_price, float)
        assert first.entry_price > 0
        assert isinstance(first.exits[0].price, float)
        assert first.exits[0].price > 0
        assert isinstance(first.quantity, float)
        assert first.quantity > 0

    def test_each_trade_has_required_fields(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
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

    def test_entry_exit_direction_consistency(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        for trade in ref.trades:
            assert trade.quantity == trade.exits[0].quantity

    def test_long_and_short_present(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        longs = [t for t in ref.trades if t.direction == "LONG"]
        shorts = [t for t in ref.trades if t.direction == "SHORT"]
        assert len(longs) > 0
        assert len(shorts) > 0

    def test_win_and_loss_present(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        wins = [t for t in ref.trades if t.pnl > 0]
        losses = [t for t in ref.trades if t.pnl < 0]
        assert len(wins) > 0
        assert len(losses) > 0

    def test_trade_pnl_consistent(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        for trade in ref.trades:
            assert isinstance(trade.pnl, float)
            exit_pnl = trade.exits[0].pnl
            assert exit_pnl == trade.pnl

    def test_exit_reason_detected(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        sl_trades = [t for t in ref.trades if t.exit_reason == "stop_loss"]
        tp_trades = [t for t in ref.trades if t.exit_reason == "take_profit"]
        assert len(sl_trades) > 0
        assert len(tp_trades) > 0

    def test_no_empty_symbol_trades(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        for trade in ref.trades:
            assert trade.symbol != ""
            assert trade.symbol is not None

    def test_sl_tp_are_float_or_none(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        for trade in ref.trades:
            sl = trade.meta.get("stop_loss")
            tp = trade.meta.get("take_profit")
            assert sl is None or isinstance(sl, float)
            assert tp is None or isinstance(tp, float)
            assert trade.stop_loss == sl
            assert trade.take_profit == tp


class TestStatsPresent:
    def test_profit_factor(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert "profit_factor" in ref.stats
        assert isinstance(ref.stats["profit_factor"], float)

    def test_sharpe_ratio(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert "sharpe_ratio" in ref.stats
        assert isinstance(ref.stats["sharpe_ratio"], float)

    def test_win_rate(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert "win_rate" in ref.stats
        assert isinstance(ref.stats["win_rate"], float)

    def test_max_drawdown_pct(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert "max_drawdown_pct" in ref.stats
        assert isinstance(ref.stats["max_drawdown_pct"], float)

    def test_stats_contains_known_keys(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert len(ref.stats) > 0
        assert "total_trades" in ref.stats

    def test_total_trades_matches_trade_count(self, importer: MT5HTMLImporter):
        ref = importer.load(_sample_path())
        assert len(ref.trades) == ref.stats.get("total_trades", 0)


class TestNegativeCases:
    def test_invalid_path_raises(self, importer: MT5HTMLImporter):
        with pytest.raises(FileNotFoundError):
            importer.load("/nonexistent/path/report.html")

    def test_empty_html_file_raises(self, importer: MT5HTMLImporter, tmp_path: Path):
        empty_html = tmp_path / "empty.html"
        empty_html.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            importer.load(empty_html)


class TestHelpers:
    def test_coerce_setting_bool(self):
        assert MT5HTMLImporter._coerce_setting("true") is True
        assert MT5HTMLImporter._coerce_setting("false") is False

    def test_coerce_setting_int(self):
        assert MT5HTMLImporter._coerce_setting("42") == 42

    def test_coerce_setting_float(self):
        assert MT5HTMLImporter._coerce_setting("3.14") == 3.14

    def test_extract_first_percent(self):
        assert MT5HTMLImporter._extract_first_percent("5.52% (6617.58)") == 5.52
        assert MT5HTMLImporter._extract_first_percent("") is None

    def test_parse_count_pct(self):
        count, pct = MT5HTMLImporter._parse_count_pct("3 (60.00%)")
        assert count == 3
        assert pct == 60.0

    def test_infer_exit_reason_sl(self):
        assert MT5HTMLImporter._infer_exit_reason("SL 1.20643") == "stop_loss"

    def test_infer_exit_reason_tp(self):
        assert MT5HTMLImporter._infer_exit_reason("TP 1.21000") == "take_profit"

    def test_infer_exit_reason_none(self):
        assert MT5HTMLImporter._infer_exit_reason("") is None
        assert MT5HTMLImporter._infer_exit_reason("some comment") is None


class TestDirectionRequiresTypColumn:
    """DV-B42: direction must never be inferred from a missing Typ column."""

    def test_missing_typ_column_raises(self, tmp_path: Path):
        report = Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_html/missing_typ.html"
        importer = MT5HTMLImporter()
        with pytest.raises(ValueError) as excinfo:
            importer.load(report)
        assert "Typ" in str(excinfo.value)

    def test_unsupported_entry_typ_raises(self):
        report = Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_html/bad_typ.html"
        importer = MT5HTMLImporter()
        with pytest.raises(ValueError) as excinfo:
            importer.load(report)
        assert "buy limit" in str(excinfo.value)


class TestGermanNumberParsing:
    """DV-B44: German-formatted SL/TP values must parse exactly."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2.500", 2500.0),
            ("1.234,56", 1234.56),
            ("1.234.567,89", 1234567.89),
            ("2.5", 2.5),
            ("1,5", 1.5),
            ("1234", 1234.0),
            ("1 234,56", 1234.56),
            ("-1.234,56", -1234.56),
            ("1.23456", 1.23456),
            # 5-digit fraction (MT5 price cell "1,08500") must stay a price,
            # not be dropped as an unparseable grouping.
            ("1,08500", 1.085),
            ("-1,08500", -1.085),
        ],
    )
    def test_parse_german_number(self, raw: str, expected: float):
        from validation.importers._numbers import parse_german_number

        value = parse_german_number(raw)
        assert value == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "n/a", "1.2.3", "abc"])
    def test_parse_german_number_rejects_garbage(self, raw: str):
        from validation.importers._numbers import parse_german_number

        assert parse_german_number(raw) is None

    def test_to_float_parses_german_locale_fallback(self):
        """PnL/commission cells outside SL/TP also honour the German locale."""
        from validation.importers.mt5_html import MT5HTMLImporter as I

        assert I._to_float(["1.234,56"], 0) == pytest.approx(1234.56)
        assert I._to_float(["1.08500"], 0) == pytest.approx(1.085)
        assert I._to_float(["-499.60"], 0) == pytest.approx(-499.60)
        assert I._to_float(["n/a"], 0) is None

    def test_sltp_map_parses_german_formats(self):
        ref = MT5HTMLImporter().load(
            Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_html/german_sltp.html"
        )
        sl_values = [t.meta.get("stop_loss") for t in ref.trades]
        tp_values = [t.meta.get("take_profit") for t in ref.trades]
        assert 2500.0 in sl_values
        assert 1234.56 in sl_values
        assert 2.5 in tp_values
        assert 1.095 in tp_values


def test_build_trades_empty_table_returns_tuple_shape() -> None:
    """CodeRabbit round: the empty path must keep the declared tuple shape."""
    from bs4 import BeautifulSoup

    empty = BeautifulSoup("<table></table>", "html.parser").find("table")
    trades, summary = MT5HTMLImporter._build_trades(empty, {})
    assert trades == []
    assert summary["open_position_count"] == 0


class TestPositionFillClassification:
    def test_partial_open_and_orphan_fills_are_evidence_only(self):
        table = BeautifulSoup(
            """
            <table><thead><tr>
              <th>Zeit</th><th>Trade</th><th>Symbol</th><th>Typ</th><th>Richtung</th>
              <th>Volumen</th><th>Preis</th><th>Auftrag</th><th>Kommission</th><th>Swap</th>
              <th>Gewinn</th><th>Kommentar</th>
            </tr></thead><tbody>
              <tr>
                <td>2024.01.01 09:00:00</td><td></td><td>EURUSD</td>
                <td>sell</td><td>out</td><td>0.10</td><td>1.09</td>
                <td>901</td><td>0</td><td>0</td><td>50</td><td>TP</td>
              </tr>
              <tr>
                <td>2024.01.02 08:00:00</td><td>1</td><td>EURUSD</td>
                <td>buy</td><td>in</td><td>0.15</td><td>1.08</td>
                <td>902</td><td>0</td><td>0</td><td>0</td><td>Entry</td>
              </tr>
              <tr>
                <td>2024.01.02 16:00:00</td><td>1</td><td>EURUSD</td>
                <td>sell</td><td>out</td><td>0.05</td><td>1.09</td>
                <td>903</td><td>0</td><td>0</td><td>25</td><td>Partial</td>
              </tr>
              <tr>
                <td>2024.01.03 08:00:00</td><td>2</td><td>EURUSD</td>
                <td>buy</td><td>in</td><td>0.10</td><td>1.09</td>
                <td>904</td><td>0</td><td>0</td><td>0</td><td>Open</td>
              </tr>
            </tbody></table>
            """,
            "lxml",
        ).find("table")
        trades, evidence = MT5HTMLImporter._build_trades(table, {})
        assert trades == []
        assert evidence["partial_position_count"] == 1
        assert evidence["open_position_count"] == 2
        assert evidence["open_only_count"] == 1
        assert evidence["orphan_exit_count"] == 1
        assert evidence["residual_positions"][0]["residual_quantity"] == pytest.approx(0.10)
        assert evidence["orphan_exits"][0]["exit_quantity"] == pytest.approx(0.10)

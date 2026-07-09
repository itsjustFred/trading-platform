"""Tests for validation.importers.mt5_html.MT5HTMLImporter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from shared.models import Trade, VALID_DIRECTIONS
from validation.importers.mt5_html import MT5HTMLImporter
from validation.reference import ValidationReference


def _sample_path() -> Path:
    return Path("tests/fixtures/validation/mt5_html/sample_report.html")


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

"""Tests for validation.reporting.generate_markdown_report."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.comparison.anomalies import AnomalyDetector
from validation.comparison.comparison import (
    MatchedTrade,
    TradeComparisonResult,
    TradeComparator,
)
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    FieldDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import MatchKey, TradeMatcher
from validation.reference import ValidationReference
from validation.reporting.json_report import (
    ReportSummary,
    StatDelta,
    ValidationReport,
    generate_json_report,
    generate_markdown_report,
)


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    position_size: float | None = None,
    pnl: float = 50.0,
) -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
        position_size=position_size,
    )


def _make_reference(trades: list[Trade]) -> ValidationReference:
    return ValidationReference(
        strategy_name="test_strategy",
        source_framework="unknown",
        symbol="EURUSD",
        timeframe="1h",
        settings={},
        trades=trades,
        stats={},
        raw_metadata={},
        allow_empty=True,
    )


def _make_platform_result(trades: list[Trade]) -> BacktestResult:
    meta = BacktestMeta(
        id="platform_test",
        created_at="2024-01-01T00:00:00Z",
        engine="test",
        strategies=["test"],
    )
    return BacktestResult(meta=meta, trades=trades, stats={})


# ---------------------------------------------------------------------------
# Basic Markdown output tests
# ---------------------------------------------------------------------------


class TestMarkdownReportBasics:
    def test_generate_markdown_report_returns_string(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert isinstance(md, str)

    def test_markdown_contains_strategy_name(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "test_strategy" in md
        assert "# Validation Report:" in md

    def test_markdown_contains_symbol_and_timeframe(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "EURUSD" in md
        assert "1h" in md

    def test_perfect_match_shows_passed(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "Overall Passed**: Yes" in md or "Overall Passed**: Yes" in md
        assert "Passed**: Yes" in md
        # Conclusion should contain success wording
        assert "All validation checks passed" in md

    def test_missing_trade_shows_in_markdown(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        md = generate_markdown_report(ref, platform)
        assert "Missing**: 1" in md
        assert "missing" in md.lower()
        # Should be in anomalies section
        assert "## Anomalies" in md
        assert "Missing" in md

    def test_extra_trade_shows_in_markdown(self):
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "Extra**: 1" in md
        assert "extra" in md.lower()
        assert "## Anomalies" in md

    def test_price_mismatch_shows_explanation(self):
        ref_trade = _make_trade(trade_id=1, entry_price=1.1000)
        act_trade = _make_trade(trade_id=10, entry_price=1.1050)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "## Trade Deltas" in md
        assert "entry_price" in md.lower() or "Entry" in md
        # Explanation should mention mismatch
        assert "mismatch" in md.lower() or "differs" in md.lower()

    def test_stat_delta_shows_in_markdown(self):
        ref_trade = _make_trade(trade_id=1, pnl=100.0)
        ref_trade.exits[0].pnl = 100.0
        act_trade = _make_trade(trade_id=10, pnl=200.0)
        act_trade.exits[0].pnl = 200.0
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "## Stat Deltas" in md
        assert "net_profit" in md.lower()
        assert "differs" in md.lower() or "mismatch" in md.lower()

    def test_severity_error_highlighted(self):
        ref_trade = _make_trade(trade_id=1, direction="LONG")
        act_trade = _make_trade(trade_id=10, direction="SHORT")
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        # Direction mismatch causes error severity
        assert "error" in md.lower()
        assert "## Anomalies" in md

    def test_markdown_has_summary_section(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "## Summary" in md
        assert "Matched**" in md
        assert "Total Trades**" in md

    def test_markdown_has_trade_deltas_section(self):
        ref_trade = _make_trade(trade_id=1, entry_price=1.1000)
        act_trade = _make_trade(trade_id=10, entry_price=1.1050)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert "## Trade Deltas" in md

    def test_markdown_has_anomalies_section(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        md = generate_markdown_report(ref, platform)
        assert "## Anomalies" in md

    def test_empty_platform_trades_handled(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        md = generate_markdown_report(ref, platform)
        assert isinstance(md, str)
        assert "Overall Passed**: No" in md or "Overall Passed**: No" in md
        assert "Passed**: No" in md
        assert "## Conclusion" in md
        assert "Review required" in md

    def test_markdown_report_from_report_object(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        report = ValidationReport(**data)

        md = generate_markdown_report(report)
        assert isinstance(md, str)
        assert "test_strategy" in md
        assert "EURUSD" in md
        assert "## Summary" in md
        assert "All validation checks passed" in md

    def test_markdown_contains_headings(self):
        ref_trade = _make_trade(trade_id=1, pnl=100.0)
        ref_trade.exits[0].pnl = 100.0
        act_trade = _make_trade(trade_id=10, pnl=200.0, entry_price=1.1050)
        act_trade.exits[0].pnl = 200.0
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        # Check various heading levels
        assert md.startswith("# ")
        assert "## Summary" in md
        assert "## Statistics Summary" in md
        assert "## Trade Deltas" in md
        assert "## Stat Deltas" in md
        assert "## Anomalies" in md
        assert "## Conclusion" in md

    def test_markdown_contains_bullet_lists(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        # Bullet lists start with "- " in markdown
        assert "- **Symbol**" in md
        assert "- **Passed**" in md
        assert "- **Matched**" in md

    def test_no_internal_python_objects_in_markdown(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        # Should not contain Python object representations like <__main__...>
        assert "<object" not in md
        assert "datetime.datetime" not in md
        assert "Decimal(" not in md

    def test_markdown_report_from_report_object_with_failures(self):
        ref_trade = _make_trade(trade_id=1, pnl=100.0)
        ref_trade.exits[0].pnl = 100.0
        act_trade = _make_trade(trade_id=10, pnl=200.0)
        act_trade.exits[0].pnl = 200.0
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        report = ValidationReport(**data)

        md = generate_markdown_report(report)
        assert "## Stat Deltas" in md
        assert "net_profit" in md.lower()
        assert "Review required" in md

    def test_conclusion_recommendation_for_failures(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        md = generate_markdown_report(ref, platform)
        assert "## Conclusion" in md
        assert "Review required" in md
        assert "missing trades" in md.lower()

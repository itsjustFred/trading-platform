"""Tests for the validation regression suite."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.comparison.comparison import TradeComparisonConfig
from validation.comparison.delta_report import FieldDelta, TradeDelta
from validation.comparison.matcher import MatchKey
from validation.importers.mt5_html import MT5HTMLImporter
from validation.reference import ValidationReference
from validation.regression.runner import (
    RegressionConfig,
    RegressionReporter,
    RegressionResult,
    _compare_stats,
    run_regression,
)
from validation.stats.calculator import TradeStatistics, compute_trade_statistics


# ── helpers ──────────────────────────────────────────────────────────


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    pnl: float = 50.0,
) -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity, pnl=pnl)],
        pnl=pnl,
    )


def _make_reference(trades: list[Trade], allow_empty: bool = False) -> ValidationReference:
    return ValidationReference(
        strategy_name="test",
        source_framework="unknown",
        symbol="EURUSD",
        timeframe="1h",
        settings={},
        trades=trades,
        stats={},
        raw_metadata={},
        allow_empty=allow_empty,
    )


def _write_golden(reference: ValidationReference, path: Path) -> None:
    path.write_text(
        json.dumps(reference.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── basic regression tests ───────────────────────────────────────────


class TestPerfectMatch:
    def test_regression_perfect_match(self, tmp_path: Path) -> None:
        trades = [_make_trade(), _make_trade(trade_id=2, pnl=-30.0, exit_price=1.0950)]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        assert result.passed is True
        assert len(result.stats_delta) == len(
            [
                "win_rate",
                "profit_factor",
                "net_profit",
                "gross_profit",
                "gross_loss",
                "max_drawdown",
                "max_drawdown_pct",
                "sharpe_ratio",
                "sortino_ratio",
                "recovery_factor",
            ]
        )
        assert all(d.passed for d in result.stats_delta)
        assert len(result.trade_delta_report.missing_trades) == 0
        assert len(result.trade_delta_report.extra_trades) == 0
        assert result.explanation.startswith("Regression passed")

    def test_regression_stats_identical_empty_delta(self, tmp_path: Path) -> None:
        trades = [_make_trade(), _make_trade(trade_id=2, pnl=-30.0, exit_price=1.0950)]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        stats_issues = [d for d in result.stats_delta if not d.passed]
        assert stats_issues == []


class TestTradeMismatch:
    def test_regression_trade_mismatch(self, tmp_path: Path) -> None:
        ref_trade = _make_trade(entry_price=1.1000)
        act_trade = _make_trade(entry_price=1.2000)
        golden = _make_reference([ref_trade])
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference([act_trade])
        result = run_regression(golden_path, current_data=current)
        assert result.passed is False
        assert any(d.anomaly_type == "mismatch" for d in result.trade_delta_report.matched_trades)

    def test_regression_missing_trade(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(trade_id=1), _make_trade(trade_id=2, pnl=-20.0)]
        act_trades = [ref_trades[0]]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        assert result.passed is False
        assert len(result.trade_delta_report.missing_trades) == 1
        assert result.trade_delta_report.missing_trades[0].anomaly_type == "missing"

    def test_regression_extra_trade(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(trade_id=1)]
        act_trades = [_make_trade(trade_id=1), _make_trade(trade_id=2, pnl=-20.0)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        assert result.passed is False
        assert len(result.trade_delta_report.extra_trades) == 1
        assert result.trade_delta_report.extra_trades[0].anomaly_type == "extra"


class TestStatsMismatch:
    def test_regression_stats_mismatch(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=80.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        assert result.passed is False
        stats_issues = [d for d in result.stats_delta if not d.passed]
        assert len(stats_issues) >= 1
        assert any(d.field == "net_profit" for d in stats_issues)

    def test_regression_trade_and_stats_both_fail(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=80.0, entry_price=1.5000)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        assert result.passed is False
        assert len(result.trade_delta_report.missing_trades) == 1
        assert any(not d.passed for d in result.stats_delta)


# ── reporter tests ───────────────────────────────────────────────────


class TestReporter:
    def test_regression_json_report(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        json_report = RegressionReporter.to_json_report(result)
        parsed = json.loads(json_report)
        assert isinstance(parsed, dict)
        assert parsed["golden_path"] == str(golden_path)
        assert parsed["passed"] is True

    def test_regression_json_report_valid(self, tmp_path: Path) -> None:
        trades = [_make_trade(), _make_trade(trade_id=2, pnl=-10.0)]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        report = RegressionReporter.to_json_report(result)
        parsed = json.loads(report)
        assert "trade_delta_report" in parsed
        assert "stats_delta" in parsed

    def test_regression_markdown_report(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        md = RegressionReporter.to_markdown_report(result)
        assert "# Regression Report" in md
        assert "Trade Comparison" in md
        assert "Statistics Comparison" in md
        assert str(golden_path) in md

    def test_regression_markdown_contains_explanations(self, tmp_path: Path) -> None:
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.2000)
        golden = _make_reference([ref])
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference([act])
        result = run_regression(golden_path, current_data=current)
        md = RegressionReporter.to_markdown_report(result)
        assert "Trade Mismatches" in md
        assert "mismatches" in md.lower() or "differs" in md.lower()

    def test_regression_markdown_contains_missing_trade(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(trade_id=1), _make_trade(trade_id=2)]
        act_trades = [ref_trades[0]]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        md = RegressionReporter.to_markdown_report(result)
        assert "Missing Trades" in md
        assert "missing" in result.explanation.lower()

    def test_regression_markdown_contains_extra_trade(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(trade_id=1)]
        act_trades = [_make_trade(trade_id=1), _make_trade(trade_id=2, pnl=-10.0)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        md = RegressionReporter.to_markdown_report(result)
        assert "Extra Trades" in md
        assert "extra" in result.explanation.lower()

    def test_regression_markdown_contains_stats_deviation(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=80.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current)
        md = RegressionReporter.to_markdown_report(result)
        assert "Statistics Comparison" in md
        assert "Deviations" in md

    def test_regression_reporter_json_is_dict(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        report = RegressionReporter.to_json_report(result)
        data = json.loads(report)
        assert isinstance(data, dict)
        assert "deviations" in data

    def test_regression_reporter_markdown_has_header(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(golden_path, current_data=golden)
        md = RegressionReporter.to_markdown_report(result)
        assert md.startswith("# Regression Report")


# ── tolerance tests ──────────────────────────────────────────────────


class TestTolerances:
    def test_regression_stats_tolerance_pass(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=100.5), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        cfg = RegressionConfig(
            stats_absolute_tolerance={"net_profit": 1.0},
            stats_relative_tolerance={"net_profit": 0.01},
        )
        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current, config=cfg)
        net_profit_delta = next(d for d in result.stats_delta if d.field == "net_profit")
        assert net_profit_delta.passed is True

    def test_regression_stats_tolerance_fail(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=200.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        cfg = RegressionConfig(
            stats_absolute_tolerance={"net_profit": 10.0},
            stats_relative_tolerance={"net_profit": 0.01},
        )
        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current, config=cfg)
        net_profit_delta = next(d for d in result.stats_delta if d.field == "net_profit")
        assert net_profit_delta.passed is False

    def test_regression_relative_tolerance(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=101.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        # DV-B35: every enabled tolerance band must hold, so a rel-band-only
        # check must widen the abs band instead of relying on the OR-rescue
        # the old gate semantics allowed.
        cfg = RegressionConfig(
            stats_relative_tolerance={"net_profit": 0.02},
            stats_absolute_tolerance={"net_profit": 2.0},
        )
        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current, config=cfg)
        net_profit_delta = next(d for d in result.stats_delta if d.field == "net_profit")
        assert net_profit_delta.passed is True

    def test_regression_absolute_tolerance(self, tmp_path: Path) -> None:
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        act_trades = [_make_trade(pnl=105.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        # DV-B35: every configured band must hold (AND).  A 5.0 absolute
        # drift on a 50.0 base is a 9.09% relative drift, so this needs both
        # bands widened — the abs band alone no longer rescues it.
        cfg = RegressionConfig(
            stats_absolute_tolerance={"net_profit": 10.0},
            stats_relative_tolerance={"net_profit": 0.10},
        )
        current = _make_reference(act_trades)
        result = run_regression(golden_path, current_data=current, config=cfg)
        net_profit_delta = next(d for d in result.stats_delta if d.field == "net_profit")
        assert net_profit_delta.passed is True

    def test_regression_zero_band_is_exact_equality(self, tmp_path: Path) -> None:
        """A 0.0 band means exact, so identical values pass and any drift fails."""
        ref_trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        identical = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        drifted = [_make_trade(pnl=105.0), _make_trade(trade_id=2, pnl=-50.0, exit_price=1.0950)]
        golden = _make_reference(ref_trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        cfg = RegressionConfig(
            stats_absolute_tolerance={"net_profit": 0.0},
            stats_relative_tolerance={"net_profit": 0.0},
        )
        identical_result = run_regression(golden_path, current_data=_make_reference(identical), config=cfg)
        drifted_result = run_regression(golden_path, current_data=_make_reference(drifted), config=cfg)
        assert next(d for d in identical_result.stats_delta if d.field == "net_profit").passed is True
        assert next(d for d in drifted_result.stats_delta if d.field == "net_profit").passed is False

    def test_regression_with_custom_trade_config(self, tmp_path: Path) -> None:
        ref_trade = _make_trade(entry_price=1.1000)
        act_trade = _make_trade(entry_price=1.1005)
        golden = _make_reference([ref_trade])
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        cfg = RegressionConfig(trade_config=TradeComparisonConfig(entry_price_pct=0.001))
        current = _make_reference([act_trade])
        result = run_regression(golden_path, current_data=current, config=cfg)
        assert result.trade_delta_report.passed is True


# ── importer / edge tests ────────────────────────────────────────────


class TestImporterAndEdgeCases:
    def test_regression_run_with_importer(self, tmp_path: Path) -> None:
        html_path = Path("tests/fixtures/validation/mt5_html/sample_report.html")
        if not html_path.exists():
            pytest.skip("HTML fixture not available")

        importer = MT5HTMLImporter()
        golden_ref = importer.load(html_path)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden_ref, golden_path)

        result = run_regression(golden_path, importer_class=MT5HTMLImporter)
        assert isinstance(result, RegressionResult)
        assert result.passed is True
        assert len(result.current_reference.trades) == len(golden_ref.trades)

    def test_regression_empty_golden(self, tmp_path: Path) -> None:
        golden = _make_reference([], allow_empty=True)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        current = _make_reference([], allow_empty=True)
        result = run_regression(golden_path, current_data=current)
        assert result.passed is True
        assert result.trade_delta_report.summary["total_expected"] == 0
        assert result.trade_delta_report.summary["total_actual"] == 0

    def test_regression_missing_golden_file(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            run_regression(missing_path, current_data=_make_reference([_make_trade()]))

    def test_regression_neither_importer_nor_data(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        with pytest.raises(ValueError, match="Either importer_class or current_data"):
            run_regression(golden_path)

    def test_regression_golden_path_is_str(self, tmp_path: Path) -> None:
        trades = [_make_trade()]
        golden = _make_reference(trades)
        golden_path = tmp_path / "golden.json"
        _write_golden(golden, golden_path)

        result = run_regression(str(golden_path), current_data=golden)
        assert result.golden_path == str(golden_path)


# ── internal helpers ─────────────────────────────────────────────────


class TestCompareStatsHelper:
    def test_compare_stats_identical(self) -> None:
        trades = [_make_trade(pnl=100.0), _make_trade(trade_id=2, pnl=-50.0)]
        stats = compute_trade_statistics(trades)
        cfg = RegressionConfig()
        deltas = _compare_stats(stats, stats, cfg)
        assert all(d.passed for d in deltas)

    def test_compare_stats_net_profit_deviation(self) -> None:
        t1 = [_make_trade(pnl=100.0)]
        t2 = [_make_trade(pnl=200.0)]
        s1 = compute_trade_statistics(t1)
        s2 = compute_trade_statistics(t2)
        cfg = RegressionConfig()
        deltas = _compare_stats(s1, s2, cfg)
        net_delta = next(d for d in deltas if d.field == "net_profit")
        assert net_delta.passed is False
        assert "net_profit" in net_delta.explanation

    def test_compare_stats_infinity_both(self) -> None:
        s1 = TradeStatistics(profit_factor=float("inf"), recovery_factor=float("inf"))
        s2 = TradeStatistics(profit_factor=float("inf"), recovery_factor=float("inf"))
        cfg = RegressionConfig()
        deltas = _compare_stats(s1, s2, cfg)
        pf = next(d for d in deltas if d.field == "profit_factor")
        rf = next(d for d in deltas if d.field == "recovery_factor")
        assert pf.passed is True
        assert rf.passed is True

    def test_compare_stats_infinity_mismatch(self) -> None:
        s1 = TradeStatistics(profit_factor=float("inf"))
        s2 = TradeStatistics(profit_factor=2.0)
        cfg = RegressionConfig()
        deltas = _compare_stats(s1, s2, cfg)
        pf = next(d for d in deltas if d.field == "profit_factor")
        assert pf.passed is False

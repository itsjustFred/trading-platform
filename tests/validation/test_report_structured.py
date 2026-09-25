"""Tests for validation.reporting.json_report (structured JSON reports)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from tests.validation.data_factory import (
    make_passing_risk_evidence as _make_passing_risk_evidence,
)
from tests.validation.data_factory import (
    make_platform_result as _make_platform_result,
)
from tests.validation.data_factory import (
    make_reference as _make_reference,
)
from tests.validation.data_factory import (
    make_trade as _make_trade,
)
from validation.comparison.anomalies import AnomalyDetector
from validation.comparison.comparison import (
    MatchedTrade,
    TradeComparator,
)
from validation.comparison.delta_report import (
    DeltaReportBuilder,
)
from validation.comparison.matcher import MatchKey
from validation.reporting.json_report import (
    ReportSummary,
    StatDelta,
    ValidationReport,
    generate_markdown_report,
)
from validation.reporting.json_report import (
    generate_json_report as _generate_json_report,
)


# ---------------------------------------------------------------------------
# Basic JSON output tests
# ---------------------------------------------------------------------------


def generate_json_report(*args, **kwargs):
    kwargs.setdefault("risk_delta_report", _make_passing_risk_evidence())
    return _generate_json_report(*args, **kwargs)


class TestJsonReportBasics:
    def test_generate_json_report_returns_valid_json(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([_make_trade(trade_id=10)])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert isinstance(data, dict)

    def test_report_has_required_top_level_keys(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([_make_trade(trade_id=10)])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)

        required = [
            "strategy_name",
            "symbol",
            "timeframe",
            "source_framework",
            "generated_at",
            "passed",
            "trade_summary",
            "stat_summary",
            "trade_deltas",
            "stat_deltas",
            "risk_deltas",
            "anomalies",
            "explanations",
        ]
        for key in required:
            assert key in data, f"Missing key: {key}"

    def test_perfect_match_passed_true(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["passed"] is True
        assert data["trade_passed"] is True
        assert data["stat_passed"] is True
        assert data["risk_passed"] is True
        assert data["trade_summary"]["matched"] == 1
        assert data["trade_summary"]["missing"] == 0
        assert data["trade_summary"]["extra"] == 0

    def test_missing_trade_in_report(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["passed"] is False
        assert data["trade_passed"] is False
        assert data["trade_summary"]["missing"] == 1
        assert data["trade_summary"]["matched"] == 0
        assert len(data["anomalies"]) == 1
        assert data["anomalies"][0]["anomaly_type"] == "missing"

    def test_extra_trade_in_report(self):
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["passed"] is False
        assert data["trade_summary"]["extra"] == 1
        assert len(data["anomalies"]) == 1
        assert data["anomalies"][0]["anomaly_type"] == "extra"

    def test_price_mismatch_in_report(self):
        ref_trade = _make_trade(trade_id=1, entry_price=1.1000)
        act_trade = _make_trade(trade_id=10, entry_price=1.1050)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["trade_summary"]["mismatched"] == 1
        trade_delta = data["trade_deltas"][0]
        assert trade_delta["anomaly_type"] == "mismatch"
        assert trade_delta["severity"] == "error"
        field_deltas = trade_delta["field_deltas"]
        entry_fd = next((fd for fd in field_deltas if fd["field"] == "entry_price"), None)
        assert entry_fd is not None
        assert entry_fd["passed"] is False
        assert "explanation" in entry_fd

    def test_direction_mismatch_is_error(self):
        # Different directions cause trades to not match at all (missing+extra)
        # rather than being classified as a "mismatch".
        ref_trade = _make_trade(trade_id=1, direction="LONG")
        act_trade = _make_trade(trade_id=10, direction="SHORT")
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["passed"] is False
        assert data["trade_summary"]["matched"] == 0
        assert data["trade_summary"]["missing"] == 1
        assert data["trade_summary"]["extra"] == 1

    def test_stat_delta_in_report(self):
        # Use explicit exit PnL so realized_pnl differs between reference and platform.
        ref_trade = _make_trade(trade_id=1, pnl=100.0)
        ref_trade.exits[0].pnl = 100.0
        act_trade = _make_trade(trade_id=10, pnl=200.0)
        act_trade.exits[0].pnl = 200.0
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        stat_deltas = data["stat_deltas"]
        assert len(stat_deltas) > 0
        net_profit_delta = next(
            (sd for sd in stat_deltas if sd["field"] == "net_profit"), None
        )
        assert net_profit_delta is not None
        assert net_profit_delta["passed"] is False
        assert net_profit_delta["expected"] == 100.0
        assert net_profit_delta["actual"] == 200.0
        assert "explanation" in net_profit_delta
        assert "severity" in net_profit_delta

    def test_report_summary_counts(self):
        ref1 = _make_trade(trade_id=1)
        ref2 = _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z")
        act1 = _make_trade(trade_id=10)
        act2 = _make_trade(trade_id=20, entry_time="2024-01-03T10:00:00Z")
        ref = _make_reference([ref1, ref2])
        platform = _make_platform_result([act1, act2])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        trade_summary = data["trade_summary"]
        assert trade_summary["matched"] == 1
        assert trade_summary["missing"] == 1
        assert trade_summary["extra"] == 1
        assert trade_summary["errors"] + trade_summary["warnings"] >= 0

    def test_report_serializes_datetimes(self):
        ref = _make_reference([_make_trade()])
        platform = _make_platform_result([_make_trade()])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        generated_at = data["generated_at"]
        assert isinstance(generated_at, str)
        # Should be a valid ISO-8601 string
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        assert isinstance(dt, datetime)

    def test_empty_platform_trades(self):
        ref = _make_reference([_make_trade(trade_id=1)])
        platform = _make_platform_result([])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        assert data["passed"] is False
        assert data["trade_summary"]["missing"] == 1
        # C4: missing flips the readiness missing-trades gate — error.
        assert data["anomalies"][0]["severity"] == "error"

    def test_platform_result_as_validation_reference(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform_ref = _make_reference([act_trade])

        json_str = generate_json_report(ref, platform_ref)
        data = json.loads(json_str)
        assert data["passed"] is True
        assert data["trade_summary"]["matched"] == 1

    def test_with_precomputed_trade_delta_report(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10, entry_price=1.1050)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        matched = MatchedTrade(
            reference=ref_trade,
            actual=act_trade,
            key=MatchKey(
                entry_time=ref_trade.entry_time,
                exit_time=ref_trade.last_exit_time,
                symbol=ref_trade.symbol,
                direction=ref_trade.direction,
            ),
        )
        comp = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp])
        tdr = DeltaReportBuilder.build(ref, platform, [comp], anomalies)

        json_str = generate_json_report(ref, platform, trade_delta_report=tdr)
        data = json.loads(json_str)
        assert data["trade_summary"]["mismatched"] == 1
        assert data["passed"] is False

    def test_with_precomputed_stats_delta(self):
        ref = _make_reference([_make_trade(trade_id=1, pnl=100.0)])
        platform = _make_platform_result([_make_trade(trade_id=10, pnl=200.0)])

        stats_delta = [
            StatDelta(
                field="net_profit",
                expected=100.0,
                actual=200.0,
                absolute_diff=100.0,
                relative_diff=0.5,
                passed=False,
                explanation="net_profit differs significantly",
                severity="warning",
            ),
            StatDelta(
                field="win_rate",
                expected=1.0,
                actual=1.0,
                passed=True,
                explanation="win_rate matches",
                severity="info",
            ),
        ]

        json_str = generate_json_report(ref, platform, stats_delta=stats_delta)
        data = json.loads(json_str)
        assert data["stat_summary"]["mismatched"] == 1
        assert data["stat_summary"]["matched"] == 1
        assert data["stat_passed"] is False

    def test_invalid_platform_input_raises(self):
        ref = _make_reference([_make_trade()])
        with pytest.raises((TypeError, AttributeError)):
            # Passing a string is invalid
            generate_json_report(ref, "invalid")  # type: ignore[arg-type]

    def test_markdown_report_generation(self):
        ref_trade = _make_trade(trade_id=1)
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])

        md = generate_markdown_report(ref, platform)
        assert isinstance(md, str)
        assert "# Validation Report" in md
        assert "test_strategy" in md
        assert "EURUSD" in md
        assert "Yes" in md or "No" in md

    def test_anomalies_have_explanation_and_severity(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)
        for anomaly in data["anomalies"]:
            assert "explanation" in anomaly
            assert isinstance(anomaly["explanation"], str)
            assert "severity" in anomaly
            assert isinstance(anomaly["severity"], str)

    def test_no_internal_python_objects(self):
        ref = _make_reference([_make_trade()])
        platform = _make_platform_result([_make_trade()])

        json_str = generate_json_report(ref, platform)
        data = json.loads(json_str)

        def _check(obj: Any) -> None:
            if isinstance(obj, dict):
                for v in obj.values():
                    _check(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check(item)
            else:
                assert obj is None or isinstance(
                    obj, (str, int, float, bool)
                ), f"Non-JSON-serializable type: {type(obj).__name__} = {obj!r}"

        _check(data)

    def test_report_model_directly(self):
        report = ValidationReport(
            strategy_name="s",
            symbol=" sym",
            timeframe="1h",
            source_framework="unknown",
            passed=True,
            trade_summary=ReportSummary(matched=2, missing=1),
        )
        d = report.model_dump(mode="json")
        assert d["strategy_name"] == "s"
        assert d["trade_summary"]["matched"] == 2
        assert d["trade_summary"]["missing"] == 1


class TestStrictJsonCompatibility:
    def test_sanitize_json_numbers_replaces_non_finite(self):
        from validation.reporting.json_report import _sanitize_json_numbers

        raw = {
            "a": float("inf"),
            "b": float("-inf"),
            "c": float("nan"),
            "d": 1.5,
            "nested": {"x": float("inf")},
            "list": [float("nan"), 2, 3.0],
        }
        clean = _sanitize_json_numbers(raw)
        assert clean["a"] is None
        assert clean["b"] is None
        assert clean["c"] is None
        assert clean["d"] == 1.5
        assert clean["nested"]["x"] is None
        assert clean["list"] == [None, 2, 3.0]
        text = json.dumps(clean, allow_nan=False)
        assert "Infinity" not in text
        assert "NaN" not in text

    def test_report_with_infinite_profit_factor_is_strict_json(self):
        # A platform result with only winners yields profit_factor == inf; the
        # report must still be strictly parseable (no NaN/Infinity tokens).
        ref = _make_reference([_make_trade(trade_id=1, pnl=50.0)])
        platform = _make_platform_result([_make_trade(trade_id=1, pnl=50.0)])

        json_str = generate_json_report(ref, platform)
        assert "Infinity" not in json_str
        assert "NaN" not in json_str
        # Strict parser (json.loads rejects NaN/Infinity only with parse hooks,
        # so assert via allow_nan=False round-trip instead).
        data = json.loads(json_str)
        json.dumps(data, allow_nan=False)

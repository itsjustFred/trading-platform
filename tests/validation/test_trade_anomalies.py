"""Tests for validation.comparison.anomalies.AnomalyDetector."""

from __future__ import annotations

from shared.models import Trade
from tests.validation.data_factory import (
    make_reference as _make_reference,
)
from tests.validation.data_factory import (
    make_trade as _make_trade,
)
from validation.comparison.anomalies import (
    AnomalyDetector,
    AnomalyType,
)
from validation.comparison.comparison import (
    MatchedTrade,
    TradeComparator,
)
from validation.comparison.matcher import MatchKey


def _make_matched(
    ref: Trade | None = None,
    actual: Trade | None = None,
) -> MatchedTrade:
    r = ref or _make_trade()
    a = actual or _make_trade()
    key = MatchKey(
        entry_time=r.entry_time,
        exit_time=r.last_exit_time,
        symbol=r.symbol,
        direction=r.direction,
    )
    return MatchedTrade(reference=r, actual=a, key=key)


class TestDetectMissing:
    def test_single_missing_trade(self):
        ref = [_make_trade(trade_id=1), _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z")]
        act = [_make_trade(trade_id=10)]
        anomalies = AnomalyDetector.detect_missing(ref, act)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.MISSING_TRADE
        assert anomalies[0].expected_trade is not None
        assert anomalies[0].expected_trade.id == 2
        assert anomalies[0].actual_trade is None

    def test_multiple_missing_trades(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z"),
            _make_trade(trade_id=3, entry_time="2024-01-03T10:00:00Z"),
        ]
        act = [_make_trade(trade_id=10, entry_time="2024-01-02T10:00:00Z")]
        anomalies = AnomalyDetector.detect_missing(ref, act)
        assert len(anomalies) == 2
        assert {a.expected_trade.id for a in anomalies} == {1, 3}

    def test_missing_severity_is_error(self):
        # C4: missing flips the readiness gate — error, not warning.
        ref = [_make_trade(trade_id=1)]
        act = []
        anomalies = AnomalyDetector.detect_missing(ref, act)
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"

    def test_missing_message_contains_details(self):
        ref = [_make_trade(trade_id=1, symbol="GBPUSD", direction="SHORT")]
        act = []
        anomalies = AnomalyDetector.detect_missing(ref, act)
        msg = anomalies[0].message
        assert "GBPUSD" in msg
        assert "SHORT" in msg
        assert "id=1" in msg

    def test_empty_platform_all_missing(self):
        ref = [_make_trade(trade_id=1), _make_trade(trade_id=2)]
        act = []
        anomalies = AnomalyDetector.detect_missing(ref, act)
        assert len(anomalies) == 2
        assert all(a.anomaly_type == AnomalyType.MISSING_TRADE for a in anomalies)


class TestDetectExtra:
    def test_single_extra_trade(self):
        ref = [_make_trade(trade_id=1)]
        act = [_make_trade(trade_id=10), _make_trade(trade_id=20, entry_time="2024-01-02T10:00:00Z")]
        anomalies = AnomalyDetector.detect_extra(ref, act)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.EXTRA_TRADE
        assert anomalies[0].actual_trade is not None
        assert anomalies[0].actual_trade.id == 20
        assert anomalies[0].expected_trade is None

    def test_multiple_extra_trades(self):
        ref = [_make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z")]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=20, entry_time="2024-01-02T10:00:00Z"),
            _make_trade(trade_id=30, entry_time="2024-01-03T10:00:00Z"),
        ]
        anomalies = AnomalyDetector.detect_extra(ref, act)
        assert len(anomalies) == 2
        assert {a.actual_trade.id for a in anomalies} == {20, 30}

    def test_extra_severity_is_error(self):
        # C4: phantom trades flip the readiness gate — error, not info.
        ref = []
        act = [_make_trade(trade_id=10)]
        anomalies = AnomalyDetector.detect_extra(ref, act)
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"

    def test_extra_message_contains_details(self):
        ref = []
        act = [_make_trade(trade_id=10, symbol="GBPUSD", direction="SHORT")]
        anomalies = AnomalyDetector.detect_extra(ref, act)
        msg = anomalies[0].message
        assert "GBPUSD" in msg
        assert "SHORT" in msg
        assert "id=10" in msg

    def test_empty_reference_all_extra(self):
        ref = []
        act = [_make_trade(trade_id=10), _make_trade(trade_id=20)]
        anomalies = AnomalyDetector.detect_extra(ref, act)
        assert len(anomalies) == 2
        assert all(a.anomaly_type == AnomalyType.EXTRA_TRADE for a in anomalies)


class TestNoAnomalies:
    def test_identical_trades_no_anomalies(self):
        ref = [_make_trade(trade_id=1)]
        act = [_make_trade(trade_id=10)]
        reference = _make_reference(ref)
        result = AnomalyDetector.detect(reference, act)
        missing = [a for a in result if a.anomaly_type == AnomalyType.MISSING_TRADE]
        extra = [a for a in result if a.anomaly_type == AnomalyType.EXTRA_TRADE]
        mismatch = [a for a in result if a.anomaly_type == AnomalyType.MISMATCHED_TRADE]
        assert missing == []
        assert extra == []
        assert mismatch == []

    def test_empty_both_sides_no_anomalies(self):
        ref = []
        act = []
        reference = _make_reference(ref)
        result = AnomalyDetector.detect(reference, act)
        assert result == []


class TestDetectMismatches:
    def test_entry_price_mismatch(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        assert not comp_result.is_match
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.MISMATCHED_TRADE
        assert anomalies[0].matched_trade == matched
        assert "entry_price" in anomalies[0].message

    def test_direction_mismatch_severity_error(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "direction" in anomalies[0].message

    def test_pnl_mismatch_severity_error(self):
        ref = _make_trade(pnl=50.0)
        act = _make_trade(pnl=60.0)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "pnl" in anomalies[0].message

    def test_entry_time_mismatch_severity_warning(self):
        ref = _make_trade(entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(entry_time="2024-01-01T10:02:00Z")
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "entry_time" in anomalies[0].message

    def test_exit_time_mismatch_severity_warning(self):
        ref = _make_trade(exit_time="2024-01-01T11:00:00Z")
        act = _make_trade(exit_time="2024-01-01T11:02:00Z")
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "exit_time" in anomalies[0].message

    def test_volume_mismatch_severity_warning(self):
        ref = _make_trade(quantity=1000.0)
        act = _make_trade(quantity=1050.0)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "volume" in anomalies[0].message

    def test_stop_loss_mismatch_severity_warning(self):
        ref = _make_trade(stop_loss=1.0900)
        act = _make_trade(stop_loss=1.0910)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "stop_loss" in anomalies[0].message

    def test_take_profit_mismatch_severity_warning(self):
        ref = _make_trade(take_profit=1.1100)
        act = _make_trade(take_profit=1.1110)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "take_profit" in anomalies[0].message

    def test_multiple_fields_error_takes_precedence(self):
        ref = _make_trade(direction="LONG", entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(direction="SHORT", entry_time="2024-01-01T10:02:00Z")
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        mismatched_fields = {fm.field for fm in anomalies[0].field_mismatches}
        assert "direction" in mismatched_fields
        assert "entry_time" in mismatched_fields

    def test_field_mismatches_populated(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies[0].field_mismatches) == 1
        assert anomalies[0].field_mismatches[0].field == "entry_price"
        assert anomalies[0].field_mismatches[0].passed is False

    def test_exact_match_produces_no_mismatch_anomaly(self):
        ref = _make_trade()
        act = _make_trade()
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert anomalies == []

    def test_exit_price_mismatch_severity_warning(self):
        ref = _make_trade(exit_price=1.1050)
        act = _make_trade(exit_price=1.1060)
        matched = _make_matched(ref, act)
        comp_result = TradeComparator().compare(matched)
        anomalies = AnomalyDetector.detect_mismatches([comp_result])
        assert len(anomalies) == 1
        assert anomalies[0].severity == "error"
        assert "exit_price" in anomalies[0].message


class TestDetectDuplicates:
    def test_duplicate_reference_trade(self):
        ref = _make_trade(trade_id=1)
        act1 = _make_trade(trade_id=10)
        act2 = _make_trade(trade_id=20)
        matches = [_make_matched(ref, act1), _make_matched(ref, act2)]
        anomalies = AnomalyDetector.detect_duplicates(matches)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.DUPLICATE_MATCH
        assert anomalies[0].expected_trade is not None
        assert anomalies[0].expected_trade.id == 1
        assert "matched 2 times" in anomalies[0].message

    def test_duplicate_platform_trade(self):
        ref1 = _make_trade(trade_id=1)
        ref2 = _make_trade(trade_id=2)
        act = _make_trade(trade_id=10)
        matches = [_make_matched(ref1, act), _make_matched(ref2, act)]
        anomalies = AnomalyDetector.detect_duplicates(matches)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.DUPLICATE_MATCH
        assert anomalies[0].actual_trade is not None
        assert anomalies[0].actual_trade.id == 10
        assert "matched 2 times" in anomalies[0].message

    def test_duplicate_severity_is_error(self):
        ref = _make_trade(trade_id=1)
        act1 = _make_trade(trade_id=10)
        act2 = _make_trade(trade_id=20)
        matches = [_make_matched(ref, act1), _make_matched(ref, act2)]
        anomalies = AnomalyDetector.detect_duplicates(matches)
        assert anomalies[0].severity == "error"

    def test_no_duplicates_no_anomaly(self):
        ref1 = _make_trade(trade_id=1)
        ref2 = _make_trade(trade_id=2)
        act1 = _make_trade(trade_id=10)
        act2 = _make_trade(trade_id=20)
        matches = [_make_matched(ref1, act1), _make_matched(ref2, act2)]
        anomalies = AnomalyDetector.detect_duplicates(matches)
        assert anomalies == []


class TestDetectIntegration:
    def test_combined_missing_extra_and_mismatch(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z", entry_price=1.1000),
            _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z"),
        ]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z", entry_price=1.1050),
            _make_trade(trade_id=30, entry_time="2024-01-03T10:00:00Z"),
        ]
        reference = _make_reference(ref)

        # Build comparison results manually for the one matched pair
        matched = _make_matched(ref[0], act[0])
        comp_results = [TradeComparator().compare(matched)]

        anomalies = AnomalyDetector.detect(reference, act, comp_results)
        types = [a.anomaly_type for a in anomalies]
        assert AnomalyType.MISMATCHED_TRADE in types
        assert AnomalyType.MISSING_TRADE in types
        assert AnomalyType.EXTRA_TRADE in types

    def test_high_level_detect_with_no_comparison_results(self):
        ref = [
            _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z"),
        ]
        act = [
            _make_trade(trade_id=10, entry_time="2024-01-01T10:00:00Z"),
            _make_trade(trade_id=30, entry_time="2024-01-03T10:00:00Z"),
        ]
        reference = _make_reference(ref)
        anomalies = AnomalyDetector.detect(reference, act)
        types = {a.anomaly_type for a in anomalies}
        assert AnomalyType.MISSING_TRADE in types
        assert AnomalyType.EXTRA_TRADE in types
        # No comparison results -> no mismatches
        assert AnomalyType.MISMATCHED_TRADE not in types

    def test_high_level_detect_all_matches_perfect(self):
        ref = [_make_trade(trade_id=1)]
        act = [_make_trade(trade_id=10)]
        reference = _make_reference(ref)
        matched = _make_matched(ref[0], act[0])
        comp_results = [TradeComparator().compare(matched)]
        anomalies = AnomalyDetector.detect(reference, act, comp_results)
        assert anomalies == []

    def test_sl_mismatch_in_full_detect(self):
        ref = [_make_trade(trade_id=1, stop_loss=1.0900)]
        act = [_make_trade(trade_id=10, stop_loss=1.0910)]
        reference = _make_reference(ref)
        matched = _make_matched(ref[0], act[0])
        comp_results = [TradeComparator().compare(matched)]
        anomalies = AnomalyDetector.detect(reference, act, comp_results)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.MISMATCHED_TRADE
        assert anomalies[0].severity == "error"
        assert any(fm.field == "stop_loss" for fm in anomalies[0].field_mismatches)

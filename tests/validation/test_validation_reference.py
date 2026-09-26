"""Tests for validation.reference.ValidationReference."""

from __future__ import annotations

from datetime import datetime

import pytest

from shared.models import BacktestResult, EquityPoint, Trade, TradeExit
from validation.reference import (
    ALLOWED_FRAMEWORKS,
    ALLOWED_STATUSES,
    ValidationReference,
)


def _make_sample_trade() -> Trade:
    return Trade(
        id=1,
        symbol="EURUSD",
        direction="LONG",
        entry_time="2024-01-01T10:00:00Z",
        entry_price=1.1000,
        quantity=1000,
        exits=[TradeExit(time="2024-01-01T11:00:00Z", price=1.1050, quantity=1000)],
        pnl=50.0,
    )


def _make_minimal_valid_reference(**overrides) -> ValidationReference:
    defaults = {
        "strategy_name": "test_strategy",
        "source_framework": "mt5_xlsx",
        "symbol": "EURUSD",
        "timeframe": "1h",
        "settings": {"period": 14},
        "trades": [_make_sample_trade()],
        "stats": {"total_trades": 1, "win_rate": 100.0},
        "raw_metadata": {"sheet": "Report"},
    }
    defaults.update(overrides)
    return ValidationReference(**defaults)


class TestMinimalValid:
    def test_create_minimal(self):
        ref = _make_minimal_valid_reference()
        assert ref.strategy_name == "test_strategy"
        assert ref.source_framework == "mt5_xlsx"
        assert ref.symbol == "EURUSD"
        assert ref.timeframe == "1h"
        assert len(ref.trades) == 1
        assert ref.validation_status == "pending"
        assert isinstance(ref.imported_at, datetime)

    def test_defaults(self):
        ref = _make_minimal_valid_reference()
        assert ref.source_path is None
        assert ref.source_hash is None
        assert ref.allow_empty is False


class TestFieldValidation:
    def test_source_framework_must_be_allowed(self):
        for fw in ALLOWED_FRAMEWORKS:
            ref = _make_minimal_valid_reference(source_framework=fw)
            assert ref.source_framework == fw

        with pytest.raises(ValueError):
            _make_minimal_valid_reference(source_framework="not_allowed")

    def test_validation_status_must_be_allowed(self):
        for status in ALLOWED_STATUSES:
            ref = _make_minimal_valid_reference(validation_status=status)
            assert ref.validation_status == status

        with pytest.raises(ValueError):
            _make_minimal_valid_reference(validation_status="bogus")


class TestTradesValidation:
    def test_trades_must_not_be_empty_by_default(self):
        with pytest.raises(ValueError):
            _make_minimal_valid_reference(trades=[])

    def test_trades_may_be_empty_when_allowed(self):
        ref = _make_minimal_valid_reference(trades=[], allow_empty=True)
        assert ref.trades == []
        assert ref.allow_empty is True


class TestToBacktestResult:
    def test_returns_backtest_result(self):
        ref = _make_minimal_valid_reference()
        result = ref.to_backtest_result()
        assert isinstance(result, BacktestResult)

    def test_meta_populated(self):
        ref = _make_minimal_valid_reference()
        result = ref.to_backtest_result()
        assert result.meta.engine == "mt5_xlsx"
        assert result.meta.strategies == ["test_strategy"]
        assert result.meta.data["symbols"] == ["EURUSD"]
        assert result.meta.data["timeframes"] == ["1h"]
        assert result.meta.parameters == {"period": 14}

    def test_settings_initial_capital_precedes_parsed_metadata(self):
        ref = _make_minimal_valid_reference(
            settings={"initial_capital": 12_345},
            raw_metadata={"parsed_meta": {"initial_capital": 67_890}},
        )

        result = ref.to_backtest_result()

        assert result.meta.initial_capital == pytest.approx(12_345.0)

    def test_parsed_metadata_supplies_missing_initial_capital(self):
        ref = _make_minimal_valid_reference(
            settings={},
            raw_metadata={"parsed_meta": {"initial_capital": 67_890}},
        )

        result = ref.to_backtest_result()

        assert result.meta.initial_capital == pytest.approx(67_890.0)

    @pytest.mark.parametrize(
        "invalid_capital",
        [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
    )
    def test_invalid_initial_capital_uses_default_fallback(self, invalid_capital):
        from validation.reference import resolve_initial_capital

        ref = _make_minimal_valid_reference(
            settings={"initial_capital": invalid_capital},
            raw_metadata={},
        )

        assert resolve_initial_capital(ref) == pytest.approx(100_000.0)

    def test_trades_preserved(self):
        ref = _make_minimal_valid_reference()
        result = ref.to_backtest_result()
        assert len(result.trades) == 1
        assert result.trades[0].symbol == "EURUSD"

    def test_stats_preserved(self):
        ref = _make_minimal_valid_reference(stats={"foo": "bar"})
        result = ref.to_backtest_result()
        assert result.stats == {"foo": "bar"}

    def test_empty_lists_for_missing_data(self):
        ref = _make_minimal_valid_reference()
        result = ref.to_backtest_result()
        assert result.equity_curve == []
        assert result.indicators == []
        assert result.drawings == []

    def test_missing_equity_evidence_is_explicit(self):
        ref = _make_minimal_valid_reference()
        result = ref.to_backtest_result()
        assert ref.equity_evidence_status == "missing"
        assert result.equity_curve == []
        assert result.equity_evidence_status == "missing"

    def test_equity_curve_is_preserved_with_available_status(self):
        curve = [EquityPoint(time="2024-01-01T00:00:00Z", equity=100000.0)]
        ref = _make_minimal_valid_reference(equity_curve=curve)
        result = ref.to_backtest_result()
        assert ref.equity_evidence_status == "available"
        assert result.equity_curve == curve
        assert result.equity_evidence_status == "available"

    def test_available_status_requires_curve(self):
        with pytest.raises(ValueError, match="requires an equity curve"):
            _make_minimal_valid_reference(equity_evidence_status="available")

    def test_notes_contains_source_info(self):
        ref = _make_minimal_valid_reference(source_path="/tmp/report.xlsx")
        result = ref.to_backtest_result()
        assert "mt5_xlsx" in result.meta.notes
        assert "/tmp/report.xlsx" in result.meta.notes


class TestRawMetadataRoundtrip:
    def test_raw_metadata_preserved(self):
        raw = {"custom_key": [1, 2, 3], "nested": {"a": "b"}}
        ref = _make_minimal_valid_reference(raw_metadata=raw)
        assert ref.raw_metadata == raw

    def test_raw_metadata_in_dict(self):
        raw = {"custom_key": [1, 2, 3]}
        ref = _make_minimal_valid_reference(raw_metadata=raw)
        d = ref.model_dump()
        assert d["raw_metadata"] == raw

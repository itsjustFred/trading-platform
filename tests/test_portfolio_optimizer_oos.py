"""Tests for portfolio optimizer OOS (Out-of-Sample) validation feature."""
import pytest
import numpy as np
from runner.portfolio_optimizer import (
    _split_equity_curves_by_time,
    StrategyEquityPoint,
    _parse_time_to_ts,
)


def test_strategy_equity_point_creation() -> None:
    """Test basic equity point creation."""
    pt = StrategyEquityPoint(time=1700000000, equity=100000.0)
    assert pt.time == 1700000000
    assert pt.equity == 100000.0


def test_parse_time_to_ts_unix() -> None:
    """Test parsing Unix timestamp."""
    ts = _parse_time_to_ts(1700000000)
    assert ts == 1700000000.0


def test_parse_time_to_ts_iso() -> None:
    """Test parsing ISO format datetime."""
    ts = _parse_time_to_ts("2024-01-01T00:00:00Z")
    # Should be around Jan 1 2024 (inclusive check)
    assert 1704067200 <= ts <= 1704153600


def test_split_equity_curves_no_test_period() -> None:
    """Test that None test_months returns original curves."""
    curves = [
        [StrategyEquityPoint(time=1, equity=100.0)],
        [StrategyEquityPoint(time=1, equity=100.0)],
    ]
    train, test = _split_equity_curves_by_time(curves, train_months=12, test_months=None)
    assert len(train) == 2
    assert len(test) == 0


def test_split_equity_curves_basic() -> None:
    """Test basic time-based split."""
    # Create curves spanning 18 months
    # Test period: last 3 months (months 15-18)
    # Train period: first 15 months (months 1-15)
    seconds_per_month = 30 * 24 * 60 * 60
    
    # Create timestamps 1-18 months apart
    all_times = [1000 + i * seconds_per_month for i in range(18)]
    
    curves = [
        [StrategyEquityPoint(time=t, equity=100.0 + idx) for idx, t in enumerate(all_times)],
    ]
    
    train_curves, test_curves = _split_equity_curves_by_time(
        curves, train_months=12, test_months=3
    )
    
    # Test period = last 3 months (months 15-18), but split point is max - 3months
    # So with 18 points: 3 months from end means ts >= max - 3months goes to test
    assert len(test_curves[0]) == 4  # Last 4 points (split point includes boundary)
    assert len(train_curves[0]) == 14  # First 14 points


def test_split_equity_curves_empty() -> None:
    """Test handling of empty curves."""
    train, test = _split_equity_curves_by_time([], train_months=12, test_months=3)
    assert train == []
    assert test == []
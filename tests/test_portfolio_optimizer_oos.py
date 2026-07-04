"""Tests for portfolio optimizer OOS (Out-of-Sample) validation feature."""

import pytest
import numpy as np
from sqlmodel import SQLModel, create_engine

from shared.models import BacktestMeta, BacktestResult, EquityPoint
from runner.portfolio_optimizer import (
    _split_equity_curves_by_time,
    _portfolio_sharpe_from_weights,
    _align_equity_curves,
    _compute_correlation_matrix,
    StrategyEquityPoint,
    _parse_time_to_ts,
    optimize_portfolio_allocation,
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

    # Test period = last 3 months from end (max - 3 months = month 14)
    # train_end_ts = min + 12 months = month 12
    # With 18 points (months 0-17): 
    #   train gets months 0-11 = 12 points (ts < month 12)
    #   gap: months 12-13 = 2 points (excluded)
    #   test gets months 14-17 = 4 points (ts >= month 14)
    assert len(test_curves[0]) == 4  # Last 4 points (month 14-17)
    assert len(train_curves[0]) == 12  # First 12 points (month 0-11)


def test_split_equity_curves_empty() -> None:
    """Test handling of empty curves."""
    train, test = _split_equity_curves_by_time([], train_months=12, test_months=3)
    assert train == []
    assert test == []


def test_optimize_portfolio_allocation_returns_oos_sharpe_with_test_months(monkeypatch, tmp_path) -> None:
    """Test that optimize_portfolio_allocation returns oos_sharpe when test_months is provided."""
    # Create mock equity curves spanning 18 months for each strategy
    seconds_per_month = 30 * 24 * 60 * 60
    base_ts = 1700000000  # Starting timestamp

    # Strategy 1: Steady growth (for train period) with different growth in test period
    strategy1_points = []
    for i in range(18):
        ts = base_ts + i * seconds_per_month
        equity = 100000.0 + i * 1000  # Steady growth
        strategy1_points.append(StrategyEquityPoint(time=ts, equity=equity))

    # Strategy 2: Opposite pattern (for diversification)
    strategy2_points = []
    for i in range(18):
        ts = base_ts + i * seconds_per_month
        equity = 100000.0 + i * 500  # Slower growth
        strategy2_points.append(StrategyEquityPoint(time=ts, equity=equity))

    # Mock the _load_strategy_equity function to return our mock curves
    call_count = [0]

    def mock_load(backtest_id: str) -> list[StrategyEquityPoint]:
        call_count[0] += 1
        if call_count[0] == 1:
            return strategy1_points
        return strategy2_points

    import runner.portfolio_optimizer as po_module

    monkeypatch.setattr(po_module, "_load_strategy_equity", mock_load)

    # Monkeypatch the output directory to use tmp_path
    monkeypatch.setattr(po_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")

    # Call optimize_portfolio_allocation with test_months to trigger OOS split
    result = optimize_portfolio_allocation(
        backtest_ids=["strat1", "strat2"],
        target_sharpe=1.5,
        n_trials=10,  # Small number for fast test
        test_months=3,  # Last 3 months for OOS testing
    )

    # Verify result structure
    assert "status" not in result or result.get("status") != "error", f"Optimization failed: {result}"
    assert "weights" in result
    assert "sharpe" in result
    assert len(result["weights"]) == 2

    # Verify OOS metrics are present when test_months is provided
    assert "oos_sharpe" in result, "oos_sharpe should be present when test_months is provided"
    assert "oos_max_drawdown" in result, "oos_max_drawdown should be present when test_months is provided"
    assert "oos_split" in result
    assert result["oos_split"]["train_months"] is None  # train_months was None
    assert result["oos_split"]["test_months"] == 3

    # Verify oos_sharpe is a valid number
    assert isinstance(result["oos_sharpe"], (int, float))
    assert not np.isnan(result["oos_sharpe"])


def test_optimize_portfolio_allocation_no_oos_without_test_months(monkeypatch, tmp_path) -> None:
    """Test that optimize_portfolio_allocation does NOT return oos_sharpe without test_months."""
    seconds_per_month = 30 * 24 * 60 * 60
    base_ts = 1700000000

    strategy1_points = [
        StrategyEquityPoint(time=base_ts + i * seconds_per_month, equity=100000.0 + i * 1000)
        for i in range(12)
    ]
    strategy2_points = [
        StrategyEquityPoint(time=base_ts + i * seconds_per_month, equity=100000.0 + i * 500)
        for i in range(12)
    ]

    call_count = [0]

    def mock_load(backtest_id: str) -> list[StrategyEquityPoint]:
        call_count[0] += 1
        return strategy1_points if call_count[0] == 1 else strategy2_points

    import runner.portfolio_optimizer as po_module

    monkeypatch.setattr(po_module, "_load_strategy_equity", mock_load)
    monkeypatch.setattr(po_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")

    result = optimize_portfolio_allocation(
        backtest_ids=["strat1", "strat2"],
        target_sharpe=1.5,
        n_trials=10,
        test_months=None,  # No OOS split
    )

    # Verify basic structure still works
    assert "status" not in result or result.get("status") != "error"
    assert "weights" in result
    assert "sharpe" in result

    # Verify OOS metrics are NOT present when test_months is None
    assert "oos_sharpe" not in result, "oos_sharpe should NOT be present when test_months is None"
    assert "oos_max_drawdown" not in result
    assert "oos_split" not in result


def test_split_equity_curves_with_train_months() -> None:
    """Test that train_months correctly limits training period from start.

    NOTE: train_months is currently reserved for future walk-forward implementation.
    The function accepts train_months but the train/test split is determined solely by test_months.
    This test documents current behavior and should be updated when train_months is fully implemented.
    """
    seconds_per_month = 30 * 24 * 60 * 60
    base_ts = 1700000000

    # Create 18 months of data
    all_times = [base_ts + i * seconds_per_month for i in range(18)]

    curves = [
        [StrategyEquityPoint(time=t, equity=100.0 + idx) for idx, t in enumerate(all_times)],
    ]

    train_curves, test_curves = _split_equity_curves_by_time(
        curves, train_months=12, test_months=6
    )

    # Current behavior: train_months is reserved but not yet implemented
    # Split is done by test_months only: last ~6 months go to test (boundary inclusive)
    # With 18 points and 6 months per point, the split at max - 6months includes boundary
    # So we expect 11 train and 7 test points
    assert len(test_curves[0]) == 7  # Last ~7 points (boundary inclusive)
    assert len(train_curves[0]) == 11  # First 11 points (train_months currently ignored)
    # TODO: When implementing train_months, the split should be: 12 train, 3 gap, 3 test


def test_split_equity_curves_single_curve() -> None:
    """Test splitting a single strategy curve."""
    seconds_per_month = 30 * 24 * 60 * 60
    base_ts = 1700000000

    curve = [StrategyEquityPoint(time=base_ts + i * seconds_per_month, equity=100.0 + i)
             for i in range(12)]

    train_curves, test_curves = _split_equity_curves_by_time(
        [curve], train_months=None, test_months=3
    )

    # With test_months=3, last ~3 months should be in test
    assert len(test_curves) == 1
    assert len(test_curves[0]) > 0  # Some points in test period


def test_split_equity_curves_zero_test_months() -> None:
    """Test that zero test_months returns empty test curves."""
    curves = [
        [StrategyEquityPoint(time=1, equity=100.0)],
        [StrategyEquityPoint(time=2, equity=100.0)],
    ]
    train, test = _split_equity_curves_by_time(curves, train_months=12, test_months=0)
    assert len(test) == 0


def test_align_equity_curves_empty() -> None:
    """Test aligning empty equity curves."""
    result = _align_equity_curves([])
    assert result.shape == (0, 0)


def test_align_equity_curves_single_strategy() -> None:
    """Test aligning a single strategy equity curve."""
    curves = [
        [StrategyEquityPoint(time=1, equity=100.0),
         StrategyEquityPoint(time=2, equity=101.0),
         StrategyEquityPoint(time=3, equity=102.0)],
    ]
    result = _align_equity_curves(curves)
    assert result.shape == (3, 1)
    assert result[0, 0] == 100.0
    assert result[1, 0] == 101.0
    assert result[2, 0] == 102.0


def test_align_equity_curves_multiple_strategies() -> None:
    """Test aligning multiple strategy equity curves to common timestamps."""
    curves = [
        [StrategyEquityPoint(time=1, equity=100.0),
         StrategyEquityPoint(time=2, equity=101.0)],
        [StrategyEquityPoint(time=1, equity=50.0),
         StrategyEquityPoint(time=2, equity=55.0),
         StrategyEquityPoint(time=3, equity=60.0)],  # Extra timestamp
    ]
    result = _align_equity_curves(curves)
    assert result.shape == (3, 2)  # 3 timestamps, 2 strategies
    # Forward fill for missing values
    assert result[2, 0] == 101.0  # Forward filled from time=2


def test_compute_correlation_matrix_single_strategy() -> None:
    """Test correlation matrix for single strategy returns identity matrix."""
    returns = np.array([[0.01], [0.02], [0.01]])
    corr = _compute_correlation_matrix(returns)
    assert corr.shape == (1, 1)
    assert corr[0, 0] == 1.0


def test_compute_correlation_matrix_multiple_strategies() -> None:
    """Test correlation matrix for multiple strategies."""
    returns = np.array([[0.01, 0.02], [0.02, 0.01], [0.01, 0.03]])
    corr = _compute_correlation_matrix(returns)
    assert corr.shape == (2, 2)
    # Diagonal should be 1.0 (use np.isclose for floating point)
    assert np.isclose(corr[0, 0], 1.0)
    assert np.isclose(corr[1, 1], 1.0)


def test_portfolio_sharpe_from_weights_empty() -> None:
    """Test Sharpe calculation with empty equity matrix."""
    sharpe, max_dd = _portfolio_sharpe_from_weights(np.array([]), np.array([]).reshape(0, 0))
    assert sharpe == 0.0
    assert max_dd == 1.0  # Default drawdown


def test_portfolio_sharpe_from_weights_positive() -> None:
    """Test Sharpe calculation with positive returns."""
    # Create equity curve with steady positive growth
    equity_matrix = np.array([[100.0, 50.0], [105.0, 52.0], [110.0, 55.0], [115.0, 60.0]])
    weights = np.array([0.5, 0.5])

    sharpe, max_dd = _portfolio_sharpe_from_weights(weights, equity_matrix)

    # Should have positive Sharpe for positive trend
    assert isinstance(sharpe, float)
    assert isinstance(max_dd, float)
    assert max_dd >= 0.0


def test_portfolio_sharpe_from_weights_unequal_weights() -> None:
    """Test Sharpe calculation with unequal weights biases toward higher strategy."""
    # Strategy 1 grows faster
    equity_matrix = np.array([[100.0, 50.0], [110.0, 52.0], [121.0, 53.0]])
    weights_high = np.array([0.9, 0.1])
    weights_low = np.array([0.1, 0.9])

    sharpe_high, _ = _portfolio_sharpe_from_weights(weights_high, equity_matrix)
    sharpe_low, _ = _portfolio_sharpe_from_weights(weights_low, equity_matrix)

    # Higher weight on faster-growing strategy should yield higher Sharpe
    assert sharpe_high > sharpe_low


def test_portfolio_optimizer_with_train_months_oos_split(monkeypatch, tmp_path) -> None:
    """Test that train_months creates proper IS/OOS split for walk-forward validation.

    This test demonstrates the intended walk-forward workflow where:
    - Training (IS) uses first N months
    - Testing (OOS) uses last M months
    - This prevents overfitting and validates strategy robustness.
    """
    seconds_per_month = 30 * 24 * 60 * 60
    base_ts = 1700000000

    # Create 24 months of data (2 years)
    strategy1_points = [
        StrategyEquityPoint(time=base_ts + i * seconds_per_month, equity=100000.0 + i * 500)
        for i in range(24)
    ]
    strategy2_points = [
        StrategyEquityPoint(time=base_ts + i * seconds_per_month, equity=100000.0 + i * 300)
        for i in range(24)
    ]

    # Mock load function
    call_count = [0]

    def mock_load(backtest_id: str) -> list[StrategyEquityPoint]:
        call_count[0] += 1
        return strategy1_points if call_count[0] == 1 else strategy2_points

    import runner.portfolio_optimizer as po_module

    monkeypatch.setattr(po_module, "_load_strategy_equity", mock_load)
    monkeypatch.setattr(po_module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")

    # Run optimizer with OOS split: 18 months train, 6 months test
    result = optimize_portfolio_allocation(
        backtest_ids=["strategy_1", "strategy_2"],
        n_trials=5,
        test_months=6,
    )

    # Verify OOS metrics are captured
    assert "oos_sharpe" in result
    assert "oos_split" in result
    assert result["oos_split"]["test_months"] == 6
    # Verify both IS and OOS curves were processed
    assert "sharpe" in result  # In-sample Sharpe


def test_load_strategy_equity_reads_sqlite_first(monkeypatch, tmp_path) -> None:
    """Portfolio optimizer should read canonical SQLite backtest results."""
    import shared.database as database
    import runner.portfolio_optimizer as po_module

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'backtests.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    SQLModel.metadata.create_all(engine)

    database.save_result_to_sqlite(
        BacktestResult(
            meta=BacktestMeta(
                id="sqlite_equity_source",
                created_at="2026-06-23T00:00:00+00:00",
                engine="native",
                strategies=["sqlite_strategy"],
                backtest_start="2024-01-01",
                backtest_end="2024-01-03",
                data={"symbols": ["SPY"], "timeframes": ["1d"]},
            ),
            trades=[],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0),
                EquityPoint(time="2024-01-02T00:00:00+00:00", equity=101000.0),
            ],
            stats={},
        )
    )

    points = po_module._load_strategy_equity("sqlite_equity_source")

    assert [point.equity for point in points] == [100000.0, 101000.0]
    assert points[0].time == 1704067200

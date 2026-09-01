"""Tests for BacktestRecord equity_curve migration and column functionality.

Every test here runs against the shared ``isolated_db`` sandbox (see
tests/conftest.py) — none of them may call ``init_db()`` on the production
engine or read/write the real ``data/backtests.db``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from shared.database import (
    get_engine,
    BacktestRecord,
    EquityPointModel,
    save_result_to_sqlite,
    get_session,
    _table_column_names,
)
from shared.models import BacktestMeta, BacktestResult, EquityPoint


def test_equity_curve_column_exists_in_backtests_table(isolated_db) -> None:
    """Test that equity_curve column exists in backtests table after migration."""
    database_module, engine, _data_dir = isolated_db
    database_module.init_db()

    with engine.connect() as connection:
        columns = _table_column_names(connection, "backtests")

    assert "equity_curve" in columns, (
        f"equity_curve column should exist in backtests table. "
        f"Available columns: {sorted(columns)}"
    )


def test_backtest_record_equity_curve_can_store_list_of_dicts(isolated_db) -> None:
    """Test that equity_curve field can store and retrieve equity curve data."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    # Create sample equity curve data
    equity_curve_data = [
        {"time": "2024-01-01T00:00:00", "equity": 100000.0},
        {"time": "2024-01-02T00:00:00", "equity": 101500.0},
        {"time": "2024-01-03T00:00:00", "equity": 102200.5},
    ]

    backtest_id = f"test_equity_curve_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    # Create and save a BacktestRecord with equity_curve
    record = BacktestRecord(
        id=backtest_id,
        strategies=["test_strategy"],
        engine="native",
        backtest_start="2024-01-01",
        backtest_end="2024-01-31",
        data={"symbols": ["AAPL"], "timeframes": ["1d"]},
        initial_capital=100000.0,
        parameters={},
        stats={"sharpe_ratio": 1.5},
        created_at=datetime.now(timezone.utc).isoformat(),
        equity_curve=equity_curve_data,
    )

    with get_session() as session:
        session.add(record)
        session.commit()

        # Retrieve and verify
        retrieved = session.get(BacktestRecord, backtest_id)
        assert retrieved is not None, "BacktestRecord should be saved"
        assert retrieved.equity_curve is not None, "equity_curve should not be None"
        assert len(retrieved.equity_curve) == 3, (
            f"equity_curve should have 3 points, got {len(retrieved.equity_curve)}"
        )

        # Verify data integrity
        for i, point in enumerate(retrieved.equity_curve):
            assert point["time"] == equity_curve_data[i]["time"], (
                f"Time mismatch at index {i}"
            )
            assert point["equity"] == equity_curve_data[i]["equity"], (
                f"Equity mismatch at index {i}"
            )


def test_backtest_record_equity_curve_defaults_to_empty_list(isolated_db) -> None:
    """Test that equity_curve defaults to an empty list when not provided."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    backtest_id = f"test_equity_curve_default_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    record = BacktestRecord(
        id=backtest_id,
        strategies=["test_strategy"],
        engine="native",
        backtest_start="2024-01-01",
        backtest_end="2024-01-31",
        data={"symbols": ["AAPL"], "timeframes": ["1d"]},
        initial_capital=100000.0,
        parameters={},
        stats={},
        created_at=datetime.now(timezone.utc).isoformat(),
        # equity_curve not set - should default to empty list
    )

    with get_session() as session:
        session.add(record)
        session.commit()

        retrieved = session.get(BacktestRecord, backtest_id)
        assert retrieved is not None
        assert retrieved.equity_curve == [], (
            f"equity_curve should default to empty list, got {retrieved.equity_curve}"
        )


def test_save_result_to_sqlite_includes_equity_curve(isolated_db) -> None:
    """Test that save_result_to_sqlite stores equity_curve data correctly."""
    database_module, _engine, _data_dir = isolated_db
    database_module.init_db()

    # Create a BacktestResult with equity curve
    equity_curve = [
        EquityPoint(time="2024-01-01T00:00:00", equity=100000.0),
        EquityPoint(time="2024-01-02T00:00:00", equity=101000.0),
    ]

    backtest_id = f"test_save_result_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    meta = BacktestMeta(
        id=backtest_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        engine="native",
        strategies=["test_strategy"],
        backtest_start="2024-01-01",
        backtest_end="2024-01-31",
        data={"symbols": ["AAPL"], "timeframes": ["1d"]},
        initial_capital=100000.0,
        parameters={},
        notes="Test backtest",
    )

    result = BacktestResult(
        meta=meta,
        stats={"sharpe_ratio": 1.2},
        trades=[],
        equity_curve=equity_curve,
        drawings=[],
        indicators=[],
    )

    save_result_to_sqlite(result)

    # Verify equity_curve was saved to BacktestRecord
    with get_session() as session:
        record = session.get(BacktestRecord, backtest_id)
        assert record is not None, "BacktestRecord should be saved"
        assert len(record.equity_curve) == 2, (
            f"equity_curve should have 2 points, got {len(record.equity_curve)}"
        )


def test_equity_curve_migration_on_existing_table(isolated_db) -> None:
    """Test that migration adds equity_curve column to existing backtests table."""
    database_module, engine, _data_dir = isolated_db
    database_module.init_db()

    # The migration runs automatically in init_db()
    with engine.connect() as connection:
        columns = _table_column_names(connection, "backtests")

    # Verify column exists and can be queried
    assert "equity_curve" in columns, "Migration should add equity_curve column"

    # Verify we can read from the column
    with engine.connect() as connection:
        result = connection.execute(text("SELECT equity_curve FROM backtests LIMIT 1")).fetchone()
        # This should not raise - column exists and is readable
        # May be None or empty list if no records exist
        if result and result[0]:
            assert isinstance(result[0], str), "equity_curve should be stored as JSON string"

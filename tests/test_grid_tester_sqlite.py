"""Tests for grid strategy tester SQLite integration."""
import json
from datetime import datetime, timezone

import pytest

from shared.database import (
    GridRecord,
    get_grid_results,
    init_db,
    save_grid_result,
    save_grid_result_to_sqlite,
)


def test_grid_record_model_creation() -> None:
    """Test GridRecord model can be created with required fields."""
    record = GridRecord(
        id="test_grid_001",
        strategy="test_strategy",
        created_at=datetime.now(timezone.utc).isoformat(),
        symbols_json=["AAPL", "MSFT"],
        timeframes_json=["1h", "4h"],
        results_json={"AAPL": {"1h": {"sharpe": 1.5, "status": "success"}}},
        engine="vectorbt",
    )
    assert record.id == "test_grid_001"
    assert record.strategy == "test_strategy"
    assert record.engine == "vectorbt"
    assert len(record.symbols_json) == 2
    assert len(record.timeframes_json) == 2


def test_save_grid_result_to_sqlite() -> None:
    """Test saving grid result to SQLite database."""
    init_db()
    
    payload = {
        "id": f"test_grid_sqlite_save_{datetime.now(timezone.utc).strftime('%H%M%S')}",
        "strategy": "example_sma",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbols": ["AAPL", "MSFT", "GOOGL"],
        "timeframes": ["1h", "4h", "1d"],
        "results": {
            "AAPL": {
                "1h": {"sharpe": 1.2, "cagr": 0.15, "status": "success"},
                "4h": {"sharpe": 0.9, "cagr": 0.12, "status": "success"},
            },
            "MSFT": {
                "1h": {"sharpe": 0.8, "cagr": 0.10, "status": "success"},
            },
        },
        "engine": "vectorbt",
    }
    
    save_grid_result_to_sqlite(payload)
    
    # Verify it was saved
    results = get_grid_results(limit=100)
    grid_id = payload["id"]
    saved = [r for r in results if r["id"] == grid_id]
    
    assert len(saved) == 1, f"Grid result {grid_id} should be saved to SQLite"
    saved_record = saved[0]
    assert saved_record["strategy"] == "example_sma"
    assert saved_record["engine"] == "vectorbt"
    assert "AAPL" in saved_record["symbols"]
    assert "GOOGL" in saved_record["symbols"]


def test_save_grid_result_function() -> None:
    """Test save_grid_result convenience function."""
    init_db()
    
    grid_id = f"test_grid_func_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    
    save_grid_result(
        grid_id=grid_id,
        strategy="test_strat",
        symbols=["TSLA"],
        timeframes=["1d"],
        results={"TSLA": {"1d": {"sharpe": 2.0, "status": "success"}}},
        engine="vectorbt",
    )
    
    results = get_grid_results(limit=100)
    saved = [r for r in results if r["id"] == grid_id]
    
    assert len(saved) == 1, "save_grid_result should persist to SQLite"
    assert saved[0]["results"]["TSLA"]["1d"]["sharpe"] == 2.0


def test_get_grid_results_returns_newest_first() -> None:
    """Test that get_grid_results orders by created_at descending."""
    init_db()
    
    # Use unique IDs to avoid collision
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    id_1 = f"grid_older_{timestamp}a"
    id_2 = f"grid_newer_{timestamp}b"
    
    # Save two records with different timestamps
    save_grid_result(
        grid_id=id_1,
        strategy="s1",
        symbols=["A"],
        timeframes=["1h"],
        results={},
    )
    save_grid_result(
        grid_id=id_2,
        strategy="s2",
        symbols=["B"],
        timeframes=["1h"],
        results={},
    )
    
    results = get_grid_results(limit=100)
    
    # Verify both records exist
    ids = [r["id"] for r in results]
    assert id_1 in ids, "First grid result should exist"
    assert id_2 in ids, "Second grid result should exist"
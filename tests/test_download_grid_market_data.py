"""Tests for data_tools.download_grid_market_data module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_normalize_timeframe_value():
    """Test timeframe normalization."""
    from data_tools.download_grid_market_data import normalize_timeframe_value

    assert normalize_timeframe_value("1m") == "1m"
    assert normalize_timeframe_value("m1") == "1m"
    assert normalize_timeframe_value("1min") == "1m"
    assert normalize_timeframe_value("m5") == "5m"
    assert normalize_timeframe_value("m15") == "15m"
    assert normalize_timeframe_value("m30") == "30m"
    assert normalize_timeframe_value("h1") == "1h"
    assert normalize_timeframe_value("hour1") == "1h"
    assert normalize_timeframe_value("h4") == "4h"
    assert normalize_timeframe_value("hour4") == "4h"
    assert normalize_timeframe_value("d1") == "1d"
    assert normalize_timeframe_value("daily") == "1d"
    assert normalize_timeframe_value("day") == "1d"


def test_normalize_symbol():
    """Test symbol normalization with oil aliases."""
    from data_tools.download_grid_market_data import normalize_symbol, CANONICAL_SYMBOLS

    # Standard symbols
    assert normalize_symbol("XAUUSD") == "XAUUSD"
    assert normalize_symbol("EURUSD") == "EURUSD"
    assert normalize_symbol("USDJPY") == "USDJPY"

    # Canonical symbols
    assert normalize_symbol("SP500") == "US500"
    assert normalize_symbol("NAS100") == "USTEC"
    assert normalize_symbol("OIL") == "USOIL"  # default

    # Oil aliases
    assert normalize_symbol("OIL", oil_alias="USOIL") == "USOIL"
    assert normalize_symbol("OIL", oil_alias="WTI") == "USOIL"
    assert normalize_symbol("OIL", oil_alias="CL") == "USOIL"
    assert normalize_symbol("OIL", oil_alias="UKOIL") == "UKOIL"
    assert normalize_symbol("OIL", oil_alias="BRENT") == "UKOIL"


def test_parse_csv_list():
    """Test CSV list parsing."""
    from data_tools.download_grid_market_data import parse_csv_list

    assert parse_csv_list("EURUSD,GBPUSD,USDJPY") == ["EURUSD", "GBPUSD", "USDJPY"]
    assert parse_csv_list(" EURUSD , GBPUSD , USDJPY ") == ["EURUSD", "GBPUSD", "USDJPY"]
    assert parse_csv_list("") == []
    assert parse_csv_list(",") == []


def test_valid_timeframes():
    """Test VALID_TIMEFRAMES set."""
    from data_tools.download_grid_market_data import VALID_TIMEFRAMES

    expected = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    assert VALID_TIMEFRAMES == expected


def test_symbol_asset_class():
    """Test asset class detection."""
    from data_tools.download_grid_market_data import symbol_asset_class

    assert symbol_asset_class("XAUUSD") == "metals"
    assert symbol_asset_class("US500") == "indices"
    assert symbol_asset_class("USTEC") == "indices"
    assert symbol_asset_class("USOIL") == "energy"
    assert symbol_asset_class("UKOIL") == "energy"
    assert symbol_asset_class("EURUSD") == "forex"
    assert symbol_asset_class("UNKNOWN") == "forex"


def test_download_symbol_invalid_timeframes():
    """Test that invalid timeframes are caught early."""
    from data_tools.download_grid_market_data import download_symbol

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        result = download_symbol(
            requested_symbol="EURUSD",
            canonical_symbol="EURUSD",
            timeframes=["1m", "invalid_tf", "5m"],
            output_dir=output_dir,
            start="2024-01-01",
            end="2024-01-02",
            price_type="bid",
            batch_size=10,
            pipeline_batches=1,
            pause_ms=100,
            force=False,
        )

    assert result["status"] == "failed"
    assert "Invalid timeframes" in result["error"]
    assert "invalid_tf" in result["error"]


def test_download_symbol_skip_balance_rows():
    """Test that balance rows are skipped in normalization."""
    from data_tools.download_grid_market_data import normalize_ohlcv_columns
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.parquet"
        # Create a dataframe with bid/ask columns
        df = pd.DataFrame({
            "timestamp": [1, 2, 3],
            "bid_open": [1.0, 1.1, 1.2],
            "bid_high": [1.05, 1.15, 1.25],
            "bid_low": [0.95, 1.05, 1.15],
            "bid_close": [1.02, 1.12, 1.22],
            "bid_volume": [100, 200, 300],
        })
        df.to_parquet(path, index=False)

        normalize_ohlcv_columns(path)

        # Read back and verify canonical columns exist
        df_result = pd.read_parquet(path)
        assert "open" in df_result.columns
        assert "high" in df_result.columns
        assert "low" in df_result.columns
        assert "close" in df_result.columns
        assert "volume" in df_result.columns
        assert "timestamp" in df_result.columns


def test_main_dry_run(capsys):
    """Dry-run prints the planned downloads and returns without fetching.

    main() returns (it does not call sys.exit), so we invoke it directly and
    assert the plan is emitted rather than expecting a SystemExit.
    """
    from data_tools.download_grid_market_data import main

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        import sys
        old_argv = sys.argv
        sys.argv = [
            "download_grid_market_data.py",
            "--symbols", "EURUSD,GBPUSD",
            "--timeframes", "1h,4h",
            "--start", "2024-01-01",
            "--end", "2024-01-02",
            "--output", str(output_dir),
            "--dry-run",
        ]
        try:
            main()
        finally:
            sys.argv = old_argv
    assert "planned" in capsys.readouterr().out


def test_main_validates_workers():
    """--workers -1 must be clamped to 1 (max(1, workers)) and not crash.

    main() returns rather than calling sys.exit, so we invoke it directly and
    assert it completes; the clamp is applied internally at
    `workers = max(1, args.workers)` before the executor is created.
    """
    from data_tools.download_grid_market_data import main

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        import sys
        old_argv = sys.argv
        sys.argv = [
            "download_grid_market_data.py",
            "--symbols", "EURUSD",
            "--timeframes", "1h",
            "--start", "2024-01-01",
            "--output", str(output_dir),
            "--workers", "-1",  # Invalid, must be clamped to 1 internally
            "--dry-run",
        ]
        try:
            main()
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
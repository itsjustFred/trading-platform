"""Shared constants for the trading platform."""

VALID_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")
VALID_ENGINES = ("lean", "vectorbt", "pynecore")

# Default data directory
DEFAULT_DATA_DIR = "~/data"

# Parquet OHLCV column names (canonical)
OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

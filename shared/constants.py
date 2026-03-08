"""Shared constants for the trading platform."""

import os
import shutil
from pathlib import Path

VALID_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")
VALID_ENGINES = ("lean", "vectorbt", "pynecore")

# Resolved data and log directories (cross-platform)
DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", "~/data")).expanduser().resolve()
LOG_DIR = Path(os.environ.get("LOG_DIR", "~/logs")).expanduser().resolve()

# Parquet OHLCV column names (canonical)
OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

# Path to bundled genesis baseline (shipped with the repo)
GENESIS_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "data_tools" / "config" / "dukascopy_genesis.json"
)


def resolve_genesis_path(data_dir: Path | None = None) -> Path:
    """Return the best available dukascopy_genesis.json path.

    Prefers runtime file in DATA_DIR (contains user discoveries),
    falls back to the repo-bundled baseline.
    """
    runtime_path = (data_dir or DATA_DIR) / "dukascopy_genesis.json"
    if runtime_path.exists():
        return runtime_path
    return GENESIS_BASELINE_PATH


def resolve_executable(name: str) -> str:
    """Resolve an executable path, handling Windows .cmd/.bat extensions."""
    path = shutil.which(name)
    if path:
        return path
    if os.name == "nt":
        for ext in (".cmd", ".bat", ".exe"):
            path = shutil.which(name + ext)
            if path:
                return path
    raise FileNotFoundError(f"Executable '{name}' not found in PATH")

"""Shared constants for the trading platform.

Environment (.env) loading happens exactly once here, before any module reads
``os.environ`` for configuration, so every entry point resolves the same
configuration regardless of import order.
"""

import contextlib
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

try:  # pragma: no cover - pwd is not available on Windows
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

VALID_TIMEFRAMES = ("tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")
VALID_ENGINES = ("native", "lean", "vectorbt", "pynecore")
TIMEFRAME_ALIASES: dict[str, str] = {
    "m1": "1m",
    "h1": "1h",
    "d1": "1d",
}


def normalize_timeframe(timeframe: str | None) -> str:
    """Normalize timeframe aliases to canonical values.

    Returns canonical values for known aliases (e.g. m1 -> 1m) and safely
    passes through already-normalized or unknown values.
    """
    normalized = str(timeframe or "").strip().lower()
    if not normalized:
        return ""
    return TIMEFRAME_ALIASES.get(normalized, normalized)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Resolved data and log directories (cross-platform)
_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
_log_dir_env = os.environ.get("LOG_DIR")


def _resolve_data_dir(raw_value: str | None) -> Path:
    """Resolve DATA_DIR deterministically.

    The configured value is authoritative: it is used verbatim (after
    ``~`` expansion) without filesystem-based candidate scoring, so two
    processes given the same configuration always agree on DATA_DIR. An
    unset configuration falls back to ``<repo>/data``.
    """
    default_path = (PROJECT_ROOT / "data").resolve()
    if not raw_value:
        return default_path

    configured = Path(raw_value).expanduser()
    if not configured.is_absolute():
        configured = (PROJECT_ROOT / configured).resolve()
    return configured.resolve()


def set_data_dir(path: str | Path) -> None:
    """Override DATA_DIR for the current process (test isolation hook).

    Keeps the derived module-level constants (DATA_DIR, OPTIMIZATIONS_DIR)
    and the already-imported ``shared.database`` bindings in sync.
    """
    global DATA_DIR, OPTIMIZATIONS_DIR
    resolved = Path(path).expanduser().resolve()
    DATA_DIR = resolved
    OPTIMIZATIONS_DIR = DATA_DIR / "optimizations"
    _sync_database_paths()


def reset_data_dir() -> None:
    """Restore the process-wide DATA_DIR resolved from the environment."""
    global DATA_DIR, OPTIMIZATIONS_DIR
    DATA_DIR = _resolve_data_dir(os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR"))
    OPTIMIZATIONS_DIR = DATA_DIR / "optimizations"
    _sync_database_paths()


def _sync_database_paths() -> None:
    """Mirror DATA_DIR/DB_PATH into an already-imported shared.database.

    Only synchronizes when ``shared.database`` is already in ``sys.modules``
    (importing it here would be a second, circular initialization path). The
    lazily created engine is disposed and reset so the next ``get_engine()``
    binds to the new DB_PATH — a cached engine must never keep pointing at the
    previous data directory while ``database.DB_PATH`` already claims the new
    one. The schema-initialization flag is reset too, so the tables are
    (re-)created in the new location on the next ``init_db()``. An explicit
    ``database.ENGINE`` override is left untouched.
    """
    database = sys.modules.get("shared.database")
    if database is None:
        return
    database.DATA_DIR = DATA_DIR
    database.DB_PATH = DATA_DIR / "backtests.db"
    with contextlib.suppress(Exception):
        database.reset_engine()
    with contextlib.suppress(Exception):
        database.reset_initialized()


DATA_DIR = _resolve_data_dir(_data_dir_env)
LOG_DIR = (
    Path(_log_dir_env).expanduser().resolve() if _log_dir_env else (PROJECT_ROOT / "logs").resolve()
)
OPTIMIZATIONS_DIR = DATA_DIR / "optimizations"

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


def ensure_path_within_workspace(path: Path) -> Path:
    """Resolve ``path`` and reject anything outside the project workspace.

    Guards operator-facing CLI tools against LLM-supplied arguments that
    escape the repository (python:S8707).
    """
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"Path escapes the project workspace: {resolved}")
    return resolved

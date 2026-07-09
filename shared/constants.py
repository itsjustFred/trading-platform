"""Shared constants for the trading platform."""

import os
import shutil
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


def _safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _home_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path | None) -> None:
        if path is None:
            return
        normalized = str(path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(path)

    home_env = os.environ.get("HOME")
    if home_env:
        _add(Path(home_env).expanduser())

    if pwd is not None:
        try:
            _add(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except (KeyError, OSError):
            pass

        try:
            project_owner_home = Path(pwd.getpwuid(PROJECT_ROOT.stat().st_uid).pw_dir)
            _add(project_owner_home)
        except (KeyError, OSError):
            pass

        for env_key in ("SUDO_USER", "USER"):
            username = str(os.environ.get(env_key) or "").strip()
            if not username:
                continue
            try:
                _add(Path(pwd.getpwnam(username).pw_dir))
            except (KeyError, OSError):
                continue

    return candidates


def _data_dir_score(path: Path) -> int:
    score = 0
    if _safe_path_exists(path):
        score += 1
    if _safe_path_exists(path / "ohlcv"):
        score += 100
    if _safe_path_exists(path / "backtests.db"):
        score += 30
    if _safe_path_exists(path / "backtests"):
        score += 20
    if _safe_path_exists(path / "catalog.json"):
        score += 10
    return score


def _resolve_data_dir(raw_value: str | None) -> Path:
    default_path = (PROJECT_ROOT / "data").resolve()
    if not raw_value:
        return default_path

    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    configured = Path(raw_value).expanduser()
    _add(configured)

    # If config uses "~", prefer the candidate that actually contains runtime data.
    if raw_value == "~" or raw_value.startswith("~/"):
        suffix = raw_value[2:] if raw_value.startswith("~/") else ""
        for home in _home_candidates():
            _add(home / suffix if suffix else home)

    best = candidates[0]
    best_score = _data_dir_score(best)
    for candidate in candidates[1:]:
        score = _data_dir_score(candidate)
        if score > best_score:
            best = candidate
            best_score = score

    return best.resolve()


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

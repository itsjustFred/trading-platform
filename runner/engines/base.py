"""
Engine Adapter Interface — abstract base for all backtest engines.

Every engine (vectorbt, LEAN, PyneCore, etc.) implements this interface
so the Runner CLI can invoke any of them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.models import VALID_TIMEFRAMES, BacktestResult
from shared.strategy_registry import load_strategy_meta

VALID_RESOLUTIONS = (
    "main_timeframe",
    "open_close",
    "1-lower",
    "s1",
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "1d",
    "1w",
    "tick",
)

ProgressCallback = Callable[[float, str], None]


class BacktestConfig(BaseModel):
    """Configuration passed to any engine adapter."""

    model_config = ConfigDict(frozen=False)

    strategy_name: str
    symbols: list[str] = Field(min_length=1)
    timeframe: str  # e.g. "1h", "5m", "1d"
    start_date: str  # "2021-01-01"
    end_date: str  # "2025-06-30"
    initial_capital: float = Field(100_000.0, gt=0)
    risk_per_trade_pct: float | None = Field(None, ge=0.0, le=1.0)
    max_position_pct: float | None = Field(None, gt=0.0)
    max_leverage: float | None = Field(None, gt=0.0)
    modeling_resolution: str = "main_timeframe"
    data_dir: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timeframe")
    @classmethod
    def _validate_timeframe(cls, v: str) -> str:
        if v not in VALID_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe '{v}', must be one of {VALID_TIMEFRAMES}")
        return v

    @field_validator("modeling_resolution")
    @classmethod
    def _validate_resolution(cls, v: str) -> str:
        if v in VALID_TIMEFRAMES:
            return v
        if v not in VALID_RESOLUTIONS:
            raise ValueError(
                f"Invalid modeling_resolution '{v}', must be one of {VALID_RESOLUTIONS}"
            )
        return v


def resolve_strategy_parameters(
    strategy_path: str | Path,
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge STRATEGY_META.parameters defaults with explicit overrides."""
    resolved = Path(strategy_path).resolve()
    try:
        # The run already trusts this explicit path (it is about to be imported
        # by load_strategy_module); allow metadata reads outside the default
        # strategy tree by containing to the file's own directory.
        meta = load_strategy_meta(resolved, resolved.parent)
    except Exception:
        meta = {}

    defaults = meta.get("parameters", {}) if isinstance(meta, dict) else {}
    if not isinstance(defaults, dict):
        defaults = {}

    merged = dict(defaults)
    merged.update(overrides or {})
    return merged


class EngineAdapter(ABC):
    """Abstract base class for backtest engine adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short engine identifier, e.g. 'vectorbt', 'lean'."""
        ...

    @abstractmethod
    def run(
        self,
        config: BacktestConfig,
        strategy_path: str,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        """
        Execute a backtest and return the result in universal format.

        Args:
            config: Backtest parameters (symbols, dates, capital, etc.)
            strategy_path: Path to the strategy file/module
            progress_callback: Optional callback receiving (percent, message)

        Returns:
            BacktestResult with trades, meta, and optional equity curve
        """
        ...

    def validate_config(self, config: BacktestConfig) -> list[str]:
        """
        Optional validation. Returns list of error messages (empty = valid).
        Default implementation checks basic fields.
        """
        errors = []
        if not config.symbols:
            errors.append("No symbols specified")
        if not config.timeframe:
            errors.append("No timeframe specified")
        # start/end dates are now optional — auto-detected from data if missing
        return errors

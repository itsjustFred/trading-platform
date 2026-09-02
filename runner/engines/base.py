"""
Engine Adapter Interface — abstract base for all backtest engines.

Every engine (vectorbt, LEAN, PyneCore, etc.) implements this interface
so the Runner CLI can invoke any of them uniformly.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from runner.engines import contract
from shared.models import VALID_TIMEFRAMES, BacktestResult
from shared.strategy_registry import load_strategy_meta

#: Deprecated alias of :attr:`BacktestConfig.max_position_exposure_pct`.
#: Kept so old invocations keep working during the Cleanup 16-E migration.
LEGACY_MAX_POSITION_PCT = "max_position_pct"

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
    # Maximum loss per trade, as a fraction of equity (Cleanup 16-E Decision
    # Record: "risk_per_trade_pct means maximum loss per trade").
    risk_per_trade_pct: float | None = Field(None, ge=0.0, le=1.0)
    # Position-size semantics (Cleanup 16-E Decision Record): maximum notional
    # exposure of a single position as a fraction of equity (0.05 = 5%).
    # Not an aggregate-portfolio limit and not a loss budget — those are
    # ``max_total_exposure`` (RiskConfig) and ``risk_per_trade_pct``.
    max_position_exposure_pct: float | None = Field(None, gt=0.0)
    max_leverage: float | None = Field(None, gt=0.0)
    modeling_resolution: str = "main_timeframe"
    data_dir: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **data: Any) -> None:
        if LEGACY_MAX_POSITION_PCT in data and data[LEGACY_MAX_POSITION_PCT] is not None:
            warnings.warn(
                "BacktestConfig(max_position_pct=...) is deprecated; use "
                "max_position_exposure_pct (same per-position notional/equity "
                "semantics). The alias will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
            if "max_position_exposure_pct" in data and data["max_position_exposure_pct"] is not None:
                raise ValueError(
                    "Pass either max_position_exposure_pct or its deprecated alias "
                    "max_position_pct, not both."
                )
            data["max_position_exposure_pct"] = data.pop(LEGACY_MAX_POSITION_PCT)
        super().__init__(**data)

    @property
    def max_position_pct(self) -> float | None:
        """Deprecated alias of :attr:`max_position_exposure_pct` (read-only)."""
        warnings.warn(
            "BacktestConfig.max_position_pct is deprecated; read "
            "max_position_exposure_pct instead. The alias will be removed in a "
            "future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.max_position_exposure_pct

    @field_validator("timeframe")
    @classmethod
    def _validate_timeframe(cls, v: str) -> str:
        if v not in VALID_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe '{v}', must be one of {VALID_TIMEFRAMES}")
        return v

    @field_validator("modeling_resolution")
    @classmethod
    def _validate_resolution(cls, v: str) -> str:
        # Conformance (Cleanup 16-A, F8): the shared intrabar-spelling table in
        # contract.py is the single authority on what a spelling means. Alias
        # spellings it resolves to a concrete timeframe ("m1" → "1m") are
        # accepted and canonicalized HERE so no adapter can ever receive a
        # spelling it might interpret differently; the documented values
        # (main_timeframe, open_close, 1-lower, named granularities) pass
        # through unchanged.
        if v in VALID_RESOLUTIONS:
            return v
        normalized = contract.normalize_intrabar_resolution(v)
        if normalized in VALID_TIMEFRAMES:
            return normalized
        raise ValueError(
            f"Invalid modeling_resolution '{v}', must be one of {VALID_RESOLUTIONS}"
        )


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

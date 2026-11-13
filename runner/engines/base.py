"""
Engine Adapter Interface — abstract base for all backtest engines.

Every engine (vectorbt, LEAN, PyneCore, etc.) implements this interface
so the Runner CLI can invoke any of them uniformly.
"""

from __future__ import annotations

import math
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


def resolve_legacy_max_position_kwarg(
    max_position_exposure_pct: float | None, max_position_pct: float | None, *, caller: str
) -> float | None:
    """Fold the deprecated ``max_position_pct`` keyword into the canonical one.

    Cleanup 16-E keeps old invocations working: the deprecated spelling maps
    onto :attr:`BacktestConfig.max_position_exposure_pct` with identical
    per-position semantics, emitting a ``DeprecationWarning``. Passing both
    spellings at once is rejected.
    """
    if max_position_pct is None:
        return max_position_exposure_pct
    if max_position_exposure_pct is not None:
        raise ValueError(
            "Pass either max_position_exposure_pct or its deprecated alias "
            "max_position_pct, not both."
        )
    warnings.warn(
        f"{caller}(max_position_pct=...) is deprecated; use "
        "max_position_exposure_pct. The alias will be removed in a future "
        "release.",
        DeprecationWarning,
        stacklevel=3,
    )
    return max_position_pct

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
    allow_fallback_stop: bool = True
    reject_missing_stop: bool = False
    require_explicit_column_roles: bool = False
    # Position-size semantics (Cleanup 16-E Decision Record): maximum notional
    # exposure of a single position as a fraction of equity (0.05 = 5%).
    # Not an aggregate-portfolio limit and not a loss budget — those are
    # ``max_total_exposure`` (RiskConfig) and ``risk_per_trade_pct``.
    max_position_exposure_pct: float | None = Field(None, gt=0.0, le=1.0)
    max_leverage: float | None = Field(None, gt=0.0)
    modeling_resolution: str = "main_timeframe"
    # Fill convention for signal-driven exits in bar engines. Owner decision
    # 2026-11-13 (supersedes D4 / A-M1 "Fill-zum-Trigger-Close"): a signal exit
    # produced on bar N fills at bar N+1's OPEN, exactly like market entries
    # ("next_open", default). A signal on the LAST bar has no next bar: the
    # position is closed by the end-of-data forced close at that bar's close and
    # the trade is flagged (meta["signal_exit_deferred_to_end_of_data"]).
    # "close" / "open" remain explicit legacy opt-ins that fill on the SIGNAL
    # bar itself (its close / its open); "open" serves MT5 clock exits whose
    # signal bar IS the exit minute. Stop/take-profit fills are independent of
    # this switch: a level fills at the level, or at the bar's open when the
    # bar gaps through it.
    signal_exit_fill: str = "next_open"
    # Indicator warmup. When set (date
    # string), market data is loaded from this earlier date so indicators
    # needing history (e.g. daily ATR) are already warmed up at start_date.
    # No orders are created and no signal exits fire before start_date; the
    # equity curve starts at start_date. Empty (default) = legacy behavior.
    warmup_start: str = ""
    data_dir: str = ""
    data_source: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **data: Any) -> None:
        # Fold the deprecated max_position_pct spelling through the same shared
        # resolver the batch entry points use (conflict check + warning).
        resolved = resolve_legacy_max_position_kwarg(
            data.get("max_position_exposure_pct"),
            data.get(LEGACY_MAX_POSITION_PCT),
            caller="BacktestConfig",
        )
        data.pop(LEGACY_MAX_POSITION_PCT, None)
        if resolved is not None or data.get("max_position_exposure_pct") is not None:
            data["max_position_exposure_pct"] = resolved
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

    @field_validator(
        "initial_capital",
        "risk_per_trade_pct",
        "max_position_exposure_pct",
        "max_leverage",
        mode="before",
    )
    @classmethod
    def _validate_finite_numbers(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{info.field_name} must be numeric") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{info.field_name} must be finite")
        return parsed

    @field_validator(
        "allow_fallback_stop",
        "reject_missing_stop",
        "require_explicit_column_roles",
        mode="before",
    )
    @classmethod
    def _validate_policy_flags(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("policy flags must be boolean")
        return value

    @field_validator("signal_exit_fill")
    @classmethod
    def _validate_signal_exit_fill(cls, v: str) -> str:
        # D4: the fill convention switch is load-bearing across all three
        # bar engines — a typo ("clos", "OPEN ") silently fell through to
        # close-fill everywhere (every reader tests `== "open"`). Normalize
        # case/whitespace so "Close"/"OPEN" keep working, reject the rest.
        normalized = str(v or "").strip().lower()
        if normalized not in {"next_open", "close", "open"}:
            raise ValueError(
                f"Invalid signal_exit_fill {v!r}, must be 'next_open', "
                "'close' or 'open'"
            )
        return normalized

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

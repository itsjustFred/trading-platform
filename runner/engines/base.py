"""
Engine Adapter Interface — abstract base for all backtest engines.

Every engine (vectorbt, LEAN, PyneCore, etc.) implements this interface
so the Runner CLI can invoke any of them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.models import BacktestResult


@dataclass
class BacktestConfig:
    """Configuration passed to any engine adapter."""

    strategy_name: str
    symbols: list[str]
    timeframe: str  # e.g. "1h", "5m", "1d"
    start_date: str  # "2021-01-01"
    end_date: str  # "2025-06-30"
    initial_capital: float = 100_000.0
    risk_per_trade_pct: float | None = None  # e.g. 0.01 for 1% risk per trade
    max_position_pct: float | None = None  # max position notional / equity (e.g. 1.0 = 100%)
    max_leverage: float | None = None  # max notional / equity allowed with leverage
    data_dir: str = ""  # override for ~/data/
    parameters: dict[str, Any] = field(default_factory=dict)


class EngineAdapter(ABC):
    """Abstract base class for backtest engine adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short engine identifier, e.g. 'vectorbt', 'lean'."""
        ...

    @abstractmethod
    def run(self, config: BacktestConfig, strategy_path: str) -> BacktestResult:
        """
        Execute a backtest and return the result in universal format.

        Args:
            config: Backtest parameters (symbols, dates, capital, etc.)
            strategy_path: Path to the strategy file/module

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

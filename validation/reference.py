"""ValidationReference domain model.

Normalizes backtest results imported from external frameworks into a
common, validated structure that can be compared with platform-native
BacktestResult objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.models import BacktestMeta, BacktestResult, Trade

ALLOWED_FRAMEWORKS = frozenset(
    {
        "mt5_xlsx",
        "mt5_html",
        "stockkit",
        "lean",
        "vectorbt",
        "pynecore",
        "unknown",
    }
)

ALLOWED_STATUSES = frozenset({"pending", "validated", "failed", "skipped"})


class ValidationReference(BaseModel):
    """Canonical representation of an imported external backtest result."""

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)

    # ── Required fields ──────────────────────────────────────────────────
    strategy_name: str
    source_framework: str
    symbol: str
    timeframe: str
    settings: dict[str, Any]
    trades: list[Trade]
    stats: dict[str, Any]
    raw_metadata: dict[str, Any]

    # ── Optional fields ──────────────────────────────────────────────────
    source_path: str | None = None
    source_hash: str | None = None
    imported_at: datetime = Field(default_factory=datetime.utcnow)
    validation_status: str = Field(default="pending")
    allow_empty: bool = False

    @field_validator("source_framework")
    @classmethod
    def _validate_source_framework(cls, v: str) -> str:
        if v not in ALLOWED_FRAMEWORKS:
            allowed = ", ".join(sorted(ALLOWED_FRAMEWORKS))
            raise ValueError(
                f"source_framework must be one of {allowed}, got '{v}'"
            )
        return v

    @field_validator("validation_status")
    @classmethod
    def _validate_validation_status(cls, v: str) -> str:
        if v not in ALLOWED_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_STATUSES))
            raise ValueError(
                f"validation_status must be one of {allowed}, got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def _check_trades_not_empty(self) -> "ValidationReference":
        if not self.allow_empty and len(self.trades) == 0:
            raise ValueError("trades must not be empty when allow_empty=False")
        return self

    def to_backtest_result(self) -> BacktestResult:
        """Convert this ValidationReference into a platform-native BacktestResult.

        Fields that are not available on the external import are populated
        with sensible defaults or ``None``.
        """
        meta = BacktestMeta(
            id=f"{self.strategy_name}_{self.source_framework}_{self.imported_at.strftime('%Y%m%d-%H%M%S')}",
            created_at=self.imported_at.isoformat(),
            engine=self.source_framework,
            strategies=[self.strategy_name],
            data={
                "symbols": [self.symbol],
                "timeframes": [self.timeframe],
                "universe": "",
                "source": self.source_framework,
            },
            parameters=self.settings,
            notes=f"Imported from {self.source_framework}"
            + (f" (path: {self.source_path})" if self.source_path else ""),
        )

        return BacktestResult(
            meta=meta,
            trades=self.trades,
            stats=self.stats,
            equity_curve=[],
            indicators=[],
            drawings=[],
        )

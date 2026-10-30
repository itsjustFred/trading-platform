"""
Shared data models for the universal trade format.

Defines the canonical JSON schema:
- StrategyMeta: Pydantic model for strategy metadata (validated)
- TradeExit: A single (partial) exit from a position (dataclass)
- Trade: One position from entry to full close (dataclass)
- BacktestMeta: Metadata about a backtest run (dataclass)
- BacktestResult: Complete backtest output (meta + trades + equity + stats)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from shared.atomic_io import write_text_atomically

logger = logging.getLogger(__name__)


# ─── Strategy metadata ──────────────────────────────────────────────────────


class StrategyMeta(BaseModel):
    """Metadaten für eine Strategie (unabhängig vom Backtest)."""

    model_config = ConfigDict(frozen=False)

    name: str
    version: str = "1.0.0"
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: dict[str, Any] = Field(default_factory=dict)
    source_url: str = ""
    notes: str = ""
    # Tags structure:
    # {
    #     "type": ["mean-reversion", "momentum", "breakout"],
    #     "source": "zenomtrader-discord",
    #     "link": "https://...",
    #     "asset_class": ["equities", "forex"],
    #     "complexity": "simple" | "medium" | "complex"
    # }

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.parameters:
            d["parameters"] = self.parameters
        if self.tags:
            d["tags"] = self.tags
        if self.source_url:
            d["source_url"] = self.source_url
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyMeta:
        return cls(
            name=d["name"],
            version=d.get("version", "1.0.0"),
            parameters=d.get("parameters", {}),
            tags=d.get("tags", {}),
            source_url=d.get("source_url", ""),
            notes=d.get("notes", ""),
        )


class StrategyType(str, Enum):
    MARKET_DATA = "market_data"
    SIGNAL_PROCESSOR = "signal_processor"


# ─── Live trading models ─────────────────────────────────────────────────────


class ServiceType(str, Enum):
    DISCORD_BOT = "discord_bot"
    SCHEDULED_JOB = "scheduled_job"


class ServiceStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


class Service(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    type: ServiceType
    status: ServiceStatus
    config: dict[str, Any] = Field(default_factory=dict)
    last_activity: datetime | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class VerificationEvidenceStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class VerificationEvidenceRecord(BaseModel):
    id: str
    run_id: str
    component: str
    flow_name: str
    category: str
    status: VerificationEvidenceStatus
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float | None = None
    git_sha: str = ""
    environment: str = ""
    mode: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_summary: str | None = None
    evidence_ref: str | None = None
    manual_validation_required: bool = False
    source: str = ""
    created_at: str = ""
    updated_at: str = ""


class VerificationEvidenceSummary(BaseModel):
    component: str
    flow_name: str
    category: str
    run_count: int
    success_count: int
    failed_count: int
    partial_count: int
    skipped_count: int
    last_status: VerificationEvidenceStatus
    last_run_at: str
    last_success_at: str = ""
    last_error: str = ""
    latest_git_sha: str = ""
    latest_environment: str = ""
    latest_mode: str = ""
    manual_validation_required: bool = False


class LiveAccountConfig(BaseModel):
    """Live account configuration stored in DB and used by API payloads."""

    id: str
    broker: Literal["alpaca", "ctrader"]
    name: str = ""
    broker_login: str | None = None
    ctid: str | None = None


class LiveAccount(BaseModel):
    """Discovered broker account payload exposed to the frontend."""

    id: str
    name: str
    broker: Literal["alpaca", "ctrader"]
    balance: float = 0.0
    is_demo: bool = False
    broker_login: str | None = None
    ctid: str | None = None


class LiveDeploymentConfig(BaseModel):
    """Live deployment record (strategy + account + market + runtime status)."""

    id: UUID = Field(default_factory=uuid4)
    strategy_name: str
    account_id: str
    symbols: list[str]
    timeframe: str
    status: Literal["ACTIVE", "PAUSED", "STOPPED"] = "ACTIVE"
    signal_source_type: Literal["internal", "discord"] = "internal"
    signal_source_id: str | None = None
    symbol_filter: list[str] = Field(default_factory=list)
    signal_source: Literal["internal", "discord"] = "internal"
    signal_config: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class LiveDeploymentCreateRequest(BaseModel):
    """Payload for creating a live deployment via API."""

    strategy_name: str = ""
    account_id: str
    symbols: list[str]
    timeframe: str
    signal_source_type: Literal["internal", "discord"] | None = None
    signal_source_id: str | None = None
    symbol_filter: list[str] = Field(default_factory=list)
    signal_source: Literal["internal", "discord"] = "internal"
    signal_config: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class LiveDeploymentStatusUpdateRequest(BaseModel):
    """Payload for updating a deployment runtime status via API."""

    status: Literal["ACTIVE", "PAUSED", "STOPPED"]


class LiveSignal(BaseModel):
    """Intent emitted by strategy logic before broker execution."""

    id: UUID = Field(default_factory=uuid4)
    deployment_id: str
    symbol: str
    action: Literal["buy", "sell"]
    theoretical_price: float
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveExecution(BaseModel):
    """Execution fill received from broker for a previously emitted signal."""

    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    actual_price: float
    filled_qty: float
    commission: float
    timestamp: datetime
    broker_order_id: str
    dedupe_key: str | None = None

    # The broker's own per-fill identifier, where it has one. cTrader calls it `dealId`; Alpaca's
    # equivalent is not populated yet.
    #
    # Stored because the dedupe key alone cannot answer the question it was built to answer. The key
    # is a string our own code assembled, so "were these two rows the same broker fill?" cannot be
    # answered afterwards without parsing it back out of that string - and when two equal-sized
    # fills on one order collide, that parse is the only evidence there is. Measured: one stored
    # signal produced two execution rows 2 ms apart at an identical price, and `live_executions`
    # carried nothing that could distinguish two real partial fills from one fill counted twice.
    #
    # Recording it decides nothing and changes no behaviour. It makes the decision *possible*.
    broker_fill_id: int | None = None


class LiveBalanceRecord(BaseModel):
    """Periodic balance/equity snapshot for a live broker account."""

    id: UUID = Field(default_factory=uuid4)
    account_id: str
    timestamp: datetime
    balance: float
    equity: float


# ─── Portfolio models ──────────────────────────────────────────────────────────


class PortfolioComponent(BaseModel):
    """A weighted strategy component inside a multi-strategy portfolio."""

    strategy_name: str = ""
    symbol: str = ""
    timeframe: str = "1h"
    weight: float = 1.0
    risk_config: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    backtest_id: str | None = None


class Portfolio(BaseModel):
    """Persisted portfolio payload shared across API and storage layers."""

    id: str
    name: str = "Untitled"
    created_at: str = ""
    account_capital: float = 100_000.0
    components: list[PortfolioComponent] = Field(default_factory=list)
    combined_stats: dict[str, Any] = Field(default_factory=dict)
    combined_equity: list[dict[str, Any]] = Field(default_factory=list)
    strategy_contributions: list[dict[str, Any]] = Field(default_factory=list)
    component_contributions: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)


# ─── Timeframe constants ────────────────────────────────────────────────────

VALID_TIMEFRAMES = ("tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
VALID_DIRECTIONS = ("LONG", "SHORT")


# ─── Exit ────────────────────────────────────────────────────────────────────


@dataclass
class TradeExit:
    """A single (partial) exit from a position."""

    time: str  # ISO 8601 UTC
    price: float
    quantity: float
    pnl: float | None = None  # PnL for this partial exit
    reason: str | None = None  # e.g. "stop_loss", "tp1", "trailing_stop"
    fees: float | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"time": self.time, "price": self.price, "quantity": self.quantity}
        if self.pnl is not None:
            d["pnl"] = self.pnl
        if self.reason is not None:
            d["reason"] = self.reason
        if self.fees is not None:
            d["fees"] = self.fees
        if self.tags:
            d["tags"] = self.tags
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TradeExit:
        return cls(
            time=d["time"],
            price=d["price"],
            quantity=d["quantity"],
            pnl=d.get("pnl"),
            reason=d.get("reason"),
            fees=d.get("fees"),
            tags=d.get("tags", []),
        )


# ─── Trade ───────────────────────────────────────────────────────────────────


@dataclass
class Trade:
    """One position from entry to full close. Supports partial exits."""

    id: int
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_time: str  # ISO 8601 UTC
    entry_price: float
    quantity: float  # total quantity at entry
    exits: list[TradeExit]  # at least 1 element
    pnl: float  # gross PnL (sum over all exits)

    # Optional fields
    position_size: float | None = None
    risk_amount: float | None = None
    risk_percent: float | None = None
    exit_reason: str | None = None
    pnl_pct: float | None = None
    fees: float | None = None  # total fees (entry + all exits)
    net_pnl: float | None = None
    r_multiple: float | None = None
    mae: float | None = None  # Max Adverse Excursion
    mfe: float | None = None  # Max Favorable Excursion
    holding_bars: int | None = None  # entry to last exit
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    stop_loss: float | None = None
    take_profit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "exits": [e.to_dict() for e in self.exits],
            "pnl": self.pnl,
        }
        # Only include optional fields if set
        for fld in (
            "position_size",
            "risk_amount",
            "risk_percent",
            "exit_reason",
            "pnl_pct",
            "fees",
            "net_pnl",
            "r_multiple",
            "mae",
            "mfe",
            "holding_bars",
            "stop_loss",
            "take_profit",
        ):
            val = getattr(self, fld)
            if val is not None:
                d[fld] = val
        if self.tags:
            d["tags"] = self.tags
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trade:
        return cls(
            id=d["id"],
            symbol=d["symbol"],
            direction=d["direction"],
            entry_time=d["entry_time"],
            entry_price=d["entry_price"],
            quantity=d["quantity"],
            exits=[TradeExit.from_dict(e) for e in d["exits"]],
            pnl=d["pnl"],
            position_size=d.get("position_size"),
            risk_amount=d.get("risk_amount"),
            risk_percent=d.get("risk_percent"),
            exit_reason=d.get("exit_reason"),
            pnl_pct=d.get("pnl_pct"),
            fees=d.get("fees"),
            net_pnl=d.get("net_pnl"),
            r_multiple=d.get("r_multiple"),
            mae=d.get("mae"),
            mfe=d.get("mfe"),
            holding_bars=d.get("holding_bars"),
            tags=d.get("tags", []),
            meta=d.get("meta", {}),
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
        )

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0

    @property
    def last_exit_time(self) -> str:
        """Time of the final exit."""
        return self.exits[-1].time if self.exits else self.entry_time

    @property
    def direction_sign(self) -> int:
        return 1 if self.direction == "LONG" else -1


# ─── Equity point ────────────────────────────────────────────────────────────


@dataclass
class EquityPoint:
    time: str
    equity: float

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time, "equity": self.equity}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EquityPoint:
        return cls(time=d["time"], equity=d["equity"])


# ─── Backtest metadata ──────────────────────────────────────────────────────


@dataclass
class BacktestMeta:
    """Metadata about a backtest run."""

    id: str  # unique id, e.g. "mr-rsi2-sp500-20260224-143022"
    created_at: str  # ISO 8601 UTC
    engine: str  # "vectorbt", "lean", "pynecore"

    # Strategie(n) — kann eine oder mehrere sein
    strategies: list[str] = field(default_factory=list)  # Namen der Strategien

    # Backtest-Zeitraum
    backtest_start: str = ""  # "2021-01-01"
    backtest_end: str = ""  # "2025-06-30"
    duration_days: int = 0
    total_bars: int = 0

    # Daten-Info
    data: dict = field(default_factory=dict)
    # {
    #     "timeframes": ["1h"],         # oder ["1h", "1d"] für Multi-TF
    #     "symbols": ["AAPL", "MSFT"],
    #     "universe": "sp500" | "forex-majors" | "custom" | "",
    #     "source": "yfinance" | "dukascopy" | "huggingface" | "alpaca",
    # }

    # Capital
    initial_capital: float = 100_000.0
    currency: str = "USD"
    risk_per_trade_pct: float | None = None
    max_position_exposure_pct: float | None = None
    max_leverage: float | None = None
    modeling_resolution: str = "main_timeframe"
    signal_exit_fill: str = "close"
    allow_fallback_stop: bool = True
    reject_missing_stop: bool = False
    require_explicit_column_roles: bool = False

    # Parameter (strategie-spezifisch)
    parameters: dict = field(default_factory=dict)

    # Tags für den Backtest selbst (nicht Strategie)
    tags: list[str] = field(default_factory=list)
    # z.B. ["paper-test", "optimized", "walk-forward", "multi-strategy"]

    # Notizen
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "engine": self.engine,
            "strategies": self.strategies,
            "backtest_start": self.backtest_start,
            "backtest_end": self.backtest_end,
            "initial_capital": self.initial_capital,
        }
        if self.duration_days:
            d["duration_days"] = self.duration_days
        if self.total_bars:
            d["total_bars"] = self.total_bars
        if self.data:
            d["data"] = self.data
        if self.currency != "USD":
            d["currency"] = self.currency
        for name in (
            "risk_per_trade_pct",
            "max_position_exposure_pct",
            "max_leverage",
        ):
            value = getattr(self, name)
            if value is not None:
                d[name] = value
        if self.modeling_resolution != "main_timeframe":
            d["modeling_resolution"] = self.modeling_resolution
        if self.signal_exit_fill != "close":
            d["signal_exit_fill"] = self.signal_exit_fill
        if not self.allow_fallback_stop:
            d["allow_fallback_stop"] = False
        if self.reject_missing_stop:
            d["reject_missing_stop"] = True
        if self.require_explicit_column_roles:
            d["require_explicit_column_roles"] = True
        if self.parameters:
            d["parameters"] = self.parameters
        if self.tags:
            d["tags"] = self.tags
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestMeta:
        return cls(
            id=d["id"],
            created_at=d["created_at"],
            engine=d["engine"],
            strategies=d.get("strategies", []),
            backtest_start=d.get("backtest_start", ""),
            backtest_end=d.get("backtest_end", ""),
            duration_days=d.get("duration_days", 0),
            total_bars=d.get("total_bars", 0),
            data=d.get("data", {}),
            initial_capital=d.get("initial_capital", 100_000.0),
            currency=d.get("currency", "USD"),
            risk_per_trade_pct=d.get("risk_per_trade_pct"),
            max_position_exposure_pct=d.get("max_position_exposure_pct"),
            max_leverage=d.get("max_leverage"),
            modeling_resolution=d.get("modeling_resolution", "main_timeframe"),
            signal_exit_fill=d.get("signal_exit_fill", "close"),
            allow_fallback_stop=d.get("allow_fallback_stop", True),
            reject_missing_stop=d.get("reject_missing_stop", False),
            require_explicit_column_roles=d.get("require_explicit_column_roles", False),
            parameters=d.get("parameters", {}),
            tags=d.get("tags", []),
            notes=d.get("notes", ""),
        )


# ─── Indicator series (optional, for viewer overlays) ─────────────────────────


@dataclass
class IndicatorSeries:
    """Time-series indicator data for viewer display.

    - type "overlay": drawn on the main price chart (EMA, SMA, SuperTrend, …)
    - type "panel":   separate sub-panel below chart (RSI, MACD, …) — Phase 3 rendering

    ``style`` is a dict with optional keys:
        color  (str)  — CSS color, e.g. "#58a6ff"
        width  (int)  — line width in px, default 1
        dash   (str)  — "solid" | "dashed" | "dotted", default "solid"
    """

    name: str
    type: str  # "overlay" | "panel"
    data: list[dict[str, Any]]  # [{"time": "ISO8601", "value": float}, ...]
    style: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "data": self.data, "style": self.style}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IndicatorSeries:
        return cls(name=d["name"], type=d["type"], data=d.get("data", []), style=d.get("style", {}))


@dataclass
class BacktestStats:
    """Canonical backtest statistics shared across runner and viewer layers."""

    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    total_pnl: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_bars: float | None = None
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: int = 0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    cagr: float = 0.0
    total_return: float = 0.0
    psr: float | None = None
    p_value: float = 1.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "total_trades": self.total_trades,
            "winners": self.winners,
            "losers": self.losers,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "total_pnl": self.total_pnl,
            "avg_winner": self.avg_winner,
            "avg_loser": self.avg_loser,
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration": self.max_drawdown_duration,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "cagr": self.cagr,
            "total_return": self.total_return,
            "p_value": self.p_value,
        }
        if self.avg_holding_bars is not None:
            data["avg_holding_bars"] = self.avg_holding_bars
        if self.psr is not None:
            data["psr"] = self.psr
        data.update(self.extras)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BacktestStats":
        field_names = {item.name for item in dataclass_fields(cls) if item.name != "extras"}
        payload = {key: value for key, value in d.items() if key in field_names}
        payload["extras"] = {key: value for key, value in d.items() if key not in field_names}
        return cls(**payload)


def resolve_defined_stat(
    stats: dict[str, Any] | None,
    key: str,
    default: float | None = None,
) -> float | None:
    """Resolve a stat, honouring the producer's ``undefined_metrics`` list.

    The convention is that a quantity which could not be computed is *named* in
    ``undefined_metrics`` and its value omitted, so an absent number is never
    readable as a real one. ``dict.get(key, 0)`` silently undoes that: it cannot
    tell "computed as zero" from "never computed" and returns 0 for the omitted
    key.

    It lives here rather than in the database or viewer layer because the
    convention belongs to the data model and every consumer has to apply it the
    same way. It has already bitten twice - once for PSR, where the default was
    the *worst* attainable value, and once for the portfolio combined stats -
    each time because the default was applied somewhere new.
    """
    if not isinstance(stats, dict):
        return default
    undefined = stats.get("undefined_metrics")
    if isinstance(undefined, (list, tuple, set)) and key in undefined:
        return None
    value = stats.get(key, default)
    return default if value is None else value


@dataclass
class PortfolioStats:
    """Canonical portfolio statistics for persisted combined portfolio metrics."""

    sharpe: float = 0.0
    sortino: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    net_profit: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    calmar: float = 0.0
    total_return: float = 0.0
    max_drawdown_duration: int = 0
    psr: float | None = None
    p_value: float = 1.0
    correlation_matrix: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "net_profit": self.net_profit,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "calmar": self.calmar,
            "total_return": self.total_return,
            "max_drawdown_duration": self.max_drawdown_duration,
            "p_value": self.p_value,
            "correlation_matrix": self.correlation_matrix,
        }
        if self.psr is not None:
            data["psr"] = self.psr
        data.update(self.extras)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PortfolioStats":
        field_names = {item.name for item in dataclass_fields(cls) if item.name != "extras"}
        payload = {key: value for key, value in d.items() if key in field_names}
        payload["extras"] = {key: value for key, value in d.items() if key not in field_names}
        return cls(**payload)


# ─── Complete backtest result ────────────────────────────────────────────────


@dataclass
class BacktestResult:
    """Complete backtest output: meta + trades + optional equity_curve + stats."""

    meta: BacktestMeta
    trades: list[Trade]
    equity_curve: list[EquityPoint] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    indicators: list[IndicatorSeries] = field(default_factory=list)
    drawings: list[dict[str, Any]] = field(default_factory=list)
    equity_evidence_status: Literal["unknown", "available", "missing"] = "unknown"

    def __post_init__(self) -> None:
        if self.equity_curve and self.equity_evidence_status == "unknown":
            self.equity_evidence_status = "available"
        if self.equity_evidence_status == "available" and not self.equity_curve:
            raise ValueError("equity_evidence_status=available requires an equity curve")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "meta": self.meta.to_dict(),
            "trades": [t.to_dict() for t in self.trades],
        }
        if self.equity_curve:
            d["equity_curve"] = [e.to_dict() for e in self.equity_curve]
        if self.stats:
            d["stats"] = self.stats
        if self.indicators:
            d["indicators"] = [i.to_dict() for i in self.indicators]
        if self.drawings:
            d["drawings"] = self.drawings
        if self.equity_evidence_status != "unknown":
            d["equity_evidence_status"] = self.equity_evidence_status
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacktestResult:
        return cls(
            meta=BacktestMeta.from_dict(d["meta"]),
            trades=[Trade.from_dict(t) for t in d.get("trades", [])],
            equity_curve=[EquityPoint.from_dict(e) for e in d.get("equity_curve", [])],
            stats=d.get("stats", {}),
            indicators=[IndicatorSeries.from_dict(i) for i in d.get("indicators", [])],
            drawings=d.get("drawings", []),
            equity_evidence_status=d.get("equity_evidence_status", "unknown"),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> BacktestResult:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> Path:
        """Save to JSON file. Returns the path written to."""
        p = Path(path).resolve()
        write_text_atomically(p, self.to_json())
        logger.info("Saved backtest result to %s", p)
        return p

    @classmethod
    def load(cls, path: str | Path) -> BacktestResult:
        """Load from JSON file."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Backtest result file not found: {path}")
        return cls.from_json(p.read_text(encoding="utf-8"))

    @property
    def filename(self) -> str:
        """Generate filename: {strategy}_{engine}_{YYYYMMDD-HHmmss}.json"""
        try:
            dt = datetime.fromisoformat(self.meta.created_at.replace("Z", "+00:00"))
            ts = dt.strftime("%Y%m%d-%H%M%S")
        except (ValueError, AttributeError):
            ts = "unknown"
        strategy_name = self.meta.strategies[0] if self.meta.strategies else "unknown"
        return f"{strategy_name}_{self.meta.engine}_{ts}.json"

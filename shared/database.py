from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import JSON, Column, String, UniqueConstraint, delete, event, func, or_, text
from sqlalchemy.engine import Connection
from sqlmodel import Field, Session, SQLModel, create_engine, select

from shared.constants import DATA_DIR as RESOLVED_DATA_DIR
from shared.logging import get_logger
from shared.models import (
    BacktestMeta,
    BacktestResult,
    IndicatorSeries,
    LiveAccountConfig,
    LiveBalanceRecord,
    LiveDeploymentConfig,
    LiveExecution,
    Service,
    ServiceStatus,
    ServiceType,
    VerificationEvidenceRecord,
    VerificationEvidenceStatus,
    Portfolio as PortfolioModel,
    PortfolioComponent as PortfolioComponentModel,
    LiveSignal,
    Trade,
    TradeExit,
)
from shared.models import (
    EquityPoint as EquityPointModel,
)

logger = get_logger("database")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
DATA_DIR = RESOLVED_DATA_DIR
DB_PATH = DATA_DIR / "backtests.db"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


ENGINE = create_engine(
    _sqlite_url(DB_PATH),
    echo=False,
    connect_args={
        "check_same_thread": False,
        "timeout": 30.0,
    },
)


@event.listens_for(ENGINE, "connect")
def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False
_CACHED_GIT_SHA: str | None = None
_FINAL_TASK_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "partial",
    "skipped",
}
_TASK_STATUS_TO_EVIDENCE_STATUS = {
    "completed": VerificationEvidenceStatus.SUCCESS.value,
    "failed": VerificationEvidenceStatus.FAILED.value,
    "interrupted": VerificationEvidenceStatus.FAILED.value,
    "cancelled": VerificationEvidenceStatus.SKIPPED.value,
    "partial": VerificationEvidenceStatus.PARTIAL.value,
    "skipped": VerificationEvidenceStatus.SKIPPED.value,
}
_MANUAL_VALIDATION_CATEGORIES = {
    "discord",
    "discord_bot",
    "scheduler",
    "service",
    "daemon",
    "websocket",
    "provider",
    "live",
}


class BacktestRecord(SQLModel, table=True):
    __tablename__ = "backtests"

    id: str = Field(primary_key=True)
    strategies: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    engine: str = Field(default="native", index=True)
    backtest_start: str = Field(default="")
    backtest_end: str = Field(default="")
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    initial_capital: float = Field(default=100_000.0)
    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    tags_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("tags", JSON, nullable=False),
    )
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    notes: str = Field(default="")
    created_at: str = Field(default="", index=True)


class PortfolioRecord(SQLModel, table=True):
    __tablename__ = "portfolios"

    id: str = Field(primary_key=True)
    name: str = Field(default="", index=True)
    created_at: str = Field(default="", index=True)
    account_capital: float = Field(default=100_000.0)
    combined_stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    combined_equity: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    strategy_contributions: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    component_contributions: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    trades: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))

    @classmethod
    def from_portfolio(cls, portfolio: PortfolioModel) -> "PortfolioRecord":
        return cls(
            id=str(portfolio.id),
            name=portfolio.name,
            created_at=portfolio.created_at,
            account_capital=portfolio.account_capital,
            combined_stats=portfolio.combined_stats or {},
            combined_equity=portfolio.combined_equity or [],
            strategy_contributions=portfolio.strategy_contributions or [],
            component_contributions=portfolio.component_contributions or [],
            trades=portfolio.trades or [],
        )

    def to_portfolio(self, components: list["PortfolioComponentRecord"]) -> PortfolioModel:
        return PortfolioModel(
            id=self.id,
            name=self.name or "",
            created_at=self.created_at or "",
            account_capital=float(self.account_capital or 100_000.0),
            components=[item.to_model() for item in components],
            combined_stats=self.combined_stats or {},
            combined_equity=self.combined_equity or [],
            strategy_contributions=self.strategy_contributions or [],
            component_contributions=self.component_contributions or [],
            trades=self.trades or [],
        )


class PortfolioComponentRecord(SQLModel, table=True):
    __tablename__ = "portfolio_components"

    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: str = Field(foreign_key="portfolios.id", index=True)
    component_id: str = Field(default="", index=True)
    strategy_name: str = Field(default="", index=True)
    symbol: str = Field(default="", index=True)
    timeframe: str = Field(default="", index=True)
    weight: float = Field(default=1.0)
    risk_config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    backtest_id: str = Field(default="", index=True)

    @classmethod
    def from_model(
        cls,
        *,
        portfolio_id: str,
        component_id: str,
        component: PortfolioComponentModel,
    ) -> "PortfolioComponentRecord":
        return cls(
            portfolio_id=portfolio_id,
            component_id=component_id,
            strategy_name=component.strategy_name,
            symbol=component.symbol,
            timeframe=component.timeframe,
            weight=component.weight,
            risk_config=component.risk_config or {},
            settings=component.settings or {},
            backtest_id=str(component.backtest_id or ""),
        )

    def to_model(self) -> PortfolioComponentModel:
        return PortfolioComponentModel(
            strategy_name=self.strategy_name or "",
            symbol=self.symbol or "",
            timeframe=self.timeframe or "1h",
            weight=float(self.weight or 1.0),
            risk_config=self.risk_config or {},
            settings=self.settings or {},
            backtest_id=self.backtest_id or None,
        )


class TradeRecord(SQLModel, table=True):
    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key="backtests.id", index=True)
    trade_nr: int = Field(index=True)
    symbol: str = Field(default="", index=True)
    direction: str = Field(default="LONG")
    entry_time: str = Field(default="", index=True)
    entry_price: float = Field(default=0.0)
    quantity: float = Field(default=0.0)
    position_size: float | None = Field(default=None)
    risk_amount: float | None = Field(default=None)
    risk_percent: float | None = Field(default=None)
    exits: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    pnl: float = Field(default=0.0)
    pnl_pct: float | None = Field(default=None)
    fees: float | None = Field(default=None)
    net_pnl: float | None = Field(default=None)
    r_multiple: float | None = Field(default=None)
    mae: float | None = Field(default=None)
    mfe: float | None = Field(default=None)
    holding_bars: int | None = Field(default=None)
    exit_reason: str | None = Field(default=None)
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))


class EquityPoint(SQLModel, table=True):
    __tablename__ = "equity_points"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key="backtests.id", index=True)
    time: str = Field(sa_column=Column(String, index=True))
    equity: float = Field(default=0.0)


class DrawingRecord(SQLModel, table=True):
    __tablename__ = "drawings"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key="backtests.id", index=True)
    trade_nr: int | None = Field(default=None, index=True)
    type: str = Field(default="", index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class IndicatorRecord(SQLModel, table=True):
    __tablename__ = "indicators"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key="backtests.id", index=True)
    series_nr: int = Field(default=0, index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class LiveSignalModel(SQLModel, table=True):
    __tablename__ = "live_signals"

    id: str = Field(primary_key=True)
    deployment_id: str = Field(index=True)
    symbol: str = Field(index=True)
    action: str = Field(index=True)
    theoretical_price: float = Field(default=0.0)
    timestamp: datetime = Field(index=True)
    source: str = Field(default="internal", index=True)
    metadata_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("metadata", JSON, nullable=False)
    )

    @classmethod
    def from_live_signal(cls, signal: LiveSignal) -> "LiveSignalModel":
        return cls(
            id=str(signal.id),
            deployment_id=signal.deployment_id,
            symbol=signal.symbol,
            action=signal.action,
            theoretical_price=signal.theoretical_price,
            timestamp=signal.timestamp,
            source=str(signal.metadata.get("source", "internal")),
            metadata_json=signal.metadata,
        )

    def to_live_signal(self) -> LiveSignal:
        return LiveSignal(
            id=self.id,
            deployment_id=self.deployment_id,
            symbol=self.symbol,
            action=self.action,
            theoretical_price=self.theoretical_price,
            timestamp=self.timestamp,
            metadata={
                **(self.metadata_json or {}),
                "source": self.source or "internal",
            },
        )


class LiveExecutionModel(SQLModel, table=True):
    __tablename__ = "live_executions"

    id: str = Field(primary_key=True)
    signal_id: str = Field(foreign_key="live_signals.id", index=True)
    actual_price: float = Field(default=0.0)
    filled_qty: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    timestamp: datetime = Field(index=True)
    broker_order_id: str = Field(index=True)

    @classmethod
    def from_live_execution(cls, execution: LiveExecution) -> "LiveExecutionModel":
        return cls(
            id=str(execution.id),
            signal_id=str(execution.signal_id),
            actual_price=execution.actual_price,
            filled_qty=execution.filled_qty,
            commission=execution.commission,
            timestamp=execution.timestamp,
            broker_order_id=execution.broker_order_id,
        )

    def to_live_execution(self) -> LiveExecution:
        return LiveExecution(
            id=self.id,
            signal_id=self.signal_id,
            actual_price=self.actual_price,
            filled_qty=self.filled_qty,
            commission=self.commission,
            timestamp=self.timestamp,
            broker_order_id=self.broker_order_id,
        )


class LiveBalanceModel(SQLModel, table=True):
    __tablename__ = "live_balances"

    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    timestamp: datetime = Field(index=True)
    balance: float = Field(default=0.0)
    equity: float = Field(default=0.0)

    @classmethod
    def from_live_balance(cls, record: LiveBalanceRecord) -> "LiveBalanceModel":
        return cls(
            id=str(record.id),
            account_id=record.account_id,
            timestamp=record.timestamp,
            balance=record.balance,
            equity=record.equity,
        )

    def to_live_balance(self) -> LiveBalanceRecord:
        return LiveBalanceRecord(
            id=self.id,
            account_id=self.account_id,
            timestamp=self.timestamp,
            balance=self.balance,
            equity=self.equity,
        )


class LiveAccountModel(SQLModel, table=True):
    __tablename__ = "live_accounts"

    id: str = Field(primary_key=True)
    broker: str = Field(index=True)
    name: str = Field(default="", index=True)
    ctid: str | None = Field(default=None, index=True)
    broker_login: str | None = Field(default=None, index=True)

    @classmethod
    def from_live_account(cls, account: LiveAccountConfig) -> "LiveAccountModel":
        return cls(
            id=account.id,
            broker=account.broker,
            name=account.name,
            ctid=account.ctid,
            broker_login=account.broker_login,
        )

    def to_live_account(self) -> LiveAccountConfig:
        return LiveAccountConfig(
            id=self.id,
            broker=self.broker,
            name=self.name,
            ctid=self.ctid or "",
            broker_login=self.broker_login or "",
        )


class ServiceModel(SQLModel, table=True):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("name", "type", name="uq_services_name_type"),)

    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    type: str = Field(index=True)
    status: str = Field(default=ServiceStatus.STOPPED.value, index=True)
    config_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("config", JSON, nullable=False),
    )
    last_activity: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    @classmethod
    def from_service(cls, service: Service) -> "ServiceModel":
        now = datetime.utcnow()
        return cls(
            id=str(service.id),
            name=str(service.name or "").strip(),
            type=str(service.type.value if isinstance(service.type, ServiceType) else service.type),
            status=str(
                service.status.value
                if isinstance(service.status, ServiceStatus)
                else service.status
            ),
            config_json=dict(service.config or {}),
            last_activity=service.last_activity,
            created_at=service.created_at,
            updated_at=now,
        )

    def to_service(self) -> Service:
        service_type = str(self.type or "").strip().lower()
        service_status = str(self.status or "").strip().lower()
        try:
            parsed_type = ServiceType(service_type)
        except ValueError:
            parsed_type = ServiceType.SCHEDULED_JOB
        try:
            parsed_status = ServiceStatus(service_status)
        except ValueError:
            parsed_status = ServiceStatus.STOPPED
        return Service(
            id=self.id,
            name=self.name,
            type=parsed_type,
            status=parsed_status,
            config=dict(self.config_json or {}),
            last_activity=self.last_activity,
            created_at=self.created_at,
        )


class LiveDeploymentModel(SQLModel, table=True):
    __tablename__ = "live_deployments"

    id: str = Field(primary_key=True)
    strategy_name: str = Field(index=True)
    account_id: str = Field(foreign_key="live_accounts.id", index=True)
    symbols_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("symbols", JSON, nullable=False),
    )
    timeframe: str = Field(index=True)
    status: str = Field(default="ACTIVE", index=True)
    signal_source: str = Field(default="internal", index=True)
    signal_source_type: str = Field(default="internal", index=True)
    signal_source_id: str = Field(default="", index=True)
    symbol_filter_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("symbol_filter", JSON, nullable=False),
    )
    signal_config_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("signal_config", JSON, nullable=False),
    )
    settings_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("settings", JSON, nullable=False),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    @classmethod
    def from_live_deployment(cls, deployment: LiveDeploymentConfig) -> "LiveDeploymentModel":
        now = datetime.utcnow()
        source_type = str(deployment.signal_source_type or "").lower()
        source_name = str(deployment.signal_source or "").lower()
        if source_type not in {"internal", "discord"}:
            source_type = source_name if source_name in {"internal", "discord"} else "internal"
        elif source_type == "internal" and source_name == "discord":
            source_type = "discord"
        source_name = source_name if source_name in {"internal", "discord"} else source_type
        source_id = str(deployment.signal_source_id or "").strip()
        symbol_filter = [
            str(symbol).strip().upper()
            for symbol in (deployment.symbol_filter or [])
            if str(symbol).strip()
        ]
        return cls(
            id=str(deployment.id),
            strategy_name=deployment.strategy_name,
            account_id=deployment.account_id,
            symbols_json=deployment.symbols,
            timeframe=deployment.timeframe,
            status=deployment.status,
            signal_source=source_name,
            signal_source_type=source_type,
            signal_source_id=source_id,
            symbol_filter_json=symbol_filter,
            signal_config_json=deployment.signal_config or {},
            settings_json=deployment.settings or {},
            created_at=now,
            updated_at=now,
        )

    def to_live_deployment(self) -> LiveDeploymentConfig:
        source_type = str(self.signal_source_type or self.signal_source or "internal").lower()
        source_name = str(self.signal_source or source_type or "internal").lower()
        return LiveDeploymentConfig(
            id=self.id,
            strategy_name=self.strategy_name,
            account_id=self.account_id,
            symbols=self.symbols_json or [],
            timeframe=self.timeframe,
            status=self.status,
            signal_source_type=source_type if source_type in {"internal", "discord"} else "internal",
            signal_source_id=str(self.signal_source_id or "").strip() or None,
            symbol_filter=[
                str(symbol).strip().upper()
                for symbol in (self.symbol_filter_json or [])
                if str(symbol).strip()
            ],
            signal_source=source_name if source_name in {"internal", "discord"} else "internal",
            signal_config=self.signal_config_json or {},
            settings=self.settings_json or {},
        )


class BackgroundTaskRecord(SQLModel, table=True):
    __tablename__ = "background_tasks"

    id: str = Field(primary_key=True)
    type: str = Field(index=True)
    status: str = Field(index=True)
    config_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("config_json", JSON, nullable=False),
    )
    progress_message: str = Field(default="")
    name: str = Field(default="")
    description: str = Field(default="")
    source: str = Field(default="")
    created_at: str = Field(default="", index=True)
    updated_at: str = Field(default="", index=True)


class VerificationEvidenceModel(SQLModel, table=True):
    __tablename__ = "verification_evidence"

    id: str = Field(primary_key=True)
    run_id: str = Field(
        sa_column=Column(String, nullable=False, unique=True, index=True),
    )
    component: str = Field(default="", index=True)
    flow_name: str = Field(default="", index=True)
    category: str = Field(default="", index=True)
    status: str = Field(default=VerificationEvidenceStatus.SKIPPED.value, index=True)
    started_at: str = Field(default="", index=True)
    finished_at: str = Field(default="", index=True)
    duration_seconds: float | None = Field(default=None)
    git_sha: str = Field(default="", index=True)
    environment: str = Field(default="", index=True)
    mode: str = Field(default="", index=True)
    metadata_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSON, nullable=False),
    )
    error_summary: str = Field(default="")
    evidence_ref: str = Field(default="")
    manual_validation_required: bool = Field(default=False, index=True)
    source: str = Field(default="", index=True)
    created_at: str = Field(default="", index=True)
    updated_at: str = Field(default="", index=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_duration_seconds(
    *,
    started_at: str,
    finished_at: str,
    duration_seconds: float | None,
) -> float | None:
    if duration_seconds is not None:
        try:
            return max(float(duration_seconds), 0.0)
        except (TypeError, ValueError):
            return None
    started_dt = _parse_iso_datetime(started_at)
    finished_dt = _parse_iso_datetime(finished_at)
    if started_dt is None or finished_dt is None:
        return None
    return max((finished_dt - started_dt).total_seconds(), 0.0)


def _detect_git_sha() -> str:
    global _CACHED_GIT_SHA
    if _CACHED_GIT_SHA is not None:
        return _CACHED_GIT_SHA

    env_sha = str(os.environ.get("GIT_SHA", "")).strip()
    if env_sha:
        _CACHED_GIT_SHA = env_sha
        return _CACHED_GIT_SHA

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        _CACHED_GIT_SHA = ""
        return _CACHED_GIT_SHA

    _CACHED_GIT_SHA = result.stdout.strip() if result.returncode == 0 else ""
    return _CACHED_GIT_SHA


def _normalize_evidence_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    allowed = {item.value for item in VerificationEvidenceStatus}
    if normalized not in allowed:
        raise ValueError(f"Invalid verification evidence status '{value}'")
    return normalized


def _serialize_verification_evidence(row: VerificationEvidenceModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id or ""),
        "component": str(row.component or ""),
        "flow_name": str(row.flow_name or ""),
        "category": str(row.category or ""),
        "status": str(row.status or VerificationEvidenceStatus.SKIPPED.value),
        "started_at": str(row.started_at or ""),
        "finished_at": str(row.finished_at or ""),
        "duration_seconds": (
            float(row.duration_seconds) if row.duration_seconds is not None else None
        ),
        "git_sha": str(row.git_sha or ""),
        "environment": str(row.environment or ""),
        "mode": str(row.mode or ""),
        "metadata": dict(row.metadata_json or {}),
        "error_summary": str(row.error_summary or "") or None,
        "evidence_ref": str(row.evidence_ref or "") or None,
        "manual_validation_required": bool(row.manual_validation_required),
        "source": str(row.source or ""),
        "created_at": str(row.created_at or ""),
        "updated_at": str(row.updated_at or ""),
    }


def _normalize_task_record(row: Any) -> BackgroundTaskRecord | None:
    if row is None:
        return None
    if isinstance(row, BackgroundTaskRecord):
        return row
    if hasattr(row, "_mapping"):
        values = list(getattr(row, "_mapping").values())
        if values:
            return _normalize_task_record(values[0])
        return None
    if isinstance(row, (tuple, list)):
        return _normalize_task_record(row[0]) if row else None
    return None


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(ENGINE) as session:
        yield session


def init_db() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return

    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SQLModel.metadata.create_all(ENGINE)
        _migrate_schema()
        _DB_INITIALIZED = True


def _table_column_names(connection: Connection, table_name: str) -> set[str]:
    rows = connection.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
    return {str(row[1]) for row in rows}


def _migrate_schema() -> None:
    with ENGINE.begin() as connection:
        deployment_column_names = _table_column_names(connection, "live_deployments")
        if "settings" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN settings JSON NOT NULL DEFAULT '{}'"
                )
            )
        if "signal_source" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'internal'"
                )
            )
        if "signal_config" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN signal_config JSON NOT NULL DEFAULT '{}'"
                )
            )
        if "signal_source_type" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN signal_source_type TEXT NOT NULL DEFAULT 'internal'"
                )
            )
            connection.execute(
                text(
                    "UPDATE live_deployments "
                    "SET signal_source_type = LOWER(COALESCE(signal_source, 'internal')) "
                    "WHERE signal_source_type = 'internal'"
                )
            )
        if "signal_source_id" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN signal_source_id TEXT NOT NULL DEFAULT ''"
                )
            )
        if "symbol_filter" not in deployment_column_names:
            connection.execute(
                text(
                    "ALTER TABLE live_deployments "
                    "ADD COLUMN symbol_filter JSON NOT NULL DEFAULT '[]'"
                )
            )

        backtest_column_names = _table_column_names(connection, "backtests")
        if backtest_column_names and "tags" not in backtest_column_names:
            connection.execute(
                text(
                    "ALTER TABLE backtests "
                    "ADD COLUMN tags JSON NOT NULL DEFAULT '[]'"
                )
            )

        account_column_names = _table_column_names(connection, "live_accounts")
        if "ctid" not in account_column_names:
            connection.execute(text("ALTER TABLE live_accounts ADD COLUMN ctid TEXT"))
        if "broker_login" not in account_column_names:
            connection.execute(text("ALTER TABLE live_accounts ADD COLUMN broker_login TEXT"))

        task_column_names = _table_column_names(connection, "background_tasks")
        if task_column_names:
            if "progress_message" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN progress_message TEXT NOT NULL DEFAULT ''")
                )
            if "name" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                )
            if "description" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                )
            if "source" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN source TEXT NOT NULL DEFAULT ''")
                )
            if "created_at" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
                )
            if "updated_at" not in task_column_names:
                connection.execute(
                    text("ALTER TABLE background_tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                )

        trade_column_names = _table_column_names(connection, "trades")
        if trade_column_names:
            if "position_size" not in trade_column_names:
                connection.execute(text("ALTER TABLE trades ADD COLUMN position_size FLOAT"))
            if "risk_amount" not in trade_column_names:
                connection.execute(text("ALTER TABLE trades ADD COLUMN risk_amount FLOAT"))
            if "risk_percent" not in trade_column_names:
                connection.execute(text("ALTER TABLE trades ADD COLUMN risk_percent FLOAT"))
            if "exit_reason" not in trade_column_names:
                connection.execute(text("ALTER TABLE trades ADD COLUMN exit_reason TEXT"))

        live_signal_column_names = _table_column_names(connection, "live_signals")
        if live_signal_column_names and "source" not in live_signal_column_names:
            connection.execute(
                text("ALTER TABLE live_signals ADD COLUMN source TEXT NOT NULL DEFAULT 'internal'")
            )


def save_live_signal(signal: LiveSignal) -> None:
    init_db()
    with get_session() as session:
        session.merge(LiveSignalModel.from_live_signal(signal))
        session.commit()


def save_live_execution(execution: LiveExecution) -> None:
    init_db()
    with get_session() as session:
        session.add(LiveExecutionModel.from_live_execution(execution))
        session.commit()


def save_live_balance(record: LiveBalanceRecord) -> None:
    init_db()
    with get_session() as session:
        session.add(LiveBalanceModel.from_live_balance(record))
        session.commit()


def list_live_accounts() -> list[LiveAccountConfig]:
    init_db()
    with get_session() as session:
        rows = session.exec(select(LiveAccountModel).order_by(LiveAccountModel.id.asc())).all()
    return [row.to_live_account() for row in rows]


def get_live_account(account_id: str) -> LiveAccountConfig | None:
    init_db()
    with get_session() as session:
        row = session.get(LiveAccountModel, account_id)
        if row is None:
            return None
    return row.to_live_account()


def ensure_live_account(
    account_id: str,
    broker: str,
    *,
    name: str = "",
    ctid: str | None = None,
    broker_login: str | None = None,
) -> LiveAccountConfig:
    init_db()
    with get_session() as session:
        current = session.get(LiveAccountModel, account_id)
        if current is None:
            account = LiveAccountModel(
                id=account_id,
                broker=broker,
                name=name or account_id,
                ctid=(ctid.strip() if isinstance(ctid, str) and ctid.strip() else None),
                broker_login=(
                    broker_login.strip()
                    if isinstance(broker_login, str) and broker_login.strip()
                    else None
                ),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.to_live_account()

        changed = False
        if broker and current.broker != broker:
            current.broker = broker
            changed = True
        if name and current.name != name:
            current.name = name
            changed = True
        normalized_ctid = ctid.strip() if isinstance(ctid, str) and ctid.strip() else None
        if ctid is not None and current.ctid != normalized_ctid:
            current.ctid = normalized_ctid
            changed = True
        normalized_broker_login = (
            broker_login.strip()
            if isinstance(broker_login, str) and broker_login.strip()
            else None
        )
        if broker_login is not None and current.broker_login != normalized_broker_login:
            current.broker_login = normalized_broker_login
            changed = True
        if changed:
            session.add(current)
            session.commit()
            session.refresh(current)
        return current.to_live_account()


def upsert_live_accounts(accounts: list[LiveAccountConfig]) -> None:
    init_db()
    with get_session() as session:
        for account in accounts:
            current = session.get(LiveAccountModel, account.id)
            if current is None:
                session.add(LiveAccountModel.from_live_account(account))
                continue
            current.broker = account.broker
            current.name = account.name
            current.ctid = account.ctid
            current.broker_login = account.broker_login
            session.add(current)
        session.commit()


def _normalize_service_type(value: ServiceType | str) -> str:
    normalized = (
        str(value.value if isinstance(value, ServiceType) else value or "").strip().lower()
    )
    if normalized not in {ServiceType.DISCORD_BOT.value, ServiceType.SCHEDULED_JOB.value}:
        raise ValueError(f"Invalid service type '{value}'")
    return normalized


def _normalize_service_status(value: ServiceStatus | str) -> str:
    normalized = (
        str(value.value if isinstance(value, ServiceStatus) else value or "").strip().lower()
    )
    if normalized not in {
        ServiceStatus.RUNNING.value,
        ServiceStatus.PAUSED.value,
        ServiceStatus.FAILED.value,
        ServiceStatus.STOPPED.value,
    }:
        raise ValueError(f"Invalid service status '{value}'")
    return normalized


def create_service(service: Service) -> Service:
    init_db()
    normalized_name = str(service.name or "").strip()
    if not normalized_name:
        raise ValueError("Service name is required")
    normalized_type = _normalize_service_type(service.type)
    normalized_status = _normalize_service_status(service.status)
    with get_session() as session:
        duplicate = session.exec(
            select(ServiceModel)
            .where(ServiceModel.name == normalized_name)
            .where(ServiceModel.type == normalized_type)
            .limit(1)
        ).first()
        if duplicate is not None:
            raise ValueError(
                f"Service '{normalized_name}' with type '{normalized_type}' already exists"
            )
        record = ServiceModel.from_service(service)
        record.name = normalized_name
        record.type = normalized_type
        record.status = normalized_status
        record.config_json = dict(service.config or {})
        if service.last_activity is not None:
            record.last_activity = service.last_activity
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_service()


def get_service(service_id: str) -> Service | None:
    normalized_id = str(service_id or "").strip()
    if not normalized_id:
        return None
    init_db()
    with get_session() as session:
        row = session.get(ServiceModel, normalized_id)
        if row is None:
            return None
    return row.to_service()


def list_services(*, service_type: ServiceType | str | None = None) -> list[Service]:
    init_db()
    with get_session() as session:
        statement = select(ServiceModel)
        if service_type is not None:
            statement = statement.where(ServiceModel.type == _normalize_service_type(service_type))
        rows = session.exec(
            statement.order_by(ServiceModel.created_at.desc(), ServiceModel.id.desc())
        ).all()
    return [row.to_service() for row in rows]


def update_service_status(
    service_id: str,
    status: ServiceStatus | str,
    *,
    last_activity: datetime | None = None,
) -> Service | None:
    normalized_id = str(service_id or "").strip()
    if not normalized_id:
        return None
    normalized_status = _normalize_service_status(status)
    init_db()
    with get_session() as session:
        record = session.get(ServiceModel, normalized_id)
        if record is None:
            return None
        record.status = normalized_status
        if last_activity is not None:
            record.last_activity = last_activity
        record.updated_at = datetime.utcnow()
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_service()


def delete_service(service_id: str) -> bool:
    normalized_id = str(service_id or "").strip()
    if not normalized_id:
        return False
    init_db()
    with get_session() as session:
        record = session.get(ServiceModel, normalized_id)
        if record is None:
            return False
        session.delete(record)
        session.commit()
    return True


def list_live_deployments() -> list[LiveDeploymentConfig]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(LiveDeploymentModel).order_by(
                LiveDeploymentModel.created_at.desc(),
                LiveDeploymentModel.id.desc(),
            )
        ).all()
    return [row.to_live_deployment() for row in rows]


def get_live_deployment(deployment_id: str) -> LiveDeploymentConfig | None:
    init_db()
    with get_session() as session:
        row = session.get(LiveDeploymentModel, deployment_id)
        if row is None:
            return None
    return row.to_live_deployment()


def create_live_deployment(deployment: LiveDeploymentConfig) -> LiveDeploymentConfig:
    init_db()
    with get_session() as session:
        account = session.get(LiveAccountModel, deployment.account_id)
        if account is None:
            raise ValueError(f"Unknown live account '{deployment.account_id}'")
        deployment_status = str(deployment.status or "ACTIVE").upper()
        if deployment_status not in {"ACTIVE", "PAUSED", "STOPPED"}:
            raise ValueError(f"Invalid deployment status '{deployment.status}'")
        signal_source_type = str(deployment.signal_source_type or "").lower()
        signal_source_name = str(deployment.signal_source or "").lower()
        if signal_source_type not in {"internal", "discord"}:
            signal_source_type = (
                signal_source_name if signal_source_name in {"internal", "discord"} else "internal"
            )
        elif signal_source_type == "internal" and signal_source_name == "discord":
            signal_source_type = "discord"
        if signal_source_type not in {"internal", "discord"}:
            raise ValueError(
                f"Invalid signal source type '{deployment.signal_source_type or deployment.signal_source}'"
            )
        signal_source = signal_source_name if signal_source_name in {"internal", "discord"} else signal_source_type
        if signal_source not in {"internal", "discord"}:
            signal_source = signal_source_type
        signal_source_id = str(deployment.signal_source_id or "").strip()
        symbol_filter = [
            str(symbol).strip().upper()
            for symbol in (deployment.symbol_filter or [])
            if str(symbol).strip()
        ]
        record = LiveDeploymentModel.from_live_deployment(deployment)
        record.status = deployment_status
        record.signal_source = signal_source
        record.signal_source_type = signal_source_type
        record.signal_source_id = signal_source_id
        record.symbol_filter_json = symbol_filter
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_live_deployment()


def update_live_deployment_status(
    deployment_id: str, status: str
) -> LiveDeploymentConfig | None:
    next_status = str(status or "").upper()
    if next_status not in {"ACTIVE", "PAUSED", "STOPPED"}:
        raise ValueError(f"Invalid live deployment status '{status}'")
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, deployment_id)
        if record is None:
            return None
        record.status = next_status
        record.updated_at = datetime.utcnow()
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_live_deployment()


def delete_live_deployment(deployment_id: str) -> bool:
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, deployment_id)
        if record is None:
            return False
        session.delete(record)
        session.commit()
    return True


def list_live_deployments_for_account(account_id: str) -> list[LiveDeploymentConfig]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(LiveDeploymentModel)
            .where(LiveDeploymentModel.account_id == account_id)
            .order_by(LiveDeploymentModel.created_at.desc(), LiveDeploymentModel.id.desc())
        ).all()
    return [row.to_live_deployment() for row in rows]


def list_live_balances(account_id: str) -> list[LiveBalanceRecord]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(LiveBalanceModel)
            .where(LiveBalanceModel.account_id == account_id)
            .order_by(LiveBalanceModel.timestamp.asc(), LiveBalanceModel.id.asc())
        ).all()
    return [row.to_live_balance() for row in rows]


def get_latest_live_balance(account_id: str) -> LiveBalanceRecord | None:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return None

    init_db()
    with get_session() as session:
        row = session.exec(
            select(LiveBalanceModel)
            .where(LiveBalanceModel.account_id == normalized_account_id)
            .order_by(LiveBalanceModel.timestamp.desc(), LiveBalanceModel.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    return row.to_live_balance()


def list_live_signal_executions_for_deployments(
    deployment_ids: list[str],
) -> list[dict[str, Any]]:
    normalized_ids = sorted({item.strip() for item in deployment_ids if item and item.strip()})
    if not normalized_ids:
        return []

    init_db()
    with get_session() as session:
        rows = session.exec(
            select(LiveSignalModel, LiveExecutionModel)
            .join(LiveExecutionModel, LiveExecutionModel.signal_id == LiveSignalModel.id)
            .where(LiveSignalModel.deployment_id.in_(normalized_ids))
            .order_by(LiveExecutionModel.timestamp.desc(), LiveExecutionModel.id.desc())
        ).all()

    return _serialize_live_signal_execution_rows(rows)


def list_live_signals_for_deployments(
    deployment_ids: list[str],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    normalized_ids = sorted({item.strip() for item in deployment_ids if item and item.strip()})
    if not normalized_ids:
        return []

    try:
        bounded_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        bounded_limit = 200

    init_db()
    with get_session() as session:
        signal_rows = session.exec(
            select(LiveSignalModel)
            .where(LiveSignalModel.deployment_id.in_(normalized_ids))
            .order_by(LiveSignalModel.timestamp.desc(), LiveSignalModel.id.desc())
            .limit(bounded_limit)
        ).all()
        if not signal_rows:
            return []

        signal_ids = [str(row.id) for row in signal_rows]
        execution_rows = session.exec(
            select(LiveExecutionModel)
            .where(LiveExecutionModel.signal_id.in_(signal_ids))
            .order_by(LiveExecutionModel.timestamp.desc(), LiveExecutionModel.id.desc())
        ).all()

    latest_execution_by_signal: dict[str, LiveExecutionModel] = {}
    for execution in execution_rows:
        signal_id = str(execution.signal_id)
        if signal_id not in latest_execution_by_signal:
            latest_execution_by_signal[signal_id] = execution

    payload: list[dict[str, Any]] = []
    for signal in signal_rows:
        execution = latest_execution_by_signal.get(str(signal.id))
        payload.append(
            {
                "signal_id": str(signal.id),
                "deployment_id": str(signal.deployment_id),
                "symbol": str(signal.symbol or ""),
                "action": str(signal.action or ""),
                "source": str(signal.source or "internal"),
                "theoretical_price": float(signal.theoretical_price or 0.0),
                "signal_timestamp": signal.timestamp,
                "signal_metadata": signal.metadata_json or {},
                "execution_id": str(execution.id) if execution is not None else None,
                "execution_timestamp": (
                    execution.timestamp if execution is not None else None
                ),
                "actual_price": (
                    float(execution.actual_price or 0.0) if execution is not None else None
                ),
                "filled_qty": (
                    float(execution.filled_qty or 0.0) if execution is not None else None
                ),
                "commission": (
                    float(execution.commission or 0.0) if execution is not None else None
                ),
                "broker_order_id": (
                    str(execution.broker_order_id or "") if execution is not None else None
                ),
            }
        )
    return payload


def list_live_signals_for_replay(
    *,
    deployment_ids: list[str] | None = None,
    source: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    normalized_ids = sorted(
        {str(item).strip() for item in (deployment_ids or []) if str(item).strip()}
    )

    normalized_source = str(source or "").strip().lower()
    if normalized_source and (
        normalized_source != "internal" and not normalized_source.startswith("discord")
    ):
        raise ValueError(f"Invalid signal source '{source}'")

    try:
        bounded_limit = max(1, min(int(limit), 20000))
    except (TypeError, ValueError):
        bounded_limit = 5000

    init_db()
    with get_session() as session:
        statement = select(LiveSignalModel).order_by(
            LiveSignalModel.timestamp.asc(),
            LiveSignalModel.id.asc(),
        )
        if normalized_ids:
            statement = statement.where(LiveSignalModel.deployment_id.in_(normalized_ids))
        if normalized_source == "internal":
            statement = statement.where(LiveSignalModel.source == "internal")
        elif normalized_source == "discord":
            statement = statement.where(
                or_(
                    LiveSignalModel.source == "discord",
                    LiveSignalModel.source.like("discord_%"),
                )
            )
        elif normalized_source:
            statement = statement.where(LiveSignalModel.source == normalized_source)
        if start is not None:
            statement = statement.where(LiveSignalModel.timestamp >= start)
        if end is not None:
            statement = statement.where(LiveSignalModel.timestamp <= end)
        rows = session.exec(statement.limit(bounded_limit)).all()

    payload: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row.metadata_json or {})
        payload.append(
            {
                "signal_id": str(row.id),
                "deployment_id": str(row.deployment_id),
                "symbol": str(row.symbol or "").upper(),
                "action": str(row.action or "").lower(),
                "source": str(row.source or "internal").lower(),
                "timestamp": row.timestamp,
                "entry_price": float(row.theoretical_price or 0.0),
                "stop_loss": metadata.get("stop_loss"),
                "take_profit": metadata.get("take_profit"),
                "timeframe": str(metadata.get("timeframe") or ""),
                "metadata": metadata,
            }
        )
    return payload


def _serialize_live_signal_execution_rows(rows: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for signal, execution in rows:
        theoretical = float(signal.theoretical_price or 0.0)
        actual = float(execution.actual_price or 0.0)
        slippage = actual - theoretical
        slippage_bps = 0.0 if theoretical == 0 else (slippage / theoretical) * 10000.0
        payload.append(
            {
                "signal_id": signal.id,
                "execution_id": execution.id,
                "deployment_id": signal.deployment_id,
                "symbol": signal.symbol,
                "action": signal.action,
                "source": signal.source,
                "theoretical_price": theoretical,
                "actual_price": actual,
                "slippage": slippage,
                "slippage_bps": slippage_bps,
                "filled_qty": float(execution.filled_qty or 0.0),
                "commission": float(execution.commission or 0.0),
                "signal_timestamp": signal.timestamp,
                "execution_timestamp": execution.timestamp,
                "broker_order_id": execution.broker_order_id,
                "signal_metadata": signal.metadata_json or {},
            }
        )
    return payload


def load_live_account_stats_inputs(
    account_id: str,
) -> tuple[list[LiveBalanceRecord], list[dict[str, Any]]]:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return [], []

    init_db()
    with get_session() as session:
        balance_rows = session.exec(
            select(LiveBalanceModel)
            .where(LiveBalanceModel.account_id == normalized_account_id)
            .order_by(LiveBalanceModel.timestamp.asc(), LiveBalanceModel.id.asc())
        ).all()

        deployment_rows = session.exec(
            select(LiveDeploymentModel)
            .where(LiveDeploymentModel.account_id == normalized_account_id)
            .order_by(LiveDeploymentModel.created_at.asc(), LiveDeploymentModel.id.asc())
        ).all()

        deployment_ids = {normalized_account_id}
        for deployment in deployment_rows:
            deployment_ids.add(str(deployment.id))
            strategy_name = str(deployment.strategy_name or "").strip()
            if strategy_name:
                deployment_ids.add(strategy_name)

        execution_rows: list[Any] = []
        if deployment_ids:
            execution_rows = session.exec(
                select(LiveSignalModel, LiveExecutionModel)
                .join(LiveExecutionModel, LiveExecutionModel.signal_id == LiveSignalModel.id)
                .where(LiveSignalModel.deployment_id.in_(sorted(deployment_ids)))
                .order_by(LiveExecutionModel.timestamp.asc(), LiveExecutionModel.id.asc())
            ).all()

    return (
        [row.to_live_balance() for row in balance_rows],
        _serialize_live_signal_execution_rows(execution_rows),
    )


def save_result_to_sqlite(result: BacktestResult) -> None:
    init_db()
    with get_session() as session:
        backtest_id = result.meta.id
        session.exec(delete(TradeRecord).where(TradeRecord.backtest_id == backtest_id))
        session.exec(delete(EquityPoint).where(EquityPoint.backtest_id == backtest_id))
        session.exec(delete(DrawingRecord).where(DrawingRecord.backtest_id == backtest_id))
        session.exec(delete(IndicatorRecord).where(IndicatorRecord.backtest_id == backtest_id))

        backtest_record = BacktestRecord(
            id=backtest_id,
            strategies=result.meta.strategies,
            engine=result.meta.engine,
            backtest_start=result.meta.backtest_start,
            backtest_end=result.meta.backtest_end,
            data=result.meta.data,
            initial_capital=result.meta.initial_capital,
            parameters=result.meta.parameters,
            tags_json=result.meta.tags or [],
            stats=result.stats or {},
            notes=result.meta.notes,
            created_at=result.meta.created_at,
        )
        session.merge(backtest_record)

        for trade in result.trades:
            exit_reason = trade.exit_reason
            if exit_reason is None and trade.exits:
                exit_reason = trade.exits[-1].reason
            session.add(
                TradeRecord(
                    backtest_id=backtest_id,
                    trade_nr=trade.id,
                    symbol=trade.symbol,
                    direction=trade.direction,
                    entry_time=trade.entry_time,
                    entry_price=trade.entry_price,
                    quantity=trade.quantity,
                    position_size=trade.position_size,
                    risk_amount=trade.risk_amount,
                    risk_percent=trade.risk_percent,
                    exits=[exit_item.to_dict() for exit_item in trade.exits],
                    pnl=trade.pnl,
                    pnl_pct=trade.pnl_pct,
                    fees=trade.fees,
                    net_pnl=trade.net_pnl,
                    r_multiple=trade.r_multiple,
                    mae=trade.mae,
                    mfe=trade.mfe,
                    holding_bars=trade.holding_bars,
                    exit_reason=exit_reason,
                    meta=trade.meta,
                    tags=trade.tags,
                )
            )

        for point in result.equity_curve:
            session.add(EquityPoint(backtest_id=backtest_id, time=point.time, equity=point.equity))

        for drawing in result.drawings:
            drawing_payload = dict(drawing)
            session.add(
                DrawingRecord(
                    backtest_id=backtest_id,
                    trade_nr=drawing_payload.get("trade_nr"),
                    type=str(drawing_payload.get("type", "")),
                    data=drawing_payload,
                )
            )

        for idx, indicator in enumerate(result.indicators):
            session.add(
                IndicatorRecord(
                    backtest_id=backtest_id,
                    series_nr=idx,
                    data=indicator.to_dict(),
                )
            )

        session.commit()


def migrate_json_to_sqlite() -> dict[str, int]:
    init_db()
    backtests_dir = DATA_DIR / "backtests"
    summary = {"migrated": 0, "failed": 0, "total": 0}

    if not backtests_dir.exists():
        return summary

    for path in sorted(backtests_dir.glob("*.trades.json")):
        summary["total"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = BacktestResult.from_dict(payload)
            save_result_to_sqlite(result)
            summary["migrated"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.warning("Could not migrate %s: %s", path, exc)

    if summary["total"]:
        logger.info(
            "JSON→SQLite migration finished: total=%s migrated=%s failed=%s",
            summary["total"],
            summary["migrated"],
            summary["failed"],
        )
    return summary


def list_backtest_ids() -> list[str]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(BacktestRecord.id).order_by(BacktestRecord.created_at.desc())
        ).all()
    ids: list[str] = []
    for row in rows:
        if isinstance(row, str):
            ids.append(row)
            continue
        if row is None:
            continue
        if hasattr(row, "_mapping"):
            mapping = getattr(row, "_mapping")
            value = mapping.get("id")
            if value:
                ids.append(str(value))
                continue
        if isinstance(row, (tuple, list)) and row:
            if row[0]:
                ids.append(str(row[0]))
    return ids


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _safe_stat_number(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if numeric != numeric:  # NaN guard
        return default
    return numeric


def _backtest_metadata_payload(backtest: BacktestRecord, trade_count: int) -> dict[str, Any]:
    data = backtest.data or {}
    stats = backtest.stats or {}
    strategies = _as_string_list(backtest.strategies)
    timeframes = _as_string_list(data.get("timeframes"))
    symbols = _as_string_list(data.get("symbols"))
    strategy_name = strategies[0] if strategies else "unknown"
    notes = str(backtest.notes or "").strip()
    display_name = notes or strategy_name
    timeframe = timeframes[0] if timeframes else ""
    tags = _as_string_list(backtest.tags_json)

    return {
        "id": str(backtest.id),
        "engine": str(backtest.engine or "unknown"),
        "strategy_name": strategy_name,
        "display_name": display_name,
        "notes": notes,
        "symbols": symbols,
        "timeframe": timeframe,
        "start_date": str(backtest.backtest_start or ""),
        "end_date": str(backtest.backtest_end or ""),
        "total_return": _safe_stat_number(stats.get("total_return"), 0.0),
        "cagr": _safe_stat_number(stats.get("cagr"), 0.0),
        "sharpe_ratio": _safe_stat_number(stats.get("sharpe_ratio"), 0.0),
        "psr": _safe_stat_number(stats.get("psr"), 0.0),
        "p_value": _safe_stat_number(stats.get("p_value"), 1.0),
        "max_drawdown": _safe_stat_number(stats.get("max_drawdown_pct"), 0.0),
        "win_rate": stats.get("win_rate"),
        "trade_count": max(int(trade_count), 0),
        "created_at": str(backtest.created_at or ""),
        "tags": tags,
    }


def _backtest_fingerprint_payload(backtest: BacktestRecord) -> dict[str, Any]:
    data = backtest.data or {}
    strategies = _as_string_list(backtest.strategies)
    timeframes = _as_string_list(data.get("timeframes"))
    symbols = _as_string_list(data.get("symbols"))
    strategy_name = strategies[0] if strategies else "unknown"
    timeframe = timeframes[0] if timeframes else ""
    return {
        "id": str(backtest.id),
        "strategy_name": strategy_name,
        "symbols": symbols,
        "timeframe": timeframe,
        "start_date": str(backtest.backtest_start or ""),
        "end_date": str(backtest.backtest_end or ""),
        "engine": str(backtest.engine or "unknown"),
        "created_at": str(backtest.created_at or ""),
        "tags": _as_string_list(backtest.tags_json),
    }


def _extract_backtest_and_trade_count(row: Any) -> tuple[BacktestRecord | None, int]:
    if isinstance(row, BacktestRecord):
        return row, 0

    if hasattr(row, "_mapping"):
        values = list(getattr(row, "_mapping").values())
    elif isinstance(row, (tuple, list)):
        values = list(row)
    else:
        values = [row]

    backtest: BacktestRecord | None = None
    trade_count = 0

    for value in values:
        if isinstance(value, BacktestRecord):
            backtest = value
            continue
        if value is None:
            continue
        try:
            trade_count = int(value)
        except (TypeError, ValueError):
            continue

    return backtest, max(trade_count, 0)


def list_backtest_metadata_rows() -> list[dict[str, Any]]:
    init_db()
    trade_counts = (
        select(
            TradeRecord.backtest_id,
            func.count(TradeRecord.id).label("trade_count"),
        )
        .group_by(TradeRecord.backtest_id)
        .subquery()
    )
    with get_session() as session:
        rows = session.exec(
            select(BacktestRecord, trade_counts.c.trade_count)
            .outerjoin(trade_counts, trade_counts.c.backtest_id == BacktestRecord.id)
            .order_by(BacktestRecord.created_at.desc(), BacktestRecord.id.desc())
        ).all()

    payload: list[dict[str, Any]] = []
    for row in rows:
        backtest, trade_count = _extract_backtest_and_trade_count(row)
        if backtest is None:
            continue
        payload.append(_backtest_metadata_payload(backtest, trade_count))
    return payload


def list_backtest_fingerprint_rows() -> list[dict[str, Any]]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(BacktestRecord).order_by(BacktestRecord.created_at.desc(), BacktestRecord.id.desc())
        ).all()

    payload: list[dict[str, Any]] = []
    for row in rows:
        backtest = row if isinstance(row, BacktestRecord) else None
        if backtest is None and hasattr(row, "_mapping"):
            values = list(getattr(row, "_mapping").values())
            if values and isinstance(values[0], BacktestRecord):
                backtest = values[0]
        if backtest is None and isinstance(row, (tuple, list)) and row and isinstance(row[0], BacktestRecord):
            backtest = row[0]
        if backtest is None:
            continue
        payload.append(_backtest_fingerprint_payload(backtest))
    return payload


def load_backtest_metadata_row(backtest_id: str) -> dict[str, Any] | None:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return None

    init_db()
    with get_session() as session:
        backtest = session.get(BacktestRecord, normalized_id)
        if backtest is None:
            return None
        trade_count = session.exec(
            select(func.count(TradeRecord.id)).where(TradeRecord.backtest_id == normalized_id)
        ).one()
    return _backtest_metadata_payload(backtest, int(trade_count or 0))


def upsert_verification_evidence(
    *,
    component: str,
    flow_name: str,
    category: str,
    status: VerificationEvidenceStatus | str,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    git_sha: str | None = None,
    environment: str | None = None,
    mode: str | None = None,
    metadata: dict[str, Any] | None = None,
    error_summary: str | None = None,
    evidence_ref: str | None = None,
    manual_validation_required: bool = False,
    source: str = "",
    run_id: str | None = None,
) -> VerificationEvidenceRecord:
    normalized_component = str(component or "").strip()
    if not normalized_component:
        raise ValueError("component is required")
    normalized_flow_name = str(flow_name or "").strip()
    if not normalized_flow_name:
        raise ValueError("flow_name is required")
    normalized_category = str(category or "").strip().lower()
    if not normalized_category:
        raise ValueError("category is required")

    status_value = status.value if isinstance(status, VerificationEvidenceStatus) else str(status)
    normalized_status = _normalize_evidence_status(status_value)
    now_iso = _now_iso()
    normalized_run_id = str(run_id or "").strip() or str(uuid4())
    normalized_metadata = dict(metadata or {})
    normalized_source = str(source or "").strip().lower()
    normalized_environment = str(environment or os.environ.get("APP_ENV", "local")).strip().lower()
    normalized_mode = str(mode or "").strip().lower()
    normalized_git_sha = str(git_sha or _detect_git_sha()).strip()
    normalized_error = str(error_summary or "").strip()
    normalized_evidence_ref = str(evidence_ref or "").strip()
    init_db()
    with get_session() as session:
        existing = session.exec(
            select(VerificationEvidenceModel)
            .where(VerificationEvidenceModel.run_id == normalized_run_id)
            .limit(1)
        ).first()
        record = existing if isinstance(existing, VerificationEvidenceModel) else None
        if record is None:
            record = VerificationEvidenceModel(
                id=str(uuid4()),
                run_id=normalized_run_id,
                created_at=now_iso,
            )

        started_value = str(started_at or "").strip()
        if not started_value:
            started_value = str(record.started_at or "").strip() or now_iso
        finished_value = str(finished_at or "").strip()
        if not finished_value:
            finished_value = str(record.finished_at or "").strip() or now_iso

        safe_duration = _safe_duration_seconds(
            started_at=started_value,
            finished_at=finished_value,
            duration_seconds=duration_seconds,
        )
        if (
            duration_seconds is None
            and not str(started_at or "").strip()
            and not str(finished_at or "").strip()
            and record.duration_seconds is not None
        ):
            safe_duration = float(record.duration_seconds)

        record.component = normalized_component
        record.flow_name = normalized_flow_name
        record.category = normalized_category
        record.status = normalized_status
        record.started_at = started_value
        record.finished_at = finished_value
        record.duration_seconds = safe_duration
        record.git_sha = normalized_git_sha
        record.environment = normalized_environment
        record.mode = normalized_mode
        record.metadata_json = normalized_metadata
        record.error_summary = normalized_error
        record.evidence_ref = normalized_evidence_ref
        record.manual_validation_required = bool(manual_validation_required)
        record.source = normalized_source
        record.updated_at = now_iso
        if not str(record.created_at or "").strip():
            record.created_at = now_iso

        session.add(record)
        session.commit()
        session.refresh(record)
        payload = _serialize_verification_evidence(record)
    return VerificationEvidenceRecord.model_validate(payload)


def list_verification_evidence_records(
    *,
    component: str | None = None,
    category: str | None = None,
    status: VerificationEvidenceStatus | str | None = None,
    limit: int = 200,
) -> list[VerificationEvidenceRecord]:
    init_db()
    try:
        bounded_limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        bounded_limit = 200

    normalized_component = str(component or "").strip()
    normalized_category = str(category or "").strip().lower()
    normalized_status = (
        _normalize_evidence_status(status.value if isinstance(status, VerificationEvidenceStatus) else str(status))
        if status is not None and str(status).strip()
        else ""
    )

    with get_session() as session:
        statement = select(VerificationEvidenceModel)
        if normalized_component:
            statement = statement.where(VerificationEvidenceModel.component == normalized_component)
        if normalized_category:
            statement = statement.where(VerificationEvidenceModel.category == normalized_category)
        if normalized_status:
            statement = statement.where(VerificationEvidenceModel.status == normalized_status)
        rows = session.exec(
            statement.order_by(
                VerificationEvidenceModel.finished_at.desc(),
                VerificationEvidenceModel.started_at.desc(),
                VerificationEvidenceModel.updated_at.desc(),
            ).limit(bounded_limit)
        ).all()

    payload: list[VerificationEvidenceRecord] = []
    for row in rows:
        if not isinstance(row, VerificationEvidenceModel):
            continue
        payload.append(VerificationEvidenceRecord.model_validate(_serialize_verification_evidence(row)))
    return payload


def list_verification_evidence_summary(
    *,
    component: str | None = None,
    category: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    records = list_verification_evidence_records(
        component=component,
        category=category,
        limit=max(limit * 20, 400),
    )
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (record.component, record.flow_name, record.category)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "component": record.component,
                "flow_name": record.flow_name,
                "category": record.category,
                "run_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "partial_count": 0,
                "skipped_count": 0,
                "last_status": record.status.value,
                "last_run_at": record.finished_at or record.started_at or record.updated_at,
                "last_success_at": "",
                "last_error": "",
                "latest_git_sha": record.git_sha,
                "latest_environment": record.environment,
                "latest_mode": record.mode,
                "manual_validation_required": bool(record.manual_validation_required),
            }
            grouped[key] = bucket
        bucket["run_count"] += 1
        bucket[f"{record.status.value}_count"] += 1
        bucket["manual_validation_required"] = bool(bucket["manual_validation_required"]) or bool(
            record.manual_validation_required
        )
        if record.status == VerificationEvidenceStatus.SUCCESS and not bucket["last_success_at"]:
            bucket["last_success_at"] = record.finished_at or record.started_at or record.updated_at
        if not bucket["last_error"] and record.error_summary:
            bucket["last_error"] = record.error_summary

    items = list(grouped.values())
    items.sort(key=lambda item: str(item.get("last_run_at") or ""), reverse=True)
    bounded = max(1, min(int(limit), 2000))
    return items[:bounded]


def _task_status_to_evidence_status(task_status: str) -> str:
    return _TASK_STATUS_TO_EVIDENCE_STATUS.get(
        str(task_status or "").strip().lower(),
        VerificationEvidenceStatus.SKIPPED.value,
    )


def _task_requires_manual_validation(category: str) -> bool:
    normalized = str(category or "").strip().lower()
    if not normalized:
        return False
    return normalized in _MANUAL_VALIDATION_CATEGORIES


def _record_verification_evidence_for_task(
    *,
    task_id: str,
    task_type: str,
    status: str,
    config_json: dict[str, Any],
    progress_message: str,
    name: str,
    description: str,
    source: str,
    created_at: str,
    updated_at: str,
) -> None:
    category = str(config_json.get("category") or task_type or "task").strip().lower()
    evidence_status = _task_status_to_evidence_status(status)
    started_at = str(config_json.get("started_at") or created_at or "").strip()
    finished_at = str(config_json.get("finished_at") or updated_at or "").strip()
    error_summary = ""
    if evidence_status in {VerificationEvidenceStatus.FAILED.value, VerificationEvidenceStatus.PARTIAL.value}:
        error_summary = str(config_json.get("error") or progress_message or "").strip()

    metadata = {
        "task_id": task_id,
        "task_type": task_type,
        "task_status": status,
        "task_name": name,
        "task_description": description,
        "task_source": source,
        "progress_message": progress_message,
        "total_items": config_json.get("total_items"),
        "result": config_json.get("result"),
    }
    evidence_ref = str(config_json.get("log_file") or "").strip()
    upsert_verification_evidence(
        run_id=f"task:{task_id}",
        component=str(source or task_type or category or "task").strip().lower(),
        flow_name=str(name or task_type or "background-task").strip(),
        category=category or "task",
        status=evidence_status,
        started_at=started_at or created_at,
        finished_at=finished_at or updated_at,
        duration_seconds=config_json.get("duration_seconds"),
        mode=str(config_json.get("control_mode") or "").strip().lower(),
        metadata=metadata,
        error_summary=error_summary or None,
        evidence_ref=evidence_ref or None,
        manual_validation_required=_task_requires_manual_validation(category),
        source=source or "task",
    )


def upsert_background_task_record(
    *,
    task_id: str,
    task_type: str,
    status: str,
    config_json: dict[str, Any] | None = None,
    progress_message: str = "",
    name: str = "",
    description: str = "",
    source: str = "",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    normalized_id = str(task_id or "").strip()
    if not normalized_id:
        raise ValueError("task_id is required")

    normalized_type = str(task_type or "").strip() or "unknown"
    normalized_status = str(status or "").strip() or "unknown"
    normalized_config = dict(config_json or {})
    now_iso = _now_iso()
    created_value = str(created_at or "").strip() or now_iso
    updated_value = str(updated_at or "").strip() or now_iso

    init_db()
    with get_session() as session:
        record = session.get(BackgroundTaskRecord, normalized_id)
        if record is None:
            record = BackgroundTaskRecord(
                id=normalized_id,
                type=normalized_type,
                status=normalized_status,
                config_json=normalized_config,
                progress_message=str(progress_message or ""),
                name=str(name or ""),
                description=str(description or ""),
                source=str(source or ""),
                created_at=created_value,
                updated_at=updated_value,
            )
        else:
            record.type = normalized_type
            record.status = normalized_status
            record.config_json = normalized_config
            record.progress_message = str(progress_message or "")
            record.name = str(name or "")
            record.description = str(description or "")
            record.source = str(source or "")
            if created_at:
                record.created_at = created_value
            elif not str(record.created_at or "").strip():
                record.created_at = created_value
            record.updated_at = updated_value
        session.add(record)
        session.commit()

    if normalized_status in _FINAL_TASK_STATUSES:
        try:
            _record_verification_evidence_for_task(
                task_id=normalized_id,
                task_type=normalized_type,
                status=normalized_status,
                config_json=normalized_config,
                progress_message=str(progress_message or ""),
                name=str(name or ""),
                description=str(description or ""),
                source=str(source or ""),
                created_at=created_value,
                updated_at=updated_value,
            )
        except Exception as exc:
            logger.warning(
                "Failed to record verification evidence for task %s: %s",
                normalized_id,
                exc,
            )


def get_background_task_record(task_id: str) -> dict[str, Any] | None:
    normalized_id = str(task_id or "").strip()
    if not normalized_id:
        return None

    init_db()
    with get_session() as session:
        record = session.get(BackgroundTaskRecord, normalized_id)
    if record is None:
        return None
    return {
        "id": record.id,
        "type": record.type,
        "status": record.status,
        "config_json": dict(record.config_json or {}),
        "progress_message": str(record.progress_message or ""),
        "name": str(record.name or ""),
        "description": str(record.description or ""),
        "source": str(record.source or ""),
        "created_at": str(record.created_at or ""),
        "updated_at": str(record.updated_at or ""),
    }


def list_background_task_records(*, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_session() as session:
        statement = select(BackgroundTaskRecord)
        if statuses:
            normalized_statuses = [str(status).strip() for status in statuses if str(status).strip()]
            if normalized_statuses:
                statement = statement.where(BackgroundTaskRecord.status.in_(normalized_statuses))
        rows = session.exec(
            statement.order_by(BackgroundTaskRecord.created_at.desc(), BackgroundTaskRecord.id.desc())
        ).all()

    payload: list[dict[str, Any]] = []
    for row in rows:
        record = _normalize_task_record(row)
        if record is None:
            continue
        payload.append(
            {
                "id": record.id,
                "type": record.type,
                "status": record.status,
                "config_json": dict(record.config_json or {}),
                "progress_message": str(record.progress_message or ""),
                "name": str(record.name or ""),
                "description": str(record.description or ""),
                "source": str(record.source or ""),
                "created_at": str(record.created_at or ""),
                "updated_at": str(record.updated_at or ""),
            }
        )
    return payload


def mark_background_tasks_interrupted(
    *,
    from_statuses: list[str] | tuple[str, ...] = ("queued", "running"),
) -> list[dict[str, Any]]:
    normalized = [str(status).strip() for status in from_statuses if str(status).strip()]
    if not normalized:
        return []

    init_db()
    now_iso = _now_iso()
    with get_session() as session:
        rows = session.exec(
            select(BackgroundTaskRecord).where(BackgroundTaskRecord.status.in_(normalized))
        ).all()
        interrupted: list[dict[str, Any]] = []
        for row in rows:
            record = _normalize_task_record(row)
            if record is None:
                continue
            record.status = "interrupted"
            record.progress_message = "Interrupted by server restart"
            record.updated_at = now_iso
            session.add(record)
            interrupted.append(
                {
                    "id": record.id,
                    "type": record.type,
                    "status": record.status,
                    "config_json": dict(record.config_json or {}),
                    "progress_message": str(record.progress_message or ""),
                    "name": str(record.name or ""),
                    "description": str(record.description or ""),
                    "source": str(record.source or ""),
                    "created_at": str(record.created_at or ""),
                    "updated_at": str(record.updated_at or ""),
                }
            )
        session.commit()
    return interrupted


def delete_background_task_record(task_id: str) -> bool:
    normalized_id = str(task_id or "").strip()
    if not normalized_id:
        return False

    init_db()
    with get_session() as session:
        record = session.get(BackgroundTaskRecord, normalized_id)
        if record is None:
            return False
        session.delete(record)
        session.commit()
    return True


def delete_backtest(backtest_id: str) -> bool:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return False

    init_db()
    with get_session() as session:
        backtest = session.get(BacktestRecord, normalized_id)
        if backtest is None:
            return False
        session.exec(delete(TradeRecord).where(TradeRecord.backtest_id == normalized_id))
        session.exec(delete(EquityPoint).where(EquityPoint.backtest_id == normalized_id))
        session.exec(delete(DrawingRecord).where(DrawingRecord.backtest_id == normalized_id))
        session.exec(delete(IndicatorRecord).where(IndicatorRecord.backtest_id == normalized_id))
        session.delete(backtest)
        session.commit()
    return True


def load_backtest_equity_points(backtest_id: str) -> list[EquityPointModel]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return []

    init_db()
    with get_session() as session:
        rows = session.exec(
            select(EquityPoint)
            .where(EquityPoint.backtest_id == normalized_id)
            .order_by(EquityPoint.time.asc(), EquityPoint.id.asc())
        ).all()

    points: list[EquityPointModel] = []
    for raw_row in rows:
        if raw_row is None:
            continue
        if isinstance(raw_row, EquityPoint):
            row = raw_row
        elif hasattr(raw_row, "_mapping"):
            values = list(getattr(raw_row, "_mapping").values())
            row = values[0] if values else None
        elif isinstance(raw_row, (tuple, list)):
            row = raw_row[0] if raw_row else None
        else:
            row = raw_row
        if not isinstance(row, EquityPoint):
            continue
        points.append(EquityPointModel(time=row.time, equity=row.equity))
    return points


def load_backtest_equity_preview_points(backtest_id: str, limit: int = 80) -> list[EquityPointModel]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return []

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 80
    normalized_limit = max(1, min(normalized_limit, 200))

    init_db()
    with get_session() as session:
        rows = session.exec(
            select(EquityPoint)
            .where(EquityPoint.backtest_id == normalized_id)
            .order_by(EquityPoint.time.desc(), EquityPoint.id.desc())
            .limit(normalized_limit)
        ).all()

    points: list[EquityPointModel] = []
    for raw_row in reversed(rows):
        if raw_row is None:
            continue
        if isinstance(raw_row, EquityPoint):
            row = raw_row
        elif hasattr(raw_row, "_mapping"):
            values = list(getattr(raw_row, "_mapping").values())
            row = values[0] if values else None
        elif isinstance(raw_row, (tuple, list)):
            row = raw_row[0] if raw_row else None
        else:
            row = raw_row
        if not isinstance(row, EquityPoint):
            continue
        points.append(EquityPointModel(time=row.time, equity=row.equity))
    return points


def load_backtest_equity_preview_map(
    backtest_ids: list[str],
    limit: int = 50,
) -> dict[str, list[float]]:
    normalized_ids = [str(backtest_id).strip() for backtest_id in backtest_ids if str(backtest_id).strip()]
    if not normalized_ids:
        return {}

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 200))

    init_db()
    params: dict[str, Any] = {"limit": normalized_limit}
    placeholders: list[str] = []
    for index, backtest_id in enumerate(normalized_ids):
        param_name = f"id_{index}"
        params[param_name] = backtest_id
        placeholders.append(f":{param_name}")

    ranked_query = text(
        f"""
        WITH ranked AS (
            SELECT
                backtest_id,
                time,
                equity,
                ROW_NUMBER() OVER (
                    PARTITION BY backtest_id
                    ORDER BY time DESC, id DESC
                ) AS row_number
            FROM equity_points
            WHERE backtest_id IN ({", ".join(placeholders)})
        )
        SELECT backtest_id, time, equity
        FROM ranked
        WHERE row_number <= :limit
        ORDER BY backtest_id ASC, time ASC
        """
    )

    with get_session() as session:
        rows = session.exec(ranked_query, params=params).all()

    previews: dict[str, list[float]] = {backtest_id: [] for backtest_id in normalized_ids}
    for row in rows:
        if hasattr(row, "_mapping"):
            mapping = getattr(row, "_mapping")
            backtest_id = str(mapping.get("backtest_id") or "").strip()
            equity_value = mapping.get("equity")
        elif isinstance(row, (tuple, list)):
            backtest_id = str(row[0] or "").strip() if len(row) > 0 else ""
            equity_value = row[2] if len(row) > 2 else None
        else:
            continue
        if not backtest_id or backtest_id not in previews:
            continue
        try:
            previews[backtest_id].append(float(equity_value))
        except (TypeError, ValueError):
            continue

    return previews


def load_backtest_result(backtest_id: str) -> BacktestResult | None:
    init_db()
    with get_session() as session:
        backtest = session.get(BacktestRecord, backtest_id)
        if backtest is None:
            return None

        trade_rows = session.exec(
            select(TradeRecord)
            .where(TradeRecord.backtest_id == backtest_id)
            .order_by(TradeRecord.trade_nr.asc(), TradeRecord.id.asc())
        ).all()
        equity_rows = session.exec(
            select(EquityPoint)
            .where(EquityPoint.backtest_id == backtest_id)
            .order_by(EquityPoint.time.asc(), EquityPoint.id.asc())
        ).all()
        drawing_rows = session.exec(
            select(DrawingRecord)
            .where(DrawingRecord.backtest_id == backtest_id)
            .order_by(DrawingRecord.id.asc())
        ).all()
        indicator_rows = session.exec(
            select(IndicatorRecord)
            .where(IndicatorRecord.backtest_id == backtest_id)
            .order_by(IndicatorRecord.series_nr.asc(), IndicatorRecord.id.asc())
        ).all()

    def _unwrap_model_row(row: Any) -> Any:
        if row is None:
            return None
        if hasattr(row, "_mapping"):
            mapping = getattr(row, "_mapping")
            values = list(mapping.values())
            return values[0] if values else None
        if isinstance(row, (tuple, list)):
            return row[0] if row else None
        return row

    trades: list[Trade] = []
    for raw_row in trade_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        exits_payload = row.exits or []
        exits = [TradeExit.from_dict(item) for item in exits_payload]
        position_size = row.position_size
        risk_amount = row.risk_amount
        risk_percent = row.risk_percent
        if isinstance(row.meta, dict):
            risk_math = row.meta.get("risk_math")
            if isinstance(risk_math, dict):
                if position_size is None:
                    position_size = risk_math.get("position_size")
                if risk_amount is None:
                    risk_amount = risk_math.get("risk_target_usd")
                if risk_percent is None:
                    risk_percent = risk_math.get("risk_target_pct")
                    if risk_percent is None:
                        max_position_pct = risk_math.get("max_position_pct")
                        if max_position_pct is not None:
                            risk_percent = max_position_pct
        trades.append(
            Trade(
                id=row.trade_nr,
                symbol=row.symbol,
                direction=row.direction,
                entry_time=row.entry_time,
                entry_price=row.entry_price,
                quantity=row.quantity,
                exits=exits,
                pnl=row.pnl,
                position_size=position_size,
                risk_amount=risk_amount,
                risk_percent=risk_percent,
                exit_reason=row.exit_reason,
                pnl_pct=row.pnl_pct,
                fees=row.fees,
                net_pnl=row.net_pnl,
                r_multiple=row.r_multiple,
                mae=row.mae,
                mfe=row.mfe,
                holding_bars=row.holding_bars,
                tags=row.tags or [],
                meta=row.meta or {},
            )
        )

    equity_curve = []
    for raw_row in equity_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        equity_curve.append(EquityPointModel(time=row.time, equity=row.equity))

    meta = BacktestMeta(
        id=backtest.id,
        created_at=backtest.created_at,
        engine=backtest.engine,
        strategies=backtest.strategies or [],
        backtest_start=backtest.backtest_start,
        backtest_end=backtest.backtest_end,
        data=backtest.data or {},
        initial_capital=backtest.initial_capital,
        parameters=backtest.parameters or {},
        tags=backtest.tags_json or [],
        notes=backtest.notes or "",
    )

    drawings: list[dict[str, Any]] = []
    for raw_row in drawing_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        if row.data:
            drawings.append(row.data)

    indicators = []
    for raw_row in indicator_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        if row.data:
            indicators.append(row.data)

    return BacktestResult(
        meta=meta,
        trades=trades,
        equity_curve=equity_curve,
        stats=backtest.stats or {},
        indicators=[IndicatorSeries.from_dict(item) for item in indicators],
        drawings=drawings,
    )


def save_portfolio(portfolio: PortfolioModel) -> None:
    normalized_id = str(portfolio.id or "").strip()
    if not normalized_id:
        raise ValueError("Portfolio id is required")

    init_db()
    with get_session() as session:
        session.exec(delete(PortfolioComponentRecord).where(PortfolioComponentRecord.portfolio_id == normalized_id))

        record = session.get(PortfolioRecord, normalized_id)
        if record is None:
            record = PortfolioRecord.from_portfolio(portfolio)
            session.add(record)
        else:
            record.name = portfolio.name
            record.created_at = portfolio.created_at
            record.account_capital = portfolio.account_capital
            record.combined_stats = portfolio.combined_stats or {}
            record.combined_equity = portfolio.combined_equity or []
            record.strategy_contributions = portfolio.strategy_contributions or []
            record.component_contributions = portfolio.component_contributions or []
            record.trades = portfolio.trades or []
            session.add(record)
        session.flush()

        for index, component in enumerate(portfolio.components, start=1):
            component_id = f"component-{index:03d}"
            if isinstance(component.settings, dict):
                raw_component_id = component.settings.get("component_id")
                if isinstance(raw_component_id, str) and raw_component_id.strip():
                    component_id = raw_component_id.strip()
            settings = dict(component.settings or {})
            settings.pop("component_id", None)
            session.add(
                PortfolioComponentRecord.from_model(
                    portfolio_id=normalized_id,
                    component_id=component_id,
                    component=PortfolioComponentModel(
                        strategy_name=component.strategy_name,
                        symbol=component.symbol,
                        timeframe=component.timeframe,
                        weight=component.weight,
                        risk_config=component.risk_config or {},
                        settings=settings,
                        backtest_id=component.backtest_id,
                    ),
                )
            )
        session.commit()


def load_portfolio(portfolio_id: str) -> PortfolioModel | None:
    normalized_id = str(portfolio_id or "").strip()
    if not normalized_id:
        return None

    init_db()
    with get_session() as session:
        portfolio_row = session.get(PortfolioRecord, normalized_id)
        if portfolio_row is None:
            return None
        component_rows = session.exec(
            select(PortfolioComponentRecord)
            .where(PortfolioComponentRecord.portfolio_id == normalized_id)
            .order_by(PortfolioComponentRecord.id.asc())
        ).all()

    components = [row for row in component_rows if isinstance(row, PortfolioComponentRecord)]
    portfolio = portfolio_row.to_portfolio(components)
    for index, component in enumerate(portfolio.components, start=1):
        try:
            source_component = components[index - 1]
        except IndexError:
            source_component = None
        if source_component is None:
            continue
        settings = dict(component.settings or {})
        settings.setdefault("component_id", source_component.component_id or f"component-{index:03d}")
        component.settings = settings
    return portfolio


def list_portfolios() -> list[PortfolioModel]:
    init_db()
    with get_session() as session:
        portfolio_rows = session.exec(
            select(PortfolioRecord).order_by(PortfolioRecord.created_at.desc(), PortfolioRecord.id.desc())
        ).all()
        component_rows = session.exec(
            select(PortfolioComponentRecord).order_by(
                PortfolioComponentRecord.portfolio_id.asc(),
                PortfolioComponentRecord.id.asc(),
            )
        ).all()

    by_portfolio: dict[str, list[PortfolioComponentRecord]] = {}
    for row in component_rows:
        if not isinstance(row, PortfolioComponentRecord):
            continue
        by_portfolio.setdefault(row.portfolio_id, []).append(row)

    payload: list[PortfolioModel] = []
    for row in portfolio_rows:
        if not isinstance(row, PortfolioRecord):
            continue
        components = by_portfolio.get(row.id, [])
        portfolio = row.to_portfolio(components)
        for index, component in enumerate(portfolio.components, start=1):
            source = components[index - 1] if index - 1 < len(components) else None
            settings = dict(component.settings or {})
            settings.setdefault("component_id", source.component_id if source is not None else f"component-{index:03d}")
            component.settings = settings
        payload.append(portfolio)
    return payload


def delete_portfolio(portfolio_id: str) -> bool:
    normalized_id = str(portfolio_id or "").strip()
    if not normalized_id:
        return False

    init_db()
    with get_session() as session:
        portfolio = session.get(PortfolioRecord, normalized_id)
        if portfolio is None:
            return False
        session.exec(delete(PortfolioComponentRecord).where(PortfolioComponentRecord.portfolio_id == normalized_id))
        session.delete(portfolio)
        session.commit()
    return True

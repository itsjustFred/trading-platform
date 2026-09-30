from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_URL, uuid4
from uuid import uuid5 as _uuid5

from sqlalchemy import (
    JSON,
    Column,
    String,
    TextClause,
    UniqueConstraint,
    delete,
    event,
    func,
    or_,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlmodel.sql.expression import SelectOfScalar

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
    LiveSignal,
    Service,
    ServiceStatus,
    ServiceType,
    Trade,
    TradeExit,
    VerificationEvidenceRecord,
    VerificationEvidenceStatus,
    resolve_defined_stat,
)
from shared.models import (
    EquityPoint as EquityPointModel,
)
from shared.models import (
    Portfolio as PortfolioModel,
)
from shared.models import (
    PortfolioComponent as PortfolioComponentModel,
)

logger = get_logger("database")

# shared.constants loads .env exactly once (see its module docstring); importing
# DATA_DIR from it here makes that ordering dependency explicit instead of
# relying on callers importing shared.constants first.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# .env is loaded exactly once, in shared.constants, before any module reads
# os.environ for configuration. Do not add load_dotenv() here: it would freeze
# configuration at a different point in the import order and could disagree
# with the DATA_DIR this module already resolved.
DATA_DIR = RESOLVED_DATA_DIR
DB_PATH = DATA_DIR / "backtests.db"
# Optional engine override. New code should call get_engine(); importing
# ENGINE no longer forces engine creation at import time. Tests that need a
# bespoke engine set this attribute directly (it is part of the module
# contract, so monkeypatching it does not materialize hidden state).
ENGINE: Any = None


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _default_database_url() -> str:
    return _sqlite_url(DATA_DIR / "backtests.db")


def _is_sqlite_url(url: str) -> bool:
    return url.strip().lower().startswith("sqlite")


def _create_database_engine() -> Any:
    url = os.environ.get("DATABASE_URL") or _default_database_url()
    # SQLite-specific connect args (and the WAL pragma listener) are only
    # meaningful for SQLite: passing check_same_thread/timeout to another
    # dialect is an error.
    connect_args = (
        {"check_same_thread": False, "timeout": 30.0} if _is_sqlite_url(url) else {}
    )
    engine = create_engine(url, echo=False, connect_args=connect_args)
    if _is_sqlite_url(url):
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


_ENGINE: Any = None
_ENGINE_LOCK = threading.Lock()
_WARNED_DRIFT = False


def _warn_if_data_dir_drifted() -> None:
    """Warn when this module's DATA_DIR diverged from shared.constants'.

    ``shared.constants.set_data_dir()`` is the only supported way to move the
    data directory: it re-binds ``database.DATA_DIR``/``DB_PATH`` and resets the
    engine. A direct ``constants.DATA_DIR = ...`` assignment (or any future
    second writer) leaves this module's by-value copy behind — every engine and
    session then silently keeps reading/writing the OLD directory. The check is
    warn-once and never raises: production behaviour must not depend on it, but
    the drift must be visible in the logs.
    """
    global _WARNED_DRIFT
    if _WARNED_DRIFT:
        return
    try:
        from shared import constants as _constants
    except Exception:  # pragma: no cover - constants is always importable here
        return
    if DATA_DIR != _constants.DATA_DIR:
        _WARNED_DRIFT = True
        logger.warning(
            "DATA_DIR drift detected: shared.database is using %s while "
            "shared.constants uses %s. Rebind via shared.constants.set_data_dir() "
            "only — direct assignment does not update the database paths.",
            DATA_DIR,
            _constants.DATA_DIR,
        )


def get_engine() -> Any:
    """Return the process-wide SQLAlchemy engine, creating it on first use.

    Lazy creation keeps imports side-effect free (no engine bound to a
    possibly stale DATA_DIR) while guaranteeing a single engine per process.
    An explicit ``ENGINE`` override (set by tests or embedders via
    ``shared.database.ENGINE = engine``) always wins — it is used as handed
    over, so the override owns its own pragmas (the test fixtures register
    ``_configure_sqlite_connection`` themselves).
    """
    global _ENGINE
    if ENGINE is not None:
        return ENGINE
    _warn_if_data_dir_drifted()
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                # _create_database_engine registers the SQLite pragma listener
                # itself; no second listener here (it would fire twice).
                _ENGINE = _create_database_engine()
    return _ENGINE


def reset_engine() -> None:
    """Dispose and forget the lazily created engine (test isolation helper)."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            with contextlib.suppress(Exception):
                _ENGINE.dispose()
        _ENGINE = None


def reset_initialized() -> None:
    """Forget that the schema was initialized (test isolation helper)."""
    global _DB_INITIALIZED
    with _DB_INIT_LOCK:
        _DB_INITIALIZED = False


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
_EXECUTABLE_COMMAND_KEYS = frozenset(
    {"argv", "cmd", "command", "commandline", "entrypoint", "executable", "shell"}
)
TASK_STATUS_VALUES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "partial",
        "skipped",
        "manual_required",
        "cleanup_pending",
        "cleanup_exhausted",
    }
)
ACTIVE_TASK_STATUSES = frozenset({"queued", "starting", "running", "paused"})
FINAL_TASK_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "partial",
        "skipped",
        "manual_required",
        "cleanup_pending",
        "cleanup_exhausted",
    }
)
_FINAL_TASK_STATUSES = FINAL_TASK_STATUSES
_TASK_STATUS_TO_EVIDENCE_STATUS = {
    "queued": VerificationEvidenceStatus.PARTIAL.value,
    "starting": VerificationEvidenceStatus.PARTIAL.value,
    "running": VerificationEvidenceStatus.PARTIAL.value,
    "paused": VerificationEvidenceStatus.PARTIAL.value,
    "completed": VerificationEvidenceStatus.SUCCESS.value,
    "failed": VerificationEvidenceStatus.FAILED.value,
    "cancelled": VerificationEvidenceStatus.SKIPPED.value,
    "interrupted": VerificationEvidenceStatus.FAILED.value,
    "partial": VerificationEvidenceStatus.PARTIAL.value,
    "skipped": VerificationEvidenceStatus.SKIPPED.value,
    "manual_required": VerificationEvidenceStatus.FAILED.value,
    "cleanup_pending": VerificationEvidenceStatus.PARTIAL.value,
    "cleanup_exhausted": VerificationEvidenceStatus.FAILED.value,
}


def normalize_task_status(status: object) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in TASK_STATUS_VALUES:
        raise ValueError(f"unknown task status: {status!r}")
    return normalized


def _strip_executable_command_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_executable_command_fields(child)
            for key, child in value.items()
            if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in _EXECUTABLE_COMMAND_KEYS
        }
    if isinstance(value, list):
        return [_strip_executable_command_fields(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_strip_executable_command_fields(child) for child in value)
    return value


def is_final_task_status(status: object) -> bool:
    """True when ``status`` is a known final background-task status."""
    try:
        return normalize_task_status(status) in FINAL_TASK_STATUSES
    except ValueError:
        return False


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
    equity_curve: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column("equity_curve", JSON, nullable=False)
    )


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


_BACKTESTS_FK = "backtests.id"


class TradeRecord(SQLModel, table=True):
    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key=_BACKTESTS_FK, index=True)
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
    backtest_id: str = Field(foreign_key=_BACKTESTS_FK, index=True)
    time: str = Field(sa_column=Column(String, index=True))
    equity: float = Field(default=0.0)


class DrawingRecord(SQLModel, table=True):
    __tablename__ = "drawings"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key=_BACKTESTS_FK, index=True)
    trade_nr: int | None = Field(default=None, index=True)
    type: str = Field(default="", index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class IndicatorRecord(SQLModel, table=True):
    __tablename__ = "indicators"

    id: int | None = Field(default=None, primary_key=True)
    backtest_id: str = Field(foreign_key=_BACKTESTS_FK, index=True)
    series_nr: int = Field(default=0, index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class GridRecord(SQLModel, table=True):
    """Model for grid strategy tester results stored in SQLite."""

    __tablename__ = "grid_results"

    id: str = Field(primary_key=True)
    strategy: str = Field(index=True)
    created_at: str = Field(default="", index=True)
    symbols_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("symbols", JSON, nullable=False),
    )
    timeframes_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("timeframes", JSON, nullable=False),
    )
    results_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("results", JSON, nullable=False),
    )
    engine: str = Field(default="vectorbt", index=True)


def _utcnow_naive() -> datetime:
    """Timezone-aware UTC now, stored naive to keep the existing schema."""
    return utcnow_naive()


def utcnow_naive() -> datetime:
    """The clock the claim-lease timestamps are written with.

    Exposed so a caller comparing a lease's ``expires_at`` against "now" uses
    this clock rather than reimplementing the naive-UTC conversion, and cannot
    silently disagree with the timestamps it is reading.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _coerce_naive_utc(value: Any) -> datetime | None:
    """Read a naive-UTC timestamp out of a column, or None when unusable.

    Legacy SQLite rows can hold a DATETIME column as text, so a stamp written by
    an older build is still readable here. Anything else — a bool, a number, a
    string that is not a timestamp, None — becomes None, which every safety gate
    treats as "not verified".
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _normalize_optional_id(value: Any) -> str | None:
    """Normalize an optional external id to a real ``None`` or a clean string.

    Missing values (``None``), empty strings and whitespace-only strings all
    collapse to ``None`` so they stay real SQL ``NULL`` (never the literal
    ``"None"``). Any present value becomes its stripped string form.
    """
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


class LiveSignalModel(SQLModel, table=True):
    __tablename__ = "live_signals"

    id: str = Field(primary_key=True, default_factory=lambda: str(uuid4()))
    deployment_id: str = Field(index=True)
    symbol: str = Field(index=True)
    action: str = Field(index=True)
    theoretical_price: float = Field(default=0.0)
    timestamp: datetime = Field(index=True)
    source: str = Field(default="internal", index=True)
    broker_order_id: str = Field(default="", index=True)
    discord_message_id: str | None = Field(
        default=None,
        sa_column=Column("discord_message_id", String, nullable=True, index=True),
    )
    # C1: idempotency key for non-Discord signals (internal/replay). Discord
    # redeliveries dedup on (deployment_id, discord_message_id); everything
    # else carries a producer-stable key here so a redelivery returns the
    # canonical row instead of opening a second broker order.
    idempotency_key: str | None = Field(
        default=None,
        sa_column=Column("idempotency_key", String, nullable=True, index=True),
    )
    metadata_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("metadata", JSON, nullable=False)
    )

    @classmethod
    def from_live_signal(cls, signal: LiveSignal) -> "LiveSignalModel":
        discord_msg_id = _normalize_optional_id(signal.metadata.get("discord_message_id"))
        broker_order_id = _normalize_optional_id(signal.metadata.get("broker_order_id")) or ""
        idempotency_key = _normalize_optional_id(signal.metadata.get("idempotency_key"))

        return cls(
            id=str(signal.id),
            deployment_id=signal.deployment_id,
            symbol=signal.symbol,
            action=signal.action,
            theoretical_price=signal.theoretical_price,
            timestamp=(
                signal.timestamp.replace(tzinfo=timezone.utc)
                if signal.timestamp.tzinfo is None
                else signal.timestamp.astimezone(timezone.utc)
            ),
            source=str(signal.metadata.get("source", "internal")),
            broker_order_id=broker_order_id or "",
            discord_message_id=discord_msg_id,
            idempotency_key=idempotency_key,
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
                **(
                    {"discord_message_id": self.discord_message_id}
                    if self.discord_message_id
                    else {}
                ),
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
    dedupe_key: str | None = Field(
        default=None,
        sa_column=Column("dedupe_key", String, nullable=True, unique=True, index=True),
    )

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
            dedupe_key=(str(execution.dedupe_key).strip() or None)
            if execution.dedupe_key is not None
            else None,
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
            dedupe_key=self.dedupe_key,
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
    is_demo: bool = Field(default=False, index=True)
    # When is_demo was last confirmed against the broker. ACTIVE transitions are
    # only allowed while this is fresh, so a demo flag cannot be trusted forever
    # after the account is re-pointed or the credentials are rotated.
    demo_verified_at: datetime | None = Field(default=None, index=True)
    # Broker-reported identity, captured at verification time. Persisting it lets
    # the daemon notice a mismatch between what we verified and what we later see.
    identity_fingerprint: str | None = Field(default=None)

    @classmethod
    def from_live_account(cls, account: LiveAccountConfig) -> "LiveAccountModel":
        return cls(
            id=account.id,
            broker=account.broker,
            name=account.name,
            ctid=account.ctid,
            broker_login=account.broker_login,
            # A brand-new row is never trusted on the strength of a payload: it
            # has no verification, so `is_demo` only decides what an operator
            # sees. It cannot be armed until the broker verification is recorded.
            is_demo=bool(account.is_demo),
        )

    def to_live_account(self) -> LiveAccountConfig:
        return LiveAccountConfig(
            id=self.id,
            broker=self.broker,
            name=self.name,
            ctid=self.ctid or "",
            broker_login=self.broker_login or "",
            is_demo=bool(self.is_demo),
            demo_verified_at=self.demo_verified_at_as_datetime(),
            identity_fingerprint=self.identity_fingerprint,
        )

    def demo_verified_at_as_datetime(self) -> datetime | None:
        """The verification stamp as a datetime, or None when unreadable.

        A legacy row can hold the stamp as text. An unreadable stamp is reported
        as None ("no verification") rather than guessed at, so every caller that
        reads it fails closed. Gates that must distinguish "never verified" from
        "corrupt stamp" read ``demo_verified_at`` off the row directly.
        """
        return _coerce_naive_utc(self.demo_verified_at)


class LiveControlAuditModel(SQLModel, table=True):
    """Durable record of every state-changing live control.

    Arming, stopping, re-pointing and deleting a live deployment are the
    operations with real consequences, and until now nothing recorded that they
    happened. The request log is a debug line: it is not queryable, it does not
    survive a log rotation, and it cannot answer "what changed, and when".

    ``actor`` is attribution by claim, not authentication - this platform has no
    auth, so the value comes from an operator-supplied header and is only as
    trustworthy as the operator. It is recorded rather than inferred because an
    unattributed change is worse than a self-declared one.
    """

    __tablename__ = "live_control_audit"

    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow_naive, index=True)
    actor: str = Field(default="unknown", index=True)
    action: str = Field(index=True)
    target_type: str = Field(index=True)
    target_id: str = Field(index=True)
    succeeded: bool = Field(default=True, index=True)
    detail_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("detail", JSON, nullable=False),
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
    last_attempt_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow_naive, index=True)
    updated_at: datetime = Field(default_factory=_utcnow_naive, index=True)

    @classmethod
    def from_service(cls, service: Service) -> "ServiceModel":
        now = _utcnow_naive()
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
            last_attempt_at=self.last_attempt_at,
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
    stopped_enforced_at: datetime | None = Field(default=None, index=True)
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
    created_at: datetime = Field(default_factory=_utcnow_naive, index=True)
    updated_at: datetime = Field(default_factory=_utcnow_naive, index=True)

    @classmethod
    def from_live_deployment(cls, deployment: LiveDeploymentConfig) -> "LiveDeploymentModel":
        now = _utcnow_naive()
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
            # Preserved across a status write, so stamping the enforcement does
            # not reset it. A deployment that is stopped, enforced and then
            # paused keeps its stamp; stopping it again clears it explicitly.
            stopped_enforced_at=deployment.stopped_enforced_at,
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
            # Read back, so the daemon's startup ledger can be rebuilt from the
            # database rather than starting empty on every process.
            stopped_enforced_at=self.stopped_enforced_at,
        )


class LiveDeploymentSubmissionClaimModel(SQLModel, table=True):
    __tablename__ = "live_deployment_submission_claims"
    __table_args__ = (
        # Exactly one live claim per deployment. The claim is what turns
        # "still ACTIVE" into a single atomic fact taken immediately before the
        # broker call, and that only holds while no second claimer can hold one
        # for the same deployment. With a non-unique index a second daemon
        # process claimed the same ACTIVE deployment and both submitted.
        # Expired leases are pruned before every claim (see
        # `claim_live_deployment_submission`), so a claimor that died cannot
        # deadlock the deployment past the lease TTL.
        UniqueConstraint(
            "deployment_id", name="uq_live_deployment_submission_claims_deployment"
        ),
    )

    token: str = Field(primary_key=True)
    deployment_id: str = Field(index=True)
    claimed_at: datetime = Field(default_factory=_utcnow_naive, index=True)
    expires_at: datetime = Field(index=True)


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


def _verification_evidence_core_fields(row: VerificationEvidenceModel) -> dict[str, Any]:
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
    }


def _verification_evidence_context_fields(
    row: VerificationEvidenceModel,
) -> dict[str, Any]:
    return {
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


def _verification_evidence_payload(row: VerificationEvidenceModel) -> dict[str, Any]:
    payload = _verification_evidence_core_fields(row)
    payload.update(_verification_evidence_context_fields(row))
    return payload


def _serialize_verification_evidence(row: VerificationEvidenceModel) -> dict[str, Any]:
    return _verification_evidence_payload(row)


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
    with Session(get_engine()) as session:
        yield session


def init_db() -> None:
    global _DB_INITIALIZED
    _warn_if_data_dir_drifted()
    if _DB_INITIALIZED:
        # The data directory may have moved (set_data_dir) since the last
        # initialization; make sure it exists before any session touches it.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return

    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SQLModel.metadata.create_all(get_engine())
        _migrate_schema()
        _DB_INITIALIZED = True


def _table_column_names(connection: Connection, table_name: str) -> set[str]:
    rows = connection.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
    return {str(row[1]) for row in rows}


def _rebuild_live_signals_without_global_discord_unique(connection: Connection) -> None:
    """Remove a SQLite table-backed global Discord UNIQUE without losing rows."""
    table_sql = connection.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'live_signals'"
        )
    ).scalar_one_or_none()
    if not table_sql:
        raise RuntimeError("Cannot rebuild live_signals: table definition is missing")

    discord_unique = re.compile(
        r",?\s*(?:CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\w+)\s+)?"
        r"UNIQUE\s*\(\s*(?:\"discord_message_id\"|`discord_message_id`|"
        r"\[discord_message_id\]|discord_message_id)\s*\)",
        flags=re.IGNORECASE,
    )
    rebuilt_sql, replacements = discord_unique.subn("", str(table_sql))
    if replacements != 1:
        raise RuntimeError(
            "Cannot safely rebuild live_signals: expected exactly one table-backed "
            "UNIQUE(discord_message_id) constraint"
        )

    index_sql = [
        str(row[0])
        for row in connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'live_signals' AND sql IS NOT NULL"
            )
        ).fetchall()
        if row[0]
    ]
    column_names = [
        str(row[1])
        for row in connection.execute(text("PRAGMA table_info('live_signals')")).fetchall()
    ]
    quoted_columns = ", ".join(
        connection.dialect.identifier_preparer.quote(column) for column in column_names
    )

    legacy_alter_table = int(
        connection.execute(text("PRAGMA legacy_alter_table")).scalar_one()
    )
    try:
        connection.execute(text("PRAGMA legacy_alter_table = ON"))
        connection.execute(text("ALTER TABLE live_signals RENAME TO live_signals__discord_legacy"))
        connection.execute(text(rebuilt_sql))
        connection.execute(
            text(
                f"INSERT INTO live_signals ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM live_signals__discord_legacy"
            )
        )
        connection.execute(text("DROP TABLE live_signals__discord_legacy"))
    finally:
        connection.execute(
            text(f"PRAGMA legacy_alter_table = {legacy_alter_table}")
        )
    for statement in index_sql:
        connection.execute(text(statement))


def _add_live_signal_identity_columns(connection: Connection, column_names: set[str]) -> None:
    """Add the columns required for deployment-scoped Discord identity."""
    if "source" not in column_names:
        connection.execute(
            text("ALTER TABLE live_signals ADD COLUMN source TEXT NOT NULL DEFAULT 'internal'")
        )
    if "broker_order_id" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_signals "
                "ADD COLUMN broker_order_id TEXT NOT NULL DEFAULT ''"
            )
        )
    if "discord_message_id" not in column_names:
        connection.execute(text("ALTER TABLE live_signals ADD COLUMN discord_message_id TEXT"))


def _drop_live_signals_global_discord_unique_indexes(connection: Connection) -> bool:
    """Drop every Discord-only global unique constraint on live_signals."""
    # Named indexes can be dropped directly; SQLite table-backed constraints
    # require one controlled transactional rebuild before the remaining
    # migration continues.
    table_rebuild_required = False
    for row in connection.execute(text("PRAGMA index_list('live_signals')")).fetchall():
        index_name = str(row[1])
        is_unique = bool(row[2])
        origin = str(row[3]) if len(row) > 3 else "c"
        index_columns = [
            str(info[2])
            for info in connection.execute(
                text(f"PRAGMA index_info({connection.dialect.identifier_preparer.quote(index_name)})")
            ).fetchall()
        ]
        if is_unique and index_columns == ["discord_message_id"]:
            if origin != "c":
                table_rebuild_required = True
                continue
            quoted_name = connection.dialect.identifier_preparer.quote(index_name)
            connection.execute(text(f"DROP INDEX {quoted_name}"))
    if table_rebuild_required:
        _rebuild_live_signals_without_global_discord_unique(connection)
    return True


def _backfill_live_signal_discord_message_ids(connection: Connection) -> None:
    """Backfill discord_message_id from signal metadata after uniqueness drops."""
    # Backfill only after global uniqueness is gone so legitimate historical
    # fan-out rows in different deployments are not rejected mid-migration.
    signals_to_fill = connection.execute(
        text(
            "SELECT id, metadata FROM live_signals "
            "WHERE discord_message_id IS NULL OR discord_message_id = ''"
        )
    ).fetchall()
    for sig_id, meta_json in signals_to_fill:
        if not meta_json:
            continue
        try:
            metadata = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
            discord_msg_id = metadata.get("discord_message_id")
        except (TypeError, ValueError, AttributeError):
            continue
        if discord_msg_id:
            connection.execute(
                text(
                    "UPDATE live_signals SET discord_message_id = :discord_message_id "
                    "WHERE id = :id"
                ),
                {"discord_message_id": str(discord_msg_id), "id": sig_id},
            )


def _raise_on_duplicate_live_signal_discord_ids(connection: Connection) -> None:
    """Refuse to create deployment-scoped uniqueness over conflicting rows."""
    duplicate = connection.execute(
        text(
            "SELECT deployment_id, discord_message_id, COUNT(*) AS duplicate_count "
            "FROM live_signals WHERE discord_message_id IS NOT NULL "
            "GROUP BY deployment_id, discord_message_id HAVING COUNT(*) > 1 "
            "LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot create deployment-scoped Discord uniqueness: live_signals "
            "contains duplicate (deployment_id, discord_message_id) execution intents. "
            "No rows were deleted; resolve the conflicting records explicitly."
        )


def _create_live_signal_discord_indexes(connection: Connection) -> None:
    """Create the non-unique and deployment-scoped unique Discord indexes."""
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_live_signals_discord_message_id "
            "ON live_signals(discord_message_id)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_live_signals_deployment_discord_msg "
            "ON live_signals(deployment_id, discord_message_id) "
            "WHERE discord_message_id IS NOT NULL"
        )
    )


def _migrate_live_signal_idempotency_key(connection: Connection) -> None:
    """Add the idempotency_key column + deployment-scoped unique index (C1).

    Non-Discord signals dedup on (deployment_id, idempotency_key) exactly
    like Discord signals dedup on (deployment_id, discord_message_id).
    """
    column_names = _table_column_names(connection, "live_signals")
    if not column_names:
        return
    if "idempotency_key" not in column_names:
        connection.execute(text("ALTER TABLE live_signals ADD COLUMN idempotency_key TEXT"))
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_live_signals_idempotency_key "
            "ON live_signals(idempotency_key)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_live_signals_deployment_idempotency_key "
            "ON live_signals(deployment_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
    )


def _migrate_live_signal_discord_identity(connection: Connection) -> None:
    """Migrate Discord execution identity from global to deployment scope."""
    column_names = _table_column_names(connection, "live_signals")
    if not column_names:
        return

    _add_live_signal_identity_columns(connection, column_names)
    _drop_live_signals_global_discord_unique_indexes(connection)
    _backfill_live_signal_discord_message_ids(connection)
    _raise_on_duplicate_live_signal_discord_ids(connection)
    _create_live_signal_discord_indexes(connection)


@contextmanager
def _schema_migration_connection() -> Iterator[Connection]:
    """Run SQLite schema changes with FK DDL disabled, then validate before commit."""
    with get_engine().connect() as connection:
        foreign_keys_enabled = int(
            connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        )
        connection.commit()
        if foreign_keys_enabled:
            connection.execute(text("PRAGMA foreign_keys = OFF"))
            connection.commit()
        try:
            # Python's sqlite3 legacy transaction mode does not necessarily emit
            # BEGIN for DDL. Start the transaction explicitly so ALTER/DROP INDEX
            # statements roll back together with data backfills on any failure.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
                if violations:
                    raise RuntimeError(
                        "Cannot safely migrate schema: foreign key violations remain"
                    )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        finally:
            if connection.in_transaction():
                connection.rollback()
            if foreign_keys_enabled:
                connection.execute(text("PRAGMA foreign_keys = ON"))
                connection.commit()


def _backfill_curve_for_id(connection: Connection, bt_id: str) -> list[dict[str, Any]]:
    """Build a missing equity_curve from equity_points, else the JSON file."""
    if _table_column_names(connection, "equity_points"):
        points = connection.execute(
            text(
                "SELECT time, equity FROM equity_points "
                "WHERE backtest_id = :id ORDER BY id"
            ),
            {"id": bt_id},
        ).fetchall()
        if points:
            return [
                {"time": str(time), "equity": float(equity)}
                for time, equity in points
            ]

    json_path = DATA_DIR / "backtests" / f"{bt_id}.trades.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        curve = payload.get("equity_curve")
        if isinstance(curve, list) and curve:
            return [dict(point) for point in curve if isinstance(point, dict)]
    return []


def _add_live_deployment_columns(connection: Connection, column_names: set[str]) -> None:
    """Add live_deployments signal-source and filter columns when missing."""
    if "settings" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN settings JSON NOT NULL DEFAULT '{}'"
            )
        )
    if "signal_source" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'internal'"
            )
        )
    if "signal_config" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN signal_config JSON NOT NULL DEFAULT '{}'"
            )
        )
    if "signal_source_type" not in column_names:
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
    if "signal_source_id" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN signal_source_id TEXT NOT NULL DEFAULT ''"
            )
        )
    if "symbol_filter" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN symbol_filter JSON NOT NULL DEFAULT '[]'"
            )
        )


def _add_backtests_tags_column(connection: Connection) -> None:
    """Add the tags column to legacy backtests tables."""
    column_names = _table_column_names(connection, "backtests")
    if column_names and "tags" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE backtests "
                "ADD COLUMN tags JSON NOT NULL DEFAULT '[]'"
            )
        )


def _add_live_deployment_enforced_column(connection: Connection) -> None:
    """Add `live_deployments.stopped_enforced_at` to legacy tables.

    Persists the STOPPED enforcement ledger, which used to be a set in daemon
    memory and so was lost on every restart - re-running cancel and flatten for
    deployments that were already stopped.
    """
    column_names = _table_column_names(connection, "live_deployments")
    if "stopped_enforced_at" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN stopped_enforced_at DATETIME"
            )
        )


def _add_service_columns(connection: Connection) -> None:
    """Add `services.last_attempt_at` to legacy tables.

    Separate from `last_activity` on purpose: that column only advances on a
    successful read, so a service failing for days shows the last time it worked.
    The two questions - "when did this last work?" and "when did anyone last try?"
    - need different answers, and conflating them makes a working retry look like
    an idle process.
    """
    column_names = _table_column_names(connection, "services")
    if "last_attempt_at" not in column_names:
        connection.execute(
            text("ALTER TABLE services ADD COLUMN last_attempt_at DATETIME")
        )


def _add_live_account_columns(connection: Connection) -> None:
    """Add broker identity columns to legacy live_accounts tables."""
    column_names = _table_column_names(connection, "live_accounts")
    if "ctid" not in column_names:
        connection.execute(text("ALTER TABLE live_accounts ADD COLUMN ctid TEXT"))
    if "broker_login" not in column_names:
        connection.execute(text("ALTER TABLE live_accounts ADD COLUMN broker_login TEXT"))
    if "is_demo" not in column_names:
        connection.execute(
            text(
                "ALTER TABLE live_accounts ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0"
            )
        )
    if "demo_verified_at" not in column_names:
        connection.execute(
            text("ALTER TABLE live_accounts ADD COLUMN demo_verified_at DATETIME")
        )
        # Existing rows carry an unverified flag. Null means "never confirmed
        # against the broker", which ACTIVATION treats as not verified, so this
        # fails closed rather than grandfathering unknown accounts as safe.
        connection.execute(text("UPDATE live_accounts SET demo_verified_at = NULL"))
    if "identity_fingerprint" not in column_names:
        connection.execute(
            text("ALTER TABLE live_accounts ADD COLUMN identity_fingerprint TEXT")
        )


def _add_background_task_columns(connection: Connection) -> None:
    """Add tracking columns to legacy background_tasks tables."""
    column_names = _table_column_names(connection, "background_tasks")
    if not column_names:
        return
    if "progress_message" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN progress_message TEXT NOT NULL DEFAULT ''")
        )
    if "name" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        )
    if "description" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        )
    if "source" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        )
    if "created_at" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        )
    if "updated_at" not in column_names:
        connection.execute(
            text("ALTER TABLE background_tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        )


def _add_trade_columns(connection: Connection) -> None:
    """Add risk and exit columns to legacy trades tables."""
    column_names = _table_column_names(connection, "trades")
    if not column_names:
        return
    if "position_size" not in column_names:
        connection.execute(text("ALTER TABLE trades ADD COLUMN position_size FLOAT"))
    if "risk_amount" not in column_names:
        connection.execute(text("ALTER TABLE trades ADD COLUMN risk_amount FLOAT"))
    if "risk_percent" not in column_names:
        connection.execute(text("ALTER TABLE trades ADD COLUMN risk_percent FLOAT"))
    if "exit_reason" not in column_names:
        connection.execute(text("ALTER TABLE trades ADD COLUMN exit_reason TEXT"))


def _add_backtests_equity_curve_column(connection: Connection) -> bool:
    """Add equity_curve to backtests (for portfolio optimizer to read from SQLite)."""
    column_names = _table_column_names(connection, "backtests")
    if column_names and "equity_curve" not in column_names:
        connection.execute(text("ALTER TABLE backtests ADD COLUMN equity_curve TEXT DEFAULT '[]'"))
        return True
    return "equity_curve" in column_names


def _artifact_equity_curve(bt_id: str) -> list[Any]:
    """Load an id-matched equity_curve payload from the legacy JSON artifact."""
    artifact_path = DATA_DIR / "backtests" / f"{bt_id}.trades.json"
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact_id = str(payload.get("meta", {}).get("id", ""))
        candidate = payload.get("equity_curve", [])
        if artifact_id == str(bt_id) and isinstance(candidate, list):
            return candidate
    except (OSError, TypeError, ValueError):
        pass
    return []


def _equity_points_curve(connection: Connection, bt_id: str) -> list[dict[str, Any]]:
    """Build a curve from normalized equity_points ordered by time then id."""
    return [
        {"time": row[0], "equity": row[1]}
        for row in connection.execute(
            text(
                "SELECT time, equity FROM equity_points "
                "WHERE backtest_id = :id ORDER BY time, id"
            ),
            {"id": bt_id},
        ).fetchall()
    ]


def _backfill_backtest_equity_curves(connection: Connection) -> None:
    """Backfill equity_curve for existing backtests with empty equity_curve."""
    # Prefer normalized equity_points; fall back to the matching legacy JSON artifact.
    backtests_to_fill = connection.execute(
        text(
            "SELECT id FROM backtests "
            "WHERE equity_curve IS NULL OR equity_curve = '[]' OR equity_curve = ''"
        )
    ).fetchall()
    for (bt_id,) in backtests_to_fill:
        equity_curve = _equity_points_curve(connection, bt_id)
        if not equity_curve and Path(str(bt_id)).name == str(bt_id):
            equity_curve = _artifact_equity_curve(str(bt_id))
        if equity_curve:
            connection.execute(
                text("UPDATE backtests SET equity_curve = :eq WHERE id = :id"),
                {"eq": json.dumps(equity_curve), "id": bt_id},
            )


def _migrate_backtests_equity_curve(connection: Connection) -> None:
    """Ensure the backtests.equity_curve column exists and is backfilled."""
    has_equity_curve = _add_backtests_equity_curve_column(connection)
    if has_equity_curve:
        _backfill_backtest_equity_curves(connection)


def _migrate_live_executions_dedupe_key(connection: Connection) -> None:
    """Add the dedupe_key column and its unique index to live_executions."""
    execution_column_names = _table_column_names(connection, "live_executions")
    if execution_column_names and "dedupe_key" not in execution_column_names:
        connection.execute(text("ALTER TABLE live_executions ADD COLUMN dedupe_key TEXT"))
    if _table_column_names(connection, "live_executions"):
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_live_executions_dedupe_key "
                "ON live_executions(dedupe_key) WHERE dedupe_key IS NOT NULL"
            )
        )


def _has_unique_index_on_column(connection: Connection, table: str, column: str) -> bool:
    """True when ``table`` already enforces UNIQUE on exactly ``column``.

    Covers both shapes SQLite can hold: a named index and the implicit
    ``sqlite_autoindex_*`` behind a table-level UNIQUE constraint, which is what
    ``SQLModel.metadata.create_all`` produces from the model declaration. A
    database created fresh therefore needs no migration, and one created before
    the constraint existed gets a named unique index instead.
    """
    quoted_table = connection.dialect.identifier_preparer.quote(table)
    for row in connection.execute(text(f"PRAGMA index_list({quoted_table})")).fetchall():
        if not bool(row[2]):  # row[2] is the "unique" flag
            continue
        index_name = str(row[1])
        quoted_index = connection.dialect.identifier_preparer.quote(index_name)
        indexed_columns = [
            str(info[2])
            for info in connection.execute(
                text(f"PRAGMA index_info({quoted_index})")
            ).fetchall()
        ]
        if indexed_columns == [column]:
            return True
    return False


def _prune_duplicate_submission_claims(connection: Connection) -> int:
    """Collapse concurrent claims for one deployment down to the newest lease.

    A plain unique index cannot be created while two rows share a deployment, and
    refusing the migration would take the whole database down for every caller
    (including the safety gates), which is a worse outcome than collapsing the
    duplicates. Which rows are redundant is unambiguous here: these are leases
    with a TTL, not records. The newest lease is the one that is live, every
    other lease for that deployment is already superseded, so keeping the newest
    and dropping the rest preserves the invariant and leaves the deployment
    submittable. Nothing is deleted unless the unique index is about to be
    created.
    """
    duplicated = connection.execute(
        text(
            "SELECT deployment_id FROM live_deployment_submission_claims "
            "GROUP BY deployment_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    removed = 0
    for (deployment_id,) in duplicated:
        keeper = connection.execute(
            text(
                "SELECT token FROM live_deployment_submission_claims "
                "WHERE deployment_id = :deployment_id "
                "ORDER BY expires_at DESC, claimed_at DESC, token DESC LIMIT 1"
            ),
            {"deployment_id": str(deployment_id)},
        ).first()
        if keeper is None:  # pragma: no cover - the group is non-empty by query
            continue
        result = connection.execute(
            text(
                "DELETE FROM live_deployment_submission_claims "
                "WHERE deployment_id = :deployment_id AND token != :token"
            ),
            {"deployment_id": str(deployment_id), "token": str(keeper[0])},
        )
        dropped = int(result.rowcount or 0)
        removed += dropped
        logger.warning(
            "Collapsed concurrent live deployment submission claims to satisfy the "
            "one-lease-per-deployment invariant; the extra leases were superseded",
            extra={"data": {"deployment_id": str(deployment_id), "dropped": dropped}},
        )
    return removed


def _migrate_live_submission_claim_uniqueness(connection: Connection) -> None:
    """Make the live submission claim mutually exclusive per deployment.

    ``deployment_id`` used to carry only a non-unique index, so two dispatchers
    could both claim the same ACTIVE deployment and both submit. The claim is
    the only thing standing between a stale "still ACTIVE" snapshot and a real
    broker order, so the uniqueness is enforced by the database itself.
    """
    if not _table_column_names(connection, "live_deployment_submission_claims"):
        return
    if _has_unique_index_on_column(
        connection, "live_deployment_submission_claims", "deployment_id"
    ):
        return
    _prune_duplicate_submission_claims(connection)
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_live_deployment_submission_claims_deployment "
            "ON live_deployment_submission_claims(deployment_id)"
        )
    )


def _migrate_schema() -> None:
    with _schema_migration_connection() as connection:
        _add_service_columns(connection)
        _add_live_deployment_enforced_column(connection)
        _add_live_deployment_columns(
            connection, _table_column_names(connection, "live_deployments")
        )
        _add_backtests_tags_column(connection)
        _add_live_account_columns(connection)
        _add_background_task_columns(connection)
        _add_trade_columns(connection)

        _migrate_live_signal_discord_identity(connection)
        _migrate_live_signal_idempotency_key(connection)

        _migrate_backtests_equity_curve(connection)
        _migrate_live_executions_dedupe_key(connection)
        _migrate_live_submission_claim_uniqueness(connection)


def _find_existing_live_signal(
    session: Session, deployment_id: str, discord_msg_id: str
) -> LiveSignalModel | None:
    """Find a persisted signal for one (deployment, discord_message_id) pair."""
    return session.exec(
        select(LiveSignalModel)
        .where(LiveSignalModel.deployment_id == deployment_id)
        .where(LiveSignalModel.discord_message_id == discord_msg_id)
        .limit(1)
    ).first()


def _find_existing_live_signal_by_idempotency_key(
    session: Session, deployment_id: str, idempotency_key: str
) -> LiveSignalModel | None:
    """Find a persisted signal for one (deployment, idempotency_key) pair."""
    return session.exec(
        select(LiveSignalModel)
        .where(LiveSignalModel.deployment_id == deployment_id)
        .where(LiveSignalModel.idempotency_key == idempotency_key)
        .limit(1)
    ).first()


def _reconcile_existing_live_signal(
    session: Session, existing: LiveSignalModel, record: LiveSignalModel
) -> None:
    """Backfill broker_order_id onto an existing duplicate signal."""
    # Reconcile broker_order_id: if existing lacks it but new record has it, update
    new_broker_id = (record.broker_order_id or "").strip()
    existing_broker_id = (existing.broker_order_id or "").strip()
    if new_broker_id and not existing_broker_id:
        existing.broker_order_id = new_broker_id
        # Also update metadata_json for consistency
        metadata = dict(existing.metadata_json or {})
        metadata["broker_order_id"] = new_broker_id
        existing.metadata_json = metadata
        session.add(existing)
        session.commit()
        logger.info(
            "Updated existing live signal with broker_order_id (discord_message_id=%s)",
            record.discord_message_id,
        )
    elif new_broker_id and existing_broker_id and new_broker_id != existing_broker_id:
        logger.warning(
            "Conflicting broker_order_id for duplicate live signal (signal_id=%s)",
            existing.id,
        )
        return
    else:
        logger.info(
            "Skipping duplicate live signal (discord_message_id=%s)", record.discord_message_id
        )


def save_live_signal(signal: LiveSignal) -> tuple[str, bool]:
    """Persist a signal and return ``(canonical_signal_id, created)``.

    Discord redeliveries can carry a new in-memory UUID. Returning the canonical
    persisted id lets callers fail closed before submitting another broker order.
    """
    init_db()
    with get_session() as session:
        record = LiveSignalModel.from_live_signal(signal)
        # Deduplicate redeliveries within one deployment while allowing an
        # intentional Discord fan-out to independent deployments.
        discord_msg_id = record.discord_message_id
        deployment_id = record.deployment_id
        if discord_msg_id:
            existing = _find_existing_live_signal(session, deployment_id, discord_msg_id)
            if existing is not None:
                _reconcile_existing_live_signal(session, existing, record)
                return str(existing.id), False
        # C1: non-Discord signals (internal/replay) dedup on the producer-
        # stable idempotency key instead of minting a second broker order.
        if record.idempotency_key:
            existing = _find_existing_live_signal_by_idempotency_key(
                session, deployment_id, record.idempotency_key
            )
            if existing is not None:
                _reconcile_existing_live_signal(session, existing, record)
                return str(existing.id), False
        record_id = str(record.id)
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            # Concurrent duplicate: re-query by whichever identity lost the
            # race (discord id or idempotency key) instead of surfacing the
            # constraint as a second broker order.
            existing = None
            if discord_msg_id:
                existing = _find_existing_live_signal(session, deployment_id, discord_msg_id)
            if existing is None and record.idempotency_key:
                existing = _find_existing_live_signal_by_idempotency_key(
                    session, deployment_id, record.idempotency_key
                )
            if existing is None:
                raise
            logger.info(
                "Skipping concurrent duplicate live signal (discord_message_id=%s)",
                discord_msg_id,
            )
            return str(existing.id), False
    return record_id, True


def _auto_execution_dedupe_key(execution: LiveExecution) -> str:
    """Deterministic dedupe key from fill content (C1).

    A redelivered fill carries identical content, so the key is stable
    across redeliveries (unlike a random UUID, which would never dedupe).
    """
    timestamp = execution.timestamp
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo:
            stamp = timestamp.astimezone(timezone.utc).isoformat()
        else:
            stamp = timestamp.replace(tzinfo=timezone.utc).isoformat()
    else:
        stamp = str(timestamp)
    return (
        f"auto:{execution.signal_id}:{float(execution.actual_price or 0.0):.8f}:"
        f"{float(execution.filled_qty or 0.0):.8f}:{stamp}:{execution.broker_order_id}"
    )


def _insert_live_execution_locked(session: Session, record: LiveExecutionModel) -> bool:
    """Insert-or-skip one execution under a write lock (C1: single path).

    Both plain and cumulative callers funnel through here so the
    select-check-insert sequence cannot interleave.
    """
    existing = session.exec(
        select(LiveExecutionModel)
        .where(LiveExecutionModel.dedupe_key == record.dedupe_key)
        .limit(1)
    ).first()
    if existing is not None:
        logger.info(
            "Skipping duplicate live execution (dedupe_key=%s)", record.dedupe_key
        )
        return False
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # CodeRabbit round: the conflict is only a duplicate when the
        # dedupe_key row actually exists — anything else (FK, NOT NULL,
        # CHECK) must propagate instead of masquerading as a skip.
        raced = session.exec(
            select(LiveExecutionModel)
            .where(LiveExecutionModel.dedupe_key == record.dedupe_key)
            .limit(1)
        ).first()
        if raced is None:
            raise
        logger.info(
            "Skipping duplicate live execution on unique constraint (dedupe_key=%s)",
            record.dedupe_key,
        )
        return False
    return True


def save_live_execution(execution: LiveExecution) -> bool:
    """Persist a fill. Returns False when a duplicate (same dedupe_key) was skipped."""
    init_db()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            record = LiveExecutionModel.from_live_execution(execution)
            if not record.dedupe_key:
                # C1: a missing key must not bypass the partial unique index
                # via NULL — derive it deterministically from the fill.
                record.dedupe_key = _auto_execution_dedupe_key(execution)
            return _insert_live_execution_locked(session, record)
        except Exception:
            session.rollback()
            raise


def save_live_execution_up_to_cumulative(
    execution: LiveExecution, broker_cumulative_filled_qty: float
) -> float:
    """Atomically persist only the quantity still missing up to a broker cumulative fill."""
    normalized_signal_id = str(execution.signal_id or "").strip()
    cumulative_qty = float(broker_cumulative_filled_qty or 0.0)
    requested_qty = float(execution.filled_qty or 0.0)
    if not normalized_signal_id or cumulative_qty <= 0.0 or requested_qty <= 0.0:
        return 0.0

    init_db()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            record = LiveExecutionModel.from_live_execution(execution)
            if not record.dedupe_key:
                record.dedupe_key = _auto_execution_dedupe_key(execution)
            existing = session.exec(
                select(LiveExecutionModel)
                .where(LiveExecutionModel.dedupe_key == record.dedupe_key)
                .limit(1)
            ).first()
            if existing is not None:
                session.rollback()
                return 0.0

            local_filled_qty = float(
                session.exec(
                    select(func.coalesce(func.sum(LiveExecutionModel.filled_qty), 0.0)).where(
                        LiveExecutionModel.signal_id == normalized_signal_id
                    )
                ).one()
                or 0.0
            )
            persisted_qty = min(requested_qty, cumulative_qty - local_filled_qty)
            if persisted_qty <= 0.0:
                session.rollback()
                return 0.0

            record.filled_qty = persisted_qty
            if requested_qty != persisted_qty and record.commission:
                record.commission *= persisted_qty / requested_qty
            if not _insert_live_execution_locked(session, record):
                session.rollback()
                return 0.0
            return persisted_qty
        except Exception:
            session.rollback()
            raise


def get_live_signal_filled_qty(signal_id: str) -> float:
    """Return the cumulative quantity persisted across execution events for a signal."""
    normalized_signal_id = str(signal_id or "").strip()
    if not normalized_signal_id:
        return 0.0
    init_db()
    with get_session() as session:
        filled_qty = session.exec(
            select(func.coalesce(func.sum(LiveExecutionModel.filled_qty), 0.0)).where(
                LiveExecutionModel.signal_id == normalized_signal_id
            )
        ).one()
    return float(filled_qty or 0.0)


def set_live_signal_broker_order_id(signal_id: str, broker_order_id: str) -> bool:
    """Persist the broker order id on a signal right after submission (crash recovery)."""
    normalized_signal_id = str(signal_id or "").strip()
    normalized_order_id = str(broker_order_id or "").strip()
    if not normalized_signal_id or not normalized_order_id:
        return False
    init_db()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        record = session.get(LiveSignalModel, normalized_signal_id)
        if record is None:
            return False
        existing_order_id = str(record.broker_order_id or "").strip()
        if existing_order_id and existing_order_id != normalized_order_id:
            return False
        if existing_order_id == normalized_order_id:
            return True
        record.broker_order_id = normalized_order_id
        metadata = dict(record.metadata_json or {})
        metadata["broker_order_id"] = normalized_order_id
        record.metadata_json = metadata
        session.add(record)
        session.commit()
    return True


def get_live_signal_broker_order_id(signal_id: str) -> str | None:
    """Return the persisted broker order id for a signal, if any (crash recovery)."""
    normalized_signal_id = str(signal_id or "").strip()
    if not normalized_signal_id:
        return None
    init_db()
    with get_session() as session:
        record = session.get(LiveSignalModel, normalized_signal_id)
        if record is None:
            return None
        broker_order_id = str(record.broker_order_id or "").strip()
    return broker_order_id or None


def get_live_signal_metadata(signal_id: str) -> dict[str, Any] | None:
    normalized_signal_id = str(signal_id or "").strip()
    if not normalized_signal_id:
        return None
    init_db()
    with get_session() as session:
        record = session.get(LiveSignalModel, normalized_signal_id)
        if record is None:
            return None
        metadata = dict(record.metadata_json or {})
    return metadata


def get_live_signal_timestamp(signal_id: str) -> datetime | None:
    """Return the persisted signal timestamp used for bounded broker recovery."""
    normalized_signal_id = str(signal_id or "").strip()
    if not normalized_signal_id:
        return None
    init_db()
    with get_session() as session:
        record = session.get(LiveSignalModel, normalized_signal_id)
        if record is None:
            return None
        timestamp = record.timestamp
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def find_live_signal_id_by_broker_order_id(
    account_id: str, broker_order_id: str
) -> str | None:
    normalized_account_id = str(account_id or "").strip()
    normalized_order_id = str(broker_order_id or "").strip()
    if not normalized_account_id or not normalized_order_id:
        return None
    init_db()
    with get_session() as session:
        row = session.exec(
            select(LiveSignalModel)
            .join(
                LiveDeploymentModel,
                LiveSignalModel.deployment_id == LiveDeploymentModel.id,
            )
            .where(LiveDeploymentModel.account_id == normalized_account_id)
            .where(LiveSignalModel.broker_order_id == normalized_order_id)
            .order_by(LiveSignalModel.timestamp.desc())
            .limit(1)
        ).first()
    return str(row.id) if row is not None else None


def get_live_signal_reconciliation_cursor(signal_id: str) -> tuple[datetime, str] | None:
    normalized_signal_id = str(signal_id or "").strip()
    if not normalized_signal_id:
        return None
    init_db()
    with get_session() as session:
        row = session.get(LiveSignalModel, normalized_signal_id)
    if row is None:
        return None
    return row.timestamp, str(row.id)


def list_live_signal_ids_for_reconciliation(
    account_id: str,
    limit: int = 200,
    *,
    before: tuple[datetime, str] | None = None,
) -> list[str]:
    """Persisted account signals in stable pages for fill reconciliation.

    Includes partially executed signals so broker deals missed during a disconnect
    can be backfilled. ``save_live_execution`` provides deal-level idempotency.
    """
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        return []
    try:
        bounded_limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        bounded_limit = 200
    init_db()
    with get_session() as session:
        statement = (
            select(LiveSignalModel)
            .join(
                LiveDeploymentModel,
                LiveSignalModel.deployment_id == LiveDeploymentModel.id,
            )
            .where(LiveDeploymentModel.account_id == normalized_account_id)
        )
        if before is not None:
            cursor_timestamp, cursor_id = before
            statement = statement.where(
                or_(
                    LiveSignalModel.timestamp < cursor_timestamp,
                    (
                        (LiveSignalModel.timestamp == cursor_timestamp)
                        & (LiveSignalModel.id < str(cursor_id))
                    ),
                )
            )
        rows = session.exec(
            statement.order_by(
                LiveSignalModel.timestamp.desc(), LiveSignalModel.id.desc()
            ).limit(bounded_limit)
        ).all()
    return [str(row.id) for row in rows]


def list_live_signal_ids_missing_executions(limit: int = 200) -> list[str]:
    """Signals persisted before broker submit that never received a fill record.

    Used by adapter reconciliation after a crash/restart to backfill lost fills.
    """
    try:
        bounded_limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        bounded_limit = 200
    init_db()
    with get_session() as session:
        executed_ids = select(LiveExecutionModel.signal_id)
        rows = session.exec(
            select(LiveSignalModel)
            .where(LiveSignalModel.id.notin_(executed_ids))
            .order_by(LiveSignalModel.timestamp.desc(), LiveSignalModel.id.desc())
            .limit(bounded_limit)
        ).all()
    return [str(row.id) for row in rows]


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


def _optional_trimmed(value: str | None) -> str | None:
    """Return a stripped non-empty string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _invalidate_verification_for_changed_identity(record: LiveAccountModel) -> bool:
    """Drop the demo verification when the identity it covered has changed.

    The fingerprint recorded at verification time is the only thing that ties
    "verified demo" to one specific broker account. A writer that changes
    broker/ctid/broker_login has just moved that account somewhere the broker
    never confirmed, so the stamp is deleted rather than kept: the account then
    fails the gates until discovery re-verifies it, which is the only path that
    may write a new stamp. Idempotent — an already-unverified row is a no-op.
    """
    if not str(record.identity_fingerprint or "").strip():
        return False
    try:
        current = live_account_identity_fingerprint(
            str(record.broker or ""),
            ctid=record.ctid,
            broker_login=record.broker_login,
        )
    except Exception as exc:  # pragma: no cover - hash of a string cannot fail
        logger.error(
            "Could not recompute the live account identity while updating it; "
            "dropping the demo verification",
            extra={"data": {"account_id": record.id, "error": repr(exc)}},
        )
    else:
        if hmac.compare_digest(str(record.identity_fingerprint), current):
            return False
    logger.warning(
        "Live account broker identity changed; dropping its demo verification",
        extra={
            "data": {
                "account_id": record.id,
                "broker": record.broker,
                "ctid": record.ctid,
                "broker_login": record.broker_login,
            }
        },
    )
    record.demo_verified_at = None
    record.identity_fingerprint = None
    return True


def _create_live_account_record(
    session: Session,
    account_id: str,
    broker: str,
    *,
    name: str,
    ctid: str | None,
    broker_login: str | None,
    is_demo: bool,
) -> LiveAccountConfig:
    """Insert a brand-new live account row."""
    account = LiveAccountModel(
        id=account_id,
        broker=broker,
        name=name or account_id,
        ctid=_optional_trimmed(ctid),
        broker_login=_optional_trimmed(broker_login),
        is_demo=bool(is_demo),
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account.to_live_account()


def _apply_live_account_updates(
    session: Session,
    current: LiveAccountModel,
    *,
    broker: str,
    name: str,
    ctid: str | None,
    broker_login: str | None,
    is_demo: bool | None,
) -> bool:
    """Overwrite non-empty fields on an existing live account row.

    Re-pointing the broker identity (broker/ctid/broker_login) is a local
    configuration change, not a broker conversation, so it also drops any demo
    verification the previous identity carried. Leaving the stamp in place is
    how a re-pointed credential used to keep trading as a verified demo.
    """
    changed = False
    identity_changed = False
    if broker and current.broker != broker:
        current.broker = broker
        identity_changed = True
    if name and current.name != name:
        current.name = name
        changed = True
    normalized_ctid = _optional_trimmed(ctid)
    if ctid is not None and current.ctid != normalized_ctid:
        current.ctid = normalized_ctid
        identity_changed = True
    normalized_broker_login = _optional_trimmed(broker_login)
    if broker_login is not None and current.broker_login != normalized_broker_login:
        current.broker_login = normalized_broker_login
        identity_changed = True
    if is_demo is not None and bool(current.is_demo) != bool(is_demo):
        current.is_demo = bool(is_demo)
        changed = True
        if not is_demo:
            # A demotion cannot keep the verification it was based on.
            current.demo_verified_at = None
            current.identity_fingerprint = None
    if identity_changed and _invalidate_verification_for_changed_identity(current):
        changed = True
    if changed or identity_changed:
        session.add(current)
        session.commit()
        session.refresh(current)
    return changed


def ensure_live_account(
    account_id: str,
    broker: str,
    *,
    name: str = "",
    ctid: str | None = None,
    broker_login: str | None = None,
    is_demo: bool | None = None,
) -> LiveAccountConfig:
    init_db()
    with get_session() as session:
        current = session.get(LiveAccountModel, account_id)
        if current is None:
            return _create_live_account_record(
                session,
                account_id,
                broker,
                name=name,
                ctid=ctid,
                broker_login=broker_login,
                is_demo=bool(is_demo),
            )

        _apply_live_account_updates(
            session,
            current,
            broker=broker,
            name=name,
            ctid=ctid,
            broker_login=broker_login,
            is_demo=None if is_demo is None else bool(is_demo),
        )
        return current.to_live_account()


def upsert_live_accounts(accounts: list[LiveAccountConfig]) -> None:
    """Refresh stored account rows from a discovery payload.

    Only values the payload positively asserts are written. ``is_demo`` is
    tri-state on ``LiveAccountConfig`` and the broker identity fields are only
    replaced when the payload carries one, so a payload that omits a value
    (which a read-only page view can produce) cannot overwrite a
    broker-verified ``True`` with ``False``, nor blank the identity a
    verification was taken from.

    The old unconditional write is what silently disarmed accounts: the stored
    flag flipped to non-demo while ``demo_verified_at`` stayed fresh, the
    adapter was torn down, and no later read could ever put it back, because a
    non-demo account never gets an adapter again.
    """
    init_db()
    with get_session() as session:
        for account in accounts:
            current = session.get(LiveAccountModel, account.id)
            if current is None:
                session.add(LiveAccountModel.from_live_account(account))
                continue
            current.broker = account.broker
            current.name = account.name
            if _optional_trimmed(account.ctid) is not None:
                current.ctid = _optional_trimmed(account.ctid)
            if _optional_trimmed(account.broker_login) is not None:
                current.broker_login = _optional_trimmed(account.broker_login)
            if account.is_demo is not None and bool(current.is_demo) != bool(account.is_demo):
                current.is_demo = bool(account.is_demo)
                if not current.is_demo:
                    # An explicit demotion invalidates the verification behind it.
                    current.demo_verified_at = None
                    current.identity_fingerprint = None
            # Whatever wrote the identity above, it can only be trusted once the
            # broker has confirmed the new one.
            _invalidate_verification_for_changed_identity(current)
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


def record_live_control_audit(
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor: str = "unknown",
    succeeded: bool = True,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one state-changing live control to the audit trail.

    Written on the failure path as well as the success path, and deliberately
    never raises: an audit write that could abort the operation it is recording
    would be a worse failure than the missing record, so a failure here is logged
    and the caller carries on.
    """
    payload = {
        "id": str(uuid4()),
        "actor": str(actor or "unknown").strip() or "unknown",
        "action": str(action or "").strip(),
        "target_type": str(target_type or "").strip(),
        "target_id": str(target_id or "").strip(),
        "succeeded": bool(succeeded),
        "detail": dict(detail or {}),
    }
    try:
        init_db()
        with get_session() as session:
            session.add(
                LiveControlAuditModel(
                    id=payload["id"],
                    actor=payload["actor"],
                    action=payload["action"],
                    target_type=payload["target_type"],
                    target_id=payload["target_id"],
                    succeeded=payload["succeeded"],
                    detail_json=payload["detail"],
                )
            )
            session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Could not write the live control audit record; the change itself stands",
            extra={"data": {"action": payload["action"], "error": repr(exc)}},
        )
    return payload


def list_live_control_audit(
    *,
    limit: int = 100,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    """Most recent first. Read-only; there is no delete path on purpose."""
    bounded = max(1, min(int(limit or 100), 1000))
    init_db()
    with get_session() as session:
        statement = select(LiveControlAuditModel)
        if target_id:
            statement = statement.where(
                LiveControlAuditModel.target_id == str(target_id).strip()
            )
        rows = session.exec(
            statement.order_by(LiveControlAuditModel.created_at.desc()).limit(bounded)
        ).all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "actor": row.actor,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "succeeded": row.succeeded,
            "detail": row.detail_json or {},
        }
        for row in rows
    ]


#: The daemon registers itself under this name, so its liveness is a service row
#: rather than a heartbeat file nobody reads. The id is derived rather than random
#: because it has to be the *same* row on every beat and across restarts, and
#: Service.id is a UUID.
TRADING_DAEMON_SERVICE_NAME = "trading-daemon"
TRADING_DAEMON_SERVICE_ID = str(
    _uuid5(NAMESPACE_URL, "urn:perma:service:trading-daemon")
)


def record_daemon_heartbeat(
    *,
    last_activity: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> Service:
    """Upsert the daemon's own service row so its liveness is observable.

    A paused platform is silent by design, so "no recent activity" is ambiguous:
    it means either "running, nothing to do" or "dead". The daemon reporting
    itself into the same services table the UI already reads removes the
    ambiguity, and keeps the daemon visible in the one place an operator looks.

    Upsert rather than update because ``update_service_status`` needs an existing
    row, and this must work on the daemon's very first loop. The status is
    deliberately left alone: the daemon does not get to declare itself healthy,
    it only reports that it is still turning. ``detail`` is merged into the config
    so the loop can publish counts (deployments seen, adapters connected) without
    inventing a second place to look.
    """
    now = last_activity or _utcnow_naive()
    config = dict(detail or {})
    init_db()
    with get_session() as session:
        record = session.get(ServiceModel, TRADING_DAEMON_SERVICE_ID)
        if record is None:
            record = ServiceModel(
                id=TRADING_DAEMON_SERVICE_ID,
                name=TRADING_DAEMON_SERVICE_NAME,
                type=ServiceType.TRADING_DAEMON.value,
                status=ServiceStatus.RUNNING.value,
            )
            session.add(record)
        record.last_activity = now
        record.updated_at = _utcnow_naive()
        merged = dict(record.config_json or {})
        merged.update(config)
        record.config_json = merged
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_service()


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
    respect_operator_intent: bool = True,
) -> Service | None:
    """Write a service's status.

    `respect_operator_intent` makes PAUSED a floor: a PAUSED service is never
    moved to any other status by this function. The pause was set by a person
    through the API, and nothing the process observes afterwards - a message
    arriving, a connector failing, an author allowlist turning out to be wrong,
    a reconnect completing - outranks that.

    This lives here rather than in a caller because there were six unguarded
    call sites in the daemon and a guarded one in the signal source, and a rule
    enforced by eight call sites is not a rule. It was applied to one of them and
    the other seven kept the hole: a FAILED landing on a PAUSED row, and
    `_recover_failed_services` then reading FAILED rather than PAUSED and
    resetting the service to RUNNING on the next daemon start. The pause was
    undone by the process, silently.

    Pass `respect_operator_intent=False` only where the caller is *establishing*
    operator intent, i.e. an API call that is itself setting or clearing a pause.
    """
    normalized_id = str(service_id or "").strip()
    if not normalized_id:
        return None
    normalized_status = _normalize_service_status(status)
    init_db()
    with get_session() as session:
        record = session.get(ServiceModel, normalized_id)
        if record is None:
            return None
        # Any status write is an attempt, successful or not. Without this the only
        # record that anything tried is `updated_at`, which is a row-modification
        # timestamp rather than a statement about the service.
        record.last_attempt_at = _utcnow_naive()
        if (
            respect_operator_intent
            and record.status == ServiceStatus.PAUSED.value
            and normalized_status != ServiceStatus.PAUSED.value
        ):
            # Refusing the status change is still an attempt, and the attempt is the
            # thing worth knowing: it is how an operator finds out something kept
            # trying to un-pause a service they paused. This needs its own commit,
            # because returning from inside `get_session` closes the session without
            # one and the pending write is rolled back - so the column added to
            # answer "when did anyone last try?" was empty for exactly the attempts
            # worth noticing. The refusal itself stays fail-closed: the status is
            # untouched either way.
            session.add(record)
            session.commit()
            return record.to_service()
        record.status = normalized_status
        if last_activity is not None:
            record.last_activity = last_activity
        record.updated_at = _utcnow_naive()
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


def _resolve_deployment_signal_source(deployment: LiveDeploymentConfig) -> tuple[str, str]:
    """Validate and normalize the deployment signal source type and source."""
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
    return signal_source_type, signal_source


def _normalized_symbol_filter(symbols: list[str]) -> list[str]:
    """Uppercase stripped symbol entries, dropping blanks."""
    return [
        str(symbol).strip().upper()
        for symbol in (symbols or [])
        if str(symbol).strip()
    ]


def create_live_deployment(deployment: LiveDeploymentConfig) -> LiveDeploymentConfig:
    from live_runner.stockkit_routing import validate_stockkit_deployment_route

    init_db()
    with get_session() as session:
        account = session.get(LiveAccountModel, deployment.account_id)
        if account is None:
            raise ValueError(f"Unknown live account '{deployment.account_id}'")
        # Validate routing against the account's broker-reported identity, not
        # the local account id, which is a different identifier space. The
        # accounts-env is supplied so route resolution follows the configured
        # account labels rather than a hardcoded table.
        try:
            from live_runner.credential_env import load_accounts_env_values
            from live_runner.service_discovery import DEFAULT_ACCOUNTS_ENV

            route_env = load_accounts_env_values(DEFAULT_ACCOUNTS_ENV)
        except Exception:
            route_env = {}
        validate_stockkit_deployment_route(
            deployment,
            account_broker_login=account.broker_login or account.ctid or "",
            accounts_env_values=route_env,
        )
        deployment_status = str(deployment.status or "ACTIVE").upper()
        if deployment_status not in {"ACTIVE", "PAUSED", "STOPPED"}:
            raise ValueError(f"Invalid deployment status '{deployment.status}'")
        if deployment_status == "ACTIVE":
            block_reason = _activation_block_reason(account)
            if block_reason is not None:
                raise ValueError(
                    f"Refusing to create an ACTIVE deployment: {block_reason}"
                )
            # Same verdict as the arming path, applied before the row exists so
            # a deployment cannot be born ACTIVE and incapable of trading.
            probe = LiveDeploymentModel.from_live_deployment(deployment)
            source_block = _signal_source_block_reason(probe)
            if source_block is not None:
                raise ValueError(
                    f"Refusing to create an ACTIVE deployment: {source_block}"
                )
        signal_source_type, signal_source = _resolve_deployment_signal_source(deployment)
        signal_source_id = str(deployment.signal_source_id or "").strip()
        symbol_filter = _normalized_symbol_filter(deployment.symbol_filter)
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


# One lease, one budget, one place. The drain that an operator's PAUSE waits on
# and the lease that a dispatcher holds are two halves of the same wait, so they
# are derived from this single pair of constants and must never be written
# independently.
#
# The relationship is the whole point: the drain budget is strictly greater than
# the claim TTL. A dispatcher releases its lease in a `finally`, so a lease is
# held for at most one TTL - but a lease may already be partly spent when the
# drain starts, so its remaining life can still be a full TTL. If the budget
# were shorter than the TTL then a submission that is merely slow would be
# reported as "still in flight" while it was in fact perfectly healthy, and the
# endpoint would have to guess. Two TTLs covers a full remaining lease plus one
# more lease window of slack, which is also what lets the drain outlast a lease
# that overran and make the expired-but-never-released case observable instead
# of silently reading as "drained".
LIVE_SUBMISSION_CLAIM_TTL_SECONDS = 30.0
LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS = LIVE_SUBMISSION_CLAIM_TTL_SECONDS * 2.0


def mark_live_deployment_stopped_enforced(deployment_id: str) -> bool:
    """Record that STOPPED enforcement completed for this deployment.

    The daemon keeps the same set in memory, which is what made a restart re-run
    cancel and flatten for deployments already stopped. Writing it down means the
    ledger survives a process, and a restart resumes rather than repeating.
    """
    normalized = str(deployment_id or "").strip()
    if not normalized:
        return False
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, normalized)
        if record is None:
            return False
        record.stopped_enforced_at = _utcnow_naive()
        record.updated_at = record.stopped_enforced_at
        session.add(record)
        session.commit()
    return True


def clear_live_deployment_stopped_enforced(deployment_id: str) -> bool:
    """Forget the enforcement stamp so a later STOPPED transition re-enforces.

    Called when a deployment leaves STOPPED. Without it, stopping the same
    deployment again would be skipped as "already done" from the previous stop,
    which is the opposite error to the one this column was added to fix.
    """
    normalized = str(deployment_id or "").strip()
    if not normalized:
        return False
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, normalized)
        if record is None:
            return False
        record.stopped_enforced_at = None
        record.updated_at = _utcnow_naive()
        session.add(record)
        session.commit()
    return True


def list_stopped_enforced_deployment_ids() -> set[str]:
    """Deployment ids whose STOPPED enforcement already completed.

    Read once at daemon startup to rebuild the in-memory ledger, so a restart
    does not repeat work that is already done.
    """
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(LiveDeploymentModel.id).where(
                LiveDeploymentModel.stopped_enforced_at.is_not(None)
            )
        ).all()
    return {str(row) for row in rows}


def claim_live_deployment_submission(
    deployment_id: str,
    token: str,
    *,
    ttl_seconds: float = LIVE_SUBMISSION_CLAIM_TTL_SECONDS,
) -> bool:
    """Take the single submission lease for one deployment, or refuse.

    Two independent facts have to hold, and both are decided by the database
    rather than by a status snapshot taken earlier in the dispatch:

    * the deployment is still ``ACTIVE`` (the ``INSERT ... SELECT`` reads the
      live row, so a PAUSE that committed first wins the race);
    * no other process holds a lease for it (``UNIQUE(deployment_id)``, see
      :class:`LiveDeploymentSubmissionClaimModel`).

    A lease that has outlived its TTL is deleted first, in its own committed
    transaction, so a claimor that died mid-submission cannot wedge the
    deployment forever. That cleanup is committed separately on purpose: it must
    survive a refused claim, otherwise a permanently-busy deployment would also
    stop reaping its own dead leases. This function is therefore the only place
    that reaps; observers must use
    :func:`list_live_deployment_submission_claims`, which never deletes.
    """
    normalized_id = str(deployment_id or "").strip()
    normalized_token = str(token or "").strip()
    if not normalized_id or not normalized_token:
        return False
    init_db()
    now = _utcnow_naive()
    expires_at = now + timedelta(seconds=max(float(ttl_seconds), 0.01))
    with get_session() as session:
        session.exec(
            delete(LiveDeploymentSubmissionClaimModel).where(
                (LiveDeploymentSubmissionClaimModel.deployment_id == normalized_id)
                & (LiveDeploymentSubmissionClaimModel.expires_at <= now)
            )
        )
        session.commit()
    with get_session() as session:
        try:
            result = session.execute(
                text(
                    "INSERT INTO live_deployment_submission_claims "
                    "(token, deployment_id, claimed_at, expires_at) "
                    "SELECT :token, id, :claimed_at, :expires_at FROM live_deployments "
                    "WHERE id = :deployment_id AND UPPER(status) = 'ACTIVE'"
                ),
                {
                    "token": normalized_token,
                    "claimed_at": now,
                    "expires_at": expires_at,
                    "deployment_id": normalized_id,
                },
            )
            session.commit()
            return int(result.rowcount or 0) == 1
        except IntegrityError:
            # The UNIQUE(deployment_id) lease is held: another dispatcher is
            # already submitting for this deployment. Refusing is the whole
            # point, so this is not an error - but it is a race the operator
            # should be able to see, so say which deployment lost it.
            session.rollback()
            logger.warning(
                "Live deployment submission already claimed by another "
                "dispatcher; refusing the second claim",
                extra={"data": {"deployment_id": normalized_id, "token": normalized_token}},
            )
            return False
        except Exception:
            session.rollback()
            raise


def release_live_deployment_submission(token: str) -> bool:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return False
    init_db()
    with get_session() as session:
        result = session.exec(
            delete(LiveDeploymentSubmissionClaimModel).where(
                LiveDeploymentSubmissionClaimModel.token == normalized_token
            )
        )
        session.commit()
        return int(result.rowcount or 0) > 0


@dataclass(frozen=True)
class LiveDeploymentSubmissionClaim:
    """One observed submission lease, and whether it is still within its TTL."""

    token: str
    expires_at: datetime
    expired: bool


def list_live_deployment_submission_claims(
    deployment_id: str,
) -> list[LiveDeploymentSubmissionClaim]:
    """Read-only inventory of the submission leases held for ``deployment_id``.

    This never deletes anything. An earlier version of this function purged
    expired leases before counting them, which made an expired-but-never-released
    claim indistinguishable from "nothing was in flight" and let a stop report
    success for an order whose outcome nobody had observed. Reaping belongs to
    :func:`claim_live_deployment_submission` alone, which is what keeps a dead
    claimor from wedging the deployment; an observer that also deletes cannot be
    used to decide whether an operation's outcome is known.

    ``expired`` is computed against the database's own clock so a caller never
    has to compare wall time itself.
    """
    normalized_id = str(deployment_id or "").strip()
    if not normalized_id:
        return []
    init_db()
    now = _utcnow_naive()
    with get_session() as session:
        rows = session.exec(
            select(
                LiveDeploymentSubmissionClaimModel.token,
                LiveDeploymentSubmissionClaimModel.expires_at,
            ).where(LiveDeploymentSubmissionClaimModel.deployment_id == normalized_id)
        ).all()
    return [
        LiveDeploymentSubmissionClaim(
            token=str(row[0]),
            expires_at=row[1],
            expired=row[1] <= now,
        )
        for row in rows
    ]


def live_account_identity_fingerprint(
    broker: str,
    *,
    ctid: str | None,
    broker_login: str | None,
) -> str:
    """Stable hash of the broker-reported identity of an account.

    Persisted at verification time and re-checked by
    :func:`live_account_verification_block_reason` on both the arm path and the
    per-dispatch path, so a credential that is silently re-pointed at a
    different (or live) account cannot inherit an old, trusted verification.
    """
    payload = "|".join(
        (
            str(broker or "").strip().lower(),
            str(ctid or "").strip(),
            str(broker_login or "").strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _live_account_fingerprint_mismatch_reason(account: LiveAccountModel) -> str | None:
    """Return why the account's current identity is not the verified one.

    Fails closed on every uncertainty: no stored fingerprint, a fingerprint that
    cannot be recomputed, or a recomputed value that differs from the stored
    one. The recomputation reads the live row, so any writer that changes the
    broker identity without re-verifying is caught here.
    """
    stored = str(account.identity_fingerprint or "").strip()
    if not stored:
        return "account has no broker identity fingerprint on record; re-verify with the broker"
    try:
        current = live_account_identity_fingerprint(
            str(account.broker or ""),
            ctid=account.ctid,
            broker_login=account.broker_login,
        )
    except Exception as exc:  # pragma: no cover - hash of a string cannot fail
        logger.error(
            "Could not recompute the live account identity fingerprint; refusing",
            extra={"data": {"account_id": account.id, "error": repr(exc)}},
        )
        return "account identity fingerprint could not be recomputed; re-verify with the broker"
    if not current:
        return "account identity fingerprint is empty; re-verify with the broker"
    if not hmac.compare_digest(stored, current):
        return (
            "account broker identity changed since it was verified as demo; "
            "re-verify with the broker"
        )
    return None


# How long a broker demo verification stays trustworthy. Short enough that a
# re-pointed credential cannot keep trading on a stale confirmation, long
# enough that an operator does not have to re-verify every click. Enforced on
# every gate below: activation AND every individual submission, so a deployment
# that was armed long ago cannot keep trading on an expired confirmation.
DEMO_VERIFICATION_TTL_SECONDS = 900


def record_live_account_demo_verification(
    account_id: str,
    *,
    is_demo: bool,
    ctid: str | None = None,
    broker_login: str | None = None,
) -> LiveAccountConfig:
    """Stamp an account as freshly broker-verified (or explicitly not demo).

    Only call this from a path that has just talked to the broker. It is the
    single writer of ``demo_verified_at``/``identity_fingerprint``, and the
    fingerprint it writes is what the gates compare against, so a re-pointed
    credential that never re-verifies cannot keep the old stamp.
    """
    normalized_id = str(account_id or "").strip()
    if not normalized_id:
        raise ValueError("account_id is required to record a demo verification")
    init_db()
    now = _utcnow_naive()
    with get_session() as session:
        record = session.get(LiveAccountModel, normalized_id)
        if record is None:
            raise ValueError(f"Unknown live account '{account_id}'")
        record.is_demo = bool(is_demo)
        if ctid is not None:
            record.ctid = ctid
        if broker_login is not None:
            record.broker_login = broker_login
        record.identity_fingerprint = live_account_identity_fingerprint(
            record.broker, ctid=record.ctid, broker_login=record.broker_login
        )
        record.demo_verified_at = now
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.to_live_account()


def demo_verification_age_seconds(account_id: str) -> float | None:
    """Seconds since an account's broker demo verification, or None if unknown.

    Same arithmetic as the freshness half of
    `live_account_verification_block_reason`, extracted so a caller that wants to
    know *whether to re-verify* is not forced to call the gate and parse its
    prose. Returns None when the account or its stamp is missing, which callers
    must treat as "needs verification" rather than "fresh" - an absent stamp is
    the unverified state, never a recent one.
    """
    init_db()
    with get_session() as session:
        record = session.get(LiveAccountModel, str(account_id or "").strip())
        if record is None:
            return None
        verified_at = record.demo_verified_at
        if verified_at is None:
            return None
        if not isinstance(verified_at, datetime):
            try:
                verified_at = datetime.fromisoformat(str(verified_at))
            except ValueError:
                return None
        return (_utcnow_naive() - verified_at).total_seconds()


def live_account_verification_needs_refresh(account_id: str) -> bool:
    """True when the stamp is absent or older than the TTL, so a refresh is due."""
    age = demo_verification_age_seconds(account_id)
    return age is None or age > DEMO_VERIFICATION_TTL_SECONDS


def live_account_verification_block_reason(
    account: LiveAccountModel | None,
) -> str | None:
    """Return why the account must not trade right now, or None when it may.

    The single fail-closed verdict shared by every path that can put an order on
    a broker: arming a deployment (``create_live_deployment``,
    ``update_live_deployment_status``, ``update_live_deployment_account``) and the
    daemon's per-dispatch gate. Uncertainties that could hide a live account all
    refuse:

    * unknown account, or one the broker did not confirm as demo;
    * no verification on record, or one that is unreadable or aged out past
      ``DEMO_VERIFICATION_TTL_SECONDS``;
    * a broker identity that no longer matches the one that was verified, or a
      fingerprint that is missing or cannot be recomputed.
    """
    if account is None:
        return "account is unknown"
    if not bool(account.is_demo):
        return "account is non-demo and not verified as demo"
    verified_at = account.demo_verified_at
    if verified_at is None:
        return "account has no broker demo verification on record"
    if not isinstance(verified_at, datetime):
        try:
            verified_at = datetime.fromisoformat(str(verified_at))
        except ValueError:
            return "account demo verification timestamp is unreadable"
    age = (_utcnow_naive() - verified_at).total_seconds()
    if age > DEMO_VERIFICATION_TTL_SECONDS:
        return (
            "account demo verification is older than "
            f"{DEMO_VERIFICATION_TTL_SECONDS}s; re-verify with the broker"
        )
    # Identity is checked after freshness so a long-idle account reports the
    # re-verify it actually needs, but it is still checked on every gate: a
    # fingerprint is only trustworthy for the identity it was taken from.
    return _live_account_fingerprint_mismatch_reason(account)


def live_account_submission_block_reason(account_id: str) -> str | None:
    """Per-dispatch verdict for one account, or None when a submission may go.

    This is the gate the live daemon calls immediately before every broker
    submission. It re-reads the account row so it sees the demo flag, the
    verification age and the broker identity as they are *now*, not as they were
    when the deployment was armed. Any failure to reach that verdict — database
    error, unmigrated schema, uncomputable fingerprint — propagates, and the
    caller's except-path refuses the dispatch.
    """
    normalized_id = str(account_id or "").strip()
    if not normalized_id:
        return "account id is required"
    init_db()
    with get_session() as session:
        record = session.get(LiveAccountModel, normalized_id)
        return live_account_verification_block_reason(record)


def _activation_block_reason(account: LiveAccountModel | None) -> str | None:
    """Return why activation must be refused, or None when it may proceed."""
    return live_account_verification_block_reason(account)


def _signal_source_block_reason(deployment: LiveDeploymentModel) -> str | None:
    """Return why this deployment can never signal, or None when it can.

    An ``internal`` deployment names a strategy module and expects the runtime to
    evaluate it against market data. No such evaluator exists on the live path:
    the daemon only ever turns a Discord message into a signal, so an armed
    internal deployment is not merely idle, it is permanently incapable of
    trading while reporting itself healthy.

    Arming it was previously accepted, which made the failure invisible: the row
    says ACTIVE, the account clears every demo and identity gate, and no order
    can ever be placed. Refusing to arm preserves the invariant that an ACTIVE
    deployment is one that can actually trade.
    """
    source_type = str(
        deployment.signal_source_type or deployment.signal_source or ""
    ).strip().lower()
    if source_type != "internal":
        return None
    return (
        "internal signal source has no live evaluator, so this deployment could "
        "never produce a signal; use a Discord signal source, or build the "
        "evaluator first"
    )


def update_live_deployment_status(
    deployment_id: str, status: str
) -> LiveDeploymentConfig | None:
    normalized_id = str(deployment_id or "").strip()
    next_status = str(status or "").upper()
    if next_status not in {"ACTIVE", "PAUSED", "STOPPED"}:
        raise ValueError(f"Invalid live deployment status '{status}'")
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, normalized_id)
        if record is None:
            return None
        if next_status == "ACTIVE":
            account = session.get(LiveAccountModel, record.account_id)
            block_reason = _activation_block_reason(account)
            if block_reason is not None:
                raise ValueError(
                    f"Refusing to activate a deployment: {block_reason}"
                )
            source_block = _signal_source_block_reason(record)
            if source_block is not None:
                raise ValueError(
                    f"Refusing to activate a deployment: {source_block}"
                )
        record.status = next_status
        record.updated_at = _utcnow_naive()
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_live_deployment()


def update_live_deployment_account(
    deployment_id: str,
    account_id: str,
    *,
    allow_active: bool = False,
) -> LiveDeploymentConfig | None:
    """Re-point a deployment at a different broker account.

    This is a safety-critical operation, so it is deliberately narrow:

    * the target account must exist and be a verified demo account;
    * an ACTIVE deployment is never re-pointed unless ``allow_active`` is set
      explicitly, because the running listener and its adapter still hold the
      old account until the next sync;
    * submissions already in flight for the old account are refused, since
      letting them land would trade the old account after the re-point.

    Returns None when the deployment does not exist.
    """
    normalized_deployment = str(deployment_id or "").strip()
    normalized_account = str(account_id or "").strip()
    if not normalized_deployment or not normalized_account:
        raise ValueError("deployment_id and account_id are required")
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, normalized_deployment)
        if record is None:
            return None

        account = session.get(LiveAccountModel, normalized_account)
        if account is None:
            raise ValueError(f"Refusing to re-point at unknown account '{account_id}'")
        # The full verdict, not just the is_demo flag. The docstring has always
        # promised a *verified* demo account, and this check only ever read the
        # flag, so it skipped both the freshness window and the identity
        # fingerprint that every other arming path enforces. An operator could
        # therefore re-point a deployment - including an ACTIVE one, with
        # allow_active - at an account whose verification expired hours ago, or
        # whose credentials had been rotated since. The daemon's per-dispatch gate
        # would still refuse the resulting orders, so no order reaches a non-demo
        # broker: the damage is a silently mis-armed deployment and a succeeded
        # audit record for a state the documented gate should have refused.
        #
        # Same helper, same reason strings, as create_live_deployment and
        # update_live_deployment_status, so the three cannot disagree about what
        # "verified" means.
        account_block_reason = live_account_verification_block_reason(account)
        if account_block_reason is not None:
            raise ValueError(
                f"Refusing to re-point a deployment at an account that is not "
                f"verified for live trading: {account_block_reason}"
            )

        current_status = str(record.status or "").strip().upper()
        if current_status == "ACTIVE" and not allow_active:
            raise ValueError(
                "Refusing to re-point an ACTIVE deployment; pause it first "
                "(or pass allow_active=True once the listener is stopped)"
            )

        # Only an unexpired lease is genuinely in flight. Counting every claim
        # regardless of expiry meant a lease whose TTL had elapsed still blocked a
        # re-point with "in-flight submissions; drain them first" -- an error the
        # operator cannot act on, because there is nothing left to drain. The
        # same expiry rule claim_live_deployment_submission uses to purge stale
        # rows (see _utcnow_naive() above it) decides what counts here. This does
        # not weaken the guard: a live claim's expires_at is in the future and is
        # still counted.
        in_flight = session.exec(
            select(func.count(LiveDeploymentSubmissionClaimModel.token)).where(
                (LiveDeploymentSubmissionClaimModel.deployment_id == normalized_deployment)
                & (LiveDeploymentSubmissionClaimModel.expires_at > _utcnow_naive())
            )
        ).one()
        if int(in_flight or 0) > 0:
            raise ValueError(
                "Refusing to re-point a deployment with in-flight submissions; "
                "drain them first"
            )

        record.account_id = normalized_account
        record.updated_at = _utcnow_naive()
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
        session.exec(
            delete(LiveDeploymentSubmissionClaimModel).where(
                LiveDeploymentSubmissionClaimModel.deployment_id == deployment_id
            )
        )
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


_LATEST_BALANCES_ROW_BUDGET = 50


def get_latest_live_balances(account_ids: list[str]) -> dict[str, LiveBalanceRecord]:
    """Latest balance per account in one query (avoids N+1 polling access).

    Reads only the newest window of balance rows (`max(account_ids) *
    _LATEST_BALANCES_ROW_BUDGET`) instead of every historical row for the
    account, then keeps the first (newest) record per account. An account with
    no row inside the window still falls back to the per-account query path.
    """
    normalized_ids = sorted(
        {str(item or "").strip() for item in account_ids if str(item or "").strip()}
    )
    if not normalized_ids:
        return {}

    init_db()
    latest: dict[str, LiveBalanceRecord] = {}
    with get_session() as session:
        row_budget = max(len(normalized_ids) * _LATEST_BALANCES_ROW_BUDGET, 1)
        rows = session.exec(
            select(LiveBalanceModel)
            .where(LiveBalanceModel.account_id.in_(normalized_ids))
            .order_by(LiveBalanceModel.timestamp.desc(), LiveBalanceModel.id.desc())
            .limit(row_budget)
        ).all()

    for row in rows:
        record = row.to_live_balance()
        latest.setdefault(record.account_id, record)

    missing_ids = [account_id for account_id in normalized_ids if account_id not in latest]
    for account_id in missing_ids:
        record = get_latest_live_balance(account_id)
        if record is not None:
            latest[account_id] = record
    return latest


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


def _bounded_query_limit(
    raw_limit: Any, *, default: int, maximum: int
) -> int:
    try:
        return max(1, min(int(raw_limit), maximum))
    except (TypeError, ValueError):
        return default


def _latest_executions_by_signal(
    execution_rows: list[LiveExecutionModel],
) -> dict[str, LiveExecutionModel]:
    latest_execution_by_signal: dict[str, LiveExecutionModel] = {}
    for execution in execution_rows:
        signal_id = str(execution.signal_id)
        if signal_id not in latest_execution_by_signal:
            latest_execution_by_signal[signal_id] = execution
    return latest_execution_by_signal


def _live_signal_deployment_base_fields(
    signal: LiveSignalModel,
) -> dict[str, Any]:
    return {
        "signal_id": str(signal.id),
        "deployment_id": str(signal.deployment_id),
        "symbol": str(signal.symbol or ""),
        "action": str(signal.action or ""),
        "source": str(signal.source or "internal"),
        "theoretical_price": float(signal.theoretical_price or 0.0),
        "signal_timestamp": signal.timestamp,
        "signal_metadata": signal.metadata_json or {},
    }


def _live_signal_deployment_execution_fields(
    execution: LiveExecutionModel | None,
) -> dict[str, Any]:
    return {
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


def _live_signal_deployment_payload(
    signal_rows: list[LiveSignalModel],
    latest_execution_by_signal: dict[str, LiveExecutionModel],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for signal in signal_rows:
        execution = latest_execution_by_signal.get(str(signal.id))
        entry = _live_signal_deployment_base_fields(signal)
        entry.update(_live_signal_deployment_execution_fields(execution))
        payload.append(entry)
    return payload


def list_live_signals_for_deployments(
    deployment_ids: list[str],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    normalized_ids = sorted({item.strip() for item in deployment_ids if item and item.strip()})
    if not normalized_ids:
        return []

    bounded_limit = _bounded_query_limit(limit, default=200, maximum=1000)

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

    latest_execution_by_signal = _latest_executions_by_signal(execution_rows)
    return _live_signal_deployment_payload(signal_rows, latest_execution_by_signal)


def _normalized_replay_deployment_ids(
    deployment_ids: list[str] | None,
) -> list[str]:
    return sorted(
        {str(item).strip() for item in (deployment_ids or []) if str(item).strip()}
    )


def _validate_replay_source(source: str | None) -> str:
    normalized_source = str(source or "").strip().lower()
    if normalized_source and (
        normalized_source != "internal" and not normalized_source.startswith("discord")
    ):
        raise ValueError(f"Invalid signal source '{source}'")
    return normalized_source


def _replay_signal_statement(
    normalized_ids: list[str],
    normalized_source: str,
    start: datetime | None,
    end: datetime | None,
) -> "SelectOfScalar[LiveSignalModel]":
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
    return statement


def _replay_signal_payload(rows: list[LiveSignalModel]) -> list[dict[str, Any]]:
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


def list_live_signals_for_replay(
    *,
    deployment_ids: list[str] | None = None,
    source: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    normalized_ids = _normalized_replay_deployment_ids(deployment_ids)

    normalized_source = _validate_replay_source(source)

    bounded_limit = _bounded_query_limit(limit, default=5000, maximum=20000)

    init_db()
    with get_session() as session:
        statement = _replay_signal_statement(normalized_ids, normalized_source, start, end)
        rows = session.exec(statement.limit(bounded_limit)).all()

    return _replay_signal_payload(rows)


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

        # One dict carries BOTH sides of the merge, under one name, so the
        # write below and the read in _backtest_meta_from_record() agree:
        #   - _backtest_config is the live-trading risk/sizing gate config. It is
        #     assigned (not setdefault) so a stale config can never survive a
        #     re-save with different risk settings.
        #   - duration_days/total_bars use setdefault so an explicit value the
        #     engine already put in meta.data wins over the meta attribute.
        persisted_data = dict(result.meta.data or {})
        persisted_data["_backtest_config"] = {
            "risk_per_trade_pct": result.meta.risk_per_trade_pct,
            "max_position_exposure_pct": result.meta.max_position_exposure_pct,
            "max_leverage": result.meta.max_leverage,
            "modeling_resolution": result.meta.modeling_resolution,
            "signal_exit_fill": result.meta.signal_exit_fill,
            "allow_fallback_stop": result.meta.allow_fallback_stop,
            "reject_missing_stop": result.meta.reject_missing_stop,
            "require_explicit_column_roles": result.meta.require_explicit_column_roles,
        }
        persisted_data.setdefault("duration_days", int(result.meta.duration_days or 0))
        persisted_data.setdefault("total_bars", int(result.meta.total_bars or 0))

        backtest_record = BacktestRecord(
            id=backtest_id,
            strategies=result.meta.strategies,
            engine=result.meta.engine,
            backtest_start=result.meta.backtest_start,
            backtest_end=result.meta.backtest_end,
            data=persisted_data,
            initial_capital=result.meta.initial_capital,
            parameters=result.meta.parameters,
            tags_json=result.meta.tags or [],
            stats=result.stats or {},
            notes=result.meta.notes,
            created_at=result.meta.created_at,
            equity_curve=[point.to_dict() for point in result.equity_curve],
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


def _top_up_equity_curve(result: BacktestResult) -> bool:
    """Fill an existing record's empty equity_curve from a migrated result.

    Returns True when the curve was topped up, False when the row was
    genuinely up to date (non-empty curve or nothing to fill from).
    """
    if not result.equity_curve:
        return False
    with get_session() as session:
        record = session.get(BacktestRecord, result.meta.id)
        if record is None:
            return False
        if record.equity_curve:
            return False
        record.equity_curve = [point.to_dict() for point in result.equity_curve]
        session.add(record)
        session.commit()
    return True


def migrate_json_to_sqlite(*, skip_existing: bool = True) -> dict[str, int]:
    """Migrate legacy ``*.trades.json`` files into SQLite.

    By default this is idempotent for app startup: existing backtest IDs are
    skipped instead of being re-written on every boot. Set ``skip_existing`` to
    false for an explicit repair/rebuild pass.
    """
    init_db()
    backtests_dir = DATA_DIR / "backtests"
    summary = {"migrated": 0, "skipped": 0, "failed": 0, "total": 0}

    if not backtests_dir.exists():
        return summary

    existing_ids: set[str] = set()
    if skip_existing:
        with get_session() as session:
            rows = session.exec(select(BacktestRecord.id)).all()
        existing_ids = {
            row_id
            for row in rows
            if (row_id := _coerce_backtest_row_id(row)) is not None
        }

    for path in sorted(backtests_dir.glob("*.trades.json")):
        summary["total"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = BacktestResult.from_dict(payload)
            if skip_existing and result.meta.id in existing_ids:
                if _top_up_equity_curve(result):
                    summary["migrated"] += 1  # topped up rather than plain skipped
                else:
                    summary["skipped"] += 1
                continue
            save_result_to_sqlite(result)
            existing_ids.add(result.meta.id)
            summary["migrated"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.warning("Could not migrate %s: %s", path, exc)

    if summary["total"]:
        logger.info(
            "JSON→SQLite migration finished: total=%s migrated=%s skipped=%s failed=%s",
            summary["total"],
            summary["migrated"],
            summary["skipped"],
            summary["failed"],
        )
    return summary


def _coerce_backtest_row_id(row: Any) -> str | None:
    """Extract a backtest id string from str/mapping/tuple query rows."""
    if isinstance(row, str):
        return row
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        mapping = getattr(row, "_mapping")
        value = mapping.get("id")
        return str(value) if value else None
    if isinstance(row, (tuple, list)) and row and row[0]:
        return str(row[0])
    return None


def list_backtest_ids() -> list[str]:
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(BacktestRecord.id).order_by(BacktestRecord.created_at.desc())
        ).all()
    ids: list[str] = []
    for row in rows:
        row_id = _coerce_backtest_row_id(row)
        if row_id is not None:
            ids.append(row_id)
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
    if math.isnan(numeric):
        return default
    return numeric


def _stat_number_or_undefined(
    stats: dict[str, Any], key: str, default: float
) -> float | None:
    """Resolve a stat, honouring the producer's ``undefined_metrics`` list.

    Delegates to :func:`shared.models.resolve_defined_stat`, which owns the
    convention. Substituting a numeric default here used to turn an undefined PSR
    into 0.0 - the *worst* attainable value - so a backtest with no trades was
    listed as a measured 0.0% PSR. A default p_value of 1.0 has the same shape.

    The default is only applied to a metric the producer did not name, so
    artifacts written before the convention keep their previous values.
    """
    return resolve_defined_stat(stats, key, default)


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
        "duration_days": max(int(_safe_stat_number(data.get("duration_days"), 0.0)), 0),
        "total_bars": max(int(_safe_stat_number(data.get("total_bars"), 0.0)), 0),
        "initial_capital": _safe_stat_number(backtest.initial_capital, 100_000.0),
        "total_pnl": _safe_stat_number(stats.get("total_pnl"), 0.0),
        "total_return": _safe_stat_number(stats.get("total_return"), 0.0),
        "cagr": _safe_stat_number(stats.get("cagr"), 0.0),
        "sharpe_ratio": _stat_number_or_undefined(stats, "sharpe_ratio", 0.0),
        "psr": _stat_number_or_undefined(stats, "psr", 0.0),
        "p_value": _stat_number_or_undefined(stats, "p_value", 1.0),
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


def _validated_evidence_identifiers(
    *,
    component: str,
    flow_name: str,
    category: str,
) -> tuple[str, str, str]:
    """Validate and normalize the required evidence identifier fields."""
    normalized_component = str(component or "").strip()
    if not normalized_component:
        raise ValueError("component is required")
    normalized_flow_name = str(flow_name or "").strip()
    if not normalized_flow_name:
        raise ValueError("flow_name is required")
    normalized_category = str(category or "").strip().lower()
    if not normalized_category:
        raise ValueError("category is required")
    return normalized_component, normalized_flow_name, normalized_category


def _verification_evidence_run_record(
    session: Session,
    *,
    run_id: str,
    now_iso: str,
) -> VerificationEvidenceModel:
    """Load the existing evidence row for a run or create a fresh one."""
    existing = session.exec(
        select(VerificationEvidenceModel)
        .where(VerificationEvidenceModel.run_id == run_id)
        .limit(1)
    ).first()
    record = existing if isinstance(existing, VerificationEvidenceModel) else None
    if record is None:
        record = VerificationEvidenceModel(
            id=str(uuid4()),
            run_id=run_id,
            created_at=now_iso,
        )
    return record


def _verification_evidence_schedule(
    record: VerificationEvidenceModel,
    *,
    started_at: str | None,
    finished_at: str | None,
    duration_seconds: float | None,
    now_iso: str,
) -> tuple[str, str, float]:
    """Resolve started/finished timestamps and duration for an evidence upsert."""
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
    return started_value, finished_value, safe_duration


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
    normalized_component, normalized_flow_name, normalized_category = (
        _validated_evidence_identifiers(component=component, flow_name=flow_name, category=category)
    )

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
        record = _verification_evidence_run_record(session, run_id=normalized_run_id, now_iso=now_iso)

        started_value, finished_value, safe_duration = _verification_evidence_schedule(
            record,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            now_iso=now_iso,
        )

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
    if status is not None and str(status).strip():
        raw_status = status.value if isinstance(status, VerificationEvidenceStatus) else str(status)
        normalized_status = _normalize_evidence_status(raw_status)
    else:
        normalized_status = ""

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
    normalized = normalize_task_status(task_status)
    return _TASK_STATUS_TO_EVIDENCE_STATUS[normalized]


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


def _build_background_task_record(
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
) -> BackgroundTaskRecord:
    """Create a fresh background task row with all fields populated."""
    return BackgroundTaskRecord(
        id=task_id,
        type=task_type,
        status=status,
        config_json=config_json,
        progress_message=str(progress_message or ""),
        name=str(name or ""),
        description=str(description or ""),
        source=str(source or ""),
        created_at=created_at,
        updated_at=updated_at,
    )


def _update_background_task_record(
    record: BackgroundTaskRecord,
    *,
    task_type: str,
    status: str,
    config_json: dict[str, Any],
    progress_message: str,
    name: str,
    description: str,
    source: str,
    created_at: str | None,
    created_value: str,
    updated_value: str,
) -> None:
    """Overwrite an existing background task row in place."""
    record.type = task_type
    record.status = status
    record.config_json = config_json
    record.progress_message = str(progress_message or "")
    record.name = str(name or "")
    record.description = str(description or "")
    record.source = str(source or "")
    if created_at or not str(record.created_at or "").strip():
        record.created_at = created_value
    record.updated_at = updated_value


def _record_task_completion_evidence(
    *,
    task_id: str,
    task_type: str,
    status: str,
    config_json: dict[str, Any],
    progress_message: str,
    name: str,
    description: str,
    source: str,
    created_value: str,
    updated_value: str,
) -> None:
    """Mirror a finished task into verification evidence, logging on failure."""
    try:
        _record_verification_evidence_for_task(
            task_id=task_id,
            task_type=task_type,
            status=status,
            config_json=config_json,
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
            task_id,
            exc,
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
    normalized_status = normalize_task_status(status)
    normalized_config = _strip_executable_command_fields(dict(config_json or {}))
    now_iso = _now_iso()
    created_value = str(created_at or "").strip() or now_iso
    updated_value = str(updated_at or "").strip() or now_iso

    init_db()
    with get_session() as session:
        record = session.get(BackgroundTaskRecord, normalized_id)
        if record is None:
            record = _build_background_task_record(
                task_id=normalized_id,
                task_type=normalized_type,
                status=normalized_status,
                config_json=normalized_config,
                progress_message=progress_message,
                name=name,
                description=description,
                source=source,
                created_at=created_value,
                updated_at=updated_value,
            )
        else:
            _update_background_task_record(
                record,
                task_type=normalized_type,
                status=normalized_status,
                config_json=normalized_config,
                progress_message=progress_message,
                name=name,
                description=description,
                source=source,
                created_at=created_at,
                created_value=created_value,
                updated_value=updated_value,
            )
        session.add(record)
        session.commit()

    if normalized_status in FINAL_TASK_STATUSES:
        _record_task_completion_evidence(
            task_id=normalized_id,
            task_type=normalized_type,
            status=normalized_status,
            config_json=normalized_config,
            progress_message=progress_message,
            name=name,
            description=description,
            source=source,
            created_value=created_value,
            updated_value=updated_value,
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
            normalized_statuses = [normalize_task_status(status) for status in statuses]
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
    normalized = [normalize_task_status(status) for status in from_statuses]
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


def _unwrap_equity_point_row(raw_row: Any) -> EquityPoint | None:
    """Coerce str/mapping/tuple/model query rows to an EquityPoint model."""
    if raw_row is None:
        return None
    if isinstance(raw_row, EquityPoint):
        row = raw_row
    elif hasattr(raw_row, "_mapping"):
        values = list(getattr(raw_row, "_mapping").values())
        row = values[0] if values else None
    elif isinstance(raw_row, (tuple, list)):
        row = raw_row[0] if raw_row else None
    else:
        row = raw_row
    return row if isinstance(row, EquityPoint) else None


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
        row = _unwrap_equity_point_row(raw_row)
        if row is None:
            continue
        points.append(EquityPointModel(time=row.time, equity=row.equity))
    return points


def load_backtest_equity_preview_points(backtest_id: str, limit: int = 80) -> list[EquityPointModel]:
    normalized_id = str(backtest_id or "").strip()
    if not normalized_id:
        return []
    preview_map = load_backtest_equity_preview_points_map([normalized_id], limit=limit)
    return list(preview_map.get(normalized_id, []))


def load_backtest_equity_preview_map(
    backtest_ids: list[str],
    limit: int = 50,
) -> dict[str, list[float]]:
    point_map = load_backtest_equity_preview_points_map(backtest_ids, limit=limit)
    return {
        backtest_id: [float(point.equity) for point in points]
        for backtest_id, points in point_map.items()
    }


def _equity_preview_query_params(
    normalized_ids: list[str], normalized_limit: int
) -> tuple[TextClause, dict[str, Any]]:
    """Build the ranked/bucketed preview SQL and its bound parameters."""
    params: dict[str, Any] = {"limit": normalized_limit}
    placeholders: list[str] = []
    for index, backtest_id in enumerate(normalized_ids):
        param_name = f"id_{index}"
        params[param_name] = backtest_id
        placeholders.append(f":{param_name}")

    if normalized_limit <= 1:
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
            WHERE row_number = 1
            ORDER BY time ASC, backtest_id ASC
            """
        )
    else:
        ranked_query = text(
            f"""
            WITH ordered AS (
                SELECT
                    backtest_id,
                    time,
                    equity,
                    ROW_NUMBER() OVER (
                        PARTITION BY backtest_id
                        ORDER BY time ASC, id ASC
                    ) AS row_number,
                    COUNT(*) OVER (
                        PARTITION BY backtest_id
                    ) AS total_count
                FROM equity_points
                WHERE backtest_id IN ({", ".join(placeholders)})
            ),
            bucketed AS (
                SELECT
                    backtest_id,
                    time,
                    equity,
                    row_number,
                    total_count,
                    CASE
                        WHEN total_count <= :limit THEN row_number
                        ELSE CAST(
                            ((row_number - 1) * 1.0 * (:limit - 1)) / (total_count - 1)
                            AS INTEGER
                        ) + 1
                    END AS bucket
                FROM ordered
            ),
            picked AS (
                SELECT
                    backtest_id,
                    time,
                    equity,
                    total_count,
                    bucket,
                    ROW_NUMBER() OVER (
                        PARTITION BY backtest_id, bucket
                        ORDER BY row_number ASC
                    ) AS bucket_first_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY backtest_id, bucket
                        ORDER BY row_number DESC
                    ) AS bucket_last_rank
                FROM bucketed
            )
            SELECT backtest_id, time, equity
            FROM picked
            WHERE total_count <= :limit
               OR (bucket = :limit AND bucket_last_rank = 1)
               OR (bucket != :limit AND bucket_first_rank = 1)
            ORDER BY time ASC, backtest_id ASC
            """
        )
    return ranked_query, params


def _equity_preview_row_fields(row: Any) -> tuple[str, str, Any] | None:
    if hasattr(row, "_mapping"):
        mapping = getattr(row, "_mapping")
        return (
            str(mapping.get("backtest_id") or "").strip(),
            str(mapping.get("time") or "").strip(),
            mapping.get("equity"),
        )
    if isinstance(row, (tuple, list)):
        return (
            str(row[0] or "").strip() if len(row) > 0 else "",
            str(row[1] or "").strip() if len(row) > 1 else "",
            row[2] if len(row) > 2 else None,
        )
    return None


def _append_equity_preview_row(
    previews: dict[str, list[EquityPointModel]], row: Any
) -> None:
    """Append one query row to its backtest's preview list, skipping bad rows."""
    fields = _equity_preview_row_fields(row)
    if fields is None:
        return
    backtest_id, timestamp, equity_value = fields
    if not backtest_id or backtest_id not in previews or not timestamp:
        return
    try:
        previews[backtest_id].append(EquityPointModel(time=timestamp, equity=float(equity_value)))
    except (TypeError, ValueError):
        return


def load_backtest_equity_preview_points_map(
    backtest_ids: list[str],
    limit: int = 50,
) -> dict[str, list[EquityPointModel]]:
    normalized_ids = [str(backtest_id).strip() for backtest_id in backtest_ids if str(backtest_id).strip()]
    if not normalized_ids:
        return {}

    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 200))

    init_db()
    ranked_query, params = _equity_preview_query_params(normalized_ids, normalized_limit)

    with get_session() as session:
        rows = session.exec(ranked_query, params=params).all()

    previews: dict[str, list[EquityPointModel]] = {backtest_id: [] for backtest_id in normalized_ids}
    for row in rows:
        _append_equity_preview_row(previews, row)

    return previews


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


def _load_backtest_child_rows(
    session: Session, backtest_id: str
) -> tuple[
    list[TradeRecord],
    list[EquityPoint],
    list[DrawingRecord],
    list[IndicatorRecord],
]:
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
    return trade_rows, equity_rows, drawing_rows, indicator_rows


def _apply_backtest_trade_risk_fallbacks(
    row: TradeRecord,
    position_size: float | None,
    risk_amount: float | None,
    risk_percent: float | None,
) -> tuple[float | None, float | None, float | None]:
    if not isinstance(row.meta, dict):
        return position_size, risk_amount, risk_percent
    risk_math = row.meta.get("risk_math")
    if not isinstance(risk_math, dict):
        return position_size, risk_amount, risk_percent
    if position_size is None:
        position_size = risk_math.get("position_size")
    realized_amount = risk_math.get("realized_risk_amount")
    realized_percent = risk_math.get("realized_risk_percent")
    if realized_amount is None:
        realized_amount = row.meta.get("realized_risk_amount")
    if realized_percent is None:
        realized_percent = row.meta.get("realized_risk_percent")
    if risk_amount is None:
        risk_amount = realized_amount
    if risk_percent is None:
        risk_percent = realized_percent
        if risk_percent is None and realized_amount is not None:
            equity_at_entry = risk_math.get("equity_at_entry")
            try:
                equity_value = float(equity_at_entry)
                amount_value = float(realized_amount)
            except (TypeError, ValueError):
                equity_value = 0.0
                amount_value = 0.0
            if equity_value > 0 and amount_value >= 0:
                risk_percent = amount_value / equity_value
    return position_size, risk_amount, risk_percent


def _build_backtest_trades(trade_rows: list[Any]) -> list[Trade]:
    trades: list[Trade] = []
    for raw_row in trade_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        exits_payload = row.exits or []
        exits = [TradeExit.from_dict(item) for item in exits_payload]
        position_size, risk_amount, risk_percent = _apply_backtest_trade_risk_fallbacks(
            row, row.position_size, row.risk_amount, row.risk_percent
        )
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
    return trades


def _build_backtest_equity_curve(equity_rows: list[Any]) -> list[EquityPointModel]:
    equity_curve = []
    for raw_row in equity_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        equity_curve.append(EquityPointModel(time=row.time, equity=row.equity))
    return equity_curve


def _backtest_meta_from_record(backtest: BacktestRecord) -> BacktestMeta:
    data = dict(backtest.data) if isinstance(backtest.data, dict) else {}
    duration_days = max(int(_safe_stat_number(data.get("duration_days"), 0.0)), 0)
    total_bars = max(int(_safe_stat_number(data.get("total_bars"), 0.0)), 0)
    runtime = data.get("_backtest_config", {})
    if not isinstance(runtime, dict):
        runtime = {}
    return BacktestMeta(
        id=backtest.id,
        created_at=backtest.created_at,
        engine=backtest.engine,
        strategies=backtest.strategies or [],
        backtest_start=backtest.backtest_start,
        backtest_end=backtest.backtest_end,
        data=data,
        initial_capital=backtest.initial_capital,
        risk_per_trade_pct=runtime.get("risk_per_trade_pct"),
        max_position_exposure_pct=runtime.get("max_position_exposure_pct"),
        max_leverage=runtime.get("max_leverage"),
        modeling_resolution=runtime.get("modeling_resolution", "main_timeframe"),
        signal_exit_fill=runtime.get("signal_exit_fill", "close"),
        allow_fallback_stop=runtime.get("allow_fallback_stop", True),
        reject_missing_stop=runtime.get("reject_missing_stop", False),
        require_explicit_column_roles=runtime.get("require_explicit_column_roles", False),
        parameters=backtest.parameters or {},
        tags=backtest.tags_json or [],
        notes=backtest.notes or "",
        duration_days=duration_days,
        total_bars=total_bars,
    )


def _collect_backtest_data_payloads(raw_rows: list[Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        row = _unwrap_model_row(raw_row)
        if row is None:
            continue
        if row.data:
            payloads.append(row.data)
    return payloads


def load_backtest_result(backtest_id: str) -> BacktestResult | None:
    init_db()
    with get_session() as session:
        backtest = session.get(BacktestRecord, backtest_id)
        if backtest is None:
            return None

        trade_rows, equity_rows, drawing_rows, indicator_rows = _load_backtest_child_rows(
            session, backtest_id
        )

    trades = _build_backtest_trades(trade_rows)

    equity_curve = _build_backtest_equity_curve(equity_rows)

    meta = _backtest_meta_from_record(backtest)

    drawings = _collect_backtest_data_payloads(drawing_rows)

    indicators = _collect_backtest_data_payloads(indicator_rows)

    return BacktestResult(
        meta=meta,
        trades=trades,
        equity_curve=equity_curve,
        stats=backtest.stats or {},
        indicators=[IndicatorSeries.from_dict(item) for item in indicators],
        drawings=drawings,
    )


def _portfolio_component_id(settings: dict[str, Any], index: int) -> str:
    """Prefer a caller-supplied component id, else the positional default."""
    component_id = f"component-{index:03d}"
    raw_component_id = settings.get("component_id")
    if isinstance(raw_component_id, str) and raw_component_id.strip():
        component_id = raw_component_id.strip()
    return component_id


def _portfolio_component_settings(component: PortfolioComponentModel) -> dict[str, Any]:
    """Copy component settings without the transient component_id key."""
    settings = dict(component.settings or {})
    settings.pop("component_id", None)
    return settings


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
            component_settings = _portfolio_component_settings(component)
            component_id = _portfolio_component_id(component.settings, index)
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
                        settings=component_settings,
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


def save_grid_result_to_sqlite(grid_result: dict[str, Any]) -> None:
    """Save grid strategy tester result to SQLite.

    Args:
        grid_result: Grid result payload from backtest_matrix.run_strategy_grid()
    """
    init_db()
    with get_session() as session:
        record = GridRecord(
            id=grid_result.get("id", ""),
            strategy=grid_result.get("strategy", ""),
            created_at=grid_result.get("created_at", ""),
            symbols_json=grid_result.get("symbols", []),
            timeframes_json=grid_result.get("timeframes", []),
            results_json=grid_result.get("results", {}),
            engine=grid_result.get("engine", "vectorbt"),
        )
        session.merge(record)
        session.commit()


def _grid_record_payload(record: GridRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "strategy": record.strategy,
        "created_at": record.created_at,
        "symbols": record.symbols_json,
        "timeframes": record.timeframes_json,
        "results": record.results_json,
        "engine": record.engine,
    }


def get_grid_results(limit: int = 100) -> list[dict[str, Any]]:
    """Retrieve grid results from SQLite, newest first.

    Args:
        limit: Maximum number of results to return

    Returns:
        List of grid result payloads
    """
    init_db()
    with get_session() as session:
        records = session.exec(
            select(GridRecord).order_by(GridRecord.created_at.desc()).limit(limit)
        ).all()

        return [_grid_record_payload(rec) for rec in records if isinstance(rec, GridRecord)]


def load_grid_result(grid_id: str) -> dict[str, Any] | None:
    """Load a single grid result payload from SQLite."""
    normalized_id = str(grid_id or "").strip()
    if not normalized_id:
        return None
    init_db()
    with get_session() as session:
        record = session.get(GridRecord, normalized_id)
    return _grid_record_payload(record) if isinstance(record, GridRecord) else None


def save_grid_result(
    grid_id: str,
    strategy: str,
    symbols: list[str],
    timeframes: list[str],
    results: dict[str, Any],
    engine: str = "vectorbt",
) -> None:
    """Save grid test results to SQLite."""
    init_db()
    with get_session() as session:
        record = GridRecord(
            id=grid_id,
            strategy=strategy,
            created_at=_now_iso(),
            symbols_json=symbols,
            timeframes_json=timeframes,
            results_json=results,
            engine=engine,
        )
        session.add(record)
        session.commit()


def list_grid_results() -> list[GridRecord]:
    """List all grid results, newest first."""
    init_db()
    with get_session() as session:
        rows = session.exec(
            select(GridRecord).order_by(GridRecord.created_at.desc())
        ).all()
    return [row for row in rows if isinstance(row, GridRecord)]


def load_grid_results(limit: int = 100) -> list[dict[str, Any]]:
    """Load persisted grid results from SQLite (newest first)."""
    return get_grid_results(limit=limit)

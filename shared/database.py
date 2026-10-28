from __future__ import annotations

import contextlib
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
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    String,
    TextClause,
    UniqueConstraint,
    delete,
    event,
    func,
    or_,
    text,
    update,
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
        sa_column=Column("tags", JSON, nullable=False, server_default=text("'[]'")),
    )
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    notes: str = Field(default="")
    created_at: str = Field(default="", index=True)
    equity_curve: list[dict[str, Any]] = Field(
        default_factory=list,
        # Production has this column NULLABLE with DEFAULT '[]'; the model says NOT NULL. The
        # nullability difference is left alone on purpose - narrowing it is a separate decision, and
        # a model that claimed NOT NULL where production allows NULL is a much smaller problem than
        # one that writes where it cannot.
        sa_column=Column("equity_curve", JSON, nullable=False, server_default=text("'[]'")),
    )
    # These four columns have existed in the physical table for some time - NOT NULL, with defaults
    # 0.0/0.0/0.0/1.0 - but the model never mapped them. Two consequences, both live:
    #
    #   * every INSERT took `DEFAULT 0.0`, so the fabricated 0.0 a corpus audit kept finding
    #     was **manufactured by the schema**, not by a legacy backward-compatibility path;
    #   * `session.merge()` issues an UPDATE over *mapped* columns only, so re-saving a backtest
    #     with freshly computed stats left the scalar column at its old value forever - so the JSON
    #     `stats` blob and the scalar column could disagree permanently, by construction.
    #
    # Mapped here so the columns can hold what the code computed. The defaults match the physical
    # definitions exactly, so a row that says nothing still stores what it stored before.
    # `Field(default=...)` is a *Python-side* default only, so `create_all` would produce NOT NULL
    # columns with **no database default** - a schema that diverges from production's, where these
    # carry DEFAULT 0.0/0.0/0.0/1.0. Any INSERT not going through the ORM would then fail outright,
    # and a fresh install would not match the database it is supposed to replace. So the defaults
    # are declared at the schema level too, matching production exactly - verified read-only
    # against `/home/ubuntu/data/backtests.db`.
    sharpe_ratio: float = Field(
        default=0.0,
        sa_column=Column("sharpe_ratio", Float, nullable=False, server_default="0.0"),
    )
    psr: float = Field(
        default=0.0,
        sa_column=Column("psr", Float, nullable=False, server_default="0.0"),
    )
    cagr: float = Field(
        default=0.0,
        sa_column=Column("cagr", Float, nullable=False, server_default="0.0"),
    )
    p_value: float = Field(
        default=1.0,
        sa_column=Column("p_value", Float, nullable=False, server_default="1.0"),
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
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    # Declared here, not only by `_migrate_schema`. A schema built by `create_all` used to have no
    # foreign key on this column at all, so whether an orphan signal was even possible depended on
    # which path created the database - and two test fixtures were relying on the difference. The
    # migration still exists because SQLite cannot add a foreign key to an existing table; it now
    # only has work to do on databases that predate this declaration.
    deployment_id: str = Field(foreign_key="live_deployments.id", index=True)
    symbol: str = Field(index=True)
    action: str = Field(index=True)
    theoretical_price: float = Field(default=0.0)
    timestamp: datetime = Field(index=True)
    # Production stores DEFAULT 'internal' on this column; the model declared only the Python-side
    # default, so a fresh install produced a NOT NULL column with no database default. `index` moves
    # onto the Column because SQLAlchemy rejects it on Field when `sa_column` is given.
    source: str = Field(
        default="internal",
        sa_column=Column("source", String, nullable=False, index=True,
                         server_default=text("'internal'")),
    )
    broker_order_id: str = Field(
        default="",
        sa_column=Column("broker_order_id", String, nullable=False, index=True,
                         server_default=text("''")),
    )
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
    # The broker's own per-fill id. Nullable and unindexed on purpose: it is *evidence*, not a
    # uniqueness constraint - `dedupe_key` is what enforces uniqueness, and indexing this would
    # invite reading it as one. Adapters with no such id leave it NULL.
    broker_fill_id: int | None = Field(default=None, nullable=True)
    # Whether `actual_price` is a price the broker stated: `observed`, `sent_bad` or `absent`.
    # Nullable and with no database default: a row written before this column existed has no
    # verdict, and "no verdict" must not be fabricated into "observed".
    actual_price_observed: str | None = Field(default=None, nullable=True)

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
            broker_fill_id=execution.broker_fill_id,
            actual_price_observed=execution.actual_price_observed,
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
            broker_fill_id=self.broker_fill_id,
            actual_price_observed=self.actual_price_observed,
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
    is_demo: bool = Field(
        default=False,
        sa_column=Column("is_demo", Boolean, nullable=False, index=True, server_default=text("0")),
    )

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
    signal_source: str = Field(
        default="internal",
        sa_column=Column(
            "signal_source", String, nullable=False, index=True, server_default=text("'internal'")
        ),
    )
    signal_source_type: str = Field(
        default="internal",
        sa_column=Column(
            "signal_source_type",
            String,
            nullable=False,
            index=True,
            server_default=text("'internal'"),
        ),
    )
    signal_source_id: str = Field(
        default="",
        sa_column=Column(
            "signal_source_id", String, nullable=False, index=True, server_default=text("''")
        ),
    )
    symbol_filter_json: list[str] = Field(
        default_factory=list,
        sa_column=Column("symbol_filter", JSON, nullable=False, server_default=text("'[]'")),
    )
    signal_config_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("signal_config", JSON, nullable=False, server_default=text("'{}'")),
    )
    settings_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("settings", JSON, nullable=False, server_default=text("'{}'")),
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
            # Preserved across a status write so stamping enforcement does not
            # reset it. A transition out of STOPPED clears it explicitly in
            # `update_live_deployment_status`.
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
            stopped_enforced_at=self.stopped_enforced_at,
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


class LiveDeploymentSubmissionClaimModel(SQLModel, table=True):
    """One in-flight submission lease per deployment.

    The claim turns "still ACTIVE" from a snapshot taken earlier in the dispatch
    into a single atomic database fact read immediately before the broker call.
    A PAUSE either wins the race (the claim fails and nothing is sent) or is
    already in effect while the claimed order finishes.

    ``UNIQUE(deployment_id)`` is the point: with a non-unique index a second
    dispatcher could claim the same ACTIVE deployment and both submit. Expired
    leases are reaped by :func:`claim_live_deployment_submission`, and a live
    submission renews its lease, so a slow broker call cannot silently lose it.
    """

    __tablename__ = "live_deployment_submission_claims"
    __table_args__ = (
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
    if "stopped_enforced_at" not in column_names:
        # The STOPPED enforcement ledger used to be a set in daemon memory, so a
        # restart re-ran cancel/flatten for deployments that were already
        # stopped. Persisting the stamp makes the ledger survive the process.
        connection.execute(
            text(
                "ALTER TABLE live_deployments "
                "ADD COLUMN stopped_enforced_at DATETIME"
            )
        )


def _add_backtests_stat_columns(connection: Connection) -> None:
    """Add the scalar stat columns to `backtests`, which were never migrated.

    `sharpe_ratio`, `psr`, `cagr` and `p_value` exist in the production table and have done for some
    time, but **no migration creates them**. They are absent from this model, so `create_all` never
    made them, and there is no `_add_*_column` step for them either. Every database in existence
    therefore splits two ways: production, where somebody added the columns by hand, and every other
    database - a fresh install, a developer's sandbox, a test fixture - built from the model, where
    they simply do not exist.

    That is why the columns read `0.0` on 2,600 rows rather than being absent: on those rows the
    *schema default* was taken, because nothing could write anything else.

    It stayed invisible while the model ignored the columns, since nothing ever named them. It stops
    being invisible the moment the model maps them, because then every `SELECT backtests.*` names
    them and a database without them fails outright - which is exactly how this surfaced.

    So the columns need the migration they should always have had. Definitions match production
    exactly, verified read-only against `/home/ubuntu/data/backtests.db`:

        sharpe_ratio REAL NOT NULL DEFAULT 0.0
        psr         REAL NOT NULL DEFAULT 0.0
        cagr        REAL NOT NULL DEFAULT 0.0
        p_value     REAL NOT NULL DEFAULT 1.0

    `p_value` defaults to 1.0, not 0.0, because "no evidence against the null hypothesis" is the
    correct value for a p-value that was never computed; 0.0 would assert the opposite.

    No-ops when the columns are already present, so this is safe on production and on repeat runs.
    """
    column_names = _table_column_names(connection, "backtests")
    if not column_names:
        return
    for column, ddl in (
        ("sharpe_ratio", "REAL NOT NULL DEFAULT 0.0"),
        ("psr", "REAL NOT NULL DEFAULT 0.0"),
        ("cagr", "REAL NOT NULL DEFAULT 0.0"),
        ("p_value", "REAL NOT NULL DEFAULT 1.0"),
    ):
        if column not in column_names:
            connection.execute(
                text(f"ALTER TABLE backtests ADD COLUMN {column} {ddl}")
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


def _add_live_account_columns(connection: Connection) -> None:
    """Add broker identity columns to legacy live_accounts tables."""
    column_names = _table_column_names(connection, "live_accounts")
    if "ctid" not in column_names:
        connection.execute(text("ALTER TABLE live_accounts ADD COLUMN ctid TEXT"))
    if "broker_login" not in column_names:
        connection.execute(text("ALTER TABLE live_accounts ADD COLUMN broker_login TEXT"))
    # The model maps this now, so every `SELECT live_accounts.*` names it. A database built before
    # that mapping existed does not have the column, and production's definition is NOT NULL
    # DEFAULT 0 - matched here so a migrated database and a fresh one are the same shape.
    if "is_demo" not in column_names:
        connection.execute(
            text("ALTER TABLE live_accounts ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0")
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


def _add_live_executions_broker_fill_id(connection: Connection) -> None:
    """Add `live_executions.broker_fill_id`, the broker's per-fill identifier.

    The dedupe key is a string this codebase assembled, so "were these two rows the same broker
    fill?" is not answerable from the table afterwards. When two equal-sized fills on one order
    collide - the case backlog item 1 is about - taking the key apart is the only evidence there is,
    and it is ambiguous by construction.

    Nullable with no default and no index: recorded evidence, not a constraint. Uniqueness stays
    with `dedupe_key` and its partial unique index, and adapters with no such id leave this NULL.
    No-ops when the column already exists.
    """
    column_names = _table_column_names(connection, "live_executions")
    if not column_names:
        return
    if "broker_fill_id" not in column_names:
        connection.execute(text("ALTER TABLE live_executions ADD COLUMN broker_fill_id INTEGER"))


def _add_live_executions_actual_price_observed(connection: Connection) -> None:
    """Add `live_executions.actual_price_observed`, the verdict on `actual_price`.

    `actual_price` cannot say whether the broker stated a price at all: the resolver's "could not
    be read" sentinel is `0.0`, so an omitted price, an explicitly-sent zero and a genuine zero
    are one stored value. Anything derived from such a row is meaningless, and nothing on the
    table said which rows those were.

    Nullable, no default, unindexed: a row written before this column existed has *no verdict*,
    which is a different fact from `observed` and must not be fabricated into one. Additive only -
    no existing row changes, and a reader that ignores the column behaves exactly as before.

    No-ops when the column already exists, because `init_db` runs on every process start.
    """
    column_names = _table_column_names(connection, "live_executions")
    if not column_names:
        return
    if "actual_price_observed" not in column_names:
        connection.execute(
            text("ALTER TABLE live_executions ADD COLUMN actual_price_observed TEXT")
        )


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


_SMOKE_DEPLOYMENT_PREFIX = "ctrader-demo-smoke-"


def _backfill_smoke_deployments(connection: Connection) -> int:
    """Give smoke-sourced orphan signals the parent deployment row they were always meant to have.

    ``live_smoke`` writes ``deployment_id = ctrader-demo-smoke-<account>`` but historically created
    no deployment row, so those signals were invisible to every deployment-scoped query. This
    recreates exactly the row ``_ensure_ctrader_smoke_deployment`` writes on new runs, so history
    and future runs agree.

    Three deliberate restrictions, each a guard against fabricating a trading record:

    * only ids carrying the smoke prefix are touched - anything else is somebody's real deployment;
    * the account must already exist in ``live_accounts``, because ``live_deployments.account_id``
      carries a foreign key and an invented account would be worse than an orphan;
    * the row is ``PAUSED`` with the smoke label as ``strategy_name``, following the existing
      ``discord::`` convention for synthetic deployments, so no runner trades it.

    **Adoption, not just creation.** ``live_deployments.id`` is typed ``uuid.UUID`` on
    ``LiveDeploymentConfig``, so the readable label lives in ``strategy_name`` and the key is a
    UUID. That means a parent can already exist under the same label with a different key - the
    shape left by a run that predates this backfill - and the signals naming the label are still
    orphans.
    An earlier version skipped that case, which left the signals orphaned *and* the parent present:
    the migration then failed its ``foreign_key_check`` and ``init_db()`` raised. So an existing
    parent is adopted and the signals are repointed at it, exactly as a newly created one is.

    Never deletes. Non-smoke orphans are left untouched and reported.

    Returns the number of smoke deployment rows created *or* adopted.
    """
    column_names = _table_column_names(connection, "live_deployments")
    signal_columns = _table_column_names(connection, "live_signals")
    if "account_id" not in column_names or "deployment_id" not in signal_columns:
        return 0

    orphans = connection.execute(
        text(
            "SELECT s.deployment_id, s.symbol "
            "FROM live_signals s "
            "WHERE s.deployment_id LIKE :prefix "
            "AND s.deployment_id NOT IN (SELECT id FROM live_deployments)"
        ),
        {"prefix": _SMOKE_DEPLOYMENT_PREFIX + "%"},
    ).fetchall()
    if not orphans:
        return 0

    # One row per deployment, not per signal: the same smoke label covers every signal of that
    # account, so iterating the pairs would count a deployment twice and give its `symbols` column
    # whichever symbol happened to come back first.
    symbols_by_deployment: dict[str, set[str]] = {}
    for deployment_id, symbol in orphans:
        symbols = symbols_by_deployment.setdefault(str(deployment_id), set())
        if symbol:
            symbols.add(str(symbol).upper())

    resolved = 0
    for deployment_id, symbols in symbols_by_deployment.items():
        account_id = str(deployment_id)[len(_SMOKE_DEPLOYMENT_PREFIX):].strip()
        if not account_id:
            continue
        known = connection.execute(
            text("SELECT 1 FROM live_accounts WHERE id = :id"), {"id": account_id}
        ).first()
        if known is None:
            # Refuse to invent an account. Leave the orphan and let the FK stay blocked.
            logger.warning(
                "Leaving smoke orphan %s: account %s is not in live_accounts",
                deployment_id,
                account_id,
            )
            continue
        # A readable lookup keys on `strategy_name`, because the id is a UUID.
        existing = connection.execute(
            text("SELECT id FROM live_deployments WHERE strategy_name = :name"),
            {"name": deployment_id},
        ).first()
        if existing is not None:
            new_id = str(existing[0])
        else:
            new_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat(sep=" ")
            connection.execute(
                text(
                    "INSERT INTO live_deployments "
                    "(id, strategy_name, account_id, symbols, timeframe, status, "
                    " signal_source, signal_source_type, signal_source_id, symbol_filter, "
                    " signal_config, settings, created_at, updated_at) "
                    "VALUES (:new_id, :name, :account_id, :symbols, '1m', 'PAUSED', "
                    " 'smoke', 'internal', '', '[]', '{}', '{}', :now, :now)"
                ),
                {
                    "new_id": new_id,
                    "name": deployment_id,
                    "account_id": account_id,
                    "symbols": json.dumps(sorted(symbols)),
                    "now": now,
                },
            )
        # The signals still name the descriptive label, which is `strategy_name` rather than `id`.
        # Repoint them, or the parent exists and every signal is still orphaned - the state this
        # function exists to leave behind.
        connection.execute(
            text("UPDATE live_signals SET deployment_id = :new WHERE deployment_id = :old"),
            {"new": new_id, "old": deployment_id},
        )
        resolved += 1
    if resolved:
        logger.info("Backfilled %d smoke deployment row(s) for orphaned signals", resolved)
    return resolved


def _migrate_live_signal_deployment_fk(connection: Connection) -> bool:
    """Add the ``live_signals.deployment_id -> live_deployments.id`` foreign key.

    SQLite cannot add a foreign key to an existing table, so the table is rebuilt: capture index DDL
    *before* dropping, create the new table, copy, drop, rename, restore indexes.

    Three traps this step exists to avoid:

    * reading index DDL after ``DROP TABLE`` silently loses every index, because the drop removes
      them. ``PRAGMA foreign_key_check`` validates referential integrity only and never notices.
    * copying rows that violate the new constraint. That is not caught here either - the surrounding
      ``_schema_migration_connection`` runs ``foreign_key_check`` and raises, rolling the whole
      migration back. So this is safe to attempt while orphans remain: it either lands or changes
      nothing.
    * ``live_executions.signal_id`` already references ``live_signals(id)`` - a foreign key this
      migration does not own and must not drop. With enforcement on, ``DROP TABLE live_signals`` is
      refused *even when every dependent row has been removed*, because the surviving executions
      point at rows inside the table being dropped. Measured on a production clone: the migration
      failed at exactly that statement with no orphan rows left. So enforcement is off for the whole
      swap and restored in a ``finally``; the surrounding ``foreign_key_check`` is what still makes
      a bad swap fail closed.

    Called from ``_migrate_schema``, after ``_backfill_smoke_deployments`` has parented what it can.
    On a database that still carries a non-smoke orphan the rebuild lands, the surrounding
    ``foreign_key_check`` reports the violation and the whole migration rolls back - so this is
    fail-closed, not silent.

    Returns True when the rebuild ran.
    """
    if any(
        row[3] == "deployment_id"
        for row in connection.execute(text("PRAGMA foreign_key_list(live_signals)")).fetchall()
    ):
        return False

    # Capture index DDL before anything is dropped: DROP TABLE removes the indexes with it, and
    # foreign_key_check validates referential integrity only, so lost indexes go unnoticed.
    index_ddl = [
        row[0]
        for row in connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'live_signals' AND sql IS NOT NULL"
            )
        ).fetchall()
    ]
    create_sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'live_signals'")
    ).scalar_one_or_none()
    if not create_sql:
        return False

    # Reuse the real CREATE TABLE text and splice the constraint in just before the closing paren.
    # Rebuilding from a column-name list silently drops PRIMARY KEY, which makes live_signals.id no
    # longer unique - and live_executions has an FK to it, so every later pragma raises
    # "foreign key mismatch" and the database is left unusable. The column-list approach was tried
    # and failed exactly that way.
    closing = create_sql.rstrip().rfind(")")
    if closing == -1:
        return False
    body = create_sql.rstrip()[:closing].rstrip()
    if body.endswith(","):
        body = body[:-1]
    new_create_sql = (
        f"{body},\n\tFOREIGN KEY (deployment_id) REFERENCES live_deployments (id)\n)"
    )

    # The table name is rewritten through a pattern rather than `str.replace`, because SQLite may
    # store it quoted: a rebuild that succeeds leaves `CREATE TABLE "live_signals" (...)` behind,
    # and a plain replace of the unquoted spelling would then match nothing - so a second run (after
    # the FK was lost some other way) would recreate `live_signals` while it still existed.
    new_create_sql, replacements = re.subn(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\"live_signals\"|`live_signals`|\[live_signals\]|live_signals)",
        "CREATE TABLE live_signals__fk_new",
        new_create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if replacements != 1:
        raise RuntimeError(
            "Cannot rebuild live_signals: could not address the table in its own CREATE statement"
        )

    columns = [
        str(row[1])
        for row in connection.execute(text("PRAGMA table_info('live_signals')")).fetchall()
    ]
    if not columns:
        return False
    column_list = ", ".join(
        connection.dialect.identifier_preparer.quote(column) for column in columns
    )

    # `foreign_keys` must be OFF for the whole swap, not just around the DROP. SQLite ignores the
    # pragma while a transaction is open, so setting it mid-transaction leaves enforcement ON and
    # the DROP is still refused. `_schema_migration_connection` already disables it before BEGIN;
    # this is here for callers that do not, and is restored in the finally below either way.
    #
    # legacy_alter_table keeps RENAME from rewriting the live_executions -> live_signals reference.
    foreign_keys_before = int(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
    legacy_alter_table_before = int(
        connection.execute(text("PRAGMA legacy_alter_table")).scalar_one()
    )
    try:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("PRAGMA legacy_alter_table=ON"))
        connection.execute(text("DROP TABLE IF EXISTS live_signals__fk_new"))
        connection.execute(text(new_create_sql))
        connection.execute(
            text(
                f"INSERT INTO live_signals__fk_new ({column_list}) "
                f"SELECT {column_list} FROM live_signals"
            )
        )
        connection.execute(text("DROP TABLE live_signals"))
        connection.execute(text("ALTER TABLE live_signals__fk_new RENAME TO live_signals"))
    finally:
        connection.execute(text(f"PRAGMA legacy_alter_table={legacy_alter_table_before}"))
        connection.execute(text(f"PRAGMA foreign_keys={foreign_keys_before}"))
    for statement in index_ddl:
        connection.execute(text(statement))
    logger.info("Rebuilt live_signals with the deployment_id foreign key")
    return True


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
    """Collapse concurrent claims for one deployment down to the live one.

    A plain unique index cannot be created while two rows share a deployment, and
    refusing the migration would take the whole database down for every caller,
    which is a worse outcome than collapsing the duplicates.

    The keeper is the **in-flight** lease, not merely the one with the latest
    ``expires_at``. A lease that has not expired is a submission that may still
    be at the broker, and deleting it would let a second dispatcher submit a
    duplicate; an expired lease is evidence that an outcome was never observed.
    So in-flight wins over expired unconditionally, and only among in-flight
    leases does the latest expiry (the most remaining runway) decide. When every
    lease has expired, the latest one is kept so the deployment stays claimable.
    """
    duplicated = connection.execute(
        text(
            "SELECT deployment_id FROM live_deployment_submission_claims "
            "GROUP BY deployment_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    now = _utcnow_naive()
    removed = 0
    for (deployment_id,) in duplicated:
        keeper = connection.execute(
            text(
                "SELECT token FROM live_deployment_submission_claims "
                "WHERE deployment_id = :deployment_id "
                "ORDER BY (expires_at > :now) DESC, "
                "expires_at DESC, claimed_at DESC, token DESC LIMIT 1"
            ),
            {"deployment_id": str(deployment_id), "now": now},
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
        _add_live_deployment_columns(
            connection, _table_column_names(connection, "live_deployments")
        )
        _add_backtests_tags_column(connection)
        _add_backtests_stat_columns(connection)
        _add_live_account_columns(connection)
        _add_background_task_columns(connection)
        _add_trade_columns(connection)

        # Parent the smoke-sourced orphans before the FK is added, so the rebuild never copies a
        # row that violates the constraint it is introducing. On a database whose only orphans are
        # smoke-shaped this makes the FK land; anything left over keeps the migration fail-closed
        # via `foreign_key_check` at the end of `_schema_migration_connection`.
        _backfill_smoke_deployments(connection)
        _migrate_live_signal_deployment_fk(connection)

        _migrate_live_signal_discord_identity(connection)
        _migrate_live_signal_idempotency_key(connection)
        _migrate_live_submission_claim_uniqueness(connection)

        _migrate_backtests_equity_curve(connection)
        _migrate_live_executions_dedupe_key(connection)
        _add_live_executions_broker_fill_id(connection)
        _add_live_executions_actual_price_observed(connection)


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
            local_filled_qty = float(
                session.exec(
                    select(func.coalesce(func.sum(LiveExecutionModel.filled_qty), 0.0)).where(
                        LiveExecutionModel.signal_id == normalized_signal_id
                    )
                ).one()
                or 0.0
            )
            if existing is not None:
                # Same dedupe key. Two very different situations share this branch, and conflating
                # them loses fills:
                #
                #   redelivery   the broker sent the same deal again, unchanged. Dropping it is
                #                right - this is the duplicate the key exists to catch.
                #   amendment    the broker sent the same dealId with a LARGER quantity, because
                #                `ProtoOADeal` carries `filledVolume` and the deal grew. Dropping it
                #                freezes the stored quantity at its first-seen value and the rest of
                #                the fill is never recorded.
                #
                # cTrader exposes no fill-level identifier - a deal entry has dealId, orderId,
                # positionId and symbolId and nothing finer - so an order split into several fills
                # arrives as several deals with distinct dealIds, each stored. That case is already
                # correct. The remaining risk is amendment, and it is handled here: only an increase
                # amends, and a decrease is treated as redelivery rather than shrinking a fill that
                # already happened.
                #
                # The increase is capped by what the broker still says is missing for the whole
                # signal, exactly as the insert path below is. Without the cap a redelivery of a
                # deal whose first insert was itself capped - because the broker's cumulative was
                # lower than the deal claimed - would grow the stored total past the broker's own
                # number, which is the same over-count the dedupe key exists to prevent.
                prior_qty = float(existing.filled_qty or 0.0)
                delta = min(requested_qty - prior_qty, cumulative_qty - local_filled_qty)
                if delta <= 0.0:
                    session.rollback()
                    return 0.0
                existing.filled_qty = prior_qty + delta
                if record.commission:
                    # Commission was recorded against the first-seen quantity, so only the
                    # incremental slice needs adding. A deal that reports commission per unit is
                    # handled by the caller scaling it, same as the partial-persist path below.
                    existing.commission = float(existing.commission or 0.0) + float(
                        record.commission
                    ) * (delta / requested_qty)
                if record.actual_price and not existing.actual_price:
                    existing.actual_price = float(record.actual_price)
                session.add(existing)
                session.commit()
                logger.info(
                    "Amended live execution %s: dedupe key %s repeated with a larger quantity, "
                    "filled_qty %s -> %s",
                    existing.id,
                    existing.dedupe_key,
                    prior_qty,
                    existing.filled_qty,
                )
                return delta

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


def _create_live_account_record(
    session: Session,
    account_id: str,
    broker: str,
    *,
    name: str,
    ctid: str | None,
    broker_login: str | None,
) -> LiveAccountConfig:
    """Insert a brand-new live account row."""
    account = LiveAccountModel(
        id=account_id,
        broker=broker,
        name=name or account_id,
        ctid=_optional_trimmed(ctid),
        broker_login=_optional_trimmed(broker_login),
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
) -> bool:
    """Overwrite non-empty fields on an existing live account row."""
    changed = False
    if broker and current.broker != broker:
        current.broker = broker
        changed = True
    if name and current.name != name:
        current.name = name
        changed = True
    normalized_ctid = _optional_trimmed(ctid)
    if ctid is not None and current.ctid != normalized_ctid:
        current.ctid = normalized_ctid
        changed = True
    normalized_broker_login = _optional_trimmed(broker_login)
    if broker_login is not None and current.broker_login != normalized_broker_login:
        current.broker_login = normalized_broker_login
        changed = True
    if changed:
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
            )

        _apply_live_account_updates(
            session,
            current,
            broker=broker,
            name=name,
            ctid=ctid,
            broker_login=broker_login,
        )
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
    respect_operator_intent: bool = True,
) -> Service | None:
    """Write a service's status, with PAUSED as an atomic floor.

    `respect_operator_intent` makes PAUSED a floor: a PAUSED service is never
    moved to any other status by this function. The pause was set by a person
    through the API, and nothing the process observes afterwards - a message
    arriving, a connector failing, a reconnect completing - outranks that.

    The check is a conditional ``UPDATE`` rather than read-check-write. The
    previous shape read the row, compared in Python, and wrote it back, so a
    FAILED write that committed between the read and the write silently
    overwrote PAUSED; `_recover_failed_services` then read FAILED and restarted
    the bot the operator had paused. SQLite serialises writers, so a single
    statement whose ``WHERE`` re-tests the status cannot lose that race.

    Pass `respect_operator_intent=False` only where the caller is *establishing*
    operator intent, i.e. an API call that is itself setting or clearing a pause.
    """
    normalized_id = str(service_id or "").strip()
    if not normalized_id:
        return None
    normalized_status = _normalize_service_status(status)
    init_db()
    values: dict[str, Any] = {
        "status": normalized_status,
        "updated_at": _utcnow_naive(),
    }
    if last_activity is not None:
        values["last_activity"] = last_activity
    with get_session() as session:
        statement = (
            update(ServiceModel)
            .where(ServiceModel.id == normalized_id)
            .values(**values)
        )
        if (
            respect_operator_intent
            and normalized_status != ServiceStatus.PAUSED.value
        ):
            # Re-tested in the same statement as the write, so a concurrent
            # PAUSE cannot be overwritten by a FAILED/RUNNING landing after it.
            statement = statement.where(
                ServiceModel.status != ServiceStatus.PAUSED.value
            )
        session.execute(statement)
        session.commit()
        record = session.get(ServiceModel, normalized_id)
        if record is None:
            return None
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

    validate_stockkit_deployment_route(deployment)
    init_db()
    with get_session() as session:
        account = session.get(LiveAccountModel, deployment.account_id)
        if account is None:
            raise ValueError(f"Unknown live account '{deployment.account_id}'")
        deployment_status = str(deployment.status or "ACTIVE").upper()
        if deployment_status not in {"ACTIVE", "PAUSED", "STOPPED"}:
            raise ValueError(f"Invalid deployment status '{deployment.status}'")
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
        if next_status != "STOPPED":
            # Clear the enforcement stamp in the *same* transaction as the
            # status change. Without this a STOPPED -> PAUSED -> STOPPED cycle
            # keeps the old stamp, and the second stop is skipped as "already
            # enforced" while its positions are still open. Doing it in one
            # commit matters: a crash between the two writes must not leave a
            # non-STOPPED deployment carrying a stamp that disables the next
            # enforcement.
            record.stopped_enforced_at = None
        record.updated_at = _utcnow_naive()
        session.add(record)
        session.commit()
        session.refresh(record)
    return record.to_live_deployment()


#: How long one submission lease is valid. Short enough that a crashed
#: dispatcher does not wedge a deployment for long, long enough that a normal
#: broker call finishes well inside it. A slow call renews its lease (see
#: :func:`renew_live_deployment_submission`), so this is not a hard cap on
#: submission duration.
LIVE_SUBMISSION_CLAIM_TTL_SECONDS = 30.0


def mark_live_deployment_stopped_enforced(deployment_id: str) -> bool:
    """Record that STOPPED enforcement completed for this deployment.

    Only called when the adapter's cancel/flatten sweeps were *complete*; a
    partial sweep must stay unmarked so the next tick retries it.
    """
    normalized = str(deployment_id or "").strip()
    if not normalized:
        return False
    init_db()
    with get_session() as session:
        record = session.get(LiveDeploymentModel, normalized)
        if record is None:
            return False
        now = _utcnow_naive()
        record.stopped_enforced_at = now
        record.updated_at = now
        session.add(record)
        session.commit()
    return True


def clear_live_deployment_stopped_enforced(deployment_id: str) -> bool:
    """Forget the enforcement stamp so a later STOPPED transition re-enforces.

    The status transition clears it transactionally; this helper exists for the
    daemon's cleanup path, which may observe a deployment that left STOPPED
    without a status write it performed itself.
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
    does not repeat cancel/flatten work that is already done.
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
    * no other process holds a lease for it (``UNIQUE(deployment_id)``).

    An expired lease is deleted first, in its own committed transaction, so a
    claimor that died mid-submission cannot wedge the deployment forever. The
    cleanup is committed separately on purpose: it must survive a refused
    claim, otherwise a permanently-busy deployment would also stop reaping its
    own dead leases.
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
            # already submitting for this deployment. Refusing is the point.
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


def renew_live_deployment_submission(
    token: str,
    *,
    ttl_seconds: float = LIVE_SUBMISSION_CLAIM_TTL_SECONDS,
) -> bool:
    """Extend a held lease so a slow broker call does not lose it.

    The claim has a TTL, and a submission that outlives it used to become
    reclaimable while it was still at the broker: a second dispatcher would then
    take the lease and submit a duplicate order. Renewal is only valid for the
    token that still owns the row, so a lease that was already reaped or taken
    over cannot be revived.
    """
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return False
    init_db()
    now = _utcnow_naive()
    expires_at = now + timedelta(seconds=max(float(ttl_seconds), 0.01))
    with get_session() as session:
        result = session.execute(
            text(
                "UPDATE live_deployment_submission_claims "
                "SET expires_at = :expires_at WHERE token = :token"
            ),
            {"expires_at": expires_at, "token": normalized_token},
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def release_live_deployment_submission(token: str) -> bool:
    """Release a lease whose submission outcome is known.

    Never called when the outcome is unknown: a broker may have accepted an
    order whose response was lost, and releasing the lease would let the next
    signal submit a duplicate before anyone reconciled.
    """
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

    This never deletes anything. An expired-but-never-released claim is
    *evidence that an outcome was never observed*, and must not read as "nothing
    was in flight".
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


class LiveDeploymentInUseError(RuntimeError):
    """Raised when a deployment cannot be deleted because records still reference it.

    A typed error instead of the driver's ``IntegrityError`` escaping through the ORM: the caller
    has to make a decision - refuse, archive, or delete the history deliberately first - and a
    ``sqlite3.IntegrityError`` cannot carry that. Nothing is deleted when this is raised, and the
    check happens before the delete rather than relying on the foreign key, so the behaviour is the
    same on a database whose ``live_signals.deployment_id`` key was never migrated in.
    """

    def __init__(
        self,
        deployment_id: str,
        *,
        signal_count: int | None = None,
        execution_count: int | None = None,
    ) -> None:
        detail = ""
        if signal_count is not None and execution_count is not None:
            detail = f" ({signal_count} signal(s), {execution_count} execution(s))"
        super().__init__(
            f"Deployment {deployment_id} still has records referencing it{detail}; nothing was "
            f"deleted. Resolve or remove those records explicitly if the history is expendable."
        )
        self.deployment_id = deployment_id
        self.signal_count = signal_count
        self.execution_count = execution_count


def delete_live_deployment(deployment_id: str) -> bool:
    """Delete a deployment, refusing while signals or executions still reference it.

    It used to attempt the delete unconditionally, so any deployment with history raised a raw
    ``sqlite3.IntegrityError`` out of the ORM - the viewer turned that into a 500 after having
    already set the deployment PAUSED, leaving it stuck there. Deleting the signals as a cascade
    would have "worked", but it silently destroys the execution record of real trades, which is the
    one thing this database exists to keep.

    So the refusal is explicit and typed, the counts come with it, and the caller decides. Returns
    True when the row was deleted and False when it was already absent.
    """
    normalized_id = str(deployment_id or "").strip()
    if not normalized_id:
        return False
    init_db()
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            record = session.get(LiveDeploymentModel, normalized_id)
            if record is None:
                session.rollback()
                return False
            signal_count = int(
                session.exec(
                    select(func.count(LiveSignalModel.id)).where(
                        LiveSignalModel.deployment_id == normalized_id
                    )
                ).one()
                or 0
            )
            execution_count = int(
                session.exec(
                    select(func.count(LiveExecutionModel.id))
                    .join(
                        LiveSignalModel,
                        LiveExecutionModel.signal_id == LiveSignalModel.id,
                    )
                    .where(LiveSignalModel.deployment_id == normalized_id)
                ).one()
                or 0
            )
            if signal_count or execution_count:
                session.rollback()
                raise LiveDeploymentInUseError(
                    normalized_id,
                    signal_count=signal_count,
                    execution_count=execution_count,
                )
            session.delete(record)
            session.commit()
        except LiveDeploymentInUseError:
            raise
        except IntegrityError as exc:
            # A signal inserted between the count and the delete. The foreign key caught it, and the
            # caller gets the same typed refusal rather than a driver exception.
            session.rollback()
            raise LiveDeploymentInUseError(normalized_id) from exc
        except Exception:
            session.rollback()
            raise
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


# The scalar stat columns, with the default each physically carries. `p_value` defaults to 1.0
# because "no evidence against the null hypothesis" is the right value for a p-value that was never
# computed - which is exactly why it must never be silently replaced by 0.0.
_SCALAR_STAT_COLUMNS: tuple[tuple[str, float], ...] = (
    ("sharpe_ratio", 0.0),
    ("psr", 0.0),
    ("cagr", 0.0),
    ("p_value", 1.0),
)


def _scalar_stats_for_save(
    session: Any, backtest_id: str, stats: dict[str, Any]
) -> dict[str, float]:
    """The values the four scalar stat columns should hold after this save.

    A metric the stats **define** is written. A metric they do not - because the producer listed it
    in `undefined_metrics`, or because the artifact predates the convention and never named it -
    **carries its current column value forward**.

    That second half is the whole point. These columns are NOT NULL, so "undefined" cannot be
    stored. Letting the model's default land instead would mean a re-save of a backtest whose
    Sharpe is no longer computable overwrites a real stored value with 0.0 - destroying data to
    express absence, which is the failure the corpus audit found in the first place, reintroduced
    by the fix for it. So absence leaves the column alone and only a finite number moves it - a
    malformed or non-finite value is not a number the column can hold honestly, so it counts as
    absence and is logged.
    """
    current: dict[str, float] = {name: default for name, default in _SCALAR_STAT_COLUMNS}
    row = session.exec(
        select(
            BacktestRecord.sharpe_ratio,
            BacktestRecord.psr,
            BacktestRecord.cagr,
            BacktestRecord.p_value,
        ).where(BacktestRecord.id == backtest_id)
    ).first()
    if row is not None:
        current.update(zip((name for name, _ in _SCALAR_STAT_COLUMNS), row))
    resolved: dict[str, float] = {}
    for name, _default in _SCALAR_STAT_COLUMNS:
        # `default=None` makes "the producer did not name it" and "the producer declared it
        # undefined" both resolve to None, which is precisely the do-not-write case.
        value = resolve_defined_stat(stats, name, None)
        resolved[name] = current[name]
        if value is None:
            continue
        # A value that is not a finite number cannot be stored honestly: the columns are NOT NULL
        # REAL, and a stored NaN or infinity poisons every later comparison and any JSON read of
        # the row. Carrying the stored value forward is the same rule as for an absent metric, and
        # it is logged because a malformed number is a producer bug, not an absence.
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if math.isfinite(number):
            resolved[name] = number
        else:
            logger.warning(
                "Ignoring unusable %s value %r for backtest %s: not a finite number",
                name,
                value,
                backtest_id,
            )
    return resolved


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

        stored_data = dict(result.meta.data or {})
        stored_data["_backtest_config"] = {
            "risk_per_trade_pct": result.meta.risk_per_trade_pct,
            "max_position_exposure_pct": result.meta.max_position_exposure_pct,
            "max_leverage": result.meta.max_leverage,
            "modeling_resolution": result.meta.modeling_resolution,
            "signal_exit_fill": result.meta.signal_exit_fill,
            "allow_fallback_stop": result.meta.allow_fallback_stop,
            "reject_missing_stop": result.meta.reject_missing_stop,
            "require_explicit_column_roles": result.meta.require_explicit_column_roles,
        }
        scalar_stats = _scalar_stats_for_save(session, backtest_id, result.stats or {})

        backtest_record = BacktestRecord(
            id=backtest_id,
            sharpe_ratio=scalar_stats["sharpe_ratio"],
            psr=scalar_stats["psr"],
            cagr=scalar_stats["cagr"],
            p_value=scalar_stats["p_value"],
            strategies=result.meta.strategies,
            engine=result.meta.engine,
            backtest_start=result.meta.backtest_start,
            backtest_end=result.meta.backtest_end,
            data=stored_data,
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
    data = backtest.data or {}
    runtime = data.get("_backtest_config", {}) if isinstance(data, dict) else {}
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

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from sqlalchemy import JSON, Column, String, delete, event, func, text
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
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    notes: str = Field(default="")
    created_at: str = Field(default="", index=True)


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
            metadata=self.metadata_json or {},
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
    settings_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("settings", JSON, nullable=False),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    @classmethod
    def from_live_deployment(cls, deployment: LiveDeploymentConfig) -> "LiveDeploymentModel":
        now = datetime.utcnow()
        return cls(
            id=str(deployment.id),
            strategy_name=deployment.strategy_name,
            account_id=deployment.account_id,
            symbols_json=deployment.symbols,
            timeframe=deployment.timeframe,
            status=deployment.status,
            settings_json=deployment.settings or {},
            created_at=now,
            updated_at=now,
        )

    def to_live_deployment(self) -> LiveDeploymentConfig:
        return LiveDeploymentConfig(
            id=self.id,
            strategy_name=self.strategy_name,
            account_id=self.account_id,
            symbols=self.symbols_json or [],
            timeframe=self.timeframe,
            status=self.status,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        record = LiveDeploymentModel.from_live_deployment(deployment)
        record.status = deployment_status
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
            stats=result.stats or {},
            notes=result.meta.notes,
            created_at=result.meta.created_at,
        )
        session.merge(backtest_record)

        for trade in result.trades:
            exit_reason = trade.exits[-1].reason if trade.exits else None
            session.add(
                TradeRecord(
                    backtest_id=backtest_id,
                    trade_nr=trade.id,
                    symbol=trade.symbol,
                    direction=trade.direction,
                    entry_time=trade.entry_time,
                    entry_price=trade.entry_price,
                    quantity=trade.quantity,
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
    timeframe = timeframes[0] if timeframes else ""

    return {
        "id": str(backtest.id),
        "engine": str(backtest.engine or "unknown"),
        "strategy_name": strategy_name,
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

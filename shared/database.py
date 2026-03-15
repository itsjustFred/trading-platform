from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from sqlalchemy import Column, JSON, String, delete, select
from sqlmodel import Field, Session, SQLModel, create_engine

from shared.logging import get_logger
from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint as EquityPointModel,
    IndicatorSeries,
    Trade,
    TradeExit,
)

logger = get_logger("database")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = Path(_data_dir_env).resolve() if _data_dir_env else (PROJECT_ROOT / "data").resolve()
DB_PATH = DATA_DIR / "backtests.db"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


ENGINE = create_engine(_sqlite_url(DB_PATH), echo=False)


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
    exits: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
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


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(ENGINE) as session:
        yield session


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(ENGINE)


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
        rows = session.exec(select(BacktestRecord.id).order_by(BacktestRecord.created_at.desc())).all()
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

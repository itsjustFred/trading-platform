"""Tests for database migrations and model persistence."""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func
from sqlmodel import SQLModel, create_engine

from shared.database import (
    BacktestRecord,
    EquityPoint,
    TradeRecord,
    get_session,
    init_db,
    migrate_json_to_sqlite,
)
from shared.models import BacktestMeta, BacktestResult, EquityPoint as EquityPointModel, Trade, TradeExit
from sqlmodel import select


def test_backtest_record_equity_curve_persistence() -> None:
    """Test that equity_curve can be stored and retrieved from SQLite."""
    init_db()

    record_id = f"test_equity_curve_record_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    with get_session() as session:
        # Create a test record
        record = BacktestRecord(
            id=record_id,
            strategies=["test_strategy"],
            engine="native",
            equity_curve=[
                {"time": 1700000000, "equity": 100000.0},
                {"time": 1700000060, "equity": 101000.0},
            ],
        )
        session.add(record)
        session.commit()

        # Retrieve and verify
        retrieved = session.exec(select(BacktestRecord).where(BacktestRecord.id == record_id)).first()

        assert retrieved is not None
        assert retrieved.equity_curve[0]["time"] == 1700000000
        assert retrieved.equity_curve[0]["equity"] == 100000.0
        assert len(retrieved.equity_curve) == 2


def test_migrate_json_to_sqlite_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    """Legacy JSON migration should skip existing backtest IDs on later runs."""
    import shared.database as database

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'backtests.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    data_dir = tmp_path / "data"
    backtests_dir = data_dir / "backtests"
    backtests_dir.mkdir(parents=True)

    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    SQLModel.metadata.create_all(engine)

    result = BacktestResult(
        meta=BacktestMeta(
            id="legacy_json_backtest",
            created_at=datetime.now(timezone.utc).isoformat(),
            engine="native",
            strategies=["legacy_strategy"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
            data={"symbols": ["AAPL"], "timeframes": ["1d"]},
            initial_capital=100000.0,
        ),
        trades=[
            Trade(
                id=1,
                symbol="AAPL",
                direction="LONG",
                entry_time="2024-01-01T00:00:00+00:00",
                entry_price=100.0,
                quantity=1.0,
                exits=[
                    TradeExit(
                        time="2024-01-02T00:00:00+00:00",
                        price=101.0,
                        quantity=1.0,
                        pnl=1.0,
                    )
                ],
                pnl=1.0,
            )
        ],
        equity_curve=[
            EquityPointModel(time="2024-01-01T00:00:00+00:00", equity=100000.0),
            EquityPointModel(time="2024-01-02T00:00:00+00:00", equity=100001.0),
        ],
        stats={"sharpe_ratio": 1.0},
    )
    result.save(backtests_dir / "legacy_json_backtest.trades.json")

    first = migrate_json_to_sqlite()
    second = migrate_json_to_sqlite()

    assert first == {"migrated": 1, "skipped": 0, "failed": 0, "total": 1}
    assert second == {"migrated": 0, "skipped": 1, "failed": 0, "total": 1}

    with get_session() as session:
        records = session.exec(select(BacktestRecord).where(BacktestRecord.id == "legacy_json_backtest")).all()
        trade_count = session.exec(
            select(func.count(TradeRecord.id)).where(TradeRecord.backtest_id == "legacy_json_backtest")
        ).one()
        equity_count = session.exec(
            select(func.count(EquityPoint.id)).where(EquityPoint.backtest_id == "legacy_json_backtest")
        ).one()

    assert len(records) == 1
    assert trade_count == 1
    assert equity_count == 2

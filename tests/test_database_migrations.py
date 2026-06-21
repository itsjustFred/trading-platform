"""Tests for database migrations and model persistence."""
from datetime import datetime, timezone

from shared.database import init_db, get_session, BacktestRecord
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
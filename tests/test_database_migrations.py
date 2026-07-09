"""Tests for database migrations and model persistence."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, text
from sqlmodel import SQLModel, create_engine

from shared.database import (
    BacktestRecord,
    EquityPoint,
    LiveSignalModel,
    TradeRecord,
    _normalize_optional_id,
    get_session,
    init_db,
    migrate_json_to_sqlite,
    save_live_signal,
)
from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint as EquityPointModel,
    LiveSignal,
    Trade,
    TradeExit,
)
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


# ─── Shared isolation fixture ────────────────────────────────────────────────


@pytest.fixture
def isolated_db(monkeypatch, tmp_path: Path):
    """Point shared.database at a throwaway SQLite DB + data dir for one test."""
    import shared.database as database

    data_dir = tmp_path / "data"
    backtests_dir = data_dir / "backtests"
    backtests_dir.mkdir(parents=True)
    db_path = data_dir / "backtests.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    SQLModel.metadata.create_all(engine)

    return database, engine, data_dir, backtests_dir


def _make_live_signal(*, discord_message_id=None, broker_order_id=None, **extra) -> LiveSignal:
    metadata: dict = {}
    if discord_message_id is not None:
        metadata["discord_message_id"] = discord_message_id
    if broker_order_id is not None:
        metadata["broker_order_id"] = broker_order_id
    metadata.update(extra)
    return LiveSignal(
        id=uuid4(),
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


# ─── Finding 4: discord_message_id normalization ─────────────────────────────


def test_normalize_optional_id_collapses_missing_and_blank() -> None:
    assert _normalize_optional_id(None) is None
    assert _normalize_optional_id("") is None
    assert _normalize_optional_id("   ") is None
    assert _normalize_optional_id("123") == "123"
    assert _normalize_optional_id(123) == "123"
    assert _normalize_optional_id("  123  ") == "123"


def test_from_live_signal_missing_discord_id_is_real_null() -> None:
    signal = _make_live_signal()
    model = LiveSignalModel.from_live_signal(signal)
    assert model.discord_message_id is None  # not the literal string "None"


def test_two_signals_without_discord_id_both_saved(isolated_db) -> None:
    save_live_signal(_make_live_signal())
    save_live_signal(_make_live_signal())
    with get_session() as session:
        count = session.exec(select(func.count(LiveSignalModel.id))).one()
    assert count == 2


def test_empty_string_discord_id_treated_as_missing(isolated_db) -> None:
    save_live_signal(_make_live_signal(discord_message_id=""))
    save_live_signal(_make_live_signal(discord_message_id="   "))
    with get_session() as session:
        rows = session.exec(select(LiveSignalModel)).all()
        count = len(rows)
    assert count == 2
    assert all(r.discord_message_id is None for r in rows)


def test_two_signals_same_discord_id_deduped(isolated_db) -> None:
    save_live_signal(_make_live_signal(discord_message_id="discord-42"))
    save_live_signal(_make_live_signal(discord_message_id="discord-42"))
    with get_session() as session:
        count = session.exec(
            select(func.count(LiveSignalModel.id)).where(
                LiveSignalModel.discord_message_id == "discord-42"
            )
        ).one()
    assert count == 1


# ─── Finding 6: concurrent / IntegrityError race ─────────────────────────────


def test_concurrent_same_discord_id_yields_one_record(isolated_db) -> None:
    """Simulate the race: pre-check sees nothing, then commit hits the unique index."""
    database, engine, _data_dir, _bt = isolated_db

    # First writer wins normally.
    save_live_signal(_make_live_signal(discord_message_id="race-1"))

    # Force ONLY the second writer's pre-check (the first discord SELECT during
    # this save) to find nothing, so it proceeds to commit and must rely on the
    # IntegrityError guard. The later reload-by-id query must still work.
    real_exec = database.Session.exec
    state = {"precheck_done": False}

    def fake_exec(self, statement, *args, **kwargs):  # noqa: ANN001
        stmt_text = str(statement).lower()
        if (
            not state["precheck_done"]
            and "discord_message_id" in stmt_text
            and "select" in stmt_text
        ):
            state["precheck_done"] = True

            class _Empty:
                def first(self_inner):  # noqa: N805
                    return None

                def all(self_inner):  # noqa: N805
                    return []

            return _Empty()
        return real_exec(self, statement, *args, **kwargs)

    database.Session.exec = fake_exec
    try:
        # Should not raise even though the pre-check was bypassed.
        save_live_signal(_make_live_signal(discord_message_id="race-1"))
    finally:
        database.Session.exec = real_exec

    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM live_signals "
                "WHERE discord_message_id = 'race-1'"
            )
        ).scalar()
    assert count == 1


# ─── Finding 7: broker_order_id top-up on dedupe ─────────────────────────────


def test_dedupe_fills_missing_broker_order_id(isolated_db) -> None:
    save_live_signal(_make_live_signal(discord_message_id="ord-1"))
    save_live_signal(
        _make_live_signal(discord_message_id="ord-1", broker_order_id="BRK-999")
    )
    with get_session() as session:
        rows = session.exec(
            select(LiveSignalModel).where(
                LiveSignalModel.discord_message_id == "ord-1"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].broker_order_id == "BRK-999"
    assert rows[0].metadata_json.get("broker_order_id") == "BRK-999"


def test_dedupe_does_not_overwrite_existing_broker_order_id(isolated_db) -> None:
    save_live_signal(
        _make_live_signal(discord_message_id="ord-2", broker_order_id="BRK-AAA")
    )
    save_live_signal(
        _make_live_signal(discord_message_id="ord-2", broker_order_id="BRK-BBB")
    )
    with get_session() as session:
        rows = session.exec(
            select(LiveSignalModel).where(
                LiveSignalModel.discord_message_id == "ord-2"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].broker_order_id == "BRK-AAA"


# ─── Finding 5: migration backfill of discord_message_id ─────────────────────


def _legacy_signals_db(engine) -> None:
    """Create a live_signals table WITHOUT discord_message_id / unique index."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE live_signals ("
                "id TEXT PRIMARY KEY, deployment_id TEXT, symbol TEXT, action TEXT, "
                "theoretical_price FLOAT, timestamp TEXT, source TEXT DEFAULT 'internal', "
                "broker_order_id TEXT DEFAULT '', metadata TEXT)"
            )
        )


def _insert_legacy_signal(engine, *, row_id, timestamp, metadata) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                "theoretical_price, timestamp, source, broker_order_id, metadata) "
                "VALUES (:id, 'dep', 'AAPL', 'buy', 1.0, :ts, 'internal', '', :md)"
            ),
            {"id": row_id, "ts": timestamp, "md": json.dumps(metadata)},
        )


@pytest.fixture
def legacy_db(monkeypatch, tmp_path: Path):
    import shared.database as database

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "backtests.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    return database, engine, data_dir


def test_migration_backfills_discord_id_from_metadata(legacy_db) -> None:
    database, engine, _data_dir = legacy_db
    _legacy_signals_db(engine)
    _insert_legacy_signal(
        engine, row_id="s1", timestamp="2024-01-01T00:00:00", metadata={"discord_message_id": "D-1"}
    )
    _insert_legacy_signal(
        engine, row_id="s2", timestamp="2024-01-01T00:01:00", metadata={"other": "x"}
    )

    database.init_db()

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, discord_message_id FROM live_signals")).fetchall())
    assert rows["s1"] == "D-1"
    assert rows["s2"] is None  # no metadata id => stays NULL


def test_migration_handles_duplicate_discord_ids_before_index(legacy_db) -> None:
    database, engine, _data_dir = legacy_db
    _legacy_signals_db(engine)
    # Two rows share the same discord id; deterministic winner = earliest ts.
    _insert_legacy_signal(
        engine, row_id="dup_b", timestamp="2024-01-02T00:00:00", metadata={"discord_message_id": "DUP"}
    )
    _insert_legacy_signal(
        engine, row_id="dup_a", timestamp="2024-01-01T00:00:00", metadata={"discord_message_id": "DUP"}
    )

    database.init_db()  # must not raise creating the unique index

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, discord_message_id FROM live_signals")).fetchall())
        idx = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_live_signals_discord_msg_id'")
        ).first()
    assert idx is not None
    assert rows["dup_a"] == "DUP"  # earliest timestamp wins
    assert rows["dup_b"] is None


def test_migration_discord_backfill_is_idempotent(legacy_db) -> None:
    database, engine, _data_dir = legacy_db
    _legacy_signals_db(engine)
    _insert_legacy_signal(
        engine, row_id="s1", timestamp="2024-01-01T00:00:00", metadata={"discord_message_id": "D-1"}
    )

    database.init_db()
    with engine.connect() as conn:
        before = dict(conn.execute(text("SELECT id, discord_message_id FROM live_signals")).fetchall())

    # Force a second migration pass.
    database._DB_INITIALIZED = False
    database._migrate_schema()
    with engine.connect() as conn:
        after = dict(conn.execute(text("SELECT id, discord_message_id FROM live_signals")).fetchall())
    assert before == after == {"s1": "D-1"}


# ─── Finding 8: equity_curve backfill ────────────────────────────────────────


def test_equity_curve_backfill_from_equity_points(isolated_db) -> None:
    database, engine, _data_dir, _bt = isolated_db
    database.init_db()
    with get_session() as session:
        session.add(
            BacktestRecord(id="bt_pts", strategies=["s"], engine="native", equity_curve=[])
        )
        session.add(EquityPoint(backtest_id="bt_pts", time="2024-01-01", equity=100.0))
        session.add(EquityPoint(backtest_id="bt_pts", time="2024-01-02", equity=110.0))
        session.commit()

    # Re-run schema migration to trigger the equity_curve backfill.
    database._DB_INITIALIZED = False
    database._migrate_schema()

    with get_session() as session:
        record = session.get(BacktestRecord, "bt_pts")
    assert len(record.equity_curve) == 2
    assert record.equity_curve[0]["equity"] == 100.0
    assert record.equity_curve[1]["equity"] == 110.0


def test_equity_curve_backfill_from_json_file(isolated_db) -> None:
    database, engine, _data_dir, backtests_dir = isolated_db
    database.init_db()
    with get_session() as session:
        session.add(
            BacktestRecord(id="bt_json", strategies=["s"], engine="native", equity_curve=[])
        )
        session.commit()

    (backtests_dir / "bt_json.trades.json").write_text(
        json.dumps(
            {
                "meta": {"id": "bt_json"},
                "equity_curve": [
                    {"time": "2024-01-01", "equity": 200.0},
                    {"time": "2024-01-02", "equity": 250.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    database._DB_INITIALIZED = False
    database._migrate_schema()

    with get_session() as session:
        record = session.get(BacktestRecord, "bt_json")
    assert len(record.equity_curve) == 2
    assert record.equity_curve[1]["equity"] == 250.0


def test_equity_curve_backfill_does_not_overwrite_non_empty(isolated_db) -> None:
    database, engine, _data_dir, backtests_dir = isolated_db
    database.init_db()
    original = [{"time": "2024-01-01", "equity": 1.0}]
    with get_session() as session:
        session.add(
            BacktestRecord(id="bt_keep", strategies=["s"], engine="native", equity_curve=original)
        )
        # Conflicting equity_points that must NOT clobber the existing curve.
        session.add(EquityPoint(backtest_id="bt_keep", time="2099-01-01", equity=999.0))
        session.commit()

    database._DB_INITIALIZED = False
    database._migrate_schema()

    with get_session() as session:
        record = session.get(BacktestRecord, "bt_keep")
    assert record.equity_curve == original


def test_migrate_json_tops_up_empty_equity_curve_when_skipping(isolated_db) -> None:
    database, engine, _data_dir, backtests_dir = isolated_db
    database.init_db()
    # Existing row with an empty equity_curve (simulates earlier migration).
    with get_session() as session:
        session.add(
            BacktestRecord(id="topup_bt", strategies=["s"], engine="native", equity_curve=[])
        )
        session.commit()

    result = BacktestResult(
        meta=BacktestMeta(
            id="topup_bt",
            created_at=datetime.now(timezone.utc).isoformat(),
            engine="native",
            strategies=["s"],
        ),
        trades=[],
        equity_curve=[
            EquityPointModel(time="2024-01-01", equity=300.0),
            EquityPointModel(time="2024-01-02", equity=305.0),
        ],
        stats={},
    )
    result.save(backtests_dir / "topup_bt.trades.json")

    summary = migrate_json_to_sqlite(skip_existing=True)
    assert summary["migrated"] == 1  # topped up rather than plain skipped

    with get_session() as session:
        record = session.get(BacktestRecord, "topup_bt")
    assert len(record.equity_curve) == 2

    # Second run must be a no-op (curve already non-empty).
    summary2 = migrate_json_to_sqlite(skip_existing=True)
    assert summary2["migrated"] == 0
    assert summary2["skipped"] == 1

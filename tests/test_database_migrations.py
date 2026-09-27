"""Tests for database migrations and model persistence."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, text
from sqlmodel import select

from shared.database import (
    BacktestRecord,
    EquityPoint,
    LiveSignalModel,
    TradeRecord,
    _normalize_optional_id,
    get_session,
    migrate_json_to_sqlite,
    save_live_signal,
)
from shared.models import (
    BacktestMeta,
    BacktestResult,
    LiveSignal,
    Trade,
    TradeExit,
)
from shared.models import (
    EquityPoint as EquityPointModel,
)


def test_backtest_record_equity_curve_persistence(isolated_db) -> None:
    """Test that equity_curve can be stored and retrieved from SQLite."""
    database_module, _engine, _data_dir, _backtests_dir = isolated_db
    database_module.init_db()

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


def test_migrate_json_to_sqlite_is_idempotent(isolated_db) -> None:
    """Legacy JSON migration should skip existing backtest IDs on later runs."""
    _database, _engine, _data_dir, backtests_dir = isolated_db

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


# ─── Isolation fixture ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_db(isolated_db_sandbox):
    """Sandbox DB (identical patch set to tests/conftest.py::isolated_db_sandbox)
    plus a pre-created ``backtests/`` dir for this module's legacy-JSON
    backfill tests. Yields ``(database_module, engine, data_dir, backtests_dir)``.
    """
    database, engine, data_dir = isolated_db_sandbox
    backtests_dir = data_dir / "backtests"
    backtests_dir.mkdir(parents=True)
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
def legacy_db(isolated_db_no_schema):
    """No-schema sandbox (see tests/conftest.py::isolated_db_no_schema): these
    tests exercise init_db()'s migration path over a pre-existing legacy
    live_signals table. Yields ``(database_module, engine, data_dir)``."""
    database, engine, data_dir = isolated_db_no_schema
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


def test_migration_fails_closed_for_duplicate_discord_ids_within_deployment(legacy_db) -> None:
    database, engine, _data_dir = legacy_db
    _legacy_signals_db(engine)
    _insert_legacy_signal(
        engine, row_id="dup_b", timestamp="2024-01-02T00:00:00", metadata={"discord_message_id": "DUP"}
    )
    _insert_legacy_signal(
        engine, row_id="dup_a", timestamp="2024-01-01T00:00:00", metadata={"discord_message_id": "DUP"}
    )

    with pytest.raises(RuntimeError, match="No rows were deleted"):
        database.init_db()

    with engine.connect() as conn:
        columns = {
            str(row[1]) for row in conn.execute(text("PRAGMA table_info('live_signals')")).fetchall()
        }
        rows = dict(conn.execute(text("SELECT id, metadata FROM live_signals")).fetchall())
        idx = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='uq_live_signals_deployment_discord_msg'"
            )
        ).first()
        legacy_table = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='live_signals__discord_legacy'"
            )
        ).first()
    assert idx is None
    assert legacy_table is None
    assert "discord_message_id" not in columns
    assert {row_id: json.loads(metadata)["discord_message_id"] for row_id, metadata in rows.items()} == {
        "dup_a": "DUP",
        "dup_b": "DUP",
    }


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


def test_equity_curve_column_add_and_backfill_happen_in_same_migration(isolated_db) -> None:
    database, engine, _data_dir, _backtests_dir = isolated_db
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE backtests DROP COLUMN equity_curve"))
        connection.execute(
            text(
                "INSERT INTO backtests ("
                "id, strategies, engine, backtest_start, backtest_end, data, "
                "initial_capital, parameters, tags, stats, notes, created_at"
                ") VALUES ("
                ":id, :strategies, :engine, '', '', '{}', 100000, '{}', '[]', '{}', '', ''"
                ")"
            ),
            {"id": "bt_legacy", "strategies": "[]", "engine": "native"},
        )
        connection.execute(
            text(
                "INSERT INTO equity_points (backtest_id, time, equity) "
                "VALUES (:id, :time, :equity)"
            ),
            {"id": "bt_legacy", "time": "2024-01-01", "equity": 123.0},
        )

    database._migrate_schema()

    with engine.connect() as connection:
        curve = connection.execute(
            text("SELECT equity_curve FROM backtests WHERE id = :id"),
            {"id": "bt_legacy"},
        ).scalar_one()
    assert json.loads(curve) == [{"time": "2024-01-01", "equity": 123.0}]


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


def test_equity_curve_backfill_rejects_json_with_mismatched_id(isolated_db) -> None:
    database, engine, _data_dir, backtests_dir = isolated_db
    database.init_db()
    with get_session() as session:
        session.add(
            BacktestRecord(id="bt_target", strategies=["s"], engine="native", equity_curve=[])
        )
        session.commit()

    (backtests_dir / "bt_target.trades.json").write_text(
        json.dumps(
            {
                "meta": {"id": "bt_other"},
                "equity_curve": [{"time": "2024-01-01", "equity": 999.0}],
            }
        ),
        encoding="utf-8",
    )

    database._DB_INITIALIZED = False
    database._migrate_schema()

    with get_session() as session:
        record = session.get(BacktestRecord, "bt_target")
    assert record is not None
    assert record.equity_curve == []


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


# ─── One submission claim per live deployment ──────────────────────────────────
#
# `deployment_id` on live_deployment_submission_claims used to carry only a
# non-unique index, so two dispatchers could both claim the same ACTIVE
# deployment. The uniqueness is enforced by the database, which means it has to
# be migrated onto databases that already have the table. These tests build that
# legacy shape by hand — including the duplicate rows the migration has to cope
# with — and run the real `init_db()` migration path over it.


def _build_legacy_claim_table(engine) -> None:
    """Recreate the pre-constraint table: non-unique index, no UNIQUE at all."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE live_deployment_submission_claims ("
                "token VARCHAR NOT NULL PRIMARY KEY, "
                "deployment_id VARCHAR NOT NULL, "
                "claimed_at DATETIME NOT NULL, "
                "expires_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX ix_live_deployment_submission_claims_deployment_id "
                "ON live_deployment_submission_claims (deployment_id)"
            )
        )


def _index_names(engine, table: str) -> set[str]:
    with engine.begin() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"),
                {"t": table},
            ).fetchall()
        }


def _is_unique_on_deployment_id(engine) -> bool:
    import shared.database as database

    with engine.begin() as connection:
        return database._has_unique_index_on_column(
            connection, "live_deployment_submission_claims", "deployment_id"
        )


def test_migration_makes_submission_claims_unique_per_deployment(
    isolated_db_no_schema,
) -> None:
    """A legacy claim table comes out of the migration mutually exclusive.

    Runs the real `init_db()` path, so this is the migration as production runs
    it, over a table built the way the old schema built it. Never the
    production database: the engine is the sandbox one.
    """
    database, engine, _data_dir = isolated_db_no_schema
    _build_legacy_claim_table(engine)
    assert not _is_unique_on_deployment_id(engine), "precondition: no UNIQUE yet"

    database.init_db()

    assert _is_unique_on_deployment_id(engine), (
        "the migration must leave a UNIQUE on deployment_id behind"
    )
    assert "uq_live_deployment_submission_claims_deployment" in _index_names(
        engine, "live_deployment_submission_claims"
    )

    # Idempotent: a second init (every process start) changes nothing.
    database.init_db()
    assert _is_unique_on_deployment_id(engine)


def test_migration_collapses_concurrent_claims_instead_of_failing(isolated_db_no_schema) -> None:
    """Duplicate leases must not take the database down with them.

    A unique index cannot be created while two rows share a deployment, and
    refusing the migration would break every caller including the safety gates.
    These rows are leases with a TTL, so the newest is the live one and the rest
    are already superseded: keeping the newest both satisfies the index and
    leaves the deployment submittable.
    """
    database, engine, _data_dir = isolated_db_no_schema
    _build_legacy_claim_table(engine)
    now = datetime(2026, 7, 15, 12, 0, 0)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO live_deployment_submission_claims "
                "(token, deployment_id, claimed_at, expires_at) VALUES "
                "('stale-token', 'dep-a', :old, :old_plus), "
                "('live-token', 'dep-a', :new, :new_plus), "
                "('other-token', 'dep-b', :new, :new_plus)"
            ),
            {
                "old": now,
                "old_plus": now.replace(minute=1),
                "new": now.replace(minute=5),
                "new_plus": now.replace(minute=6),
            },
        )
    # 'stale-token' has the earlier expires_at, so it is the superseded lease.
    with engine.begin() as connection:
        rows = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT token FROM live_deployment_submission_claims "
                    "WHERE deployment_id = 'dep-a'"
                )
            ).fetchall()
        }
    assert rows == {"stale-token", "live-token"}, "precondition: two leases, one deployment"

    database.init_db()

    with engine.begin() as connection:
        kept = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                text(
                    "SELECT token, deployment_id "
                    "FROM live_deployment_submission_claims ORDER BY token"
                )
            ).fetchall()
        }
    assert kept == {"live-token": "dep-a", "other-token": "dep-b"}, (
        "the newest lease is kept and the superseded one is dropped; the "
        "unrelated deployment is untouched"
    )
    assert _is_unique_on_deployment_id(engine)


def test_fresh_database_already_has_claim_uniqueness(isolated_db_no_schema) -> None:
    """A database created from the models needs no migration to be exclusive.

    The invariant must hold on a brand-new install too, not only after the
    migration has run, so `create_all` has to produce the UNIQUE as well.
    """
    database, engine, _data_dir = isolated_db_no_schema
    database.init_db()
    assert _is_unique_on_deployment_id(engine)

"""The smoke-deployment backfill and the deployment_id foreign key migration.

Production had 7 of 7 signals naming a deployment that does not exist. Six carried the smoke
convention and could be resolved mechanically; the seventh was a Discord signal whose deployment was
removed by hand and carries the only in-production instance of the item 1 double-count. So the FK
lands for the six and stays blocked on exactly one row, and these tests pin both halves.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event, text

from shared.database import (
    _backfill_smoke_deployments,
    _migrate_live_signal_deployment_fk,
)

SIGNAL_COLUMNS = (
    "id, deployment_id, symbol, action, theoretical_price, timestamp, metadata, source, "
    "broker_order_id, discord_message_id, idempotency_key"
)


@pytest.fixture
def conn(tmp_path):
    """A schema shaped like production, including live_executions' FK into live_signals."""
    path = tmp_path / "probe.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        # Mirrors _schema_migration_connection: DDL runs with FK enforcement off, then
        # foreign_key_check validates once at the end. PRAGMA foreign_keys is a no-op inside a
        # transaction, so this has to be set before the BEGIN below - exactly as production does.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "CREATE TABLE live_deployments (id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, "
                "account_id TEXT NOT NULL, symbols TEXT NOT NULL, timeframe TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "settings TEXT NOT NULL DEFAULT '{}', "
                "signal_source TEXT NOT NULL DEFAULT 'internal', "
                "signal_config TEXT NOT NULL DEFAULT '{}', "
                "signal_source_type TEXT NOT NULL DEFAULT 'internal', "
                "signal_source_id TEXT NOT NULL DEFAULT '', "
                "symbol_filter TEXT NOT NULL DEFAULT '[]')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE live_accounts (id TEXT PRIMARY KEY, broker TEXT, name TEXT, "
                "ctid TEXT, is_demo INTEGER)"
            )
        )
        # Production's real DDL, including PRIMARY KEY - the rebuild must preserve it.
        connection.execute(
            text(
                "CREATE TABLE live_signals (\n"
                "\tid VARCHAR NOT NULL, \n"
                "\tdeployment_id VARCHAR NOT NULL, \n"
                "\tsymbol VARCHAR NOT NULL, \n"
                "\taction VARCHAR NOT NULL, \n"
                "\ttheoretical_price FLOAT NOT NULL, \n"
                "\ttimestamp DATETIME NOT NULL, \n"
                "\tmetadata JSON NOT NULL, source TEXT NOT NULL DEFAULT 'internal', "
                "broker_order_id TEXT NOT NULL DEFAULT '', discord_message_id TEXT, "
                "idempotency_key TEXT, \n"
                "\tPRIMARY KEY (id)\n"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE live_executions (id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, "
                "actual_price REAL, filled_qty REAL, commission REAL, timestamp DATETIME NOT NULL, "
                "broker_order_id TEXT NOT NULL DEFAULT '', dedupe_key TEXT, "
                "FOREIGN KEY (signal_id) REFERENCES live_signals (id))"
            )
        )
        connection.execute(
            text("CREATE INDEX ix_live_signals_deployment_id ON live_signals (deployment_id)")
        )
        connection.execute(text("CREATE INDEX ix_live_signals_symbol ON live_signals (symbol)"))
        connection.execute(text("BEGIN IMMEDIATE"))
        yield connection


def _add_account(conn, account_id: str) -> None:
    conn.execute(
        text("INSERT INTO live_accounts (id, broker, name) VALUES (:i, 'ctrader', :n)"),
        {"i": account_id, "n": f"cTrader {account_id}"},
    )


def _add_signal(conn, signal_id: str, deployment_id: str, symbol: str = "EURUSD") -> None:
    conn.execute(
        text(
            f"INSERT INTO live_signals ({SIGNAL_COLUMNS}) "
            "VALUES (:id, :dep, :sym, 'buy', 1.0, '2026-09-25 10:00:00', '{}', 'internal', '',"
            " NULL, NULL)"
        ),
        {"id": signal_id, "dep": deployment_id, "sym": symbol},
    )


def test_backfill_creates_the_parent_row_for_a_smoke_orphan(conn):
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")

    assert _backfill_smoke_deployments(conn) == 1

    row = conn.execute(
        text(
            "select id, strategy_name, account_id, symbols, timeframe, status, signal_source "
            "from live_deployments where strategy_name = 'ctrader-demo-smoke-44695443'"
        )
    ).fetchone()
    assert row is not None
    # The key is a UUID because LiveDeploymentConfig.id is typed uuid.UUID; the readable label
    # lives in strategy_name. A descriptive primary key stopped the daemon syncing every deployment.
    import uuid as _uuid

    _uuid.UUID(row[0])
    assert row[1] == "ctrader-demo-smoke-44695443"
    assert row[2] == "44695443"
    # Mirrors the discord:: synthetic convention: PAUSED, so no runner trades it.
    assert row[4] == "1m"
    assert row[5] == "PAUSED"
    assert row[6] == "smoke"


def test_one_parent_row_covers_every_signal_of_the_smoke_label(conn):
    """The label identifies a deployment, not a signal.

    Two signals for the same account on different symbols must produce one parent whose `symbols`
    column names both. Iterating the (deployment, symbol) pairs instead would count the deployment
    twice and record only whichever symbol came back first.
    """
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443", symbol="EURUSD")
    _add_signal(conn, "s2", "ctrader-demo-smoke-44695443", symbol="XAUUSD")

    assert _backfill_smoke_deployments(conn) == 1

    rows = conn.execute(text("select id, symbols from live_deployments")).fetchall()
    assert len(rows) == 1, rows
    assert json.loads(rows[0][1]) == ["EURUSD", "XAUUSD"], rows[0][1]
    parented = conn.execute(
        text("select count(*) from live_signals where deployment_id = :parent"),
        {"parent": rows[0][0]},
    ).scalar_one()
    assert parented == 2, "not every signal of the label was repointed at its parent"


def test_backfill_is_idempotent(conn):
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")

    assert _backfill_smoke_deployments(conn) == 1
    assert _backfill_smoke_deployments(conn) == 0
    assert conn.execute(text("select count(*) from live_deployments")).scalar() == 1


def test_backfill_never_deletes_and_leaves_unknown_accounts_alone(conn):
    """An absent account must stay an orphan: inventing one would be worse."""
    _add_signal(conn, "s1", "ctrader-demo-smoke-99999999")
    _add_signal(conn, "s2", "a-real-deployment-uuid")

    assert _backfill_smoke_deployments(conn) == 0
    assert conn.execute(text("select count(*) from live_deployments")).scalar() == 0
    assert conn.execute(text("select count(*) from live_signals")).scalar_one() == 2


def test_fk_rebuild_keeps_every_index(conn):
    """The trap: DROP TABLE removes indexes, so DDL must be captured before it.

    foreign_key_check validates referential integrity only and would never notice lost indexes.
    """
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _backfill_smoke_deployments(conn)

    before = {
        r[0]
        for r in conn.execute(
            text(
                "select name from sqlite_master where type='index' and tbl_name='live_signals' "
                "and name not like 'sqlite%'"
            )
        ).fetchall()
    }
    assert _migrate_live_signal_deployment_fk(conn) is True

    after = {
        r[0]
        for r in conn.execute(
            text(
                "select name from sqlite_master where type='index' and tbl_name='live_signals' "
                "and name not like 'sqlite%'"
            )
        ).fetchall()
    }
    assert after == before, "indexes were silently lost by the rebuild"


def test_fk_rebuild_declares_the_constraint_and_keeps_rows(conn):
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _add_signal(conn, "s2", "ctrader-demo-smoke-44695443")
    _backfill_smoke_deployments(conn)

    assert _migrate_live_signal_deployment_fk(conn) is True

    fks = conn.execute(text("pragma foreign_key_list(live_signals)")).fetchall()
    assert any((r[2], r[3], r[4]) == ("live_deployments", "deployment_id", "id") for r in fks)
    assert conn.execute(text("pragma foreign_key_check(live_signals)")).fetchall() == []
    assert conn.execute(text("select count(*) from live_signals")).scalar_one() == 2


def test_rebuild_preserves_the_primary_key(conn):
    """Rebuilding from a column-name list drops PRIMARY KEY, which breaks live_executions' FK.

    Without it, live_signals.id stops being unique and every later pragma raises "foreign key
    mismatch", leaving the database unusable. This test pins the fix: the real CREATE TABLE text is
    reused rather than reconstructed.
    """
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _backfill_smoke_deployments(conn)

    _migrate_live_signal_deployment_fk(conn)

    pk = [r[1] for r in conn.execute(text("pragma table_info(live_signals)")).fetchall() if r[5]]
    assert pk == ["id"]


def test_rebuild_leaves_the_executions_foreign_key_intact(conn):
    """live_executions.signal_id references live_signals.id, so the rebuild must not break it."""
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    conn.execute(
        text(
            "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, commission, "
            "timestamp, broker_order_id) "
            "VALUES ('e1', 's1', 1.0, 0.001, 0.0, '2026-09-25 10:00:01', 'b1')"
        )
    )
    _backfill_smoke_deployments(conn)

    _migrate_live_signal_deployment_fk(conn)

    refs = conn.execute(text("pragma foreign_key_list(live_executions)")).fetchall()
    assert any((r[2], r[3], r[4]) == ("live_signals", "signal_id", "id") for r in refs)
    # The whole-database check is the one that catches a mismatch across tables.
    assert conn.execute(text("pragma foreign_key_check")).fetchall() == []


def test_fk_migration_is_a_noop_when_already_present(conn):
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _backfill_smoke_deployments(conn)
    _migrate_live_signal_deployment_fk(conn)

    assert _migrate_live_signal_deployment_fk(conn) is False


def test_orphan_blocks_the_fk_and_the_framework_rolls_it_back(conn):
    """The real production shape: one non-smoke orphan blocks the FK, by design.

    ``_schema_migration_connection`` runs DDL with ``PRAGMA foreign_keys=OFF`` and then
    validates, so that is the path under test: the copy succeeds and ``foreign_key_check``
    reports the violation that makes the framework raise and roll back.
    """
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _add_signal(conn, "s2", "removed-by-hand-uuid")
    _backfill_smoke_deployments(conn)
    conn.commit()

    # A second engine, because PRAGMA foreign_keys is a no-op inside a transaction and SQLAlchemy
    # opens one implicitly. Setting it on connect gets it applied before any statement runs.
    strict = create_engine(f"sqlite:///{conn.engine.url.database}")

    @event.listens_for(strict, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with strict.connect() as enforced:
        assert enforced.execute(text("pragma foreign_keys")).scalar_one() == 1, (
            "enforcement is off, so this test would pass regardless of the fix"
        )
        assert _migrate_live_signal_deployment_fk(enforced) is True, (
            "the rebuild refused even though the surviving rows are all parented - this is the "
            "live_executions child-FK bug, which no test covered until 2026-10-11"
        )
        # The new FK is present and parented, which is what the old assertion was protecting.
        assert enforced.execute(text("pragma foreign_key_list(live_signals)")).fetchall(), (
            "the deployment_id FK was not created"
        )
        # A whole-database check still reports the fixture's deliberate orphan - s2's
        # `removed-by-hand-uuid` is not backfilled, and this migration does not backfill anything.
        # That is the contract: `_schema_migration_connection` runs foreign_key_check and raises,
        # rolling the rebuild back. Here we assert the violation is *that* one, not that it is
        # absent, because this fixture is deliberately shaped with an orphan in it.
        violations = enforced.execute(text("pragma foreign_key_check")).fetchall()
        assert all(v[2] == "live_deployments" for v in violations), (
            f"an unexpected violation appeared: {violations}"
        )
        # The pre-existing child FK must not be rewritten by the RENAME.
        child = enforced.execute(text("pragma foreign_key_list(live_executions)")).fetchall()
        assert child, "the live_executions -> live_signals FK was lost by the rebuild"
        assert all(row[2] == "live_signals" for row in child), (
            f"the child FK was rewritten to {[row[2] for row in child]}"
        )


def test_probe_agrees_with_the_migration_on_production_shaped_data(conn):
    """Six smoke ids collapse to five parent rows; the seventh is not smoke-shaped."""
    for account in ("44695443", "46736434", "46736452", "46736456", "47644617"):
        _add_account(conn, account)
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _add_signal(conn, "s2", "ctrader-demo-smoke-46736434")
    _add_signal(conn, "s3", "ctrader-demo-smoke-46736452")
    _add_signal(conn, "s4", "ctrader-demo-smoke-46736456")
    _add_signal(conn, "s5", "ctrader-demo-smoke-47644617")
    _add_signal(conn, "s6", "ctrader-demo-smoke-47644617")
    _add_signal(conn, "s7", "4e9bbdab-0d15-4232-91e1-cc459166b764", symbol="AAPL")

    assert _backfill_smoke_deployments(conn) == 5
    remaining = conn.execute(
        text(
            "select count(*) from live_signals where deployment_id not in "
            "(select id from live_deployments)"
        )
    ).scalar_one()
    assert remaining == 1


def test_the_backfill_repoints_the_signal_at_the_new_uuid(conn):
    """Creating the parent is not enough on its own.

    The signal names the descriptive label, which after the fix is `strategy_name` rather than
    `id`. If the parent is created with a fresh UUID and the signal is left pointing at the label,
    every signal stays orphaned - the state this function exists to leave behind - while the
    deployment exists and looks fine. So the repoint is asserted, not assumed.
    """
    _add_account(conn, "44695443")
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")
    _backfill_smoke_deployments(conn)

    row = conn.execute(
        text(
            "select s.deployment_id, d.strategy_name from live_signals s "
            "join live_deployments d on d.id = s.deployment_id where s.id = 's1'"
        )
    ).fetchone()
    assert row is not None, (
        "the signal still points at the descriptive label rather than the deployment's UUID id, "
        "so it remains an orphan even though its parent row exists"
    )
    assert row[1] == "ctrader-demo-smoke-44695443"


def test_the_backfill_adopts_a_parent_that_already_carries_the_label(conn):
    """The case that made `init_db()` raise: a parent exists, keyed by a UUID, labelled with the
    smoke name, and the signals still name the label.

    Skipping it - which an earlier version did - leaves the parent present and every signal
    orphaned. The FK rebuild then copies them, `foreign_key_check` reports the violation and
    `_schema_migration_connection` raises `foreign key violations remain`, so nothing migrates
    at all. The parent must be adopted, and no second row invented for the same label.
    """
    _add_account(conn, "44695443")
    existing_id = "8f14e45f-ceea-467f-a3b1-2c0e1c1e5f00"
    conn.execute(
        text(
            "INSERT INTO live_deployments "
            "(id, strategy_name, account_id, symbols, timeframe, status, created_at, updated_at) "
            "VALUES (:id, 'ctrader-demo-smoke-44695443', '44695443', '[\"EURUSD\"]', '1m', "
            "'PAUSED', '', '')"
        ),
        {"id": existing_id},
    )
    _add_signal(conn, "s1", "ctrader-demo-smoke-44695443")

    assert _backfill_smoke_deployments(conn) == 1

    rows = conn.execute(
        text("select deployment_id from live_signals where id = 's1'")
    ).fetchall()
    assert [row[0] for row in rows] == [existing_id], (
        "the orphan was not repointed at the deployment that already carried its label"
    )
    assert conn.execute(text("select count(*) from live_deployments")).scalar() == 1, (
        "a second deployment row was invented for a label that already had one"
    )
    # And with the orphan resolved, the rebuild has nothing to object to.
    assert _migrate_live_signal_deployment_fk(conn) is True
    assert conn.execute(text("pragma foreign_key_check")).fetchall() == []

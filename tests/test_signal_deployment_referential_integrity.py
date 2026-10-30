"""The deployment_id foreign key: why it exists, and what adding it costs.

Measured facts held in place here, because together they are the whole argument on both sides.

1. **The FKs that do exist are enforced.** `shared/database.py` applies `PRAGMA foreign_keys=ON`
   from a connect-listener, so every app connection has them on. A plain `sqlite3.connect` reports
   **off**, which is why the pragma is set rather than assumed.

2. **`live_signals.deployment_id` names a deployment.** Measured on production: 7 of 7 signals
   named an absent deployment, and every signal listing is deployment-scoped, so all 7 - and the 3
   executions on them - were invisible. The column therefore carries a foreign key, both in the
   model (so a schema built by `create_all` has it) and through a migration (because SQLite cannot
   add a foreign key to an existing table).

3. **The smoke tooling creates orphans on purpose.** `scripts/live_smoke.py` used to write a
   signal naming `ctrader-demo-smoke-<account>` and insert no `live_deployments` row at all. It now
   creates the parent first, and keys it on `strategy_name` with a UUID id.

4. **Nothing in the app deletes a deployment**, so the seventh orphan - `4e9bbdab`, a real
   deployment that is simply gone - was not made by application code removing a row.

**So adding the FK has a cost**: every writer of a signal must first write its parent. That is not
an argument against the constraint - a signal naming a nonexistent deployment is a defect, and
these seven are why they were invisible - but it is a cost to weigh, and it is invisible unless
someone writes it down at the moment somebody adds the constraint.

The migration path is what this file exercises: a database that predates the declaration still has
orphans in it, and `init_db()` has to resolve what it can and refuse rather than half-apply.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

REPO = Path(__file__).resolve().parents[1]
DATABASE = REPO / "shared" / "database.py"
SMOKE = REPO / "scripts" / "live_smoke.py"

SMOKE_LABEL = "ctrader-demo-smoke-{account_id}"
DELETED_REAL_DEPLOYMENT = "4e9bbdab-0d15-4232-91e1-cc459166b764"


def test_foreign_keys_are_enabled_for_every_app_connection() -> None:
    """The premise: the declared FKs are real, so a missing one is a gap and not a convention."""
    text = DATABASE.read_text(encoding="utf-8")
    assert "PRAGMA foreign_keys=ON" in text, (
        "the connect-listener no longer enables foreign keys, so every declared FK is decorative"
    )


def test_live_signals_declares_the_deployment_foreign_key() -> None:
    """The gap, closed.

    This file previously asserted the opposite, deliberately: adding the FK had a measured cost in
    the smoke tooling, and the tripwire was there so nobody could add the constraint without seeing
    it. The cost is now paid - `live_smoke._ensure_ctrader_smoke_deployment` writes the parent row
    before the signal - so the assertion is inverted rather than deleted.
    """
    match = re.search(
        r"class LiveSignalModel\(SQLModel, table=True\):(.*?)\n\s*@classmethod",
        DATABASE.read_text(),
        re.S,
    )
    block = match.group(1) if match else ""
    assert block, "could not locate LiveSignalModel in shared/database.py"
    assert 'foreign_key="live_deployments.id"' in block, (
        "live_signals no longer declares a foreign key on deployment_id, so a schema built by "
        "create_all accepts a signal naming a deployment that does not exist"
    )


def test_the_smoke_tooling_creates_the_parent_before_the_signal() -> None:
    """The cost of the FK, paid: the smoke path inserts the deployment first.

    It originally asserted that ``LiveDeploymentModel(`` appears nowhere in the smoke tooling, so
    that nobody would add the FK without seeing the breakage. The tripwire fired and the helper was
    added; what matters now is that the order is right and that the row is keyed on a UUID.
    """
    text = SMOKE.read_text(encoding="utf-8")
    assert SMOKE_LABEL in text, (
        "the smoke tooling no longer names a synthetic deployment label, so re-measure this file"
    )
    assert "LiveDeploymentModel(" in text, (
        "the smoke tooling stopped inserting a deployment row, so the FK on "
        "live_signals.deployment_id would break every smoke run"
    )
    ensure_call = "smoke_deployment_id = _ensure_ctrader_smoke_deployment("
    signal_construction = "signal = LiveSignal(\n        deployment_id=smoke_deployment_id,"
    assert ensure_call in text, (
        "the smoke signal no longer takes the id of the deployment row that was just ensured"
    )
    assert signal_construction in text, (
        "the smoke signal no longer takes the id of the deployment row that was just ensured"
    )
    assert text.index(ensure_call) < text.index(signal_construction), (
        "the deployment must be ensured before the signal that depends on it"
    )
    # The label is a strategy_name, never a primary key: live_deployments.id is typed uuid.UUID,
    # so a descriptive id stopped the daemon syncing every deployment including the armed ones.
    assert "strategy_name=strategy_name" in text
    assert "id=strategy_name" not in text, (
        "the smoke deployment went back to using the descriptive label as its primary key, which "
        "is not a UUID and will stop the daemon syncing every deployment again"
    )


def test_the_smoke_helper_creates_one_paused_parent_per_account(isolated_db_sandbox) -> None:
    """The behaviour behind the source-shape assertions above.

    A grep can see that the helper exists and that it is called before the signal, but not that it
    is idempotent or that the row it writes is inert. Both matter: a second smoke run must not
    create a second deployment for the same account, and a deployment a runner could pick up is a
    trade nobody asked for.
    """
    from uuid import UUID

    from sqlmodel import select

    database, _engine, _data_dir = isolated_db_sandbox
    from scripts.live_smoke import _ensure_ctrader_smoke_deployment

    with database.get_session() as session:
        session.add(database.LiveAccountModel(id="44695443", broker="ctrader", name="smoke"))
        session.commit()

    first = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="xauusd")
    second = _ensure_ctrader_smoke_deployment(account_id="44695443", symbol="xauusd")

    assert first == second, "a second smoke run created a second deployment for the same account"
    with database.get_session() as session:
        rows = session.exec(select(database.LiveDeploymentModel)).all()
    assert len(rows) == 1, f"expected one deployment row, got {len(rows)}"
    row = rows[0]
    assert row.strategy_name == "ctrader-demo-smoke-44695443"
    assert row.status == "PAUSED", "a runner could trade a deployment the smoke run created"
    assert row.signal_source == "smoke"
    assert row.symbols_json == ["XAUUSD"], row.symbols_json
    assert str(UUID(str(row.id))) == str(row.id), "the id is not a UUID"


def test_no_code_path_deletes_a_deployment() -> None:
    """Why the seventh orphan is a different story from the other six."""
    for path in (REPO / "shared").rglob("*.py"):
        if "tests" in path.parts:
            continue
        body = path.read_text(errors="replace")
        assert "delete(LiveDeploymentModel)" not in body, (
            f"{path} can now delete a deployment row, so the orphans have a second cause and this "
            f"file's account of them is incomplete"
        )


def test_the_two_orphan_stories_are_recorded_separately() -> None:
    """Six from the smoke tooling, one removed by hand. Collapsing them loses the cause."""
    assert SMOKE_LABEL.count("smoke") == 1
    assert DELETED_REAL_DEPLOYMENT.startswith("4e9bbdab"), (
        "the deleted real deployment id must stay recorded; it is the only orphan whose cause is "
        "not the smoke tooling"
    )


# ─── The migration path, on a database that predates the declaration ─────────────────────────────


def _strip_deployment_fk(engine) -> None:
    """Rebuild `live_signals` without the FK, reproducing a pre-migration database.

    Necessary because the model now declares the constraint, so `create_all` emits it and a fresh
    sandbox can never hold an orphan. The shape under test is the one every existing database has.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='live_signals'"
        ).scalar_one()
        without_fk, replacements = re.subn(
            r",?\s*FOREIGN\s+KEY\s*\(deployment_id\)\s+REFERENCES\s+live_deployments\s*\(id\)",
            "",
            create_sql,
        )
        assert replacements == 1, (
            "the model's live_signals DDL no longer carries exactly one deployment_id foreign key, "
            "so this fixture cannot stage the pre-migration shape"
        )
        connection.exec_driver_sql("ALTER TABLE live_signals RENAME TO live_signals__legacy")
        connection.exec_driver_sql(without_fk)
        connection.exec_driver_sql("INSERT INTO live_signals SELECT * FROM live_signals__legacy")
        connection.exec_driver_sql("DROP TABLE live_signals__legacy")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        connection.commit()


def _seed_smoke_parent(engine, *, account_id: str, parent_id: str | None) -> str | None:
    """An account, optionally a deployment labelled like a smoke one, and an orphan signal."""
    label = SMOKE_LABEL.format(account_id=account_id)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO live_accounts (id, broker, name) "
            f"VALUES ('{account_id}', 'ctrader', '{account_id}')"
        )
        if parent_id is not None:
            connection.exec_driver_sql(
                "INSERT INTO live_deployments "
                "(id, strategy_name, account_id, symbols, timeframe, status, created_at,"
                " updated_at)"
                f" VALUES ('{parent_id}', '{label}', '{account_id}', '[]', '1m', 'PAUSED', '', '')"
            )
        connection.exec_driver_sql(
            "INSERT INTO live_signals "
            "(id, deployment_id, symbol, action, theoretical_price, timestamp, metadata, source)"
            f" VALUES ('sig-1', '{label}', 'EURUSD', 'buy', 1.0, '2026-09-25 10:00:00', '{{}}', "
            "'smoke')"
        )
        connection.commit()
    return parent_id


def _state(engine) -> tuple[str, int, list, list]:
    with engine.connect() as connection:
        deployment_id = connection.exec_driver_sql(
            "SELECT deployment_id FROM live_signals WHERE id = 'sig-1'"
        ).scalar_one()
        deployments = connection.exec_driver_sql(
            "SELECT count(*) FROM live_deployments"
        ).scalar_one()
        keys = connection.exec_driver_sql("PRAGMA foreign_key_list(live_signals)").fetchall()
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    return deployment_id, deployments, keys, violations


def _schema_fingerprint(engine) -> list[tuple[str, str, str]]:
    """Every schema object, plus the declared keys - the cheapest honest "did anything change"."""
    with engine.connect() as connection:
        objects = connection.exec_driver_sql(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        keys = connection.exec_driver_sql("PRAGMA foreign_key_list(live_signals)").fetchall()
    return [tuple(row) for row in objects] + [("fk", str(row), "") for row in keys]


def test_init_db_is_idempotent_on_a_fresh_database(isolated_db_sandbox) -> None:
    """A second start must not change the schema.

    `init_db()` runs on every process start, and `create_all` plus the migration both have to be
    no-ops the second time - including the table rebuild, which must not run again just because the
    first pass rewrote `sqlite_master`.
    """
    database, engine, _data_dir = isolated_db_sandbox
    database.reset_initialized()
    database.init_db()
    first = _schema_fingerprint(engine)

    database.reset_initialized()
    database.init_db()

    assert _schema_fingerprint(engine) == first, "the second init_db() changed the schema"
    assert any(row[2] == "live_deployments" for row in _state_fks(engine)), (
        "a fresh database has no deployment_id foreign key, so the model declaration is not "
        "reaching create_all"
    )


def _state_fks(engine) -> list:
    with engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA foreign_key_list(live_signals)").fetchall()


def test_init_db_adopts_an_existing_smoke_parent_instead_of_failing(isolated_db_sandbox) -> None:
    """Blocker: a parent with the same `strategy_name` must not block `init_db()`.

    The parent's key is a UUID and the label lives in `strategy_name`, so a signal naming the label
    is an orphan *even though its deployment exists*. Skipping that case - which an earlier version
    did - leaves the signals orphaned and the parent present, and the migration then fails its
    `foreign_key_check`, so `init_db()` raises `foreign key violations remain` and nothing migrates.

    The signal must end up pointing at the deployment that already exists, and no second row may be
    invented for it.
    """
    database, engine, _data_dir = isolated_db_sandbox
    _strip_deployment_fk(engine)
    parent_id = str(uuid4())
    _seed_smoke_parent(engine, account_id="acct-1", parent_id=parent_id)

    database.init_db()  # must not raise

    deployment_id, deployments, keys, violations = _state(engine)
    assert deployment_id == parent_id, (
        "the orphan signal was not repointed at the deployment that already carried its label"
    )
    assert deployments == 1, "a second deployment row was invented for an existing parent"
    assert any(row[2] == "live_deployments" for row in keys), "the foreign key was not created"
    assert violations == [], f"init_db() left foreign key violations behind: {violations}"


def test_init_db_creates_the_parent_when_none_exists(isolated_db_sandbox) -> None:
    """The other half: no parent at all, so one is created and the signal is repointed."""
    database, engine, _data_dir = isolated_db_sandbox
    _strip_deployment_fk(engine)
    _seed_smoke_parent(engine, account_id="acct-2", parent_id=None)

    database.init_db()

    deployment_id, deployments, keys, violations = _state(engine)
    assert deployments == 1
    assert deployment_id != SMOKE_LABEL.format(account_id="acct-2"), (
        "the signal still names the descriptive label rather than the deployment's UUID id"
    )
    with engine.connect() as connection:
        label = connection.exec_driver_sql(
            "SELECT strategy_name FROM live_deployments WHERE id = :id", {"id": deployment_id}
        ).scalar_one()
        assert label == SMOKE_LABEL.format(account_id="acct-2"), (
            "the readable label must survive in strategy_name"
        )
    assert any(row[2] == "live_deployments" for row in keys)
    assert violations == []


def test_init_db_is_idempotent_on_a_migrated_database(isolated_db_sandbox) -> None:
    """A second start must be a no-op: `init_db()` runs on every process start."""
    database, engine, _data_dir = isolated_db_sandbox
    _strip_deployment_fk(engine)
    _seed_smoke_parent(engine, account_id="acct-3", parent_id=None)

    database.init_db()
    before = _state(engine)

    database.reset_initialized()
    database.init_db()
    after = _state(engine)

    assert after == before, "the second init_db() changed the schema or the rows"
    assert after[3] == [], f"the second init_db() left foreign key violations: {after[3]}"


def test_init_db_still_refuses_while_a_non_smoke_orphan_remains(isolated_db_sandbox) -> None:
    """Fail-closed, unchanged. The FK cannot land over a row that names a deployment nobody has.

    The refusal is what makes it safe to attempt the migration on a database with real orphans: it
    either lands or changes nothing, and `_schema_migration_connection` rolls the whole thing back.
    """
    database, engine, _data_dir = isolated_db_sandbox
    _strip_deployment_fk(engine)
    _seed_smoke_parent(engine, account_id="acct-4", parent_id=None)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO live_signals "
            "(id, deployment_id, symbol, action, theoretical_price, timestamp, metadata) "
            f"VALUES ('sig-orphan', '{DELETED_REAL_DEPLOYMENT}', 'AAPL', 'buy', 170.0, "
            "'2026-04-10 15:16:17', '{}')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="foreign key violations remain"):
        database.init_db()

    # Nothing half-applied: the key did not land and the rows survived.
    with engine.connect() as connection:
        keys = connection.exec_driver_sql("PRAGMA foreign_key_list(live_signals)").fetchall()
        signals = connection.exec_driver_sql("SELECT count(*) FROM live_signals").scalar_one()
    assert keys == [], "the key landed despite the orphan, so the migration is not failing closed"
    assert signals == 2, "the refused migration dropped rows"


def test_the_fk_migration_is_actually_wired_into_the_schema_migration() -> None:
    """`_migrate_schema` must call it, and the backfill must run before it.

    Asserted by *source*, deliberately: the alternative - running the whole schema migration and
    checking `PRAGMA foreign_key_list` - would be a far heavier test, and would still be satisfied
    by a call inside a branch that never executes. This is the one place in this file where reading
    source is the honest instrument, and it says so.

    The order matters: the backfill parents the smoke orphans, so the rebuild must not copy rows
    that violate the constraint it is introducing.
    """
    from shared.database import _migrate_schema

    source = inspect.getsource(_migrate_schema)
    assert "_migrate_live_signal_deployment_fk(connection)" in source, (
        "_migrate_schema no longer calls the FK migration, so the constraint only exists on "
        "databases built from the model"
    )
    assert "_backfill_smoke_deployments(connection)" in source, (
        "_migrate_schema no longer backfills smoke deployments, so smoke-sourced orphans block the "
        "foreign key forever"
    )
    assert source.index("_backfill_smoke_deployments(connection)") < source.index(
        "_migrate_live_signal_deployment_fk(connection)"
    ), "the backfill must run before the rebuild that depends on it"


class TestTheRebuildSwapsCorrectlyDespiteTheChildForeignKey:
    """The rebuild must survive `live_executions.signal_id -> live_signals.id`.

    This is not hypothetical and it is not about orphans. `live_executions` already carries a
    foreign key into `live_signals`, which this migration does not own and must not drop. With
    enforcement on, `DROP TABLE live_signals` is refused *even when no dependent row exists*,
    because the surviving executions point at rows inside the table being dropped - so the rebuild
    failed at the DROP on a production clone whose orphan rows had already been removed.

    The fix is `PRAGMA foreign_keys=OFF` around the swap, set **before any statement the migration
    issues**. SQLite ignores that pragma while a transaction is open, so setting it immediately
    before the DROP, inside the migration, was silently a no-op.
    """

    @staticmethod
    def _db(tmp_path) -> Path:
        path = tmp_path / "swap.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE live_deployments (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE live_signals (id TEXT PRIMARY KEY, deployment_id TEXT)")
        conn.execute(
            "CREATE TABLE live_executions (id TEXT PRIMARY KEY, signal_id TEXT REFERENCES "
            "live_signals(id))"
        )
        conn.execute("INSERT INTO live_deployments VALUES ('dep-1')")
        conn.execute("INSERT INTO live_signals VALUES ('sig-1', 'dep-1')")
        # A row that points into the table the rebuild will drop. This is what blocks it.
        conn.execute("INSERT INTO live_executions VALUES ('exe-1', 'sig-1')")
        conn.commit()
        conn.close()
        return path

    def test_the_rebuild_lands_while_a_child_foreign_key_exists(self, tmp_path) -> None:
        from sqlalchemy import create_engine, text

        from shared.database import _migrate_live_signal_deployment_fk

        path = self._db(tmp_path)
        engine = create_engine(f"sqlite:///{path.as_posix()}")
        try:
            with engine.begin() as conn:
                # Enforcement must be ON. SQLAlchemy leaves `PRAGMA foreign_keys` at SQLite's
                # default of OFF, so without this the fixture never reproduces the production
                # failure and the test passes whatever the migration does.
                conn.execute(text("PRAGMA foreign_keys=ON"))
                assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1, (
                    "foreign key enforcement is off, so this test would pass regardless of the fix"
                )
                ran = _migrate_live_signal_deployment_fk(conn)
            assert ran is True, "the rebuild refused to run"
        finally:
            engine.dispose()

        check = sqlite3.connect(path)
        try:
            new_fk = check.execute("PRAGMA foreign_key_list(live_signals)").fetchall()
            assert len(new_fk) == 1, f"expected the new deployment_id FK, found {len(new_fk)}"
            child_fk = check.execute("PRAGMA foreign_key_list(live_executions)").fetchall()
            assert len(child_fk) == 1, "the pre-existing child FK was lost by the rebuild"
            assert child_fk[0][2] == "live_signals", (
                f"the child FK was rewritten to {child_fk[0][2]!r}; legacy_alter_table exists to "
                f"prevent exactly that"
            )
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
            assert check.execute("SELECT count(*) FROM live_signals").fetchone()[0] == 1
            assert check.execute("SELECT count(*) FROM live_executions").fetchone()[0] == 1
        finally:
            check.close()

    def test_the_swap_works_again_after_the_key_was_lost(self, tmp_path) -> None:
        """A second rebuild must not collide with its own leftovers or a quoted table name.

        Two ways this broke: the table name was rewritten with a plain `str.replace` of the
        unquoted spelling, and SQLite stores the name quoted once a rebuild has run; and
        `live_signals__fk_new` was left behind if the copy failed. Both are cheap to get wrong and
        invisible until the key has to be re-applied to a database that already had it.
        """
        from sqlalchemy import create_engine, text

        from shared.database import _migrate_live_signal_deployment_fk

        path = self._db(tmp_path)
        engine = create_engine(f"sqlite:///{path.as_posix()}")
        try:
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                assert _migrate_live_signal_deployment_fk(conn) is True
                # Lose the key again, as an interrupted restore would. `legacy_alter_table` is on
                # so the rename does not rewrite live_executions' FK onto the scratch name - which
                # is exactly the trap `_migrate_live_signal_deployment_fk` guards against.
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("PRAGMA legacy_alter_table=ON"))
                conn.execute(text("ALTER TABLE live_signals RENAME TO live_signals__keep"))
                conn.execute(
                    text(
                        "CREATE TABLE live_signals (id TEXT PRIMARY KEY, deployment_id TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO live_signals SELECT id, deployment_id"
                        " FROM live_signals__keep"
                    )
                )
                conn.execute(text("DROP TABLE live_signals__keep"))
                conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                assert _migrate_live_signal_deployment_fk(conn) is True, (
                    "the rebuild could not run a second time, so a re-applied key is unreachable"
                )
        finally:
            engine.dispose()

        check = sqlite3.connect(path)
        try:
            assert len(check.execute("PRAGMA foreign_key_list(live_signals)").fetchall()) == 1
            leftovers = check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='live_signals__fk_new'"
            ).fetchall()
            assert leftovers == [], "the rebuild left its scratch table behind"
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            check.close()

    def test_the_rebuild_handles_a_table_whose_name_is_stored_quoted(self, tmp_path) -> None:
        """SQLite stores the name quoted after a rename, and a plain `str.replace` misses it.

        Staged directly - a rename rewrites the stored `CREATE TABLE` text as
        ``CREATE TABLE "live_signals" (...)` - because the re-run test above rebuilds from an
        unquoted table and so never reaches this branch.
        """
        from sqlalchemy import create_engine

        from shared.database import _migrate_live_signal_deployment_fk

        path = self._db(tmp_path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE live_signals RENAME TO live_signals_old")
            conn.execute("ALTER TABLE live_signals_old RENAME TO live_signals")
            stored = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='live_signals'"
            ).fetchone()[0]
            assert stored.startswith('CREATE TABLE "live_signals"'), (
                f"could not stage a quoted table name, got {stored!r}"
            )
            conn.commit()
        finally:
            conn.close()

        engine = create_engine(f"sqlite:///{path.as_posix()}")
        try:
            with engine.begin() as connection:
                assert _migrate_live_signal_deployment_fk(connection) is True, (
                    "the rebuild could not address a quoted table name, so a database whose name "
                    "was ever rewritten by a rename can never regain the key"
                )
        finally:
            engine.dispose()

        check = sqlite3.connect(path)
        try:
            assert len(check.execute("PRAGMA foreign_key_list(live_signals)").fetchall()) == 1
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            check.close()

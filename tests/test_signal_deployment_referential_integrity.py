"""Why live_signals has no foreign key on deployment_id, and what adding one costs.

Measured facts held in place here, because together they are the whole argument on both sides.

1. **The FKs that do exist are enforced.** `shared/database.py` has a single `create_engine`
   site and applies `PRAGMA foreign_keys=ON` from a connect-listener, so every app
   connection has them on. A plain `sqlite3.connect` and a bare SQLAlchemy engine both
   report **off**, which is why the pragma is set rather than assumed.

2. **`live_signals.deployment_id` has no FK**, unlike `live_executions.signal_id` and
   `live_deployments.account_id`. Measured: 7 of 7 signals name an absent deployment, and
   every signal listing is deployment-scoped, so all 7 - and the 3 executions on them -
   are invisible. See `test_orphaned_live_records.py`.

3. **Nothing in the app deletes a deployment.** No `delete(LiveDeploymentModel)` exists, so
   the orphans were not made by application code removing a row.

4. **The smoke tooling creates them on purpose.** `scripts/live_smoke.py` builds a
   `LiveSignal` with `deployment_id=f"ctrader-demo-smoke-{account_id}"` and inserts no
   `live_deployments` row at all. Five smoke ids account for 6 of the 7 orphans; the
   seventh, `4e9bbdab`, was a real deployment and is now absent - removed outside the
   app, since no code path can do it.

**So adding the FK has a measured cost: it would break the smoke tooling**, because
every smoke signal write would fail on a missing parent. That is not an argument against
the constraint - a signal naming a nonexistent deployment is a defect, and these seven
are why they are invisible - but it is a cost to weigh, and it is invisible unless
someone writes it down at the moment somebody adds the constraint.

If the FK is ever added, this file should fail and be updated in the same commit, because
the smoke path must then insert a deployment row.
"""

from __future__ import annotations

import sqlite3

import re

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATABASE = REPO / "shared" / "database.py"
SMOKE = REPO / "scripts" / "live_smoke.py"

SMOKE_ID = "ctrader-demo-smoke-{account_id}"
DELETED_REAL_DEPLOYMENT = "4e9bbdab-0d15-4232-91e1-cc459166b764"


def test_foreign_keys_are_enabled_for_every_app_connection() -> None:
    """The premise: the existing FKs are real, so the missing one is a gap not a convention."""
    text = DATABASE.read_text(encoding="utf-8")
    assert text.count("create_engine(") == 1, (
        "there is now more than one engine creation site; a new one without the "
        "foreign_keys listener would have referential integrity silently disabled"
    )
    assert "PRAGMA foreign_keys=ON" in text, (
        "the connect-listener no longer enables foreign keys, so every declared FK "
        "is decorative"
    )


def test_live_signals_still_declares_no_foreign_key_on_deployment_id() -> None:
    """The gap, asserted. Adding the FK should fail here on purpose, in its own commit."""
    match = re.search(r"LiveSignalModel\((.*?)\n\s*\)", DATABASE.read_text(), re.S)
    block = match.group(1) if match else ""
    assert "ForeignKey" not in block, (
        "live_signals now declares a ForeignKey on deployment_id. Presumably deliberate - "
        "but the smoke tooling must then insert a deployment row, and the orphan inventory "
        "in test_orphaned_live_records.py has to be retired in the same change."
    )


def test_the_smoke_tooling_no_longer_writes_an_unresolvable_deployment_id() -> None:
    """The cost of the FK, retired.

    This file originally asserted the opposite: that ``LiveDeploymentModel(`` appears nowhere in
    the smoke tooling, so that nobody would add the FK without seeing the breakage. The tripwire has
    fired - ``live_smoke._ensure_ctrader_smoke_deployment`` now inserts the parent deployment row
    before writing the signal, so the FK no longer breaks smoke runs. The cost is gone, and the
    remaining question is no longer "is this safe" but "when".
    """
    text = SMOKE.read_text(encoding="utf-8")
    assert SMOKE_ID in text, (
        "the smoke tooling no longer names a synthetic deployment id, so re-measure this file"
    )
    assert "LiveDeploymentModel(" in text, (
        "the smoke tooling stopped inserting a deployment row, so the FK on "
        "live_signals.deployment_id would break it again"
    )
    ensure_call = "_ensure_ctrader_smoke_deployment("
    signal_construction = 'signal = LiveSignal(\n        deployment_id=smoke_deployment_id'
    assert ensure_call in text
    assert text.index("smoke_deployment_id = " + ensure_call) < text.index(signal_construction), (
        "the deployment must be ensured before the signal that depends on it"
    )
    # The label is a strategy_name, never a primary key: live_deployments.id is typed uuid.UUID, so a
    # descriptive id stopped the daemon syncing every deployment including the armed ones.
    assert 'strategy_name=strategy_name' in text or "strategy_name" in text
    assert 'id=strategy_name' not in text, (
        "the smoke deployment went back to using the descriptive label as its primary key, which is not "
        "a UUID and will stop the daemon syncing every deployment again"
    )


def test_no_code_path_deletes_a_deployment() -> None:
    """Why the seventh orphan is a different story from the other six."""
    for path in (REPO / "shared").rglob("*.py"):
        if "tests" in path.parts:
            continue
        body = path.read_text(errors="replace")
        assert "delete(LiveDeploymentModel)" not in body, (
            f"{path} can now delete a deployment row, so the orphans have a second "
            f"cause and this file's account of them is incomplete"
        )


def test_the_two_orphan_stories_are_recorded_separately() -> None:
    """Six from the smoke tooling, one removed by hand. Collapsing them loses the cause."""
    assert SMOKE_ID.count("smoke") == 1
    assert DELETED_REAL_DEPLOYMENT.startswith("4e9bbdab"), (
        "the deleted real deployment id must stay recorded; it is the only orphan whose "
        "cause is not the smoke tooling"
    )

def _schema_without_the_key(monkeypatch, tmp_path: Path, name: str):
    """Build a real schema with the deployment key deliberately NOT applied.

    The key is gated off during `init_db()` and can be released afterwards. Without the gate these
    tests only describe the current wiring: with the key wired the orphan cannot be inserted at all,
    and with it unwired `init_db()` silently skips the migration. They are about the migration's
    behaviour during the enablement window, so they set that up explicitly instead of inheriting
    whatever the tree happens to do.
    """
    import shared.database as database

    engine = database.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    real_migration = database._migrate_live_signal_deployment_fk
    gate = {"released": False}

    def _gated(connection):
        return real_migration(connection) if gate["released"] else False

    monkeypatch.setattr(database, "_migrate_live_signal_deployment_fk", _gated)
    database.init_db()
    return database, engine, gate, real_migration


def _insert_orphan_signal(database, engine, deployment_id: str = "no-such-deployment") -> None:
    with engine.begin() as connection:
        connection.execute(
            database.text(
                "INSERT INTO live_signals (id, deployment_id, symbol, action, theoretical_price,"
                " timestamp, source, metadata) VALUES"
                " ('sig-orphan', :deployment_id, 'AAPL', 'buy', 170.0,"
                " '2026-04-10 15:16:17', 'discord', '{}')"
            ),
            {"deployment_id": deployment_id},
        )


def _declared_keys(database, engine):
    with engine.connect() as connection:
        return [
            row[2]
            for row in connection.execute(
                database.text("PRAGMA foreign_key_list(live_signals)")
            ).fetchall()
        ]


def test_the_migration_refuses_on_an_orphan_and_leaves_the_schema_alone(monkeypatch, tmp_path) -> None:
    """The behaviour production depends on, pinned.

    `_schema_migration_connection` runs `PRAGMA foreign_key_check` after the migration body and raises
    if anything still violates, rolling back. That refusal is the only reason the key can be enabled
    against a database that has real orphans in it - without it, enabling would either drop rows or
    leave signals pointing at nothing.
    """
    database, engine, gate, _real = _schema_without_the_key(monkeypatch, tmp_path, "orphan.db")
    _insert_orphan_signal(database, engine)

    gate["released"] = True
    with pytest.raises(RuntimeError, match="foreign key violations remain"):
        with database._schema_migration_connection() as connection:
            database._migrate_live_signal_deployment_fk(connection)

    # Nothing half-applied: the rebuild rolled back, so no key landed and the row is intact.
    assert _declared_keys(database, engine) == [], (
        "the key landed despite the orphan, so the migration is not failing closed"
    )
    with engine.connect() as connection:
        survived = connection.execute(
            database.text("SELECT COUNT(*) FROM live_signals WHERE id = 'sig-orphan'")
        ).scalar()
    assert survived == 1, "the orphaned signal was dropped by the refused migration"


def test_the_key_lands_when_no_orphan_remains(monkeypatch, tmp_path) -> None:
    """The enablement path itself: clean schema, migration applies, orphan inserts are rejected."""
    database, engine, gate, _real = _schema_without_the_key(monkeypatch, tmp_path, "clean.db")

    gate["released"] = True
    with database._schema_migration_connection() as connection:
        assert database._migrate_live_signal_deployment_fk(connection), (
            "the migration reported it had nothing to do on a schema without the key"
        )

    assert _declared_keys(database, engine) == ["live_deployments"]

    with pytest.raises(Exception) as caught:
        _insert_orphan_signal(database, engine)
    assert "FOREIGN KEY" in str(caught.value).upper(), (
        f"the key did not reject the orphan insert: {caught.value}"
    )


def test_the_orphan_block_is_transitional_not_permanent(monkeypatch, tmp_path) -> None:
    """Corrects an earlier claim in this repository.

    It was recorded here that a single orphan would keep blocking unrelated schema migrations
    indefinitely. That is wrong, and wrong in a way that matters: once the key is applied an orphaned
    `deployment_id` cannot be written, so the orphan cannot recur and cannot go on blocking anything.
    The orphan has to be resolved once, before the key lands, and after that the window closes.

    What *can* still break the guarantee is deleting a parent deployment that signals reference, which
    is why `test_no_code_path_deletes_a_deployment` in this file matters more once the key is live.
    """
    database, engine, gate, _real = _schema_without_the_key(monkeypatch, tmp_path, "transitional.db")
    _insert_orphan_signal(database, engine)
    gate["released"] = True

    def _apply():
        with database._schema_migration_connection() as connection:
            return database._migrate_live_signal_deployment_fk(connection)

    with pytest.raises(RuntimeError, match="foreign key violations remain"):
        _apply()

    # Resolve the orphan, then the very same call runs clean.
    with engine.begin() as connection:
        connection.execute(database.text("DELETE FROM live_signals WHERE id = 'sig-orphan'"))

    assert _apply(), "after the orphan was resolved the migration still reported nothing to do"

    assert _declared_keys(database, engine) == ["live_deployments"], (
        "after the orphan was resolved the migration still did not land"
    )


class TestTheRebuildSwapsCorrectlyDespiteTheChildForeignKey:
    """The rebuild must survive `live_executions.signal_id -> live_signations.id`.

    This is not hypothetical and it is not about orphans. `live_executions` already carries a foreign
    key into `live_signations`, which this migration does not own and must not drop. With enforcement
    on, `DROP TABLE live_signations` is refused *even when no dependent row exists*, because the surviving
    executions point at rows inside the table being dropped - so the rebuild failed at the DROP on a
    production clone whose orphan rows had already been removed, with the FK count still 0.

    The fix is `PRAGMA foreign_keys=OFF` around the swap, set **before any statement the migration
    issues**. SQLite ignores that pragma while a transaction is open, so the first attempt - setting it
    immediately before the DROP, inside the migration - was silently a no-op and the rehearsal still
    failed. Both tests below exist because that regression is invisible to every other test here.
    """

    @staticmethod
    def _db(tmp_path):
        import sqlite3

        path = tmp_path / "swap.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE live_deployments (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE live_signals (id TEXT PRIMARY KEY, deployment_id TEXT)")
        conn.execute("CREATE TABLE live_executions (id TEXT PRIMARY KEY, signal_id TEXT REFERENCES live_signals(id))")
        conn.execute("INSERT INTO live_deployments VALUES ('dep-1')")
        conn.execute("INSERT INTO live_signals VALUES ('sig-1', 'dep-1')")
        # A row that points into the table the rebuild will drop. This is what blocks it.
        conn.execute("INSERT INTO live_executions VALUES ('exe-1', 'sig-1')")
        conn.commit()
        return path

    def test_the_rebuild_lands_while_a_child_foreign_key_exists(self, tmp_path) -> None:
        from sqlalchemy import create_engine, text

        from shared.database import _migrate_live_signal_deployment_fk

        path = self._db(tmp_path)
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.begin() as conn:
                # Enforcement must be ON. SQLAlchemy leaves `PRAGMA foreign_keys` at its SQLite
                # default of OFF, so without this the fixture never reproduces the production failure
                # and the test passes whatever the migration does - which is exactly what the first
                # version of this test did. Measured on a 703 MB production clone: with enforcement
                # on, the DROP is refused; with it off, the same code succeeds.
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

    def test_the_swap_is_not_a_no_op_when_the_pragma_is_moved_late(self, tmp_path) -> None:
        """A regression guard for the fix itself, not for the migration's outcome.

        `PRAGMA foreign_keys` is ignored while a transaction is open. This asserts that fact directly,
        so that "someone moved the pragma back next to the DROP" fails here instead of failing silently
        in a rehearsal three weeks from now.
        """
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("BEGIN")
            conn.execute("PRAGMA foreign_keys=OFF")
            during = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            conn.execute("COMMIT")
            conn.execute("PRAGMA foreign_keys=OFF")
            after = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert during == 0 and after == 0, (
                "this test assumes the pragma is honoured both inside and outside a transaction; if "
                "that ever changes on this SQLite build, the migration's guard needs rechecking"
            )
        finally:
            conn.close()


def test_the_fk_migration_is_actually_wired_into_the_schema_migration() -> None:
    """`_migrate_schema` must call it.

    It was left uncalled for months on a measured blast radius of "48 tests plus 8 errors" - a figure
    taken against a migration that could not run at all, because `live_executions.signal_id` already
    referenced the table it drops. After that was fixed the same wiring passes the whole suite, so the
    original measurement no longer describes reality.

    This asserts the call is present in `_migrate_schema` by *source*, deliberately: the alternative -
    running the whole schema migration and checking `PRAGMA foreign_key_list` - would be a far heavier
    test, and would still be satisfied by a call inside a branch that never executes for these fixtures.
    It is the one place in this file where reading source is the honest instrument, and it says so.
    """
    import inspect

    from shared.database import _migrate_schema

    source = inspect.getsource(_migrate_schema)
    assert "_migrate_live_signal_deployment_fk(connection)" in source, (
        "_migrate_schema no longer calls the FK migration. It was previously omitted deliberately; "
        "if it is being removed again, delete or update this test rather than letting the "
        "constraint go missing without a record."
    )

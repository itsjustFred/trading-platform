"""A database built from the model must match the schema production actually has.

`Field(default=...)` is a **Python-side** default. It never reaches the DDL. So a model declaring
only Python-side defaults produces `NOT NULL` columns with **no database-level default** - and where
production has carried those defaults for some time, the two schemas diverge.

That divergence is not cosmetic. Measured on both schemas, one column at a time, with every
*other* required column supplied and only the target omitted:

| | model-built database | production |
|---|---|---|
| `INSERT` omitting the column | `IntegrityError` | succeeds |

Same statement, same schema intent, different outcome - decided purely by which database you are
pointed at. ORM inserts are unaffected, because the ORM always supplies the Python-side value. **Raw
SQL is not**, and raw SQL is what migrations, backfills and ad-hoc repair scripts are.

This is the same defect as the four scalar stat columns fixed in `476d6d6`, and that commit left it
behind in eleven more places. So the defaults are now declared at the schema level too.

The expectations below are **read from production**, not copied from the model - that is the whole
point. If somebody edits the model and production together the test still holds; if they edit one
alone, it fails, which is the case worth catching.

SQLite reports `server_default` through `PRAGMA table_info` **quoted** - `'[]'` rather than `[]` -
so the comparison strips the quotes rather than guessing. Getting this wrong cost one run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")

# Production's actual defaults, read-only, verified 2026-10-04, and **unquoted** - SQLite reports
# them through PRAGMA as the text `'[]'`, which `_unquote` strips. Any change here is a real
# schema change made to production by hand, and should be reconciled rather than edited to suit.
PRODUCTION_DEFAULTS = {
    ("backtests", "tags"): "[]",
    ("backtests", "equity_curve"): "[]",
    ("live_accounts", "is_demo"): "0",
    ("live_deployments", "settings"): "{}",
    ("live_deployments", "signal_config"): "{}",
    ("live_deployments", "symbol_filter"): "[]",
    ("live_deployments", "signal_source"): "internal",
    ("live_deployments", "signal_source_type"): "internal",
    ("live_deployments", "signal_source_id"): "",
    ("live_signals", "source"): "internal",
    ("live_signals", "broker_order_id"): "",
}

# And one that must NOT gain a default: `portfolio_components.settings` has none in production,
# and a well-meaning "fix" once added one. Pinned because I made that mistake here.
MUST_NOT_HAVE_A_DEFAULT = {("portfolio_components", "settings")}


def _unquote(value: object) -> str | None:
    """SQLite reports a server_default quoted, as the text `'[]'`. Normalise to `[]`."""
    if value is None:
        return None
    return str(value).strip("'")


def _defaults_from(db_path: Path, tables: set[str]) -> dict[tuple[str, str], str | None]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: dict[tuple[str, str], str | None] = {}
        for table in tables:
            for row in conn.execute("PRAGMA table_info(%s)" % table):
                out[(table, str(row[1]))] = _unquote(row[4])
        return out
    finally:
        conn.close()


@pytest.fixture(scope="module")
def model_built_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database whose schema comes from the model alone - `create_all`, no migrations.

    Built from `SQLModel.metadata` in a throwaway file. Production is never opened for writing, and
    the fixture asserts the file it created is the one it inspects.
    """
    from shared.database import SQLModel as _SM  # noqa: F401  (ensures metadata is populated)

    db_file = tmp_path_factory.mktemp("model-built") / "schema.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        SQLModel.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert db_file.is_file(), "create_all did not produce the file under test"
    return db_file


@pytest.mark.skipif(not PRODUCTION_DB.is_file(), reason="production database not present")
def test_the_model_declares_every_default_production_has(model_built_db: Path) -> None:
    """The parity check itself.

    Read production fresh rather than trusting the constant above, so this cannot pass by comparing
    the model against a copy of itself. If the two ever disagree, the message says which direction -
    which is the difference between "someone changed the model" and "someone changed production by
    hand", and those need opposite responses.
    """
    # Both sets, not just the first. Collecting only `PRODUCTION_DEFAULTS` left
    # `MUST_NOT_HAVE_A_DEFAULT` looking at a table that was never read, so `.get()` returned None
    # and
    # the negative guard could not fire - a check that cannot fail, found by mutation.
    tables = {table for table, _ in PRODUCTION_DEFAULTS}
    tables |= {table for table, _ in MUST_NOT_HAVE_A_DEFAULT}
    mine = _defaults_from(model_built_db, tables)
    theirs = _defaults_from(PRODUCTION_DB, tables)

    model_lacks, model_added = [], []
    for key, expected in PRODUCTION_DEFAULTS.items():
        if theirs.get(key) != expected:
            pytest.fail(
                f"production's default for {key[0]}.{key[1]} is {theirs.get(key)!r}, not the "
                f"{expected!r} this test was written against. Production changed - reconcile the "
                "test and the model rather than editing either to match."
            )
        if mine.get(key) != expected:
            model_lacks.append(
                f"{key[0]}.{key[1]}: model={mine.get(key)!r} production={expected!r}"
            )

    for key in MUST_NOT_HAVE_A_DEFAULT:
        if mine.get(key) is not None:
            model_added.append(
                f"{key[0]}.{key[1]}: model now has default {mine.get(key)!r}, production has none"
            )
    assert not model_added, (
        "these columns gained a database default the model invented, so a fresh install now "
        f"diverges from production in the opposite direction: {model_added}"
    )
    assert not model_lacks, (
        "the model is missing database-level defaults that production has, so an INSERT that "
        "omits these columns raises IntegrityError on a fresh install and succeeds on production: "
        f"{model_lacks}"
    )


@pytest.mark.skipif(not PRODUCTION_DB.is_file(), reason="production database not present")
def test_a_raw_insert_lands_on_a_model_built_database(model_built_db: Path) -> None:
    """The consequence, demonstrated rather than asserted.

    Supplies every other required column of `live_deployments` and omits only the one whose
    default is declared at schema level. If the default were missing, this raises - which is the
    whole failure mode, shown rather than described.
    """
    conn = sqlite3.connect(model_built_db)
    try:
        conn.execute(
            "INSERT INTO live_deployments "
            "(id, strategy_name, account_id, symbols, timeframe, status, created_at, updated_at, "
            " signal_source, signal_source_type, signal_source_id, symbol_filter) "
            "VALUES ('probe', 's', 'acct', '[]', '1m', 'PAUSED', '', '', 'internal', "
            "'internal', '', '[]')"
        )
        conn.commit()
        stored = conn.execute(
            "SELECT settings FROM live_deployments WHERE id = 'probe'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "{}", f"the schema default did not apply, got {stored!r}"


def test_the_scalar_stat_defaults_are_still_declared(model_built_db: Path) -> None:
    """The four from `476d6d6`, kept honest by the same mechanism.

    Separate from the parity test above because they were the original case: they are the columns
    whose absence made ~2,600 rows read a fabricated `0.0`, so a regression here is silent and
    expensive.
    """
    defaults = _defaults_from(model_built_db, {"backtests"})
    for column, expected in (
        ("sharpe_ratio", "0.0"),
        ("psr", "0.0"),
        ("cagr", "0.0"),
        ("p_value", "1.0"),
    ):
        assert defaults.get(("backtests", column)) == expected, (
            f"backtests.{column} has model default {defaults.get(('backtests', column))!r}, "
            f"expected {expected!r}"
        )


def test_migrations_run_once_per_process_and_never_again(isolated_db: tuple) -> None:
    """Why the production drift persisted - and why one restart is all it takes.

    # init_db()` runs `create_all` and `_migrate_schema` **once per process** and then
    # short-circuits on
    a module-level `_DB_INITIALIZED` flag. Proven by dropping a column and calling it again:

        1. init_db() ran; _DB_INITIALIZED = True
        2. dropped backtests.sharpe_ratio to simulate a pre-migration database
        3. called init_db() again - migrations should run
        4. sharpe_ratio present after the second init_db(): False

    So a long-lived process never picks up a migration added to the code after it started. That is a
    # efensible design - a process should not run migrations its own code does not know about - but
    # it
    has a consequence worth writing down, because it is the whole explanation for the four pending
    production changes: **no process has started with code that contains them.**

    The only thing that resets the flag is `reset_initialized()`, whose own docstring says it is a
    "test isolation helper". There is no `set_data_dir` and no other reset, so nothing re-arms
    migrations in a running process.

    This is asserted on behaviour rather than on the flag, because the behaviour is what matters: a
    second `init_db()` must not claim to have migrated when it did not.
    """
    import shared.database as database

    # The fixture deliberately leaves `_DB_INITIALIZED = False` so tests *can* run migrations, so
    # the flag has to be armed first - otherwise the second `init_db()` is the first one and runs
    # migrations, which is exactly what this test is not about.
    database.init_db()
    assert database._DB_INITIALIZED, "init_db did not arm the flag; this test cannot run"

    db_path = Path(database.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE backtests DROP COLUMN sharpe_ratio")
        conn.commit()
    finally:
        conn.close()

    database.init_db()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(backtests)")}
    finally:
        conn.close()
    assert "sharpe_ratio" not in columns, (
        "a second init_db() re-ran the migrations; if that ever becomes true this test's premise "
        "is stale and the once-per-process note in the backlog needs revisiting"
    )

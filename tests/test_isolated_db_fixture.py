"""Guard tests for the shared database-isolation fixture.

``isolated_db`` lives in tests/conftest.py; the rest of the suite relies on its
contract:

- it redirects ``shared.database`` at a throwaway SQLite engine, database path
  and data dir, and resets the module-level init flag;
- the redirected engine keeps the production SQLite PRAGMAs (WAL, foreign keys,
  busy timeout) so behavior matches production;
- the real database that the module resolved at import time is never touched;
- module state is restored after the test.

The production database path is frozen at import time, before any fixture can
patch ``shared.database``, so the negative control below compares against the
database the suite would have hit without isolation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import shared.database as database

# Captured at import: the unpatched production database location.
REAL_DB_PATH: Path = database.DB_PATH
REAL_ENGINE = database.ENGINE
REAL_DATA_DIR = database.DATA_DIR


def _count_real_backtest_rows() -> int:
    """Row count of the real database, read-only (never creates the file)."""
    if not REAL_DB_PATH.exists():
        return 0
    connection = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM backtests").fetchone()[0])
    except sqlite3.OperationalError:
        # Real database exists but the table has not been created yet.
        return 0
    finally:
        connection.close()


def test_isolated_db_redirects_module_at_tmp_database(isolated_db, tmp_path: Path) -> None:
    db_module, engine, data_dir = isolated_db

    assert db_module is database
    assert data_dir == tmp_path / "data"
    assert data_dir.exists()

    # The module globals the suite's helpers read must point at the sandbox.
    assert database.ENGINE.url.database == (data_dir / "backtests.db").as_posix()
    assert database.DB_PATH == data_dir / "backtests.db"
    assert database.DATA_DIR == data_dir


def test_isolated_db_writes_stay_out_of_real_database(isolated_db) -> None:
    before = _count_real_backtest_rows()

    record_id = "fixture_isolation_probe"
    with database.get_session() as session:
        session.add(database.BacktestRecord(id=record_id, strategies=["fixture"], engine="native"))
        session.commit()
        from sqlmodel import select

        saved = session.exec(
            select(database.BacktestRecord).where(database.BacktestRecord.id == record_id)
        ).first()

    assert saved is not None, "the write must land in the sandboxed database"
    assert _count_real_backtest_rows() == before, "real database must stay untouched"


def test_isolated_db_attaches_production_sqlite_pragmas(isolated_db) -> None:
    from sqlalchemy import text

    with database.ENGINE.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar()

    assert str(journal_mode).lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 30000


def test_isolated_db_supports_bare_init_db(isolated_db) -> None:
    """init_db() inside the fixture must build schema in the sandbox, not real DB."""
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.BacktestRecord(id="init_db_probe", strategies=["fixture"], engine="native")
        )
        session.commit()

    from sqlalchemy import text

    with database.ENGINE.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "backtests" in tables


def test_isolated_db_restores_module_state_after_teardown(restored_db_state, isolated_db) -> None:
    """Patching is active inside the test and undone once the fixture tears down."""
    assert database.ENGINE is not REAL_ENGINE
    assert database.DB_PATH != REAL_DB_PATH
    assert database.DATA_DIR != REAL_DATA_DIR


def test_grid_tester_sqlite_tests_do_not_use_bare_init_db() -> None:
    """tests/test_grid_tester_sqlite.py must run on the isolated_db fixture (B-32).

    A bare ``init_db()`` there writes permanent rows into the production
    database that the module resolved at import time.
    """
    source = Path(__file__).parent.joinpath("test_grid_tester_sqlite.py").read_text()
    assert "isolated_db" in source, "grid tester sqlite tests must opt into isolated_db"
    bare_calls = [
        line
        for line in source.splitlines()
        if line.strip() == "init_db()" and not line.startswith("        ")
    ]
    assert bare_calls == [], f"bare init_db() found outside fixture usage: {bare_calls}"


@pytest.fixture
def restored_db_state():
    """Assert ``isolated_db`` restored module state after its own teardown.

    Listed before ``isolated_db`` so pytest finalizes it *after* that fixture
    has already restored the production module attributes.
    """
    yield
    assert database.ENGINE is REAL_ENGINE
    assert database.DB_PATH == REAL_DB_PATH
    assert database.DATA_DIR == REAL_DATA_DIR

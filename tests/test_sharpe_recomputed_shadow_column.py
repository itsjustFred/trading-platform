"""The shadow column exists to make two *absent facts* expressible.

Neither could be represented before, and both were measured rather than assumed:

* **"Not computed" cannot live in `sharpe_ratio`.** It is `REAL NOT NULL DEFAULT 0.0`, so a row that
  was never computed is indistinguishable from one that genuinely scored zero. In production, 1,487 rows
  hold a real `0.0` in their stats JSON and only **85** have no Sharpe value at all.

* **Disagreement cannot be seen.** Recomputing from each row's own stored `daily_returns`, over the 212
  rows carrying at least 100 observations, `runner/stats.py:_sharpe_manual` reproduces 59 bit-exactly
  and **disagrees with 113 (53.3%)**. Within the `native` engine alone the stored values split three
  ways: 97 annualise by `sqrt(252)`, 50 by `sqrt(n)`, 62 match neither. The corpus cannot currently
  verify its own Sharpe.

The alternatives were rejected on measurements, not taste. Making `sharpe_ratio` nullable needs a table
rebuild of a 668 MB production table that four child tables reference by foreign key, and it would not
change the display at all: the viewer computes its sort key as
`float(item.get("sharpe_ratio", 0.0) or 0.0)`, so a NULL comes straight back as `0.0`.

These assertions are mostly source-level on purpose. The property is the *shape* of the schema - a
nullable column beside a NOT NULL one - and the reason is a measurement the code cannot re-derive.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DATABASE = REPO / "shared" / "database.py"


def test_the_shadow_column_is_nullable_and_the_original_is_not() -> None:
    block = re.search(r"sharpe_recomputed:.*?\)\n", DATABASE.read_text(encoding="utf-8"), re.S)
    assert block, "sharpe_recomputed is not on the model"
    assert "nullable=True" in block.group(0), (
        "sharpe_recomputed must be nullable - a non-null default would reintroduce exactly the "
        "ambiguity it exists to remove"
    )
    assert "sharpe_ratio" in DATABASE.read_text(encoding="utf-8"), "sharpe_ratio should still exist"
    assert re.search(r'sharpe_ratio: float = Field\(\s*default=0\.0,.*?nullable=False', DATABASE.read_text(encoding="utf-8"), re.S), (
        "sharpe_ratio must keep NOT NULL DEFAULT 0.0; changing it is the table-rebuild path this column "
        "was chosen to avoid"
    )


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch):
    """Point the module at a temporary database.

    Without this the migration runs against whichever engine the module already holds, which is not a
    thing a test should be doing to a real file. The first version of these tests built a local engine
    and then called `init_db()`, which migrates the *module's* engine - so the assertions read an empty
    schema and looked like the migration was broken when it had never been pointed at the right
    database.
    """
    import shared.database as database

    engine = database.create_engine(f"sqlite:///{(tmp_path / 'shadow.db').as_posix()}")
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    database.init_db()
    yield database, engine
    engine.dispose()


def test_the_migration_is_idempotent(sandbox) -> None:
    """It has to no-op on production, which already has every other column."""
    database, engine = sandbox
    with engine.connect() as connection:
        database._add_backtests_sharpe_recomputed_column(connection)
        columns = [r[1] for r in connection.execute(
            database.text("PRAGMA table_info(backtests)")).fetchall()]
    assert columns.count("sharpe_recomputed") == 1, "the migration is not idempotent"


def test_a_fresh_install_carries_the_column(sandbox) -> None:
    """The original stat columns were absent from every fresh database for years; do not repeat that."""
    database, engine = sandbox
    with engine.connect() as connection:
        columns = [r[1] for r in connection.execute(
            database.text("PRAGMA table_info(backtests)")).fetchall()]
    assert "sharpe_recomputed" in columns


def test_null_is_representable_and_distinct_from_zero() -> None:
    """The whole point: absent and zero must be different values in the same column."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (stored REAL NOT NULL DEFAULT 0.0, recomputed REAL)")
    con.execute("INSERT INTO t (stored, recomputed) VALUES (0.0, NULL)")
    con.execute("INSERT INTO t (stored, recomputed) VALUES (0.0, 0.0)")
    rows = con.execute("SELECT stored, recomputed FROM t ORDER BY rowid").fetchall()
    assert rows[0] == (0.0, None), rows
    assert rows[1] == (0.0, 0.0), rows
    assert rows[0] != rows[1], "absent and genuine zero must not collapse to one state"


def test_the_column_is_not_written_by_ordinary_saves() -> None:
    """Adding a column must not start populating it. The backfill is a separate, explicit step."""
    source = DATABASE.read_text(encoding="utf-8")
    writes = re.findall(r"sharpe_recomputed\s*=\s*[^=]", source)
    assert not writes, (
        f"something assigns sharpe_recomputed directly: {writes}. The column is populated by an "
        f"explicit backfill so the comparison stays reviewable."
    )

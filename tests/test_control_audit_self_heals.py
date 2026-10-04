"""The live-control audit trail must self-heal, and must never raise.

This corrects a finding I recorded earlier and then measured wrong.

I had written that `record_live_control_audit` "swallows every failure", making the absence of the
`live_control_audit` table on production a live hazard - every safety-critical control action would
proceed with no audit record and no raised error. Both halves of that are wrong:

1. **It does not swallow silently.** The `except Exception` is deliberate and documented in the
   function's own docstring: "an audit write that could abort the operation it is recording would
   be a
   worse failure than the missing record". It logs at **`error`**, not debug or info.
2. **The missing table is not a hazard for this path at all.** The function calls `init_db()` before
   writing, and `init_db()` runs `create_all` - so the table is created on demand. Measured by
   dropping the table to match production and then recording an entry:

       table present again: True
       rows recorded      : [('arm', 'deployment', 'dep-1', 1)]

Both properties are pinned here. The first because the reasoning in the docstring is exactly the
kind
of decision that looks like a bug to a later reader - and "fixing" it would abort a live control
change because its *audit record* could not be written, which is the worse failure the docstring
names.
The second because it is the reason the missing table is a bookkeeping gap rather than a safety one,
and that is not obvious from reading either the function or the migration list.

The genuine remaining question is unchanged and still the human's: whether an operation that
succeeded
while its audit write failed should be surfaced *beyond* an error log - see H1 in the backlog.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import shared.database as database


def _tables() -> set[str]:
    conn = sqlite3.connect(f"file:{Path(database.DB_PATH)}?mode=ro", uri=True)
    try:
        return {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _rows() -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{Path(database.DB_PATH)}?mode=ro", uri=True)
    try:
        return list(
            conn.execute(
                "SELECT action, target_type, target_id, succeeded FROM live_control_audit"
            )
        )
    finally:
        conn.close()


def _drop_the_audit_table() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        conn.execute("DROP TABLE live_control_audit")
        conn.commit()
    finally:
        conn.close()
    database.reset_initialized()


def test_the_audit_trail_self_heals_when_the_table_is_missing(isolated_db: Any) -> None:
    """**The correction.** Production has no `live_control_audit` table, and that is survivable.

    The write path calls `init_db()`, which creates it. So a first control action on a database that
    has never had one still records - which is why the missing table is a bookkeeping gap and not
    the
    safety hazard I originally described.
    """
    _drop_the_audit_table()
    assert "live_control_audit" not in _tables(), "could not stage the missing-table case"

    payload = database.record_live_control_audit(
        action="arm", target_type="deployment", target_id="dep-1", actor="tester", succeeded=True
    )

    assert payload["action"] == "arm", payload
    assert "live_control_audit" in _tables(), (
        "the write path did not recreate the table, so production's missing table would lose "
        "every audit record rather than creating it on demand"
    )
    assert _rows() == [("arm", "deployment", "dep-1", 1)], _rows()


def test_the_audit_write_never_raises_even_when_the_database_is_unusable(
    isolated_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The deliberate decision, pinned.**

    "An audit write that could abort the operation it is recording would be a worse failure than the
    missing record." So a broken database must not stop the caller - it must return the payload and
    log.

    Without this test the `except Exception` reads as an unfinished thought, and the natural next
    change is to let it propagate - which would mean a deployment could not be stopped because its
    stop could not be recorded.
    """

    def explode() -> None:
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(database, "get_session", explode)
    payload = database.record_live_control_audit(
        action="stop", target_type="deployment", target_id="dep-1", succeeded=True
    )
    assert payload["action"] == "stop", payload
    assert payload["target_id"] == "dep-1", payload


def test_a_failed_audit_is_logged_at_error_not_buried(
    isolated_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Logged, and loudly enough to find.

    The severity is the whole difference between "recorded somewhere" and "invisible". A reader who
    greps for warnings would miss a debug-level line entirely.
    """

    def explode() -> None:
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(database, "get_session", explode)
    with caplog.at_level("ERROR", logger="database"):
        database.record_live_control_audit(
            action="stop", target_type="deployment", target_id="dep-1"
        )

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "the failed audit write logged nothing at ERROR"
    assert "audit record" in errors[-1], errors[-1]

"""Remove duplicate LiveExecution rows from the live_executions table.

Duplicates are rows sharing the same (signal_id, filled_qty, timestamp). Within a
duplicate group the fill timestamp is identical, so "oldest" is defined by insertion
order (SQLite rowid): the first-inserted row is kept, all later rows are deleted.

Such duplicates were produced before dedupe_key-based idempotency existed, e.g. when
a fill was persisted both from the live stream and from a replayed/duplicate event.

Dry-run discipline: without ``--confirm`` nothing is deleted — the script only
reports what it would remove.

Usage:
    python -m scripts.dedupe_executions                  # report only
    python -m scripts.dedupe_executions --dry-run        # report only (explicit)
    python -m scripts.dedupe_executions --confirm        # delete duplicates
"""

from __future__ import annotations

import argparse

from sqlalchemy import bindparam, text

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.dedupe_executions
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("dedupe_executions")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from shared.database import get_session, init_db
from shared.logging import get_logger

logger = get_logger("scripts.dedupe-executions")

_DUPLICATE_ROWIDS_SQL = """
SELECT rowid FROM live_executions
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM live_executions
    GROUP BY signal_id, filled_qty, timestamp
)
"""

_DUPLICATE_GROUPS_SQL = """
SELECT signal_id, filled_qty, timestamp, COUNT(*) AS row_count
FROM live_executions
GROUP BY signal_id, filled_qty, timestamp
HAVING COUNT(*) > 1
"""


def dedupe_executions(*, dry_run: bool = False, confirm: bool = False) -> int:
    """Delete duplicate live_executions rows, keeping the oldest row per group.

    Rows are only deleted when ``confirm=True`` and ``dry_run`` is not set.
    Returns the number of rows deleted (or that would be deleted in dry-run).
    """
    effective_dry_run = dry_run or not confirm

    init_db()
    with get_session() as session:
        groups = session.connection().execute(text(_DUPLICATE_GROUPS_SQL)).fetchall()
        duplicate_rowids = [
            row[0]
            for row in session.connection().execute(text(_DUPLICATE_ROWIDS_SQL)).fetchall()
        ]

        if not duplicate_rowids:
            logger.info("No duplicate live executions found")
            return 0

        logger.info(
            "Found %d duplicate execution rows across %d groups",
            len(duplicate_rowids),
            len(groups),
            extra={
                "data": {
                    "duplicate_rows": len(duplicate_rowids),
                    "duplicate_groups": len(groups),
                }
            },
        )
        for signal_id, filled_qty, timestamp, row_count in groups:
            logger.info(
                "Duplicate group: signal_id=%s filled_qty=%s timestamp=%s rows=%d "
                "(keeping oldest, deleting %d)",
                signal_id,
                filled_qty,
                timestamp,
                row_count,
                row_count - 1,
            )

        if effective_dry_run:
            logger.info(
                "Dry run: no rows deleted",
                extra={
                    "data": {
                        "would_delete": len(duplicate_rowids),
                        "reason": "explicit_dry_run" if dry_run else "missing_confirm",
                    }
                },
            )
            return len(duplicate_rowids)

        session.connection().execute(
            text("DELETE FROM live_executions WHERE rowid IN :rowids").bindparams(
                bindparam("rowids", expanding=True)
            ),
            {"rowids": duplicate_rowids},
        )
        session.commit()
        logger.info("Deleted %d duplicate execution rows", len(duplicate_rowids))
        return len(duplicate_rowids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates without deleting anything (default).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the reported duplicate rows.",
    )
    args = parser.parse_args()
    if args.dry_run and args.confirm:
        parser.error("--dry-run and --confirm are mutually exclusive")
    dedupe_executions(dry_run=args.dry_run, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

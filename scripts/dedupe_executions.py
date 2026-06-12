"""Remove duplicate LiveExecution rows from the live_executions table.

Duplicates are rows sharing the same (signal_id, filled_qty, timestamp). Within a
duplicate group the fill timestamp is identical, so "oldest" is defined by insertion
order (SQLite rowid): the first-inserted row is kept, all later rows are deleted.

Such duplicates were produced before dedupe_key-based idempotency existed, e.g. when
a fill was persisted both from the live stream and from a replayed/duplicate event.

Usage:
    python -m scripts.dedupe_executions            # delete duplicates
    python -m scripts.dedupe_executions --dry-run  # report only, delete nothing
"""

from __future__ import annotations

import argparse

from sqlalchemy import bindparam, text

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


def dedupe_executions(*, dry_run: bool = False) -> int:
    """Delete duplicate live_executions rows, keeping the oldest row per group.

    Returns the number of rows deleted (or that would be deleted with --dry-run).
    """
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

        if dry_run:
            logger.info(
                "Dry run: would delete %d duplicate rows", len(duplicate_rowids)
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
        help="Report duplicates without deleting anything.",
    )
    args = parser.parse_args()
    dedupe_executions(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

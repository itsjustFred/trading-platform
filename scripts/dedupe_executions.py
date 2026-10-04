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

import sys
from pathlib import Path
# --- import-origin guard -------------------------------------------------
# Running a script puts `scripts/` on `sys.path[0]`, not the repo root, so `import shared` does not
# find the checkout this file lives in: it falls through to the installed package, which points at a
# *different* tree. Measured on 2026-10-04 - the two are genuinely different revisions (md5
# 6a72c6cb4dd4 vs 3b3f3d27182e), and the installed one has a *shorter migration set*, so a script
# run
# this way applies an older schema than the code believes and then reports success.
#
# This is the failure `continuation/merge-rehearsal.sh` exists to catch - its own header calls it
# "passed against the wrong data" - and the rehearsal guards itself while the scripts did not.
# Refused rather than silently corrected: quietly importing a different tree than the one being
# edited is the hazard, not the fix. Run from the repo root as `python -m
# scripts.dedupe_executions` to use this
# checkout.
import shared as _shared_package

_EXPECTED_PKG = Path(__file__).resolve().parent.parent / "shared"
_ACTUAL_PKG = Path(next(iter(_shared_package.__path__))).resolve()
if _ACTUAL_PKG != _EXPECTED_PKG:
    print(
        "refusing to run: `shared` was imported from the wrong tree.\n"
        f"  this script lives in : {Path(__file__).resolve().parent.parent}\n"
        f"  expected shared/     : {_EXPECTED_PKG}\n"
        f"  actually imported    : {_ACTUAL_PKG}\n"
        "\n"
        "Running a script puts scripts/ on sys.path[0], not the repo root, so `import shared`\n"
        "falls through to the installed package - a different revision, with a\n"
        "different migration set. To run against the checkout being edited, from the repo root:\n"
        "    python -m scripts.dedupe_executions\n"
        "continuation/merge-rehearsal.sh already refuses this for itself; this is the same check.",
        file=sys.stderr,
    )
    raise SystemExit(2)# --- end import-origin guard ---------------------------------------------

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

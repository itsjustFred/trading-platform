"""Every schema change the models declare and production does not have.

`tests/test_model_schema_matches_production.py` already checks the other direction of
model/production parity - database-level *defaults* - and it is good. This file covers the
direction nothing checked: **a column or table the model declares that production lacks.**

It matters because the count was wrong. The notes and the dashboard both said production was
**four** schema changes behind, with the list named. Measured, it is **five**:
`live_executions` gained `broker_fill_id` when the dedupe-evidence work landed, that column is
written on the fill path by `LiveExecutionModel.from_live_execution`, and the count was never
revised. So the documentation understated the gap by one, and the item it understated is the
one that fails on the *first cTrader fill after a restart* rather than on some future query.

A count is exactly the kind of claim that rots: easy to write down, never recomputed. The
point of this file is that the number is **derived from `SQLModel.metadata`** rather than
transcribed, so a future column lands here automatically instead of silently.

Both directions are asserted, because both are real transitions:

  * drift appears that is not listed -> a model change landed; add it to EXPECTED_PENDING.
  * drift disappears entirely        -> production was migrated; set EXPECTED_PENDING empty.

Reading a database that does not exist skips rather than passes. The production digest in
the gate is what guarantees it exists where this matters; a bare environment without it
should not report a schema it never looked at.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlmodel import SQLModel

import shared.database  # noqa: F401 - importing registers every model on the metadata
from shared import models  # noqa: F401

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")

# The measured, currently-pending set. Every entry is something the current code needs and
# production does not have. The reasons are kept because this list is read by whoever picks
# the migration window, and "what breaks if I skip this" is the whole question then.
# The constraint name is long, so it is wrapped across two literals rather than assembled from
# pieces. An earlier version built it with a placeholder key and a `pop`, which is exactly the kind
# of cleverness that makes a reader check the pop before they trust the dict.
_CLAIM_UNIQUENESS = (
    "unique live_deployment_submission_claims"
    ".uq_live_deployment_submission_claims_deployment"
)

# APPLIED 2026-10-06 against production with the operator's authorisation, after a 703 MB backup.
#
#     services.last_attempt_at                      added
#     live_deployments.stopped_enforced_at         added
#     live_executions.broker_fill_id               added
#     live_control_audit                           table created
#     uq_live_deployment_submission_claims        index created
#
# init_db() applied all five and returned cleanly, `pragma foreign_key_check` reported 0 violations, and
# the row counts held: 2,655 backtests, 309,886 trades, 202,183 balances, 9 accounts, 3 executions.
#
# Empty on purpose now. The point of this file was to make the count the operator plans a window around
# an asserted number rather than a transcription. That window has closed, so the assertion inverts: a
# non-empty measurement now means a model change landed without a migration, which is the drift this
# file exists to catch.
EXPECTED_PENDING: dict[str, str] = {}

# Only live-facing and timer-written tables: drift in a reporting table cannot stop a trade.
RELEVANT_PREFIXES = ("live_", "services", "trades", "background")


def _production_drift() -> set[str]:
    if not PRODUCTION_DB.exists():
        pytest.skip(f"{PRODUCTION_DB} is absent, so there is no production schema to compare")
    conn = sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
    try:
        prod_tables = {
            r[0] for r in conn.execute("select name from sqlite_master where type='table'")
        }
        drift: set[str] = set()
        for name, table in SQLModel.metadata.tables.items():
            if not name.startswith(RELEVANT_PREFIXES):
                continue
            if name not in prod_tables:
                drift.add(f"table {name}")
                continue
            have = {r[1] for r in conn.execute(f"pragma table_info({name})")}
            drift.update(f"column {name}.{c.name}" for c in table.columns if c.name not in have)
            for constraint in table.constraints:
                cname = getattr(constraint, "name", None)
                cols = tuple(getattr(c, "name", "") for c in getattr(constraint, "columns", ()))
                if not cname or len(cols) != 1:
                    continue
                # `pragma index_list` columns are (seq, name, unique, origin, partial) - column 0
                # is a sequence number, not a name. Reading r[0] compared the constraint name against
                # {0,1,2,...}, which can never match, so this check reported every named unique
                # constraint as missing and would have reported it as missing even after the migration
                # created it. It only ever looked right by accident, while the index genuinely was
                # absent. r[1] is the name.
                listed = {r[1] for r in conn.execute(f"pragma index_list({name})")}
                if cname not in listed:
                    drift.add(f"unique {name}.{cname}")
        return drift
    finally:
        conn.close()


def test_the_pending_set_is_exactly_what_is_declared() -> None:
    """Both directions. A mismatch in either is a documentation defect, not a schema defect."""
    measured = _production_drift()
    unexpected = measured - set(EXPECTED_PENDING)
    assert not unexpected, (
        f"the models declare {sorted(unexpected)} and production does not have them, but they are "
        f"not in EXPECTED_PENDING. A model change landed without the pending list being revised - "
        f"which is exactly how 'four' became five. Add each with its reason."
    )
    resolved = set(EXPECTED_PENDING) - measured
    assert not resolved, (
        f"EXPECTED_PENDING lists {sorted(resolved)} but production now has them. If the migration "
        f"has been applied, empty EXPECTED_PENDING in the same change, so this test stays a "
        f"description of reality rather than a wish."
    )


def test_the_documented_count_matches_what_is_measured() -> None:
    """The number the operator plans a window around, asserted rather than transcribed."""
    measured = len(_production_drift())
    assert measured == len(EXPECTED_PENDING), (
        f"production is {measured} schema changes behind and EXPECTED_PENDING lists "
        f"{len(EXPECTED_PENDING)}; the two must agree"
    )


def test_the_fill_path_column_is_present_so_the_next_fill_cannot_fail() -> None:
    """The one item that failed on the very next fill, inverted.

    `broker_fill_id` is written on every cTrader fill, so a partial migration - the three columns and
    the audit table but not this one - breaks live trading on a *write*, which is the worst moment to
    find out. That was why it had its own assertion while pending.

    Applied 2026-10-06, so the assertion now checks the opposite and for the same reason: if the
    column ever disappears, the next fill fails in production, and this is where that should be
    noticed first. The error message says which direction the test was written for, because a future
    reader seeing "is no longer pending" on a column that is genuinely absent needs to know it should
    be re-added to EXPECTED_PENDING rather than deleted.
    """
    key = "column live_executions.broker_fill_id"
    drift = _production_drift()
    assert key not in drift, (
        f"broker_fill_id has vanished from production ({sorted(drift)}). It is written on every cTrader "
        f"fill, so the next fill will fail on a write. If the column was dropped deliberately, add it "
        f"back to EXPECTED_PENDING in the same change so the count and reality agree."
    )

"""Correct the scalar stat columns where they disagree with the stats payload.

Four backtest columns each carry a physically-stored default meaning "never computed":
``sharpe_ratio``, ``psr`` and ``cagr`` default to ``0.0``, and ``p_value`` to ``1.0`` because "no
evidence against the null hypothesis" is the correct value for an uncomputed p-value. Where the
stats payload holds a real computed value and the column still holds its sentinel, the column is
stale: the producer published the number and nothing wrote it into the column.

That distinction matters and is checked rather than assumed. The writer gates the column on the
producer's ``undefined_metrics`` (``_scalar_stats_for_save`` uses ``resolve_defined_stat``), so two
situations look identical in the raw data and mean opposite things:

    the producer declared the metric undefined   the sentinel is deliberate; overwriting it would
                                                destroy a real "never computed" marker
    the producer published the value            the column was never written; the sentinel is stale

Measured across all four columns, every disagreeing row falls in the second case. This script
nevertheless checks per row and skips the first, so it stays correct if that changes.

The load path decides which side is authoritative:

    BacktestResult(stats=backtest.stats or {})                      shared/database.py
    viewer -> _stats_payload_from_result -> raw["sharpe_ratio"]     viewer/core.py

The payload is what the viewer displays, what the API returns and what a reader sees. The columns
are read by nothing on the display path - they are written by ``_scalar_stats_for_save`` and read
back by the same function to carry a value forward across a re-save. So **column from payload** is
the only safe direction. The reverse would erase thousands of real computed values from the UI.

Two cases are deliberately NOT reconciled, because they are not staleness:

    a row declaring its metric undefined      the sentinel is deliberate - skipped, reported
    a column holding a real value             recomputation drift, not staleness - skipped, reported

``cagr`` is the one column where the second case is common: 54 rows where both sides are non-zero
and differ by a median of 0.00055. Overwriting a real value with a slightly different real value is
a different operation from replacing a sentinel, and it would destroy information rather than
restore it.

Writes are opt-in: dry-run is the default and previews need no flag. This is not the same as running
``scripts/recalculate_backtest_stats.py``, which merges recomputed values over the payload and would
replace every one of these with 0.0.
"""


from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

# Resolve the data directory the same way the application does, rather than assuming `~/data`: a
# hard-coded home path is not portable (Windows, macOS) and silently aims `--apply` at whatever
# happens to live there. `shared.constants` reads `DATA_DIR` from the environment and is the single
# place that decision is made; the bootstrap above it keeps `python scripts/...` working from any
# cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.constants import DATA_DIR  # noqa: E402, I001 - after the sys.path bootstrap
_DISAGREEMENT_TOLERANCE = 1e-9

# Each column's physically-stored "never computed" default. `p_value` is 1.0, not 0.0, because
# "no evidence against the null hypothesis" is the correct value for a p-value that was never
# computed.
SENTINELS: dict[str, float] = {
    "sharpe_ratio": 0.0,
    "psr": 0.0,
    "cagr": 0.0,
    "p_value": 1.0,
}
_PAYLOAD_KEY_FALLBACK = {name: name.replace("_ratio", "") for name in SENTINELS}


def _payload_value(payload: dict, column: str) -> float | None:
    """The producer's value for one column, honouring the legacy key spelling, or None."""
    raw = payload.get(column, payload.get(_PAYLOAD_KEY_FALLBACK[column]))
    if raw is None:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    # `NaN` and `Infinity` are number-shaped and json.loads accepts them, so they would be queued
    # for an UPDATE into a NOT NULL REAL column, where they poison every later comparison and any
    # JSON read of the row. Not a number the column can hold honestly, so treated as absent.
    return number if math.isfinite(number) else None


def _is_declared_undefined(payload: dict, column: str) -> bool:
    """Whether the producer withheld this metric. True means the sentinel is deliberate."""
    undefined = payload.get("undefined_metrics")
    return isinstance(undefined, list) and column in undefined


def _scan(
    conn: sqlite3.Connection, columns: list[str] | None = None
) -> Iterator[tuple[str, str, float, float, bool]]:
    """Every cell that sits on its sentinel while the payload holds a different number.

    Yields ``(id, column, column_value, payload_value, withheld)``. ``withheld`` is True when the
    producer named the metric in ``undefined_metrics``, which makes the sentinel deliberate: the
    cell qualifies for the same reason as a stale one but must not be written.

    The sentinel test is exact. A tolerance would treat a real, tiny value - a Sharpe of ``1e-12``
    is a computed zero, not a missing one - as the sentinel and overwrite it, which is the one
    failure this tool exists to avoid.
    """
    selected = list(columns or SENTINELS)
    names = ["id", *selected, "stats"]
    rows = conn.execute("select " + ", ".join(names) + " from backtests").fetchall()
    for row in rows:
        # By name, not by position: the select list is built from the same list, and a column added
        # to `SENTINELS` must not silently shift every value one field over.
        record = dict(zip(names, row))
        stats = record["stats"]
        if not stats:
            continue
        try:
            payload = json.loads(stats)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for column in selected:
            payload_value = _payload_value(payload, column)
            if payload_value is None:
                continue
            column_value = float(record[column])
            if column_value != SENTINELS[column]:
                continue  # a real value: drift, not staleness
            if abs(payload_value - column_value) <= _DISAGREEMENT_TOLERANCE:
                continue
            yield (
                record["id"],
                column,
                column_value,
                payload_value,
                _is_declared_undefined(payload, column),
            )


def find_disagreements(
    conn: sqlite3.Connection, columns: list[str] | None = None
) -> list[tuple[str, str, float, float]]:
    """Every stale-sentinel disagreement, as ``(id, column, column_value, payload_value)``.

    Only cells where the column still holds its sentinel are returned. A cell holding a real value
    that differs from the payload is recomputation drift, not staleness, and reconciling it would
    overwrite a real number with a different real number - so it is deliberately not here. Cells the
    producer declared undefined are excluded here and not only in ``main()``, because a caller
    reaching this function directly must not be handed a cell whose overwrite would destroy a real
    "never computed" marker.
    """
    return [row[:4] for row in _scan(conn, columns) if not row[4]]


def find_declared_undefined(
    conn: sqlite3.Connection, columns: list[str] | None = None
) -> list[tuple[str, str]]:
    """Cells that would otherwise be reconciled but where the producer withheld the metric.

    Same qualification as ``find_disagreements`` - the column sits on its sentinel and the payload
    holds a different number - so a row that merely names a metric in ``undefined_metrics`` while
    its column already agrees does not block the run. Reported rather than applied: overwriting
    those sentinels would destroy a real "never computed" marker.
    """
    return [(row[0], row[1]) for row in _scan(conn, columns) if row[4]]


def _reconcile(conn: sqlite3.Connection, rows: list[tuple[str, str, float, float]]) -> int:
    """Write each payload value into its column. Returns how many cells were corrected.

    Each cell is re-checked against the row as it is *now* before it is written, and the value that
    was read is part of the UPDATE's condition. The scan and the write are separate statements, so
    another writer can recompute a backtest in between; writing the queued number then would
    overwrite a newer value with one compared against the old payload. ``main`` holds
    ``BEGIN IMMEDIATE`` across this function, so the re-read and the write cannot be interleaved
    either. The count comes from the statement's own rowcount for the same reason: it reports what
    was written, not what was queued.
    """
    corrected = 0
    for backtest_id, column, column_value, payload_value in rows:
        # The column is guarded by the UPDATE's own condition; the payload is not, so it is
        # re-read here. A row whose stats changed since the scan no longer holds the number this
        # payload value was computed from.
        current = conn.execute(
            "select stats from backtests where id = ?", (backtest_id,)
        ).fetchone()
        if current is None:
            continue
        try:
            current_payload = json.loads(current[0]) if current[0] else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(current_payload, dict):
            continue
        if _is_declared_undefined(current_payload, column):
            # The producer now withholds this metric, so the sentinel became deliberate after the
            # scan. Writing the queued value would destroy a real "never computed" marker.
            continue
        if _payload_value(current_payload, column) != payload_value:
            continue
        cursor = conn.execute(
            # noqa: S608 - column from SENTINELS, values are bound
            f"update backtests set {column} = ? where id = ? and {column} = ?",
            (payload_value, backtest_id, column_value),
        )
        corrected += cursor.rowcount
    return corrected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correct stale scalar stat columns in backtests from the stats payload."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Data directory containing backtests.db",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the corrections. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--column",
        action="append",
        choices=sorted(SENTINELS),
        help="Restrict to one column. Repeatable. Defaults to all four.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve()
    db_path = data_dir / "backtests.db"
    if not db_path.is_file():
        raise SystemExit(f"No backtests.db at {db_path}")

    with sqlite3.connect(db_path) as conn:
        # Both scans are scoped to the requested columns: a withheld cell in a column the operator
        # did not ask about must not block the run.
        withheld = find_declared_undefined(conn, args.column)
        if withheld:
            print(
                f"Refusing: {len(withheld)} cell(s) sit on a sentinel the producer explicitly "
                f"declared unavailable. Overwriting those would destroy a real 'never computed' "
                f"marker."
            )
            for backtest_id, column in withheld[:10]:
                print(f"  {backtest_id:<44} {column}")
            # Stop, do not fall through: `--apply` below would write every other disagreement and
            # the operator would read "Refusing" above a successful exit. Reported first so the
            # operator can see which rows block the run.
            raise SystemExit(1)

        rows = find_disagreements(conn, args.column)
        if not rows:
            print("No stale sentinel disagreement between the columns and the stats payload.")
            return

        counts = Counter(column for _id, column, _c, _p in rows)
        print(f"Found {len(rows)} stale cell(s), by column:")
        for column in SENTINELS:
            if counts.get(column):
                print(f"  {column:<14} {counts[column]:>5}")

        print()
        for backtest_id, column, column_value, payload_value in rows[:20]:
            print(
                f"  {backtest_id:<40} {column:<13} column={column_value:>10.4f}  "
                f"payload={payload_value:>10.4f}"
            )
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")

        if not args.apply:
            print("\nDry run: nothing written. Re-run with --apply to correct the columns.")
            return

        conn.execute("BEGIN IMMEDIATE")
        corrected = _reconcile(conn, rows)
        conn.commit()
        print(f"\nCorrected {corrected} of {len(rows)} cell(s): columns now match the payload.")


if __name__ == "__main__":
    main()

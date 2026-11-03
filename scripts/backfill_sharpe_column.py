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


def find_disagreements(conn: sqlite3.Connection) -> list[tuple[str, str, float, float]]:
    """Every stale-sentinel disagreement, as ``(id, column, column_value, payload_value)``.

    Only rows where the column still holds its sentinel are returned. A row where the column holds a
    real value that differs from the payload is recomputation drift, not staleness, and reconciling
    it would overwrite a real number with a different real number - so it is deliberately not here.
    """
    found: list[tuple[str, str, float, float]] = []
    columns = ["id", *SENTINELS, "stats"]
    rows = conn.execute("select " + ", ".join(columns) + " from backtests").fetchall()
    for row in rows:
        record = dict(zip(columns, row))
        stats = record["stats"]
        if not stats:
            continue
        try:
            payload = json.loads(stats)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for column in SENTINELS:
            payload_value = _payload_value(payload, column)
            if payload_value is None:
                continue
            # By name, not by position: the select list above is built from the same tuple, and a
            # column added to `SENTINELS` must not silently shift every value one field over.
            column_value = float(record[column])
            if abs(payload_value - column_value) <= _DISAGREEMENT_TOLERANCE:
                continue
            if abs(column_value - SENTINELS[column]) > _DISAGREEMENT_TOLERANCE:
                continue  # a real value that differs: drift, not staleness
            if _is_declared_undefined(payload, column):
                # The sentinel here is deliberate. Excluded here and not only in main(), because a
                # caller reaching this function directly must not be handed a cell whose overwrite
                # would destroy a real "never computed" marker. A test caught exactly that.
                continue
            found.append((record["id"], column, column_value, payload_value))
    return found


def find_declared_undefined(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Rows that would have qualified but where the producer withheld the metric.

    Reported rather than applied. Measured today this is empty; if it is ever not, overwriting those
    sentinels would destroy a real "never computed" marker and the script must stop.
    """
    found: list[tuple[str, str]] = []
    rows = conn.execute("select id, stats from backtests").fetchall()
    for backtest_id, stats in rows:
        if not stats:
            continue
        try:
            payload = json.loads(stats)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for column in SENTINELS:
            if (
                _is_declared_undefined(payload, column)
                and _payload_value(payload, column) is not None
            ):
                found.append((backtest_id, column))
    return found


def _reconcile(conn: sqlite3.Connection, rows: list[tuple[str, str, float, float]]) -> int:
    """Write each payload value into its column. Returns how many cells were corrected.

    The value that was read is part of the UPDATE's condition, so a cell another writer changed
    between the scan and the write keeps its new value instead of being overwritten with a payload
    number computed against the old one. The count comes from the statement's own rowcount for the
    same reason: it reports what was written, not what was queued.
    """
    corrected = 0
    for backtest_id, column, column_value, payload_value in rows:
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
        withheld = find_declared_undefined(conn)
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

        rows = find_disagreements(conn)
        if args.column:
            rows = [row for row in rows if row[1] in set(args.column)]
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

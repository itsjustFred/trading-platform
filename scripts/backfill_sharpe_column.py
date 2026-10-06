"""Correct `backtests.sharpe_ratio` where it disagrees with the stats payload.

53 production backtests store ``sharpe_ratio = 0.0`` in the scalar column while
``stats.sharpe_ratio`` holds a real value - one of them -37.66. The load path decides which side is
stale:

    BacktestResult(stats=backtest.stats or {})                      shared/database.py
    viewer -> _stats_payload_from_result -> raw["sharpe_ratio"]     viewer/core.py

The payload is what the viewer displays, what the API returns and what a reader sees. The column is
read by nothing on the display path - it is written by ``_scalar_stats_for_save`` and read back by the
same function to carry a value forward across a re-save. So the column is the stale side and this
script corrects **column from payload**.

The direction matters. The reverse - writing the column's 0.0 into the payload - would erase 53 real
Sharpe values from the UI. It is also worth stating what this is *not*: it is not the same as running
``scripts/recalculate_backtest_stats.py``, which merges recomputed values over the payload and would
replace -37.66 with 0.0 for any row whose curve cannot yield a Sharpe.

Writes are opt-in: dry-run is the default and requires no flag to preview. Correcting a number nobody
displays is cheap, and this only ever runs on an operator's explicit ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DATA_DIR = Path.home() / "data"
_DISAGREEMENT_TOLERANCE = 1e-9


def find_disagreements(conn: sqlite3.Connection) -> list[tuple[str, float, float]]:
    """Rows where the column and the payload disagree, as ``(id, column, payload)``.

    Both are read; nothing is derived. A row where either side is 0.0 and the other is not is the
    whole population - ``tests/test_sharpe_column_vs_payload_disagreement.py`` pins that this function
    is not the place where the count is decided.
    """
    found: list[tuple[str, float, float]] = []
    for backtest_id, column, stats in conn.execute(
        "select id, sharpe_ratio, stats from backtests"
    ):
        if not stats:
            continue
        try:
            parsed = json.loads(stats)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw = parsed.get("sharpe_ratio", parsed.get("sharpe"))
        if raw is None:
            continue
        try:
            column_value = float(column)
            payload_value = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(payload_value - column_value) > _DISAGREEMENT_TOLERANCE:
            found.append((backtest_id, column_value, payload_value))
    return found


def _reconcile(conn: sqlite3.Connection, rows: list[tuple[str, float, float]]) -> int:
    """Write the payload value into the column. Returns how many rows changed.

    Refuses rather than guesses when the column is not the stale side. Every production disagreement
    has the column at 0.0, so a row where the column holds a real value and the payload holds 0.0
    means something else is going on - and overwriting it would destroy the value the platform shows.
    Skipped and counted so the operator sees it rather than having it silently applied.
    """
    corrected = 0
    for backtest_id, column_value, payload_value in rows:
        if column_value != 0.0:
            print(
                f"⚠️  skipping {backtest_id}: column holds {column_value}, not 0.0. The column is not "
                f"the stale side here, so this is a different problem than this script fixes."
            )
            continue
        conn.execute(
            "update backtests set sharpe_ratio = ? where id = ?",
            (payload_value, backtest_id),
        )
        corrected += 1
    return corrected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correct backtests.sharpe_ratio from the stats payload, where they disagree."
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
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve()
    db_path = data_dir / "backtests.db"
    if not db_path.is_file():
        raise SystemExit(f"No backtests.db at {db_path}")

    with sqlite3.connect(db_path) as conn:
        rows = find_disagreements(conn)
        if not rows:
            print("✅ No disagreement between the Sharpe column and the stats payload.")
            return

        print(f"Found {len(rows)} row(s) where the column and the payload disagree:\n")
        for backtest_id, column_value, payload_value in rows:
            print(f"  {backtest_id:<44} column={column_value:>10.4f}  payload={payload_value:>10.4f}")

        if not args.apply:
            print("\nDry run: nothing written. Re-run with --apply to correct the column.")
            return

        conn.execute("BEGIN IMMEDIATE")
        corrected = _reconcile(conn, rows)
        conn.commit()
        print(f"\n✅ Corrected {corrected} of {len(rows)} row(s): column now matches the payload.")


if __name__ == "__main__":
    main()
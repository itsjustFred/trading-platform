from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from runner.stats import compute_stats_for_result
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
# scripts.recalculate_backtest_stats` to use this
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
        "    python -m scripts.recalculate_backtest_stats\n"
        "continuation/merge-rehearsal.sh already refuses this for itself; this is the same check.",
        file=sys.stderr,
    )
    raise SystemExit(2)# --- end import-origin guard ---------------------------------------------

from shared.constants import DATA_DIR
from shared.database import (
    DB_PATH,
    init_db,
    list_backtest_ids,
    load_backtest_result,
    save_result_to_sqlite,
)


def _schema_fingerprint() -> tuple[frozenset[str], frozenset[str]]:
    """The database's schema: its objects, and those objects' definitions.

    Used to hold a dry run to its claim. It is deliberately a *schema* comparison and not a file
    hash: the only writes reachable on this path are the DDL in `init_db` (`create_all` plus
    `_migrate_schema`), because both persisted-stats writes sit behind `if not dry_run`. A missing
    database reads as an empty schema, so "it did not exist before" is distinguishable from
    "it existed and is unchanged".
    """
    if not DB_PATH.exists():
        return frozenset(), frozenset()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return (
        frozenset(f"{r[0]} {r[1]}" for r in rows),
        frozenset(f"{r[0]} {r[1]}: {r[2]}" for r in rows),
    )


def _iter_json_backtests(backtests_dir: Path) -> list[Path]:
    if not backtests_dir.exists():
        return []
    return sorted(backtests_dir.glob("*.trades.json"))


def _recalculate_sqlite_backtests(*, dry_run: bool) -> tuple[int, int, int]:
    ids = list_backtest_ids()
    scanned = 0
    updated = 0
    failed = 0

    for backtest_id in ids:
        scanned += 1
        try:
            result = load_backtest_result(backtest_id)
            if result is None:
                failed += 1
                print(f"⚠️ SQLite backtest not found during recalc: {backtest_id}")
                continue

            existing_stats = dict(result.stats or {})
            recalculated_stats = compute_stats_for_result(result)
            merged_stats = {**existing_stats, **recalculated_stats}

            if merged_stats == existing_stats:
                continue

            if not dry_run:
                result.stats = merged_stats
                save_result_to_sqlite(result)
            updated += 1
        except Exception as exc:
            failed += 1
            print(f"❌ SQLite recalc failed for {backtest_id}: {exc}")

    return scanned, updated, failed


def _load_json_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_result_payload(payload: dict[str, Any]):
    from shared.models import BacktestResult

    return BacktestResult.from_dict(payload)


def _save_json_payload(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.write_text(serialized, encoding="utf-8")
        return
    except PermissionError:
        pass

    rewrite_path = path.parent / f".{path.name}.rewrite"
    rewrite_path.write_text(serialized, encoding="utf-8")
    try:
        os.replace(rewrite_path, path)
    except Exception:
        if rewrite_path.exists():
            rewrite_path.unlink()
        raise


def _recalculate_json_backtests(backtests_dir: Path, *, dry_run: bool) -> tuple[int, int, int]:
    files = _iter_json_backtests(backtests_dir)
    scanned = 0
    updated = 0
    failed = 0

    for path in files:
        scanned += 1
        try:
            payload = _load_json_payload(path)
            result = _parse_result_payload(payload)
            existing_stats = dict(result.stats or {})
            recalculated_stats = compute_stats_for_result(result)
            merged_stats = {**existing_stats, **recalculated_stats}

            if merged_stats == existing_stats:
                continue

            if not dry_run:
                payload["stats"] = merged_stats
                _save_json_payload(path, payload)
            updated += 1
        except Exception as exc:
            failed += 1
            print(f"❌ JSON recalc failed for {path.name}: {exc}")

    return scanned, updated, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recalculate persisted backtest stats (including CAGR) in SQLite and JSON storage."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Data directory containing backtests.db and backtests/*.trades.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report affected backtests without writing changes",
    )
    parser.add_argument(
        "--skip-json",
        action="store_true",
        help="Skip recalculating JSON backtest files",
    )
    parser.add_argument(
        "--skip-sqlite",
        action="store_true",
        help="Skip recalculating SQLite backtests",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser().resolve()
    if data_dir != DATA_DIR:
        raise ValueError(
            f"Resolved DATA_DIR is {DATA_DIR}, but --data-dir={data_dir} was provided. "
            "Set DATA_DIR/TRADING_DATA_DIR before running this script."
        )

    backtests_dir = data_dir / "backtests"

    # **A dry run is only allowed to claim what it has checked.**
    #
    # `init_db()` is unavoidable on this path even in a dry run: `list_backtest_ids` and
    # `load_backtest_result` both call it internally, so removing the top-level call would change
    # nothing. And `init_db` runs `create_all` and `_migrate_schema` - which write DDL. Measured, on
    # an empty directory:
    #
    #     $ recalculate_backtest_stats.py --dry-run
    #     Dry run: yes
    #     SQLite: scanned=0 updated=0 failed=0
    #     $ ls
    #     backtests.db            <- 479 KB, 17 tables, created by the dry run
    #
    # So "Dry run: yes" and "updated=0" together read as *nothing changed*, and against production
    # that would mean the one thing an operator used `--dry-run` to avoid: applying a pending schema
    # migration. The writes themselves are unavoidable without reworking how every reader obtains
    # its schema, so the claim is made verifiable instead - the schema is fingerprinted either side
    # of the run and a change is reported and failed, rather than passed off as `updated=0`.
    schema_before = _schema_fingerprint() if args.dry_run else None
    init_db()

    print(f"Recalculating backtest stats in DATA_DIR={data_dir}")
    print(f"Dry run: {'yes' if args.dry_run else 'no'}")

    if schema_before is not None:
        names_after, defs_after = _schema_fingerprint()
        created = sorted(names_after - schema_before[0])
        if created or defs_after != schema_before[1]:
            print(
                "❌ this dry run CHANGED the database schema, so it was not a dry run:\n"
                f"     database : {DB_PATH}\n"
                f"     created  : {', '.join(created) if created else '(none)'}\n"
                "     `init_db` runs create_all and _migrate_schema, and the read helpers call\n"
                "     it internally, so this cannot be avoided by not calling it here. Re-run\n"
                "     against a database whose schema is already current.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print("  dry run verified: schema unchanged by this run")

    sqlite_scanned = sqlite_updated = sqlite_failed = 0
    json_scanned = json_updated = json_failed = 0

    if not args.skip_sqlite:
        sqlite_scanned, sqlite_updated, sqlite_failed = _recalculate_sqlite_backtests(dry_run=args.dry_run)
    if not args.skip_json:
        json_scanned, json_updated, json_failed = _recalculate_json_backtests(
            backtests_dir,
            dry_run=args.dry_run,
        )

    print(
        "SQLite:",
        f"scanned={sqlite_scanned}",
        f"updated={sqlite_updated}",
        f"failed={sqlite_failed}",
    )
    print(
        "JSON:",
        f"scanned={json_scanned}",
        f"updated={json_updated}",
        f"failed={json_failed}",
    )

    total_failed = sqlite_failed + json_failed
    if total_failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

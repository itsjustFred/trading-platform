from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR
from shared.database import (
    init_db,
    list_backtest_ids,
    load_backtest_result,
    save_result_to_sqlite,
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
    init_db()

    print(f"Recalculating backtest stats in DATA_DIR={data_dir}")
    print(f"Dry run: {'yes' if args.dry_run else 'no'}")

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

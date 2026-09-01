"""Consolidate OHLCV parquet fragments into canonical per-timeframe files.

Fragments (``1h_part2.parquet``, ``1d-chunk3.parquet``, ...) are merged into the
canonical ``<timeframe>.parquet`` of their symbol directory. This script is
destructive — fragments are deleted after a successful merge — so it is guarded:

- ``--dry-run`` is the default: prints what would be merged/deleted and touches nothing.
- A confirmation prompt is required unless ``--yes`` is passed.
- Before the first write, every file that will be modified or deleted is copied to a
  backup directory outside the OHLCV tree, so a run can be reverted with
  ``scripts.consolidate_parquets.restore_backup``.
- Merged output is written to a temporary file and ``os.replace``d into place, so the
  canonical target is never observed half-written.

Usage:
    python -m scripts.consolidate_parquets              # dry run (default)
    python -m scripts.consolidate_parquets --yes        # consolidate, with backup
    python -m scripts.consolidate_parquets --backup-dir # custom snapshot location
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("TRADING_DATA_DIR")
DATA_DIR = (
    Path(_data_dir_env).expanduser().resolve()
    if _data_dir_env
    else (PROJECT_ROOT / "data").resolve()
)

FRAGMENT_SUFFIX = re.compile(r"[_-](?:part|chunk|fragment)\d+$", re.IGNORECASE)
KNOWN_TIMEFRAMES = {"tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "mn1"}
TIMEFRAME_PATTERN = re.compile(r"^(?:tick|s1|mn1|\d+[smhdw])$", re.IGNORECASE)


def _normalize_timeframe(stem: str, symbol: str) -> str:
    cleaned = FRAGMENT_SUFFIX.sub("", stem).lower()
    if cleaned in KNOWN_TIMEFRAMES or TIMEFRAME_PATTERN.match(cleaned):
        return cleaned
    if cleaned == symbol.lower():
        return "1d"
    return cleaned


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    if "timestamp" in normalized.columns:
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
        normalized = normalized.dropna(subset=["timestamp"])
    return normalized


def _canonical_target(parquet_path: Path) -> Path:
    symbol = parquet_path.parent.name
    timeframe = _normalize_timeframe(parquet_path.stem, symbol)
    return parquet_path.parent / f"{timeframe}.parquet"


def _iter_fragment_groups(root: Path) -> dict[Path, list[Path]]:
    groups: dict[Path, list[Path]] = {}
    for parquet_path in sorted(root.rglob("*.parquet")):
        target = _canonical_target(parquet_path)
        groups.setdefault(target, []).append(parquet_path)
    return groups


def _plan_actions(root: Path) -> list[tuple[Path, list[Path]]]:
    """Return the (target, members) merges that would change the tree."""
    actions: list[tuple[Path, list[Path]]] = []
    grouped = sorted(_iter_fragment_groups(root).items(), key=lambda item: str(item[0]))
    for target_path, members in grouped:
        unique_members = sorted(set(members), key=lambda path: str(path))
        if len(unique_members) == 1 and unique_members[0] == target_path:
            continue
        actions.append((target_path, unique_members))
    return actions


def _merge_members(members: list[Path]) -> pd.DataFrame:
    frames = [_normalize_frame(pd.read_parquet(member)) for member in members]
    combined = pd.concat(frames, ignore_index=True, sort=False)
    if "timestamp" in combined.columns:
        combined = (
            combined.drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
    else:
        combined = combined.drop_duplicates().reset_index(drop=True)
    return combined


def _validate_backup_dir(backup_dir: Path, root: Path) -> Path:
    """Validate a consolidation backup directory before it is used.

    ``--backup-dir`` arrives from the command line, so it is resolved and checked
    before anything touches the file system: it must not be the OHLCV tree itself
    or live inside it — a snapshot inside ``root`` would be picked up as parquet
    fragments (and deleted) by the very next consolidation run. ``root`` is
    resolved as well, so relative or symlinked roots compare correctly.
    """
    resolved = backup_dir.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if resolved == resolved_root or resolved_root in resolved.parents:
        raise ValueError(
            f"Backup directory {resolved} must live outside the OHLCV tree {root} — "
            "a snapshot inside the tree would be consolidated and deleted by the next run."
        )
    return resolved


def _default_backup_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root.parent / f"{root.name}-consolidation-backup-{stamp}"


def _stage_backup(actions: list[tuple[Path, list[Path]]], root: Path, backup_dir: Path) -> Path:
    """Copy every file that will be written or deleted into ``backup_dir``.

    Paths inside the snapshot mirror their location relative to ``root``, so the
    snapshot can be copied straight back over the tree to undo a run.
    """
    if backup_dir.exists():
        raise FileExistsError(
            f"Backup directory {backup_dir} already exists. Remove it or pass --backup-dir."
        )
    for _target, members in actions:
        for member in members:
            destination = backup_dir / member.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(member, destination)
    return backup_dir


def restore_backup(backup_dir: Path, root: Path) -> int:
    """Copy a consolidation backup back into the OHLCV tree. Returns files restored."""
    restored = 0
    for source in sorted(backup_dir.rglob("*.parquet")):
        destination = root / source.relative_to(backup_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        restored += 1
    return restored


def _merge_group(target_path: Path, members: list[Path]) -> None:
    """Atomically write the merged frame for one group to ``target_path``."""
    combined = _merge_members(members)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.parent / f".{target_path.name}.consolidating-{uuid.uuid4().hex[:8]}"
    try:
        combined.to_parquet(temp_path, index=False)
        os.replace(temp_path, target_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def _delete_replaced_fragments(actions: list[tuple[Path, list[Path]]]) -> int:
    """Delete the fragments that were merged into their canonical target."""
    deleted_fragments = 0
    for target_path, members in actions:
        for member in members:
            if member == target_path:
                continue
            member.unlink()
            deleted_fragments += 1
    return deleted_fragments


def consolidate(
    root: Path,
    *,
    dry_run: bool = False,
    backup_dir: Path | None = None,
) -> tuple[int, int, Path | None]:
    """Consolidate parquet fragments under ``root``.

    Returns ``(merged_groups, deleted_fragments, backup_dir)``. ``backup_dir`` is
    ``None`` for a dry run, and points at the snapshot directory for a real run.
    Nothing is deleted until every merge has been written atomically.
    """
    actions = _plan_actions(root)
    if dry_run:
        for target_path, members in actions:
            print(f"would merge {len(members)} file(s) into {target_path}")
            for member in members:
                print(f"  - {member}")
        print("Dry run (default): nothing written, nothing deleted. Pass --yes to consolidate.")
        return 0, 0, None

    if not actions:
        print(f"Nothing to consolidate under {root}")
        return 0, 0, None

    resolved_backup_dir = _validate_backup_dir(
        backup_dir if backup_dir is not None else _default_backup_dir(root), root
    )
    _stage_backup(actions, root, resolved_backup_dir)
    print(f"Backup snapshot: {resolved_backup_dir}")

    for target_path, members in actions:
        _merge_group(target_path, members)
    merged_groups = len(actions)
    deleted_fragments = _delete_replaced_fragments(actions)

    print(
        "Consolidation complete:",
        f"merged_groups={merged_groups}",
        f"deleted_fragments={deleted_fragments}",
        f"backup_dir={resolved_backup_dir}",
    )
    return merged_groups, deleted_fragments, resolved_backup_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate OHLCV parquet fragments (dry run unless --yes is passed)."
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print the plan without changing anything (default).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run for real (still writes a restorable backup snapshot first).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=(
            "Directory for the backup snapshot (default: <ohlcv>-consolidation-backup-<timestamp>)."
        ),
    )
    args = parser.parse_args(argv)

    ohlcv_root = DATA_DIR / "ohlcv"
    if not ohlcv_root.exists():
        print(f"No OHLCV directory found at {ohlcv_root}")
        return 0

    if args.yes:
        pending = _plan_actions(ohlcv_root)
        if not pending:
            print(f"Nothing to consolidate under {ohlcv_root}")
            return 0
        answer = input(
            f"Consolidate {len(pending)} group(s) under {ohlcv_root} "
            "and delete the fragments? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("Aborted — nothing was changed.")
            return 1

    consolidate(
        ohlcv_root,
        dry_run=not args.yes,
        backup_dir=args.backup_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

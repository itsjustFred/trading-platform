"""SHA256-bound human review gate for LLM-generated strategy code.

Decision Record (2026-08-31): "LLM-generated strategy code requires a human
review gate before the first run. The approval must be bound to the generated
file's SHA256 hash; if the file changes after approval, it is no longer
approved."

The gate lives next to the reviewed code: approving ``strategies/<slug>/<slug>.py``
writes ``strategies/<slug>/<slug>.py.approved.json`` containing the file's
SHA256 hash, the reviewer and the timestamp. ``is_strategy_approved`` re-reads
the file at check time, so any change after approval — including a regeneration
or a hand-edit — silently invalidates the approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.codegen_safety import screen_strategy_source

LEDGER_SUFFIX = ".approved.json"
GENERATED_MARKER_SUFFIX = ".generated.json"


class StrategyNotApprovedError(RuntimeError):
    """Raised when generated strategy code is not covered by a valid approval."""


def approve_strategy(
    strategy_path: Path | str,
    *,
    approved_by: str | None = None,
    strategies_root: Path | str | None = None,
) -> dict[str, Any]:
    """Record a human approval for the current content of ``strategy_path``."""
    path = Path(strategy_path)
    if not path.exists():
        raise FileNotFoundError(f"Cannot approve missing strategy file: {path}")

    reviewer = str(approved_by or "").strip()
    if not reviewer or reviewer.casefold() == "unspecified":
        raise ValueError("approved_by must identify the human reviewer")

    digest = _sha256_of(path)
    record: dict[str, Any] = {
        "file": _relative_name(path, strategies_root),
        "sha256": digest,
        "approved_by": reviewer,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    ledger = path.with_name(path.name + LEDGER_SUFFIX)
    _atomic_write_json(ledger, record)
    return record


def is_strategy_approved(
    strategy_path: Path | str,
    *,
    strategies_root: Path | str | None = None,
) -> bool:
    """True only if the file's current SHA256 hash matches the recorded approval."""
    return _matching_record(strategy_path, strategies_root) is not None


def require_approval(
    strategy_path: Path | str,
    *,
    strategies_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return the matching approval record or raise ``StrategyNotApprovedError``."""
    path = Path(strategy_path)
    record = _matching_record(path, strategies_root)
    if record is None:
        raise StrategyNotApprovedError(
            "Strategy file is not covered by a human review approval "
            f"(approval is bound to the file's SHA256 hash): {path}"
        )
    return record


def approval_path_for(strategy_path: Path | str) -> Path:
    """Ledger path for ``strategy_path``: the file name plus ``LEDGER_SUFFIX``."""
    path = Path(strategy_path)
    return path.with_name(path.name + LEDGER_SUFFIX)


def generated_marker_path_for(strategy_path: Path | str) -> Path:
    path = Path(strategy_path)
    return path.with_name(path.name + GENERATED_MARKER_SUFFIX)


def mark_generated_strategy(
    strategy_path: Path | str,
    *,
    strategies_root: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(strategy_path)
    record = {
        "file": _relative_name(path, strategies_root),
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "state": "awaiting_approval",
    }
    _atomic_write_json(generated_marker_path_for(path), record)
    return record


def is_generated_strategy(strategy_path: Path | str) -> bool:
    return generated_marker_path_for(strategy_path).exists()


def require_generated_strategy_approval(
    strategy_path: Path | str,
    *,
    strategies_root: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    path = Path(strategy_path)
    if not force and not is_generated_strategy(path):
        return None
    if not path.exists():
        raise StrategyNotApprovedError(f"Generated strategy file is missing: {path}")

    if path.suffix.casefold() == ".py":
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StrategyNotApprovedError(
                f"Could not read generated strategy for approval screening: {path}"
            ) from exc
        screening = screen_strategy_source(source)
        if not screening.allowed:
            raise StrategyNotApprovedError(
                "Generated strategy failed safety screening: "
                + "; ".join(screening.messages)
            )

    effective_root = strategies_root
    if effective_root is None:
        effective_root = _marker_strategies_root(path)
    return require_approval(path, strategies_root=effective_root)


def _matching_record(
    strategy_path: Path | str,
    strategies_root: Path | str | None,
) -> dict[str, Any] | None:
    path = Path(strategy_path)
    if not path.exists():
        return None

    ledger = approval_path_for(path)
    if not ledger.exists():
        return None

    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None

    # Provenance check: a ledger must name the file it is found beside. A
    # ledger copied (or left behind) for a different strategy file never
    # legitimizes this one, even if the hashes happen to match.
    reviewer = str(record.get("approved_by") or "").strip()
    if not reviewer or reviewer.casefold() == "unspecified":
        return None

    if str(record.get("file") or "") != _relative_name(path, strategies_root):
        return None

    if str(record.get("sha256") or "") != _sha256_of(path):
        return None
    return record


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_name(path: Path, strategies_root: Path | str | None) -> str:
    if strategies_root is None:
        return path.name
    try:
        return path.resolve().relative_to(Path(strategies_root).resolve()).as_posix()
    except ValueError:
        return path.name


def _marker_strategies_root(path: Path) -> Path | None:
    marker = generated_marker_path_for(path)
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    relative = str(record.get("file") or "").strip()
    parts = Path(relative).parts
    if len(parts) < 2:
        return None
    root = path.resolve()
    for _ in parts:
        root = root.parent
    return root


def _atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | directory_flag)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)

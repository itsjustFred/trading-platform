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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_SUFFIX = ".approved.json"


class StrategyNotApprovedError(RuntimeError):
    """Raised when generated strategy code is not covered by a valid approval."""


def approve_strategy(
    strategy_path: Path | str,
    *,
    approved_by: str = "",
    strategies_root: Path | str | None = None,
) -> dict[str, Any]:
    """Record a human approval for the current content of ``strategy_path``."""
    path = Path(strategy_path)
    if not path.exists():
        raise FileNotFoundError(f"Cannot approve missing strategy file: {path}")

    digest = _sha256_of(path)
    record: dict[str, Any] = {
        "file": _relative_name(path, strategies_root),
        "sha256": digest,
        "approved_by": str(approved_by or "unspecified").strip() or "unspecified",
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    ledger = approval_path_for(path, strategies_root)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def approval_path_for(
    strategy_path: Path | str,
    strategies_root: Path | str | None = None,
) -> Path:
    path = Path(strategy_path)
    return path.with_name(path.name + LEDGER_SUFFIX)


def _matching_record(
    strategy_path: Path | str,
    strategies_root: Path | str | None,
) -> dict[str, Any] | None:
    path = Path(strategy_path)
    if not path.exists():
        return None

    ledger = approval_path_for(path, strategies_root)
    if not ledger.exists():
        return None

    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
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

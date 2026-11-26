"""Re-attribute cTrader close fills that were booked as broker-side closes.

Bug (paper soak, 2026-11-26): an internal strategy's exit on cTrader is sent
as ``ProtoOAClosePositionReq``, which has no ``clientOrderId`` field, so the
fill's execution event carries an SDK-assigned id that parses as no signal.
The fill was booked through ``record_live_broker_side_close_fill`` as a
synthetic ``broker_close`` signal - while the exit signal that actually owns
the broker order id looked unfilled, and every restart's fill reconciliation
hit ``_log_dedupe_key_owned_by_other_signal`` ("Dedupe key collision across
signals ... the fill is NOT stored") because the deal's dedupe key was already
owned by the synthetic signal.

This script finds exactly that shape and repairs it:

- a ``broker_close`` signal whose executions carry a ``broker_order_id``
- that equals the ``broker_order_id`` of exactly one NON-synthetic signal on
  the SAME deployment - the exit signal that submitted the close order

For each such execution it moves the ``live_executions`` row onto the real
signal, so the dedupe key ends up owned by the exit signal and the next
reconcile skips it as an ordinary duplicate instead of erroring. The ledger
stays identical: the moved fill keeps its quantity, price, timestamp and
broker order id, and its reducing-side attribution is only changed when the
repair can prove the same entry is credited (see ``_plan_owner_moves``). A
synthetic signal left with no executions is deleted - ``broker_close_signal_id``
is deterministic, so a genuine broker-side close recreates it on demand.

Anything ambiguous is REFUSED, not guessed: no unique same-deployment owner,
an owner that is not an exit signal, an owner whose action or symbol
disagrees with the synthetic close, an attribution target that conflicts
with the entry the synthetic close named, an owner that already carries
executions the repair cannot re-attribute identically, or several parents
claiming one owner. Refused rows are reported and left untouched.

Dry-run discipline: without ``--apply`` nothing is written - the script only
reports what it would move. Every applied repair writes a
``live_control_audit`` row (``action=broker_close_attribution_repair``).

Usage:
    python -m scripts.repair_ctrader_close_attribution            # report only
    python -m scripts.repair_ctrader_close_attribution --apply    # write
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlmodel import Session, select

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.repair_ctrader_close_attribution
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("repair_ctrader_close_attribution")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from shared.database import (
    BROKER_CLOSE_PARENT_SIGNAL_KEY,
    BROKER_CLOSE_SIGNAL_SOURCE,
    LiveControlAuditModel,
    LiveExecutionModel,
    LiveSignalModel,
    get_session,
    init_db,
)
from shared.logging import get_logger

logger = get_logger("scripts.repair-ctrader-close-attribution")

REPAIR_AUDIT_ACTION = "broker_close_attribution_repair"

#: Metadata keys that name the entry a reducing fill is credited to, in the
#: same precedence ``shared.database._reduction_target_signal_id`` uses.
_ATTRIBUTION_TARGET_KEYS = ("exit_target_signal_id", BROKER_CLOSE_PARENT_SIGNAL_KEY)


def _metadata(signal: LiveSignalModel) -> dict[str, Any]:
    return dict(signal.metadata_json or {})


def _reduction_target(metadata: dict[str, Any]) -> str:
    """The entry a signal's reducing fills are credited to, or ``""``."""
    for key in _ATTRIBUTION_TARGET_KEYS:
        target = str(metadata.get(key) or "").strip()
        if target:
            return target
    return ""


def _is_exit_signal_metadata(metadata: dict[str, Any]) -> bool:
    """Mirror of the adapter's exit-signal check (``_metadata_is_exit_signal``)."""
    return (
        str(metadata.get("signal_intent") or "") == "exit"
        or bool(metadata.get("exit_only"))
        or str(metadata.get("risk_sizing_mode") or "").strip().lower() == "exit_only"
    )


def _candidate_owners(
    session: Session, *, deployment_id: str, broker_order_id: str, exclude_id: str
) -> list[LiveSignalModel]:
    """Non-synthetic signals on the same deployment carrying this order id."""
    return list(
        session.exec(
            select(LiveSignalModel)
            .where(LiveSignalModel.deployment_id == deployment_id)
            .where(LiveSignalModel.broker_order_id == broker_order_id)
            .where(LiveSignalModel.source != BROKER_CLOSE_SIGNAL_SOURCE)
            .where(LiveSignalModel.id != exclude_id)
        ).all()
    )


def _owner_preexisting_executions(session: Session, owner_id: str) -> int:
    return len(
        session.exec(
            select(LiveExecutionModel).where(LiveExecutionModel.signal_id == owner_id)
        ).all()
    )


def _plan_moves(session: Session) -> dict[str, Any]:
    """Classify every execution on a broker-close signal: move, refuse or skip.

    Returns ``{"moves", "refusals", "unmatched", "close_signals"}`` where a
    move is ``{"execution", "close", "owner", "parent", "metadata_parent"}``
    and ``metadata_parent`` is the ``parent_signal_id`` the repair must stamp
    on the owner so its fills keep crediting the same entry (``None`` = no
    metadata change needed).
    """
    close_signals = list(
        session.exec(
            select(LiveSignalModel).where(
                LiveSignalModel.source == BROKER_CLOSE_SIGNAL_SOURCE
            )
        ).all()
    )
    moves: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    executions_by_close: dict[str, list[LiveExecutionModel]] = {}
    for close in close_signals:
        executions = list(
            session.exec(
                select(LiveExecutionModel).where(
                    LiveExecutionModel.signal_id == close.id
                )
            ).all()
        )
        executions_by_close[str(close.id)] = executions
        for execution in executions:
            broker_order_id = str(execution.broker_order_id or "").strip()
            base = {
                "execution_id": str(execution.id),
                "close_signal_id": str(close.id),
                "broker_order_id": broker_order_id,
                "dedupe_key": execution.dedupe_key,
                "filled_qty": float(execution.filled_qty or 0.0),
            }
            if not broker_order_id:
                unmatched.append({**base, "reason": "empty_broker_order_id"})
                continue
            owners = _candidate_owners(
                session,
                deployment_id=str(close.deployment_id),
                broker_order_id=broker_order_id,
                exclude_id=str(close.id),
            )
            if not owners:
                unmatched.append({**base, "reason": "no_same_deployment_owner"})
                continue
            if len(owners) > 1:
                refusals.append(
                    {
                        **base,
                        "reason": "ambiguous_owner",
                        "owner_signal_ids": sorted(str(o.id) for o in owners),
                    }
                )
                continue
            owner = owners[0]
            owner_metadata = _metadata(owner)
            close_metadata = _metadata(close)
            parent = str(close_metadata.get(BROKER_CLOSE_PARENT_SIGNAL_KEY) or "").strip()
            owner_target = _reduction_target(owner_metadata)
            reason = None
            if not _is_exit_signal_metadata(owner_metadata):
                reason = "owner_not_exit_signal"
            elif str(owner.action or "").strip().lower() != str(
                close.action or ""
            ).strip().lower():
                # The synthetic close's action IS the reducing side; an owner
                # with a different action would flip the ledger's sign.
                reason = "action_mismatch"
            elif str(owner.symbol or "").strip().upper() != str(
                close.symbol or ""
            ).strip().upper():
                reason = "symbol_mismatch"
            elif owner_target and owner_target != parent:
                # The owner already credits a different entry: moving would
                # re-attribute the fill.
                reason = "attribution_conflict"
            elif owner_target and not parent:
                # The synthetic fill was unattributed (FIFO); the owner's
                # target would newly attribute it.
                reason = "attribution_conflict"
            if reason is not None:
                refusals.append(
                    {**base, "reason": reason, "owner_signal_id": str(owner.id)}
                )
                continue
            moves.append(
                {
                    "execution": execution,
                    "close": close,
                    "owner": owner,
                    "parent": parent,
                    "metadata_parent": None,
                }
            )

    # Second pass, per owner: decide whether the owner's fills keep crediting
    # the same entry, and whether a ``parent_signal_id`` stamp on the owner is
    # needed and safe. ``moves`` still holds every candidate; ``accepted``
    # collects the ones that survive.
    moves_by_owner: dict[str, list[dict[str, Any]]] = {}
    for move in moves:
        moves_by_owner.setdefault(str(move["owner"].id), []).append(move)
    accepted: list[dict[str, Any]] = []

    for owner_id, owner_moves in moves_by_owner.items():
        owner = owner_moves[0]["owner"]
        owner_target = _reduction_target(_metadata(owner))
        targeted = [m for m in owner_moves if str(m["parent"]).strip()]
        untargeted = [m for m in owner_moves if not str(m["parent"]).strip()]

        def _refuse(move: dict[str, Any], reason: str, *, _owner_id: str = owner_id) -> None:
            refusals.append(
                {
                    "execution_id": str(move["execution"].id),
                    "close_signal_id": str(move["close"].id),
                    "broker_order_id": str(move["execution"].broker_order_id or "")
                    .strip(),
                    "dedupe_key": move["execution"].dedupe_key,
                    "filled_qty": float(move["execution"].filled_qty or 0.0),
                    "reason": reason,
                    "owner_signal_id": _owner_id,
                }
            )

        if owner_target or not targeted:
            # ``owner_target`` non-empty means the first pass already proved
            # every move names that same entry. No targeted move and no owner
            # target means unattributed FIFO on both sides - identical.
            accepted.extend(owner_moves)
            continue

        parents = {str(m["parent"]).strip() for m in targeted}
        reason: str | None = None
        if len(parents) != 1:
            reason = "conflicting_parents"
        elif _owner_preexisting_executions(session, owner_id):
            # Stamping parent_signal_id would also re-attribute fills the
            # owner already carries: unattributed before, attributed after.
            reason = "owner_has_other_fills"
        if reason is not None:
            for move in targeted:
                _refuse(move, reason)
            accepted.extend(untargeted)
            continue

        parent = next(iter(parents))
        for move in targeted:
            move["metadata_parent"] = parent
        accepted.extend(targeted)
        # The stamp applies to the whole signal: fills that named no parent
        # would become attributed, so they cannot move.
        for move in untargeted:
            _refuse(move, "would_be_attributed_by_repair")

    return {
        "moves": accepted,
        "refusals": refusals,
        "unmatched": unmatched,
        "close_signals": close_signals,
        "executions_by_close": executions_by_close,
    }


def repair_ctrader_close_attribution(*, apply: bool = False) -> dict[str, Any]:
    """Plan (and with ``apply`` write) the close-attribution repair.

    Returns a JSON-serialisable report. Dry-run is the default: nothing is
    written unless ``apply`` is true.
    """
    init_db()
    report: dict[str, Any] = {
        "apply": bool(apply),
        "moves": [],
        "metadata_updates": [],
        "deleted_signals": [],
        "refusals": [],
        "unmatched": [],
    }
    with get_session() as session:
        plan = _plan_moves(session)
        moved_execution_ids = {str(m["execution"].id) for m in plan["moves"]}
        for move in plan["moves"]:
            report["moves"].append(
                {
                    "execution_id": str(move["execution"].id),
                    "close_signal_id": str(move["close"].id),
                    "owner_signal_id": str(move["owner"].id),
                    "broker_order_id": str(move["execution"].broker_order_id or "")
                    .strip(),
                    "dedupe_key": move["execution"].dedupe_key,
                    "filled_qty": float(move["execution"].filled_qty or 0.0),
                    "parent_signal_id": move["parent"],
                    "metadata_parent": move["metadata_parent"],
                }
            )
        report["refusals"] = plan["refusals"]
        report["unmatched"] = plan["unmatched"]

        # Metadata stamps: one per owner that needs ``parent_signal_id`` so
        # the moved fills keep crediting the entry the synthetic close named.
        stamped: set[str] = set()
        for move in plan["moves"]:
            if move["metadata_parent"] is None:
                continue
            owner = move["owner"]
            if str(owner.id) in stamped:
                continue
            stamped.add(str(owner.id))
            owner_metadata = _metadata(owner)
            owner_metadata[BROKER_CLOSE_PARENT_SIGNAL_KEY] = move["metadata_parent"]
            owner.metadata_json = owner_metadata
            report["metadata_updates"].append(
                {
                    "signal_id": str(owner.id),
                    "parent_signal_id": move["metadata_parent"],
                }
            )

        # A close signal is deleted only when every execution it carried was
        # moved away; refused/unmatched rows keep it alive as their ledger row.
        deletions: list[LiveSignalModel] = []
        for close in plan["close_signals"]:
            remaining = [
                e
                for e in plan["executions_by_close"].get(str(close.id), [])
                if str(e.id) not in moved_execution_ids
            ]
            if remaining:
                continue
            if not plan["executions_by_close"].get(str(close.id)):
                # Never carried fills: not this repair's business.
                continue
            deletions.append(close)
            report["deleted_signals"].append(str(close.id))

        if not apply:
            return report

        # One transaction for the whole repair: a partial move would leave the
        # dedupe key owned by the synthetic signal - the exact failure this
        # script exists to remove.
        for move in plan["moves"]:
            move["execution"].signal_id = str(move["owner"].id)
            session.add(move["execution"])
        for close in deletions:
            session.delete(close)
        audit_rows = _audit_rows(plan, report)
        for row in audit_rows:
            session.add(row)
        session.commit()
        logger.info(
            "Repaired broker-close attribution: %d fill(s) moved, "
            "%d signal(s) deleted, %d metadata stamp(s), "
            "%d refusal(s), %d unmatched",
            len(plan["moves"]),
            len(deletions),
            len(stamped),
            len(plan["refusals"]),
            len(plan["unmatched"]),
        )
    return report


def _audit_rows(plan: dict[str, Any], report: dict[str, Any]) -> list[LiveControlAuditModel]:
    """One audit row per repaired close signal (empty when nothing moved)."""
    from uuid import uuid4

    by_close: dict[str, list[dict[str, Any]]] = {}
    for move_report in report["moves"]:
        by_close.setdefault(move_report["close_signal_id"], []).append(move_report)
    rows: list[LiveControlAuditModel] = []
    for close_signal_id, moves in by_close.items():
        close = next(
            (c for c in plan["close_signals"] if str(c.id) == close_signal_id), None
        )
        rows.append(
            LiveControlAuditModel(
                id=str(uuid4()),
                actor="repair_ctrader_close_attribution",
                action=REPAIR_AUDIT_ACTION,
                target_type="live_signal",
                target_id=close_signal_id,
                succeeded=True,
                detail_json={
                    "close_signal_deleted": close_signal_id
                    in set(report["deleted_signals"]),
                    "deployment_id": str(close.deployment_id) if close else "",
                    "moves": moves,
                    "refusals": [
                        r
                        for r in plan["refusals"]
                        if r.get("close_signal_id") == close_signal_id
                    ],
                },
            )
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repair. Without it the script only reports (default).",
    )
    args = parser.parse_args()
    report = repair_ctrader_close_attribution(apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

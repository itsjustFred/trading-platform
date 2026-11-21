"""Broker order id conflicts: the health read and the halt that keeps them halted (#126).

A duplicate live signal that carries a **different non-empty** ``broker_order_id``
than the stored one means a broker order may exist that the ledger does not track,
which can leave an unmanaged open position. The conflict is recorded on the signal
(``metadata_json["conflicting_broker_order_ids"]``) and the affected deployment is
paused where the conflict is detected
(:func:`shared.database.save_live_signal`).

This module is what makes that fail closed **and stays closed**:

* :func:`check_broker_order_conflicts` is the health read. It answers, in one query,
  which deployments still carry an unresolved conflict and with which ids, so an
  operator (and any health job) sees it without reading logs.
* :func:`enforce_broker_order_conflict_halts` re-pauses every deployment that still
  carries one. A pause is a row like any other: an operator's ``ACTIVE`` write, a
  restore that undid more than it meant to, or a conflict recorded by an older build
  would otherwise silently un-halt a deployment with an untracked broker order.
  Re-asserting the halt is what the maintainer decision means by "reactivated only
  after the operator has resolved the conflict": the way out is to resolve the
  conflict, not to write a status.

Nothing here talks to a broker. Clearing a conflict is an operator action on a
resolved order id; this module only reports and refuses to forget.
"""
from __future__ import annotations

from typing import Any

from shared.database import (
    deployments_with_broker_order_conflicts,
    get_live_deployment,
    resolve_live_signal_broker_order_conflicts,
    update_live_deployment_status,
)
from shared.logging import get_logger

logger = get_logger("live-runner")

#: ``deployments_with_broker_order_conflicts`` caps in SQL; the health read wants the
#: whole fleet, not the most recent slice.
CONFLICT_SCAN_LIMIT = 10_000


def check_broker_order_conflicts() -> dict[str, Any]:
    """Return the unresolved broker order id conflicts as a health-check payload.

    ``status`` is ``ok`` or ``error``; ``deployment_ids`` names the deployments still
    carrying one; ``conflicts`` carries one entry per signal with the tracked id, the
    conflicting ids and the deployment and symbol they belong to.
    """
    grouped = deployments_with_broker_order_conflicts(limit=CONFLICT_SCAN_LIMIT)
    conflicts = [conflict for rows in grouped.values() for conflict in rows]
    return {
        "status": "error" if conflicts else "ok",
        "deployment_ids": sorted(grouped),
        "conflicts": conflicts,
    }


def enforce_broker_order_conflict_halts() -> list[dict[str, Any]]:
    """Re-pause every deployment that still carries an unresolved conflict.

    Returns one entry per deployment that had to be halted by this call, in the
    shape the caller logs. A deployment already PAUSED is left exactly as it is:
    re-writing it would move ``updated_at`` on every pass and stamp a fresh halt
    over a pause an operator set themselves.

    A status the daemon does not know (anything but ACTIVE, PAUSED or STOPPED) is
    halted like an ACTIVE one: an unknown status must not be traded from, and
    ``update_live_deployment_status`` refuses to write it at all.
    """
    halted: list[dict[str, Any]] = []
    for deployment_id, conflicts in deployments_with_broker_order_conflicts(
        limit=CONFLICT_SCAN_LIMIT
    ).items():
        if _deployment_is_paused(deployment_id):
            continue
        try:
            update_live_deployment_status(deployment_id, "PAUSED")
        except Exception as exc:
            # The halt is the safety property. Failing to write it must be loud and
            # must not stop the remaining deployments from being re-halted.
            logger.error(
                "ALERT: could not halt the deployment carrying a broker order id "
                "conflict; an untracked broker order may exist and the deployment "
                "is still trading",
                extra={
                    "data": {
                        "alert": "live_broker_order_id_conflict_halt_failed",
                        "deployment_id": deployment_id,
                        "conflicting_broker_order_ids": sorted(
                            {
                                str(conflict_id)
                                for conflict in conflicts
                                for conflict_id in conflict["conflicting_broker_order_ids"]
                            }
                        ),
                        "error": str(exc),
                    }
                },
            )
            continue
        halted.append(_halt_record(deployment_id, conflicts))
    return halted


def _deployment_is_paused(deployment_id: str) -> bool:
    """True when the deployment's row already reads PAUSED or STOPPED."""
    deployment = get_live_deployment(deployment_id)
    if deployment is None:
        # A deleted deployment has nothing left to halt; its conflict rows are still
        # reported by the health read.
        return True
    return str(deployment.status or "").strip().upper() in {"PAUSED", "STOPPED"}


def resolve_broker_order_conflicts(deployment_id: str) -> list[dict[str, Any]]:
    """Clear every recorded conflict for a deployment and return what was cleared.

    The other half of the fail-closed pair (#126): the halt is only re-asserted while
    a conflict is *recorded*, so an operator who has reconciled the conflicting broker
    order ids with the broker needs this to get out of the halt. Kept in one function so
    "resolve" cannot mean "resolve some of them": a deployment left with half a
    conflict would still be halted, and the operator would not know why.

    The caller is the viewer's resolve endpoint, which confirms and audits the action.
    Clearing here without that is how a broker order nobody tracks gets forgotten.
    """
    normalized = str(deployment_id or "").strip()
    if not normalized:
        return []
    cleared: list[dict[str, Any]] = []
    for conflict in deployments_with_broker_order_conflicts(limit=CONFLICT_SCAN_LIMIT).get(
        normalized, []
    ):
        removed = resolve_live_signal_broker_order_conflicts(str(conflict["signal_id"]))
        if removed:
            cleared.append({**conflict, "conflicting_broker_order_ids": removed})
    if cleared:
        logger.warning(
            "Broker order id conflict(s) resolved by an operator for deployment %s; "
            "the deployment can be reactivated once its positions are verified",
            normalized,
            extra={
                "data": {
                    "deployment_id": normalized,
                    "resolved": cleared,
                }
            },
        )
    return cleared


def _halt_record(deployment_id: str, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    conflicting_ids = sorted(
        {
            str(conflict_id)
            for conflict in conflicts
            for conflict_id in conflict["conflicting_broker_order_ids"]
        }
    )
    logger.error(
        "ALERT: halted the deployment carrying a broker order id conflict; an "
        "untracked broker order may exist. Entries are blocked, exits are still "
        "dispatched; resolve the conflict with the broker before reactivating",
        extra={
            "data": {
                "alert": "live_broker_order_id_conflict",
                "deployment_id": deployment_id,
                "signal_ids": [str(conflict["signal_id"]) for conflict in conflicts],
                "conflicting_broker_order_ids": conflicting_ids,
            }
        },
    )
    return {
        "deployment_id": deployment_id,
        "signal_ids": [str(conflict["signal_id"]) for conflict in conflicts],
        "conflicting_broker_order_ids": conflicting_ids,
    }

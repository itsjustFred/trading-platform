"""Unit tests for ``scripts/repair_ctrader_close_attribution.py``.

Throwaway SQLite sandboxes only; nothing here touches the production database.

The properties under test are the ones that make a ledger repair safe to run
against a live database: dry-run writes nothing, an apply moves a fill onto
the exit signal that owns its broker order id (so the dedupe key ends up
owned by the right signal and restart reconciliation stops erroring), the
ledger's entry attribution is preserved or the move is refused, anything
ambiguous is reported rather than guessed, and a second run is a no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from scripts import repair_ctrader_close_attribution as repair
from shared.database import (
    BROKER_CLOSE_PARENT_SIGNAL_KEY,
    BROKER_CLOSE_SIGNAL_SOURCE,
    LiveControlAuditModel,
    LiveExecutionModel,
    LiveSignalModel,
    get_session,
)
from shared.models import LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

DEP = "55555555-5555-4555-8555-555555555555"
DEP_OTHER = "66666666-6666-4666-8666-666666666666"
ACCOUNT = "repair-account"
T0 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
CLOSE_ORDER_ID = "4242"
DEDUPE_KEY = "ctrader:deal:9001"


@pytest.fixture
def database(isolated_db):
    db = isolated_db[0]
    seed_live_deployments(db, [DEP, DEP_OTHER], account_id=ACCOUNT)
    return db


# ─── fixtures / helpers ───────────────────────────────────────────────────────


def _persist_signal(
    database,
    *,
    deployment_id: str = DEP,
    symbol: str = "AAPL",
    action: str = "sell",
    order_id: str = "",
    source: str = "internal",
    metadata_extra: dict[str, Any] | None = None,
) -> str:
    metadata: dict[str, Any] = {
        "source": source,
        "broker_order_id": order_id,
        **(metadata_extra or {}),
    }
    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        theoretical_price=100.0,
        timestamp=T0,
        metadata=metadata,
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    return signal_id


def _persist_exit_signal(
    database,
    *,
    deployment_id: str = DEP,
    order_id: str = CLOSE_ORDER_ID,
    action: str = "sell",
    metadata_extra: dict[str, Any] | None = None,
) -> str:
    """An exit-only signal as the internal runner persists it."""
    return _persist_signal(
        database,
        deployment_id=deployment_id,
        action=action,
        order_id=order_id,
        metadata_extra={
            "signal_intent": "exit",
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            **(metadata_extra or {}),
        },
    )


def _persist_entry(database, *, deployment_id: str = DEP) -> str:
    return _persist_signal(
        database, deployment_id=deployment_id, action="buy", order_id="111"
    )


def _persist_execution(
    database,
    *,
    signal_id: str,
    dedupe_key: str = DEDUPE_KEY,
    broker_order_id: str = CLOSE_ORDER_ID,
    qty: float = 10.0,
) -> str:
    assert database.save_live_execution(
        LiveExecution(
            signal_id=UUID(signal_id),
            actual_price=95.0,
            actual_price_observed="observed",
            filled_qty=qty,
            commission=0.0,
            timestamp=T0,
            broker_order_id=broker_order_id,
            dedupe_key=dedupe_key,
        )
    )
    return dedupe_key


def _broker_close_signal(
    database,
    *,
    parent_id: str,
    deployment_id: str = DEP,
    action: str = "sell",
    symbol: str = "AAPL",
) -> str:
    """The synthetic signal ``record_live_broker_side_close_fill`` would write."""
    return _persist_signal(
        database,
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        source=BROKER_CLOSE_SIGNAL_SOURCE,
        metadata_extra={BROKER_CLOSE_PARENT_SIGNAL_KEY: parent_id},
    )


def _signal(database, signal_id: str) -> LiveSignalModel | None:
    with get_session() as session:
        return session.get(LiveSignalModel, signal_id)


def _executions_for(database, signal_id: str) -> list[LiveExecutionModel]:
    with get_session() as session:
        return list(
            session.exec(
                select(LiveExecutionModel).where(
                    LiveExecutionModel.signal_id == signal_id
                )
            ).all()
        )


def _audit_rows(database) -> list[LiveControlAuditModel]:
    with get_session() as session:
        return list(session.exec(select(LiveControlAuditModel)).all())


# ─── tests ────────────────────────────────────────────────────────────────────


def test_dry_run_reports_the_move_and_writes_nothing(database) -> None:
    entry_id = _persist_entry(database)
    exit_id = _persist_exit_signal(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=False)

    assert report["apply"] is False
    assert [m["owner_signal_id"] for m in report["moves"]] == [exit_id]
    assert report["refusals"] == []
    assert report["unmatched"] == []
    # Nothing was written.
    assert _executions_for(database, close_id)[0].signal_id == close_id
    assert _signal(database, close_id) is not None
    assert _audit_rows(database) == []


def test_apply_moves_the_fill_and_deletes_the_emptied_close_signal(database) -> None:
    entry_id = _persist_entry(database)
    exit_id = _persist_exit_signal(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["apply"] is True
    moved = _executions_for(database, exit_id)
    assert len(moved) == 1
    assert moved[0].dedupe_key == DEDUPE_KEY
    assert _executions_for(database, close_id) == []
    # Every fill moved, so the deterministic-id synthetic signal is gone: a
    # genuine broker-side close recreates it on demand.
    assert _signal(database, close_id) is None
    assert report["deleted_signals"] == [close_id]
    audits = _audit_rows(database)
    assert len(audits) == 1
    assert audits[0].action == repair.REPAIR_AUDIT_ACTION
    assert audits[0].target_id == close_id
    # The moved fill named an entry the owner did not: the repair stamps
    # ``parent_signal_id`` on the owner so the fill keeps crediting it.
    owner = _signal(database, exit_id)
    assert owner is not None
    assert (owner.metadata_json or {}).get(BROKER_CLOSE_PARENT_SIGNAL_KEY) == entry_id
    assert report["metadata_updates"] == [
        {"signal_id": exit_id, "parent_signal_id": entry_id}
    ]


def test_apply_is_idempotent(database) -> None:
    entry_id = _persist_entry(database)
    _persist_exit_signal(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    first = repair.repair_ctrader_close_attribution(apply=True)
    second = repair.repair_ctrader_close_attribution(apply=True)

    assert len(first["moves"]) == 1
    assert second["moves"] == []
    assert second["refusals"] == []
    assert second["unmatched"] == []


def test_a_genuine_broker_close_with_no_owner_is_reported_not_touched(database) -> None:
    entry_id = _persist_entry(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id, broker_order_id="9999")

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [u["reason"] for u in report["unmatched"]] == ["no_same_deployment_owner"]
    assert _executions_for(database, close_id)[0].signal_id == close_id
    assert _signal(database, close_id) is not None
    assert _audit_rows(database) == []


def test_an_execution_without_a_broker_order_id_is_unmatched(database) -> None:
    entry_id = _persist_entry(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id, broker_order_id="")

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [u["reason"] for u in report["unmatched"]] == ["empty_broker_order_id"]


def test_two_candidate_owners_on_the_deployment_is_a_refusal(database) -> None:
    entry_id = _persist_entry(database)
    _persist_exit_signal(database)
    _persist_exit_signal(database)  # same broker_order_id, same deployment
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [r["reason"] for r in report["refusals"]] == ["ambiguous_owner"]
    assert len(report["refusals"][0]["owner_signal_ids"]) == 2
    assert _executions_for(database, close_id)[0].signal_id == close_id


def test_an_owner_on_another_deployment_is_not_a_candidate(database) -> None:
    entry_id = _persist_entry(database)
    _persist_exit_signal(database, deployment_id=DEP_OTHER)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [u["reason"] for u in report["unmatched"]] == ["no_same_deployment_owner"]


def test_a_non_exit_owner_is_refused(database) -> None:
    """An entry signal that happens to carry the order id is not an exit:
    moving a close fill onto it would open quantity, not reduce it."""
    entry_id = _persist_entry(database)
    impostor_id = _persist_signal(database, action="sell", order_id=CLOSE_ORDER_ID)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [r["reason"] for r in report["refusals"]] == ["owner_not_exit_signal"]
    assert report["refusals"][0]["owner_signal_id"] == impostor_id


def test_an_action_mismatch_is_refused(database) -> None:
    entry_id = _persist_entry(database)
    # Exit marked but action 'buy' against a 'sell' synthetic close.
    _persist_exit_signal(database, action="buy")
    close_id = _broker_close_signal(database, parent_id=entry_id, action="sell")
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [r["reason"] for r in report["refusals"]] == ["action_mismatch"]


def test_a_conflicting_owner_target_is_refused(database) -> None:
    """The exit already credits a different entry: moving would re-attribute."""
    entry_id = _persist_entry(database)
    other_entry_id = str(uuid4())
    _persist_exit_signal(
        database,
        metadata_extra={"exit_target_signal_id": other_entry_id},
    )
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [r["reason"] for r in report["refusals"]] == ["attribution_conflict"]


def test_a_matching_owner_target_needs_no_metadata_stamp(database) -> None:
    entry_id = _persist_entry(database)
    exit_id = _persist_exit_signal(
        database, metadata_extra={"exit_target_signal_id": entry_id}
    )
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert [m["owner_signal_id"] for m in report["moves"]] == [exit_id]
    assert report["metadata_updates"] == []
    owner = _signal(database, exit_id)
    assert owner is not None
    assert (owner.metadata_json or {}).get("exit_target_signal_id") == entry_id
    assert BROKER_CLOSE_PARENT_SIGNAL_KEY not in (owner.metadata_json or {})


def test_an_owner_with_other_fills_is_not_stamped(database) -> None:
    """Stamping parent_signal_id would re-attribute fills the owner already
    carries, so targeted moves are refused and the owner is untouched."""
    entry_id = _persist_entry(database)
    exit_id = _persist_exit_signal(database)
    _persist_execution(
        database, signal_id=exit_id, dedupe_key="ctrader:deal:other", broker_order_id=""
    )
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert report["moves"] == []
    assert [r["reason"] for r in report["refusals"]] == ["owner_has_other_fills"]
    owner = _signal(database, exit_id)
    assert owner is not None
    assert BROKER_CLOSE_PARENT_SIGNAL_KEY not in (owner.metadata_json or {})


def test_an_unattributed_close_moves_without_a_stamp(database) -> None:
    """A synthetic close that named no parent and an owner with no target are
    both unattributed FIFO - identical on both sides, no metadata change."""
    _persist_entry(database)
    exit_id = _persist_exit_signal(database)
    close_id = _broker_close_signal(database, parent_id="")
    _persist_execution(database, signal_id=close_id)

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert [m["owner_signal_id"] for m in report["moves"]] == [exit_id]
    assert report["moves"][0]["metadata_parent"] is None
    assert report["metadata_updates"] == []
    assert len(_executions_for(database, exit_id)) == 1


def test_a_close_signal_with_a_refused_fill_is_not_deleted(database) -> None:
    """One moved fill does not free the synthetic signal while a refused fill
    still needs it as its ledger row."""
    entry_id = _persist_entry(database)
    exit_id = _persist_exit_signal(database)
    close_id = _broker_close_signal(database, parent_id=entry_id)
    _persist_execution(database, signal_id=close_id, dedupe_key="ctrader:deal:9001")
    # A second fill on the same close signal whose order id has no owner.
    _persist_execution(
        database,
        signal_id=close_id,
        dedupe_key="ctrader:deal:9002",
        broker_order_id="9999",
    )

    report = repair.repair_ctrader_close_attribution(apply=True)

    assert len(report["moves"]) == 1
    assert len(report["unmatched"]) == 1
    assert report["deleted_signals"] == []
    assert _signal(database, close_id) is not None
    assert len(_executions_for(database, close_id)) == 1
    assert len(_executions_for(database, exit_id)) == 1

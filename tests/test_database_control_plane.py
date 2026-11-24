"""Database primitives for the daemon control plane.

Throwaway SQLite sandboxes only; nothing here touches the production database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from shared.database import (
    LiveExecutionModel,
    _prune_duplicate_submission_claims,
    update_service_status,
)
from shared.models import Service, ServiceStatus, ServiceType


def test_paused_service_is_an_atomic_floor_against_a_failed_write(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(
            name="paused-bot",
            type=ServiceType.DISCORD_BOT,
            status=ServiceStatus.PAUSED,
        )
    )

    failed = update_service_status(
        str(service.id), ServiceStatus.FAILED, respect_operator_intent=True
    )
    running = update_service_status(
        str(service.id), ServiceStatus.RUNNING, respect_operator_intent=True
    )

    assert failed is not None and failed.status == ServiceStatus.PAUSED
    assert running is not None and running.status == ServiceStatus.PAUSED


def test_operator_can_resume_a_paused_service_when_establishing_intent(
    isolated_db_sandbox,
) -> None:
    """The floor is the default; the operator's own write opts out of it.

    The API resume path passes `respect_operator_intent=False` explicitly (as the
    viewer status endpoint does). Without the opt-out the floor would swallow the
    request: `PUT /api/services/{id}/status {"status": "running"}` would answer 200
    and leave the row PAUSED - a paused bot that can never be restarted.
    """
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(
            name="paused-bot",
            type=ServiceType.DISCORD_BOT,
            status=ServiceStatus.PAUSED,
        )
    )

    resumed = update_service_status(
        str(service.id), ServiceStatus.RUNNING, respect_operator_intent=False
    )

    assert resumed is not None and resumed.status == ServiceStatus.RUNNING


@pytest.mark.parametrize("operator_status", [ServiceStatus.PAUSED, ServiceStatus.STOPPED])
@pytest.mark.parametrize(
    "automated_status",
    [ServiceStatus.RUNNING, ServiceStatus.FAILED, ServiceStatus.PAUSED, ServiceStatus.STOPPED],
)
def test_the_floor_is_the_default_and_strict(
    isolated_db_sandbox, operator_status, automated_status
) -> None:
    """A caller that forgets the flag cannot move PAUSED/STOPPED to a different status.

    That includes PAUSED <-> STOPPED; writing the same status is a harmless no-op.
    """
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.RUNNING)
    )
    update_service_status(
        str(service.id), operator_status, respect_operator_intent=False
    )  # the operator sets it

    result = update_service_status(str(service.id), automated_status)

    assert result is not None and result.status == operator_status


def test_scheduler_writes_do_not_overwrite_operator_pause_or_stop(
    isolated_db_sandbox,
) -> None:
    """The scheduler's RUNNING/FAILED writes pass no flag and must lose to the operator."""
    database, _, _ = isolated_db_sandbox
    for operator_status in (ServiceStatus.PAUSED, ServiceStatus.STOPPED):
        service = database.create_service(
            Service(
                name=f"sched-{operator_status.value}",
                type=ServiceType.DISCORD_BOT,
                status=ServiceStatus.RUNNING,
            )
        )
        update_service_status(
            str(service.id), operator_status, respect_operator_intent=False
        )
        for scheduler_status in (ServiceStatus.RUNNING, ServiceStatus.FAILED):
            # Exactly the shape of the scheduler's calls: no respect_operator_intent.
            update_service_status(
                str(service.id), scheduler_status, last_activity=None
            )
            assert database.get_service(str(service.id)).status == operator_status


def test_claim_refuses_a_deployment_that_is_not_active(isolated_db_sandbox) -> None:
    """The lease is granted by the database, not by a snapshot the caller holds.

    A PAUSED deployment must not be claimable: that is the race the claim exists
    to close, and dropping the status predicate from the INSERT..SELECT would
    reopen it while every other test still passed.
    """
    database, _, _ = isolated_db_sandbox
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id="acc-1", broker="ctrader", name="demo", is_demo=True
            )
        )
        session.add(
            database.LiveDeploymentModel(
                id="dep-1",
                strategy_name="discord::ramjet",
                account_id="acc-1",
                symbols_json=["AAPL"],
                timeframe="1h",
                status="PAUSED",
            )
        )
        session.commit()

    assert database.claim_live_deployment_submission("dep-1", "token-1") is False
    assert database.list_live_deployment_submission_claims("dep-1") == []


def test_prune_keeps_the_in_flight_lease(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE live_deployment_submission_claims ("
                "token TEXT PRIMARY KEY, deployment_id TEXT, "
                "claimed_at DATETIME, expires_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_deployment_submission_claims VALUES "
                "('expired', 'dep', :old, :expired), "
                "('running', 'dep', :older, :in_flight)"
            ),
            {
                "old": now - timedelta(seconds=5),
                "expired": now - timedelta(seconds=1),
                "older": now - timedelta(seconds=20),
                "in_flight": now + timedelta(seconds=10),
            },
        )

        removed = _prune_duplicate_submission_claims(connection)
        remaining = [
            row[0]
            for row in connection.execute(
                text("SELECT token FROM live_deployment_submission_claims")
            ).fetchall()
        ]

    assert removed == 1
    assert remaining == ["running"]


def test_pending_exit_reprotections_skip_non_exit_only_signals(
    isolated_db_sandbox,
) -> None:
    """#145: only signals that are themselves exit-only qualify - a marker on
    an entry signal is inert and must never be replayed as protection."""
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    marker = {"side": "sell", "stop_price": 190.0, "take_profit_price": 210.0}
    now = datetime.now(timezone.utc)
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id="sig-exit",
                deployment_id="dep-1",
                symbol="AAPL",
                action="sell",
                theoretical_price=200.0,
                timestamp=now,
                metadata_json={
                    "risk_sizing_mode": "exit_only",
                    "signal_intent": "exit",
                    "exit_reprotection": marker,
                },
            )
        )
        session.add(
            database.LiveSignalModel(
                id="sig-entry",
                deployment_id="dep-1",
                symbol="AAPL",
                action="buy",
                theoretical_price=200.0,
                # NEWER than sig-exit on purpose: without the
                # ``signal_intent == exit`` filter this forged row would win
                # the newest-per-symbol ranking and be replayed (#350 D2).
                timestamp=now + timedelta(minutes=1),
                # Even fully forged exit fields on an entry row must not
                # qualify: ``signal_intent`` is the intent stamp every real
                # exit carries (#350 review).
                metadata_json={
                    "risk_sizing_mode": "exit_only",
                    "exit_reprotection": marker,
                },
            )
        )
        session.commit()

    pending = database.list_pending_exit_reprotections("acct-1")

    assert [row["signal_id"] for row in pending] == ["sig-exit"]
    assert pending[0]["marker"] == marker
    assert pending[0]["symbol"] == "AAPL"
    assert pending[0]["broker_order_id"] == ""



def _marker_signal_row(
    signal_id: str,
    symbol: str,
    timestamp: datetime,
    *,
    deployment_id: str = "dep-1",
):
    from shared.database import LiveSignalModel

    return LiveSignalModel(
        id=signal_id,
        deployment_id=deployment_id,
        symbol=symbol,
        action="sell",
        theoretical_price=200.0,
        timestamp=timestamp,
        metadata_json={
            "risk_sizing_mode": "exit_only",
            "signal_intent": "exit",
            "exit_reprotection": {"side": "sell", "stop_price": 190.0},
        },
    )


def test_pending_exit_reprotections_returns_newest_per_symbol(
    isolated_db_sandbox,
) -> None:
    """#350 review: 60 markers on one symbol must not starve another symbol.

    The recovery page used to be the newest 50 marker rows globally, then
    newest-per-symbol in Python - so a symbol with many partial exits pushed
    every other symbol's crash-recovery marker off the page and those
    remainders were never restored. Selection is now newest-per-symbol in SQL.
    """
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database.get_session() as session:
        for index in range(60):
            session.add(
                _marker_signal_row(
                    f"sig-aapl-{index}",
                    "AAPL",
                    base + timedelta(minutes=index),
                )
            )
        session.add(
            _marker_signal_row(
                "sig-msft", "MSFT", base - timedelta(days=1)
            )
        )
        session.commit()

    pending = database.list_pending_exit_reprotections("acct-1")

    assert {row["signal_id"] for row in pending} == {"sig-aapl-59", "sig-msft"}
    by_symbol = {row["symbol"]: row for row in pending}
    assert by_symbol["AAPL"]["signal_id"] == "sig-aapl-59"
    assert by_symbol["MSFT"]["signal_id"] == "sig-msft"


def test_pending_exit_reprotections_partition_on_normalized_symbol(
    isolated_db_sandbox,
) -> None:
    """``"aapl "`` and ``"AAPL"`` are ONE symbol for newest-per-symbol.

    Partitioning by the raw ``symbol`` column would return both rows; the
    ``upper(trim(...))`` partition keeps only the newest (#350 D4).
    """
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database.get_session() as session:
        session.add(
            _marker_signal_row("sig-aapl-old", "AAPL", base)
        )
        session.add(
            _marker_signal_row("sig-aapl-messy", " aapl ", base + timedelta(minutes=5))
        )
        session.commit()

    pending = database.list_pending_exit_reprotections("acct-1")

    assert [row["signal_id"] for row in pending] == ["sig-aapl-messy"]
    assert pending[0]["symbol"] == "AAPL"


def test_clear_superseded_exit_reprotection_markers(
    isolated_db_sandbox,
) -> None:
    """Once a symbol's newest marker resolves, its older markers are spent."""
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database.get_session() as session:
        for index in range(3):
            session.add(
                _marker_signal_row(
                    f"sig-aapl-{index}",
                    "AAPL",
                    base + timedelta(minutes=index),
                )
            )
        session.add(
            _marker_signal_row("sig-msft", "MSFT", base)
        )
        session.commit()

    cleared = database.clear_superseded_exit_reprotection_markers(
        "acct-1", "AAPL", keep_signal_id="sig-aapl-2"
    )

    assert cleared == 2
    pending = database.list_pending_exit_reprotections("acct-1")
    assert {row["signal_id"] for row in pending} == {"sig-aapl-2", "sig-msft"}


def test_earliest_live_signal_timestamp_bounds_order_scans(
    isolated_db_sandbox,
) -> None:
    """The ``after`` lower bound for ``status=all`` scans comes from the ledger."""
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id="sig-old",
                deployment_id="dep-1",
                symbol="AAPL",
                action="buy",
                theoretical_price=200.0,
                timestamp=base,
                metadata_json={},
            )
        )
        session.add(
            database.LiveSignalModel(
                id="sig-new",
                deployment_id="dep-1",
                symbol="MSFT",
                action="buy",
                theoretical_price=100.0,
                timestamp=base + timedelta(days=10),
                metadata_json={},
            )
        )
        session.commit()

    assert database.earliest_live_signal_timestamp("acct-1", {"AAPL"}) == base
    assert (
        database.earliest_live_signal_timestamp("acct-1", {"MSFT"})
        == base + timedelta(days=10)
    )
    assert database.earliest_live_signal_timestamp("acct-1", {"TSLA"}) is None
    assert database.earliest_live_signal_timestamp("acct-1", set()) is None


def _fill_row(signal_id: str, qty: float, ts: datetime) -> LiveExecutionModel:
    return LiveExecutionModel(
        id=f"exec-{signal_id}",
        signal_id=signal_id,
        actual_price=200.0,
        filled_qty=qty,
        timestamp=ts,
        broker_order_id=f"bo-{signal_id}",
    )


def test_earliest_live_open_lot_entry_timestamp_advances_with_round_trips(
    isolated_db_sandbox,
) -> None:
    """The held-leg scan bound follows OPEN lots, not all-time signals (#350).

    An old closed round trip must not pin the ``after`` bound forever: once
    its residual reaches zero only the still-open entry's timestamp counts,
    so the bound moves forward as positions close. With no open lot the
    function returns ``None`` and the caller falls back to the all-signals
    bound.
    """
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_entry = base
    old_exit = base + timedelta(hours=1)
    new_entry = base + timedelta(days=30)
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id="sig-old-entry",
                deployment_id="dep-1",
                symbol="AAPL",
                action="buy",
                theoretical_price=200.0,
                timestamp=old_entry,
                metadata_json={},
            )
        )
        session.add(
            database.LiveSignalModel(
                id="sig-old-exit",
                deployment_id="dep-1",
                symbol="AAPL",
                action="sell",
                theoretical_price=200.0,
                timestamp=old_exit,
                metadata_json={"risk_sizing_mode": "exit_only"},
            )
        )
        session.add(
            database.LiveSignalModel(
                id="sig-new-entry",
                deployment_id="dep-1",
                symbol="AAPL",
                action="buy",
                theoretical_price=200.0,
                timestamp=new_entry,
                metadata_json={},
            )
        )
        session.flush()
        session.add(_fill_row("sig-old-entry", 10.0, old_entry))
        session.add(_fill_row("sig-old-exit", 10.0, old_exit))
        session.add(_fill_row("sig-new-entry", 5.0, new_entry))
        session.commit()

    # The old round trip is flat; only the new entry still holds a lot.
    assert (
        database.earliest_live_open_lot_entry_timestamp("acct-1", {"AAPL"})
        == new_entry
    )
    # A symbol with no open lot falls back (None -> all-signals bound).
    assert (
        database.earliest_live_open_lot_entry_timestamp("acct-1", {"TSLA"})
        is None
    )


def test_earliest_live_open_lot_entry_timestamp_is_none_when_all_flat(
    isolated_db_sandbox,
) -> None:
    """Every residual closed -> ``None`` (the caller scans from all signals)."""
    from tests.conftest import seed_live_deployments

    database, _, _ = isolated_db_sandbox
    database.init_db()
    seed_live_deployments(database, ["dep-1"], account_id="acct-1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id="sig-e",
                deployment_id="dep-1",
                symbol="AAPL",
                action="buy",
                theoretical_price=200.0,
                timestamp=base,
                metadata_json={},
            )
        )
        session.add(
            database.LiveSignalModel(
                id="sig-x",
                deployment_id="dep-1",
                symbol="AAPL",
                action="sell",
                theoretical_price=200.0,
                timestamp=base + timedelta(hours=1),
                metadata_json={"exit_only": True},
            )
        )
        session.flush()
        session.add(_fill_row("sig-e", 10.0, base))
        session.add(_fill_row("sig-x", 10.0, base + timedelta(hours=1)))
        session.commit()

    assert (
        database.earliest_live_open_lot_entry_timestamp("acct-1", {"AAPL"})
        is None
    )


def test_adapter_state_roundtrip(isolated_db_sandbox) -> None:
    """The reconcile watermark persists across adapter instances."""
    database, _, _ = isolated_db_sandbox
    database.init_db()

    assert database.get_live_adapter_state("acct-1", "wm") is None
    database.set_live_adapter_state("acct-1", "wm", {"after": "2026-01-01T00:00:00+00:00"})
    assert database.get_live_adapter_state("acct-1", "wm") == {
        "after": "2026-01-01T00:00:00+00:00"
    }
    database.set_live_adapter_state("acct-1", "wm", {"after": "2026-02-01T00:00:00+00:00"})
    assert database.get_live_adapter_state("acct-1", "wm") == {
        "after": "2026-02-01T00:00:00+00:00"
    }
    assert database.get_live_adapter_state("acct-2", "wm") is None

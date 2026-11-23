"""Periodic broker reconciliation (#303): ledger vs broker, fail closed, no flap."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from live_runner.base_adapter import BrokerOrderView, ProtectiveStop
from live_runner.broker_reconciliation import (
    BrokerReconciliationManager,
    load_reconciliation_report,
)
from shared.database import (
    ENTRIES_HALT_SETTINGS_KEY,
    claim_live_deployment_submission,
    get_live_deployment,
    list_live_control_audit,
    live_deployment_entries_halted,
    release_live_deployment_submission,
    resume_live_deployment_entries_halt,
    save_live_execution,
    save_live_signal,
    set_live_deployment_entries_halt,
    update_live_deployment_status,
)
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

ACCOUNT_ID = "acct-recon"
DEPLOYMENT_ID = str(uuid4())


class FakeAdapter:
    broker_name = "fake"

    def __init__(
        self,
        *,
        positions=None,
        orders=None,
        stops=None,
        stops_supported=True,
        positions_error=None,
        orders_error=None,
    ) -> None:
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.stops = list(stops or [])
        self.stops_supported = stops_supported
        self.positions_error = positions_error
        self.orders_error = orders_error

    async def get_positions(self):
        if self.positions_error is not None:
            raise self.positions_error
        return list(self.positions)

    async def list_reconciliation_orders(self):
        if self.orders_error is not None:
            raise self.orders_error
        return list(self.orders)

    async def list_protective_stops(self):
        if not self.stops_supported:
            raise NotImplementedError("no protective stops")
        return list(self.stops)

    def position_qty_tolerance(self, symbol: str) -> float:
        return 1e-9


def _deployment(
    *, status: str = "ACTIVE", settings: dict | None = None, deployment_id: str = DEPLOYMENT_ID
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=UUID(deployment_id),
        strategy_name="recon-strategy",
        account_id=ACCOUNT_ID,
        symbols=["AAPL"],
        timeframe="1m",
        status=status,  # type: ignore[arg-type]
        settings=settings or {},
    )


def _seed_db(database, deployment_id: str = DEPLOYMENT_ID) -> None:
    seed_live_deployments(database, [deployment_id], account_id=ACCOUNT_ID)
    update_live_deployment_status(deployment_id, "ACTIVE")


def _fill(signal_id: str, *, broker_order_id: str, qty: float) -> None:
    save_live_execution(
        LiveExecution(
            signal_id=UUID(signal_id),
            actual_price=100.0,
            filled_qty=qty,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id=broker_order_id,
        )
    )


def _manager(tmp_path: Path, halted: list[str]) -> BrokerReconciliationManager:
    async def halt(deployment_id: str, detail: dict) -> None:
        halted.append(deployment_id)
        set_live_deployment_entries_halt(deployment_id, detail)

    return BrokerReconciliationManager(
        report_path=tmp_path / "report.json", halt_entries=halt
    )


def _reconcile(manager: BrokerReconciliationManager, deployments, adapters):
    return asyncio.run(manager.reconcile_once(deployments, adapters))


def test_clean_account_reconciles_and_writes_report(isolated_db, tmp_path) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[{"symbol": "AAPL", "qty": 10.0}],
        stops=[ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)],
    )
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    reports = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    report = reports[ACCOUNT_ID]
    assert report.ok is True
    assert report.findings == ()
    assert halted == []
    assert get_live_deployment(DEPLOYMENT_ID).status == "ACTIVE"
    assert ENTRIES_HALT_SETTINGS_KEY not in (get_live_deployment(DEPLOYMENT_ID).settings or {})
    payload = json.loads(manager.report_path.read_text())
    assert payload["accounts"][ACCOUNT_ID]["ok"] is True


def test_position_mismatch_halts_entries_only_after_two_consecutive_passes(
    isolated_db, tmp_path, caplog
) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[{"symbol": "AAPL", "qty": 9.0}],
        stops=[ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)],
    )
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    with caplog.at_level("WARNING"):
        first = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
        second = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert first[ACCOUNT_ID].consecutive_mismatches == 1
    assert first[ACCOUNT_ID].entries_halted_deployment_ids == ()
    assert second[ACCOUNT_ID].consecutive_mismatches == 2
    assert second[ACCOUNT_ID].entries_halted_deployment_ids == (DEPLOYMENT_ID,)
    assert halted == [DEPLOYMENT_ID]
    # The halt is entry-only: the deployment stays ACTIVE so exits, in-flight
    # orders and trailing stops keep working.
    current = get_live_deployment(DEPLOYMENT_ID)
    assert current.status == "ACTIVE"
    assert (current.settings or {}).get(ENTRIES_HALT_SETTINGS_KEY, {}).get("by") == (
        "broker_reconciliation"
    )
    assert any(
        getattr(record, "data", {}).get("alert") == "broker_reconciliation_mismatch"
        for record in caplog.records
    )
    audit_actions = {row["action"] for row in list_live_control_audit(target_id=DEPLOYMENT_ID)}
    assert "broker_reconciliation_entries_halt" in audit_actions


def test_clean_pass_resets_the_mismatch_streak(isolated_db, tmp_path) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[{"symbol": "AAPL", "qty": 9.0}],
        stops=[ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)],
    )
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    adapter.positions = [{"symbol": "AAPL", "qty": 10.0}]
    clean = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    adapter.positions = [{"symbol": "AAPL", "qty": 9.0}]
    again = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert clean[ACCOUNT_ID].ok is True
    assert clean[ACCOUNT_ID].consecutive_mismatches == 0
    assert again[ACCOUNT_ID].consecutive_mismatches == 1
    assert halted == []


def test_unreadable_broker_state_is_a_mismatch_and_halts_after_threshold(
    isolated_db, tmp_path
) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    deployment = _deployment()
    adapter = FakeAdapter(positions_error=RuntimeError("broker down"))
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    first = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    second = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert first[ACCOUNT_ID].unreadable is True
    assert second[ACCOUNT_ID].entries_halted_deployment_ids == (DEPLOYMENT_ID,)
    assert halted == [DEPLOYMENT_ID]


def test_missing_working_order_and_untracked_order_are_findings(
    isolated_db, tmp_path
) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-expected", "qty": 10.0},
        )
    )
    assert signal_id

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[],
        orders=[
            BrokerOrderView(
                order_id="ord-stray",
                client_order_id="",
                symbol="AAPL",
                side="buy",
                qty=1.0,
                order_type="limit",
                protection=False,
            )
        ],
    )
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    kinds = {finding.kind for finding in report[ACCOUNT_ID].findings}

    assert "missing_working_order" in kinds
    assert "untracked_working_order" in kinds


def test_missing_protective_stop_is_a_finding(isolated_db, tmp_path) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(positions=[{"symbol": "AAPL", "qty": 10.0}], stops=[])
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert {finding.kind for finding in report[ACCOUNT_ID].findings} == {
        "position_without_protective_stop"
    }


def test_load_reconciliation_report_reads_written_payload(tmp_path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"generated_at": "now", "accounts": {"a": {"ok": True}}}))
    payload = load_reconciliation_report(path)
    assert payload["accounts"]["a"]["ok"] is True
    assert load_reconciliation_report(tmp_path / "missing.json") == {
        "generated_at": None,
        "accounts": {},
    }


def test_reconciliation_endpoint_returns_read_only_report(viewer_client, monkeypatch) -> None:
    import viewer.main as viewer_main

    payload = {"generated_at": "now", "accounts": {ACCOUNT_ID: {"ok": True}}}
    monkeypatch.setattr(viewer_main, "load_reconciliation_report", lambda: payload)
    response = viewer_client.get("/api/live/reconciliation")
    assert response.status_code == 200
    assert response.json() == payload


def _unfilled_signal(
    deployment_id: str,
    *,
    broker_order_id: str,
    timestamp: datetime | None = None,
    symbol: str = "AAPL",
) -> str:
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=deployment_id,
            symbol=symbol,
            action="buy",
            theoretical_price=100.0,
            timestamp=timestamp or datetime.now(timezone.utc),
            metadata={"broker_order_id": broker_order_id, "qty": 10.0},
        )
    )
    return signal_id


def test_refused_delete_endpoint_preserves_the_entries_halt(isolated_db) -> None:
    """The endpoint's status rollback is not an operator resume (#303)."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    _unfilled_signal(DEPLOYMENT_ID, broker_order_id="ord-delete")
    set_live_deployment_entries_halt(
        DEPLOYMENT_ID,
        {"by": "broker_reconciliation", "at": "now", "account_id": ACCOUNT_ID},
    )

    from fastapi.testclient import TestClient

    import viewer.main as viewer_main

    response = TestClient(viewer_main.app).delete(
        f"/api/live/deployments/{DEPLOYMENT_ID}"
        f"?confirm_deployment_id={DEPLOYMENT_ID}"
    )

    assert response.status_code == 409, response.text
    deployment = get_live_deployment(DEPLOYMENT_ID)
    assert deployment is not None
    assert deployment.status == "ACTIVE"
    assert (deployment.settings or {}).get(ENTRIES_HALT_SETTINGS_KEY, {}).get(
        "by"
    ) == "broker_reconciliation"


def test_historical_unfilled_order_is_not_expected(isolated_db, tmp_path) -> None:
    """A cancelled/expired order from weeks ago must not pause a healthy account."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _unfilled_signal(DEPLOYMENT_ID, broker_order_id="ord-ancient", timestamp=old)

    deployment = _deployment()
    adapter = FakeAdapter(positions=[], orders=[])
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert report[ACCOUNT_ID].ok is True
    assert report[ACCOUNT_ID].findings == ()


def test_stopped_deployments_orders_are_not_expected(isolated_db, tmp_path) -> None:
    """Leftover unfilled orders of a STOPPED deployment were already enforced away."""
    database, _engine, _data_dir = isolated_db
    stopped_id = str(uuid4())
    _seed_db(database)
    seed_live_deployments(database, [stopped_id], account_id=ACCOUNT_ID)
    update_live_deployment_status(stopped_id, "STOPPED")
    _unfilled_signal(stopped_id, broker_order_id="ord-stopped")

    deployment = _deployment()
    adapter = FakeAdapter(positions=[], orders=[])
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert report[ACCOUNT_ID].ok is True
    assert report[ACCOUNT_ID].findings == ()


def test_expected_order_matches_a_protection_flagged_broker_order(
    isolated_db, tmp_path
) -> None:
    """An expected working order stays matched even when the adapter marks it
    protection: the flag only suppresses the *untracked* finding."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    _unfilled_signal(DEPLOYMENT_ID, broker_order_id="ord-exit")

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[],
        orders=[
            BrokerOrderView(
                order_id="ord-exit",
                client_order_id="",
                symbol="AAPL",
                side="sell",
                qty=10.0,
                order_type="market",
                protection=True,
            )
        ],
    )
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert report[ACCOUNT_ID].ok is True
    assert report[ACCOUNT_ID].findings == ()


def test_broker_read_timeout_is_an_unreadable_pass(isolated_db, tmp_path, monkeypatch) -> None:
    import live_runner.broker_reconciliation as recon

    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    monkeypatch.setattr(recon, "BROKER_READ_TIMEOUT_SECONDS", 0.05)

    class SlowAdapter(FakeAdapter):
        async def get_positions(self):
            await asyncio.sleep(5)
            return []

    deployment = _deployment()
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: SlowAdapter()})

    assert report[ACCOUNT_ID].unreadable is True
    assert "timed out" in report[ACCOUNT_ID].findings[0].detail["error"]


def test_pending_read_makes_the_next_pass_unreadable_without_restacking(
    isolated_db, tmp_path, monkeypatch
) -> None:
    """A timed-out worker still owns the account until it actually finishes."""
    import live_runner.broker_reconciliation as recon

    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    monkeypatch.setattr(recon, "BROKER_READ_TIMEOUT_SECONDS", 0.02)

    release = asyncio.Event()
    calls = 0

    class BlockingAdapter(FakeAdapter):
        async def get_positions(self):
            nonlocal calls
            calls += 1
            await release.wait()
            return []

    deployment = _deployment()
    adapter = BlockingAdapter(positions=[], orders=[])
    manager = _manager(tmp_path, [])

    async def run_passes():
        first = await manager.reconcile_once([deployment], {ACCOUNT_ID: adapter})
        second = await manager.reconcile_once([deployment], {ACCOUNT_ID: adapter})
        assert calls == 1
        assert "still in flight" in second[ACCOUNT_ID].findings[0].detail["error"]

        release.set()
        await asyncio.sleep(0.01)
        third = await manager.reconcile_once([deployment], {ACCOUNT_ID: adapter})
        return first, second, third

    first, second, third = asyncio.run(run_passes())

    assert first[ACCOUNT_ID].unreadable is True
    assert "timed out" in first[ACCOUNT_ID].findings[0].detail["error"]
    assert second[ACCOUNT_ID].unreadable is True
    assert calls == 2
    assert third[ACCOUNT_ID].ok is True


def test_comparison_failure_fails_closed_as_unreadable(isolated_db, tmp_path, monkeypatch) -> None:
    import live_runner.broker_reconciliation as recon

    database, _engine, _data_dir = isolated_db
    _seed_db(database)

    def _boom(**_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(recon, "compare_account_state", _boom)
    deployment = _deployment()
    adapter = FakeAdapter(positions=[], orders=[])
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    first = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    second = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert first[ACCOUNT_ID].unreadable is True
    assert second[ACCOUNT_ID].entries_halted_deployment_ids == (DEPLOYMENT_ID,)
    assert halted == [DEPLOYMENT_ID]


def test_accounts_that_leave_the_snapshot_lose_streak_and_report(
    isolated_db, tmp_path
) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    deployment = _deployment()
    adapter = FakeAdapter(positions=[{"symbol": "AAPL", "qty": 9.0}], stops=[])
    manager = _manager(tmp_path, [])

    _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    assert ACCOUNT_ID in manager.latest_report()["accounts"]

    # The deployment is gone (deleted/stopped): nothing may keep its streak.
    _reconcile(manager, [], {})

    assert manager.latest_report()["accounts"] == {}


def test_unrecognised_enabled_value_fails_closed_to_enabled() -> None:
    from live_runner.broker_reconciliation import reconciliation_config_for_deployment

    config = reconciliation_config_for_deployment(
        _deployment(settings={"broker_reconciliation_enabled": "flase"})
    )
    assert config.enabled is True
    disabled = reconciliation_config_for_deployment(
        _deployment(settings={"broker_reconciliation_enabled": "false"})
    )
    assert disabled.enabled is False


def test_clean_pass_releases_a_reconciler_entry_halt(isolated_db, tmp_path) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[{"symbol": "AAPL", "qty": 9.0}],
        stops=[ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)],
    )
    manager = _manager(tmp_path, [])
    _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    assert ENTRIES_HALT_SETTINGS_KEY in (get_live_deployment(DEPLOYMENT_ID).settings or {})

    adapter.positions = [{"symbol": "AAPL", "qty": 10.0}]
    clean = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert clean[ACCOUNT_ID].ok is True
    assert ENTRIES_HALT_SETTINGS_KEY not in (get_live_deployment(DEPLOYMENT_ID).settings or {})
    audit_actions = {
        row["action"] for row in list_live_control_audit(target_id=DEPLOYMENT_ID)
    }
    assert "broker_reconciliation_entries_resume" in audit_actions


def test_operator_resume_clears_the_entry_halt(isolated_db, tmp_path) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(
        DEPLOYMENT_ID, {"by": "broker_reconciliation", "at": "now", "account_id": ACCOUNT_ID}
    )
    assert ENTRIES_HALT_SETTINGS_KEY in (get_live_deployment(DEPLOYMENT_ID).settings or {})

    update_live_deployment_status(DEPLOYMENT_ID, "PAUSED")
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")

    assert ENTRIES_HALT_SETTINGS_KEY not in (get_live_deployment(DEPLOYMENT_ID).settings or {})


def test_resume_helper_refuses_a_foreign_halt_marker(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(DEPLOYMENT_ID, {"by": "operator", "reason": "manual"})

    assert resume_live_deployment_entries_halt(DEPLOYMENT_ID) is False
    assert ENTRIES_HALT_SETTINGS_KEY in (get_live_deployment(DEPLOYMENT_ID).settings or {})


def _adapter_with_order_states(states: dict[str, str]) -> FakeAdapter:
    """A FakeAdapter whose ``get_order_state`` answers from ``states``."""
    adapter = FakeAdapter(positions=[], orders=[])

    async def get_order_state(order_id: str, *, client_order_id: str = "") -> str | None:
        return states.get(order_id) or states.get(client_order_id)

    adapter.get_order_state = get_order_state  # type: ignore[attr-defined]
    return adapter


def test_terminal_expected_order_is_not_a_finding(isolated_db, tmp_path) -> None:
    """A 1-day-old expired Alpaca DAY limit must not pause a healthy account."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    _unfilled_signal(
        DEPLOYMENT_ID, broker_order_id="ord-expired", timestamp=recent
    )

    deployment = _deployment()
    adapter = _adapter_with_order_states({"ord-expired": "terminal"})
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert report[ACCOUNT_ID].ok is True
    assert report[ACCOUNT_ID].findings == ()


def test_partially_filled_ioc_with_cancelled_remainder_is_not_a_finding(
    isolated_db, tmp_path
) -> None:
    """A partial fill whose remainder the broker cancelled is terminal, not missing."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
            metadata={"broker_order_id": "ord-ioc", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-ioc", qty=4.0)

    deployment = _deployment()
    adapter = _adapter_with_order_states({"ord-ioc": "terminal"})
    # The partial fill is real at the broker: position and protection agree,
    # so the only thing that could mismatch is the missing working order.
    adapter.positions = [{"symbol": "AAPL", "qty": 4.0}]
    adapter.stops = [
        ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)
    ]
    manager = _manager(tmp_path, [])

    report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert report[ACCOUNT_ID].ok is True
    assert report[ACCOUNT_ID].findings == ()


def test_non_terminal_or_unknown_expected_order_still_finds(
    isolated_db, tmp_path
) -> None:
    """Working, unknown and unreadable lookups all keep the finding (fail closed)."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    _unfilled_signal(DEPLOYMENT_ID, broker_order_id="ord-live")

    deployment = _deployment()
    for state in ("working", None):
        adapter = _adapter_with_order_states({"ord-live": state} if state else {})
        manager = _manager(tmp_path, [])
        report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
        kinds = {f.kind for f in report[ACCOUNT_ID].findings}
        assert "missing_working_order" in kinds
        finding = next(
            f for f in report[ACCOUNT_ID].findings if f.kind == "missing_working_order"
        )
        assert finding.detail["broker_order_state"] == (state or "unknown")

    raising = FakeAdapter(positions=[], orders=[])

    async def _raise(order_id: str, *, client_order_id: str = ""):
        raise RuntimeError("broker lookup failed")

    raising.get_order_state = _raise  # type: ignore[attr-defined]

    manager = _manager(tmp_path, [])
    report = _reconcile(manager, [deployment], {ACCOUNT_ID: raising})
    assert "missing_working_order" in {
        f.kind for f in report[ACCOUNT_ID].findings
    }


def test_order_state_lookups_share_one_phase_deadline(tmp_path, monkeypatch) -> None:
    """Ten slow lookups must not serialize into a ten-minute stall (#303)."""
    import live_runner.broker_reconciliation as recon

    monkeypatch.setattr(recon, "ORDER_STATE_LOOKUP_PHASE_TIMEOUT_SECONDS", 0.07)
    monkeypatch.setattr(recon, "BROKER_READ_TIMEOUT_SECONDS", 5.0)

    calls: list[str] = []

    class SlowLookupAdapter(FakeAdapter):
        async def get_order_state(self, order_id: str, *, client_order_id: str = ""):
            calls.append(order_id)
            await asyncio.sleep(0.04)
            return "terminal"

    findings = [
        recon.ReconciliationFinding(
            kind="missing_working_order",
            detail={"broker_order_id": f"ord-{index}"},
        )
        for index in range(3)
    ]
    summary: dict = {}
    pending: set[asyncio.Task] = set()
    manager = BrokerReconciliationManager(report_path=tmp_path / "report.json")

    kept = asyncio.run(
        manager._resolve_terminal_missing_orders(
            ACCOUNT_ID,
            SlowLookupAdapter(),
            findings,
            summary,
            pending,
        )
    )

    assert calls == ["ord-0", "ord-1"]
    assert len(kept) == 2
    assert kept[0].detail.get("broker_order_state") == "unknown"


def test_halt_is_written_and_audited_only_on_transition(
    isolated_db, tmp_path
) -> None:
    """Every mismatched pass keeps the halt, but writes and audits it once."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    signal_id, _ = save_live_signal(
        LiveSignal(
            id=uuid4(),
            deployment_id=DEPLOYMENT_ID,
            symbol="AAPL",
            action="buy",
            theoretical_price=100.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"broker_order_id": "ord-1", "qty": 10.0},
        )
    )
    _fill(signal_id, broker_order_id="ord-1", qty=10.0)

    deployment = _deployment()
    adapter = FakeAdapter(
        positions=[{"symbol": "AAPL", "qty": 9.0}],
        stops=[ProtectiveStop(order_id="stop-1", symbol="AAPL", side="sell", stop_price=95.0)],
    )
    halted: list[str] = []
    manager = _manager(tmp_path, halted)

    reports = [
        _reconcile(manager, [deployment], {ACCOUNT_ID: adapter}) for _ in range(4)
    ]

    # The halt write ran once (first pass over the threshold); later passes
    # still report the deployment as halted without rewriting the marker.
    assert halted == [DEPLOYMENT_ID]
    for report in reports[1:]:
        assert report[ACCOUNT_ID].entries_halted_deployment_ids == (DEPLOYMENT_ID,)
    halt_audits = [
        row
        for row in list_live_control_audit(target_id=DEPLOYMENT_ID)
        if row["action"] == "broker_reconciliation_entries_halt"
    ]
    assert len(halt_audits) == 1


def test_failed_resume_is_surfaced_instead_of_ok(
    isolated_db, tmp_path, monkeypatch, caplog
) -> None:
    """A resume that fails must not let the report claim a clean pass."""
    import live_runner.broker_reconciliation as recon

    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(
        DEPLOYMENT_ID,
        {"by": "broker_reconciliation", "at": "earlier", "account_id": ACCOUNT_ID},
    )

    def _boom(deployment_id: str) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(recon, "resume_live_deployment_entries_halt", _boom)

    deployment = _deployment()
    adapter = FakeAdapter(positions=[], orders=[])
    manager = _manager(tmp_path, [])

    with caplog.at_level("ERROR"):
        report = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    result = report[ACCOUNT_ID]
    assert result.ok is False
    assert result.resume_failed_deployment_ids == (DEPLOYMENT_ID,)
    assert result.findings == ()
    assert any(
        getattr(record, "data", {}).get("alert") == "broker_reconciliation_resume_failed"
        for record in caplog.records
    )
    # The halt itself is still in force: the failed resume changed nothing.
    assert ENTRIES_HALT_SETTINGS_KEY in (get_live_deployment(DEPLOYMENT_ID).settings or {})


def test_submission_claim_gates_entries_on_the_halt(isolated_db) -> None:
    """The claim re-checks the halt atomically; exits are never gated by it."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(
        DEPLOYMENT_ID, {"by": "broker_reconciliation", "at": "now"}
    )

    assert (
        claim_live_deployment_submission(
            DEPLOYMENT_ID, "tok-entry", refuse_entries_halt=True
        )
        is False
    )
    # The same deployment still claims for an exit: the halt is entry-only.
    assert (
        claim_live_deployment_submission(
            DEPLOYMENT_ID, "tok-exit", refuse_entries_halt=False
        )
        is True
    )
    release_live_deployment_submission("tok-exit")

    resume_live_deployment_entries_halt(DEPLOYMENT_ID)
    assert (
        claim_live_deployment_submission(
            DEPLOYMENT_ID, "tok-entry-2", refuse_entries_halt=True
        )
        is True
    )
    release_live_deployment_submission("tok-entry-2")


def test_empty_entries_halt_marker_fails_closed_everywhere(isolated_db) -> None:
    """An empty JSON marker must not split the reader and the claim gate."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(DEPLOYMENT_ID, {})

    assert live_deployment_entries_halted(DEPLOYMENT_ID) is True
    assert (
        claim_live_deployment_submission(
            DEPLOYMENT_ID, "tok-empty-halt", refuse_entries_halt=True
        )
        is False
    )
    assert (
        claim_live_deployment_submission(
            DEPLOYMENT_ID, "tok-empty-halt-exit", refuse_entries_halt=False
        )
        is True
    )
    release_live_deployment_submission("tok-empty-halt-exit")


def test_status_restore_without_release_keeps_the_entries_halt(
    isolated_db,
) -> None:
    """A refused delete's status rollback is not an operator resume."""
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    set_live_deployment_entries_halt(
        DEPLOYMENT_ID, {"by": "broker_reconciliation", "at": "now"}
    )

    # Mirrors the delete endpoint: PAUSED gate, then ACTIVE restore with
    # release_entries_halt=False because the delete was refused.
    update_live_deployment_status(DEPLOYMENT_ID, "PAUSED", release_entries_halt=False)
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE", release_entries_halt=False)

    assert ENTRIES_HALT_SETTINGS_KEY in (get_live_deployment(DEPLOYMENT_ID).settings or {})

    # The explicit operator resume still releases it.
    update_live_deployment_status(DEPLOYMENT_ID, "PAUSED")
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    assert ENTRIES_HALT_SETTINGS_KEY not in (get_live_deployment(DEPLOYMENT_ID).settings or {})


def test_validate_reconciliation_settings_accepts_good_values() -> None:
    from live_runner.broker_reconciliation import validate_broker_reconciliation_settings

    validate_broker_reconciliation_settings(
        {
            "broker_reconciliation_interval_seconds": 600,
            "broker_reconciliation_consecutive_mismatches": 3,
            "broker_reconciliation_enabled": "false",
            "broker_reconciliation_qty_tolerance": 0.5,
        }
    )
    validate_broker_reconciliation_settings({})
    validate_broker_reconciliation_settings(None)


@pytest.mark.parametrize(
    "settings",
    [
        {"broker_reconciliation_interval_seconds": 5},
        {"broker_reconciliation_interval_seconds": "soon"},
        {"broker_reconciliation_consecutive_mismatches": 0},
        {"broker_reconciliation_enabled": "flase"},
        {"broker_reconciliation_qty_tolerance": "wide"},
        {"broker_reconciliation_qty_tolerance": 2.0},
    ],
)
def test_validate_reconciliation_settings_refuses_bad_values(settings) -> None:
    from live_runner.broker_reconciliation import validate_broker_reconciliation_settings

    with pytest.raises(ValueError, match="broker_reconciliation"):
        validate_broker_reconciliation_settings(settings)


def test_create_live_deployment_refuses_bad_reconciliation_settings(isolated_db) -> None:
    from shared.database import create_live_deployment

    with pytest.raises(ValueError, match="broker_reconciliation"):
        create_live_deployment(
            _deployment(settings={"broker_reconciliation_enabled": "flase"})
        )


def test_explicit_tolerance_never_exceeds_adapter_precision() -> None:
    from live_runner.broker_reconciliation import _position_qty_tolerance

    class PrecisionAdapter(FakeAdapter):
        def position_qty_tolerance(self, symbol: str) -> float:
            return 0.01

    class NoPrecisionAdapter(FakeAdapter):
        position_qty_tolerance = None  # type: ignore[assignment]

    adapter = PrecisionAdapter()
    # Loose explicit tolerance is reduced to what the adapter can represent.
    assert _position_qty_tolerance(adapter, "AAPL", 0.5) == pytest.approx(0.01)
    # A tighter explicit tolerance is kept.
    assert _position_qty_tolerance(adapter, "AAPL", 1e-12) == pytest.approx(1e-12)
    # No explicit tolerance still means adapter precision.
    assert _position_qty_tolerance(adapter, "AAPL", None) == pytest.approx(0.01)
    # An adapter that cannot report precision keeps honouring the setting.
    assert _position_qty_tolerance(NoPrecisionAdapter(), "AAPL", 0.5) == pytest.approx(0.5)
    assert _position_qty_tolerance(NoPrecisionAdapter(), "AAPL", None) == pytest.approx(1e-9)


def test_account_tolerance_includes_an_unset_deployments_adapter_default() -> None:
    """A loose explicit tolerance must not widen a deployment using defaults."""
    from live_runner.broker_reconciliation import (
        _position_qty_tolerance,
        effective_reconciliation_config,
    )
    from live_runner.exit_signals import DEFAULT_QTY_TOLERANCE

    explicit = _deployment(
        settings={"broker_reconciliation_qty_tolerance": 0.5},
        deployment_id=str(uuid4()),
    )
    unset = _deployment(settings={}, deployment_id=str(uuid4()))

    config = effective_reconciliation_config([explicit, unset], ACCOUNT_ID)

    assert config is not None
    assert config.qty_tolerance == pytest.approx(0.5)
    assert config.qty_tolerance_includes_adapter_default is True

    class NoPrecisionAdapter(FakeAdapter):
        def position_qty_tolerance(self, symbol: str) -> float:
            raise RuntimeError("precision unavailable")

    assert _position_qty_tolerance(
        NoPrecisionAdapter(),
        "AAPL",
        config.qty_tolerance,
        include_adapter_default=config.qty_tolerance_includes_adapter_default,
    ) == pytest.approx(DEFAULT_QTY_TOLERANCE)


_HALT_MARKER_CASES = [
    (None, False),
    (False, False),
    (0, False),
    (0.0, False),
    ("", False),
    (True, True),
    (1, True),
    (-1, True),
    (0.5, True),
    (float("nan"), True),
    ("x", True),
    ("0", True),
    ({}, True),
    ([], True),
    ({"halted_at": "2026-11-22T00:00:00Z", "reason": "mismatch"}, True),
]


@pytest.mark.parametrize(("value", "halted"), _HALT_MARKER_CASES)
def test_entries_halt_marker_truth_table(value, halted):
    from shared.database import entries_halt_marker_is_halted

    assert entries_halt_marker_is_halted(value) is halted


@pytest.mark.parametrize(
    ("value", "halted"),
    [
        case
        for case in _HALT_MARKER_CASES
        if not (isinstance(case[0], float) and case[0] != case[0])  # JSON has no NaN
    ],
)
def test_entries_halt_marker_mirrors_the_claim_sql_predicate(value, halted):
    """The Python helper and the atomic claim's SQL must agree on every JSON value."""
    import sqlite3

    from shared.database import ENTRIES_HALT_SETTINGS_KEY

    settings = json.dumps({ENTRIES_HALT_SETTINGS_KEY: value})
    with sqlite3.connect(":memory:") as conn:
        (not_halted,) = conn.execute(
            "SELECT COALESCE(json_extract(?, "
            f"'$.{ENTRIES_HALT_SETTINGS_KEY}'), 0) IN (0, '')",
            (settings,),
        ).fetchone()
    assert (not not_halted) is halted

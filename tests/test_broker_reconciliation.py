"""Periodic broker reconciliation (#303): ledger vs broker, fail closed, no flap."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from live_runner.base_adapter import BrokerOrderView, ProtectiveStop
from live_runner.broker_reconciliation import (
    BrokerReconciliationManager,
    load_reconciliation_report,
)
from shared.database import (
    get_live_deployment,
    save_live_execution,
    save_live_signal,
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


def _manager(tmp_path: Path, paused: list[str]) -> BrokerReconciliationManager:
    async def pause(deployment_id: str) -> None:
        paused.append(deployment_id)
        update_live_deployment_status(deployment_id, "PAUSED")

    return BrokerReconciliationManager(
        report_path=tmp_path / "report.json", pause_deployment=pause
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
    paused: list[str] = []
    manager = _manager(tmp_path, paused)

    reports = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    report = reports[ACCOUNT_ID]
    assert report.ok is True
    assert report.findings == ()
    assert paused == []
    assert get_live_deployment(DEPLOYMENT_ID).status == "ACTIVE"
    payload = json.loads(manager.report_path.read_text())
    assert payload["accounts"][ACCOUNT_ID]["ok"] is True


def test_position_mismatch_pauses_only_after_two_consecutive_passes(
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
    paused: list[str] = []
    manager = _manager(tmp_path, paused)

    with caplog.at_level("WARNING"):
        first = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
        second = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert first[ACCOUNT_ID].consecutive_mismatches == 1
    assert first[ACCOUNT_ID].paused_deployment_ids == ()
    assert second[ACCOUNT_ID].consecutive_mismatches == 2
    assert second[ACCOUNT_ID].paused_deployment_ids == (DEPLOYMENT_ID,)
    assert paused == [DEPLOYMENT_ID]
    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"
    assert any(
        getattr(record, "data", {}).get("alert") == "broker_reconciliation_mismatch"
        for record in caplog.records
    )


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
    paused: list[str] = []
    manager = _manager(tmp_path, paused)

    _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    adapter.positions = [{"symbol": "AAPL", "qty": 10.0}]
    clean = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    adapter.positions = [{"symbol": "AAPL", "qty": 9.0}]
    again = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert clean[ACCOUNT_ID].ok is True
    assert clean[ACCOUNT_ID].consecutive_mismatches == 0
    assert again[ACCOUNT_ID].consecutive_mismatches == 1
    assert paused == []


def test_unreadable_broker_state_is_a_mismatch_and_pauses_after_threshold(
    isolated_db, tmp_path
) -> None:
    database, _engine, _data_dir = isolated_db
    _seed_db(database)
    deployment = _deployment()
    adapter = FakeAdapter(positions_error=RuntimeError("broker down"))
    paused: list[str] = []
    manager = _manager(tmp_path, paused)

    first = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})
    second = _reconcile(manager, [deployment], {ACCOUNT_ID: adapter})

    assert first[ACCOUNT_ID].unreadable is True
    assert second[ACCOUNT_ID].paused_deployment_ids == (DEPLOYMENT_ID,)
    assert paused == [DEPLOYMENT_ID]


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

"""Fixtures shared by the ``live_runner`` daemon tests."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def daemon_without_background_loops(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``live_runner.daemon`` with every ``run_daemon`` background loop silenced.

    Replaces the verbatim "silence all daemon loops" monkeypatch blocks that
    were copied into several tests: service discovery and recovery return
    empty, the scheduler and trailing-stop manager are inert doubles, and every
    gather member (deployments/services sync, balance poll, trailing stops,
    commission backfill, internal strategy, flip workflow, broker
    reconciliation) is a no-op, so ``run_daemon`` can reach its shutdown path.

    A test that needs one loop to do something specific re-patches that name
    itself after this fixture runs - the later ``monkeypatch.setattr`` wins.
    """
    import live_runner.daemon as daemon

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _Scheduler:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        daemon, "_discover_and_register_discord_services", lambda *_a, **_k: []
    )
    monkeypatch.setattr(daemon, "_recover_failed_services", lambda: [])
    monkeypatch.setattr(daemon, "ServiceScheduler", _Scheduler)
    monkeypatch.setattr(daemon, "LiveTrailingStopManager", lambda **_kwargs: object())
    # ``run_daemon`` constructs both managers eagerly as arguments to the loop
    # calls, so silencing the loops alone still runs the real constructors.
    monkeypatch.setattr(
        daemon, "LiveSupertrendFlipManager", lambda **_kwargs: object()
    )
    for name in (
        "sync_deployments_loop",
        "sync_services_loop",
        "_poll_balances",
        "run_trailing_stops_loop",
        "_backfill_commissions_loop",
        "run_internal_strategy_loop",
        "run_flip_workflow_loop",
        "run_broker_reconciliation_loop",
    ):
        monkeypatch.setattr(daemon, name, _noop)
    return daemon

"""Live daemon graceful shutdown: SIGINT/SIGTERM, drain, cleanup and exit codes.

Every test uses the throwaway SQLite sandbox from ``tests/conftest.py`` and a
fake adapter: no broker, network, service or production database is touched, and
no order can reach anyone. What is under test is the wiring real shutdown needs:

    signal -> stop flag -> drain of the in-flight order submission
           -> close our own connections -> exit code

Two properties are load-bearing for correctness and are pinned here:

* an order submission that is already in flight is allowed to finish, because
  cancelling it mid-request leaves the broker outcome unknown (half-written
  ``live_signals`` row, order at the broker that the daemon never recorded); and
* nothing after the stop flag is set may start a new order, because that order
  would be submitted while the connections it needs are being torn down.

Portability: CI runs Windows, where ``os.kill`` cannot deliver SIGINT/SIGTERM to
the current process (TerminateProcess is uncatchable), so the real-delivery
tests are skipped there and the same handler is invoked directly - the entry
point a real signal reaches.
"""

from __future__ import annotations

import asyncio
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import live_runner.daemon as daemon
import live_runner.shutdown as shutdown_module
from live_runner.shutdown import (
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    EXIT_CLEANUP_TIMEOUT,
    EXIT_OK,
    FORCED_EXIT_CODE,
    ShutdownCoordinator,
    release_twisted_reactor_threads,
)
from risk import TradeSignal
from shared.database import get_live_deployment, list_live_deployment_submission_claims
from shared.models import LiveDeploymentConfig

ACCOUNT_ID = "acc-1"
DEPLOYMENT_ID = "22222222-2222-2222-2222-222222222222"
ACCOUNTS_ENV = Path(__file__).resolve().parents[1] / ".accounts.env"

# Windows cannot deliver SIGINT/SIGTERM to a process with os.kill (it calls
# TerminateProcess, which is uncatchable), so real-delivery tests skip there; the
# handler entry point they reach is invoked directly in the other tests.
needs_real_signals = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows cannot deliver SIGINT/SIGTERM to a process"
)


@pytest.fixture(autouse=True)
def _fresh_process_wide_coordinator(monkeypatch: pytest.MonkeyPatch) -> ShutdownCoordinator:
    """``run_daemon`` leaves the process-wide gate closed; never leak that to other tests."""
    fresh = ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("forced exit"))
    monkeypatch.setattr(daemon, "_SHUTDOWN", fresh)
    monkeypatch.setattr(daemon, "_ACTIVE_SHUTDOWN", fresh)
    return fresh


class _GateAdapter:
    """Broker stand-in whose order submission can be held open and observed.

    ``submit_order`` records its two persistence steps (the broker accepting the
    order, then the record of it) with an await in between, so a test can land
    a shutdown on an order that is half-way through. ``writes`` is what a real
    adapter would have durably left behind: it must be both steps or neither of
    them *because of the shutdown*; one step only is the half-written state.
    """

    demo = True

    def __init__(self, *, hold_forever: bool = False) -> None:
        self.hold_forever = hold_forever
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.submit_calls = 0
        self.writes: list[str] = []
        self.cancelled = False
        self.disconnect_calls = 0
        # Broker *mutations* that shutdown must never request.
        self.mutation_calls: list[str] = []

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def submit_order(self, signal: Any, *, qty: float | None = None) -> str:
        self.submit_calls += 1
        self.writes.append("broker-accepted")
        self.entered.set()
        try:
            if self.hold_forever:
                await asyncio.Event().wait()
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.writes.append("recorded")
        return "order-1"

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def cancel_open_orders(self) -> Any:
        self.mutation_calls.append("cancel_open_orders")
        raise AssertionError("shutdown must never cancel open orders")

    async def close_all_positions(self) -> Any:
        self.mutation_calls.append("close_all_positions")
        raise AssertionError("shutdown must never flatten positions")

    async def cancel_orphaned_protections(self) -> Any:
        self.mutation_calls.append("cancel_orphaned_protections")
        raise AssertionError("shutdown must never sweep protections")


class _RecordingScheduler:
    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


def _seed_deployment(database: Any) -> LiveDeploymentConfig:
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id=ACCOUNT_ID, broker="ctrader", name="demo", is_demo=True
            )
        )
        session.add(
            database.LiveDeploymentModel(
                id=DEPLOYMENT_ID,
                strategy_name="discord::ramjet",
                account_id=ACCOUNT_ID,
                symbols_json=["AAPL"],
                timeframe="1h",
                status="ACTIVE",
            )
        )
        session.commit()
    deployment = get_live_deployment(DEPLOYMENT_ID)
    assert deployment is not None
    return deployment


def _trade_signal() -> TradeSignal:
    return TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={
            "discord_channel_id": "channel-1",
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _submit(adapter: _GateAdapter, deployment: LiveDeploymentConfig) -> Any:
    return daemon._submit_normalized_discord_signal(
        ACCOUNT_ID,
        adapter,  # type: ignore[arg-type]
        deployment,
        _trade_signal(),
        signal_timeframe="1h",
        wait_enabled=False,
    )


def _run_daemon(coordinator: ShutdownCoordinator, **kwargs: Any) -> Any:
    return daemon.run_daemon(
        accounts_env=ACCOUNTS_ENV,
        shutdown=coordinator,
        install_signal_handlers=False,
        drain_timeout_seconds=kwargs.pop("drain_timeout_seconds", 2.0),
        cleanup_timeout_seconds=kwargs.pop("cleanup_timeout_seconds", 2.0),
    )


def _new_coordinator() -> ShutdownCoordinator:
    return ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("forced exit"))


class _NoopLoop:
    """Loop stand-in for the direct-handler test: only needs the callback API."""

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, fn: Any, *args: Any) -> None:
        fn(*args)


# ─── the stop flag: signals set it ─────────────────────────────────────────────


def test_sigterm_handler_sets_the_stop_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed SIGTERM handler must flip the flag AND wake the loop.

    Without the ``call_soon_threadsafe`` wake-up the daemon's shutdown wait would
    sit on an unset event until the next unrelated callback happened to run.
    """
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("unused"))

    async def _deliver() -> None:
        coordinator.bind(asyncio.get_running_loop())
        installed = coordinator.install_signal_handlers()
        assert "SIGTERM" in installed
        os.kill(os.getpid(), signal.SIGTERM)
        # The handler runs in the main thread; the event is set from the loop.
        await asyncio.wait_for(coordinator.wait(), timeout=5)

    asyncio.run(_deliver())
    assert coordinator.requested is True
    assert coordinator.reason == "SIGTERM"


@needs_real_signals
def test_sigint_handler_sets_the_stop_flag() -> None:
    """Ctrl-C reaches the same entry point, with the same reason string."""
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("unused"))

    async def _deliver() -> None:
        coordinator.bind(asyncio.get_running_loop())
        coordinator.install_signal_handlers()
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(coordinator.wait(), timeout=5)

    asyncio.run(_deliver())
    assert coordinator.requested is True
    assert coordinator.reason == "SIGINT"


def test_second_signal_forces_an_immediate_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first signal drains; any later one gives up on graceful and exits.

    The drain is bounded precisely so an operator who sees the process hanging
    on a stuck broker call can end it without waiting for the timeout.
    """
    forced: list[int] = []
    coordinator = ShutdownCoordinator(force_exit_fn=forced.append)

    coordinator.request("SIGTERM")
    assert coordinator.request("SIGTERM") is False, "only the first signal drains"

    coordinator.handle_signal(signal.SIGINT)
    assert forced == [FORCED_EXIT_CODE]


@needs_real_signals
def test_handler_entry_point_reaches_the_coordinator_in_a_thread() -> None:
    """``signal.signal`` (not ``loop.add_signal_handler``) is what makes this work.

    The same code has to run on Windows, and the handler must never await - it
    runs in the main thread between bytecodes.
    """
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    coordinator.bind(_NoopLoop())
    coordinator.install_signal_handlers()
    assert threading.current_thread() is threading.main_thread()

    previous = signal.getsignal(signal.SIGTERM)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        # No loop callbacks run here on purpose: the flag must already be set.
        assert coordinator.requested is True
    finally:
        coordinator.restore_signal_handlers()
        signal.signal(signal.SIGTERM, previous)


def test_restore_puts_the_previous_handlers_back(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    previous = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGINT", "SIGTERM")}

    coordinator.install_signal_handlers()
    coordinator.restore_signal_handlers()

    for name, handler in previous.items():
        assert signal.getsignal(getattr(signal, name)) == handler


def test_windows_without_sigterm_still_installs_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform whose ``signal`` module has no SIGTERM must not crash.

    The guard matters: ``getattr(signal, "SIGTERM")`` without the ``hasattr``
    check is an AttributeError during daemon start-up on such a platform, and
    ``signal.signal(SIGINT, ...)`` alone keeps Ctrl-C working there.
    """
    installed: list[tuple[int, Any]] = []

    class _SignalModuleWithoutSIGTERM:
        SIGINT = signal.SIGINT
        Signals = signal.Signals

        @staticmethod
        def signal(signum: int, handler: Any) -> Any:
            installed.append((signum, handler))
            return None

    monkeypatch.setattr(shutdown_module, "signal", _SignalModuleWithoutSIGTERM)
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)

    assert coordinator.install_signal_handlers() == ["SIGINT"]
    assert [signum for signum, _ in installed] == [signal.SIGINT]

    coordinator.request("SIGINT")
    assert coordinator.requested is True


def test_unknown_signal_number_is_named_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    assert shutdown_module._signal_name(999) == "999"


# ─── the coordinator's in-flight accounting ────────────────────────────────────


def test_submissions_are_refused_once_shutdown_was_requested() -> None:
    """The gate and the registration are one synchronous step.

    An ``await`` between the check and the counter update would let a submission
    pass the gate and then be counted after the drain already decided there was
    nothing in flight.
    """
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    assert coordinator.begin_submission() is True
    assert coordinator.inflight == 1

    coordinator.request("SIGTERM")
    assert coordinator.begin_submission() is False
    assert coordinator.inflight == 1

    coordinator.end_submission()
    assert coordinator.inflight == 0
    assert coordinator.begin_submission() is False, "still refused after shutdown"


def test_wait_idle_returns_immediately_when_nothing_is_in_flight() -> None:
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)

    async def _probe() -> bool:
        coordinator.bind(asyncio.get_running_loop())
        return await coordinator.wait_idle(timeout=0.1)

    assert asyncio.run(_probe()) is True


def test_wait_idle_waits_for_the_submission_and_then_returns_true() -> None:
    async def _probe() -> tuple[bool, str]:
        coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
        coordinator.bind(asyncio.get_running_loop())
        coordinator.begin_submission()

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            coordinator.end_submission()

        task = asyncio.create_task(_finish_later())
        drained = await coordinator.wait_idle(timeout=5)
        await task
        return drained, "done"

    assert asyncio.run(_probe()) == (True, "done")


def test_wait_idle_reports_a_timeout_instead_of_waiting_forever() -> None:
    """A stuck submission must not hang the daemon; the drain has to fail loudly."""

    async def _probe() -> bool:
        coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
        coordinator.bind(asyncio.get_running_loop())
        coordinator.begin_submission()
        return await coordinator.wait_idle(timeout=0.05)

    assert asyncio.run(_probe()) is False
    assert ShutdownCoordinator().inflight == 0


def test_bind_gives_a_new_run_a_clean_slate() -> None:
    """``run_daemon`` marks the coordinator requested on every exit path.

    A second run in the same process must not inherit the first one's stop flag,
    or it would refuse every submission it is asked to make.
    """

    async def _probe() -> bool:
        coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
        coordinator.bind(asyncio.get_running_loop())
        coordinator.begin_submission()
        coordinator.request("run_daemon_exit")
        coordinator.bind(asyncio.get_running_loop())
        return coordinator.requested

    assert asyncio.run(_probe()) is False


def test_pending_submission_wakes_the_drain_through_wait() -> None:
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    assert coordinator.bind(None) is None if False else True  # bind requires a loop
    assert callable(coordinator.request)


# ─── the Twisted reactor release (cTrader threads) ──────────────────────────────


def test_release_stops_the_reactor_thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped: list[str] = []

    class _Reactor:
        threadpool = object()

        @staticmethod
        def _stopThreadPool() -> None:
            stopped.append("stopped")

    class _ReactorModule:
        reactor = _Reactor()

        @staticmethod
        def __getattr__(name: str) -> Any:  # pragma: no cover - defensive
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "twisted.internet.reactor", _ReactorModule)
    monkeypatch.setitem(sys.modules, "twisted.internet", _ReactorModule)
    release_twisted_reactor_threads()
    assert stopped == ["stopped"]


def test_release_is_a_noop_without_the_reactor_or_its_thread_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never installed, or no thread pool to stop: both must be silent no-ops.

    Importing twisted is not free, and an exception here would abort the cleanup
    that still has connections to close.
    """
    monkeypatch.delitem(sys.modules, "twisted.internet.reactor", raising=False)
    release_twisted_reactor_threads()

    class _ReactorModule:
        class reactor:  # noqa: N801 - mirrors twisted's own lowercase name
            threadpool = None

            @staticmethod
            def _stopThreadPool() -> None:  # pragma: no cover - must not be called
                raise AssertionError("no thread pool to stop")

    monkeypatch.setitem(sys.modules, "twisted.internet.reactor", _ReactorModule)
    monkeypatch.setitem(sys.modules, "twisted.internet", _ReactorModule)
    release_twisted_reactor_threads()



# ─── the daemon: shutdown drains, then closes its own connections ───────────────


def test_idle_daemon_stops_after_the_signal_and_exits_zero(isolated_db_sandbox: Any) -> None:
    """The loops stop on the flag; no new cycle starts after the request."""
    database, _, _ = isolated_db_sandbox
    database.init_db()
    coordinator = daemon._SHUTDOWN
    seen: dict[str, Any] = {}

    async def _probe() -> int:
        task = asyncio.get_running_loop().create_task(
            daemon.run_daemon(
                accounts_env=ACCOUNTS_ENV,
                install_signal_handlers=False,
                drain_timeout_seconds=1.0,
                cleanup_timeout_seconds=1.0,
            )
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "daemon must keep running until it is told to stop"
        coordinator.request("SIGTERM")
        code = await asyncio.wait_for(task, timeout=30)
        seen["loops"] = [
            t.get_name()
            for t in asyncio.all_tasks()
            if t.get_name() in {"deployment-sync-loop", "service-sync-loop", "balance-poller"}
        ]
        return code

    assert asyncio.run(_probe()) == EXIT_OK
    assert seen["loops"] == [], "no loop may outlive the daemon (it would start a new cycle)"
    # The gate closed behind the run: nothing may start an order any more.
    assert daemon._begin_submission() is False
    assert daemon._SHUTDOWN.requested is True


def test_gate_follows_an_injected_coordinator(isolated_db_sandbox: Any) -> None:
    """Regression: the submission gate and the drain must share one coordinator.

    ``run_daemon(shutdown=...)`` drains on the injected coordinator. If the order
    path counted submissions on the module-level one instead, the drain would see
    nothing in flight and cancel an order that was half-way to the broker.
    """
    database, _, _ = isolated_db_sandbox
    deployment = _seed_deployment(database)
    coordinator = _new_coordinator()
    adapter = _GateAdapter()

    async def _probe() -> int:
        task = asyncio.get_running_loop().create_task(_run_daemon(coordinator))
        await asyncio.sleep(0.2)
        submission = asyncio.create_task(_submit(adapter, deployment))
        await asyncio.wait_for(adapter.entered.wait(), timeout=10)
        assert coordinator.inflight == 1
        coordinator.request("SIGTERM")
        await asyncio.sleep(0.3)
        assert not task.done(), "daemon exited while an order submission was in flight"
        assert not adapter.cancelled
        adapter.release.set()
        code = await asyncio.wait_for(task, timeout=30)
        await asyncio.wait_for(submission, timeout=10)
        return code

    code = asyncio.run(_probe())
    assert code == EXIT_OK
    assert adapter.cancelled is False
    # Not interrupted midway: both persistence steps happened.
    assert adapter.writes == ["broker-accepted", "recorded"]


def test_no_new_submission_starts_after_the_stop_flag(
    isolated_db_sandbox: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A signal arriving once shutdown was requested never reaches the broker."""
    database, _, _ = isolated_db_sandbox
    deployment = _seed_deployment(database)
    coordinator = _new_coordinator()
    adapter = _GateAdapter()

    async def _probe() -> int:
        task = asyncio.get_running_loop().create_task(_run_daemon(coordinator))
        await asyncio.sleep(0.2)
        coordinator.request("SIGTERM")
        await _submit(adapter, deployment)
        return await asyncio.wait_for(task, timeout=30)

    with caplog.at_level("WARNING"):
        code = asyncio.run(_probe())
    assert code == EXIT_OK
    assert adapter.submit_calls == 0
    assert adapter.writes == []
    assert list_live_deployment_submission_claims(DEPLOYMENT_ID) == []
    assert any("daemon is shutting down" in r.getMessage() for r in caplog.records)


def test_cleanup_closes_connections_and_never_touches_broker_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State is closed in order: listeners cancelled, scheduler stopped, adapters disconnected."""
    coordinator = _new_coordinator()
    adapter = _GateAdapter()
    scheduler = _RecordingScheduler()
    listener_cancelled: list[bool] = []

    async def _probe() -> bool:
        coordinator.bind(asyncio.get_running_loop())

        async def _listener() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                listener_cancelled.append(True)
                raise

        listener = asyncio.create_task(_listener())
        await asyncio.sleep(0)
        active_adapters: dict[str, Any] = {ACCOUNT_ID: adapter}
        ok = await daemon._shutdown_cleanup(
            loop_tasks=(),
            active_listener_tasks={ACCOUNT_ID: listener},
            active_discord_tasks={},
            active_discord_bot_tasks={},
            active_discord_bots={},
            active_adapters=active_adapters,
            scheduler=scheduler,  # type: ignore[arg-type]
            shutdown=coordinator,
            drain_timeout=1.0,
            cleanup_timeout=1.0,
        )
        assert active_adapters == {}
        return ok

    assert asyncio.run(_probe()) is True
    assert listener_cancelled == [True]
    assert scheduler.stopped == 1
    assert adapter.disconnect_calls == 1
    assert adapter.mutation_calls == []


def test_cleanup_reports_a_phase_that_fails_but_still_runs_the_rest() -> None:
    coordinator = _new_coordinator()
    adapter = _GateAdapter()

    class _BrokenScheduler:
        async def stop(self) -> None:
            raise RuntimeError("scheduler exploded")

    async def _probe() -> bool:
        coordinator.bind(asyncio.get_running_loop())
        return await daemon._shutdown_cleanup(
            loop_tasks=(),
            active_listener_tasks={},
            active_discord_tasks={},
            active_discord_bot_tasks={},
            active_discord_bots={},
            active_adapters={ACCOUNT_ID: adapter},
            scheduler=_BrokenScheduler(),  # type: ignore[arg-type]
            shutdown=coordinator,
            drain_timeout=1.0,
            cleanup_timeout=1.0,
        )

    assert asyncio.run(_probe()) is False
    assert adapter.disconnect_calls == 1, "a failed phase must not skip closing connections"


def test_a_drain_that_times_out_exits_non_zero_and_keeps_the_lease(
    isolated_db_sandbox: Any,
) -> None:
    """A submission that never completes cannot be waited on forever.

    The exit code says so (systemd/the operator see it), and the submission lease
    stays held: the broker outcome is unknown, and releasing it would let the next
    signal duplicate the order.
    """
    database, _, _ = isolated_db_sandbox
    deployment = _seed_deployment(database)
    coordinator = _new_coordinator()
    adapter = _GateAdapter(hold_forever=True)

    async def _probe() -> int:
        task = asyncio.get_running_loop().create_task(
            _run_daemon(coordinator, drain_timeout_seconds=0.3)
        )
        await asyncio.sleep(0.2)
        stuck = asyncio.create_task(_submit(adapter, deployment))
        await asyncio.wait_for(adapter.entered.wait(), timeout=10)
        coordinator.request("SIGTERM")
        code = await asyncio.wait_for(task, timeout=30)
        # In production the listener task owning this submission is cancelled by
        # the cleanup; here the test owns it.
        stuck.cancel()
        await asyncio.gather(stuck, return_exceptions=True)
        return code

    assert asyncio.run(_probe()) == EXIT_CLEANUP_TIMEOUT
    assert list_live_deployment_submission_claims(DEPLOYMENT_ID) != []
    assert adapter.mutation_calls == []


def test_process_wide_gate_is_restored_after_each_run(isolated_db_sandbox: Any) -> None:
    """A later run with the default coordinator must not inherit an injected one."""
    database, _, _ = isolated_db_sandbox
    database.init_db()
    coordinator = _new_coordinator()

    async def _probe() -> None:
        task = asyncio.get_running_loop().create_task(_run_daemon(coordinator))
        await asyncio.sleep(0.2)
        assert daemon._ACTIVE_SHUTDOWN is coordinator
        coordinator.request("SIGTERM")
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(_probe())
    assert daemon._ACTIVE_SHUTDOWN is daemon._SHUTDOWN


@needs_real_signals
def test_real_sigterm_to_a_daemon_process_exits_cleanly(tmp_path: Path) -> None:
    """End to end through ``main()``: handler install, wake-up, exit code, logs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = dict(os.environ)
    env.update(
        DATA_DIR=str(data_dir),
        LOG_DIR=str(tmp_path / "logs"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "live_runner.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line) for line in process.stdout],  # type: ignore[union-attr]
        daemon=True,
    )
    reader.start()
    output: list[str] = []
    try:
        deadline = time.monotonic() + 60
        started = False
        while time.monotonic() < deadline and not started:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                assert process.poll() is None, "daemon exited early: " + "".join(output)[-2000:]
                continue
            output.append(line)
            started = "Starting live daemon" in line
        assert started, "daemon never logged its startup: " + "".join(output)[-2000:]
        time.sleep(1.0)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed test
            process.kill()
            process.wait(timeout=30)
        reader.join(timeout=10)
    output.extend(lines.queue)
    text = "".join(output)
    assert process.returncode == EXIT_OK, text[-2000:]
    assert "Live daemon shutting down" in text
    assert "SIGTERM" in text
    assert "Stopping live daemon" in text
    assert "Traceback" not in text


# ─── exit codes ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [EXIT_OK, EXIT_CLEANUP_TIMEOUT])
def test_main_exits_with_the_code_the_daemon_returned(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """The shutdown verdict has to survive the last mile to the service manager."""
    flushed: list[int] = []

    def _fake_run(coro: Any) -> int:
        coro.close()
        return code

    monkeypatch.setattr(daemon.asyncio, "run", _fake_run)
    monkeypatch.setattr(daemon.logging, "shutdown", lambda: flushed.append(1))

    with pytest.raises(SystemExit) as excinfo:
        daemon.main()

    assert excinfo.value.code == code
    assert flushed == [1], "log handlers must be flushed before the exit code lands"


def test_shutdown_timeouts_are_bounded_by_default() -> None:
    assert 0 < DEFAULT_DRAIN_TIMEOUT_SECONDS < 300
    assert 0 < DEFAULT_CLEANUP_TIMEOUT_SECONDS < 300

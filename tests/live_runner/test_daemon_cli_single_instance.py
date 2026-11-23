"""The daemon's CLI and single-instance guard (#310).

Two failures, one incident: ``python -m live_runner.daemon`` ignored every argument, so an agent's
``--help`` started a second daemon instead of printing usage, and that daemon opened the live
database and ran the schema and data migrations next to the daemon that was already running.

The tests pin the contract that prevents a repeat:

* ``--help`` and unknown arguments exit (0 / 2) **without side effects** - no database file, no
  migrations, no lock taken. The subprocess tests check the real ``python -m live_runner.daemon``
  path, because the in-process ones cannot see what a module's imports do.
* A second daemon is refused while the first holds the lock, and the refusal happens before the
  first database touch - ``backtests.db`` staying absent is the incident, pinned.
* The lock is released when the daemon exits, in this process and in a killed one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from live_runner import daemon_cli
from shared.single_instance import (
    SingleInstanceError,
    SingleInstanceLock,
    process_lifetime_locks,
)
from tests.helpers.lock_holder import await_held, read_holder_line, spawn_holder

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Any:
    """An empty per-test data dir, restored after the test (the supported isolation hook)."""
    from shared import constants

    data_dir = tmp_path / "data"
    constants.set_data_dir(data_dir)
    try:
        yield data_dir
    finally:
        constants.reset_data_dir()


def _spy_on_migrations(monkeypatch: Any) -> list[str]:
    """Fail any test whose path reaches the database migrations, and record the attempt."""
    import shared.database as database

    calls: list[str] = []

    def _spy(name: str) -> Any:
        def _record(*_args: Any, **_kwargs: Any) -> None:
            calls.append(name)
            raise AssertionError(f"{name} ran: this path must not touch the database")

        return _record

    monkeypatch.setattr(database, "init_db", _spy("init_db"))
    monkeypatch.setattr(database, "_migrate_schema", _spy("_migrate_schema"))
    monkeypatch.setattr(database.SQLModel.metadata, "create_all", _spy("metadata.create_all"))
    return calls


def _assert_nothing_was_written(data_dir: Path) -> None:
    assert not (data_dir / "backtests.db").exists(), "the live database file was created"
    assert not any(data_dir.glob("*.db")), "a database file was created under the data dir"
    assert not (data_dir / "backtests.db.lock").exists(), "the single-instance lock was taken"


def _recording_run_daemon(recorded: list[str]) -> Any:
    async def _run_daemon(**_kwargs: Any) -> None:
        recorded.append("run_daemon")

    return _run_daemon


class TestArgumentParsing:
    """``main`` must answer the CLI before anything that can touch the live database."""

    def test_help_exits_zero_and_touches_nothing(
        self, monkeypatch: Any, tmp_data_dir: Path, capsys: Any
    ) -> None:
        migration_calls = _spy_on_migrations(monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            daemon_cli.main(["--help"])

        assert excinfo.value.code == 0
        assert "usage:" in capsys.readouterr().out
        assert migration_calls == []
        _assert_nothing_was_written(tmp_data_dir)

    def test_an_unknown_argument_exits_two_and_touches_nothing(
        self, monkeypatch: Any, tmp_data_dir: Path, capsys: Any
    ) -> None:
        """``--config`` is the option an operator reaches for first; it must fail, not start."""
        migration_calls = _spy_on_migrations(monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            daemon_cli.main(["--config", "live_deployments.yaml"])

        assert excinfo.value.code == 2
        assert "usage:" in capsys.readouterr().err
        assert migration_calls == []
        _assert_nothing_was_written(tmp_data_dir)

    def test_a_valid_invocation_starts_the_daemon(self, monkeypatch: Any) -> None:
        """The other half of the contract: a plain start still runs the daemon."""
        from live_runner import daemon

        started: list[str] = []
        monkeypatch.setattr(daemon, "run_daemon", _recording_run_daemon(started))

        assert daemon_cli.main([]) == 0
        assert started == ["run_daemon"]


class TestSingleInstanceGuard:
    """The guard sits in ``run_daemon``: before the first database touch and every connect."""

    # A broken guard lets the daemon run on (the loops never end), so the refusal is pinned with a
    # short per-test timeout: a regression must fail in 30s as a timeout, not in the suite-wide 180s
    # as a hang. The passing path raises in about a second.
    @pytest.mark.timeout(30)
    def test_a_second_daemon_is_refused_while_the_first_holds_the_lock(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        from live_runner import daemon

        reached: list[str] = []
        monkeypatch.setattr(
            daemon,
            "_discover_and_register_discord_services",
            lambda **_kwargs: reached.append("discovery"),
        )
        with SingleInstanceLock(daemon.daemon_lock_path()):
            with pytest.raises(SingleInstanceError) as excinfo:
                asyncio.run(daemon.run_daemon())

        assert reached == [], "the refusal must precede the first database read"
        assert str(tmp_data_dir / "backtests.db.lock") in str(excinfo.value)

    @pytest.mark.timeout(30)
    def test_the_lock_is_released_when_the_daemon_exits(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """Release on the failure path: a crashed start must not strand the lock either."""
        from live_runner import daemon

        class _DiscoveryReached(RuntimeError):
            pass

        def _stop_at_discovery(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise _DiscoveryReached("discovery reached")

        monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _stop_at_discovery)

        with pytest.raises(_DiscoveryReached):
            asyncio.run(daemon.run_daemon())

        SingleInstanceLock(daemon.daemon_lock_path()).acquire().release()

    @pytest.mark.timeout(60)
    def test_the_lock_is_kept_when_the_worker_join_is_interrupted(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """An interrupted join must not release the lock with workers still writing.

        The process is on its way out in that state and the OS releases the lock with it; handing
        the lock on would give the next daemon exactly the overlap this guard exists to prevent.
        """
        from live_runner import daemon

        class _JoinInterrupted(BaseException):
            """Stands in for the signal that interrupts the join (a KeyboardInterrupt).

            A plain BaseException rather than KeyboardInterrupt itself: asyncio re-raises a
            KeyboardInterrupt out of ``run_forever`` instead of delivering it through the future,
            which would end the test session rather than this test. The production cleanup branches
            on ``BaseException`` either way.
            """

        class _InterruptedJoin:
            """Runs the submitted work inline, then interrupts the join - a signal mid-join."""

            def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
                future = concurrent.futures.Future()
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - relayed through the future
                    future.set_exception(exc)
                return future

            def shutdown(self, *, wait: bool) -> None:
                raise _JoinInterrupted("interrupted mid-join")

        def _stop_at_discovery(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("discovery reached")

        monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _stop_at_discovery)
        loop = asyncio.new_event_loop()
        monkeypatch.setattr(loop, "_default_executor", _InterruptedJoin(), raising=False)
        try:
            task = loop.create_task(daemon.run_daemon())
            with pytest.raises(_JoinInterrupted):
                loop.run_until_complete(task)
        finally:
            # Detach the fake before closing: loop.close() shuts the executor down again.
            loop._default_executor = None
            loop.close()

        # The lock was not released in-process: the process must die holding it, and it must be
        # anchored to the process rather than to the dying coroutine frame, task and traceback -
        # garbage collection would otherwise close the descriptor and free the OS lock while the
        # workers are still writing.
        contender = SingleInstanceLock(daemon.daemon_lock_path())
        with pytest.raises(SingleInstanceError):
            contender.acquire()
        anchored = [item.lock_path for item in process_lifetime_locks()]
        assert daemon.daemon_lock_path() in anchored, (
            "the interrupted-join lock is held only by the dying frame, task and traceback; "
            f"garbage collection would release it. anchored={anchored}"
        )

    @pytest.mark.timeout(60)
    def test_the_lock_outlives_in_flight_startup_database_work(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """A cancelled daemon drops the lock only after its own executor work has finished.

        The startup discovery runs its database reads and writes on an executor thread, and
        cancelling the daemon does not stop that thread. If the lock were released on the
        cancellation alone, a second daemon could start while the first one's thread was still
        writing - the overlap this lock exists to prevent - so the refused holder below is the
        assertion, and the lock must only be acquirable once the daemon has exited for good.
        """
        from live_runner import daemon

        started = threading.Event()
        unblock = threading.Event()
        ready = threading.Event()
        daemon_loop: list[asyncio.AbstractEventLoop] = []
        daemon_task: list[asyncio.Task[None]] = []

        def _blocking_discovery(*_args: Any, **_kwargs: Any) -> list[Any]:
            started.set()
            assert unblock.wait(10), "the test never unblocked the startup discovery"
            return []

        monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _blocking_discovery)

        def _run_daemon() -> None:
            loop = asyncio.new_event_loop()
            daemon_loop.append(loop)
            task = loop.create_task(daemon.run_daemon())
            daemon_task.append(task)
            ready.set()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        lock_path = daemon.daemon_lock_path()
        thread = threading.Thread(target=_run_daemon, name="daemon-under-test")
        probes: list[Any] = []
        thread.start()
        try:
            assert ready.wait(10), "the daemon's event loop never started"
            assert started.wait(10), "the daemon never reached the startup discovery"
            daemon_loop[0].call_soon_threadsafe(daemon_task[0].cancel)
            # While the discovery thread is still writing, a second process must be refused.
            probes.append(spawn_holder(lock_path, hold_seconds=30.0))
            assert read_holder_line(probes[-1]) == "refused", (
                "the lock was released while the daemon's own executor work was still in flight"
            )
            # A second cancellation arrives while the daemon is waiting for that work. It must not
            # abandon the wait either: the lock outlives the in-flight writes, still.
            daemon_loop[0].call_soon_threadsafe(daemon_task[0].cancel)
            probes.append(spawn_holder(lock_path, hold_seconds=30.0))
            assert read_holder_line(probes[-1]) == "refused", (
                "a second cancellation released the lock before the in-flight work finished"
            )
            unblock.set()
        finally:
            thread.join(timeout=30)
            for probe in probes:
                if probe.poll() is None:
                    probe.kill()
                    probe.wait(timeout=10)

        assert not thread.is_alive(), "the daemon thread did not exit after the cancellation"

        # The daemon exited for good: the lock it held is now free.
        after = spawn_holder(lock_path, hold_seconds=15.0)
        try:
            await_held(after)
        finally:
            after.kill()
            after.wait(timeout=10)


class _LoopsStopped(Exception):
    """Stands in for the daemon's loops ending: a crash, or a stop from outside."""


class _StreamAdapterStub:
    """A broker adapter whose listener thread never ends by itself.

    Alpaca's shape (``alpaca_adapter.py:2801``): ``listen_executions`` runs
    ``TradingStream.run`` through ``asyncio.to_thread``, so the stream occupies one executor thread
    for its whole life. Cancelling the listener task does not stop that thread - only a stream close
    does, which is what ``disconnect()`` performs.

    ``releases`` decides whether this stream closes when it is told to: a healthy one (``True``)
    proves the join finishes because the daemon disconnected it, and a stuck one (``False``) proves
    what the join does when it cannot finish.
    """

    def __init__(self, *, releases: bool) -> None:
        self._releases = releases
        self.disconnect_calls: list[str] = []
        self._released = threading.Event()

    async def listen_executions(self) -> None:
        await asyncio.to_thread(self._stream_thread_body)

    def _stream_thread_body(self) -> None:
        # Bounded so that a test which never releases the stream still leaves a thread that
        # terminates: an unbounded wait here would hold the interpreter's exit path open for good.
        assert self._released.wait(120), "the stream thread was never released"

    def disconnect(self) -> None:
        self.disconnect_calls.append("daemon_shutdown")
        if self._releases:
            self._released.set()

    def release(self) -> None:
        """Test-side teardown: let the stuck thread finish so nothing is left running."""
        self._released.set()


class _FakeScheduler:
    """Scheduler stand-in: ``start()`` lists services from the database, which this path must
    not."""

    def __init__(self) -> None:
        self.stopped = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped += 1


def _no_thread_call(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


async def _idle_loop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _stub_the_daemons_loops(monkeypatch: Any, adapter: _StreamAdapterStub) -> None:
    """Replace everything in ``_run_daemon_locked`` that would touch the database or the network.

    The one loop left real is the deployment sync: it registers ``adapter`` and starts its listener
    task, exactly as the running daemon does, then stops (raises) so the shutdown path under test
    runs. Everything else is idle, so the ``gather`` ends on the first task - the one that matters.
    """
    from live_runner import daemon

    async def _sync_loop(
        active_adapters: dict[str, Any],
        active_listener_tasks: dict[str, Any],
        active_discord_tasks: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        active_adapters["account-1"] = adapter
        active_listener_tasks["account-1"] = asyncio.create_task(
            adapter.listen_executions(), name="listener-account-1"
        )
        # Let the listener reach its to_thread before this loop stops: the shutdown path has to
        # find a thread that is already blocked inside the stream.
        await asyncio.sleep(0)
        raise _LoopsStopped("the daemon's loops stopped")

    monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _no_thread_call)
    monkeypatch.setattr(daemon, "_recover_failed_services", _no_thread_call)
    monkeypatch.setattr(daemon, "ServiceScheduler", _FakeScheduler)
    monkeypatch.setattr(daemon, "sync_deployments_loop", _sync_loop)
    monkeypatch.setattr(daemon, "sync_services_loop", _idle_loop)
    monkeypatch.setattr(daemon, "_poll_balances", _idle_loop)
    monkeypatch.setattr(daemon, "run_trailing_stops_loop", _idle_loop)
    monkeypatch.setattr(daemon, "_backfill_commissions_loop", _idle_loop)


class TestShutdownJoin:
    """What the daemon does after its loops stop: the streams it leaves, and the join it waits."""

    @pytest.mark.timeout(60)
    def test_a_stopping_daemon_disconnects_its_adapters_and_joins_its_workers(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """A cancelled listener never stops its own worker thread; only a disconnect does.

        Without the disconnect the join waits forever for a thread inside the broker stream, and
        the process stops trading while holding the single-instance lock - the state a
        ``Restart=always`` unit cannot recover from, because the process never dies.
        """
        from live_runner import daemon

        adapter = _StreamAdapterStub(releases=True)
        _stub_the_daemons_loops(monkeypatch, adapter)

        try:
            with pytest.raises(_LoopsStopped):
                asyncio.run(daemon.run_daemon())
        finally:
            # A disconnect regression leaves the stream thread blocked for its whole bound;
            # release it so the failure surfaces at once instead of after the wait.
            adapter.release()

        assert adapter.disconnect_calls == ["daemon_shutdown"], (
            "the daemon left its broker stream open, so its worker thread could not finish"
        )
        # The join finished because the thread did: the lock is handed on, and a second daemon can
        # take it in this very process.
        SingleInstanceLock(daemon.daemon_lock_path()).acquire().release()

    @pytest.mark.timeout(60)
    def test_a_worker_thread_that_never_returns_costs_the_daemon_its_join_budget(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """A stream that ignores its close must not wait for the join forever.

        The budget expires, the CRITICAL line leaves the process, the lock stays held by the dying
        process and the loop stops carrying the executor - so ``asyncio.run``'s own close (and the
        interpreter's teardown) cannot spend 300s and then join the same thread forever.
        """
        from live_runner import daemon

        adapter = _StreamAdapterStub(releases=False)
        monkeypatch.setattr(daemon, "WORKER_JOIN_TIMEOUT_SECONDS", 0.5)
        _stub_the_daemons_loops(monkeypatch, adapter)
        # The console handler's stream is bound when the logger is first created, which depends on
        # which test imported the daemon first - capture the records on the logger itself instead
        # of asserting on a particular fd.
        records: list[logging.LogRecord] = []

        class _RecordCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _RecordCapture()
        daemon.logger.addHandler(capture)
        loop = asyncio.new_event_loop()
        task = loop.create_task(daemon.run_daemon())
        try:
            with pytest.raises(daemon.WorkerJoinTimeout) as excinfo:
                loop.run_until_complete(task)
            # Assert the detach while the loop is still open: ``loop.close()`` sets
            # ``_default_executor`` to None itself, so the same assertion after close() cannot
            # fail - it passed even with the detach removed, leaving ``asyncio.run``'s own
            # executor shutdown to spend its full budget on the thread this join gave up on.
            assert loop._default_executor is None, (
                "the loop still carries the executor the join gave up on"
            )
        finally:
            daemon.logger.removeHandler(capture)
            # Release before close: a loop that still carries the executor would block in
            # ``close()`` on the stuck stream thread.
            adapter.release()
            loop.close()

        assert "still running" in str(excinfo.value)
        critical = [r for r in records if r.levelno >= logging.CRITICAL]
        assert any("giving up on its worker threads" in r.getMessage() for r in critical), (
            "an operator saw nothing about the join that never finished"
        )
        # The lock is kept, and anchored to the process rather than to the dying frame.
        contender = SingleInstanceLock(daemon.daemon_lock_path())
        with pytest.raises(SingleInstanceError):
            contender.acquire()
        anchored = [item.lock_path for item in process_lifetime_locks()]
        assert daemon.daemon_lock_path() in anchored, (
            "the expired-join lock is held only by the dying frame, task and traceback; "
            f"garbage collection would release it. anchored={anchored}"
        )


class _LeftTheProcess(BaseException):
    """Stands in for the process leaving through ``os._exit``."""


class TestExitCodes:
    """The refusal codes the daemon and a ``systemctl`` unit key on."""

    def test_a_worker_join_timeout_leaves_the_process_at_once(
        self, monkeypatch: Any, capsys: Any, tmp_path: Path
    ) -> None:
        """Exit code 4 has to arrive, and the process has to leave to arrive at all.

        Returning normally would leave the interpreter's teardown joining the abandoned worker
        threads with no deadline, which is the hang this exit code exists to end.
        """
        from live_runner import daemon

        async def _join_that_never_finishes(**_kwargs: Any) -> None:
            raise daemon.WorkerJoinTimeout(
                "the daemon's worker threads are still running after 30s"
            )

        monkeypatch.setattr(daemon, "run_daemon", _join_that_never_finishes)
        exits: list[int] = []

        def _leave_without_returning(code: int) -> None:
            """Stands in for ``os._exit``: the mark of the real one is that it never returns."""
            exits.append(int(code))
            raise _LeftTheProcess(code)

        monkeypatch.setattr(os, "_exit", _leave_without_returning)

        with pytest.raises(_LeftTheProcess):
            daemon_cli.main([])

        assert exits == [daemon_cli.EXIT_WORKER_JOIN_TIMEOUT], (
            "the process did not leave immediately after reporting the expired join"
        )
        assert "still running" in capsys.readouterr().err

    def test_the_exit_flush_survives_an_interrupt_and_still_exits(
        self, monkeypatch: Any
    ) -> None:
        """A KeyboardInterrupt inside ``handler.flush()`` must not skip ``os._exit``.

        The flush exists so the CRITICAL line reaches the journal before the process leaves;
        an interrupt landing mid-flush would otherwise drop the operator straight back into
        the unbounded interpreter teardown this exit path exists to avoid. The Sentry flush
        gets the same treatment: ``os._exit`` runs no ``atexit`` hook, so the queued event
        needs its explicit last chance.
        """
        import sentry_sdk

        exits: list[int] = []
        flushed: list[float] = []

        def _leave_without_returning(code: int) -> None:
            exits.append(int(code))
            raise _LeftTheProcess(code)

        class _InterruptingFlush(logging.Handler):
            def flush(self) -> None:
                raise KeyboardInterrupt()

        monkeypatch.setattr(os, "_exit", _leave_without_returning)
        monkeypatch.setattr(sentry_sdk, "flush", lambda timeout=0: flushed.append(timeout))
        root = logging.getLogger()
        handler = _InterruptingFlush()
        root.addHandler(handler)
        try:
            with pytest.raises(_LeftTheProcess):
                daemon_cli._leave_now(daemon_cli.EXIT_WORKER_JOIN_TIMEOUT)
        finally:
            root.removeHandler(handler)

        assert exits == [daemon_cli.EXIT_WORKER_JOIN_TIMEOUT]
        assert flushed == [2], "the queued Sentry event never got its last chance to leave"

    def test_a_keyboard_interrupt_leaves_the_process_at_once(
        self, monkeypatch: Any
    ) -> None:
        """A second Ctrl-C during the worker join must not fall into interpreter teardown.

        The daemon's cleanup has already run - or given up, with the executor detached - by the
        time the interrupt reaches the CLI, so the process leaves through ``os._exit`` with the
        conventional SIGINT code instead of letting ``concurrent.futures``' atexit hook join
        the abandoned threads with no bound.
        """
        from live_runner import daemon

        async def _interrupted_daemon(**_kwargs: Any) -> None:
            raise KeyboardInterrupt()

        monkeypatch.setattr(daemon, "run_daemon", _interrupted_daemon)
        exits: list[int] = []

        def _leave_without_returning(code: int) -> None:
            exits.append(int(code))
            raise _LeftTheProcess(code)

        monkeypatch.setattr(os, "_exit", _leave_without_returning)

        with pytest.raises(_LeftTheProcess):
            daemon_cli.main([])

        assert exits == [130]

    def test_the_exit_codes_stay_distinct(self) -> None:
        """An operator tells "start over" (3) from "its outcome is unknown" (4)."""
        assert daemon_cli.EXIT_ALREADY_RUNNING == 3
        assert daemon_cli.EXIT_WORKER_JOIN_TIMEOUT == 4


class TestLockPath:
    def test_the_lock_sits_next_to_the_live_database(self, tmp_data_dir: Path) -> None:
        from live_runner import daemon

        assert daemon.daemon_lock_path() == tmp_data_dir / "backtests.db.lock"

    def test_the_lock_follows_a_database_url(self, monkeypatch: Any, tmp_path: Path) -> None:
        """A DATABASE_URL-hosted database gets its lock beside the file the engine really opens."""
        from live_runner import daemon

        other_db = tmp_path / "elsewhere" / "live.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{other_db.as_posix()}")

        assert daemon.daemon_lock_path() == other_db.with_name("live.db.lock")

    def test_the_lock_falls_back_to_the_data_dir_when_the_url_names_no_file(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """An in-memory or non-SQLite URL has no adjacent file; DATA_DIR is the fallback."""
        from live_runner import daemon

        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/trading")

        assert daemon.daemon_lock_path() == tmp_data_dir / "backtests.db.lock"

    @pytest.mark.skipif(
        os.name == "nt", reason="creating a symlink needs elevated privileges on Windows"
    )
    def test_the_lock_resolves_a_symlinked_data_dir(self, monkeypatch: Any, tmp_path: Path) -> None:
        """Two spellings of one database must select one lock, not two.

        A deployment may symlink its data directory onto another disk. Without the resolve, the
        daemon would take its lock at the alias while the SYMLINKED database itself sits in the
        target directory.
        """
        from live_runner import daemon

        target = tmp_path / "real-data"
        target.mkdir()
        alias = tmp_path / "linked-data"
        alias.symlink_to(target, target_is_directory=True)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(alias / 'live.db').as_posix()}")

        assert daemon.daemon_lock_path() == target / "live.db.lock"

    @pytest.mark.skipif(
        os.name == "nt", reason="creating a symlink needs elevated privileges on Windows"
    )
    def test_the_lock_resolves_a_symlinked_database_file(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A symlink may rename as well as redirect: the lock takes the resolved file's name.

        ``alias.db`` -> ``live.db`` must not lock ``alias.db.lock``, or a daemon that reaches the
        database through the target would take a second lock beside the real file.
        """
        from live_runner import daemon

        real_db = tmp_path / "live.db"
        alias_db = tmp_path / "alias.db"
        alias_db.symlink_to(real_db)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{alias_db.as_posix()}")

        assert daemon.daemon_lock_path() == real_db.with_name("live.db.lock")

    def test_the_lock_follows_the_sqlite_uri_form(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``sqlite:///file:/abs/db.db?uri=true`` is the same database as ``sqlite:////abs/db.db``.

        The ``file:`` prefix is URI syntax the SQLite driver parses, not part of the path; leaving
        it in would put the lock beside a nonexistent ``file:`` directory.
        """
        from live_runner import daemon

        db_file = tmp_path / "live.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///file:{db_file.as_posix()}?uri=true")

        assert daemon.daemon_lock_path() == db_file.with_name("live.db.lock")

    def test_the_lock_normalizes_the_localhost_authority_of_a_file_uri(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``file://localhost/abs/db.db`` is ``/abs/db.db``: SQLite accepts only an empty or
        ``localhost`` authority, and keeping ``//localhost`` in the path would lock a directory
        the database is not in - a second daemon could then take a second lock."""
        from live_runner import daemon

        db_file = tmp_path / "live.db"
        monkeypatch.setenv(
            "DATABASE_URL", f"sqlite:///file://localhost{db_file.as_posix()}?uri=true"
        )

        assert daemon.daemon_lock_path() == db_file.with_name("live.db.lock")

    def test_the_lock_keeps_the_file_prefix_without_uri_mode(
        self, monkeypatch: Any
    ) -> None:
        """``sqlite:///file:live.db`` without ``uri=true`` opens a literal ``file:live.db``.

        The ``file:`` prefix is URI syntax only in URI mode; stripping it anyway would put the
        lock beside ``live.db`` while the engine opens ``file:live.db`` - two spellings of what
        the driver sees as two different files."""
        from live_runner import daemon

        monkeypatch.setenv("DATABASE_URL", "sqlite:///file:live.db")

        expected = Path("file:live.db").resolve().with_name("file:live.db.lock")
        assert daemon.daemon_lock_path() == expected

    def test_the_lock_falls_back_to_the_data_dir_for_a_named_memory_uri(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """``file:memdb1?mode=memory&cache=shared&uri=true`` is an in-memory database: it has no
        file to sit beside, so the lock falls back to ``DATA_DIR`` like ``:memory:`` does."""
        from live_runner import daemon

        monkeypatch.setenv(
            "DATABASE_URL", "sqlite:///file:memdb1?mode=memory&cache=shared&uri=true"
        )

        assert daemon.daemon_lock_path() == tmp_data_dir / "backtests.db.lock"

    def test_the_lock_falls_back_to_the_data_dir_for_an_in_memory_url(
        self, monkeypatch: Any, tmp_data_dir: Path
    ) -> None:
        """An in-memory database has no file to sit beside.

        Treating ``:memory:`` as a path would create ``:memory:.lock`` in the process's working
        directory - the repo root for the daemon - which is both the wrong directory and a file
        the caller never asked for.
        """
        from live_runner import daemon

        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

        assert daemon.daemon_lock_path() == tmp_data_dir / "backtests.db.lock"


def _daemon_child_env(data_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    env["DATA_DIR"] = str(data_dir)
    # Pointed at the child's scratch dir so the test can tell whether the heavy daemon module was
    # imported at all: it creates the log directory from the logger factory on the way through.
    env["LOG_DIR"] = str(data_dir / "logs")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing else os.pathsep.join([str(REPO_ROOT), existing])
    )
    return env


def _run_daemon_process(args: list[str], data_dir: Path, timeout: float = 120.0) -> Any:
    return subprocess.run(
        [sys.executable, "-m", "live_runner.daemon", *args],
        cwd=REPO_ROOT,
        env=_daemon_child_env(data_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


#: The refusal tests' child. It drives the real ``daemon_cli.main([])`` - real argument parsing,
#: real ``run_daemon``, real lock acquisition - but ``_run_daemon_locked`` is replaced by a stub
#: that exits 99 loudly. The stub is the point: these tests exist to prove the guard refuses
#: *before the body runs*, and a regression that let the body run must fail loudly here, never
#: start a real daemon - the child's ``DEFAULT_ACCOUNTS_ENV`` is the checkout's real
#: ``.accounts.env``, so an unguarded body would log into the real brokers (#313 review).
_REFUSAL_CHILD = """
import sys

from live_runner import daemon, daemon_cli


async def _stubbed_daemon_body(**_kwargs):
    print("the daemon body ran: the single-instance guard did not refuse", file=sys.stderr)
    raise SystemExit(99)


daemon._run_daemon_locked = _stubbed_daemon_body
sys.exit(daemon_cli.main([]))
"""


def _run_refusal_process(data_dir: Path, timeout: float = 120.0) -> Any:
    return subprocess.run(
        [sys.executable, "-c", _REFUSAL_CHILD],
        cwd=REPO_ROOT,
        env=_daemon_child_env(data_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestRealEntryPoint:
    """Subprocesses: ``python -m live_runner.daemon`` runs the module's imports first, so only a
    child process can show what an operator's (or an agent's) invocation actually does."""

    def test_help_exits_zero_and_writes_nothing(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        result = _run_daemon_process(["--help"], data_dir)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert "usage:" in result.stdout
        assert not (data_dir / "backtests.db").exists(), "--help created the live database file"
        assert not (data_dir / "backtests.db.lock").exists(), "--help took the single-instance lock"
        assert not (data_dir / "logs").exists(), "the heavy daemon module was imported for --help"

    def test_importing_the_daemon_does_not_initialize_the_database(self, tmp_path: Path) -> None:
        """A fresh interpreter imports the daemon and initializes nothing.

        The guard acquires inside ``run_daemon``, so anything that opened or migrated the database
        at import time would put a refused daemon ahead of the guard. The engine in
        ``shared.database`` is created lazily, and this pins that rather than assuming it: the
        refusal test proves the same thing through the real entry point, and this proves it for
        every other way the module gets imported (a debugger, a script, an agent).
        """
        data_dir = tmp_path / "data"
        result = subprocess.run(
            [sys.executable, "-c", "import live_runner.daemon"],
            cwd=REPO_ROOT,
            env=_daemon_child_env(data_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        written = sorted(p.name for p in data_dir.iterdir())
        assert written == ["logs"], (
            f"importing the daemon wrote more than its log directory: {written}"
        )

    def test_an_unknown_argument_exits_two(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        result = _run_daemon_process(["--config", "live_deployments.yaml"], data_dir)

        assert result.returncode == 2
        assert "usage:" in result.stderr
        assert not (data_dir / "backtests.db").exists()
        assert not (data_dir / "backtests.db.lock").exists()
        assert not (data_dir / "logs").exists()

    def test_a_second_daemon_process_is_refused(self, tmp_path: Path) -> None:
        """The incident: a second start next to a running daemon must be refused, and must not
        open the live database - ``backtests.db`` staying absent is the 2026-11-21 failure pinned.
        """
        data_dir = tmp_path / "data"
        lock_path = data_dir / "backtests.db.lock"
        holder = spawn_holder(lock_path, hold_seconds=90.0)
        try:
            await_held(holder)
            result = _run_refusal_process(data_dir)
        finally:
            holder.kill()
            holder.wait(timeout=10)

        # The literal 3, not ``daemon_cli.EXIT_ALREADY_RUNNING``: the exit code is the contract
        # an operator and a systemd ``Restart=always`` unit key on, and importing the constant
        # would let an edit to it move this assertion with it - the test would pass either way.
        assert result.returncode == 3, f"stdout={result.stdout}\nstderr={result.stderr}"
        # Only the lock file and the log directory (the child's LOG_DIR points inside the data dir,
        # and the logger factory creates it while importing the heavy module): no database file, no
        # WAL/SHM sidecars, no migration. Importing the daemon must not initialize the database
        # either, so the refusal is the first thing that touches it.
        written = sorted(p.name for p in data_dir.iterdir())
        assert written == ["backtests.db.lock", "logs"], (
            f"the refused daemon wrote into the data directory: {written}"
        )
        assert "already running" in result.stderr.lower(), result.stderr
        assert str(lock_path) in result.stderr, "the refusal must name the lock it found held"
        assert not (data_dir / "backtests.db").exists(), (
            "the refused daemon opened the live database"
        )
        assert lock_path.is_file(), "the running daemon's lock file must still be there"

    def test_a_refused_start_leaves_the_running_daemons_lock_alone(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        lock_path = data_dir / "backtests.db.lock"
        holder = spawn_holder(lock_path, hold_seconds=90.0)
        try:
            await_held(holder)
            result = _run_refusal_process(data_dir)
            # The literal 3 for the same reason as above: a child that crashed or reached the
            # stubbed body (exit 99) must not pass for a refusal.
            assert result.returncode == 3, f"stdout={result.stdout}\nstderr={result.stderr}"
            contender = SingleInstanceLock(lock_path)
            with pytest.raises(SingleInstanceError):
                contender.acquire()
        finally:
            holder.kill()
            holder.wait(timeout=10)

        SingleInstanceLock(lock_path).acquire().release()


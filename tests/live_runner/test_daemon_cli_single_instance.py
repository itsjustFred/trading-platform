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
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from live_runner import daemon_cli
from shared.single_instance import SingleInstanceError, SingleInstanceLock
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

        def _stop_at_discovery(**_kwargs: Any) -> list[Any]:
            raise _DiscoveryReached("discovery reached")

        monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _stop_at_discovery)

        with pytest.raises(_DiscoveryReached):
            asyncio.run(daemon.run_daemon())

        SingleInstanceLock(daemon.daemon_lock_path()).acquire().release()

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

        def _blocking_discovery(**_kwargs: Any) -> list[Any]:
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
            result = _run_daemon_process([], data_dir)
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
            _run_daemon_process([], data_dir)
            contender = SingleInstanceLock(lock_path)
            with pytest.raises(SingleInstanceError):
                contender.acquire()
        finally:
            holder.kill()
            holder.wait(timeout=10)

        SingleInstanceLock(lock_path).acquire().release()


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
from pathlib import Path
from typing import Any

import pytest

from live_runner import daemon_cli
from shared.single_instance import SingleInstanceError, SingleInstanceLock

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
        holder = _spawn_holder(lock_path, hold_seconds=90.0)
        try:
            _await_held(holder)
            result = _run_daemon_process([], data_dir)
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert result.returncode == daemon_cli.EXIT_ALREADY_RUNNING, (
            f"stdout={result.stdout}\nstderr={result.stderr}"
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
        holder = _spawn_holder(lock_path, hold_seconds=90.0)
        try:
            _await_held(holder)
            _run_daemon_process([], data_dir)
            with pytest.raises(SingleInstanceError):
                SingleInstanceLock(lock_path).acquire()
        finally:
            holder.kill()
            holder.wait(timeout=10)

        SingleInstanceLock(lock_path).acquire().release()


def _spawn_holder(lock_path: Path, hold_seconds: float) -> Any:
    """Start a process that acquires ``lock_path`` and holds it for ``hold_seconds``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing else os.pathsep.join([str(REPO_ROOT), existing])
    )
    return subprocess.Popen(
        [sys.executable, "-m", "tests.helpers.lock_holder", str(lock_path), str(hold_seconds)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_held(holder: Any) -> None:
    """Block until the holder reports the lock is granted; fail loudly if it died instead."""
    line = holder.stdout.readline() if holder.stdout is not None else ""
    if line.strip() != "held":
        holder.kill()
        stderr = holder.stderr.read() if holder.stderr is not None else ""
        pytest.fail(f"the holder never acquired the lock (returncode={holder.poll()}): {stderr}")

"""Restore-drill and guard tests for ``scripts.db_backup`` on throwaway databases.

Every test works on ``tmp_path`` SQLite files only - never the repo's ``data/`` and never the
live database. The daemon-running detection is exercised two ways: the real lock probe against a
lock this test process holds, and a monkeypatched process scan for the refusal paths (a real
``live_runner.daemon`` may legitimately be running on the machine running the suite).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import db_backup


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "backtests.db"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def _make_db(path: Path, rows: list[int], migrations: list[str]) -> None:
    """A minimal stand-in for the live DB: WAL mode, one table, the migrations ledger."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY, qty REAL)")
        connection.execute(
            "CREATE TABLE schema_data_migrations "
            "(name TEXT PRIMARY KEY, applied_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO fills (qty) VALUES (?)", [(float(row),) for row in rows]
        )
        connection.executemany(
            "INSERT INTO schema_data_migrations (name, applied_at) VALUES (?, 'now')",
            [(name,) for name in migrations],
        )
        connection.commit()
    finally:
        connection.close()


def _read_state(path: Path) -> tuple[list[float], list[str]]:
    connection = sqlite3.connect(path)
    try:
        rows = [
            float(row[0])
            for row in connection.execute("SELECT qty FROM fills ORDER BY id")
        ]
        migrations = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM schema_data_migrations ORDER BY name"
            )
        ]
        return rows, migrations
    finally:
        connection.close()


def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the restore guard to 'no daemon' regardless of what the test host is running."""
    monkeypatch.setattr(
        db_backup,
        "check_daemon",
        lambda _db: db_backup.DaemonCheck(
            lock_held=False,
            lock_authoritative=True,
            processes=(),
            notes=(),
        ),
    )


def _daemon_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db_backup,
        "check_daemon",
        lambda _db: db_backup.DaemonCheck(
            lock_held=False,
            lock_authoritative=False,
            processes=("python -m live_runner.daemon",),
            notes=(),
        ),
    )


# --- backup -----------------------------------------------------------------


def test_backup_produces_a_verified_consistent_snapshot(
    db_path: Path, backup_dir: Path
) -> None:
    _make_db(db_path, [1.0, 2.0], ["m1"])
    info = db_backup.backup_db(db_path, backup_dir)

    assert info.quick_check == "ok"
    assert info.page_count > 0
    assert info.migrations == ("m1",)
    assert info.path.parent == backup_dir
    assert _read_state(info.path) == ([1.0, 2.0], ["m1"])
    # The snapshot is a single self-contained file: no -wal/-shm sidecars in the backup dir.
    assert sorted(p.name for p in backup_dir.iterdir()) == [info.path.name]


def test_backup_refuses_a_missing_database(db_path: Path, backup_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        db_backup.backup_db(db_path, backup_dir)


def test_backup_rotates_to_keep_newest(db_path: Path, backup_dir: Path) -> None:
    _make_db(db_path, [1.0], ["m1"])
    created = [db_backup.backup_db(db_path, backup_dir, keep=2).path for _ in range(3)]

    remaining = db_backup.list_backups(backup_dir, db_path)
    assert remaining == created[1:]
    assert len(remaining) == 2


def test_list_backups_ignores_foreign_files(
    db_path: Path, backup_dir: Path
) -> None:
    _make_db(db_path, [1.0], ["m1"])
    db_backup.backup_db(db_path, backup_dir)
    # A different database's backups and non-backup files must survive rotation and listing.
    (backup_dir / "other.db-20000101T000000.000000Z.db").write_bytes(b"x")
    (backup_dir / "notes.txt").write_text("not a backup")

    assert len(db_backup.list_backups(backup_dir, db_path)) == 1


def test_verify_backup_rejects_non_sqlite(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database" * 4)
    with pytest.raises(db_backup.BackupVerificationError):
        db_backup.verify_backup(garbage)


# --- restore ----------------------------------------------------------------


def test_restore_drill_roundtrip(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drill: snapshot, mutate, restore, and prove the database returns to snapshot state."""
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path

    # Mutate: a new fill row and a new one-way-style data migration marker.
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO fills (qty) VALUES (2.0)")
    connection.execute(
        "INSERT INTO schema_data_migrations (name, applied_at) VALUES ('m2', 'now')"
    )
    connection.commit()
    connection.close()
    assert _read_state(db_path) == ([1.0, 2.0], ["m1", "m2"])

    result = db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    assert _read_state(db_path) == ([1.0], ["m1"])
    assert result.dropped_migrations == ("m2",)
    assert result.added_migrations == ()
    # The pre-restore state was captured first: the safety snapshot still has m2 and row 2.
    assert result.safety_snapshot is not None
    assert _read_state(result.safety_snapshot) == ([1.0, 2.0], ["m1", "m2"])
    # The restored database passes the same verification a backup does.
    assert db_backup.verify_backup(db_path).quick_check == "ok"


def test_restore_reports_migrations_the_backup_adds(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], [])
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO schema_data_migrations (name, applied_at) VALUES ('m1', 'now')"
    )
    connection.commit()
    connection.close()
    snapshot = db_backup.backup_db(db_path, backup_dir).path

    # Roll the ledger back by hand, then restore: the backup's migration reappears.
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_data_migrations")
    connection.commit()
    connection.close()

    result = db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)
    assert result.added_migrations == ("m1",)


def test_restore_refuses_while_a_daemon_is_detected(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    _daemon_seen(monkeypatch)

    with pytest.raises(db_backup.DaemonRunningError, match="live daemon process"):
        db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    # The refusal happens before anything is written: state is untouched.
    assert _read_state(db_path) == ([1.0], ["m1"])


def test_restore_refuses_while_the_single_instance_lock_is_held(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    # A held lock blocks even when the process scan finds nothing and --force is absent.
    monkeypatch.setattr(
        db_backup,
        "check_daemon",
        lambda _db: db_backup.DaemonCheck(
            lock_held=True,
            lock_authoritative=True,
            processes=(),
            notes=(),
        ),
    )

    with pytest.raises(db_backup.DaemonRunningError, match="single-instance lock"):
        db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)


def test_restore_force_overrides_daemon_detection(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO fills (qty) VALUES (9.0)")
    connection.commit()
    connection.close()
    _daemon_seen(monkeypatch)

    result = db_backup.restore_db(
        db_path, snapshot, backup_dir=backup_dir, force=True
    )
    assert _read_state(db_path) == ([1.0], ["m1"])
    assert result.safety_snapshot is not None


def test_restore_to_a_fresh_path_takes_no_snapshot(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = db_path.with_name("source.db")
    _make_db(source, [1.0], ["m1"])
    snapshot = db_backup.backup_db(source, backup_dir).path
    _no_daemon(monkeypatch)

    result = db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    assert result.safety_snapshot is None
    assert _read_state(db_path) == ([1.0], ["m1"])


def test_restore_rejects_a_non_database_target(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    db_path.write_bytes(b"definitely not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)


def test_restore_rejects_self_restore(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    db_backup.backup_db(db_path, backup_dir)

    with pytest.raises(db_backup.BackupVerificationError, match="onto itself"):
        db_backup.restore_db(db_path, db_path, backup_dir=backup_dir)


def _hold_db_open(db_path: Path, ready: Path) -> subprocess.Popen:
    """A second process holding ``db_path`` open, the way the viewer does.

    The connection must be a *different* process: POSIX locks are per-process, so an idle
    ``sqlite3.connect`` in this test process cannot reproduce the cross-process busy the
    journal-mode probe exists to catch. ``ready`` is touched once the connection is live.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys, time\n"
            "c = sqlite3.connect(sys.argv[1])\n"
            "c.execute('SELECT name FROM sqlite_master').fetchall()\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "time.sleep(60)\n",
            str(db_path),
            str(ready),
        ]
    )


def _wait_for_ready(ready: Path, process: subprocess.Popen) -> None:
    deadline = 100
    while not ready.exists() and deadline:
        if process.poll() is not None:
            pytest.fail(f"db holder exited early with {process.returncode}")
        time.sleep(0.1)
        deadline -= 1
    assert ready.exists(), "db holder never signalled ready"


def test_restore_refuses_while_another_process_has_the_database_open(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The viewer (or any process) holding the DB open must stop the restore.

    ``os.replace`` would strand that connection on the unlinked inode - writes it commits
    afterwards land in a file nobody reads - so the journal-mode probe treats "the file is
    open" as "a daemon may be running" and refuses before anything is staged.
    """
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    ready = tmp_path / "holder.ready"
    holder = _hold_db_open(db_path, ready)
    try:
        _wait_for_ready(ready, holder)
        with pytest.raises(db_backup.DaemonRunningError, match="has the database open"):
            db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # Nothing was staged or swapped: the live database is untouched and a retry succeeds.
    assert _read_state(db_path) == ([1.0], ["m1"])
    assert db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)
    assert _read_state(db_path) == ([1.0], ["m1"])


def test_restore_force_overrides_an_open_database_connection(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """``--force`` is the documented override for the open-database refusal."""
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    ready = tmp_path / "holder.ready"
    holder = _hold_db_open(db_path, ready)
    try:
        _wait_for_ready(ready, holder)
        db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir, force=True)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    assert "WARNING: --force used" in capsys.readouterr().err
    assert _read_state(db_path) == ([1.0], ["m1"])


def test_restore_holds_the_single_instance_lock_through_the_swap(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock must stay held from before staging until after ``os.replace``.

    A probe-then-release leaves a window in which a daemon can start mid-restore; holding the
    lock is what makes its own acquire fail instead. The spy probes the lock file at the exact
    moment of the swap: busy means this restore still holds it.
    """
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    lock_path = db_backup.daemon_lock_path(db_path)
    real_replace = os.replace
    seen: list[bool] = []

    def spy_replace(src: object, dst: object) -> object:
        seen.append(db_backup._probe_flock(lock_path))
        return real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(db_backup.os, "replace", spy_replace)
    db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    assert seen == [True]


def test_restore_refuses_a_daemon_process_even_with_a_free_authoritative_lock(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #313 transition window: an old-build daemon holds no lock but is still a daemon.

    Once ``shared.single_instance`` exists a free lock plus a found ``live_runner.daemon``
    process must still refuse - the process could be an old build that never takes the lock,
    or one trading a different ``DATABASE_URL``. ``--force`` stays the override.
    """
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path
    monkeypatch.setattr(
        db_backup,
        "check_daemon",
        lambda _db: db_backup.DaemonCheck(
            lock_held=False,
            lock_authoritative=True,
            processes=("python -m live_runner.daemon",),
            notes=(),
        ),
    )

    with pytest.raises(db_backup.DaemonRunningError, match="live daemon process"):
        db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    assert _read_state(db_path) == ([1.0], ["m1"])


def test_restore_removes_leftover_sidecars_before_the_swap(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed session can leave ``-wal``/``-shm``/``-journal`` files behind.

    Frames in them belong to the pre-restore database; replayed over the restored file they
    would resurrect rows the backup does not have. Real sidecar bytes are copied back after
    the writer closes (which normally deletes them) to simulate the leftovers.
    """
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO fills (qty) VALUES (2.0)")
    connection.commit()
    leftovers: dict[str, bytes] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            leftovers[suffix] = sidecar.read_bytes()
    connection.close()
    for suffix, data in leftovers.items():
        db_path.with_name(db_path.name + suffix).write_bytes(data)
    db_path.with_name(db_path.name + "-journal").write_bytes(b"")

    db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)

    assert _read_state(db_path) == ([1.0], ["m1"])
    for suffix in ("-wal", "-shm", "-journal"):
        assert not db_path.with_name(db_path.name + suffix).exists()


def test_backup_files_are_stored_in_rollback_journal_mode(
    db_path: Path, backup_dir: Path
) -> None:
    """A backup must be one self-contained file: WAL mode would need -wal/-shm sidecars."""
    _make_db(db_path, [1.0], ["m1"])
    info = db_backup.backup_db(db_path, backup_dir)

    connection = db_backup._connect_read(info.path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert str(mode).lower() == "delete"


# --- daemon detection ---------------------------------------------------------


def test_daemon_cmdline_pattern() -> None:
    pattern = db_backup._DAEMON_CMDLINE_RE
    assert pattern.search("python -m live_runner.daemon")
    assert pattern.search("/usr/bin/python3 -m live_runner.daemon --flag")
    assert pattern.search("python live_runner/daemon.py")
    assert pattern.search("python -m live_runner.daemon_cli")
    assert pattern.search('python -m live_runner.daemon"')
    assert not pattern.search("tail -f live_runner/daemon.log")
    assert not pattern.search("python -m scripts.db_backup restore x.db")
    assert not pattern.search("python -m live_runner.daemon2")


@contextlib.contextmanager
def _hold_flock(path: Path):
    """Hold an exclusive non-blocking lock on ``path`` the way the daemon would."""
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def test_lock_probe_sees_a_held_lock(db_path: Path, tmp_path: Path) -> None:
    lock_path = db_backup.daemon_lock_path(db_path)
    held, _authoritative = db_backup.probe_single_instance_lock(lock_path)
    assert held is False  # nothing holds it yet (module absent -> inline probe)

    with _hold_flock(lock_path):
        held, _ = db_backup.probe_single_instance_lock(lock_path)
        assert held is True

    held, _ = db_backup.probe_single_instance_lock(lock_path)
    assert held is False


def test_check_daemon_reports_lock_and_scan(tmp_path: Path) -> None:
    check = db_backup.check_daemon(tmp_path / "backtests.db")
    assert check.lock_held is False
    assert isinstance(check.processes, tuple)


# --- CLI ---------------------------------------------------------------------


def test_cli_backup_list_verify_and_forced_restore(
    db_path: Path, backup_dir: Path
) -> None:
    _make_db(db_path, [1.0], ["m1"])
    env = dict(os.environ)

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "scripts.db_backup", *argv],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            timeout=120,
        )

    backup = run("backup", "--db", str(db_path), "--backup-dir", str(backup_dir))
    assert backup.returncode == 0, backup.stderr
    snapshot = next(backup_dir.glob("*.db"))

    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO fills (qty) VALUES (2.0)")
    connection.commit()
    connection.close()

    listing = run("list", "--db", str(db_path), "--backup-dir", str(backup_dir))
    assert listing.returncode == 0
    assert str(snapshot) in listing.stdout

    verified = run("verify", str(snapshot))
    assert verified.returncode == 0
    assert "quick_check=ok" in verified.stdout

    # --force: the test host may legitimately run a daemon; the flag is the documented override.
    restored = run(
        "restore",
        str(snapshot),
        "--db",
        str(db_path),
        "--backup-dir",
        str(backup_dir),
        "--force",
    )
    assert restored.returncode == 0, restored.stderr
    assert _read_state(db_path) == ([1.0], ["m1"])


def _hold_db_exclusive(db_path: Path, ready: Path) -> subprocess.Popen:
    """A second process holding an EXCLUSIVE lock on ``db_path``.

    Unlike a merely-open connection this reproduces the dangerous late opener:
    a process that started while the restore was staging and now holds locks,
    so commits it makes would land on the inode ``os.replace`` unlinks.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys, time\n"
            "c = sqlite3.connect(sys.argv[1], timeout=5)\n"
            "c.execute('BEGIN EXCLUSIVE')\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "time.sleep(60)\n",
            str(db_path),
            str(ready),
        ]
    )


def test_restore_reprobes_the_open_database_before_the_swap(
    db_path: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A process that opens the DB during staging/verification/snapshot must
    still stop the restore: the open-database probe re-runs immediately before
    ``os.replace``, when the window has shrunk to milliseconds."""
    _no_daemon(monkeypatch)
    _make_db(db_path, [1.0], ["m1"])
    snapshot = db_backup.backup_db(db_path, backup_dir).path

    real_backup_db = db_backup.backup_db
    ready = tmp_path / "late-holder.ready"
    holder_box: list[subprocess.Popen] = []

    def _snapshot_then_open_late(*args: object, **kwargs: object):
        result = real_backup_db(*args, **kwargs)
        holder = _hold_db_exclusive(db_path, ready)
        holder_box.append(holder)
        _wait_for_ready(ready, holder)
        return result

    monkeypatch.setattr(db_backup, "backup_db", _snapshot_then_open_late)
    try:
        with pytest.raises(db_backup.DaemonRunningError, match="has the database open"):
            db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)
    finally:
        for holder in holder_box:
            holder.terminate()
            holder.wait(timeout=10)

    # The live database was never swapped and no staged file was left behind.
    assert _read_state(db_path) == ([1.0], ["m1"])
    assert not db_path.with_name(db_path.name + ".restore-tmp").exists()
    assert db_backup.restore_db(db_path, snapshot, backup_dir=backup_dir)
    assert _read_state(db_path) == ([1.0], ["m1"])

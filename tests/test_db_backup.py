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
from pathlib import Path

import pytest

from scripts import db_backup


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "backtests.db"


@pytest.fixture()
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

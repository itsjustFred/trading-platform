"""Backup and restore the live SQLite database (``live_executions``, ledgers, deployments).

The database is ``DATA_DIR/backtests.db`` unless ``DATABASE_URL`` names a SQLite file; path
resolution is delegated to ``live_runner.preflight_db.resolve_db_path`` so this script can never
disagree with the preflight check or the daemon about which file is the live one.

Backups use the SQLite online backup API (``sqlite3.Connection.backup``), which produces a
consistent snapshot while the daemon keeps writing - no stop is needed for a backup. Backups are
written to ``<db parent>/backups/<db name>-<UTC stamp>.db`` and rotated to ``--keep`` files.

Restore is the dangerous direction, so it refuses to run while anything could be holding the
database:

* the daemon single-instance lock (PR #313, ``shared/single_instance.py``, ``<db>.lock`` next to
  the database file) is probed and then *held* for the whole restore - a daemon that starts
  afterwards fails its own acquire. Until that module exists on main the same ``flock`` is held
  inline on the same file;
* the process table is scanned for a ``live_runner.daemon`` command line (``ps`` on POSIX,
  PowerShell CIM on Windows). A found process refuses the restore whatever the lock reports:
  during the first release after #313 the running daemon is an old build that never takes the
  lock, and a daemon on another ``DATABASE_URL`` is indistinguishable from one on this database -
  ``--force`` is the documented override for exactly that case, and for a scan that cannot run
  at all;
* the live database is then switched to rollback-journal mode before the swap, which fails while
  any other process - the viewer, a smoke run, a daemon the scan missed - has it open.
  ``os.replace`` under an open connection would strand that connection on the unlinked inode, so
  "the file is open" is treated as "a daemon may be running".

Usage (from the repo root):

    python -m scripts.db_backup backup [--db PATH] [--backup-dir DIR] [--keep N]
    python -m scripts.db_backup restore BACKUP_FILE [--db PATH] [--backup-dir DIR]
        [--keep N] [--no-safety-snapshot] [--force]
    python -m scripts.db_backup list [--db PATH] [--backup-dir DIR]
    python -m scripts.db_backup verify BACKUP_FILE
    python -m scripts.db_backup status [--db PATH]

Exit codes: 0 success, 1 operational error, 2 refused (database in use / daemon possibly
running, or bad usage).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.db_backup
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("db_backup")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from live_runner.preflight_db import resolve_db_path

#: Backups kept per database by default: two weeks of daily backups plus headroom for the ad-hoc
#: pre-deploy snapshots the release procedure takes.
DEFAULT_KEEP = 14

#: Errnos that mean "someone else holds it" rather than "this call is broken" - the same set
#: ``shared/single_instance.py`` treats as busy on both lock primitives.
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLOCK, errno.EWOULDBLOCK})

#: A daemon command line: ``python -m live_runner.daemon``, ``live_runner/daemon.py`` and the
#: ``daemon_cli`` entry point all match; ``live_runner/daemon.log`` or ``daemon2`` do not. The
#: lookahead refuses a following ``.``, ``/`` or word character so log files and sibling modules
#: are not false positives, while quotes and whitespace (end of argument) still match.
_DAEMON_CMDLINE_RE = re.compile(r"live_runner[./]daemon(?:_cli|\.py)?(?=[^\w./]|$)")

#: Backup names are ``<db file name>-<UTC stamp>.db`` with an optional ``-N`` collision suffix.
#: The stamp's fixed width keeps lexical order equal to chronological order.
_STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"
_STAMP_RE = r"\d{8}T\d{6}\.\d{6}Z"


class DaemonRunningError(RuntimeError):
    """Restore refused: the database may still be in use by a daemon or another process."""


class BackupVerificationError(ValueError):
    """A backup file failed verification (missing, unreadable, or failed ``quick_check``)."""


class _LockBusy(Exception):
    """The platform lock primitive reported the file already locked."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    quick_check: str
    page_count: int
    size_bytes: int
    migrations: tuple[str, ...]


@dataclass(frozen=True)
class DaemonCheck:
    """What the restore guard observed before refusing or proceeding."""

    #: ``True``/``False`` when the single-instance lock could be probed, ``None`` when it could
    #: not (unwritable directory, unsupported platform primitive).
    lock_held: bool | None
    #: ``True`` when ``shared.single_instance`` answered the probe, i.e. the daemon itself holds
    #: ``<db>.lock`` and the lock - not the process scan - is the authority on "a daemon runs".
    lock_authoritative: bool
    #: Daemon command lines found in the process table; ``None`` when the scan could not run.
    processes: tuple[str, ...] | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestoreResult:
    db_path: Path
    backup_path: Path
    safety_snapshot: Path | None
    #: Data migrations the restored backup carries that the pre-restore DB lacked.
    added_migrations: tuple[str, ...]
    #: Data migrations the pre-restore DB carried that the backup lacks - the schema delta the
    #: next ``init_db`` re-applies (or an old build must not see) after the rollback.
    dropped_migrations: tuple[str, ...]


def default_backup_dir(db_path: Path) -> Path:
    """``<db parent>/backups`` - inside ``DATA_DIR`` for the default database."""
    return Path(db_path).resolve().parent / "backups"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _backup_file_name(db_path: Path, stamp: str, collision: int = 0) -> str:
    suffix = f"-{collision}" if collision else ""
    return f"{db_path.name}-{stamp}{suffix}.db"


def _backup_pattern(db_path: Path) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(db_path.name)}-({_STAMP_RE})(?:-(\d+))?\.db$"
    )


def list_backups(backup_dir: Path, db_path: Path) -> list[Path]:
    """This database's backup files in ``backup_dir``, oldest first."""
    pattern = _backup_pattern(db_path)
    entries: list[tuple[str, int, Path]] = []
    if not Path(backup_dir).is_dir():
        return []
    for candidate in Path(backup_dir).iterdir():
        match = pattern.match(candidate.name)
        if match and candidate.is_file():
            entries.append((match.group(1), int(match.group(2) or 0), candidate))
    entries.sort(key=lambda item: (item[0], item[1]))
    return [path for _, _, path in entries]


def _connect_read(path: Path) -> sqlite3.Connection:
    """Open ``path`` for reading without creating it.

    ``mode=ro`` first: a backup must never write to the live file, not even WAL-recovery
    side-effects. A WAL database left needing recovery refuses a read-only open, so that case
    falls back to ``mode=rw`` - still no CREATE, and the recovery it performs is the same one any
    writer would run.
    """
    uri = Path(path).resolve().as_uri()
    connection = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.OperationalError:
        connection.close()
        connection = sqlite3.connect(f"{uri}?mode=rw", uri=True, timeout=30)
    return connection


def _migration_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'schema_data_migrations'"
    ).fetchone()
    if not exists:
        return ()
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM schema_data_migrations ORDER BY name"
        ).fetchall()
    )


def verify_backup(path: Path) -> BackupInfo:
    """Open ``path`` and prove it is a usable SQLite database."""
    path = Path(path)
    if not path.is_file():
        raise BackupVerificationError(f"backup file not found: {path}")
    try:
        connection = _connect_read(path)
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"cannot open backup {path}: {exc}") from exc
    try:
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            migrations = _migration_names(connection)
        except sqlite3.DatabaseError as exc:
            raise BackupVerificationError(
                f"backup {path} is not a readable SQLite database: {exc}"
            ) from exc
    finally:
        connection.close()
    if quick_check != "ok":
        raise BackupVerificationError(
            f"backup {path} failed PRAGMA quick_check: {quick_check}"
        )
    return BackupInfo(
        path=path,
        quick_check=quick_check,
        page_count=page_count,
        size_bytes=path.stat().st_size,
        migrations=migrations,
    )


def _write_backup(source: Path, destination: Path) -> None:
    """Copy ``source`` into ``destination`` through the online backup API."""
    source_connection = _connect_read(source)
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
        # The backup inherits the source's WAL journal mode, which would leave -wal/-shm
        # sidecars next to every snapshot and make a bare copy of the .db file lossy. Store
        # backups in rollback-journal mode instead: one self-contained file per snapshot. The
        # live database's own journal mode is untouched - the engine sets WAL on every connect.
        destination_connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        destination_connection.close()
        source_connection.close()


def _prune_backups(backup_dir: Path, db_path: Path, keep: int) -> None:
    """Delete this database's oldest backups beyond ``keep``; never touch other files."""
    backups = list_backups(backup_dir, db_path)
    for stale in backups[: max(0, len(backups) - keep)]:
        stale.unlink()


def backup_db(
    db_path: Path, backup_dir: Path, *, keep: int = DEFAULT_KEEP
) -> BackupInfo:
    """Snapshot ``db_path`` into ``backup_dir`` and rotate to ``keep`` files.

    Safe while the daemon runs: ``Connection.backup`` with the default ``pages=-1`` copies the
    whole database in one step under a single read transaction, so the result is always a
    consistent committed-state snapshot.
    """
    db_path = Path(db_path)
    if keep < 1:
        raise ValueError("--keep must be at least 1")
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = _utc_stamp()
    collision = 0
    while True:
        destination = backup_dir / _backup_file_name(db_path, stamp, collision)
        if not destination.exists():
            break
        collision += 1
    try:
        _write_backup(db_path, destination)
        info = verify_backup(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    _prune_backups(backup_dir, db_path, keep)
    return info


def daemon_lock_path(db_path: Path) -> Path:
    """``<db>.lock`` - the path ``live_runner.daemon.daemon_lock_path`` uses (#313).

    Resolved through the real database file so a symlinked data directory and its target select
    one lock file, matching the daemon's own resolution.
    """
    resolved = Path(db_path).resolve()
    return resolved.with_name(f"{resolved.name}.lock")


def _lock_exclusive_nonblocking(handle: IO[bytes]) -> None:
    """Take the platform's exclusive non-blocking lock on ``handle``.

    ``flock`` on POSIX, ``msvcrt`` byte-range locking on Windows - the same primitives
    ``shared/single_instance.py`` uses on the same lock file, released by the OS when the holder
    dies. Raises ``_LockBusy`` when another process holds it and ``OSError`` for anything else.
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _BUSY_ERRNOS:
                raise _LockBusy(str(exc)) from exc
            raise
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY_ERRNOS:
                raise _LockBusy(str(exc)) from exc
            raise


def _probe_flock(lock_path: Path) -> bool:
    """Inline equivalent of ``shared/single_instance.py``'s non-blocking probe.

    Used only until that module exists on main (#313): same file, same primitives, released by
    the OS when the holder dies. Creating the file when absent is what the daemon itself does on
    acquire, so an empty ``<db>.lock`` left behind is expected, not litter.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[bytes] = open(lock_path, "a+b")
    try:
        _lock_exclusive_nonblocking(handle)
    except _LockBusy:
        return True
    finally:
        # Closing the handle releases the lock on both platforms.
        handle.close()
    return False


@contextlib.contextmanager
def _hold_single_instance_lock(db_path: Path, *, force: bool) -> Iterator[None]:
    """Hold the daemon's single-instance lock for the whole restore.

    Holding - not just probing - is what closes the window between the daemon check and
    ``os.replace``: a daemon that starts afterwards fails its own acquire.
    ``shared.single_instance`` is used when it exists (#313); until then the inline ``flock``
    the probe uses is held on the same file. ``--force`` proceeds without the lock, with a
    warning, when it turns out held.
    """
    lock_path = daemon_lock_path(db_path)
    try:
        from shared import single_instance
    except ImportError:
        single_instance = None
    if single_instance is not None:
        lock = single_instance.SingleInstanceLock(lock_path)
        try:
            lock.acquire()
        except single_instance.SingleInstanceError as exc:
            if not force:
                raise DaemonRunningError(
                    f"refusing to restore {db_path}: the single-instance lock is held: "
                    f"{lock_path}. If this is a false positive re-run with --force."
                ) from exc
            print(
                "WARNING: --force used; the single-instance lock is held, so the restore "
                "runs without the lock that would block a starting daemon.",
                file=sys.stderr,
            )
            yield
            return
        try:
            yield
        finally:
            lock.release()
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[bytes] = open(lock_path, "a+b")
    try:
        _lock_exclusive_nonblocking(handle)
    except _LockBusy as exc:
        handle.close()
        if not force:
            raise DaemonRunningError(
                f"refusing to restore {db_path}: the single-instance lock is held: "
                f"{lock_path}. If this is a false positive re-run with --force."
            ) from exc
        print(
            "WARNING: --force used; the single-instance lock is held, so the restore "
            "runs without the lock that would block a starting daemon.",
            file=sys.stderr,
        )
        yield
        return
    except Exception:
        handle.close()
        raise
    try:
        yield
    finally:
        handle.close()


def probe_single_instance_lock(lock_path: Path) -> tuple[bool | None, bool]:
    """``(held, authoritative)`` for the daemon's single-instance lock.

    ``held`` is ``True``/``False`` when the probe ran, ``None`` when it could not. ``authoritative``
    is ``True`` only when ``shared.single_instance`` answered - i.e. the daemon build that holds
    this lock is deployed, so a free lock really does mean no daemon trades this database. The
    inline ``flock`` probe detects the same held lock but cannot distinguish "no daemon" from "a
    pre-#313 daemon that never takes a lock", which is what the process scan covers.
    """
    try:
        from shared import single_instance
    except ImportError:
        single_instance = None
    if single_instance is not None:
        try:
            single_instance.SingleInstanceLock(lock_path).acquire().release()
        except single_instance.SingleInstanceError:
            return True, True
        except Exception:
            return None, True
        return False, True
    try:
        return _probe_flock(lock_path), False
    except OSError:
        return None, False


def _iter_process_cmdlines() -> Iterator[tuple[int, str]]:
    """``(pid, command line)`` for every process, via the platform's process table.

    ``ps`` covers Linux and macOS; Windows has no ``ps``, so PowerShell's CIM cmdlet is used
    instead. A non-zero exit or missing binary raises - the caller decides whether an unavailable
    scan is blocking. A header line, should one appear, fails the pid parse and is skipped.
    """
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | "
            'ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }',
        ]
    else:
        command = ["ps", "-eo", "pid=,command="]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"process scan failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, cmdline = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        yield pid, cmdline.strip()


def find_daemon_processes() -> list[str]:
    """Command lines that look like a running ``live_runner.daemon`` (best effort).

    Cannot tell which database the daemon trades - that is why the single-instance lock is
    preferred once it exists, and why ``--force`` exists for the fallback's false positives.
    """
    own_pid = os.getpid()
    found: list[str] = []
    for pid, cmdline in _iter_process_cmdlines():
        if pid == own_pid or not cmdline:
            continue
        if _DAEMON_CMDLINE_RE.search(cmdline):
            found.append(cmdline)
    return found


def check_daemon(db_path: Path) -> DaemonCheck:
    """Observe everything the restore guard knows about a possibly-running daemon."""
    lock_path = daemon_lock_path(db_path)
    held, authoritative = probe_single_instance_lock(lock_path)
    notes: list[str] = []
    try:
        processes: tuple[str, ...] | None = tuple(find_daemon_processes())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        processes = None
        notes.append(f"process scan unavailable: {exc}")
    return DaemonCheck(
        lock_held=held,
        lock_authoritative=authoritative,
        processes=processes,
        notes=tuple(notes),
    )


def _assert_daemon_stopped(db_path: Path, *, force: bool) -> DaemonCheck:
    """Refuse the restore while a daemon could hold ``db_path``, unless ``force``.

    A daemon process refuses whatever the lock reports: during the first release after #313
    the running daemon is an old build that never takes the lock, and a daemon on another
    ``DATABASE_URL`` cannot be told apart from one on this database. ``--force`` is the
    documented override for both, and for a check that cannot run at all.
    """
    check = check_daemon(db_path)
    reasons: list[str] = []
    if check.lock_held:
        reasons.append(f"the single-instance lock is held: {daemon_lock_path(db_path)}")
    if check.processes:
        reasons.append("a live daemon process was detected: " + "; ".join(check.processes))
    unknown = check.lock_held is None or check.processes is None
    if reasons or unknown:
        detail = "; ".join(reasons) if reasons else "daemon state could not be verified"
        if force:
            print(
                f"WARNING: --force used; restoring while daemon state is uncertain "
                f"({detail}).",
                file=sys.stderr,
            )
        else:
            raise DaemonRunningError(
                f"refusing to restore {db_path}: {detail}. If this is a false positive - "
                "e.g. a daemon trading a different DATABASE_URL - re-run with --force."
            )
    return check


def _read_migrations_quiet(db_path: Path) -> tuple[str, ...]:
    """The ``schema_data_migrations`` names in ``db_path``; empty when unreadable or absent."""
    if not Path(db_path).is_file():
        return ()
    try:
        connection = _connect_read(db_path)
    except sqlite3.Error:
        return ()
    try:
        return _migration_names(connection)
    except sqlite3.Error:
        return ()
    finally:
        connection.close()


def _switch_live_db_to_rollback_journal(db_path: Path, *, force: bool) -> None:
    """Checkpoint the live DB out of WAL and prove no other process holds it open.

    ``PRAGMA journal_mode=DELETE`` needs the database to itself: it raises ``database is locked``
    while any other connection - the viewer, a smoke run, a daemon whose command line the scan
    missed - has the file open. That is exactly the condition under which ``os.replace`` would
    strand those connections on the unlinked inode, so a busy result refuses the restore
    (``DaemonRunningError``, exit 2; ``--force`` overrides with a warning). On success the switch
    also checkpoints and removes the ``-wal``/``-shm`` sidecars, so no pre-restore frame can
    replay over the restored file; the next engine connect re-enables WAL.
    """
    if not db_path.is_file():
        return
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        try:
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            # ``journal_mode`` is a no-op once the file already reads DELETE, so
            # on its own it cannot see a connection that opened the database
            # after an earlier probe passed. An EXCLUSIVE lock attempt fails on
            # any lock another connection holds - exactly the readers/writers
            # whose later commits ``os.replace`` would strand on the unlinked
            # inode. An idle connection holds no lock and still passes; that
            # residual is why the swap itself stays milliseconds after this.
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            busy = getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY or (
                "locked" in str(exc).lower() or "busy" in str(exc).lower()
            )
            if not busy:
                raise
            if not force:
                raise DaemonRunningError(
                    f"refusing to restore {db_path}: another process has the database open "
                    f"({exc}). Stop the viewer and every process using it, then re-run; "
                    "--force overrides if this is a false positive."
                ) from exc
            print(
                f"WARNING: --force used; the database is open elsewhere ({exc}); "
                "restoring anyway.",
                file=sys.stderr,
            )
    finally:
        connection.close()


def restore_db(
    db_path: Path,
    backup_path: Path,
    *,
    backup_dir: Path | None = None,
    keep: int = DEFAULT_KEEP,
    safety_snapshot: bool = True,
    force: bool = False,
) -> RestoreResult:
    """Restore ``db_path`` from a verified backup, atomically and only while stopped.

    Order matters: verify the backup, refuse a live daemon *before* any connection could touch
    the live file, hold the single-instance lock so no daemon can start mid-restore, prove the
    database itself is not open elsewhere (the journal-mode switch), stage the restore beside
    the target so a failed copy never leaves a half-written database, snapshot the current
    database (the only copy of anything written since the last backup - taken after staging so
    rotation cannot prune the file being restored), then swap sidecar files out and
    ``os.replace`` into place.
    """
    db_path = Path(db_path).resolve()
    backup_path = Path(backup_path)
    info = verify_backup(backup_path)
    if db_path.exists() and backup_path.resolve() == db_path:
        raise BackupVerificationError(f"refusing to restore a database onto itself: {db_path}")
    _assert_daemon_stopped(db_path, force=force)
    with _hold_single_instance_lock(db_path, force=force):
        _switch_live_db_to_rollback_journal(db_path, force=force)
        if db_path.is_file():
            # Prove the current file is a readable database before anything overwrites it; a
            # corrupt or non-SQLite file must stop the restore, not be silently replaced.
            probe = _connect_read(db_path)
            try:
                probe.execute("PRAGMA schema_version").fetchone()
            finally:
                probe.close()

        current_migrations = _read_migrations_quiet(db_path)

        # Stage first, snapshot second: the staged copy consumes the backup file completely
        # before the snapshot's rotation could prune it, and a snapshot failure still leaves
        # the live database untouched. ``db_path`` is resolved above so ``os.replace`` swaps
        # the real file, never a symlink standing in for it.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        staged = db_path.with_name(f"{db_path.name}.restore-tmp")
        staged.unlink(missing_ok=True)
        snapshot: Path | None = None
        try:
            source = sqlite3.connect(str(backup_path))
            target = sqlite3.connect(str(staged))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            verify_backup(staged)
            snapshot_dir = (
                Path(backup_dir) if backup_dir is not None else default_backup_dir(db_path)
            )
            if safety_snapshot and db_path.is_file():
                snapshot = backup_db(db_path, snapshot_dir, keep=keep).path
        except Exception:
            staged.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                staged.with_name(staged.name + suffix).unlink(missing_ok=True)
            raise

        # Every sidecar of both files goes before the swap: a ``-wal``/``-shm`` belongs to the
        # pre-restore database and would replay over it on next open (the journal switch above
        # already checkpointed them away unless ``--force`` got here with the database still in
        # WAL), and a hot ``-journal`` would roll back over the restored file the same way. The
        # safety snapshot already captured everything they committed.
        try:
            # Re-probe immediately before the swap: staging, verification and the safety
            # snapshot can take minutes, and a non-daemon opener (the viewer, a script) that
            # started in that window still holds the old inode - ``os.replace`` would strand
            # its commits on the unlinked file. The journal switch needs the database itself,
            # so a busy result refuses here, milliseconds before the swap, instead of after.
            _switch_live_db_to_rollback_journal(db_path, force=force)
            for suffix in ("-wal", "-shm", "-journal"):
                db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                staged.with_name(staged.name + suffix).unlink(missing_ok=True)
            os.replace(staged, db_path)
        except Exception:
            staged.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                staged.with_name(staged.name + suffix).unlink(missing_ok=True)
            raise

    backup_migrations = set(info.migrations)
    current = set(current_migrations)
    return RestoreResult(
        db_path=db_path,
        backup_path=backup_path,
        safety_snapshot=snapshot,
        added_migrations=tuple(sorted(backup_migrations - current)),
        dropped_migrations=tuple(sorted(current - backup_migrations)),
    )


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _print_backup_info(info: BackupInfo) -> None:
    print(
        f"{info.path}  {_fmt_bytes(info.size_bytes)}  pages={info.page_count}  "
        f"quick_check={info.quick_check}  migrations={len(info.migrations)}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.db_backup",
        description=(
            "Backup and restore the live SQLite database using the SQLite online backup "
            "API. See docs/live-deploy-runbook.md, 'Release / rollback'."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            default=None,
            help=(
                "SQLite database file (default: the file DATABASE_URL names, else "
                "DATA_DIR/backtests.db)"
            ),
        )
        p.add_argument(
            "--backup-dir",
            default=None,
            help="backup directory (default: <db parent>/backups)",
        )

    backup = sub.add_parser(
        "backup", help="snapshot the database now; safe while the daemon runs"
    )
    add_db_args(backup)
    backup.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"backups to keep per database (default {DEFAULT_KEEP})",
    )

    restore = sub.add_parser(
        "restore",
        help="restore the database from a backup file; refuses while the database may be in use",
    )
    restore.add_argument("backup_file", help="backup file to restore from")
    add_db_args(restore)
    restore.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"backups to keep, incl. the pre-restore snapshot (default {DEFAULT_KEEP})",
    )
    restore.add_argument(
        "--no-safety-snapshot",
        action="store_true",
        help="skip the automatic backup of the current database before overwriting it",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help=(
            "proceed even when a daemon may be running or the database is open elsewhere "
            "(false-positive escape hatch)"
        ),
    )

    listing = sub.add_parser("list", help="list this database's backups, oldest first")
    add_db_args(listing)

    verify = sub.add_parser("verify", help="check a backup file is a readable SQLite database")
    verify.add_argument("backup_file")

    status = sub.add_parser(
        "status", help="report whether a daemon appears to hold this database"
    )
    add_db_args(status)

    return parser


def _resolve_db(explicit: str | None) -> Path:
    return resolve_db_path(explicit)


def _resolve_backup_dir(explicit: str | None, db_path: Path) -> Path:
    return Path(explicit) if explicit else default_backup_dir(db_path)


def _cmd_verify(args: argparse.Namespace) -> int:
    info = verify_backup(Path(args.backup_file))
    _print_backup_info(info)
    for name in info.migrations:
        print(f"  migration: {name}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args.db)
    info = backup_db(
        db_path,
        _resolve_backup_dir(args.backup_dir, db_path),
        keep=args.keep,
    )
    _print_backup_info(info)
    print(f"kept newest {args.keep} backup(s) in {info.path.parent}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args.db)
    backup_dir = _resolve_backup_dir(args.backup_dir, db_path)
    backups = list_backups(backup_dir, db_path)
    if not backups:
        print(f"no backups for {db_path} in {backup_dir}")
        return 0
    for path in backups:
        try:
            _print_backup_info(verify_backup(path))
        except (BackupVerificationError, OSError, sqlite3.Error) as exc:
            print(f"{path}  INVALID: {exc}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args.db)
    check = check_daemon(db_path)
    print(f"database: {db_path}")
    print(f"lock file: {daemon_lock_path(db_path)}")
    print(
        f"lock held: {check.lock_held} "
        f"(authoritative={check.lock_authoritative})"
    )
    if check.processes is None:
        print("daemon processes: <scan unavailable>")
    elif check.processes:
        for cmdline in check.processes:
            print(f"daemon process: {cmdline}")
    else:
        print("daemon processes: none found")
    for note in check.notes:
        print(f"note: {note}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args.db)
    result = restore_db(
        db_path,
        Path(args.backup_file),
        backup_dir=_resolve_backup_dir(args.backup_dir, db_path),
        keep=args.keep,
        safety_snapshot=not args.no_safety_snapshot,
        force=args.force,
    )
    print(f"restored {result.db_path} from {result.backup_path}")
    if result.safety_snapshot is not None:
        print(f"pre-restore snapshot: {result.safety_snapshot}")
    if result.added_migrations:
        print(
            "migrations now present (absent before restore): "
            + ", ".join(result.added_migrations)
        )
    if result.dropped_migrations:
        print(
            "WARNING: migrations the previous database had are absent from the "
            "backup: " + ", ".join(result.dropped_migrations)
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "verify": _cmd_verify,
        "backup": _cmd_backup,
        "list": _cmd_list,
        "status": _cmd_status,
        "restore": _cmd_restore,
    }
    handler = handlers.get(args.command)
    if handler is None:
        return 2
    try:
        return handler(args)
    except DaemonRunningError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

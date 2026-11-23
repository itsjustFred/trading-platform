"""Cross-process single-instance guard (Linux, Windows, macOS).

The live daemon trades one shared SQLite database and one set of broker sessions, so exactly one
process may run it per database. This module is the guard, and it is built around two properties
that matter more than the lock itself:

* **It refuses instead of waiting.** A second daemon is a correctness problem (two processes
  migrating and trading one database), not a throughput problem, so the acquisition is
  non-blocking and the loser fails immediately.
* **The operating system owns the lock.** ``flock`` on POSIX and byte-range locking via ``msvcrt``
  on Windows are released when the holding process exits, including when it is SIGKILLed. There is
  no stale lock to steal or time out, so a killed daemon cannot block every later start. The lock
  *file* outlives a release on purpose: deleting it would race with a process about to create and
  lock its own, which is how lock files lose their exclusion.
"""

from __future__ import annotations

import contextlib
import errno
import os
import threading
from pathlib import Path
from typing import IO, Any

#: Errnos that mean "someone else holds it" rather than "this call is broken". ``fcntl.flock``
#: reports a held lock as EAGAIN; ``msvcrt.locking`` reports a held byte range as EACCES, and
#: EDEADLOCK for the same condition after a retry. Anything else (EACCES on a directory we cannot
#: write, EBADF, EINVAL) is a real error and must not masquerade as a busy lock.
_BUSY_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLOCK,
        errno.EWOULDBLOCK,
    }
)

#: Lock paths held by this process. The OS lock excludes other processes; this excludes a second
#: acquisition in the same one, because a daemon started twice inside one process (an embedder, a
#: test) is just as wrong - and because not every platform refuses that at the OS level.
_HELD_PATHS: set[Path] = set()
_HELD_PATHS_LOCK = threading.Lock()


class SingleInstanceError(RuntimeError):
    """Raised when another process already holds the single-instance lock.

    Carries the lock path so a caller, and an operator reading the daemon's log, can see which
    file is held without having to guess which data directory the other daemon used.
    """

    def __init__(self, lock_path: str | Path) -> None:
        lock_path = Path(lock_path)
        super().__init__(
            f"another live daemon is already running (single-instance lock held: {lock_path}); "
            "stop it before starting another one"
        )
        self.lock_path = lock_path


class SingleInstanceLock:
    """An exclusive, non-blocking, cross-process lock on one file.

    Used as a context manager (``with SingleInstanceLock(path):``) or with explicit
    ``acquire()``/``release()``. ``release()`` is idempotent, so a ``finally`` block can call it
    without checking whether ``acquire()`` succeeded, and an in-process re-acquisition of a held
    path is refused too.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._handle is not None

    def acquire(self) -> SingleInstanceLock:
        """Take the lock, or raise :class:`SingleInstanceError` when it is held.

        Returns ``self`` so ``SingleInstanceLock(path).acquire().release()`` reads as one step.
        """
        with _HELD_PATHS_LOCK:
            if self.lock_path in _HELD_PATHS:
                raise SingleInstanceError(self.lock_path)
            handle = self._open_and_lock()
            _HELD_PATHS.add(self.lock_path)
            self._handle = handle
        return self

    def release(self) -> None:
        """Release the lock; a no-op when this object does not hold it.

        Unlock errors are suppressed deliberately: closing the handle releases the lock on both
        platforms, and an unlock failure must never mask the exception the body raised.
        """
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        with _HELD_PATHS_LOCK:
            _HELD_PATHS.discard(self.lock_path)
        try:
            if os.name == "nt":
                _unlock_windows(handle)
            else:
                _unlock_posix(handle)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    def _open_and_lock(self) -> IO[bytes]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # "a+b": the file is created if missing, and appends cannot truncate a byte another
        # process is locking.
        handle = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                _lock_windows(handle)
            else:
                _lock_posix(handle)
        except OSError as exc:
            with contextlib.suppress(OSError):
                handle.close()
            if exc.errno in _BUSY_ERRNOS:
                raise SingleInstanceError(self.lock_path) from exc
            raise
        return handle


#: Locks this process keeps for its lifetime. A daemon whose worker join was interrupted leaves its
#: lock here instead of releasing it: the coroutine frame, the task and the traceback that held the
#: last reference all die with the exception, and garbage collection would then close the
#: descriptor and release the OS lock while the workers are still writing.
_PROCESS_LIFETIME_LOCKS: list[SingleInstanceLock] = []


def keep_for_process_lifetime(lock: SingleInstanceLock) -> None:
    """Anchor ``lock`` for the process's lifetime, so its OS lock cannot be collected away."""
    _PROCESS_LIFETIME_LOCKS.append(lock)


def process_lifetime_locks() -> tuple[SingleInstanceLock, ...]:
    """The locks anchored for this process's lifetime (diagnostics and tests)."""
    return tuple(_PROCESS_LIFETIME_LOCKS)


def _lock_posix(handle: IO[bytes]) -> None:
    """Exclusive, non-blocking ``flock``; the kernel drops it when this process dies."""
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_windows(handle: IO[bytes]) -> None:
    """Lock byte 0 with ``msvcrt``; Windows releases byte-range locks when the handle closes.

    The byte-range lock needs a byte to lock and locks from the current file position, so an empty
    file gets one NUL byte and the position is rewound first - the same dance as
    ``live_runner/ctrader_adapter._exclusive_file_lock``, which does this in production.
    """
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_posix(handle: IO[bytes]) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _unlock_windows(handle: IO[bytes]) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

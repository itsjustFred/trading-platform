"""The single-instance lock the live daemon runs under (#310).

The lock has one job: keep a second live daemon away from a live database that another daemon is
already migrating and trading. Two properties make that real, and both are pinned here across
process boundaries, not just in-process:

* **It excludes another process.** A daemon in a second process must be refused while the first
  holds the lock. In-process checks alone would not prove that: the whole point of the lock is what
  happens between two OS processes that share one SQLite file.
* **A dead holder does not block a restart.** ``flock`` on POSIX and byte-range locking via
  ``msvcrt`` on Windows are owned by the operating system and released when the holding process
  dies, including on SIGKILL. A lock that survived a killed daemon would trade a transient outage
  for a permanent one, so the release semantics are the load-bearing part, not the acquisition.

The lock *file* stays behind on release: deleting it would race with a process that is about to
create and lock its own, which is how lock files lose their exclusion. The file is a handle, not
the record of the lock.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.single_instance import SingleInstanceError, SingleInstanceLock
from tests.helpers.lock_holder import await_held, spawn_holder


class TestAcquireAndRelease:
    def test_acquire_creates_the_lock_file_and_its_parent_directory(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "missing-dir" / "backtests.db.lock"

        SingleInstanceLock(lock_path).acquire().release()

        assert lock_path.is_file(), "the lock file was not created"
        assert lock_path.parent.is_dir(), "the lock file's directory was not created"

    def test_the_lock_file_outlives_a_release(self, tmp_path: Path) -> None:
        """Deleting the lock file on release would race with the next acquirer (classic bug)."""
        lock_path = tmp_path / "backtests.db.lock"

        SingleInstanceLock(lock_path).acquire().release()

        assert lock_path.is_file(), (
            "the lock file was deleted on release; a concurrent acquirer would create a second "
            "file and both would believe they hold the lock"
        )

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        lock = SingleInstanceLock(tmp_path / "backtests.db.lock")
        lock.acquire()

        lock.release()
        lock.release()  # must not raise: finally blocks release unconditionally

        assert not lock.held

    def test_the_context_manager_releases_on_an_exception(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "backtests.db.lock"
        lock = SingleInstanceLock(lock_path)

        def _body() -> None:
            with lock:
                raise ValueError("daemon body failed")

        with pytest.raises(ValueError):
            _body()

        SingleInstanceLock(lock_path).acquire().release()

    def test_reacquiring_in_the_same_process_is_refused(self, tmp_path: Path) -> None:
        """Two daemons inside one process are as wrong as two across processes."""
        lock = SingleInstanceLock(tmp_path / "backtests.db.lock")
        lock.acquire()
        try:
            contender = SingleInstanceLock(lock.lock_path)
            with pytest.raises(SingleInstanceError) as excinfo:
                contender.acquire()
        finally:
            lock.release()
        assert str(lock.lock_path) in str(excinfo.value)

    def test_the_same_lock_can_be_taken_again_after_release(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "backtests.db.lock"

        first = SingleInstanceLock(lock_path).acquire()
        first.release()

        second = SingleInstanceLock(lock_path).acquire()
        second.release()


class TestCrossProcessExclusion:
    def test_a_lock_held_in_another_process_is_refused(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "backtests.db.lock"
        holder = spawn_holder(lock_path, hold_seconds=15.0)
        try:
            await_held(holder)

            contender = SingleInstanceLock(lock_path)
            with pytest.raises(SingleInstanceError) as excinfo:
                contender.acquire()
            assert str(lock_path) in str(excinfo.value)
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_a_holder_that_exited_releases_the_lock(self, tmp_path: Path) -> None:
        """A clean exit releases too: the daemon's shutdown path must not strand the lock."""
        lock_path = tmp_path / "backtests.db.lock"
        holder = spawn_holder(lock_path, hold_seconds=0.5)
        try:
            await_held(holder)
        finally:
            holder.wait(timeout=10)

        assert holder.returncode == 0
        SingleInstanceLock(lock_path).acquire().release()

    @pytest.mark.skipif(
        os.name == "nt", reason="creating a symlink needs elevated privileges on Windows"
    )
    def test_a_lock_held_through_a_symlink_is_refused_at_the_target(self, tmp_path: Path) -> None:
        """Two spellings of one lock file are one lock.

        A deployment may reach its data directory through a symlink. Both spellings address the
        same inode, and the OS lock belongs to the inode, so a holder that acquired through the
        alias must still refuse a process that goes through the target.
        """
        real_path = tmp_path / "real" / "backtests.db.lock"
        real_path.parent.mkdir()
        alias_dir = tmp_path / "linked"
        alias_dir.symlink_to(real_path.parent, target_is_directory=True)
        alias_path = alias_dir / "backtests.db.lock"

        held = SingleInstanceLock(alias_path)
        held.acquire()
        try:
            contender = SingleInstanceLock(real_path)
            with pytest.raises(SingleInstanceError):
                contender.acquire()
        finally:
            held.release()

    def test_a_killed_holder_does_not_block_a_restart(self, tmp_path: Path) -> None:
        """The stale-lock case: the lock must be owned by the OS, not by a marker file.

        A daemon killed with SIGKILL (OOM, a crash, ``kill -9``) leaves its lock file behind. If the
        lock itself survived the process, every later start would be refused until an operator
        deleted the file by hand.
        """
        lock_path = tmp_path / "backtests.db.lock"
        holder = spawn_holder(lock_path, hold_seconds=60.0)
        try:
            await_held(holder)
            holder.kill()
            holder.wait(timeout=10)
        finally:
            if holder.poll() is None:  # pragma: no cover - only on a failed kill
                holder.kill()
                holder.wait(timeout=10)

        SingleInstanceLock(lock_path).acquire().release()

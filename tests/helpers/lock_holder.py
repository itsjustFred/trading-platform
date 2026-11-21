"""Hold a single-instance lock in a separate process (test helper for #310).

Run as ``python -m tests.helpers.lock_holder <lock_path> <hold_seconds>`` from the repo root: the
lock is acquired, ``held`` is printed once the OS has granted it - the parent reads that line as
the readiness signal, because a lock file's mere existence says nothing about who holds its lock -
and the lock is released when the file descriptor closes on exit.

A holder that is killed instead never releases: that is how the stale-lock tests exercise a dead
holder, which is the state a SIGKILLed daemon leaves behind.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from shared.single_instance import SingleInstanceLock


def main(argv: list[str]) -> int:
    lock_path = Path(argv[1])
    hold_seconds = float(argv[2])
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    print("held", flush=True)
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Hold a single-instance lock in a separate process (test helper for #310).

Run as ``python -m tests.helpers.lock_holder <lock_path> <hold_seconds>`` from the repo root: the
lock is acquired, ``held`` is printed once the OS has granted it - the parent reads that line as
the readiness signal, because a lock file's mere existence says nothing about who holds its lock -
and the lock is released when the file descriptor closes on exit.

A holder that could not acquire the lock prints ``refused`` instead: that is the negative readiness
signal, and a caller waiting on the line never has to distinguish it from a dead process. A holder
that is killed prints neither and never releases - that is how the stale-lock tests exercise a dead
holder, which is the state a SIGKILLed daemon leaves behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from shared.single_instance import SingleInstanceError, SingleInstanceLock

REPO_ROOT = Path(__file__).resolve().parents[2]


def spawn_holder(lock_path: Path, hold_seconds: float) -> Any:
    """Start a holder process that acquires ``lock_path`` and holds it for ``hold_seconds``."""
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


def read_holder_line(holder: Any, timeout: float = 30.0) -> str:
    """The holder's first line (``held`` or ``refused``), bounded by ``timeout``.

    A plain ``readline()`` blocks forever if the holder stalls silently, which fails a test only
    through the suite-wide timeout and leaks the holder. The reader is a daemon thread so an expired
    wait can kill the process; a killed holder prints nothing, and a caller waiting for ``held``
    must not mistake that for a ``refused`` (a real refusal still prints its line).
    """
    line: list[str] = []
    reader = threading.Thread(
        target=lambda: line.append(holder.stdout.readline().strip() if holder.stdout else ""),
        daemon=True,
    )
    reader.start()
    reader.join(timeout)
    return line[0] if line else ""


def await_held(holder: Any) -> None:
    """Block until the holder reports the lock is granted; fail loudly if it died instead."""
    line = read_holder_line(holder)
    if line != "held":
        holder.kill()
        holder.wait(timeout=10)
        stderr = holder.stderr.read() if holder.stderr is not None else ""
        raise AssertionError(
            f"the holder never acquired the lock (line={line!r}, returncode={holder.poll()}): "
            f"{stderr}"
        )

def main(argv: list[str]) -> int:
    lock_path = Path(argv[1])
    hold_seconds = float(argv[2])
    lock = SingleInstanceLock(lock_path)
    try:
        lock.acquire()
    except SingleInstanceError:
        print("refused", flush=True)
        return 0
    print("held", flush=True)
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
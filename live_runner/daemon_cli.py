"""The live daemon's command line, deliberately free of heavy imports (#310).

``python -m live_runner.daemon`` *executes* ``live_runner/daemon.py``, so every module-level
import in that module runs before any statement in it: pandas, the broker SDKs, ``shared.database``
and the logger factory. Parsing the arguments down there would make a template
``python -m live_runner.daemon --help`` open the live database, run the schema and data
migrations, and connect brokers - which is what an agent got on 2026-11-21 when it inspected the
entry point and started a second daemon next to a running one.

So the parsing - and everything that can refuse a start, including the single-instance guard the
daemon takes - happens here, in a module whose imports are the standard library plus the
dependency-free ``shared.single_instance``, and the heavy ``live_runner.daemon`` module is imported
only once the arguments say "start".

Exit codes: 0 (clean exit, or ``--help``), 1 (anything the daemon itself raised), 2 (argparse usage
error), 3 (the single-instance lock is held by another daemon), 4 (the daemon's worker threads are
still running and may still be writing, so its outcome is unknown), 130 (the conventional SIGINT
code, when a Ctrl-C reaches the CLI - the process leaves through the same immediate exit so the
interpreter's unbounded thread-join stays out of the path).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Sequence
from typing import NoReturn

from shared.single_instance import SingleInstanceError

#: argparse exits 2 on a usage error, so the single-instance refusal gets its own code: an
#: operator - and a ``systemctl`` unit with ``Restart=always`` - can tell "another daemon is
#: already running" from "the command was wrong".
EXIT_ALREADY_RUNNING = 3

#: The daemon stopped but its worker threads are still running. The lock stays held by the dying
#: process (the OS releases it on death), so a unit with ``Restart=always`` picks the daemon up on
#: the next pass instead of sitting on a process that no longer trades anything.
EXIT_WORKER_JOIN_TIMEOUT = 4

DAEMON_PROG = "python -m live_runner.daemon"


def build_parser() -> argparse.ArgumentParser:
    """The daemon's CLI. It takes no options on purpose: deployments come from the database.

    ``live_deployments.yaml`` is preflight input, not daemon input, so there is deliberately no
    ``--config``: the daemon syncs ACTIVE rows from the ``live_deployments`` table. An unknown
    option is a usage error (exit 2), never a silently ignored argument.
    """
    return argparse.ArgumentParser(
        prog=DAEMON_PROG,
        description=(
            "Run the live trading daemon: it syncs ACTIVE deployments from the database, runs "
            "their signal sources and submits broker orders. Only one daemon may run per "
            "database; a second start is refused with exit code 3."
        ),
        epilog="Documentation: docs/live-deploy-runbook.md, section 'Demo Daemon'.",
    )


def _leave_now(exit_code: int) -> NoReturn:
    """Leave the process immediately, so the daemon can report ``exit_code`` at all.

    The interpreter's own teardown would join every worker thread the daemon gave up on - with no
    deadline - which is the hang this exit path exists to avoid. Flushing the log handlers is on
    this side of the exit because ``os._exit`` runs no ``atexit`` hook, and a CRITICAL line that
    never left the process is no use to an operator reading a journal. The service loggers do not
    propagate to the root logger, so every logger's handlers are flushed, not just the root's.
    """
    loggers = [logging.getLogger(), *logging.root.manager.loggerDict.values()]
    for logger in loggers:
        for handler in getattr(logger, "handlers", []):
            # BaseException, not Exception: a KeyboardInterrupt landing inside ``flush()`` must
            # not skip the ``os._exit`` this function exists to reach.
            with contextlib.suppress(BaseException):
                handler.flush()
    with contextlib.suppress(BaseException):
        # ``os._exit`` runs no ``atexit`` hook, so the Sentry client's own flush never happens:
        # give the queued CRITICAL event its last chance to leave. Imported here because this
        # module stays free of heavy imports until a start is certain, and the flush is a no-op
        # when Sentry was never initialized.
        import sentry_sdk

        sentry_sdk.flush(timeout=2)
    os._exit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, then start the daemon. Returns the process exit code."""
    build_parser().parse_args(argv)  # --help exits 0, an unknown argument exits 2: both here

    # Imported only now: a start is the only path that needs the heavy module.
    from live_runner import daemon

    try:
        asyncio.run(daemon.run_daemon())
    except SingleInstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except daemon.WorkerJoinTimeout as exc:
        print(f"error: {exc}", file=sys.stderr)
        _leave_now(EXIT_WORKER_JOIN_TIMEOUT)
    except KeyboardInterrupt:
        # A second Ctrl-C can land while the join is still waiting on the abandoned worker
        # threads: by the time it reaches here the daemon's own cleanup has run (or given up,
        # with the executor detached), so leaving through ``os._exit`` is what keeps the
        # interpreter's unbounded thread-join out of the exit path. 130 is the SIGINT code.
        _leave_now(130)
    return 0

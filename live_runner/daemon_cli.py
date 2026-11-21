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
error), 3 (the single-instance lock is held by another daemon).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from shared.single_instance import SingleInstanceError

#: argparse exits 2 on a usage error, so the single-instance refusal gets its own code: an
#: operator - and a ``systemctl`` unit with ``Restart=always`` - can tell "another daemon is
#: already running" from "the command was wrong".
EXIT_ALREADY_RUNNING = 3

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
    return 0

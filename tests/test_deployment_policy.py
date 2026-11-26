"""The shared "can the daemon trade this deployment?" policy (#242).

The daemon, the YAML preflight and the ``--check-db`` preflight all answer the
same question and must not disagree. These tests pin the definition itself, and
pin that the two read-only preflights get it without importing ``live_runner.daemon``
(which would initialise Sentry and load both broker adapters into a process that
used to touch sqlite only).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from live_runner.deployment_policy import (
    DEFAULT_SIGNAL_SOURCE,
    KNOWN_SIGNAL_SOURCES,
    TRADEABLE_SIGNAL_SOURCES,
    normalize_deployment_status,
    normalize_signal_source,
    signal_source_can_trade,
    untradeable_signal_source_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_signal_source_has_a_listener_runner() -> None:
    """Discord ingestion was removed: nothing the daemon listens to remains.

    ``internal`` deployments are traded by the internal strategy runner on its
    own opt-in, not by a signal listener, so the listener set is empty.
    """
    assert TRADEABLE_SIGNAL_SOURCES == frozenset()
    assert not signal_source_can_trade("discord")
    assert not signal_source_can_trade("Discord")
    assert not signal_source_can_trade("internal")
    assert not signal_source_can_trade("")
    assert not signal_source_can_trade(None)
    assert not signal_source_can_trade("webhook")


def test_status_is_a_known_one_or_unknown() -> None:
    assert normalize_deployment_status("ACTIVE") == "ACTIVE"
    assert normalize_deployment_status("paused") == "PAUSED"
    assert normalize_deployment_status("STOPPED") == "STOPPED"
    assert normalize_deployment_status("DISABLED") == "UNKNOWN"
    assert normalize_deployment_status("") == "UNKNOWN"
    assert normalize_deployment_status(None) == "UNKNOWN"


def test_the_recognised_vocabulary_contains_the_default_and_the_retired_source() -> None:
    """Stored discord rows must keep loading, so the recognised set is wider
    than the (empty) tradeable set: ``internal`` plus the retired ``discord``.

    The DB preflight errors (exit 1) on a source outside the recognised set, so
    a stored value the rest of the system cannot spell must land here too.
    """
    assert KNOWN_SIGNAL_SOURCES == frozenset({"internal", "discord"})
    assert TRADEABLE_SIGNAL_SOURCES <= KNOWN_SIGNAL_SOURCES
    assert DEFAULT_SIGNAL_SOURCE in KNOWN_SIGNAL_SOURCES
    assert DEFAULT_SIGNAL_SOURCE not in TRADEABLE_SIGNAL_SOURCES


def test_retired_discord_reason_names_the_retirement() -> None:
    reason = untradeable_signal_source_reason("discord")
    assert reason is not None
    assert "retired" in reason


def test_internal_reason_names_the_runner_opt_in() -> None:
    reason = untradeable_signal_source_reason("internal")
    assert reason is not None
    assert "internal strategy runner" in reason
    assert "opted in" in reason


def test_unknown_source_reason_names_the_stored_source() -> None:
    reason = untradeable_signal_source_reason("webhook")
    assert reason == "signal source 'webhook' is not a known signal source and has no runner"


def test_the_empty_source_is_the_internal_default() -> None:
    # The DB column and the pydantic model both default to 'internal'.
    assert normalize_signal_source("") == "internal"
    assert normalize_signal_source(None) == "internal"
    assert normalize_signal_source("Internal") == "internal"
    assert normalize_signal_source("discord") == "discord"
    assert "internal strategy runner" in (untradeable_signal_source_reason("") or "")


def test_preflights_import_the_policy_not_the_daemon() -> None:
    """Both read-only preflights stay read-only imports (#242 blocker).

    ``live_runner.preflight_db`` is a sqlite-only module: importing the daemon
    pulled in both broker adapters and the Discord stack, and called
    ``initialize_sentry("live-runner")`` on load, so a ``--check-db`` run with a
    DSN configured would emit daemon-named Sentry events for a preflight.
    """
    source = (
        "import sys\n"
        "import live_runner.deployment_policy\n"
        "import {module}\n"
        "assert 'live_runner.daemon' not in sys.modules, 'imports the daemon'\n"
    )
    for module in ("live_runner.preflight", "live_runner.preflight_db"):
        result = subprocess.run(
            [sys.executable, "-c", source.format(module=module)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"{module}: {result.stderr[-2000:]}"


def test_the_db_preflight_stays_a_sqlite_only_import() -> None:
    """``--check-db`` is a read-only module: no broker adapter, no Discord, no Sentry.

    It read sqlite with ``mode=ro`` until it needed the daemon's reason function,
    which dragged the live stack (and Sentry, under the daemon's service name)
    into a diagnostic that used to run on a bare interpreter.
    """
    forbidden = (
        "live_runner.alpaca_adapter",
        "live_runner.ctrader_adapter",
        "live_runner.signal_sources.discord_source",
        "requests",
        "sentry_sdk",
    )
    checks = "\n".join(
        f"assert {name!r} not in sys.modules, 'imports {name}'" for name in forbidden
    )
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\nimport live_runner.preflight_db\n{checks}\n"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]

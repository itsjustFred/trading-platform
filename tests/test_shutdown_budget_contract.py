"""The shutdown budget, as three artifacts that must not drift apart.

The live daemon's bounded shutdown is a promise with a number attached: the
drain plus one deadline per cleanup phase, ``WORST_CASE_SHUTDOWN_SECONDS`` in
``live_runner/shutdown.py``. systemd has to believe it (``TimeoutStopSec`` in
the unit ``scripts/setup_services.sh`` generates, or the daemon is SIGKILLed
before it can report its exit code) and an operator has to believe it (the
figure in ``docs/live-deploy-runbook.md``).

A number that lives in three places drifts unless something holds it together,
so these tests pin the code constant, the runbook figure and the generated unit
to each other - and pin the phase count to the phases the cleanup really runs,
so a phase added or dropped cannot quietly lengthen the shutdown past the budget
the unit file was written for.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

import live_runner.daemon as daemon
import live_runner.shutdown as shutdown_module
from live_runner.shutdown import (
    CLEANUP_PHASE_COUNT,
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    EXIT_PATH_TIMEOUT_SECONDS,
    EXIT_PATH_WINDOW_SECONDS,
    SENTRY_FLUSH_TIMEOUT_SECONDS,
    WORST_CASE_SHUTDOWN_SECONDS,
    ShutdownCoordinator,
)

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "live-deploy-runbook.md"
SETUP_SCRIPT = ROOT / "scripts" / "setup_services.sh"


class _RecordingScheduler:
    """Scheduler stand-in: this file only observes the phases it stops."""

    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


def _runbook_section(heading: str) -> str:
    """One runbook section, from its heading to the next heading of any level."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert f"## {heading}" in text, f"the runbook has no '{heading}' section"
    tail = text.split(f"## {heading}", 1)[1]
    return re.split(r"\n#{2,3} ", tail, maxsplit=1)[0]


def _unit_block(script: str, unit_name: str) -> str:
    """The body of one heredoc-generated systemd unit file."""
    marker = f"> /etc/systemd/system/{unit_name}"
    assert marker in script, f"the script does not generate {unit_name}"
    return script.split(marker, 1)[1].split("\ncat <<EOF", 1)[0]


def test_the_budget_is_the_drain_plus_one_deadline_per_phase() -> None:
    """The published worst case is computed, not retold."""
    assert CLEANUP_PHASE_COUNT > 0
    assert DEFAULT_DRAIN_TIMEOUT_SECONDS > 0
    assert DEFAULT_CLEANUP_TIMEOUT_SECONDS > 0
    assert WORST_CASE_SHUTDOWN_SECONDS == (
        DEFAULT_DRAIN_TIMEOUT_SECONDS + CLEANUP_PHASE_COUNT * DEFAULT_CLEANUP_TIMEOUT_SECONDS
    )


def test_the_cleanup_runs_as_many_bounded_phases_as_the_budget_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measure the phases: the budget is only true if these are the phases.

    Each of the six deadlines really has to be reached inside one shutdown, and
    a phase someone adds (or a deadline someone drops) changes the worst case
    without changing the arithmetic - so this test counts what the cleanup
    actually runs and names it, rather than trusting the constant.
    """
    coordinator = ShutdownCoordinator(force_exit_fn=lambda code: None)
    phases: list[str] = []
    real_bounded = daemon._bounded

    async def _record(
        awaitable: Any, *, timeout: float, phase: str
    ) -> bool:
        phases.append(phase)
        return await real_bounded(awaitable, timeout=timeout, phase=phase)

    # The release itself is exercised elsewhere; here it must not touch whatever
    # reactor the test process happens to have installed.
    monkeypatch.setattr(shutdown_module, "release_twisted_reactor_threads", lambda: None)

    async def _probe() -> None:
        coordinator.bind(asyncio.get_running_loop())
        monkeypatch.setattr(daemon, "_bounded", _record)
        await daemon._shutdown_cleanup(
            loop_tasks=(),
            active_listener_tasks={},
            active_discord_tasks={},
            active_discord_bot_tasks={},
            active_discord_bots={},
            active_adapters={},
            scheduler=_RecordingScheduler(),  # type: ignore[arg-type]
            shutdown=coordinator,
            drain_timeout=1.0,
            cleanup_timeout=1.0,
        )

    asyncio.run(_probe())

    assert phases == [
        "cancel_loops",
        "cancel_listeners",
        "disconnect_discord",
        "stop_scheduler",
        "disconnect_adapters",
        "release_reactor",
    ]
    assert len(phases) == CLEANUP_PHASE_COUNT


def test_the_runbook_states_the_budget_the_code_actually_has() -> None:
    """The operator-facing figure has to be the same one the daemon runs on."""
    section = _runbook_section("Stopping the daemon")
    drain = re.search(r"(\d+(?:\.\d+)?) s \(drain\)", section)
    per_phase = re.search(r"(\d+(?:\.\d+)?) s for each of the (\d+) cleanup phases", section)
    total = re.search(r"(\d+(?:\.\d+)?) s in total", section)
    assert drain is not None, "the runbook must state the drain it documents"
    assert per_phase is not None, "the runbook must state the per-phase deadline and the count"
    assert total is not None, "the runbook must state the worst case it documents"

    assert float(drain.group(1)) == DEFAULT_DRAIN_TIMEOUT_SECONDS
    assert float(per_phase.group(1)) == DEFAULT_CLEANUP_TIMEOUT_SECONDS
    assert int(per_phase.group(2)) == CLEANUP_PHASE_COUNT
    assert float(total.group(1)) == WORST_CASE_SHUTDOWN_SECONDS, (
        "the documented worst case must be the computed one"
    )
    exit_path = re.search(r"at most (\d+(?:\.\d+)?) s \(`EXIT_PATH_TIMEOUT_SECONDS`\)", section)
    assert exit_path is not None, "the runbook must state the exit path's own deadline"
    assert float(exit_path.group(1)) == EXIT_PATH_TIMEOUT_SECONDS, (
        "the documented exit path must be the computed one"
    )
    sentry_flush = re.search(
        r"at most (\d+(?:\.\d+)?) s \(`SENTRY_FLUSH_TIMEOUT_SECONDS`\)", section
    )
    assert sentry_flush is not None, "the runbook must state the flush the exit path pays"
    assert float(sentry_flush.group(1)) == SENTRY_FLUSH_TIMEOUT_SECONDS, (
        "the documented flush must be the computed one"
    )


def test_the_exit_path_is_bounded_and_short() -> None:
    """The tail after ``run_daemon`` returns is bounded, and it is not a phase.

    The drain and the six phases are the deadlines a shutdown needs. The exit
    path only has to let a cancelled task unwind, so it waits for a bounded
    grace rather than re-spending a phase's budget on work the phases already
    abandoned - but it must still have a figure of its own, because that figure
    is what the operator's window is computed from.
    """
    assert 0 < EXIT_PATH_TIMEOUT_SECONDS < DEFAULT_CLEANUP_TIMEOUT_SECONDS


def test_the_exit_window_covers_the_flush_the_exit_pays() -> None:
    """``main`` flushes Sentry before it leaves, so the window has to include it.

    The process leaves through ``os._exit``, which runs no ``atexit`` hook, so
    the SDK's queue is flushed explicitly and that flush is a network wait. It
    adds its own deadline to the exit path rather than being unbounded, and the
    two together are what the unit's stop timeout has to stay above.
    """
    assert 0 < SENTRY_FLUSH_TIMEOUT_SECONDS < DEFAULT_CLEANUP_TIMEOUT_SECONDS
    assert EXIT_PATH_WINDOW_SECONDS == (
        EXIT_PATH_TIMEOUT_SECONDS + SENTRY_FLUSH_TIMEOUT_SECONDS
    )


def test_the_generated_daemon_unit_outlives_the_worst_case_shutdown() -> None:
    """The installer's unit must not SIGKILL the daemon mid-shutdown.

    systemd's default ``TimeoutStopSec`` is 90 s. That is shorter than the
    bounded shutdown, so the daemon would be killed before it could report its
    exit code - the operator would never learn that a broker outcome was left
    unknown, which is the signal the whole bounded shutdown exists to give.
    """
    unit = _unit_block(SETUP_SCRIPT.read_text(encoding="utf-8"), "trading-daemon.service")
    timeout = re.search(r"^TimeoutStopSec=(\d+(?:\.\d+)?)$", unit, re.MULTILINE)
    assert timeout is not None, (
        "trading-daemon.service must set TimeoutStopSec above the shutdown budget"
    )
    seconds = float(timeout.group(1))
    assert seconds > WORST_CASE_SHUTDOWN_SECONDS, (
        f"TimeoutStopSec={seconds:g} is inside the {WORST_CASE_SHUTDOWN_SECONDS:g} s worst case"
    )
    # The window is the whole shutdown, the bounded exit path included - the loop
    # close and the flush that follows it both: a daemon that is SIGKILLed in its
    # tail is killed after it has already done all of its work, which loses the
    # exit code just the same.
    assert seconds >= WORST_CASE_SHUTDOWN_SECONDS + EXIT_PATH_WINDOW_SECONDS, (
        f"TimeoutStopSec={seconds:g} is inside the "
        f"{WORST_CASE_SHUTDOWN_SECONDS + EXIT_PATH_WINDOW_SECONDS:g} s shutdown window"
    )
    # The reviewed value: the worst case plus 30 s for the exit path itself.
    assert seconds == 180.0
    # And the runbook tells the operator the unit file it generates, so a
    # hand-written unit can be checked against the same number. Compare the whole
    # number, not a substring: "TimeoutStopSec=180" is also a prefix of the
    # dangerous "TimeoutStopSec=1800" and of the insufficient "TimeoutStopSec=18".
    documented = re.search(
        r"TimeoutStopSec=(\d+(?:\.\d+)?)", _runbook_section("Stopping the daemon")
    )
    assert documented is not None, "the runbook must name the unit's TimeoutStopSec"
    assert float(documented.group(1)) == seconds, (
        f"the runbook documents TimeoutStopSec={documented.group(1)} but the "
        f"installer writes {seconds:g}"
    )

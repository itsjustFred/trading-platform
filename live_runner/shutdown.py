"""Graceful, bounded shutdown for the live daemon.

Shutdown contract (see ``live_runner.daemon.run_daemon``):

1. The first SIGINT/SIGTERM stops the daemon accepting new work: an order
   submission, a trailing-stop amendment and a STOPPED enforcement sweep are
   all refused once the flag is set.
2. Broker work that is already in flight is allowed to finish, up to a
   timeout. Cancelling it mid-request would leave the broker outcome unknown.
   Every kind of in-flight broker work shares one ledger (the coordinator's
   in-flight count) so one bounded drain waits for all of them, before
   anything is cancelled.
3. Broker and signal-source connections are closed and the Twisted reactor's
   thread pool is released so the interpreter can exit.
4. Exit code 0 on a clean shutdown, non-zero when a cleanup phase timed out or
   failed. After a non-zero exit the outcome of the broker work that did not
   finish (an order submission, a stop amendment, a stopped-deployment sweep) is
   unknown and needs reconciliation.
5. A second signal forces an immediate exit.

Every step is bounded: the drain gets ``DEFAULT_DRAIN_TIMEOUT_SECONDS`` and each
of the ``CLEANUP_PHASE_COUNT`` cleanup phases its own
``DEFAULT_CLEANUP_TIMEOUT_SECONDS``, so a whole shutdown fits inside
``WORST_CASE_SHUTDOWN_SECONDS``. That figure is what the deploy runbook
documents and what the generated systemd unit's stop timeout has to stay above
(``scripts/setup_services.sh``); ``tests/test_shutdown_budget_contract.py``
pins the three together so they cannot drift apart.

"A whole shutdown" has to include the exit path - everything the daemon does
after ``run_daemon`` returned and reported its verdict (see
:func:`close_the_loop_bounded` and ``live_runner.daemon.main``), because that is
where the interpreter would otherwise wait, with no bound, for whatever the
bounded phases already gave up on. Bounding the loop close is half of it: the
other half is that ``main`` then leaves the process immediately, because the
interpreter's own teardown would join every thread the bounded close could not
reach.

Shutdown NEVER cancels, closes or modifies open orders or positions at the
broker. Positions belong to the strategy's own exit logic and the operator;
a restart must find the account exactly as the daemon left it. The only
broker mutations a shutdown lets run are ones the daemon had already started
before the flag - and it lets them finish.

Windows: ``SIGINT`` (Ctrl-C / Ctrl-Break) is delivered normally. ``SIGTERM`` is
defined by the ``signal`` module but Windows has no way to deliver it (service
managers use ``TerminateProcess``, which cannot be caught), so installing a
handler is harmless but it will never fire there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType

from shared.logging import get_logger

logger = get_logger("live_runner.shutdown")

DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 20.0
EXIT_OK = 0
EXIT_CLEANUP_TIMEOUT = 1
FORCED_EXIT_CODE = 130
# A daemon that crashed also leaves non-zero. It is the number the interpreter
# itself would have used for the unhandled exception this path replaces (see
# ``live_runner.daemon.main``), so nothing watching the service manager sees a
# change in what a crash reports.
EXIT_CRASH = 1

# How many bounded cleanup phases run after the drain, in order: the daemon loop
# cancel, the listener/Discord cancel, the Discord close, the scheduler stop, the
# adapter disconnect, the signal-archive close and the Twisted reactor release.
# Each gets its own ``DEFAULT_CLEANUP_TIMEOUT_SECONDS``, and the shutdown budget
# the runbook and the systemd unit pin is derived from how many there are - see
# ``WORST_CASE_SHUTDOWN_SECONDS``.
CLEANUP_PHASE_COUNT = 7

# The longest a bounded shutdown can take: the drain, then one deadline per
# cleanup phase. A service manager that stops the daemon (``systemctl stop`` →
# SIGTERM, then SIGKILL at ``TimeoutStopSec``) must stay above this or it kills
# the daemon before it can report its exit code, which is the signal an
# operator needs when a broker outcome was left unknown. Kept here, next to the
# timeouts, so the number the runbook and the unit file quote is computed rather
# than retold.
WORST_CASE_SHUTDOWN_SECONDS = (
    DEFAULT_DRAIN_TIMEOUT_SECONDS + CLEANUP_PHASE_COUNT * DEFAULT_CLEANUP_TIMEOUT_SECONDS
)

# How much of the budget the exit path may still spend: the tail that runs after
# ``run_daemon`` has returned its verdict (``main`` -> ``close_the_loop_bounded``).
# It is not a phase with work of its own - every deadline a shutdown actually
# needed already happened inside the drain and the phases - so what this covers
# is a cancelled task unwinding (its own ``finally``, one more loop iteration),
# which is cooperative and takes iterations rather than a deadline. Anything
# still running here was already abandoned by the phase that owned it and has
# had its outcome logged, so the exit path must not re-spend that phase's budget
# on it: it stays small, and the worst case plus this figure is still inside the
# ``TimeoutStopSec`` the generated unit sets.
EXIT_PATH_TIMEOUT_SECONDS = 2.0

# The bounded flush of the Sentry queue that precedes the exit: ``main`` leaves
# through ``os._exit``, which runs no ``atexit`` hook, so without an explicit
# flush the ERROR events this shutdown emitted (a drain timeout, an exit path
# that gave up) would never leave the process. It is a network wait, so it is
# bounded, and it is counted in the exit window below rather than left to add an
# unbounded tail of its own to the budget.
SENTRY_FLUSH_TIMEOUT_SECONDS = 2.0

# The whole window a shutdown may still spend after ``run_daemon`` has reported:
# the bounded loop close above, then the flush. ``main`` cannot let the
# interpreter do this part for it - see ``live_runner.daemon.main`` - so the
# budget has to cover it explicitly.
EXIT_PATH_WINDOW_SECONDS = EXIT_PATH_TIMEOUT_SECONDS + SENTRY_FLUSH_TIMEOUT_SECONDS


def release_twisted_reactor_threads(log: logging.Logger | None = None) -> None:
    """Stop the Twisted reactor's thread pool so the process can exit.

    The cTrader adapter starts the asyncio-backed Twisted reactor with
    ``startRunning`` and never stops it (the daemon owns it for the process
    lifetime). Twisted resolves the broker host name through its reactor thread
    pool, whose worker threads are not daemon threads and are only stopped by
    the reactor's shutdown event. Without this, a process that connected to
    cTrader hangs forever in interpreter shutdown.

    ``log`` receives the warning about a reactor whose private shutdown API is
    gone. It defaults to this module's logger; ``live_runner.preflight`` passes
    its own so the warning stays in the log an operator reads for preflight.
    """
    if log is None:
        log = logger
    # ``installReactor`` stores the reactor instance under this module key, so
    # membership means "a reactor was installed"; importing it here cannot
    # install a new one. The check has to come first: importing
    # ``twisted.internet.reactor`` would otherwise start a reactor that nothing
    # asked for.
    if "twisted.internet.reactor" not in sys.modules:
        return
    from twisted.internet import reactor

    if getattr(reactor, "threadpool", None) is None:
        return
    stop_thread_pool = getattr(reactor, "_stopThreadPool", None)
    if callable(stop_thread_pool):
        # The reactor's own shutdown-trigger handler: removes its triggers and
        # joins the workers. Shutdown never fires here, so it is called directly.
        stop_thread_pool()
    else:
        log.warning(
            "Twisted reactor has a thread pool but no _stopThreadPool handler; "
            "the process may hang at exit. The Twisted private API likely changed."
        )


async def release_twisted_reactor_threads_bounded(timeout: float) -> None:
    """Release the Twisted reactor's thread pool off the loop, under a deadline.

    ``release_twisted_reactor_threads`` joins the reactor's workers, so it blocks
    for as long as a stuck broker request does - and awaiting it inside the
    shutdown would bound nothing: the one phase that can hang without the deadline
    is the phase that keeps the process from reaching its exit code, which is the
    signal that tells an operator a broker outcome was left unknown. So the join
    runs on a daemon thread and is only awaited for ``timeout`` (the cleanup-phase
    deadline): a release that does not come back in time raises ``TimeoutError``,
    which the caller records as a failed phase.

    The abandoned release thread itself is a daemon thread, but the reactor pool
    workers it was joining are not: a timed-out release can still block the
    interpreter at exit. That is why the caller also records the abandonment
    (``ShutdownCoordinator.note_abandoned_reactor_release``) - and why ``main``
    leaves through ``os._exit`` on every path, abandoned release or not.
    """
    loop = asyncio.get_running_loop()
    finished: asyncio.Future[None] = loop.create_future()
    failure: list[BaseException] = []

    def _release() -> None:
        try:
            release_twisted_reactor_threads()
        except BaseException as exc:  # except-kind: loop-survival - verdict survives release
            failure.append(exc)
        finally:
            try:
                loop.call_soon_threadsafe(_finished)
            except RuntimeError:
                pass  # the loop closed first; there is nobody left to tell

    def _finished() -> None:
        if not finished.done():
            finished.set_result(None)

    threading.Thread(target=_release, name="reactor-release", daemon=True).start()
    async with asyncio.timeout(timeout):
        await finished
    if failure:
        raise failure[0]


async def close_the_loop_bounded(timeout: float) -> bool:
    """Cancel what is left on the loop and wait for those cancels, at most ``timeout``.

    ``live_runner.daemon.main`` runs this instead of letting ``asyncio.run`` close
    the loop, because the close a loop gets once its coroutine has returned is
    unbounded in exactly the two places the shutdown budget has to cover:

    * every task still on the loop is cancelled and those cancels are gathered
      with no deadline, so a task that ignores its cancellation holds that gather
      open forever - after the bounded cleanup has already reported its verdict
      and abandoned the very same task; and
    * the loop's default executor is then joined, and a ``to_thread`` call that
      is already running cannot be cancelled, so an in-flight broker request the
      drain did not cover holds the join for as long as that request takes. No
      bound on the cancellation can touch it.

    Either one is a wait past the budget the runbook published and past the unit
    file's ``TimeoutStopSec``, so the service manager SIGKILLs the daemon after
    it has already done all of its work, and the operator never learns the exit
    code that says a broker outcome was left unknown.

    Returns ``True`` only when nothing was left unfinished. That is *not* proof
    that the process may now let the interpreter finish: a task that awaited a
    ``to_thread`` call honours its cancel, reports nothing unfinished here, and
    leaves the worker thread running all the same. ``main`` therefore reports
    this verdict in its log and leaves through ``os._exit`` either way.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # ``all_tasks`` returns the loop's unfinished tasks, so nothing that already
    # finished is cancelled and nothing pending is missed. This task is the one
    # running the close; it is excluded because cancelling it would cancel the
    # close itself.
    pending = [task for task in asyncio.all_tasks(loop) if task is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    clean = True
    if pending:
        _, unfinished = await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
        if unfinished:
            # A second cancel: the first was ignored, and this is the last thing
            # the daemon will ever do to them. Their outcome was already logged
            # by the phase that abandoned them.
            for task in unfinished:
                task.cancel()
            logger.error(
                "The exit path gave up on task(s) that ignored cancellation and "
                "is exiting without waiting for them",
                extra={
                    "data": {
                        "abandoned_tasks": sorted(task.get_name() for task in unfinished),
                        "timeout_seconds": timeout,
                    }
                },
            )
            clean = False
    # ``asyncio.run`` closes pending async generators before it closes the loop,
    # so a suspended one (a submission's ``async with`` unwinding late) is not
    # left for the interpreter. Same deadline, same verdict: one whose closing
    # blocks is abandoned and reported rather than waited out.
    generators = asyncio.ensure_future(loop.shutdown_asyncgens())
    done, _ = await asyncio.wait({generators}, timeout=max(0.0, deadline - loop.time()))
    if not done:
        generators.cancel()
        logger.error(
            "The exit path gave up on closing async generators and is exiting "
            "without waiting for them",
            extra={"data": {"timeout_seconds": timeout}},
        )
        clean = False
    return clean


class SubmissionRefusedError(RuntimeError):
    """Broker work was attempted after a shutdown was requested.

    Refused rather than sent: work started once the stop flag is set would be
    sent through the connections the shutdown is tearing down, and it would not
    be on the in-flight ledger the drain waits for, so its broker outcome could
    never be reported. Callers of a submission helper that registers itself (see
    ``live_runner.daemon.submit_order_with_risk``) see this instead of a
    submission that silently bypasses the ledger.
    """


def force_exit(code: int = FORCED_EXIT_CODE) -> None:
    """Terminate immediately (second signal). Flushes logs, skips all cleanup."""
    try:
        logging.shutdown()
    finally:
        os._exit(code)


class ShutdownCoordinator:
    """Shutdown flag, signal handling and in-flight broker-work accounting."""

    def __init__(self, *, force_exit_fn: Callable[[int], None] = force_exit) -> None:
        self._force_exit = force_exit_fn
        self._requested = False
        self._reason = ""
        self._inflight = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._idle: asyncio.Event | None = None
        self._previous_handlers: dict[int, object] = {}
        self._reactor_release_abandoned = False

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def reactor_release_abandoned(self) -> bool:
        """Whether the last run gave up on the reactor release.

        A release that timed out was joining the reactor pool's workers, which
        are non-daemon threads, so the interpreter could still block on them at
        exit. The timed-out phase is what reports the failure (and the exit
        code); this is the recorded reason behind it, kept so the state of an
        abandoned release is observable rather than inferred from the verdict.
        """
        return self._reactor_release_abandoned

    def note_abandoned_reactor_release(self) -> None:
        """Record that the reactor release did not come back before its deadline."""
        self._reactor_release_abandoned = True

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def inflight(self) -> int:
        """Broker work currently in flight: submissions and mutations alike."""
        return self._inflight

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the running loop. Must be called before ``wait``/``wait_idle``."""
        self._loop = loop
        # A new run starts with a clean slate (the daemon's coordinator is
        # process-wide and run_daemon marks it requested on every exit path).
        self._requested = False
        self._reason = ""
        self._reactor_release_abandoned = False
        self._event = asyncio.Event()
        self._idle = asyncio.Event()
        # The ledger too: a previous run whose drain timed out can leave work
        # counted that never got its ``end_submission`` (the submission was
        # abandoned with the task holding it). Carrying that count into the next
        # run makes its drain wait for a submission nobody is running, time out,
        # and report ``1`` for a shutdown that had nothing to reconcile - so the
        # count is reset with the flag, and the idle event follows it.
        self._inflight = 0
        self._idle.set()

    def request(self, reason: str) -> bool:
        """Request shutdown. Returns False if one was already requested."""
        if self._requested:
            return False
        self._requested = True
        self._reason = reason
        self._call_in_loop(self._set_event)
        return True

    def handle_signal(self, signum: int) -> None:
        """Signal entry point: first signal drains, any later one forces exit."""
        name = _signal_name(signum)
        if self.request(name):
            logger.warning(
                "Shutdown requested; draining in-flight submissions and closing connections. "
                "Send the signal again to force exit.",
                extra={"data": {"signal": name}},
            )
            return
        logger.error(
            "Second shutdown signal received; forcing immediate exit",
            extra={"data": {"signal": name}},
        )
        self._force_exit(FORCED_EXIT_CODE)

    async def wait(self) -> None:
        assert self._event is not None, "bind() must be called first"
        await self._event.wait()

    # -- in-flight submission accounting ---------------------------------------------

    def begin_submission(self) -> bool:
        """Register in-flight broker work, or refuse it once shutdown was requested.

        Covers an order submission and a broker mutation (a trailing-stop
        amendment, a STOPPED cancel/flatten sweep) alike: both must be awaited
        by the drain, and neither may start once shutdown was requested.

        Synchronous on purpose: the check and the registration happen with no
        ``await`` between them, so the drain can never miss work that passed
        the gate.
        """
        if self._requested:
            return False
        self._inflight += 1
        if self._idle is not None:
            self._idle.clear()
        return True

    def end_submission(self) -> None:
        """Unregister in-flight broker work once the broker has answered it."""
        # ``max(0, …)``: a mutation the drain abandoned - its task ignored the
        # cancel, so nothing awaits it any more - can still reach this line from
        # the run that is over, after ``bind`` reset the ledger for the next one.
        # That subtraction must not make the ledger negative: a count below zero
        # never comes back to zero, so the next run's drain would wait for work
        # nobody is running and report ``1`` for a shutdown with nothing to
        # reconcile. Saturating is enough because the daemon runs once per
        # process and leaves it immediately after a shutdown
        # (``live_runner.daemon.main``), so a stranded subtraction has no later
        # run to be charged to.
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0 and self._idle is not None:
            self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no submission is in flight. False if ``timeout`` elapsed."""
        if self._inflight == 0:
            return True
        assert self._idle is not None, "bind() must be called first"
        try:
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True

    # -- signal installation ---------------------------------------------------------

    def install_signal_handlers(self) -> list[str]:
        """Install SIGINT and (where it exists) SIGTERM handlers; returns their names.

        ``signal.signal`` is used rather than ``loop.add_signal_handler`` so the
        same code runs on Windows and so a second signal is honoured even when
        the event loop is busy: the handler runs in the main thread and only
        flips flags (or exits), it never awaits.
        """
        installed: list[str] = []
        for name in ("SIGINT", "SIGTERM"):
            if not hasattr(signal, name):
                continue
            signum = getattr(signal, name)
            try:
                self._previous_handlers[signum] = signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                # Not the main thread, or the platform refuses this signal.
                continue
            installed.append(name)
        return installed

    def restore_signal_handlers(self) -> None:
        for signum, previous in self._previous_handlers.items():
            try:
                signal.signal(signum, previous)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                continue
        self._previous_handlers.clear()

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        self.handle_signal(signum)

    # -- internals -------------------------------------------------------------------

    def _set_event(self) -> None:
        if self._event is not None:
            self._event.set()

    def _call_in_loop(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)

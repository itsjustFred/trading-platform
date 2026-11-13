"""Graceful, bounded shutdown for the live daemon.

Shutdown contract (see ``live_runner.daemon.run_daemon``):

1. The first SIGINT/SIGTERM stops the daemon accepting new signals.
2. An order submission that is already in flight is allowed to finish, up to a
   timeout. Cancelling it mid-request would leave the broker outcome unknown.
3. Broker and signal-source connections are closed and the Twisted reactor's
   thread pool is released so the interpreter can exit.
4. Exit code 0 on a clean shutdown, non-zero when a cleanup phase timed out.
5. A second signal forces an immediate exit.

Shutdown NEVER cancels, closes or modifies open orders or positions at the
broker. Positions belong to the strategy's own exit logic and the operator;
a restart must find the account exactly as the daemon left it.

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
from collections.abc import Callable
from types import FrameType

from shared.logging import get_logger

logger = get_logger("live_runner.shutdown")

DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 20.0
EXIT_OK = 0
EXIT_CLEANUP_TIMEOUT = 1
FORCED_EXIT_CODE = 130


def release_twisted_reactor_threads() -> None:
    """Stop the Twisted reactor's thread pool so the process can exit.

    The cTrader adapter starts the asyncio-backed Twisted reactor with
    ``startRunning`` and never stops it (the daemon owns it for the process
    lifetime). Twisted resolves the broker host name through its reactor thread
    pool, whose worker threads are not daemon threads and are only stopped by
    the reactor's shutdown event. Without this, a process that connected to
    cTrader hangs forever in interpreter shutdown.
    """
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


def force_exit(code: int = FORCED_EXIT_CODE) -> None:
    """Terminate immediately (second signal). Flushes logs, skips all cleanup."""
    try:
        logging.shutdown()
    finally:
        os._exit(code)


class ShutdownCoordinator:
    """Shutdown flag, signal handling and in-flight submission accounting."""

    def __init__(self, *, force_exit_fn: Callable[[int], None] = force_exit) -> None:
        self._force_exit = force_exit_fn
        self._requested = False
        self._reason = ""
        self._inflight = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._idle: asyncio.Event | None = None
        self._previous_handlers: dict[int, object] = {}

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def inflight(self) -> int:
        return self._inflight

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the running loop. Must be called before ``wait``/``wait_idle``."""
        self._loop = loop
        # A new run starts with a clean slate (the daemon's coordinator is
        # process-wide and run_daemon marks it requested on every exit path).
        self._requested = False
        self._reason = ""
        self._event = asyncio.Event()
        self._idle = asyncio.Event()
        if self._inflight == 0:
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
        """Register an order submission, or refuse it once shutdown was requested.

        Synchronous on purpose: the check and the registration happen with no
        ``await`` between them, so the drain can never miss a submission that
        passed the gate.
        """
        if self._requested:
            return False
        self._inflight += 1
        if self._idle is not None:
            self._idle.clear()
        return True

    def end_submission(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0 and self._idle is not None:
            self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no submission is in flight. False if ``timeout`` elapsed."""
        if self._inflight == 0:
            return True
        assert self._idle is not None, "bind() must be called first"
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except asyncio.TimeoutError:
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

"""Per-account order-placement lock shared by the daemon and broker adapters.

Issue #362 (maintainer decision 2026-11-23, option b): every broker
position/quantity read that feeds sizing, pre-submit caps, leverage or exit
quantity, and the ``adapter.submit_order`` call it feeds, runs inside one
critical section per account per event loop. The same primitive is available
to adapter-internal paths that read broker state and then write outside the
daemon's submit chokepoints (for example the Alpaca exit re-protection recovery
on #350), so those paths serialise with signal submissions instead of racing
them.

Lock ordering is ``_exit_lock(account, symbol)`` outermost, then this account
lock, then adapter-level locks such as ``_order_book_lock``. Nothing acquires
``_exit_lock`` while holding this lock, so the ordering cannot deadlock.

The lock serialises *our own* read->submit paths only. A fill the broker
produces from a working order (a resting entry, a bracket leg, a flatten or a
manual trade) can still move the book inside the section; broker reconciliation
remains the backstop for that residual race.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import AsyncIterator

from shared.logging import get_logger

logger = get_logger("live-runner")

#: Order-placement critical sections in flight, per running event loop and
#: account. Weak keys let a finished loop's locks be collected with it.
_ACCOUNT_ORDER_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()

#: Upper bound on the wait for an account's order-placement lock. The section
#: it guards runs the bounded broker reads sizing and the pre-submit caps make
#: (entry quote, pip value and the caps fetch) plus the submit itself, so the
#: bound must cover a slow-but-live chain of broker round trips; a submission
#: that cannot start inside it is refused rather than sized on a read another
#: submission may already have invalidated. Refusing is safe: nothing reached
#: the broker.
ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS = 120.0

#: A waiter this long means submissions of the account are queued behind a
#: slow or stuck section; say so before the bounded wait expires.
ACCOUNT_ORDER_LOCK_WAIT_WARN_SECONDS = 5.0

#: A section still held after this long is abnormal even for the slowest
#: bounded chain. The warning fires while the lock is still held, ahead of the
#: wait bound that will refuse later submissions.
ACCOUNT_ORDER_LOCK_HOLD_WARN_SECONDS = 60.0


class AccountOrderLockTimeoutError(RuntimeError):
    """The per-account order-placement lock could not be acquired in time.

    Raised instead of sizing or submitting on a broker read that another
    in-flight mutation of the same account may already have invalidated: the
    safe answer to "the book moved under us" is to refuse, never to trade on it.
    """


def _account_key(account_id: object) -> str:
    key = str(account_id or "").strip()
    if not key:
        raise ValueError("account_id is required for the order-placement lock")
    return key


def account_order_lock(account_id: str) -> asyncio.Lock:
    """The lock serialising one account's position-read -> submit sections."""
    locks = _ACCOUNT_ORDER_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(_account_key(account_id), asyncio.Lock())


@contextlib.asynccontextmanager
async def account_order_placement(
    account_id: str, *, timeout: float | None = None
) -> AsyncIterator[None]:
    """Hold the account's order-placement lock for one critical section.

    ``timeout`` defaults to :data:`ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS`; tests
    may pass a shorter bound, while daemon and adapter call sites use the
    shared default so there is one knob.
    """
    loop = asyncio.get_running_loop()
    lock = account_order_lock(account_id)
    wait_timeout = (
        ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    wait_started = loop.time()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=wait_timeout)
    except TimeoutError as exc:
        raise AccountOrderLockTimeoutError(
            f"account {account_id!r}: the order-placement lock was still held "
            f"after {wait_timeout:g}s; refusing rather than sizing or "
            "submitting on a broker read that may be stale"
        ) from exc

    waited_seconds = loop.time() - wait_started
    key = _account_key(account_id)
    if waited_seconds > ACCOUNT_ORDER_LOCK_WAIT_WARN_SECONDS:
        logger.warning(
            "Account order-placement lock wait exceeded %.1fs (account=%s); "
            "submissions of this account are queued behind another section",
            waited_seconds,
            key,
            extra={
                "data": {
                    "account_id": key,
                    "lock_wait_seconds": waited_seconds,
                    "lock_timeout_seconds": wait_timeout,
                }
            },
        )

    acquired_at = loop.time()
    still_held = True

    def _warn_if_still_held() -> None:
        if still_held:
            held_seconds = loop.time() - acquired_at
            logger.warning(
                "Account order-placement lock held for %.1fs (account=%s); "
                "later submissions of this account will be refused if it "
                "outlives the %.0fs wait bound",
                held_seconds,
                key,
                wait_timeout,
                extra={
                    "data": {
                        "account_id": key,
                        "lock_hold_seconds": held_seconds,
                        "lock_timeout_seconds": wait_timeout,
                    }
                },
            )

    hold_warning = (
        loop.call_later(ACCOUNT_ORDER_LOCK_HOLD_WARN_SECONDS, _warn_if_still_held)
        if ACCOUNT_ORDER_LOCK_HOLD_WARN_SECONDS > 0
        else None
    )
    try:
        yield
    finally:
        still_held = False
        if hold_warning is not None:
            hold_warning.cancel()
        lock.release()

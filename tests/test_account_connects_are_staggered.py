"""Accounts must be connected in sequence, not simultaneously.

cTrader authenticates against the application, not the account, so connecting every account at once
presents several simultaneous handshakes on one set of credentials. Measured against five demo
accounts: a read-only query of all of them in one loop failed for every account after the first with
`(15, 'Deferred')` - the request reaches the socket and nothing answers within 15s - and succeeded for
every account when the calls were spread out.

So the daemon's connects are staggered. Per-account isolation is unchanged: they are still gathered
with `return_exceptions=True`, so one failing account cannot fail the tick.
"""

from __future__ import annotations

import asyncio

import pytest

from live_runner import daemon

ACCOUNTS = ["11111111", "22222222", "33333333"]


STAGGER = 0.05  # real seconds: small enough to keep the test fast, large enough to measure


@pytest.fixture
def recorded(monkeypatch):
    """Capture the order and start times of connect attempts, without touching asyncio.

    An earlier version monkeypatched `daemon.asyncio.sleep`, which is the asyncio module itself, so
    every `sleep` in the process became a no-op that mutated a shared counter - it modelled serial
    execution and broke `asyncio.run` internals. A real 50ms stagger is cheap and measures the thing
    that matters: when each account's connect actually began.
    """
    loop = asyncio.new_event_loop()
    started: list[tuple[str, float]] = []
    origin = loop.time()

    async def _fake_ensure(account_id, *_args, **_kwargs):
        started.append((account_id, loop.time() - origin))
        return account_id

    monkeypatch.setattr(daemon, "_ensure_adapter_for_active_account", _fake_ensure)
    monkeypatch.setattr(daemon, "_connect_stagger_seconds", lambda: STAGGER)
    try:
        yield started
    finally:
        loop.close()


def _run(account_ids):
    asyncio.run(
        daemon._connect_active_accounts(
            set(account_ids),
            {account_id: object() for account_id in account_ids},
            {},
            accounts_env_values={},
        )
    )


def test_connects_do_not_all_start_at_the_same_moment(recorded):
    _run(ACCOUNTS)

    assert len(recorded) == len(ACCOUNTS)
    assert [a for a, _t in recorded] == sorted(ACCOUNTS), "accounts must connect in a stable order"
    gaps = [b - a for (_ia, a), (_ib, b) in zip(recorded, recorded[1:])]
    assert all(gap > 0 for gap in gaps), (
        f"two accounts started at the same moment (gaps {gaps}), which is the "
        f"simultaneous-handshake pattern that produces (15, 'Deferred')"
    )


def test_the_stagger_is_uniform(recorded):
    _run(ACCOUNTS)

    gaps = [b - a for (_ia, a), (_ib, b) in zip(recorded, recorded[1:])]
    assert all(gap > STAGGER * 0.5 for gap in gaps), (
        f"expected each start about {STAGGER}s after the previous, gaps were {gaps}"
    )


def test_one_failing_account_does_not_fail_the_tick(monkeypatch):
    """Per-account isolation is why the connects are still gathered."""
    attempted: list[str] = []

    async def _ensure(account_id, *_args, **_kwargs):
        attempted.append(account_id)
        if account_id == "22222222":
            raise RuntimeError("connect refused")
        return account_id

    monkeypatch.setattr(daemon, "_ensure_adapter_for_active_account", _ensure)
    monkeypatch.setattr(daemon, "_connect_stagger_seconds", lambda: 0.0)

    _run(ACCOUNTS)  # must not raise
    assert set(attempted) == set(ACCOUNTS)


def test_a_single_account_is_not_delayed(recorded):
    """One account has nothing to be serialised against, so waiting would be pure latency."""
    _run(["99999999"])

    assert [a for a, _t in recorded] == ["99999999"]
    assert recorded[0][1] < STAGGER, "a lone account must not wait out a stagger"


@pytest.mark.parametrize(
    "raw,expected",
    [("12", 12.0), ("0", 0.0), (" 2.5 ", 2.5), (None, daemon.CONNECT_STAGGER_SECONDS)],
)
def test_the_stagger_reads_the_environment(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CONNECT_STAGGER_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CONNECT_STAGGER_SECONDS", raw)

    assert daemon._connect_stagger_seconds() == expected


@pytest.mark.parametrize("raw", ["not-a-number", "-1", "inf", ""])
def test_a_bad_stagger_falls_back_rather_than_failing_the_tick(monkeypatch, raw):
    """A pacing knob is not a correctness setting; a typo in it must not stop the daemon connecting."""
    monkeypatch.setenv("CONNECT_STAGGER_SECONDS", raw)

    assert daemon._connect_stagger_seconds() == daemon.CONNECT_STAGGER_SECONDS

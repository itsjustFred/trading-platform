"""Tests for the protective-stop maintenance daemon loop."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import pytest

from live_runner.protective_stop_maintenance import (
    run_protective_stop_maintenance_loop,
)


class _Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self._fail = fail

    async def maintain_protective_stops(self) -> None:
        self.calls += 1
        if self._fail:
            raise RuntimeError("heal exploded")


def _gate(*, allowed: bool = True):
    @contextlib.asynccontextmanager
    async def _cm() -> AsyncIterator[bool]:
        yield allowed

    return _cm


def _run_coro(coro) -> None:
    asyncio.run(coro)


def test_one_adapter_failure_does_not_skip_siblings() -> None:
    """A raising ``maintain_protective_stops`` must not starve the adapters
    after it in the same tick."""
    bad = _Adapter(fail=True)
    good = _Adapter()
    adapters: Mapping[str, object] = {"bad": bad, "good": good}

    async def _drive() -> None:
        task = asyncio.create_task(
            run_protective_stop_maintenance_loop(
                lambda: adapters,  # type: ignore[arg-type]
                mutation_gate=_gate(),
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run_coro(_drive())
    assert good.calls >= 1


def test_adapters_without_maintain_are_skipped() -> None:
    called: list[str] = []

    class _Plain:
        pass

    class _Maintained(_Adapter):
        async def maintain_protective_stops(self) -> None:
            called.append("ran")

    adapters: Mapping[str, object] = {"plain": _Plain(), "maintained": _Maintained()}

    async def _drive() -> None:
        task = asyncio.create_task(
            run_protective_stop_maintenance_loop(
                lambda: adapters,  # type: ignore[arg-type]
                mutation_gate=_gate(),
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run_coro(_drive())
    assert called


def test_gate_refusal_ends_the_loop() -> None:
    """``may_mutate=False`` means shutdown was requested: the loop exits
    rather than retrying forever."""
    adapter = _Adapter()
    adapters: Mapping[str, object] = {"a": adapter}

    async def _drive() -> None:
        await run_protective_stop_maintenance_loop(
            lambda: adapters,  # type: ignore[arg-type]
            mutation_gate=_gate(allowed=False),
            interval_seconds=0.01,
        )

    _run_coro(_drive())
    assert adapter.calls == 0


def test_trailing_eligible_adapters_hard_block_real_money() -> None:
    """The maintenance loop's adapter filter must keep excluding
    real-money adapters: the heal places, cancels and closes orders, so
    the demo/paper bar is the deliberate hard block, not a convenience."""
    from live_runner.daemon import _trailing_eligible_adapters

    class _Stub:
        def __init__(self, **flags) -> None:
            self._connected = True
            self._authenticated = True
            for key, value in flags.items():
                setattr(self, key, value)

    demo = _Stub(paper=True)
    live = _Stub(paper=False)
    unknown = _Stub()

    eligible = _trailing_eligible_adapters(
        {"demo": demo, "live": live, "unknown": unknown}
    )

    assert eligible == {"demo": demo}

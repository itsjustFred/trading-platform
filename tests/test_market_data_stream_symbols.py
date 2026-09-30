"""Changing a worker's symbols must actually unsubscribe the ones being dropped.

`live_runner/market_data_stream.py` had no test at all - no test file in the suite
referenced the module - and the bug this covers is the kind that only shows up after
a deployment changes what it trades.

`_StreamWorker.set_symbols` was written as "unsubscribe everything, then subscribe
the new set". Read against the SDK that is not what it does. In `alpaca-py`:

    def unsubscribe_bars(self, *symbols):   # "*" means everything
        self._unsubscribe("bars", symbols)

    def _unsubscribe(self, channel, symbols):
        for symbol in symbols:
            del self._handlers[channel][symbol]      # deletes only what it is given

    def _subscribe(self, handler, symbols, handlers):
        for symbol in symbols:
            handlers[symbol] = handler               # merges, never clears

So `self.stream.unsubscribe_bars()` with no arguments deletes **zero** entries, and
the following `subscribe_bars` merges the new symbols into whatever was there. The
subscribed set could only ever grow. A deployment that stops trading EURUSD and
keeps AAPL under the same credentials keeps streaming EURUSD indefinitely, because
the worker's whole purpose is that a removed symbol stops costing bandwidth.

The consequence is not only bandwidth. `_on_bar` does not check the worker's current
symbol set - it takes whatever the stream sends and appends it - so bars for an
abandoned symbol keep being recorded in `_bars`, where they are indistinguishable
from live ones.

The `except Exception: pass` wrapped around the unsubscribe is a red herring and was
worth stating plainly: the call was a no-op even when it succeeded, so removing the
swallow would not have fixed anything. The bug is the missing argument.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace

from live_runner import market_data_stream
from live_runner.market_data_stream import LiveMarketDataStreamService, _StreamWorker


class RecordingStream:
    """Stands in for `alpaca.data.live` streams, with the same subscribe semantics.

    `_subscribe` merges into the handler dict and `_unsubscribe` deletes only the
    symbols it is handed - both read from the installed SDK rather than assumed.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.subscribe_calls: list[tuple[str, ...]] = []
        self.unsubscribe_calls: list[tuple[str, ...]] = []
        self.fail_unsubscribe = False

    def subscribe_bars(self, handler, *symbols: str) -> None:
        self.subscribe_calls.append(tuple(symbols))
        for symbol in symbols:
            self.handlers[symbol] = handler

    def unsubscribe_bars(self, *symbols: str) -> None:
        self.unsubscribe_calls.append(tuple(symbols))
        if self.fail_unsubscribe:
            raise RuntimeError("websocket closed")
        for symbol in symbols:
            self.handlers.pop(symbol, None)

    def stop(self) -> None:
        pass


def _worker(stream) -> _StreamWorker:
    worker = _StreamWorker(
        kind="stock",
        api_key="key",
        secret_key="secret",
        paper=True,
        bar_handler=lambda bar: None,
    )
    worker.stream = stream
    return worker


def test_dropping_a_symbol_unsubscribes_it() -> None:
    """The core claim: a symbol no longer wanted stops being subscribed."""
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"AAPL", "MSFT"})
    assert set(stream.handlers) == {"AAPL", "MSFT"}

    worker.set_symbols({"AAPL"})
    assert set(stream.handlers) == {"AAPL"}, (
        f"MSFT is no longer traded but is still subscribed: {sorted(stream.handlers)}"
    )


def test_adding_a_symbol_keeps_the_existing_ones() -> None:
    """The fix must not break the additive case that the merge semantics serve."""
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"AAPL"})
    worker.set_symbols({"AAPL", "NVDA"})

    assert set(stream.handlers) == {"AAPL", "NVDA"}


def test_the_unsubscribe_names_the_symbols_being_dropped() -> None:
    """`unsubscribe_bars()` with no arguments deletes nothing - the whole bug.

    Pinned directly so that a future "simplification" back to a bare call, which
    reads as tidier and is silently a no-op, fails here.
    """
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"AAPL", "MSFT"})
    worker.set_symbols({"AAPL"})

    dropped = [call for call in stream.unsubscribe_calls if call]
    assert dropped, (
        "every unsubscribe call was argument-less, which deletes nothing in "
        "alpaca-py: %r" % (stream.unsubscribe_calls,)
    )
    assert set(dropped[0]) == {"MSFT"}


def test_nothing_is_unsubscribed_when_the_symbol_set_is_unchanged() -> None:
    """A no-op refresh should not churn the broker's subscription set."""
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"AAPL", "MSFT"})
    before = len(stream.unsubscribe_calls)
    worker.set_symbols({"MSFT", "AAPL"})

    assert stream.unsubscribe_calls[before:] == [], (
        "an unchanged symbol set unsubscribed something: %r" % (stream.unsubscribe_calls[before:],)
    )
    assert set(stream.handlers) == {"AAPL", "MSFT"}


def test_a_failed_unsubscribe_is_logged_rather_than_swallowed() -> None:
    """The swallow was not the bug, but a silent failure is still a silent failure.

    With the argument fixed, a websocket that is already closed now leaves the old
    subscription in place - and nothing anywhere would say so. Logged rather than
    raised on purpose: `set_symbols` runs once per worker from the refresh loop, so
    an exception here would abandon the remaining workers' updates entirely. The
    trade is deliberate, and the point of this test is that the trade is visible.
    """
    stream = RecordingStream()
    stream.fail_unsubscribe = True
    worker = _worker(stream)
    worker.set_symbols({"AAPL"})

    # `shared.logging.get_logger` sets `propagate = False`, so pytest's `caplog`
    # - which hangs a handler off the root logger - never sees these records. The
    # first version of this test asserted on `caplog` and passed against code that
    # swallowed the failure completely. The handler goes on the module's own logger.
    captured = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    target = market_data_stream.logger
    handler = Capture()
    target.addHandler(handler)
    try:
        worker.set_symbols({"NVDA"})
    finally:
        target.removeHandler(handler)

    assert any("unsubscribe" in r.getMessage().lower() for r in captured), (
        "a failed unsubscribe was swallowed again: %r" % [r.getMessage() for r in captured]
    )
    assert stream.handlers, "the failed unsubscribe left the old subscription, as expected"


def test_emptying_the_symbol_set_unsubscribes_everything() -> None:
    """Going to no symbols must leave nothing subscribed."""
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"AAPL", "MSFT"})
    worker.set_symbols(set())

    assert stream.handlers == {}, (
        f"an emptied symbol set left subscriptions behind: {sorted(stream.handlers)}"
    )


def test_a_stream_without_unsubscribe_support_still_subscribes() -> None:
    """The `hasattr` guard is load-bearing: a stream lacking the method must work."""

    class NoUnsubscribe(RecordingStream):
        unsubscribe_bars = None  # type: ignore[assignment]

        def __getattribute__(self, name):
            if name == "unsubscribe_bars":
                raise AttributeError(name)
            return object.__getattribute__(self, name)

    stream = NoUnsubscribe()
    worker = _worker(stream)

    worker.set_symbols({"AAPL"})
    assert set(stream.handlers) == {"AAPL"}


def test_the_worker_tracks_the_symbols_it_was_given() -> None:
    """`self._symbols` is the worker's own record, independent of the stream."""
    stream = RecordingStream()
    worker = _worker(stream)

    worker.set_symbols({"aapl", " msft "})
    assert worker._symbols == {"AAPL", "MSFT"}, (
        "symbols must be normalised on the way in, or the diff below is noise"
    )
    worker.set_symbols({"AAPL"})
    assert worker._symbols == {"AAPL"}


def test_bars_for_an_abandoned_symbol_are_not_recorded() -> None:
    """The consequence, not just the subscription state.

    `_on_bar` lived on the service, not the worker - the worker is handed the
    handler - so the bar path is driven through the service with a real worker
    attached. It accepted any symbol the stream sent, so a stale subscription turned
    into bars sitting in `_bars` where a consumer cannot tell them from live ones.

    The bar carries the full attribute names `_serialize_bar_payload` reads. An
    abbreviated stand-in would leave `symbol` empty, `_on_bar` would return early,
    and this test would pass without recording anything at all.
    """
    service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
    service._lock = threading.Lock()
    service._max_bars_per_symbol = 10
    service._bars = defaultdict(lambda: deque(maxlen=10))

    def bar(symbol: str):
        return SimpleNamespace(
            symbol=symbol,
            timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
            open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
        )

    async def deliver(symbol: str) -> None:
        await service._on_bar(bar(symbol))

    service._symbols = {"AAPL", "MSFT"}
    asyncio.run(deliver("AAPL"))
    assert set(service._bars) == {"AAPL"}, "the fixture must actually record bars"

    # The deployment drops MSFT.
    service._symbols = {"AAPL"}
    asyncio.run(deliver("MSFT"))

    assert set(service._bars) == {"AAPL"}, (
        f"bars for a dropped symbol are still being recorded: {sorted(service._bars)}"
    )

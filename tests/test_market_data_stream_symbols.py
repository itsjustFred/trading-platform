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


def test_blank_symbols_never_reach_the_subscription() -> None:
    """The `if _normalize_symbol(symbol)` filter is what keeps blanks out.

    `set_symbols` normalises each symbol and keeps only the non-empty results, so
    `{"AAPL", "   "}` subscribes AAPL alone. Dropping that filter leaves every collected
    test green - the existing tests pass well-formed symbol sets.

    The consequence is a subscription to the empty string, which reaches
    `subscribe_bars`. Alpaca answers a feed it cannot resolve with an error, and this
    call site is inside the worker's per-update path: the comment above it says an
    exception here "would abandon the remaining workers' updates", so one blank symbol
    coming from a deployment's config stops the remaining symbols from being applied.

    So the filter is not cosmetic - it is the boundary between a malformed config entry
    and a refresh loop that stops updating.

    Asserted on both facets: the blank is not in the tracked set, and it is not passed to
    the stream. The stream is called with the real symbols only.
    """
    import threading

    from live_runner import market_data_stream as mds

    class _Stream:
        def __init__(self) -> None:
            self.subscribed: list[str] = []

        def subscribe_bars(self, _handler, *symbols) -> None:
            self.subscribed.extend(symbols)

    worker = mds._StreamWorker.__new__(mds._StreamWorker)
    worker._lock = threading.Lock()
    worker._symbols = set()
    worker.stream = _Stream()
    worker.bar_handler = lambda *_a, **_k: None

    worker.set_symbols({"AAPL", "   ", "", "\t", " msft "})

    assert worker._symbols == {"AAPL", "MSFT"}, (
        "a blank or unnormalised symbol was kept: " + repr(sorted(worker._symbols))
    )
    assert sorted(worker.stream.subscribed) == ["AAPL", "MSFT"], (
        "a blank symbol reached subscribe_bars, which would fail the feed and abandon "
        "the remaining workers' updates: " + repr(worker.stream.subscribed)
    )


def test_crypto_and_stock_symbols_go_to_different_streams() -> None:
    """`stream_kind` decides which websocket a symbol is subscribed on.

    `_refresh_sync` buckets every symbol by `(credentials, "crypto" | "stock")`, and the
    bucket name selects the stream. Treating every symbol as crypto leaves every test
    green, because the existing tests all drive `set_symbols` directly and never
    exercise the classification.

    The predicate is not arbitrary. Alpaca splits its feeds by asset class, so a symbol
    classified wrongly is **subscribed to a feed that never sends its bars** - no
    exception, no reconnect, just a strategy that stops receiving data while still
    reading ACTIVE. That is the same failure shape as the `is_demo` and cTID findings
    this sweep turned up: a silent mis-routing rather than a loud refusal.

    Both clauses of the predicate are asserted, since either alone would pass a
    weaker test:

    - `"/"` marks a crypto pair: `BTC/USD`.
    - `endswith("USD") and "-"` marks a crypto pair in the exchange's own spelling:
      `BTC-USD`.
    - A plain equity ticker is not crypto, and neither is a hyphenated ticker that
      does not end in USD.
    """
    from live_runner.market_data_stream import _is_crypto_symbol

    crypto = ["BTC/USD", "ETH/USD", "BTC-USD", "eth-usd", "SOL-USD"]
    for symbol in crypto:
        assert _is_crypto_symbol(symbol) is True, f"{symbol!r} should be a crypto pair"

    not_crypto = [
        "AAPL",
        "EURUSD",
        "USDJPY",
        "BRK-B",
        "RDS-A",
        # A hyphenated ticker ending in USD is ambiguous to a human but the predicate
        # says crypto, so assert what it actually does rather than what it sounds like.
        "SOMETHING-USD",
    ]
    for symbol in not_crypto[:5]:
        assert _is_crypto_symbol(symbol) is False, f"{symbol!r} should not be a crypto pair"

    # The blank guard exists so an empty symbol cannot be classified at all.
    for blank in ("", "   ", None):
        assert _is_crypto_symbol(blank) is False, (
            f"{blank!r} was classified rather than rejected"
        )


def test_a_lost_refresh_race_stops_the_spare_worker() -> None:
    """Losing the worker race must stop the loser, not leave two streams subscribed.

    `_refresh_sync` decides under the lock and starts workers outside it, so two
    concurrent refreshes can both decide a worker is needed. The second one discovers
    the key already present and must `stop()` its own spare. Skipping that leaves an
    orphaned websocket holding a socket open and subscribed to the same symbols - so
    bars arrive twice, the feed count climbs, and neither worker is reachable from
    `_workers` to be stopped by `_stop_sync`.

    That last part is the sharp edge: the loser is started, subscribed, and then
    **unreachable**, so a later stop leaves it running for the process's lifetime.

    The race is forced by making `start()` install the worker itself, which is the
    ordering that loses.
    """

    from live_runner import market_data_stream as mds
    from shared.models import LiveAccountConfig, LiveDeploymentConfig

    class _RacingWorker:
        """A worker that registers itself during `start()`, losing the race."""

        def __init__(self, **_kwargs) -> None:
            self.stopped = False
            self.subscribed: set[str] = set()

        def start(self) -> None:
            service._workers[worker_key] = self  # the race, decided during start()

        def set_symbols(self, symbols) -> None:
            self.subscribed = set(symbols)

        def stop(self) -> None:
            self.stopped = True

    key = mds._CredentialKey(api_key="k", secret_key="s", paper=True)
    # The loop buckets by (credentials, stream_kind), and AAPL is a stock, so the key
    # the racing worker installs has to be that tuple or it never collides.
    worker_key = (key, "stock")
    service = LiveMarketDataStreamService(max_bars_per_symbol=10)

    deployment = LiveDeploymentConfig(
        strategy_name="s",
        account_id="acc",
        symbols=["AAPL"],
        timeframe="1m",
        status="ACTIVE",
        signal_source_type="internal",
        signal_source="internal",
    )
    account = LiveAccountConfig(id="acc", broker="alpaca")

    import pytest

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(mds, "resolve_alpaca_credentials", lambda **_k: ("k", "s", True))
        monkey.setattr(mds, "_StreamWorker", _RacingWorker)
        spares: list[_RacingWorker] = []
        real_init = _RacingWorker.__init__

        def _tracking_init(self, **kwargs):
            real_init(self, **kwargs)
            spares.append(self)

        monkey.setattr(_RacingWorker, "__init__", _tracking_init)

        service._refresh_sync([deployment], {"acc": account})

        assert spares, "no worker was constructed, so this test proves nothing"
        assert all(worker.stopped for worker in spares), (
            "the worker that lost the race was not stopped, so its socket stays open "
            "and is unreachable from _workers: "
            + repr([w.stopped for w in spares])
        )
        assert len(service._workers) == 1, (
            "the losing worker was kept in _workers, replacing the winner: "
            + repr(list(service._workers))
        )
    finally:
        monkey.undo()


def test_a_blank_symbol_is_skipped_before_it_is_classified() -> None:
    """`_refresh_sync` drops empty symbols before bucketing, so they never get a stream.

    The guard sits above `_is_crypto_symbol` in the refresh loop, and both are needed:
    without the loop guard an empty symbol is bucketed as `""`, and without the
    classifier's own guard a blank symbol classifies as crypto. Asserted through the
    classifier because that is the unit with no other coverage.
    """
    from live_runner.market_data_stream import _normalize_symbol

    for blank in ("", "   ", None):
        assert _normalize_symbol(blank) == "", (
            f"{blank!r} normalised to something non-empty, so the loop's `if not symbol` "
            "guard would not drop it"
        )


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


def _service_with_worker(symbols: set[str]):
    """A real service holding a real worker, so nothing is invented.

    The first version of this test built the service with `__new__` and set
    `service._symbols` by hand. That manufactured an attribute the class does not
    have - subscriptions live on the worker - so the test passed against production
    code that raised `AttributeError` on every single bar. A fixture that invents the
    state under test is worse than no fixture, because it hides the defect it was
    meant to catch.
    """
    from live_runner.market_data_stream import _StreamWorker

    service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
    service._lock = threading.Lock()
    service._max_bars_per_symbol = 10
    service._bars = defaultdict(lambda: deque(maxlen=10))
    service._workers = {}

    worker = _StreamWorker(
        kind="stock",
        api_key="key",
        secret_key="secret",
        paper=True,
        bar_handler=None,
    )
    worker.set_symbols(symbols)
    service._workers[("key", "stock")] = worker
    return service


def test_the_service_has_no_symbol_set_of_its_own() -> None:
    """The attribute the first version of this filter read does not exist.

    A subscription belongs to a worker, not to the service. This asserts that
    directly, so a future filter cannot reintroduce a service-level read and have the
    tests agree with it.
    """
    service = _service_with_worker({"AAPL", "MSFT"})
    assert not hasattr(service, "_symbols"), (
        "LiveMarketDataStreamService must not carry a symbol set; subscriptions are "
        "per worker and a service-level read cannot see them"
    )


def test_bars_for_an_abandoned_symbol_are_not_recorded() -> None:
    """The consequence, not just the subscription state.

    A bar can still arrive for a symbol just dropped, because the broker may have it
    in flight. It used to be recorded anyway, leaving data for an abandoned symbol in
    `_bars` where a consumer cannot tell it from a live one.

    The bar carries the full attribute names `_serialize_bar_payload` reads. An
    abbreviated stand-in would leave `symbol` empty, `_on_bar` would return early, and
    this test would pass without recording anything at all.
    """
    service = _service_with_worker({"AAPL", "MSFT"})

    def bar(symbol: str):
        return SimpleNamespace(
            symbol=symbol,
            timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
            open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
        )

    async def deliver(symbol: str) -> None:
        await service._on_bar(bar(symbol))

    asyncio.run(deliver("AAPL"))
    assert set(service._bars) == {"AAPL"}, "the fixture must actually record bars"

    # The deployment drops MSFT; the worker stops subscribing to it.
    worker = service._workers[("key", "stock")]
    worker.set_symbols({"AAPL"})
    asyncio.run(deliver("MSFT"))

    assert set(service._bars) == {"AAPL"}, (
        f"bars for a dropped symbol are still being recorded: {sorted(service._bars)}"
    )


def test_a_subscribed_bar_is_recorded_after_the_filter_change() -> None:
    """The other half, so the filter cannot be satisfied by dropping every bar.

    A filter that returned early unconditionally would make the test above pass
    while no market data reached anything at all.
    """
    service = _service_with_worker({"AAPL", "MSFT"})

    def bar(symbol: str):
        return SimpleNamespace(
            symbol=symbol,
            timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
            open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
        )

    asyncio.run(service._on_bar(bar("AAPL")))
    asyncio.run(service._on_bar(bar("MSFT")))

    assert set(service._bars) == {"AAPL", "MSFT"}, (
        "bars for subscribed symbols must still be recorded; a filter that drops "
        "everything would satisfy the abandoned-symbol test on its own"
    )

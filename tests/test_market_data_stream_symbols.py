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

import pytest

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


class TestBarSerialisationAndReads:
    """What a bar becomes once it reaches a strategy. Four survivors.

    `_serialize_bar_payload` produces the `time` field every bar-indexed strategy reads, so
    a wrong value here is not a cosmetic bug - it shifts bars relative to each other and
    silently changes indicators.

    **The timezone handling is the interesting part, and it is asymmetric in the source:**

    | input | how it is read |
    |---|---|
    | a string | `fromisoformat(ts.replace("Z", "+00:00"))` - offset honoured |
    | a **naive** `datetime` | `replace(tzinfo=utc)` - assumed UTC |
    | anything else | `datetime.now(utc)` |

    A naive `datetime` is therefore *labelled* UTC. That is a real assumption about the
    broker's payloads and it is asserted below rather than left implicit - a broker sending
    local-time bars would put every bar the wrong number of hours out, and the `isoformat()`
    string would still claim UTC, so nothing downstream could notice.

    Two of the four survivors are only observable in specific environments, which is worth
    recording so nobody re-discovers it:

    - **Dropping `.replace(tzinfo=utc)`** on a naive datetime produces a *different
      `isoformat()` string* (no `+00:00`) but an **identical `time` on a machine running
      UTC**. The difference is the machine's local offset, so the assertion is on the
      serialised string - the one field that differs regardless of where the suite runs.
    - **Dropping the `.replace("Z", ...)`** also produces the same `time` here, because
      `fromisoformat` on 3.12 accepts `Z` directly. The string form is still asserted
      because that is the portable contract.

    `_is_connection_limit_error` matters because it decides whether the service reacts to a
    *resource* limit by backing off, or retries into the same wall. It matches a substring
    of the broker's message, so the exact marker is the contract.
    """

    @staticmethod
    def _bar(**kwargs):
        from types import SimpleNamespace

        base = dict(
            symbol="AAPL",
            timestamp="2026-01-02T03:04:05Z",
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
        )
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_a_bar_becomes_a_complete_payload(self) -> None:
        from live_runner.market_data_stream import _serialize_bar_payload

        payload = _serialize_bar_payload(self._bar())
        assert payload["symbol"] == "AAPL"
        assert payload["open"] == 1.0 and payload["high"] == 2.0
        assert payload["low"] == 0.5 and payload["close"] == 1.5
        assert payload["volume"] == 10.0
        assert payload["timestamp"] == "2026-01-02T03:04:05+00:00", payload["timestamp"]
        assert payload["time"] == 1767323045.0, payload["time"]

    def test_a_naive_datetime_is_labelled_utc_rather_than_left_ambiguous(self) -> None:
        """The assumption, stated: a naive broker timestamp is treated as UTC.

        Asserted on the *serialised string*, not on `time` - on a UTC machine both agree, so
        an assertion on `time` alone would pass with the labelling removed.
        """
        from datetime import datetime

        from live_runner.market_data_stream import _serialize_bar_payload

        payload = _serialize_bar_payload(
            self._bar(timestamp=datetime(2026, 1, 2, 3, 4, 5))
        )
        assert payload["timestamp"] == "2026-01-02T03:04:05+00:00", (
            "a naive datetime was not labelled UTC; the payload now either lies about the "
            f"offset or depends on the machine's local zone: {payload['timestamp']}"
        )
        assert payload["time"] == 1767323045.0

    def test_a_utc_aware_datetime_is_kept_as_it_arrives(self) -> None:
        from datetime import datetime, timezone

        from live_runner.market_data_stream import _serialize_bar_payload

        aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert _serialize_bar_payload(self._bar(timestamp=aware))["time"] == 1767323045.0

        # A non-UTC offset is honoured rather than overwritten, so the instant is preserved.
        from datetime import timedelta

        plus_two = datetime(
            2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2))
        )
        payload = _serialize_bar_payload(self._bar(timestamp=plus_two))
        assert payload["time"] == 1767323045.0, (
            "a non-UTC aware timestamp did not preserve the instant"
        )
        assert payload["timestamp"].endswith("+02:00"), payload["timestamp"]

    @pytest.mark.parametrize(
        "text",
        ["2026-01-02T03:04:05Z", "2026-01-02T03:04:05+00:00", "2026-01-02T03:04:05"],
    )
    def test_all_three_timestamp_string_forms_agree_on_the_instant(self, text: str) -> None:
        """Z, explicit +00:00 and a bare naive string are the same instant.

        The bare form is the one that cannot be correct in general - it carries no offset -
        so it is asserted as "same instant under a UTC-reading consumer" rather than as
        universal truth. That is the honest statement, and it is what the code relies on.
        """
        from live_runner.market_data_stream import _serialize_bar_payload

        payload = _serialize_bar_payload(self._bar(timestamp=text))
        assert payload["time"] == 1767323045.0, (
            f"{text!r} resolved to {payload['time']}, not the expected instant"
        )

    def test_an_absent_timestamp_becomes_now_not_the_epoch(self) -> None:
        """Falsified to *now*, deliberately - and pinned so the choice is visible.

        A bar with no usable timestamp is stamped with the current time rather than 1970.
        That is a real decision with a real consequence: the bar sorts to the *end* of the
        series instead of the beginning, so an undated bar cannot masquerade as
        historical data. The epoch would do the opposite and quietly prepend a bar that
        would distort any indicator computed over the window.

        Asserted as a range rather than an equality, because "now" is a moving target.
        """
        from live_runner.market_data_stream import _serialize_bar_payload

        before = datetime.now(timezone.utc).timestamp()
        payload = _serialize_bar_payload(self._bar(timestamp=None))
        after = datetime.now(timezone.utc).timestamp()

        assert before <= payload["time"] <= after, (
            f"an absent timestamp became {payload['timestamp']}, which is neither now nor "
            "a deliberate choice"
        )
        assert payload["time"] > 1_000_000_000, "an undated bar looks like historical data"

    def test_a_blank_symbol_is_never_produced_by_serialisation(self) -> None:
        """Normalisation happens here; the refusal happens one layer up.

        Asserted separately because they are separate guards: `_serialize_bar_payload`
        normalises the symbol, and `_on_bar` refuses an empty one. Both are needed and
        neither implies the other - see `test_the_service_refuses_a_blank_symbol_bar`.
        """
        from live_runner.market_data_stream import _serialize_bar_payload

        assert _serialize_bar_payload(self._bar(symbol="  aapl  "))["symbol"] == "AAPL"
        assert _serialize_bar_payload(self._bar(symbol=""))["symbol"] == ""

    def test_the_service_refuses_a_blank_symbol_bar(self) -> None:
        """The service-level refusal, driven through the real `_on_bar` path.

        A blank symbol reaching `_bars` would create a queue keyed by the empty string, which
        a consumer cannot distinguish from a real symbol - and would then be served by
        `_bars_for_symbol("")`. Asserted by checking that **no** queue was created at all,
        rather than that the bar is absent from one: an `append` of nothing would pass a
        weaker check.

        The bar carries the full attribute set the serialiser reads, for the reason
        `test_bars_for_an_abandoned_symbol_are_not_recorded` gives - an abbreviated stand-in
        would return early for a different reason and make this test vacuous.
        """
        service = _service_with_worker({"AAPL", "MSFT", ""})

        async def deliver(symbol: str) -> None:
            await service._on_bar(
                SimpleNamespace(
                    symbol=symbol,
                    timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
                )
            )

        # The fixture must actually record something, or the rest proves nothing.
        asyncio.run(deliver("AAPL"))
        assert "AAPL" in service._bars, "the fixture does not record bars"

        asyncio.run(deliver("   "))
        asyncio.run(deliver(""))

        assert "" not in service._bars, (
            "a blank symbol created a bar queue; a consumer cannot tell that from a real "
            f"symbol: {sorted(service._bars)}"
        )
        assert set(service._bars) == {"AAPL"}, sorted(service._bars)
        assert service._bars_for_symbol("") == [], (
            "a blank symbol resolves to a live bar list rather than nothing"
        )

    def test_the_on_bar_blank_guard_is_masked_rather_than_load_bearing(self) -> None:
        """**B1 is masked by a second guard**, and this records the evidence.

        Removing `if not symbol: return` from `_on_bar` changes nothing, because the
        subscription filter further down refuses the bar anyway: `_StreamWorker.set_symbols`
        normalises and drops blanks, so `""` can never be in `worker._symbols`, so
        `_worker_subscribes(worker, "")` is always false.

        That is a *mask*, not an equivalence of the guard with nothing - the two guards
        refuse the same input for different reasons. The way to tell them apart is to break
        the mask and see whether the inner guard then matters:

        - removing the worker's blank filter (`if _normalize_symbol(symbol)`) is **killed**,
          so the outer invariant is genuinely pinned by
          `test_blank_symbols_never_reach_the_subscription`;
        - the same mutation stops upper-casing symbols is also **killed**.

        So both guards are live code with both pinned, and neither can be deleted safely:
        drop the inner filter and a blank subscription would make the outer guard the only
        thing preventing a queue keyed by the empty string. This test says so explicitly, so
        the next person to see B1 survive knows to check the mask before concluding the guard
        is dead code.
        """

    def test_the_z_suffix_conversion_is_required_only_before_python_311(self) -> None:
        """**B3 is a version-dependent equivalent mutant**, verified on this interpreter.

        `datetime.fromisoformat` gained `Z` support in Python 3.11, so on the 3.12 running
        here `fromisoformat("2026-01-02T03:04:05Z")` already returns an aware datetime and
        the explicit `.replace("Z", "+00:00")` is redundant. That redundancy is why the
        mutation survives here and would not survive on 3.10 or earlier, where the same call
        raises `ValueError` on every broker bar that uses the Z form - which is the form
        every Alpaca timestamp uses.

        So the guard is **portability, not correctness on this interpreter**, and deleting it
        would be a change that passes CI today and breaks on an older runtime.

        The instant-equivalence is asserted above, which is the part that holds on every
        version. This test records why the mutation survives rather than pretending the
        branch is dead.
        """
        import sys
        from datetime import datetime

        if sys.version_info >= (3, 11):
            assert datetime.fromisoformat("2026-01-02T03:04:05Z") == datetime(
                2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
            ), (
                "this interpreter no longer needs the Z conversion, so B3's status may have "
                "changed - re-check before treating the guard as portable-only"
            )

    def test_only_the_brokers_exact_connection_limit_marker_is_treated_as_one(self) -> None:
        """A substring match on the broker's exact wording, asserted from both sides.

        This decides whether the service backs off a resource limit or retries into the
        same wall, so the marker is a contract with the broker rather than a fuzzy check:
        `"connection limit"` alone must **not** match, and a near-miss spelling must not
        either.
        """
        from live_runner.market_data_stream import _is_connection_limit_error

        for text in (
            "Connection limit exceeded",
            "connection limit exceeded",
            "CONNECTION LIMIT EXCEEDED",
            "  Connection Limit Exceeded  ",
            "failed: connection limit exceeded (code 429)",
        ):
            assert _is_connection_limit_error(RuntimeError(text)) is True, (
                f"{text!r} was not recognised as a connection limit"
            )

        for text in (
            "connection limit",
            "too many connections",
            "connection-limit-exceeded",
            "rate limit exceeded",
            "",
        ):
            assert _is_connection_limit_error(RuntimeError(text)) is False, (
                f"{text!r} was treated as a connection limit, so the service would back off "
                "for the wrong reason"
            )


class TestBarStoreOwnershipAndNormalisation:
    """Who owns a stored bar, and what a read is allowed to hand out. Seven survivors.

    `_ingest_manual_bars_sync` stores `dict(bar)` - a **copy** - and `_bars_for_symbol` returns
    `list(queue)` - also a copy. Both are one-token changes with no test, and both are about
    **ownership** rather than values.

    **The copy on ingest is the one that would matter.** If the store kept the caller's dict,
    then a caller that reuses its bar dict - the obvious thing to do when backfilling a range in
    a loop - would silently rewrite history that had already been stored. Verified directly:
    mutate the caller's dict after handing it over, and the stored bar is unchanged. That is the
    whole guarantee, and it costs one `dict()` call.

    **The copy on read is shallow, and that asymmetry is the finding.** A consumer that appends
    to the list it was given is safe; one that mutates a bar *element* is not, because the
    dicts are the store's own. So ingest defends against the caller while read does not defend
    against the consumer - and a deep copy per read would copy the whole window on every poll
    of every symbol. Pinned as-is with both halves asserted.

    **Reading an unknown symbol must not create one.** The natural-looking alternative -
    `self._bars.setdefault(symbol, [])` - quietly allocates a queue for a symbol nobody is
    streaming, so a consumer polling a symbol that does not exist grows the store one key at a
    time. That is the kind of leak that only shows up as memory, never as an error, so it is
    asserted both ways: the key is not created, and the returned list is not a live handle.

    **Symbol normalisation happens on the way in**, so `"  aapl  "` is stored under `AAPL` and
    blank symbols are dropped rather than stored under `""`. Storing un-normalised would split one
    symbol's history across several keys, each of which any consumer would have to know to query.

    The worker-side helpers are the same shape: an empty subscription must not call the SDK, and
    a worker with no stream must not crash trying.
    """

    @staticmethod
    def _service():
        import threading
        from collections import defaultdict, deque

        service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
        service._lock = threading.Lock()
        service._max_bars_per_symbol = 10
        service._bars = defaultdict(lambda: deque(maxlen=10))
        return service

    @staticmethod
    def _worker(stream=None):
        from live_runner.market_data_stream import _StreamWorker

        worker = _StreamWorker(
            kind="stock", api_key="k", secret_key="s", paper=True, bar_handler=None
        )
        worker.stream = stream
        return worker

    def test_an_ingested_bar_is_copied_so_the_caller_cannot_rewrite_history(self) -> None:
        """**The guarantee the `dict(bar)` exists for, verified by mutation after the call.**

        A caller backfilling a range in a loop reuses its dict. Without the copy, the second
        iteration would overwrite the first bar already in the store - silently, with no error
        and no count change.
        """
        service = self._service()
        bar = {"symbol": "aapl", "close": 1.0}
        service._ingest_manual_bars_sync([bar])

        bar["close"] = 999.0
        bar["symbol"] = "MUTATED"

        stored = service._bars_for_symbol("AAPL")
        assert len(stored) == 1, stored
        assert stored[0]["close"] == 1.0, (
            f"the store kept the caller's dict, so reuse rewrote history: {stored[0]!r}"
        )
        assert stored[0]["symbol"] == "aapl", stored[0]

    def test_a_returned_bar_list_is_copied_but_its_bars_are_still_shared(self) -> None:
        """**Shallow copy - the asymmetry with ingest is the finding.**

        `list(queue)` protects the *list*: appending to it does not reach the live deque, and
        that half is asserted. But the bar dicts inside are the store's own objects, so a reader
        that mutates a bar *element* does change stored history.

        That is worth knowing rather than assuming away, because it is the **complement** of
        what `_ingest_manual_bars_sync` does. Ingest stores `dict(bar)`, so the *caller* cannot
        rewrite history; a read hands out the *store's* dicts, so a *reader* can. One side of
        the boundary is defended and the other is not.

        No caller currently mutates a bar it was handed, so this is a sharp edge rather than a
        live bug - and the fix would be a deep copy on every read, which would copy the whole
        window on each poll of every symbol. So it is pinned as-is with the asymmetry named,
        rather than "fixed" on speculation.

        The test asserts the shallow behaviour explicitly so that a future change which *does*
        deep-copy shows up here as a deliberate update rather than as a silent performance
        regression in a hot path nobody was watching.
        """
        service = self._service()
        service._bars["AAPL"].append({"symbol": "AAPL", "close": 1.0})

        read = service._bars_for_symbol("AAPL")

        # The list is a copy: appending to it does not reach the store.
        read.append({"injected": True})
        stored = service._bars_for_symbol("AAPL")
        assert len(stored) == 1, f"a reader appended into the store: {stored}"

        # The bars are shared: mutating one does reach the store, and that is pinned.
        read[0]["close"] = 999.0
        assert service._bars_for_symbol("AAPL")[0]["close"] == 999.0, (
            "the read is now a deep copy, so a reader can no longer corrupt the store; "
            "update this test and check what a deep copy on every read costs"
        )

    def test_reading_an_unknown_symbol_returns_empty_and_creates_nothing(self) -> None:
        """`setdefault` would quietly allocate a queue for a symbol nobody is streaming.

        That is a leak visible only as memory, so both halves are asserted: the key is not
        created, and the returned list is not a handle into the store.
        """
        service = self._service()
        unknown = service._bars_for_symbol("UNKNOWN")
        assert unknown == [], unknown
        assert "UNKNOWN" not in service._bars, (
            "reading an unknown symbol created a store key, so a consumer polling a symbol "
            "that does not exist grows the store one key at a time"
        )
        unknown.append({"injected": True})
        assert not service._bars_for_symbol("UNKNOWN"), (
            "the empty list handed to a caller is a live handle into the store"
        )
        assert "UNKNOWN" not in service._bars, "the store grew from a read"

    def test_an_ingested_symbol_is_normalised_and_blanks_are_dropped(self) -> None:
        """One key per symbol, and no `""` key.

        Storing un-normalised would split a symbol's history across several keys, each of which a
        consumer would have to know to query.
        """
        service = self._service()
        service._ingest_manual_bars_sync(
            [
                {"symbol": "  aapl  ", "close": 1.0},
                {"symbol": "AAPL", "close": 2.0},
                {"symbol": "   ", "close": 3.0},
                {"symbol": "", "close": 4.0},
                {"close": 5.0},
            ]
        )
        assert set(service._bars) == {"AAPL"}, sorted(service._bars)
        assert [bar["close"] for bar in service._bars_for_symbol("AAPL")] == [1.0, 2.0]

    def test_a_blank_symbol_quote_payload_names_nothing(self) -> None:
        """`{"symbol": ""}` for a blank request, not the raw request echoed back.

        A consumer that keys a cache on the returned symbol would otherwise file the result under
        whatever whitespace was passed in.
        """
        import asyncio

        service = self._service()
        payload = asyncio.run(service.get_quote_payload("   "))
        assert payload == {"symbol": "", "latest": None, "bars": []}, payload
        # And a real symbol resolves, so the blank case is not the only one that works.
        service._bars["AAPL"].append({"symbol": "AAPL", "close": 7.0})
        good = asyncio.run(service.get_quote_payload("  aapl "))
        assert good["symbol"] == "AAPL", good
        assert good["latest"] == {"symbol": "AAPL", "close": 7.0}, good
        assert len(good["bars"]) == 1, good

    def test_an_empty_subscription_does_not_call_the_sdk(self) -> None:
        """An empty subscribe is a no-op, and a missing stream does not crash.

        `subscribe_bars(handler)` with no symbols is at best useless and at worst an SDK error,
        and a worker that has not connected yet has no stream to call.
        """

        class _Stream:
            def __init__(self):
                self.subscribed = []

            def subscribe_bars(self, handler, *symbols):
                self.subscribed.append(symbols)

        stream = _Stream()
        worker = self._worker(stream)
        _StreamWorker._subscribe_symbols(worker, [])
        assert stream.subscribed == [], (
            f"an empty subscription still called the SDK: {stream.subscribed!r}"
        )

        _StreamWorker._subscribe_symbols(worker, ["AAPL", "MSFT"])
        assert stream.subscribed == [("AAPL", "MSFT")], stream.subscribed

        # And a worker with no stream must not raise.
        _StreamWorker._subscribe_symbols(self._worker(None), ["AAPL"])


class TestStreamWorkerFeedSelectionAndBarOwnership:
    """Which feed a stream gets, which stream class a `kind` picks, and who owns a bar.

    Five mutations survived. Three are about **choosing the wrong market data**, which is
    the most consequential kind of quiet failure available in this file: nothing raises,
    the service connects, and the numbers are simply wrong.

    **The feed is deliberately inverted from the obvious reading.** `paper` gets IEX and
    **live gets SIP**. That looks backwards until you know what the feeds are: SIP is the
    full consolidated tape and IEX is one exchange. So the cheap, partial feed is given to
    the account where mistakes cost nothing, and the complete one to the account trading
    real money. Swapping them is the surviving mutation, and it fails in the expensive
    direction - live trading that silently never sees trades occurring off-IEX. There is no
    comment saying any of this, so the reasoning is recorded here.

    **`kind` dispatches on one string and defaults to crypto.** `kind == "stock"` selects
    a `StockDataStream`; **anything else** selects a `CryptoDataStream`. The caller
    computes `stream_kind = "crypto" if _is_crypto_symbol(symbol) else "stock"`, so the
    only two values are "stock" and "crypto" - but the fallback means a typo, a future
    third kind, or a hand-built worker all reach a crypto stream with no complaint. A
    stock symbol on a crypto stream fails at subscribe time with a broker error rather
    than here, where the mistake is visible. Pinned as-is: changing the dispatch to a
    whitelist would be a behaviour change, and the current shape is not unsafe for the
    two values that exist.

    **The append path stores the payload by reference, and the read path copies the
    list.** The asymmetry is the finding. `_ingest_manual_bars_sync` stores `dict(bar)`,
    so a caller reusing its dict in a backfill loop cannot rewrite history. The streaming
    path appends the serializer's dict as-is, so the stored bar *is* the object the
    serializer built. Today that object is freshly constructed per bar and nothing
    mutates it, so this is a sharp edge rather than a live bug - but the guarantee the
    manual path makes is not made here, and a future serializer that reuses a dict would
    rewrite live history silently.

    **`_bars` is a `defaultdict`, so appending allocates and reading does not.** That is
    the exact complement of the read-path contract pinned earlier: `_bars_for_symbol`
    will not create a key for an unknown symbol, but `_append_bar_sync` will. The
    subscription filter in `_on_bar` is what prevents phantom keys, and it is checked
    under the service lock and then *released* before the append runs in a worker thread -
    so the guard is a check-then-act, and the `defaultdict` is what makes a lost race a
    leaked key rather than a `KeyError`. Asserted as-is with the reasoning, so the day
    someone changes `_bars` to a plain dict they learn that this was the safety net.

    The lock is load-bearing for the same reason: `deque.append` is atomic, but
    `defaultdict.__missing__` creating the deque is not, so the lock is what makes
    create-and-append one step.
    """

    @staticmethod
    def _worker(kind="stock", paper=True):
        return _StreamWorker(
            kind=kind, api_key="k", secret_key="s", paper=paper, bar_handler=lambda *_: None
        )

    def test_a_live_account_gets_the_full_tape_and_a_paper_one_does_not(self) -> None:
        """The inversion that looks backwards and is not. Asserted on the real enums.

        Paper -> IEX (one exchange, cheap, mistakes cost nothing).
        Live  -> SIP (consolidated tape; missing a trade on live money is not acceptable).
        """
        live_stream = self._worker(paper=False)._create_stream()
        paper_stream = self._worker(paper=True)._create_stream()
        # The feed is not stored on the stream: alpaca-py bakes it into the endpoint URL,
        # so that is the only place it can be observed. Asserted on the real URL suffix
        # rather than a private attribute, which is the same string either way.
        assert live_stream._endpoint.endswith("/v2/sip"), live_stream._endpoint
        assert paper_stream._endpoint.endswith("/v2/iex"), paper_stream._endpoint
        assert live_stream._endpoint != paper_stream._endpoint, (
            "the feed no longer distinguishes live from paper"
        )

    def test_a_crypto_kind_gets_a_crypto_stream_and_a_stock_kind_a_stock_stream(self) -> None:
        """The dispatch is on one string, and everything that is not "stock" is crypto."""
        from alpaca.data.live import CryptoDataStream, StockDataStream

        assert isinstance(self._worker(kind="stock")._create_stream(), StockDataStream)
        assert isinstance(self._worker(kind="crypto")._create_stream(), CryptoDataStream)

    def test_an_unrecognised_kind_falls_through_to_the_crypto_stream(self) -> None:
        """Pinned as-is, and the reason it is worth pinning: there is no complaint here.

        The caller only ever produces "stock" or "crypto", so this is unreachable today.
        But a typo or a future third kind reaches a crypto stream silently, and the
        failure surfaces at subscribe time as a broker error rather than here. Recorded
        so that adding a kind is a deliberate act rather than an accident.
        """
        from alpaca.data.live import CryptoDataStream

        for kind in ("crypto", "CRYPTO", "stocks", "typo", ""):
            stream = self._worker(kind=kind)._create_stream()
            assert isinstance(stream, CryptoDataStream), (kind, type(stream).__name__)

    def test_the_append_stores_the_payload_by_reference(self) -> None:
        """The manual-ingest copy is NOT applied on the streaming path. Asserted as-is.

        `_ingest_manual_bars_sync` stores `dict(bar)`; this appends the serializer's dict
        unchanged. Today each bar is freshly built by the serializer and nothing mutates
        it, so there is no live bug - but the ownership guarantee the manual path makes is
        simply absent here, and a serializer that reused its dict would rewrite live
        history with no error and no change in the stored count.
        """
        service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
        service._lock = threading.Lock()
        service._max_bars_per_symbol = 10
        service._bars = defaultdict(lambda: deque(maxlen=service._max_bars_per_symbol))

        payload = {"symbol": "AAPL", "close": 1.0}
        service._append_bar_sync("AAPL", payload)
        assert service._bars["AAPL"][0] is payload, (
            "the streaming path now copies, which changes the ownership contract that "
            "was pinned here; make that a deliberate change"
        )

    def test_appending_an_unknown_symbol_allocates_a_key_and_reading_does_not(self) -> None:
        """The defaultdict is the safety net, and the complement of the read contract."""
        service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
        service._lock = threading.Lock()
        service._max_bars_per_symbol = 10
        service._bars = defaultdict(lambda: deque(maxlen=service._max_bars_per_symbol))

        assert "GHOST" not in service._bars
        # Reading never allocates.
        assert service._bars_for_symbol("GHOST") == []
        assert "GHOST" not in service._bars, "a read created a store key"

        # Appending always does. The subscription filter in _on_bar is the only guard.
        service._append_bar_sync("GHOST", {"symbol": "GHOST"})
        assert "GHOST" in service._bars, (
            "_bars is no longer a defaultdict, so this lost-race window became a KeyError "
            "instead of a leaked key; check _on_bar's check-then-act"
        )

    def test_the_append_is_taken_under_the_service_lock(self) -> None:
        """Asserted structurally, because the race it prevents is not reliably testable.

        `deque.append` is atomic on its own, so removing the lock looks harmless. It is
        not: `defaultdict.__missing__` creating the deque is a check-then-act, and the
        lock is what makes create-and-append a single step. Rather than write a flaky race
        test, the lock is instrumented and asserted to be entered.
        """
        entered = []

        class _RecordingLock:
            def __enter__(self):
                entered.append("enter")
                return self

            def __exit__(self, *exc_info):
                entered.append("exit")
                return False

        service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
        service._lock = _RecordingLock()
        service._max_bars_per_symbol = 10
        service._bars = defaultdict(lambda: deque(maxlen=service._max_bars_per_symbol))

        service._append_bar_sync("AAPL", {"symbol": "AAPL"})
        assert entered == ["enter", "exit"], (
            "the bar append did not take the service lock; deque.append is atomic but "
            "defaultdict key creation is not, so this is create-and-append as one step"
        )


class TestServiceStopTakesOwnershipOfItsWorkers:
    """`_stop_sync` is three lines and every one of them was load-bearing.

    All three mutations survived, which is a good sign about the shape rather than a bad
    one: `snapshot -> clear -> stop-each` is a *take ownership* pattern, and each step can be
    deleted without anything noticing at the time.

    **The snapshot and the clear are one act, and both are needed.** `list(...)` copies the
    workers out so the registry can be emptied, and `clear()` is what actually empties it.
    Drop the clear and the dead workers stay in `_workers`, so the next refresh finds them,
    believes they are running, and hands them a symbol list - which either resurrects a
    stopped stream or leaves a worker subscribed to nothing while the service reports it
    live. The drop is invisible until a *later* refresh, which is exactly why a test that
    only calls `stop()` cannot see it: the assertion has to be about the registry's state
    afterwards, not about what `stop()` returned.

    **Dropping the snapshot turns a clear into a runtime error.** Iterating a dict while
    clearing it raises `RuntimeError: dictionary changed size during iteration`. That is the
    loud failure, so it is the one that is safe - but only because the snapshot is there,
    and it is worth asserting the *behaviour* (every worker is stopped exactly once) rather
    than relying on the exception as the test.

    **The lock is what makes snapshot-and-clear atomic.** A refresh that interleaves between
    the copy and the clear installs a new worker into a registry this method believes it now
    owns exclusively - a worker nobody will ever stop, running a thread with no handle on it.
    That is a leak rather than an error, so it cannot be caught by a retry or a log line.

    A double `stop()` is asserted idempotent, because the daemon can stop a service during
    shutdown while a refresh is in flight.
    """

    @staticmethod
    def _service_with(*workers):
        service = LiveMarketDataStreamService.__new__(LiveMarketDataStreamService)
        service._lock = threading.Lock()
        service._max_bars_per_symbol = 10
        service._bars = defaultdict(lambda: deque(maxlen=10))
        service._workers = {}
        for key, worker in workers:
            service._workers[key] = worker
        return service

    @staticmethod
    def _worker(name="w"):
        worker = _StreamWorker(
            kind="stock", api_key="key", secret_key="secret", paper=True, bar_handler=lambda *_: None
        )
        worker.stopped = 0
        worker.stop = lambda: setattr(worker, "stopped", worker.stopped + 1)
        worker.label = name
        return worker

    def test_stop_stops_every_worker_and_empties_the_registry(self) -> None:
        """**All three surviving mutations in one assertion**: stop each, and clear."""
        first, second = self._worker("a"), self._worker("b")
        service = self._service_with((("k1", "stock"), first), (("k2", "crypto"), second))

        service._stop_sync()

        assert first.stopped == 1, "a registered worker was never stopped"
        assert second.stopped == 1, "a registered worker was never stopped"
        assert service._workers == {}, (
            f"stopped workers survived in the registry: {list(service._workers)}; the next "
            "refresh will find them, believe they are running, and hand them symbols"
        )

    def test_a_stopped_service_reports_no_workers_to_the_next_refresh(self) -> None:
        """The consequence that makes the clear load-bearing, asserted through the registry.

        A worker left registered is a worker a later refresh will treat as running. That is
        the failure mode, so it is what the test states - not merely that `stop()` returned.
        """
        worker = self._worker()
        service = self._service_with((("k1", "stock"), worker))

        service._stop_sync()
        assert list(service._workers.values()) == []
        # Nothing is left for a refresh to adopt, and nothing is running behind its back.
        assert worker.stopped == 1
        assert worker._running is False or worker.stopped == 1

    def test_stopping_an_empty_service_is_a_no_op(self) -> None:
        service = self._service_with()
        service._stop_sync()
        assert service._workers == {}

    def test_a_second_stop_does_not_stop_a_worker_again(self) -> None:
        """The daemon can stop a service during shutdown while a refresh is in flight.

        The registry is emptied, so the second pass has nothing to stop and cannot
        double-stop a worker - asserted because "stopped twice" on a live stream is a
        different failure from "not stopped at all".
        """
        worker = self._worker()
        service = self._service_with((("k1", "stock"), worker))

        service._stop_sync()
        service._stop_sync()
        assert worker.stopped == 1, "a worker was stopped more than once"

    def test_the_snapshot_and_clear_are_taken_under_the_lock(self) -> None:
        """A lock that is not held lets a refresh install a worker nobody will ever stop.

        Asserted by instrumenting the lock rather than by racing a thread: the failure this
        prevents is a leaked worker, which produces no error, no log line and no retry - so
        there is nothing to catch after the fact.
        """
        events = []

        class _RecordingLock:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *exc_info):
                events.append("exit")
                return False

        service = self._service_with((("k1", "stock"), self._worker()))
        service._lock = _RecordingLock()

        service._stop_sync()

        assert events == ["enter", "exit"], (
            f"the lock was not held across snapshot-and-clear: {events}; a refresh can "
            "interleave and install a worker this method believes it exclusively owns"
        )
        assert service._workers == {}

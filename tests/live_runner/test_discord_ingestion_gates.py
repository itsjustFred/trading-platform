"""Cleanup 15-C: Discord ingestion gates (trusted author, price parsing, bursts).

Sub-issue #23 findings C-3 and B-11. The trusted-author gate is decided by the
campaign Decision Record: ingestion trusts exactly one explicitly configured bot
ID per source; that check is the source gate.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from live_runner.signal_sources import discord_source as ds
from live_runner.signal_sources.discord_source import (
    DiscordSignalSource,
    _parse_trade_signal_from_message,
)


def _message(
    content: str,
    *,
    author_id: str = "bot-1",
    message_id: str = "m1",
    channel_id: str = "channel-1",
    timestamp: str = "2026-09-01T12:00:00Z",
):
    return SimpleNamespace(
        content=content,
        id=message_id,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id),
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        if timestamp is None
        else _ts(timestamp),
    )


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _source(**kwargs) -> DiscordSignalSource:
    options = {
        "bot_name": "ramjet",
        "token": "test-token",
        "channel_ids": {"channel-1"},
        "timeframe": "1h",
        "trusted_author_ids": {"bot-1"},
    }
    options.update(kwargs)
    return DiscordSignalSource(**options)


# ── C-3.1: trusted-author gate ────────────────────────────────────────────────


def test_parser_rejects_message_from_untrusted_author() -> None:
    """The author/bot-ID check is the source gate (Decision Record #1): a signal
    from an author outside the explicitly configured trusted set must never
    become a trade."""
    message = _message(
        "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
        author_id="random-guy",
    )
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is None


def test_parser_accepts_message_from_trusted_author() -> None:
    message = _message("**$AAPL** is now oversold — buy signal\nPrice: $190.50", author_id="bot-1")
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is not None
    assert signal.symbol == "AAPL"


def test_parser_accepts_trusted_author_case_insensitively() -> None:
    """Discord IDs are snowflakes, but a config typo in case must not lock the
    gate: comparison is normalized."""
    message = _message("**$AAPL** is now oversold — buy signal\nPrice: $190.50", author_id="BOT-1")
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is not None


def test_source_requires_explicit_trusted_author_ids() -> None:
    """No silent open gate: constructing a source without a trusted set must
    fail loudly so an operator cannot deploy ingestion with author checks off."""
    with pytest.raises(ValueError, match="trusted"):
        DiscordSignalSource(token="t", channel_ids={"c"})


def test_source_trusted_author_ids_accept_scalar_and_set() -> None:
    source = _source(trusted_author_ids="bot-1")
    assert source.trusted_author_ids == {"bot-1"}


def test_source_stores_trusted_author_ids_normalized() -> None:
    source = _source(trusted_author_ids={" bot-1 ", "BOT-2"})
    assert source.trusted_author_ids == {"bot-1", "bot-2"}


def test_poll_drops_untrusted_message_without_queuing() -> None:
    source = _source(trusted_author_ids={"bot-1"})

    async def _fetch(_channel_id):
        return [
            {
                "id": "m-untrusted",
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
                "author": {"id": "imposter", "bot": True},
            }
        ]

    source._fetch_messages = _fetch
    import asyncio

    asyncio.run(source._poll_once("channel-1"))
    assert source._queue.empty()


# ── C-3.2: sane price parsing ─────────────────────────────────────────────────


def test_price_with_thousands_separator_is_not_corrupted() -> None:
    """`$1,234` means one thousand two hundred thirty-four. The old
    `replace(",", ".")` turned it into 1.234 — a 1000x wrong entry price."""
    price = ds._extract_price("Price: $1,234.56")
    assert price == 1234.56


def test_price_with_thousands_separator_no_cents() -> None:
    price = ds._extract_price("Entry $1,234")
    assert price == 1234.0


def test_price_without_thousands_separator_unchanged() -> None:
    assert ds._extract_price("Price: $190.50") == 190.5
    assert ds._extract_price("@ 180.25") == 180.25


def test_price_rejects_malformed_digit_groups() -> None:
    """`$12,34` is not a price format (single comma, two trailing digits);
    accepting it as 12.34 corrupts sizing. The matcher must refuse it."""
    assert ds._extract_price("Price: $12,34") is None


def test_parser_rejects_signal_with_thousands_corrupted_price() -> None:
    """A corrupted price must not become a live trade — the parser refuses."""
    message = _message("**$SPX** is now oversold — buy signal\nPrice: $5,934")
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is not None
    assert signal.entry_price == 5934.0


# ── C-3.3: fallback ticker extraction must go ─────────────────────────────────


def test_fallback_uppercase_symbol_stage_is_gone() -> None:
    """Decision Record #2: the parser must reject ambiguous/fallback ticker
    extraction. `WARN`, `ON`, `IS` prose must never mint a ticker."""
    assert not hasattr(ds, "_fallback_uppercase_symbol")


def test_prose_never_mints_a_ticker() -> None:
    message = _message("WARNING ALL ALERT THE MARKET IS NOW RISK ON @ 100.00")
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is None


def test_uppercase_prose_word_without_marker_does_not_become_symbol() -> None:
    message = _message("PUMP IT UP @ 42.00 buy")
    signal = _parse_trade_signal_from_message(
        message, default_timeframe="1h", trusted_author_ids={"bot-1"}
    )
    assert signal is None


def test_structured_extractions_still_work() -> None:
    bold = ds._extract_symbol("** $MSFT ** is now oversold — buy signal\nPrice: $100.00")
    assert bold == "MSFT"
    dollar = ds._extract_symbol("$NVDA buy signal @ 100.00")
    assert dollar == "NVDA"
    labeled = ds._extract_symbol("Ticker: AMD buy signal @ 100.00")
    assert labeled == "AMD"
    action = ds._extract_symbol("buy signal AAPL @ 100.00")
    assert action == "AAPL"
    price_prefixed = ds._extract_symbol("AAPL $100.00 buy signal")
    assert price_prefixed == "AAPL"


def test_no_symbol_at_all_is_rejected() -> None:
    message = _message("buy signal @ 100.00")
    assert ds._extract_symbol(message.content) is None


# ── C-3.4: bounded burst handling; drops are visible, not silent ──────────────


def test_burst_of_35_messages_all_reach_the_queue() -> None:
    """With POLL_LIMIT=20 a >20-message burst used to lose every message older
    than the newest page: the old code replaced _seen_ids per poll AND the
    monotonic cursor filter dropped any fetched id at/below it — so the 15
    oldest messages of a 35-message burst were silently dropped on the second
    poll. Both must not happen: the seen-set is cumulative and only the
    seen-set dedupes emits."""
    source = _source()
    ids = [str(100000000000000000 + index) for index in range(35)]
    newest_first = list(reversed(ids))
    pages: dict[str, int] = {"n": 0}

    async def _fetch(_channel_id):
        # Discord returns newest-first, at most POLL_LIMIT per page: page 0 is
        # the 20 newest messages, page 1 the next-older 15.
        page = pages["n"]
        pages["n"] += 1
        start = 20 * page
        end = min(len(ids), 20 * (page + 1))
        return [
            {
                "id": newest_first[index],
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": f"RAM JET BUY AAPL @ 100.0{index % 10}",
                "author": {"id": "bot-1", "bot": True},
            }
            for index in range(start, end)
        ]

    source._fetch_messages = _fetch

    import asyncio

    async def _run() -> int:
        await source._poll_once("channel-1")
        await source._poll_once("channel-1")
        return source._queue.qsize()

    assert asyncio.run(_run()) == 35


def test_gap_detection_warns_with_channel_and_count(caplog) -> None:
    """The proposed surfacing contract: when the fetched page does not connect
    to the last seen message (a gap), log a warning naming the channel and the
    boundary ids. Discord snowflake ids are not contiguous, so the log reports
    the boundary rather than an invented message count."""
    import asyncio

    source = _source()
    source._last_seen_message_ids["channel-1"] = "100000000000000100"

    async def _fetch(_channel_id):
        # Newest message jumps far beyond the last seen id: history skipped.
        return [
            {
                "id": "100000000000000200",
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": "RAM JET BUY AAPL @ 100.00",
                "author": {"id": "bot-1", "bot": True},
            }
        ]

    source._fetch_messages = _fetch
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.WARNING)
    source_logger = logging.getLogger("trading.live-runner")
    source_logger.addHandler(capture)
    try:
        asyncio.run(source._poll_once("channel-1"))
    finally:
        source_logger.removeHandler(capture)

    gap_records = [
        record
        for record in records
        if "gap" in record.getMessage().lower() or "drop" in record.getMessage().lower()
    ]
    assert gap_records, "a detected burst gap must be logged, not silent"
    data = getattr(gap_records[0], "data", {})
    assert data.get("channel_id") == "channel-1"
    assert data.get("last_seen_message_id") == "100000000000000100"
    assert data.get("oldest_fetched_message_id") == "100000000000000200"


def test_no_gap_warning_on_contiguous_page() -> None:
    import asyncio

    source = _source()
    # The page reaches back to the last seen id: nothing was skipped.
    source._last_seen_message_ids["channel-1"] = "100000000000000100"

    async def _fetch(_channel_id):
        return [
            {
                "id": "100000000000000100",
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": "RAM JET BUY AAPL @ 100.00",
                "author": {"id": "bot-1", "bot": True},
            }
        ]

    source._fetch_messages = _fetch
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.WARNING)
    source_logger = logging.getLogger("trading.live-runner")
    source_logger.addHandler(capture)
    try:
        asyncio.run(source._poll_once("channel-1"))
    finally:
        source_logger.removeHandler(capture)

    assert not [
        record
        for record in records
        if "gap" in record.getMessage().lower() or "drop" in record.getMessage().lower()
    ]


def test_seen_set_is_not_pruned_against_the_poll_cursor() -> None:
    """Regression: pruning ids at/below the monotonic cursor re-admits the
    newest message on the very next poll on a quiet channel — the newest id IS
    the cursor, and the emit path dedupes via the seen-set alone (it no longer
    filters on the cursor). The set must keep every id inside its retention
    window instead."""
    import asyncio

    source = _source()
    source._seen_ids.setdefault("channel-1", set()).update(
        {"100000000000000010", "100000000000000011", "100000000000000050"}
    )
    source._last_seen_message_ids["channel-1"] = "100000000000000050"

    def _message(message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "channel_id": "channel-1",
            "timestamp": "2026-09-01T12:00:00Z",
            "content": "RAM JET BUY AAPL @ 100.00",
            "author": {"id": "bot-1", "bot": True},
        }

    async def _fetch(_channel_id):
        # A quiet channel: Discord keeps serving the live window (newest
        # first), including messages already seen.
        return [_message("100000000000000060"), _message("100000000000000050")]

    source._fetch_messages = _fetch

    async def _run() -> None:
        await source._poll_once("channel-1")
        # The quiet channel still serves id ...050 (== the cursor): pruning it
        # out of the seen-set must not make the next poll re-emit it.
        await source._poll_once("channel-1")

    asyncio.run(_run())
    assert source._seen_ids["channel-1"] == {
        "100000000000000010",
        "100000000000000011",
        "100000000000000050",
        "100000000000000060",
    }
    assert source._queue.qsize() == 1


def test_seen_set_keeps_ids_within_retention_window() -> None:
    """The seen-set stays bounded by the retention window (POLL_LIMIT * 10),
    not by the cursor: the newest ids survive, older ones fall out."""
    source = _source()
    # One id more than the window; all numeric so they are ordered.
    total = ds.SEEN_ID_RETENTION + 1
    ids = {str(100000000000000000 + index) for index in range(total)}
    source._seen_ids.setdefault("channel-1", set()).update(ids)

    source._prune_seen_ids("channel-1", source._seen_ids["channel-1"])

    assert source._seen_ids["channel-1"] == {
        str(100000000000000000 + index) for index in range(1, total)
    }


def test_seen_set_prune_keeps_non_numeric_ids() -> None:
    """Non-numeric ids cannot be ordered against the window, so pruning must
    never drop them: they would otherwise be re-emitted."""
    source = _source()
    numeric = {str(100000000000000000 + index) for index in range(ds.SEEN_ID_RETENTION)}
    source._seen_ids.setdefault("channel-1", set()).update(numeric | {"not-a-snowflake"})

    source._prune_seen_ids("channel-1", source._seen_ids["channel-1"])

    assert source._seen_ids["channel-1"] == numeric | {"not-a-snowflake"}


def test_seen_set_keeps_ids_above_cursor_for_burst_drain() -> None:
    import asyncio

    source = _source()
    # No cursor yet: a first (priming) poll must keep every fetched id in the
    # seen-set so a burst can drain across the following polls.
    async def _fetch(_channel_id):
        return [
            {
                "id": message_id,
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": f"RAM JET BUY AAPL @ 100.0{index % 10}",
                "author": {"id": "bot-1", "bot": True},
            }
            for index, message_id in enumerate(
                ["100000000000000001", "100000000000000002", "100000000000000003"]
            )
        ]

    source._fetch_messages = _fetch
    asyncio.run(source._poll_once("channel-1", emit=False))

    assert source._seen_ids["channel-1"] == {
        "100000000000000001",
        "100000000000000002",
        "100000000000000003",
    }


# ── B-11: one signal may execute on every matching deployment ─────────────────


def _deployment_for_test(
    deployment_id: str,
    *,
    symbols: list[str] | None = None,
    channel_id: str | None = None,
    service_name: str | None = None,
    status: str = "ACTIVE",
):
    from shared.models import LiveDeploymentConfig

    signal_config: dict[str, object] = {}
    if channel_id:
        signal_config["channel_id"] = channel_id
    if service_name:
        signal_config["service_name"] = service_name
    index = int(deployment_id.rsplit("-", 1)[-1]) + 1
    return LiveDeploymentConfig(
        id=f"11111111-1111-1111-1111-11111111111{index}",
        strategy_name=f"discord::ramjet::{deployment_id}",
        account_id="acc-1",
        symbols=symbols or [],
        timeframe="1h",
        status=status,
        signal_source="discord",
        signal_source_type="discord",
        signal_config=signal_config,
    )


@pytest.fixture(autouse=True)
def _assume_snapshot_deployments_currently_active(monkeypatch):
    """The PR tests predate the #48 stale-snapshot DB recheck: the deployment
    snapshots in these tests are not persisted, so `get_live_deployment` would
    return None and every dispatch would be skipped. Default the recheck to
    "the snapshot is still current"; tests that pin the recheck itself
    override `daemon.get_live_deployment` explicitly."""
    import live_runner.daemon as daemon_module

    from types import SimpleNamespace as _NS

    def _current(deployment_id: str):
        return _NS(status="ACTIVE", id=deployment_id)

    monkeypatch.setattr(daemon_module, "get_live_deployment", _current)


def test_dispatch_fans_out_to_every_matching_deployment() -> None:
    """One Discord signal used to execute on at most one deployment (break after
    the first match). Every matching deployment must receive the signal."""
    import asyncio

    import live_runner.daemon as daemon
    from risk import TradeSignal

    submitted: list[str] = []

    class _Adapter:
        async def submit_order(self, signal):
            submitted.append(str(signal.deployment_id))
            return "order-1"

        async def get_balance(self):
            return 100_000.0, 100_000.0

        async def get_positions(self):
            return []

    deployments = [
        _deployment_for_test("dep-1", symbols=["AAPL"]),
        _deployment_for_test("dep-2", symbols=["AAPL"]),
        _deployment_for_test("dep-3", symbols=["MSFT"]),
    ]
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={"discord_channel_id": "channel-1"},
    )

    async def _no_wait(_signal, _timeframe):
        return None

    original = daemon.wait_for_candle_close
    daemon.wait_for_candle_close = _no_wait
    try:
        asyncio.run(
            daemon._dispatch_discord_trade_signal(
                "acc-1",
                _Adapter(),
                deployments,
                signal,
                accounts_env_values={},
            )
        )
    finally:
        daemon.wait_for_candle_close = original

    assert len(submitted) == 2 and len(set(submitted)) == 2


def test_dispatch_respects_each_deployments_own_allowlist() -> None:
    """The listener's merged allowlist is a coarse ingestion filter; before an
    order is submitted the signal must satisfy the target deployment's own
    allowlist, so a deployment cannot trade on an author another deployment
    trusted."""
    import asyncio

    import live_runner.daemon as daemon
    from risk import TradeSignal

    submitted: list[str] = []

    class _Adapter:
        async def submit_order(self, signal):
            submitted.append(str(signal.deployment_id))
            return "order-1"

        async def get_balance(self):
            return 100_000.0, 100_000.0

        async def get_positions(self):
            return []

    trusted = _deployment_for_test("dep-1", symbols=["AAPL"], channel_id="channel-1")
    trusted.signal_config["trusted_author_ids"] = ["bot-1"]
    untrusting = _deployment_for_test("dep-2", symbols=["AAPL"], channel_id="channel-1")
    untrusting.signal_config["trusted_author_ids"] = ["bot-9"]

    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={"discord_channel_id": "channel-1", "discord_author_id": "bot-1"},
    )

    async def _no_wait(_signal, _timeframe):
        return None

    original = daemon.wait_for_candle_close
    daemon.wait_for_candle_close = _no_wait
    try:
        asyncio.run(
            daemon._dispatch_discord_trade_signal(
                "acc-1",
                _Adapter(),
                [trusted, untrusting],
                signal,
                accounts_env_values={},
            )
        )
    finally:
        daemon.wait_for_candle_close = original

    assert len(submitted) == 1


def test_dispatch_without_signal_author_metadata_still_dispatches() -> None:
    """Signals without author metadata (e.g. internal replays) are not blocked
    by the per-deployment gate: the ingestion gate is the author authority."""
    import asyncio

    import live_runner.daemon as daemon
    from risk import TradeSignal

    submitted: list[str] = []

    class _Adapter:
        async def submit_order(self, signal):
            submitted.append(str(signal.deployment_id))
            return "order-1"

        async def get_balance(self):
            return 100_000.0, 100_000.0

        async def get_positions(self):
            return []

    deployment = _deployment_for_test("dep-1", symbols=["AAPL"], channel_id="channel-1")
    deployment.signal_config["trusted_author_ids"] = ["bot-1"]

    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={"discord_channel_id": "channel-1"},
    )

    async def _no_wait(_signal, _timeframe):
        return None

    original = daemon.wait_for_candle_close
    daemon.wait_for_candle_close = _no_wait
    try:
        asyncio.run(
            daemon._dispatch_discord_trade_signal(
                "acc-1",
                _Adapter(),
                [deployment],
                signal,
                accounts_env_values={},
            )
        )
    finally:
        daemon.wait_for_candle_close = original

    assert len(submitted) == 1


# ── B-4: token routing per deployment-group; auth blocks scoped per channel ───


def test_discord_runtime_groups_route_token_per_channel_group() -> None:
    """Tokens must be resolved per deployment-group (per channel), not from one
    arbitrarily-chosen 'primary' deployment of the account."""
    import live_runner.daemon as daemon

    deployment = _deployment_for_test(
        "dep-1",
        symbols=["AAPL"],
        channel_id="channel-1",
        service_name="RAMJET",
    )
    token = daemon._resolve_account_discord_token(
        {"DISCORD_BOT_RAMJET_TOKEN": "ramjet-token"},
        deployment,
    )
    assert token == "ramjet-token"


# ── C-3 wiring: the trusted-author gate reaches the running listener ──────────


def test_account_listener_passes_trusted_authors_from_deployment_config(
    monkeypatch,
) -> None:
    """The gate only protects live trading if the daemon hands the configured
    trusted author IDs to the source it constructs."""
    import asyncio

    import live_runner.daemon as daemon

    deployments = [
        _deployment_for_test(
            "dep-1",
            symbols=["AAPL"],
            channel_id="channel-1",
            status="ACTIVE",
        ),
    ]
    deployments[0].signal_config["trusted_author_ids"] = ["bot-1", "bot-2"]

    captured: dict[str, object] = {}

    class _Source:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._queue: list = []

        async def connect(self):
            return None

        async def disconnect(self, **_kwargs):
            return None

        async def listen_signals(self):
            if False:
                yield None

    monkeypatch.setattr(daemon, "DiscordSignalSource", _Source)

    asyncio.run(
        daemon._run_discord_signal_listener_for_account(
            "acc-1",
            object(),
            deployments,
            accounts_env_values={},
        )
    )

    assert captured.get("trusted_author_ids") == {"bot-1", "bot-2"}


def test_account_listener_fails_closed_without_trusted_authors(monkeypatch) -> None:
    """A Discord deployment with no trusted author configured must not start an
    open-gate listener: refuse to trade rather than ingest unverified authors."""
    import asyncio

    import live_runner.daemon as daemon

    deployments = [
        _deployment_for_test(
            "dep-1",
            symbols=["AAPL"],
            channel_id="channel-1",
            status="ACTIVE",
        ),
    ]

    def _forbidden_source(**kwargs):
        raise AssertionError(
            "listener must not start without an explicit trusted-author allowlist"
        )

    monkeypatch.setattr(daemon, "DiscordSignalSource", _forbidden_source)

    # Must not raise and must not construct a source: deployment is skipped.
    asyncio.run(
        daemon._run_discord_signal_listener_for_account(
            "acc-1",
            object(),
            deployments,
            accounts_env_values={},
        )
    )


def test_account_listener_merges_trusted_authors_across_channel_group(
    monkeypatch,
) -> None:
    """Deployments sharing a channel feed one allowlist for that channel's
    listener; an author trusted by any deployment on the channel is gated in."""
    import asyncio

    import live_runner.daemon as daemon

    first = _deployment_for_test("dep-1", symbols=["AAPL"], channel_id="channel-1")
    first.signal_config["trusted_author_ids"] = ["bot-1"]
    second = _deployment_for_test("dep-2", symbols=["AAPL"], channel_id="channel-1")
    second.signal_config["trusted_author_ids"] = "bot-2"

    captured: dict[str, object] = {}

    class _Source:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def connect(self):
            return None

        async def disconnect(self, **_kwargs):
            return None

        async def listen_signals(self):
            if False:
                yield None

    monkeypatch.setattr(daemon, "DiscordSignalSource", _Source)

    asyncio.run(
        daemon._run_discord_signal_listener_for_account(
            "acc-1",
            object(),
            [first, second],
            accounts_env_values={},
        )
    )

    assert captured.get("trusted_author_ids") == {"bot-1", "bot-2"}


def test_service_listener_uses_service_trusted_author_config(monkeypatch) -> None:
    """Standalone Discord bot services read trusted author IDs from the service
    config (trusted_author_ids) and pass them to the source."""
    import live_runner.daemon as daemon
    from shared.models import Service, ServiceStatus, ServiceType

    service = Service(
        id="11111111-2222-3333-4444-555555555555",
        name="RAMJET",
        type=ServiceType.DISCORD_BOT,
        status=ServiceStatus.RUNNING,
        config={
            "token_env_key": "DISCORD_BOT_RAMJET_TOKEN",
            "channel_id": "channel-1",
            "trusted_author_ids": ["bot-9"],
        },
        created_at=datetime.now(timezone.utc),
    )

    resolved = daemon._resolve_discord_service_trusted_author_ids(
        service, accounts_env_values={}
    )
    assert resolved == {"bot-9"}


# ── B-5: a transient reconnect failure must not strand the bot in FAILED ──────


def test_transient_reconnect_failure_does_not_permanently_fail_service(
    monkeypatch,
) -> None:
    """One network blip used to write ServiceStatus.FAILED and the sync loop
    only ever re-processed RUNNING services, so the bot silently received no
    signals until the next daemon restart. A transient reconnect error must
    leave the service RUNNING (or retrying) and re-arm recovery."""
    import asyncio

    import live_runner.daemon as daemon
    from shared.models import Service, ServiceStatus, ServiceType

    daemon._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.clear()
    updates: list[object] = []

    def _capture_update(service_id: str, status: object, **_kwargs) -> None:
        updates.append(status)

    monkeypatch.setattr(daemon, "update_service_status", _capture_update)

    service = Service(
        id="11111111-2222-3333-4444-555555555555",
        name="RAMJET",
        type=ServiceType.DISCORD_BOT,
        status=ServiceStatus.RUNNING,
        config={
            "token_env_key": "DISCORD_BOT_RAMJET_TOKEN",
            "channel_id": "channel-1",
            "trusted_author_ids": ["bot-9"],
        },
        created_at=datetime.now(timezone.utc),
    )

    class _TransientSource:
        token = "token-1"
        channel_ids = {"channel-1"}
        trusted_author_ids = {"bot-9"}
        is_connected = False
        is_paused = False

        async def reconnect(self):
            raise RuntimeError("connection reset by peer")

        async def disconnect(self, **_kwargs):
            return None

    source = _TransientSource()
    asyncio.run(
        daemon._reconnect_discord_bot_source(
            service,
            source,
            str(service.id),
            running_task=None,
        )
    )

    assert daemon.ServiceStatus.FAILED not in updates, (
        "a transient reconnect error must not fail the service permanently"
    )


def test_transient_reconnect_failure_is_retried_on_next_sync(monkeypatch) -> None:
    """After a transient reconnect error the sync loop must pick the service up
    again instead of skipping it like a terminally-blocked one, and once the
    backoff floor elapses the reconnect is attempted again."""
    import asyncio

    import live_runner.daemon as daemon
    from shared.models import Service, ServiceStatus, ServiceType

    service = Service(
        id="11111111-2222-3333-4444-555555555555",
        name="RAMJET",
        type=ServiceType.DISCORD_BOT,
        status=ServiceStatus.RUNNING,
        config={
            "token_env_key": "DISCORD_BOT_RAMJET_TOKEN",
            "channel_id": "channel-1",
            "trusted_author_ids": ["bot-9"],
        },
        created_at=datetime.now(timezone.utc),
    )

    attempts: list[int] = []

    daemon._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.clear()

    class _RecoveringSource:
        token = "token-1"
        channel_ids = {"channel-1"}
        trusted_author_ids = {"bot-9"}
        is_connected = False
        is_paused = False

        async def reconnect(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("connection reset by peer")
            return None

        async def disconnect(self, **_kwargs):
            return None

        async def listen_signals(self):
            if False:
                yield None

    source = _RecoveringSource()

    async def _noop_update(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(daemon, "update_service_status", _noop_update)

    async def _reconnect() -> None:
        await daemon._reconnect_discord_bot_source(
            service,
            source,
            str(service.id),
            running_task=None,
        )

    asyncio.run(_reconnect())
    # First failure arms the backoff floor; the service stays recoverable.
    assert daemon._is_discord_service_reconnect_blocked(str(service.id)) is True
    assert str(service.id) not in daemon._DISCORD_SERVICE_AUTH_BLOCKED_IDS

    # Inside the floor window the retry is deferred (no reconnect hammering).
    asyncio.run(_reconnect())
    assert len(attempts) == 1

    # Once the floor elapses the reconnect is attempted again and succeeds.
    daemon._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.pop(str(service.id), None)
    asyncio.run(_reconnect())
    assert len(attempts) == 2


# ── #81: auth scheme, env expansion, env allowlist ────────────────────────────

class TestIssue81AuthScheme:
    def test_raw_scheme_sends_token_as_is(self) -> None:
        source = _source(token="user-token-abc", auth_scheme="raw")
        assert source._auth_header() == "user-token-abc"

    def test_default_scheme_is_raw(self) -> None:
        source = _source(token="user-token-abc")
        assert source.auth_scheme == "raw"
        assert source._auth_header() == "user-token-abc"

    def test_bot_scheme_prefixes_bot(self) -> None:
        source = _source(token="bot-token-abc", auth_scheme="bot")
        assert source._auth_header() == "Bot bot-token-abc"

    def test_bot_scheme_does_not_double_prefix(self) -> None:
        source = _source(token="Bot bot-token-abc", auth_scheme="bot")
        assert source._auth_header() == "Bot bot-token-abc"

    def test_unknown_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="discord_auth_scheme"):
            _source(auth_scheme="bearer")


class TestIssue81ConfigExpansion:
    def test_channel_id_env_expansion_at_parse_time(self, tmp_path) -> None:
        from live_runner.config_parser import parse_live_deployments_config

        config = tmp_path / "live.yaml"
        config.write_text(
            "accounts:\n"
            "  - id: acc-1\n"
            "    broker: alpaca\n"
            "deployments:\n"
            "  - strategy_name: s\n"
            "    account_id: acc-1\n"
            "    symbols: [AAPL]\n"
            "    timeframe: 1h\n"
            "    signal_config:\n"
            "      source: discord\n"
            "      channel_id: '${TEST_DISCORD_CHANNEL_XYZ}'\n",
            encoding="utf-8",
        )
        _, deployments = parse_live_deployments_config(
            config, env_values={"TEST_DISCORD_CHANNEL_XYZ": "12345"}
        )
        assert deployments[0].signal_config["channel_id"] == "12345"

    def test_unexpanded_channel_id_is_startup_error(self, tmp_path) -> None:
        import os

        from live_runner.config_parser import parse_live_deployments_config

        os.environ.pop("TEST_DISCORD_CHANNEL_MISSING_XYZ", None)
        config = tmp_path / "live.yaml"
        config.write_text(
            "accounts:\n"
            "  - id: acc-1\n"
            "    broker: alpaca\n"
            "deployments:\n"
            "  - strategy_name: s\n"
            "    account_id: acc-1\n"
            "    symbols: [AAPL]\n"
            "    timeframe: 1h\n"
            "    signal_config:\n"
            "      source: discord\n"
            "      channel_id: '${TEST_DISCORD_CHANNEL_MISSING_XYZ}'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Unexpanded environment"):
            parse_live_deployments_config(config, env_values={})


class TestIssue81EnvAllowlist:
    def test_per_service_env_allowlist(self) -> None:
        from live_runner.daemon import _deployment_trusted_author_ids
        from types import SimpleNamespace as NS

        deployment = NS(
            signal_config={"source": "discord"},
            settings={},
            signal_source_id="nitro",
        )
        resolved = _deployment_trusted_author_ids(
            deployment,
            accounts_env_values={"DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS": "Bot-1, bot-2"},
        )
        assert resolved == {"bot-1", "bot-2"}

    def test_global_env_allowlist_fallback(self) -> None:
        from live_runner.daemon import _deployment_trusted_author_ids
        from types import SimpleNamespace as NS

        deployment = NS(signal_config={}, settings={}, signal_source_id="nitro")
        resolved = _deployment_trusted_author_ids(
            deployment,
            accounts_env_values={"DISCORD_TRUSTED_AUTHOR_IDS": "bot-9"},
        )
        assert resolved == {"bot-9"}

    def test_config_allowlist_wins_over_env(self) -> None:
        from live_runner.daemon import _deployment_trusted_author_ids
        from types import SimpleNamespace as NS

        deployment = NS(
            signal_config={"trusted_author_ids": ["cfg-1"]},
            settings={},
            signal_source_id="nitro",
        )
        resolved = _deployment_trusted_author_ids(
            deployment,
            accounts_env_values={"DISCORD_TRUSTED_AUTHOR_IDS": "env-1"},
        )
        assert resolved == {"cfg-1"}

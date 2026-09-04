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
    if signal is not None:
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
    than the newest page (_seen_ids replaced per poll). The seen-set must be
    cumulative so a burst is drained across polls without loss."""
    source = _source()
    ids = [str(100000000000000000 + index) for index in range(35)]
    newest_first = list(reversed(ids))
    pages: dict[str, int] = {"n": 0}

    async def _fetch(_channel_id):
        # Discord returns newest-first, at most POLL_LIMIT per page.
        page = pages["n"]
        pages["n"] += 1
        start = max(0, len(ids) - 20 * (page + 1))
        end = len(ids) - 20 * page
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
    estimated dropped count."""
    import asyncio

    source = _source()
    source._last_seen_message_ids["channel-1"] = "100000000000000100"

    async def _fetch(_channel_id):
        # Newest message jumps far beyond the last seen id: 99 messages missed.

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
    assert int(data.get("estimated_dropped", 0)) >= 1


def test_no_gap_warning_on_contiguous_page() -> None:
    import asyncio


    source = _source()
    source._last_seen_message_ids["channel-1"] = "100000000000000099"

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


def test_seen_set_is_pruned_below_cursor_after_poll() -> None:
    """Pre-mortem mitigation: the cumulative seen-set must not grow without
    bound. Ids at or below the monotonic per-channel cursor are redundant (a
    repeat poll can never re-emit them), so they are dropped after each poll
    while everything above the cursor is retained for burst draining."""
    import asyncio

    source = _source()
    source._seen_ids.setdefault("channel-1", set()).update(
        {"100000000000000010", "100000000000000011", "100000000000000050"}
    )
    source._last_seen_message_ids["channel-1"] = "100000000000000050"

    async def _fetch(_channel_id):
        return [
            {
                "id": "100000000000000060",
                "channel_id": "channel-1",
                "timestamp": "2026-09-01T12:00:00Z",
                "content": "RAM JET BUY AAPL @ 100.00",
                "author": {"id": "bot-1", "bot": True},
            }
        ]

    source._fetch_messages = _fetch
    asyncio.run(source._poll_once("channel-1"))

    assert source._seen_ids["channel-1"] == {
        "100000000000000060",
    }


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

"""Cleanup 15-C: Discord ingestion gates (trusted author, price parsing, bursts).

Sub-issue #23 findings C-3 and B-11. The trusted-author gate is decided by the
campaign Decision Record: ingestion trusts exactly one explicitly configured bot
ID per source; that check is the source gate.
"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import live_runner.daemon as daemon
from live_runner.signal_sources import discord_source as ds
from live_runner.signal_sources.discord_source import (
    DiscordSignalSource,
    _parse_trade_signal_from_message,
)


@pytest.fixture(autouse=True)
def _stub_submission_claims(monkeypatch):
    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(daemon, "_deployment_account_is_demo", lambda _account_id: True)


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
    from types import SimpleNamespace as _NS

    import live_runner.daemon as daemon_module

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
        async def submit_order(self, signal, *, qty: float | None = None):
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
        metadata={
            "discord_channel_id": "channel-1",
            # Fail-closed freshness (B2): real ingestion always stamps
            # received_at; fixtures must carry a fresh stamp to dispatch.
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
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
        async def submit_order(self, signal, *, qty: float | None = None):
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
        metadata={
            "discord_channel_id": "channel-1",
            "discord_author_id": "bot-1",
            # Fail-closed freshness (B2): real ingestion always stamps
            # received_at; fixtures must carry a fresh stamp to dispatch.
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
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
        async def submit_order(self, signal, *, qty: float | None = None):
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
        metadata={
            "discord_channel_id": "channel-1",
            # Fail-closed freshness (B2): real ingestion always stamps
            # received_at; fixtures must carry a fresh stamp to dispatch.
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
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
    # The resolver also reports which key the token came from, so the auth
    # scheme is read from the same scope. Both halves are asserted here.
    token, source_key = daemon._resolve_account_discord_token(
        {"DISCORD_BOT_RAMJET_TOKEN": "ramjet-token"},
        deployment,
    )
    assert token == "ramjet-token"
    assert source_key == "DISCORD_BOT_RAMJET_TOKEN"


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
        from types import SimpleNamespace as NS

        from live_runner.daemon import _deployment_trusted_author_ids

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
        from types import SimpleNamespace as NS

        from live_runner.daemon import _deployment_trusted_author_ids

        deployment = NS(signal_config={}, settings={}, signal_source_id="nitro")
        resolved = _deployment_trusted_author_ids(
            deployment,
            accounts_env_values={"DISCORD_TRUSTED_AUTHOR_IDS": "bot-9"},
        )
        assert resolved == {"bot-9"}

    def test_config_allowlist_wins_over_env(self) -> None:
        from types import SimpleNamespace as NS

        from live_runner.daemon import _deployment_trusted_author_ids

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


# ── B2: backfill, poison ids, 403, prime-all, connect status ──────────────────

def _burst_message(i: int) -> dict:
    return {
        "id": str(1000 + i),
        "channel_id": "channel-1",
        "timestamp": "2026-09-01T12:00:00Z",
        "content": "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
        "author": {"id": "bot-1", "bot": True},
    }


class TestIssueB2Backfill:
    def test_burst_beyond_poll_limit_backfills(self) -> None:
        """Bursts > POLL_LIMIT paginate backwards instead of losing exits."""
        import asyncio

        source = _source(trusted_author_ids={"bot-1"})
        newest = [_burst_message(i) for i in range(20, 0, -1)]  # ids 1020..1001
        older = [_burst_message(i) for i in range(0, -5, -1)]  # ids 1000..996
        calls: list[dict] = []

        async def _fetch(_channel_id, **kwargs):
            calls.append(kwargs)
            if not kwargs.get("before"):
                return newest
            assert kwargs["before"] == "1001"
            return older

        source._fetch_messages = _fetch
        source._last_seen_message_ids["channel-1"] = "990"
        asyncio.run(source._poll_once("channel-1"))

        assert any(call.get("before") == "1001" for call in calls)
        assert source._queue.qsize() == 25

    def test_short_page_does_not_backfill(self) -> None:
        """A short page proves completeness — no extra REST traffic."""
        import asyncio

        source = _source(trusted_author_ids={"bot-1"})
        calls: list[dict] = []

        async def _fetch(_channel_id, **kwargs):
            calls.append(kwargs)
            return [_burst_message(5), _burst_message(4)]

        source._fetch_messages = _fetch
        source._last_seen_message_ids["channel-1"] = "1000"
        asyncio.run(source._poll_once("channel-1"))
        assert calls == [{}]

    def test_poison_message_id_does_not_crash_cursor(self) -> None:
        """A non-numeric newest id must not raise out of the cursor update."""
        import asyncio

        source = _source(trusted_author_ids={"bot-1"})

        async def _fetch(_channel_id, **kwargs):
            return [{**_burst_message(1), "id": "not-a-snowflake"}]

        source._fetch_messages = _fetch
        source._last_seen_message_ids["channel-1"] = "1000"
        asyncio.run(source._poll_once("channel-1"))  # must not raise
        # Cursor keeps the last orderable value; the poison message still
        # flowed through the seen-set exactly once.
        assert source._last_seen_message_ids["channel-1"] == "1000"

    def test_channel_forbidden_skips_channel_not_source(self) -> None:
        """403 skips one channel; other channels still poll."""
        import asyncio

        from live_runner.signal_sources.discord_source import (
            DiscordChannelForbiddenError,
        )

        source = _source(trusted_author_ids={"bot-1"})
        source.channel_ids = {"bad-channel", "channel-1"}

        async def _fetch(channel_id, **kwargs):
            if channel_id == "bad-channel":
                raise DiscordChannelForbiddenError(channel_id)
            return [_burst_message(1)]

        source._fetch_messages = _fetch

        async def _run():
            for channel_id in sorted(source.channel_ids):
                try:
                    await source._poll_once(channel_id)
                except DiscordChannelForbiddenError:
                    continue

        asyncio.run(_run())
        assert source._queue.qsize() == 1


def test_dispatch_discards_stale_signals() -> None:
    """B2: a signal older than one deployment-timeframe candle never trades."""
    import asyncio
    from datetime import timedelta

    import live_runner.daemon as daemon
    from risk import TradeSignal

    submitted: list[str] = []

    class _Adapter:
        async def submit_order(self, signal, *, qty: float | None = None):
            submitted.append(str(signal.deployment_id))
            return "order-1"

        async def get_balance(self):
            return 100_000.0, 100_000.0

        async def get_positions(self):
            return []

    deployment = _deployment_for_test("dep-1", symbols=["AAPL"], channel_id="channel-1")
    deployment.signal_config["trusted_author_ids"] = ["bot-1"]
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={
            "discord_channel_id": "channel-1",
            "discord_author_id": "bot-1",
            "received_at": stale_at,
        },
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

    assert submitted == []


# ─── Credential type: bot tokens and account tokens are not interchangeable ──


def test_bot_token_is_sent_with_the_bot_prefix():
    from live_runner.signal_sources.discord_source import DiscordSignalSource

    # Merge resolution: the scheme is now configured explicitly. With no
    # configuration the resolved default is "raw" (a user/account token, sent
    # verbatim), so relying on the default here would no longer exercise the
    # "Bot " prefix at all. The unconfigured default is covered by
    # `TestIssue81AuthScheme::test_default_scheme_is_raw`.
    source = DiscordSignalSource(
        bot_name="NITRO",
        token="abc123",
        channel_id="1",
        trusted_author_ids=["1"],
        auth_scheme="bot",
    )
    assert source._authorization_header() == "Bot abc123"


def test_account_token_is_sent_raw():
    """An account token must NOT carry the "Bot " prefix or Discord answers 401."""
    from live_runner.signal_sources.discord_source import DiscordSignalSource

    source = DiscordSignalSource(
        bot_name="NITRO",
        token="abc123",
        channel_id="1",
        trusted_author_ids=["1"],
        auth_scheme="account",
    )
    assert source._authorization_header() == "abc123"


def test_preset_scheme_in_token_is_not_double_prefixed():
    from live_runner.signal_sources.discord_source import DiscordSignalSource

    for scheme in ("bot", "account"):
        source = DiscordSignalSource(
            bot_name="NITRO",
            token="Bot preset",
            channel_id="1",
            trusted_author_ids=["1"],
            auth_scheme=scheme,
        )
        assert source._authorization_header() == "Bot preset"


def test_unknown_auth_scheme_is_rejected():
    from live_runner.signal_sources.discord_source import DiscordSignalSource

    with pytest.raises(ValueError, match="Unsupported Discord auth_scheme"):
        DiscordSignalSource(
            bot_name="NITRO",
            token="t",
            channel_id="1",
            trusted_author_ids=["1"],
            auth_scheme="bearer",
        )


def test_daemon_resolves_auth_scheme_file_first_with_per_bot_override():
    import live_runner.daemon as daemon

    env = {"DISCORD_AUTH_SCHEME": "account"}
    assert daemon._resolve_discord_auth_scheme("RAMJET", env) == "account"
    # A per-bot entry wins over the global default.
    env_override = {**env, "DISCORD_BOT_NITRO_AUTH_SCHEME": "bot"}
    assert daemon._resolve_discord_auth_scheme("NITRO", env_override) == "bot"
    # Absent configuration falls back to the recorded operator default.
    # Merge resolution: this asserted "bot"; the resolved default is "raw"
    # (DEFAULT_DISCORD_AUTH_SCHEME). "raw" and "account" name the same
    # user/account credential, so the accepted vocabulary is {raw, bot, account}
    # and the unconfigured case is no longer a bot credential.
    assert daemon._resolve_discord_auth_scheme("NITRO", {}) == "raw"


def test_daemon_resolves_per_bot_auth_scheme_from_process_environment(monkeypatch):
    """An explicitly exported per-bot scheme must not be ignored.

    The daemon passes a resolved scheme into the source, which bypasses the
    source's own environment lookup. Every other credential here is file-first
    and then process-environment, so a scheme exported only in the environment
    has to win over the default rather than being silently downgraded to 'bot'.
    """
    import live_runner.daemon as daemon

    monkeypatch.setenv("DISCORD_BOT_NITRO_AUTH_SCHEME", "account")
    monkeypatch.delenv("DISCORD_AUTH_SCHEME", raising=False)
    assert daemon._resolve_discord_auth_scheme("NITRO", {}) == "account"


def test_daemon_auth_scheme_key_canonicalisation_matches_token_key(monkeypatch):
    """A service name with punctuation must use the same env key as its token.

    ``_resolve_account_discord_token`` canonicalises the service name before
    building ``DISCORD_BOT_<NAME>_...``. The scheme resolver has to use the same
    key, otherwise a service such as "Rocket Fuel" reads a key nobody can set in
    an env file and the scheme silently falls back to the default.
    """
    import live_runner.daemon as daemon

    monkeypatch.delenv("DISCORD_AUTH_SCHEME", raising=False)
    env = {"DISCORD_BOT_ROCKET_FUEL_AUTH_SCHEME": "account"}
    assert daemon._resolve_discord_auth_scheme("Rocket Fuel", env) == "account"
    assert daemon._resolve_discord_auth_scheme("  rocket-fuel  ", env) == "account"


def test_process_environment_cannot_override_the_accounts_file(monkeypatch):
    """File-first precedence must hold for the scheme, like every credential."""
    import live_runner.daemon as daemon

    monkeypatch.setenv("DISCORD_BOT_NITRO_AUTH_SCHEME", "account")
    env = {"DISCORD_BOT_NITRO_AUTH_SCHEME": "bot"}
    assert daemon._resolve_discord_auth_scheme("NITRO", env) == "bot"


# ─── an unknown auth scheme is a config error, not a guess ───────────────────
#
# `raw`/`account` name a user credential, `bot` a bot credential; they are not
# interchangeable, so an unrecognised value (a typo such as `bearer`) must be
# refused at config load. Guessing it instead means sending the wrong
# Authorization header and 401ing at send time, once per poll.


def _no_auth_scheme_in_environment(monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_AUTH_SCHEME", raising=False)
    monkeypatch.delenv("DISCORD_BOT_NITRO_AUTH_SCHEME", raising=False)


def test_daemon_rejects_an_unknown_global_auth_scheme(monkeypatch):
    _no_auth_scheme_in_environment(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported Discord auth_scheme"):
        daemon._resolve_discord_auth_scheme(
            "NITRO", {"DISCORD_AUTH_SCHEME": "bearer"}
        )


def test_daemon_rejects_an_unknown_per_bot_auth_scheme(monkeypatch):
    _no_auth_scheme_in_environment(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported Discord auth_scheme"):
        daemon._resolve_discord_auth_scheme(
            "NITRO", {"DISCORD_BOT_NITRO_AUTH_SCHEME": "bearer"}
        )


@pytest.mark.parametrize("configured", ["  Bearer  ", "\tbearer\n", "BEARER  "])
def test_daemon_rejects_an_unknown_auth_scheme_hidden_by_whitespace(
    monkeypatch, configured
):
    """Surrounding whitespace must not smuggle a typo past the vocabulary.

    The value is trimmed and case-folded before it is checked, so a padded
    unknown value is still an unknown value; without that, a stray space is a
    second way to end up with a `Bot` header on an account token.
    """
    _no_auth_scheme_in_environment(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported Discord auth_scheme"):
        daemon._resolve_discord_auth_scheme(
            "NITRO", {"DISCORD_BOT_NITRO_AUTH_SCHEME": configured}
        )


@pytest.mark.parametrize("configured", ["user", "account_token", "bearer"])
def test_daemon_rejects_any_scheme_outside_the_recorded_vocabulary(
    monkeypatch, configured
):
    """Only raw/account/bot are accepted; nothing else is silently honoured."""
    _no_auth_scheme_in_environment(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported Discord auth_scheme"):
        daemon._resolve_discord_auth_scheme(
            "NITRO", {"DISCORD_AUTH_SCHEME": configured}
        )


def test_daemon_treats_a_whitespace_only_auth_scheme_as_unset(monkeypatch):
    """A blank value means "not configured", so it falls through to the default.

    It is not an unknown vocabulary word: an env file line an editor left
    blank is a missing credential, not a typo, and must not block startup.
    """
    _no_auth_scheme_in_environment(monkeypatch)

    assert (
        daemon._resolve_discord_auth_scheme("NITRO", {"DISCORD_AUTH_SCHEME": "   "})
        == "raw"
    )
    assert (
        daemon._resolve_discord_auth_scheme(
            "NITRO", {"DISCORD_BOT_NITRO_AUTH_SCHEME": "\t \n"}
        )
        == "raw"
    )


# ─── Real message shapes from the live channels ──────────────────────────────
#
# Captured verbatim (structure only) from the three Discord signal channels so
# the parser is pinned to reality rather than to invented fixtures.

NITRO_OVERSOLD_MESSAGE = (
    "\U0001f4a5 **NITRO WATCHLIST — BUY SIGNAL**\n\n"
    "**BE** is down significantly\n\n"
    "Price:      $256.30\n"
    "RSI:        29.8\n\n"
    "**Triggered by:** \U0001f53b RSI Cross-Under\n"
    "• RSI crossed below 30 (now 29.8)\n"
)

NITRO_SUMMARY_MESSAGE = (
    "\U0001f4a5 **Nitro Daily Summary** — 2026-09-25\n\n"
    "Today:\n• Alerts: 0 | Closed: 0\n\n"
    "All Time (since 2026-02-25):\n• Alerts: 853 | Closed: 249 | Open: 19\n"
)

ROCKETFUEL_OVERBOUGHT_MESSAGE = (
    "⚡ **OVERBOUGHT ALERT**\n\n"
    "**MXL** is overbought\n\n"
    "Price: $93.80\n"
    "RSI: 71.4\n\n"
    "RSI has crossed above 70\n"
)


def _live_channel_message(content: str, *, author_id: str = "1484753770645553192"):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        id="900000000000000001",
        content=content,
        channel=SimpleNamespace(id="1484749160824574124"),
        author=SimpleNamespace(id=author_id, bot=True),
        created_at=datetime.now(timezone.utc),
    )


def test_nitro_oversold_alert_parses_into_a_buy_signal():
    """Nitro formats its ticker as "**BE**" with no space after the asterisks,
    and never uses the word "oversold"; both details used to drop the signal."""
    from live_runner.signal_sources.discord_source import (
        _parse_trade_signal_from_message,
    )

    signal = _parse_trade_signal_from_message(
        _live_channel_message(NITRO_OVERSOLD_MESSAGE), default_timeframe="1h"
    )
    assert signal is not None, "a real Nitro alert must produce a signal"
    assert signal.action == "buy"
    assert signal.symbol == "BE"
    assert signal.entry_price == pytest.approx(256.30)
    assert signal.metadata["discord_signal_type"] == "oversold_buy"


def test_rocketfuel_overbought_alert_is_deliberately_not_traded():
    """Overbought is a warning, not an entry. It must never open a short."""
    from live_runner.signal_sources.discord_source import (
        _parse_trade_signal_from_message,
    )

    assert (
        _parse_trade_signal_from_message(
            _live_channel_message(ROCKETFUEL_OVERBOUGHT_MESSAGE, author_id="1469353101352767575"),
            default_timeframe="1h",
        )
        is None
    )


def test_daily_summary_message_is_not_mistaken_for_a_signal():
    """Summary posts mention tickers-free aggregates and must stay inert."""
    from live_runner.signal_sources.discord_source import (
        _parse_trade_signal_from_message,
    )

    assert (
        _parse_trade_signal_from_message(
            _live_channel_message(NITRO_SUMMARY_MESSAGE), default_timeframe="1h"
        )
        is None
    )


def test_bold_header_is_not_mistaken_for_a_ticker():
    """A multi-word bold header must not be captured as a symbol."""
    from live_runner.signal_sources.discord_source import _extract_bold_symbol

    assert _extract_bold_symbol("**NITRO WATCHLIST — BUY SIGNAL**") is None
    assert _extract_bold_symbol("** $TICKER**") == "TICKER"
    assert _extract_bold_symbol("**TICKER**") == "TICKER"


def test_untrusted_author_is_rejected_even_for_a_valid_nitro_alert():
    from live_runner.signal_sources.discord_source import (
        _parse_trade_signal_from_message,
    )

    assert (
        _parse_trade_signal_from_message(
            _live_channel_message(NITRO_OVERSOLD_MESSAGE, author_id="1"),
            default_timeframe="1h",
            trusted_author_ids=["1484753770645553192"],
        )
        is None
    )


# ── The pre-wait age gate must fail closed on a signal with no producer stamp ──
#
# `daemon._validate_signal_age_before_wait` decided whether a missing
# `received_at` was fatal by comparing `metadata["source"]` to the literal
# string "discord". It decided that by inspecting metadata, and the producer
# overwrites that key: `_emit_polled_message` replaces the parser's
# `metadata["source"] = "discord"` with `f"discord_{bot_name.lower()}"`. The
# value the gate compared against therefore never arrived, the fail-closed
# branch was dead, and a signal with no producer timestamp walked through the
# gate and on to `submit_order`.
#
# These tests build the signal from a real poll so `metadata["source"]` is
# whatever production actually writes. `signal_is_fresh` catches the common
# case upstream, so the gate is the only defence for the timeframes that gate
# cannot parse at all -- which is why those are exercised explicitly.


def _polled_signal(*, timestamp: str = "2026-09-01T12:00:00Z", **source_kwargs):
    """A TradeSignal that came out of a real DiscordSignalSource poll."""
    import asyncio

    source = _source(**source_kwargs)

    async def _fetch(_channel_id):
        return [
            {
                "id": "m-stamped",
                "channel_id": "channel-1",
                "timestamp": timestamp,
                "content": "RAM JET BUY AAPL @ 100.05",
                "author": {"id": "bot-1", "bot": True},
            }
        ]

    source._fetch_messages = _fetch
    asyncio.run(source._poll_once("channel-1"))
    assert source._queue.qsize() == 1, "the poll must have emitted one signal"
    return source._queue.get_nowait()


def test_polled_discord_source_value_is_not_the_literal_the_age_gate_compared() -> None:
    """Pin the premise the dead branch rested on.

    If this ever stops holding, the exact-match comparison the gate used is
    doubly dead and the source of truth for "is this a Discord signal" is
    unambiguous: it is the call site, not the metadata.
    """
    signal = _polled_signal(bot_name="Nitro")

    assert signal.metadata["source"] == "discord_nitro"
    assert signal.metadata["source"] != "discord"
    # The gate must not be able to recover the source by lowercasing either.
    assert signal.metadata["source"].strip().lower() != "discord"


def test_polled_discord_signal_without_a_producer_stamp_is_refused() -> None:
    """The whole defect, end to end from a real poll.

    `signal_is_fresh` refuses a missing `received_at` for every timeframe it
    can size a budget for, so the age gate is only reachable for the rest. With
    a `received_at` of None the gate refused nothing at all.
    """
    from live_runner.daemon import _validate_signal_age_before_wait
    from live_runner.signal_sources.execution_timing import (
        max_signal_age_seconds,
        signal_is_fresh,
    )

    unparseable_timeframes = [
        timeframe
        for timeframe in ("tick", "daily", "m5", "5min", "")
        if max_signal_age_seconds(timeframe) is None
    ]
    assert unparseable_timeframes, "expected timeframes the freshness gate cannot size"

    signal = _polled_signal(bot_name="Nitro")
    signal.metadata.pop("received_at")

    for timeframe in unparseable_timeframes:
        assert signal_is_fresh(signal, timeframe) is True, (
            f"{timeframe!r} is exactly the case this gate exists for: the "
            "upstream freshness gate has no budget and so reports fresh"
        )
        with pytest.raises(ValueError, match="missing received_at"):
            _validate_signal_age_before_wait(
                signal, _gate_deployment(), timeframe=timeframe or "1h",
                require_producer_timestamp=True,
            )


def _gate_deployment():
    from uuid import uuid4

    from shared.models import LiveDeploymentConfig

    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
        signal_config={},
        settings={"max_risk_per_trade": 0.01, "max_total_exposure": 0.05},
    )


def test_unreadable_producer_timestamp_is_not_replaced_by_now() -> None:
    """`_parse_timestamp` used to return `datetime.now(timezone.utc)` for both a
    missing and an unparseable value, so `received_at` was always populated and
    an unreadable timestamp looked brand new -- defeating every age check that
    reads it. Fail closed: an age that cannot be read is unknown, not zero.
    """
    from live_runner.signal_sources.discord_source import _parse_timestamp

    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-timestamp") is None
    assert _parse_timestamp("2026-01-02T03:04:05+00:00") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )
    # A naive stamp is still read, and stamped as UTC rather than discarded.
    assert _parse_timestamp("2026-01-02T03:04:05") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )


def test_message_with_an_unreadable_timestamp_yields_no_received_at() -> None:
    """A polled message whose timestamp cannot be read is still emitted -- it is
    not silently dropped, because a dropped message leaves no operator-visible
    trace -- but it carries no fabricated `received_at`, so the daemon's gate
    refuses it with a warning naming the real reason.
    """
    signal = _polled_signal(bot_name="Nitro", timestamp="garbage")

    assert "received_at" not in signal.metadata
    assert signal.metadata["discord_received_at_unreadable"] is True
    assert signal.metadata["source"] == "discord_nitro"


class TestNitroGrammarContract:
    """Nitro is BUY-only, but the Discord parser is shared across all three bots.

    The backlog framed this as "validate Nitro grammar against a reference
    implementation and a real message export", both of which are missing. What
    is actually testable without either is the contract the code *does* encode,
    and it turns out to have a real gap:

    ``_detect_rocket_fuel_signal_type`` returns ``momentum_reversal`` for a
    momentum-reversal message, and ``_resolve_action`` maps that to **sell**. The
    shared parser is not per-bot, so a momentum-reversal message in the Nitro
    channel produces a sell signal, which Nitro's own processor then refuses.

    That refusal is safe - the daemon catches the ValueError and drops the signal
    - but it is a *silent* drop: the operator sees no trade and no explanation
    while the parser logs nothing either, because the parser never knew the
    deployment was BUY-only.

    These tests pin what the code does today, so the behaviour cannot change
    unnoticed, and they make the asymmetry explicit rather than leaving it to be
    rediscovered. The real-message validation still needs an export.
    """

    @staticmethod
    def _parse(content: str):
        from live_runner.signal_sources.discord_source import (
            _detect_rocket_fuel_signal_type,
            _resolve_action,
        )

        signal_type = _detect_rocket_fuel_signal_type(content)
        return signal_type, _resolve_action(content, signal_type=signal_type)

    def test_nitro_watchlist_buy_signal_is_recognised(self):
        """The Nitro-specific pattern in the oversold regex."""
        signal_type, action = self._parse("Nitro watchlist — buy signal EURUSD @ 1.0850")

        assert signal_type == "oversold_buy"
        assert action == "buy"

    def test_nitro_watchlist_survives_both_dash_spellings(self):
        """The regex accepts an em dash or a hyphen. Only the em-dash form was
        ever exercised, so a change to one spelling would have been invisible."""
        from live_runner.signal_sources.discord_source import _OVERSOLD_RE

        for content in (
            "Nitro watchlist — buy signal",
            "Nitro watchlist - buy signal",
        ):
            assert _OVERSOLD_RE.search(content), content

    def test_ramjet_shape_also_parses_as_buy(self):
        signal_type, action = self._parse("EURUSD is now oversold buy signal")

        assert signal_type == "oversold_buy"
        assert action == "buy"

    def test_momentum_reversal_parses_to_sell_regardless_of_bot(self):
        """The asymmetry, stated plainly.

        The parser has no per-bot awareness, so a momentum-reversal message
        yields a sell no matter which deployment consumes it. For RocketFuel that
        is the intent. For Nitro it is a signal the processor will refuse.

        Documenting it as a test means a future change to the parser - making it
        bot-aware, say - has to update this deliberately rather than silently
        altering Nitro's behaviour.
        """
        signal_type, action = self._parse("EURUSD momentum reversal, short signal")

        assert signal_type == "momentum_reversal"
        assert action == "sell"

    def test_nitro_refuses_that_sell_downstream(self):
        """End of the chain: the BUY-only gate catches what the parser produced.

        This is the reason the asymmetry above is safe rather than dangerous.
        """
        from signal_bots.registry import get_signal_processor

        processor = get_signal_processor("nitro", {})
        with pytest.raises(ValueError, match="BUY|LONG"):
            processor._normalize_buy_action("sell")

    def test_sell_bearing_words_do_not_flip_a_buy_message(self):
        """A word in prose must not reverse the direction of a real buy signal.

        This is a regression test for a live-trading bug. ``_ACTION_SELL_RE``
        used to match the bare words "sell" and "short" anywhere, and
        ``_detect_rocket_fuel_signal_type`` consults it *before* the oversold
        check. So an unambiguous RamJet buy message whose commentary mentioned
        sellers - "EURUSD is now oversold buy signal - sellers short-term weak" -
        was typed ``momentum_reversal`` and resolved to **sell**.

        RocketFuel accepts sell actions, so that was not a dropped signal: it was
        a real short position opened on a message that said buy. Nitro refused it
        as BUY-only, which is the only reason the asymmetry was ever visible.
        """
        for content in (
            "EURUSD is now oversold buy signal - sellers short-term weak",
            "EURUSD is now oversold buy signal, short covering",
            "EURUSD is now oversold buy signal; no shorts here",
        ):
            signal_type, action = self._parse(content)
            assert action == "buy", f"{content!r} was typed {signal_type!r}"

    def test_explicit_sell_phrases_are_still_sells(self):
        """Narrowing the action regex must not lose genuine sell signals."""
        for content in (
            "EURUSD sell signal",
            "EURUSD short signal",
            "EURUSD momentum reversal",
            "Signal: SELL EURUSD",
        ):
            _signal_type, action = self._parse(content)
            assert action == "sell", content

    def test_bare_directive_sell_forms_still_parse(self):
        """Guards the fix against over-tightening.

        An earlier attempt at this fix required the word "signal" next to the
        action word, which looked safer and was actually a regression: the plain
        "SELL EURUSD" form stopped resolving entirely, silently. A buy message
        mentioning sellers was fixed by dropping a real sell signal, which is not
        a trade-off worth making. These are the forms that must keep working.
        """
        for content in ("SELL EURUSD", "short EURUSD", "SELL: EURUSD"):
            _signal_type, action = self._parse(content)
            assert action == "sell", content

    def test_incidental_buy_words_do_not_open_a_position(self):
        """The same hazard as the sell side, in the direction that actually opens.

        ``_ACTION_BUY_RE`` matched the bare words "buy"/"long" and is the
        *fallback* - a message with a symbol and a price but no recognised signal
        shape resolves to buy if it contains either word. So commentary became an
        entry:

            EURUSD 1.0850 could buy here     -> buy
            EURUSD 1.0850 do not buy         -> buy

        The second is the worse one. A negated instruction parsed as a trade is
        the clearest possible failure of this parser, and it would have placed a
        real long on a message that said not to.
        """
        for content in (
            "EURUSD 1.0850 could buy here",
            "EURUSD 1.0850 I would long it",
            "EURUSD 1.0850 do not buy",
            "EURUSD 1.0850 buyers are pushing",
        ):
            _signal_type, action = self._parse(content)
            assert action is None, f"{content!r} was read as {action!r}"

    def test_real_entry_forms_still_parse(self):
        """The legitimate directive forms, carrying a price as every real one does.

        Tested through the full parser rather than the action resolver alone,
        because that is the level that matters: a message with no price is
        discarded before the action is ever consulted, so asserting an action for
        "BUY EURUSD" would have been asserting something unreachable. The price
        requirement is what the fix keys on, so it belongs in the example.
        """
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        for content in (
            "RAM JET BUY AAPL @ 100.0",
            "BUY EURUSD @ 1.0850",
            "LONG AAPL @ 100.0",
        ):
            signal = _parse_trade_signal_from_message(
                _live_channel_message(content), default_timeframe="1h"
            )
            assert signal is not None, content
            assert signal.action == "buy", content

    def test_the_action_regexes_do_not_match_incidental_words(self):
        """The predicates filter, so a widened regex alone is not a failure.

        Mutating ``_ACTION_BUY_RE`` back to bare words still passed every other
        test here, because ``_reads_as_buy_directive`` re-checks the ticker and
        the price afterwards. The predicate is the load-bearing part; the regex
        is the cheap pre-filter in front of it.

        Pinning the regexes directly matters anyway, because they are the thing a
        reader looks at to understand what counts as a signal, and because a
        future change that consults one of them directly would otherwise inherit
        the wider behaviour with nothing to catch it.
        """
        from live_runner.signal_sources.discord_source import (
            _ACTION_BUY_RE,
            _ACTION_SELL_RE,
        )

        # The buy pre-filter is a cheap "is there a buy word at all" test, so it
        # does match "could buy here" - the ticker and price checks in the
        # predicate are what reject it. Asserting the wider truth here rather
        # than the imagined narrow one, because a test that documents a fiction
        # is worse than no test.
        assert _ACTION_BUY_RE.search("BUY AAPL") is not None
        assert _ACTION_BUY_RE.search("buy signal") is not None
        assert _ACTION_BUY_RE.search("do not buy") is None

        # The sell regex is genuinely narrow, because the sell predicate needs a
        # bare-word match to tell an explicit "sell signal" from a noun phrase.
        assert _ACTION_SELL_RE.search("sellers are weak") is None
        assert _ACTION_SELL_RE.search("SELL AAPL") is not None
        assert _ACTION_SELL_RE.search("sell signal") is not None

    def test_oversold_buy_signal_still_needs_a_price(self):
        """The RamJet/Nitro phrasing is recognised as a buy by the action
        resolver, and still yields no signal without a price - the parser's
        price requirement, asserted for the phrasing that matters most."""
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        assert (
            _parse_trade_signal_from_message(
                _live_channel_message("EURUSD is now oversold buy signal"),
                default_timeframe="1h",
            )
            is None
        )

    def test_a_buy_without_a_price_is_discarded_regardless_of_action(self):
        """A directive with no price cannot trade, so the parser drops it either
        way. Asserted so the price requirement reads as intentional."""
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        assert (
            _parse_trade_signal_from_message(
                _live_channel_message("BUY EURUSD"), default_timeframe="1h"
            )
            is None
        )

    def test_prose_noun_forms_are_not_directives(self):
        for content in (
            "sellers are weak into the close",
            "short-term weakness in EURUSD",
            "short covering underway",
        ):
            _signal_type, action = self._parse(content)
            assert action is None, f"{content!r} was read as {action!r}"

    def test_alert_only_messages_stay_non_trading(self):
        for content in ("EURUSD is overbought", "death cross warning EURUSD"):
            _signal_type, action = self._parse(content)
            assert action is None, content


class TestRocketFuelSellTypingIsSafetyCritical:
    """Why the sell-parsing direction matters, and where it was already masked.

    The parser's action is not equally consequential for every RocketFuel
    variant, which is why this bug could sit unnoticed:

      longsonly       a sell becomes ``exit_only`` - it closes, it does not open.
                      A mis-typed sell was therefore harmless by accident.
      longandshort    a sell is a real short position. Here the same mis-typing
                      is a live-trading defect.

    The ``longsonly`` deployment is the one actually configured, so the exposure
    was theoretical in practice - but that is a property of the current
    configuration, not of the code. Anyone switching the variant would have
    activated it, with no test to fail.

    These tests pin the direction at the processor boundary, so the distinction
    cannot be quietly reversed.
    """

    @staticmethod
    def _parsed_action(content: str) -> str | None:
        from live_runner.signal_sources.discord_source import (
            _detect_rocket_fuel_signal_type,
            _resolve_action,
        )

        return _resolve_action(
            content, signal_type=_detect_rocket_fuel_signal_type(content)
        )

    def test_a_buy_message_with_seller_prose_never_typed_as_sell(self):
        for content in (
            "BE is down significantly — buy signal, sellers capitulating",
            "EURUSD is now oversold buy signal - sellers short-term weak",
        ):
            assert self._parsed_action(content) == "buy", content

    def test_longsonly_would_have_absorbed_a_mistyped_sell(self):
        """Documents the accident that hid this, so nobody 'simplifies' the
        longsonly sell path back into a real position."""
        from signal_bots.registry import get_signal_processor

        processor = get_signal_processor("rocketfuel_longsonly", {})
        assert processor.variant == "longsonly"
        # The processor accepts a sell; the variant is what makes it safe.
        assert processor._normalize_action("sell") == "sell"

    def test_longandshort_would_have_traded_the_mistyped_sell(self):
        from signal_bots.registry import get_signal_processor

        processor = get_signal_processor("rocketfuel_longandshort", {})
        assert processor.variant == "longandshort"
        assert processor._normalize_action("sell") == "sell"


class TestPerBotAuthorAllowlist:
    """Which Discord author may drive which bot.

    Three bots share one credentials file and each reads its own channel. The
    author allowlist did not follow that pattern: there was a per-bot token and a
    per-bot channel, but only a single ``DISCORD_TRUSTED_AUTHOR_IDS``. So all
    three bots resolved the *same* three authors, and each trusted the other
    bots' signal bots.

    With per-bot tokens and channels that is not a theoretical overlap. A bot
    account that also had posting access to another bot's channel could post
    there, and its message would clear the author gate and trade that
    deployment. The channel binding did not help: the allowlist is checked
    against the author, and the same author was trusted everywhere.
    """

    @staticmethod
    def _service(name: str, **config):
        class _Service:
            pass

        service = _Service()
        service.name = name
        service.config = dict(config)
        return service

    NITRO_AUTHOR = "1484753770645553192"
    ROCKETFUEL_AUTHOR = "1469353101352767575"
    RAMJET_AUTHOR = "1433633853091610745"

    def _resolve(self, name: str, env: dict[str, str]):
        from live_runner.daemon import _resolve_discord_service_trusted_author_ids

        return _resolve_discord_service_trusted_author_ids(
            self._service(name), accounts_env_values=env
        )

    def test_a_per_bot_var_isolates_each_bot_to_its_own_author(self):
        env = {
            "DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS": self.NITRO_AUTHOR,
            "DISCORD_BOT_ROCKETFUEL_TRUSTED_AUTHOR_IDS": self.ROCKETFUEL_AUTHOR,
            "DISCORD_BOT_RAMJET_TRUSTED_AUTHOR_IDS": self.RAMJET_AUTHOR,
        }

        assert self._resolve("nitro", env) == {self.NITRO_AUTHOR}
        assert self._resolve("rocketfuel", env) == {self.ROCKETFUEL_AUTHOR}
        assert self._resolve("ramjet", env) == {self.RAMJET_AUTHOR}

    def test_another_bots_author_is_refused_when_a_per_bot_var_exists(self):
        """The cross-bot case, stated as the property that matters."""
        env = {
            "DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS": self.NITRO_AUTHOR,
            "DISCORD_BOT_ROCKETFUEL_TRUSTED_AUTHOR_IDS": self.ROCKETFUEL_AUTHOR,
        }

        nitro = self._resolve("nitro", env)

        assert self.ROCKETFUEL_AUTHOR not in nitro
        assert self.RAMJET_AUTHOR not in nitro

    def test_the_shared_var_still_works_for_a_single_bot_setup(self):
        """Backwards compatibility: a one-bot deployment configures the original
        name, and that must not be broken by adding the per-bot one."""
        env = {"DISCORD_TRUSTED_AUTHOR_IDS": f"{self.NITRO_AUTHOR},{self.ROCKETFUEL_AUTHOR}"}

        assert self._resolve("nitro", env) == {self.NITRO_AUTHOR, self.ROCKETFUEL_AUTHOR}

    def test_a_per_bot_var_wins_over_the_shared_one(self):
        env = {
            "DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS": self.NITRO_AUTHOR,
            "DISCORD_TRUSTED_AUTHOR_IDS": self.ROCKETFUEL_AUTHOR,
        }

        assert self._resolve("nitro", env) == {self.NITRO_AUTHOR}

    def test_an_explicit_service_config_still_wins_over_both(self):
        env = {
            "DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS": self.NITRO_AUTHOR,
            "DISCORD_TRUSTED_AUTHOR_IDS": self.ROCKETFUEL_AUTHOR,
        }
        service = self._service(
            "nitro", trusted_author_ids=[self.RAMJET_AUTHOR]
        )

        from live_runner.daemon import _resolve_discord_service_trusted_author_ids

        assert _resolve_discord_service_trusted_author_ids(
            service, accounts_env_values=env
        ) == {self.RAMJET_AUTHOR}

    def test_no_allowlist_anywhere_still_fails_closed(self):
        """The property the whole gate exists for, re-asserted after the change."""
        assert self._resolve("nitro", {}) == set()

    def test_the_current_shared_only_config_is_unchanged_by_this(self):
        """The migration path, and the reason this change is safe to deploy.

        The live credentials file sets only ``DISCORD_TRUSTED_AUTHOR_IDS``, so
        until an operator adds per-bot variables all three bots resolve the same
        list exactly as before. That is deliberate: the mapping from author id to
        bot is not derivable from the repository, and guessing it wrong would
        refuse that bot's real messages and silently stop it trading. So the
        operator sets the per-bot variables, and the fallback keeps working until
        they do.
        """
        env = {
            "DISCORD_TRUSTED_AUTHOR_IDS": (
                f"{self.RAMJET_AUTHOR},{self.ROCKETFUEL_AUTHOR},{self.NITRO_AUTHOR}"
            )
        }

        for bot in ("nitro", "ramjet", "rocketfuel"):
            assert self._resolve(bot, env) == {
                self.RAMJET_AUTHOR,
                self.ROCKETFUEL_AUTHOR,
                self.NITRO_AUTHOR,
            }


class TestAuthorRejectionIsCountable:
    """A mis-configured allowlist is a silent stop, and now it is not silent.

    `DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS` is operator-set, and nothing in the
    repository says which author belongs to which bot. So the realistic failure
    when it is introduced is a *wrong* value: the bot then refuses every message
    from its real author, logs one WARNING per message, and keeps reporting
    connected while never trading.

    That is the same signature as the two other silent-stop bugs on this branch -
    the holiday-widened staleness budget and the stray `return` - where nothing
    failed and every field still looked healthy. So the rejections are counted,
    and the distinct authors recorded, which makes the pattern answerable without
    reading a log.
    """

    NITRO_AUTHOR = "1484753770645553192"
    WRONG_AUTHOR = "1469353101352767575"

    def test_rejections_are_counted_and_the_author_recorded(self):
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        rejections: list[str] = []
        allowlist = {self.WRONG_AUTHOR}  # the realistic misconfiguration

        for _ in range(3):
            _parse_trade_signal_from_message(
                _live_channel_message(NITRO_OVERSOLD_MESSAGE),
                default_timeframe="1h",
                trusted_author_ids=allowlist,
                on_author_rejection=rejections.append,
            )

        assert len(rejections) == 3
        # The *real* author is reported, which is what tells the operator which
        # value to put in the variable.
        assert set(rejections) == {self.NITRO_AUTHOR}

    def test_an_accepted_message_records_no_rejection(self):
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        rejections: list[str] = []
        signal = _parse_trade_signal_from_message(
            _live_channel_message(NITRO_OVERSOLD_MESSAGE),
            default_timeframe="1h",
            trusted_author_ids={self.NITRO_AUTHOR},
            on_author_rejection=rejections.append,
        )

        assert signal is not None
        assert rejections == []

    def test_the_observer_is_optional(self):
        """Backwards compatibility: the existing callers pass no observer."""
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        assert (
            _parse_trade_signal_from_message(
                _live_channel_message(NITRO_OVERSOLD_MESSAGE, author_id=self.WRONG_AUTHOR),
                default_timeframe="1h",
                trusted_author_ids={self.NITRO_AUTHOR},
            )
            is None
        )

    def test_the_source_exposes_a_summary_for_operators(self):
        from live_runner.signal_sources.discord_source import DiscordSignalSource

        source = DiscordSignalSource(
            bot_name="nitro",
            token="t",
            channel_id="c",
            service_id="svc",
            trusted_author_ids=[self.WRONG_AUTHOR],
        )
        # Nothing seen yet, so nothing to report. Asserted explicitly so a
        # missing summary is a failure rather than an AttributeError elsewhere.
        assert source.author_rejection_summary() == {
            "rejected_count": 0,
            "rejected_author_ids": [],
        }

    def test_a_rejected_author_never_reaches_a_deployment(self):
        """The property that matters: the gate still refuses, whatever the
        counting does."""
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        signal = _parse_trade_signal_from_message(
            _live_channel_message(NITRO_OVERSOLD_MESSAGE),
            default_timeframe="1h",
            trusted_author_ids={self.NITRO_AUTHOR},
            on_author_rejection=lambda _a: None,
        )
        assert signal is not None

        refused = _parse_trade_signal_from_message(
            _live_channel_message(
                NITRO_OVERSOLD_MESSAGE, author_id=self.WRONG_AUTHOR
            ),
            default_timeframe="1h",
            trusted_author_ids={self.NITRO_AUTHOR},
            on_author_rejection=lambda _a: None,
        )
        assert refused is None


class TestRejectionStreakFailsTheService:
    """Counting rejections is only useful if it reaches the operator.

    The counter alone would sit in memory. What an operator actually sees is the
    service record on the dashboard, so a bot that has rejected every message it
    received is marked FAILED there.

    Failing the *service* is the right lever rather than refusing to ingest: the
    refusal is already correct and unchanged, and the point is to stop the
    dashboard reporting a healthy bot that is silently not trading.
    """

    NITRO_AUTHOR = "1484753770645553192"
    WRONG_AUTHOR = "1469353101352767575"

    def test_the_threshold_is_low_enough_to_catch_a_wrong_allowlist_quickly(self):
        """The value itself is pinned, not just read from the module.

        The other tests here feed ``AUTHOR_REJECTION_STREAK_THRESHOLD`` messages
        in, so raising it to an absurd number left them all passing - they were
        feeding the mutant its own input. This asserts the real number instead,
        because the operational requirement is "caught on the first burst of real
        messages", not "caught at some threshold".

        Low enough that a burst of a few real signals trips it, high enough that
        one impersonation attempt cannot fail a healthy bot.
        """
        from live_runner.signal_sources.discord_source import (
            AUTHOR_REJECTION_STREAK_THRESHOLD,
        )

        assert AUTHOR_REJECTION_STREAK_THRESHOLD == 3

    def _source(self, allowed: str):
        from live_runner.signal_sources.discord_source import DiscordSignalSource

        source = DiscordSignalSource(
            bot_name="nitro",
            token="t",
            channel_id="c",
            service_id="svc",
            trusted_author_ids=[allowed],
        )
        self.statuses: list[str] = []

        async def _record(status, **_kwargs):
            self.statuses.append(str(status))

        source._set_service_status = _record
        return source

    @staticmethod
    def _message(author: str, message_id: str):
        return _live_channel_message(NITRO_OVERSOLD_MESSAGE, author_id=author)

    @staticmethod
    async def _feed(source, author: str, count: int) -> None:
        """Feed messages and let the scheduled status write run.

        The parser is synchronous, so the observer's coroutine is scheduled on
        the running loop rather than awaited. A test that only asserted
        immediately would see an empty status list and pass for the wrong reason,
        so the loop is given a turn here explicitly.
        """
        import asyncio as _asyncio

        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        for index in range(count):
            _parse_trade_signal_from_message(
                _live_channel_message(NITRO_OVERSOLD_MESSAGE, author_id=author),
                default_timeframe="1h",
                trusted_author_ids=source.trusted_author_ids,
                on_author_rejection=source._record_author_rejection,
            )
        await _asyncio.sleep(0)

    def test_a_wrong_allowlist_fails_the_service(self):
        from live_runner.signal_sources.discord_source import (
            AUTHOR_REJECTION_STREAK_THRESHOLD,
        )

        source = self._source(self.WRONG_AUTHOR)
        asyncio.run(self._feed(source, self.NITRO_AUTHOR, AUTHOR_REJECTION_STREAK_THRESHOLD))

        assert any("FAILED" in status for status in self.statuses), (
            f"a bot rejecting every message still looked healthy: {self.statuses}"
        )

    def test_a_correct_allowlist_is_never_failed(self):
        source = self._source(self.NITRO_AUTHOR)
        asyncio.run(self._feed(source, self.NITRO_AUTHOR, 10))

        assert self.statuses == []
        assert source.author_rejection_summary()["rejected_count"] == 0

    def test_a_single_rejection_does_not_fail_a_healthy_bot(self):
        """One impersonation attempt must not take down a working deployment."""
        from live_runner.signal_sources.discord_source import (
            _parse_trade_signal_from_message,
        )

        source = self._source(self.NITRO_AUTHOR)

        async def _interleave() -> None:
            # A rejection then an acceptance, repeatedly: the streak must reset
            # each time, so scattered impersonation attempts never accumulate
            # into a failure.
            for index in range(6):
                _parse_trade_signal_from_message(
                    self._message(self.WRONG_AUTHOR, f"bad{index}"),
                    default_timeframe="1h",
                    trusted_author_ids=source.trusted_author_ids,
                    on_author_rejection=source._record_author_rejection,
                )
                _parse_trade_signal_from_message(
                    self._message(self.NITRO_AUTHOR, f"good{index}"),
                    default_timeframe="1h",
                    trusted_author_ids=source.trusted_author_ids,
                    on_author_rejection=source._record_author_rejection,
                    on_author_accepted=source._record_author_accepted,
                )
            await asyncio.sleep(0)

        asyncio.run(_interleave())
        assert self.statuses == [], (
            f"interleaved impersonation attempts failed a healthy bot: {self.statuses}"
        )

    def test_the_summary_names_the_real_author_so_the_value_can_be_fixed(self):
        from live_runner.signal_sources.discord_source import (
            AUTHOR_REJECTION_STREAK_THRESHOLD,
        )

        source = self._source(self.WRONG_AUTHOR)
        asyncio.run(self._feed(source, self.NITRO_AUTHOR, AUTHOR_REJECTION_STREAK_THRESHOLD))

        summary = source.author_rejection_summary()
        # The *real* author is what the operator needs in the variable - not the
        # one currently configured.
        assert summary["rejected_author_ids"] == [self.NITRO_AUTHOR]
        assert summary["rejected_count"] >= AUTHOR_REJECTION_STREAK_THRESHOLD

    def test_the_alert_fires_once_not_per_message(self):
        from live_runner.signal_sources.discord_source import (
            AUTHOR_REJECTION_STREAK_THRESHOLD,
        )

        source = self._source(self.WRONG_AUTHOR)
        asyncio.run(self._feed(source, self.NITRO_AUTHOR, AUTHOR_REJECTION_STREAK_THRESHOLD * 4))

        failed = [s for s in self.statuses if "FAILED" in s]
        assert len(failed) == 1, f"repeated failure writes: {self.statuses}"

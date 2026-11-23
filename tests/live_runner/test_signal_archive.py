"""Tests for the append-only raw Discord signal archive (issue #308)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import live_runner.daemon as daemon
import live_runner.signal_archive as sa
from live_runner.signal_archive import (
    SignalArchive,
    SignalDecisionRecorder,
    market_features_from_frame,
    received_record,
    record_signal_decision,
)
from live_runner.signal_sources.discord_source import DiscordSignalSource
from risk import TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A SignalArchive writing into a tmp DATA_DIR."""
    monkeypatch.setattr(sa._constants, "DATA_DIR", tmp_path)
    arch = SignalArchive(queue_size=64)
    yield arch
    arch.close(timeout=2.0)


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """A tmp-DATA_DIR archive installed as the process singleton.

    Production call sites resolve the archive through ``get_signal_archive``;
    patching the module global keeps every record inside ``tmp_path``.
    """
    monkeypatch.setattr(sa._constants, "DATA_DIR", tmp_path)
    arch = SignalArchive(queue_size=256)
    monkeypatch.setattr(sa, "_ARCHIVE", arch)
    yield arch
    arch.close(timeout=2.0)


def _all_records() -> list[dict]:
    records: list[dict] = []
    for path in sorted(sa.archive_dir().glob("*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
    return records


def _read_records(arch: SignalArchive) -> list[dict]:
    assert arch.flush(timeout=5.0)
    records: list[dict] = []
    for path in sorted(sa.archive_dir().glob("*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
    return records


def test_record_writes_one_jsonl_line_per_record(archive: SignalArchive) -> None:
    archive.record({"type": "x", "logged_at": "2026-11-21T10:00:00+00:00", "n": 1})
    archive.record({"type": "x", "logged_at": "2026-11-21T10:00:01+00:00", "n": 2})
    records = _read_records(archive)
    assert [r["n"] for r in records] == [1, 2]
    assert records[0]["type"] == "x"


def test_records_split_across_utc_day_files(archive: SignalArchive) -> None:
    archive.record({"type": "x", "logged_at": "2026-11-20T23:59:00+00:00"})
    archive.record({"type": "x", "logged_at": "2026-11-21T00:01:00+00:00"})
    _read_records(archive)
    names = sorted(p.name for p in sa.archive_dir().glob("*.jsonl"))
    assert names == [
        "discord_signals-2026-11-20.jsonl",
        "discord_signals-2026-11-21.jsonl",
    ]


def test_full_queue_drops_counts_and_never_blocks(archive: SignalArchive) -> None:
    # Fill the queue faster than the writer drains by pausing it via a
    # tiny queue and many records; put_nowait must return immediately.
    small = SignalArchive(queue_size=2)
    try:
        for i in range(50):
            small.record({"type": "x", "i": i})
        assert small.dropped + small.written > 0
        assert small.stats()["queued"] <= 2
    finally:
        small.close(timeout=2.0)


def test_record_after_close_is_ignored(archive: SignalArchive) -> None:
    archive.record({"type": "x"})
    archive.close(timeout=2.0)
    before = archive.written
    archive.record({"type": "x", "late": True})
    assert archive._queue.empty()
    assert archive.written == before


def test_unwritable_dir_counts_errors_not_raises(monkeypatch, tmp_path) -> None:
    # Point DATA_DIR at a path that cannot be created as a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(sa._constants, "DATA_DIR", blocker / "data")
    arch = SignalArchive(queue_size=8)
    try:
        for i in range(5):
            arch.record({"type": "x", "i": i})
        arch.flush(timeout=5.0)
        assert arch.write_errors >= 1
    finally:
        arch.close(timeout=2.0)


def test_disabled_archive_records_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sa._constants, "DATA_DIR", tmp_path / "off")
    monkeypatch.setenv(sa.ARCHIVE_ENABLED_ENV, "0")
    arch = SignalArchive(enabled=sa._archive_enabled_from_env())
    try:
        assert not arch.enabled
        arch.record({"type": "x"})
        arch.flush(timeout=1.0)
        assert arch.written == 0
        assert not sa.archive_dir().exists()
    finally:
        arch.close(timeout=1.0)


# -- received_record ----------------------------------------------------------


def _raw_message(**overrides):
    message = {
        "id": "123456789",
        "content": "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
        "timestamp": "2026-11-21T14:30:00+00:00",
        "channel_id": "chan-1",
        "type": 0,
        "author": {"id": "author-1", "username": "ramjet_bot", "bot": True},
    }
    message.update(overrides)
    return message


def test_received_record_parsed_signal() -> None:
    parsed = SimpleNamespace(
        action="buy",
        symbol="AAPL",
        entry_price=190.5,
        stop_loss=185.0,
        take_profit=200.0,
        timeframe="1h",
        metadata={"discord_signal_type": "ram_jet_oversold_buy"},
    )
    record = received_record(
        bot_name="ramjet",
        account_id="acc-1",
        service_id="svc-1",
        message=_raw_message(),
        channel_id="chan-1",
        parsed=parsed,
        refusal=None,
        author_trusted=True,
    )
    assert record["type"] == "discord_signal_received"
    assert record["schema_version"] == 1
    assert record["discord_message_id"] == "123456789"
    assert record["discord_channel_id"] == "chan-1"
    assert record["discord_author_id"] == "author-1"
    assert record["author_username"] == "ramjet_bot"
    assert record["author_is_bot"] is True
    assert record["author_trusted"] is True
    assert record["content"].startswith("**$AAPL**")
    assert record["message_created_at"] == "2026-11-21T14:30:00+00:00"
    assert record["parse_status"] == "parsed"
    assert record["refusal_reason"] is None
    assert record["signal"]["symbol"] == "AAPL"
    assert record["signal"]["entry_price"] == 190.5
    assert record["signal"]["signal_type"] == "ram_jet_oversold_buy"
    assert record["features"]["hour_utc"] == 14
    assert record["features"]["has_explicit_stop_loss"] is True
    assert record["features"]["is_exit_signal"] is False
    # Must be JSON-serializable as-is.
    json.dumps(record)


def test_received_record_refused_signal() -> None:
    record = received_record(
        bot_name="nitro",
        account_id=None,
        service_id=None,
        message=_raw_message(
            content="Took profit on long AAPL",
            timestamp=None,
            channel_id="chan-9",
        ),
        channel_id="chan-9",
        parsed=None,
        refusal=("non_entry_rejection", "report of a completed trade, not an instruction"),
        author_trusted=False,
    )
    assert record["parse_status"] == "rejected"
    assert record["author_trusted"] is False
    assert record["refusal_reason"] == "non_entry_rejection"
    assert record["refusal_detail"].startswith("report")
    assert record["signal"] is None
    assert record["discord_channel_id"] == "chan-9"
    json.dumps(record)


# -- SignalDecisionRecorder ----------------------------------------------------


def _signal(**metadata_overrides):
    metadata = {
        "discord_message_id": "123456789",
        "discord_channel_id": "chan-1",
        "discord_author_id": "author-1",
        "discord_bot_name": "ramjet",
        "discord_signal_type": "ram_jet_oversold_buy",
        "received_at": "2026-11-20T14:00:00+00:00",
    }
    metadata.update(metadata_overrides)
    return SimpleNamespace(
        action="buy",
        symbol="AAPL",
        entry_price=190.5,
        stop_loss=185.0,
        take_profit=200.0,
        timeframe="1h",
        metadata=metadata,
    )


def _deployment(**overrides) -> LiveDeploymentConfig:
    """A real deployment model: the recorder resolves the processor through the model's
    ``signal_processor_name`` (#150), which a bare namespace does not carry."""
    options = {
        "id": "00000000-0000-0000-0000-0000000000d1",
        "strategy_name": "ramjet-live",
        "account_id": "acc-1",
        "symbols": ["AAPL"],
        "timeframe": "1h",
        "signal_config": {"signal_processor": "ramjet"},
        "settings": {},
    }
    options.update(overrides)
    return LiveDeploymentConfig(**options)


def test_decision_recorder_writes_one_record_with_features(archive: SignalArchive) -> None:
    recorder = SignalDecisionRecorder(
        trade_signal=_signal(),
        account_id="acc-1",
        deployment=_deployment(),
        archive=archive,
    )
    recorder.set_features({"atr_14": 1.5, "trend_regime": "above_sma50", "none_val": None})
    recorder.traded(detail="broker-123", broker_order_id="broker-123")
    # A second terminal call must not double-write.
    recorder.refused("late_refusal")

    records = _read_records(archive)
    assert len(records) == 1
    record = records[0]
    assert record["type"] == "discord_signal_decision"
    assert record["schema_version"] == 1
    assert record["outcome"] == "traded"
    assert record["reason"] is None
    assert record["broker_order_id"] == "broker-123"
    assert record["deployment_id"] == "00000000-0000-0000-0000-0000000000d1"
    assert record["strategy_name"] == "ramjet-live"
    assert record["signal_processor"] == "ramjet"
    assert record["discord_message_id"] == "123456789"
    assert record["features"]["atr_14"] == 1.5
    assert record["features"]["trend_regime"] == "above_sma50"
    assert "none_val" not in record["features"]
    assert record["signal_age_seconds"] is not None
    assert record["signal_age_seconds"] >= 0
    json.dumps(record)


def test_decision_recorder_refused(archive: SignalArchive) -> None:
    record_signal_decision(
        _signal(),
        account_id="acc-1",
        deployment=_deployment(),
        outcome="refused",
        reason="symbol_not_covered",
        archive=archive,
    )
    records = _read_records(archive)
    assert len(records) == 1
    assert records[0]["outcome"] == "refused"
    assert records[0]["reason"] == "symbol_not_covered"


def test_decision_recorder_reads_processor_from_settings(archive: SignalArchive) -> None:
    """Deployments may configure the processor via ``settings``, not only
    ``signal_config`` — the record must name the processor that actually ran."""
    record_signal_decision(
        _signal(),
        account_id="acc-1",
        deployment=_deployment(
            signal_config={}, settings={"signal_processor": "nitro"}
        ),
        outcome="refused",
        reason="symbol_not_covered",
        archive=archive,
    )
    records = _read_records(archive)
    assert records[0]["signal_processor"] == "nitro"


def test_decision_recorder_without_deployment(archive: SignalArchive) -> None:
    record_signal_decision(
        _signal(),
        account_id="acc-1",
        deployment=None,
        outcome="refused",
        reason="unmapped_channel",
        archive=archive,
    )
    records = _read_records(archive)
    assert records[0]["deployment_id"] is None
    assert records[0]["reason"] == "unmapped_channel"


def test_decision_recorder_never_raises_on_bad_signal(archive: SignalArchive) -> None:
    record_signal_decision(
        object(),
        account_id=None,
        deployment=None,
        outcome="refused",
        reason="x",
        archive=archive,
    )
    records = _read_records(archive)
    assert records[0]["outcome"] == "refused"


# -- market_features_from_frame -----------------------------------------------


def _frame(rows: int = 120, *, volume: bool = True) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(7)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, rows))
    data = {
        "open": close - 0.1,
        "high": close + 0.4,
        "low": close - 0.4,
        "close": close,
    }
    if volume:
        data["volume"] = rng.integers(1000, 5000, rows)
    return pd.DataFrame(data, index=pd.date_range("2026-11-01", periods=rows, freq="5min"))


def test_market_features_computes_expected_keys() -> None:
    features = market_features_from_frame(_frame())
    assert features["bars"] == 120
    assert features["atr_14"] > 0
    assert 0.0 <= features["atr_14_pct_rank"] <= 1.0
    assert features["sma_20"] is not None
    assert features["sma_50"] is not None
    assert "dist_sma_20_pct" in features
    assert features["trend_regime"] in {"above_sma50", "below_sma50"}
    assert features["vwap"] is not None
    assert "dist_vwap_pct" in features
    assert "gap_pct" in features
    assert features["last_bar_time"].startswith("2026-11")
    json.dumps(features)


def test_market_features_without_volume_omits_vwap() -> None:
    features = market_features_from_frame(_frame(volume=False))
    assert "vwap" not in features
    assert "dist_vwap_pct" not in features
    assert features["atr_14"] > 0


def test_market_features_empty_and_malformed() -> None:
    assert market_features_from_frame(None) == {}
    assert market_features_from_frame(pd.DataFrame()) == {}
    assert market_features_from_frame(pd.DataFrame({"open": [1.0]})) == {}
    # Non-numeric garbage must not raise.
    bad = pd.DataFrame(
        {"open": ["x"], "high": ["y"], "low": ["z"], "close": ["w"]}
    )
    assert market_features_from_frame(bad) == {}


def test_writer_exits_when_closed_and_queue_drained(tmp_path, monkeypatch) -> None:
    """close() drops its sentinel on a full queue; the writer must still exit.

    Regression: the sentinel was the only exit signal, so a full queue at
    close() left the writer thread alive forever.
    """
    monkeypatch.setattr(sa._constants, "DATA_DIR", tmp_path)
    arch = SignalArchive(queue_size=1)
    gate = threading.Event()
    real_write = arch._write_batch

    def _slow_write(batch):
        gate.wait(5.0)
        return real_write(batch)

    monkeypatch.setattr(arch, "_write_batch", _slow_write)
    try:
        arch.record({"type": "x", "logged_at": "2026-11-21T00:00:00+00:00", "n": 1})
        # Wait until the writer holds item 1 inside the gated write, then
        # fill the queue so close()'s sentinel cannot be enqueued.
        deadline = time.monotonic() + 5.0
        while not arch._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        arch._queue.put_nowait(
            {"type": "x", "logged_at": "2026-11-21T00:00:01+00:00", "n": 2}
        )
        arch.close(timeout=0.2)
        gate.set()
        assert arch._thread is not None
        arch._thread.join(5.0)
        assert not arch._thread.is_alive()
        assert arch.written == 2
    finally:
        gate.set()
        arch.close(timeout=2.0)


def test_short_write_is_an_error_not_retried(archive: SignalArchive, monkeypatch) -> None:
    calls: list[int] = []

    def _short_write(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return len(data) // 2

    monkeypatch.setattr(sa.os, "write", _short_write)
    archive.record({"type": "x", "logged_at": "2026-11-21T00:00:00+00:00"})
    assert archive.flush(timeout=5.0)
    assert len(calls) == 1
    assert archive.write_errors == 1
    assert archive.written == 0


def test_run_daemon_shutdown_flushes_signal_archive(
    archive_env: SignalArchive, monkeypatch, tmp_path
) -> None:
    close_calls: list[float] = []
    real_close = archive_env.close

    def _close(timeout: float = 5.0) -> None:
        close_calls.append(timeout)
        real_close(timeout=timeout)

    monkeypatch.setattr(archive_env, "close", _close)

    class _Scheduler:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def _sync_loop(*_args, **_kwargs) -> None:
        sa.get_signal_archive().record(
            {
                "type": "shutdown_flush_probe",
                "logged_at": "2026-11-21T00:00:00+00:00",
            }
        )

    async def _noop_loop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        daemon, "_discover_and_register_discord_services", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(daemon, "_recover_failed_services", lambda: [])
    monkeypatch.setattr(daemon, "ServiceScheduler", _Scheduler)
    monkeypatch.setattr(daemon, "LiveTrailingStopManager", lambda: object())
    monkeypatch.setattr(daemon, "sync_deployments_loop", _sync_loop)
    monkeypatch.setattr(daemon, "sync_services_loop", _noop_loop)
    monkeypatch.setattr(daemon, "_poll_balances", _noop_loop)
    monkeypatch.setattr(daemon, "run_trailing_stops_loop", _noop_loop)
    monkeypatch.setattr(daemon, "_backfill_commissions_loop", _noop_loop)

    asyncio.run(daemon.run_daemon(accounts_env=tmp_path / "accounts.env"))

    assert close_calls == [5.0]
    records = [
        record
        for record in _all_records()
        if record.get("type") == "shutdown_flush_probe"
    ]
    assert len(records) == 1


# -- discord_source integration -------------------------------------------------


def _rest_message(
    content: str,
    *,
    author_id: str = "bot-1",
    message_id: str = "m1",
    channel_id: str = "channel-1",
    timestamp: str | None = "2026-11-21T14:30:00+00:00",
    username: str = "ramjet_bot",
    is_bot: bool = True,
) -> dict:
    return {
        "id": message_id,
        "content": content,
        "timestamp": timestamp,
        "channel_id": channel_id,
        "type": 0,
        "author": {"id": author_id, "username": username, "bot": is_bot},
    }


def _poll(source: DiscordSignalSource, messages: list[dict]) -> None:
    async def _fetch(channel_id, **_kwargs):
        return list(messages)

    source._fetch_messages = _fetch
    asyncio.run(source._poll_once("channel-1"))


def _source(**kwargs) -> DiscordSignalSource:
    options = {
        "bot_name": "ramjet",
        "token": "test-token",
        "channel_ids": {"channel-1"},
        "timeframe": "1h",
        "trusted_author_ids": {"bot-1"},
        "account_id": "acc-1",
    }
    options.update(kwargs)
    return DiscordSignalSource(**options)


def test_polled_parsed_signal_is_archived(archive_env: SignalArchive) -> None:
    source = _source()
    _poll(
        source,
        [
            _rest_message(
                "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
                message_id="m-parsed",
            )
        ],
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_received"]
    assert len(records) == 1
    record = records[0]
    assert record["discord_message_id"] == "m-parsed"
    assert record["discord_author_id"] == "bot-1"
    assert record["author_username"] == "ramjet_bot"
    assert record["author_trusted"] is True
    assert record["schema_version"] == 1
    assert record["account_id"] == "acc-1"
    assert record["bot_name"] == "ramjet"
    assert record["parse_status"] == "parsed"
    assert record["signal"]["symbol"] == "AAPL"
    assert record["signal"]["action"] == "buy"
    assert "Price" not in record["signal"]
    assert "190.5" in record["content"]


def test_polled_refused_signal_is_archived_with_reason(archive_env: SignalArchive) -> None:
    source = _source()
    _poll(
        source,
        [
            _rest_message(
                "Took profit on long AAPL",
                message_id="m-refused",
            )
        ],
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_received"]
    assert len(records) == 1
    record = records[0]
    assert record["discord_message_id"] == "m-refused"
    assert record["parse_status"] == "rejected"
    assert record["refusal_reason"] == "non_entry_rejection"
    assert record["signal"] is None


def test_polled_untrusted_author_is_archived_as_rejected(archive_env: SignalArchive) -> None:
    source = _source()
    _poll(
        source,
        [
            _rest_message(
                "**$AAPL** is now oversold — buy signal\nPrice: $190.50",
                author_id="impostor",
                message_id="m-impostor",
            )
        ],
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_received"]
    assert len(records) == 1
    assert records[0]["refusal_reason"] == "untrusted_author"
    assert records[0]["parse_status"] == "rejected"
    # Kept for impostor auditing, but marked so offline consumers can filter.
    assert records[0]["author_trusted"] is False


def test_non_emitting_poll_archives_nothing(archive_env: SignalArchive) -> None:
    source = _source()
    _poll(source, [_rest_message("**$AAPL** buy signal\nPrice: $190.50", message_id="m-skip")])
    archive_env.flush(timeout=5.0)
    before = len(_all_records())

    # A FRESH message id: dedup alone must not be what keeps the count flat —
    # emit=False has to skip the archive for a message never seen before.
    async def _fetch(channel_id, **_kwargs):
        return [
            _rest_message(
                "**$AAPL** buy signal\nPrice: $190.50", message_id="m-skip-fresh"
            )
        ]

    source._fetch_messages = _fetch
    asyncio.run(source._poll_once("channel-1", emit=False))
    archive_env.flush(timeout=5.0)
    assert len(_all_records()) == before


# -- daemon decision integration -----------------------------------------------


def _live_deployment(**overrides) -> LiveDeploymentConfig:
    options = {
        "strategy_name": "ramjet-live",
        "account_id": "acc-1",
        "symbols": ["EURUSD"],
        "timeframe": "15m",
        "signal_source": "discord",
        "signal_config": {},
        "settings": {},
    }
    options.update(overrides)
    return LiveDeploymentConfig(**options)


def _trade_signal(
    symbol: str = "AAPL",
    *,
    action: str = "buy",
    metadata: dict[str, object] | None = None,
) -> TradeSignal:
    signal_metadata = {
        "discord_message_id": "m-1",
        "discord_channel_id": "channel-1",
        "discord_author_id": "bot-1",
        "discord_bot_name": "ramjet",
        "discord_signal_type": "ram_jet_oversold_buy",
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    signal_metadata.update(metadata or {})
    return TradeSignal(
        action=action,
        symbol=symbol,
        entry_price=190.5,
        stop_loss=185.0,
        take_profit=200.0,
        timeframe="1h",
        metadata=signal_metadata,
    )


def _exit_trade_signal() -> TradeSignal:
    from live_runner.exit_signals import FULL_EXIT, exit_intent_metadata

    return _trade_signal(
        action="sell",
        metadata=exit_intent_metadata(FULL_EXIT),
    )


def _dispatch_deployment(
    *,
    status: str = "ACTIVE",
    signal_config: dict[str, object] | None = None,
) -> LiveDeploymentConfig:
    config = {
        "trusted_author_ids": ["bot-1"],
    }
    config.update(signal_config or {})
    return _live_deployment(
        status=status,
        symbols=["AAPL"],
        signal_config=config,
    )


def _patch_dispatch_db(monkeypatch, deployments: list[LiveDeploymentConfig]) -> None:
    current = {str(deployment.id): deployment for deployment in deployments}
    monkeypatch.setattr(
        daemon,
        "get_live_deployment",
        lambda deployment_id: current.get(str(deployment_id)),
    )
    monkeypatch.setattr(daemon, "_live_account_broker_login", lambda _account_id: "")


def _record_submissions(monkeypatch) -> list[dict[str, object]]:
    submissions: list[dict[str, object]] = []

    async def _submit(
        account_id,
        adapter,
        deployment,
        trade_signal,
        *,
        signal_timeframe,
        wait_enabled,
    ):
        submissions.append(
            {
                "account_id": account_id,
                "adapter": adapter,
                "deployment_id": str(deployment.id),
                "signal": trade_signal,
                "signal_timeframe": signal_timeframe,
                "wait_enabled": wait_enabled,
            }
        )

    monkeypatch.setattr(daemon, "_submit_normalized_discord_signal", _submit)
    return submissions


def _decision_records() -> list[dict]:
    return [
        record
        for record in _all_records()
        if record.get("type") == "discord_signal_decision"
    ]


def test_dispatch_symbol_gate_refusal_is_archived(
    archive_env: SignalArchive, monkeypatch
) -> None:
    deployment = _live_deployment()
    monkeypatch.setattr(daemon, "_live_account_broker_login", lambda _a: "")
    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[deployment],
            trade_signal=_trade_signal(symbol="AAPL"),
            accounts_env_values={},
        )
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_decision"]
    assert len(records) == 1
    assert records[0]["outcome"] == "refused"
    assert records[0]["reason"] == "symbol_not_covered"
    assert records[0]["deployment_id"] == str(deployment.id)
    assert records[0]["discord_message_id"] == "m-1"


def test_paused_deployment_entry_records_deployment_not_active(
    archive_env: SignalArchive, monkeypatch
) -> None:
    deployment = _dispatch_deployment(status="PAUSED")
    submissions = _record_submissions(monkeypatch)
    _patch_dispatch_db(monkeypatch, [deployment])

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[deployment],
            trade_signal=_trade_signal(),
            accounts_env_values={},
        )
    )
    archive_env.flush(timeout=5.0)

    records = _decision_records()
    assert len(records) == 1
    assert records[0]["outcome"] == "refused"
    assert records[0]["reason"] == "deployment_not_active"
    assert records[0]["deployment_id"] == str(deployment.id)
    assert submissions == []


def test_paused_deployment_exit_dispatches_without_not_active_record(
    archive_env: SignalArchive, monkeypatch
) -> None:
    deployment = _dispatch_deployment(status="PAUSED")
    submissions = _record_submissions(monkeypatch)
    _patch_dispatch_db(monkeypatch, [deployment])

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[deployment],
            trade_signal=_exit_trade_signal(),
            accounts_env_values={},
        )
    )
    archive_env.flush(timeout=5.0)

    assert [submission["deployment_id"] for submission in submissions] == [
        str(deployment.id)
    ]
    assert all(
        record.get("reason") != "deployment_not_active"
        for record in _decision_records()
    )


def test_invalid_wait_config_skips_only_that_deployment(
    archive_env: SignalArchive, monkeypatch
) -> None:
    broken = _dispatch_deployment(signal_config={"wait_candle_close": "sooon"})
    valid = _dispatch_deployment(signal_config={"wait_candle_close": False})
    submissions = _record_submissions(monkeypatch)
    _patch_dispatch_db(monkeypatch, [broken, valid])

    asyncio.run(
        daemon._dispatch_discord_trade_signal(
            "acc-1",
            adapter=object(),
            deployments_for_channel=[broken, valid],
            trade_signal=_trade_signal(),
            accounts_env_values={},
        )
    )
    archive_env.flush(timeout=5.0)

    records = _decision_records()
    assert len(records) == 1
    assert records[0]["outcome"] == "refused"
    assert records[0]["reason"] == "invalid_deployment_config"
    assert records[0]["deployment_id"] == str(broken.id)
    assert [submission["deployment_id"] for submission in submissions] == [
        str(valid.id)
    ]
    assert submissions[0]["wait_enabled"] is False


def test_prepare_stale_signal_records_refusal(
    archive_env: SignalArchive,
) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=190.5,
        stop_loss=185.0,
        take_profit=200.0,
        timeframe="1h",
        metadata={
            "discord_message_id": "m-stale",
            "discord_channel_id": "channel-1",
            "received_at": stale,
        },
    )
    deployment = _live_deployment()
    decision = SignalDecisionRecorder(
        trade_signal=signal, account_id="acc-1", deployment=deployment, archive=archive_env
    )
    prepared = asyncio.run(
        daemon._prepare_discord_signal_for_submission(
            "acc-1",
            deployment,
            signal,
            signal_timeframe="1h",
            wait_enabled=False,
            decision=decision,
        )
    )
    assert prepared is None
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_decision"]
    assert len(records) == 1
    assert records[0]["outcome"] == "refused"
    assert records[0]["reason"] == "stale_signal"


def test_claim_and_submit_success_records_traded(
    archive_env: SignalArchive, monkeypatch
) -> None:
    deployment = _live_deployment()
    normalized = _trade_signal()
    live_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=190.5,
        timestamp=datetime.now(timezone.utc),
        metadata=dict(normalized.metadata),
    )
    prepared_signal = SimpleNamespace(
        symbol="AAPL",
        action="buy",
        theoretical_price=190.5,
        metadata={
            "entry_price": 190.5,
            "stop_loss": 185.0,
            "take_profit": 200.0,
        },
    )

    async def _prepare_order(_adapter, _deployment, _signal):
        return prepared_signal, 1.0

    async def _caps_passthrough(_adapter, _deployment, _signal, *, qty, risk_config):
        # The caps have their own suite (test_live_pre_submit_caps.py); this
        # test pins the archive record, so the check is stubbed to a pass —
        # the SimpleNamespace signal/adapter doubles cannot satisfy its
        # broker reads.
        return qty

    class _Adapter:
        async def submit_order(self, _signal, qty=None):
            return "ord-42"

        async def get_quote(self, symbol):
            from live_runner.base_adapter import LiveQuote

            return LiveQuote(
                bid=190.5,
                ask=190.5,
                timestamp=datetime.now(timezone.utc),
                source="test",
            )

        async def get_pip_value(self, symbol):
            from live_runner.base_adapter import LivePipValue

            return LivePipValue(
                value=1.0,
                units_per_qty=1.0,
                conversion_rate=1.0,
                quote_asset="USD",
                account_asset="USD",
                source="test",
            )

        async def get_equity(self):
            return 100_000.0

        async def get_used_margin(self):
            return 0.0

    monkeypatch.setattr(daemon, "prepare_order_with_risk", _prepare_order)
    monkeypatch.setattr(daemon, "enforce_pre_submit_caps", _caps_passthrough)
    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda *_a, **_k: True
    )

    decision = SignalDecisionRecorder(
        trade_signal=normalized,
        account_id="acc-1",
        deployment=deployment,
        archive=archive_env,
    )
    asyncio.run(
        daemon._prepare_claim_and_submit_signal(
            "acc-1",
            _Adapter(),
            deployment,
            normalized,
            live_signal,
            is_exit=False,
            decision=decision,
        )
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_decision"]
    assert len(records) == 1
    assert records[0]["outcome"] == "traded"
    assert records[0]["broker_order_id"] == "ord-42"


def test_claim_and_submit_nothing_to_exit_records_ignored(
    archive_env: SignalArchive, monkeypatch
) -> None:
    deployment = _live_deployment()
    normalized = _trade_signal()
    live_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="sell",
        theoretical_price=190.5,
        timestamp=datetime.now(timezone.utc),
        metadata=dict(normalized.metadata),
    )

    async def _prepare_order(_adapter, _deployment, _signal):
        raise daemon.NothingToExitError("nothing to close", broker_qty=0.0)

    monkeypatch.setattr(daemon, "prepare_order_with_risk", _prepare_order)

    decision = SignalDecisionRecorder(
        trade_signal=normalized,
        account_id="acc-1",
        deployment=deployment,
        archive=archive_env,
    )
    asyncio.run(
        daemon._prepare_claim_and_submit_signal(
            "acc-1",
            object(),
            deployment,
            normalized,
            live_signal,
            is_exit=True,
            decision=decision,
        )
    )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_decision"]
    assert len(records) == 1
    assert records[0]["outcome"] == "ignored"
    assert records[0]["reason"] == "nothing_to_exit"


def test_submit_unexpected_error_records_unknown(
    archive_env: SignalArchive, monkeypatch
) -> None:
    """An exception on the dispatch path must still leave a decision record."""
    deployment = _live_deployment()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(daemon, "_prepare_discord_signal_for_submission", _boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(
            daemon._submit_normalized_discord_signal(
                "acc-1",
                object(),
                deployment,
                _trade_signal(),
                signal_timeframe="1h",
                wait_enabled=False,
            )
        )
    archive_env.flush(timeout=5.0)
    records = [r for r in _all_records() if r["type"] == "discord_signal_decision"]
    assert len(records) == 1
    assert records[0]["outcome"] == "unknown"
    assert records[0]["reason"] == "unhandled_error"
    assert "kaboom" in records[0]["detail"]

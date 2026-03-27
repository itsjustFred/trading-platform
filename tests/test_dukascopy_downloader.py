from __future__ import annotations

import sys
import types
from pathlib import Path
from threading import Event

import pandas as pd


def test_native_downloader_accepts_control_events_and_reports_progress(
    monkeypatch, tmp_path: Path
):
    import data_tools.downloaders.dukascopy_native as native

    monkeypatch.setattr(native, "DATA_DIR", tmp_path)
    monkeypatch.setattr(native, "SOURCE_META_PATH", tmp_path / "source_meta.json")
    monkeypatch.setattr(native, "GENESIS_PATH", tmp_path / "dukascopy_genesis.json")
    monkeypatch.setattr(native, "_load_genesis_index", lambda: {})
    monkeypatch.setattr(native, "_load_source_meta_index", lambda: {})
    monkeypatch.setattr(native, "_write_source_meta_index", lambda payload: None)
    monkeypatch.setattr(
        native,
        "generate_minute_urls",
        lambda instrument, start_dt, end_dt, price_type="bid": [(start_dt, "https://example.com/test.bi5")],
    )

    seen: dict[str, object] = {}
    progress_updates: list[tuple[float, str]] = []

    async def fake_download_bi5_files(
        urls,
        concurrency=10,
        max_retries=3,
        *,
        batch_size=None,
        pause_ms=1000,
        report_progress=None,
        stop_event=None,
        pause_event=None,
        log_callback=None,
    ):
        seen["urls"] = urls
        seen["stop_event"] = stop_event
        seen["pause_event"] = pause_event
        seen["batch_size"] = batch_size
        seen["pause_ms"] = pause_ms
        if report_progress is not None:
            report_progress(50.0, "Halfway there")
        return [(urls[0][0], b"placeholder")]

    monkeypatch.setattr(native, "download_bi5_files", fake_download_bi5_files)
    monkeypatch.setattr(
        native,
        "_assemble_candle_dataframe",
        lambda results, decimal_factor: pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T00:01:00Z"], utc=True),
                "open": [1.1, 1.2],
                "high": [1.2, 1.3],
                "low": [1.0, 1.1],
                "close": [1.15, 1.25],
                "volume": [10.0, 12.0],
            }
        ),
    )

    downloader = native.DukascopyNativeDownloader()
    pause_event = Event()
    stop_event = Event()

    result = downloader.download(
        "EURUSD",
        "m1",
        tmp_path,
        start="2024-01-01",
        end="2024-01-01",
        pause_event=pause_event,
        stop_event=stop_event,
        report_progress=lambda pct, msg: progress_updates.append((pct, msg)),
    )

    assert result.symbol == "EURUSD"
    assert seen["pause_event"] is pause_event
    assert seen["stop_event"] is stop_event
    assert seen["batch_size"] == 10
    assert seen["pause_ms"] == 1000
    assert any(pct == 0.0 for pct, _ in progress_updates)
    assert any("Halfway there" in msg for _, msg in progress_updates)
    assert progress_updates[-1][0] == 100.0


def test_download_bi5_files_batches_and_increases_pause_on_throttle(monkeypatch):
    import data_tools.downloaders.dukascopy_native as native

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_aiohttp = types.SimpleNamespace(
        TCPConnector=FakeConnector,
        ClientTimeout=FakeTimeout,
        ClientSession=FakeClientSession,
    )
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)

    sleep_calls: list[float] = []
    log_lines: list[str] = []
    fetch_calls: list[str] = []

    async def fake_fetch_one(
        session,
        dt,
        url,
        semaphore,
        max_retries=3,
        *,
        pause_event=None,
        stop_event=None,
    ):
        fetch_calls.append(url)
        return native.FetchResult(
            dt=dt,
            data=url.encode("utf-8"),
            throttle_signal=url == "u2",
        )

    async def fake_sleep_with_control(delay_seconds, pause_event, stop_event):
        sleep_calls.append(delay_seconds)

    monkeypatch.setattr(native, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(native, "_sleep_with_control", fake_sleep_with_control)

    start = pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
    urls = [
        (start, "u1"),
        (start, "u2"),
        (start, "u3"),
    ]

    results = native.asyncio.run(
        native.download_bi5_files(
            urls,
            batch_size=2,
            pause_ms=1000,
            log_callback=log_lines.append,
        )
    )

    assert fetch_calls == ["u1", "u2", "u3"]
    assert results == [
        (start, b"u1"),
        (start, b"u2"),
        (start, b"u3"),
    ]
    assert sleep_calls == [2.0]
    assert any("increasing pause to 2000 ms" in line for line in log_lines)


def test_download_bi5_files_caps_backoff_and_resets_after_cooldown(monkeypatch):
    import data_tools.downloaders.dukascopy_native as native

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_aiohttp = types.SimpleNamespace(
        TCPConnector=FakeConnector,
        ClientTimeout=FakeTimeout,
        ClientSession=FakeClientSession,
    )
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)

    sleep_calls: list[float] = []
    log_lines: list[str] = []

    async def fake_fetch_one(
        session,
        dt,
        url,
        semaphore,
        max_retries=3,
        *,
        pause_event=None,
        stop_event=None,
    ):
        if url == "u1":
            return native.FetchResult(dt=dt, data=b"ok", throttle_signal=True)
        if url == "u2":
            return native.FetchResult(dt=dt, data=b"ok", throttle_signal=True)
        if url == "u3":
            return native.FetchResult(dt=dt, data=b"", throttle_signal=True, cooldown_required=True)
        return native.FetchResult(dt=dt, data=b"ok")

    async def fake_sleep_with_control(delay_seconds, pause_event, stop_event):
        sleep_calls.append(delay_seconds)

    monkeypatch.setattr(native, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(native, "_sleep_with_control", fake_sleep_with_control)

    start = pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
    urls = [(start, "u1"), (start, "u2"), (start, "u3"), (start, "u4")]

    results = native.asyncio.run(
        native.download_bi5_files(
            urls,
            batch_size=2,
            pause_ms=8000,
            log_callback=log_lines.append,
        )
    )

    assert results == [
        (start, b"ok"),
        (start, b"ok"),
        (start, b""),
        (start, b"ok"),
    ]
    assert sleep_calls == [10.0, 15.0]
    assert any("backoff cap reached at 10000 ms" in line for line in log_lines)
    assert any("starting 15s cooldown and resetting pause" in line for line in log_lines)


def test_jforex_classpath_bootstraps_from_jnlp_when_no_local_jars(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.jforex_dl as jforex

    monkeypatch.setattr(jforex, "DATA_DIR", tmp_path)
    monkeypatch.setenv("DUKASCOPY_JNLP_URL", "https://example.com/jforex.jnlp")
    monkeypatch.delenv("JFOREX_CLASSPATH", raising=False)
    monkeypatch.delenv("JFOREX_JARS", raising=False)
    monkeypatch.delenv("JFOREX_HOME", raising=False)

    monkeypatch.setattr(
        jforex,
        "_download_jforex_jars_from_jnlp",
        lambda jnlp_url, target_dir: [target_dir / "api.jar", target_dir / "sdk.jar"],
    )

    classpath = jforex._resolve_jforex_classpath()
    expected = [str(tmp_path / ".jforex-sdk" / "api.jar"), str(tmp_path / ".jforex-sdk" / "sdk.jar")]
    assert classpath == expected


def test_jforex_classpath_prefers_explicit_env(monkeypatch):
    import data_tools.downloaders.jforex_dl as jforex

    monkeypatch.setenv("JFOREX_CLASSPATH", "/tmp/a.jar:/tmp/b.jar")
    classpath = jforex._resolve_jforex_classpath()
    assert classpath == ["/tmp/a.jar", "/tmp/b.jar"]

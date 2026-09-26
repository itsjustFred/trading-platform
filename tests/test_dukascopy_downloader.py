from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from threading import Event

import pandas as pd
import pytest


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
        batch_size=10,
        max_retries=3,
        *,
        pipeline_batches=1,
        pause_ms=1000,
        report_progress=None,
        progress_unit="files",
        stop_event=None,
        pause_event=None,
        log_callback=None,
    ):
        seen["urls"] = urls
        seen["stop_event"] = stop_event
        seen["pause_event"] = pause_event
        seen["batch_size"] = batch_size
        seen["pipeline_batches"] = pipeline_batches
        seen["pause_ms"] = pause_ms
        seen["progress_unit"] = progress_unit
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
        pipeline_batches=3,
    )

    assert result.symbol == "EURUSD"
    assert seen["pause_event"] is pause_event
    assert seen["stop_event"] is stop_event
    assert seen["batch_size"] == 10
    assert seen["pipeline_batches"] == 3
    assert seen["pause_ms"] == 1000
    assert seen["progress_unit"] == "days"
    assert any(pct == 0.0 for pct, _ in progress_updates)
    assert any("Halfway there" in msg for _, msg in progress_updates)
    assert progress_updates[-1][0] == 100.0


def _tick_request_and_options(native, *, offer_side: str = "bid", price_type: str = "bid"):
    from datetime import datetime, timezone

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    request = native._TickRequest(
        instrument="EURUSD",
        sym="EURUSD",
        normalized_tf="tick",
        price_type=price_type,
        offer_side=offer_side,
        decimal_factor=1000,
        start_dt=start,
        end_dt=start,
        requested_start="2024-01-01",
        requested_end="2024-01-01",
    )
    options = native._TickDownloadOptions(
        batch_size=4,
        pipeline_batches=2,
        pause_ms=7,
        max_retries=6,
        stop_event=None,
        pause_event=None,
        report_progress=None,
        log_callback=None,
    )
    return request, options


def test_tick_strategy_helpers_bind_download_bi5_arguments_once(monkeypatch):
    """The single-side tick helper must bind download_bi5_files' args once.

    ``batch_size`` and ``max_retries`` are positional-or-keyword, so passing
    them positionally *and* by keyword raises TypeError at download time.
    """
    import data_tools.downloaders.dukascopy_native as native

    monkeypatch.setattr(
        native,
        "generate_tick_urls",
        lambda instrument, start_dt, end_dt, price_type="both": [],
    )

    seen: dict[str, object] = {}

    # Signature mirrors the real download_bi5_files so a duplicated argument
    # raises TypeError here exactly as it would in production.
    async def fake_download_bi5_files(
        urls,
        batch_size=10,
        max_retries=3,
        *,
        pipeline_batches=1,
        pause_ms=1000,
        report_progress=None,
        progress_unit="files",
        stop_event=None,
        pause_event=None,
        log_callback=None,
    ):
        seen["batch_size"] = batch_size
        seen["max_retries"] = max_retries
        seen["pipeline_batches"] = pipeline_batches
        seen["pause_ms"] = pause_ms
        return []

    monkeypatch.setattr(native, "download_bi5_files", fake_download_bi5_files)

    request, options = _tick_request_and_options(native)
    downloader = native.DukascopyNativeDownloader()
    df, warnings = asyncio.run(
        downloader._download_single_side_ticks(request=request, options=options)
    )

    assert df.empty
    assert warnings == ["offer_side:bid", "price_type:bid"]
    assert seen == {
        "batch_size": 4,
        "max_retries": 6,
        "pipeline_batches": 2,
        "pause_ms": 7,
    }


def test_merged_tick_strategy_helper_forwards_every_option(monkeypatch):
    """The merged tick helper must forward its options as keywords.

    ``_download_and_assemble_merged_ticks`` declares pipeline_batches,
    pause_ms and max_retries keyword-only; passing them positionally raises
    TypeError the moment a merged tick download runs (DV-C1).
    """
    import data_tools.downloaders.dukascopy_native as native

    seen: dict[str, object] = {}

    async def fake_merged(
        instrument,
        start_dt,
        end_dt,
        batch_size,
        *,
        pipeline_batches=1,
        pause_ms=1000,
        max_retries=3,
        report_progress=None,
        stop_event=None,
        pause_event=None,
        log_callback=None,
    ):
        seen["batch_size"] = batch_size
        seen["pipeline_batches"] = pipeline_batches
        seen["pause_ms"] = pause_ms
        seen["max_retries"] = max_retries
        seen["report_progress"] = report_progress
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"], utc=True),
                "bid": [1.10],
                "ask": [1.12],
                "bid_vol": [1.0],
                "ask_vol": [2.0],
            }
        )

    monkeypatch.setattr(native, "_download_and_assemble_merged_ticks", fake_merged)

    request, options = _tick_request_and_options(native, offer_side="both", price_type="both")
    downloader = native.DukascopyNativeDownloader()
    df, warnings = asyncio.run(
        downloader._download_tick_strategy(
            request=request, options=options, merge_bid_ask=True
        )
    )

    assert len(df) == 1
    assert warnings == ["offer_side:both", "price_type:bid+ask"]
    assert seen == {
        "batch_size": 4,
        "pipeline_batches": 2,
        "pause_ms": 7,
        "max_retries": 6,
        "report_progress": None,
    }


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


def test_download_bi5_files_reports_custom_progress_unit(monkeypatch):
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
        del session, semaphore, max_retries, pause_event, stop_event
        return native.FetchResult(dt=dt, data=url.encode("utf-8"))

    async def fake_sleep_with_control(delay_seconds, pause_event, stop_event):
        del delay_seconds, pause_event, stop_event
        return None

    monkeypatch.setattr(native, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(native, "_sleep_with_control", fake_sleep_with_control)

    start = pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
    urls = [(start, "u1"), (start, "u2")]
    updates: list[tuple[float, str]] = []

    native.asyncio.run(
        native.download_bi5_files(
            urls,
            batch_size=2,
            pause_ms=0,
            progress_unit="days",
            report_progress=lambda pct, msg: updates.append((pct, msg)),
        )
    )

    assert updates[-1] == (100.0, "Downloaded 2/2 days")


def test_download_bi5_files_pipelines_multiple_batches_before_pause(monkeypatch):
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

    fetch_calls: list[str] = []
    sleep_calls: list[float] = []

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
        del session, semaphore, max_retries, pause_event, stop_event
        fetch_calls.append(url)
        return native.FetchResult(dt=dt, data=url.encode("utf-8"))

    async def fake_sleep_with_control(delay_seconds, pause_event, stop_event):
        del pause_event, stop_event
        sleep_calls.append(delay_seconds)

    monkeypatch.setattr(native, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(native, "_sleep_with_control", fake_sleep_with_control)

    start = pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
    urls = [(start, f"u{i}") for i in range(1, 5)]

    results = native.asyncio.run(
        native.download_bi5_files(
            urls,
            batch_size=1,
            pipeline_batches=2,
            pause_ms=500,
        )
    )

    assert [data.decode("utf-8") for _, data in results] == ["u1", "u2", "u3", "u4"]
    assert fetch_calls == ["u1", "u2", "u3", "u4"]
    assert sleep_calls == [0.5]


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

    def fake_download_jars(jnlp_url, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        api_jar = target_dir / "api.jar"
        sdk_jar = target_dir / "sdk.jar"
        api_jar.write_text("api", encoding="utf-8")
        sdk_jar.write_text("sdk", encoding="utf-8")
        return [api_jar, sdk_jar]

    monkeypatch.setattr(
        jforex,
        "_download_jforex_jars_from_jnlp",
        fake_download_jars,
    )

    classpath = jforex._resolve_jforex_classpath()
    expected = [str(tmp_path / ".jforex-sdk" / "api.jar"), str(tmp_path / ".jforex-sdk" / "sdk.jar")]
    assert classpath == expected


def test_jforex_classpath_prefers_explicit_env(monkeypatch):
    import data_tools.downloaders.jforex_dl as jforex

    monkeypatch.setenv("JFOREX_CLASSPATH", os.pathsep.join(["/tmp/a.jar", "/tmp/b.jar"]))
    classpath = jforex._resolve_jforex_classpath()
    assert classpath == ["/tmp/a.jar", "/tmp/b.jar"]


def test_jforex_discovers_and_dedupes_resolved_jvm_paths(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.jforex_dl as jforex

    java_home = tmp_path / "jdk"
    jvm_path = java_home / "bin" / "server" / "jvm.dll"
    jvm_path.parent.mkdir(parents=True)
    jvm_path.write_text("fake-jvm", encoding="utf-8")

    monkeypatch.setenv("JAVA_HOME", str(java_home))
    monkeypatch.setenv("JFOREX_TRUSTED_JAVA_HOME", str(java_home))
    monkeypatch.setenv("JFOREX_ALLOW_CUSTOM_JVM", "1")
    monkeypatch.setenv("JFOREX_JVM_PATH", str(jvm_path))
    monkeypatch.setattr(jforex, "JPYPE_AVAILABLE", False)

    discovered = jforex._discover_jvm_paths()

    assert discovered == [str(jvm_path.resolve(strict=True))]


def test_jforex_accepts_legacy_java_home_jvm_layout(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.jforex_dl as jforex

    java_home = tmp_path / "legacy-jdk"
    jvm_path = java_home / "jre" / "bin" / "server" / "jvm.dll"
    jvm_path.parent.mkdir(parents=True)
    jvm_path.write_text("fake-jvm", encoding="utf-8")

    monkeypatch.setenv("JFOREX_TRUSTED_JAVA_HOME", str(java_home))

    assert jforex._is_trusted_custom_jvm_path(jvm_path)


def test_jforex_formats_startup_diagnostics(monkeypatch):
    import data_tools.downloaders.jforex_dl as jforex

    diagnostics = jforex._format_jvm_startup_diagnostics(
        classpath=["/tmp/sdk.jar"],
        classpath_source="explicit env",
        candidates=[("/tmp/jvm.dll", "access denied")],
        final_error=RuntimeError("boom"),
    )

    assert "Classpath source: explicit env" in diagnostics
    assert "Classpath entries (1):" in diagnostics
    assert "JVM candidates tried (1):" in diagnostics
    assert "reason: access denied" in diagnostics
    assert "Last error: boom" in diagnostics


def test_jforex_cli_validation_raises_value_error(monkeypatch):
    import data_tools.downloaders.jforex_dl as jforex

    monkeypatch.setattr(
        jforex.argparse.ArgumentParser,
        "parse_args",
        lambda self: types.SimpleNamespace(
            symbols=None,
            timeframes="1h",
            start="2024-01-01",
            end="2024-01-02",
            output=str(Path(".")),
            offer_side="bid",
            query_timeout_seconds=900,
            list=False,
        ),
    )

    with pytest.raises(ValueError, match="--symbols is required"):
        jforex.main()


def test_jforex_aligns_daily_query_range_to_bar_boundaries():
    import data_tools.downloaders.jforex_dl as jforex

    start_dt = jforex._parse_datetime("2025-01-01", is_end=False)
    end_dt = jforex._parse_datetime("2025-01-31", is_end=True)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    aligned_start, aligned_end = jforex._align_bar_query_range("1d", start_ms, end_ms)

    expected_end = int(jforex._parse_datetime("2025-01-31", is_end=False).timestamp() * 1000)
    assert aligned_start == start_ms
    assert aligned_end == expected_end


def test_jforex_aligns_hourly_query_range_to_hour_starts():
    import data_tools.downloaders.jforex_dl as jforex

    start_ms = int(pd.Timestamp("2025-01-01T00:17:15Z").timestamp() * 1000)
    end_ms = int(pd.Timestamp("2025-01-01T03:59:59.900Z").timestamp() * 1000)

    aligned_start, aligned_end = jforex._align_bar_query_range("1h", start_ms, end_ms)

    assert aligned_start == int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp() * 1000)
    assert aligned_end == int(pd.Timestamp("2025-01-01T03:00:00Z").timestamp() * 1000)


def test_jforex_normalize_timeframe_aliases():
    import data_tools.downloaders.jforex_dl as jforex

    assert jforex._normalize_timeframe("m1") == "1m"
    assert jforex._normalize_timeframe("h1") == "1h"
    assert jforex._normalize_timeframe("d1") == "1d"
    assert jforex._normalize_timeframe("1h") == "1h"


def test_jforex_daily_window_equals_one_day():
    import data_tools.downloaders.jforex_dl as jforex

    assert jforex.WINDOW_DAYS_BY_BASE_TF["1d"] == 1

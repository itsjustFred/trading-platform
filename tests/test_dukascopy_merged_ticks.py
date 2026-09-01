"""Regression tests for merged bid/ask tick downloads (Dukascopy native).

Covers cleanup findings DV-C1 (pipeline_batches passed twice -> TypeError /
arg shift) and DV-C2 (ask side downloaded with bid URLs -> ask columns
contain bid data).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pandas as pd
import pytest


def _empty_tick_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "ask": pd.Series(dtype="float64"),
            "bid": pd.Series(dtype="float64"),
            "ask_vol": pd.Series(dtype="float64"),
            "bid_vol": pd.Series(dtype="float64"),
        }
    )


def test_download_bi5_files_rejects_duplicate_batch_arguments():
    """DV-C1 seam: batch_size/pipeline_batches must be forwarded exactly once."""
    import data_tools.downloaders.dukascopy_native as native

    with pytest.raises(TypeError):
        native.download_bi5_files(  # type: ignore[call-arg]
            [("2024-01-01", "https://example.com/x.bi5")],
            10,  # positional batch_size
            3,  # positional max_retries
            batch_size=10,
        )


def test_download_bi5_files_positional_params_are_batch_size_and_max_retries():
    """Locks the positional contract of download_bi5_files for call sites."""
    import data_tools.downloaders.dukascopy_native as native

    code = native.download_bi5_files.__code__
    params = code.co_varnames[: code.co_argcount]
    assert params == (
        "urls",
        "batch_size",
        "max_retries",
    ), params
    kwonly = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    assert "pipeline_batches" in kwonly
    assert "batch_size" not in kwonly


def test_merged_ticks_download_side_specific_urls():
    """DV-C2: the ask side must request ask URLs, not the bid URL set.

    Dukascopy serves one tick file per hour containing BOTH sides, so the
    corrected pipeline downloads the shared tick file once and splits it by
    side instead of fetching the identical URL twice.
    """
    import data_tools.downloaders.dukascopy_native as native

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 2, tzinfo=timezone.utc)
    tick_urls = native.generate_tick_urls("EURUSD", start, end)

    assert tick_urls[0][1] == (
        "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/01/00h_ticks.bi5"
    )
    assert tick_urls[1][1].endswith("2024/00/01/01h_ticks.bi5")

    bid_urls = native.generate_tick_urls(
        "EURUSD", start, end, price_type="bid"
    )
    ask_urls = native.generate_tick_urls(
        "EURUSD", start, end, price_type="ask"
    )

    assert [url for _, url in ask_urls] == [url for _, url in bid_urls]
    assert [url for _, url in ask_urls] == [url for _, url in tick_urls]


def test_generate_tick_urls_rejects_unknown_price_type():
    import data_tools.downloaders.dukascopy_native as native

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        native.generate_tick_urls("EURUSD", start, end, price_type="mid")


def test_merged_ticks_uses_single_fetch_and_single_batch_args(monkeypatch):
    """End-to-end slice for the merged tick download path.

    Both sides come from the same per-hour tick file, so the fixed pipeline
    fetches each file exactly once and derives bid and ask columns from the
    parsed records. Argument plumbing must contain no duplicates (DV-C1).
    """
    import data_tools.downloaders.dukascopy_native as native

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
    tick_urls = native.generate_tick_urls("EURUSD", start, end)

    shared_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:10Z"], utc=True),
            "ask": [2.0],
            "bid": [1.0],
            "ask_vol": [20.0],
            "bid_vol": [1.0],
        }
    )

    calls: list[dict] = []
    assemble_calls: list[int] = []

    async def fake_download_bi5_files(urls, *args, **kwargs):
        calls.append({"urls": list(urls), "args": args, "kwargs": kwargs})
        return [(urls[0][0], b"tick")]

    def fake_assemble(results, decimal_factor):
        assemble_calls.append(1)
        return shared_frame.copy()

    monkeypatch.setattr(native, "download_bi5_files", fake_download_bi5_files)
    monkeypatch.setattr(native, "_assemble_tick_dataframe", fake_assemble)

    progress_updates: list[tuple[float, str]] = []
    df = asyncio.run(
        native._download_and_assemble_merged_ticks(
            "EURUSD",
            start,
            end,
            batch_size=5,
            max_retries=2,
            pipeline_batches=3,
            pause_ms=10,
            report_progress=lambda pct, msg: progress_updates.append((pct, msg)),
            stop_event=None,
            pause_event=None,
            log_callback=None,
        )
    )

    assert len(calls) == 1, "merged ticks must fetch the shared tick files once"
    assert [url for _, url in calls[0]["urls"]] == [url for _, url in tick_urls]
    assert calls[0]["args"] == (5, 2), calls[0]
    assert calls[0]["kwargs"].get("pipeline_batches") == 3
    assert "batch_size" not in calls[0]["kwargs"]
    assert len(assemble_calls) == 1

    # Split keeps each side on its own column: bid stays bid, ask stays ask.
    assert float(df.loc[0, "bid"]) == 1.0
    assert float(df.loc[0, "ask"]) == 2.0
    assert float(df.loc[0, "bid_vol"]) == 1.0
    assert float(df.loc[0, "ask_vol"]) == 20.0


def test_merged_ticks_empty_shared_fetch_returns_empty_frame_with_columns(
    monkeypatch,
):
    import data_tools.downloaders.dukascopy_native as native

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 1, tzinfo=timezone.utc)

    async def fake_download_bi5_files(urls, *args, **kwargs):
        return [(urls[0][0], b"")]

    monkeypatch.setattr(native, "download_bi5_files", fake_download_bi5_files)
    # Real _assemble_tick_dataframe returns an empty (typed) frame for no data.

    df = asyncio.run(
        native._download_and_assemble_merged_ticks(
            "EURUSD",
            start,
            end,
            batch_size=5,
            max_retries=1,
            pipeline_batches=1,
            pause_ms=0,
        )
    )

    assert df.empty
    assert list(df.columns) == ["timestamp", "ask", "bid", "ask_vol", "bid_vol"]


def test_merge_tick_sides_keeps_side_columns_when_one_side_is_empty():
    """Direct seam: no silent bid->ask substitution for missing sides (DV-C2)."""
    import data_tools.downloaders.dukascopy_native as native

    bid_only = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:10Z"], utc=True),
            "bid": [1.0],
            "bid_vol": [1.0],
        }
    )
    ask_only = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:10Z"], utc=True),
            "ask": [2.0],
            "ask_vol": [20.0],
        }
    )

    from_bid = native._merge_tick_sides(bid_only, _empty_tick_frame())
    from_ask = native._merge_tick_sides(_empty_tick_frame(), ask_only)
    both = native._merge_tick_sides(bid_only, ask_only)

    assert list(from_bid.columns) == ["timestamp", "ask", "bid", "ask_vol", "bid_vol"]
    assert pd.isna(from_bid.loc[0, "ask"])
    assert float(from_bid.loc[0, "bid"]) == 1.0

    assert list(from_ask.columns) == ["timestamp", "ask", "bid", "ask_vol", "bid_vol"]
    assert float(from_ask.loc[0, "ask"]) == 2.0
    assert pd.isna(from_ask.loc[0, "bid"])

    assert list(both.columns) == ["timestamp", "ask", "bid", "ask_vol", "bid_vol"]
    assert float(both.loc[0, "bid"]) == 1.0
    assert float(both.loc[0, "ask"]) == 2.0


def test_split_tick_sides_keeps_disjoint_values():
    import data_tools.downloaders.dukascopy_native as native

    shared = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:10Z"], utc=True),
            "ask": [2.0],
            "bid": [1.0],
            "ask_vol": [20.0],
            "bid_vol": [1.0],
        }
    )
    bid, ask = native._split_tick_sides(shared)

    assert list(bid.columns) == ["timestamp", "bid", "bid_vol"]
    assert list(ask.columns) == ["timestamp", "ask", "ask_vol"]
    assert float(bid.loc[0, "bid"]) == 1.0
    assert float(ask.loc[0, "ask"]) == 2.0


def test_download_async_merged_tick_persists_bid_and_ask_columns(
    monkeypatch, tmp_path: Path
):
    """Full-path regression: merged tick download crashes (DV-C1) and must persist both sides."""
    import data_tools.downloaders.dukascopy_native as native

    monkeypatch.setattr(native, "DATA_DIR", tmp_path)
    monkeypatch.setattr(native, "SOURCE_META_PATH", tmp_path / "source_meta.json")
    monkeypatch.setattr(native, "GENESIS_PATH", tmp_path / "dukascopy_genesis.json")
    monkeypatch.setattr(native, "_load_genesis_index", lambda: {})
    monkeypatch.setattr(native, "_load_source_meta_index", lambda: {})
    monkeypatch.setattr(native, "_write_source_meta_index", lambda payload: None)

    shared_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-01T00:00:10Z"], utc=True),
            "ask": [2.0],
            "bid": [1.0],
            "ask_vol": [20.0],
            "bid_vol": [1.0],
        }
    )
    recorded: list[dict] = []
    assemble_calls: list[int] = []

    async def fake_download_bi5_files(urls, *args, **kwargs):
        recorded.append({"urls": list(urls), "args": args, "kwargs": kwargs})
        return [(urls[0][0], b"tick")]

    def fake_assemble(results, decimal_factor):
        assemble_calls.append(1)
        return shared_frame.copy()

    monkeypatch.setattr(native, "download_bi5_files", fake_download_bi5_files)
    monkeypatch.setattr(native, "_assemble_tick_dataframe", fake_assemble)

    downloader = native.DukascopyNativeDownloader()
    result = asyncio.run(
        downloader._download_async(
            "EURUSD",
            "tick",
            tmp_path,
            start="2024-01-01",
            end="2024-01-01",
            offer_side="both",
            pipeline_batches=3,
            batch_size=5,
            max_retries=2,
            pause_ms=10,
        )
    )

    assert len(recorded) == 1, "shared tick files fetched once, split into sides"
    assert recorded[0]["args"] == (5, 2)
    assert recorded[0]["kwargs"].get("pipeline_batches") == 3
    assert "batch_size" not in recorded[0]["kwargs"]
    assert len(assemble_calls) == 1

    stored = pd.read_parquet(result.path)
    assert {"bid", "ask", "bid_vol", "ask_vol"}.issubset(stored.columns)
    assert float(stored.loc[0, "bid"]) == 1.0
    assert float(stored.loc[0, "ask"]) == 2.0


def test_dukascopy_event_smoke():
    """Cheap smoke guard: control-event plumbing types stay importable."""
    import data_tools.downloaders.dukascopy_native as native

    assert Event() is not None
    assert native.DukascopyNativeDownloader().source_name == "dukascopy"

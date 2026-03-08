from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd

from data_tools.downloaders.dukascopy_dl import DukascopyDownloader


def _fake_dukascopy_run_factory(calls: list[list[str]]):
    def fake_run(cmd: list[str], capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output_dir = Path(cmd[cmd.index("-dir") + 1])
        price_type = cmd[cmd.index("-p") + 1]
        output_dir.mkdir(parents=True, exist_ok=True)

        if price_type == "bid":
            frame = pd.DataFrame({
                "timestamp": ["2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z"],
                "open": [1.1000, 1.1001],
                "high": [1.1002, 1.1003],
                "low": [1.0998, 1.0999],
                "close": [1.1001, 1.1002],
                "volume": [100, 110],
            })
        else:
            frame = pd.DataFrame({
                "timestamp": ["2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z"],
                "open": [1.1004, 1.1005],
                "high": [1.1006, 1.1007],
                "low": [1.1002, 1.1003],
                "close": [1.1005, 1.1006],
                "volume": [120, 130],
            })

        frame.to_csv(output_dir / f"{price_type}.csv", index=False)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def test_download_uses_genesis_when_start_is_missing(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.dukascopy_dl as dukascopy_dl

    calls: list[list[str]] = []
    monkeypatch.setattr(dukascopy_dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dukascopy_dl, "SOURCE_META_PATH", tmp_path / "source_meta.json")
    monkeypatch.setattr(dukascopy_dl, "GENESIS_PATH", tmp_path / "dukascopy_genesis.json")
    monkeypatch.setattr(dukascopy_dl.subprocess, "run", _fake_dukascopy_run_factory(calls))

    (tmp_path / "dukascopy_genesis.json").write_text(json.dumps({
        "EURUSD": {"h4": "2003-05-04"},
    }))

    downloader = DukascopyDownloader()
    result = downloader.download("EURUSD", "h4", tmp_path, end="2024-01-01")

    assert result.timeframe == "4h"
    assert any(cmd[cmd.index("-from") + 1] == "2003-05-04" for cmd in calls)

    source_meta = json.loads((tmp_path / "source_meta.json").read_text())
    assert source_meta["dukascopy/forex/EURUSD/4h"]["requested_start"] == "2003-05-04"


def test_download_can_merge_bid_and_ask_into_one_parquet_for_s1(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.dukascopy_dl as dukascopy_dl

    calls: list[list[str]] = []
    monkeypatch.setattr(dukascopy_dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dukascopy_dl, "SOURCE_META_PATH", tmp_path / "source_meta.json")
    monkeypatch.setattr(dukascopy_dl, "GENESIS_PATH", tmp_path / "dukascopy_genesis.json")
    monkeypatch.setattr(dukascopy_dl.subprocess, "run", _fake_dukascopy_run_factory(calls))

    downloader = DukascopyDownloader()
    result = downloader.download(
        "EURUSD",
        "s1",
        tmp_path,
        start="2024-01-01",
        end="2024-01-01",
        merge_s1=True,
    )

    assert [cmd[cmd.index("-p") + 1] for cmd in calls] == ["bid", "ask"]
    assert "price_type:bid+ask" in result.warnings

    merged = pd.read_parquet(result.path)
    assert list(merged.columns) == [
        "timestamp",
        "open_bid",
        "high_bid",
        "low_bid",
        "close_bid",
        "volume_bid",
        "open_ask",
        "high_ask",
        "low_ask",
        "close_ask",
        "volume_ask",
    ]

    source_meta = json.loads((tmp_path / "source_meta.json").read_text())
    assert source_meta["dukascopy/forex/EURUSD/sec"]["merge_bid_ask"] is True


def test_download_uses_selected_price_type_when_merge_flags_are_off(monkeypatch, tmp_path: Path):
    import data_tools.downloaders.dukascopy_dl as dukascopy_dl

    calls: list[list[str]] = []
    monkeypatch.setattr(dukascopy_dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dukascopy_dl, "SOURCE_META_PATH", tmp_path / "source_meta.json")
    monkeypatch.setattr(dukascopy_dl, "GENESIS_PATH", tmp_path / "dukascopy_genesis.json")
    monkeypatch.setattr(dukascopy_dl.subprocess, "run", _fake_dukascopy_run_factory(calls))

    downloader = DukascopyDownloader()
    result = downloader.download(
        "EURUSD",
        "m1",
        tmp_path,
        start="2024-01-01",
        end="2024-01-01",
        price_type="ask",
        merge_tick=True,
        merge_s1=True,
    )

    assert [cmd[cmd.index("-p") + 1] for cmd in calls] == ["ask"]
    assert "price_type:ask" in result.warnings

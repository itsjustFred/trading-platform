"""Tests for scripts/signal_archive_market_features.py (issue #308 offline half)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts import signal_archive_market_features as saf

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ohlcv_frame(
    *, start: str = "2026-10-01", periods: int = 1000, freq: str = "1h"
) -> pd.DataFrame:
    stamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    close = 100.0 + pd.Series(range(periods), dtype=float) * 0.1
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1_000_000.0,
        }
    )


def _write_ohlcv(
    data_root: Path,
    symbol: str = "AAPL",
    timeframe: str = "1h",
    *,
    freq: str = "1h",
    start: str = "2026-10-01",
    periods: int = 1000,
) -> None:
    symbol_dir = data_root / "ohlcv" / "yfinance" / "equity" / "US" / symbol
    symbol_dir.mkdir(parents=True)
    _ohlcv_frame(start=start, periods=periods, freq=freq).to_parquet(
        symbol_dir / f"{timeframe}.parquet", index=False
    )


def _received_record(
    message_id: str,
    *,
    symbol: str = "AAPL",
    timeframe: str = "1h",
    created_at: str = "2026-11-10T12:23:00+00:00",
    parse_status: str = "parsed",
    bot_name: str = "nitro",
    account_id: str | None = None,
) -> dict:
    return {
        "type": "discord_signal_received",
        "schema_version": 1,
        "discord_message_id": message_id,
        "message_created_at": created_at,
        "bot_name": bot_name,
        "account_id": account_id,
        "parse_status": parse_status,
        "signal": {
            "action": "BUY",
            "symbol": symbol,
            "timeframe": timeframe,
        },
    }


def _write_archive(data_root: Path, day: str, records: list[dict]) -> Path:
    log_dir = data_root / "signal_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"discord_signals-{day}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n{bad json\n",
        encoding="utf-8",
    )
    return path


def _read_sidecar(output_dir: Path, day: str) -> list[dict]:
    path = output_dir / f"discord_signal_features-{day}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_enriches_parsed_signal_with_closed_bars_only(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    # Signal at 12:23 UTC on a 1h feed: the 12:00 bar is still forming and
    # must be excluded; the last usable bar is 11:00.
    _write_archive(data_root, "2026-11-10", [_received_record("m1")])

    counts = saf.process_archive_file(
        data_root / "signal_log" / "discord_signals-2026-11-10.jsonl",
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )

    assert counts["features"] == 1
    (record,) = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert record["symbol"] == "AAPL"
    assert record["skip_reason"] is None
    assert record["bars_used"] == saf.DEFAULT_LOOKBACK_BARS
    features = record["features"]
    assert features["atr_14"] is not None
    assert features["atr_14_pct_rank"] is not None
    assert features["dist_sma_20_pct"] is not None
    assert features["dist_vwap_pct"] is not None
    assert features["gap_pct"] is not None
    assert features["trend_regime"] in {"above_sma50", "below_sma50"}
    # No lookahead: the last bar the features saw closed at 12:00, i.e. the
    # 11:00 bar - not the still-forming 12:00 bar.
    assert features["last_bar_time"].startswith("2026-11-10T11:00")


def test_skips_unparsed_and_unmatched_records(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    records = [
        _received_record("m-rejected", parse_status="rejected"),
        _received_record("m-nodata", symbol="NOPE"),
        _received_record("m-notimeframe", timeframe=""),
        {"type": "discord_signal_decision", "discord_message_id": "d1"},
    ]
    archive = _write_archive(data_root, "2026-11-10", records)
    saf.process_archive_file(
        archive,
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    out = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert [r["discord_message_id"] for r in out] == [
        "m-rejected",
        "m-nodata",
        "m-notimeframe",
        "d1",
    ]
    reasons = {r["discord_message_id"]: r["skip_reason"] for r in out}
    assert reasons["m-rejected"] == "not_a_parsed_received_signal"
    assert reasons["m-nodata"] == "no_ohlcv"
    assert reasons["m-notimeframe"] == "no_timeframe"
    assert reasons["d1"] == "not_a_received_record"
    assert all(not r["features"] for r in out)


def test_duplicate_message_ids_keep_first_occurrence(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    archive = _write_archive(
        data_root,
        "2026-11-10",
        [_received_record("dup"), _received_record("dup")],
    )
    counts = saf.process_archive_file(
        archive,
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
        seen_keys=set(),
    )
    assert counts["duplicates"] == 1
    assert counts["features"] == 1
    assert len(_read_sidecar(tmp_path / "out", "2026-11-10")) == 1


def test_decision_sharing_message_id_is_not_deduped(tmp_path: Path) -> None:
    """A ``discord_signal_decision`` reuses its received record's message id.

    Dedupe must only apply to ``discord_signal_received`` records: the
    decision record - even when it precedes the received record in the file -
    still gets its own sidecar row, and the received record keeps its
    features.
    """
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    decision = {
        "type": "discord_signal_decision",
        "discord_message_id": "m1",
        "discord_bot_name": "nitro",
        "account_id": "acc-1",
        "outcome": "traded",
    }
    archive = _write_archive(
        data_root,
        "2026-11-10",
        [decision, _received_record("m1")],
    )
    counts = saf.process_archive_file(
        archive,
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    assert counts["records"] == 2
    assert counts["duplicates"] == 0
    assert counts["features"] == 1
    assert counts["skipped"] == 1
    out = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert len(out) == 2
    by_features = sorted(out, key=lambda r: bool(r["features"]))
    assert by_features[0]["skip_reason"] == "not_a_received_record"
    assert by_features[1]["features"]["atr_14"] is not None


def test_same_message_received_by_two_bots_is_not_a_duplicate(tmp_path: Path) -> None:
    """Two listeners can archive the same Discord message with different parsers."""
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    archive = _write_archive(
        data_root,
        "2026-11-10",
        [
            _received_record("m1", bot_name="nitro"),
            _received_record("m1", bot_name="other-bot", account_id="acc-2"),
        ],
    )
    counts = saf.process_archive_file(
        archive,
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    assert counts["duplicates"] == 0
    assert counts["features"] == 2
    assert len(_read_sidecar(tmp_path / "out", "2026-11-10")) == 2


def test_bar_boundary_is_inclusive_of_last_closed_bar(tmp_path: Path) -> None:
    """A signal at exactly 12:00:00 on a 1h feed sees the 11:00 bar, not 12:00.

    The 11:00 bar closes at 12:00:00, so ``bar_close <= signal_time`` must
    hold; a ``<`` mutant would drop it and report 10:00 as the last bar.
    """
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    _write_archive(
        data_root,
        "2026-11-10",
        [_received_record("m1", created_at="2026-11-10T12:00:00+00:00")],
    )
    saf.process_archive_file(
        data_root / "signal_log" / "discord_signals-2026-11-10.jsonl",
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    (record,) = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert record["features"]["last_bar_time"].startswith("2026-11-10T11:00")


def test_daily_bar_boundary_is_inclusive_of_last_closed_bar(tmp_path: Path) -> None:
    """Same boundary on a 1d feed: signal at 2026-11-10T00:00 sees the Nov 9 bar."""
    data_root = tmp_path / "data"
    _write_ohlcv(
        data_root,
        timeframe="1d",
        freq="D",
        start="2026-01-01",
        periods=400,
    )
    _write_archive(
        data_root,
        "2026-11-10",
        [
            _received_record(
                "m1",
                timeframe="1d",
                created_at="2026-11-10T00:00:00+00:00",
            )
        ],
    )
    saf.process_archive_file(
        data_root / "signal_log" / "discord_signals-2026-11-10.jsonl",
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    (record,) = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert record["features"]["last_bar_time"].startswith("2026-11-09")


def test_frame_and_feature_failures_become_skip_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising normalize/features helper costs one skip, not the whole run."""
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    _write_archive(
        data_root,
        "2026-11-10",
        [_received_record("m1"), _received_record("m2")],
    )

    def _boom(frame: pd.DataFrame) -> pd.DataFrame:
        raise RuntimeError("bad frame")

    monkeypatch.setattr(saf, "normalize_ohlcv_frame", _boom)
    counts = saf.process_archive_file(
        data_root / "signal_log" / "discord_signals-2026-11-10.jsonl",
        tmp_path / "out" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    assert counts["features"] == 0
    assert counts["skipped"] == 2
    out = _read_sidecar(tmp_path / "out", "2026-11-10")
    assert {r["skip_reason"] for r in out} == {"features_failed"}

    monkeypatch.undo()

    def _boom_features(frame: pd.DataFrame) -> dict:
        raise RuntimeError("bad features")

    monkeypatch.setattr(saf, "market_features_from_frame", _boom_features)
    counts = saf.process_archive_file(
        data_root / "signal_log" / "discord_signals-2026-11-10.jsonl",
        tmp_path / "out2" / "discord_signal_features-2026-11-10.jsonl",
        data_root=data_root,
    )
    assert counts["features"] == 0
    assert counts["skipped"] == 2
    out = _read_sidecar(tmp_path / "out2", "2026-11-10")
    assert {r["skip_reason"] for r in out} == {"features_failed"}


def test_main_writes_sidecars_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    _write_archive(data_root, "2026-11-10", [_received_record("m1")])
    _write_archive(
        data_root,
        "2026-11-11",
        [_received_record("m2", created_at="2026-11-11T09:05:00+00:00")],
    )

    rc = saf.main(
        [
            "--data-dir",
            str(data_root),
            "--lookback-bars",
            "60",
        ]
    )

    assert rc == 0
    out_dir = data_root / "signal_log" / "features"
    day1 = _read_sidecar(out_dir, "2026-11-10")
    day2 = _read_sidecar(out_dir, "2026-11-11")
    assert day1[0]["features"]["atr_14"] is not None
    assert day2[0]["features"]["atr_14"] is not None
    assert day1[0]["bars_used"] == 60
    # The archive itself is untouched: still exactly the two input lines plus
    # the deliberately corrupt one per file.
    for day in ("2026-11-10", "2026-11-11"):
        raw = (data_root / "signal_log" / f"discord_signals-{day}.jsonl").read_text()
        assert "discord_signal_market_features" not in raw
    assert "2 file(s)" in capsys.readouterr().out


def test_main_since_until_filters_days(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_ohlcv(data_root)
    _write_archive(data_root, "2026-11-10", [_received_record("m1")])
    _write_archive(data_root, "2026-11-11", [_received_record("m2")])

    rc = saf.main(
        [
            "--data-dir",
            str(data_root),
            "--since",
            "2026-11-11",
            "--until",
            "2026-11-11",
        ]
    )

    assert rc == 0
    out_dir = data_root / "signal_log" / "features"
    assert not (out_dir / "discord_signal_features-2026-11-10.jsonl").exists()
    assert (out_dir / "discord_signal_features-2026-11-11.jsonl").exists()


def _child_env(data_dir: Path) -> dict[str, str]:
    """Sandboxed environment for a script subprocess.

    AGENTS.md: a child that imports ``shared.constants`` loads the checkout's
    real ``.env`` unless the data paths are redirected, so the inherited
    environment is not enough - point every data root at the test sandbox.
    """
    env = dict(os.environ)
    data_dir.mkdir(parents=True, exist_ok=True)
    env["DATA_DIR"] = str(data_dir)
    env["TRADING_DATA_DIR"] = str(data_dir)
    env["LOG_DIR"] = str(data_dir / "logs")
    return env


def test_module_help_exits_without_touching_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.signal_archive_market_features", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=_child_env(data_dir),
    )
    assert result.returncode == 0
    assert "signal_archive_market_features" in result.stdout
    # --help must not build the data trees; the only files the child may write
    # are the import-time logs under LOG_DIR, all inside the sandbox.
    assert not (data_dir / "signal_log").exists()
    assert not (data_dir / "ohlcv").exists()
    written = [p for p in data_dir.rglob("*") if p.is_file()]
    assert written
    assert all("logs" in p.relative_to(data_dir).parts for p in written)

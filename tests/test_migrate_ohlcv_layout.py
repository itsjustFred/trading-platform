"""Tests for ``scripts.migrate_ohlcv_layout`` on tmp old-layout trees (#471)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.migrate_ohlcv_layout import migrate
from shared import ohlcv_store


def _frame(*, periods: int = 3, start: str = "2024-01-01", freq: str = "h") -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": [1.0] * periods,
            "high": [2.0] * periods,
            "low": [0.5] * periods,
            "close": [1.5] * periods,
            "volume": [10.0] * periods,
        }
    )


def _write_old(root: Path, *parts: str, frame: pd.DataFrame | None = None) -> Path:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    (frame if frame is not None else _frame()).to_parquet(path, index=False)
    return path


@pytest.fixture
def old_tree(tmp_path: Path) -> Path:
    """An old-layout tree exercising every migration shape."""
    src = tmp_path / "old" / "ohlcv"
    # Plain canonical file.
    _write_old(src, "dukascopy", "forex", "EURUSD", "1h.parquet")
    # JForex alias stems with split bid/ask sides.
    bid = _frame()
    bid["open"] = 1.0
    ask = _frame()
    ask["open"] = 1.2
    _write_old(src, "dukascopy", "metals", "XAUUSD", "h1_bid.parquet", frame=bid)
    _write_old(src, "dukascopy", "metals", "XAUUSD", "h1_ask.parquet", frame=ask)
    # Region level.
    _write_old(src, "yfinance", "equity", "US", "AAPL", "1d.parquet", frame=_frame(freq="D"))
    # Tick monthly segments.
    _write_old(
        src, "dukascopy", "forex", "EURUSD", "tick_2024_01.parquet",
        frame=_frame(periods=2, freq="min"),
    )
    _write_old(
        src, "dukascopy", "forex", "EURUSD", "tick_2024_02.parquet",
        frame=_frame(periods=2, start="2024-02-01", freq="min"),
    )
    # Flat {symbol}.parquet variant → implicit 1d.
    _write_old(src, "dolthub", "equity", "MSFT.parquet", frame=_frame(freq="D"))
    # Unmappable stem → skipped.
    _write_old(src, "dukascopy", "forex", "EURUSD", "mn1.parquet")
    # Collision: same symbol under two asset classes.
    _write_old(src, "testsrc", "forex", "COLL", "1h.parquet")
    _write_old(src, "testsrc", "metals", "COLL", "1h.parquet")
    return src


def test_migrate_writes_new_layout(old_tree: Path, tmp_path: Path) -> None:
    dst = tmp_path / "new" / "ohlcv"
    migrate(old_tree, dst)

    eurusd_h1 = dst / "dukascopy" / "EURUSD" / "EURUSD_H1.parquet"
    assert eurusd_h1.is_file()
    xauusd_h1 = dst / "dukascopy" / "XAUUSD" / "XAUUSD_H1.parquet"
    assert xauusd_h1.is_file()
    aapl_d1 = dst / "yfinance" / "AAPL" / "AAPL_D1.parquet"
    assert aapl_d1.is_file()
    msft_d1 = dst / "dolthub" / "MSFT" / "MSFT_D1.parquet"
    assert msft_d1.is_file()
    # Collided symbol is never written.
    assert not (dst / "testsrc" / "COLL").exists()

    # Manifest carries asset_class/region from the old path, normalized to
    # the store's single lowercase spelling.
    yfinance_manifest = ohlcv_store.read_manifest(tmp_path / "new", "yfinance")
    assert yfinance_manifest["AAPL"]["asset_class"] == "equity"
    assert yfinance_manifest["AAPL"]["region"] == "us"
    dukascopy_manifest = ohlcv_store.read_manifest(tmp_path / "new", "dukascopy")
    assert dukascopy_manifest["EURUSD"]["asset_class"] == "forex"
    assert dukascopy_manifest["XAUUSD"]["asset_class"] == "metals"

    # Tick segments land as monthly segment files of one dataset.
    entry = ohlcv_store.find_dataset(tmp_path / "new", "EURUSD", "tick", data_source="dukascopy")
    assert entry is not None
    assert len(entry.segments) == 2
    assert {path.name for path in entry.segments} == {
        "EURUSD_TICK_2024_01.parquet",
        "EURUSD_TICK_2024_02.parquet",
    }

    # The migrated tree validates clean.
    assert ohlcv_store.validate_manifest(tmp_path / "new", "dukascopy") == []
    assert ohlcv_store.validate_manifest(tmp_path / "new", "yfinance") == []
    assert ohlcv_store.validate_manifest(tmp_path / "new", "dolthub") == []


def test_split_bid_ask_merged_into_one_file(old_tree: Path, tmp_path: Path) -> None:
    dst = tmp_path / "new" / "ohlcv"
    migrate(old_tree, dst, sources=["dukascopy"])
    entry = ohlcv_store.find_dataset(tmp_path / "new", "XAUUSD", "1h")
    assert entry is not None
    frame = ohlcv_store.read_frame(entry)
    # Canonical columns are the bid side; ask columns survive.
    assert frame["open"].tolist() == [1.0] * 3
    assert "ask_open" in frame.columns
    assert "bid_open" in frame.columns
    assert frame["ask_open"].tolist() == pytest.approx([1.2] * 3)


def test_equity_etf_collision_resolves_to_etf_with_longest_coverage(
    tmp_path: Path,
) -> None:
    """{equity, etf} collisions keep the longest-coverage copy, labelled etf."""
    src = tmp_path / "old" / "ohlcv"
    # ETF1: equity copy starts earlier → wins despite fewer rows.
    _write_old(
        src, "yfinance", "equity", "US", "ETF1", "1d.parquet",
        frame=_frame(periods=2, start="2020-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "etf", "US", "ETF1", "1d.parquet",
        frame=_frame(periods=5, start="2024-01-01", freq="D"),
    )
    # ETF2: same first timestamp → the copy with most rows wins (etf).
    _write_old(
        src, "yfinance", "equity", "US", "ETF2", "1d.parquet",
        frame=_frame(periods=3, start="2024-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "etf", "US", "ETF2", "1d.parquet",
        frame=_frame(periods=6, start="2024-01-01", freq="D"),
    )

    dst = tmp_path / "new" / "ohlcv"
    report = migrate(src, dst, sources=["yfinance"])

    assert report.collisions == []
    assert len(report.resolved) == 2

    manifest = ohlcv_store.read_manifest(tmp_path / "new", "yfinance")
    # Winner data is kept (earliest first wins for ETF1) but the asset_class
    # is always canonicalised to "etf" for {equity, etf} collisions.
    assert manifest["ETF1"]["asset_class"] == "etf"
    assert manifest["ETF1"]["region"] == "us"
    assert manifest["ETF2"]["asset_class"] == "etf"

    entry1 = ohlcv_store.find_dataset(tmp_path / "new", "ETF1", "1d", data_source="yfinance")
    assert entry1 is not None
    assert entry1.first is not None
    assert str(entry1.first).startswith("2020-01-01")
    entry2 = ohlcv_store.find_dataset(tmp_path / "new", "ETF2", "1d", data_source="yfinance")
    assert entry2 is not None
    assert entry2.rows == 6


def test_equity_etf_index_collision_resolves_to_etf(tmp_path: Path) -> None:
    """SPY-style {equity, etf, index} collisions keep the longest-coverage
    copy, canonicalised to asset_class "etf"."""
    src = tmp_path / "old" / "ohlcv"
    # The index copy starts earliest → wins on coverage despite fewer rows.
    _write_old(
        src, "yfinance", "index", "US", "SPY", "1d.parquet",
        frame=_frame(periods=2, start="2020-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "equity", "US", "SPY", "1d.parquet",
        frame=_frame(periods=4, start="2024-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "etf", "US", "SPY", "1d.parquet",
        frame=_frame(periods=6, start="2024-01-01", freq="D"),
    )
    # A second symbol where the etf copy itself has the longest coverage.
    _write_old(
        src, "yfinance", "equity", "US", "QQQ", "1d.parquet",
        frame=_frame(periods=3, start="2024-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "etf", "US", "QQQ", "1d.parquet",
        frame=_frame(periods=8, start="2024-01-01", freq="D"),
    )
    _write_old(
        src, "yfinance", "index", "US", "QQQ", "1d.parquet",
        frame=_frame(periods=2, start="2025-01-01", freq="D"),
    )

    dst = tmp_path / "new" / "ohlcv"
    report = migrate(src, dst, sources=["yfinance"])

    assert report.collisions == []
    assert len(report.resolved) == 2

    manifest = ohlcv_store.read_manifest(tmp_path / "new", "yfinance")
    assert manifest["SPY"]["asset_class"] == "etf"
    assert manifest["QQQ"]["asset_class"] == "etf"

    spy = ohlcv_store.find_dataset(tmp_path / "new", "SPY", "1d", data_source="yfinance")
    assert spy is not None
    assert str(spy.first).startswith("2020-01-01")
    qqq = ohlcv_store.find_dataset(tmp_path / "new", "QQQ", "1d", data_source="yfinance")
    assert qqq is not None
    assert qqq.rows == 8


def test_non_equity_etf_index_collision_still_fails_loudly(tmp_path: Path) -> None:
    """Collisions outside {equity, etf, index} — and {equity, index} with no
    etf copy — are still reported and skipped."""
    src = tmp_path / "old" / "ohlcv"
    _write_old(src, "testsrc", "forex", "COLL", "1h.parquet")
    _write_old(src, "testsrc", "metals", "COLL", "1h.parquet")
    _write_old(src, "testsrc", "equity", "NOETF", "1d.parquet", frame=_frame(freq="D"))
    _write_old(src, "testsrc", "index", "NOETF", "1d.parquet", frame=_frame(freq="D"))
    _write_old(src, "testsrc", "etf", "BADMIX", "1d.parquet", frame=_frame(freq="D"))
    _write_old(src, "testsrc", "crypto", "BADMIX", "1d.parquet", frame=_frame(freq="D"))

    dst = tmp_path / "new" / "ohlcv"
    report = migrate(src, dst)

    assert any("COLL" in item and "multiple layouts" in item for item in report.collisions)
    assert any("NOETF" in item and "multiple layouts" in item for item in report.collisions)
    assert any("BADMIX" in item and "multiple layouts" in item for item in report.collisions)
    assert report.per_source["testsrc"]["collisions"] == 3
    assert not (dst / "testsrc" / "COLL").exists()
    assert not (dst / "testsrc" / "NOETF").exists()
    assert not (dst / "testsrc" / "BADMIX").exists()


def test_collision_winner_refuses_unreadable_parquet(tmp_path: Path) -> None:
    """An unreadable colliding parquet must fail loudly, not skew the pick.

    ``_parquet_first_rows`` used to return ``Timestamp.max`` for a file it
    could not read, which silently disqualified that copy from "longest
    coverage" — the readable-but-shorter copy won and the migration wrote
    the wrong canonical dataset. Coverage that cannot be measured must
    abort the run instead.
    """
    src = tmp_path / "old" / "ohlcv"
    _write_old(
        src, "yfinance", "equity", "US", "BAD", "1d.parquet",
        frame=_frame(periods=2, start="2020-01-01", freq="D"),
    )
    corrupt = _write_old(
        src, "yfinance", "etf", "US", "BAD", "1d.parquet",
        frame=_frame(periods=5, start="2024-01-01", freq="D"),
    )
    corrupt.write_bytes(b"this is not a parquet file")

    with pytest.raises(ValueError, match="BAD"):
        migrate(src, tmp_path / "new" / "ohlcv", sources=["yfinance"])


def test_collisions_and_skips_reported(old_tree: Path, tmp_path: Path) -> None:
    dst = tmp_path / "new" / "ohlcv"
    report = migrate(old_tree, dst)
    assert any("COLL" in item and "multiple layouts" in item for item in report.collisions)
    assert any("mn1" in item for item in report.skipped)
    assert report.per_source["testsrc"]["collisions"] == 1


def test_dry_run_writes_nothing(old_tree: Path, tmp_path: Path) -> None:
    dst = tmp_path / "new" / "ohlcv"
    report = migrate(old_tree, dst, dry_run=True)
    assert report.written
    assert not dst.exists()


def test_source_filters(old_tree: Path, tmp_path: Path) -> None:
    dst = tmp_path / "new" / "ohlcv"
    migrate(old_tree, dst, sources=["yfinance"])
    assert (dst / "yfinance" / "AAPL" / "AAPL_D1.parquet").is_file()
    assert not (dst / "dukascopy").exists()

    dst2 = tmp_path / "new2" / "ohlcv"
    migrate(old_tree, dst2, skip_sources=["dukascopy", "testsrc"])
    assert (dst2 / "yfinance" / "AAPL" / "AAPL_D1.parquet").is_file()
    assert (dst2 / "dolthub" / "MSFT" / "MSFT_D1.parquet").is_file()
    assert not (dst2 / "dukascopy").exists()
    assert not (dst2 / "testsrc").exists()


def test_migrate_never_deletes_source(old_tree: Path, tmp_path: Path) -> None:
    before = sorted(p.relative_to(old_tree) for p in old_tree.rglob("*.parquet"))
    migrate(old_tree, tmp_path / "new" / "ohlcv")
    after = sorted(p.relative_to(old_tree) for p in old_tree.rglob("*.parquet"))
    assert before == after


def test_cli_dry_run(old_tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.migrate_ohlcv_layout import main

    code = main(
        [
            "--src", str(old_tree),
            "--dst", str(tmp_path / "new" / "ohlcv"),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert "per-source summary" in out
    assert code == 1  # collisions present → non-zero

"""Tests for ``shared.ohlcv_store`` — the single OHLCV layout owner (#471)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from shared import ohlcv_store
from shared.ohlcv_store import DatasetEntry


def _frame(
    *,
    periods: int = 4,
    start: str = "2024-01-01",
    freq: str = "h",
    prefix: str = "",
    volume: bool = True,
) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    data: dict[str, object] = {
        "timestamp": index,
        f"{prefix}open": [float(i) for i in range(periods)],
        f"{prefix}high": [float(i) + 0.5 for i in range(periods)],
        f"{prefix}low": [float(i) - 0.5 for i in range(periods)],
        f"{prefix}close": [float(i) + 0.25 for i in range(periods)],
    }
    if volume:
        data[f"{prefix}volume"] = [100.0] * periods
    return pd.DataFrame(data)


_FRAME_KEYS = {"periods", "start", "freq", "prefix", "volume"}


def _write(
    data_root: Path,
    source: str = "testsrc",
    symbol: str = "TEST",
    timeframe: str = "1h",
    **kwargs,
) -> Path:
    frame_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in _FRAME_KEYS}
    kwargs.setdefault("asset_class", "equity")
    return ohlcv_store.write_frame(
        data_root, source, symbol, timeframe, _frame(**frame_kwargs), **kwargs
    )


class TestTimeframeMapping:
    @pytest.mark.parametrize(
        ("platform", "token"),
        [
            ("tick", "TICK"),
            ("s1", "S1"),
            ("1m", "M1"),
            ("5m", "M5"),
            ("15m", "M15"),
            ("30m", "M30"),
            ("1h", "H1"),
            ("4h", "H4"),
            ("1d", "D1"),
            ("1w", "W1"),
        ],
    )
    def test_round_trip(self, platform: str, token: str) -> None:
        assert ohlcv_store.tf_token(platform) == token
        assert ohlcv_store.platform_timeframe(token) == platform

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [("m1", "M1"), ("h1", "H1"), ("d1", "D1"), ("M1", "M1"), (" 1H ", "H1")],
    )
    def test_aliases_accepted(self, alias: str, expected: str) -> None:
        assert ohlcv_store.tf_token(alias) == expected

    @pytest.mark.parametrize("bad", ["", "2h", "mn1", "bogus", None])
    def test_unknown_raises(self, bad) -> None:
        with pytest.raises(ValueError):
            ohlcv_store.tf_token(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ohlcv_store.platform_timeframe(str(bad))


class TestPaths:
    def test_data_path_layout(self, tmp_path: Path) -> None:
        path = ohlcv_store.data_path(tmp_path, "Dukascopy", "EURUSD", "1h")
        assert path == tmp_path / "ohlcv" / "dukascopy" / "EURUSD" / "EURUSD_H1.parquet"

    def test_segment_suffix(self, tmp_path: Path) -> None:
        path = ohlcv_store.data_path(
            tmp_path, "dukascopy", "EURUSD", "tick", segment="2024_01"
        )
        assert path.name == "EURUSD_TICK_2024_01.parquet"

    def test_store_helpers(self, tmp_path: Path) -> None:
        assert ohlcv_store.store_root(tmp_path) == tmp_path / "ohlcv"
        assert ohlcv_store.source_dir(tmp_path, "YFINANCE") == tmp_path / "ohlcv" / "yfinance"
        assert ohlcv_store.manifest_path(tmp_path, "yfinance") == (
            tmp_path / "ohlcv" / "yfinance" / "manifest.json"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"source": "a/b"},
            {"symbol": ".."},
            {"timeframe": "bogus"},
        ],
    )
    def test_unsafe_components_rejected(self, tmp_path: Path, kwargs: dict) -> None:
        args = {"source": "src", "symbol": "SYM", "timeframe": "1h"}
        args.update(kwargs)
        with pytest.raises(ValueError):
            ohlcv_store.data_path(tmp_path, **args)  # type: ignore[arg-type]


class TestManifest:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        _write(tmp_path)
        manifest = ohlcv_store.read_manifest(tmp_path, "testsrc")
        assert manifest["TEST"]["asset_class"] == "equity"
        assert manifest["TEST"]["files"]["TEST_H1.parquet"]["rows"] == 4

    def test_missing_manifest_is_empty_dict(self, tmp_path: Path) -> None:
        assert ohlcv_store.read_manifest(tmp_path, "nope") == {}

    def test_extra_keys_tolerated(self, tmp_path: Path) -> None:
        path = ohlcv_store.write_manifest(
            tmp_path,
            "dukascopy",
            {
                "EURUSD": {
                    "dukascopy_symbol": "EURUSD",
                    "decimal_factor": 5,
                    "surprise": {"nested": True},
                    "files": {"EURUSD_H1.parquet": {"rows": 1}},
                }
            },
        )
        assert path.name == "manifest.json"
        manifest = ohlcv_store.read_manifest(tmp_path, "dukascopy")
        assert manifest["EURUSD"]["dukascopy_symbol"] == "EURUSD"
        assert manifest["EURUSD"]["decimal_factor"] == 5

    def test_source_symbol_fallbacks(self, tmp_path: Path) -> None:
        ohlcv_store.write_manifest(
            tmp_path,
            "dukascopy",
            {
                "EURUSD": {
                    "asset_class": "forex",
                    "dukascopy_symbol": "EURUSD.id",
                    "files": {"EURUSD_H1.parquet": {"rows": 1}},
                },
                "GBPUSD": {
                    "asset_class": "forex",
                    "files": {"GBPUSD_H1.parquet": {"rows": 1}},
                },
            },
        )
        entries = {
            entry.symbol: entry
            for entry in ohlcv_store._dataset_entries_for_source(tmp_path, "dukascopy")
        }
        assert entries["EURUSD"].source_symbol == "EURUSD.id"
        assert entries["GBPUSD"].source_symbol == "GBPUSD"

    def test_asset_class_fallback_table(self, tmp_path: Path) -> None:
        """A manifest without asset_class resolves via the dukascopy defaults."""
        ohlcv_store.write_manifest(
            tmp_path,
            "dukascopy",
            {
                "EURUSD": {"files": {"EURUSD_H1.parquet": {"rows": 1}}},
                "XAUUSD": {"files": {"XAUUSD_H1.parquet": {"rows": 1}}},
                "NAS100": {"files": {"NAS100_H1.parquet": {"rows": 1}}},
                "BRENT": {"files": {"BRENT_H1.parquet": {"rows": 1}}},
                "WEIRDSYM": {"files": {"WEIRDSYM_H1.parquet": {"rows": 1}}},
            },
        )
        classes = {
            entry.symbol: entry.asset_class
            for entry in ohlcv_store._dataset_entries_for_source(tmp_path, "dukascopy")
        }
        assert classes == {
            "EURUSD": "forex",
            "XAUUSD": "metals",
            "NAS100": "indices",
            "BRENT": "energy",
            "WEIRDSYM": "",
        }

    def test_validate_clean_manifest(self, tmp_path: Path) -> None:
        _write(tmp_path)
        assert ohlcv_store.validate_manifest(tmp_path, "testsrc") == []

    def test_validate_missing_manifest(self, tmp_path: Path) -> None:
        problems = ohlcv_store.validate_manifest(tmp_path, "testsrc")
        assert problems == ["testsrc: manifest.json missing"]

    def test_validate_missing_file_and_bad_rows(self, tmp_path: Path) -> None:
        _write(tmp_path)
        manifest = ohlcv_store.read_manifest(tmp_path, "testsrc")
        manifest["TEST"]["files"]["TEST_H1.parquet"]["rows"] = 999
        manifest["TEST"]["files"]["TEST_D1.parquet"] = {
            "rows": 1,
            "first": "2024-01-01 00:00:00+00:00",
            "last": "2024-01-01 00:00:00+00:00",
        }
        ohlcv_store.write_manifest(tmp_path, "testsrc", manifest)
        problems = ohlcv_store.validate_manifest(tmp_path, "testsrc")
        assert any("rows mismatch" in problem for problem in problems)
        assert any("declared file missing" in problem for problem in problems)

    def test_validate_unknown_tf_token(self, tmp_path: Path) -> None:
        ohlcv_store.write_manifest(
            tmp_path,
            "testsrc",
            {"TEST": {"files": {"TEST_X9.parquet": {"rows": 1}}}},
        )
        problems = ohlcv_store.validate_manifest(tmp_path, "testsrc")
        assert any(
            "unparseable" in problem or "unknown TF token" in problem
            for problem in problems
        )


class TestWriteFrame:
    def test_writes_expected_path_and_manifest(self, tmp_path: Path) -> None:
        path = _write(tmp_path, source="dukascopy", symbol="EURUSD", timeframe="4h")
        assert path == (
            tmp_path / "ohlcv" / "dukascopy" / "EURUSD" / "EURUSD_H4.parquet"
        )
        assert path.is_file()
        manifest = ohlcv_store.read_manifest(tmp_path, "dukascopy")
        file_meta = manifest["EURUSD"]["files"]["EURUSD_H4.parquet"]
        assert file_meta["rows"] == 4
        assert file_meta["first"].startswith("2024-01-01")
        assert file_meta["last"].startswith("2024-01-01")

    def test_recomputed_stats_not_caller_supplied(self, tmp_path: Path) -> None:
        _write(tmp_path, periods=7)
        manifest = ohlcv_store.read_manifest(tmp_path, "testsrc")
        assert manifest["TEST"]["files"]["TEST_H1.parquet"]["rows"] == 7

    def test_atomic_write_leaves_no_temp_files(self, tmp_path: Path) -> None:
        _write(tmp_path)
        leftovers = list((tmp_path / "ohlcv").rglob("*.tmp*"))
        assert leftovers == []

    def test_manifest_written_after_data(self, tmp_path: Path) -> None:
        path = _write(tmp_path)
        # The data file exists and the manifest references exactly it.
        manifest = ohlcv_store.read_manifest(tmp_path, "testsrc")
        assert set(manifest["TEST"]["files"]) == {path.name}

    def test_rewrite_updates_manifest_entry(self, tmp_path: Path) -> None:
        _write(tmp_path, periods=4)
        _write(tmp_path, periods=6, start="2024-02-01")
        manifest = ohlcv_store.read_manifest(tmp_path, "testsrc")
        assert manifest["TEST"]["files"]["TEST_H1.parquet"]["rows"] == 6
        assert manifest["TEST"]["files"]["TEST_H1.parquet"]["first"].startswith(
            "2024-02-01"
        )

    def test_extra_meta_lands_on_symbol_entry(self, tmp_path: Path) -> None:
        ohlcv_store.write_frame(
            tmp_path,
            "dukascopy",
            "EURUSD",
            "1h",
            _frame(),
            asset_class="forex",
            extra_meta={"dukascopy_symbol": "EURUSD", "decimal_factor": 5},
        )
        manifest = ohlcv_store.read_manifest(tmp_path, "dukascopy")
        assert manifest["EURUSD"]["dukascopy_symbol"] == "EURUSD"
        assert manifest["EURUSD"]["decimal_factor"] == 5

    @pytest.mark.parametrize(
        "frame",
        [
            pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}),
            _frame(periods=2).iloc[::-1].reset_index(drop=True),
            _frame(periods=3).assign(timestamp=lambda d: d["timestamp"].iloc[0]),
        ],
        ids=["no-timestamp", "unsorted", "duplicate-timestamps"],
    )
    def test_invalid_frames_rejected(self, tmp_path: Path, frame: pd.DataFrame) -> None:
        with pytest.raises(ValueError):
            ohlcv_store.write_frame(
                tmp_path, "src", "SYM", "1h", frame, asset_class="equity"
            )

    def test_bid_only_frame_accepted(self, tmp_path: Path) -> None:
        path = ohlcv_store.write_frame(
            tmp_path, "src", "SYM", "1h", _frame(prefix="bid_"), asset_class="forex"
        )
        assert path.is_file()


class TestListAndFind:
    def test_list_datasets_filters(self, tmp_path: Path) -> None:
        _write(tmp_path, source="alpha", symbol="AAA", timeframe="1h")
        _write(tmp_path, source="beta", symbol="BBB", timeframe="1d")
        _write(tmp_path, source="beta", symbol="AAA", timeframe="1h")
        all_entries = ohlcv_store.list_datasets(tmp_path)
        assert len(all_entries) == 3
        assert {e.source for e in all_entries} == {"alpha", "beta"}
        assert len(ohlcv_store.list_datasets(tmp_path, source="beta")) == 2
        assert len(ohlcv_store.list_datasets(tmp_path, symbol="aaa")) == 2
        assert len(ohlcv_store.list_datasets(tmp_path, timeframe="1h")) == 2
        assert ohlcv_store.list_datasets(tmp_path, timeframe="1w") == []

    def test_unmanifested_files_ignored_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path, symbol="AAA")
        stray = ohlcv_store.data_path(tmp_path, "testsrc", "STRAY", "1h")
        stray.parent.mkdir(parents=True, exist_ok=True)
        _frame().to_parquet(stray, index=False)
        with caplog.at_level("WARNING"):
            entries = ohlcv_store.list_datasets(tmp_path)
        assert [e.symbol for e in entries] == ["AAA"]
        assert any(
            "not in" in record.message and "manifest" in record.message
            for record in caplog.records
        )

    def test_find_dataset_ranking_prefers_asset_class_then_source(self, tmp_path: Path) -> None:
        # EURUSD is a 6-letter forex-shaped symbol: a "metals"-classed copy
        # must lose to a "forex"-classed one regardless of source name.
        _write(tmp_path, source="zzz", symbol="EURUSD", asset_class="metals")
        _write(tmp_path, source="aaa", symbol="EURUSD", asset_class="forex")
        entry = ohlcv_store.find_dataset(tmp_path, "EURUSD", "1h")
        assert entry is not None
        assert entry.asset_class == "forex"
        assert entry.source == "aaa"

    def test_find_dataset_source_priority(self, tmp_path: Path) -> None:
        # Same asset class: huggingface outranks dukascopy outranks yfinance.
        for source in ("yfinance", "dukascopy", "huggingface"):
            _write(tmp_path, source=source, symbol="AAPL", asset_class="equity")
        entry = ohlcv_store.find_dataset(tmp_path, "AAPL", "1h")
        assert entry is not None
        assert entry.source == "huggingface"

    def test_find_dataset_explicit_source(self, tmp_path: Path) -> None:
        _write(tmp_path, source="alpha", symbol="AAA", asset_class="equity")
        _write(tmp_path, source="beta", symbol="AAA", asset_class="equity")
        entry = ohlcv_store.find_dataset(tmp_path, "AAA", "1h", data_source="beta")
        assert entry is not None
        assert entry.source == "beta"
        assert ohlcv_store.find_dataset(tmp_path, "AAA", "1h", data_source="gamma") is None

    def test_find_dataset_data_source_matches_case_insensitively(
        self, tmp_path: Path
    ) -> None:
        """The data_source filter matched lowercased path components under the
        old layout; manifest values keep their case (region "US"), so both
        sides must compare lowercase."""
        _write(tmp_path, source="yfinance", symbol="SPY", asset_class="etf", region="US")
        for requested in ("us", "US", "YFINANCE", "Etf"):
            entry = ohlcv_store.find_dataset(
                tmp_path, "SPY", "1h", data_source=requested
            )
            assert entry is not None, requested

    def test_find_dataset_missing(self, tmp_path: Path) -> None:
        assert ohlcv_store.find_dataset(tmp_path, "NOPE", "1h") is None
        _write(tmp_path, symbol="AAA", timeframe="1h")
        assert ohlcv_store.find_dataset(tmp_path, "AAA", "1d") is None

    def test_find_dataset_timeframe_alias(self, tmp_path: Path) -> None:
        _write(tmp_path, symbol="AAA", timeframe="1h")
        entry = ohlcv_store.find_dataset(tmp_path, "AAA", "h1")
        assert entry is not None
        assert entry.timeframe == "1h"


class TestReadFrame:
    def test_read_frame_basic(self, tmp_path: Path) -> None:
        path = _write(tmp_path, periods=5)
        frame = ohlcv_store.read_frame(path)
        assert list(frame.columns[:6]) == [
            "timestamp", "open", "high", "low", "close", "volume",
        ]
        assert len(frame) == 5
        assert frame["timestamp"].dt.tz is not None

    def test_read_frame_two_sided_bid_fallback(self, tmp_path: Path) -> None:
        frame_in = _frame(prefix="bid_")
        for side in ("ask_",):
            for column in ("open", "high", "low", "close", "volume"):
                frame_in[f"{side}{column}"] = frame_in[f"bid_{column}"] + 0.1
        ohlcv_store.write_frame(
            tmp_path, "dukascopy", "EURUSD", "1h", frame_in, asset_class="forex"
        )
        entry = ohlcv_store.find_dataset(tmp_path, "EURUSD", "1h")
        assert entry is not None
        out = ohlcv_store.read_frame(entry)
        assert out["open"].tolist() == frame_in["bid_open"].tolist()
        assert out["close"].tolist() == frame_in["bid_close"].tolist()
        assert out["volume"].tolist() == frame_in["bid_volume"].tolist()
        assert "ask_open" in out.columns
        assert "bid_open" in out.columns

    def test_read_frame_plain_columns_win_over_bid(self, tmp_path: Path) -> None:
        frame_in = _frame()
        frame_in["bid_close"] = frame_in["close"] + 100.0
        ohlcv_store.write_frame(tmp_path, "src", "SYM", "1h", frame_in, asset_class="equity")
        out = ohlcv_store.read_frame(tmp_path / "ohlcv" / "src" / "SYM" / "SYM_H1.parquet")
        assert out["close"].tolist() == frame_in["close"].tolist()

    def test_read_frame_date_filter_inclusive_end(self, tmp_path: Path) -> None:
        _write(tmp_path, periods=4, freq="D", start="2024-01-01")
        frame = ohlcv_store.read_frame(
            tmp_path / "ohlcv" / "testsrc" / "TEST" / "TEST_H1.parquet",
            start="2024-01-02",
            end="2024-01-03",
        )
        # Date-only end is inclusive of the whole last day (F9 convention).
        assert len(frame) == 2
        assert frame["timestamp"].dt.strftime("%Y-%m-%d").tolist() == [
            "2024-01-02", "2024-01-03",
        ]

    def test_read_frame_columns_subset(self, tmp_path: Path) -> None:
        path = _write(tmp_path)
        frame = ohlcv_store.read_frame(path, columns=["timestamp", "close"])
        assert list(frame.columns) == ["timestamp", "close"]

    def test_read_frame_missing_file_is_empty(self, tmp_path: Path) -> None:
        frame = ohlcv_store.read_frame(tmp_path / "ohlcv" / "src" / "SYM" / "SYM_H1.parquet")
        assert frame.empty

    def test_read_frame_unreadable_segment_fails_loudly(self, tmp_path: Path) -> None:
        """A corrupt segment must not silently shrink a multi-segment frame:
        the remaining rows would produce a wrong backtest."""
        _write(tmp_path, symbol="SYM", timeframe="tick", segment="2024_01", freq="D")
        feb_path = _write(
            tmp_path, symbol="SYM", timeframe="tick", segment="2024_02",
            start="2024-02-01", freq="D",
        )
        feb_path.write_bytes(b"not a parquet")
        entry = ohlcv_store.find_dataset(tmp_path, "SYM", "tick")
        assert entry is not None
        with pytest.raises(ValueError):
            ohlcv_store.read_frame(entry)

    def test_read_frame_missing_segment_fails_loudly(self, tmp_path: Path) -> None:
        """Same for a declared segment absent from disk: partial coverage is
        not a frame."""
        _write(tmp_path, symbol="SYM", timeframe="tick", segment="2024_01", freq="D")
        feb_path = _write(
            tmp_path, symbol="SYM", timeframe="tick", segment="2024_02",
            start="2024-02-01", freq="D",
        )
        feb_path.unlink()
        entry = ohlcv_store.find_dataset(tmp_path, "SYM", "tick")
        assert entry is not None
        with pytest.raises(FileNotFoundError):
            ohlcv_store.read_frame(entry)

    def test_read_frame_segments_concatenated_in_order(self, tmp_path: Path) -> None:
        jan = _frame(periods=2, start="2024-01-01", freq="D")
        feb = _frame(periods=2, start="2024-02-01", freq="D")
        ohlcv_store.write_frame(
            tmp_path, "src", "SYM", "tick", jan, asset_class="forex", segment="2024_01"
        )
        ohlcv_store.write_frame(
            tmp_path, "src", "SYM", "tick", feb, asset_class="forex", segment="2024_02"
        )
        entry = ohlcv_store.find_dataset(tmp_path, "SYM", "tick")
        assert entry is not None
        assert len(entry.segments) == 2
        out = ohlcv_store.read_frame(entry)
        assert out["timestamp"].is_monotonic_increasing
        assert len(out) == 4
        assert out["timestamp"].dt.strftime("%Y-%m").tolist() == [
            "2024-01", "2024-01", "2024-02", "2024-02",
        ]

    def test_timestamp_bounds_from_row_groups(self, tmp_path: Path) -> None:
        path = _write(tmp_path, periods=4, freq="D", start="2024-01-01")
        bounds = ohlcv_store.timestamp_bounds(path)
        assert bounds is not None
        first, last = bounds
        assert first == pd.Timestamp("2024-01-01", tz="UTC")
        assert last == pd.Timestamp("2024-01-04", tz="UTC")

    def test_timestamp_bounds_missing_file(self, tmp_path: Path) -> None:
        assert ohlcv_store.timestamp_bounds(tmp_path / "nope.parquet") is None


class TestEntryForPath:
    def test_resolves_dataset_for_stored_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path, source="dukascopy", symbol="EURUSD")
        entry = ohlcv_store.entry_for_path(tmp_path, path)
        assert isinstance(entry, DatasetEntry)
        assert entry.source == "dukascopy"
        assert entry.symbol == "EURUSD"
        assert entry.timeframe == "1h"
        assert entry.asset_class == "equity"

    def test_segment_path_resolves_to_dataset(self, tmp_path: Path) -> None:
        path = ohlcv_store.write_frame(
            tmp_path, "src", "SYM", "tick", _frame(), asset_class="forex",
            segment="2024_01",
        )
        entry = ohlcv_store.entry_for_path(tmp_path, path)
        assert entry is not None
        assert entry.timeframe == "tick"
        assert path in entry.segments

    @pytest.mark.parametrize(
        "path",
        [
            "ohlcv/src/SYM/notaparquet.txt",
            "ohlcv/src/SYM/STRAY_H1.parquet",
            "outside/SYM_H1.parquet",
            "ohlcv/src/SYM",
        ],
    )
    def test_unrelated_paths_return_none(self, tmp_path: Path, path: str) -> None:
        _write(tmp_path, source="src", symbol="SYM")
        assert ohlcv_store.entry_for_path(tmp_path, tmp_path / path) is None

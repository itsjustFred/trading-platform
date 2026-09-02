"""Failed-download visibility, source_meta atomicity, and storage-path contracts.

Covers DV-B9 (DownloadResult.path is a directory), DV-B16 (failed month/year
downloads silently skipped), DV-B22 (tick CSVs stored under 1m.parquet; metals
under asset_class forex), DV-C1 (merged tick download duplicate kwarg).
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
from pathlib import Path

import pandas as pd
import pytest

from data_tools.downloaders.base import Downloader, DownloadResult


def _merged_tick_call_site(native_module) -> ast.Call:
    """Return the AST call node of the merged bid+ask tick download in download()."""
    source_path = Path(inspect.getfile(native_module))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_download_and_assemble_merged_ticks":
                return node.value
    raise AssertionError("merged tick call site not found")


class _BareDownloader(Downloader):
    @property
    def source_name(self) -> str:
        return "bare"

    def list_available(self, **kwargs) -> list[dict]:
        return []

    def download(self, symbol, timeframe, output_dir, *, start=None, end=None, **kwargs):
        raise NotImplementedError


@pytest.fixture
def downloader():
    return _BareDownloader()


class TestDownloadResultContract:
    def test_path_must_point_at_a_file(self, tmp_path):
        """Regression DV-B9: a directory path breaks every consumer that reads
        result.path as the stored parquet."""
        directory = tmp_path / "ohlcv" / "bare" / "forex" / "EURUSD"
        directory.mkdir(parents=True)
        (directory / "1m.parquet").write_bytes(b"stub")

        result = DownloadResult(
            symbol="EURUSD",
            timeframe="1m",
            source="bare",
            asset_class="forex",
            path=directory / "1m.parquet",
            rows=1,
            start="2024-01-01",
            end="2024-01-01",
            size_bytes=4,
        )
        assert result.path.is_file()
        assert not result.path.is_dir()

    def test_jforex_merged_download_reports_file_not_directory(self, tmp_path):
        """JForex bid/ask merges store two parquets; the result path decision
        must point at the stored bid file, never the symbol directory."""
        from data_tools.downloaders.jforex_dl import merged_result_path

        symbol_dir = tmp_path / "ohlcv" / "jforex" / "forex" / "EURUSD"
        symbol_dir.mkdir(parents=True)
        bid_path = symbol_dir / "1h_bid.parquet"
        frame = pd.DataFrame(
            {"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"], utc=True), "close": [1.0]}
        )
        frame.to_parquet(bid_path, index=False)

        resolved = merged_result_path(bid_path)
        assert resolved.is_file()
        assert not resolved.is_dir()
        assert resolved == bid_path

    def test_jforex_merged_result_path_fails_closed_when_missing(self, tmp_path):
        """A partial merge (bid parquet absent) must not report a path."""
        from data_tools.downloaders.jforex_dl import merged_result_path

        with pytest.raises(RuntimeError, match="did not store"):
            merged_result_path(tmp_path / "ohlcv" / "jforex" / "forex" / "EURUSD"
                               / "1h_bid.parquet")


class TestSourceMetaAtomicity:
    def test_source_meta_write_is_atomic(self, tmp_path, monkeypatch):
        """source_meta.json is read by every catalog scan; a partial write must
        never replace the previous content."""
        import data_tools.downloaders.dukascopy_native as native

        meta_path = tmp_path / "source_meta.json"
        meta_path.write_text(json.dumps({"dukascopy/forex/EURUSD/1h": {"license": "free"}}))

        monkeypatch.setattr(native, "SOURCE_META_PATH", meta_path)
        monkeypatch.setattr(native, "DATA_DIR", tmp_path)

        native._write_source_meta_index(
            {
                "dukascopy/forex/EURUSD/1h": {"license": "free"},
                "dukascopy/forex/GBPUSD/1h": {"license": "free"},
            }
        )

        payload = json.loads(meta_path.read_text())
        assert len(payload) == 2
        assert not list(tmp_path.glob("source_meta.json.tmp-*"))


class TestCtraderStorageLayout:
    def test_tick_data_is_not_stored_as_1m_parquet(self, tmp_path):
        """Regression DV-B22: tick CSVs must land in tick.parquet, not 1m.parquet."""
        from data_tools.downloaders.ctrader_dl import CTraderDownloader

        csv_path = tmp_path / "EURUSD_tick.csv"
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z"], utc=True
                ),
                "open": [1.1, 1.1001],
                "high": [1.1005, 1.1006],
                "low": [1.0995, 1.0996],
                "close": [1.1002, 1.1003],
                "volume": [2, 2],
            }
        ).to_csv(csv_path, index=False)

        downloader = CTraderDownloader()
        result = downloader.download(
            "EURUSD", "tick", tmp_path, csv_file=str(csv_path)
        )

        stored = Path(result.path)
        assert stored.name == "tick.parquet", f"tick data stored as {stored.name}"
        assert stored.is_file()

    def test_metals_use_metals_asset_class(self, tmp_path):
        """Regression DV-B22: XAUUSD is a metal, not forex."""
        from data_tools.downloaders.ctrader_dl import CTraderDownloader

        csv_path = tmp_path / "XAUUSD_tick.csv"
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z"], utc=True
                ),
                "open": [2000.0, 2000.1],
                "high": [2000.5, 2000.6],
                "low": [1999.5, 1999.6],
                "close": [2000.2, 2000.3],
                "volume": [2, 2],
            }
        ).to_csv(csv_path, index=False)

        downloader = CTraderDownloader()
        result = downloader.download("XAUUSD", "tick", tmp_path, csv_file=str(csv_path))

        assert result.asset_class == "metals"
        assert "metals" in str(result.path)


class TestHistdataFailureVisibility:
    def test_failed_year_is_reported_not_silently_skipped(self, tmp_path, monkeypatch):
        """Regression DV-B16: a year that fails to download must show up in the
        result, not disappear while the dataset looks covered."""
        from data_tools.downloaders.histdata_dl import HistdataDownloader

        downloader = HistdataDownloader()
        monkeypatch.setattr(
            downloader,
            "_download_year",
            lambda symbol, year: (
                pd.DataFrame(
                    {
                        "timestamp": pd.to_datetime([f"{year}-06-01T00:00:00Z"], utc=True),
                        "open": [1.0],
                        "high": [1.1],
                        "low": [0.9],
                        "close": [1.0],
                        "volume": [10],
                    }
                )
                if year != 2021
                else None
            ),
        )

        result = downloader.download(
            "EURUSD", "1m", tmp_path, years=[2020, 2021, 2022], start=None, end=None
        )

        assert result.warnings, "missing year must be surfaced in warnings"
        assert any("2021" in warning for warning in result.warnings)


class TestHuggingfaceFailureVisibility:
    def test_failed_month_is_reported_not_silently_skipped(self, tmp_path, monkeypatch):
        """Regression DV-B16: a month that fails to download must show up in the
        result warnings."""
        from data_tools.downloaders.huggingface_dl import HuggingFaceDownloader

        downloader = HuggingFaceDownloader()
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-05T00:00:00Z"], utc=True),
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.0],
                "volume": [10],
                "symbol": ["AAPL"],
            }
        )

        def fake_read_parquet(url, *args, **kwargs):
            if "2024-01" in url:
                return frame
            raise RuntimeError(f"network down for {url}")

        monkeypatch.setattr(pd, "read_parquet", fake_read_parquet)

        result = downloader.download(
            "AAPL", "1m", tmp_path, start="2024-01-01", end="2024-02-29"
        )

        assert result.warnings, "missing month must be surfaced in warnings"
        assert any("2024-02" in warning for warning in result.warnings)


class TestDukascopyMergedTickContract:
    def test_merged_tick_call_passes_pipeline_batches_without_duplication(self):
        """Regression DV-C1 / Sonar S5549: the merged bid+ask tick call must
        pass pipeline_batches exactly once. The pre-fix call passed six
        positional args plus pipeline_batches as keyword, which raises
        TypeError at download time (never covered by a test until now)."""
        import data_tools.downloaders.dukascopy_native as native

        signature = inspect.signature(native._download_and_assemble_merged_ticks)
        positional = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        keyword_names = {
            name for name in signature.parameters if name not in positional
        }

        call = _merged_tick_call_site(native)
        passed_positional = [ast.unparse(arg) for arg in call.args]
        passed_keyword = {keyword.arg for keyword in call.keywords}

        assert len(passed_positional) == len(positional), (
            f"call passes {len(passed_positional)} positional args, "
            f"function defines {len(positional)}: {passed_positional} vs {positional}"
        )
        overlap = passed_keyword & set(positional)
        assert not overlap, f"positional params re-passed as keywords: {sorted(overlap)}"
        unknown = passed_keyword - set(signature.parameters)
        assert not unknown, f"call passes unknown keywords: {sorted(unknown)}"
        assert "pipeline_batches" in keyword_names | passed_keyword

    def test_merged_tick_call_is_executable(self, tmp_path, monkeypatch):
        """The merged tick path must not raise TypeError when invoked with the
        arguments the download() method actually passes."""
        import data_tools.downloaders.dukascopy_native as native

        captured: dict[str, object] = {}

        async def fake_merged(instrument, start_dt, end_dt, batch_size, **kwargs):
            captured["pipeline_batches"] = kwargs["pipeline_batches"]
            captured["pause_ms"] = kwargs["pause_ms"]
            captured["max_retries"] = kwargs["max_retries"]
            return pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"], utc=True)})

        monkeypatch.setattr(native, "_download_and_assemble_merged_ticks", fake_merged)
        monkeypatch.setattr(native, "DATA_DIR", tmp_path)
        monkeypatch.setattr(native, "SOURCE_META_PATH", tmp_path / "source_meta.json")
        monkeypatch.setattr(native, "GENESIS_PATH", tmp_path / "genesis.json")
        monkeypatch.setattr(native, "_load_genesis_index", lambda: {})
        monkeypatch.setattr(native, "_load_source_meta_index", lambda: {})
        monkeypatch.setattr(native, "_write_source_meta_index", lambda payload: None)

        downloader = native.DukascopyNativeDownloader()
        # Run the sync download wrapper in a dedicated thread: the e2e
        # playwright tests in this suite can leave the main thread's event
        # loop in a non-runnable state, which makes asyncio.run() raise.
        result_box: dict[str, object] = {}

        def _run() -> None:
            result_box["result"] = downloader.download(
                "EURUSD",
                "tick",
                tmp_path,
                start="2024-01-01",
                end="2024-01-02",
                offer_side="both",
                batch_size=7,
                pipeline_batches=3,
                pause_ms=11,
                max_retries=5,
            )

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "merged tick download timed out"

        result = result_box["result"]
        assert captured == {"pipeline_batches": 3, "pause_ms": 11, "max_retries": 5}
        assert result.rows >= 0


class TestAtomicWriteTargetValidation:
    """Sonar S8707: the shared atomic-write sink validates its target path."""

    def test_write_rejects_parent_traversal(self, tmp_path):
        from shared.atomic_io import atomic_replace

        with pytest.raises(ValueError, match="traverse upward"):
            atomic_replace(tmp_path / "sub" / ".." / "evil.parquet", lambda _p: None)

    def test_write_anchors_relative_path_to_cwd(self, tmp_path, monkeypatch):
        from shared.atomic_io import atomic_replace

        # CLI callers (resample --output, catalog_builder --output) pass
        # relative paths; they are anchored to the working directory.
        monkeypatch.chdir(tmp_path)
        target = Path("out/catalog.json")
        atomic_replace(target, lambda p: p.write_text("ok"))
        assert (tmp_path / "out" / "catalog.json").read_text() == "ok"

    def test_write_rejects_symlinked_parent_escape(self, tmp_path):
        from shared.atomic_io import atomic_replace

        outside = tmp_path / "outside"
        outside.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(outside)
        target = link_dir / "escape" / "data.parquet"

        with pytest.raises(ValueError, match="symlink"):
            atomic_replace(target, lambda _p: None)

    def test_write_rejects_sibling_symlink_name_trick(self, tmp_path):
        """outside2 -> outside must be refused: the lexical prefix check alone
        would let a sibling-named symlink redirect the write (CodeRabbit)."""
        from shared.atomic_io import atomic_replace

        outside = tmp_path / "outside"
        outside.mkdir()
        sibling = tmp_path / "outside2"
        sibling.symlink_to(outside)
        target = sibling / "data.parquet"

        with pytest.raises(ValueError, match="symlink"):
            atomic_replace(target, lambda p: p.write_text("evil"))
        assert not (outside / "data.parquet").exists()
        assert list(tmp_path.rglob("*.tmp-*")) == []

    def test_write_allows_legitimate_target(self, tmp_path):
        from shared.atomic_io import atomic_replace

        target = tmp_path / "ohlcv" / "x" / "1m.parquet"
        atomic_replace(target, lambda p: p.write_text("ok"))
        assert target.read_text() == "ok"

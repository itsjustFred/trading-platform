"""Remote-artifact integrity bounds (B3: histdata zip, HF host/gaps)."""

from __future__ import annotations

import io
import zipfile


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class TestHistdataZipBounds:
    def test_valid_single_csv_parses(self) -> None:
        from data_tools.downloaders.histdata_dl import _read_histdata_zip

        payload = _zip_bytes({"EURUSD_2024.csv": b"20240101 000000;1.1;1.2;1.0;1.15;10\n"})
        frame = _read_histdata_zip(payload, symbol="EURUSD", year=2024)
        assert len(frame) == 1
        assert frame["close"].iloc[0] == 1.15

    def test_multi_entry_rejected(self) -> None:
        import pytest

        from data_tools.downloaders.histdata_dl import _read_histdata_zip

        payload = _zip_bytes({"a.csv": b"x", "b.csv": b"y"})
        with pytest.raises(ValueError, match="exactly one CSV"):
            _read_histdata_zip(payload, symbol="EURUSD", year=2024)

    def test_zip_slip_rejected(self) -> None:
        import pytest

        from data_tools.downloaders.histdata_dl import _read_histdata_zip

        payload = _zip_bytes({"../evil.csv": b"20240101 000000;1.1;1.2;1.0;1.15;10\n"})
        with pytest.raises(ValueError, match="not a plain CSV filename"):
            _read_histdata_zip(payload, symbol="EURUSD", year=2024)

    def test_oversize_archive_rejected(self, monkeypatch) -> None:
        import pytest

        import data_tools.downloaders.histdata_dl as histdata_module

        monkeypatch.setattr(histdata_module, "HISTDATA_MAX_ARCHIVE_BYTES", 10)
        with pytest.raises(ValueError, match="cap"):
            histdata_module._read_histdata_zip(b"x" * 11, symbol="EURUSD", year=2024)


class TestHuggingFaceGuards:
    def test_unknown_host_rejected(self) -> None:
        import pytest

        from data_tools.downloaders.huggingface_dl import _check_month_url

        with pytest.raises(ValueError, match="not allowlisted"):
            _check_month_url("https://evil.example/data.parquet")
        _check_month_url(
            "https://huggingface.co/datasets/mito0o852/OHLCV-1m/resolve/main/data/ohlcv_2024-01.parquet"
        )

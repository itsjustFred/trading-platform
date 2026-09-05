"""Tests for atomic catalog metadata writes and path-parse fail-closed (DV-B11)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import data_tools.catalog_builder as catalog_builder
from data_tools.catalog_builder import build_catalog, parse_ohlcv_path


@pytest.fixture
def symbol_dir(tmp_path):
    ohlcv = tmp_path / "ohlcv" / "test_source" / "equity" / "AAPL"
    ohlcv.mkdir(parents=True)
    return ohlcv


def _write_bars(path: Path, periods: int = 5) -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="h", tz="UTC"),
            "open": range(periods),
            "high": range(1, periods + 1),
            "low": range(periods),
            "close": range(periods),
            "volume": range(periods),
        }
    )
    frame.to_parquet(path, index=False)


class TestAtomicCatalogWrite:
    def test_catalog_output_write_is_atomic(self, tmp_path, monkeypatch):
        """A crash while serializing must not truncate an existing catalog.json."""
        output = tmp_path / "ohlcv" / "test_source" / "equity" / "AAPL" / "1h.parquet"
        output.parent.mkdir(parents=True)
        _write_bars(output)

        first = build_catalog(tmp_path)
        catalog_path = tmp_path / "catalog.json"
        catalog_path.write_text(json.dumps(first, indent=2, default=str))

        def flaky_dumps(*args, **kwargs):
            raise KeyboardInterrupt("crash while serializing catalog")

        monkeypatch.setattr(catalog_builder.json, "dumps", flaky_dumps)
        rebuilt = catalog_builder.build_catalog(tmp_path)

        with pytest.raises(KeyboardInterrupt):
            catalog_builder.write_catalog_json(catalog_path, rebuilt)

        # The existing catalog is still valid JSON and unparsed entries survive.
        payload = json.loads(catalog_path.read_text())
        assert payload["ohlcv"], "previous catalog must survive a failed rebuild"
        assert not list(tmp_path.glob("catalog.json.tmp-*")), "temp file cleaned up"

    def test_write_catalog_json_replaces_atomically(self, tmp_path, monkeypatch):
        """The helper writes a temp file and os.replace()s it into place."""
        target = tmp_path / "catalog.json"
        target.write_text('{"previous": true}')

        observed: dict[str, object] = {}

        def fake_dumps(payload, **kwargs):
            observed["payload"] = payload
            return '{"next": true}'

        monkeypatch.setattr(catalog_builder.json, "dumps", fake_dumps)

        catalog_builder.write_catalog_json(target, {"generated_at": "x"})

        assert target.read_text() == '{"next": true}'
        assert not list(tmp_path.glob("catalog.json.tmp-*")), "no temp file left behind"
        assert observed["payload"] == {"generated_at": "x"}


class TestTimeframeFailClosed:
    def test_unparseable_filename_is_rejected_not_defaulted_to_1d(self, tmp_path):
        """Regression DV-B11: a file whose timeframe cannot be parsed must not
        silently enter the catalog as a 1d dataset."""
        flat = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
        flat.mkdir(parents=True)
        _write_bars(flat / "AAPL_2020_snapshot.parquet")

        entry = parse_ohlcv_path(flat / "AAPL_2020_snapshot.parquet", tmp_path / "ohlcv")
        if entry is not None:
            assert entry["timeframe"] != "1d", (
                "unparseable timeframe must not be silently reported as 1d"
            )

    def test_parse_ohlcv_path_returns_none_for_unknown_stem(self, tmp_path):
        """Stems that are neither a timeframe nor symbol_timeframe are skipped."""
        flat = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
        flat.mkdir(parents=True)
        weird = flat / "not-a-timeframe-or-symbol.parquet"
        _write_bars(weird)

        entry = parse_ohlcv_path(weird, tmp_path / "ohlcv")

        assert entry is None or entry["timeframe"] != "1d"

    def test_catalog_does_not_list_unparseable_file_as_1d(self, tmp_path):
        flat = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
        flat.mkdir(parents=True)
        _write_bars(flat / "AAPL_2020_snapshot.parquet")

        catalog = build_catalog(tmp_path)

        timeframes = [entry.get("timeframe") for entry in catalog["ohlcv"]]
        assert "1d" not in timeframes, f"silent 1d default leaked into catalog: {catalog['ohlcv']}"

    def test_legacy_flat_symbol_file_still_resolves_to_1d(self, tmp_path):
        """The documented flat export layout {symbol}.parquet keeps its 1d mapping."""
        flat = tmp_path / "ohlcv" / "dolthub" / "equity" / "US"
        flat.mkdir(parents=True)
        _write_bars(flat / "AAPL.parquet")

        catalog = build_catalog(tmp_path)

        assert catalog["ohlcv"][0]["timeframe"] == "1d"
        assert catalog["ohlcv"][0]["symbol"] == "AAPL"

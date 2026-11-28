"""Tests for atomic catalog metadata writes and manifest-driven OHLCV scans.

Pre-#471 this file also covered ``parse_ohlcv_path`` fail-closed behaviour on
the legacy ``ohlcv/<source>/<asset_class>/...`` layout. The manifest store has
no path parsing left to get wrong: files the manifest does not declare are
invisible to the catalog, which is the stronger guarantee the tests below pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import data_tools.catalog_builder as catalog_builder
from data_tools.catalog_builder import build_catalog
from shared import ohlcv_store


def _bars_frame(periods: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="h", tz="UTC"),
            "open": range(periods),
            "high": range(1, periods + 1),
            "low": range(periods),
            "close": range(periods),
            "volume": range(periods),
        }
    )


def _write_unmanifested_bars(path: Path, periods: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _bars_frame(periods).to_parquet(path, index=False)


class TestAtomicCatalogWrite:
    def test_catalog_output_write_is_atomic(self, tmp_path, monkeypatch):
        """A crash while serializing must not truncate an existing catalog.json."""
        ohlcv_store.write_frame(
            tmp_path, "test_source", "AAPL", "1h", _bars_frame(), asset_class="equity"
        )

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


class TestManifestDrivenCatalog:
    def test_unmanifested_parquet_is_not_listed(self, tmp_path):
        """A parquet the manifest does not declare cannot leak into the catalog.

        This is the manifest-store successor of the DV-B11 fail-closed rule:
        there is no filename stem left to misparse as ``1d`` — undeclared
        files are simply invisible.
        """
        stray = (
            tmp_path / "ohlcv" / "dolthub" / "AAPL" / "AAPL_D1_2020_snapshot.parquet"
        )
        _write_unmanifested_bars(stray)

        catalog = build_catalog(tmp_path)

        assert catalog["ohlcv"] == []

    def test_manifest_declared_dataset_is_listed(self, tmp_path):
        ohlcv_store.write_frame(
            tmp_path, "dolthub", "AAPL", "1d", _bars_frame(), asset_class="equity"
        )

        catalog = build_catalog(tmp_path)

        assert len(catalog["ohlcv"]) == 1
        entry = catalog["ohlcv"][0]
        assert entry["symbol"] == "AAPL"
        assert entry["timeframe"] == "1d"

"""Tests for data_tools.dukascopy_discovery."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from data_tools.dukascopy_discovery import _is_valid_probe_csv, _month_probe_date, _year_probe_date

# ---------------------------------------------------------------------------
# Fake genesis data used to drive the binary-search probe algorithm.
# ---------------------------------------------------------------------------

_FAKE_GENESIS: dict[str, dict[str, str]] = {
    "EURUSD": {
        "d1": "1999-01-01",
        "h4": "2003-05-04",
        "h1": "2003-05-04",
        "m30": "2003-05-04",
        "m15": "2003-05-04",
        "m5": "2003-05-04",
        "m1": "2003-05-04",
        "s1": "2003-05-04",
        "tick": "2003-05-04",
    },
    "XAUUSD": {
        "d1": "1999-01-01",
        "h4": "2003-05-04",
        "h1": "2003-05-04",
        "m30": "2003-05-04",
        "m15": "2003-05-04",
        "m5": "2003-05-04",
        "m1": "2003-05-04",
        "s1": "2003-05-04",
        "tick": "2003-05-04",
    },
}


def _make_fake_probe(genesis: dict[str, dict[str, str]]):
    """Return a probe_single_day drop-in that answers from a local genesis dict."""
    def fake_probe(
        symbol: str,
        probe_date: date,
        *,
        timeframe: str = "tick",
        price_type: str = "bid",
    ) -> bool:
        sym = symbol.upper()
        genesis_str = genesis.get(sym, {}).get(timeframe)
        if genesis_str is None:
            return False
        genesis_day = date.fromisoformat(genesis_str)
        # Saturday = no data (mirrors real Dukascopy market-closed behaviour)
        return probe_date >= genesis_day and probe_date.weekday() != 5

    return fake_probe


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tests for the full discovery CLI pipeline (monkeypatching probe_single_day).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_discovery_cli_finds_reference_genesis_and_cleans_up(
    tmp_path: Path, monkeypatch, capsys
):
    import data_tools.dukascopy_discovery as discovery

    output_path = tmp_path / "dukascopy_genesis.json"
    # Pre-populate with a third symbol that must be preserved untouched.
    output_path.write_text(json.dumps({"GBPUSD": {"tick": "2005-01-01"}}))

    monkeypatch.setattr(discovery, "probe_single_day", _make_fake_probe(_FAKE_GENESIS))
    monkeypatch.setattr(
        sys,
        "argv",
        ["dukascopy_discovery", "--symbols", "EURUSD,XAUUSD", "--output", str(output_path)],
    )

    discovery.main()

    payload = json.loads(output_path.read_text())

    # Pre-existing symbol preserved
    assert payload["GBPUSD"] == {"tick": "2005-01-01"}

    # Discovered symbols have correct genesis dates
    assert payload["EURUSD"]["h4"] == "2003-05-04"
    assert payload["EURUSD"]["d1"] == "1999-01-01"
    assert payload["XAUUSD"]["tick"] == "2003-05-04"
    assert payload["XAUUSD"]["d1"] == "1999-01-01"

    captured = capsys.readouterr()
    assert "✅ EURUSD h4: 2003-05-04" in captured.out
    assert "✅ EURUSD tick: 2003-05-04" in captured.out
    assert "✅ XAUUSD tick: 2003-05-04" in captured.out


def test_discovery_cli_skips_known_timeframes_without_force(
    tmp_path: Path, monkeypatch
):
    import data_tools.dukascopy_discovery as discovery

    output_path = tmp_path / "dukascopy_genesis.json"
    output_path.write_text(json.dumps({"EURUSD": {"h4": "2003-05-04"}}))

    probe_calls: list[tuple[str, str]] = []
    original_probe = _make_fake_probe(_FAKE_GENESIS)

    def tracked_probe(symbol, probe_date, *, timeframe="tick", price_type="bid"):
        probe_calls.append((symbol, timeframe))
        return original_probe(symbol, probe_date, timeframe=timeframe, price_type=price_type)

    monkeypatch.setattr(discovery, "probe_single_day", tracked_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dukascopy_discovery",
            "--symbols", "EURUSD",
            "--timeframes", "h4,h1",
            "--output", str(output_path),
        ],
    )

    discovery.main()

    payload = json.loads(output_path.read_text())
    assert payload["EURUSD"]["h4"] == "2003-05-04"
    assert payload["EURUSD"]["h1"] == "2003-05-04"

    timeframes_probed = {tf for _, tf in probe_calls}
    assert "h1" in timeframes_probed   # h1 was unknown → must be probed
    assert "h4" not in timeframes_probed  # h4 was known → must be skipped


def test_discovery_cli_force_overwrites_known_timeframes(tmp_path: Path, monkeypatch):
    import data_tools.dukascopy_discovery as discovery

    output_path = tmp_path / "dukascopy_genesis.json"
    output_path.write_text(json.dumps({"EURUSD": {"h4": "1900-01-01"}}))

    monkeypatch.setattr(discovery, "probe_single_day", _make_fake_probe(_FAKE_GENESIS))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dukascopy_discovery",
            "--symbols", "EURUSD",
            "--timeframes", "h4",
            "--force",
            "--output", str(output_path),
        ],
    )

    discovery.main()

    payload = json.loads(output_path.read_text())
    assert payload == {"EURUSD": {"h4": "2003-05-04"}}


def test_main_persists_new_timeframes_before_interrupt(tmp_path: Path, monkeypatch):
    import data_tools.dukascopy_discovery as discovery

    output_path = tmp_path / "dukascopy_genesis.json"

    def fake_resolve(symbol: str):
        normalized = symbol.upper()
        return normalized, normalized.lower(), normalized.lower()

    def fake_discover(symbol: str, *, timeframe: str, **_: object) -> str:
        if timeframe == "h1":
            return "2003-05-04"
        raise KeyboardInterrupt("stop immediately")

    monkeypatch.setattr(discovery, "_resolve_symbol_and_instrument", fake_resolve)
    monkeypatch.setattr(discovery, "discover_symbol_genesis", fake_discover)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dukascopy_discovery",
            "--symbols",
            "EURUSD",
            "--timeframes",
            "h1,h4",
            "--output",
            str(output_path),
        ],
    )

    with pytest.raises(KeyboardInterrupt, match="stop immediately"):
        discovery.main()

    payload = json.loads(output_path.read_text())
    assert payload == {"EURUSD": {"h1": "2003-05-04"}}


def test_probe_csv_validity_and_current_period_probes_are_supported(tmp_path: Path):
    csv_path = tmp_path / "tick.csv"
    csv_path.write_text(
        "timestamp,askPrice,bidPrice\n"
        "1609372800438,1.22989,1.22986\n"
    )

    assert _is_valid_probe_csv(csv_path)
    assert _year_probe_date(date.today().year) <= date.today()
    assert _month_probe_date(date.today().year, date.today().month) <= date.today()

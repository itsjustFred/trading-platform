"""Tests for data_tools.dukascopy_discovery."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

from data_tools.dukascopy_discovery import _is_valid_probe_csv, _month_probe_date, _year_probe_date


def _make_executable(path: Path) -> None:
    current_mode = path.stat().st_mode
    path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_dukascopy_node(bin_dir: Path) -> None:
    script_path = bin_dir / "dukascopy-node"
    script_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import csv
            import sys
            from datetime import date
            from pathlib import Path

            GENESIS = {
                "eurusd": {
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
                "xauusd": {
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

            def arg(*flags: str) -> str:
                for flag in flags:
                    if flag in sys.argv:
                        idx = sys.argv.index(flag)
                        return sys.argv[idx + 1]
                raise ValueError(f"Missing arg for {flags}")

            instrument = arg("-i", "--instrument")
            timeframe = arg("-t", "--timeframe")
            from_iso = arg("-from", "--date-from")
            to_iso = arg("-to", "--date-to")
            out_dir = Path(arg("-dir", "--directory"))
            probe_day = date.fromisoformat(from_iso)
            to_day = date.fromisoformat(to_iso)
            instrument_genesis = GENESIS.get(instrument, {})
            genesis_day = date.fromisoformat(instrument_genesis.get(timeframe, "2100-01-01"))

            out_dir.mkdir(parents=True, exist_ok=True)
            csv_path = out_dir / f"{instrument}_{timeframe}_{from_iso}.csv"

            has_data = (to_day - probe_day).days == 1 and probe_day >= genesis_day and probe_day.weekday() != 5
            if has_data:
                with csv_path.open("w", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["timestamp", "bid", "ask"])
                    writer.writerow([f"{from_iso}T00:00:00Z", "1.1000", "1.1002"])
            else:
                csv_path.write_text("")
            """
        )
    )
    _make_executable(script_path)


def _write_fake_npx(bin_dir: Path) -> None:
    script_path = bin_dir / "npx"
    script_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            record_path = os.environ.get("FAKE_DUKA_NPX_RECORD")
            if record_path:
                path = Path(record_path)
                existing = json.loads(path.read_text()) if path.exists() else []
                existing.append({
                    "argv": sys.argv[1:],
                    "node_options": os.environ.get("NODE_OPTIONS"),
                })
                path.write_text(json.dumps(existing))

            args = sys.argv[1:]
            if args and args[0] == "dukascopy-node":
                args = args[1:]

            cmd = [str(Path(__file__).with_name("dukascopy-node")), *args]
            proc = subprocess.run(cmd, check=False)
            raise SystemExit(proc.returncode)
            """
        )
    )
    _make_executable(script_path)


def test_discovery_cli_finds_reference_genesis_and_cleans_up(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    bin_dir = tmp_path / "bin"
    home_dir = tmp_path / "home"
    probe_dir = tmp_path / "duka_probe"
    record_path = tmp_path / "npx_calls.json"
    bin_dir.mkdir()
    home_dir.mkdir()
    _write_fake_dukascopy_node(bin_dir)
    _write_fake_npx(bin_dir)

    existing_output_dir = home_dir / "data"
    existing_output_dir.mkdir()
    (existing_output_dir / "dukascopy_genesis.json").write_text(
        json.dumps({"GBPUSD": {"tick": "2005-01-01"}})
    )

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DUKASCOPY_DISCOVERY_PROBE_DIR"] = str(probe_dir)
    env["FAKE_DUKA_NPX_RECORD"] = str(record_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_tools.dukascopy_discovery",
            "--symbols",
            "EURUSD,XAUUSD",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    output_path = home_dir / "data" / "dukascopy_genesis.json"
    assert output_path.exists()

    payload = json.loads(output_path.read_text())
    assert payload == {
        "EURUSD": {
            "d1": "1999-01-01",
            "h1": "2003-05-04",
            "h4": "2003-05-04",
            "m1": "2003-05-04",
            "m15": "2003-05-04",
            "m30": "2003-05-04",
            "m5": "2003-05-04",
            "s1": "2003-05-04",
            "tick": "2003-05-04",
        },
        "GBPUSD": {
            "tick": "2005-01-01",
        },
        "XAUUSD": {
            "d1": "1999-01-01",
            "h1": "2003-05-04",
            "h4": "2003-05-04",
            "m1": "2003-05-04",
            "m15": "2003-05-04",
            "m30": "2003-05-04",
            "m5": "2003-05-04",
            "s1": "2003-05-04",
            "tick": "2003-05-04",
        },
    }
    assert "✅ EURUSD h4: 2003-05-04" in result.stdout
    assert "✅ EURUSD tick: 2003-05-04" in result.stdout
    assert "✅ XAUUSD tick: 2003-05-04" in result.stdout

    calls = json.loads(record_path.read_text())
    assert calls, "expected at least one npx probe call"
    assert all(call["node_options"] == "--max-old-space-size=4096" for call in calls)

    timeframes_seen: set[str] = set()
    for call in calls:
        argv = call["argv"]
        assert argv[0] == "dukascopy-node"
        from_iso = argv[argv.index("-from") + 1]
        to_iso = argv[argv.index("-to") + 1]
        timeframe = argv[argv.index("--timeframe") + 1]
        timeframes_seen.add(timeframe)
        assert date.fromisoformat(to_iso) - date.fromisoformat(from_iso) == timedelta(days=1)

    assert {"d1", "h4", "h1", "m30", "m15", "m5", "m1", "s1", "tick"} <= timeframes_seen
    assert not probe_dir.exists() or not any(probe_dir.iterdir())


def test_discovery_cli_skips_known_timeframes_without_force(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    bin_dir = tmp_path / "bin"
    home_dir = tmp_path / "home"
    probe_dir = tmp_path / "duka_probe"
    record_path = tmp_path / "npx_calls.json"
    bin_dir.mkdir()
    home_dir.mkdir()
    _write_fake_dukascopy_node(bin_dir)
    _write_fake_npx(bin_dir)

    existing_output_dir = home_dir / "data"
    existing_output_dir.mkdir()
    (existing_output_dir / "dukascopy_genesis.json").write_text(
        json.dumps({"EURUSD": {"h4": "2003-05-04"}})
    )

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DUKASCOPY_DISCOVERY_PROBE_DIR"] = str(probe_dir)
    env["FAKE_DUKA_NPX_RECORD"] = str(record_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_tools.dukascopy_discovery",
            "--symbols",
            "EURUSD",
            "--timeframes",
            "h4,h1",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "Skipping EURUSD h4, already known: 2003-05-04" in result.stdout
    assert "✅ EURUSD h1: 2003-05-04" in result.stdout

    payload = json.loads((home_dir / "data" / "dukascopy_genesis.json").read_text())
    assert payload == {"EURUSD": {"h1": "2003-05-04", "h4": "2003-05-04"}}

    calls = json.loads(record_path.read_text())
    timeframes_seen = {
        call["argv"][call["argv"].index("--timeframe") + 1]
        for call in calls
    }
    assert "h1" in timeframes_seen
    assert "h4" not in timeframes_seen


def test_discovery_cli_force_overwrites_known_timeframes(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    bin_dir = tmp_path / "bin"
    home_dir = tmp_path / "home"
    probe_dir = tmp_path / "duka_probe"
    record_path = tmp_path / "npx_calls.json"
    bin_dir.mkdir()
    home_dir.mkdir()
    _write_fake_dukascopy_node(bin_dir)
    _write_fake_npx(bin_dir)

    existing_output_dir = home_dir / "data"
    existing_output_dir.mkdir()
    (existing_output_dir / "dukascopy_genesis.json").write_text(
        json.dumps({"EURUSD": {"h4": "1900-01-01"}})
    )

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DUKASCOPY_DISCOVERY_PROBE_DIR"] = str(probe_dir)
    env["FAKE_DUKA_NPX_RECORD"] = str(record_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_tools.dukascopy_discovery",
            "--symbols",
            "EURUSD",
            "--timeframes",
            "h4",
            "--force",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "Skipping EURUSD h4" not in result.stdout
    assert "✅ EURUSD h4: 2003-05-04" in result.stdout

    payload = json.loads((home_dir / "data" / "dukascopy_genesis.json").read_text())
    assert payload == {"EURUSD": {"h4": "2003-05-04"}}

    calls = json.loads(record_path.read_text())
    timeframes_seen = [
        call["argv"][call["argv"].index("--timeframe") + 1]
        for call in calls
    ]
    assert "h4" in timeframes_seen


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


def test_tick_probe_csv_headers_and_current_period_probes_are_supported(tmp_path: Path):
    csv_path = tmp_path / "tick.csv"
    csv_path.write_text(
        "timestamp,askPrice,bidPrice\n"
        "1609372800438,1.22989,1.22986\n"
    )

    assert _is_valid_probe_csv(csv_path)
    assert _year_probe_date(date.today().year) <= date.today()
    assert _month_probe_date(date.today().year, date.today().month) <= date.today()

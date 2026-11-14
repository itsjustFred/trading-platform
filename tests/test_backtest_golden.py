from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

# Running this module as a script puts ``tests/`` first on ``sys.path`` and does
# not add the working directory, so ``shared`` would resolve from whatever the
# environment's editable install happens to point at.  A blessing path that
# mixes the tested checkout with a foreign one cannot be trusted, so the repo
# root of *this* file wins for both pytest and direct execution.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.atomic_io import write_text_atomically  # noqa: E402
from shared.models import (  # noqa: E402
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    Trade,
    TradeExit,
)

GOLDEN_PATH = ROOT / "tests" / "fixtures" / "backtest" / "native_cli_v1.json"
RESULT_ID = "backtest-golden-native-v1"
#: Two half-cent roundings (previous and current close) bound a cent-quantised return.
EQUITY_CENT_ROUNDING = 0.01
DYNAMIC_CREATED_AT = "<dynamic>"
DYNAMIC_PATH = "<dynamic-path>"
MAX_REPORTED_PATHS = 40
# Every golden run starts a fresh hermetic child: an empty ``PYTHONPYCACHEPREFIX``
# directory forces CPython to recompile the whole dependency set byte-for-byte
# (measured: ~30 s instead of ~10 s just for ``import scipy.stats``), the hermetic
# ``sitecustomize`` empties matplotlib's font cache, and the CLI itself then
# imports quantstats/seaborn.  One full child therefore costs ~1.5-2.5 minutes on
# a contended host, so the generation, comparison, blessing and failure paths
# each get a fixture-scoped budget above the fixed 120 s they used to share.
CLI_TIMEOUT_SECONDS = 120
# Blessing regenerates the fixture in-process (the golden CLI run plus stats) and
# the test reruns the CLI to show the bless is stable: two full children.
BLESS_TIMEOUT_SECONDS = 600
# ``pytest.raises`` still has to *finish* the child that is expected to fail, so
# the failure paths are not cheaper than the passing one.
FAILING_CLI_TIMEOUT_SECONDS = 300
SQLITE_SENTINEL_TIMEOUT_SECONDS = 300
HERMETIC_SITE_SOURCE = '''
import os
import socket
import sqlite3
import sqlite3.dbapi2 as sqlite_dbapi
import subprocess
from pathlib import Path

import dotenv


def blocked(*args, **kwargs):
    raise AssertionError("golden child attempted a blocked external operation")


def ignore_dotenv(*args, **kwargs):
    return False


def command_name(command):
    raw = str(command[0]) if command else ""
    name = os.path.basename(raw).lower()
    return name[:-4] if name.endswith(".exe") else name


def allowed_process(command):
    return command_name(command) in {
        "fc-cache",
        "fc-list",
        "fc-match",
        "system_profiler",
    }


def process_command(popenargs, kwargs):
    if "args" in kwargs:
        return kwargs["args"]
    return popenargs[0] if popenargs else ()


def reject_process_escape(command, popenargs, kwargs):
    positional_executable = len(popenargs) > 2 and popenargs[2] is not None
    if kwargs.get("shell") or kwargs.get("executable") is not None or positional_executable:
        blocked(command, *popenargs, **kwargs)
    if not allowed_process(command):
        blocked(command, *popenargs, **kwargs)


def guarded_run(*popenargs, **kwargs):
    command = process_command(popenargs, kwargs)
    reject_process_escape(command, popenargs, kwargs)
    return real_run(*popenargs, **kwargs)


real_run = subprocess.run
real_popen = subprocess.Popen


class GuardedPopen(real_popen):
    def __init__(self, *popenargs, **kwargs):
        command = process_command(popenargs, kwargs)
        reject_process_escape(command, popenargs, kwargs)
        super().__init__(*popenargs, **kwargs)


dotenv.load_dotenv = ignore_dotenv
socket.create_connection = blocked
socket.getaddrinfo = blocked
socket.gethostbyname = blocked
socket.gethostbyname_ex = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
sqlite3.connect = blocked
sqlite_dbapi.connect = blocked
subprocess.run = guarded_run
subprocess.Popen = GuardedPopen
for name in (
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "popen",
    "posix_spawn",
    "posix_spawnp",
    "spawnl",
    "spawnle",
    "spawnv",
    "spawnve",
    "startfile",
    "startfile2",
    "system",
):
    if hasattr(os, name):
        setattr(os, name, blocked)
Path(os.environ["GOLDEN_ISOLATION_MARKER"]).write_text("isolation-ok", encoding="utf-8")
'''
CLOSES = (
    110,
    108,
    106,
    104,
    102,
    100,
    98,
    96,
    95,
    94,
    93,
    92,
    91,
    90,
    89,
    88,
    87,
    86,
    85,
    84,
    83,
    82,
    81,
    80,
    120,
    118,
    116,
    114,
    112,
    110,
    108,
    106,
    105,
    104,
    103,
    102,
    101,
    100,
    99,
    98,
    97,
    96,
    95,
    94,
    93,
    92,
    91,
    90,
    118,
    120,
    122,
    123,
    145,
    155,
    165,
    180,
    *([180.0] * 16),
)
STRATEGY_SOURCE = '''
import pandas as pd

STRATEGY_META = {
    "name": "backtest_golden",
    "version": "1.0",
    "parameters": {},
}

def generate_signals(df, params):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[[2, 26, 50]] = True
    exits.iloc[[7, 31, 55]] = True
    indicators = {
        "direction": pd.Series("LONG", index=df.index),
        "stop_loss": pd.Series(50.0, index=df.index),
    }
    return entries, exits, indicators, []
'''


def _assert_utc_timestamp(value: str, label: str) -> None:
    """Check that a dynamic clock value is a real UTC instant, not a constant."""
    if value == DYNAMIC_CREATED_AT:
        return
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"{label} must be timezone-aware"
    assert parsed.utcoffset() == timedelta(0), f"{label} must be UTC"


def _canonicalize(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = deepcopy(payload)
    meta = canonical["meta"]
    _assert_utc_timestamp(meta["created_at"], "meta.created_at")
    meta["created_at"] = DYNAMIC_CREATED_AT

    # The universe provenance records when the symbol list was resolved and
    # repeats that instant verbatim inside its disclosure.  Both are wall-clock
    # values of the run that produced the fixture, so they are normalised here
    # rather than pinned; a golden that embeds its own generation time can only
    # ever pass once.
    provenance = (meta.get("data") or {}).get("universe_provenance")
    if isinstance(provenance, dict) and provenance.get("resolved_at") is not None:
        resolved_at = provenance["resolved_at"]
        _assert_utc_timestamp(resolved_at, "meta.data.universe_provenance.resolved_at")
        provenance["disclosure"] = str(provenance.get("disclosure") or "").replace(
            str(resolved_at), DYNAMIC_CREATED_AT
        )
        provenance["resolved_at"] = DYNAMIC_CREATED_AT

    # The execution context records the data directory the run read from, which
    # is the per-test temporary directory.  Canonicalising is idempotent: the
    # fixture's own placeholder is already the normal form.
    execution_context = (meta.get("data") or {}).get("execution_context")
    if isinstance(execution_context, dict) and execution_context.get("data_dir"):
        if execution_context["data_dir"] != DYNAMIC_PATH:
            assert Path(execution_context["data_dir"]).is_absolute(), (
                "execution_context.data_dir must be an absolute path"
            )
            execution_context["data_dir"] = DYNAMIC_PATH
    return canonical


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_json_equal(actual: Any, expected: Any, path: str = "$") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {type(actual).__name__}"
        assert set(actual) == set(expected), (
            f"{path}: missing={sorted(set(expected) - set(actual))}, "
            f"extra={sorted(set(actual) - set(expected))}"
        )
        for key, expected_value in expected.items():
            _assert_json_equal(actual[key], expected_value, f"{path}.{key}")
        return

    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected array, got {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
            _assert_json_equal(actual_value, expected_value, f"{path}[{index}]")
        return

    if isinstance(expected, float):
        assert type(actual) is float, f"{path}: expected float, got {type(actual).__name__}"
        assert math.isfinite(actual), f"{path}: actual is non-finite"
        assert math.isfinite(expected), f"{path}: expected is non-finite"
        if path.startswith("$.stats"):
            relative_tolerance, absolute_tolerance = 1e-10, 1e-8
        elif path.startswith("$.trades") or path.startswith("$.equity_curve"):
            relative_tolerance, absolute_tolerance = 1e-12, 1e-9
        else:
            relative_tolerance, absolute_tolerance = 0.0, 0.0
        difference = abs(actual - expected)
        allowed = absolute_tolerance + relative_tolerance * max(abs(actual), abs(expected))
        assert difference <= allowed, f"{path}: {actual!r} != {expected!r}"
        return

    assert type(actual) is type(expected), (
        f"{path}: type {type(actual).__name__} != {type(expected).__name__}"
    )
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def _build_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    parquet_path = (
        data_dir / "ohlcv" / "golden" / "equities" / "PARITY" / "1h.parquet"
    )
    parquet_path.parent.mkdir(parents=True)
    timestamps = pd.date_range("2024-01-01T00:00:00Z", periods=len(CLOSES), freq="h")
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": CLOSES,
            "high": [close + 1.0 for close in CLOSES],
            "low": [close - 1.0 for close in CLOSES],
            "close": CLOSES,
            "volume": [1000.0] * len(CLOSES),
        }
    ).to_parquet(parquet_path, index=False)

    strategy_path = tmp_path / "strategies" / "backtest_golden.py"
    strategy_path.parent.mkdir()
    strategy_path.write_text(STRATEGY_SOURCE, encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(HERMETIC_SITE_SOURCE, encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "child-tmp").mkdir()
    return data_dir, strategy_path, tmp_path / "backtest-golden.trades.json"


def _child_environment(tmp_path: Path, data_dir: Path) -> dict[str, str]:
    environment = {
        "DATA_DIR": str(data_dir),
        "GOLDEN_ISOLATION_MARKER": str(tmp_path / "isolation-ok"),
        "HOME": str(tmp_path),
        "LOG_DIR": str(tmp_path / "logs"),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
        "MPLIGNORE_SYSTEM_FONTS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(tmp_path),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        "PYTHONUTF8": "1",
        "SENTRY_ENABLED": "false",
        "SENTRY_RELEASE": "backtest-golden",
        "TMPDIR": str(tmp_path / "child-tmp"),
        "TZ": "UTC",
        "USERPROFILE": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    for key in (
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    for key in (
        "MPLCONFIGDIR",
        "PYTHONPYCACHEPREFIX",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    return environment


def _run_child_probe(environment: dict[str, str], source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _assert_isolation_tripwires(
    environment: dict[str, str],
    marker: Path,
) -> None:
    marker.unlink(missing_ok=True)
    loaded = _run_child_probe(
        environment,
        "import os; from pathlib import Path; "
        "assert Path(os.environ['GOLDEN_ISOLATION_MARKER']).read_text("
        "'utf-8') == 'isolation-ok'",
    )
    assert loaded.returncode == 0, loaded.stderr
    assert marker.read_text(encoding="utf-8") == "isolation-ok"

    probe_source = (
        "import shutil, socket, sqlite3, sqlite3.dbapi2 as sqlite_dbapi, subprocess, sys\n"
        "checks = (\n"
        "    lambda: socket.create_connection(('127.0.0.1', 9)),\n"
        "    lambda: socket.getaddrinfo('localhost', 80),\n"
        "    lambda: sqlite3.connect(':memory:'),\n"
        "    lambda: sqlite_dbapi.connect(':memory:'),\n"
        "    lambda: subprocess.run(['git', '--version']),\n"
        "    lambda: subprocess.run(['child-process']),\n"
        "    lambda: subprocess.run(args=['child-process']),\n"
        "    lambda: subprocess.Popen(['child-process']),\n"
        "    lambda: subprocess.Popen(args=['child-process']),\n"
        "    lambda: subprocess.run(['fc-list', '--help'], shell=True),\n"
        "    lambda: subprocess.run(['fc-list', '--help'], executable=sys.executable),\n"
        ")\n"
        "for check in checks:\n"
        "    try:\n"
        "        check()\n"
        "    except AssertionError as exc:\n"
        "        assert 'blocked external operation' in str(exc)\n"
        "    else:\n"
        "        raise AssertionError('isolation tripwire was bypassed')\n"
        "if shutil.which('fc-list'):\n"
        "    assert subprocess.run(args=['fc-list', '--help']).returncode == 0\n"
        "    process = subprocess.Popen(\n"
        "        args=['fc-list', '--help'], stdout=subprocess.PIPE, stderr=subprocess.PIPE\n"
        "    )\n"
        "    assert process.wait(timeout=10) == 0\n"
        "print('isolation-tripwires-ok')\n"
    )
    tripwires = _run_child_probe(environment, probe_source)
    assert tripwires.returncode == 0, tripwires.stderr
    assert "isolation-tripwires-ok" in tripwires.stdout
    marker.unlink()


def _run_native_cli(tmp_path: Path, *, timeout: float = CLI_TIMEOUT_SECONDS) -> BacktestResult:
    data_dir, strategy_path, output_path = _build_fixture(tmp_path)
    environment = _child_environment(tmp_path, data_dir)
    marker = Path(environment["GOLDEN_ISOLATION_MARKER"])
    _assert_isolation_tripwires(environment, marker)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "runner.cli",
            "run",
            "--engine",
            "native",
            "--strategy",
            str(strategy_path),
            "--symbols",
            "PARITY",
            "--timeframe",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-03",
            "--capital",
            "100000",
            "--risk-pct",
            "0.01",
            "--max-position-exposure-pct",
            "1.0",
            "--max-leverage",
            "1.0",
            "--fee-rate",
            "0.0005",
            "--fee-flat-per-trade",
            "1.0",
            "--intrabar-resolution",
            "main_timeframe",
            "--data-dir",
            str(data_dir),
            "--output",
            str(output_path),
            "--result-id",
            RESULT_ID,
            "--skip-sqlite",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, (
        f"golden CLI failed with {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert marker.read_text(encoding="utf-8") == "isolation-ok"
    assert not (data_dir / "backtests.db").exists()
    raw = json.loads(output_path.read_text(encoding="utf-8"))
    result = BacktestResult.load(output_path)
    _assert_json_equal(_canonicalize(raw), _canonicalize(result.to_dict()))
    return result


def _oracle_mtm_equities(result: BacktestResult) -> list[float]:
    """Independent per-bar mark-to-market equity from the raw bars and the ledger."""
    bar_times = pd.date_range("2024-01-01T00:00:00Z", periods=len(CLOSES), freq="h")
    equities = [100_000.0]
    for stamp, close in zip(bar_times, CLOSES, strict=True):
        moment = stamp.to_pydatetime()
        value = 100_000.0
        for trade in result.trades:
            exit_time = _utc_timestamp(trade.exits[-1].time)
            if _utc_timestamp(trade.entry_time) > moment:
                continue
            if exit_time <= moment:
                value += trade.net_pnl
            else:
                value += (close - trade.entry_price) * trade.quantity
        equities.append(value)
    return equities


def _assert_native_cli_semantics(result: BacktestResult) -> None:
    assert result.meta.id == RESULT_ID
    assert result.meta.engine == "native"
    assert result.meta.strategies == ["backtest_golden"]
    assert result.meta.total_bars == 72
    assert result.meta.duration_days == 2
    assert result.meta.parameters == {
        "fee_rate": 0.0005,
        "fee_flat_per_trade": 1.0,
    }
    assert [trade.id for trade in result.trades] == [1, 2, 3]
    assert [trade.entry_time for trade in result.trades] == [
        "2024-01-01T03:00:00+00:00",
        "2024-01-02T03:00:00+00:00",
        "2024-01-03T03:00:00+00:00",
    ]
    assert [trade.exits[-1].time for trade in result.trades] == [
        "2024-01-01T07:00:00+00:00",
        "2024-01-02T07:00:00+00:00",
        "2024-01-03T07:00:00+00:00",
    ]
    assert [trade.entry_price for trade in result.trades] == [104.0, 114.0, 123.0]
    assert [trade.exits[-1].price for trade in result.trades] == [96.0, 106.0, 180.0]
    assert result.stats["total_trades"] == 3
    assert result.stats["winners"] == 1
    assert result.stats["losers"] == 2
    assert result.stats["profit_factor"] is not None
    assert result.stats["daily_return_count"] == 3
    # The persisted curve is MARK-TO-MARKET at bar resolution (hourly here), not
    # one realized point per day: drawdown / Sharpe must see the paper loss of an
    # open position. Its daily compaction is `daily_returns` (checked below).
    assert result.equity_curve[0].time == "2024-01-01T00:00:00+00:00"
    assert result.equity_curve[0].equity == 100_000.0
    curve_times = [_utc_timestamp(point.time) for point in result.equity_curve]
    assert curve_times == sorted(set(curve_times)), "curve must be strictly increasing"
    assert len(result.equity_curve) > result.stats["daily_return_count"] + 1
    assert result.meta.data["equity_curve_basis"] == "mark_to_market"

    equity = 100_000.0
    peak_equity = equity
    max_drawdown = 0.0
    expected_daily_pnl: dict[str, float] = {}
    window_start = _utc_timestamp(f"{result.meta.backtest_start}T00:00:00+00:00")
    window_end = _utc_timestamp(f"{result.meta.backtest_end}T23:59:59.999999+00:00")
    for trade in result.trades:
        assert trade.exits
        entry_time = _utc_timestamp(trade.entry_time)
        previous_exit_time = entry_time
        for exit_leg in trade.exits:
            assert exit_leg.quantity > 0.0
            assert exit_leg.pnl is not None
            assert exit_leg.reason
            exit_time = _utc_timestamp(exit_leg.time)
            assert entry_time <= exit_time <= window_end
            assert previous_exit_time <= exit_time
            expected_leg_pnl = (
                (exit_leg.price - trade.entry_price)
                * exit_leg.quantity
                * trade.direction_sign
            )
            assert exit_leg.pnl == pytest.approx(expected_leg_pnl, rel=1e-12, abs=1e-9)
            previous_exit_time = exit_time
        assert window_start <= entry_time <= window_end
        exit_ = trade.exits[-1]
        assert sum(item.quantity for item in trade.exits) == pytest.approx(trade.quantity)
        assert sum(item.pnl or 0.0 for item in trade.exits) == pytest.approx(trade.pnl)
        expected_quantity = equity * 0.01 / (trade.entry_price - 50.0)
        expected_gross = (exit_.price - trade.entry_price) * expected_quantity
        expected_fees = (trade.entry_price + exit_.price) * expected_quantity * 0.0005 + 1.0
        expected_net = expected_gross - expected_fees
        exit_day = _utc_timestamp(exit_.time).date().isoformat()
        expected_daily_pnl[exit_day] = expected_daily_pnl.get(exit_day, 0.0) + expected_net
        assert trade.quantity == pytest.approx(expected_quantity, rel=1e-12, abs=1e-9)
        assert trade.pnl == pytest.approx(expected_gross, rel=1e-12, abs=1e-9)
        assert trade.fees == pytest.approx(expected_fees, rel=1e-12, abs=1e-9)
        assert trade.net_pnl == pytest.approx(expected_net, rel=1e-12, abs=1e-9)
        equity += expected_net
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)

    last_exit_by_day: dict[str, datetime] = {}
    for trade in result.trades:
        exit_time = _utc_timestamp(trade.exits[-1].time)
        exit_day = exit_time.date().isoformat()
        last_exit_by_day[exit_day] = max(last_exit_by_day.get(exit_day, exit_time), exit_time)

    # The curve is a reporting artifact quoted to the cent, while the ledger
    # below keeps full precision.  Pin the rounding itself instead of allowing a
    # slack tolerance: an unrounded or double-rounded close must fail here.
    assert result.equity_curve[-1].equity == round(equity, 2)
    assert abs(result.equity_curve[-1].equity - equity) <= 0.005
    daily_rows = result.stats["daily_returns"]
    assert [row["date"] for row in daily_rows] == sorted(expected_daily_pnl)
    assert set(last_exit_by_day) <= {row["date"] for row in daily_rows}
    running_equity = 100_000.0
    for index, row in enumerate(daily_rows, start=1):
        day_pnl = expected_daily_pnl[row["date"]]
        expected_close = running_equity + day_pnl
        # The day's close is the LAST curve observation of that UTC day (every
        # golden trade is flat by day end, so close equity == realized ledger).
        day_points = [
            point
            for point in result.equity_curve
            if _utc_timestamp(point.time).date().isoformat() == row["date"]
        ]
        assert day_points, f"curve has no observation on {row['date']}"
        curve_point = day_points[-1]
        last_exit = last_exit_by_day.get(row["date"])
        if last_exit is not None:
            assert _utc_timestamp(curve_point.time) >= last_exit
        assert curve_point.equity == round(expected_close, 2)
        assert abs(curve_point.equity - expected_close) <= 0.005
        # The reported equity is cent-quantised so that the two published views
        # of the same instant agree; the return still uses the exact balance.
        assert row["equity"] == round(expected_close, 2)
        assert row["equity"] == curve_point.equity
        # The published return is derived from the cent-quantised equity path
        # (the same mark-to-market curve the stats use), so it can differ from
        # the exact ledger ratio by at most the two half-cent roundings.
        assert row["return"] == pytest.approx(
            day_pnl / running_equity,
            rel=1e-12,
            abs=EQUITY_CENT_ROUNDING / running_equity,
        )
        running_equity = expected_close
    assert running_equity == pytest.approx(equity, rel=1e-12, abs=1e-9)
    assert result.stats["total_pnl"] == pytest.approx(
        sum(trade.net_pnl or 0.0 for trade in result.trades),
        abs=0.01,
    )
    assert result.stats["total_pnl"] != pytest.approx(
        sum(trade.pnl for trade in result.trades),
        abs=0.01,
    )
    expected_total_return = equity / 100_000.0 - 1.0
    assert result.stats["total_return"] == pytest.approx(expected_total_return, abs=1e-6)
    # Headline drawdown is measured on the mark-to-market equity, rebuilt here
    # from the raw bars with an independent per-bar loop (no engine code).
    mtm_equities = _oracle_mtm_equities(result)
    mtm_peak = mtm_equities[0]
    mtm_drawdown = 0.0
    for value in mtm_equities:
        mtm_peak = max(mtm_peak, value)
        mtm_drawdown = max(mtm_drawdown, (mtm_peak - value) / mtm_peak)
    # Mark-to-market can only be as deep or deeper than the balance drawdown
    # (this fixture's losers fall monotonically into their exits, so here the
    # two coincide; intra-trade drawdown is pinned by tests/test_mtm_equity.py).
    assert mtm_drawdown >= max_drawdown - 1e-12
    assert result.stats["max_drawdown_pct"] == pytest.approx(-mtm_drawdown, abs=1e-6)
    # The balance (closed-trade) drawdown stays available under its own name.
    assert result.stats["max_drawdown_realized_pct"] == pytest.approx(
        -max_drawdown, abs=1e-6
    )
    curve_peak = result.equity_curve[0].equity
    duration = longest = 0
    for point in result.equity_curve:
        if point.equity >= curve_peak:
            curve_peak, duration = point.equity, 0
        else:
            duration += 1
            longest = max(longest, duration)
    assert result.stats["max_drawdown_duration"] == longest
    compounded_returns = 1.0
    for daily_return in result.stats["daily_returns"]:
        compounded_returns *= 1.0 + daily_return["return"]
    # Cent-quantised daily returns telescope to the rounded final equity.
    assert compounded_returns - 1.0 == pytest.approx(
        expected_total_return, rel=1e-12, abs=EQUITY_CENT_ROUNDING / 2 / 100_000.0
    )
    # CAGR annualizes over the configured calendar window, not over the span of
    # whatever curve the engine happened to keep.
    elapsed_days = (window_end - window_start).total_seconds() / 86_400.0
    expected_cagr = (result.equity_curve[-1].equity / 100_000.0) ** (365.25 / elapsed_days) - 1.0
    assert result.stats["cagr"] == pytest.approx(expected_cagr, abs=1e-6)


def _economic_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    """The economic identity of a golden payload, at reviewable precision.

    Money is compared to the cent and ratios to six decimals.  Representation
    noise below those thresholds (float last bits, the cent rounding of a curve
    close) must not require an economic sign-off, while a real shift in
    quantities, prices, fees or PnL must.
    """
    trades = payload.get("trades") or []
    curve = payload.get("equity_curve") or []
    stats = payload.get("stats") or {}

    def money(value: Any) -> float | None:
        return None if value is None else round(float(value), 2)

    def ratio(value: Any) -> float | None:
        return None if value is None else round(float(value), 6)

    def total(values: list[Any]) -> float | None:
        present = [float(value) for value in values if value is not None]
        return money(sum(present)) if present else None

    return {
        "initial_capital": money((payload.get("meta") or {}).get("initial_capital")),
        "start_equity": money(curve[0]["equity"]) if curve else None,
        "end_equity": money(curve[-1]["equity"]) if curve else None,
        "curve_points": len(curve),
        "total_trades": len(trades),
        "total_pnl": money(stats.get("total_pnl")),
        "total_return": ratio(stats.get("total_return")),
        "cagr": ratio(stats.get("cagr")),
        "max_drawdown_pct": ratio(stats.get("max_drawdown_pct")),
        "sharpe_ratio": ratio(stats.get("sharpe_ratio")),
        "sortino_ratio": ratio(stats.get("sortino_ratio")),
        "calmar_ratio": ratio(stats.get("calmar_ratio")),
        "profit_factor": ratio(stats.get("profit_factor")),
        "win_rate": ratio(stats.get("win_rate")),
        "winners": stats.get("winners"),
        "losers": stats.get("losers"),
        "daily_return_count": stats.get("daily_return_count"),
        "net_pnl_sum": total([trade.get("net_pnl") for trade in trades]),
        "gross_pnl_sum": total([trade.get("pnl") for trade in trades]),
        "fee_sum": total([trade.get("fees") for trade in trades]),
        "entry_prices": [money(trade.get("entry_price")) for trade in trades],
        "exit_prices": [
            money(trade["exits"][-1]["price"]) for trade in trades if trade.get("exits")
        ],
    }


def _flatten(payload: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
    else:
        flat[prefix] = payload
    return flat


def _describe_golden_change(
    previous: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[list[str], dict[str, tuple[Any, Any]]]:
    """Human-readable before/after report for a regeneration.

    Returns the report lines and the changed economic values.  The report is
    printed on every regeneration so that the diff is reviewable in the terminal
    and in the task log instead of only in the fixture file.
    """
    old_flat = _flatten(previous)
    new_flat = _flatten(candidate)
    added = sorted(set(new_flat) - set(old_flat))
    removed = sorted(set(old_flat) - set(new_flat))
    changed = sorted(key for key in set(old_flat) & set(new_flat) if old_flat[key] != new_flat[key])

    lines = [
        f"golden change summary for {GOLDEN_PATH}:",
        f"  fields added: {len(added)}",
        f"  fields removed: {len(removed)}",
        f"  fields changed: {len(changed)}",
    ]
    for key in added[:MAX_REPORTED_PATHS]:
        lines.append(f"    + {key} = {new_flat[key]!r}")
    for key in removed[:MAX_REPORTED_PATHS]:
        lines.append(f"    - {key} = {old_flat[key]!r}")
    for key in changed[:MAX_REPORTED_PATHS]:
        lines.append(f"    ~ {key}: {old_flat[key]!r} -> {new_flat[key]!r}")
    if len(changed) > MAX_REPORTED_PATHS:
        lines.append(f"    ... {len(changed) - MAX_REPORTED_PATHS} further changed fields")

    old_ledger = _economic_ledger(previous)
    new_ledger = _economic_ledger(candidate)
    economic = {
        key: (old_ledger[key], new_ledger[key])
        for key in old_ledger
        if old_ledger[key] != new_ledger[key]
    }
    lines.append(f"  economic values changed: {len(economic)}")
    for key, (before, after) in economic.items():
        lines.append(f"    ! {key}: {before!r} -> {after!r}")
    return lines, economic


def _write_golden(canonical: dict[str, Any]) -> None:
    write_text_atomically(
        GOLDEN_PATH,
        json.dumps(
            canonical,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
    )


def test_native_cli_pipeline_matches_golden(tmp_path: Path) -> None:
    if not GOLDEN_PATH.is_file():
        pytest.fail(
            f"Missing {GOLDEN_PATH}; regenerate with "
            "python tests/test_backtest_golden.py --update-golden"
        )
    result = _run_native_cli(tmp_path)
    _assert_native_cli_semantics(result)
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    BacktestResult.from_dict(expected)
    _assert_json_equal(_canonicalize(result.to_dict()), _canonicalize(expected))


# --- In-process oracle for the windowed / engine-curve daily-equity paths -------
#
# The CLI golden above starts at the first bar with no earlier history and
# adopts the native engine's own dense curve, so it cannot see two things the
# accounting depends on: a window whose starting equity is DERIVED from trades
# that closed before it, and a sparse engine curve compacted by the engine-curve
# path. This oracle pins both with hand-computed numbers (no code under test is
# used to produce an expectation) and cross-checks the two daily builders against
# each other.

ORACLE_INITIAL_CAPITAL = 100_000.0
# (exit time, net PnL, fees). The first trade closes before the window and is the
# only source of the window's starting equity: 100_000 + 1_500 = 101_500.
ORACLE_PRIOR_TRADE = ("2024-01-10T15:00:00+00:00", 1_500.0, 10.0)
ORACLE_START_EQUITY = 101_500.0
ORACLE_DAYS = [f"2024-02-{day:02d}" for day in range(1, 11)]
# Window trades whose first day carries a move (the common case), and the same
# economics shifted so that the FIRST day of the window is flat.
ORACLE_TRADES_DAY_ONE_MOVE = (
    ("2024-02-01T12:00:00+00:00", 609.0, 1.0),
    ("2024-02-04T12:00:00+00:00", -812.0, 1.0),
    ("2024-02-07T12:00:00+00:00", 406.0, 1.0),
)
ORACLE_TRADES_FLAT_FIRST_DAY = (
    ("2024-02-03T12:00:00+00:00", 609.0, 1.0),
    ("2024-02-04T12:00:00+00:00", -812.0, 1.0),
    ("2024-02-07T12:00:00+00:00", 406.0, 1.0),
)
ORACLE_SCENARIOS = {
    "day_one_move_intraday_stamps": (ORACLE_TRADES_DAY_ONE_MOVE, "T12:00:00+00:00"),
    # An engine that stamps a close at the start of the day it closes: the first
    # curve point then sits at the window-start midnight AND differs from the
    # starting equity.
    "day_one_move_midnight_stamps": (ORACLE_TRADES_DAY_ONE_MOVE, "T00:00:00+00:00"),
    "flat_first_day": (ORACLE_TRADES_FLAT_FIRST_DAY, "T12:00:00+00:00"),
}


def _oracle_trade(trade_id: int, exit_time: str, net: float, fees: float) -> Trade:
    gross = net + fees
    trade = Trade(
        id=trade_id,
        symbol="PARITY",
        direction="LONG",
        entry_time="2024-01-02T09:00:00+00:00",
        entry_price=100.0,
        quantity=10.0,
        exits=[TradeExit(time=exit_time, price=101.0, quantity=10.0, pnl=gross)],
        pnl=gross,
    )
    trade.fees = fees
    trade.net_pnl = net
    return trade


def _oracle_result(
    window_trades: tuple[tuple[str, float, float], ...], engine_curve: list[EquityPoint]
) -> BacktestResult:
    trades = [_oracle_trade(1, *ORACLE_PRIOR_TRADE)] + [
        _oracle_trade(index, *row) for index, row in enumerate(window_trades, start=2)
    ]
    return BacktestResult(
        meta=BacktestMeta(
            id="oracle-window",
            created_at="2024-03-01T00:00:00+00:00",
            engine="native",
            strategies=["oracle"],
            initial_capital=ORACLE_INITIAL_CAPITAL,
            backtest_start="2024-02-01",
            backtest_end="2024-02-10",
        ),
        trades=trades,
        equity_curve=engine_curve,
    )


def _oracle_engine_curve(
    window_trades: tuple[tuple[str, float, float], ...], stamp: str
) -> list[EquityPoint]:
    """A sparse engine curve (three in-window points over ten days).

    Too thin for the density gate, so the reconciling engine-curve path adopts
    it. The first point is the earlier trade's close, which reconciles the
    starting equity.
    """
    points = [EquityPoint(time=ORACLE_PRIOR_TRADE[0], equity=ORACLE_START_EQUITY)]
    equity = ORACLE_START_EQUITY
    for exit_time, net, _fees in window_trades:
        equity += net
        points.append(EquityPoint(time=exit_time[:10] + stamp, equity=equity))
    return points


def _expected_oracle_returns(
    window_trades: tuple[tuple[str, float, float], ...],
) -> list[tuple[str, float, float]]:
    """(date, return, cent-quantised close) per window day, from first principles."""
    pnl_by_day = {row[0][:10]: row[1] for row in window_trades}
    equity = ORACLE_START_EQUITY
    rows: list[tuple[str, float, float]] = []
    for day in ORACLE_DAYS:
        pnl = pnl_by_day.get(day, 0.0)
        rows.append((day, pnl / equity, round(equity + pnl, 2)))
        equity += pnl
    return rows


def _assert_oracle_series(
    result: BacktestResult, window_trades: tuple[tuple[str, float, float], ...]
) -> None:
    expected = _expected_oracle_returns(window_trades)
    # One return per calendar day of the ten-day window, the first day included.
    assert result.stats["daily_return_count"] == len(ORACLE_DAYS) == 10
    assert [row["date"] for row in result.stats["daily_returns"]] == ORACLE_DAYS
    for row, (_day, expected_return, expected_close) in zip(
        result.stats["daily_returns"], expected
    ):
        assert row["return"] == pytest.approx(expected_return, rel=1e-12, abs=1e-15)
        assert row["equity"] == expected_close
    final = ORACLE_START_EQUITY + sum(row[1] for row in window_trades)
    compounded = ORACLE_START_EQUITY
    for row in result.stats["daily_returns"]:
        compounded *= 1.0 + row["return"]
    assert compounded == pytest.approx(final, rel=1e-12)
    curve = result.equity_curve
    assert curve[0].equity == ORACLE_START_EQUITY
    assert curve[-1].equity == final
    times = [point.time for point in curve]
    assert len(set(times)) == len(times), "no timestamp may be published twice"
    assert times == sorted(times)


@pytest.mark.parametrize("scenario", sorted(ORACLE_SCENARIOS))
def test_oracle_engine_curve_path_from_a_start_equity_derived_from_earlier_trades(
    scenario: str,
) -> None:
    from runner.daily_equity import attach_daily_equity

    window_trades, stamp = ORACLE_SCENARIOS[scenario]
    result = _oracle_result(window_trades, _oracle_engine_curve(window_trades, stamp))

    attach_daily_equity(result, start="2024-02-01", end="2024-02-10")

    # The earlier engine point reconciles with the earlier net ledger, so the
    # base is engine-confirmed: 100_000 + 1_500 (net of the 10 in fees).
    assert result.meta.data["window_start_basis"] == "engine"
    _assert_oracle_series(result, window_trades)
    # The engine path was taken (not the calendar fill): it published the drift
    # it accepted, which is zero for this exactly reconciling curve.
    reconciliation = result.meta.data["engine_curve_reconciliation"]
    assert reconciliation["drift"] == pytest.approx(0.0, abs=1e-9)
    assert reconciliation["expected_final_equity"] == pytest.approx(
        ORACLE_START_EQUITY + sum(row[1] for row in window_trades)
    )
    assert result.meta.data["equity_frequency"] == "daily"
    assert result.equity_evidence_status == "available"
    assert result.to_dict()["equity_evidence_status"] == "available"


@pytest.mark.parametrize("scenario", sorted(ORACLE_SCENARIOS))
def test_oracle_calendar_path_agrees_with_the_engine_curve_path(scenario: str) -> None:
    """The same economics, with no engine curve to adopt, give the same series."""
    from runner.daily_equity import attach_daily_equity
    from runner.stats import compute_stats

    window_trades, stamp = ORACLE_SCENARIOS[scenario]
    engine = _oracle_result(window_trades, _oracle_engine_curve(window_trades, stamp))
    attach_daily_equity(engine, start="2024-02-01", end="2024-02-10")
    # No engine point at all: the base comes from the ledger alone, is labelled
    # so, and the numbers are identical.
    calendar = _oracle_result(window_trades, [])
    attach_daily_equity(calendar, start="2024-02-01", end="2024-02-10")

    assert calendar.meta.data["window_start_basis"] == "ledger_only"
    assert "engine_curve_reconciliation" not in calendar.meta.data
    _assert_oracle_series(calendar, window_trades)
    for engine_row, calendar_row in zip(
        engine.stats["daily_returns"], calendar.stats["daily_returns"]
    ):
        assert engine_row["date"] == calendar_row["date"]
        assert engine_row["return"] == pytest.approx(calendar_row["return"], abs=1e-15)
        assert engine_row["equity"] == calendar_row["equity"]

    ratios = {
        name: compute_stats(
            trades=result.trades,
            initial_capital=ORACLE_START_EQUITY,
            equity_curve=result.equity_curve,
            backtest_start="2024-02-01",
            backtest_end="2024-02-10",
        )
        for name, result in (("engine", engine), ("calendar", calendar))
    }
    for key in ("sharpe_ratio", "sortino_ratio", "psr", "p_value", "total_return"):
        assert ratios["engine"][key] == pytest.approx(ratios["calendar"][key], abs=1e-9), key
    assert ratios["engine"]["duplicate_equity_timestamps"] == 0


def main(argv: list[str] | None = None) -> int:
    """Rewrite the golden fixture from the current pipeline output.

    This is the only path that writes the fixture; the test never blesses
    itself.  A regeneration additionally has to be reviewed: the economic
    ledger is printed and a change to it needs a second, explicit flag.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-golden",
        action="store_true",
        help="rewrite the golden fixture from the current pipeline output",
    )
    parser.add_argument(
        "--accept-economic-change",
        action="store_true",
        help="confirm that the printed economic ledger change is intended",
    )
    args = parser.parse_args(argv)
    if not args.update_golden:
        parser.error("--update-golden is required; the golden is never rewritten implicitly")

    previous = (
        json.loads(GOLDEN_PATH.read_text(encoding="utf-8")) if GOLDEN_PATH.is_file() else None
    )
    with tempfile.TemporaryDirectory(prefix="backtest-golden-") as directory:
        result = _run_native_cli(Path(directory), timeout=BLESS_TIMEOUT_SECONDS)
        _assert_native_cli_semantics(result)
        candidate = _canonicalize(result.to_dict())

    if previous is not None:
        lines, economic = _describe_golden_change(_canonicalize(previous), candidate)
        print("\n".join(lines))
        if economic and not args.accept_economic_change:
            parser.error(
                "the regeneration changes economic values; review the ledger above and "
                "re-run with --accept-economic-change if the new economics are intended"
            )
    else:
        print(f"creating new golden fixture at {GOLDEN_PATH}")

    _write_golden(candidate)
    print(f"wrote {GOLDEN_PATH}; review the fixture diff before committing it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The operator scripts write exactly their own files - never a path handed in.

Both writers used to take the target path as a parameter. The callers passed module constants, so
the value was never wrong, but a path parameter is indistinguishable from caller input to a reader
(and to a static analyzer) - which is why the two sinks kept being reported as path traversal. The
writers now own their targets, and these cases pin what that means:

- the state and profile files are the ones the module constants name, and nothing else is created,
- the backtest rewrite touches only files it discovered under the directory it was given.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"script_{path.stem}", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` resolves its own module through `sys.modules`, so a
    # module that is executed without being registered cannot define one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harvester(tmp_path: Path, monkeypatch):
    module = _load("adaptive_harvester.py")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state" / "harvester_state.json")
    monkeypatch.setattr(module, "PROFILE_PATH", tmp_path / "profiles" / "downloader_profiles.json")
    return module


def test_the_state_writer_writes_its_own_file(harvester, tmp_path: Path) -> None:
    harvester._save_state({"version": 1, "jobs": {}})

    target = tmp_path / "state" / "harvester_state.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1, "jobs": {}}
    assert not list(target.parent.glob("*.tmp")), "the atomic write must not leave its temp file"


def test_the_profile_writer_writes_its_own_file(harvester, tmp_path: Path) -> None:
    harvester._save_profiles({"XAUUSD": {"batch_size": 5}})

    target = tmp_path / "profiles" / "downloader_profiles.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {"XAUUSD": {"batch_size": 5}}


def test_the_state_writer_creates_only_its_own_directory(harvester, tmp_path: Path) -> None:
    harvester._save_state({"version": 1, "jobs": {}})

    created = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert created == ["state/harvester_state.json"]


def _write_backtest(root: Path) -> Path:
    meta = BacktestMeta(
        id="bt-1",
        created_at="2024-01-03T00:00:00+00:00",
        engine="native",
        strategies=["demo"],
        backtest_start="2024-01-01",
        backtest_end="2024-01-03",
        initial_capital=100_000.0,
    )
    trade = Trade(
        id=1,
        symbol="EURUSD",
        direction="LONG",
        entry_time="2024-01-01T00:00:00+00:00",
        entry_price=1.1,
        quantity=1.0,
        pnl=100.0,
        fees=0.0,
        exits=[TradeExit(time="2024-01-02T00:00:00+00:00", price=1.2, quantity=1.0, pnl=100.0)],
    )
    result = BacktestResult(
        meta=meta,
        trades=[trade],
        equity_curve=[
            EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100_000.0),
            EquityPoint(time="2024-01-03T00:00:00+00:00", equity=100_100.0),
        ],
        stats={},
    )
    target = root / "bt-1.trades.json"
    target.write_text(json.dumps(result.to_dict()), encoding="utf-8")
    return target


def test_the_backtest_rewrite_rewrites_the_file_it_read(tmp_path: Path) -> None:
    module = _load("recalculate_backtest_stats.py")
    target = _write_backtest(tmp_path)

    assert module._recalculate_json_backtests(tmp_path, dry_run=False) == (1, 1, 0)

    rewritten = json.loads(target.read_text(encoding="utf-8"))
    assert rewritten["stats"]["cagr"] is not None
    assert not list(tmp_path.glob("*.tmp"))


def test_the_backtest_rewrite_touches_nothing_outside_the_directory(tmp_path: Path) -> None:
    module = _load("recalculate_backtest_stats.py")
    backtests = tmp_path / "backtests"
    backtests.mkdir()
    _write_backtest(backtests)
    outside = tmp_path / "outside.trades.json"
    outside.write_text("{}", encoding="utf-8")

    module._recalculate_json_backtests(backtests, dry_run=False)

    assert outside.read_text(encoding="utf-8") == "{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backtests", "outside.trades.json"]

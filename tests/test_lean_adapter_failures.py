from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner.engines.base import BacktestConfig
from runner.engines.lean_adapter import LeanAdapter


def _config(tmp_path: Path) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="lean_demo",
        symbols=["SPY"],
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-02-01",
        initial_capital=100_000.0,
    )


LEAN_FIXTURE = {
    "orders": {
        "1": {
            "id": 1,
            "time": "2024-01-05T15:00:00Z",
            "quantity": 10,
            "price": 100.0,
            "tag": "Entry",
            "symbol": {"value": "SPY"},
        },
        "2": {
            "id": 2,
            "time": "2024-01-10T15:00:00Z",
            "quantity": -10,
            "price": 105.0,
            "tag": "Exit",
            "symbol": {"value": "SPY"},
        },
    },
    "profitLoss": {"2024-01-10T15:00:00Z": 50.0},
}


def test_lean_adapter_imports_cleanly():
    import runner.engines.lean_adapter as module

    assert hasattr(module, "LeanAdapter")


def test_lean_adapter_runs_end_to_end_from_result_fixture(tmp_path):
    fixture = tmp_path / "lean_result.json"
    fixture.write_text(json.dumps(LEAN_FIXTURE), encoding="utf-8")

    result = LeanAdapter(lean_project_dir=tmp_path).run(_config(tmp_path), str(fixture))

    assert result.meta.engine == "lean"
    assert len(result.trades) == 1


def test_lean_adapter_fails_loudly_when_lean_cli_missing(tmp_path, monkeypatch):
    import runner.engines.lean_adapter as module

    strategy = tmp_path / "strategy.py"
    strategy.write_text("class DemoAlgorithm: pass\n", encoding="utf-8")
    project_dir = tmp_path / "lean_project"
    project_dir.mkdir()
    monkeypatch.setattr(module, "LEAN_PROJECT_DIR", project_dir)

    adapter = LeanAdapter(lean_project_dir=project_dir)
    config = _config(tmp_path)
    with pytest.raises(RuntimeError, match="LEAN"):
        adapter.run(config, str(strategy))

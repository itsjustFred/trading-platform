from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner.engines.base import BacktestConfig
from runner.engines.lean_adapter import LeanAdapter


def _config(tmp_path: Path) -> BacktestConfig:
    return BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero"},
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


def test_lean_adapter_imports_result_through_explicit_path(tmp_path):
    fixture = tmp_path / "lean_result.json"
    fixture.write_text(json.dumps(LEAN_FIXTURE), encoding="utf-8")

    result = LeanAdapter(lean_project_dir=tmp_path).import_result(
        fixture, config=_config(tmp_path)
    )

    assert result.meta.engine == "lean"
    assert len(result.trades) == 1


def test_lean_adapter_rejects_result_json_as_strategy_path(tmp_path):
    fixture = tmp_path / "lean_result.json"
    fixture.write_text(json.dumps(LEAN_FIXTURE), encoding="utf-8")

    with pytest.raises(ValueError, match="import_result"):
        LeanAdapter(lean_project_dir=tmp_path).run(_config(tmp_path), str(fixture))


def test_lean_adapter_converts_list_shaped_orders_end_to_end(tmp_path):
    fixture = tmp_path / "lean_result.json"
    payload = {
        "orders": list(LEAN_FIXTURE["orders"].values()),
        "profitLoss": LEAN_FIXTURE["profitLoss"],
    }
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    result = LeanAdapter(lean_project_dir=tmp_path).import_result(fixture)

    assert result.meta.engine == "lean"
    assert len(result.trades) == 1
    assert result.meta.backtest_start.startswith("2024-01-05")


def test_lean_config_write_preserves_previous_file_on_failure(tmp_path, monkeypatch):
    import runner.engines.lean_adapter as module

    project_dir = tmp_path / "lean_project"
    project_dir.mkdir()
    config_path = project_dir / "backtest_config.json"
    config_path.write_text("previous", encoding="utf-8")
    adapter = module.LeanAdapter(lean_project_dir=project_dir)

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated config write failure")

    monkeypatch.setattr(module, "write_text_atomically", fail_write)
    with pytest.raises(OSError, match="simulated"):
        adapter._write_config(_config(tmp_path), "strategy.py")

    assert config_path.read_text(encoding="utf-8") == "previous"


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


def test_find_result_rejects_non_result_json(tmp_path: Path) -> None:
    """C2: config echoes and log-shaped JSON are never mistaken for results."""
    import json

    from runner.engines.lean_adapter import LeanAdapter

    out = tmp_path / "output"
    out.mkdir()
    (out / "b_config.json").write_text(json.dumps({"config": True}))
    (out / "a_log.json").write_text(json.dumps(["not", "a", "dict"]))
    (out / "c_result.json").write_text(
        json.dumps({"orders": [], "trades": [{"id": 1}]})
    )
    adapter = LeanAdapter.__new__(LeanAdapter)
    found = adapter._find_result(out)
    assert found is not None and found.name == "c_result.json"


def test_find_result_returns_none_when_nothing_validates(tmp_path: Path) -> None:
    import json

    from runner.engines.lean_adapter import LeanAdapter

    out = tmp_path / "output"
    out.mkdir()
    (out / "config.json").write_text(json.dumps({"config": True}))
    adapter = LeanAdapter.__new__(LeanAdapter)
    assert adapter._find_result(out) is None


def test_find_result_accepts_dict_shaped_orders(tmp_path: Path) -> None:
    """CodeRabbit round (critical): LEAN writes orders dict-shaped keyed
    by order id — the finder must accept what the converter consumes."""
    import json

    from runner.engines.lean_adapter import LeanAdapter

    out = tmp_path / "output"
    out.mkdir()
    (out / "result.json").write_text(json.dumps(LEAN_FIXTURE))
    adapter = LeanAdapter.__new__(LeanAdapter)
    found = adapter._find_result(out)
    assert found is not None and found.name == "result.json"

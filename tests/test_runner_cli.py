from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import runner.cli as runner_cli
from shared.models import BacktestMeta, BacktestResult, EquityPoint


class _DummyEngine:
    name = "vectorbt"

    def validate_config(self, config):
        return []

    def run(self, config, strategy_path: str):
        return BacktestResult(
            meta=BacktestMeta(
                id="cli-demo",
                created_at="2026-03-09T10:00:00Z",
                engine="vectorbt",
                strategies=[config.strategy_name],
                backtest_start=config.start_date,
                backtest_end=config.end_date,
                initial_capital=config.initial_capital,
                data={"symbols": config.symbols, "timeframes": [config.timeframe], "source": "test"},
                parameters=config.parameters,
            ),
            trades=[],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=config.initial_capital),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=config.initial_capital * 1.05),
            ],
            stats={"engine_metric": 99},
        )


def test_cmd_run_persists_canonical_stats(monkeypatch, tmp_path: Path):
    output_path = tmp_path / "cli-demo.trades.json"

    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _DummyEngine())

    args = SimpleNamespace(
        engine="vectorbt",
        strategy="strategies/demo.py",
        symbols=["AAPL"],
        timeframe="1d",
        start="2024-01-01",
        end="2024-02-01",
        capital=100000.0,
        risk_pct=None,
        max_position_pct=None,
        max_leverage=None,
        data_dir="",
        params=json.dumps({"window": 20}),
        name="demo",
        output=str(output_path),
    )

    runner_cli.cmd_run(args)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["meta"]["parameters"] == {"window": 20}
    assert payload["stats"]["engine_metric"] == 99
    assert payload["stats"]["cagr"] > 0
    assert payload["stats"]["total_return"] == 0.05

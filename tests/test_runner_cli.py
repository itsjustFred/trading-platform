from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import runner.cli as runner_cli
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit


class _DummyEngine:
    name = "vectorbt"

    def validate_config(self, config):
        return []

    def run(self, config, strategy_path: str, progress_callback=None):
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
            trades=[
                Trade(
                    id=1,
                    symbol=config.symbols[0],
                    direction="LONG",
                    entry_time=config.start_date,
                    entry_price=100.0,
                    quantity=10.0,
                    exits=[
                        TradeExit(
                            time=config.end_date,
                            price=105.0,
                            quantity=10.0,
                            pnl=50.0,
                        )
                    ],
                    pnl=50.0,
                )
            ],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=config.initial_capital),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=config.initial_capital * 1.05),
            ],
            stats={"engine_metric": 99},
        )


def test_cmd_run_persists_canonical_stats(monkeypatch, tmp_path: Path):
    output_path = tmp_path / "cli-demo.trades.json"

    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _DummyEngine())
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])

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


def test_cmd_run_normalizes_timeframe_alias(monkeypatch, tmp_path: Path):
    output_path = tmp_path / "cli-demo-alias.trades.json"
    captured: dict[str, object] = {}

    class _CaptureEngine(_DummyEngine):
        def validate_config(self, config):
            captured["timeframe"] = config.timeframe
            return []

    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _CaptureEngine())
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])

    args = SimpleNamespace(
        engine="vectorbt",
        strategy="strategies/demo.py",
        symbols=["AAPL"],
        timeframe="h1",
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

    assert captured["timeframe"] == "1h"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["meta"]["data"]["timeframes"] == ["1h"]


def test_cmd_walkforward_maps_cli_args_and_prints_json(monkeypatch, capsys, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_run_walkforward(**kwargs):
        captured.update(kwargs)
        return {
            "mode": "strategy",
            "summary": {"fold_count": 2, "avg_oos_sharpe": 1.2},
            "output_path": str(tmp_path / "wf.json"),
        }

    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _DummyEngine())
    monkeypatch.setattr(runner_cli, "run_walkforward", fake_run_walkforward)

    args = SimpleNamespace(
        strategy="strategies/demo.py",
        engine="vectorbt",
        symbols="SPY,TLT",
        timeframe="d1",
        start="2020-01-01",
        end="2022-01-01",
        capital=100000.0,
        train_months=12,
        test_months=3,
        step_months=3,
        risk_pct=0.01,
        max_position_pct=1.0,
        max_leverage=2.0,
        intrabar_resolution="open_close",
        params=json.dumps({"window": 20}),
        data_dir="",
        output_dir=str(tmp_path),
    )

    runner_cli.cmd_walkforward(args)

    assert captured["strategy"] == "strategies/demo.py"
    assert captured["symbols"] == ["SPY", "TLT"]
    assert captured["timeframe"] == "1d"
    assert captured["train_months"] == 12
    assert captured["test_months"] == 3
    assert captured["step_months"] == 3
    assert captured["params"] == {"window": 20}
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["fold_count"] == 2

from pathlib import Path

from runner.backtest_matrix import run_backtest_matrix


def _write_strategy(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / f"{name}.py").write_text(
        "STRATEGY_META = {'name': 'Test " + name + "'}\n",
        encoding="utf-8",
    )


def _write_strategy_with_meta(path: Path, name: str, meta: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").write_text("", encoding="utf-8")
    (path / f"{name}.py").write_text(f"STRATEGY_META = {meta}\n", encoding="utf-8")


def test_run_backtest_matrix_runs_selected_strategies_and_writes_outputs(monkeypatch, tmp_path):
    strategies_dir = tmp_path / "strategies"
    _write_strategy(strategies_dir / "sma", "sma")
    _write_strategy(strategies_dir / "rsi", "rsi")
    output_dir = tmp_path / "matrix_out"
    calls = []

    def fake_run_backtest_grid(**kwargs):
        calls.append(kwargs)
        strategy_slug = Path(str(kwargs["strategy"])).parent.name
        return {
            "strategy": str(kwargs["strategy"]),
            "runs": [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "1h",
                    "status": "ok",
                    "total_trades": 1,
                    "total_pnl": 12.5,
                    "net_profit_pct": 1.25,
                    "sharpe": 0.5,
                    "cagr": 0.1,
                    "max_drawdown_pct": -2.0,
                    "win_rate": 1.0,
                    "daily_return_count": 1,
                    "output": str(kwargs["output_dir"] / f"{strategy_slug}_xauusd_1h.trades.json"),
                }
            ],
            "ok": 1,
            "failed": 0,
            "skipped_existing": 0,
            "requested": 1,
            "matrix_csv": str(kwargs["output_dir"] / "grid_matrix.csv"),
        }

    def fake_load_daily_rows(path, *, strategy, symbol, timeframe):
        return [
            {
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "time": "2025-01-01",
                "equity": 100000.0,
                "daily_return": 0.0,
            }
        ]

    monkeypatch.setattr("runner.backtest_matrix.run_backtest_grid", fake_run_backtest_grid)
    monkeypatch.setattr("runner.backtest_matrix._load_daily_rows", fake_load_daily_rows)

    summary = run_backtest_matrix(
        strategy_identifiers=["sma"],
        strategies_dir=strategies_dir,
        symbols=["XAUUSD"],
        timeframes=["1h"],
        start="2025-01-01",
        end="2025-01-31",
        capital=10000.0,
        risk_pct=0.01,
        output_dir=output_dir,
        max_workers=1,
    )

    assert summary["strategies"] == ["sma"]
    assert summary["requested_cells"] == 1
    assert summary["ok_cells"] == 1
    assert summary["failed_cells"] == 0
    assert summary["daily_equity_rows"] == 1
    assert len(calls) == 1
    assert calls[0]["risk_pct"] == 0.01
    assert calls[0]["symbols"] == ["XAUUSD"]
    assert calls[0]["timeframes"] == ["1h"]
    assert (output_dir / "matrix_matrix.csv").exists()
    assert (output_dir / "matrix_daily_equity.csv").exists()
    assert (output_dir / "matrix_summary.json").exists()


def test_run_backtest_matrix_honors_strategy_engine_metadata(monkeypatch, tmp_path):
    strategies_dir = tmp_path / "strategies"
    _write_strategy_with_meta(
        strategies_dir / "intraday_volatility_breakout",
        "intraday_volatility_breakout",
        "{'name': 'intraday_volatility_breakout', 'engine': 'native'}",
    )
    output_dir = tmp_path / "matrix_out"
    calls = []

    def fake_run_backtest_grid(**kwargs):
        calls.append(kwargs)
        return {
            "strategy": str(kwargs["strategy"]),
            "runs": [],
            "ok": 0,
            "failed": 0,
            "skipped_existing": 0,
            "requested": 0,
            "matrix_csv": str(kwargs["output_dir"] / "grid_matrix.csv"),
        }

    monkeypatch.setattr("runner.backtest_matrix.run_backtest_grid", fake_run_backtest_grid)

    summary = run_backtest_matrix(
        strategy_identifiers=["intraday_volatility_breakout"],
        strategies_dir=strategies_dir,
        symbols=["EURUSD"],
        timeframes=["1h"],
        output_dir=output_dir,
        max_workers=1,
    )

    assert summary["strategies"] == ["intraday_volatility_breakout"]
    assert len(calls) == 1
    assert calls[0]["engine"] == "native"


def test_run_backtest_matrix_reports_strategy_failure(monkeypatch, tmp_path):
    strategies_dir = tmp_path / "strategies"
    _write_strategy(strategies_dir / "broken", "broken")
    output_dir = tmp_path / "matrix_out"

    def fake_run_backtest_grid(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("runner.backtest_matrix.run_backtest_grid", fake_run_backtest_grid)

    summary = run_backtest_matrix(
        strategy_identifiers=["broken"],
        strategies_dir=strategies_dir,
        symbols=["XAUUSD"],
        timeframes=["1h"],
        output_dir=output_dir,
        max_workers=1,
    )

    assert summary["requested_cells"] == 1
    assert summary["ok_cells"] == 0
    assert summary["failed_cells"] == 1
    assert summary["runs"][0]["status"] == "failed"
    assert "boom" in summary["runs"][0]["error"]

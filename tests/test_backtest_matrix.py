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
                    "sharpe_ratio": 0.5,
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


def test_load_daily_rows_reads_date_key_from_daily_equity(tmp_path):
    """Regression (F18): daily_equity writes 'date', not 'time'."""
    from runner.backtest_matrix import _load_daily_rows

    output = tmp_path / "grid_demo_aapl_1h.trades.json"
    output.write_text(
        '{"stats": {"daily_returns": ['
        '{"date": "2024-01-01", "equity": 100000.0, "return": 0.0},'
        '{"date": "2024-01-02", "equity": 101000.0, "return": 0.01}'
        ']}}',
        encoding="utf-8",
    )

    rows = _load_daily_rows(output, strategy="demo", symbol="AAPL", timeframe="1h")

    assert [row["time"] for row in rows] == ["2024-01-01", "2024-01-02"]
    assert rows[0]["equity"] == 100000.0
    assert rows[1]["daily_return"] == 0.01


def test_nested_matrix_workers_cannot_multiply(monkeypatch, tmp_path):
    """The two pools multiply, so the bound belongs in the parent.

    Each of the outer `strategy_workers` tasks calls run_backtest_grid, which
    builds its own pool of `max_workers` threads. Both numbers were independently
    bounded and nothing related them, so `--max-workers 8` with four strategies
    asked for 32 concurrent workers on a 4-core host.

    A previous attempt guarded inside each worker instead. That is per-worker
    rather than per-run, and its tests were heavy enough to OOM-kill the host
    twice, so it was discarded. This one is arithmetic in the parent and spawns
    nothing.
    """
    strategies_dir = tmp_path / "strategies"
    for name in ("alpha", "beta", "gamma", "delta"):
        d = strategies_dir / name
        d.mkdir(parents=True)
        (d / f"{name}.py").write_text("def generate_signals(*a, **k):\n    return []\n")
    output_dir = tmp_path / "out"

    seen_workers: list[int] = []

    def fake_run_backtest_grid(**kwargs):
        seen_workers.append(int(kwargs["max_workers"]))
        slug = Path(str(kwargs["strategy"])).parent.name
        return {
            "strategy": str(kwargs["strategy"]),
            "runs": [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "1h",
                    "status": "ok",
                    "total_trades": 1,
                    "total_pnl": 1.0,
                    "net_profit_pct": 0.1,
                    "sharpe_ratio": 0.5,
                    "cagr": 0.1,
                    "max_drawdown_pct": -1.0,
                    "win_rate": 1.0,
                    "daily_return_count": 1,
                    "output": str(kwargs["output_dir"] / f"{slug}.trades.json"),
                }
            ],
            "ok": 1,
            "failed": 0,
            "skipped_existing": 0,
            "requested": 1,
            "matrix_csv": str(kwargs["output_dir"] / "grid_matrix.csv"),
        }

    monkeypatch.setattr("runner.backtest_matrix.run_backtest_grid", fake_run_backtest_grid)
    monkeypatch.setattr(
        "runner.backtest_matrix._load_daily_rows",
        lambda path, **kw: [
            {
                "strategy": kw.get("strategy", "s"),
                "symbol": "XAUUSD",
                "timeframe": "1h",
                "time": "2025-01-01",
                "equity": 100000.0,
                "daily_return": 0.0,
            }
        ],
    )

    budget = 4
    summary = run_backtest_matrix(
        strategy_identifiers=["alpha", "beta", "gamma", "delta"],
        strategies_dir=strategies_dir,
        symbols=["XAUUSD"],
        timeframes=["1h"],
        output_dir=output_dir,
        max_workers=8,
        strategy_workers=4,
        worker_budget=budget,
    )

    # 4 strategies x 8 workers would be 32.
    assert seen_workers, "the strategies must actually have run"
    assert max(seen_workers) * 4 <= budget, (
        f"nested pools multiplied: {len(seen_workers)} strategies each asking for "
        f"{max(seen_workers)} workers exceeds the budget of {budget}"
    )
    assert all(w == 1 for w in seen_workers), seen_workers
    # The request and the applied value are both recorded, so a run is auditable.
    assert summary["max_workers"] == 8
    assert summary["per_strategy_max_workers"] == 1
    assert summary["effective_total_workers"] == 4
    assert summary["worker_budget"] == budget


def test_single_strategy_run_honours_the_requested_worker_count(monkeypatch, tmp_path):
    """No second pool means nothing to multiply, so nothing is clamped.

    Clamping here would silently reduce a legitimate single-strategy grid run,
    which is not what this guard is for.
    """
    strategies_dir = tmp_path / "strategies"
    d = strategies_dir / "solo"
    d.mkdir(parents=True)
    (d / "solo.py").write_text("def generate_signals(*a, **k):\n    return []\n")

    seen: list[int] = []

    def fake_run_backtest_grid(**kwargs):
        seen.append(int(kwargs["max_workers"]))
        return {
            "strategy": str(kwargs["strategy"]),
            "runs": [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "1h",
                    "status": "ok",
                    "total_trades": 1,
                    "total_pnl": 1.0,
                    "net_profit_pct": 0.1,
                    "sharpe_ratio": 0.5,
                    "cagr": 0.1,
                    "max_drawdown_pct": -1.0,
                    "win_rate": 1.0,
                    "daily_return_count": 1,
                    "output": str(kwargs["output_dir"] / "solo.trades.json"),
                }
            ],
            "ok": 1,
            "failed": 0,
            "skipped_existing": 0,
            "requested": 1,
            "matrix_csv": str(kwargs["output_dir"] / "grid_matrix.csv"),
        }

    monkeypatch.setattr("runner.backtest_matrix.run_backtest_grid", fake_run_backtest_grid)
    monkeypatch.setattr("runner.backtest_matrix._load_daily_rows", lambda path, **kw: [])

    summary = run_backtest_matrix(
        strategy_identifiers=["solo"],
        strategies_dir=strategies_dir,
        symbols=["XAUUSD"],
        timeframes=["1h"],
        output_dir=tmp_path / "out",
        max_workers=12,
        strategy_workers=1,
        worker_budget=4,
    )

    assert seen == [12]
    assert summary["per_strategy_max_workers"] == 12

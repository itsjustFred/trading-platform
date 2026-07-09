import json
from pathlib import Path

from runner.backtest_grid import build_cli_command, normalize_timeframe_value, parse_csv_list, run_backtest_grid


def test_parse_and_normalize_grid_inputs():
    assert parse_csv_list("xauusd, sp500,NAS100") == ["XAUUSD", "SP500", "NAS100"]
    assert [normalize_timeframe_value(tf) for tf in ["1min", "h1", "h4", "daily"]] == ["1m", "1h", "4h", "1d"]


def test_build_cli_command_keeps_strategy_risk_and_symbol_cell(tmp_path):
    cmd = build_cli_command(
        strategy=str(tmp_path / "trend_following.py"),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=25000.0,
        risk_pct=0.0075,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params={"fast_sma": 20},
        output_path=tmp_path / "grid_trend_following_sp500_1h.trades.json",
        data_dir="/tmp/data",
        save_sqlite=False,
    )

    joined = " ".join(cmd)
    assert "--skip-sqlite" in cmd
    assert "-m runner.cli run" in joined
    assert "--symbols SP500" in joined
    assert "--timeframe 1h" in joined
    assert "--risk-pct 0.0075" in joined
    assert "--capital 25000.0" in joined
    assert "--params" in joined


def test_run_backtest_grid_runs_all_cells_with_same_risk(monkeypatch, tmp_path):
    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")
    output_dir = tmp_path / "grid_out"
    calls = []

    def fake_run_one(**kwargs):
        calls.append(kwargs)
        return {
            "symbol": kwargs["symbol"],
            "timeframe": kwargs["timeframe"],
            "status": "ok",
            "total_trades": 1,
            "total_pnl": 123.45,
            "net_profit_pct": 1.23,
            "sharpe": 0.5,
            "cagr": 0.1,
            "max_drawdown_pct": -2.0,
            "win_rate": 1.0,
            "output": str(kwargs["output_dir"] / "fake.trades.json"),
        }

    monkeypatch.setattr("runner.backtest_grid.run_one_backtest", fake_run_one)

    summary = run_backtest_grid(
        strategy=str(strategy),
        symbols=["XAUUSD", "SP500"],
        timeframes=["1h", "4h"],
        start="2025-01-01",
        end="2025-01-31",
        capital=10000.0,
        risk_pct=0.01,
        output_dir=output_dir,
        max_workers=1,
    )

    assert summary["requested"] == 4
    assert summary["ok"] == 4
    assert summary["failed"] == 0
    assert [call["risk_pct"] for call in calls] == [0.01, 0.01, 0.01, 0.01]
    assert (output_dir / "grid_matrix.csv").exists()
    assert (output_dir / "grid_summary.json").exists()
    assert json.loads((output_dir / "grid_summary.json").read_text(encoding="utf-8"))["requested"] == 4

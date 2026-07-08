from pathlib import Path

import runner.backtest_matrix as matrix_module
from shared.strategy_registry import StrategyEntry


def test_run_strategy_matrix_cell_adds_inspection_links(monkeypatch, tmp_path: Path) -> None:
    strategy_path = tmp_path / "strategies" / "demo.py"
    strategy_path.parent.mkdir(parents=True)
    strategy_path.write_text("STRATEGY_META = {'name': 'Demo'}\n", encoding="utf-8")
    entry = StrategyEntry(
        slug="demo",
        file_path=strategy_path,
        relative_file="demo.py",
        directory=strategy_path.parent,
        documentation_path=None,
        meta={"engine": "vectorbt", "name": "Demo"},
    )

    output = tmp_path / "grid_demo_aapl_1h.trades.json"
    output.write_text(
        '{"stats": {"daily_returns": [{"time": "2024-01-01", "equity": 100000, "return": 0.0}]}}',
        encoding="utf-8",
    )

    def fake_run_backtest_grid(**kwargs):
        return {
            "runs": [
                {
                    "run_id": "grid_demo_aapl_1h",
                    "symbol": "AAPL",
                    "timeframe": "1h",
                    "status": "ok",
                    "backtest_id": "grid_demo_aapl_1h",
                    "output": str(output),
                    "total_trades": 1,
                }
            ]
        }

    monkeypatch.setattr(matrix_module, "run_backtest_grid", fake_run_backtest_grid)

    result = matrix_module._run_strategy_matrix_cell(
        entry=entry,
        normalized_symbols=["AAPL"],
        normalized_timeframes=["1h"],
        start="2024-01-01",
        end="2024-01-02",
        engine="vectorbt",
        capital=100000.0,
        risk_pct=0.01,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params={},
        output_dir=tmp_path / "matrix",
        data_dir="",
        max_workers=1,
        timeout_seconds=10,
        skip_existing=False,
        use_subprocess=False,
        save_sqlite=True,
        keep_indicators=False,
    )

    row = result["rows"][0]
    assert row["matrix_node_id"] == "demo:AAPL:1h"
    assert row["backtest_url"] == "/backtests/grid_demo_aapl_1h"
    assert row["trades_url"] == "/api/backtest/grid_demo_aapl_1h/trades"
    assert result["daily_rows"][0]["run_id"] == "grid_demo_aapl_1h"

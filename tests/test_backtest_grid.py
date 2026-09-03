import json
import subprocess
from pathlib import Path

import pytest

import runner.backtest_grid as bg
from runner.backtest_grid import (
    build_cli_command,
    normalize_timeframe_value,
    parse_csv_list,
    run_backtest_grid,
    run_one_backtest,
)


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
            "sharpe_ratio": 0.5,
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


def test_subprocess_timeout_is_isolated_to_cell(monkeypatch, tmp_path):
    # A hung subprocess cell must be marked failed/timeout, not raise and abort
    # the whole grid run.
    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "cmd", timeout=5)

    monkeypatch.setattr("runner.backtest_grid.subprocess.run", fake_run)

    result = run_one_backtest(
        strategy=str(strategy),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=tmp_path / "out",
        data_dir="/tmp/data",
        timeout_seconds=5,
        skip_existing=False,
        use_subprocess=True,
    )

    assert result["status"] == "failed"
    assert result["error_class"] == "timeout"
    assert "timed out" in result["error"]


def test_subprocess_launch_failure_is_isolated_to_cell(monkeypatch, tmp_path):
    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")

    def fake_run(*args, **kwargs):
        raise OSError("python executable missing")

    monkeypatch.setattr("runner.backtest_grid.subprocess.run", fake_run)

    result = run_one_backtest(
        strategy=str(strategy),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=tmp_path / "out",
        data_dir="/tmp/data",
        timeout_seconds=5,
        skip_existing=False,
        use_subprocess=True,
    )

    assert result["status"] == "failed"
    assert result["error_class"] == "subprocess_error"
    assert "python executable missing" in result["error"]


def test_in_process_uses_resolved_dates_for_daily_equity(monkeypatch, tmp_path):
    # Regression: attach_daily_equity must receive the resolved date range, not
    # the original (empty) start/end.
    from shared.models import BacktestMeta, BacktestResult

    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")

    monkeypatch.setattr(bg, "detect_strategy_type", lambda p: bg.StrategyType.MARKET_DATA)
    monkeypatch.setattr(bg, "resolve_strategy_parameters", lambda s, p: dict(p or {}))
    monkeypatch.setattr(bg, "_detect_date_range", lambda *a, **k: ("2024-01-01", "2024-02-01"))
    monkeypatch.setattr(bg, "ensure_backtest_data", lambda **k: [])
    monkeypatch.setattr(bg, "merge_canonical_stats", lambda r: None)

    captured: dict[str, str | None] = {}

    def fake_attach(result, *, start=None, end=None):
        captured["start_date"] = start
        captured["end_date"] = end
        return result

    monkeypatch.setattr(bg, "attach_daily_equity", fake_attach)

    class FakeEngine:
        def validate_config(self, config):
            return []

        def run(self, config, strategy):
            return BacktestResult(
                meta=BacktestMeta(
                    id="x",
                    created_at="2024-01-01T00:00:00+00:00",
                    engine="native",
                    strategies=["trend_following"],
                    backtest_start="2024-01-01",
                    backtest_end="2024-02-01",
                    data={"symbols": ["SP500"], "timeframes": ["1h"]},
                ),
                trades=[],
                equity_curve=[],
                stats={},
            )

    monkeypatch.setattr(bg, "get_engine", lambda e: FakeEngine())

    result = bg.run_one_backtest_in_process(
        bg.CellRequest(
            strategy=str(strategy),
            symbol="SP500",
            timeframe="1h",
            start="",
            end="",
            engine="native",
            capital=10000.0,
            risk_pct=0.01,
            max_position_pct=None,
            max_leverage=None,
            intrabar_resolution="open_close",
            params=None,
            output_dir=tmp_path / "out",
            data_dir="/tmp/data",
            skip_existing=False,
            save_sqlite=False,
        )
    )

    assert captured["start_date"] == "2024-01-01"
    assert captured["end_date"] == "2024-02-01"
    assert result["start"] == "2024-01-01"
    assert result["end"] == "2024-02-01"


def test_subprocess_command_does_not_pass_grid_only_flags(tmp_path):
    """Regression (F3): runner.cli run has no --keep-indicators flag."""
    cmd = build_cli_command(
        strategy=str(tmp_path / "trend_following.py"),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_path=tmp_path / "out.trades.json",
        data_dir="/tmp/data",
        save_sqlite=False,
    )

    assert "--keep-indicators" not in cmd
    assert "--skip-sqlite" in cmd


def test_subprocess_command_forwards_keep_indicators(tmp_path):
    """F3 residue: with keep_indicators=True the subprocess cell must pass the
    flag explicitly (runner.cli run accepts it) instead of silently dropping
    the knob in subprocess mode."""
    cmd = build_cli_command(
        strategy=str(tmp_path / "trend_following.py"),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_path=tmp_path / "out.trades.json",
        data_dir="/tmp/data",
        save_sqlite=False,
        keep_indicators=True,
    )

    assert "--keep-indicators" in cmd


def test_no_date_range_is_fabricated_when_data_missing(tmp_path):
    """Regression (F19): missing data must fail loudly, not invent 2023..2025."""
    with pytest.raises(ValueError, match="No OHLCV data"):
        bg._detect_date_range("NOSUCHSYMBOL", "1h", str(tmp_path / "data"))


def test_read_backtest_stats_exposes_canonical_keys(tmp_path):
    """Regression (F40): canonical keys only; net_profit_pct is derived from
    total_return (never read from the stats dict) and the sharpe alias is gone."""
    path = tmp_path / "run.trades.json"
    path.write_text(
        json.dumps(
            {
                "meta": {"id": "run-1"},
                "trades": [{}],
                "stats": {"total_return": 0.05, "sharpe_ratio": 1.5},
            }
        ),
        encoding="utf-8",
    )
    stats = bg.read_backtest_stats(path)
    assert stats["total_return"] == 0.05
    assert stats["net_profit_pct"] == pytest.approx(5.0)
    assert stats["sharpe_ratio"] == 1.5
    # The non-canonical alias must not reappear in grid rows.
    assert "sharpe" not in stats
    # A stale/foreign net_profit_pct in the stats dict cannot leak through.
    poisoned = tmp_path / "poisoned.trades.json"
    poisoned.write_text(
        json.dumps(
            {
                "meta": {"id": "run-2"},
                "trades": [{}],
                "stats": {"net_profit_pct": 999.0},
            }
        ),
        encoding="utf-8",
    )
    poisoned_stats = bg.read_backtest_stats(poisoned)
    assert poisoned_stats["net_profit_pct"] is None

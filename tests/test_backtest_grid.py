import json
import subprocess

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
        max_position_exposure_pct=None,
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
        max_position_exposure_pct=None,
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


def test_subprocess_child_uses_filtered_environment(monkeypatch, tmp_path):
    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="failed", stderr="")

    monkeypatch.setattr("runner.backtest_grid.subprocess.run", fake_run)
    monkeypatch.setenv("NIM_API_KEY", "secret-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = run_one_backtest(
        strategy=str(strategy),
        symbol="SP500",
        timeframe="1h",
        start="2025-01-01",
        end="2025-12-31",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=tmp_path / "out",
        data_dir="/tmp/data",
        timeout_seconds=5,
        skip_existing=False,
        use_subprocess=True,
    )

    env = captured["env"]
    assert result["status"] == "failed"
    assert "NIM_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONUNBUFFERED"] == "1"


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
        max_position_exposure_pct=None,
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


def test_missing_data_fails_only_that_cell(monkeypatch, tmp_path):
    """CodeRabbit (cycle 2, critical): a cell whose data cannot be resolved must
    produce a failed cell record — not raise out of run_one_backtest and abort
    the whole grid run."""
    result = run_one_backtest(
        strategy=str(tmp_path / "trend_following.py"),
        symbol="NOSUCHSYMBOL",
        timeframe="1h",
        start="",
        end="",
        engine="vectorbt",
        capital=10000.0,
        risk_pct=0.01,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=tmp_path / "out",
        data_dir=str(tmp_path / "data"),
        timeout_seconds=5,
        skip_existing=False,
        use_subprocess=False,
    )

    assert result["status"] == "failed"
    assert result["error_class"] == "no_data"
    assert "refusing to fabricate" in result["error"]
    assert result["finished_at"]


def test_detect_date_range_handles_case_insensitive_timestamp_column(tmp_path):
    """CodeRabbit (cycle 2, minor): a parquet whose timestamp column is cased
    differently must still contribute its range (same contract as
    runner.cli._detect_date_range); a file without any timestamp column fails
    loudly instead of being projected into a KeyError."""
    import pandas as pd

    data_dir = tmp_path / "data"
    cased = data_dir / "ohlcv" / "test" / "AAA" / "1d.parquet"
    cased.parent.mkdir(parents=True, exist_ok=True)
    stamps = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    pd.DataFrame({"Timestamp": stamps, "close": [1.0] * 5}).to_parquet(cased, index=False)

    start, end = bg._detect_date_range("AAA", "1d", str(data_dir))
    assert (start, end) == ("2024-01-01", "2024-01-05")

    nots = data_dir / "ohlcv" / "test" / "BBB" / "1d.parquet"
    nots.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"px": [1.0, 2.0]}).to_parquet(nots, index=False)

    with pytest.raises(ValueError, match="timestamp column"):
        bg._detect_date_range("BBB", "1d", str(data_dir))


def test_grid_tasks_carry_keep_indicators(tmp_path, monkeypatch):
    """CodeRabbit (cycle 2, major): run_backtest_grid must forward
    keep_indicators into each cell request instead of silently dropping it."""
    strategy = tmp_path / "trend_following.py"
    strategy.write_text("STRATEGY_META = {'name': 'trend_following'}\n", encoding="utf-8")
    captured: list[dict] = []

    def fake_run_one(**kwargs):
        captured.append(kwargs)
        return {"symbol": kwargs["symbol"], "timeframe": kwargs["timeframe"], "status": "failed",
                "finished_at": "2026-09-03T00:00:00+00:00"}

    monkeypatch.setattr("runner.backtest_grid.run_one_backtest", fake_run_one)

    run_backtest_grid(
        strategy=str(strategy), symbols=["XAUUSD"], timeframes=["1h"],
        start="2025-01-01", end="2025-01-31", output_dir=tmp_path / "out",
        max_workers=1, keep_indicators=True,
    )

    assert captured
    assert all(cell["keep_indicators"] is True for cell in captured)


# ─── grid cell identity: a stale artifact is refused, never reused ───────────


def _write_frame(path, *, periods: int = 5) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC"),
            "close": [float(index + 1) for index in range(periods)],
        }
    ).to_parquet(path, index=False)


def _identity_fixture(tmp_path, *, periods: int = 5, stamp: bool = True):
    """A data dir, a strategy file and a persisted cell artifact for one cell.

    Returns ``(request, artifact, parquet)``. When ``stamp`` is set the artifact
    carries the identity a real run would have written, so the reuse decision
    has something legitimate to accept.
    """
    from shared.models import BacktestMeta, BacktestResult

    data_dir = tmp_path / "data"
    parquet = data_dir / "ohlcv" / "yfinance" / "equity" / "US" / "AAPL" / "1d.parquet"
    _write_frame(parquet, periods=periods)
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    strategy = tmp_path / "demo.py"
    strategy.write_text("STRATEGY_META = {'name': 'demo'}\n", encoding="utf-8")

    request = bg.CellRequest(
        strategy=str(strategy),
        symbol="AAPL",
        timeframe="1d",
        start="2024-01-01",
        end="2024-01-05",
        engine="native",
        capital=10_000.0,
        risk_pct=0.01,
        params=None,
        output_dir=output_dir,
        data_dir=str(data_dir),
        skip_existing=True,
        save_sqlite=False,
    )
    result = BacktestResult(
        meta=BacktestMeta(
            id=bg.build_run_id(str(strategy), "AAPL", "1d"),
            created_at="2024-01-06T00:00:00+00:00",
            engine="native",
            strategies=["demo"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-05",
            initial_capital=10_000.0,
            data={"symbols": ["AAPL"], "timeframes": ["1d"]},
        ),
        trades=[],
        equity_curve=[],
        stats={},
    )
    if stamp:
        identity = bg._cell_identity(request, "2024-01-01", "2024-01-05")
        result.meta.data = {**result.meta.data, "grid_identity": identity}
    artifact = output_dir / f"{bg.build_run_id(str(strategy), 'AAPL', '1d')}.trades.json"
    result.save(artifact)
    return request, artifact, parquet


def _cell_record(request) -> dict:
    record = bg._init_cell_result(request)
    return record


def test_resume_recomputes_when_the_underlying_frame_changed(tmp_path):
    """A re-downloaded parquet is the defect this identity exists for.

    Every field the artifact records -- engine, symbol, window, capital, params
    -- is unchanged by a re-download, so the artifact metadata check passes and
    the data fingerprint is the only thing that can catch it. Reusing here
    reports numbers from five bars as the answer for nine.
    """
    request, _artifact, parquet = _identity_fixture(tmp_path)
    # A different length, so this is not a mere mtime touch.
    _write_frame(parquet, periods=9)

    record = _cell_record(request)

    assert record["status"] == "pending", (
        "the cell must recompute: the data it was computed from no longer exists"
    )


def test_resume_recomputes_when_the_strategy_source_changed(tmp_path):
    request, _artifact, _parquet = _identity_fixture(tmp_path)
    strategy_path = bg.Path(request.strategy)
    strategy_path.write_text(
        "STRATEGY_META = {'name': 'demo'}\n# edited after the run\n",
        encoding="utf-8",
    )

    assert _cell_record(request)["status"] == "pending"


def test_resume_recomputes_when_a_risk_knob_changed(tmp_path):
    """risk_per_trade_pct/max_leverage are not in the artifact schema.

    The identity carries them, so changing one invalidates the cell instead of
    silently returning a run sized with the old knob.
    """
    from dataclasses import replace

    request, _artifact, _parquet = _identity_fixture(tmp_path)

    assert _cell_record(replace(request, risk_pct=0.05))["status"] == "pending"
    assert _cell_record(replace(request, max_leverage=3.0))["status"] == "pending"


def test_resume_refuses_an_artifact_that_never_carried_an_identity(tmp_path):
    """A legacy artifact promises nothing about what it was computed from.

    Refusing it costs one recompute; serving it is how a stale artifact gets
    reported as current.
    """
    request, _artifact, _parquet = _identity_fixture(tmp_path, stamp=False)

    assert _cell_record(request)["status"] == "pending"


def test_resume_reuses_an_artifact_whose_identity_still_holds(tmp_path):
    """The gate has to accept a genuinely current cell, or --resume is useless."""
    request, _artifact, _parquet = _identity_fixture(tmp_path)

    record = _cell_record(request)

    assert record["status"] == "skipped_existing"
    assert record["grid_identity"], "a reused cell must report the identity it matched"


def test_in_process_cell_stamps_its_identity(monkeypatch, tmp_path):
    """A finished cell must be resumable: no stamp, no reuse next time."""
    from shared.models import BacktestMeta, BacktestResult

    request, artifact, _parquet = _identity_fixture(tmp_path, stamp=False)
    artifact.unlink()
    monkeypatch.setattr(bg, "require_generated_strategy_approval", lambda strategy: None)
    monkeypatch.setattr(bg, "detect_strategy_type", lambda path: bg.StrategyType.MARKET_DATA)
    monkeypatch.setattr(
        bg, "resolve_strategy_parameters", lambda strategy, params: dict(params or {})
    )
    monkeypatch.setattr(bg, "ensure_backtest_data", lambda **kwargs: [])
    monkeypatch.setattr(bg, "attach_daily_equity", lambda result, **kwargs: result)
    monkeypatch.setattr(bg, "merge_canonical_stats", lambda result: None)

    class FakeEngine:
        def validate_config(self, config):
            return []

        def run(self, config, strategy):
            return BacktestResult(
                meta=BacktestMeta(
                    id=bg.build_run_id(strategy, "AAPL", "1d"),
                    created_at="2024-01-06T00:00:00+00:00",
                    engine="native",
                    strategies=["demo"],
                    backtest_start="2024-01-01",
                    backtest_end="2024-01-05",
                    initial_capital=10_000.0,
                    data={"symbols": ["AAPL"], "timeframes": ["1d"]},
                ),
                trades=[],
                equity_curve=[],
                stats={},
            )

    monkeypatch.setattr(bg, "get_engine", lambda engine: FakeEngine())

    result = bg.run_one_backtest_in_process(request)

    assert result["status"] == "ok"
    stamped = json.loads(artifact.read_text(encoding="utf-8"))["meta"]["data"]["grid_identity"]
    assert stamped["fingerprint"] == result["grid_identity"]
    assert stamped["data"] is not None, "the stamp must record the data it read"
    # The point of the stamp: the very next resume accepts it.
    assert _cell_record(request)["status"] == "skipped_existing"


def test_subprocess_cell_stamps_its_identity(monkeypatch, tmp_path):
    """`runner.cli` knows nothing about grid identity, so the parent writes it.

    An unstamped artifact is one the next --resume must refuse, so a cell that
    could not be stamped is a failed cell rather than a slower one.
    """
    from shared.models import BacktestMeta, BacktestResult

    request, artifact, _parquet = _identity_fixture(tmp_path, stamp=False)
    artifact.unlink()
    monkeypatch.setattr(bg, "require_generated_strategy_approval", lambda strategy: None)

    def fake_run(cmd, **kwargs):
        BacktestResult(
            meta=BacktestMeta(
                id="grid_demo_aapl_1d",
                created_at="2024-01-06T00:00:00+00:00",
                engine="native",
                strategies=["demo"],
                backtest_start="2024-01-01",
                backtest_end="2024-01-05",
                initial_capital=10_000.0,
                data={"symbols": ["AAPL"], "timeframes": ["1d"]},
            ),
            trades=[],
            equity_curve=[],
            stats={},
        ).save(artifact)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")

    monkeypatch.setattr(bg.subprocess, "run", fake_run)

    result = run_one_backtest(
        strategy=request.strategy,
        symbol="AAPL",
        timeframe="1d",
        start="2024-01-01",
        end="2024-01-05",
        engine="native",
        capital=10_000.0,
        risk_pct=0.01,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=request.output_dir,
        data_dir=request.data_dir,
        timeout_seconds=10,
        skip_existing=True,
        use_subprocess=True,
        save_sqlite=False,
    )

    assert result["status"] == "ok"
    assert json.loads(artifact.read_text(encoding="utf-8"))["meta"]["data"]["grid_identity"]

    assert _cell_record(request)["status"] == "skipped_existing"


def test_subprocess_cell_that_cannot_be_stamped_is_a_failed_cell(monkeypatch, tmp_path):
    """The subprocess exits 0 but leaves an artifact that cannot carry an identity.

    Reporting that cell as finished would hand the operator numbers from a
    result the next --resume is obliged to throw away.
    """
    request, artifact, _parquet = _identity_fixture(tmp_path, stamp=False)
    monkeypatch.setattr(bg, "require_generated_strategy_approval", lambda strategy: None)

    def fake_run(cmd, **kwargs):
        artifact.write_text("{not json", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")

    monkeypatch.setattr(bg.subprocess, "run", fake_run)

    result = run_one_backtest(
        strategy=request.strategy,
        symbol="AAPL",
        timeframe="1d",
        start="2024-01-01",
        end="2024-01-05",
        engine="native",
        capital=10_000.0,
        risk_pct=0.01,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params=None,
        output_dir=request.output_dir,
        data_dir=request.data_dir,
        timeout_seconds=10,
        skip_existing=True,
        use_subprocess=True,
        save_sqlite=False,
    )

    assert result["status"] == "failed", (
        "an unstamped cell would be reported as a finished result that the next "
        "resume is obliged to throw away"
    )
    assert "identity" in result["error"]

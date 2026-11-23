from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
                data={
                    "symbols": config.symbols,
                    "timeframes": [config.timeframe],
                    "source": "test",
                },
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
                            pnl=5000.0,
                        )
                    ],
                    pnl=5000.0,
                )
            ],
            equity_curve=[
                EquityPoint(time="2024-01-01T00:00:00+00:00", equity=config.initial_capital),
                EquityPoint(time="2024-02-01T00:00:00+00:00", equity=config.initial_capital * 1.05),
            ],
            stats={"engine_metric": 99, "total_pnl": -1.0},
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("fee_rate", -0.001),
        ("fee_rate", float("nan")),
        ("fee_rate", float("inf")),
        ("fee_flat_per_trade", -1.0),
        ("fee_flat_per_trade", float("nan")),
        ("fee_flat_per_trade", float("inf")),
    ],
)
def test_fee_cli_rejects_invalid_fee_values(attribute: str, value: float) -> None:
    args = SimpleNamespace(
        fee_rate=None,
        fee_flat_per_trade=None,
        fees_zero=False,
    )
    setattr(args, attribute, value)

    with pytest.raises(ValueError, match="finite and non-negative"):
        runner_cli._fee_params_from_args(args)


def test_fee_cli_accepts_valid_fee_values() -> None:
    args = SimpleNamespace(
        fee_rate=0.001,
        fee_flat_per_trade=2.0,
        fees_zero=False,
    )

    assert runner_cli._fee_params_from_args(args) == {
        "fee_rate": 0.001,
        "fee_flat_per_trade": 2.0,
    }


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
        max_position_exposure_pct=None,
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
    assert payload["stats"]["total_pnl"] == 5000.0
    assert payload["stats"]["cagr"] > 0
    assert payload["stats"]["total_return"] == 0.05


def test_cmd_run_records_not_modeled_slippage_note_for_out_of_scope_engine(
    monkeypatch, tmp_path: Path
):
    """#130: engines outside native/signal-replay get an explicit 'not
    modeled' slippage record — never silence."""
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
        max_position_exposure_pct=None,
        max_leverage=None,
        data_dir="",
        params="{}",
        name="demo",
        output=str(output_path),
        slippage=None,
        slippage_bps=None,
        slippage_default_bps=None,
    )

    runner_cli.cmd_run(args)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    slippage = payload["meta"]["data"]["slippage"]
    assert slippage["models"] == {}
    assert "does not apply a slippage model" in slippage["note"]


def _slippage_args(**overrides):
    base = dict(
        engine="vectorbt",
        strategy="strategies/demo.py",
        symbols=["AAPL"],
        timeframe="1d",
        start="2024-01-01",
        end="2024-02-01",
        capital=100000.0,
        risk_pct=None,
        max_position_exposure_pct=None,
        max_leverage=None,
        data_dir="",
        params="{}",
        name="demo",
        output="",
        result_id="",
        skip_sqlite=True,
        keep_indicators=False,
        progress_stdout=False,
        intrabar_resolution="open_close",
        universe_source="",
        universe_as_of="",
        universe_membership="",
        universe_source_name="",
        fee_rate=None,
        fee_flat_per_trade=None,
        fees_zero=False,
        slippage=None,
        slippage_bps=None,
        slippage_default_bps=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cmd_run_rejects_declared_slippage_on_out_of_scope_engine(
    monkeypatch, tmp_path: Path
):
    """#130: ``run --engine vectorbt --slippage-bps 5`` must fail closed —
    the engine has no slippage path, so the flag would be silently ignored."""
    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _DummyEngine())
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])

    args = _slippage_args(slippage="bps", slippage_bps=5.0)
    with pytest.raises(ValueError, match="does not model slippage"):
        runner_cli.cmd_run(args)


def test_cmd_run_allows_explicit_none_slippage_on_out_of_scope_engine(
    monkeypatch, tmp_path: Path
):
    """``slippage='none'`` declares the gross behaviour vectorbt already
    has — allowed, and still recorded as 'not modeled'."""
    output_path = tmp_path / "cli-demo.trades.json"
    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _DummyEngine())
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])

    args = _slippage_args(slippage="none", output=str(output_path))
    runner_cli.cmd_run(args)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "does not apply a slippage model" in payload["meta"]["data"]["slippage"]["note"]
    assert payload["meta"]["data"]["pnl_basis"]["slippage_embedded"] is False


def test_cmd_run_forwards_slippage_flags_into_params(monkeypatch, tmp_path: Path):
    """Mutant guard: the CLI slippage flags must reach engine params."""
    captured: dict[str, object] = {}

    class _CaptureEngine(_DummyEngine):
        name = "native"

        def validate_config(self, config):
            captured["parameters"] = dict(config.parameters)
            return []

    monkeypatch.setattr(runner_cli, "get_engine", lambda _: _CaptureEngine())
    monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])

    args = _slippage_args(
        slippage="bps",
        slippage_bps=7.5,
        slippage_default_bps=2.5,
        output=str(tmp_path / "out.json"),
    )
    runner_cli.cmd_run(args)

    params = captured["parameters"]
    assert params["slippage"] == "bps"
    assert params["slippage_bps"] == 7.5
    assert params["slippage_default_bps"] == 2.5


def test_cmd_walkforward_forwards_slippage_flags(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_run_walkforward(**kwargs):
        captured.update(kwargs)
        return {"mode": "strategy", "summary": {}, "output_path": ""}

    monkeypatch.setattr(runner_cli, "run_walkforward", fake_run_walkforward)

    args = SimpleNamespace(
        strategy="strategies/demo.py",
        engine="native",
        symbols="SPY",
        timeframe="1d",
        start="2020-01-01",
        end="2022-01-01",
        capital=100000.0,
        train_months=12,
        test_months=3,
        step_months=None,
        purge_days=0,
        embargo_days=0,
        risk_pct=None,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params="{}",
        data_dir="",
        output_dir="",
        fee_rate=None,
        fee_flat_per_trade=None,
        fees_zero=False,
        slippage="bps",
        slippage_bps=7.5,
        slippage_default_bps=None,
    )
    runner_cli.cmd_walkforward(args)

    assert captured["params"]["slippage"] == "bps"
    assert captured["params"]["slippage_bps"] == 7.5


def test_cmd_grid_forwards_slippage_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_backtest_grid(**kwargs):
        captured.update(kwargs)
        return {"runs": [], "failed": 0}

    monkeypatch.setattr(runner_cli, "run_backtest_grid", fake_run_backtest_grid)

    args = SimpleNamespace(
        strategy="strategies/demo.py",
        symbols="SPY",
        timeframes="1d",
        start="2020-01-01",
        end="2021-01-01",
        engine="native",
        capital=100000.0,
        risk_pct=None,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params="{}",
        output_dir="",
        data_dir="",
        max_workers=1,
        timeout_seconds=60,
        resume=False,
        subprocess=False,
        skip_sqlite=True,
        keep_indicators=False,
        fee_rate=None,
        fee_flat_per_trade=None,
        fees_zero=False,
        slippage="bps",
        slippage_bps=7.5,
        slippage_default_bps=None,
    )
    runner_cli.cmd_grid(args)

    assert captured["params"]["slippage"] == "bps"
    assert captured["params"]["slippage_bps"] == 7.5


def test_cmd_matrix_forwards_slippage_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_backtest_matrix(**kwargs):
        captured.update(kwargs)
        return {"rows": [], "failed_cells": 0}

    monkeypatch.setattr(runner_cli, "run_backtest_matrix", fake_run_backtest_matrix)

    args = SimpleNamespace(
        strategy=["demo"],
        strategies_dir="strategies",
        symbols="SPY",
        timeframes="1d",
        start="2020-01-01",
        end="2021-01-01",
        engine="native",
        capital=100000.0,
        risk_pct=None,
        max_position_exposure_pct=None,
        max_leverage=None,
        intrabar_resolution="open_close",
        params="{}",
        output_dir="",
        data_dir="",
        max_workers=1,
        timeout_seconds=60,
        resume=False,
        subprocess=False,
        skip_sqlite=True,
        keep_indicators=False,
        fee_rate=None,
        fee_flat_per_trade=None,
        fees_zero=False,
        slippage="bps",
        slippage_bps=7.5,
        slippage_default_bps=None,
    )
    runner_cli.cmd_matrix(args)

    assert captured["params"]["slippage"] == "bps"
    assert captured["params"]["slippage_bps"] == 7.5


def test_run_backtest_grid_rejects_declared_slippage_on_vectorbt(tmp_path: Path):
    """The grid defaults to vectorbt — a declared slippage model must fail
    before any cell runs, not produce gross artifacts."""
    from runner.backtest_grid import run_backtest_grid

    with pytest.raises(ValueError, match="does not model slippage"):
        run_backtest_grid(
            strategy="strategies/demo.py",
            symbols=["SPY"],
            timeframes=["1d"],
            start="2020-01-01",
            end="2021-01-01",
            engine="vectorbt",
            params={"slippage": "bps", "slippage_bps": 5.0},
            output_dir=tmp_path,
        )


def test_run_walkforward_rejects_declared_slippage_on_vectorbt():
    from runner.walkforward import run_walkforward

    class _StubEngine:
        name = "vectorbt"

    with pytest.raises(ValueError, match="does not model slippage"):
        run_walkforward(
            strategy="strategies/demo.py",
            engine=_StubEngine(),
            symbols=["SPY"],
            timeframe="1d",
            start="2020-01-01",
            end="2022-01-01",
            params={"slippage_bps": 5.0},
        )


def test_cmd_run_refuses_unapproved_generated_strategy(monkeypatch, tmp_path: Path):
    from runner.approval_gate import StrategyNotApprovedError, mark_generated_strategy

    strategy = tmp_path / "generated.py"
    strategy.write_text("def generate_signals(df, params):\n    return df, df\n", encoding="utf-8")
    mark_generated_strategy(strategy, strategies_root=tmp_path)
    engine_called = False

    def fake_get_engine(_name):
        nonlocal engine_called
        engine_called = True
        return _DummyEngine()

    monkeypatch.setattr(runner_cli, "get_engine", fake_get_engine)
    args = SimpleNamespace(
        engine="vectorbt",
        strategy=str(strategy),
        symbols=["AAPL"],
        timeframe="1d",
        start="2024-01-01",
        end="2024-02-01",
        capital=100000.0,
        risk_pct=None,
        max_position_exposure_pct=None,
        max_leverage=None,
        data_dir="",
        params="{}",
        name="generated",
        output=str(tmp_path / "out.json"),
    )

    with pytest.raises(StrategyNotApprovedError):
        runner_cli.cmd_run(args)
    assert engine_called is False


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
        max_position_exposure_pct=None,
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
        max_position_exposure_pct=1.0,
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


def test_detect_date_range_rejects_missing_data(tmp_path):
    """Regression (F19): no fabricated 2023..2025 fallback."""
    import pytest

    with pytest.raises(ValueError, match="No OHLCV data"):
        runner_cli._detect_date_range(["NOSUCHSYMBOL"], "1d", str(tmp_path))


def test_detect_date_range_rejects_disjoint_symbol_ranges(tmp_path):
    """Regression (F19): disjoint ranges must not flow start>end into configs."""
    import pandas as pd
    import pytest

    for symbol, month in (("AAA", "2020-01"), ("BBB", "2024-06")):
        path = tmp_path / "ohlcv" / "test" / symbol / "1d.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamps = pd.date_range(f"{month}-01", periods=5, freq="D", tz="UTC")
        pd.DataFrame(
            {
                "timestamp": stamps,
                "open": [1.0] * 5,
                "high": [1.0] * 5,
                "low": [1.0] * 5,
                "close": [1.0] * 5,
                "volume": [1.0] * 5,
            }
        ).to_parquet(path, index=False)

    with pytest.raises(ValueError, match="disjoint"):
        runner_cli._detect_date_range(["AAA", "BBB"], "1d", str(tmp_path))
class TestMaxPositionPctHelpSemantics:
    """Mutation hardening (F7/D12): the CLI help must pin the flag semantics."""

    def test_help_documents_per_position_cap_not_leverage(self, capsys, monkeypatch):
        """--max-position-pct help must say per-position and deny leverage.

        Kills the mutant that rewrote the help into 'leverage may be applied'
        — the whole F7 defusal rests on the documented semantics.
        """
        from runner.cli import main

        monkeypatch.setattr("sys.argv", ["runner.cli", "run", "--help"])
        with pytest.raises(SystemExit):
            main()
        text = " ".join(capsys.readouterr().out.split())
        assert "Per-position notional cap" in text, (
            "help must state the per-position notional-cap semantics"
        )
        assert "not a leverage grant" in text, (
            "help must explicitly deny the leverage reading"
        )


def test_help_mentions_max_leverage_separately(capsys, monkeypatch):
    """The help must point leverage at --max-leverage, not blur the two.

    Kills the mutant that rewrote the leverage sentence into 'leverage may
    be applied' — blurring the two knobs back into the F7 ambiguity.
    """
    from runner.cli import main

    monkeypatch.setattr("sys.argv", ["runner.cli", "run", "--help"])
    with pytest.raises(SystemExit):
        main()
    text = " ".join(capsys.readouterr().out.split())
    assert "--max-leverage controls leverage separately" in text, (
        "the help must route leverage semantics to --max-leverage"
    )

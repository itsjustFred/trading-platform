"""Cleanup 16-E (#41) — one documented meaning per sizing knob.

Decision Record (parent Cleanup 16, 2026-08-31; verbatim):

- ``max_position_exposure_pct`` = position-size semantics (per-position notional
  cap as a fraction of equity);
- ``risk_per_trade_pct`` = maximum loss per trade;
- ``max_total_exposure`` = aggregate exposure limit.

The deprecated ``max_position_pct`` spellings (config field, CLI flag, helper
name) keep old invocations working during migration and emit a
``DeprecationWarning``.
"""

from __future__ import annotations
from tests.helpers import with_zero_fees

import argparse
import contextlib
import io
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import _resolve_max_position_exposure_pct

# ─── BacktestConfig field semantics ─────────────────────────────────────────


class TestBacktestConfigField:
    @staticmethod
    def _config(**overrides) -> BacktestConfig:
        payload: dict = {
            "strategy_name": "s",
            "symbols": ["X"],
            "timeframe": "1d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        }
        payload.update(overrides)
        return BacktestConfig(**with_zero_fees(payload))

    def test_new_field_is_the_canonical_name(self):
        config = self._config(max_position_exposure_pct=0.75)
        assert config.max_position_exposure_pct == 0.75

    def test_rejects_non_positive_exposure(self):
        with pytest.raises(ValidationError):
            self._config(max_position_exposure_pct=0.0)

    def test_deprecated_alias_maps_onto_the_new_field_and_warns(self):
        with pytest.warns(DeprecationWarning):
            config = self._config(max_position_pct=0.4)
        assert config.max_position_exposure_pct == 0.4

    def test_alias_is_not_a_stored_field(self):
        config = self._config(max_position_exposure_pct=0.4)
        assert "max_position_pct" not in config.model_fields_set
        assert "max_position_exposure_pct" in config.model_fields_set

    def test_alias_validation_still_applies(self):
        with pytest.raises(ValidationError):
            self._config(max_position_pct=-0.1)

    def test_alias_default_stays_none(self):
        config = self._config()
        assert config.max_position_exposure_pct is None

    def test_both_spellings_conflict_is_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            self._config(max_position_exposure_pct=0.4, max_position_pct=0.5)

    def test_invalid_timeframe_is_rejected(self):
        with pytest.raises(ValidationError):
            self._config(timeframe="7y")

    def test_invalid_modeling_resolution_is_rejected(self):
        with pytest.raises(ValidationError):
            self._config(modeling_resolution="tick-per-second")

    def test_alias_is_a_documented_readonly_property(self):
        assert isinstance(getattr(BacktestConfig, "max_position_pct"), property)
        assert "max_position_exposure_pct" in BacktestConfig.model_fields
        assert "max_position_pct" not in BacktestConfig.model_fields


# ─── Helper rename (vectorbt adapter, imported by pynecore) ─────────────────


class TestResolveHelperRename:
    def test_new_helper_name_is_canonical(self):
        assert _resolve_max_position_exposure_pct(None, "unknown") == 0.20
        assert _resolve_max_position_exposure_pct(None, "equities") == 1.0
        assert _resolve_max_position_exposure_pct(0.3, "equities") == 0.3

    def test_deprecated_helper_alias_keeps_working_and_warns(self):
        from runner.engines import vectorbt_adapter

        with pytest.deprecated_call():
            resolved = vectorbt_adapter._resolve_max_position_pct(0.3, "unknown")
        assert resolved == 0.3

    def test_persisted_risk_math_key_stays_max_position_pct(self):
        """Artifact-key stability: the emitted keys in
        ``trade.meta["risk_math"]`` must keep the historical spelling
        (frontend ``TradeRiskMath`` + SQLite fallback read it), even though the
        config field was renamed in 16-E. The canonical
        ``max_position_exposure_pct`` spelling is emitted ALONGSIDE (PM-2
        sizing-record viewer contract) — never instead of — the legacy key."""
        import inspect

        from runner.engines import vectorbt_adapter

        source = inspect.getsource(vectorbt_adapter._process_symbol)
        assert '"max_position_pct":' in source
        assert '"max_position_exposure_pct":' in source


# ─── CLI flag alias ─────────────────────────────────────────────────────────


class TestCliFlagAlias:
    # Required arguments per subcommand so parse_args() reaches the sizing flags.
    _REQUIRED: dict[str, list[str]] = {
        "run": ["--engine", "vectorbt", "--strategy", "strategies/demo.py"],
        "walkforward": [
            "--strategy",
            "demo",
            "--symbols",
            "AAPL",
            "--start",
            "2024-01-01",
            "--end",
            "2024-12-31",
        ],
        "grid": ["--strategy", "strategies/demo.py"],
        "matrix": [],
    }

    @staticmethod
    def _parse(*argv: str, expect_warning: bool = False):
        import runner.cli as runner_cli

        parser = runner_cli.build_parser()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                args = parser.parse_args(list(argv))
        if expect_warning:
            assert any(issubclass(item.category, DeprecationWarning) for item in caught), (
                "deprecated --max-position-pct must emit a DeprecationWarning"
            )
        return args

    def test_new_flag_parses_for_every_subcommand(self):
        for subcommand, required in self._REQUIRED.items():
            args = self._parse(subcommand, *required, "--max-position-exposure-pct", "0.6")
            assert args.max_position_exposure_pct == 0.6, subcommand

    def test_deprecated_flag_still_parses_and_warns(self):
        args = self._parse(
            "run",
            *self._REQUIRED["run"],
            "--max-position-pct",
            "0.6",
            expect_warning=True,
        )
        assert args.max_position_exposure_pct == 0.6

    def test_flags_conflict_is_rejected(self):
        import runner.cli as runner_cli

        parser = runner_cli.build_parser()
        with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(
                [
                    "run",
                    *self._REQUIRED["run"],
                    "--max-position-pct",
                    "0.6",
                    "--max-position-exposure-pct",
                    "0.7",
                ]
            )


# ─── CLI run wires the new field through to BacktestConfig ──────────────────


class _CaptureEngine:
    name = "vectorbt"

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def validate_config(self, config):
        return []

    def run(self, config, strategy_path, progress_callback=None):
        self.captured["max_position_exposure_pct"] = config.max_position_exposure_pct
        self.captured["risk_per_trade_pct"] = config.risk_per_trade_pct
        from shared.models import BacktestMeta, BacktestResult

        return BacktestResult(
            meta=BacktestMeta(
                id="sizing-demo",
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
            trades=[],
            equity_curve=[],
            stats={},
        )


class TestCliRunWiring:
    def _run_cmd(self, monkeypatch, tmp_path: Path, flag: list[str]) -> dict[str, object]:
        import runner.cli as runner_cli

        engine = _CaptureEngine()
        monkeypatch.setattr(runner_cli, "get_engine", lambda _: engine)
        monkeypatch.setattr(runner_cli, "ensure_backtest_data", lambda **_kwargs: [])
        monkeypatch.setattr(runner_cli, "save_result_to_sqlite", lambda _result: None)

        args = runner_cli.build_parser().parse_args(
            [
                "run",
                "--engine",
                "vectorbt",
                "--strategy",
                "strategies/demo.py",
                "--symbols",
                "AAPL",
                *flag,
                "--risk-pct",
                "0.01",
                "--start",
                "2024-01-01",
                "--end",
                "2024-02-01",
                "--data-dir",
                str(tmp_path),
                "--output",
                str(tmp_path / "out.trades.json"),
                "--skip-sqlite",
            ]
        )
        runner_cli.cmd_run(args)
        return engine.captured

    def test_new_flag_reaches_backtest_config(self, monkeypatch, tmp_path: Path):
        captured = self._run_cmd(
            monkeypatch, tmp_path, ["--max-position-exposure-pct", "0.5"]
        )
        assert captured["max_position_exposure_pct"] == 0.5
        assert captured["risk_per_trade_pct"] == 0.01

    def test_deprecated_flag_still_reaches_backtest_config(self, monkeypatch, tmp_path: Path):
        captured = self._run_cmd(monkeypatch, tmp_path, ["--max-position-pct", "0.5"])
        assert captured["max_position_exposure_pct"] == 0.5


# ─── Native engine: exposure semantics without the silent divergence ────────


class TestNativeEngineExposureWiring:
    """F7, native half: ``max_position_exposure_pct`` is the aggregate exposure
    input the native TradeEngine feeds into ``RiskConfig.max_total_exposure`` —
    explicitly, no undocumented 5% default divergence."""

    @staticmethod
    def _engine(**overrides):
        from runner.engine import TradeEngine

        payload: dict = {
            "strategy_name": "s",
            "symbols": ["X"],
            "timeframe": "1d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "initial_capital": 100_000.0,
            "parameters": {},
        }
        payload.update(overrides)
        return TradeEngine(BacktestConfig(**with_zero_fees(payload)), symbol="X")

    def test_exposure_flag_feeds_max_total_exposure(self):
        engine = self._engine(max_position_exposure_pct=0.4)
        assert engine._risk_engine.config.max_total_exposure == 0.4

    def test_unset_flag_keeps_the_documented_risk_engine_default(self):
        engine = self._engine()
        assert engine._risk_engine.config.max_total_exposure == 0.05

    def test_unset_default_is_pinned_to_risk_config_itself(self):
        """The fallback must stay RiskConfig's own default, not a literal.

        A literal ``0.05`` here would silently diverge the moment
        ``risk.RiskConfig.max_total_exposure`` changes its default.
        """
        from risk.config import RiskConfig

        engine = self._engine()
        assert engine._risk_engine.config.max_total_exposure == (
            RiskConfig().max_total_exposure
        )

    def test_deprecated_alias_feeds_the_same_value(self):
        with pytest.warns(DeprecationWarning):
            engine = self._engine(max_position_pct=0.4)
        assert engine._risk_engine.config.max_total_exposure == 0.4

    def test_risk_per_trade_pct_is_the_maximum_loss_per_trade(self):
        engine = self._engine(risk_per_trade_pct=0.01)
        sized = engine._risk_engine.calculate_position_size(
            signal=_signal(entry_price=100.0, stop_loss=95.0),
            account_balance=100_000.0,
            pip_value=1.0,
            current_exposure=0.0,
        )
        assert sized.risk_amount == pytest.approx(1_000.0)
        assert sized.risk_percent == pytest.approx(0.01)
        assert sized.position_size == pytest.approx(200.0)


def _signal(*, entry_price: float, stop_loss: float):
    from risk.models import TradeSignal

    return TradeSignal(
        action="buy",
        symbol="X",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=None,
        timeframe="1d",
        metadata={},
    )


# ─── Batch entry points accept both spellings ───────────────────────────────


class TestBatchEntryPoints:
    @staticmethod
    def _strategy(tmp_path: Path) -> Path:
        strategy = tmp_path / "sizing_stub.py"
        strategy.write_text("STRATEGY_META = {'name': 'sizing_stub'}\n", encoding="utf-8")
        return strategy

    @staticmethod
    def _stub_grid(monkeypatch, tmp_path: Path) -> list[dict]:
        """Stub the per-cell backtest so the param plumbing can be asserted
        without touching market data (mirrors tests/test_backtest_grid.py)."""
        import runner.backtest_grid as bg

        calls: list[dict] = []

        def fake_run_one(**kwargs):
            calls.append(kwargs)
            return {
                "symbol": kwargs["symbol"],
                "timeframe": kwargs["timeframe"],
                "status": "ok",
                "total_trades": 1,
                "total_pnl": 1.0,
                "net_profit_pct": 0.01,
                "sharpe": 0.1,
                "cagr": 0.01,
                "max_drawdown_pct": -0.5,
                "win_rate": 1.0,
                "output": str(kwargs["output_dir"] / "fake.trades.json"),
            }

        monkeypatch.setattr(bg, "run_one_backtest", fake_run_one)
        return calls

    @pytest.mark.parametrize(
        ("kwargs", "expected_summary_value"),
        [
            ({"max_position_exposure_pct": 0.5}, 0.5),
            ({"max_position_pct": 0.5}, 0.5),
        ],
        ids=["new-name", "deprecated-alias"],
    )
    def test_run_backtest_grid_accepts_both_spellings(
        self, tmp_path, monkeypatch, kwargs, expected_summary_value
    ):
        calls = self._stub_grid(monkeypatch, tmp_path)
        from runner.backtest_grid import run_backtest_grid

        summary = run_backtest_grid(
            strategy=str(self._strategy(tmp_path)),
            symbols=["XAUUSD"],
            timeframes=["1h"],
            start="2025-01-01",
            end="2025-01-31",
            risk_pct=0.01,
            output_dir=tmp_path / f"out-{list(kwargs)[0]}",
            max_workers=1,
            **kwargs,
        )
        assert summary["runs"][0]["status"] == "ok", summary["runs"][0].get("error")
        assert summary["max_position_exposure_pct"] == expected_summary_value
        assert {call["max_position_exposure_pct"] for call in calls} == {0.5}

    def test_run_backtest_grid_both_spellings_conflict_is_rejected(self, tmp_path):
        from runner.backtest_grid import run_backtest_grid

        # Strategy setup runs outside pytest.raises so the guarded block holds
        # only the invocation that must raise (S5778).
        strategy = str(self._strategy(tmp_path))
        with pytest.raises(ValueError, match="not both"):
            run_backtest_grid(
                strategy=strategy,
                symbols=["XAUUSD"],
                timeframes=["1h"],
                max_position_exposure_pct=0.5,
                max_position_pct=0.5,
            )

    def test_walkforward_accepts_both_spellings(self, tmp_path, monkeypatch):
        import runner.walkforward as wf

        # Spy on the fold backtest: the alias mapping happens before folds run,
        # and a real multi-fold backtest is unnecessary for this assertion.
        captured: list[dict] = []

        def _fake_fold(**kwargs):
            captured.append(kwargs)
            return {"sharpe_ratio": 0.1, "sharpe": 0.1}

        monkeypatch.setattr(wf, "_run_fold_backtest", _fake_fold)

        common = dict(
            strategy=str(self._strategy(tmp_path)),
            symbols=["XAUUSD"],
            timeframe="1h",
            capital=100_000.0,
            train_months=1,
            test_months=1,
            step_months=1,
            output_dir=tmp_path / "wf",
        )
        new_payload = wf.run_walkforward(
            start="2025-01-01",
            end="2025-06-30",
            max_position_exposure_pct=0.5,
            **common,
        )
        legacy_payload = wf.run_walkforward(
            start="2025-01-01",
            end="2025-06-30",
            max_position_pct=0.5,
            **common,
        )
        assert new_payload["config"]["max_position_exposure_pct"] == 0.5
        assert legacy_payload["config"]["max_position_exposure_pct"] == 0.5
        assert {fold["max_position_exposure_pct"] for fold in captured} == {0.5}

    def test_walkforward_train_and_test_folds_get_identical_sizing_context(
        self, tmp_path, monkeypatch
    ):
        """Both folds of every window must receive byte-identical sizing and
        context parameters — a parameter reaching only the train call would
        silently size OOS folds differently from IS folds."""
        import runner.walkforward as wf

        captured: list[dict] = []

        def _fake_fold(**kwargs):
            captured.append(kwargs)
            return {"sharpe_ratio": 0.1, "sharpe": 0.1}

        monkeypatch.setattr(wf, "_run_fold_backtest", _fake_fold)
        wf.run_walkforward(
            strategy=str(self._strategy(tmp_path)),
            symbols=["XAUUSD"],
            timeframe="1h",
            start="2025-01-01",
            end="2025-06-30",
            capital=100_000.0,
            risk_pct=0.01,
            max_position_exposure_pct=0.5,
            max_leverage=2.0,
            train_months=1,
            test_months=1,
            step_months=1,
            output_dir=tmp_path / "wf",
        )
        assert len(captured) >= 2, captured
        for train, test in zip(captured[0::2], captured[1::2]):
            train_fold = {k: v for k, v in train.items() if k not in {"start", "end"}}
            test_fold = {k: v for k, v in test.items() if k not in {"start", "end"}}
            assert train_fold == test_fold
            assert train_fold["max_position_exposure_pct"] == 0.5

    def test_walkforward_alias_and_new_name_conflict_is_rejected(self, tmp_path):
        from runner.walkforward import run_walkforward

        # Strategy setup runs outside pytest.raises so the guarded block holds
        # only the invocation that must raise (S5778).
        strategy = str(self._strategy(tmp_path))
        with pytest.raises(ValueError, match="not both"):
            run_walkforward(
                strategy=strategy,
                symbols=["XAUUSD"],
                start="2025-01-01",
                end="2025-06-30",
                max_position_exposure_pct=0.5,
                max_position_pct=0.5,
            )


# ─── argparse wiring parity across all four subcommands ─────────────────────


def test_every_subcommand_exposes_the_new_flag_with_documented_help():
    import runner.cli as runner_cli

    parser = runner_cli.build_parser()
    for subcommand in ("run", "walkforward", "grid", "matrix"):
        subparser = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices[subcommand]
        option_strings = {
            option
            for action in subparser._actions
            for option in action.option_strings
        }
        assert "--max-position-exposure-pct" in option_strings, subcommand
        assert "--max-position-pct" in option_strings, subcommand


# ─── standalone module mains (runner -m entry points) ───────────────────────


@pytest.mark.parametrize(
    ("module_name", "flag"),
    [
        ("runner.backtest_grid", "--max-position-pct"),
        ("runner.backtest_matrix", "--max-position-pct"),
        ("runner.backtest_grid", "--max-position-exposure-pct"),
        ("runner.backtest_matrix", "--max-position-exposure-pct"),
    ],
)
def test_standalone_main_wires_the_sizing_flag(
    monkeypatch, tmp_path: Path, module_name: str, flag: str
):
    """``python -m runner.backtest_grid --max-position-pct …`` must reach the
    batch entry point: the standalone mains parse BOTH spellings and forward
    them under the canonical + deprecated keywords, so the entry point's shared
    resolver performs the fold (warning + conflict rejection) exactly as for
    direct keyword callers."""
    import importlib

    from runner.engines.base import resolve_legacy_max_position_kwarg

    module = importlib.import_module(module_name)
    runner_name = "run_backtest_grid" if "grid" in module_name else "run_backtest_matrix"
    captured: dict = {}

    def fake_entry(**kwargs):
        # Emulate the entry point's alias fold (the real function does this
        # before any cell runs; stubbing only isolates the wiring).
        folded = resolve_legacy_max_position_kwarg(
            kwargs.get("max_position_exposure_pct"),
            kwargs.get("max_position_pct"),
            caller=runner_name,
        )
        captured["max_position_exposure_pct"] = folded
        # Standalone mains exit non-zero on failed cells/runs.
        return {"runs": [], "cells": [], "failed": 0, "failed_cells": 0}

    monkeypatch.setattr(module, runner_name, fake_entry)
    strategy = tmp_path / "sizing_stub.py"
    strategy.write_text("STRATEGY_META = {'name': 'sizing_stub'}\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--strategy",
            str(strategy),
            "--symbols",
            "XAUUSD",
            "--timeframes",
            "1h",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-31",
            flag,
            "0.5",
        ],
    )
    module.main()

    assert captured["max_position_exposure_pct"] == 0.5


@pytest.mark.parametrize("module_name", ["runner.backtest_grid", "runner.backtest_matrix"])
def test_standalone_main_rejects_both_sizing_spellings(
    monkeypatch, tmp_path: Path, module_name: str
):
    """Passing both spellings on a standalone main's command line must fail
    loudly (the entry point's conflict check), not silently pick one."""
    import importlib

    module = importlib.import_module(module_name)
    runner_name = "run_backtest_grid" if "grid" in module_name else "run_backtest_matrix"

    def rejecting_entry(**kwargs):
        from runner.engines.base import resolve_legacy_max_position_kwarg

        resolve_legacy_max_position_kwarg(
            kwargs.get("max_position_exposure_pct"),
            kwargs.get("max_position_pct"),
            caller=runner_name,
        )
        return {"runs": [], "cells": [], "failed": 0, "failed_cells": 0}

    monkeypatch.setattr(module, runner_name, rejecting_entry)
    strategy = tmp_path / "sizing_stub.py"
    strategy.write_text("STRATEGY_META = {'name': 'sizing_stub'}\n", encoding="utf-8")
    # The standalone mains receive argv WITHOUT the module name (that part is
    # consumed by `python -m` before main() sees it).
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--strategy",
            str(strategy),
            "--max-position-pct",
            "0.5",
            "--max-position-exposure-pct",
            "0.7",
        ],
    )
    with pytest.raises(ValueError, match="not both"):
        module.main()

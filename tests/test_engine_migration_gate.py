"""Tests for ``scripts/engine_migration_gate.py`` (issue #318 phase S).

The gate compares ``NativeAdapter`` and ``VectorbtAdapter`` over registry
strategies and reports per-strategy trade/stat diffs — it flips no defaults.
These tests exercise it on a tiny synthetic dataset and a throwaway strategy
tree so the suite stays fast and data-independent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import engine_migration_gate as gate
from tests.helpers import deterministic_ohlcv_frame, sma_crossover_strategy_text, write_ohlcv

SMA_STRATEGY = "gate_sma"
BROKEN_STRATEGY = "gate_broken"
AGENT_STRATEGY = "gate_agent"
PYNE_STRATEGY = "gate_pyne"


def _write_strategy(strategies_dir: Path, slug: str, text: str) -> Path:
    folder = strategies_dir / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}.py"
    path.write_text(text)
    return path


@pytest.fixture
def gate_env(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _write_strategy(
        strategies_dir, SMA_STRATEGY, sma_crossover_strategy_text(SMA_STRATEGY)
    )
    _write_strategy(
        strategies_dir,
        BROKEN_STRATEGY,
        "STRATEGY_META = {'name': 'broken', 'version': '1.0'}\n"
        "def not_signals(df, params):\n    return None\n",
    )
    _write_strategy(
        strategies_dir,
        AGENT_STRATEGY,
        "STRATEGY_META = {'name': 'agent', 'version': '1.0', "
        "'strategy_type': 'signal_processor'}\n"
        "def generate_signals(df, params):\n    return None\n",
    )
    _write_strategy(
        strategies_dir,
        PYNE_STRATEGY,
        "STRATEGY_META = {'name': 'pyne', 'version': '1.0', 'engine': 'pynecore'}\n"
        "def generate_signals(df, params):\n    return None\n",
    )

    data_dir = tmp_path / "data"
    frame = deterministic_ohlcv_frame(periods=300)
    write_ohlcv(data_dir, "GSYN", frame)
    return {"strategies_dir": strategies_dir, "data_dir": data_dir}


def _gate_config(env, **overrides) -> gate.GateConfig:
    payload = {
        "data_dir": str(env["data_dir"]),
        "symbols": ["GSYN"],
        "timeframe": "1h",
        "start": "2024-01-01",
        "end": "2024-02-01",
        "strategies_dir": env["strategies_dir"],
    }
    payload.update(overrides)
    return gate.GateConfig(**payload)


class TestRunGate:
    def test_report_covers_every_strategy_with_per_engine_outcomes(self, gate_env):
        report = gate.run_gate(_gate_config(gate_env))

        by_slug = {s["slug"]: s for s in report["strategies"]}
        assert set(by_slug) == {
            SMA_STRATEGY,
            BROKEN_STRATEGY,
            AGENT_STRATEGY,
            PYNE_STRATEGY,
        }

        sma = by_slug[SMA_STRATEGY]
        # ``engine`` is the EFFECTIVE engine (undeclared → vectorbt default).
        assert sma["engine"] == "vectorbt"
        assert sma["native"]["status"] == "ok"
        assert sma["vectorbt"]["status"] == "ok"
        assert sma["native"]["trade_count"] > 0
        assert sma["diff"]["trade_count"]["delta"] == 0
        assert sma["status"] in {"identical", "trades_differ", "stats_differ"}
        # The diff report must let a reviewer explain any difference.
        assert "only_in_native" in sma["diff"]
        assert "only_in_vectorbt" in sma["diff"]
        assert "stats_diffs" in sma["diff"]

        broken = by_slug[BROKEN_STRATEGY]
        assert broken["status"] == "engine_error"
        assert broken["native"]["status"] == "error"
        assert broken["vectorbt"]["status"] == "error"
        assert broken["diff"] is None

        agent = by_slug[AGENT_STRATEGY]
        assert agent["status"] == "skipped"
        assert "signal_processor" in agent["reason"]

        pyne = by_slug[PYNE_STRATEGY]
        assert pyne["status"] == "skipped"
        assert "pynecore" in pyne["reason"]
        # Skipped strategies never reach an engine — no native/vectorbt keys.
        assert "native" not in pyne and "vectorbt" not in pyne

        summary = report["summary"]
        assert summary["total"] == 4
        assert summary["skipped"] == 2
        assert summary["engine_error"] == 1

    def test_only_filter_limits_the_run(self, gate_env):
        report = gate.run_gate(_gate_config(gate_env, only=(SMA_STRATEGY,)))
        assert [s["slug"] for s in report["strategies"]] == [SMA_STRATEGY]

    def test_write_report_produces_valid_json_under_root(self, gate_env, tmp_path, monkeypatch):
        # write_report confines --output to the project root; point the root at
        # tmp_path so the test never writes into the repo (data/ is guarded).
        monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
        report = gate.run_gate(_gate_config(gate_env, only=(SMA_STRATEGY,)))
        out = gate.write_report(report, Path("reports/test-report.json"))
        payload = json.loads(out.read_text())
        assert payload["gate"].startswith("native-vs-vectorbt")
        assert payload["strategies"][0]["slug"] == SMA_STRATEGY

    def test_write_report_refuses_paths_outside_root(self, gate_env, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path / "root")
        (tmp_path / "root").mkdir()
        report = gate.run_gate(_gate_config(gate_env, only=(SMA_STRATEGY,)))
        with pytest.raises(ValueError, match="refusing"):
            gate.write_report(report, tmp_path / "escape.json")


class TestAlignedSizingCaps:
    """The gate must pin the same sizing caps on both engines.

    Without explicit values each adapter falls back to its own defaults
    (native 50% notional, vectorbt the 20% non-equity cap and 2.0x
    leverage), so the default report would diff configuration, not engine
    logic.
    """

    def test_gate_config_defaults_pin_both_engines(self):
        cfg = gate.GateConfig(data_dir="x", symbols=["S"])
        assert cfg.max_position_exposure_pct == 0.2
        assert cfg.max_leverage == 2.0

    def test_backtest_config_carries_the_pinned_caps(self, gate_env):
        config = gate._backtest_config(_gate_config(gate_env), "slug")
        assert config.max_position_exposure_pct == 0.2
        assert config.max_leverage == 2.0

    def test_cli_overrides_reach_the_config(self, gate_env):
        config = gate._backtest_config(
            _gate_config(gate_env, max_position_exposure_pct=0.5, max_leverage=5.0),
            "slug",
        )
        assert config.max_position_exposure_pct == 0.5
        assert config.max_leverage == 5.0

    def test_report_records_the_pinned_caps(self, gate_env):
        report = gate.run_gate(_gate_config(gate_env, only=(SMA_STRATEGY,)))
        assert report["config"]["max_position_exposure_pct"] == 0.2
        assert report["config"]["max_leverage"] == 2.0


class TestDefaultParameters:
    def test_defaults_declare_fees_and_slippage_explicitly(self, gate_env):
        # #84 fails closed without a fee declaration; once #354 lands the
        # native engine defaults to auto slippage while vectorbt models
        # none — "slippage": "none" keeps the comparison gross on both.
        assert gate.DEFAULT_PARAMETERS["fees"] == "zero"
        assert gate.DEFAULT_PARAMETERS["slippage"] == "none"
        config = _gate_config(gate_env)
        assert config.parameters["slippage"] == "none"
        assert config.parameters["fees"] == "zero"


class TestEmptyRunRefused:
    def test_only_matching_nothing_exits_nonzero(self, gate_env):
        with pytest.raises(SystemExit, match="no strategy entries"):
            gate.run_gate(_gate_config(gate_env, only=("no_such_slug",)))

    def test_empty_strategies_dir_exits_nonzero(self, gate_env, tmp_path):
        empty = tmp_path / "empty_strategies"
        empty.mkdir()
        with pytest.raises(SystemExit, match="no strategy entries"):
            gate.run_gate(_gate_config(gate_env, strategies_dir=empty))


class TestDataResolution:
    def test_explicit_data_dir_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(gate.TEST_OHLCV_DIR_ENV, str(tmp_path / "env_ohlcv"))
        resolution = gate._resolve_data_root(str(tmp_path / "explicit"))
        assert resolution.mode == "data_dir"
        assert resolution.data_root == tmp_path / "explicit"

    def test_env_dir_named_ohlcv_uses_parent(self, tmp_path, monkeypatch):
        ohlcv = tmp_path / "datasets" / "ohlcv"
        ohlcv.mkdir(parents=True)
        monkeypatch.setenv(gate.TEST_OHLCV_DIR_ENV, str(ohlcv))
        resolution = gate._resolve_data_root("")
        assert resolution.mode == gate.TEST_OHLCV_DIR_ENV
        assert resolution.data_root == tmp_path / "datasets"
        assert resolution.cleanup is None

    def test_env_dir_with_ohlcv_subdir_used_directly(self, tmp_path, monkeypatch):
        root = tmp_path / "dataroot"
        (root / "ohlcv").mkdir(parents=True)
        monkeypatch.setenv(gate.TEST_OHLCV_DIR_ENV, str(root))
        resolution = gate._resolve_data_root("")
        assert resolution.data_root == root

    def test_env_content_root_gets_symlink_wrapper(self, tmp_path, monkeypatch):
        content = tmp_path / "market_data"
        content.mkdir()
        monkeypatch.setenv(gate.TEST_OHLCV_DIR_ENV, str(content))
        resolution = gate._resolve_data_root("")
        assert resolution.mode == gate.TEST_OHLCV_DIR_ENV
        assert (resolution.data_root / "ohlcv").is_symlink()
        assert os.path.realpath(resolution.data_root / "ohlcv") == os.path.realpath(content)
        resolution.cleanup.cleanup()

    def test_synthetic_fallback_uses_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv(gate.TEST_OHLCV_DIR_ENV, raising=False)
        resolution = gate._resolve_data_root("", synthetic_dir=tmp_path / "synth")
        assert resolution.mode == "synthetic"
        assert resolution.data_root == tmp_path / "synth"


class TestDiffHelpers:
    def test_numbers_close_treats_non_numeric_as_equality(self):
        assert gate._numbers_close(None, None)
        assert gate._numbers_close("x", "x")
        assert not gate._numbers_close(None, 0.0)
        assert gate._numbers_close(1.0000000001, 1.0)
        assert not gate._numbers_close(1.1, 1.0)


def _trade(**overrides) -> SimpleNamespace:
    base = {
        "symbol": "GSYN",
        "direction": "long",
        "entry_time": "2024-01-10T10:00:00+00:00",
        "entry_price": 100.0,
        "quantity": 10.0,
        "exits": [
            SimpleNamespace(
                time="2024-01-11T10:00:00+00:00",
                price=101.0,
                quantity=10.0,
                pnl=10.0,
                reason="signal",
            )
        ],
        "pnl": 10.0,
        "fees": 0.5,
        "net_pnl": 9.5,
        "exit_reason": "signal",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTradeSignaturePrecision:
    """The gate must not call representation rounding a trade difference.

    The vectorbt adapter persists prices at 6dp, quantities at 4dp and money
    at 2dp while native stores unrounded floats; pairs compare at one stored
    unit of tolerance so equivalent fills still report ``identical`` and a
    real divergence is reported as a self-explaining per-field diff.
    """

    def test_stored_precision_rounding_still_reports_identical(self):
        native_trade = _trade(
            entry_price=100.1234567891,
            quantity=3.1415926535,
            pnl=12.345678,
            fees=0.123456,
            net_pnl=12.222222,
            exits=[
                SimpleNamespace(
                    time="2024-01-11T10:00:00+00:00",
                    price=101.987654321,
                    quantity=3.1415926535,
                    pnl=12.345678,
                    reason="signal",
                )
            ],
        )
        # The real vectorbt adapter leaves exit-leg ``reason`` as None —
        # an unlabelled leg must not diff against native's "signal".
        vectorbt_trade = _trade(
            entry_price=100.123457,
            quantity=3.1416,
            pnl=12.35,
            fees=0.12,
            net_pnl=12.22,
            exits=[
                SimpleNamespace(
                    time="2024-01-11T10:00:00+00:00",
                    price=101.987654,
                    quantity=3.1416,
                    pnl=12.35,
                    reason=None,
                )
            ],
        )
        diff = gate._diff_trades(
            SimpleNamespace(trades=[native_trade]),
            SimpleNamespace(trades=[vectorbt_trade]),
        )
        assert diff["trades_identical"] is True
        assert diff["matched_pairs"] == 1
        assert diff["field_diffs"] == []
        assert diff["only_in_native_count"] == 0
        assert diff["only_in_vectorbt_count"] == 0

    def test_qty_straddle_within_one_stored_unit_reports_identical(self):
        """A 1e-4 quantity straddle (rounding boundary) is not a difference."""
        native_trade = _trade(quantity=151.38114999, pnl=10.004, net_pnl=9.504)
        vectorbt_trade = _trade(quantity=151.3812, pnl=10.0, net_pnl=9.5)
        diff = gate._diff_trades(
            SimpleNamespace(trades=[native_trade]),
            SimpleNamespace(trades=[vectorbt_trade]),
        )
        assert diff["trades_identical"] is True
        assert diff["field_diffs"] == []

    def test_real_fill_difference_still_diffs(self):
        native_trade = _trade(quantity=3.1416)
        vectorbt_trade = _trade(quantity=3.15)
        diff = gate._diff_trades(
            SimpleNamespace(trades=[native_trade]),
            SimpleNamespace(trades=[vectorbt_trade]),
        )
        assert diff["trades_identical"] is False
        assert diff["matched_pairs"] == 1
        assert diff["only_in_native_count"] == 0
        assert diff["only_in_vectorbt_count"] == 0
        # The diff must explain itself: which field, on which trade, both values.
        assert diff["field_diffs_count"] == 1
        entry = diff["field_diffs"][0]
        assert entry["field"] == "quantity"
        assert entry["trade"]["symbol"] == "GSYN"
        assert entry["native"] == pytest.approx(3.1416)
        assert entry["vectorbt"] == pytest.approx(3.15)

    def test_label_only_difference_still_diffs_when_both_sides_label(self):
        """Two different non-empty exit reasons are a real label diff."""
        native_trade = _trade(
            exits=[
                SimpleNamespace(
                    time="2024-01-11T10:00:00+00:00",
                    price=101.0,
                    quantity=10.0,
                    pnl=10.0,
                    reason="stop_loss",
                )
            ]
        )
        vectorbt_trade = _trade(
            exits=[
                SimpleNamespace(
                    time="2024-01-11T10:00:00+00:00",
                    price=101.0,
                    quantity=10.0,
                    pnl=10.0,
                    reason="signal",
                )
            ]
        )
        diff = gate._diff_trades(
            SimpleNamespace(trades=[native_trade]),
            SimpleNamespace(trades=[vectorbt_trade]),
        )
        assert diff["trades_identical"] is False
        fields = {d["field"] for d in diff["field_diffs"]}
        assert "exits[0].reason" in fields

    def test_unpaired_trades_land_in_only_in_lists(self):
        native_trade = _trade()
        other_entry = _trade(entry_time="2024-02-01T10:00:00+00:00")
        diff = gate._diff_trades(
            SimpleNamespace(trades=[native_trade]),
            SimpleNamespace(trades=[other_entry]),
        )
        assert diff["trades_identical"] is False
        assert diff["matched_pairs"] == 0
        assert diff["only_in_native_count"] == 1
        assert diff["only_in_vectorbt_count"] == 1

    def test_trade_count_difference_still_diffs(self):
        diff = gate._diff_trades(
            SimpleNamespace(trades=[_trade()]),
            SimpleNamespace(trades=[_trade(), _trade(entry_time="2024-02-01T10:00:00+00:00")]),
        )
        assert diff["trades_identical"] is False
        assert diff["trade_count"]["delta"] == 1


class TestDiffStats:
    def test_matching_stats_report_identical(self):
        native = SimpleNamespace(
            stats={"sharpe": 1.0, "total_return": 0.05, "extras": {"a": 1}}
        )
        vectorbt = SimpleNamespace(
            stats={
                "sharpe": 1.0,
                "total_return": 0.0500000001,
                "extras": {"b": 2},
            }
        )
        diff = gate._diff_stats(native, vectorbt)
        assert diff["stats_identical"] is True
        assert diff["stats_diffs"] == {}

    def test_real_stat_difference_is_reported(self):
        native = SimpleNamespace(stats={"sharpe": 1.0, "total_return": 0.05})
        vectorbt = SimpleNamespace(stats={"sharpe": 1.0, "total_return": 0.08})
        diff = gate._diff_stats(native, vectorbt)
        assert diff["stats_identical"] is False
        assert diff["stats_diffs"]["total_return"] == {
            "native": 0.05,
            "vectorbt": 0.08,
        }

    def test_stored_precision_noise_still_reports_identical(self):
        """Stats recomputed from each engine's own rounded trades differ by
        at most one stored unit — that is representation noise, not a diff
        (the bundle-35 reproduction: total_pnl ±0.01, sortino ±1e-4)."""
        native = SimpleNamespace(
            stats={"total_pnl": 9401.36, "sortino_ratio": 1.5482, "total_trades": 219}
        )
        vectorbt = SimpleNamespace(
            stats={"total_pnl": 9401.37, "sortino_ratio": 1.5483, "total_trades": 219}
        )
        diff = gate._diff_stats(native, vectorbt)
        assert diff["stats_identical"] is True
        assert diff["stats_diffs"] == {}

    def test_real_money_difference_still_diffs(self):
        native = SimpleNamespace(stats={"total_pnl": 9401.36})
        vectorbt = SimpleNamespace(stats={"total_pnl": 9402.0})
        diff = gate._diff_stats(native, vectorbt)
        assert diff["stats_identical"] is False
        assert diff["stats_diffs"]["total_pnl"] == {
            "native": 9401.36,
            "vectorbt": 9402.0,
        }

    def test_unmapped_keys_compare_strictly_and_extras_keys_are_skipped(self):
        native = SimpleNamespace(
            stats={"custom_count": 5, "ruined": False, "risk_free_rate": 0.0}
        )
        vectorbt = SimpleNamespace(
            stats={"custom_count": 5.00001, "ruined": True, "risk_free_rate": 0.05}
        )
        diff = gate._diff_stats(native, vectorbt)
        # Flattened extras diagnostics never diff; the unmapped count does.
        assert set(diff["stats_diffs"]) == {"custom_count"}


class TestDetectWindow:
    def _write_shard(self, root: Path, symbol: str, start: str, end: str) -> None:
        folder = root / "ohlcv" / symbol
        folder.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range(start, end, freq="h", tz="UTC"),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        )
        frame.to_parquet(folder / "1h.parquet", index=False)

    def test_window_is_the_intersection_not_the_union(self, tmp_path):
        self._write_shard(tmp_path, "AAA", "2024-01-01", "2024-06-30")
        self._write_shard(tmp_path, "BBB", "2024-03-01", "2024-09-30")
        window = gate._detect_window(tmp_path / "ohlcv", ["AAA", "BBB"], "1h")
        assert window == ("2024-03-01", "2024-06-30")

    def test_missing_symbol_returns_none(self, tmp_path):
        self._write_shard(tmp_path, "AAA", "2024-01-01", "2024-06-30")
        assert gate._detect_window(tmp_path / "ohlcv", ["AAA", "MISSING"], "1h") is None

    def test_window_comes_from_the_first_shard_the_adapter_reads(self, tmp_path):
        """Two shards for one symbol: the adapters take ``matches[0]`` of the
        sorted rglob, so the detected window must be that shard's extent —
        not the union and not the last shard's."""
        for name, start, end in (
            ("a", "2024-01-01", "2024-03-31"),
            ("b", "2024-06-01", "2024-09-30"),
        ):
            folder = tmp_path / "ohlcv" / name / "AAA"
            folder.mkdir(parents=True)
            frame = pd.DataFrame(
                {
                    "timestamp": pd.date_range(start, end, freq="h", tz="UTC"),
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
            )
            frame.to_parquet(folder / "1h.parquet", index=False)
        window = gate._detect_window(tmp_path / "ohlcv", ["AAA"], "1h")
        assert window == ("2024-01-01", "2024-03-31")


class TestSyntheticOhlcv:
    def test_unmapped_timeframe_refuses_instead_of_guessing(self, tmp_path):
        with pytest.raises(ValueError, match="tick"):
            gate._write_synthetic_ohlcv(
                tmp_path, ["X"], "tick", "2024-01-01", "2024-01-02"
            )

    @pytest.mark.parametrize(
        "timeframe,start,end",
        [("s1", "2024-01-01", "2024-01-02"), ("1w", "2024-01-01", "2024-02-01")],
    )
    def test_supported_timeframes_write_bars(self, tmp_path, timeframe, start, end):
        bars = gate._write_synthetic_ohlcv(tmp_path, ["X"], timeframe, start, end)
        assert bars > 0
        assert list(tmp_path.rglob(f"{timeframe}.parquet"))


class TestSyntheticWindowGuard:
    def _args(self, **overrides):
        payload = {"symbols": "", "start": "", "end": "", "max_symbols": 3}
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def test_estimate_scales_with_timeframe(self):
        assert gate._estimate_synthetic_bars("1h", "2024-01-01", "2024-01-08") == 169
        assert (
            gate._estimate_synthetic_bars("s1", "2024-01-01", "2024-06-30")
            > 15_000_000
        )
        assert gate._estimate_synthetic_bars("bogus", "2024-01-01", "2024-06-30") == 0

    def test_oversized_default_window_refuses(self, tmp_path):
        # The six-month default at s1 is ~15.5M rows — several GB.
        resolution = gate.DataResolution(data_root=tmp_path, mode="synthetic")
        with pytest.raises(SystemExit, match="--start/--end"):
            gate._resolve_symbols_and_window(
                self._args(), resolution, tmp_path / "ohlcv", "s1"
            )

    def test_explicit_bounds_opt_in_to_the_same_window(self, tmp_path):
        resolution = gate.DataResolution(data_root=tmp_path, mode="synthetic")
        symbols, start, end = gate._resolve_symbols_and_window(
            self._args(start="2024-01-01", end="2024-01-02"),
            resolution,
            tmp_path / "ohlcv",
            "s1",
        )
        assert (start, end) == ("2024-01-01", "2024-01-02")
        assert symbols == ["SYNTH_A", "SYNTH_B"]

    def test_small_default_window_still_runs(self, tmp_path):
        resolution = gate.DataResolution(data_root=tmp_path, mode="synthetic")
        symbols, start, end = gate._resolve_symbols_and_window(
            self._args(), resolution, tmp_path / "ohlcv", "1h"
        )
        assert (start, end) == ("2024-01-01", "2024-06-30")
        assert list((tmp_path / "ohlcv").rglob("*.parquet"))


class TestSyntheticDataResolution:
    def test_default_synthetic_dir_is_a_throwaway_tempdir(self, tmp_path, monkeypatch):
        monkeypatch.delenv(gate.TEST_OHLCV_DIR_ENV, raising=False)
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        resolution = gate._resolve_data_root("")
        try:
            assert resolution.mode == "synthetic"
            assert resolution.cleanup is not None
            # Must not write fixture data next to real market data.
            assert not str(resolution.data_root).startswith(str(tmp_path / "data"))
        finally:
            if resolution.cleanup is not None:
                resolution.cleanup.cleanup()

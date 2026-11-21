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

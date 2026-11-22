"""Tests for ``scripts/engine_speed_benchmark.py`` (issue #318 phase S).

The benchmark is report-only tooling, but its grid derivation, parameter
parsing and summary ratio are pure functions worth pinning down.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import engine_speed_benchmark as bench


def _strategy(path: Path, parameters: dict) -> Path:
    path.write_text(
        "STRATEGY_META = {"
        f"'name': 'bench', 'version': '1.0', 'parameters': {parameters!r}"
        "}\n"
        "def signals(df, params):\n    return df\n"
    )
    return path


class TestDefaultParamGrid:
    def test_numeric_meta_parameters_drive_the_grid(self, tmp_path):
        strategy = _strategy(
            tmp_path / "bench.py", {"fast": 10, "slow": 50, "label": "x"}
        )
        cells = bench._default_param_grid(strategy, 9)
        assert len(cells) == 9
        # Sorted numeric names only; multipliers 0.8/1.0/1.2 in product order.
        assert cells[0] == {"fast": 8.0, "slow": 40.0}
        assert cells[-1] == {"fast": 12.0, "slow": 60.0}
        assert all("label" not in cell for cell in cells)

    def test_max_cells_caps_the_grid(self, tmp_path):
        strategy = _strategy(tmp_path / "bench.py", {"fast": 10, "slow": 50})
        assert len(bench._default_param_grid(strategy, 4)) == 4

    def test_no_numeric_parameters_yields_single_empty_cell(self, tmp_path):
        strategy = _strategy(tmp_path / "bench.py", {"label": "x"})
        assert bench._default_param_grid(strategy, 9) == [{}]


class TestParseParamGrid:
    def test_empty_string_uses_default_grid(self, tmp_path):
        strategy = _strategy(tmp_path / "bench.py", {"fast": 10})
        cells = bench._parse_param_grid("", strategy, 9)
        assert cells == [{"fast": m * 10} for m in (0.8, 1.0, 1.2)]

    def test_json_list_of_objects_is_accepted_and_capped(self, tmp_path):
        strategy = _strategy(tmp_path / "bench.py", {})
        cells = bench._parse_param_grid('[{"a": 1}, {"b": 2}]', strategy, 1)
        assert cells == [{"a": 1}]

    @pytest.mark.parametrize("raw", ['{"a": 1}', "[1, 2]", '"x"'])
    def test_non_list_shapes_are_rejected(self, tmp_path, raw):
        strategy = _strategy(tmp_path / "bench.py", {})
        with pytest.raises(ValueError, match="param-grid"):
            bench._parse_param_grid(raw, strategy, 9)


class TestSummarizeCells:
    def test_per_engine_totals_and_vectorbt_over_native_ratio(self):
        rows = [
            {"engine": "native", "status": "ok", "seconds": 1.0, "trade_count": 3},
            {"engine": "native", "status": "ok", "seconds": 2.0, "trade_count": 4},
            {"engine": "vectorbt", "status": "ok", "seconds": 8.0, "trade_count": 3},
            {"engine": "vectorbt", "status": "ok", "seconds": 4.0, "trade_count": 4},
            # Error rows count as runs but not in the timed totals.
            {"engine": "native", "status": "error", "seconds": 9.0},
        ]
        summary = bench._summarize_cells(rows, {"native": {"seconds": 0.1}})

        native = summary["cells_per_engine"]["native"]
        vectorbt = summary["cells_per_engine"]["vectorbt"]
        assert native["runs"] == 3
        assert native["errors"] == 1
        assert native["total_seconds"] == 3.0
        assert native["mean_cell_seconds"] == 1.5
        assert native["trades_total"] == 7
        assert vectorbt["total_seconds"] == 12.0

        assert summary["vectorbt_over_native_total_seconds"] == 4.0
        assert summary["warmup"] == {"native": {"seconds": 0.1}}

    def test_zero_native_total_omits_the_ratio(self):
        rows = [
            {"engine": "native", "status": "error", "seconds": 1.0},
            {"engine": "vectorbt", "status": "ok", "seconds": 2.0, "trade_count": 1},
        ]
        summary = bench._summarize_cells(rows, {})
        assert "vectorbt_over_native_total_seconds" not in summary
        assert summary["cells_per_engine"]["vectorbt"]["mean_cell_seconds"] == 2.0


class TestTimeRun:
    def test_adapter_exception_becomes_an_error_row(self):
        class _Boom:
            def run(self, config, strategy_path):
                raise RuntimeError("boom")

        row = bench._time_run(_Boom(), object(), Path("s.py"))
        assert row["status"] == "error"
        assert row["error_type"] == "RuntimeError"
        assert "boom" in row["error"]

    def test_success_reports_trade_count(self):
        class _Ok:
            def run(self, config, strategy_path):
                return SimpleNamespace(trades=[1, 2])

        row = bench._time_run(_Ok(), object(), Path("s.py"))
        assert row["status"] == "ok"
        assert row["trade_count"] == 2

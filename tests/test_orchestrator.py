from __future__ import annotations

import json

import pytest

from tests import orchestrator


def _result(layer: str, *, exit_code: int = 0) -> orchestrator.LayerResult:
    return orchestrator.LayerResult(
        layer=layer,
        exit_code=exit_code,
        duration=0.01,
        passed=1,
        failed=1 if exit_code else 0,
        skipped=0,
        errors=0,
    )


def test_layer_names_match_what_runs():
    """M6: layer names must not claim suites they never execute."""
    assert orchestrator._layer_pytest_args("ai_code")
    assert orchestrator._layer_pytest_args("pipeline")
    # "validation"/"strategies" names ran neither tests/validation/** nor
    # tests/strategies/**; they must not come back under those names.
    assert "validation" not in orchestrator._LAYER_ALIASES
    assert "strategies" not in orchestrator._LAYER_ALIASES


def test_legacy_layer_names_are_accepted_aliases(monkeypatch):
    """Old CLI invocations keep working but report the real layer name."""
    executed: list[str] = []

    def fake_run_layer(layer: str) -> orchestrator.LayerResult:
        executed.append(layer)
        return _result(layer)

    monkeypatch.setattr(orchestrator, "_run_layer", fake_run_layer)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda _results: None)
    monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py", "--layer", "validation"])

    assert orchestrator.main() == 0
    assert executed == ["ai_code"]


def test_main_runs_non_e2e_before_e2e(monkeypatch):
    executed_layers: list[str] = []

    def fake_run_layer(layer: str) -> orchestrator.LayerResult:
        executed_layers.append(layer)
        return _result(layer)

    monkeypatch.setattr(orchestrator, "_run_layer", fake_run_layer)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda _results: None)
    monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py"])

    assert orchestrator.main() == 0
    assert executed_layers == ["ai_code", "pipeline", "unit", "e2e"]


def test_main_runs_non_e2e_layers_even_when_e2e_fails(monkeypatch):
    executed_layers: list[str] = []

    def fake_run_layer(layer: str) -> orchestrator.LayerResult:
        executed_layers.append(layer)
        return _result(layer, exit_code=1 if layer == "e2e" else 0)

    monkeypatch.setattr(orchestrator, "_run_layer", fake_run_layer)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda _results: None)
    monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py"])

    assert orchestrator.main() == 1
    assert executed_layers[:3] == ["ai_code", "pipeline", "unit"]
    assert executed_layers[-1] == "e2e"


def test_ai_code_layer_targets_the_ai_code_suite():
    assert "tests/integration/test_ai_code_quality.py" in orchestrator._layer_pytest_args("ai_code")


def test_pipeline_layer_targets_the_strategy_pipeline_suite():
    assert (
        "tests/integration/test_strategy_pipeline.py"
        in orchestrator._layer_pytest_args("pipeline")
    )


def _write_report(tmp_path, layer: str, summary: dict) -> None:
    report_path = tmp_path / f"{layer}-report.json"
    report_path.write_text(json.dumps({"summary": summary}), encoding="utf-8")


def test_zero_counts_with_green_exit_code_become_error(tmp_path):
    """M7: a crashed reporter must not look like a PASS row with 0/0/0/0."""
    _write_report(tmp_path, "unit", {})

    result = orchestrator._build_layer_result(
        "unit", exit_code=0, duration=0.01, report_path=tmp_path / "unit-report.json"
    )

    assert result.status == "ERROR"
    assert result.errors == 1
    assert result.passed == 0
    assert result.failed == 0


def test_missing_report_file_becomes_error(tmp_path):
    result = orchestrator._build_layer_result(
        "unit", exit_code=0, duration=0.01, report_path=tmp_path / "missing.json"
    )

    assert result.status == "ERROR"


def test_corrupt_report_file_becomes_error(tmp_path):
    """m6: truncated JSON must degrade to an ERROR row, not crash the run."""
    (tmp_path / "unit-report.json").write_text("{not json", encoding="utf-8")

    result = orchestrator._build_layer_result(
        "unit", exit_code=0, duration=0.01, report_path=tmp_path / "unit-report.json"
    )

    assert result.status == "ERROR"


def test_failed_layer_reports_its_counts(tmp_path):
    _write_report(tmp_path, "unit", {"passed": 3, "failed": 2, "skipped": 1, "total": 6})

    result = orchestrator._build_layer_result(
        "unit", exit_code=1, duration=0.01, report_path=tmp_path / "unit-report.json"
    )

    assert result.status == "FAIL"
    assert (result.passed, result.failed, result.skipped, result.errors) == (3, 2, 1, 0)


def test_summary_marks_error_rows():
    results = [_result("unit")]
    results[0].errors = 1

    assert results[0].status == "ERROR"


def test_summary_prints_the_pytest_target_of_each_layer(capsys):
    """M6: operators must see what a layer actually executed."""
    orchestrator._print_summary([_result("unit")])

    output = capsys.readouterr().out
    assert "tests" in output


def test_layer_run_uses_timeout(monkeypatch):
    """m5: a hung layer must not block the orchestrator forever."""
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["kwargs"] = kwargs
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    monkeypatch.setattr(orchestrator, "_layer_pytest_args", lambda _layer: ["tests"])

    orchestrator._run_layer("unit")

    assert captured["kwargs"].get("timeout") == orchestrator.LAYER_TIMEOUT_SECONDS


@pytest.mark.parametrize("layer", ["validation", "strategies"])
def test_legacy_aliases_map_to_real_layers(layer):
    assert orchestrator._CANONICAL_LAYER[layer] in orchestrator._LAYER_ARGS

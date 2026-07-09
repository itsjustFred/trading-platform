from __future__ import annotations

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


def test_main_runs_non_e2e_before_e2e(monkeypatch):
    executed_layers: list[str] = []

    def fake_run_layer(layer: str) -> orchestrator.LayerResult:
        executed_layers.append(layer)
        return _result(layer)

    monkeypatch.setattr(orchestrator, "_run_layer", fake_run_layer)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda _results: None)
    monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py"])

    assert orchestrator.main() == 0
    assert executed_layers == ["validation", "strategies", "unit", "e2e"]


def test_main_runs_non_e2e_layers_even_when_e2e_fails(monkeypatch):
    executed_layers: list[str] = []

    def fake_run_layer(layer: str) -> orchestrator.LayerResult:
        executed_layers.append(layer)
        return _result(layer, exit_code=1 if layer == "e2e" else 0)

    monkeypatch.setattr(orchestrator, "_run_layer", fake_run_layer)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda _results: None)
    monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py"])

    assert orchestrator.main() == 1
    assert executed_layers[:3] == ["validation", "strategies", "unit"]
    assert executed_layers[-1] == "e2e"

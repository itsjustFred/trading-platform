"""Cleanup 18-C slice 3 — unexpected exceptions must not strand tasks.

The `_run` / `_run_signal_replay` thread closures in the live backtest routes
catch only specific exception tuples at the *outer* layer; anything else
escaped and left the task in `running` forever. The route handlers now guard
the thread launch, and the worker closures finalize lifecycle on `Exception`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _read_main_source() -> str:
    return (Path(__file__).resolve().parents[1] / "viewer" / "main.py").read_text(
        encoding="utf-8"
    )


def _find_function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"function {name} not found in viewer/main.py")


def _handler_exception_types(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["BaseException"]
    if isinstance(handler.type, ast.Tuple):
        return [
            item.id
            for item in handler.elts
            if isinstance(item, ast.Name)
        ]
    if isinstance(handler.type, ast.Name):
        return [handler.type.id]
    if isinstance(handler.type, ast.Attribute):
        return [handler.type.attr]
    return [ast.dump(handler.type)]


def test_backtest_worker_finalizes_on_any_exception():
    """The `_run` closure of POST /api/backtests/run must catch Exception."""
    source = _read_main_source()
    run_endpoint = _find_function(source, "run_backtest")
    run_closures = [
        node
        for node in ast.walk(run_endpoint)
        if isinstance(node, ast.FunctionDef) and node.name == "_run"
    ]
    assert len(run_closures) == 1

    handlers = [
        handler
        for node in ast.walk(run_closures[0])
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    exception_types = [name for handler in handlers for name in _handler_exception_types(handler)]
    assert "Exception" in exception_types, (
        "Backtest worker must finalize lifecycle on any Exception, "
        f"but only catches {exception_types}"
    )


def test_signal_replay_worker_finalizes_on_any_exception():
    """The `_run_signal_replay` closure must catch Exception."""
    source = _read_main_source()
    endpoint = _find_function(source, "run_backtest_signals")
    run_closures = [
        node
        for node in ast.walk(endpoint)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_signal_replay"
    ]
    assert len(run_closures) == 1

    handlers = [
        handler
        for node in ast.walk(run_closures[0])
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    exception_types = [name for handler in handlers for name in _handler_exception_types(handler)]
    assert "Exception" in exception_types, (
        "Signal replay worker must finalize lifecycle on any Exception, "
        f"but only catches {exception_types}"
    )


def test_worker_closures_finalize_lifecycle_status_in_except_path():
    """A failure path must mark the task finished AND fail the job record."""
    source = _read_main_source()
    for closure_name in ("_run", "_run_signal_replay"):
        endpoint_name = (
            "run_backtest" if closure_name == "_run" else "run_backtest_signals"
        )
        endpoint = _find_function(source, endpoint_name)
        closure = next(
            node
            for node in ast.walk(endpoint)
            if isinstance(node, ast.FunctionDef) and node.name == closure_name
        )
        except_bodies = [
            statement
            for node in ast.walk(closure)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            for statement in handler.body
        ]
        calls = set()
        for statement in except_bodies:
            for sub in ast.walk(statement):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                ):
                    calls.add(sub.func.id)
        assert "_mark_task_finished" in calls, (
            f"{closure_name} except path must call _mark_task_finished"
        )
        assert "_finish_job" in calls or "_set_job_status" in calls, (
            f"{closure_name} except path must fail the job record"
        )

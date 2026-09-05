"""Cleanup 18-C slice 10 — no `asyncio.run()` inside viewer.main helpers.

Each `asyncio.run()` spins a throwaway loop and raises `RuntimeError` the day
a caller sits on the live event loop. The restart/discovery helpers must use
explicit loop-free invocation instead.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_viewer_main_does_not_call_asyncio_run_outside_dispatcher():
    source = (Path(__file__).resolve().parents[1] / "viewer" / "main.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        ):
            enclosing = _enclosing_function_name(tree, node)
            if enclosing not in {"_run_coroutine_sync", "_runner"}:
                offenders.append((enclosing, node.lineno))
    assert offenders == [], (
        f"asyncio.run() calls outside the sync dispatcher: {offenders}"
    )


def _enclosing_function_name(tree: ast.AST, target: ast.AST) -> str | None:
    parent_map = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    node = target
    while node in parent_map:
        node = parent_map[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def test_restarted_backtest_helper_runs_task_inline():
    """The restart path must return a task payload without a nested loop."""
    import asyncio

    import viewer.main as viewer_main

    async def _fake_run_backtest(payload):
        return {"task_id": "new-task", "status": "queued"}

    async def _main():
        return viewer_main._run_backtest_sync(_fake_run_backtest, {"strategy": "x"})

    payload = asyncio.run(_main())
    assert payload == {"task_id": "new-task", "status": "queued"}

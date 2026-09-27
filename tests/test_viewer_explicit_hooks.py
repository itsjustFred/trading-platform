"""Cleanup 18-C slice 11 — explicit legacy hooks and thread-local log capture.

The legacy core task lifecycle was persistence-aware only because
viewer.main reassigned eight module attributes at import time; behavior
depended on import order and the chain was invisible to static analysis. The
hooks are now injected by an explicit function, and the agent task runner no
longer swaps process-global sys.stdout/sys.stderr from inside a thread.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _read(path: str) -> str:
    return (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")


def test_legacy_hook_injection_is_explicit_not_import_time():
    import viewer.main as viewer_main

    # The module-level statements are gone; injection happens via a function.
    assert callable(viewer_main.install_legacy_task_hooks)

    source = _read("viewer/main.py")
    tree = ast.parse(source)
    offenders = []
    for node in tree.body:  # module level only
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id == "legacy_viewer"
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr in {
                "_create_task_record",
                "_get_task",
                "_snapshot_task",
                "_update_task_fields",
                "_set_task_status",
                "_update_task_progress",
                "_mark_task_finished",
                "_serialize_task",
            }
        ):
            offenders.append(node.lineno)
    assert offenders == [], f"import-time legacy monkeypatching at {offenders}"

    # And installing the hooks wires the wrappers into the legacy module.
    import viewer.core as legacy

    viewer_main.install_legacy_task_hooks()
    assert legacy._create_task_record is viewer_main._create_task_record
    assert legacy._get_task is viewer_main._get_task
    assert legacy._update_task_fields is viewer_main._update_task_fields
    assert legacy._mark_task_finished is viewer_main._mark_task_finished


def test_agent_task_runner_does_not_redirect_process_global_streams():
    source = _read("viewer/core.py")
    tree = ast.parse(source)

    launch = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_launch_agent_task":
            launch = node
            break
    assert launch is not None

    with_items = []
    for node in ast.walk(launch):
        if isinstance(node, ast.withitem) and isinstance(node.context_expr, ast.Call):
            func = node.context_expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            with_items.append(name)
    assert "redirect_stdout" not in with_items, (
        "agent task runner must not swap sys.stdout globally from a thread"
    )
    assert "redirect_stderr" not in with_items

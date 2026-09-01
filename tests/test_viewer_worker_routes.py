"""Cleanup 18-C slice 9 — blocking viewer routes use worker execution.

Sync `async def` routes that run parquet scans, catalog builds or DataFrame
loads inline stall the whole event loop (WebSockets included). Each of these
routes must delegate its blocking body to a worker thread.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_BLOCKING_ROUTES = [
    "data_overview",
    "data_category_details",
    "list_symbols",
    "get_benchmark",
    "get_trade_with_context",
    "get_backtest_replay",
    "get_backtest_ohlcv",
    "compare_backtests_endpoint",
    "get_backtest_stats",
]


def _read_main_source() -> str:
    return (Path(__file__).resolve().parents[1] / "viewer" / "main.py").read_text(
        encoding="utf-8"
    )


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"route {name} not found in viewer/main.py")


def _called_names(fn) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        ):
            names.add(node.func.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        ):
            names.add(node.func.attr)
    return names


def test_blocking_routes_delegate_to_worker_threads():
    tree = ast.parse(_read_main_source())
    offenders: list[str] = []

    for route_name in _BLOCKING_ROUTES:
        route = _find_function(tree, route_name)
        calls = _called_names(route)
        if "to_thread" not in calls and "gather" not in calls:
            offenders.append(route_name)

    assert offenders == [], (
        f"routes still running blocking I/O inline on the event loop: {offenders}"
    )


def test_to_thread_helper_exists_for_bodies():
    """The helper that wraps blocking bodies in a worker thread exists."""
    import viewer.main as viewer_main

    assert callable(viewer_main.legacy_viewer._stats_payload_from_result)

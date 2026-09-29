from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.integration import viewer_isolation
from tests.validators.backtest_validator import BacktestExpectation, BacktestResultValidator

#: This suite drives a RUNNING viewer over HTTP, which means whatever DATA_DIR
#: that viewer was started with. The default used to be the production viewer on
#: :5556, whose DATA_DIR is the live ``/mnt/block-volume-data`` volume -- so a
#: plain ``pytest tests/`` silently wrote its backtest artifacts into the
#: production database. 219 rows landed there that way.
#:
#: There is no way to tell an isolated dev viewer apart from the production one
#: by URL alone, so the suite now refuses to guess: BASE_URL must be set
#: explicitly, and opt-in is required before it will write anything at all.
API_BASE_URL = os.getenv("BASE_URL", "").strip()
_REQUIRE_OPT_IN = os.getenv("PERMA_ALLOW_LIVE_VIEWER_WRITES", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

#: The isolation decision lives in a side-effect-free module so it can be tested
#: without making a request, and so this file consults it *before* the
#: import-time discovery below. See viewer_isolation.py for why that ordering is
#: the whole fix: marks attach to test functions and are evaluated long after
#: the import-time request has already gone out.
_ISOLATION_REFUSED = bool(API_BASE_URL) and viewer_isolation.targets_production(
    API_BASE_URL
)


_REQUIRES_ISOLATED_VIEWER = pytest.mark.skipif(
    not (API_BASE_URL and _REQUIRE_OPT_IN),
    reason=(
        "Drives a live viewer over HTTP and writes backtest artifacts into its "
        "DATA_DIR. Set BASE_URL to an isolated viewer and "
        "PERMA_ALLOW_LIVE_VIEWER_WRITES=1 to run it. Refusing to guess, because "
        "the default previously targeted the production viewer on :5556."
    ),
)

POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 120.0
MIN_STRATEGIES = 5


@dataclass(slots=True)
class DiscoveryResult:
    matrix: list[dict[str, Any]]
    error: str | None = None


def _discover_strategy_matrix() -> DiscoveryResult:
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=20.0) as client:
            response = client.get("/api/strategies")
        response.raise_for_status()
    except Exception as exc:  # pragma: no cover - network/setup issue
        return DiscoveryResult(matrix=[], error=f"Could not discover strategies from API: {exc}")

    payload = response.json()
    if not isinstance(payload, list):
        return DiscoveryResult(matrix=[], error="Unexpected /api/strategies payload format.")

    usable_names: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        strategy_name = str(entry.get("slug") or entry.get("name") or "").strip()
        if strategy_name:
            usable_names.append(strategy_name)

    unique_names = sorted(set(usable_names))
    if len(unique_names) < MIN_STRATEGIES:
        return DiscoveryResult(
            matrix=[],
            error=(
                f"Need at least {MIN_STRATEGIES} discoverable strategies, "
                f"found {len(unique_names)}."
            ),
        )

    matrix = [
        {
            "name": strategy_name,
            "strategy": strategy_name,
            "symbols": ["AAPL"],
            "timeframe": "1d",
            "start": "2024-01-01",
            "end": "2024-06-30",
            "engine": "native",
            "capital": 100000.0,
        }
        for strategy_name in unique_names[:MIN_STRATEGIES]
    ]
    return DiscoveryResult(matrix=matrix)


def _poll_task_completion(client: httpx.Client, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_status = "unknown"

    while time.monotonic() < deadline:
        response = client.get("/api/tasks")
        response.raise_for_status()
        payload = response.json()
        tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
        current_task = next(
            (
                task
                for task in tasks
                if isinstance(task, dict) and str(task.get("id") or "").strip() == task_id
            ),
            None,
        )
        if current_task is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        last_status = str(current_task.get("status") or "unknown").lower()
        if last_status in {"completed", "failed", "cancelled", "interrupted"}:
            return current_task

        time.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(
        f"Task {task_id} did not finish within {POLL_TIMEOUT_SECONDS}s "
        f"(last={last_status})."
    )


# Isolation is checked here, not only in the marks above, and that ordering is the
# whole fix. This module performs HTTP *at import time*: the skipif marks are
# attached to test functions, so they have not been evaluated when this line runs.
# A short-circuit on the opt-in alone was already here and was not enough - with
# BASE_URL aimed at the live platform on :5556 and the opt-in set, discovery still
# ran and still reached production during collection, before any test could be
# skipped. That is the shape of the incident that put 219 rows in the live
# database, so the refusal has to happen before the request, not before the test.

if not (API_BASE_URL and _REQUIRE_OPT_IN) or _ISOLATION_REFUSED:
    _DISCOVERY = None
else:
    _DISCOVERY = _discover_strategy_matrix()
STRATEGY_TEST_MATRIX = _DISCOVERY.matrix if _DISCOVERY is not None else []

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    _REQUIRES_ISOLATED_VIEWER,
]

if _ISOLATION_REFUSED:
    # Skipped at module level so the reason names the refusal rather than a
    # connection error from a viewer that should never have been contacted.
    pytest.skip(
        viewer_isolation.refusal_reason(API_BASE_URL),
        allow_module_level=True,
    )

if _DISCOVERY is not None and _DISCOVERY.error:
    pytest.skip(_DISCOVERY.error, allow_module_level=True)


@pytest.mark.parametrize("case", STRATEGY_TEST_MATRIX, ids=lambda c: c["name"])
def test_strategy_pipeline(case: dict[str, Any]) -> None:
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        run_response = client.post(
            "/api/backtests/run",
            json={
                "strategy": case["strategy"],
                "engine": case["engine"],
                "symbols": case["symbols"],
                "timeframe": case["timeframe"],
                "start": case["start"],
                "end": case["end"],
                "capital": case["capital"],
                "parameters": {},
            },
        )
        run_response.raise_for_status()
        run_payload = run_response.json()
        task_id = str(run_payload.get("task_id") or run_payload.get("job_id") or "").strip()
        assert task_id, f"Missing task id in run response: {run_payload}"

        final_task = _poll_task_completion(client, task_id)
        final_status = str(final_task.get("status") or "").lower()
        assert final_status == "completed", (
            f"Task {task_id} failed with status={final_status}: {final_task}"
        )

        status_response = client.get(f"/api/backtests/run/{task_id}/status")
        status_response.raise_for_status()
        status_payload = status_response.json()
        result_payload = status_payload.get("result") if isinstance(status_payload, dict) else None
        backtest_id = (
            str(result_payload.get("backtest_id") or "").strip()
            if isinstance(result_payload, dict)
            else ""
        )
        assert backtest_id, f"Backtest id missing for completed task {task_id}: {status_payload}"

        backtest_response = client.get(f"/api/backtests/{backtest_id}")
        backtest_response.raise_for_status()
        backtest_result = backtest_response.json()

    validation = BacktestResultValidator().validate(
        backtest_result,
        BacktestExpectation(min_trades=0),
    )
    assert validation["passed"], validation

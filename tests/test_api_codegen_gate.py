"""API tests for the LLM codegen review gate (F25, Cleanup 16-C).

Seams under test (FastAPI routes in ``viewer.main``):
- ``GET  /api/strategies/{relative_file:path}/approval`` reports whether the
  file's current content is covered by a SHA256-bound approval;
- ``POST /api/strategies/{relative_file:path}/approve`` records that approval.
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from viewer.main import app  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    monkeypatch.setattr("viewer.main.STRATEGIES_DIR", strategies_dir)
    # Point the dolthub scan at a data dir that does not exist. Otherwise a
    # developer machine with a real dolthub checkout makes every TestClient start
    # shell out to `dolt pull`, and the suite stalls on network git operations
    # that have nothing to do with the approval gate. Patch DATA_DIR, which is
    # what `_dolthub_pull` actually resolves its repos from.
    monkeypatch.setattr("viewer.core.DATA_DIR", tmp_path / "no-such-data")
    with fastapi_testclient.TestClient(app) as test_client:
        yield test_client, strategies_dir


def test_approval_flow_reports_unapproved_then_approved(client):
    test_client, strategies_dir = client
    strategy_dir = strategies_dir / "demo"
    strategy_dir.mkdir()
    strategy_file = strategy_dir / "demo.py"
    strategy_file.write_text("x = 1\n")

    response = test_client.get("/api/strategies/demo/demo.py/approval")
    assert response.status_code == 200
    assert response.json() == {"approved": False}

    response = test_client.post("/api/strategies/demo/demo.py/approve")
    assert response.status_code == 200
    payload = response.json()
    assert payload["approved"] is True
    assert payload["sha256"]
    assert payload["approved_by"] == "human"

    response = test_client.get("/api/strategies/demo/demo.py/approval")
    assert response.status_code == 200
    assert response.json() == {"approved": True}


def test_approval_is_invalidated_when_file_changes(client):
    test_client, strategies_dir = client
    strategy_dir = strategies_dir / "demo"
    strategy_dir.mkdir()
    strategy_file = strategy_dir / "demo.py"
    strategy_file.write_text("x = 1\n")
    test_client.post("/api/strategies/demo/demo.py/approve")

    strategy_file.write_text("x = 2\n")
    response = test_client.get("/api/strategies/demo/demo.py/approval")
    assert response.json() == {"approved": False}


def test_approve_missing_strategy_returns_404(client):
    test_client, _ = client
    response = test_client.post("/api/strategies/ghost/ghost.py/approve")
    assert response.status_code == 404


def test_approve_rejects_path_traversal(client):
    test_client, _ = client
    # ..%2f survives httpx/ASGI path normalization and reaches the route handler.
    response = test_client.post("/api/strategies/..%2f..%2fetc%2fpasswd/approve")
    assert response.status_code == 400

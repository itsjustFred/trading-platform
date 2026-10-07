from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import viewer.main as viewer_main_module


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)



@pytest.fixture(autouse=True)
def _hermetic_db(isolated_db_sandbox):
    """Keep these tests off the configured database.

    They import `viewer.main`, which initialises the schema on import, so without this they read -
    and migrate - whatever `DATA_DIR` points at. That is invisible while the schema migrations are
    inert, and it is the reason the `live_signals.deployment_id` foreign key broke them: the migration
    correctly refused to run against a database that still has orphaned rows, which turned a latent
    production read into a test failure.

    Being explicit here is the fix rather than a workaround: a unit test that parses a log line has
    no business opening the live trading database.
    """

@pytest.fixture
def _capture_viewer_logs() -> Iterator[_CaptureHandler]:
    handler = _CaptureHandler()
    logger = viewer_main_module.logger
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_request_middleware_logs_non_api_request(_capture_viewer_logs: _CaptureHandler) -> None:
    client = TestClient(viewer_main_module.app)

    response = client.get("/strategies")
    assert response.status_code == 200

    records = [r for r in _capture_viewer_logs.records if r.getMessage() == "HTTP request completed"]
    assert records, "Expected request completion log record"
    record = records[-1]
    assert getattr(record, "data", {})["method"] == "GET"
    assert getattr(record, "data", {})["path"] == "/strategies"
    assert getattr(record, "data", {})["status"] == 200
    assert isinstance(getattr(record, "data", {})["duration_ms"], float)


def test_request_middleware_logs_500_with_traceback(_capture_viewer_logs: _CaptureHandler, monkeypatch) -> None:
    client = TestClient(viewer_main_module.app)

    def _raise() -> list[dict[str, str]]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(viewer_main_module, "_cached_strategies_payload", _raise)
    with pytest.raises(RuntimeError, match="kaboom"):
        client.get("/api/strategies")

    records = [r for r in _capture_viewer_logs.records if r.getMessage() == "HTTP request failed"]
    assert records, "Expected request failure log record"
    record = records[-1]
    assert getattr(record, "data", {})["method"] == "GET"
    assert getattr(record, "data", {})["path"] == "/api/strategies"
    assert getattr(record, "data", {})["status"] == 500
    assert isinstance(getattr(record, "data", {})["duration_ms"], float)
    assert record.exc_info is not None


def test_request_middleware_uses_debug_for_quiet_polling_paths(
    _capture_viewer_logs: _CaptureHandler,
) -> None:
    client = TestClient(viewer_main_module.app)

    response = client.get("/api/data/tasks")
    assert response.status_code == 200

    records = [r for r in _capture_viewer_logs.records if r.getMessage() == "HTTP request completed"]
    assert records, "Expected request completion log record"
    record = records[-1]
    assert getattr(record, "data", {})["path"] == "/api/data/tasks"
    assert record.levelno == logging.DEBUG

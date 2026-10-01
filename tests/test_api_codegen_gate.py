"""API tests for the LLM codegen review gate (F25, Cleanup 16-C).

Seams under test (FastAPI routes in ``viewer.main``):
- ``GET  /api/strategies/{relative_file:path}/approval`` reports whether the
  file's current content is covered by a SHA256-bound approval;
- ``POST /api/strategies/{relative_file:path}/approve`` records that approval.
"""

from __future__ import annotations

import pytest

from viewer.main import app  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Scoped to the tests that actually drive an HTTP client.
    #
    # This used to be a module-level `pytest.importorskip("fastapi.testclient")`, which
    # gated all five tests in the file. Four of them need a client; the fifth,
    # `test_jforex_prewarm_starts_the_jvm_on_the_main_thread`, needs only
    # `viewer.main` - it asserts the JVM starts on the main thread rather than on a
    # ThreadPoolExecutor worker, which is the defect whose atexit deadlock had the whole
    # suite hanging and reporting green.
    #
    # So on a machine with `fastapi` but without `httpx`, that guard went silently - and
    # it is the guard for the bug that produced 7.2 GiB of hung pytest runs. `viewer.main`
    # itself is imported at module scope with no skip, so a genuinely missing `fastapi` is
    # a collection *error* - loud, which is the right outcome and the reason the skip can
    # be narrowed to this fixture rather than removed.
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
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


def test_jforex_prewarm_starts_the_jvm_on_the_main_thread(monkeypatch):
    """The JForex JVM prewarm must not be pushed onto a worker thread.

    `viewer.main._prewarm_jforex_jvm_background` used to await
    `asyncio.to_thread(jforex_dl.prewarm_jvm)`, which started the JVM on a
    `ThreadPoolExecutor` worker. JPype then deadlocks in its atexit shutdown, so
    the process hangs forever *after* the suite has already printed a green
    summary. That is worse than a plain failure: it looks like a pass, and it
    keeps the worktree's RAM pinned until something reaps it.

    Measured, not assumed: a main-thread `prewarm_jvm()` exits in ~1s, the
    worker-thread version hangs, and calling `jpype.shutdownJVM()` from the main
    thread afterwards does not rescue it. The only correct fix is to start the
    JVM on the main thread, which this pins.

    The prewarm stays a background task via `asyncio.create_task`, so the server
    still is not delayed by the ~1s.
    """
    import asyncio
    import threading

    from data_tools.downloaders import jforex_dl

    seen: dict[str, object] = {}

    def _fake_prewarm() -> None:
        seen["thread"] = threading.current_thread()

    monkeypatch.setattr(jforex_dl, "prewarm_jvm", _fake_prewarm)

    asyncio.run(_call_prewarm())

    assert seen.get("thread") is threading.main_thread(), (
        "the JForex prewarm ran off the main thread, which is how the JVM gets "
        "started somewhere JPype cannot shut it down again"
    )


def test_jforex_prewarm_failure_classification(monkeypatch):
    """Four JPype failures mean "JForex is unavailable"; anything else is a real fault.

    `_prewarm_jforex_jvm_background` must never take the server down, so both
    handlers only log. But they do not log the *same* thing, and the difference is the
    point: a missing JPype, a missing Dukascopy module, a missing SDK classpath, or a
    JVM that would not start all mean JForex is simply not installed here, and are
    logged at INFO. Any other RuntimeError is an unexpected fault and is a WARNING.

    Both survivors in this function come from that boundary being unpinned: raising
    instead of warning escapes the coroutine, and dropping one phrase from the list
    demotes a genuine "JVM failed to start" fault to an INFO line, which is how a
    broken prewarm stays invisible.

    Asserted on the log *level* rather than the message, since the level is the
    contract the operator reads.
    """
    import asyncio
    import logging

    from data_tools.downloaders import jforex_dl

    from viewer.main import _prewarm_jforex_jvm_background

    unavailable = (
        "JPype is required",
        "failed to import 'com.dukascopy'",
        "jforex sdk classpath",
        "failed to start jvm",
    )
    for message in unavailable:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger = logging.getLogger("trading.viewer")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            def _raise_unavailable() -> None:
                raise RuntimeError(message)

            monkeypatch.setattr(jforex_dl, "prewarm_jvm", _raise_unavailable)
            # Must not raise: the server has to survive an unavailable JForex.
            asyncio.run(_prewarm_jforex_jvm_background())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        assert records, f"nothing was logged for {message!r}"
        assert all(r.levelno == logging.INFO for r in records), (
            f"{message!r} was logged at "
            + str({logging.getLevelName(r.levelno) for r in records})
            + " rather than INFO; an expected-unavailable broker must not look like a fault"
        )

    # An unrecognised RuntimeError is a real fault, and stays a WARNING.
    records: list[logging.LogRecord] = []

    class _Capture2(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture2()
    logger = logging.getLogger("trading.viewer")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        def _raise_unexpected() -> None:
            raise RuntimeError("something nobody anticipated")

        monkeypatch.setattr(jforex_dl, "prewarm_jvm", _raise_unexpected)
        asyncio.run(_prewarm_jforex_jvm_background())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert records, "an unexpected prewarm failure logged nothing at all"
    assert all(r.levelno == logging.WARNING for r in records), (
        "an unrecognised failure was demoted to INFO, so a broken prewarm looks like "
        "an absent one: " + str({logging.getLevelName(r.levelno) for r in records})
    )

    # And a non-RuntimeError exception is a warning too, not a crash.
    records.clear()

    class _Capture3(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture3()
    logger.addHandler(handler)
    try:
        def _raise_other() -> None:
            raise ValueError("not a RuntimeError")

        monkeypatch.setattr(jforex_dl, "prewarm_jvm", _raise_other)
        asyncio.run(_prewarm_jforex_jvm_background())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert records and all(r.levelno == logging.WARNING for r in records), (
        "a non-RuntimeError prewarm failure was not logged as a warning"
    )


async def _call_prewarm() -> None:
    from viewer.main import _prewarm_jforex_jvm_background

    await _prewarm_jforex_jvm_background()

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import pytest

# Keep test runs deterministic regardless of machine/user HOME resolution.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_DIR = (PROJECT_ROOT / "data").resolve()

os.environ.setdefault("DATA_DIR", str(TEST_DATA_DIR))
os.environ.setdefault("TRADING_DATA_DIR", str(TEST_DATA_DIR))


def _requested_browser_name(item: pytest.Item) -> str:
    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        browser_name = callspec.params.get("browser_name")
        if isinstance(browser_name, str) and browser_name:
            return browser_name

    configured = item.config.getoption("browser", default=None)
    if isinstance(configured, (list, tuple)):
        for candidate in configured:
            if isinstance(candidate, str) and candidate:
                return candidate
    if isinstance(configured, str) and configured:
        return configured
    return "chromium"


def _is_missing_browser_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "executable doesn't exist" in normalized
        or ("browser" in normalized and "not found" in normalized)
    )


def _make_sandbox_db(monkeypatch, tmp_path: Path, *, create_schema: bool):
    """Build a throwaway ``shared.database`` sandbox and restore state after.

    The production engine URL is built from ``DB_PATH`` at import time, so the
    engine swap below is what actually isolates database access; patching
    ``DB_PATH`` alongside keeps any code that resolves paths from the module
    consistent, and ``DATA_DIR`` stays patched because ``init_db()`` mkdirs it
    and the artifact writers (``DATA_DIR / "backtests"``) must land in the
    sandbox too.
    """
    import shared.database as database

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "backtests.db"

    engine = database.create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    database.event.listen(engine, "connect", database._configure_sqlite_connection)
    if create_schema:
        database.SQLModel.metadata.create_all(engine)

    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DATA_DIR", data_dir)
    # False on purpose: tests (and production code paths) must still be able to
    # call init_db() and run _migrate_schema() — e.g. the per-deployment UNIQUE
    # index migrations in shared/database.py.
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    try:
        yield database, engine, data_dir
    finally:
        engine.dispose()


@pytest.fixture
def isolated_db_sandbox(monkeypatch, tmp_path: Path):
    """Redirect ``shared.database`` at a throwaway SQLite database (pre-created
    schema). Yields ``(database_module, engine, data_dir)``."""
    yield from _make_sandbox_db(monkeypatch, tmp_path, create_schema=True)


@pytest.fixture
def isolated_db(isolated_db_sandbox):
    """Alias of :func:`isolated_db_sandbox`."""
    return isolated_db_sandbox


@pytest.fixture
def isolated_db_no_schema(monkeypatch, tmp_path: Path):
    """Sandbox without pre-created schema: for tests that exercise
    ``init_db()``'s migration path over a hand-built legacy layout."""
    yield from _make_sandbox_db(monkeypatch, tmp_path, create_schema=False)


@lru_cache(maxsize=None)
def _playwright_browser_launchable(browser_name: str) -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser_type = getattr(playwright, browser_name, None)
            if browser_type is None:
                return False, f"Unsupported Playwright browser: {browser_name}"
            browser = browser_type.launch(headless=True)
            browser.close()
            return True, ""
    except Exception as exc:  # plugin raises PlaywrightError, but keep message-based skip gating.
        return False, str(exc)


@pytest.fixture
def viewer_client(monkeypatch, tmp_path, isolated_db_sandbox):
    """Shared viewer TestClient with DATA_DIR-scoped storage paths.

    Depends on ``isolated_db_sandbox`` because the task machinery persists through
    ``shared.database``, which is a process-wide engine that nothing else here
    redirects. Without it a viewer's background-task records land in the real
    database and outlive the test that wrote them, which caused two real problems:
    a later TestClient's startup recovery found a leftover ``source="dolthub"``
    download record and resumed it for real, cloning ~15 GB from the internet; and
    test runs could write to the live database at all, which is how 219 stray
    backtest rows once landed in production.
    """
    from fastapi.testclient import TestClient as _TestClient

    import viewer.main as viewer_app_module

    legacy_module = viewer_app_module.legacy_viewer

    for module in (viewer_app_module, legacy_module):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
        monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
        monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
        monkeypatch.setattr(module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
        monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")

    monkeypatch.setattr(viewer_app_module, "list_backtest_ids", lambda: [])
    monkeypatch.setattr(viewer_app_module, "load_backtest_result", lambda _backtest_id: None)
    monkeypatch.setattr(viewer_app_module, "save_result_to_sqlite", lambda _result: None)
    monkeypatch.setattr(
        viewer_app_module,
        "migrate_json_to_sqlite",
        lambda: {"migrated": 0, "failed": 0, "total": 0},
    )
    viewer_app_module.DATA_TASKS.clear()
    viewer_app_module._COALESCED_CLEANUP_PENDING.clear()
    viewer_app_module._COALESCED_CLEANUP_ORDER.clear()
    viewer_app_module._COALESCED_CLEANUP_ATTEMPTS.clear()
    viewer_app_module._COALESCED_CLEANUP_EXHAUSTED.clear()
    return _TestClient(viewer_app_module.app)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("e2e") is None:
        return

    launchable, error_message = _playwright_browser_launchable(_requested_browser_name(item))
    if launchable:
        return
    if _is_missing_browser_error(error_message):
        pytest.skip(
            "Playwright browser binaries not installed. "
            "Run: python -m playwright install firefox"
        )


@pytest.fixture(autouse=True, scope="session")
def _never_start_a_real_jvm_during_tests():
    """Stop the JForex JVM prewarm from starting a real JVM in the test session.

    The prewarm genuinely works here: the SDK jars are vendored under
    `data/.jforex-sdk/`, so `prewarm_jvm` starts a real JVM and imports the real
    `com.dukascopy.api` classes. Any test that builds a `TestClient(app)` runs
    the lifespan, which fires the prewarm.

    That leaves a live JVM inside the pytest process, and JPype's atexit
    shutdown then deadlocks - the process hangs *after* printing a green
    summary. So the suite looked like it passed while pinning a worktree's RAM
    until something reaped it, and `merge-rehearsal.sh` read the SIGTERM as a
    failed suite and refused to certify the merge.

    No test needs a live JVM: the JForex paths are covered with a JPype stand-in
    in `tests/test_jforex_bootstrap.py`. So the JVM is stubbed out for the whole
    session, and the real start-on-the-main-thread requirement is pinned
    separately in `test_api_codegen_gate.py`, where it can be checked without
    paying for a JVM.
    """
    from data_tools.downloaders import jforex_dl

    patch = pytest.MonkeyPatch()
    patch.setattr(jforex_dl, "prewarm_jvm", lambda: None)
    try:
        yield
    finally:
        patch.undo()


def seed_synthetic_deployment_parents(database, deployment_ids=("dep-1", "dep-2")) -> None:
    """Create the parent `live_deployments` rows that synthetic test signals point at.

    `live_signals.deployment_id` is headed for a foreign key onto `live_deployments.id`. A large body
    of tests writes signals with placeholder ids such as `"dep-1"` and no parent row, which the
    constraint correctly rejects - those failures are the FK doing its job on rows that would otherwise
    be invisible.

    Seeding the parents once per test, here, is far better than rewriting the ~66 inline
    `LiveSignal(deployment_id="dep-1")` constructions at their call sites, and far better than a
    blanket autouse fixture: this is called from the `isolated_db` fixtures that already opt in to a
    temporary database, so it never forces isolation onto a test that legitimately uses the configured
    database.

    The insert is raw SQL on purpose. `LiveDeploymentConfig.id` is typed `UUID`, so `"dep-1"` is not a
    value the API would accept - but the column itself is VARCHAR, and these parents exist only to
    satisfy referential integrity for tests that are about something else entirely (fills, dedupe,
    concurrency claims). Tests that assert the constraint itself keep building real parents through
    `create_live_deployment`.
    """
    from datetime import datetime, timezone

    import json

    now = datetime.now(timezone.utc)
    database.init_db()
    with database.get_session() as session:
        # `live_deployments.account_id` is itself a foreign key onto `live_accounts.id`, so a parent
        # deployment needs an account above it or the insert fails on its own constraint. These
        # parents exist only to satisfy referential integrity for signals, so the account is a bare
        # placeholder too.
        session.execute(
            database.text(
                "INSERT OR IGNORE INTO live_accounts (id, broker, name, is_demo)"
                " VALUES ('fk-parent-acct', 'ctrader', 'fk-parent', 1)"
            )
        )
        for deployment_id in deployment_ids:
            session.execute(
                database.text(
                    "INSERT OR IGNORE INTO live_deployments ("
                    "id, strategy_name, account_id, symbols, timeframe, status,"
                    " signal_source, signal_source_type, signal_source_id, symbol_filter,"
                    " signal_config, settings, created_at, updated_at"
                    ") VALUES (:id, 'fk-parent', 'fk-parent-acct', :symbols, '1h', 'PAUSED',"
                    " 'discord', 'discord', 'fk-parent', :symbol_filter, '{}', '{}', :now, :now)"
                ),
                {
                    "id": str(deployment_id),
                    "symbols": json.dumps(["EURUSD"]),
                    "symbol_filter": json.dumps([]),
                    "now": now,
                },
            )
        session.commit()

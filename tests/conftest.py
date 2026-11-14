from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_DATA_DIR = (PROJECT_ROOT / "data").resolve()

# The suite must never write into the repo's ``data/``: in the main checkout that is the live
# platform's database, catalog and market data. Every process (the xdist controller and each
# worker imports this file) gets its own throwaway DATA_DIR, forced rather than ``setdefault`` so
# an exported DATA_DIR pointing at production cannot leak in either. It is set before any
# ``shared`` import, because ``shared.constants`` resolves DATA_DIR at import time.
_SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="tp-pytest-data-")).resolve()
atexit.register(shutil.rmtree, _SESSION_DATA_DIR, ignore_errors=True)
TEST_DATA_DIR = _SESSION_DATA_DIR

os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["TRADING_DATA_DIR"] = str(TEST_DATA_DIR)


def snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` for every file and directory under ``root``."""
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snapshot
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            snapshot[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    changes = [f"created: {p}" for p in sorted(after.keys() - before.keys())]
    changes += [f"deleted: {p}" for p in sorted(before.keys() - after.keys())]
    changes += [f"modified: {p}" for p in sorted(p for p in before.keys() & after.keys() if before[p] != after[p])]
    return changes


_REPO_DATA_BEFORE: dict[str, tuple[int, int]] | None = None


def pytest_configure(config: pytest.Config) -> None:
    # Controller only: xdist workers would each snapshot a tree the others are racing against.
    if hasattr(config, "workerinput"):
        return
    global _REPO_DATA_BEFORE
    _REPO_DATA_BEFORE = snapshot_tree(REPO_DATA_DIR)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run loudly if anything wrote into the repo's real ``data/`` directory."""
    if hasattr(session.config, "workerinput") or _REPO_DATA_BEFORE is None:
        return
    changes = diff_snapshots(_REPO_DATA_BEFORE, snapshot_tree(REPO_DATA_DIR))
    if not changes:
        return
    shown = "\n  ".join(changes[:25])
    more = f"\n  ... and {len(changes) - 25} more" if len(changes) > 25 else ""
    print(
        f"\n\nERROR: the test session modified the repo data directory {REPO_DATA_DIR}:"
        f"\n  {shown}{more}\nTests must write under tmp_path / the session DATA_DIR only.",
        file=sys.stderr,
    )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


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


@pytest.fixture(autouse=True)
def _no_jforex_jvm_prewarm():
    """Keep ``TestClient(app)`` lifespans from starting the JForex JVM.

    The viewer's startup prewarm resolves the JForex SDK classpath and, when no jars are found,
    *downloads* ~35 jars from Dukascopy over the network into ``DATA_DIR/.jforex-sdk``. Any test
    that enters the real lifespan would otherwise do that (slow, network-dependent, and into the
    live data dir before DATA_DIR was isolated). Tests exercising the downloader call its helpers
    directly and are unaffected.

    Uses its own ``MonkeyPatch`` rather than the ``monkeypatch`` fixture: an autouse fixture that
    requested it would instantiate it before every test's other fixtures and so delay its undo until
    after them, which breaks fixtures that assert state restored by their own ``monkeypatch`` use.
    """
    from data_tools.downloaders import jforex_dl

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jforex_dl, "prewarm_jvm", lambda: None)
        yield


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


def seed_live_deployments(
    database, deployment_ids, *, account_id: str = "fixture-account"
) -> None:
    """Create the parent rows that a fixture's synthetic signals point at.

    ``live_signals.deployment_id`` carries a foreign key, so a fixture writing a signal has to write
    its deployment first, exactly like production code does. One definition, because several modules
    write signals against synthetic ids and every one of them predates the constraint.
    """
    database.init_db()
    with database.get_session() as session:
        if session.get(database.LiveAccountModel, account_id) is None:
            session.add(
                database.LiveAccountModel(id=account_id, broker="fixture", name=account_id)
            )
            session.flush()
        for deployment_id in deployment_ids:
            if session.get(database.LiveDeploymentModel, deployment_id) is None:
                session.add(
                    database.LiveDeploymentModel(
                        id=deployment_id,
                        strategy_name=f"fixture-{deployment_id}",
                        account_id=account_id,
                        symbols_json=[],
                        timeframe="1m",
                        status="PAUSED",
                    )
                )
        session.commit()


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
def viewer_client(monkeypatch, tmp_path):
    """Shared viewer TestClient with DATA_DIR-scoped storage paths."""
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

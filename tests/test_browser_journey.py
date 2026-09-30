"""Browser-level read-only journey through the real UI.

Everything else in the suite exercises the API or the components. This drives an
actual browser against an actually-served frontend, because the failure modes that
matter most here are the ones only a browser sees: the SPA failing to boot, a
route rendering nothing, and - the reason this test insists on being read-only -
a "view" page quietly issuing a state-changing request.

The server is launched against a throwaway DATA_DIR so the journey cannot read or
write the live database, and the browser is Firefox because that is the one
installed here; the conftest `e2e` marker skips the whole file when no browser can
launch, so this is a no-op rather than a failure on a bare checkout.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_INDEX = PROJECT_ROOT / "frontend" / "dist" / "index.html"


def _sync_playwright():
    """`sync_playwright`, or skip the test that needs it.

    importorskip rather than a bare import: nothing else here covers "the
    playwright package is not installed". The `e2e` gate covers the browser and the
    fixture covers "no browser launches", but on a checkout without the optional
    dependency the import raised ModuleNotFoundError and the run failed instead of
    skipping. Both call sites go through this so the two cannot disagree.
    """
    return pytest.importorskip(
        "playwright.sync_api", reason="playwright is not installed"
    ).sync_playwright


@pytest.fixture(scope="module")
def journey_browser() -> str:
    """The first browser that can actually launch here.

    Self-gating on purpose. conftest's `e2e` gate defaults to chromium, which is
    not necessarily the browser that is installed, and pytest-playwright already
    owns the `browser_name` paramspec - so relying on either would make this test
    silently skip on a box where a browser *is* present.
    """
    sync_playwright = _sync_playwright()

    with sync_playwright() as playwright:
        for name in ("chromium", "firefox", "webkit"):
            browser_type = getattr(playwright, name, None)
            if browser_type is None:
                continue
            try:
                browser_type.launch(headless=True).close()
            except Exception:
                continue
            return name
    pytest.skip("no Playwright browser is installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_backtest(data_dir: Path) -> None:
    """One stored backtest whose Sharpe could not be computed.

    The undefined metric is the point: the list card must show an em dash for it,
    never 0.000. A Sharpe of exactly zero and a Sharpe that was never measured
    are very different facts, and this is the only layer that renders the
    difference.
    """
    backtests = data_dir / "backtests"
    backtests.mkdir(parents=True, exist_ok=True)
    equity_curve = [
        {"time": f"2026-03-{day:02d}T00:00:00+00:00", "equity": 100_000.0 + day * 100}
        for day in range(1, 8)
    ]
    payload = {
        "meta": {
            "id": "journey-undefined-sharpe",
            "engine": "vectorbt",
            "strategies": ["journey_demo"],
            "initial_capital": 100_000.0,
            "backtest_start": "2026-03-01T00:00:00+00:00",
            "backtest_end": "2026-03-07T00:00:00+00:00",
            "created_at": "2026-03-08T00:00:00+00:00",
            "data": {"equity_frequency": "daily"},
        },
        # sharpe_ratio is deliberately absent and named as undefined.
        "stats": {
            "total_pnl": 700.0,
            "total_return": 0.007,
            "cagr": 0.12,
            "max_drawdown_pct": -0.031,
            "win_rate": 0.5,
            "winners": 1,
            "losers": 1,
            "undefined_metrics": ["sharpe_ratio", "psr", "p_value"],
        },
        "equity_curve": equity_curve,
        "trades": [],
    }
    # The startup migration only globs `*.trades.json`, so a plain `.json` is
    # never indexed and the row never appears in the list.
    (backtests / "journey_undefined_sharpe.trades.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def read_only_viewer(tmp_path_factory: pytest.TempPathFactory):
    """A real viewer on a throwaway DATA_DIR. Yields its base URL."""
    if not DIST_INDEX.exists():
        pytest.skip(f"frontend not built: {DIST_INDEX} is missing")

    data_dir = tmp_path_factory.mktemp("journey-data")
    _seed_backtest(data_dir)

    port = _free_port()
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    # Unset the integration-suite guards so nothing in the app can opt into them.
    env.pop("BASE_URL", None)
    env.pop("PERMA_ALLOW_LIVE_VIEWER_WRITES", None)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "viewer.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    ready = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = (process.stdout.read() or b"").decode(errors="replace")[-800:]
            pytest.skip(f"viewer failed to start; it must not be a test failure:\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                if response.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.5)
    if not ready:
        process.terminate()
        pytest.skip("viewer did not become ready within 90s")

    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)


def test_backtests_journey_is_read_only_and_honest(
    read_only_viewer: str, journey_browser: str
) -> None:
    sync_playwright = _sync_playwright()

    base_url = read_only_viewer
    # Anything a "view" page must never do. Recorded, not blocked: the point is to
    # observe the real request set, and a blocked request would also be a failure.
    mutating: list[str] = []

    with sync_playwright() as playwright:
        browser = getattr(playwright, journey_browser).launch(headless=True)
        try:
            page = browser.new_page()
            page.on(
                "request",
                lambda request: mutating.append(f"{request.method} {request.url}")
                if request.method not in {"GET", "HEAD", "OPTIONS"}
                else None,
            )

            response = page.goto(f"{base_url}/backtests", wait_until="networkidle")
            assert response is not None and response.status == 200

            # The SPA booted and routed: a blank page is the failure this catches.
            page.wait_for_selector("text=Backtest Matrix", timeout=30_000)

            # The seeded artifact is actually listed.
            page.wait_for_selector("text=journey_demo", timeout=30_000)

            body = page.inner_text("body")
            # The invariant, not a specific glyph: an undefined metric must not be
            # rendered as a measured zero. The list card spells "unavailable" as
            # N/A rather than an em dash, so the marker is matched loosely on
            # purpose - pinning the glyph would fail on a cosmetic change while
            # still missing the thing that matters.
            assert "0.000" not in body, (
                "an undefined Sharpe rendered as 0.000, which is indistinguishable "
                "from a measured zero"
            )
            assert "N/A" in body, (
                "no explicit unavailable marker on the page; a missing value is "
                "being presented as a number somewhere"
            )

            # And the journey really was read-only.
            assert mutating == [], f"a read-only page issued {mutating}"
        finally:
            browser.close()

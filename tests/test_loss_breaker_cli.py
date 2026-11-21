"""The loss-breaker CLI (#301): `scripts/live_loss_breaker.py`.

Throwaway SQLite sandboxes only; nothing here touches the production database.

The properties under test are the ones that make an operator CLI safe to hand
out: ``--help`` answers without touching the database, ``status`` is read-only,
and ``reset-drawdown`` is the explicit operator reset the drawdown breaker
requires - it clears the latch AND re-bases the peak to the current equity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "acc-cli"


def _child_env(data_dir: Path) -> dict[str, str]:
    """An environment that cannot reach the live platform's database.

    ``DATABASE_URL`` is dropped *and* pointed at the sandbox: the engine builder
    prefers it over ``DATA_DIR``, so inheriting it would have a CLI subprocess
    write real breaker state into a real live database.
    """
    env = dict(os.environ)
    for dropped in ("BASE_URL", "PERMA_ALLOW_LIVE_VIEWER_WRITES", "DATABASE_URL"):
        env.pop(dropped, None)
    data_dir.mkdir(parents=True, exist_ok=True)
    env["DATA_DIR"] = str(data_dir)
    env["TRADING_DATA_DIR"] = str(data_dir)
    env["LOG_DIR"] = str(data_dir / "logs")
    env["DATABASE_URL"] = f"sqlite:///{(data_dir / 'backtests.db').as_posix()}"
    return env


def _run(argv: list[str], data_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the CLI as its own process, against an isolated data directory."""
    return subprocess.run(
        [sys.executable, "-m", "scripts.live_loss_breaker", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )


def _seed_state(data_dir: Path) -> None:
    """Create breaker state in the sandbox: account scope tripped on drawdown."""
    code = (
        "from datetime import datetime, timezone;"
        "from shared.database import observe_live_loss_breaker,"
        " latch_live_loss_breaker_breach;"
        "now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc);"
        "scope = 'account:acc-cli';"
        "observe_live_loss_breaker(scope, 'acc-cli', 100000.0, now);"
        "observe_live_loss_breaker(scope, 'acc-cli', 80000.0, now);"
        "latch_live_loss_breaker_breach("
        "    scope, 'drawdown', {'kind': 'drawdown'}, now)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["status", "--help"],
        ["reset-drawdown", "--help"],
    ],
)
def test_help_answers_without_touching_the_database(
    tmp_path: Path, argv: list[str]
) -> None:
    """An operator reads --help before deciding; it must cost nothing."""
    data_dir = tmp_path / "data"

    result = _run(argv, data_dir)

    assert result.returncode == 0, result.stderr
    assert not (data_dir / "backtests.db").exists()
    assert "usage:" in result.stdout


def test_status_is_read_only_and_reports_the_latch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir)
    db_before = (data_dir / "backtests.db").read_bytes()

    result = _run(["status", "--account", ACCOUNT], data_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["scopes"]) == 1
    scope = payload["scopes"][0]
    assert scope["scope"] == f"account:{ACCOUNT}"
    assert scope["drawdown_breached"] is True
    assert scope["daily_breached"] is False
    assert scope["peak_equity"] == pytest.approx(100_000.0)
    # status must not write: the database file is byte-identical afterwards.
    assert (data_dir / "backtests.db").read_bytes() == db_before


def test_reset_drawdown_clears_the_latch_and_rebases_the_peak(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir)

    result = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "82000"], data_dir
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["scope"] == f"account:{ACCOUNT}"
    assert payload["rebase_equity"] == pytest.approx(82_000.0)
    assert payload["peak_equity"] == pytest.approx(82_000.0)
    assert payload["drawdown_breached"] is False

    # And the state the daemon reads confirms it in a fresh process.
    status = _run(["status", "--account", ACCOUNT], data_dir)
    assert status.returncode == 0, status.stderr
    scope = json.loads(status.stdout)["scopes"][0]
    assert scope["drawdown_breached"] is False
    assert scope["peak_equity"] == pytest.approx(82_000.0)


def test_reset_drawdown_uses_the_latest_balance_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir)
    seed_balance = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timezone;"
            "from shared.database import save_live_balance;"
            "from shared.models import LiveBalanceRecord;"
            "save_live_balance(LiveBalanceRecord("
            "    account_id='acc-cli', timestamp=datetime.now(timezone.utc),"
            "    balance=81000.0, equity=81000.0))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert seed_balance.returncode == 0, seed_balance.stderr

    result = _run(["reset-drawdown", "--account", ACCOUNT], data_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["rebase_equity"] == pytest.approx(81_000.0)


def test_reset_drawdown_refuses_a_scope_with_no_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = _run(
        ["reset-drawdown", "--account", "acc-missing", "--equity", "1000"],
        data_dir,
    )

    assert result.returncode == 1
    assert "no loss breaker state" in result.stderr


def test_reset_drawdown_requires_equity_when_no_snapshot_exists(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir)

    result = _run(["reset-drawdown", "--scope", f"account:{ACCOUNT}"], data_dir)

    assert result.returncode == 1
    assert "--equity" in result.stderr

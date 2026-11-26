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
# ``get_live_deployment`` validates the id as a UUID, so the seeded
# deployment uses a real one; the breaker scope is ``deployment:<id>``.
DEP_ID = "11111111-2222-4333-8444-555555555555"


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


def _run_code(code: str, data_dir: Path) -> None:
    """Run setup Python in the same sandboxed environment as the CLI."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def _seed_state(data_dir: Path, *, stale: bool = False) -> None:
    """Create breaker state in the sandbox: account scope tripped on drawdown.

    ``stale=True`` back-dates the observation past ``STALE_SNAPSHOT_SECONDS``
    so the reset's freshness check refuses the scope's own last equity.
    """
    when = (
        "datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)"
        if stale
        else "datetime.now(timezone.utc)"
    )
    code = (
        "from datetime import datetime, timezone;"
        "from shared.database import observe_live_loss_breaker,"
        " latch_live_loss_breaker_breach;"
        f"now = {when};"
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


def _seed_cli_account(data_dir: Path, account_id: str) -> None:
    _run_code(
        f"""
from shared.database import LiveAccountModel, get_session, init_db
init_db()
with get_session() as session:
    session.add(LiveAccountModel(id='{account_id}', broker='test', name='{account_id}'))
    session.commit()
""",
        data_dir,
    )


def _seed_cli_deployment(data_dir: Path, dep_id: str, account_id: str) -> None:
    _run_code(
        f"""
from shared.database import (
    LiveAccountModel, LiveDeploymentModel, get_session, init_db)
init_db()
with get_session() as session:
    session.add(LiveAccountModel(id='{account_id}', broker='test', name='{account_id}'))
    session.add(LiveDeploymentModel(
        id='{dep_id}', strategy_name='t', account_id='{account_id}',
        symbols_json=['AAPL'], timeframe='1h', status='PAUSED'))
    session.commit()
""",
        data_dir,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["status", "--help"],
        ["reset-drawdown", "--help"],
        ["re-anchor", "--help"],
        ["resolve-ledger-flag", "--help"],
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
    assert len(payload["scopes"]) == 1
    scope = payload["scopes"][0]
    assert scope["scope"] == f"account:{ACCOUNT}"
    assert scope["rebase_equity"] == pytest.approx(82_000.0)
    assert scope["peak_equity"] == pytest.approx(82_000.0)
    assert scope["drawdown_breached"] is False

    # And the state the daemon reads confirms it in a fresh process.
    status = _run(["status", "--account", ACCOUNT], data_dir)
    assert status.returncode == 0, status.stderr
    state = json.loads(status.stdout)["scopes"][0]
    assert state["drawdown_breached"] is False
    assert state["peak_equity"] == pytest.approx(82_000.0)


def test_reset_drawdown_rebases_on_breaker_equity_not_balance_snapshot(
    tmp_path: Path,
) -> None:
    """The re-base equity is the scope's OWN last observed breaker equity -
    on cTrader that includes unrealized P&L, which the balance snapshot's
    equity slot does not carry. A snapshot saying 81000 must NOT win over
    the breaker's observed 80000."""
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
    scope = payload["scopes"][0]
    assert scope["rebase_equity"] == pytest.approx(80_000.0)
    assert scope["equity_source"] == "last_observed_equity"


def test_reset_drawdown_refuses_a_stale_breaker_observation(tmp_path: Path) -> None:
    """An observation older than STALE_SNAPSHOT_SECONDS must not re-base a
    peak: pass --equity instead."""
    data_dir = tmp_path / "data"
    _seed_state(data_dir, stale=True)

    result = _run(["reset-drawdown", "--account", ACCOUNT], data_dir)

    assert result.returncode == 1
    assert "old" in result.stderr

    # --equity still works: the operator states the figure explicitly.
    explicit = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "81000"], data_dir
    )
    assert explicit.returncode == 0, explicit.stderr


def test_reset_drawdown_rejects_non_finite_equity(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir)

    for bad in ("inf", "-5", "nan"):
        result = _run(
            ["reset-drawdown", "--account", ACCOUNT, "--equity", bad], data_dir
        )
        assert result.returncode == 1, (bad, result.stdout, result.stderr)
        assert "positive finite" in result.stderr


def _seed_account_with_deployment(data_dir: Path, *, stale: bool = False) -> None:
    """Account + deployment scopes, both drawdown-latched."""
    when = (
        "datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)"
        if stale
        else "datetime.now(timezone.utc)"
    )
    code = f"""
from datetime import datetime, timezone
from shared.database import observe_live_loss_breaker, latch_live_loss_breaker_breach
now = {when}
for scope in ('account:acc-cli', 'deployment:dep-1'):
    observe_live_loss_breaker(scope, 'acc-cli', 100000.0, now)
    observe_live_loss_breaker(scope, 'acc-cli', 80000.0, now)
    latch_live_loss_breaker_breach(scope, 'drawdown', {{'kind': 'drawdown'}}, now)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_reset_drawdown_on_account_cascades_to_deployment_scopes(tmp_path: Path) -> None:
    """With default thresholds the deployment scopes latch with the account
    scope; resetting only account:<id> would leave entries refused."""
    data_dir = tmp_path / "data"
    _seed_account_with_deployment(data_dir)

    result = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "82000"], data_dir
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    reset_scopes = {s["scope"] for s in payload["scopes"]}
    assert reset_scopes == {f"account:{ACCOUNT}", "deployment:dep-1"}
    # The deployment scope re-based to its OWN last observed equity (80k),
    # not the --equity account figure.
    dep_result = next(s for s in payload["scopes"] if s["scope"] == "deployment:dep-1")
    assert dep_result["rebase_equity"] == pytest.approx(80_000.0)
    assert dep_result["equity_source"] == "last_observed_equity"

    status = _run(["status", "--account", ACCOUNT], data_dir)
    assert status.returncode == 0, status.stderr
    for state in json.loads(status.stdout)["scopes"]:
        assert state["drawdown_breached"] is False


def test_reset_drawdown_reports_scopes_still_latched(tmp_path: Path) -> None:
    """The daily latch intentionally survives a drawdown reset; the CLI must
    say so instead of implying everything is clear."""
    data_dir = tmp_path / "data"
    code = """
from datetime import datetime, timezone
from shared.database import observe_live_loss_breaker, latch_live_loss_breaker_breach
now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
scope = 'account:acc-cli'
observe_live_loss_breaker(scope, 'acc-cli', 100000.0, now)
observe_live_loss_breaker(scope, 'acc-cli', 80000.0, now)
latch_live_loss_breaker_breach(scope, 'drawdown', {'kind': 'drawdown'}, now)
latch_live_loss_breaker_breach(scope, 'daily', {'kind': 'daily'}, now)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr

    reset = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "82000"], data_dir
    )
    assert reset.returncode == 0, reset.stderr
    payload = json.loads(reset.stdout)
    still = {s["scope"]: s["latched"] for s in payload["still_latched"]}
    assert still[f"account:{ACCOUNT}"] == ["daily"]


def test_reset_drawdown_refuses_a_scope_with_no_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = _run(
        ["reset-drawdown", "--account", "acc-missing", "--equity", "1000"],
        data_dir,
    )

    assert result.returncode == 1
    assert "no loss breaker state" in result.stderr


def test_reset_drawdown_requires_equity_when_observation_is_stale(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _seed_state(data_dir, stale=True)

    result = _run(["reset-drawdown", "--scope", f"account:{ACCOUNT}"], data_dir)

    assert result.returncode == 1
    assert "--equity" in result.stderr


def test_reset_drawdown_refuses_a_non_positive_last_observed_equity(
    tmp_path: Path,
) -> None:
    """A zero/negative stored equity is not a re-base figure: refuse it."""
    data_dir = tmp_path / "data"
    _seed_state(data_dir)
    zero_it = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.database import get_session, LiveLossBreakerStateModel\n"
            "from sqlalchemy import update\n"
            "with get_session() as s:\n"
            "    s.exec(update(LiveLossBreakerStateModel)\n"
            "        .where(LiveLossBreakerStateModel.scope == 'account:acc-cli')\n"
            "        .values(last_equity=0.0))\n"
            "    s.commit()\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert zero_it.returncode == 0, zero_it.stderr

    result = _run(["reset-drawdown", "--account", ACCOUNT], data_dir)

    assert result.returncode == 1
    assert "no usable last observed equity" in result.stderr

    # --equity still works: the operator states the figure explicitly.
    explicit = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "81000"], data_dir
    )
    assert explicit.returncode == 0, explicit.stderr


def test_reset_drawdown_exits_nonzero_when_a_cascade_scope_is_skipped(
    tmp_path: Path,
) -> None:
    """A partial cascade is not a success: the account scope resets but the
    deployment scope cannot be re-based (no usable last observed equity), so
    entries may still be refused - the CLI must exit non-zero."""
    data_dir = tmp_path / "data"
    _seed_account_with_deployment(data_dir)
    # Strip the deployment row's last_equity: its re-base lives in
    # P&L-adjusted units and there is nothing to fall back to.
    strip = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.database import get_session, LiveLossBreakerStateModel\n"
            "from sqlalchemy import update\n"
            "with get_session() as s:\n"
            "    s.exec(update(LiveLossBreakerStateModel)\n"
            "        .where(LiveLossBreakerStateModel.scope == 'deployment:dep-1')\n"
            "        .values(last_equity=None))\n"
            "    s.commit()\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert strip.returncode == 0, strip.stderr

    result = _run(
        ["reset-drawdown", "--account", ACCOUNT, "--equity", "82000"], data_dir
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    # The account scope DID reset - the failure is partial, not total.
    assert any(s["scope"] == f"account:{ACCOUNT}" for s in payload["scopes"])
    skipped = {s["scope"] for s in payload["skipped"]}
    assert "deployment:dep-1" in skipped
    assert "deployment:dep-1" in result.stderr


def test_status_reports_fractions_against_breaker_equity(tmp_path: Path) -> None:
    """``status`` must measure "now" against the breaker's own equity series
    (``last_equity``), not the balance snapshot - on cTrader those differ by
    unrealized P&L."""
    data_dir = tmp_path / "data"
    _seed_state(data_dir)  # day_start 100000, last_equity 80000 -> -20%
    seed_balance = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timezone;"
            "from shared.database import save_live_balance;"
            "from shared.models import LiveBalanceRecord;"
            "save_live_balance(LiveBalanceRecord("
            "    account_id='acc-cli', timestamp=datetime.now(timezone.utc),"
            "    balance=95000.0, equity=95000.0))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert seed_balance.returncode == 0, seed_balance.stderr

    result = _run(["status", "--account", ACCOUNT], data_dir)

    assert result.returncode == 0, result.stderr
    scope = json.loads(result.stdout)["scopes"][0]
    # Against breaker equity 80000: daily loss = (100000-80000)/100000 = 0.2.
    # Against the snapshot's 95000 it would wrongly read 0.05.
    assert scope["daily_loss_fraction_now"] == pytest.approx(0.2)
    assert scope["drawdown_fraction_now"] == pytest.approx(0.2)


def _seed_ledger_flag(data_dir: Path) -> None:
    code = (
        "from shared.database import record_live_control_audit;"
        "record_live_control_audit("
        "    action='ledger_reconciliation_required',"
        "    target_type='account_symbol',"
        "    target_id='acc-cli:AAPL',"
        "    actor='test', detail={'cause': 'unattributed close'})"
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


def test_resolve_ledger_flag_clears_the_unresolved_flag(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_ledger_flag(data_dir)

    result = _run(
        [
            "resolve-ledger-flag",
            "--account", ACCOUNT,
            "--symbol", "aapl",
            "--note", "reconciled against broker statement",
        ],
        data_dir,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["resolved_target"] == "acc-cli:AAPL"
    assert payload["was_unresolved"] is True
    assert payload["unresolved_flags"] == []

    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.database import account_has_ledger_reconciliation_flags;"
            "print(account_has_ledger_reconciliation_flags('acc-cli'))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "False"


def _seed_reanchor_state(data_dir: Path, *, stale_account: bool = False) -> None:
    """Account scope (equity 20k) + one anchored deployment scope.

    The deployment row implies own P&L ``last_equity - equity_anchor`` =
    96000 - 102000 = -6000, so the residual at account equity E is
    ``E + 6000`` and the scope equity after re-anchor is E.
    """
    account_when = (
        "datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)"
        if stale_account
        else "datetime.now(timezone.utc)"
    )
    code = f"""
from datetime import datetime, timezone
from shared.database import (
    LiveAccountModel, LiveDeploymentModel, get_session, init_db,
    observe_live_loss_breaker,
)
init_db()
now = datetime.now(timezone.utc)
with get_session() as session:
    session.add(LiveAccountModel(id='acc-cli', broker='test', name='acc-cli'))
    session.add(LiveDeploymentModel(
        id='{DEP_ID}', strategy_name='t', account_id='acc-cli',
        symbols_json=['AAPL'], timeframe='1h', status='PAUSED'))
    session.commit()
observe_live_loss_breaker('account:acc-cli', 'acc-cli', 20000.0, {account_when})
observe_live_loss_breaker(
    'deployment:{DEP_ID}', 'acc-cli', 96000.0, now, equity_anchor=102000.0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_reanchor_rebases_anchor_and_shifts_series(tmp_path: Path) -> None:
    """``re-anchor --deployment`` sets the anchor to the current residual,
    moves the whole equity series by the same delta, keeps the latch and
    writes a ``loss_breaker.reanchor`` audit row."""
    data_dir = tmp_path / "data"
    _seed_reanchor_state(data_dir)
    latch = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timezone;"
            "from shared.database import latch_live_loss_breaker_breach;"
            f"latch_live_loss_breaker_breach('deployment:{DEP_ID}', 'drawdown',"
            " {'kind': 'drawdown'}, datetime.now(timezone.utc))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert latch.returncode == 0, latch.stderr

    result = _run(
        ["re-anchor", "--deployment", DEP_ID, "--account-equity", "20000"],
        data_dir,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # residual = 20000 - (-6000) = 26000; scope equity = 26000 + (-6000) = 20000.
    assert payload["equity_anchor_after"] == pytest.approx(26_000.0)
    assert payload["equity_anchor_before"] == pytest.approx(102_000.0)
    assert payload["last_equity_after"] == pytest.approx(20_000.0)
    assert payload["day_start_equity_after"] == pytest.approx(20_000.0)
    assert payload["peak_equity_after"] == pytest.approx(20_000.0)
    assert payload["drawdown_breached"] is True
    assert payload["equity_source"] == "--account-equity"

    status = _run(["status", "--deployment", DEP_ID], data_dir)
    assert status.returncode == 0, status.stderr
    state = json.loads(status.stdout)["scopes"][0]
    assert state["equity_anchor"] == pytest.approx(26_000.0)
    assert state["last_equity"] == pytest.approx(20_000.0)
    assert state["drawdown_breached"] is True

    audit = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json;"
            "from shared.database import list_live_control_audit;"
            f"rows = [r for r in list_live_control_audit("
            f"target_id='deployment:{DEP_ID}')"
            "        if r['action'] == 'loss_breaker.reanchor'];"
            "print(json.dumps(rows))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert audit.returncode == 0, audit.stderr
    rows = json.loads(audit.stdout)
    assert len(rows) == 1
    assert rows[0]["succeeded"] is True
    assert rows[0]["detail"]["equity_anchor_after"] == pytest.approx(26_000.0)


def test_reanchor_defaults_to_fresh_account_scope_equity(tmp_path: Path) -> None:
    """Without ``--account-equity`` the account scope's own last observed
    breaker equity is the basis (fresh observation required)."""
    data_dir = tmp_path / "data"
    _seed_reanchor_state(data_dir)

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["equity_source"] == "last_observed_equity"
    assert payload["account_equity"] == pytest.approx(20_000.0)
    assert payload["equity_anchor_after"] == pytest.approx(26_000.0)


def test_reanchor_refuses_account_scope(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_reanchor_state(data_dir)

    result = _run(
        ["re-anchor", "--scope", f"account:{ACCOUNT}", "--account-equity", "20000"],
        data_dir,
    )

    assert result.returncode == 1
    assert "deployment scopes only" in result.stderr


def test_reanchor_refuses_stale_account_equity(tmp_path: Path) -> None:
    """A stale account-scope observation must not re-base the anchor; the
    operator states the figure with ``--account-equity`` instead."""
    data_dir = tmp_path / "data"
    _seed_reanchor_state(data_dir, stale_account=True)

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 1
    assert "--account-equity" in result.stderr

    explicit = _run(
        ["re-anchor", "--deployment", DEP_ID, "--account-equity", "20000"],
        data_dir,
    )
    assert explicit.returncode == 0, explicit.stderr


def test_reanchor_rejects_non_positive_account_equity(tmp_path: Path) -> None:
    """``--account-equity`` is operator-asserted CURRENT equity: a
    non-positive or non-finite figure is refused before any state moves."""
    data_dir = tmp_path / "data"
    _seed_reanchor_state(data_dir)

    for bad in ("0", "-5", "nan", "inf"):
        result = _run(
            ["re-anchor", "--deployment", DEP_ID, "--account-equity", bad],
            data_dir,
        )
        assert result.returncode == 1, (bad, result.stdout, result.stderr)
        assert "positive finite" in result.stderr


def test_reanchor_refuses_non_positive_stored_account_equity(
    tmp_path: Path,
) -> None:
    """Without ``--account-equity`` the account scope's stored equity is the
    basis; a zero/negative stored figure must be refused, not re-anchored on.
    """
    data_dir = tmp_path / "data"
    code = f"""
from datetime import datetime, timezone
from shared.database import (
    LiveAccountModel, LiveDeploymentModel, get_session, init_db,
    observe_live_loss_breaker,
)
init_db()
now = datetime.now(timezone.utc)
with get_session() as session:
    session.add(LiveAccountModel(id='acc-cli', broker='test', name='acc-cli'))
    session.add(LiveDeploymentModel(
        id='{DEP_ID}', strategy_name='t', account_id='acc-cli',
        symbols_json=['AAPL'], timeframe='1h', status='PAUSED'))
    session.commit()
observe_live_loss_breaker('account:acc-cli', 'acc-cli', 0.0, now)
observe_live_loss_breaker(
    'deployment:{DEP_ID}', 'acc-cli', 50000.0, now, equity_anchor=50000.0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 1
    assert "positive finite" in result.stderr

    # --account-equity still works: the operator states the figure explicitly.
    explicit = _run(
        ["re-anchor", "--deployment", DEP_ID, "--account-equity", "20000"],
        data_dir,
    )
    assert explicit.returncode == 0, explicit.stderr


def test_reanchor_refuses_scope_with_no_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = _run(
        ["re-anchor", "--deployment", "dep-missing", "--account-equity", "20000"],
        data_dir,
    )

    assert result.returncode == 1
    assert "no loss breaker state" in result.stderr


def test_reanchor_uses_current_account_after_repoint_without_observe(
    tmp_path: Path,
) -> None:
    """B1 pin through the CLI's default equity source: after a re-point with
    no intervening deployment observe, ``re-anchor`` must measure the NEW
    account's scope equity and move the breaker row to that account."""
    data_dir = tmp_path / "data"
    old_account = "acc-repoint-cli-old"
    new_account = "acc-repoint-cli-new"
    _seed_cli_deployment(data_dir, DEP_ID, old_account)
    _seed_cli_account(data_dir, new_account)
    _run_code(
        f"""
from datetime import datetime, timedelta, timezone
from live_runner.loss_breaker import observe_account_loss_breaker_equity
from shared.database import update_live_deployment_account
from shared.models import LiveDeploymentConfig
now = datetime.now(timezone.utc)
dep = LiveDeploymentConfig(
    id='{DEP_ID}', strategy_name='t', account_id='{old_account}',
    symbols=['AAPL'], timeframe='1h', status='PAUSED',
    signal_source='internal', signal_config={{}}, settings={{}})
observe_account_loss_breaker_equity(
    '{old_account}', 100000.0, now=now, deployments=[dep],
    deployment_pnls={{'{DEP_ID}': -10000.0}}, env_values={{}})
update_live_deployment_account('{DEP_ID}', '{new_account}')
observe_account_loss_breaker_equity(
    '{new_account}', 70000.0, now=now + timedelta(seconds=1),
    deployments=[], deployment_pnls={{}}, env_values={{}})
""",
        data_dir,
    )

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["equity_source"] == "last_observed_equity"
    assert payload["account_equity"] == pytest.approx(70_000.0)
    assert payload["equity_anchor_after"] == pytest.approx(70_000.0)
    assert payload["last_equity_after"] == pytest.approx(70_000.0)

    status = _run(["status", "--account", new_account], data_dir)
    assert status.returncode == 0, status.stderr
    scopes = json.loads(status.stdout)["scopes"]
    dep_state = next(s for s in scopes if s["scope"] == f"deployment:{DEP_ID}")
    assert dep_state["account_id"] == new_account
    assert dep_state["equity_anchor"] == pytest.approx(70_000.0)


def test_reanchor_refuses_future_account_equity_observation(
    tmp_path: Path,
) -> None:
    """The CLI's default equity source refuses an account observation dated
    in the future instead of re-anchoring on it."""
    data_dir = tmp_path / "data"
    account_id = "acc-future-equity"
    _seed_cli_deployment(data_dir, DEP_ID, account_id)
    _run_code(
        f"""
from datetime import datetime, timedelta, timezone
from shared.database import observe_live_loss_breaker
future = datetime.now(timezone.utc) + timedelta(days=1)
observe_live_loss_breaker('account:{account_id}', '{account_id}', 70000.0, future)
observe_live_loss_breaker(
    'deployment:{DEP_ID}', '{account_id}', 100000.0, future)
""",
        data_dir,
    )

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 1
    assert "in the future" in result.stderr


def test_reset_drawdown_refuses_future_breaker_observation(
    tmp_path: Path,
) -> None:
    """``reset-drawdown`` refuses a stored observation dated in the future."""
    data_dir = tmp_path / "data"
    account_id = "acc-future-reset"
    _run_code(
        f"""
from datetime import datetime, timedelta, timezone
from shared.database import observe_live_loss_breaker
future = datetime.now(timezone.utc) + timedelta(days=1)
observe_live_loss_breaker('account:{account_id}', '{account_id}', 80000.0, future)
""",
        data_dir,
    )

    result = _run(["reset-drawdown", "--account", account_id], data_dir)

    assert result.returncode == 1
    assert "in the future" in result.stderr


def test_reanchor_bounds_deployment_rows_by_account_equity_time(
    tmp_path: Path,
) -> None:
    """The deployment-row age check must use the ACCOUNT EQUITY observation
    time, not the re-anchor's wall clock: a fills row observed after that
    equity snapshot is refused even though it is fresh right now."""
    data_dir = tmp_path / "data"
    account_id = "acc-cli"
    _seed_cli_deployment(data_dir, DEP_ID, account_id)
    _run_code(
        f"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from live_runner.loss_breaker import observe_account_loss_breaker_equity
from shared.database import (
    observe_live_loss_breaker, save_live_execution, save_live_signal)
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
now = datetime.now(timezone.utc)
row_time = now - timedelta(seconds=100)
equity_time = now - timedelta(seconds=500)
dep = LiveDeploymentConfig(
    id='{DEP_ID}', strategy_name='t', account_id='{account_id}',
    symbols=['AAPL'], timeframe='1h', status='PAUSED',
    signal_source='internal', signal_config={{}}, settings={{}})
observe_account_loss_breaker_equity(
    '{account_id}', 100000.0, now=row_time, deployments=[dep],
    deployment_pnls={{'{DEP_ID}': 0.0}}, env_values={{}})
observe_live_loss_breaker(
    'account:{account_id}', '{account_id}', 70000.0, equity_time)
signal = LiveSignal(
    id=uuid4(), deployment_id='{DEP_ID}', symbol='AAPL', action='buy',
    theoretical_price=100.0, timestamp=row_time, metadata={{}})
save_live_signal(signal)
save_live_execution(LiveExecution(
    signal_id=signal.id, actual_price=100.0, filled_qty=10.0,
    commission=0.0, timestamp=row_time, broker_order_id='bo-cli-age',
    actual_price_observed='observed'))
""",
        data_dir,
    )

    result = _run(["re-anchor", "--deployment", DEP_ID], data_dir)

    assert result.returncode == 1
    assert "AFTER the equity observation" in result.stderr


def test_resolve_ledger_flag_reports_when_nothing_was_flagged(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = _run(
        ["resolve-ledger-flag", "--account", ACCOUNT, "--symbol", "AAPL"],
        data_dir,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["was_unresolved"] is False

"""The kill-switch CLI (#300): `scripts/live_kill_switch.py`.

Throwaway SQLite sandboxes only; nothing here touches the production database.

The properties under test are the ones that make an operator CLI safe to hand
out: ``--help`` answers without touching the database, a halt written by one
process is in force for the next one (which is the whole point of persisting it),
a typo'd scope is refused instead of written as a halt that covers nothing, and
every attempt - refused ones included - lands in the control audit trail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import live_kill_switch
from shared.database import (
    LiveAccountModel,
    get_live_kill_switch_state,
    get_session,
    list_live_control_audit,
    read_live_kill_switch_scopes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "acc-cli"


@pytest.fixture
def account(isolated_db_sandbox) -> str:
    """One known live account: an account-scoped halt must name a real one."""
    database, _engine, _data_dir = isolated_db_sandbox
    database.init_db()
    with get_session() as session:
        session.add(LiveAccountModel(id=ACCOUNT, broker="alpaca", name=ACCOUNT))
        session.commit()
    return ACCOUNT


def _child_env(data_dir: Path) -> dict[str, str]:
    """An environment that cannot reach the live platform's database.

    ``DATABASE_URL`` is dropped *and* pointed at the sandbox: the engine builder
    prefers it over ``DATA_DIR``, so inheriting it would have a CLI subprocess
    write real halts into a real live database and then assert on the sandbox.
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
        [sys.executable, "-m", "scripts.live_kill_switch", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )


def _seed_account(data_dir: Path) -> subprocess.CompletedProcess[str]:
    """Register the account the subprocess tests halt, in that same sandbox."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import shared.database as d;"
            "d.init_db();"
            "session = d.get_session().__enter__();"
            "session.add(d.LiveAccountModel(id='acc-cli', broker='alpaca',"
            " name='acc-cli'));"
            "session.commit()",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(data_dir),
        timeout=300,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["halt", "--help"],
        ["resume", "--help"],
        ["status", "--help"],
        ["accounts", "--help"],
    ],
)
def test_help_answers_without_touching_the_database(
    tmp_path: Path, argv: list[str]
) -> None:
    """An operator reads --help before deciding; it must cost nothing.

    The database file is the thing that must not appear: creating it would run
    the migration set against whatever ``DATA_DIR`` happens to be configured, and
    a command that answers a question has no business writing a schema.
    """
    data_dir = tmp_path / "data"

    result = _run(argv, data_dir)

    assert result.returncode == 0, result.stderr
    assert not (data_dir / "backtests.db").exists()
    assert "usage:" in result.stdout


def test_a_halt_written_by_one_process_is_in_force_for_the_next(tmp_path: Path) -> None:
    """The restart property, across real processes.

    The daemon is not the only thing that restarts: the operator's shell, a
    CI job and an automation script all halt and read in different processes, and
    an in-memory flag would report 'nothing halted' to the one that has to act on
    it.
    """
    data_dir = tmp_path / "data"
    seeded = _seed_account(data_dir)
    assert seeded.returncode == 0, seeded.stderr

    halted = _run(
        [
            "halt",
            "--scope",
            ACCOUNT,
            "--mode",
            "flatten",
            "--reason",
            "incident 42",
            "--actor",
            "jane",
        ],
        data_dir,
    )
    assert halted.returncode == 0, halted.stderr
    assert f"Halted scope '{ACCOUNT}' in 'flatten' mode." in halted.stdout

    status = _run(["status"], data_dir)

    assert status.returncode == 0, status.stderr
    assert ACCOUNT in status.stdout
    assert "flatten" in status.stdout
    assert "incident 42" in status.stdout


def test_a_lifted_halt_is_gone_from_the_next_process_too(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assert _seed_account(data_dir).returncode == 0
    halted = _run(["halt", "--scope", "global", "--mode", "orders"], data_dir)
    assert halted.returncode == 0, halted.stderr

    lifted = _run(["resume", "--scope", "global"], data_dir)
    assert lifted.returncode == 0, lifted.stderr
    assert "Lifted the halt on scope 'global'." in lifted.stdout

    status = _run(["status"], data_dir)
    assert status.returncode == 0, status.stderr
    assert "nothing is halted" in status.stdout


def test_resuming_a_scope_that_is_not_halted_says_so(tmp_path: Path) -> None:
    """An operator asking to resume an unhalted account has another problem.

    Reporting that as a success would leave them believing the account was halted
    a moment ago and is safe now, which is the wrong belief to install.
    """
    data_dir = tmp_path / "data"
    assert _seed_account(data_dir).returncode == 0

    result = _run(["resume", "--scope", "global"], data_dir)

    assert result.returncode == 1
    assert "No halt was in force on scope 'global'" in result.stderr


def test_a_typo_d_account_scope_is_refused_and_writes_nothing(account) -> None:
    """A halt that covers nothing while it reports being armed is worse than none."""
    exit_code = live_kill_switch.main(["halt", "--scope", "acc-typo", "--mode", "flatten"])

    assert exit_code == 1
    assert read_live_kill_switch_scopes() == {}


def test_a_mode_this_build_cannot_interpret_is_refused_and_audited(account) -> None:
    """A mistyped mode is refused *and* recorded as an attempt.

    Validated in the command rather than by argparse `choices`, because the
    parser's own exit happens before the audited refusal path - so an attempted
    halt with a bad mode would be missing from the control audit trail, which is
    exactly the event somebody has to find later.
    """
    exit_code = live_kill_switch.main(["halt", "--scope", "global", "--mode", "stop"])

    assert exit_code == 1
    assert read_live_kill_switch_scopes() == {}
    refusals = [
        entry
        for entry in list_live_control_audit()
        if entry["action"] == "kill_switch.halt" and entry["succeeded"] is False
    ]
    assert refusals[-1]["target_id"] == "global"
    assert "stop" in refusals[-1]["detail"]["error"]


def test_a_json_result_is_the_only_thing_on_stdout(account, capsys) -> None:
    """A caller parsing `--json` must not have to unwrap a log line.

    The CLI's own warning about the halt goes to stderr (the console handlers are
    rerouted in `main`), so stdout stays a single JSON document.
    """
    exit_code = live_kill_switch.main(
        ["halt", "--scope", ACCOUNT, "--mode", "entries", "--json"]
    )
    out, err = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(out)["mode"] == "entries"
    assert "Kill switch halt written by the CLI" in err


def test_a_halt_is_recorded_in_the_control_audit_trail(account) -> None:
    """Who halted trading, and why, stays answerable afterwards."""
    exit_code = live_kill_switch.main(
        [
            "halt",
            "--scope",
            ACCOUNT,
            "--mode",
            "orders",
            "--reason",
            "incident 42",
            "--actor",
            "jane",
        ]
    )

    assert exit_code == 0
    halts = [
        entry
        for entry in list_live_control_audit()
        if entry["action"] == "kill_switch.halt" and entry["target_id"] == ACCOUNT
    ]
    assert halts, "the halt was not written to live_control_audit"
    assert halts[-1]["actor"] == "jane"
    assert halts[-1]["succeeded"] is True
    assert halts[-1]["detail"]["mode"] == "orders"
    assert halts[-1]["detail"]["reason"] == "incident 42"


def test_a_refused_halt_is_recorded_too(isolated_db_sandbox) -> None:
    """A refused attempt is the more interesting half of the record."""
    exit_code = live_kill_switch.main(["halt", "--scope", "acc-typo", "--mode", "flatten"])

    assert exit_code == 1
    refusals = [
        entry
        for entry in list_live_control_audit()
        if entry["action"] == "kill_switch.halt" and entry["succeeded"] is False
    ]
    assert refusals, "a refused halt was not recorded as an attempt"
    assert "acc-typo" in refusals[-1]["target_id"]


def test_status_reports_the_stored_halt_and_its_history(account) -> None:
    """The read path reports what a later reader will enforce, not a summary."""
    exit_code = live_kill_switch.main(
        ["halt", "--scope", ACCOUNT, "--mode", "flatten", "--reason", "incident 42"]
    )

    assert exit_code == 0
    state = get_live_kill_switch_state(ACCOUNT)
    assert state is not None
    assert state.active is True
    assert state.mode == "flatten"
    assert state.reason == "incident 42"
    assert read_live_kill_switch_scopes() == {ACCOUNT: "flatten"}


def test_status_reports_an_unreadable_state_rather_than_an_empty_list(
    isolated_db_sandbox, monkeypatch, capsys
) -> None:
    """An operator must be able to tell 'nothing halted' from 'cannot tell'.

    The fail-closed answer (every account treated as halted in 'entries' mode) is
    only useful if the CLI says so, instead of printing an empty list.
    """
    import live_runner.kill_switch as kill_switch

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    exit_code = live_kill_switch.main(["status"])

    assert exit_code == 1
    assert "could not be read" in capsys.readouterr().err


def test_the_status_json_payload_carries_the_fail_closed_flag(account, capsys) -> None:
    live_kill_switch.main(["halt", "--scope", "global", "--mode", "entries"])
    capsys.readouterr()

    exit_code = live_kill_switch.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["readable"] is True
    assert payload["global_halted"] is True
    assert payload["halted_accounts"] == []
    assert payload["modes"] == ["entries", "orders", "flatten"]
    assert payload["halts"] == [{"scope": "global", "mode": "entries"}]

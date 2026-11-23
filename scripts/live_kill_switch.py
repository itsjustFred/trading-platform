"""Set, lift and read the live kill switch (#300).

The operator-facing half of the kill switch. It writes one row in the
``live_kill_switch`` table of the live database, which is the state the daemon
reads before every entry and on every tick, so a halt set here holds across a
restart, a redeploy and the daemon being down altogether.

    python -m scripts.live_kill_switch halt --scope global --mode flatten --reason "incident 42"
    python -m scripts.live_kill_switch resume --scope global
    python -m scripts.live_kill_switch status
    python -m scripts.live_kill_switch accounts

``--scope`` is either the reserved ``global`` scope or one live account id;
``accounts`` lists the ones that exist, because a scope that names no account is
refused rather than written as a halt that covers nothing while it reports being
armed. ``status`` and ``accounts`` read only.

Every command answers ``--help`` without touching the database, and importing
this module starts nothing: the daemon is never started from here.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of halting trading against another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.live_kill_switch
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("live_kill_switch")
# --- end import-origin guard ---

from live_runner.kill_switch import (
    KILL_SWITCH_GLOBAL_SCOPE,
    KillSwitchMode,
    kill_switch_payload,
    read_kill_switch_state,
)
from shared.database import (
    clear_live_kill_switch,
    get_live_kill_switch_state,
    list_live_accounts,
    record_live_control_audit,
    set_live_kill_switch,
)
from shared.logging import get_logger, route_console_logs_to_stderr

logger = get_logger("live-runner")

#: What to attribute a halt to when ``--actor`` is not given. There is no
#: authentication on this platform, so this is attribution by claim, not proof -
#: recorded rather than left blank because an unattributed halt is worse than a
#: self-declared one.
DEFAULT_ACTOR = "cli"


def _default_actor() -> str:
    try:
        return getpass.getuser() or DEFAULT_ACTOR
    except Exception:  # pragma: no cover - no passwd entry (containers, some CI)
        return DEFAULT_ACTOR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live_kill_switch",
        description=(
            "Set, lift and read the live kill switch (#300). The daemon reads this "
            "state before every entry and on every tick, so a halt set here holds "
            "across restarts."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    halt = commands.add_parser(
        "halt", help="Halt trading on one scope (global, or one live account)"
    )
    halt.add_argument(
        "--scope",
        required=True,
        help=(
            f"'{KILL_SWITCH_GLOBAL_SCOPE}' for every account, or a live account id "
            "(see the accounts command)"
        ),
    )
    # Not an argparse `choices`: a mode the parser rejects exits before the
    # audited refusal path in `main`, so an attempted halt with a mistyped mode
    # would be missing from the control audit trail. Validated in `_halt` instead.
    halt.add_argument(
        "--mode",
        required=True,
        metavar="{" + ",".join(mode.value for mode in KillSwitchMode) + "}",
        help=(
            "'entries' refuses new entries; 'orders' also cancels working orders; "
            "'flatten' also closes every open position"
        ),
    )
    halt.add_argument("--reason", default="", help="Why, for the incident record")
    halt.add_argument("--actor", default=_default_actor(), help="Who is halting")

    resume = commands.add_parser("resume", help="Lift the halt on one scope")
    resume.add_argument("--scope", required=True, help="The scope to lift")
    resume.add_argument("--actor", default=_default_actor(), help="Who is lifting")

    for command, help_text in (
        ("status", "Show the halts in force (read-only)"),
        ("accounts", "List the account scopes a halt may name (read-only)"),
    ):
        reader = commands.add_parser(command, help=help_text)
        reader.add_argument("--json", action="store_true", help="Print JSON")

    for writer in (halt, resume):
        writer.add_argument("--json", action="store_true", help="Print JSON")

    return parser


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _halt(args: argparse.Namespace) -> int:
    if KillSwitchMode.parse(args.mode) is None:
        raise ValueError(
            f"Unknown kill switch mode {args.mode!r}; expected one of "
            f"{', '.join(mode.value for mode in KillSwitchMode)}"
        )
    state = set_live_kill_switch(
        args.scope,
        args.mode,
        actor=args.actor,
        reason=args.reason,
    )
    record_live_control_audit(
        action="kill_switch.halt",
        target_type="live_kill_switch",
        target_id=state.scope,
        actor=args.actor,
        succeeded=True,
        detail={"mode": state.mode, "reason": state.reason},
    )
    logger.warning(
        "Kill switch halt written by the CLI",
        extra={
            "data": {
                "scope": state.scope,
                "mode": state.mode,
                "actor": args.actor,
                "reason": state.reason,
            }
        },
    )
    if args.json:
        _print_json(
            {
                "halted": True,
                "scope": state.scope,
                "mode": state.mode,
                "halted_at": state.halted_at,
                "halted_by": state.halted_by,
                "reason": state.reason,
            }
        )
        return 0
    print(f"Halted scope '{state.scope}' in '{state.mode}' mode.")
    print(f"  since   {state.halted_at} (UTC)")
    print(f"  by      {state.halted_by}")
    if state.reason:
        print(f"  reason  {state.reason}")
    print(
        "Lift it with: python -m scripts.live_kill_switch resume "
        f"--scope {state.scope}"
    )
    return 0


def _resume(args: argparse.Namespace) -> int:
    lifted = clear_live_kill_switch(args.scope, actor=args.actor)
    record_live_control_audit(
        action="kill_switch.resume",
        target_type="live_kill_switch",
        target_id=str(args.scope).strip(),
        actor=args.actor,
        succeeded=lifted,
        detail={"lifted": lifted},
    )
    if not lifted:
        # An operator asking to resume an account that was not halted has a
        # different problem than the one they think they have, so this is not a
        # silent success.
        print(
            f"No halt was in force on scope '{args.scope}'; nothing to lift.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        _print_json({"halted": False, "scope": str(args.scope).strip()})
        return 0
    logger.warning(
        "Kill switch halt lifted by the CLI",
        extra={
            "data": {
                "scope": str(args.scope).strip(),
                "actor": args.actor,
            }
        },
    )
    print(f"Lifted the halt on scope '{args.scope}'.")
    return 0


def _status(args: argparse.Namespace) -> int:
    state = read_kill_switch_state()
    if not state.readable:
        # Fail closed, and say so: an operator reading this needs to know that
        # every account is being treated as halted in 'entries' mode, not that
        # nothing is halted.
        print(
            "The kill switch state could not be read; every account is treated as "
            f"halted in 'entries' mode. Reason: {state.error}",
            file=sys.stderr,
        )
        return 1
    halts = []
    for scope in state.halted_scopes():
        record = get_live_kill_switch_state(scope)
        halts.append(
            {
                "scope": scope,
                "mode": (record.mode if record is not None else state.halts[scope].value),
                "halted_at": (record.halted_at if record is not None else None),
                "halted_by": (record.halted_by if record is not None else "unknown"),
                "reason": (record.reason if record is not None else ""),
            }
        )
    if args.json:
        _print_json({**kill_switch_payload(state), "halt_details": halts})
        return 0
    if not halts:
        print("Kill switch: nothing is halted.")
        return 0
    print(f"Kill switch: {len(halts)} scope(s) halted.")
    for halt in halts:
        reason = f"  reason='{halt['reason']}'" if halt["reason"] else ""
        print(
            f"  {halt['scope']:<24} {halt['mode']:<8} since {halt['halted_at']} "
            f"by {halt['halted_by']}{reason}"
        )
    return 0


def _accounts(args: argparse.Namespace) -> int:
    accounts = [str(account.id) for account in list_live_accounts()]
    if args.json:
        _print_json({"accounts": accounts, "global_scope": KILL_SWITCH_GLOBAL_SCOPE})
        return 0
    print(f"Scopes a halt may name ({KILL_SWITCH_GLOBAL_SCOPE} covers every account):")
    for account_id in accounts:
        print(f"  {account_id}")
    return 0


_COMMANDS = {
    "halt": _halt,
    "resume": _resume,
    "status": _status,
    "accounts": _accounts,
}


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code; never raises for a refusal."""
    # stdout is the result document for --json callers; the log lines go to stderr.
    route_console_logs_to_stderr()
    args = _build_parser().parse_args(argv)
    command = _COMMANDS[args.command]
    try:
        return command(args)
    except Exception as exc:
        # A refused halt is audited like any other attempt, and it is the more
        # interesting half of the record: it shows what was tried against a
        # scope that does not exist or with a mode this build cannot interpret.
        # The audit write never raises, so it cannot replace the failure it is
        # recording.
        record_live_control_audit(
            action=f"kill_switch.{args.command}",
            target_type="live_kill_switch",
            target_id=str(getattr(args, "scope", "") or "").strip(),
            actor=str(getattr(args, "actor", DEFAULT_ACTOR) or DEFAULT_ACTOR),
            succeeded=False,
            detail={"error": str(exc), "type": type(exc).__name__},
        )
        logger.warning(
            "Kill switch command failed",
            extra={
                "data": {
                    "command": args.command,
                    "scope": str(getattr(args, "scope", "") or ""),
                    "error": repr(exc),
                }
            },
        )
        print(f"Refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

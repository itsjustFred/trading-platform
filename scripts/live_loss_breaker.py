"""Read and reset the live loss circuit breaker (#301).

The breaker state lives in ``live_loss_breaker_state`` in the live database:
one row per ``account:<id>`` scope and per ``deployment:<id>`` scope. The
daemon observes broker equity on every balance poll and refuses new entries
(never exits) while a scope's daily-loss or drawdown latch stands.

    python -m scripts.live_loss_breaker status [--account ID] [--deployment ID]
    python -m scripts.live_loss_breaker reset-drawdown --account ID
    python -m scripts.live_loss_breaker reset-drawdown --deployment ID
    python -m scripts.live_loss_breaker reset-drawdown --scope account:ID --equity 95000

The daily breaker resets itself at the UTC day boundary; there is no daily
reset command on purpose. The drawdown breaker needs this explicit reset: it
clears the latch AND re-bases the peak equity to the current equity, so the
breaker does not re-trip on the observation that cleared it. The equity used
for the re-base is the latest recorded balance snapshot for the scope's
account, or ``--equity`` when the operator wants to state it explicitly (a
scope with no snapshot and no ``--equity`` is refused rather than guessed).

``status`` and ``reset-drawdown --help`` never start the daemon and ``status``
only reads; importing this module starts nothing.
"""

from __future__ import annotations

import argparse
import json
import sys

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.live_loss_breaker
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("live_loss_breaker")
# --- end import-origin guard ---

from live_runner.loss_breaker import (
    account_loss_breaker_limits,
    account_loss_breaker_scope,
    deployment_loss_breaker_limits,
    deployment_loss_breaker_scope,
)
from shared.database import (
    get_latest_live_balance,
    get_live_loss_breaker_state,
    list_live_deployments,
    list_live_loss_breaker_states,
)
from shared.logging import get_logger, route_console_logs_to_stderr
from shared.models import LiveLossBreakerRecord

logger = get_logger("live-runner")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live_loss_breaker",
        description=(
            "Read the live loss circuit breaker (#301) or reset its "
            "drawdown latch. The daily breaker resets at the UTC day "
            "boundary on its own."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="Show breaker state per scope (read-only)."
    )
    status.add_argument("--account", help="Only scopes of this account id.")
    status.add_argument("--deployment", help="Only this deployment id.")

    reset = sub.add_parser(
        "reset-drawdown",
        help=(
            "Clear the drawdown latch and re-base the peak equity to the "
            "current equity for one scope."
        ),
    )
    target = reset.add_mutually_exclusive_group(required=True)
    target.add_argument("--account", help="Reset the account:<id> scope.")
    target.add_argument("--deployment", help="Reset the deployment:<id> scope.")
    target.add_argument("--scope", help="Raw scope, e.g. account:alpaca-paper.")
    reset.add_argument(
        "--equity",
        type=float,
        default=None,
        help=(
            "Equity to re-base the peak to. Defaults to the latest recorded "
            "balance snapshot for the scope's account."
        ),
    )
    return parser


def _scope_for_args(args: argparse.Namespace) -> str:
    if args.scope:
        return args.scope.strip()
    if args.account:
        return account_loss_breaker_scope(args.account)
    return deployment_loss_breaker_scope(args.deployment)


def _format_state(record: LiveLossBreakerRecord, *, equity: float | None) -> dict:
    daily_loss = None
    drawdown = None
    if equity is not None and record.day_start_equity > 0:
        daily_loss = (record.day_start_equity - equity) / record.day_start_equity
    if equity is not None and record.peak_equity > 0:
        drawdown = (record.peak_equity - equity) / record.peak_equity
    return {
        "scope": record.scope,
        "account_id": record.account_id,
        "utc_day": record.utc_day,
        "day_start_equity": record.day_start_equity,
        "peak_equity": record.peak_equity,
        "last_equity": record.last_equity if record.last_equity is not None else equity,
        "daily_breached": record.daily_breached,
        "drawdown_breached": record.drawdown_breached,
        "daily_loss_fraction_now": daily_loss,
        "drawdown_fraction_now": drawdown,
        "breach_detail": record.breach_detail,
        "last_observed_at": (
            record.last_observed_at.isoformat() if record.last_observed_at else None
        ),
    }


def _limits_for_scope(scope: str, deployments_by_id: dict) -> dict | None:
    """Effective limits for display; None for a scope that cannot be resolved."""
    kind, _, ident = scope.partition(":")
    try:
        if kind == "account":
            limits = account_loss_breaker_limits(ident)
        elif kind == "deployment":
            deployment = deployments_by_id.get(ident)
            if deployment is None:
                return None
            limits = deployment_loss_breaker_limits(deployment)
        else:
            return None
    except ValueError as exc:
        return {"config_error": str(exc)}
    return {
        "enabled": limits.enabled,
        "daily_loss_fraction": limits.daily_loss_fraction,
        "max_drawdown_fraction": limits.max_drawdown_fraction,
    }


def _cmd_status(args: argparse.Namespace) -> int:
    records = list_live_loss_breaker_states(account_id=args.account)
    if args.deployment:
        scope = deployment_loss_breaker_scope(args.deployment)
        record = get_live_loss_breaker_state(scope)
        records = [record] if record is not None else []
    deployments_by_id = {
        str(d.id): d for d in list_live_deployments()
    }
    balances = {}
    out = []
    for record in records:
        if record.account_id not in balances:
            latest = get_latest_live_balance(record.account_id)
            balances[record.account_id] = latest.equity if latest else None
        payload = _format_state(record, equity=balances[record.account_id])
        payload["limits"] = _limits_for_scope(record.scope, deployments_by_id)
        out.append(payload)
    print(json.dumps({"scopes": out}, indent=2, default=str))
    return 0


def _cmd_reset_drawdown(args: argparse.Namespace) -> int:
    scope = _scope_for_args(args)
    record = get_live_loss_breaker_state(scope)
    if record is None:
        print(
            f"error: no loss breaker state for scope {scope!r}; nothing to reset",
            file=sys.stderr,
        )
        return 1
    equity = args.equity
    if equity is None:
        latest = get_latest_live_balance(record.account_id)
        if latest is None:
            print(
                f"error: no balance snapshot for account {record.account_id!r}; "
                "pass --equity to state the re-base equity explicitly",
                file=sys.stderr,
            )
            return 1
        equity = float(latest.equity)
    if not equity > 0:
        print(f"error: --equity must be positive, got {equity!r}", file=sys.stderr)
        return 1
    from live_runner.loss_breaker import reset_drawdown_breaker

    updated = reset_drawdown_breaker(
        scope, equity=equity, now=datetime.now(timezone.utc)
    )
    if updated is None:
        print(
            f"error: no loss breaker state for scope {scope!r}; nothing to reset",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "reset": "drawdown",
                "scope": scope,
                "rebase_equity": equity,
                "peak_equity": updated.peak_equity,
                "drawdown_breached": updated.drawdown_breached,
                "reset_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    route_console_logs_to_stderr()
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "reset-drawdown":
        return _cmd_reset_drawdown(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

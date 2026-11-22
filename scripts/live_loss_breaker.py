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
for the re-base is the scope's own last observed equity - the same series the
breaker itself measures (on cTrader that includes unrealized P&L, which the
balance snapshot's equity slot does NOT) - refused when that observation is
missing, non-positive or older than ``STALE_SNAPSHOT_SECONDS`` - or
``--equity`` when the operator wants to state it explicitly. Resetting an
ACCOUNT scope also resets every deployment scope on that account (each
re-based to its own last observed equity): with the defaults every
deployment scope latches together with the account scope, so resetting only
``account:<id>`` would leave entries blocked by stale deployment latches.
``--equity`` applies to the account scope; deployment scopes always re-base
to their own last equity because it lives in deployment-P&L-adjusted units,
not raw account equity.

An unresolved ``ledger_reconciliation_required`` audit flag (#277) fails
deployment-scoped entries closed until an operator reconciles it:

    python -m scripts.live_loss_breaker resolve-ledger-flag --account ID --symbol SYM --note "..."

The resolution is itself an append-only audit row; a flag newer than the
latest resolution blocks again.

``status`` and ``reset-drawdown --help`` never start the daemon and ``status``
only reads; importing this module starts nothing.
"""

from __future__ import annotations

import argparse
import json
import math
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
    list_unresolved_ledger_reconciliation_flags,
    resolve_ledger_reconciliation_flag,
)
from shared.logging import get_logger, route_console_logs_to_stderr
from shared.models import LiveLossBreakerRecord

logger = get_logger("live-runner")

#: A balance snapshot older than this is too stale to re-base a peak on.
STALE_SNAPSHOT_SECONDS = 600.0


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
    target.add_argument("--account", help="Reset the account:<id> scope and its deployment scopes.")
    target.add_argument("--deployment", help="Reset the deployment:<id> scope.")
    target.add_argument("--scope", help="Raw scope, e.g. account:alpaca-paper.")
    reset.add_argument(
        "--equity",
        type=float,
        default=None,
        help=(
            "Equity to re-base the peak to. Defaults to the scope's own "
            "last observed breaker equity."
        ),
    )

    resolve = sub.add_parser(
        "resolve-ledger-flag",
        help=(
            "Record the operator resolution of an unresolved "
            "ledger_reconciliation_required flag (#277) for one "
            "account+symbol, unblocking deployment-scoped entries."
        ),
    )
    resolve.add_argument("--account", required=True, help="Account id.")
    resolve.add_argument("--symbol", required=True, help="Symbol, e.g. AAPL.")
    resolve.add_argument(
        "--note",
        default="",
        help="Why the flag is resolved (kept in the audit row).",
    )
    resolve.add_argument(
        "--actor",
        default="operator",
        help="Attribution recorded on the audit row.",
    )
    return parser


def _scope_for_args(args: argparse.Namespace) -> str:
    if args.scope:
        return args.scope.strip()
    if args.account:
        return account_loss_breaker_scope(args.account)
    return deployment_loss_breaker_scope(args.deployment)


def _format_state(record: LiveLossBreakerRecord, *, equity: float | None) -> dict:
    # The "now" fractions measure the breaker's OWN equity series
    # (``last_equity`` - on cTrader that includes unrealized P&L), never the
    # balance snapshot's equity slot, which is realized balance by design.
    now_equity = (
        float(record.last_equity)
        if record.last_equity is not None
        else equity
    )
    daily_loss = None
    drawdown = None
    if now_equity is not None and record.day_start_equity > 0:
        daily_loss = (record.day_start_equity - now_equity) / record.day_start_equity
    if now_equity is not None and record.peak_equity > 0:
        drawdown = (record.peak_equity - now_equity) / record.peak_equity
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


def _reset_equity_for_scope(
    record: LiveLossBreakerRecord,
    args: argparse.Namespace,
    *,
    now: datetime,
) -> tuple[float | None, str | None, str | None]:
    """``(equity, source, error)`` for one scope's drawdown re-base.

    Every scope re-bases to its OWN last observed breaker equity - the series
    the breaker actually measures. On cTrader that includes unrealized P&L,
    which the balance snapshot's equity slot does NOT carry (it is realized
    balance by the #267 decision), so re-basing on the snapshot would set the
    peak ABOVE the observed equity and the next poll would re-latch. An
    account scope additionally refuses an observation older than
    ``STALE_SNAPSHOT_SECONDS``: re-basing on an hour-old equity could
    silently re-arm a breaker that should still be latched. ``--equity``
    always wins, but only for the account scope the operator named;
    deployment scopes re-base to their own last observed equity because it
    lives in deployment-P&L-adjusted units a raw account-equity figure
    cannot express.
    """
    kind = record.scope.split(":", 1)[0]
    if kind != "deployment" and args.equity is not None:
        equity = float(args.equity)
        if not math.isfinite(equity) or equity <= 0:
            return None, None, f"--equity must be a positive finite number, got {args.equity!r}"
        return equity, "--equity", None
    last = record.last_equity
    if last is None or not math.isfinite(float(last)) or float(last) <= 0:
        return (
            None,
            None,
            (
                f"scope {record.scope!r} has no usable last observed equity; "
                "its re-base equity lives in the breaker's own equity units - "
                "wait for the next poll observation or pass --equity to state "
                "it explicitly"
            ),
        )
    observed_at = record.last_observed_at
    if observed_at is None:
        return (
            None,
            None,
            (
                f"scope {record.scope!r} has no recorded observation time; "
                "wait for the next poll observation or pass --equity to "
                "state the re-base equity explicitly"
            ),
        )
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = (now - observed_at).total_seconds()
    if kind != "deployment" and age > STALE_SNAPSHOT_SECONDS:
        return (
            None,
            None,
            (
                f"last breaker observation for scope {record.scope!r} is "
                f"{age:.0f}s old (limit {STALE_SNAPSHOT_SECONDS:.0f}s); "
                "pass --equity to state the re-base equity explicitly"
            ),
        )
    return float(last), "last_observed_equity", None


def _still_latched(account_id: str) -> list[dict]:
    """Scopes on the account still refusing entries after the reset."""
    out = []
    for record in list_live_loss_breaker_states(account_id):
        latched = []
        if record.daily_breached:
            latched.append("daily")
        if record.drawdown_breached:
            latched.append("drawdown")
        if latched:
            out.append({"scope": record.scope, "latched": latched})
    return out


def _cmd_reset_drawdown(args: argparse.Namespace) -> int:
    from live_runner.loss_breaker import reset_drawdown_breaker

    scope = _scope_for_args(args)
    record = get_live_loss_breaker_state(scope)
    if record is None:
        print(
            f"error: no loss breaker state for scope {scope!r}; nothing to reset",
            file=sys.stderr,
        )
        return 1
    if args.equity is not None and (
        not math.isfinite(float(args.equity)) or float(args.equity) <= 0
    ):
        print(
            f"error: --equity must be a positive finite number, got {args.equity!r}",
            file=sys.stderr,
        )
        return 1
    now = datetime.now(timezone.utc)
    # An account scope reset cascades to the account's deployment scopes: with
    # the default thresholds they latch together, so clearing only the account
    # row would leave every entry refused by stale deployment latches.
    records = [record]
    kind = scope.split(":", 1)[0]
    if kind == "account":
        records = [
            r
            for r in list_live_loss_breaker_states(record.account_id)
            if r.scope == scope or r.scope.startswith("deployment:")
        ]
        if record not in records:
            records.insert(0, record)
    results = []
    skipped = []
    failed = False
    for target in records:
        equity, source, error = _reset_equity_for_scope(target, args, now=now)
        if error is not None:
            skipped.append({"scope": target.scope, "reason": error})
            if target.scope == scope:
                failed = True
            continue
        updated = reset_drawdown_breaker(target.scope, equity=equity, now=now)
        if updated is None:
            skipped.append(
                {"scope": target.scope, "reason": "state row disappeared mid-reset"}
            )
            if target.scope == scope:
                failed = True
            continue
        results.append(
            {
                "scope": target.scope,
                "rebase_equity": equity,
                "equity_source": source,
                "peak_equity": updated.peak_equity,
                "drawdown_breached": updated.drawdown_breached,
            }
        )
    if failed and not results:
        for entry in skipped:
            print(f"error: {entry['reason']}", file=sys.stderr)
        return 1
    payload = {
        "reset": "drawdown",
        "reset_at": now.isoformat(),
        "scopes": results,
        "still_latched": _still_latched(record.account_id),
    }
    if skipped:
        payload["skipped"] = skipped
    print(json.dumps(payload, indent=2, default=str))
    if failed or skipped:
        # The requested scope failed, or a cascade target could not be
        # re-based: entries may still be refused, so the command did not
        # fully succeed even though part of the reset landed. The JSON
        # payload above names what was skipped; exit non-zero for scripts.
        for entry in skipped:
            print(f"error: {entry['scope']}: {entry['reason']}", file=sys.stderr)
        return 1
    return 0


def _cmd_resolve_ledger_flag(args: argparse.Namespace) -> int:
    account = str(args.account or "").strip()
    symbol = str(args.symbol or "").strip().upper()
    if not account or not symbol:
        print(
            "error: --account and --symbol are required and must be non-empty",
            file=sys.stderr,
        )
        return 1
    target = f"{account}:{symbol}"
    before = list_unresolved_ledger_reconciliation_flags(account)
    audit = resolve_ledger_reconciliation_flag(
        account_id=account,
        symbol=symbol,
        actor=args.actor,
        note=args.note,
    )
    after = list_unresolved_ledger_reconciliation_flags(account)
    print(
        json.dumps(
            {
                "resolved_target": target,
                "was_unresolved": target in before,
                "unresolved_flags": after,
                "audit_id": audit.get("id"),
            },
            indent=2,
            default=str,
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
    if args.command == "resolve-ledger-flag":
        return _cmd_resolve_ledger_flag(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

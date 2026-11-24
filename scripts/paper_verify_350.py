"""Paper-verification driver for PR #350 (Alpaca partial-exit re-protection).

Drives the exact broker sequence the PR's "Paper verification pending" section
asks for, through the adapter's real public entry points - never a hand-rolled
REST call for the actions under test:

* entry: a ``LiveSignal`` submitted through ``AlpacaLiveAdapter.submit_order``
  (the same call the daemon makes after sizing; sizing itself is deliberately
  bypassed so ``--qty`` is exact - the PR under test is the exit path, not the
  risk engine), producing a market bracket order with SL/TP legs;
* partial exits: ``exit_only`` signals carrying ``signal_intent=exit`` +
  ``exit_fraction``, sized by the daemon's real ``prepare_order_with_risk``
  (which routes to ``prepare_exit_intent_order``) and submitted through
  ``adapter.submit_order``, which releases the bracket protection, submits the
  exit and re-places an OCO at the recorded levels (#350);
* trailing: ``adapter.list_protective_stops`` + ``adapter.amend_protective_stop``,
  the same amend path the trailing manager uses;
* crash recovery: ``--step 5`` hard-exits the process (``os._exit(3)``) inside
  ``_place_exit_protection``, i.e. after the exit order is submitted and before
  re-protection - the persisted ``exit_reprotection`` marker is the crash
  artifact. ``--step 5-recover`` builds a fresh adapter and lets ``connect()``
  replay it.

Safety gates (all fail closed):

* ``--db-dir`` is REQUIRED and must not be the checkout's ``data/``: it is
  exported as ``DATA_DIR``/``TRADING_DATA_DIR``/``LOG_DIR`` before any project
  module is imported, so every signal/ledger/audit row lands in a fresh SQLite
  DB there - never the real one;
* the adapter is built through the daemon's own account-loading path
  (``_create_adapter_for_account`` + ``resolve_alpaca_credentials``), and the
  run REFUSES unless ``adapter.paper`` is true, the trading client's base URL
  is the Alpaca paper endpoint, and the broker account number starts with
  ``PA`` (Alpaca paper accounts);
* orders are only sent with ``--execute``; the default is a dry run that
  connects read-only, resolves what it would do, and prints the same evidence.
  A dry run also refuses to ``connect()`` while any ``exit_reprotection``
  marker is pending, because ``connect()`` replays markers with broker writes.

Market hours: entries and exits are market (DAY) orders, so they only fill
while the US equity market is open. Every fill wait is bounded; a timeout is
reported INCONCLUSIVE, never retried into a stale state.

Usage (from the repo root, the venv that has alpaca-py):

    python -m scripts.paper_verify_350 --account-id <live_accounts id> \
        --db-dir /tmp/paper-verify-350 [--execute] [--step STEP]

See the module docstring of the PR body for the step -> expectation mapping.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.paper_verify_350
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("paper_verify_350")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2
EXIT_CRASH_SIMULATED = 3

_DEFAULT_SYMBOL = "SPY"
_DEFAULT_QTY = 4.0
_DEPLOYMENT_STRATEGY = "paper-verify-350"
_QTY_TOLERANCE = 1e-6


class PaperVerifyError(RuntimeError):
    """A safety gate or an unverifiable expectation failed; nothing is guessed."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}" + (
            f" - {self.detail}" if self.detail else ""
        )


@dataclass
class Ctx:
    """Everything a step needs; ``adapter``/``deployment`` are filled by setup."""

    args: argparse.Namespace
    account_id: str
    symbol: str
    adapter: Any = None
    deployment: Any = None
    deployment_id: str = ""

    @property
    def execute(self) -> bool:
        return bool(self.args.execute)

    def require_execute(self, what: str) -> None:
        if not self.execute:
            raise PaperVerifyError(
                f"{what} writes to the broker; re-run with --execute "
                "(the default is a dry run)."
            )


def _configure_data_dirs(db_dir: Path) -> None:
    """Point every project data path at ``db_dir`` BEFORE project imports.

    ``shared.constants`` resolves ``DATA_DIR``/``LOG_DIR`` at import time, so
    this must run before ``shared.database``/``live_runner`` are imported -
    which is why all project imports in this script are function-local.
    """
    resolved = db_dir.expanduser().resolve()
    repo_data = (PROJECT_ROOT / "data").resolve()
    if resolved == repo_data or repo_data in resolved.parents or resolved in repo_data.parents:
        raise PaperVerifyError(
            f"--db-dir {resolved} is the checkout's data directory "
            f"({repo_data}) or contains it; refusing - the run must use a "
            "throwaway directory so the real DB is never touched."
        )
    resolved.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(resolved)
    os.environ["TRADING_DATA_DIR"] = str(resolved)
    os.environ["LOG_DIR"] = str(resolved / "logs")


def _deployment_id_for(account_id: str, symbol: str) -> str:
    """Deterministic deployment id so every --step invocation shares one ledger.

    ``live_signals.deployment_id`` is a FK to ``live_deployments.id`` and the
    exit sizing ledger is keyed on it, so steps run as separate processes must
    land on the SAME deployment row inside the shared ``--db-dir`` database.
    """
    return str(
        uuid5(NAMESPACE_URL, f"trading-platform/paper-verify-350/{account_id}/{symbol}")
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Drive the PR #350 paper-verification steps against an Alpaca PAPER "
            "account. Dry-run by default; broker writes need --execute."
        )
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="live_accounts id of the Alpaca paper account (from .accounts.env hints).",
    )
    parser.add_argument(
        "--symbol",
        default=_DEFAULT_SYMBOL,
        help="US equity symbol to trade (default: SPY). Must be whole-share liquid.",
    )
    parser.add_argument(
        "--qty",
        type=float,
        default=_DEFAULT_QTY,
        help="Whole-share entry quantity (default: 4).",
    )
    parser.add_argument(
        "--db-dir",
        required=True,
        type=Path,
        help="Empty-ish temp dir used as DATA_DIR/LOG_DIR; a fresh SQLite DB is created there.",
    )
    parser.add_argument(
        "--step",
        default="1",
        choices=["1", "2", "3", "3-trail", "3-tp", "4", "5", "5-recover", "all"],
        help=(
            "Which PR-body step to run. '3' is the second partial exit, '3-trail' "
            "amends the stop and sends a third partial exit, '3-tp' waits for the "
            "take-profit fill, '5' simulates the crash (process exits 3), "
            "'5-recover' replays the marker on a fresh connect, 'all' runs "
            "1-4 + 3-trail + 3-tp (step 5 must be its own invocation)."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send paper orders. Without it every step is a dry run.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Cancel all open orders for the symbol and flatten its position (paper account only).",
    )
    parser.add_argument("--exit-fraction", type=float, default=0.25)
    parser.add_argument(
        "--sl-pct",
        type=float,
        default=0.02,
        help="Entry stop distance as a fraction of the ask (default 0.02).",
    )
    parser.add_argument(
        "--tp-pct",
        type=float,
        default=0.01,
        help=(
            "Entry take-profit distance as a fraction of the ask (default 0.01). "
            "Pick it small enough to fill during the session but large enough "
            "not to fill during steps 1-2."
        ),
    )
    parser.add_argument(
        "--trail-pct",
        type=float,
        default=0.003,
        help="Step 3-trail: amend the protective stop to this fraction below the bid.",
    )
    parser.add_argument("--fill-timeout", type=float, default=180.0)
    parser.add_argument("--tp-wait-seconds", type=float, default=900.0)
    parser.add_argument(
        "--broker-login",
        default="",
        help=(
            "Broker account number hint for credential matching "
            "(e.g. PA35HB6T7815). Only needed when the accounts-env blocks "
            "match on login_hint rather than account_id_hint."
        ),
    )
    return parser.parse_args(argv)


# ── setup: credentials, adapter, safety gates, temp DB rows ──────────────────


async def _build_adapter(ctx: Ctx) -> Any:
    """Build the adapter exactly the way the daemon does for this account.

    Reuses ``daemon._create_adapter_for_account`` - the daemon's own
    account-loading code path - so credentials come from the standard loader
    (``.accounts.env`` file-first, then process env). Secrets are never logged.
    """
    from live_runner.account_discovery import DEFAULT_ACCOUNTS_ENV
    from live_runner.credential_env import load_accounts_env_values
    from live_runner.daemon import _create_adapter_for_account
    from shared.database import ensure_live_account, get_live_account

    account = get_live_account(ctx.account_id)
    if account is None:
        # The temp DB starts empty: register the account row the daemon would
        # have discovered, so signals/deployments satisfy their FKs. The row
        # is bookkeeping only - the paper gates below read the BROKER, not it.
        account = ensure_live_account(
            ctx.account_id,
            "alpaca",
            name=f"{_DEPLOYMENT_STRATEGY} ({ctx.account_id})",
            broker_login=ctx.args.broker_login or None,
            is_demo=True,
        )
    if account.broker != "alpaca":
        raise PaperVerifyError(
            f"Account {ctx.account_id!r} is broker {account.broker!r}, not alpaca."
        )
    env_values = load_accounts_env_values(DEFAULT_ACCOUNTS_ENV)
    adapter = _create_adapter_for_account(
        account, accounts_env_values=env_values
    )
    if adapter is None:
        raise PaperVerifyError(
            f"No Alpaca credentials resolved for account {ctx.account_id!r}. "
            "Check --account-id against the accounts-env hints, or pass "
            "--broker-login with the account number."
        )
    ctx.adapter = adapter
    return adapter


def _check_paper_gates(adapter: Any, account: Any) -> list[str]:
    """Refuse unless every paper signal agrees. Returns human-readable evidence."""
    problems: list[str] = []
    evidence: list[str] = []
    paper = getattr(adapter, "paper", None)
    evidence.append(f"adapter.paper={paper!r}")
    if paper is not True:
        problems.append("adapter.paper is not True")
    client = getattr(adapter, "_trading_client", None)
    # alpaca-py stores the endpoint as a BaseURL enum; str() of it is the enum
    # name, so read .value first. A plain string (test doubles) passes through.
    base_url_raw = getattr(client, "_base_url", None)
    base_url = str(getattr(base_url_raw, "value", base_url_raw) or "")
    sandbox = getattr(client, "_sandbox", None)
    evidence.append(f"trading_client._base_url={base_url!r} _sandbox={sandbox!r}")
    if "paper-api" not in base_url:
        problems.append(f"trading client base URL is not the Alpaca paper endpoint: {base_url!r}")
    if sandbox is not True:
        problems.append(f"trading client _sandbox is {sandbox!r}, not True")
    login = str(
        getattr(account, "account_number", None)
        or getattr(account, "id", None)
        or ""
    ).strip()
    evidence.append(f"broker account_number={login!r}")
    if not login.upper().startswith("PA"):
        problems.append(
            f"broker account login {login!r} does not start with 'PA' "
            "(Alpaca paper account numbers do)"
        )
    if problems:
        raise PaperVerifyError(
            "REFUSING TO RUN - not provably a paper account:\n  "
            + "\n  ".join(problems)
            + "\n  evidence: "
            + "; ".join(evidence)
        )
    return evidence


async def _ensure_db_rows(ctx: Ctx) -> Any:
    """Create/refresh the account + deployment rows in the temp DB."""
    from shared.database import (
        LiveDeploymentModel,
        ensure_live_account,
        get_live_account,
        get_live_deployment,
        get_session,
        init_db,
    )

    init_db()
    account = get_live_account(ctx.account_id)
    if account is None:
        account = ensure_live_account(
            ctx.account_id,
            "alpaca",
            name=f"{_DEPLOYMENT_STRATEGY} ({ctx.account_id})",
            broker_login=ctx.args.broker_login or None,
            is_demo=True,
        )
    deployment_id = ctx.deployment_id or _deployment_id_for(ctx.account_id, ctx.symbol)
    ctx.deployment_id = deployment_id
    deployment = get_live_deployment(deployment_id)
    if deployment is None:
        with get_session() as session:
            existing = session.get(LiveDeploymentModel, deployment_id)
            if existing is None:
                session.add(
                    LiveDeploymentModel(
                        id=deployment_id,
                        strategy_name=_DEPLOYMENT_STRATEGY,
                        account_id=ctx.account_id,
                        symbols_json=[ctx.symbol.upper()],
                        timeframe="1m",
                        status="ACTIVE",
                        signal_source="internal",
                        signal_source_type="internal",
                    )
                )
                session.commit()
        deployment = get_live_deployment(deployment_id)
    if deployment is None:
        raise PaperVerifyError(f"Could not persist deployment {deployment_id}")
    ctx.deployment = deployment
    return deployment


async def _connect_and_gate(ctx: Ctx) -> list[str]:
    """connect() + paper gates. connect() also reconciles and replays markers."""
    adapter = ctx.adapter
    await adapter.connect()
    client = adapter._trading_client
    account = await asyncio.to_thread(client.get_account)
    return _check_paper_gates(adapter, account)


def _pending_markers_for(ctx: Ctx) -> list[dict[str, Any]]:
    from shared.database import list_pending_exit_reprotections

    return [
        row
        for row in list_pending_exit_reprotections(ctx.account_id)
        if str(row.get("symbol") or "").upper() == ctx.symbol.upper()
    ]


def _refuse_connect_with_pending_markers(ctx: Ctx) -> None:
    """Dry-run safety: connect() replays exit_reprotection markers with writes."""
    account_pending = _all_pending_markers(ctx)
    if account_pending:
        detail = "; ".join(
            f"{row.get('symbol')}:{row.get('signal_id')}" for row in account_pending[:5]
        )
        if ctx.execute:
            print(
                f"NOTE: {len(account_pending)} pending exit_reprotection marker(s) "
                f"on this account; connect() will replay them: {detail}",
                flush=True,
            )
        else:
            raise PaperVerifyError(
                "REFUSING dry-run connect: pending exit_reprotection marker(s) "
                f"would be replayed with broker writes ({detail}). Resolve them "
                "with --execute or a fresh --db-dir."
            )


def _all_pending_markers(ctx: Ctx) -> list[dict[str, Any]]:
    from shared.database import list_pending_exit_reprotections

    return list_pending_exit_reprotections(ctx.account_id)


# ── market data helpers ───────────────────────────────────────────────────────


async def _quote(ctx: Ctx) -> Any:
    return await ctx.adapter.get_quote(ctx.symbol)


async def _position_qty(ctx: Ctx) -> float:
    """Signed broker quantity for the symbol (0.0 when flat)."""
    total = 0.0
    for position in await ctx.adapter.get_positions():
        if str(position.get("symbol") or "").strip().upper() == ctx.symbol.upper():
            total += float(position.get("qty") or 0.0)
    return total


async def _wait_for(ctx: Ctx, predicate: Any, *, timeout: float, what: str) -> bool:
    """Bounded poll; returns False on timeout (caller reports INCONCLUSIVE)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(1.0)
    print(f"TIMEOUT waiting for: {what} (>{timeout:.0f}s)", flush=True)
    return False


async def _reconcile(ctx: Ctx) -> int:
    """Book broker fills into the temp DB, like the daemon's connect pass."""
    return await ctx.adapter.reconcile_executions()


def _ledger_qty(ctx: Ctx) -> dict[str, float]:
    from shared.database import get_account_net_filled_qty_by_deployment

    return get_account_net_filled_qty_by_deployment(ctx.account_id, ctx.symbol)


def _deployment_ledger_qty(ctx: Ctx) -> float:
    from shared.database import get_live_deployment_net_filled_qty

    return get_live_deployment_net_filled_qty(ctx.deployment_id, ctx.symbol)


# ── evidence ──────────────────────────────────────────────────────────────────


def _order_field(order: Any, name: str, default: Any = None) -> Any:
    from live_runner import alpaca_adapter as alpaca

    return alpaca._order_attr(order, name, default)


def _fmt_price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return repr(value)
    return f"{number:.4f}" if number else repr(value)


def _describe_order(order: Any, *, indent: str = "") -> str:
    from live_runner import alpaca_adapter as alpaca

    legs = alpaca._order_legs(order)
    parts = [
        f"{indent}order id={_order_field(order, 'id')}",
        f"client_order_id={_order_field(order, 'client_order_id')!r}",
        f"class={alpaca._order_class_text(order)!r}",
        f"type={alpaca._order_type(order)!r}",
        f"side={alpaca._order_side(order)!r}",
        f"qty={_order_field(order, 'qty')}",
        f"limit_price={_fmt_price(_order_field(order, 'limit_price'))}",
        f"stop_price={_fmt_price(_order_field(order, 'stop_price'))}",
        f"status={alpaca._order_status(order)!r}",
        f"filled_qty={_order_field(order, 'filled_qty')}",
    ]
    lines = [" ".join(parts)]
    for leg in legs:
        lines.append(_describe_order(leg, indent=indent + "  "))
    return "\n".join(lines)


async def _list_symbol_orders(ctx: Ctx, *, status: str = "all") -> list[Any]:
    """GET /v2/orders equivalent for the symbol (nested legs included)."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    client = ctx.adapter._trading_client
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL if status == "all" else QueryOrderStatus.OPEN,
        symbols=[ctx.symbol.upper()],
        nested=True,
        limit=500,
    )
    return list(await asyncio.to_thread(client.get_orders, filter=request))


async def _print_evidence(ctx: Ctx, *, title: str) -> None:
    """The evidence the PR body asks for after each step."""
    print(f"\n=== evidence: {title} ===", flush=True)
    try:
        orders = await _list_symbol_orders(ctx, status="all")
    except Exception as exc:
        orders = []
        print(f"  orders listing failed: {exc!r}", flush=True)
    for order in orders:
        print(_describe_order(order), flush=True)
    try:
        positions = await ctx.adapter.get_positions()
    except Exception as exc:
        positions = []
        print(f"  positions listing failed: {exc!r}", flush=True)
    for position in positions:
        if str(position.get("symbol") or "").upper() == ctx.symbol.upper():
            print(
                f"  position {position.get('symbol')}: qty={position.get('qty')} "
                f"entry={position.get('entry_price')} "
                f"unrealized_pnl={position.get('unrealized_pnl')}",
                flush=True,
            )
    markers = _pending_markers_for(ctx)
    if markers:
        for row in markers:
            print(
                f"  exit_reprotection marker: signal={row.get('signal_id')} "
                f"marker={json.dumps(row.get('marker'), default=str)}",
                flush=True,
            )
    else:
        print("  exit_reprotection marker: none pending", flush=True)
    try:
        ledgers = _ledger_qty(ctx)
        deployment_qty = _deployment_ledger_qty(ctx)
        print(
            f"  ledger net filled qty by deployment: {ledgers}; "
            f"this deployment: {deployment_qty:.6f}",
            flush=True,
        )
    except Exception as exc:
        print(f"  ledger read failed: {exc!r}", flush=True)
    try:
        from shared.database import LEDGER_RECONCILIATION_AUDIT_ACTION, list_live_control_audit

        flags = [
            row
            for row in list_live_control_audit(
                limit=200, target_id=f"{ctx.account_id}:{ctx.symbol.upper()}"
            )
            if row.get("action") == LEDGER_RECONCILIATION_AUDIT_ACTION
        ]
        print(f"  ledger_reconciliation_required audit rows: {len(flags)}", flush=True)
        for row in flags[:5]:
            print(f"    {row.get('created_at')} {row.get('detail')}", flush=True)
    except Exception as exc:
        print(f"  audit read failed: {exc!r}", flush=True)
    print("=== end evidence ===\n", flush=True)


# ── order construction ────────────────────────────────────────────────────────


def _entry_levels(ctx: Ctx, ask: float) -> tuple[float, float, float]:
    entry = float(ask)
    stop = entry * (1.0 - float(ctx.args.sl_pct))
    take = entry * (1.0 + float(ctx.args.tp_pct))
    return entry, stop, take


def _build_entry_signal(ctx: Ctx, *, entry: float, stop: float, take: float) -> Any:
    from shared.models import LiveSignal

    return LiveSignal(
        deployment_id=ctx.deployment_id,
        symbol=ctx.symbol.upper(),
        action="buy",
        theoretical_price=float(entry),
        timestamp=datetime.now(timezone.utc),
        metadata={
            "entry_price": float(entry),
            "stop_loss": float(stop),
            "take_profit": float(take),
            "qty": float(ctx.args.qty),
            "explicit_qty": float(ctx.args.qty),
            "timeframe": "1m",
            "source": _DEPLOYMENT_STRATEGY,
            # Deterministic key: re-running step 1 dedupes to the canonical
            # row instead of opening a second position.
            "idempotency_key": (
                f"paper-verify-350:{ctx.account_id}:{ctx.symbol.upper()}:entry"
            ),
        },
    )


def _build_exit_signal(ctx: Ctx, *, fraction: float) -> Any:
    from live_runner.exit_signals import ExitInstruction, exit_intent_metadata
    from shared.models import LiveSignal

    metadata = exit_intent_metadata(
        ExitInstruction(fraction=float(fraction), position_side="long")
    )
    metadata["timeframe"] = "1m"
    metadata["source"] = _DEPLOYMENT_STRATEGY
    return LiveSignal(
        deployment_id=ctx.deployment_id,
        symbol=ctx.symbol.upper(),
        action="sell",
        theoretical_price=0.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


async def _submit_entry(ctx: Ctx) -> str | None:
    """Step 1: bracket entry through adapter.submit_order (daemon's entry point)."""
    quote = await _quote(ctx)
    ask = float(quote.ask)
    entry, stop, take = _entry_levels(ctx, ask)
    signal = _build_entry_signal(ctx, entry=entry, stop=stop, take=take)
    print(
        f"entry plan: BUY {ctx.args.qty:g} {ctx.symbol} @ market "
        f"(ask={ask:.4f}) SL={stop:.4f} TP={take:.4f}",
        flush=True,
    )
    if not ctx.execute:
        print("DRY RUN: entry not submitted (re-run with --execute).", flush=True)
        return None
    broker_order_id = await ctx.adapter.submit_order(signal, qty=float(ctx.args.qty))
    print(f"entry submitted: broker_order_id={broker_order_id}", flush=True)
    qty = float(ctx.args.qty)
    filled = await _wait_for(
        ctx,
        lambda: _position_reached(ctx, qty),
        timeout=float(ctx.args.fill_timeout),
        what=f"position qty >= {qty:g} for {ctx.symbol}",
    )
    if not filled:
        return broker_order_id
    await _reconcile(ctx)
    return broker_order_id


async def _position_reached(ctx: Ctx, qty: float) -> bool:
    return abs(await _position_qty(ctx)) >= qty - _QTY_TOLERANCE


async def _submit_partial_exit(ctx: Ctx, *, fraction: float, crash: bool = False) -> Any:
    """Steps 2/3/5: exit_only signal -> daemon sizing -> adapter.submit_order."""
    from live_runner.daemon import prepare_order_with_risk

    before = await _position_qty(ctx)
    signal = _build_exit_signal(ctx, fraction=fraction)
    prepared, prepared_qty = await prepare_order_with_risk(
        ctx.adapter, ctx.deployment, signal
    )
    print(
        f"exit plan: fraction={fraction} broker_qty={before:.6f} "
        f"sized_qty={prepared_qty:.6f} signal_id={prepared.id}",
        flush=True,
    )
    if not ctx.execute:
        print("DRY RUN: exit not submitted (re-run with --execute).", flush=True)
        return None
    if crash:
        _install_crash_hook(ctx.adapter)
    broker_order_id = await ctx.adapter.submit_order(prepared, qty=prepared_qty)
    print(f"exit submitted: broker_order_id={broker_order_id}", flush=True)
    expected = abs(before) - float(prepared_qty)
    if expected >= -_QTY_TOLERANCE:
        await _wait_for(
            ctx,
            lambda: _position_near(ctx, expected),
            timeout=float(ctx.args.fill_timeout),
            what=f"position qty ~= {expected:.6f} for {ctx.symbol}",
        )
    await _reconcile(ctx)
    return broker_order_id


async def _position_near(ctx: Ctx, qty: float) -> bool:
    return abs(abs(await _position_qty(ctx)) - abs(qty)) <= max(
        _QTY_TOLERANCE, 1e-4
    )


def _install_crash_hook(adapter: Any) -> None:
    """Simulate the crash: die inside _place_exit_protection, post-submit.

    The exit order is already at the broker; the re-protect never runs and the
    process exits exactly like a kill - the persisted ``exit_reprotection``
    marker is the only recovery artifact.
    """

    async def _crash(*_args: Any, **_kwargs: Any) -> bool:
        print(
            "CRASH SIMULATION: exiting inside _place_exit_protection, "
            "after the exit submit and before re-protection.",
            flush=True,
        )
        os._exit(EXIT_CRASH_SIMULATED)
        return False  # pragma: no cover - unreachable

    adapter._place_exit_protection = _crash  # type: ignore[assignment]


# ── steps ─────────────────────────────────────────────────────────────────────


async def _step_entry(ctx: Ctx) -> int:
    await _submit_entry(ctx)
    await _print_evidence(ctx, title="step 1: bracket entry")
    checks: list[Check] = []
    if not ctx.execute:
        checks.append(Check("entry submitted (dry run)", True, "skipped - dry run"))
        return _verdict(ctx, checks)
    position = await _position_qty(ctx)
    checks.append(
        Check(
            "position opened at --qty",
            abs(abs(position) - float(ctx.args.qty)) <= max(
                _QTY_TOLERANCE, 1e-4
            ),
            f"position qty={position:.6f}",
        )
    )
    bracket = await _bracket_parent(ctx)
    if bracket is None:
        checks.append(Check("bracket parent order exists", False, "none found"))
    else:
        from live_runner import alpaca_adapter as alpaca

        legs = alpaca._order_legs(bracket)
        leg_types = sorted(alpaca._order_type(leg) for leg in legs)
        checks.append(
            Check(
                "entry is a bracket: filled parent + stop leg + take-profit leg",
                alpaca._order_class_text(bracket) == "bracket"
                and alpaca._order_status(bracket) == "filled"
                and leg_types == ["limit", "stop"],
                f"class={alpaca._order_class_text(bracket)} "
                f"status={alpaca._order_status(bracket)} leg types={leg_types}",
            )
        )
    return _verdict(ctx, checks)


async def _bracket_parent(ctx: Ctx) -> Any | None:
    """The most recent bracket-class order for the symbol."""
    from live_runner import alpaca_adapter as alpaca

    brackets = [
        order
        for order in await _list_symbol_orders(ctx, status="all")
        if alpaca._order_class_text(order) == "bracket"
    ]
    if not brackets:
        return None
    return max(
        brackets,
        key=lambda order: str(_order_field(order, "created_at") or ""),
    )


async def _oco_for_symbol(ctx: Ctx) -> list[Any]:
    """Working OCO orders (the re-placed protection) for the symbol."""
    from live_runner import alpaca_adapter as alpaca

    ocos: list[Any] = []
    for order in await _list_symbol_orders(ctx, status="all"):
        if alpaca._order_class_text(order) != "oco":
            continue
        if alpaca._order_status(order) in alpaca._TERMINAL_ORDER_STATUSES:
            continue
        ocos.append(order)
    return ocos


async def _step_partial_exit(ctx: Ctx, *, label: str) -> int:
    fraction = float(ctx.args.exit_fraction)
    before = await _position_qty(ctx)
    await _submit_partial_exit(ctx, fraction=fraction)
    await _print_evidence(ctx, title=f"step {label}: partial exit")
    checks: list[Check] = []
    if not ctx.execute:
        checks.append(Check("partial exit submitted (dry run)", True, "skipped"))
        return _verdict(ctx, checks)
    position = await _position_qty(ctx)
    ocos = await _oco_for_symbol(ctx)
    checks.append(
        Check(
            "exit accepted (no 'insufficient qty')",
            True,
            "submit_order returned; see evidence",
        )
    )
    checks.append(
        Check(
            "position decreased by the exit",
            abs(position) < abs(before) - _QTY_TOLERANCE,
            f"position qty {before:.6f} -> {position:.6f}",
        )
    )
    checks.append(
        Check(
            "exactly one OCO covers the remainder",
            len(ocos) == 1,
            f"{len(ocos)} oco order(s) found",
        )
    )
    if ocos:
        from live_runner import alpaca_adapter as alpaca

        parent = ocos[0]
        legs = alpaca._order_legs(parent)
        parent_tp = _order_field(parent, "limit_price")
        leg_stops = [
            _order_field(leg, "stop_price") for leg in legs
        ]
        checks.append(
            Check(
                "OCO parent is the take-profit (order_type=limit, limit_price set)",
                alpaca._order_type(parent) == "limit" and parent_tp is not None,
                f"parent type={alpaca._order_type(parent)} limit={_fmt_price(parent_tp)} "
                f"client_order_id={_order_field(parent, 'client_order_id')!r}",
            )
        )
        checks.append(
            Check(
                "OCO stop leg carries a stop_price",
                any(s is not None for s in leg_stops),
                "; ".join(
                    f"leg client_order_id={_order_field(leg, 'client_order_id')!r} "
                    f"stop={_fmt_price(_order_field(leg, 'stop_price'))} "
                    f"status={alpaca._order_status(leg)!r}"
                    for leg in legs
                ),
            )
        )
    # The marker clears once the exit order is proven fully filled; give the
    # broker a moment before calling it a failure.
    async def _markers_cleared() -> bool:
        return len(_pending_markers_for(ctx)) == 0

    await _wait_for(ctx, _markers_cleared, timeout=30.0, what="exit_reprotection marker cleared")
    checks.append(
        Check(
            "exit_reprotection marker cleared after proven full fill",
            len(_pending_markers_for(ctx)) == 0,
            f"{len(_pending_markers_for(ctx))} pending",
        )
    )
    return _verdict(ctx, checks)


async def _step_trail(ctx: Ctx) -> int:
    """Step 3 (trailing part): amend the stop, then another partial exit."""
    checks: list[Check] = []
    stops = await ctx.adapter.list_protective_stops()
    symbol_stops = [s for s in stops if s.symbol.upper() == ctx.symbol.upper()]
    print(f"protective stops for {ctx.symbol}: {len(symbol_stops)}", flush=True)
    for stop in symbol_stops:
        print(
            f"  order_id={stop.order_id} side={stop.side} "
            f"stop_price={stop.stop_price} qty={stop.qty}",
            flush=True,
        )
    if not symbol_stops:
        checks.append(Check("a working protective stop exists to amend", False, "none found"))
        return _verdict(ctx, checks)
    stop = symbol_stops[0]
    quote = await _quote(ctx)
    bid = float(quote.bid)
    new_price = bid * (1.0 - float(ctx.args.trail_pct))
    rounded = ctx.adapter.round_stop_price(stop, new_price)
    print(
        f"trail plan: amend stop {stop.order_id} {stop.stop_price} -> {rounded} "
        f"(bid={bid:.4f}, trail_pct={ctx.args.trail_pct})",
        flush=True,
    )
    if not ctx.execute:
        print("DRY RUN: stop amendment and exit not submitted.", flush=True)
        return _verdict(ctx, [Check("amend+exit (dry run)", True, "skipped")])
    amended = await ctx.adapter.amend_protective_stop(stop, new_price)
    checks.append(
        Check(
            "protective stop amended",
            abs(amended.stop_price - rounded) <= 1e-9,
            f"new stop_price={amended.stop_price} order_id={amended.order_id}",
        )
    )
    # Third partial exit: the re-placed OCO must sit at the CURRENT (trailed)
    # level, per the maintainer decision.
    await _submit_partial_exit(ctx, fraction=float(ctx.args.exit_fraction))
    await _print_evidence(ctx, title="step 3-trail: amended stop + partial exit")
    ocos = await _oco_for_symbol(ctx)
    checks.append(
        Check("exactly one OCO covers the remainder", len(ocos) == 1, f"{len(ocos)} found")
    )
    if ocos:
        from live_runner import alpaca_adapter as alpaca

        leg_stops = [
            _order_field(leg, "stop_price")
            for leg in alpaca._order_legs(ocos[0])
        ]
        at_trail = any(
            s is not None and abs(float(s) - float(amended.stop_price)) <= 0.011
            for s in leg_stops
        )
        checks.append(
            Check(
                "re-placed OCO stop leg sits at the trailed level",
                at_trail,
                f"trailed={amended.stop_price} leg stops={leg_stops}",
            )
        )
    return _verdict(ctx, checks)


async def _step_tp_fill(ctx: Ctx) -> int:
    """Step 3 (TP part): wait for the remainder's take-profit to fill."""
    checks: list[Check] = []
    ocos = await _oco_for_symbol(ctx)
    if not ocos:
        checks.append(Check("a working OCO exists to fill", False, "none found"))
        return _verdict(ctx, checks)
    from live_runner import alpaca_adapter as alpaca

    parent = ocos[0]
    tp_price = _order_field(parent, "limit_price")
    print(
        f"waiting up to {ctx.args.tp_wait_seconds:.0f}s for OCO take-profit "
        f"(parent limit_price={_fmt_price(tp_price)}) to fill",
        flush=True,
    )

    async def _tp_filled() -> bool:
        for order in await _list_symbol_orders(ctx, status="all"):
            if (
                _order_field(order, "id") == _order_field(parent, "id")
                and alpaca._order_status(order) == "filled"
            ):
                return True
        return False

    filled = await _wait_for(
        ctx, _tp_filled, timeout=float(ctx.args.tp_wait_seconds), what="OCO take-profit fill"
    )
    if filled:
        await _reconcile(ctx)
        # Give the -reprotect reconcile a second pass: the TP parent fill is
        # booked against the ENTRY ledger exactly once (#277 wiring).
        await _reconcile(ctx)
    await _print_evidence(ctx, title="step 3-tp: take-profit fill")
    position = await _position_qty(ctx)
    deployment_qty = _deployment_ledger_qty(ctx)
    checks.extend(
        [
            Check(
                "remainder hit its take-profit",
                filled,
                "OCO parent status=filled" if filled else "timeout - INCONCLUSIVE",
            ),
            Check(
                "position flat after TP fill",
                abs(position) <= max(_QTY_TOLERANCE, 1e-4),
                f"position qty={position:.6f}",
            ),
            Check(
                "entry ledger fully closed by the TP fill (booked once)",
                abs(deployment_qty) <= 1e-6,
                f"deployment ledger qty={deployment_qty:.6f}",
            ),
        ]
    )
    rc = _verdict(ctx, checks)
    # A TP that never filled is a market outcome, not a code failure: the
    # position simply did not reach the limit inside --tp-wait-seconds.
    return EXIT_INCONCLUSIVE if not filled else rc


async def _step_full_exit(ctx: Ctx) -> int:
    await _submit_partial_exit(ctx, fraction=1.0)
    await _print_evidence(ctx, title="step 4: full exit")
    position = await _position_qty(ctx)
    working = await _working_symbol_orders(ctx)
    checks = [
        Check(
            "position flat after full exit",
            abs(position) <= max(_QTY_TOLERANCE, 1e-4),
            f"position qty={position:.6f}",
        ),
        Check(
            "no leftover working legs for the symbol",
            len(working) == 0,
            f"{len(working)} working order(s)",
        ),
    ]
    return _verdict(ctx, checks)


async def _working_symbol_orders(ctx: Ctx) -> list[Any]:
    from live_runner import alpaca_adapter as alpaca

    working: list[Any] = []
    for order in await _list_symbol_orders(ctx, status="all"):
        if alpaca._order_status(order) in alpaca._TERMINAL_ORDER_STATUSES:
            continue
        working.append(order)
        for leg in alpaca._order_legs(order):
            if alpaca._order_status(leg) not in alpaca._TERMINAL_ORDER_STATUSES:
                working.append(leg)
    return working


async def _step_crash(ctx: Ctx) -> int:
    """Step 5: ensure a protected position, then die mid-exit (exit code 3)."""
    position = await _position_qty(ctx)
    if abs(position) < _QTY_TOLERANCE:
        print("no open position; running the step-1 entry first", flush=True)
        await _submit_entry(ctx)
        await _reconcile(ctx)
    else:
        await _reconcile(ctx)
    print(
        "step 5: partial exit with crash hook - the process will exit(3) "
        "inside _place_exit_protection. Run '--step 5-recover' next.",
        flush=True,
    )
    if not ctx.execute:
        print("DRY RUN: crash simulation not run (re-run with --execute).", flush=True)
        return EXIT_INCONCLUSIVE
    await _submit_partial_exit(ctx, fraction=float(ctx.args.exit_fraction), crash=True)
    # Reached only when no protection plan existed (nothing to release, so no
    # marker and nothing to recover) - the crash artifact step 5 needs was
    # never created. That is a FAIL, not a pass.
    print(
        "CRASH HOOK NOT REACHED: the exit had no protection plan, so no "
        "exit_reprotection marker was persisted. Step 5 needs a protected "
        "position (run step 1 first).",
        flush=True,
    )
    return EXIT_FAIL


async def _step_recover(ctx: Ctx) -> int:
    """Step 5-recover: fresh adapter, connect() replays the persisted marker."""
    pending_before = _pending_markers_for(ctx)
    print(
        f"pending exit_reprotection markers for {ctx.symbol}: {len(pending_before)}",
        flush=True,
    )
    for row in pending_before:
        print(
            f"  signal={row.get('signal_id')} marker={json.dumps(row.get('marker'), default=str)}",
            flush=True,
        )
    if not ctx.execute:
        print(
            "DRY RUN: recovery writes broker orders; re-run with --execute "
            "to let connect() replay the marker.",
            flush=True,
        )
        return EXIT_INCONCLUSIVE
    # A FRESH adapter: the point is that recovery does not depend on the
    # crashed process's in-memory state.
    from live_runner.account_discovery import DEFAULT_ACCOUNTS_ENV
    from live_runner.credential_env import load_accounts_env_values
    from live_runner.daemon import _create_adapter_for_account
    from shared.database import get_live_account

    account = get_live_account(ctx.account_id)
    env_values = load_accounts_env_values(DEFAULT_ACCOUNTS_ENV)
    adapter = _create_adapter_for_account(account, accounts_env_values=env_values)
    if adapter is None:
        raise PaperVerifyError("Could not rebuild the adapter for recovery.")
    ctx.adapter = adapter
    await adapter.connect()  # reconcile + _recover_exit_reprotections run here
    await _print_evidence(ctx, title="step 5-recover: first connect")
    checks: list[Check] = []
    working = await _working_symbol_orders(ctx)
    checks.append(
        Check(
            "connect() restored protection (or logged CRITICAL - check logs)",
            len(working) > 0,
            f"{len(working)} working protective order(s) for {ctx.symbol}",
        )
    )
    # Second connect: the marker must be spent once protection is verified.
    await adapter.connect()
    pending_after = _pending_markers_for(ctx)
    checks.append(
        Check(
            "second connect clears the marker once protection is verified",
            len(pending_after) == 0,
            f"{len(pending_after)} still pending",
        )
    )
    await _print_evidence(ctx, title="step 5-recover: second connect")
    return _verdict(ctx, checks)


async def _cleanup(ctx: Ctx) -> int:
    """Cancel every working order for the symbol, then flatten its position."""
    orders = await _list_symbol_orders(ctx, status="all")
    from live_runner import alpaca_adapter as alpaca

    cancel_ids: list[str] = []
    for order in orders:
        if alpaca._order_status(order) in alpaca._TERMINAL_ORDER_STATUSES:
            continue
        for item in (order, *alpaca._order_legs(order)):
            oid = _order_field(item, "id")
            if oid is not None:
                cancel_ids.append(str(oid))
    position = await _position_qty(ctx)
    print(
        f"cleanup plan: cancel {len(set(cancel_ids))} working order id(s), "
        f"position qty={position:.6f}",
        flush=True,
    )
    if not ctx.execute:
        print("DRY RUN: cleanup not executed (re-run with --execute).", flush=True)
        return EXIT_INCONCLUSIVE
    client = ctx.adapter._trading_client
    for oid in sorted(set(cancel_ids)):
        try:
            await asyncio.to_thread(client.cancel_order_by_id, oid)
            print(f"  cancelled {oid}", flush=True)
        except Exception as exc:
            print(f"  cancel {oid} failed (may already be gone): {exc!r}", flush=True)
    await _wait_for(
        ctx,
        lambda: _no_working_orders(ctx),
        timeout=60.0,
        what="all symbol orders terminal",
    )
    if abs(await _position_qty(ctx)) > _QTY_TOLERANCE:
        await asyncio.to_thread(client.close_position, ctx.symbol.upper())
        print("  close_position submitted", flush=True)
    await _wait_for(
        ctx,
        lambda: _flat(ctx),
        timeout=float(ctx.args.fill_timeout),
        what="position flat",
    )
    await _print_evidence(ctx, title="cleanup")
    checks = [
        Check(
            "symbol flat with no working orders",
            abs(await _position_qty(ctx)) <= max(_QTY_TOLERANCE, 1e-4)
            and len(await _working_symbol_orders(ctx)) == 0,
        )
    ]
    return _verdict(ctx, checks)


async def _no_working_orders(ctx: Ctx) -> bool:
    return len(await _working_symbol_orders(ctx)) == 0


async def _flat(ctx: Ctx) -> bool:
    return abs(await _position_qty(ctx)) <= max(_QTY_TOLERANCE, 1e-4)


def _verdict(ctx: Ctx, checks: list[Check]) -> int:
    print("\n=== step verdict ===", flush=True)
    for check in checks:
        print(check.line(), flush=True)
    failed = [c for c in checks if not c.ok]
    print(
        f"RESULT: {'FAIL' if failed else 'PASS'} "
        f"({len(checks) - len(failed)}/{len(checks)} checks passed)",
        flush=True,
    )
    return EXIT_FAIL if failed else EXIT_OK


_STEP_TABLE = {
    "1": _step_entry,
    "2": lambda ctx: _step_partial_exit(ctx, label="2"),
    "3": lambda ctx: _step_partial_exit(ctx, label="3"),
    "3-trail": _step_trail,
    "3-tp": _step_tp_fill,
    "4": _step_full_exit,
    "5": _step_crash,
    "5-recover": _step_recover,
}

_ALL_STEPS = ["1", "2", "3", "3-trail", "3-tp", "4"]


async def _run(ctx: Ctx) -> int:
    await _build_adapter(ctx)
    await _ensure_db_rows(ctx)
    if ctx.args.cleanup:
        _refuse_connect_with_pending_markers(ctx)
        evidence = await _connect_and_gate(ctx)
        print("paper gates: " + "; ".join(evidence), flush=True)
        return await _cleanup(ctx)
    if ctx.args.step == "5-recover":
        # Recovery builds its own fresh adapter inside the step and owns the
        # pending-marker handling itself (a pending marker is the EXPECTED
        # input here, not a reason to refuse).
        return await _step_recover(ctx)
    _refuse_connect_with_pending_markers(ctx)
    evidence = await _connect_and_gate(ctx)
    print("paper gates: " + "; ".join(evidence), flush=True)
    if ctx.args.step == "all":
        rc = EXIT_OK
        for step in _ALL_STEPS:
            print(f"\n########## running step {step} ##########", flush=True)
            step_rc = await _STEP_TABLE[step](ctx)
            if step_rc != EXIT_OK:
                rc = step_rc
                if step_rc == EXIT_INCONCLUSIVE:
                    print(
                        f"step {step} INCONCLUSIVE - continuing; fix the cause "
                        "before the next step.",
                        flush=True,
                    )
        print(
            "\n'all' covers PR steps 1-4 (+3-trail, 3-tp). Step 5 must be run "
            "separately because it kills the process on purpose:\n"
            "  python -m scripts.paper_verify_350 --account-id <id> "
            "--db-dir <dir> --execute --step 5\n"
            "  python -m scripts.paper_verify_350 --account-id <id> "
            "--db-dir <dir> --execute --step 5-recover\n"
            "  python -m scripts.paper_verify_350 --account-id <id> "
            "--db-dir <dir> --execute --cleanup",
            flush=True,
        )
        return rc
    step = _STEP_TABLE[ctx.args.step]
    return await step(ctx)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    symbol = str(args.symbol or "").strip().upper()
    if not symbol:
        print("--symbol must not be empty", file=sys.stderr)
        return 2
    if float(args.qty) < 1:
        print("--qty must be >= 1 whole share", file=sys.stderr)
        return 2
    try:
        _configure_data_dirs(args.db_dir)
    except PaperVerifyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ctx = Ctx(args=args, account_id=str(args.account_id).strip(), symbol=symbol)
    try:
        return asyncio.run(_run(ctx))
    except PaperVerifyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:
        print(f"ERROR: step failed: {exc!r}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return EXIT_INCONCLUSIVE


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit tests for scripts/paper_verify_350.py - the PR #350 paper driver.

These cover the safety gates that must hold without a broker: the DATA_DIR
redirection, the paper-account refusal, the dry-run guarantee (nothing is
submitted without --execute), and the exit-sizing dry run through the daemon's
real ``prepare_order_with_risk`` path. No test here touches a broker.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from live_runner.base_adapter import LiveQuote
from scripts import paper_verify_350 as pv
from shared.models import LiveExecution, LiveSignal


@pytest.fixture
def restored_env(monkeypatch):
    """Snapshot DATA_DIR-family env vars; _configure_data_dirs mutates os.environ."""
    keys = ("DATA_DIR", "TRADING_DATA_DIR", "LOG_DIR")
    snapshot = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _args(tmp_path: Path, **overrides: Any):
    argv = [
        "--account-id",
        "fixture-account",
        "--db-dir",
        str(tmp_path / "pv-db"),
        "--symbol",
        "SPY",
        "--qty",
        "4",
    ]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    return pv._parse_args(argv)


def _ctx(tmp_path: Path, *, execute: bool = False, adapter: Any = None, **overrides: Any):
    args = _args(tmp_path, execute=execute, **overrides)
    ctx = pv.Ctx(args=args, account_id="fixture-account", symbol="SPY")
    ctx.deployment_id = pv._deployment_id_for("fixture-account", "SPY")
    if adapter is not None:
        ctx.adapter = adapter
    return ctx


class _FakeTradingClient:
    """Minimal stand-in for alpaca.trading.client.TradingClient (read-only)."""

    def __init__(self) -> None:
        self._base_url = "https://paper-api.alpaca.markets"
        self._sandbox = True
        self.submit_calls: list[Any] = []

    def get_orders(self, filter: Any = None) -> list[Any]:  # noqa: A002 - mirrors alpaca-py
        return []

    def get_account(self) -> Any:
        return SimpleNamespace(account_number="PA35HB6T7815")


class _FakeAdapter:
    """Duck-typed adapter for dry-run paths; records any submit attempt."""

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self.paper = True
        self._trading_client = _FakeTradingClient()
        self._positions = positions if positions is not None else []
        self.submitted: list[tuple[Any, float | None]] = []

    async def get_quote(self, symbol: str) -> LiveQuote:
        return LiveQuote(bid=100.0, ask=100.01, timestamp=datetime.now(timezone.utc))

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    async def submit_order(self, signal: Any, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        return "fake-order-id"

    async def reconcile_executions(self, **_kwargs: Any) -> int:
        return 0


# ─── DATA_DIR safety ──────────────────────────────────────────────────────────


def test_configure_data_dirs_exports_env(tmp_path: Path, restored_env) -> None:
    db_dir = tmp_path / "pv-db"
    pv._configure_data_dirs(db_dir)
    assert Path(os.environ["DATA_DIR"]).resolve() == db_dir.resolve()
    assert Path(os.environ["TRADING_DATA_DIR"]).resolve() == db_dir.resolve()
    assert Path(os.environ["LOG_DIR"]).resolve() == (db_dir / "logs").resolve()
    assert db_dir.is_dir()


def test_configure_data_dirs_refuses_repo_data(tmp_path: Path, restored_env) -> None:
    with pytest.raises(pv.PaperVerifyError, match="data directory"):
        pv._configure_data_dirs(pv.PROJECT_ROOT / "data")
    # A nested path that would swallow the checkout's data dir is refused too.
    with pytest.raises(pv.PaperVerifyError):
        pv._configure_data_dirs(pv.PROJECT_ROOT)


def test_configure_data_dirs_must_run_before_project_imports() -> None:
    """The module must not import shared.database at top level: DATA_DIR is
    resolved at shared.constants import time, so a top-level import would pin
    the checkout's real data dir before --db-dir can redirect it."""
    import scripts.paper_verify_350 as module

    assert "shared.database" not in vars(module)
    assert not hasattr(module, "database")


# ─── Paper gates ──────────────────────────────────────────────────────────────


def _gate_adapter(*, paper: bool, base_url: str) -> Any:
    return SimpleNamespace(
        paper=paper,
        _trading_client=SimpleNamespace(_base_url=base_url, _sandbox=paper),
    )


def test_paper_gates_accept_a_paper_account() -> None:
    adapter = _gate_adapter(paper=True, base_url="https://paper-api.alpaca.markets")
    account = SimpleNamespace(account_number="PA35HB6T7815")
    evidence = pv._check_paper_gates(adapter, account)
    assert any("paper-api" in item for item in evidence)


@pytest.mark.parametrize(
    ("paper", "endpoint", "login", "match"),
    [
        (False, "https://paper-api.alpaca.markets", "PA35HB6T7815", "paper"),
        (True, "https://api.alpaca.markets", "PA35HB6T7815", "paper endpoint"),
        (True, "https://paper-api.alpaca.markets", "7B8C9D0E1F", "PA"),
    ],
    ids=["adapter-not-paper", "live-base-url", "login-not-pa"],
)
def test_paper_gates_refuse_non_paper(
    paper: bool, endpoint: str, login: str, match: str
) -> None:
    adapter = _gate_adapter(paper=paper, base_url=endpoint)
    account = SimpleNamespace(account_number=login)
    with pytest.raises(pv.PaperVerifyError, match=match):
        pv._check_paper_gates(adapter, account)


# ─── Dry-run guarantee ────────────────────────────────────────────────────────


def test_require_execute_blocks_writes_in_dry_run(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, execute=False)
    with pytest.raises(pv.PaperVerifyError, match="--execute"):
        ctx.require_execute("test write")
    ctx.args.execute = True
    ctx.require_execute("test write")  # must not raise


def test_dry_run_step1_submits_nothing(tmp_path: Path, capsys) -> None:
    adapter = _FakeAdapter()
    ctx = _ctx(tmp_path, execute=False, adapter=adapter)
    rc = asyncio.run(pv._step_entry(ctx))
    assert rc == pv.EXIT_OK
    assert adapter.submitted == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "RESULT: PASS" in out


def test_dry_run_partial_exit_sizes_through_daemon_but_submits_nothing(
    tmp_path: Path, isolated_db
) -> None:
    """The exit path runs the daemon's real sizing (prepare_order_with_risk ->
    prepare_exit_intent_order) against the seeded ledger, then stops before
    the broker call."""
    database, _engine, _data_dir = isolated_db
    account_id = "fixture-account"
    deployment_id = pv._deployment_id_for(account_id, "SPY")
    from tests.conftest import seed_live_deployments

    seed_live_deployments(database, [deployment_id], account_id=account_id)
    # A 4-share entry fill -> this deployment's ledger is +4 for SPY.
    signal = LiveSignal(
        id=uuid.uuid4(),
        deployment_id=deployment_id,
        symbol="SPY",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"idempotency_key": uuid.uuid4().hex},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    database.save_live_execution(
        LiveExecution(
            signal_id=uuid.UUID(signal_id),
            actual_price=100.0,
            filled_qty=4.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=1),
            broker_order_id=f"order-{uuid.uuid4().hex[:8]}",
        )
    )
    deployment = database.get_live_deployment(deployment_id)
    assert deployment is not None

    adapter = _FakeAdapter(positions=[{"symbol": "SPY", "qty": 4.0}])
    ctx = _ctx(tmp_path, execute=False, adapter=adapter)
    ctx.deployment = deployment
    rc = asyncio.run(pv._submit_partial_exit(ctx, fraction=0.25))
    assert rc is None
    assert adapter.submitted == []


def test_dry_run_crash_step_never_runs_hook(tmp_path: Path) -> None:
    adapter = _FakeAdapter(positions=[{"symbol": "SPY", "qty": 4.0}])
    ctx = _ctx(tmp_path, execute=False, adapter=adapter)
    rc = asyncio.run(pv._step_crash(ctx))
    assert rc == pv.EXIT_INCONCLUSIVE
    assert adapter.submitted == []


# ─── End-to-end arg handling ──────────────────────────────────────────────────


def test_main_refuses_without_credentials(tmp_path: Path, restored_env) -> None:
    """With no accounts-env credentials resolvable, main must fail closed
    (non-zero) rather than connect or submit anything."""
    rc = pv.main(
        [
            "--account-id",
            "no-such-account",
            "--db-dir",
            str(tmp_path / "pv-db"),
            "--step",
            "1",
        ]
    )
    assert rc == pv.EXIT_FAIL

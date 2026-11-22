"""Promotion gate (#165) at every enforcement point.

Per point: a non-promoted deployment on real money is refused with a clear
error, a promoted one passes, paper/demo passes without promotion, and an
account whose type is unknown is treated as real money. The existing demo-only
block is additionally pinned to still refuse real money for promoted code.

The cTrader top-N deploy gets its own section (#261): `activate` there arms the
rows it creates, with none of the activate path's fresh broker re-verification,
so a non-demo account gets PAUSED rows that the response and the log name.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from live_runner import daemon
from live_runner import promotion as live_promotion
from live_runner.base_adapter import BaseLiveAdapter
from live_runner.preflight import run_live_preflight
from runner.promotion_gate import StrategyNotPromotedError
from shared.models import LiveAccountConfig, LiveSignal
from tests.test_promotion_gate import (
    EVIDENCE_SUBDIR,
    STRATEGY_NAME,
    make_signal_bots_root,
    promote_ramjet_bot,
    promote_strategy_file,
    strategy_deployment,
    use_strategies_dir,
)


@pytest.fixture
def strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_strategies_dir(tmp_path, monkeypatch)


@pytest.fixture
def bots_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import signal_bots.registry as registry

    root = make_signal_bots_root(tmp_path / "signal_bots")
    monkeypatch.setattr(registry, "_ROOT", root)
    monkeypatch.setattr(live_promotion, "REPOSITORY_ROOT", tmp_path)
    return root


# --------------------------------------------------------------------------- #
# Live preflight: the verdict is the paper/demo mode preflight resolved
# --------------------------------------------------------------------------- #

_CTRADER_ENV = {
    "CTRADER_APP_ID": "app",
    "CTRADER_APP_SECRET": "secret",
    "CTRADER_ACCESS_TOKEN": "token",
    "CTRADER_REFRESH_TOKEN": "refresh",
}


def _preflight(tmp_path: Path, env: dict[str, str], strategy: str = STRATEGY_NAME):
    config = tmp_path / "live_deployments.yaml"
    config.write_text(
        dedent(
            f"""
            accounts:
              - id: "10105152"
                broker: ctrader
            deployments:
              - strategy_name: {strategy}
                account_id: "10105152"
                symbols: [EURUSD]
                timeframe: 15m
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return run_live_preflight(config, env=env)


def _promotion_errors(result) -> list[str]:
    return [error for error in result.deployment_errors if "Promotion gate" in error]


@pytest.mark.parametrize(
    "broker, paper, demo, expected",
    [
        # Alpaca's source of truth is the paper flag; its demo flag means nothing here.
        ("alpaca", True, False, False),
        ("alpaca", False, True, True),
        ("alpaca", None, None, True),
        # cTrader's source of truth is the demo flag; its paper flag means nothing here.
        ("ctrader", True, None, True),
        ("ctrader", None, True, False),
        ("ctrader", False, False, True),
    ],
)
def test_preflight_reads_each_brokers_own_demo_flag(
    broker: str, paper: bool | None, demo: bool | None, expected: bool
) -> None:
    from live_runner.preflight import LivePreflightAccountResult, _account_result_is_real_money

    result = LivePreflightAccountResult(
        account_id="acc-1", broker=broker, ok=True, paper=paper, demo=demo
    )
    assert _account_result_is_real_money(result) is expected


def test_preflight_demo_account_passes_without_promotion(
    strategies_dir: Path, tmp_path: Path
) -> None:
    result = _preflight(tmp_path, {**_CTRADER_ENV, "CTRADER_DEMO": "true"})
    assert result.ok is True, result
    assert _promotion_errors(result) == []


def test_preflight_refuses_a_non_promoted_strategy_on_real_money(
    strategies_dir: Path, tmp_path: Path
) -> None:
    result = _preflight(tmp_path, {**_CTRADER_ENV, "CTRADER_DEMO": "false"})
    assert result.ok is False
    [error] = _promotion_errors(result)
    assert error.startswith(f"{STRATEGY_NAME} (10105152)")
    assert "no promotion ledger" in error


def test_preflight_accepts_a_promoted_strategy_on_real_money(
    strategies_dir: Path, tmp_path: Path
) -> None:
    promote_strategy_file(strategies_dir)
    result = _preflight(tmp_path, {**_CTRADER_ENV, "CTRADER_DEMO": "false"})
    assert _promotion_errors(result) == []
    assert result.ok is True, result


def test_preflight_treats_an_unresolved_account_mode_as_real_money(
    strategies_dir: Path, tmp_path: Path
) -> None:
    # No credentials: preflight cannot resolve demo or live.
    result = _preflight(tmp_path, {})
    assert result.ok is False
    assert _promotion_errors(result), result.deployment_errors


def test_preflight_refuses_a_missing_strategy_file_on_real_money(
    strategies_dir: Path, tmp_path: Path
) -> None:
    result = _preflight(tmp_path, {**_CTRADER_ENV, "CTRADER_DEMO": "false"}, strategy="ghost")
    assert result.ok is False
    [error] = _promotion_errors(result)
    assert "no strategy file for 'ghost'" in error


# --------------------------------------------------------------------------- #
# Daemon: every entry order is prepared by prepare_order_with_risk
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Adapter(BaseLiveAdapter):
    """Adapter double. ``demo`` is the flag ``_adapter_is_demo`` reads."""

    demo: Any = None
    submitted: list[LiveSignal] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def get_quote(self, symbol: str):
        # Entries need a fresh broker quote for the #125 deviation gate.
        from live_runner.base_adapter import LiveQuote

        return LiveQuote(
            bid=100.0,
            ask=100.0,
            timestamp=datetime.now(timezone.utc),
            source="test",
            timestamp_source="broker",
        )

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append(signal)
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def get_pip_value(self, symbol: str):
        # #146: sizing needs a trusted broker pip value for entries.
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=1.0, units_per_qty=1.0, conversion_rate=1.0,
            quote_asset="USD", account_asset="USD", source="test",
        )


def _entry_signal(**metadata: Any) -> LiveSignal:
    return LiveSignal(
        deployment_id="11111111-1111-1111-1111-111111111111",
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0, **metadata},
    )


def _submit(adapter: _Adapter) -> str:
    return asyncio.run(
        daemon.submit_order_with_risk(adapter, strategy_deployment(), _entry_signal())
    )


def test_daemon_demo_adapter_submits_without_promotion(strategies_dir: Path) -> None:
    adapter = _Adapter(demo=True)
    assert _submit(adapter) == "order-1"
    assert len(adapter.submitted) == 1


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_daemon_refuses_entries_of_a_non_promoted_strategy_on_real_money(
    strategies_dir: Path, flag: Any
) -> None:
    # None / "true" / 1: the adapter does not prove demo, so it is real money.
    adapter = _Adapter(demo=flag)
    with pytest.raises(StrategyNotPromotedError, match="real-money account 'acc-1'"):
        _submit(adapter)
    assert adapter.submitted == []


def test_daemon_submits_entries_of_a_promoted_strategy_on_real_money(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    adapter = _Adapter(demo=False)
    assert _submit(adapter) == "order-1"


def test_daemon_refuses_a_promoted_strategy_after_a_file_change(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    path = strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"
    path.write_bytes(path.read_bytes() + b"\n")
    adapter = _Adapter(demo=False)
    with pytest.raises(StrategyNotPromotedError, match="changed"):
        _submit(adapter)


def test_daemon_refuses_a_promoted_strategy_after_an_evidence_change(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """The re-check opens the evidence files, so every enforcement point inherits it (#260)."""
    evidence_dir = tmp_path / EVIDENCE_SUBDIR
    promote_strategy_file(strategies_dir, evidence_dir=evidence_dir)
    result = evidence_dir / "opt-1.json"
    result.write_text(result.read_text(encoding="utf-8") + " ", encoding="utf-8")
    adapter = _Adapter(demo=False)
    with pytest.raises(StrategyNotPromotedError, match="changed after the promotion"):
        _submit(adapter)


def test_daemon_never_blocks_an_exit_only_order(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.live_runner.test_exit_signal_sizing import _patch_ledger

    deployment = strategy_deployment()
    # Exits size from the deployment's own ledger (#121): it owns the whole
    # 5.0 units the broker reports, so there is something of its own to close.
    _patch_ledger(monkeypatch, deployment, 5.0)
    adapter = _Adapter(
        demo=False, positions=[{"symbol": "EURUSD", "qty": 5.0, "side": "long"}]
    )
    signal = _entry_signal(exit_only=True).model_copy(update={"action": "sell"})
    prepared, qty = asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))
    assert qty > 0
    assert prepared.metadata["exit_only"] is True


def test_daemon_discord_path_refuses_a_non_promoted_entry_on_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Discord submission path is gated by the same preparation step."""
    adapter = _Adapter(demo=False)
    deployment = strategy_deployment(signal_source="discord", signal_source_type="discord")
    refusals: list[BaseException] = []

    async def _run() -> None:
        try:
            await daemon.prepare_order_with_risk(adapter, deployment, _entry_signal())
        except StrategyNotPromotedError as exc:
            refusals.append(exc)

    asyncio.run(_run())
    assert refusals
    assert adapter.submitted == []


def test_demo_only_block_still_drops_a_non_demo_adapter_for_promoted_code(
    strategies_dir: Path,
) -> None:
    """Promotion does not lift the existing block: a real-money adapter is never kept."""
    promote_strategy_file(strategies_dir)
    adapter = _Adapter(demo=False)
    active: dict[str, BaseLiveAdapter] = {"acc-1": adapter}
    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            "acc-1",
            {"acc-1": LiveAccountConfig(id="acc-1", broker="ctrader", name="x")},
            active,
            accounts_env_values={},
        )
    )
    assert active == {}


# --------------------------------------------------------------------------- #
# Viewer: create, activate, cTrader top-N (incl. dry run)
# --------------------------------------------------------------------------- #


def _discovery(monkeypatch: pytest.MonkeyPatch, viewer, accounts: Any) -> None:
    def _payload(*args: Any, **kwargs: Any):
        if isinstance(accounts, Exception):
            raise accounts
        return accounts

    monkeypatch.setattr(viewer, "_cached_live_accounts_payload", _payload)


@pytest.mark.parametrize(
    "accounts, expected",
    [
        ([{"id": "a", "is_demo": True, "is_demo_asserted": True}], True),
        ([{"id": "a", "is_demo": False, "is_demo_asserted": True}], False),
        ([{"id": "a", "is_demo": False, "is_demo_asserted": False}], None),
        ([{"id": "a", "is_demo": True}], None),
        ([{"id": "b", "is_demo": True, "is_demo_asserted": True}], None),
        ([], None),
        (RuntimeError("broker down"), None),
    ],
)
def test_viewer_broker_reported_demo_flag_fails_closed(
    monkeypatch: pytest.MonkeyPatch, accounts: Any, expected: bool | None
) -> None:
    import viewer.main as viewer

    _discovery(monkeypatch, viewer, accounts)
    assert asyncio.run(viewer._broker_reported_demo_flag("a")) is expected


def test_viewer_broker_verdict_is_fresh_not_the_30s_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker that reclassifies the account must be seen at once, not after the cache TTL."""
    import viewer.main as viewer

    answers = [[{"id": "a", "is_demo": True, "is_demo_asserted": True}]]
    monkeypatch.setattr(viewer, "_discover_live_accounts_payload", lambda: answers[0])
    # Warm the 30 s discovery cache with the account as demo.
    viewer._cached_live_accounts_payload(force_refresh=True)

    answers[0] = [{"id": "a", "is_demo": False, "is_demo_asserted": True}]
    assert asyncio.run(viewer._broker_reported_demo_flag("a")) is False


def _top_n(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_demo: bool | None,
    dry_run: bool = True,
    activate: bool = False,
):
    import viewer.main as viewer

    monkeypatch.setattr(viewer, "_seed_default_live_accounts", lambda: None)
    account: dict[str, Any] = {"id": "ct-1", "broker": "ctrader"}
    if is_demo is not None:
        account.update(is_demo=is_demo, is_demo_asserted=True)
    _discovery(monkeypatch, viewer, [account])
    monkeypatch.setattr(
        viewer,
        "list_live_accounts",
        lambda: [LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")],
    )
    monkeypatch.setattr(
        viewer,
        "list_backtest_metadata_rows",
        lambda: [
            {
                "id": "bt-1",
                "strategy_name": STRATEGY_NAME,
                "symbols": ["EURUSD"],
                "timeframe": "15m",
                "sharpe_ratio": 1.4,
                "cagr": 0.2,
                "psr": 0.8,
                "max_drawdown": -5,
                "trade_count": 11,
                "oos_sharpe": 0.9,
            }
        ],
    )
    created: list[Any] = []
    monkeypatch.setattr(viewer, "create_live_deployment", lambda d: created.append(d) or d)
    response = TestClient(viewer.app).post(
        "/api/live/deployments/ctrader/top5",
        json={"dry_run": dry_run, "activate": activate},
    )
    return response, created


def test_viewer_top_n_dry_run_on_demo_passes_without_promotion(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response, _ = _top_n(monkeypatch, is_demo=True)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "planned"


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("is_demo", [False, None])
def test_viewer_top_n_refuses_a_non_promoted_strategy_on_real_or_unknown_money(
    strategies_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    is_demo: bool | None,
) -> None:
    response, created = _top_n(monkeypatch, is_demo=is_demo, dry_run=dry_run)
    assert response.status_code == 409, response.text
    assert "Promotion gate refused" in response.json()["detail"]
    assert created == []


def test_viewer_top_n_dry_run_accepts_a_promoted_strategy_on_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    promote_strategy_file(strategies_dir)
    response, _ = _top_n(monkeypatch, is_demo=False)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Viewer: cTrader top-N `activate` never arms a non-demo account (#261)
# --------------------------------------------------------------------------- #


def test_viewer_top_n_activate_creates_paused_on_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#261: the broker reporting a live account is a refuse-to-arm answer."""
    promote_strategy_file(strategies_dir)
    response, created = _top_n(monkeypatch, is_demo=False, dry_run=False, activate=True)
    assert response.status_code == 200, response.text
    assert [deployment.status for deployment in created] == ["PAUSED"]
    [paused] = response.json()["paused_as_real_money"]
    assert paused["strategy_name"] == STRATEGY_NAME
    assert paused["account_id"] == "ct-1"
    assert "non-demo" in paused["reason"]


def test_viewer_top_n_activate_creates_paused_on_an_unknown_account(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#261: an unanswered demo question fails closed, like the daemon's block."""
    promote_strategy_file(strategies_dir)
    response, created = _top_n(monkeypatch, is_demo=None, dry_run=False, activate=True)
    assert response.status_code == 200, response.text
    assert [deployment.status for deployment in created] == ["PAUSED"]
    [paused] = response.json()["paused_as_real_money"]
    assert paused["account_id"] == "ct-1"
    assert "demo" in paused["reason"]


@contextmanager
def _viewer_log_records(viewer: ModuleType) -> Iterator[list[logging.LogRecord]]:
    """Capture exactly what the viewer logger emits, and restore it afterwards.

    The service loggers do not propagate (``shared.logging.get_logger``), so a
    handler hung on the root logger - caplog's - sees nothing. One dedicated
    handler on the viewer logger itself is both the accurate way to observe them
    and immune to whatever handlers a session has already left around.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    viewer.logger.addHandler(handler)
    try:
        yield records
    finally:
        viewer.logger.removeHandler(handler)


def test_viewer_top_n_pause_on_real_money_is_logged_with_the_reason(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#261: the operator is told, in the log as well as in the response."""
    import viewer.main as viewer

    promote_strategy_file(strategies_dir)
    with _viewer_log_records(viewer) as records:
        response, created = _top_n(monkeypatch, is_demo=False, dry_run=False, activate=True)
    assert response.status_code == 200, response.text
    assert [deployment.status for deployment in created] == ["PAUSED"]
    [record] = [item for item in records if "PAUSED instead of ACTIVE" in item.getMessage()]
    assert record.levelname == "WARNING"
    assert STRATEGY_NAME in record.getMessage()
    assert "live (non-demo)" in record.getMessage()
    assert record.data["paused"][0]["account_id"] == "ct-1"


def test_viewer_top_n_dry_run_reports_paused_not_active_for_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview must not show a deployment an `activate` would not get (#261)."""
    promote_strategy_file(strategies_dir)
    response, created = _top_n(monkeypatch, is_demo=False, dry_run=True, activate=True)
    assert response.status_code == 200, response.text
    assert created == []
    assert response.json()["deployments"][0]["status"] == "PAUSED"


def test_viewer_top_n_activate_creates_active_on_demo(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#261: the demo path is unchanged - an activate on demo still arms it."""
    import viewer.main as viewer

    with _viewer_log_records(viewer) as records:
        response, created = _top_n(monkeypatch, is_demo=True, dry_run=False, activate=True)
    assert response.status_code == 200, response.text
    assert [deployment.status for deployment in created] == ["ACTIVE"]
    assert response.json()["paused_as_real_money"] == []
    # ...and nothing claims a pause that did not happen.
    assert not [item for item in records if "PAUSED instead of ACTIVE" in item.getMessage()]


def test_viewer_top_n_without_activate_reports_nothing_paused(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#261: nothing is reported as paused when PAUSED was requested already."""
    promote_strategy_file(strategies_dir)
    response, created = _top_n(monkeypatch, is_demo=False, dry_run=False, activate=False)
    assert response.status_code == 200, response.text
    assert [deployment.status for deployment in created] == ["PAUSED"]
    assert response.json()["paused_as_real_money"] == []


def _create(
    monkeypatch: pytest.MonkeyPatch,
    discovery: Any,
    *,
    settings: dict | None = None,
    signal_processor: str | None = None,
):
    """POST /api/live/deployments for an Alpaca Discord deployment of STRATEGY_NAME."""
    import viewer.main as viewer
    from shared.models import Service, ServiceStatus, ServiceType

    monkeypatch.setattr(viewer, "_seed_default_live_accounts", lambda: None)
    _discovery(monkeypatch, viewer, discovery)
    monkeypatch.setattr(
        viewer,
        "list_services",
        lambda *args, **kwargs: [
            Service(
                id="88888888-8888-8888-8888-888888888888",
                name=STRATEGY_NAME,
                type=ServiceType.DISCORD_BOT,
                status=ServiceStatus.RUNNING,
                config={
                    "token_env_key": "DISCORD_BOT_ALPHA_TOKEN",
                    "channel_id": "1431465140905185452",
                },
                created_at=datetime.now(timezone.utc),
            )
        ],
    )
    monkeypatch.setattr(
        viewer,
        "_ensure_live_account_for_deployment",
        lambda account_id: {
            "id": account_id,
            "broker": "alpaca",
            "name": "Alpaca",
            # The account number the ramjet route requires (live_runner.stockkit_routing).
            "broker_login": "PA3T0MQAGQ51",
        },
    )
    created: list[Any] = []
    monkeypatch.setattr(viewer, "create_live_deployment", lambda d: created.append(d) or d)
    response = TestClient(viewer.app).post(
        "/api/live/deployments",
        json={
            "strategy_name": STRATEGY_NAME,
            "account_id": "acc-1",
            "symbols": ["AAPL"],
            "timeframe": "1h",
            "signal_source_type": "discord",
            "signal_source_id": STRATEGY_NAME,
            "signal_config": {
                "channel_id": "1431465140905185452",
                **({"signal_processor": signal_processor} if signal_processor else {}),
            },
            "settings": settings or {},
        },
    )
    return response, created


_DEMO = [{"id": "acc-1", "is_demo": True, "is_demo_asserted": True}]
_LIVE = [{"id": "acc-1", "is_demo": False, "is_demo_asserted": True}]


def test_viewer_create_on_demo_passes_without_promotion(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response, created = _create(monkeypatch, _DEMO)
    assert response.status_code == 201, response.text
    assert len(created) == 1


@pytest.mark.parametrize(
    "discovery",
    [_LIVE, [{"id": "acc-1", "is_demo": False, "is_demo_asserted": False}], [], RuntimeError("x")],
)
def test_viewer_create_refuses_a_non_promoted_strategy_on_real_or_unknown_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch, discovery: Any
) -> None:
    response, created = _create(monkeypatch, discovery)
    assert response.status_code == 409, response.text
    assert "Promotion gate refused" in response.json()["detail"]
    assert created == []


def test_viewer_create_accepts_a_promoted_signal_bot_on_real_money(
    bots_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    promote_ramjet_bot()
    response, created = _create(
        monkeypatch, _LIVE, settings={"risk_per_trade_pct": 0.01}, signal_processor="ramjet"
    )
    assert response.status_code == 201, response.text
    assert len(created) == 1


def test_viewer_create_refuses_a_discord_deployment_without_a_processor_on_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw channel signal is traded, not the same-named strategy file (#165)."""
    promote_strategy_file(strategies_dir)
    response, created = _create(monkeypatch, _LIVE, settings={"risk_per_trade_pct": 0.01})
    assert response.status_code == 409, response.text
    assert "raw channel signals" in response.json()["detail"]
    assert created == []


def _paused_discord_deployment(database_module, *, signal_processor: str | None = None) -> str:
    database_module.ensure_live_account(
        "acc-1", "alpaca", is_demo=True, broker_login="PA3T0MQAGQ51"
    )
    did = str(uuid.uuid4())
    database_module.create_live_deployment(
        database_module.LiveDeploymentConfig(
            id=uuid.UUID(did),
            strategy_name=STRATEGY_NAME,
            account_id="acc-1",
            symbols=["AAPL"],
            timeframe="1h",
            status="PAUSED",
            signal_source_type="discord",
            signal_source="discord",
            signal_config={
                "channel_id": "1431465140905185452",
                **({"signal_processor": signal_processor} if signal_processor else {}),
            },
        )
    )
    return did


def _activate(monkeypatch: pytest.MonkeyPatch, did: str, *, broker_confirms_demo: bool):
    import viewer.main as viewer

    async def _reverify(_account_id: str) -> bool:
        return broker_confirms_demo

    monkeypatch.setattr(viewer, "_reverify_account_against_broker", _reverify)
    return TestClient(viewer.app).put(
        f"/api/live/deployments/{did}/status",
        json={"status": "ACTIVE", "confirmation": {"confirmed_deployment_id": did}},
    )


def test_viewer_activation_on_demo_passes_without_promotion(
    strategies_dir: Path, isolated_db_sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_module, _engine, _data_dir = isolated_db_sandbox
    did = _paused_discord_deployment(database_module)
    response = _activate(monkeypatch, did, broker_confirms_demo=True)
    assert response.status_code == 200, response.text
    assert database_module.get_live_deployment(did).status == "ACTIVE"


def test_viewer_activation_on_unconfirmed_account_names_the_promotion_refusal(
    strategies_dir: Path, isolated_db_sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_module, _engine, _data_dir = isolated_db_sandbox
    did = _paused_discord_deployment(database_module)
    response = _activate(monkeypatch, did, broker_confirms_demo=False)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "refusing to activate" in detail
    assert "Promotion gate refused" in detail
    assert database_module.get_live_deployment(did).status == "PAUSED"

    from shared.database import list_live_control_audit

    [entry] = [
        item
        for item in list_live_control_audit()
        if item.get("target_id") == did and not item.get("succeeded")
    ]
    assert "Promotion gate refused" in entry["detail"]["promotion_gate"]


def test_viewer_demo_only_block_still_refuses_a_promoted_signal_bot_on_real_money(
    bots_root: Path, isolated_db_sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion is necessary, not sufficient: the demo-only block stays (#165)."""
    database_module, _engine, _data_dir = isolated_db_sandbox
    promote_ramjet_bot()
    did = _paused_discord_deployment(database_module, signal_processor="ramjet")
    response = _activate(monkeypatch, did, broker_confirms_demo=False)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "refusing to activate" in detail
    assert "Promotion gate" not in detail
    assert database_module.get_live_deployment(did).status == "PAUSED"


def test_viewer_activation_gate_uses_the_same_function(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latent post-block check refuses real money on its own (no second copy)."""
    import viewer.main as viewer

    deployment = strategy_deployment()
    assert viewer._promotion_gate_refusal(deployment, demo_flag=True) is None
    assert "Promotion gate refused" in viewer._promotion_gate_refusal(deployment, demo_flag=None)
    assert "Promotion gate refused" in viewer._promotion_gate_refusal(deployment, demo_flag=False)
    promote_strategy_file(strategies_dir)
    assert viewer._promotion_gate_refusal(deployment, demo_flag=False) is None

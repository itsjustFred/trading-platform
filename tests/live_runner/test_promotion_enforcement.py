"""Promotion gate (#165) at every enforcement point.

Per point: a non-promoted deployment on real money is refused with a clear
error, a promoted one passes, paper/demo passes without promotion, and an
account whose type is unknown is treated as real money. The existing demo-only
block is additionally pinned to still refuse real money for promoted code.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi.testclient import TestClient

from live_runner import daemon
from live_runner.base_adapter import BaseLiveAdapter
from live_runner.preflight import run_live_preflight
from runner.promotion_gate import StrategyNotPromotedError
from shared.models import LiveAccountConfig, LiveSignal
from tests.test_promotion_gate import (
    STRATEGY_NAME,
    promote_strategy_file,
    strategy_deployment,
    use_strategies_dir,
)


@pytest.fixture
def strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_strategies_dir(tmp_path, monkeypatch)


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

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append(signal)
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)


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
    with pytest.raises(StrategyNotPromotedError, match="changed"):
        _submit(_Adapter(demo=False))


def test_daemon_never_blocks_an_exit_only_order(strategies_dir: Path) -> None:
    adapter = _Adapter(
        demo=False, positions=[{"symbol": "EURUSD", "qty": 5.0, "side": "long"}]
    )
    signal = _entry_signal(exit_only=True).model_copy(update={"action": "sell"})
    prepared, qty = asyncio.run(
        daemon.prepare_order_with_risk(adapter, strategy_deployment(), signal)
    )
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
    assert refusals and adapter.submitted == []


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


def _top_n(monkeypatch: pytest.MonkeyPatch, *, is_demo: bool | None, dry_run: bool = True):
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
        "/api/live/deployments/ctrader/top5", json={"dry_run": dry_run}
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


def _create(monkeypatch: pytest.MonkeyPatch, discovery: Any, *, settings: dict | None = None):
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
        lambda account_id: {"id": account_id, "broker": "alpaca", "name": "Alpaca"},
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
            "signal_config": {"channel_id": "1431465140905185452"},
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


def test_viewer_create_accepts_a_promoted_strategy_on_real_money(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    promote_strategy_file(strategies_dir)
    response, created = _create(monkeypatch, _LIVE, settings={"risk_per_trade_pct": 0.01})
    assert response.status_code == 201, response.text
    assert len(created) == 1


def _paused_discord_deployment(database_module) -> str:
    database_module.ensure_live_account("acc-1", "alpaca", is_demo=True)
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
            signal_config={"channel_id": "1431465140905185452"},
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


def test_viewer_demo_only_block_still_refuses_a_promoted_strategy_on_real_money(
    strategies_dir: Path, isolated_db_sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion is necessary, not sufficient: the demo-only block stays (#165)."""
    database_module, _engine, _data_dir = isolated_db_sandbox
    promote_strategy_file(strategies_dir)
    did = _paused_discord_deployment(database_module)
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

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
LIVE_INTEGRATION_REPORT = PROJECT_ROOT / "live_integration_report.json"
_SMOKE_MODE_ALIASES = {
    "alpaca": "alpaca-crypto-roundtrip",
    "ctrader": "ctrader-demo-roundtrip",
}


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class SmokeReport:
    mode: str
    ok: bool
    started_at: str
    finished_at: str
    checks: list[SmokeCheck]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_values(path: Path) -> dict[str, str]:
    values = dotenv_values(path)
    return {
        str(key): str(value).strip()
        for key, value in values.items()
        if key and isinstance(value, str)
    }


_WORKER_NO_JSON_ERROR = "worker returned no JSON payload"


def _public_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _position_qty_for_symbol(positions: list[Any], symbol: str) -> float:
    normalized = symbol.replace("/", "").upper()
    for position in positions:
        if isinstance(position, Mapping):
            raw_symbol = position.get("symbol", "")
            raw_qty = position.get("qty", 0.0)
        else:
            raw_symbol = getattr(position, "symbol", "")
            raw_qty = getattr(position, "qty", 0.0)
        position_symbol = str(raw_symbol or "").replace("/", "").upper()
        if position_symbol == normalized:
            return float(raw_qty or 0.0)
    return 0.0


def _write_report(report: SmokeReport, reports_dir: Path) -> Path:
    target = reports_dir.resolve()
    if not target.is_dir():
        raise ValueError(f"Reports directory does not exist or is not a directory: {reports_dir}")
    target.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target / f"live_smoke_{report.mode}_{timestamp}.json"
    path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_live_integration_report(report: SmokeReport) -> Path:
    LIVE_INTEGRATION_REPORT.write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return LIVE_INTEGRATION_REPORT


async def _alpaca_account_checks(env: dict[str, str]) -> list[SmokeCheck]:
    from live_runner.alpaca_adapter import AlpacaLiveAdapter, resolve_alpaca_credentials

    checks: list[SmokeCheck] = []
    for index in (1, 2, 3):
        account_name = env.get(f"ALPACA_ACCOUNT_NAME_ACC{index}", f"ACC{index}")
        account_number = env.get(f"ALPACA_ACCOUNT_NUMBER_ACC{index}", "")
        check_name = f"alpaca-{account_name.lower()}-account"
        try:
            credentials = resolve_alpaca_credentials(broker_login=account_number, env=env)
            if credentials is None:
                checks.append(SmokeCheck(check_name, False, error="No matching credentials."))
                continue
            api_key, secret_key, paper = credentials
            adapter = AlpacaLiveAdapter(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper,
                account_id=f"alpaca-acc{index}",
            )
            try:
                await asyncio.wait_for(adapter.connect(), timeout=20)
                cash, equity = await asyncio.wait_for(adapter.get_balance(), timeout=10)
                positions = await asyncio.wait_for(adapter.get_positions(), timeout=10)
            finally:
                await adapter.disconnect()
            checks.append(
                SmokeCheck(
                    check_name,
                    bool(paper),
                    {
                        "paper": paper,
                        "cash": round(float(cash), 2),
                        "equity": round(float(equity), 2),
                        "positions": len(positions),
                        "account_hint": account_number,
                    },
                    "" if paper else "Account is not in paper mode.",
                )
            )
        except Exception as exc:
            checks.append(SmokeCheck(check_name, False, error=_public_error(exc)))
    return checks


async def _ctrader_account_checks(
    env: dict[str, str],
    *,
    accounts_env: Path,
    timeout_seconds: int,
) -> list[SmokeCheck]:
    from live_runner.account_discovery import discover_ctrader_accounts

    checks: list[SmokeCheck] = []
    try:
        accounts = await asyncio.wait_for(
            discover_ctrader_accounts(
                env.get("CTRADER_APP_ID", ""),
                env.get("CTRADER_APP_SECRET", ""),
                env.get("CTRADER_ACCESS_TOKEN", ""),
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return [SmokeCheck("ctrader-discovery", False, error=_public_error(exc))]

    checks.append(
        SmokeCheck(
            "ctrader-discovery",
            bool(accounts),
            {"accounts": len(accounts), "demo_accounts": sum(1 for account in accounts if account.is_demo)},
            "" if accounts else "No cTrader accounts discovered.",
        )
    )

    demo_accounts = [account for account in accounts if account.is_demo]
    if not demo_accounts:
        checks.append(SmokeCheck("ctrader-demo-adapter", False, error="No demo account found."))
        return checks

    selected = max(demo_accounts, key=lambda account: float(account.balance or 0.0))
    try:
        adapter_result = await _run_ctrader_adapter_subprocess(
            account_id=str(selected.ctid or selected.id),
            accounts_env=accounts_env,
            timeout_seconds=timeout_seconds,
        )
        checks.append(SmokeCheck("ctrader-demo-adapter", True, adapter_result))
    except Exception as exc:
        checks.append(SmokeCheck("ctrader-demo-adapter", False, error=_public_error(exc)))
    return checks


async def _run_ctrader_adapter_subprocess(
    *,
    account_id: str,
    accounts_env: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        "ctrader-adapter-worker",
        "--accounts-env",
        str(accounts_env),
        "--ctrader-account-id",
        account_id,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"worker exited {completed.returncode}")
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    payload_line = next((line for line in reversed(lines) if line.startswith("{")), "")
    if not payload_line:
        raise RuntimeError(_WORKER_NO_JSON_ERROR)
    return json.loads(payload_line)


async def _run_ctrader_order_subprocess(
    *,
    account_id: str,
    accounts_env: Path,
    symbol: str,
    volume: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        "ctrader-order-worker",
        "--accounts-env",
        str(accounts_env),
        "--ctrader-account-id",
        account_id,
        "--ctrader-symbol",
        symbol,
        "--ctrader-volume",
        str(volume),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 20,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"worker exited {completed.returncode}")
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    payload_line = next((line for line in reversed(lines) if line.startswith("{")), "")
    if not payload_line:
        raise RuntimeError(_WORKER_NO_JSON_ERROR)
    return json.loads(payload_line)


async def _run_ctrader_strategy_e2e_subprocess(
    *,
    account_id: str,
    accounts_env: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        "ctrader-e2e-worker",
        "--accounts-env",
        str(accounts_env),
        "--ctrader-account-id",
        account_id,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 20,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"worker exited {completed.returncode}")
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    payload_line = next((line for line in reversed(lines) if line.startswith("{")), "")
    if not payload_line:
        raise RuntimeError(_WORKER_NO_JSON_ERROR)
    return json.loads(payload_line)


async def _ctrader_adapter_worker(
    *,
    account_id: str,
    accounts_env: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    env = _env_values(accounts_env)
    adapter = CTraderLiveAdapter(
        app_id=env.get("CTRADER_APP_ID", ""),
        app_secret=env.get("CTRADER_APP_SECRET", ""),
        access_token=env.get("CTRADER_ACCESS_TOKEN", ""),
        refresh_token=env.get("CTRADER_REFRESH_TOKEN", ""),
        account_id=account_id,
        demo=True,
        account_config_id=f"ctrader-demo-{account_id}",
    )
    await asyncio.wait_for(adapter.connect(), timeout=timeout_seconds)
    balance, equity = await asyncio.wait_for(adapter.get_balance(), timeout=15)
    positions = await asyncio.wait_for(adapter.get_positions(), timeout=15)
    return {
        "account_id": account_id,
        "balance": round(float(balance), 2),
        "equity": round(float(equity), 2),
        "positions": len(positions),
    }


async def _find_ctrader_open_position(
    adapter: Any,
    *,
    symbol: str,
) -> dict[str, Any] | None:
    reconcile_req = adapter._ProtoOAReconcileReq()
    reconcile_req.ctidTraderAccountId = adapter.ctid_trader_account_id
    reconcile_res = await adapter._send_request(reconcile_req, timeout_seconds=15)
    if not isinstance(reconcile_res, adapter._ProtoOAReconcileRes):
        raise RuntimeError(f"Unexpected reconcile response: {type(reconcile_res).__name__}")

    symbol_id = adapter._symbol_name_to_id.get(symbol.upper())
    for position in getattr(reconcile_res, "position", []):
        if int(getattr(position, "positionStatus", 0) or 0) != (
            adapter._ProtoOAPositionStatus.POSITION_STATUS_OPEN
        ):
            continue
        trade_data = getattr(position, "tradeData", None)
        if trade_data is None:
            continue
        if symbol_id is not None and int(getattr(trade_data, "symbolId", 0) or 0) != symbol_id:
            continue
        return {
            "position_id": int(getattr(position, "positionId", 0) or 0),
            "volume": int(getattr(trade_data, "volume", 0) or 0),
            "symbol_id": int(getattr(trade_data, "symbolId", 0) or 0),
        }
    return None


async def _wait_for_ctrader_position(
    adapter: Any,
    *,
    symbol: str,
    should_exist: bool,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        latest = await _find_ctrader_open_position(adapter, symbol=symbol)
        if should_exist and latest is not None:
            return latest
        if not should_exist and latest is None:
            return None
        await asyncio.sleep(1)
    return latest


async def _ctrader_order_worker(
    *,
    account_id: str,
    accounts_env: Path,
    symbol: str,
    volume: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    from live_runner.ctrader_adapter import CTraderLiveAdapter
    from shared.models import LiveSignal

    env = _env_values(accounts_env)
    adapter = CTraderLiveAdapter(
        app_id=env.get("CTRADER_APP_ID", ""),
        app_secret=env.get("CTRADER_APP_SECRET", ""),
        access_token=env.get("CTRADER_ACCESS_TOKEN", ""),
        refresh_token=env.get("CTRADER_REFRESH_TOKEN", ""),
        account_id=account_id,
        demo=True,
        account_config_id=f"ctrader-demo-{account_id}",
    )
    await asyncio.wait_for(adapter.connect(), timeout=timeout_seconds)
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq

    before_position = await _find_ctrader_open_position(adapter, symbol=symbol)
    if before_position is not None:
        raise RuntimeError(f"Refusing smoke order because {symbol} already has an open position.")

    signal = LiveSignal(
        deployment_id=f"ctrader-demo-smoke-{account_id}",
        symbol=symbol.upper(),
        action="buy",
        theoretical_price=1.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "ctrader_volume": volume,
            "entry_price": 1.0,
            "timeframe": "1m",
        },
    )
    broker_order_id = await asyncio.wait_for(adapter.submit_order(signal), timeout=timeout_seconds)

    opened_position = await _wait_for_ctrader_position(
        adapter,
        symbol=symbol,
        should_exist=True,
        timeout_seconds=timeout_seconds,
    )
    if opened_position is None:
        raise RuntimeError("cTrader order submitted but no open position appeared.")

    close_req = ProtoOAClosePositionReq()
    close_req.ctidTraderAccountId = adapter.ctid_trader_account_id
    close_req.positionId = int(opened_position["position_id"])
    close_req.volume = int(opened_position["volume"])
    close_res = await asyncio.wait_for(adapter._send_request(close_req, timeout_seconds=20), timeout=25)
    remaining_position = await _wait_for_ctrader_position(
        adapter,
        symbol=symbol,
        should_exist=False,
        timeout_seconds=timeout_seconds,
    )
    if remaining_position is not None:
        raise RuntimeError(f"cTrader close did not flatten position: {remaining_position}")

    return {
        "account_id": account_id,
        "symbol": symbol.upper(),
        "volume": volume,
        "broker_order_id": str(broker_order_id or ""),
        "position_id": opened_position["position_id"],
        "close_response": type(close_res).__name__,
        "remaining_position": None,
    }


async def _ctrader_strategy_e2e_worker(
    *,
    account_id: str,
    accounts_env: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    import live_runner.ctrader_adapter as ctrader_adapter
    from live_runner.ctrader_adapter import CTraderLiveAdapter
    from live_runner.daemon import submit_order_with_risk
    from shared.models import LiveDeploymentConfig, LiveSignal

    symbol = "EURUSD"
    ctrader_adapter._ensure_asyncio_twisted_reactor(asyncio.get_running_loop())
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq

    env = _env_values(accounts_env)
    adapter = CTraderLiveAdapter(
        app_id=env.get("CTRADER_APP_ID", ""),
        app_secret=env.get("CTRADER_APP_SECRET", ""),
        access_token=env.get("CTRADER_ACCESS_TOKEN", ""),
        refresh_token=env.get("CTRADER_REFRESH_TOKEN", ""),
        account_id=account_id,
        demo=True,
        account_config_id=f"ctrader-demo-{account_id}",
    )
    await asyncio.wait_for(adapter.connect(), timeout=timeout_seconds)

    before_position = await _find_ctrader_open_position(adapter, symbol=symbol)
    if before_position is not None:
        raise RuntimeError(f"Refusing e2e order because {symbol} already has an open position.")

    deployment = LiveDeploymentConfig(
        strategy_name="volatility_breakout",
        account_id=account_id,
        symbols=[symbol],
        timeframe="1m",
        status="ACTIVE",
        signal_source_type="internal",
        signal_source="internal",
        settings={
            "max_risk_per_trade": 0.0001,
            "max_total_exposure": 0.0002,
            "max_positions": 20,
        },
    )
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol=symbol,
        action="buy",
        theoretical_price=1.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "ctrader_volume": 100000,
            "entry_price": 1.0,
            "stop_loss": 0.99,
            "timeframe": deployment.timeframe,
        },
    )
    broker_order_id = await asyncio.wait_for(
        submit_order_with_risk(adapter, deployment, signal),
        timeout=timeout_seconds,
    )
    opened_position = await _wait_for_ctrader_position(
        adapter,
        symbol=symbol,
        should_exist=True,
        timeout_seconds=timeout_seconds,
    )
    if opened_position is None:
        raise RuntimeError("cTrader order submitted but no open position appeared.")

    close_req = ProtoOAClosePositionReq()
    close_req.ctidTraderAccountId = adapter.ctid_trader_account_id
    close_req.positionId = int(opened_position["position_id"])
    close_req.volume = int(opened_position["volume"])
    close_res = await asyncio.wait_for(
        adapter._send_request(close_req, timeout_seconds=20),
        timeout=25,
    )
    remaining_position = await _wait_for_ctrader_position(
        adapter,
        symbol=symbol,
        should_exist=False,
        timeout_seconds=timeout_seconds,
    )
    remaining_volume = int(remaining_position["volume"]) if remaining_position else 0
    if remaining_volume != 0:
        raise RuntimeError(f"cTrader flatten left remaining {symbol} volume={remaining_volume}")

    return {
        "status": "passed",
        "account_id": account_id,
        "symbol": symbol,
        "broker_order_ids": [str(broker_order_id or "")],
        "filled_volume": int(opened_position["volume"]),
        "position_id": int(opened_position["position_id"]),
        "close_response": type(close_res).__name__,
        "remaining_volume": remaining_volume,
        "error": "",
    }


async def _alpaca_crypto_roundtrip(
    env: dict[str, str],
    *,
    symbol: str,
    notional: float,
    timeout_seconds: int,
) -> list[SmokeCheck]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    from live_runner.alpaca_adapter import resolve_alpaca_credentials

    checks: list[SmokeCheck] = []
    for index in (1, 2, 3):
        account_name = env.get(f"ALPACA_ACCOUNT_NAME_ACC{index}", f"ACC{index}")
        account_number = env.get(f"ALPACA_ACCOUNT_NUMBER_ACC{index}", "")
        check_name = f"alpaca-{account_name.lower()}-crypto-roundtrip"
        credentials = resolve_alpaca_credentials(broker_login=account_number, env=env)
        if credentials is None:
            checks.append(SmokeCheck(check_name, False, error="No matching credentials."))
            continue
        api_key, secret_key, paper = credentials
        if not paper:
            checks.append(SmokeCheck(check_name, False, error="Refusing non-paper Alpaca account."))
            continue

        client = TradingClient(api_key, secret_key, paper=True)
        client_order_id = f"codex-smoke-{uuid4().hex[:20]}"
        order_id = ""
        close_status = "not_started"
        try:
            before_positions = client.get_all_positions()
            order = client.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        notional=notional,
                        side=OrderSide.BUY,
                        type=OrderType.MARKET,
                        time_in_force=TimeInForce.GTC,
                    client_order_id=client_order_id,
                )
            )
            order_id = str(getattr(order, "id", ""))
            deadline = time.monotonic() + timeout_seconds
            latest = order
            while time.monotonic() < deadline:
                latest = client.get_order_by_id(order_id)
                if getattr(latest, "status", None) in {
                    OrderStatus.FILLED,
                    OrderStatus.CANCELED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                }:
                    break
                await asyncio.sleep(1)

            filled_qty = float(getattr(latest, "filled_qty", 0.0) or 0.0)
            status = str(getattr(latest, "status", ""))
            if filled_qty > 0:
                positions_before_close = client.get_all_positions()
                close_qty = _position_qty_for_symbol(positions_before_close, symbol)
                if close_qty <= 0:
                    raise RuntimeError("Buy filled but no matching position was available to close.")
                close_order = client.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=close_qty,
                        side=OrderSide.SELL,
                        type=OrderType.MARKET,
                        time_in_force=TimeInForce.GTC,
                        client_order_id=f"codex-close-{uuid4().hex[:20]}",
                    )
                )
                close_status = str(getattr(close_order, "status", "submitted"))
                close_order_id = str(getattr(close_order, "id", ""))
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    close_latest = client.get_order_by_id(close_order_id)
                    close_status = str(getattr(close_latest, "status", close_status))
                    if getattr(close_latest, "status", None) in {
                        OrderStatus.FILLED,
                        OrderStatus.CANCELED,
                        OrderStatus.REJECTED,
                        OrderStatus.EXPIRED,
                    }:
                        break
                    await asyncio.sleep(1)
            else:
                try:
                    client.cancel_order_by_id(order_id)
                    close_status = "buy_order_cancelled"
                except Exception as cancel_exc:
                    close_status = f"cancel_failed:{type(cancel_exc).__name__}"

            after_positions = client.get_all_positions()
            remaining_qty = _position_qty_for_symbol(after_positions, symbol)
            checks.append(
                SmokeCheck(
                    check_name,
                    filled_qty > 0 and "filled" in close_status.lower() and remaining_qty == 0,
                    {
                        "symbol": symbol,
                        "notional": notional,
                        "order_id": order_id,
                        "buy_status": status,
                        "filled_qty": filled_qty,
                        "close_status": close_status,
                        "remaining_qty": remaining_qty,
                        "positions_before": len(before_positions),
                        "positions_after": len(after_positions),
                    },
                    ""
                    if filled_qty > 0 and "filled" in close_status.lower() and remaining_qty == 0
                    else "Roundtrip did not fill and flatten cleanly.",
                )
            )
        except Exception as exc:
            if order_id:
                with contextlib_suppress():
                    client.cancel_order_by_id(order_id)
            checks.append(SmokeCheck(check_name, False, {"order_id": order_id}, _public_error(exc)))
    return checks


async def _ctrader_demo_roundtrip(
    env: dict[str, str],
    *,
    accounts_env: Path,
    symbol: str,
    volume: int,
    timeout_seconds: int,
) -> list[SmokeCheck]:
    from live_runner.account_discovery import discover_ctrader_accounts

    try:
        accounts = await asyncio.wait_for(
            discover_ctrader_accounts(
                env.get("CTRADER_APP_ID", ""),
                env.get("CTRADER_APP_SECRET", ""),
                env.get("CTRADER_ACCESS_TOKEN", ""),
            ),
            timeout=timeout_seconds,
        )
        demo_accounts = [account for account in accounts if account.is_demo]
        if not demo_accounts:
            return [SmokeCheck("ctrader-demo-roundtrip", False, error="No demo account found.")]
        selected = max(demo_accounts, key=lambda account: float(account.balance or 0.0))
        details = await _run_ctrader_order_subprocess(
            account_id=str(selected.ctid or selected.id),
            accounts_env=accounts_env,
            symbol=symbol,
            volume=volume,
            timeout_seconds=timeout_seconds,
        )
        return [SmokeCheck("ctrader-demo-roundtrip", True, details)]
    except Exception as exc:
        return [SmokeCheck("ctrader-demo-roundtrip", False, error=_public_error(exc))]


class _DiscordSmokeAdapter:
    def __init__(self) -> None:
        self.submitted_signal: Any | None = None

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: Any) -> str:
        self.submitted_signal = signal
        return "discord-smoke-order"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100000.0, 100000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return []


async def _discord_replay_smoke() -> list[SmokeCheck]:
    from types import SimpleNamespace

    from live_runner.daemon import _trade_signal_to_live_signal
    from live_runner.daemon import submit_order_with_risk
    from live_runner.signal_sources.discord_source import _parse_trade_signal_from_message
    from shared.models import LiveDeploymentConfig

    channel_id = "discord-smoke-channel"
    message = SimpleNamespace(
        content="**$AAPL** is now oversold - buy signal\nPrice: $190.50",
        id="discord-smoke-message",
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id="discord-smoke-author", bot=False),
        created_at=datetime.now(timezone.utc),
    )
    deployment = LiveDeploymentConfig(
        strategy_name="discord_smoke",
        account_id="alpaca-paper-smoke",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
        signal_config={"channel_id": channel_id},
        settings={"max_risk_per_trade": 0.01, "max_total_exposure": 0.05},
    )
    adapter = _DiscordSmokeAdapter()

    try:
        trade_signal = _parse_trade_signal_from_message(message, default_timeframe="1h")
        if trade_signal is None:
            raise RuntimeError("Discord parser returned no signal.")
        if str(trade_signal.metadata.get("discord_channel_id")) != channel_id:
            raise RuntimeError("Discord parser did not preserve channel routing metadata.")
        if trade_signal.symbol.upper() not in {symbol.upper() for symbol in deployment.symbols}:
            raise RuntimeError("Parsed signal did not match deployment symbols.")

        live_signal = _trade_signal_to_live_signal(trade_signal, deployment)
        broker_order_id = await submit_order_with_risk(
            adapter,
            deployment,
            live_signal,
            account_balance=100000.0,
        )
        submitted = adapter.submitted_signal
        if submitted is None:
            raise RuntimeError("Fake adapter received no submitted signal.")
        qty = float(submitted.metadata.get("qty") or 0.0)
        if qty <= 0:
            raise RuntimeError("Risk sizing produced non-positive quantity.")

        return [
            SmokeCheck(
                "discord-replay-routing",
                True,
                {
                    "channel_id": channel_id,
                    "message_id": str(trade_signal.metadata.get("discord_message_id")),
                    "symbol": submitted.symbol,
                    "action": submitted.action,
                    "qty": qty,
                    "broker_order_id": str(broker_order_id or ""),
                },
            )
        ]
    except Exception as exc:
        return [SmokeCheck("discord-replay-routing", False, error=_public_error(exc))]


def _as_aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.now(timezone.utc)


def _is_alpaca_premarket_eligible(clock: Any, asset: Any) -> tuple[bool, dict[str, Any]]:
    now = _as_aware_datetime(getattr(clock, "timestamp", None))
    now_et = now.astimezone(ZoneInfo("America/New_York"))
    next_open = _as_aware_datetime(getattr(clock, "next_open", None)).astimezone(
        ZoneInfo("America/New_York")
    )
    in_premarket_window = (
        now_et.weekday() < 5
        and datetime_time(4, 0) <= now_et.time() < datetime_time(9, 30)
        and next_open.date() == now_et.date()
    )
    asset_class = str(getattr(asset, "asset_class", "") or "").lower()
    asset_status = str(getattr(asset, "status", "") or "").lower()
    tradable = bool(getattr(asset, "tradable", False))
    equity_asset = "us_equity" in asset_class or "equity" in asset_class
    active_asset = asset_status == "active" or asset_status.endswith(".active")
    eligible = bool(in_premarket_window and tradable and equity_asset and active_asset)
    return eligible, {
        "clock_timestamp": now.isoformat(),
        "clock_timestamp_et": now_et.isoformat(),
        "next_open": next_open.isoformat(),
        "asset_status": asset_status,
        "asset_class": asset_class,
        "asset_tradable": tradable,
        "premarket_window": in_premarket_window,
    }


def _alpaca_stock_prices(api_key: str, secret_key: str, symbol: str) -> dict[str, float]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

    data_client = StockHistoricalDataClient(api_key, secret_key)
    quote_response = data_client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol)
    )
    trade_response = data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol)
    )
    quote = quote_response.get(symbol) if isinstance(quote_response, dict) else None
    trade = trade_response.get(symbol) if isinstance(trade_response, dict) else None
    bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
    ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
    last = float(getattr(trade, "price", 0.0) or 0.0)
    positive_prices = [price for price in (bid, ask, last) if price > 0]
    reference = last or ask or bid
    if reference <= 0:
        raise RuntimeError(f"No positive Alpaca market data price available for {symbol}.")
    buy_limit = round(max(positive_prices) * 1.05, 2)
    sell_limit = round(min(positive_prices) * 0.95, 2)
    if sell_limit <= 0:
        sell_limit = round(reference * 0.97, 2)
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "reference": round(reference, 2),
        "buy_limit": buy_limit,
        "sell_limit": sell_limit,
    }


async def _wait_for_alpaca_order_terminal(client: Any, order_id: str, timeout_seconds: int) -> Any:
    from alpaca.trading.enums import OrderStatus

    terminal_statuses = {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = await asyncio.to_thread(client.get_order_by_id, order_id)
        if getattr(latest, "status", None) in terminal_statuses:
            return latest
        await asyncio.sleep(1)
    if latest is not None:
        return latest
    return await asyncio.to_thread(client.get_order_by_id, order_id)


async def _wait_for_alpaca_position_qty(
    adapter: Any,
    *,
    symbol: str,
    should_be_flat: bool,
    timeout_seconds: int,
) -> float:
    deadline = time.monotonic() + timeout_seconds
    latest_qty = 0.0
    while time.monotonic() < deadline:
        latest_qty = _position_qty_for_symbol(await adapter.get_positions(), symbol)
        if should_be_flat and abs(latest_qty) <= 1e-9:
            return 0.0
        if not should_be_flat and latest_qty > 0:
            return latest_qty
        await asyncio.sleep(1)
    return latest_qty


def _stockkit_route_with_credentials(env: Mapping[str, str]) -> tuple[str, str, tuple[str, str, bool]]:
    from live_runner.alpaca_adapter import resolve_alpaca_credentials
    from live_runner.stockkit_routing import STOCKKIT_ACCOUNT_ROUTES
    from live_runner.stockkit_routing import expected_account_for_stockkit_route

    for route in STOCKKIT_ACCOUNT_ROUTES:
        expected_account = expected_account_for_stockkit_route(route)
        credentials = resolve_alpaca_credentials(
            account_id=expected_account,
            broker_login=expected_account,
            env=env,
        )
        if credentials is not None:
            return route, expected_account, credentials
        for index in (1, 2, 3):
            configured_name = str(env.get(f"ALPACA_ACCOUNT_NAME_ACC{index}", "")).strip()
            if configured_name.replace(" ", "").replace("_", "").lower() != route:
                continue
            broker_login = str(env.get(f"ALPACA_ACCOUNT_NUMBER_ACC{index}", "")).strip()
            credentials = resolve_alpaca_credentials(
                broker_login=broker_login,
                env=env,
            )
            if credentials is not None:
                return route, expected_account, credentials
    raise RuntimeError("No Alpaca credentials matched a configured StockKit route.")


async def _alpaca_stockkit_e2e(
    env: Mapping[str, str],
    *,
    timeout_seconds: int,
) -> SmokeCheck:
    from types import SimpleNamespace

    from alpaca.trading.client import TradingClient
    from live_runner.alpaca_adapter import AlpacaLiveAdapter
    from live_runner.daemon import _trade_signal_to_live_signal
    from live_runner.daemon import submit_order_with_risk
    from live_runner.signal_sources.discord_source import _parse_trade_signal_from_message
    from live_runner.stockkit_routing import validate_stockkit_deployment_route
    from shared.models import LiveDeploymentConfig, LiveSignal

    symbol = "AAPL"
    broker_order_ids: list[str] = []
    details: dict[str, Any] = {
        "status": "not_started",
        "symbol": symbol,
        "broker_order_ids": broker_order_ids,
        "filled_qty": 0.0,
        "remaining_qty": None,
        "market_closed_skipped": False,
        "error": "",
    }
    adapter: AlpacaLiveAdapter | None = None
    buy_order_id = ""
    try:
        route, account_id, credentials = _stockkit_route_with_credentials(env)
        api_key, secret_key, paper = credentials
        details.update({"stockkit_route": route, "account_id": account_id, "paper": paper})
        if not paper:
            raise RuntimeError("Refusing non-paper Alpaca account for live-stockkit-e2e.")

        client = TradingClient(api_key, secret_key, paper=True)
        clock = await asyncio.to_thread(client.get_clock)
        asset = await asyncio.to_thread(client.get_asset, symbol)
        premarket_eligible, market_details = _is_alpaca_premarket_eligible(clock, asset)
        regular_open = bool(getattr(clock, "is_open", False))
        extended_hours = bool(not regular_open and premarket_eligible)
        details.update(
            {
                **market_details,
                "regular_market_open": regular_open,
                "premarket_eligible": premarket_eligible,
                "extended_hours": extended_hours,
            }
        )
        if not regular_open and not premarket_eligible:
            details["status"] = "skipped (market closed)"
            details["market_closed_skipped"] = True
            details["remaining_qty"] = 0.0
            return SmokeCheck("alpaca-stockkit-e2e", True, details)

        stock_prices = await asyncio.to_thread(_alpaca_stock_prices, api_key, secret_key, symbol)
        details["market_data"] = stock_prices
        message_price = stock_prices["reference"]
        message = SimpleNamespace(
            content=f"**$AAPL** is now oversold - buy signal\nPrice: ${message_price:.2f}",
            id=f"stockkit-e2e-{uuid4().hex}",
            channel=SimpleNamespace(id=str(env.get(f"DISCORD_BOT_{route.upper()}_CHANNEL_ID", ""))),
            author=SimpleNamespace(id=f"stockkit-{route}", bot=True),
            created_at=datetime.now(timezone.utc),
        )
        trade_signal = _parse_trade_signal_from_message(message, default_timeframe="1h")
        if trade_signal is None:
            raise RuntimeError("StockKit Discord parser returned no signal.")

        deployment = LiveDeploymentConfig(
            strategy_name="stockkit_discord_e2e",
            account_id=account_id,
            symbols=[symbol],
            timeframe="1h",
            status="ACTIVE",
            signal_source_type="discord",
            signal_source="discord",
            signal_source_id=route.upper(),
            signal_config={"service_name": route, "channel_id": str(message.channel.id)},
            settings={
                "max_risk_per_trade": 0.00004,
                "max_total_exposure": 0.00008,
                "max_positions": 20,
            },
        )
        validate_stockkit_deployment_route(deployment)
        live_signal = _trade_signal_to_live_signal(trade_signal, deployment)
        live_signal.metadata.update({"entry_price": float(trade_signal.entry_price)})
        if extended_hours:
            live_signal.metadata["extended_hours"] = True
            live_signal.metadata["limit_price"] = float(stock_prices["buy_limit"])

        adapter = AlpacaLiveAdapter(
            api_key=api_key,
            secret_key=secret_key,
            paper=True,
            account_id=account_id,
        )
        buy_order_id = await asyncio.wait_for(
            submit_order_with_risk(adapter, deployment, live_signal),
            timeout=timeout_seconds,
        )
        if buy_order_id:
            broker_order_ids.append(str(buy_order_id))
        order = await _wait_for_alpaca_order_terminal(
            adapter._trading_client,
            str(buy_order_id),
            timeout_seconds,
        )
        filled_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
        details["buy_status"] = str(getattr(order, "status", ""))
        details["filled_qty"] = filled_qty
        if filled_qty <= 0:
            raise RuntimeError(f"Alpaca buy did not fill; status={details['buy_status']}")

        position_qty = await _wait_for_alpaca_position_qty(
            adapter,
            symbol=symbol,
            should_be_flat=False,
            timeout_seconds=timeout_seconds,
        )
        if position_qty <= 0:
            raise RuntimeError("Alpaca buy filled but no AAPL position appeared.")

        close_signal = LiveSignal(
            deployment_id=str(deployment.id),
            symbol=symbol,
            action="sell",
            theoretical_price=float(trade_signal.entry_price),
            timestamp=datetime.now(timezone.utc),
            metadata={
                "qty": float(position_qty),
                "explicit_qty": float(position_qty),
                "entry_price": float(trade_signal.entry_price),
                "timeframe": deployment.timeframe,
                "exit_only": True,
                "risk_sizing_mode": "flatten",
                "extended_hours": extended_hours,
            },
        )
        if extended_hours:
            close_signal.metadata["limit_price"] = float(stock_prices["sell_limit"])
        close_order_id = await asyncio.wait_for(
            adapter.submit_order(close_signal),
            timeout=timeout_seconds,
        )
        if close_order_id:
            broker_order_ids.append(str(close_order_id))
        close_order = await _wait_for_alpaca_order_terminal(
            adapter._trading_client,
            str(close_order_id),
            timeout_seconds,
        )
        details["close_status"] = str(getattr(close_order, "status", ""))
        remaining_qty = await _wait_for_alpaca_position_qty(
            adapter,
            symbol=symbol,
            should_be_flat=True,
            timeout_seconds=timeout_seconds,
        )
        details["remaining_qty"] = remaining_qty
        if abs(remaining_qty) > 1e-9:
            raise RuntimeError(f"Alpaca flatten left remaining AAPL qty={remaining_qty}")
        details["status"] = "passed"
        return SmokeCheck("alpaca-stockkit-e2e", True, details)
    except Exception as exc:
        details["status"] = "failed"
        details["error"] = _public_error(exc)
        if adapter is not None and buy_order_id:
            try:
                await asyncio.to_thread(adapter._trading_client.cancel_order_by_id, buy_order_id)
            except Exception:
                pass
            try:
                remaining = _position_qty_for_symbol(await adapter.get_positions(), symbol)
                details["remaining_qty"] = remaining
            except Exception:
                pass
        return SmokeCheck("alpaca-stockkit-e2e", False, details, details["error"])
    finally:
        if adapter is not None:
            try:
                await adapter.disconnect()
            except Exception:
                pass


async def _ctrader_strategy_e2e(
    env: Mapping[str, str],
    *,
    accounts_env: Path,
    timeout_seconds: int,
) -> SmokeCheck:
    from live_runner.account_discovery import discover_ctrader_accounts

    symbol = "EURUSD"
    details: dict[str, Any] = {
        "status": "not_started",
        "symbol": symbol,
        "broker_order_ids": [],
        "filled_volume": 0,
        "remaining_volume": None,
        "error": "",
    }
    try:
        accounts = await asyncio.wait_for(
            discover_ctrader_accounts(
                env.get("CTRADER_APP_ID", ""),
                env.get("CTRADER_APP_SECRET", ""),
                env.get("CTRADER_ACCESS_TOKEN", ""),
            ),
            timeout=timeout_seconds,
        )
        demo_accounts = [account for account in accounts if account.is_demo]
        if not demo_accounts:
            raise RuntimeError("No cTrader demo account found.")
        selected = max(demo_accounts, key=lambda account: float(account.balance or 0.0))
        account_id = str(selected.ctid or selected.id)
        details.update({"account_id": account_id, "balance": float(selected.balance or 0.0)})

        worker_details = await _run_ctrader_strategy_e2e_subprocess(
            account_id=account_id,
            accounts_env=accounts_env,
            timeout_seconds=timeout_seconds,
        )
        details.update(worker_details)
        return SmokeCheck("ctrader-strategy-e2e", True, details)
    except Exception as exc:
        details["status"] = "failed"
        details["error"] = _public_error(exc)
        return SmokeCheck("ctrader-strategy-e2e", False, details, details["error"])


async def _live_stockkit_e2e(
    env: Mapping[str, str],
    *,
    accounts_env: Path,
    timeout_seconds: int,
) -> list[SmokeCheck]:
    results = await asyncio.gather(
        _alpaca_stockkit_e2e(env, timeout_seconds=timeout_seconds),
        _ctrader_strategy_e2e(
            env,
            accounts_env=accounts_env,
            timeout_seconds=timeout_seconds,
        ),
        return_exceptions=True,
    )
    checks: list[SmokeCheck] = []
    for name, result in zip(("alpaca-stockkit-e2e", "ctrader-strategy-e2e"), results, strict=True):
        if isinstance(result, SmokeCheck):
            checks.append(result)
        elif isinstance(result, BaseException):
            checks.append(SmokeCheck(name, False, error=_public_error(result)))
    return checks


class contextlib_suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True


async def run_smoke(
    *,
    mode: str,
    accounts_env: Path,
    alpaca_symbol: str,
    alpaca_notional: float,
    ctrader_symbol: str,
    ctrader_volume: int,
    timeout_seconds: int,
) -> SmokeReport:
    started_at = _utc_now()
    env = _env_values(accounts_env)
    checks: list[SmokeCheck] = []

    normalized_mode = _SMOKE_MODE_ALIASES.get(mode, mode)

    if normalized_mode in {"account-check", "all"}:
        checks.extend(await _alpaca_account_checks(env))
        checks.extend(
            await _ctrader_account_checks(
                env,
                accounts_env=accounts_env,
                timeout_seconds=timeout_seconds,
            )
        )
    if normalized_mode in {"alpaca-crypto-roundtrip", "all"}:
        checks.extend(
            await _alpaca_crypto_roundtrip(
                env,
                symbol=alpaca_symbol,
                notional=alpaca_notional,
                timeout_seconds=timeout_seconds,
            )
        )
    if normalized_mode in {"ctrader-demo-roundtrip", "all"}:
        checks.extend(
            await _ctrader_demo_roundtrip(
                env,
                accounts_env=accounts_env,
                symbol=ctrader_symbol,
                volume=ctrader_volume,
                timeout_seconds=timeout_seconds,
            )
        )
    if normalized_mode in {"discord-replay", "all"}:
        checks.extend(await _discord_replay_smoke())
    if normalized_mode in {"live-stockkit-e2e"}:
        checks.extend(
            await _live_stockkit_e2e(
                env,
                accounts_env=accounts_env,
                timeout_seconds=timeout_seconds,
            )
        )

    return SmokeReport(
        mode=normalized_mode,
        ok=all(check.ok for check in checks) and bool(checks),
        started_at=started_at,
        finished_at=_utc_now(),
        checks=checks,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run live broker smoke checks.")
    parser.add_argument(
        "--mode",
        choices=[
            "account-check",
            "alpaca",
            "alpaca-crypto-roundtrip",
            "ctrader",
            "ctrader-demo-roundtrip",
            "discord-replay",
            "live-stockkit-e2e",
            "all",
            "ctrader-adapter-worker",
            "ctrader-order-worker",
            "ctrader-e2e-worker",
        ],
        default="account-check",
    )
    parser.add_argument("--accounts-env", default=str(DEFAULT_ACCOUNTS_ENV))
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument("--alpaca-symbol", default="BTC/USD")
    parser.add_argument("--alpaca-notional", type=float, default=10.0)
    parser.add_argument("--ctrader-account-id", default="")
    parser.add_argument("--ctrader-symbol", default="EURUSD")
    parser.add_argument(
        "--ctrader-volume",
        type=int,
        default=100000,
        help="cTrader order volume; EURUSD demo minimum observed via Open API is 100000.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args(argv)

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    args.mode = _SMOKE_MODE_ALIASES.get(args.mode, args.mode)
    if args.mode == "ctrader-adapter-worker":
        try:
            payload = asyncio.run(
                _ctrader_adapter_worker(
                    account_id=args.ctrader_account_id,
                    accounts_env=Path(args.accounts_env),
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(json.dumps(payload, sort_keys=True), flush=True)
            os._exit(0)
        except Exception as exc:
            print(json.dumps({"error": _public_error(exc)}, sort_keys=True), flush=True)
            os._exit(1)
    if args.mode == "ctrader-order-worker":
        try:
            payload = asyncio.run(
                _ctrader_order_worker(
                    account_id=args.ctrader_account_id,
                    accounts_env=Path(args.accounts_env),
                    symbol=args.ctrader_symbol,
                    volume=args.ctrader_volume,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(json.dumps(payload, sort_keys=True), flush=True)
            os._exit(0)
        except Exception as exc:
            print(json.dumps({"error": _public_error(exc)}, sort_keys=True), flush=True)
            os._exit(1)
    if args.mode == "ctrader-e2e-worker":
        try:
            payload = asyncio.run(
                _ctrader_strategy_e2e_worker(
                    account_id=args.ctrader_account_id,
                    accounts_env=Path(args.accounts_env),
                    timeout_seconds=args.timeout_seconds,
                )
            )
            print(json.dumps(payload, sort_keys=True), flush=True)
            os._exit(0)
        except Exception as exc:
            print(json.dumps({"error": _public_error(exc)}, sort_keys=True), flush=True)
            os._exit(1)

    report = asyncio.run(
        run_smoke(
            mode=args.mode,
            accounts_env=Path(args.accounts_env),
            alpaca_symbol=args.alpaca_symbol,
            alpaca_notional=args.alpaca_notional,
            ctrader_symbol=args.ctrader_symbol,
            ctrader_volume=args.ctrader_volume,
            timeout_seconds=args.timeout_seconds,
        )
    )
    report_path = _write_report(report, Path(args.reports_dir))
    live_report_path = None
    if report.mode == "live-stockkit-e2e":
        live_report_path = _write_live_integration_report(report)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    print(f"report_path={report_path}")
    if live_report_path is not None:
        print(f"live_integration_report_path={live_report_path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

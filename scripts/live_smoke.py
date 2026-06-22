from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


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


def _public_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _position_qty_for_symbol(positions: list[Any], symbol: str) -> float:
    normalized = symbol.replace("/", "").upper()
    for position in positions:
        position_symbol = str(getattr(position, "symbol", "") or "").replace("/", "").upper()
        if position_symbol == normalized:
            return float(getattr(position, "qty", 0.0) or 0.0)
    return 0.0


def _write_report(report: SmokeReport, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir / f"live_smoke_{report.mode}_{timestamp}.json"
    path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


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
        raise RuntimeError("worker returned no JSON payload")
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
        raise RuntimeError("worker returned no JSON payload")
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
            "explicit_qty": volume / 1000.0,
            "ctrader_volume": volume,
            "entry_price": 1.0,
            "timeframe": "1m",
            "risk_percent": 0.0,
            "risk_amount": 0.0,
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
        "broker_order_id": broker_order_id,
        "position_id": opened_position["position_id"],
        "close_response": type(close_res).__name__,
        "remaining_position": None,
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
                    "broker_order_id": broker_order_id,
                },
            )
        ]
    except Exception as exc:
        return [SmokeCheck("discord-replay-routing", False, error=_public_error(exc))]


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

    if mode in {"account-check", "all"}:
        checks.extend(await _alpaca_account_checks(env))
        checks.extend(
            await _ctrader_account_checks(
                env,
                accounts_env=accounts_env,
                timeout_seconds=timeout_seconds,
            )
        )
    if mode in {"alpaca-crypto-roundtrip", "all"}:
        checks.extend(
            await _alpaca_crypto_roundtrip(
                env,
                symbol=alpaca_symbol,
                notional=alpaca_notional,
                timeout_seconds=timeout_seconds,
            )
        )
    if mode in {"ctrader-demo-roundtrip", "all"}:
        checks.extend(
            await _ctrader_demo_roundtrip(
                env,
                accounts_env=accounts_env,
                symbol=ctrader_symbol,
                volume=ctrader_volume,
                timeout_seconds=timeout_seconds,
            )
        )
    if mode in {"discord-replay", "all"}:
        checks.extend(await _discord_replay_smoke())

    return SmokeReport(
        mode=mode,
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
            "alpaca-crypto-roundtrip",
            "ctrader-demo-roundtrip",
            "discord-replay",
            "all",
            "ctrader-adapter-worker",
            "ctrader-order-worker",
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
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    print(f"report_path={report_path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

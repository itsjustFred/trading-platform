from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping
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
    if target.exists() and not target.is_dir():
        raise ValueError(f"Reports directory path is not a directory: {reports_dir}")
    target.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target / f"live_smoke_{report.mode}_{timestamp}.json"
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
            {
                "accounts": len(accounts),
                "demo_accounts": sum(1 for account in accounts if account.is_demo),
            },
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


def _ensure_ctrader_smoke_deployment(*, account_id: str, symbol: str) -> str:
    """Give the cTrader smoke signal a parent deployment row, and return that row's id.

    The row's ``id`` is a fresh UUID and the descriptive name lives in ``strategy_name``. A
    descriptive primary key is only safe if the model allows it, and ``LiveDeploymentConfig.id``
    is typed ``uuid.UUID`` - so ``ctrader-demo-smoke-<account>`` as the key made the daemon's
    deployment sync fail validation on these rows, and because that failure sits inside a try that
    wraps the whole iteration it stopped the daemon syncing *every* deployment, armed ones included.

    A namespaced ``strategy_name`` and PAUSED, so no runner trades it. Idempotent by
    ``strategy_name``, because that is the only field a readable lookup can key on.

    ``live_deployments.account_id`` carries a foreign key to ``live_accounts``. The smoke is
    demo-only (its adapter is built with ``demo=True``), so on a fresh database - where nothing
    has run account discovery yet - the demo account row is registered here instead of failing
    with a bare FOREIGN KEY error. An account row that already exists is never modified, so the
    smoke cannot relabel or re-flag an account discovery registered.
    """
    from sqlmodel import select

    from shared.database import (
        LiveDeploymentModel,
        get_session,
        init_db,
        register_live_account_if_absent,
    )

    # The worker mode reaches this before anything else touches the database, so on a fresh data
    # dir the tables do not exist yet and the select below would raise "no such table".
    init_db()
    register_live_account_if_absent(
        account_id,
        "ctrader",
        name=f"cTrader demo {account_id} (registered by live_smoke)",
        ctid=account_id,
        is_demo=True,
    )
    strategy_name = f"ctrader-demo-smoke-{account_id}"
    with get_session() as session:
        existing = session.exec(
            select(LiveDeploymentModel).where(
                LiveDeploymentModel.strategy_name == strategy_name
            )
        ).first()
        if existing is not None:
            # A later run may trade a different symbol. The row describes the account, so the new
            # symbol joins the list instead of being dropped - otherwise `symbols` disagrees with
            # the signals the deployment parents.
            symbols = list(existing.symbols_json or [])
            if symbol.upper() not in symbols:
                existing.symbols_json = [*symbols, symbol.upper()]
                session.add(existing)
                session.commit()
            return str(existing.id)
        deployment_id = str(uuid4())
        session.add(
            LiveDeploymentModel(
                id=deployment_id,
                strategy_name=strategy_name,
                account_id=account_id,
                symbols_json=[symbol.upper()],
                timeframe="1m",
                status="PAUSED",
                signal_source="smoke",
                signal_source_type="internal",
            )
        )
        session.commit()
        return deployment_id


#: cTrader reports trendbar prices as integers in 1/100000 of a price unit. This is the same
#: wire unit as the adapter's CTRADER_RELATIVE_PRICE_UNIT_DIVISOR, but the constants stay separate
#: on purpose: a trendbar price is an absolute quote, a relative SL/TP is a distance, and the two
#: scalings could legitimately diverge. A test pins them at the same value so a one-sided edit
#: fails loudly instead of quietly misreading every quote.
_CTRADER_TRENDBAR_PRICE_DIVISOR = 100_000
#: SL/TP distance of the cTrader smoke order, as a fraction of the live price. Chosen wide so a
#: broker stop-distance minimum cannot refuse the smoke order; the position is closed within
#: seconds, so the distance costs nothing either way.
_CTRADER_SMOKE_SL_TP_FRACTION = 0.005
#: How far back to look for the latest M1 bar; a fresh bar older than this means the market is shut.
_CTRADER_SMOKE_QUOTE_LOOKBACK_MINUTES = 30


def _ctrader_smoke_sl_tp(price: float, action: str) -> tuple[float, float]:
    """Real stop-loss and take-profit prices around a live ``price``.

    The adapter refuses an order that is missing either leg, and refuses SL/TP on the wrong side of
    ``theoretical_price`` or closer than one 1/100000 price unit. It cannot tell a plausible
    invented price from a fetched one, so the smoke derives all three from one fetched quote: the
    price the order is sized with and the prices the stops are measured from never disagree.
    """
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Cannot derive smoke SL/TP from price {price!r}.")
    distance = price * _CTRADER_SMOKE_SL_TP_FRACTION
    if action == "buy":
        return round(price - distance, 5), round(price + distance, 5)
    if action == "sell":
        return round(price + distance, 5), round(price - distance, 5)
    raise ValueError(f"Unsupported smoke action: {action!r}")


async def _ctrader_live_reference_price(
    adapter: Any, symbol: str, *, timeout_seconds: int
) -> float:
    """The close of the latest M1 trendbar for ``symbol``, as a real price.

    Fails with a clear message when there is no recent bar (market closed) instead of letting the
    order be built on an invented price.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

    symbol_id = await adapter._resolve_symbol_id(symbol)
    now_ms = int(time.time() * 1000)
    request = ProtoOAGetTrendbarsReq()
    request.ctidTraderAccountId = adapter.ctid_trader_account_id
    request.symbolId = int(symbol_id)
    request.period = ProtoOATrendbarPeriod.M1
    request.fromTimestamp = now_ms - _CTRADER_SMOKE_QUOTE_LOOKBACK_MINUTES * 60_000
    request.toTimestamp = now_ms
    response = await asyncio.wait_for(
        adapter._send_request(request, timeout_seconds=timeout_seconds),
        timeout=timeout_seconds + 5,
    )
    bars = list(getattr(response, "trendbar", []) or [])
    if not bars:
        raise RuntimeError(
            f"No M1 bar for {symbol.upper()} in the last "
            f"{_CTRADER_SMOKE_QUOTE_LOOKBACK_MINUTES} minutes (market closed?); "
            "refusing to build the smoke order on an invented price."
        )
    latest = max(bars, key=lambda bar: int(getattr(bar, "utcTimestampInMinutes", 0)))
    close = (int(latest.low) + int(latest.deltaClose)) / _CTRADER_TRENDBAR_PRICE_DIVISOR
    if close <= 0:
        raise RuntimeError(f"Invalid M1 close {close!r} for {symbol.upper()}.")
    return close


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

    smoke_deployment_id = _ensure_ctrader_smoke_deployment(
        account_id=account_id, symbol=symbol
    )
    reference_price = await _ctrader_live_reference_price(
        adapter, symbol, timeout_seconds=timeout_seconds
    )
    stop_loss, take_profit = _ctrader_smoke_sl_tp(reference_price, "buy")
    signal = LiveSignal(
        deployment_id=smoke_deployment_id,
        symbol=symbol.upper(),
        action="buy",
        theoretical_price=reference_price,
        timestamp=datetime.now(timezone.utc),
        metadata={
            # Unit contract: metadata 'qty' is in qty units (broker volume / 1000);
            # raw broker-unit keys ('ctrader_volume') are no longer read by the
            # adapter (Cleanup 15-D, B-7).
            "qty": float(volume) / 1000.0,
            "entry_price": reference_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timeframe": "1m",
        },
    )
    # C1/B2: adapters size zero times — the smoke qty is explicit intent.
    broker_order_id = await asyncio.wait_for(
        adapter.submit_order(signal, qty=float(volume) / 1000.0), timeout=timeout_seconds
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
        adapter._send_request(close_req, timeout_seconds=20), timeout=25
    )
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
    reference_price = await _ctrader_live_reference_price(
        adapter, symbol, timeout_seconds=timeout_seconds
    )
    stop_loss, take_profit = _ctrader_smoke_sl_tp(reference_price, "buy")
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol=symbol,
        action="buy",
        theoretical_price=reference_price,
        timestamp=datetime.now(timezone.utc),
        metadata={
            # Unit contract: qty is in qty units (broker volume / 1000), i.e.
            # 100000 broker volume == 100.0 qty. The adapter no longer reads
            # raw broker-unit metadata keys (Cleanup 15-D, B-7).
            "qty": 100.0,
            "entry_price": reference_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timeframe": deployment.timeframe,
        },
    )
    broker_order_id = await asyncio.wait_for(
        submit_order_with_risk(adapter, deployment, signal),
        timeout=timeout_seconds,
    )

    async def _best_effort_close_open_position() -> None:
        try:
            leftover_position = await _find_ctrader_open_position(adapter, symbol=symbol)
            if leftover_position is None:
                return
            cleanup_req = ProtoOAClosePositionReq()
            cleanup_req.ctidTraderAccountId = adapter.ctid_trader_account_id
            cleanup_req.positionId = int(leftover_position["position_id"])
            cleanup_req.volume = int(leftover_position["volume"])
            await asyncio.wait_for(
                adapter._send_request(cleanup_req, timeout_seconds=20),
                timeout=25,
            )
        except Exception as cleanup_exc:
            print(
                json.dumps(
                    {
                        "warning": (
                            "cTrader e2e best-effort close failed: "
                            f"{_public_error(cleanup_exc)}"
                        )
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    try:
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
    except BaseException:
        # Never leave the demo position open: the next run refuses to start while
        # an open position exists, so flatten best-effort before propagating.
        await _best_effort_close_open_position()
        raise

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


def _cancel_unfilled_alpaca_order(client: Any, order_id: str) -> str:
    try:
        client.cancel_order_by_id(order_id)
        return "buy_order_cancelled"
    except Exception as cancel_exc:
        return f"cancel_failed:{type(cancel_exc).__name__}"


async def _wait_for_alpaca_fill(
    client: Any,
    order_id: str,
    *,
    timeout_seconds: int,
    initial_status: str,
    initial_filled_qty: float,
) -> tuple[float, str]:
    from alpaca.trading.enums import OrderStatus

    deadline = time.monotonic() + timeout_seconds
    filled_qty = initial_filled_qty
    status = initial_status
    while time.monotonic() < deadline:
        latest = await asyncio.to_thread(client.get_order_by_id, order_id)
        raw_filled = getattr(latest, "filled_qty", None)
        if raw_filled is not None:
            filled_qty = float(raw_filled or 0.0)
        status = str(getattr(latest, "status", status))
        if getattr(latest, "status", None) in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            break
        await asyncio.sleep(1)
    return filled_qty, status


async def _await_alpaca_terminal_status(
    client: Any,
    order_id: str,
    *,
    timeout_seconds: int,
    initial_status: str,
) -> str:
    _filled_qty, status = await _wait_for_alpaca_fill(
        client,
        order_id,
        timeout_seconds=timeout_seconds,
        initial_status=initial_status,
        initial_filled_qty=0.0,
    )
    return status


async def _close_filled_alpaca_position(
    client: Any,
    *,
    symbol: str,
    close_qty: float,
    timeout_seconds: int,
) -> str:
    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    close_order = await asyncio.to_thread(
        client.submit_order,
        MarketOrderRequest(
            symbol=symbol,
            qty=close_qty,
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            client_order_id=f"codex-close-{uuid4().hex[:20]}",
        ),
    )
    return await _await_alpaca_terminal_status(
        client,
        str(getattr(close_order, "id", "")),
        timeout_seconds=timeout_seconds,
        initial_status=str(getattr(close_order, "status", "submitted")),
    )


async def _alpaca_roundtrip_check(
    client: Any,
    *,
    check_name: str,
    symbol: str,
    notional: float,
    timeout_seconds: int,
) -> SmokeCheck:
    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    order_id = ""
    close_status = "not_started"
    try:
        before_positions = await asyncio.to_thread(client.get_all_positions)
        order = await asyncio.to_thread(
            client.submit_order,
            MarketOrderRequest(
                symbol=symbol,
                notional=notional,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                client_order_id=f"codex-smoke-{uuid4().hex[:20]}",
            ),
        )
        order_id = str(getattr(order, "id", ""))
        filled_qty, status = await _wait_for_alpaca_fill(
            client,
            order_id,
            timeout_seconds=timeout_seconds,
            initial_status=str(getattr(order, "status", "")),
            initial_filled_qty=float(getattr(order, "filled_qty", 0.0) or 0.0),
        )

        if filled_qty > 0:
            positions_before_close = await asyncio.to_thread(client.get_all_positions)
            close_qty = _position_qty_for_symbol(positions_before_close, symbol)
            if close_qty <= 0:
                raise RuntimeError("Buy filled but no matching position was available to close.")
            close_status = await _close_filled_alpaca_position(
                client,
                symbol=symbol,
                close_qty=close_qty,
                timeout_seconds=timeout_seconds,
            )
        else:
            close_status = _cancel_unfilled_alpaca_order(client, order_id)

        after_positions = await asyncio.to_thread(client.get_all_positions)
        remaining_qty = _position_qty_for_symbol(after_positions, symbol)
        # Exact terminal-status comparison: `"filled" in close_status` also matched
        # "partially_filled" and misreported partial closes as success. Real fills
        # surface as OrderStatus.FILLED ("filled") or its str() form.
        close_status_key = close_status.lower().rsplit(".", 1)[-1]
        ok = filled_qty > 0 and close_status_key == "filled" and remaining_qty == 0
        return SmokeCheck(
            check_name,
            ok,
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
            if ok
            else "Roundtrip did not fill and flatten cleanly.",
        )
    except Exception as exc:
        if order_id:
            with contextlib_suppress():
                client.cancel_order_by_id(order_id)
        return SmokeCheck(check_name, False, {"order_id": order_id}, _public_error(exc))


async def _run_alpaca_crypto_iteration(
    env: dict[str, str],
    *,
    index: int,
    symbol: str,
    notional: float,
    timeout_seconds: int,
) -> SmokeCheck:
    from alpaca.trading.client import TradingClient

    from live_runner.alpaca_adapter import resolve_alpaca_credentials

    account_name = env.get(f"ALPACA_ACCOUNT_NAME_ACC{index}", f"ACC{index}")
    account_number = env.get(f"ALPACA_ACCOUNT_NUMBER_ACC{index}", "")
    check_name = f"alpaca-{account_name.lower()}-crypto-roundtrip"
    credentials = resolve_alpaca_credentials(broker_login=account_number, env=env)
    if credentials is None:
        return SmokeCheck(check_name, False, error="No matching credentials.")
    api_key, secret_key, paper = credentials
    if not paper:
        return SmokeCheck(check_name, False, error="Refusing non-paper Alpaca account.")

    client = TradingClient(api_key, secret_key, paper=True)
    return await _alpaca_roundtrip_check(
        client,
        check_name=check_name,
        symbol=symbol,
        notional=notional,
        timeout_seconds=timeout_seconds,
    )


async def _alpaca_crypto_roundtrip(
    env: dict[str, str],
    *,
    symbol: str,
    notional: float,
    timeout_seconds: int,
) -> list[SmokeCheck]:
    checks: list[SmokeCheck] = []
    # An async list comprehension: the iterable handed to ``extend`` is a real
    # list. A bare ``await`` inside a generator expression turns the genexp
    # into an async generator, which is not iterable, so ``extend`` would raise
    # TypeError and no roundtrip account would be checked at all.
    checks.extend(
        [
            await _run_alpaca_crypto_iteration(
                env,
                index=index,
                symbol=symbol,
                notional=notional,
                timeout_seconds=timeout_seconds,
            )
            for index in (1, 2, 3)
        ]
    )
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
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    print(f"report_path={report_path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

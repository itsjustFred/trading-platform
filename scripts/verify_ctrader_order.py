#!/usr/bin/env python3
"""Verify a cTrader DEMO order against broker state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_ENV = _ROOT / ".accounts.env"
REPORTS_DIR = _ROOT / "reports"
MAX_ATTEMPTS = 4
ATTEMPT_GAP_S = 30
_REQUIRED_CREDENTIALS = (
    "CTRADER_APP_ID",
    "CTRADER_APP_SECRET",
    "CTRADER_ACCESS_TOKEN",
    "CTRADER_REFRESH_TOKEN",
)


def _resolve_ids(args: argparse.Namespace) -> tuple[int, int]:
    order_raw = getattr(args, "order_id", None) or os.environ.get("CTRADER_VERIFY_ORDER_ID", "")
    account_raw = str(
        getattr(args, "account_id", None) or os.environ.get("CTRADER_VERIFY_ACCOUNT_ID", "")
    )
    try:
        order_id = int(str(order_raw).strip())
        account_id = int(account_raw.strip())
    except (TypeError, ValueError):
        order_id, account_id = 0, 0
    if order_id <= 0 or account_id <= 0:
        raise SystemExit(
            "error: --order-id and --account-id (or CTRADER_VERIFY_ORDER_ID / "
            "CTRADER_VERIFY_ACCOUNT_ID) are required; refusing defaults."
        )
    return order_id, account_id


def _load_credentials() -> dict[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(ACCOUNTS_ENV)
    credentials = {
        key: str(values.get(key) or "").strip()
        for key in _REQUIRED_CREDENTIALS
    }
    missing = [key for key, value in credentials.items() if not value]
    if missing:
        raise SystemExit(
            "error: missing cTrader credentials in .accounts.env: "
            + ", ".join(missing)
        )
    return credentials


def _error_summary(exc: BaseException) -> str:
    from shared.logging import redact_text

    return f"{type(exc).__name__}: {redact_text(str(exc))}"


def _build_adapter(**kwargs: Any) -> Any:
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    return CTraderLiveAdapter(**kwargs)


def _install_reactor() -> None:
    from twisted.internet import asyncioreactor, error

    try:
        asyncioreactor.install()
    except error.ReactorAlreadyInstalledError:
        return


async def verify_once(adapter: Any, order_id: int) -> dict[str, Any]:
    try:
        await adapter.connect()
        request = adapter._ProtoOAOrderDetailsReq()
        request.ctidTraderAccountId = adapter.ctid_trader_account_id
        request.orderId = order_id
        response = await adapter._send_request_with_refresh_retry(request, timeout_seconds=20)
        deals = getattr(response, "deal", [])
        output: dict[str, Any] = {"order_id": order_id, "deals": []}
        for deal in deals:
            output["deals"].append(
                {
                    "deal_id": int(getattr(deal, "dealId", 0) or 0),
                    "deal_status": int(getattr(deal, "dealStatus", 0) or 0),
                    "filled_volume": float(getattr(deal, "filledVolume", 0) or 0),
                    "execution_price": float(getattr(deal, "executionPrice", 0) or 0),
                    "execution_timestamp": int(getattr(deal, "executionTimestamp", 0) or 0),
                }
            )
        try:
            positions = await adapter.get_positions()
            output["open_positions"] = len(positions)
        except Exception as exc:
            output["open_positions_error"] = _error_summary(exc)[:200]
        return output
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass


async def main(order_id: int, account_id: int) -> dict[str, Any]:
    credentials = _load_credentials()
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        adapter = None
        try:
            adapter = _build_adapter(
                app_id=credentials["CTRADER_APP_ID"],
                app_secret=credentials["CTRADER_APP_SECRET"],
                access_token=credentials["CTRADER_ACCESS_TOKEN"],
                refresh_token=credentials["CTRADER_REFRESH_TOKEN"],
                account_id=account_id,
                demo=True,
            )
            print(f"[attempt {attempt}/{MAX_ATTEMPTS}] connecting cTrader DEMO...")
            result = await verify_once(adapter, order_id)
            result["verified"] = True
            result["attempt"] = attempt
            result["verified_at"] = datetime.now(timezone.utc).isoformat()
            print("VERIFIED:", json.dumps(result, indent=2))
            return result
        except Exception as exc:
            last_error = _error_summary(exc)[:200]
            print(f"[attempt {attempt}] failed: {last_error}")
            if attempt < MAX_ATTEMPTS:
                print(f"  retrying in {ATTEMPT_GAP_S}s...")
                await asyncio.sleep(ATTEMPT_GAP_S)
    return {
        "verified": False,
        "order_id": order_id,
        "error": last_error,
        "attempts": MAX_ATTEMPTS,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_report_atomic(payload: dict[str, Any], out_path: str) -> None:
    from shared.atomic_io import atomic_replace

    def _write(target: Path) -> None:
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(target, 0o600)

    atomic_replace(Path(out_path), _write)
    os.chmod(out_path, 0o600)


def _write_report_for_run(payload: dict[str, Any]) -> Path:
    REPORTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(REPORTS_DIR, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = REPORTS_DIR / f"ctrader_order_verification_{stamp}.json"
    _write_report_atomic(payload, str(output_path))
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify a cTrader DEMO order against broker state."
    )
    parser.add_argument("--order-id", type=int, default=0)
    parser.add_argument("--account-id", type=int, default=0)
    parsed = parser.parse_args()
    _order_id, _account_id = _resolve_ids(parsed)
    _install_reactor()
    _result = asyncio.run(main(_order_id, _account_id))
    _report_path = _write_report_for_run(_result)
    print("report:", _report_path)

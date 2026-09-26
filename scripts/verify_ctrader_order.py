#!/usr/bin/env python3
"""Independent cTrader verification.

Reconnects to cTrader DEMO and confirms the executed order/deal exists on
cTrader's own side via ProtoOAOrderDetailsReq (by order id). This is the
cTrader equivalent of Alpaca's get_order_by_id check -- proof that the trade
is real per cTrader, not just per our platform log.

The cTrader DEMO endpoint (demo.ctraderapi.com:5035) is frequently flaky, so
this retries with gaps and writes a JSON report either way.

Usage:
    python scripts/verify_ctrader_order.py
    CTRADER_VERIFY_ORDER_ID=27678592 CTRADER_VERIFY_ACCOUNT_ID=47644617 \
        python scripts/verify_ctrader_order.py
"""
import argparse
import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timezone

# Install the asyncio reactor BEFORE importing the Twisted-backed adapter.
# B3: guarded (only when no reactor is installed yet) instead of unconditional.
from twisted.internet import error as _twisted_error

try:
    from twisted.internet import asyncioreactor

    asyncioreactor.install()
except _twisted_error.ReactorAlreadyInstalledError:
    pass

# B3: no sys.path hack — resolve the repo root and require it on sys.path
# only when missing (e.g. direct script invocation outside `uv run`).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import dotenv_values  # noqa: E402
from live_runner.ctrader_adapter import CTraderLiveAdapter  # noqa: E402

ACCOUNTS_ENV = os.path.join(_ROOT, ".accounts.env")
MAX_ATTEMPTS = 4
ATTEMPT_GAP_S = 30


def _resolve_ids(args: argparse.Namespace) -> tuple[int, int]:
    """Mandatory order/account ids from flags or env — no hardcoded defaults (B3)."""
    order_raw = args.order_id or os.environ.get("CTRADER_VERIFY_ORDER_ID", "")
    account_raw = str(args.account_id or os.environ.get("CTRADER_VERIFY_ACCOUNT_ID", ""))
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


async def verify_once(adapter: CTraderLiveAdapter, order_id: int) -> dict:
    await adapter.connect()
    req = adapter._ProtoOAOrderDetailsReq()
    req.ctidTraderAccountId = adapter.ctid_trader_account_id
    req.orderId = order_id
    res = await adapter._send_request_with_refresh_retry(req, timeout_seconds=20)
    deals = getattr(res, "deal", [])
    out: dict = {"order_id": order_id, "deals": []}
    for d in deals:
        out["deals"].append(
            {
                "deal_id": int(getattr(d, "dealId", 0) or 0),
                "deal_status": int(getattr(d, "dealStatus", 0) or 0),
                "filled_volume": float(getattr(d, "filledVolume", 0) or 0),
                "execution_price": float(getattr(d, "executionPrice", 0) or 0),
                "execution_timestamp": int(getattr(d, "executionTimestamp", 0) or 0),
            }
        )
    try:
        pos = await adapter.get_positions()
        out["open_positions"] = len(pos)
    except Exception as exc:  # pragma: no cover - best effort
        out["open_positions_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    await adapter.disconnect()
    return out


async def main(order_id: int, account_id: int) -> dict:
    env = {str(k): str(v).strip() for k, v in dotenv_values(ACCOUNTS_ENV).items() if k}
    last_err: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        adapter = CTraderLiveAdapter(
            app_id=env["CTRADER_APP_ID"],
            app_secret=env["CTRADER_APP_SECRET"],
            access_token=env["CTRADER_ACCESS_TOKEN"],
            refresh_token=env["CTRADER_REFRESH_TOKEN"],
            account_id=account_id,
            demo=True,
        )
        try:
            print(f"[attempt {attempt}/{MAX_ATTEMPTS}] connecting cTrader DEMO...")
            result = await verify_once(adapter, order_id)
            result["verified"] = True
            result["attempt"] = attempt
            result["verified_at"] = datetime.now(timezone.utc).isoformat()
            print("VERIFIED:", json.dumps(result, indent=2))
            return result
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"[attempt {attempt}] failed: {last_err}")
            try:
                await adapter.disconnect()
            except Exception:
                pass
            if attempt < MAX_ATTEMPTS:
                print(f"  retrying in {ATTEMPT_GAP_S}s...")
                await asyncio.sleep(ATTEMPT_GAP_S)
    return {
        "verified": False,
        "order_id": order_id,
        "error": last_err,
        "attempts": MAX_ATTEMPTS,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_report_atomic(payload: dict, out_path: str) -> None:
    """Write the verification report with owner-only permissions (B3: 0o600)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(out_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(out_path)
        raise


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Verify a cTrader order against broker truth.")
    cli.add_argument("--order-id", type=int, default=0)
    cli.add_argument("--account-id", type=int, default=0)
    cli_args = cli.parse_args()
    _order_id, _account_id = _resolve_ids(cli_args)
    res = asyncio.run(main(_order_id, _account_id))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports_dir = os.path.join(_ROOT, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    out_path = os.path.join(reports_dir, f"ctrader_order_verification_{stamp}.json")
    _write_report_atomic(res, out_path)
    print("report:", out_path)

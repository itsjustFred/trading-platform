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
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# Install the asyncio reactor BEFORE importing the Twisted-backed adapter.
from twisted.internet import asyncioreactor

asyncioreactor.install()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values  # noqa: E402
from live_runner.ctrader_adapter import CTraderLiveAdapter  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_ENV = os.path.join(_ROOT, ".accounts.env")
ORDER_ID = int(os.environ.get("CTRADER_VERIFY_ORDER_ID", "27678592"))
ACCOUNT_ID = int(os.environ.get("CTRADER_VERIFY_ACCOUNT_ID", "47644617"))
MAX_ATTEMPTS = 4
ATTEMPT_GAP_S = 30


async def verify_once(adapter: CTraderLiveAdapter) -> dict:
    await adapter.connect()
    req = adapter._ProtoOAOrderDetailsReq()
    req.ctidTraderAccountId = adapter.ctid_trader_account_id
    req.orderId = ORDER_ID
    res = await adapter._send_request_with_refresh_retry(req, timeout_seconds=20)
    deals = getattr(res, "deal", [])
    out: dict = {"order_id": ORDER_ID, "deals": []}
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


async def main() -> dict:
    env = {str(k): str(v).strip() for k, v in dotenv_values(ACCOUNTS_ENV).items() if k}
    last_err: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        adapter = CTraderLiveAdapter(
            app_id=env["CTRADER_APP_ID"],
            app_secret=env["CTRADER_APP_SECRET"],
            access_token=env["CTRADER_ACCESS_TOKEN"],
            refresh_token=env["CTRADER_REFRESH_TOKEN"],
            account_id=ACCOUNT_ID,
            demo=True,
        )
        try:
            print(f"[attempt {attempt}/{MAX_ATTEMPTS}] connecting cTrader DEMO...")
            result = await verify_once(adapter)
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
        "order_id": ORDER_ID,
        "error": last_err,
        "attempts": MAX_ATTEMPTS,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    res = asyncio.run(main())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(_ROOT, "reports", f"ctrader_order_verification_{stamp}.json")
    with open(out_path, "w") as fh:
        json.dump(res, fh, indent=2)
    print("report:", out_path)

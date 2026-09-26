#!/usr/bin/env python3
"""Verify a cTrader DEMO order against broker state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_ENV = ROOT / ".accounts.env"
REPORTS_DIR = ROOT / "reports"
MAX_ATTEMPTS = 4
ATTEMPT_GAP_S = 30
_REQUIRED_CREDENTIALS = (
    "CTRADER_APP_ID",
    "CTRADER_APP_SECRET",
    "CTRADER_ACCESS_TOKEN",
    "CTRADER_REFRESH_TOKEN",
)


def _install_reactor() -> None:
    from twisted.internet import asyncioreactor, error

    try:
        asyncioreactor.install()
    except error.ReactorAlreadyInstalledError:
        return


def _load_adapter_class() -> type[Any]:
    try:
        _install_reactor()
        from live_runner.ctrader_adapter import CTraderLiveAdapter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Install the optional cTrader extra before running order verification."
        ) from exc
    return CTraderLiveAdapter


def _load_credentials(path: Path | None = None) -> dict[str, str]:
    from dotenv import dotenv_values

    credentials_path = ACCOUNTS_ENV if path is None else path
    values = dotenv_values(credentials_path)
    credentials = {
        key: str(values.get(key) or "").strip()
        for key in _REQUIRED_CREDENTIALS
    }
    missing = [key for key, value in credentials.items() if not value]
    if missing:
        raise SystemExit(
            f"error: missing cTrader credentials in {credentials_path}: "
            + ", ".join(missing)
        )
    return credentials


def _error_summary(exc: BaseException) -> str:
    from shared.logging import redact_text

    return f"{type(exc).__name__}: {redact_text(str(exc))}"


def _build_adapter(adapter_class: type[Any] | None = None, **kwargs: Any) -> Any:
    resolved_adapter_class = adapter_class or _load_adapter_class()
    return resolved_adapter_class(**kwargs)


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
        with contextlib.suppress(Exception):
            await adapter.disconnect()


async def main(
    order_id: int,
    account_id: int,
    adapter_class: type[Any] | None = None,
) -> dict[str, Any]:
    credentials = _load_credentials()
    resolved_adapter_class = adapter_class or _load_adapter_class()
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            adapter = _build_adapter(
                adapter_class=resolved_adapter_class,
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


def _write_report_atomic(payload: dict[str, Any], out_path: str | Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent)
    )
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, out_path)
        os.chmod(out_path, 0o600)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def _write_report_for_run(
    payload: dict[str, Any],
    reports_dir: Path | None = None,
) -> Path:
    target_dir = Path(REPORTS_DIR if reports_dir is None else reports_dir)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = target_dir / f"ctrader_order_verification_{stamp}.json"
    _write_report_atomic(payload, output_path)
    return output_path


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a cTrader order against broker truth.")
    parser.add_argument("--order-id", type=int, default=0)
    parser.add_argument("--account-id", type=int, default=0)
    args = parser.parse_args(argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    order_id, account_id = _resolve_ids(args)
    adapter_class = _load_adapter_class()
    result = asyncio.run(main(order_id, account_id, adapter_class))
    output_path = _write_report_for_run(result, ROOT / "reports")
    print("report:", output_path)
    return 0 if result.get("verified") is True else 1


if __name__ == "__main__":
    raise SystemExit(cli_main())

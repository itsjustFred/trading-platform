from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.credential_env import (
    load_accounts_env_values,
    parse_bool_env,
    resolve_env_value,
)
from shared.models import LiveSignal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"


def _fetch_latest_price(symbol: str, api_key: str, secret_key: str) -> float:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
    except ModuleNotFoundError as exc:
        raise RuntimeError("alpaca-py is required for test_signal_trigger.") from exc
    data_client = StockHistoricalDataClient(api_key, secret_key)
    latest = data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol)
    )
    return float(latest[symbol].price)


async def _run(
    symbol: str,
    deployment_id: str,
    qty: float,
    *,
    live: bool,
    env_values: dict[str, str],
) -> None:
    # C1: credentials resolve file-first via credential_env — never by
    # mutating os.environ with load_dotenv.
    api_key = resolve_env_value(env_values, "ALPACA_API_KEY")
    secret_key = resolve_env_value(env_values, "ALPACA_SECRET_KEY")
    paper_raw = resolve_env_value(env_values, "ALPACA_PAPER")
    paper = (
        None
        if not paper_raw
        else parse_bool_env("ALPACA_PAPER", paper_raw, default=True)
    )
    adapter = AlpacaLiveAdapter(
        api_key=api_key or None,
        secret_key=secret_key or None,
        paper=paper,
        account_id=deployment_id,
        env_values=env_values,
    )
    await adapter.connect()
    assert adapter.api_key
    assert adapter.secret_key
    theoretical_price = _fetch_latest_price(symbol, adapter.api_key, adapter.secret_key)

    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol=symbol,
        action="buy",
        theoretical_price=theoretical_price,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "qty": qty,
            "entry_price": theoretical_price,
            "stop_loss": theoretical_price * 0.98,
            "take_profit": theoretical_price * 1.04,
            "timeframe": "1m",
        },
    )
    if not live:
        # C1: dry run by default — this script can move real money, so it
        # only prints what WOULD be submitted unless --live is passed.
        print(f"dry-run signal_id={signal.id} symbol={symbol} qty={qty}")
        print(f"dry-run theoretical_price={theoretical_price}")
        print("pass --live to submit this signal to the broker")
        return
    # Operator-confirmed intent: --qty is passed as the trusted explicit
    # quantity (binding), not advisory metadata.
    broker_order_id = await adapter.submit_order(signal, qty=qty)
    print(f"signal_id={signal.id}")
    print(f"broker_order_id={broker_order_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trigger one Alpaca live signal for slippage pipeline testing."
    )
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--deployment-id", default="alpaca-paper")
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--accounts-env", default=str(DEFAULT_ACCOUNTS_ENV))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually submit the order. Without this flag, dry-run only.",
    )
    args = parser.parse_args()

    env_values = load_accounts_env_values(Path(args.accounts_env))
    asyncio.run(
        _run(
            args.symbol,
            args.deployment_id,
            args.qty,
            live=args.live,
            env_values=env_values,
        )
    )


if __name__ == "__main__":
    main()

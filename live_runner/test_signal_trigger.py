from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from live_runner.alpaca_adapter import AlpacaLiveAdapter
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


async def _run(symbol: str, deployment_id: str, qty: float) -> None:
    adapter = AlpacaLiveAdapter(account_id=deployment_id)
    await adapter.connect()
    assert adapter.api_key and adapter.secret_key
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
    broker_order_id = await adapter.submit_order(signal)
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
    args = parser.parse_args()

    load_dotenv(Path(args.accounts_env))
    asyncio.run(_run(args.symbol, args.deployment_id, args.qty))


if __name__ == "__main__":
    main()

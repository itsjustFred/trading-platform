#!/usr/bin/env python3
"""Auto-discover trading strategies overnight.

Random parameter search across SMA/RSI/Keltner combinations.
Saves top performers to data/discovered_strategies/ for morning review.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runner.engines.base import BacktestConfig

try:
    from shared.constants import DATA_DIR
except ImportError:
    DATA_DIR = PROJECT_ROOT / "data"

DISCOVERED_DIR = DATA_DIR / "discovered_strategies"
DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
TIMEFRAMES = ["1h", "4h", "1d"]

# Strategy parameter ranges
SMA_FAST_RANGE = (5, 50)
SMA_SLOW_RANGE = (50, 200)
RSI_PERIOD_RANGE = (7, 21)
RSI_OVERSOLD_RANGE = (10, 35)
RSI_OVERBOUGHT_RANGE = (65, 90)
KELTNER_PERIOD_RANGE = (10, 50)
KELTNER_MULT_RANGE = (1.0, 4.0)


def random_sma_params() -> dict:
    """Generate random SMA crossover params."""
    fast = random.randint(*SMA_FAST_RANGE)
    slow = random.randint(max(fast + 10, SMA_SLOW_RANGE[0]), SMA_SLOW_RANGE[1])
    return {"sma_fast": fast, "sma_slow": slow}


def random_rsi_params() -> dict:
    """Generate random RSI params."""
    period = random.randint(*RSI_PERIOD_RANGE)
    oversold = random.randint(*RSI_OVERSOLD_RANGE)
    overbought = random.randint(*RSI_OVERBOUGHT_RANGE)
    return {"rsi_period": period, "rsi_oversold": oversold, "rsi_overbought": overbought}


def random_keltner_params() -> dict:
    """Generate random Keltner Channel params."""
    period = random.randint(*KELTNER_PERIOD_RANGE)
    mult = round(random.uniform(*KELTNER_MULT_RANGE), 1)
    return {"keltner_period": period, "keltner_mult": mult}


def run_discovery_batch(n_trials: int = 10) -> list[dict]:
    """Run random strategy discovery."""
    results = []

    for i in range(n_trials):
        symbol = random.choice(SYMBOLS)
        tf = random.choice(TIMEFRAMES)

        # Pick random strategy type
        strategy_types = ["sma", "rsi", "keltner"]
        strat_type = random.choice(strategy_types)

        params = {}
        if strat_type == "sma":
            params = random_sma_params()
        elif strat_type == "rsi":
            params = random_rsi_params()
        elif strat_type == "keltner":
            params = random_keltner_params()

        try:
            config = BacktestConfig(
                strategy_name=f"{strat_type}_cross",  # Simplified
                symbols=[symbol],
                timeframe=tf,
                start_date="2023-01-01",
                end_date="2024-12-31",
                initial_capital=100000.0,
                risk_per_trade_pct=0.01,
            )
            # Note: Real implementation would need actual strategy files
            # This is experimental skeleton
            result = {
                "id": f"disc_{i:04d}",
                "strategy": f"{strat_type}",
                "symbol": symbol,
                "timeframe": tf,
                "params": params,
                "status": "mock",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(result)
        except Exception as e:
            results.append({
                "id": f"disc_{i:04d}",
                "strategy": strat_type,
                "error": str(e),
            })

    # Save results
    out_path = DISCOVERED_DIR / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    out_path.write_text(json.dumps(results, indent=2))

    return results


if __name__ == "__main__":
    print("Overnight Strategy Discovery - Experimental")
    print("=" * 50)

    results = run_discovery_batch(n_trials=20)
    print(f"Completed {len(results)} trial configurations")
    print(f"Results saved to {DISCOVERED_DIR}")
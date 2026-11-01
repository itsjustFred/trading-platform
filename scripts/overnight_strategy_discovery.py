#!/usr/bin/env python3
"""Auto-discover trading strategy parameter candidates (experimental).

Random parameter search across SMA/RSI/Keltner combinations. The generated
rows are candidates with ``"status": "mock"`` — no backtest has been run for
them. They are NOT written to the discovery directory consumed by
``scripts/run_discovery_grid_backtests.py`` unless ``--publish`` is passed
explicitly, because the real grid runner treats everything it finds there as
an executable discovery.
"""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.overnight_strategy_discovery
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("overnight_strategy_discovery")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from runner.engines.base import BacktestConfig
from shared.logging import get_logger

try:
    from shared.constants import DATA_DIR
except ImportError:
    DATA_DIR = PROJECT_ROOT / "data"

logger = get_logger("scripts.discovery")

DISCOVERED_DIR = DATA_DIR / "discovered_strategies"

# Parameter sampling uses the OS CSPRNG (``secrets.SystemRandom``) instead of the
# ``random`` module default: the search space is small, and reproducibility of a
# candidate generator is worthless — but Sonar/auditors flag the Mersenne Twister
# (S2245) in every security scan, which masks real findings.
_RNG = secrets.SystemRandom()

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
    fast = _RNG.randint(*SMA_FAST_RANGE)
    slow = _RNG.randint(max(fast + 10, SMA_SLOW_RANGE[0]), SMA_SLOW_RANGE[1])
    return {"sma_fast": fast, "sma_slow": slow}


def random_rsi_params() -> dict:
    """Generate random RSI params."""
    period = _RNG.randint(*RSI_PERIOD_RANGE)
    oversold = _RNG.randint(*RSI_OVERSOLD_RANGE)
    overbought = _RNG.randint(*RSI_OVERBOUGHT_RANGE)
    return {"rsi_period": period, "rsi_oversold": oversold, "rsi_overbought": overbought}


def random_keltner_params() -> dict:
    """Generate random Keltner Channel params."""
    period = _RNG.randint(*KELTNER_PERIOD_RANGE)
    mult = round(_RNG.uniform(*KELTNER_MULT_RANGE), 1)
    return {"keltner_period": period, "keltner_mult": mult}


def run_discovery_batch(n_trials: int = 10, publish: bool = False) -> list[dict]:
    """Generate random trial candidates.

    Results are candidates only (``status: mock``); no backtest is executed.
    They are only written to ``DISCOVERED_DIR`` when ``publish=True`` — the
    real grid runner consumes that directory and would otherwise treat mock
    rows as executable discoveries.
    """
    results = []

    for i in range(n_trials):
        symbol = _RNG.choice(SYMBOLS)
        tf = _RNG.choice(TIMEFRAMES)

        # Pick random strategy type
        strategy_types = ["sma", "rsi", "keltner"]
        strat_type = _RNG.choice(strategy_types)

        params = {}
        if strat_type == "sma":
            params = random_sma_params()
        elif strat_type == "rsi":
            params = random_rsi_params()
        elif strat_type == "keltner":
            params = random_keltner_params()

        try:
            BacktestConfig(
                strategy_name=f"{strat_type}_cross",  # Simplified
                symbols=[symbol],
                timeframe=tf,
                start_date="2023-01-01",
                end_date="2024-12-31",
                initial_capital=100000.0,
                risk_per_trade_pct=0.01,
            )
            # Note: Real implementation would need actual strategy files.
            # This is an experimental skeleton; rows stay marked as mock.
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
                "status": "mock",
                "error": str(e),
            })

    if publish:
        DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
        out_path = (
            DISCOVERED_DIR
            / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
        )
        out_path.write_text(json.dumps(results, indent=2))

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=20, help="Number of trial candidates")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Write the candidates to the discovery directory consumed by the grid runner.",
    )
    args = parser.parse_args()

    logger.info("Overnight strategy discovery started")
    results = run_discovery_batch(n_trials=args.trials, publish=args.publish)
    logger.info(
        "Overnight strategy discovery finished",
        extra={
            "data": {
                "trials": len(results),
                "published": args.publish,
                "output_dir": str(DISCOVERED_DIR) if args.publish else None,
            }
        },
    )
    if not args.publish:
        logger.info("Dry run: results were NOT published (pass --publish to write them)")

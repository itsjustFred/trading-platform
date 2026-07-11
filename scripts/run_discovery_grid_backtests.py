#!/usr/bin/env python3
"""Run the latest discovery grid trials as real vectorbt backtests.

The script intentionally does not depend on an LLM. It reads the discovery JSON,
maps discovered strategies to executable repository strategies, waits for the
required data files, and runs runner.cli with explicit --params and --output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = Path(os.environ.get("DISCOVERY_PATH", "/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json"))
DATA_DIR = PROJECT_ROOT / "data"
BACKTEST_DIR = PROJECT_ROOT / "data" / "backtests"
LOG_DIR = PROJECT_ROOT / "logs"

STRATEGY_PATHS = {
    "sma": "strategies/example_sma/example_sma.py",
    "rsi": "strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py",
    "keltner": "strategies/custom_swings_supertrend/custom_swings_supertrend.py",
}


def load_trials(discovery_path: Path = DISCOVERY_PATH) -> list[dict[str, Any]]:
    with discovery_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def data_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / "ohlcv" / "yfinance" / "crypto" / "USDT" / symbol / f"{timeframe}.parquet"


def wait_for_data(symbol: str, timeframe: str, timeout_seconds: int = 1800) -> tuple[bool, str]:
    path = data_path(symbol, timeframe)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            try:
                df = pd.read_parquet(path)
                if len(df) > 0:
                    return True, str(path)
            except Exception:
                pass
        time.sleep(10)
    return False, str(path)


def date_bounds(symbol: str, timeframe: str) -> tuple[str, str]:
    path = data_path(symbol, timeframe)
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    start = df["timestamp"].min().strftime("%Y-%m-%d")
    end = df["timestamp"].max().strftime("%Y-%m-%d")
    return start, end


def params_for(trial: dict[str, Any]) -> dict[str, Any]:
    strategy = trial["strategy"]
    p = trial.get("params", {})
    if strategy == "sma":
        return {"fast_length": int(p["sma_fast"]), "slow_length": int(p["sma_slow"])}
    if strategy == "rsi":
        return {
            "rsi_period": int(p["rsi_period"]),
            "rsi_entry": int(p["rsi_oversold"]),
            "rsi_exit": int(p["rsi_overbought"]),
            "bb_period": 20,
            "bb_std": 2.0,
            "max_hold": 15,
            "atr_period": 14,
            "atr_stop_mult": 2.0,
        }
    if strategy == "keltner":
        return {
            "st_period": int(p["keltner_period"]),
            "st_multiplier": float(p["keltner_mult"]),
            "ema_fast": 21,
            "ema_slow": 120,
            "ema_filter_enabled": False,
            "use_rrr": False,
            "use_breakeven": True,
            "use_trailing_sl": True,
            "trade_direction": "both",
        }
    raise ValueError(f"Unsupported discovered strategy: {strategy}")


def run_trial(trial: dict[str, Any]) -> dict[str, Any]:
    trial_id = trial["id"]
    strategy = trial["strategy"]
    symbol = trial["symbol"]
    timeframe = trial["timeframe"]
    output = BACKTEST_DIR / f"grid_{strategy}_{trial_id}.trades.json"
    log_path = LOG_DIR / f"grid_{strategy}_{trial_id}.log"
    started = datetime.now(timezone.utc).isoformat()
    result = {
        "id": trial_id,
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "pending",
        "started_at": started,
        "output": str(output),
        "log": str(log_path),
    }

    have_data, data_file = wait_for_data(symbol, timeframe)
    if not have_data:
        result.update({"status": "skipped", "reason": f"data unavailable: {data_file}", "finished_at": datetime.now(timezone.utc).isoformat()})
        return result

    start, end = date_bounds(symbol, timeframe)
    params = params_for(trial)
    cmd = [
        sys.executable,
        "-m",
        "runner.cli",
        "run",
        "--engine",
        "vectorbt",
        "--strategy",
        str(PROJECT_ROOT / STRATEGY_PATHS[strategy]),
        "--symbols",
        symbol,
        "--timeframe",
        timeframe,
        "--start",
        start,
        "--end",
        end,
        "--params",
        json.dumps(params),
        "--output",
        str(output),
        "--progress-stdout",
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # Remove any stale output from a previous run so we never parse old results.
    if output.exists():
        output.unlink()

    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
    except subprocess.TimeoutExpired:
        result.update(
            {
                "status": "failed",
                "returncode": None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "timed out after 900s",
                "cmd": " ".join(cmd),
            }
        )
        return result

    stats = {}
    if proc.returncode == 0 and output.exists():
        try:
            with output.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            trades = payload.get("trades", [])
            stats = {
                "trades": len(trades),
                "total_pnl": payload.get("stats", {}).get("total_pnl"),
                "sharpe": payload.get("stats", {}).get("sharpe"),
            }
        except Exception as exc:
            result["parse_error"] = str(exc)
    elif proc.returncode != 0:
        result["reason"] = result.get("reason") or "subprocess failed"

    result.update(
        {
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "stats": stats,
            "cmd": " ".join(cmd),
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run discovery grid trials as real vectorbt backtests."
    )
    parser.add_argument(
        "--discovery-path",
        type=Path,
        default=DISCOVERY_PATH,
        help="Path to the discovery JSON file describing the trials to run.",
    )
    args = parser.parse_args()

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    trials = load_trials(args.discovery_path)
    summary_started = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    for trial in trials:
        result = run_trial(trial)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    summary = {
        "started_at": summary_started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "requested": len(trials),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "results": results,
    }
    (LOG_DIR / "grid_backtests_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

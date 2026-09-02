"""Backtest-target resolution and artifact path building for the auto agent.

Picks the validation target (symbols/timeframe/date range) from the local
OHLCV catalog and computes the per-strategy artifact paths (strategy file,
README, backtest output).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from data_tools.catalog_builder import scan_ohlcv
from runner.cli import _detect_date_range
from shared.constants import DATA_DIR
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
BACKTESTS_DIR = DATA_DIR / "backtests"

TIMEFRAME_RANK = {
    "1h": 0,
    "4h": 1,
    "1d": 2,
    "30m": 3,
    "15m": 4,
    "5m": 5,
    "1m": 6,
}
ASSET_CLASS_RANK = {
    "equities": 0,
    "equity": 0,
    "etf": 1,
    "etfs": 1,
    "forex": 2,
    "indices": 3,
    "crypto": 4,
}
SYMBOL_RANK = {
    "AAPL": 0,
    "SPY": 1,
    "MSFT": 2,
    "QQQ": 3,
    "EURUSD": 4,
    "XAUUSD": 5,
    "BTCUSD": 6,
}


@dataclass
class BacktestTarget:
    symbols: list[str]
    timeframe: str
    start: str
    end: str


@dataclass(frozen=True)
class StrategyArtifacts:
    resolved_name: str
    engine: str
    strategy_dir: Path
    strategy_path: Path
    documentation_path: Path
    output_path: Path


def build_strategy_artifacts(*, resolved_name: str, engine: str) -> StrategyArtifacts:
    strategy_dir = STRATEGIES_DIR / resolved_name
    return StrategyArtifacts(
        resolved_name=resolved_name,
        engine=engine,
        strategy_dir=strategy_dir,
        strategy_path=strategy_dir / f"{resolved_name}.py",
        documentation_path=strategy_dir / "README.md",
        output_path=BACKTESTS_DIR
        / f"{resolved_name}_auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.trades.json",
    )


def resolve_backtest_target(
    *,
    symbols: list[str] | None,
    timeframe: str | None,
    start: str | None,
    end: str | None,
) -> BacktestTarget:
    normalized_symbols = [
        str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip()
    ]
    normalized_timeframe = (timeframe or "").strip().lower()

    if not normalized_symbols or not normalized_timeframe:
        entries = _load_ohlcv_entries()
        if not entries:
            raise RuntimeError(
                "No local OHLCV data found. Download data before using the auto-convert agent."
            )

        filtered = entries
        if normalized_symbols:
            filtered = [
                entry
                for entry in filtered
                if str(entry.get("symbol", "")).strip().upper() in normalized_symbols
            ]
        if normalized_timeframe:
            filtered = [
                entry
                for entry in filtered
                if str(entry.get("timeframe", "")).strip().lower() == normalized_timeframe
            ]
        if not filtered:
            raise RuntimeError(
                "No matching OHLCV dataset found for the requested auto-agent target."
            )

        selected = min(filtered, key=_entry_rank)
        if not normalized_symbols:
            normalized_symbols = [str(selected.get("symbol", "")).strip().upper()]
        if not normalized_timeframe:
            normalized_timeframe = str(selected.get("timeframe", "")).strip().lower()

    detected_start, detected_end = _detect_date_range(
        normalized_symbols,
        normalized_timeframe,
        str(DATA_DIR),
    )
    return BacktestTarget(
        symbols=normalized_symbols,
        timeframe=normalized_timeframe,
        start=(start or detected_start).strip(),
        end=(end or detected_end).strip(),
    )


def _load_ohlcv_entries() -> list[dict]:
    catalog_path = DATA_DIR / "catalog.json"
    if catalog_path.exists():
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            entries = payload.get("ohlcv", []) if isinstance(payload, dict) else []
            if isinstance(entries, list) and entries:
                return [entry for entry in entries if isinstance(entry, dict)]
        except Exception:
            pass
    return scan_ohlcv(DATA_DIR)


def _entry_rank(entry: dict) -> tuple[int, int, int, str, str]:
    asset_class = str(entry.get("asset_class", "")).strip().lower()
    timeframe = str(entry.get("timeframe", "")).strip().lower()
    symbol = str(entry.get("symbol", "")).strip().upper()
    source = str(entry.get("source", "")).strip().lower()
    return (
        ASSET_CLASS_RANK.get(asset_class, 99),
        TIMEFRAME_RANK.get(timeframe, 99),
        SYMBOL_RANK.get(symbol, 99),
        source,
        symbol,
    )

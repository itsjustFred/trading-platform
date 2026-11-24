"""Shared deterministic fixtures for the Cleanup 16-D1 engine-behavior tests.

Single source of the trend/consolidation OHLCV series, the bundled SMA
crossover strategy, and the OHLCV-on-disk helpers used by
``test_adapter_curve_semantics.py``, ``test_vectorbt_sizing_semantics.py``
and ``tests/strategies/contract_sma.py``-style conformance tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

#: Opt-in dataset root for locally run, dataset-backed tests (see the strategy
#: data gate in ``tests/strategies/conftest.py``). Read-only by design, so
#: pointing it at a real dataset cannot trip the repo data-dir guard.
TEST_OHLCV_DIR_ENV = "TP_TEST_OHLCV_DIR"


def resolve_ohlcv_root(data_dir: Path) -> Path:
    """Directory holding ``<symbol>/<timeframe>.parquet`` shards.

    The explicit ``TP_TEST_OHLCV_DIR`` opt-in when one is exported (what
    ``AGENTS.md`` documents for local dataset-backed runs), else
    ``<data_dir>/ohlcv``. The suite's ``DATA_DIR`` is a throwaway temp
    directory (``tests/conftest.py``), so without the opt-in no local dataset
    is visible and such a test must skip naming the variable.
    """
    override = str(os.environ.get(TEST_OHLCV_DIR_ENV, "")).strip()
    return Path(override) if override else Path(data_dir) / "ohlcv"


#: Crossover strategy shared by the sizing/curve regression tests.
SMA_CROSSOVER_STRATEGY = """
import pandas as pd

STRATEGY_META = {"name": "__NAME__", "version": "1.0",
                 "parameters": {"fast_length": 3, "slow_length": 10}}

def generate_signals(df, params):
    close = df["close"]
    fast = close.rolling(int(params.get("fast_length", 3))).mean()
    slow = close.rolling(int(params.get("slow_length", 10))).mean()
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    indicators = {"direction": pd.Series("LONG", index=df.index)}
    return entries, exits, indicators
"""


def sma_crossover_strategy_text(name: str = "sma_cross", *, with_stop_loss: bool = False) -> str:
    """Strategy text, optionally emitting a static stop_loss indicator."""
    text = SMA_CROSSOVER_STRATEGY.replace("__NAME__", name)
    if with_stop_loss:
        text = text.replace(
            '    indicators = {"direction": pd.Series("LONG", index=df.index)}\n',
            '    indicators = {\n'
            '        "direction": pd.Series("LONG", index=df.index),\n'
            '        "stop_loss": pd.Series([df["close"].iloc[0] * 0.95] * len(df), index=df.index),\n'
            '    }\n',
        )
    return text


def deterministic_ohlcv_frame(*, periods: int = 300, start: float = 100.0) -> pd.DataFrame:
    """Alternating trend/consolidation series producing SMA-crossover trades.

    Deterministic (no random component): 3 cycles of 30 up-bars (+0.5),
    20 flat, 30 down (−0.5), 20 flat, truncated to ``periods``.
    """
    closes: list[float] = []
    price = start
    for _ in range(3):
        for _ in range(30):
            price += 0.5
            closes.append(price)
        closes.extend(price for _ in range(20))
        for _ in range(30):
            price -= 0.5
            closes.append(price)
        closes.extend(price for _ in range(20))
    closes = closes[:periods]
    timestamps = pd.date_range("2024-01-01", periods=len(closes), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def write_ohlcv(
    data_dir, symbol: str, frame: pd.DataFrame, *, timeframe: str = "1h",
    source: str = "test_source", asset_class: str = "equities",
) -> None:
    """Write ``frame`` as the OHLCV parquet for ``symbol`` under ``data_dir``."""
    symbol_dir = data_dir / "ohlcv" / source / asset_class / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(symbol_dir / f"{timeframe}.parquet", index=False)


def with_zero_fees(payload: dict) -> dict:
    """Merge an explicit no-cost declaration into engine-test params (#84, #130).

    Shared by engine contract/parity tests whose fixtures assert
    gross-equivalent numbers: declaring ``fees="zero"`` and
    ``slippage="none"`` keeps the numbers identical while satisfying the
    mandatory cost-model gates. (The name predates #130; it now declares a
    fully cost-free run, not just fees.)
    """
    merged = dict(payload)
    params = dict(merged.get("parameters") or {})
    params.setdefault("fees", "zero")
    params.setdefault("slippage", "none")
    merged["parameters"] = params
    return merged

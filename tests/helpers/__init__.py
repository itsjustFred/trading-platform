"""Shared deterministic fixtures for the Cleanup 16-D1 engine-behavior tests.

Single source of the trend/consolidation OHLCV series, the bundled SMA
crossover strategy, and the OHLCV-on-disk helpers used by
``test_adapter_curve_semantics.py``, ``test_vectorbt_sizing_semantics.py``
and ``tests/strategies/contract_sma.py``-style conformance tests.
"""

from __future__ import annotations

import pandas as pd


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
        for _ in range(20):
            closes.append(price)
        for _ in range(30):
            price -= 0.5
            closes.append(price)
        for _ in range(20):
            closes.append(price)
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
    """Merge an explicit no-cost fee declaration into engine-test params (#84).

    Shared by engine contract/parity tests whose fixtures assert
    gross-equivalent numbers: declaring ``fees="zero"`` keeps the numbers
    identical while satisfying the mandatory fee-config gate.
    """
    merged = dict(payload)
    params = dict(merged.get("parameters") or {})
    params.setdefault("fees", "zero")
    merged["parameters"] = params
    return merged

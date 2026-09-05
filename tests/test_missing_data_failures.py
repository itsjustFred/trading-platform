from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY = """
import pandas as pd

STRATEGY_META = {"name": "sma_demo", "parameters": {"window": 3}}


def generate_signals(df: pd.DataFrame, params: dict):
    close = df["close"]
    sma = close.rolling(int(params.get("window", 3))).mean()
    entries = close > sma
    exits = close < sma
    return entries, exits, {"sma": sma}
"""


def _config(tmp_path: Path, symbols: list[str]) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="sma_demo",
        symbols=symbols,
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-02-01",
        initial_capital=100_000.0,
        data_dir=str(tmp_path / "data"),
    )


def _write_strategy(tmp_path: Path) -> Path:
    strategy = tmp_path / "sma_demo.py"
    strategy.write_text(STRATEGY, encoding="utf-8")
    return strategy


def _write_ohlcv(tmp_path: Path, symbol: str) -> None:
    path = tmp_path / "data" / "ohlcv" / "test" / symbol / "1d.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = pd.date_range("2024-01-02", periods=20, freq="D", tz="UTC")
    close = [100.0 + i for i in range(len(stamps))]
    pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000.0] * len(stamps),
        }
    ).to_parquet(path, index=False)


@pytest.mark.parametrize("adapter_factory", [VectorbtAdapter, NativeAdapter])
def test_missing_symbol_data_fails_loudly(tmp_path, adapter_factory):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")  # QQQ deliberately missing

    adapter = adapter_factory()
    config = _config(tmp_path, ["QQQ"])
    with pytest.raises(RuntimeError, match="QQQ"):
        adapter.run(config, str(strategy))


def test_vectorbt_partial_missing_symbol_fails_loudly(tmp_path):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")  # QQQ deliberately missing

    adapter = VectorbtAdapter()
    config = _config(tmp_path, ["SPY", "QQQ"])
    with pytest.raises(RuntimeError, match="QQQ"):
        adapter.run(config, str(strategy))


class _FailingSymbolProcessor:
    """Module-level stand-in for _process_symbol: ProcessPoolExecutor needs a
    picklable callable, so the failing symbol raises inside the worker."""

    def __call__(self, symbol, config, strategy_path, resolved_parameters, data_root):
        if symbol == "QQQ":
            raise RuntimeError("No OHLCV data for QQQ/1d")
        return [], [], [], [], 0


def test_vectorbt_parallel_branch_preserves_loud_failure(monkeypatch):
    """Pre-mortem hardening (16-B #3): in the ProcessPoolExecutor branch a
    per-symbol failure must still raise, not degrade into a partial success."""
    from types import SimpleNamespace

    import runner.engines.vectorbt_adapter as module

    monkeypatch.setattr(module, "_process_symbol", _FailingSymbolProcessor())
    monkeypatch.setattr(module, "resolve_strategy_parameters", lambda *a, **k: {})
    monkeypatch.setattr(
        module, "_load_strategy_module", lambda path: SimpleNamespace(generate_signals=None)
    )

    config = _config(Path("/tmp"), ["SPY", "QQQ"])
    adapter = VectorbtAdapter()
    with pytest.raises(RuntimeError, match="QQQ") as exc_info:
        adapter.run(config, "unused.py")
    assert "No OHLCV data" in str(exc_info.value.__cause__)

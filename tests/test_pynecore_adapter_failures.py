from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import PyneCoreAdapter


def _write_ohlcv(tmp_path: Path, symbol: str) -> None:
    path = tmp_path / "data" / "ohlcv" / "test" / symbol / "1d.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = pd.date_range("2024-01-02", periods=20, freq="D", tz="UTC")
    pd.DataFrame(
        {
            "timestamp": stamps,
            "open": [100.0] * len(stamps),
            "high": [101.0] * len(stamps),
            "low": [99.0] * len(stamps),
            "close": [100.5] * len(stamps),
            "volume": [1000.0] * len(stamps),
        }
    ).to_parquet(path, index=False)


def _config(tmp_path: Path, symbols: list[str]) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="pyne_demo",
        symbols=symbols,
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-02-01",
        initial_capital=100_000.0,
        data_dir=str(tmp_path / "data"),
    )


def _write_strategy(tmp_path: Path) -> Path:
    strategy = tmp_path / "pyne_demo.py"
    strategy.write_text("STRATEGY_META = {'name': 'pyne_demo'}\n", encoding="utf-8")
    return strategy


def test_missing_pyne_binary_fails_loudly(tmp_path, monkeypatch):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")
    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.shutil.which", lambda name: None
    )
    import sys as _sys
    monkeypatch.setattr(
        _sys, "executable", str(tmp_path / "python-without-pyne" / "bin" / "python")
    )

    adapter = PyneCoreAdapter()
    config = _config(tmp_path, ["SPY"])
    with pytest.raises(RuntimeError, match="pyne"):
        adapter.run(config, str(strategy))


def test_failed_pyne_run_fails_loudly(tmp_path, monkeypatch):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")
    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.shutil.which", lambda name: "/usr/bin/pyne"
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.subprocess.run", fake_run
    )

    adapter = PyneCoreAdapter()
    config = _config(tmp_path, ["SPY"])
    with pytest.raises(RuntimeError, match="pyne_demo"):
        adapter.run(config, str(strategy))


def test_pyne_timeout_is_reported_as_failure(tmp_path, monkeypatch):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")
    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.shutil.which", lambda name: "/usr/bin/pyne"
    )

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pyne", timeout=300)

    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.subprocess.run", fake_run
    )

    adapter = PyneCoreAdapter()
    config = _config(tmp_path, ["SPY"])
    with pytest.raises(RuntimeError, match=r"SPY.*timed out after 300 seconds"):
        # The raise must be self-contained (symbol + timeout in str(exc)): the
        # logger.exception side channel is a superset, never the only carrier
        # of the failure detail (pre-mortem 16-B #10).
        adapter.run(config, str(strategy))


def test_stats_are_merged_across_symbols(tmp_path, monkeypatch):
    strategy = _write_strategy(tmp_path)
    _write_ohlcv(tmp_path, "SPY")
    _write_ohlcv(tmp_path, "QQQ")
    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.shutil.which", lambda name: "/usr/bin/pyne"
    )

    seen_symbols: list[str] = []

    def fake_run(*args, **kwargs):
        # both symbols succeed; each writes its own stats so the adapter must
        # merge them (the old all_stats = ... overwrite would keep only QQQ's).
        # Every symbol also writes a symbol-unique key: shared keys alone cannot
        # distinguish merge (`update`) from overwrite (`=`) — the overwrite would
        # keep QQQ's values for the shared keys and still satisfy the assertions
        # below. `spy_only` surviving is the actual merge pin.
        cmd = args[0]
        data_csv = Path(cmd[cmd.index("--trade") - 1])
        symbol = data_csv.name.split("_")[0]
        seen_symbols.append(symbol)
        stats_csv = Path(cmd[cmd.index("--strat") + 1])
        stats_csv.write_text(
            "metric,value\n"
            + f"total_pnl,{100.0 if symbol == 'SPY' else 200.0}\n"
            + f"sharpe_ratio,{1.0 if symbol == 'SPY' else 2.0}\n"
            + (f"spy_only,42.0\n" if symbol == "SPY" else "qqq_only,7.0\n"),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "runner.engines.pynecore_adapter.subprocess.run", fake_run
    )

    result = PyneCoreAdapter().run(_config(tmp_path, ["SPY", "QQQ"]), str(strategy))
    assert result.meta.engine == "pynecore"
    # Both symbols ran and both contributed: the per-symbol stats are merged
    # (never overwritten by the last symbol) and the symbol-unique keys from
    # both symbols survive. Canonical keys (total_pnl, sharpe_ratio) are then
    # recomputed from the merged trades/equity curve and win a collision with
    # any per-symbol CSV value of the same name (contract §6), so they carry
    # the derived values rather than the CSV literals.
    assert sorted(seen_symbols) == ["QQQ", "SPY"]
    assert result.stats["spy_only"] == 42.0
    assert result.stats["qqq_only"] == 7.0
    assert result.stats["total_pnl"] != 100.0  # merged, not last-symbol-wins
    assert result.stats["sharpe_ratio"] != 1.0

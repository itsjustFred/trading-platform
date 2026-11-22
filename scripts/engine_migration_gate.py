#!/usr/bin/env python3
"""Native-vs-vectorbt migration gate for the #318 engine consolidation.

Runs every strategy-registry entry through ``NativeAdapter`` and
``VectorbtAdapter`` on the *same* configuration and writes a per-strategy
trade/stat diff report. This is the phase-S evidence for the phase-M default
flips described in ``docs/adr/0001-native-single-strategy-engine.md``: the
gate changes no defaults and asserts nothing — per-strategy differences are
*reported* so each can be explained before any flip.

Data sources, in precedence order:

- ``--data-dir``: an existing data root (must contain ``ohlcv/``), used
  read-only.
- ``TP_TEST_OHLCV_DIR``: the same opt-in the strategy test-suite data gate
  uses — a read-only OHLCV root. When it is not already shaped like
  ``<root>/ohlcv``, a temporary wrapper directory with an ``ohlcv`` symlink
  is created (the adapters resolve ``<data_dir>/ohlcv``).
- otherwise: deterministic synthetic OHLCV is generated under
  ``--synthetic-dir``; when that flag is absent a temporary directory is
  used so tooling runs never drop synthetic data into ``$DATA_DIR``.

Run from the repo root::

    python -m scripts.engine_migration_gate --output data/engine-migration-gate/report.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.engine_migration_gate
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("engine_migration_gate")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time - it
# is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of scripts
# that must carry the guard. Kept deliberately, and silenced rather than removed: dropping it would
# take this script off the roster and out of the gate that keeps the guard alive.
import shared  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.stats import merge_canonical_stats
from scripts._safe_path import under_root
from shared.atomic_io import write_text_atomically
from shared.constants import normalize_timeframe
from shared.models import StrategyType
from shared.strategy_registry import (
    detect_strategy_type,
    iter_strategy_entries,
)

TEST_OHLCV_DIR_ENV = "TP_TEST_OHLCV_DIR"
DEFAULT_TIMEFRAME = "1h"
DEFAULT_RISK_PCT = 0.01
# Fees are declared explicitly (#84 fails closed otherwise). Zero-fee is the
# default so the report isolates fill/sizing parity; pass --params with
# fee_rate/fee_flat_per_trade for a cost-inclusive run.
DEFAULT_PARAMETERS: dict[str, Any] = {"fees": "zero"}
SYNTHETIC_SOURCE = "engine_migration_gate"
SYNTHETIC_ASSET_CLASS = "synthetic"

#: Scalar stats keys compared between engines. Non-scalar stats entries
#: (``extras`` and friends) are skipped — they are diagnostics, not results.
_STATS_SKIP_KEYS = {"extras"}


@dataclass
class GateConfig:
    """One gate run: a strategy set over one market-data window."""

    data_dir: str
    symbols: list[str]
    timeframe: str = DEFAULT_TIMEFRAME
    start: str = ""
    end: str = ""
    capital: float = 100_000.0
    risk_pct: float | None = DEFAULT_RISK_PCT
    modeling_resolution: str = "open_close"
    parameters: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PARAMETERS))
    strategies_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "strategies")
    only: tuple[str, ...] = ()
    max_symbols: int = 3


@dataclass
class DataResolution:
    """Where the adapters read OHLCV from, and how it was obtained."""

    data_root: Path
    mode: str  # "data_dir" | "tp_test_ohlcv_dir" | "synthetic"
    cleanup: tempfile.TemporaryDirectory | None = None


def _ohlcv_root(data_root: Path) -> Path:
    return data_root / "ohlcv"


def _first_ohlcv_match(ohlcv_root: Path, symbol: str, timeframe: str) -> Path | None:
    """The parquet the adapters will actually read (they use ``matches[0]``)."""
    matches = sorted(
        ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"),
        key=lambda path: path.as_posix(),
    )
    return matches[0] if matches else None


def _probe_symbols(ohlcv_root: Path, timeframe: str, limit: int) -> list[str]:
    """Symbols with at least one ``<symbol>/<timeframe>.parquet`` shard."""
    if not ohlcv_root.exists():
        return []
    symbols = {
        path.parent.name
        for path in ohlcv_root.rglob(f"*/{timeframe}.parquet")
        if path.parent.name != ohlcv_root.name
    }
    return sorted(symbols)[:limit]


def _detect_window(
    ohlcv_root: Path, symbols: list[str], timeframe: str
) -> tuple[str, str] | None:
    """Intersection of each symbol's first-shard timestamp extent.

    Mirrors ``runner.cli._detect_date_range``: every requested symbol must
    contribute a readable range or the window cannot be fabricated.
    """
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for symbol in symbols:
        path = _first_ohlcv_match(ohlcv_root, symbol, timeframe)
        if path is None:
            return None
        try:
            df = pd.read_parquet(path, columns=["timestamp"])
        except Exception:
            return None
        if df.empty:
            return None
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
        if ts.empty:
            return None
        starts.append(ts.min())
        ends.append(ts.max())
    if len(starts) != len(symbols):
        return None
    start = max(starts).strftime("%Y-%m-%d")
    end = min(ends).strftime("%Y-%m-%d")
    return (start, end) if start <= end else None


def _resolve_data_root(
    data_dir: str,
    *,
    ohlcv_env: str | None = None,
    synthetic_dir: Path | None = None,
) -> DataResolution:
    """Pick the adapter data root: explicit > TP_TEST_OHLCV_DIR > synthetic."""
    if data_dir:
        return DataResolution(data_root=Path(data_dir), mode="data_dir")

    env_raw = (
        ohlcv_env
        if ohlcv_env is not None
        else os.environ.get(TEST_OHLCV_DIR_ENV, "")
    )
    env_value = str(env_raw).strip()
    if env_value:
        root = Path(env_value).expanduser()
        if (root / "ohlcv").is_dir():
            return DataResolution(data_root=root, mode=TEST_OHLCV_DIR_ENV)
        if root.name == "ohlcv":
            return DataResolution(data_root=root.parent, mode=TEST_OHLCV_DIR_ENV)
        # The adapters hardcode ``<data_dir>/ohlcv``; bridge with a symlink so
        # the env var keeps its conftest meaning (the OHLCV content root).
        # ``resolve()`` first: a relative env value would leave a dangling
        # symlink inside the temp wrapper.
        root = root.resolve()
        wrapper = tempfile.TemporaryDirectory(prefix="engine_gate_ohlcv_")
        link = Path(wrapper.name) / "ohlcv"
        try:
            os.symlink(root, link, target_is_directory=True)
        except OSError as exc:
            wrapper.cleanup()
            raise RuntimeError(
                f"{TEST_OHLCV_DIR_ENV}={root} is not shaped like an adapter data "
                "root and a symlink wrapper could not be created "
                f"({exc}). Pass --data-dir pointing at a directory that "
                "contains ohlcv/ instead."
            ) from exc
        return DataResolution(
            data_root=Path(wrapper.name),
            mode=TEST_OHLCV_DIR_ENV,
            cleanup=wrapper,
        )

    if synthetic_dir is not None:
        return DataResolution(
            data_root=Path(synthetic_dir).expanduser(), mode="synthetic"
        )
    # Default: a throwaway directory. Writing synthetic OHLCV under the
    # default $DATA_DIR would drop test fixtures next to real market data.
    wrapper = tempfile.TemporaryDirectory(prefix="engine_gate_synth_")
    return DataResolution(
        data_root=Path(wrapper.name), mode="synthetic", cleanup=wrapper
    )


def _write_synthetic_ohlcv(
    data_root: Path,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
) -> int:
    """Write deterministic synthetic OHLCV for ``symbols``; returns bar count."""
    freq_map = {
        "s1": "s",
        "1m": "min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "h",
        "4h": "4h",
        "1d": "D",
        "1w": "W",
    }
    freq = freq_map.get(timeframe)
    if freq is None:
        raise ValueError(
            f"no synthetic bar frequency for timeframe {timeframe!r}; "
            f"supported: {sorted(freq_map)}"
        )
    timestamps = pd.date_range(start, end, freq=freq, tz="UTC")
    bars = 0
    for index, symbol in enumerate(symbols):
        rng = np.random.default_rng(seed=20261121 + index)
        returns = rng.normal(0.0004, 0.012, size=len(timestamps))
        # Clip the cumulative log-return: fine timeframes (s1) over long
        # windows would otherwise overflow np.exp into non-finite prices.
        close = 100.0 * np.exp(np.clip(np.cumsum(returns), -50.0, 50.0))
        open_ = np.concatenate([[100.0], close[:-1]])
        spread = np.abs(rng.normal(0.0, 0.15, size=len(timestamps)))
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": open_,
                "high": np.maximum(open_, close) + spread,
                "low": np.minimum(open_, close) - spread,
                "close": close,
                "volume": rng.integers(500, 5000, size=len(timestamps)).astype(float),
            }
        )
        symbol_dir = (
            _ohlcv_root(data_root)
            / SYNTHETIC_SOURCE
            / SYNTHETIC_ASSET_CLASS
            / symbol.upper()
        )
        symbol_dir.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(symbol_dir / f"{timeframe}.parquet", index=False)
        bars = max(bars, len(frame))
    return bars


def _backtest_config(gate: GateConfig, slug: str) -> BacktestConfig:
    return BacktestConfig(
        strategy_name=slug,
        symbols=list(gate.symbols),
        timeframe=gate.timeframe,
        start_date=gate.start,
        end_date=gate.end,
        initial_capital=gate.capital,
        risk_per_trade_pct=gate.risk_pct,
        modeling_resolution=gate.modeling_resolution,
        data_dir=gate.data_dir,
        parameters=dict(gate.parameters),
    )


def _run_engine(engine_name: str, config: BacktestConfig, strategy_path: Path) -> dict[str, Any]:
    adapter = {"native": NativeAdapter, "vectorbt": VectorbtAdapter}[engine_name]()
    try:
        result = adapter.run(config, str(strategy_path))
    except Exception as exc:  # per-strategy engine failure is report data, not a gate failure
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    merge_canonical_stats(result)
    return {
        "status": "ok",
        "trade_count": len(result.trades),
        "_result": result,
    }


def _f(value: Any) -> float | None:
    """Coerce to float for comparison; non-numeric → None sentinel handling."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numbers_close(a: Any, b: Any) -> bool:
    fa, fb = _f(a), _f(b)
    if fa is not None and fb is not None:
        return math.isclose(fa, fb, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def _norm_time(value: Any) -> str:
    """Normalize an engine timestamp to one ISO spelling for comparison.

    Native emits ``2024-01-05T05:00:00+00:00`` while vectorbt emits the same
    instant as ``2024-01-05 05:00:00+00:00`` — the same fill must not diff on
    spelling alone.
    """
    try:
        return pd.Timestamp(str(value)).isoformat()
    except Exception:
        return str(value)


#: Signature precision = the coarsest precision either engine stores. The
#: vectorbt adapter persists prices at 6dp, quantities at 4dp and money at
#: 2dp (``vectorbt_adapter.py`` Trade construction) while native stores
#: unrounded values; comparing at the stored precision keeps pure
#: representation rounding from reading as a trade difference while any
#: real fill/size/PnL divergence still diffs.
_SIG_PRICE_DP = 6
_SIG_QTY_DP = 4
_SIG_MONEY_DP = 2


def _trade_signature(trade: Any) -> tuple:
    """Stable comparable signature for one trade at stored precision."""

    def rnd(value: Any, digits: int) -> Any:
        number = _f(value)
        return round(number, digits) if number is not None else value

    exits = tuple(
        (
            _norm_time(exit_.time),
            rnd(exit_.price, _SIG_PRICE_DP),
            rnd(exit_.quantity, _SIG_QTY_DP),
            rnd(exit_.pnl, _SIG_MONEY_DP),
            exit_.reason,
        )
        for exit_ in trade.exits
    )
    return (
        trade.symbol,
        trade.direction,
        _norm_time(trade.entry_time),
        rnd(trade.entry_price, _SIG_PRICE_DP),
        rnd(trade.quantity, _SIG_QTY_DP),
        exits,
        rnd(trade.pnl, _SIG_MONEY_DP),
        rnd(trade.fees, _SIG_MONEY_DP),
        rnd(trade.net_pnl, _SIG_MONEY_DP),
        trade.exit_reason,
    )


def _signature_example(trade: Any) -> dict[str, Any]:
    return {
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_time": str(trade.entry_time),
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "net_pnl": trade.net_pnl,
    }


def _diff_trades(native_result: Any, vectorbt_result: Any) -> dict[str, Any]:
    """Multiset diff of the two engines' trade lists (order-insensitive)."""
    native_trades = sorted(
        native_result.trades, key=lambda t: (_norm_time(t.entry_time), t.symbol)
    )
    vectorbt_trades = sorted(
        vectorbt_result.trades, key=lambda t: (_norm_time(t.entry_time), t.symbol)
    )

    native_sigs = [_trade_signature(t) for t in native_trades]
    vectorbt_sigs = [_trade_signature(t) for t in vectorbt_trades]

    remaining = list(vectorbt_sigs)
    matched = 0
    unmatched_native: list[Any] = []
    for sig, trade in zip(native_sigs, native_trades):
        if sig in remaining:
            remaining.remove(sig)
            matched += 1
        else:
            unmatched_native.append(trade)
    unmatched_vectorbt_sigs = remaining

    # Map unmatched vectorbt signatures back to trades for readable examples.
    vbt_by_sig: dict[tuple, list[Any]] = {}
    for sig, trade in zip(vectorbt_sigs, vectorbt_trades):
        vbt_by_sig.setdefault(sig, []).append(trade)
    unmatched_vectorbt = [
        vbt_by_sig[sig].pop(0) for sig in unmatched_vectorbt_sigs if vbt_by_sig.get(sig)
    ]

    identical = matched == len(native_sigs) == len(vectorbt_sigs)
    return {
        "trades_identical": identical,
        "trade_count": {
            "native": len(native_trades),
            "vectorbt": len(vectorbt_trades),
            "delta": len(vectorbt_trades) - len(native_trades),
        },
        "exact_signature_matches": matched,
        "only_in_native": [_signature_example(t) for t in unmatched_native[:5]],
        "only_in_native_count": len(unmatched_native),
        "only_in_vectorbt": [
            _signature_example(t) for t in unmatched_vectorbt[:5]
        ],
        "only_in_vectorbt_count": len(unmatched_vectorbt),
    }


def _diff_stats(native_result: Any, vectorbt_result: Any) -> dict[str, Any]:
    """Scalar stat keys that differ between the two engines' merged stats."""
    native_stats = native_result.stats or {}
    vectorbt_stats = vectorbt_result.stats or {}
    diffs: dict[str, Any] = {}
    for key in sorted(set(native_stats) | set(vectorbt_stats)):
        if key in _STATS_SKIP_KEYS:
            continue
        a = native_stats.get(key)
        b = vectorbt_stats.get(key)
        if not _numbers_close(a, b):
            diffs[key] = {"native": a, "vectorbt": b}
    return {
        "stats_identical": not diffs,
        "stats_diffs": diffs,
    }


def _jsonable(value: Any) -> Any:
    """Make report values JSON-serializable (non-finite floats → None)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _skip_reason(entry: Any, gate: GateConfig) -> str | None:
    """Why a registry entry cannot join the native/vectorbt comparison."""
    if (
        detect_strategy_type(entry.file_path, gate.strategies_dir).value
        == StrategyType.SIGNAL_PROCESSOR.value
    ):
        return "signal_processor strategies are not engine backtests"
    # Strategies declaring an engine outside {native, vectorbt} (pynecore,
    # lean) cannot run on either side of this comparison. Skipping avoids
    # executing their modules at all — importing a pynecore strategy
    # rewrites its generated .toml/context files as a side effect.
    if entry.engine not in {"native", "vectorbt"}:
        return (
            f"declared engine {entry.engine!r} is outside the "
            "native/vectorbt comparison"
        )
    return None


def _compare_engines(
    record: dict[str, Any], gate: GateConfig, entry: Any
) -> dict[str, Any]:
    """Run both adapters for one strategy and attach the diff verdict."""
    config = _backtest_config(gate, entry.slug)
    outcomes: dict[str, dict[str, Any]] = {}
    results: dict[str, Any] = {}
    for engine_name in ("native", "vectorbt"):
        outcome = _run_engine(engine_name, config, entry.file_path)
        outcomes[engine_name] = {
            key: value for key, value in outcome.items() if key != "_result"
        }
        if "_result" in outcome:
            results[engine_name] = outcome["_result"]

    record["native"] = outcomes["native"]
    record["vectorbt"] = outcomes["vectorbt"]

    if "native" in results and "vectorbt" in results:
        trade_diff = _diff_trades(results["native"], results["vectorbt"])
        stats_diff = _diff_stats(results["native"], results["vectorbt"])
        if trade_diff["trades_identical"] and stats_diff["stats_identical"]:
            status = "identical"
        elif not trade_diff["trades_identical"]:
            status = "trades_differ"
        else:
            status = "stats_differ"
        record["diff"] = {**trade_diff, **stats_diff, "status": status}
        record["status"] = status
    else:
        record["status"] = "engine_error"
        record["diff"] = None
    return record


def run_gate(gate: GateConfig) -> dict[str, Any]:
    """Run the gate and return the report dict (also written by ``main``)."""
    if not gate.symbols:
        raise ValueError("gate.symbols must name at least one symbol")
    ohlcv_root = _ohlcv_root(Path(gate.data_dir))
    entries = [
        entry
        for entry in iter_strategy_entries(gate.strategies_dir)
        if not gate.only or entry.slug in gate.only
    ]

    strategies: list[dict[str, Any]] = []
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        record: dict[str, Any] = {
            "slug": entry.slug,
            "file": entry.relative_file,
            # ``entry.engine`` is the EFFECTIVE engine (undeclared strategies
            # default to vectorbt), not a declared preference.
            "engine": entry.engine,
            "strategy_type": detect_strategy_type(
                entry.file_path, gate.strategies_dir
            ).value,
        }
        reason = _skip_reason(entry, gate)
        if reason is not None:
            record["status"] = "skipped"
            record["reason"] = reason
        else:
            record = _compare_engines(record, gate, entry)
        strategies.append(record)
        print(
            f"[{index}/{total}] {entry.slug}: {record['status']}",
            file=sys.stderr,
            flush=True,
        )

    summary = {"total": len(strategies)}
    for status in ("identical", "trades_differ", "stats_differ", "engine_error", "skipped"):
        summary[status] = sum(1 for s in strategies if s.get("status") == status)

    return _jsonable(
        {
            "gate": "native-vs-vectorbt migration gate (issue #318, phase S)",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "strategies_dir": str(gate.strategies_dir),
                "symbols": list(gate.symbols),
                "timeframe": gate.timeframe,
                "start": gate.start,
                "end": gate.end,
                "capital": gate.capital,
                "risk_per_trade_pct": gate.risk_pct,
                "modeling_resolution": gate.modeling_resolution,
                "parameters": gate.parameters,
                "ohlcv_root": str(ohlcv_root),
            },
            "summary": summary,
            "strategies": strategies,
        }
    )


def write_report(report: dict[str, Any], output: Path) -> Path:
    """Write the report JSON atomically, confined to the project root."""
    safe = under_root(output, root=PROJECT_ROOT, field="--output")
    safe.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(safe, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return safe


def _csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _parse_params(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--params must decode to a JSON object")
    return parsed


def _resolve_symbols_and_window(
    args: argparse.Namespace,
    resolution: DataResolution,
    ohlcv_root: Path,
    timeframe: str,
) -> tuple[list[str], str, str]:
    """Pick symbols and the start/end window for one gate run."""
    symbols = _csv_list(args.symbols)
    if not symbols:
        if resolution.mode == "synthetic":
            symbols = ["SYNTH_A", "SYNTH_B"]
        else:
            symbols = _probe_symbols(ohlcv_root, timeframe, args.max_symbols)
            if not symbols:
                raise SystemExit(
                    f"no symbols with {timeframe}.parquet under {ohlcv_root}; "
                    "pass --symbols or --data-dir"
                )

    start, end = args.start, args.end
    if resolution.mode == "synthetic":
        start = start or "2024-01-01"
        end = end or "2024-06-30"
        bars = _write_synthetic_ohlcv(
            resolution.data_root, symbols, timeframe, start, end
        )
        print(f"synthetic data: {len(symbols)} symbol(s), ~{bars} bars each")
    elif not start or not end:
        window = _detect_window(ohlcv_root, symbols, timeframe)
        if window is None:
            raise SystemExit(
                f"could not detect a common {timeframe} window for {symbols} "
                f"under {ohlcv_root}; pass --start/--end"
            )
        start = start or window[0]
        end = end or window[1]
    return symbols, start, end


def _build_gate(
    args: argparse.Namespace, resolution: DataResolution, ohlcv_root: Path
) -> GateConfig:
    """Assemble the GateConfig from parsed CLI args and resolved data."""
    timeframe = normalize_timeframe(args.timeframe) or DEFAULT_TIMEFRAME
    symbols, start, end = _resolve_symbols_and_window(
        args, resolution, ohlcv_root, timeframe
    )
    parameters = {**DEFAULT_PARAMETERS, **_parse_params(args.params)}
    return GateConfig(
        data_dir=str(resolution.data_root),
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        capital=args.capital,
        risk_pct=args.risk_pct,
        modeling_resolution=args.modeling_resolution,
        parameters=parameters,
        strategies_dir=args.strategies_dir,
        only=tuple(_csv_list(args.only)),
        max_symbols=args.max_symbols,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategies-dir",
        type=Path,
        default=PROJECT_ROOT / "strategies",
        help="Strategy registry directory (default: repo strategies/)",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="Adapter data root containing ohlcv/ (read-only). Overrides "
        f"{TEST_OHLCV_DIR_ENV}; when neither is set, synthetic data is generated.",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols. Default: probe the data for up to "
        "--max-symbols symbols with the chosen timeframe, else synthetic symbols.",
    )
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--start", default="", help="Default: detected from data")
    parser.add_argument("--end", default="", help="Default: detected from data")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    parser.add_argument("--modeling-resolution", default="open_close")
    parser.add_argument(
        "--params",
        default="",
        help='Extra engine params JSON, merged over {"fees": "zero"}',
    )
    parser.add_argument("--max-symbols", type=int, default=3)
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated strategy slugs to run (default: all registry entries)",
    )
    parser.add_argument(
        "--synthetic-dir",
        type=Path,
        default=None,
        help="Where synthetic OHLCV is written when no real data source is "
        "configured (default: a throwaway temporary directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/engine-migration-gate/report.json"),
        help="Report JSON path, confined to the project root",
    )
    args = parser.parse_args(argv)

    resolution = _resolve_data_root(
        args.data_dir, synthetic_dir=args.synthetic_dir
    )
    ohlcv_root = _ohlcv_root(resolution.data_root)
    gate = _build_gate(args, resolution, ohlcv_root)

    try:
        report = run_gate(gate)
    finally:
        if resolution.cleanup is not None:
            resolution.cleanup.cleanup()
    report["config"]["data_mode"] = resolution.mode
    out = write_report(report, args.output)

    summary = report["summary"]
    print(
        f"gate complete: {summary['total']} strategies — "
        f"{summary['identical']} identical, {summary['trades_differ']} trades_differ, "
        f"{summary['stats_differ']} stats_differ, {summary['engine_error']} engine_error, "
        f"{summary['skipped']} skipped"
    )
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

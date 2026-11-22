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
# fee_rate/fee_flat_per_trade for a cost-inclusive run. ``slippage: none``
# pins both engines gross — once #354 lands the native engine would
# otherwise default to the auto slippage model while vectorbt models none.
DEFAULT_PARAMETERS: dict[str, Any] = {"fees": "zero", "slippage": "none"}
SYNTHETIC_SOURCE = "engine_migration_gate"
SYNTHETIC_ASSET_CLASS = "synthetic"

#: Scalar stats keys compared between engines. ``to_dict()`` flattens
#: ``BacktestStats.extras`` into top-level keys, so the diagnostics live
#: under these names — they are skipped because they are diagnostics, not
#: results.
_STATS_SKIP_KEYS = {
    "extras",
    "risk_free_rate",
    "undefined_metrics",
    "equity_return_frequency",
    "duplicate_equity_timestamps",
    "ruined",
    "ruin_time",
}

#: Per-key comparison tolerance for ``_diff_stats`` — the same stored-unit
#: convention the trade diff uses, sized to the noise recomputing stats from
#: each engine's own (differently rounded) trades can produce: money stats
#: get the 2dp stored unit (0.01) and ratio/percent stats get 1e-4, the
#: coarsest stored ratio precision. Anything unmapped — counts and any new
#: key — falls back to strict equality so a real difference can never hide.
_STATS_TOLERANCES: dict[str, float] = {
    "total_pnl": 0.01,
    "avg_winner": 0.01,
    "avg_loser": 0.01,
    "avg_holding_bars": 0.1,
    "win_rate": 1e-4,
    "profit_factor": 1e-4,
    "sharpe_ratio": 1e-4,
    "sortino_ratio": 1e-4,
    "calmar_ratio": 1e-4,
    "psr": 1e-4,
    "max_drawdown_pct": 1e-4,
    "cagr": 1e-4,
    "total_return": 1e-4,
    "p_value": 1e-4,
}


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
    # Sizing caps pinned explicitly on BOTH engines: each adapter otherwise
    # falls back to its own defaults (native 50% notional, vectorbt the
    # 20% non-equity cap and 2.0x leverage), which would make the default
    # report config noise instead of engine-logic diffs.
    max_position_exposure_pct: float = 0.2
    max_leverage: float = 2.0


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


#: Bar spacing per supported synthetic timeframe, mirroring the ``freq_map``
#: inside ``_write_synthetic_ohlcv`` (kept as Timedeltas so window size can
#: be estimated without materializing the index).
_SYNTHETIC_BAR_FREQ: dict[str, str] = {
    "s1": "1s",
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
    "1w": "7D",
}

#: Largest synthetic window the DEFAULT (unbounded) ``--start``/``--end`` may
#: imply. The six-month default at ``s1`` is ~15.5M rows — several GB — so an
#: oversized defaulted window refuses and asks for explicit bounds.
_SYNTHETIC_MAX_DEFAULT_BARS = 2_000_000


def _estimate_synthetic_bars(timeframe: str, start: str, end: str) -> int:
    """Approximate bar count for a synthetic window (0 when unestimable)."""
    freq = _SYNTHETIC_BAR_FREQ.get(timeframe)
    if freq is None:
        return 0
    try:
        span = pd.Timestamp(end) - pd.Timestamp(start)
    except Exception:
        return 0
    if span < pd.Timedelta(0):
        return 0
    return int(span / pd.Timedelta(freq)) + 1


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
        max_position_exposure_pct=gate.max_position_exposure_pct,
        max_leverage=gate.max_leverage,
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


#: Comparison tolerance = ONE STORED UNIT of the coarsest precision either
#: engine persists. The vectorbt adapter stores prices at 6dp, quantities at
#: 4dp and money at 2dp (``vectorbt_adapter.py`` Trade construction) while
#: native keeps unrounded floats, so a value sitting on a rounding boundary
#: (e.g. qty 151.3811 vs 151.3812) must not read as a trade difference —
#: but any divergence larger than one stored unit still diffs.
_SIG_TOL_PRICE = 1e-6
_SIG_TOL_QTY = 1e-4
_SIG_TOL_MONEY = 0.01

#: Cap on per-field diff entries so a fully-divergent run cannot emit an
#: unbounded report; the true count is always reported alongside.
_MAX_FIELD_DIFFS = 100


def _within_stored_precision(a: Any, b: Any, tolerance: float) -> bool:
    """True when ``a`` and ``b`` agree to within one stored unit.

    The bound carries a small relative headroom: two floats that differ by
    exactly one stored unit can land a hair above it in binary
    (``194.9428 - 194.9427`` is ``1.0000000000287557e-04``), which is
    representation noise, not a real divergence.
    """
    fa, fb = _f(a), _f(b)
    if fa is not None and fb is not None:
        return abs(fa - fb) <= tolerance * (1 + 1e-9) + 1e-12
    return a == b


def _label_equal(a: Any, b: Any) -> bool:
    """Exit-leg labels: ``None`` means *unlabelled*, not a different reason.

    The vectorbt adapter does not label individual exit legs (``reason`` is
    ``None``) while native writes e.g. ``"signal"`` — a label-presence
    difference is not a money difference. Two different non-empty labels
    still diff.
    """
    if a is None or b is None:
        return True
    return a == b


def _trade_field_diffs(native: Any, vectorbt: Any) -> list[dict[str, Any]]:
    """Per-field differences between one paired native/vectorbt trade.

    Every entry names the field, both stored values and is self-explaining —
    the #318 requirement that each reported difference can be explained.
    """
    diffs: list[dict[str, Any]] = []

    def add(field: str, a: Any, b: Any) -> None:
        diffs.append(
            {
                "field": field,
                "native": _jsonable(a),
                "vectorbt": _jsonable(b),
            }
        )

    for name, tolerance in (
        ("entry_price", _SIG_TOL_PRICE),
        ("quantity", _SIG_TOL_QTY),
        ("pnl", _SIG_TOL_MONEY),
        ("fees", _SIG_TOL_MONEY),
        ("net_pnl", _SIG_TOL_MONEY),
    ):
        a_value = getattr(native, name, None)
        b_value = getattr(vectorbt, name, None)
        if not _within_stored_precision(a_value, b_value, tolerance):
            add(name, a_value, b_value)

    a_reason = getattr(native, "exit_reason", None)
    b_reason = getattr(vectorbt, "exit_reason", None)
    if not _label_equal(a_reason, b_reason):
        add("exit_reason", a_reason, b_reason)

    native_exits = list(getattr(native, "exits", None) or [])
    vectorbt_exits = list(getattr(vectorbt, "exits", None) or [])
    if len(native_exits) != len(vectorbt_exits):
        add("exits.count", len(native_exits), len(vectorbt_exits))
    else:
        for index, (a_exit, b_exit) in enumerate(zip(native_exits, vectorbt_exits)):
            prefix = f"exits[{index}]"
            a_time = _norm_time(getattr(a_exit, "time", None))
            b_time = _norm_time(getattr(b_exit, "time", None))
            if a_time != b_time:
                add(
                    f"{prefix}.time",
                    getattr(a_exit, "time", None),
                    getattr(b_exit, "time", None),
                )
            for name, tolerance in (
                ("price", _SIG_TOL_PRICE),
                ("quantity", _SIG_TOL_QTY),
                ("pnl", _SIG_TOL_MONEY),
            ):
                a_value = getattr(a_exit, name, None)
                b_value = getattr(b_exit, name, None)
                if not _within_stored_precision(a_value, b_value, tolerance):
                    add(f"{prefix}.{name}", a_value, b_value)
            a_leg_reason = getattr(a_exit, "reason", None)
            b_leg_reason = getattr(b_exit, "reason", None)
            if not _label_equal(a_leg_reason, b_leg_reason):
                add(f"{prefix}.reason", a_leg_reason, b_leg_reason)
    return diffs


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


def _pair_key(trade: Any) -> tuple:
    """Identity used to pair one native trade with its vectorbt counterpart."""
    return (
        trade.symbol,
        trade.direction,
        _norm_time(trade.entry_time),
    )


def _diff_trades(native_result: Any, vectorbt_result: Any) -> dict[str, Any]:
    """Paired diff of the two engines' trade lists.

    Trades pair by ``(symbol, direction, entry_time)``; each pair is compared
    field-by-field at one-stored-unit tolerance and every divergence is
    reported as a self-explaining ``{"field", "native", "vectorbt"}`` entry.
    Trades that cannot be paired at all land in ``only_in_*`` as before.
    """
    native_trades = sorted(
        native_result.trades, key=lambda t: (_norm_time(t.entry_time), t.symbol)
    )
    vectorbt_trades = sorted(
        vectorbt_result.trades, key=lambda t: (_norm_time(t.entry_time), t.symbol)
    )

    # Bucket vectorbt trades by pair key; each native trade claims the
    # same-key candidate with the fewest field differences.
    vbt_by_key: dict[tuple, list[Any]] = {}
    for trade in vectorbt_trades:
        vbt_by_key.setdefault(_pair_key(trade), []).append(trade)

    matched = 0
    unmatched_native: list[Any] = []
    field_diffs: list[dict[str, Any]] = []
    for trade in native_trades:
        candidates = vbt_by_key.get(_pair_key(trade)) or []
        if not candidates:
            unmatched_native.append(trade)
            continue
        best_index = min(
            range(len(candidates)),
            key=lambda i: len(_trade_field_diffs(trade, candidates[i])),
        )
        candidate = candidates.pop(best_index)
        matched += 1
        for diff in _trade_field_diffs(trade, candidate):
            field_diffs.append(
                {
                    "trade": {
                        "symbol": trade.symbol,
                        "direction": trade.direction,
                        "entry_time": str(trade.entry_time),
                    },
                    **diff,
                }
            )
    unmatched_vectorbt = [
        trade for trades in vbt_by_key.values() for trade in trades
    ]
    unmatched_vectorbt.sort(
        key=lambda t: (_norm_time(t.entry_time), t.symbol)
    )

    identical = (
        matched == len(native_trades) == len(vectorbt_trades)
        and not field_diffs
    )
    return {
        "trades_identical": identical,
        "trade_count": {
            "native": len(native_trades),
            "vectorbt": len(vectorbt_trades),
            "delta": len(vectorbt_trades) - len(native_trades),
        },
        "matched_pairs": matched,
        "field_diffs": field_diffs[:_MAX_FIELD_DIFFS],
        "field_diffs_count": len(field_diffs),
        "field_diffs_truncated": len(field_diffs) > _MAX_FIELD_DIFFS,
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
        tolerance = _STATS_TOLERANCES.get(key)
        if tolerance is not None:
            if not _within_stored_precision(a, b, tolerance):
                diffs[key] = {"native": a, "vectorbt": b}
        elif not _numbers_close(a, b):
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
    if not entries:
        # An empty run would exit 0 saying "gate complete: 0 strategies" —
        # a green-looking report that compared nothing. Refuse instead.
        raise SystemExit(
            "no strategy entries to compare "
            f"(strategies_dir={gate.strategies_dir}, "
            f"only={','.join(gate.only) if gate.only else 'all'})"
        )

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
                "max_position_exposure_pct": gate.max_position_exposure_pct,
                "max_leverage": gate.max_leverage,
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
        if not (args.start and args.end):
            # The default six-month window is fine at 1h but explodes at s1
            # (~15.5M rows, several GB). A defaulted oversized window refuses
            # rather than allocate; an explicit --start/--end is the opt-in.
            bars_estimate = _estimate_synthetic_bars(timeframe, start, end)
            if bars_estimate > _SYNTHETIC_MAX_DEFAULT_BARS:
                raise SystemExit(
                    f"synthetic default window {start}..{end} at {timeframe} "
                    f"is ~{bars_estimate:,} bars — pass explicit --start/--end "
                    "to keep the run small"
                )
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
        max_position_exposure_pct=args.max_position_exposure_pct,
        max_leverage=args.max_leverage,
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
        help='Extra engine params JSON, merged over the defaults '
        '(fees zero, slippage none)',
    )
    parser.add_argument("--max-symbols", type=int, default=3)
    parser.add_argument(
        "--max-position-exposure-pct",
        type=float,
        default=0.2,
        help="Per-position exposure cap pinned on BOTH engines so the report "
        "compares engine logic, not each adapter's own default "
        "(default: 0.2)",
    )
    parser.add_argument(
        "--max-leverage",
        type=float,
        default=2.0,
        help="Broker-max leverage pinned on BOTH engines (default: 2.0; "
        "native clamps to it on free margin since #347)",
    )
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
        "configured (default: a throwaway temporary directory). Unlike "
        "--output this operator-chosen path is NOT confined to the project "
        "root — it is used verbatim.",
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

#!/usr/bin/env python3
"""Native-engine equivalence / timing harness.

Runs ``TradeEngine.run`` on a fixed set of (strategy, symbol, timeframe,
window) cases against local market data and either dumps the full result
(every trade field + the whole equity curve) to JSON or compares two dumps.

    python scripts/engine_equivalence.py dump OUT.json [--data-dir DIR] [--only NAME ...]
    python scripts/engine_equivalence.py compare A.json B.json [--tol 0]
    python scripts/engine_equivalence.py profile CASE [--data-dir DIR]

Used to prove that hot-loop optimisations of the engine are behaviour
preserving: dump on the base commit, dump on the change, compare.
"""

from __future__ import annotations

import argparse
import cProfile
import dataclasses
import json
import math
import pstats
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from runner.engine import TradeEngine  # noqa: E402
from runner.engines.base import BacktestConfig, resolve_strategy_parameters  # noqa: E402
from runner.engines.native_adapter import _load_ohlcv, _load_strategy_module  # noqa: E402

# name: (strategy, symbol, timeframe, start, end, extra parameters)
CASES: dict[str, tuple[str, str, str, str, str, dict]] = {
    "ivb_usatech_1m": ("intraday_volatility_breakout", "USATECH", "1m", "2026-03-20", "2026-05-01", {}),
    "ivb_usa500_1m": ("intraday_volatility_breakout", "USA500", "1m", "2026-03-20", "2026-04-15", {}),
    "ivb_eurusd_1m": ("intraday_volatility_breakout", "EURUSD", "1m", "2026-02-20", "2026-04-02", {}),
    "trend_btc_1d": ("trend_following", "BTCUSDT", "1d", "2023-01-01", "2026-06-18", {}),
    "volbreak_eth_1d": ("volatility_breakout", "ETHUSDT", "1d", "2023-01-01", "2026-06-18", {}),
    "channel_xau_1h": ("channel_breakout", "XAUUSD", "1h", "2006-01-01", "2007-12-31", {}),
    "supertrend_dema_sol_1h": ("pine_supertrend_dema", "SOLUSDT", "1h", "2026-04-19", "2026-06-18", {}),
    "meanrev_eth_1h": ("mean_reversion_rsi_bb", "ETHUSDT", "1h", "2026-04-19", "2026-06-18", {}),
    "ivb_usatech_1m_tpq": (
        "intraday_volatility_breakout", "USATECH", "1m", "2026-03-25", "2026-04-10",
        {"signal_exit_fill": "close"},
    ),
}


def _build(name: str, data_dir: Path):
    strat, symbol, tf, start, end, extra = CASES[name]
    path = REPO / "strategies" / strat / f"{strat}.py"
    module = _load_strategy_module(str(path))
    data = _load_ohlcv(data_dir, symbol, tf, start, end)
    if data is None or data.empty:
        raise RuntimeError(f"no data for {name}")
    cfg_kwargs = {k: v for k, v in extra.items() if k in BacktestConfig.model_fields}
    params = {k: v for k, v in extra.items() if k not in BacktestConfig.model_fields}
    cfg = BacktestConfig(
        strategy_name=strat, symbols=[symbol], timeframe=tf, start_date=start,
        end_date=end, initial_capital=100_000.0, risk_per_trade_pct=0.01,
        parameters=resolve_strategy_parameters(str(path), {"fees": "zero", **params}), data_dir=str(data_dir), **cfg_kwargs,
    )
    return TradeEngine(cfg, symbol=symbol), data, module.generate_signals


def _run(name: str, data_dir: Path):
    engine, data, fn = _build(name, data_dir)
    t0 = time.perf_counter()
    result = engine.run(data, fn)
    return result, time.perf_counter() - t0, len(data)


def _plain(obj):
    """Recursively turn a trade / dataclass / pydantic object into JSON data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return "NaN"
    return obj


def _dump(args) -> int:
    out = {}
    for name in args.only or CASES:
        result, secs, bars = _run(name, Path(args.data_dir))
        trades = [_plain(t) for t in result.trades]
        curve = [(p.time, p.equity) for p in result.equity_curve]
        out[name] = {"bars": bars, "seconds": secs, "trades": trades, "equity": curve}
        print(f"{name:24s} bars={bars:7d} trades={len(trades):5d} t={secs:8.2f}s", flush=True)
    Path(args.out).write_text(json.dumps(out))
    return 0


def _same(a, b, tol: float) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b or abs(a - b) <= tol
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b))
    return a == b


def _compare(args) -> int:
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    bad = 0
    print(f"{'case':24s} {'trades':>7s} {'curve':>7s} {'before s':>9s} {'after s':>9s} {'x':>6s}  result")
    for name in a:
        if name not in b:
            print(f"{name}: missing in B"); bad += 1; continue
        ok_t = _same(a[name]["trades"], b[name]["trades"], args.tol)
        ok_e = _same(a[name]["equity"], b[name]["equity"], args.tol)
        sa, sb = a[name]["seconds"], b[name]["seconds"]
        status = "IDENTICAL" if (ok_t and ok_e) else "DIFF"
        bad += status == "DIFF"
        print(f"{name:24s} {len(a[name]['trades']):7d} {len(a[name]['equity']):7d} "
              f"{sa:9.2f} {sb:9.2f} {sa / sb if sb else 0:6.1f}  {status}")
    return 1 if bad else 0


def _profile(args) -> int:
    engine, data, fn = _build(args.case, Path(args.data_dir))
    prof = cProfile.Profile()
    prof.enable()
    engine.run(data, fn)
    prof.disable()
    pstats.Stats(prof).sort_stats("cumulative").print_stats(args.top)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump"); d.add_argument("out"); d.add_argument("--data-dir", default=str(REPO / "data" / "data")); d.add_argument("--only", nargs="*"); d.set_defaults(fn=_dump)
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b"); c.add_argument("--tol", type=float, default=0.0); c.set_defaults(fn=_compare)
    pr = sub.add_parser("profile"); pr.add_argument("case"); pr.add_argument("--data-dir", default=str(REPO / "data" / "data")); pr.add_argument("--top", type=int, default=30); pr.set_defaults(fn=_profile)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

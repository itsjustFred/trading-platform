"""CLI for the phase-1 sleeve allocation harness.

Reads matrix daily-equity CSVs and/or walk-forward result JSONs, runs the
monthly walk-forward allocation and writes the report (which always includes
the 1/N and inverse-vol baselines) as JSON.

Example:
    python -m runner.sleeve_allocator \
        --matrix-csv data/backtest_matrix/matrix_daily_equity.csv \
        --matrix-is-oos \
        --walkforward data/walkforward_results/wf_xxx.json \
        --allow-full-sample-trade-counts \
        --output data/portfolios/sleeve_allocation_report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shared.atomic_io import write_text_atomically

from . import AllocationConfig, EligibilityConfig, load_sleeve_returns, walk_forward_allocation


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Walk-forward sleeve allocation report (phase 1: baselines only)"
    )
    parser.add_argument("--matrix-csv", type=Path, default=None,
                        help="matrix_daily_equity.csv path")
    parser.add_argument("--matrix-is-oos", action="store_true",
                        help="acknowledge that the matrix curves are "
                             "out-of-sample for the strategies involved "
                             "(required with --matrix-csv: matrix rows are "
                             "full-window backtests and cannot be verified)")
    parser.add_argument("--matrix-summary", type=Path, default=None,
                        help="matrix_summary.json for per-run trade counts")
    parser.add_argument("--walkforward", type=Path, action="append", default=[],
                        help="walk-forward result JSON; repeatable")
    parser.add_argument("--output", type=Path, required=True,
                        help="output JSON report path")
    parser.add_argument("--lookback-days", type=int, default=252,
                        help="estimation window in business days")
    parser.add_argument("--min-history-days", type=int, default=730)
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--ruin-equity-floor", type=float, default=0.01)
    parser.add_argument("--allow-full-sample-trade-counts", action="store_true",
                        help="admit total_trades-only sleeves at "
                             "point-in-time rebalances on a full-sample "
                             "(future-inclusive) trade count; off by default "
                             "because it is lookahead")
    parser.add_argument("--objective", action="append", default=[],
                        help="objective to report; baselines are always included")
    args = parser.parse_args(argv)

    if args.matrix_csv is None and not args.walkforward:
        parser.error("provide --matrix-csv and/or --walkforward")
    if args.matrix_csv is not None and not args.matrix_is_oos:
        parser.error(
            "--matrix-csv holds full-window backtest curves that cannot be "
            "verified out-of-sample; pass --matrix-is-oos to assert they are"
        )

    sleeve_returns = load_sleeve_returns(
        matrix_csv=args.matrix_csv,
        matrix_summary=args.matrix_summary,
        walkforward_paths=args.walkforward,
    )
    config = AllocationConfig(
        lookback_days=args.lookback_days,
        objectives=tuple(args.objective) if args.objective else ("equal_weight", "inverse_vol"),
        eligibility=EligibilityConfig(
            min_history_days=args.min_history_days,
            min_trades=args.min_trades,
            ruin_equity_floor=args.ruin_equity_floor,
            allow_full_sample_trade_counts=args.allow_full_sample_trade_counts,
        ),
    )
    result = walk_forward_allocation(sleeve_returns, config)

    payload = dict(result.report)
    payload["inputs"] = {
        "matrix_csv": str(args.matrix_csv) if args.matrix_csv else None,
        "matrix_is_oos_acknowledged": bool(args.matrix_is_oos),
        "walkforward": [str(p) for p in args.walkforward],
    }
    payload["sleeves_loaded"] = {
        sleeve_id: {
            "strategy": meta.strategy,
            "symbol": meta.symbol,
            "timeframe": meta.timeframe,
            "source": meta.source,
            "first_date": meta.first_date,
            "last_date": meta.last_date,
            "n_observations": meta.n_observations,
            "total_trades": meta.total_trades,
        }
        for sleeve_id, meta in sleeve_returns.metadata.items()
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        args.output, json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)
    )
    summary = {"output": str(args.output), "objectives": list(payload["objectives"])}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

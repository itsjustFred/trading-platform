"""``python -m runner.cli promote``: record a promotion to production (#165).

The command is the only way a promotion is recorded. It resolves the promoted
file and the bound parameters through the same functions the enforcement uses
(:mod:`live_runner.promotion`), so what is promoted is by construction what a
deployment is later checked against.

Every piece of evidence is mandatory. The out-of-sample evidence is read from
the optimization result file; the paper-trading and parity evidence must each
name a report file that exists, whose SHA256 is recorded in the ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from runner.promotion_gate import (
    InvalidPromotionError,
    PaperTradingEvidence,
    ParityEvidence,
    PromotionEvidence,
    ledger_path_for,
    load_promotion_thresholds,
    out_of_sample_evidence_from_result,
    promote,
    report_reference,
)


def add_promote_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "promote",
        help="Promote a strategy or signal bot to production (real-money accounts)",
        description=(
            "Record a promotion in the ledger next to the promoted file. The "
            "promotion is bound to the file's SHA256 and to the parameter set; "
            "all three pieces of evidence are required."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--strategy",
        help="Strategy name: promotes strategies/<name>/<name>.py",
    )
    target.add_argument(
        "--signal-processor",
        help="Registered signal processor id (e.g. ramjet): promotes the bot's processor module",
    )
    parser.add_argument(
        "--params",
        default="",
        help=(
            "JSON object, --signal-processor only: the deployment's "
            "signal_config.signal_processor_config. A strategy is promoted with its "
            "STRATEGY_META defaults; there are no per-deployment strategy parameters."
        ),
    )
    parser.add_argument(
        "--promoted-by", required=True, help="Name of the human reviewer who promotes"
    )
    parser.add_argument(
        "--optimization-result",
        required=True,
        help=(
            "Optimization result JSON with a three-way split. Verdict, test window "
            "and test trade count are read from it."
        ),
    )
    parser.add_argument("--paper-account-id", required=True, help="Paper/demo account id")
    parser.add_argument(
        "--paper-broker", required=True, choices=["alpaca", "ctrader"], help="Paper/demo broker"
    )
    parser.add_argument("--paper-start", required=True, help="Paper run start (ISO date/time)")
    parser.add_argument("--paper-end", required=True, help="Paper run end (ISO date/time)")
    parser.add_argument(
        "--paper-trades", required=True, type=int, help="Number of paper/demo trades"
    )
    parser.add_argument(
        "--paper-report", required=True, help="Paper-trading report file (hashed into the ledger)"
    )
    parser.add_argument(
        "--parity-reference",
        required=True,
        help="What parity was measured against (e.g. 'lean', 'TradingView export')",
    )
    parser.add_argument(
        "--parity-deviation",
        required=True,
        action="append",
        metavar="METRIC=VALUE",
        help=(
            "Measured relative deviation of one metric from the reference, as a "
            "fraction (total_return=0.012 is 1.2 %%). Repeat per metric."
        ),
    )
    parser.add_argument(
        "--parity-report", required=True, help="Parity report file (hashed into the ledger)"
    )
    parser.set_defaults(func=cmd_promote)
    return parser


def _parse_params(raw: str) -> dict[str, Any]:
    if not str(raw or "").strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidPromotionError(f"--params is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidPromotionError("--params must be a JSON object")
    return value


def _parse_deviations(items: list[str]) -> dict[str, float]:
    deviations: dict[str, float] = {}
    for item in items:
        metric, separator, raw_value = str(item).partition("=")
        metric = metric.strip()
        if not separator or not metric:
            raise InvalidPromotionError(
                f"--parity-deviation must look like METRIC=VALUE, got {item!r}"
            )
        if metric in deviations:
            raise InvalidPromotionError(f"--parity-deviation names '{metric}' twice")
        try:
            deviations[metric] = float(raw_value)
        except ValueError as exc:
            raise InvalidPromotionError(
                f"--parity-deviation value for '{metric}' is not a number: {raw_value!r}"
            ) from exc
    return deviations


def record_promotion(args: argparse.Namespace) -> dict[str, Any]:
    """Build the evidence from ``args`` and record the promotion.

    Raises ``InvalidPromotionError`` for anything missing, unreadable or below
    the thresholds in force; nothing is written in that case.
    """
    # Imported here: the live runner pulls in broker adapters, which the
    # backtest commands of this CLI have no business loading.
    from live_runner.promotion import PromotionTargetError, signal_bot_target, strategy_target

    params = _parse_params(args.params)
    try:
        if args.signal_processor:
            target = signal_bot_target(args.signal_processor, params)
        else:
            if params:
                raise InvalidPromotionError(
                    "--params applies to --signal-processor only; a strategy is "
                    "promoted with its STRATEGY_META defaults"
                )
            target = strategy_target(args.strategy)
    except PromotionTargetError as exc:
        raise InvalidPromotionError(str(exc)) from exc

    paper_report_file, paper_report_sha256 = report_reference(args.paper_report)
    parity_report_file, parity_report_sha256 = report_reference(args.parity_report)
    evidence = PromotionEvidence(
        out_of_sample=out_of_sample_evidence_from_result(args.optimization_result),
        paper_trading=PaperTradingEvidence.from_mapping(
            {
                "account_id": args.paper_account_id,
                "broker": args.paper_broker,
                "start": args.paper_start,
                "end": args.paper_end,
                "trade_count": args.paper_trades,
                "report_file": paper_report_file,
                "report_sha256": paper_report_sha256,
            }
        ),
        parity=ParityEvidence.from_mapping(
            {
                "reference": args.parity_reference,
                "deviations": _parse_deviations(args.parity_deviation),
                "report_file": parity_report_file,
                "report_sha256": parity_report_sha256,
            }
        ),
    )
    record = promote(
        target.path,
        kind=target.kind,
        parameters=target.parameters,
        evidence=evidence,
        promoted_by=args.promoted_by,
        root=target.root,
        thresholds=load_promotion_thresholds(),
    )
    return {"ledger": str(ledger_path_for(target.path)), "record": record}


def cmd_promote(args: argparse.Namespace) -> None:
    try:
        outcome = record_promotion(args)
    except InvalidPromotionError as exc:
        print(f"Promotion refused: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Promoted to production. Ledger: {outcome['ledger']}")
    print(json.dumps(outcome["record"], indent=2, sort_keys=True))
    print("Commit the ledger file: the promotion is only in force where the ledger is.")

"""Report pytest skips from JUnit XML and fail on money-path skips.

CI runs pytest with ``--junitxml``; this script reads the resulting files and
prints a markdown table of skipped tests grouped by skip reason, so a quietly
narrowed suite is visible in the job summary instead of hidden inside a green
build ("a skip is not a pass", AGENTS.md).

The report is appended to ``$GITHUB_STEP_SUMMARY`` when that variable is set.

Exit status is non-zero when a test on the money path was skipped for a reason
that is not explicitly allowed. The money-path set is ``MUST_NOT_SKIP_PATTERNS``
below; the allowed reasons are ``ALLOWED_SKIP_REASON_PATTERNS`` — gates that
can never open in CI (dataset opt-ins, generated-but-uncommitted artifacts,
the live production database, one documented engine limitation), whose
coverage gap is tracked by #289.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

GITHUB_STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

# The per-run pytest sandbox dir (tests/conftest.py) is embedded in dataset-gate
# skip messages; normalising it keeps one logical reason in one table row.
_SESSION_DIR_PATTERN = re.compile(r"tp-pytest-session-[A-Za-z0-9_-]+")

#: Money-path surfaces that must never skip silently in CI. Each pattern is
#: matched against the JUnit ``classname`` — pytest's dotted module path, e.g.
#: ``tests.live_runner.test_account_order_lock`` (a class-based testcase keeps
#: its class as a trailing component, which the ``^``-anchored prefixes still
#: match). The set is derived from the packages AGENTS.md names as the money
#: path: ``live_runner`` (the live order path), ``risk`` (the risk engine), and
#: the backtest engines and sizing under ``runner``/``strategies`` runtime.
MUST_NOT_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        # tests/live_runner/** — orders, kill switch, reconciliation, sizing.
        r"^tests\.live_runner\.",
        # The live surface outside the package tests: adapters, daemon,
        # preflight, promotion gate, broker fills.
        r"^tests\.test_live_",
        r"^tests\.test_alpaca_",
        r"^tests\.test_ctrader_",
        r"^tests\.test_broker_",
        r"^tests\.test_daemon",
        r"^tests\.test_preflight",
        r"^tests\.test_loss_breaker",
        r"^tests\.test_account_",
        r"^tests\.test_promotion",
        r"^tests\.test_deployment_policy",
        r"^tests\.test_credential",
        r"^tests\.test_market_data_stream",
        r"^tests\.test_shutdown_budget",
        # risk/ engine and its parity/validation coverage.
        r"^tests\.test_risk",
        r"^tests\.validation\.test_.*risk",
        r"^tests\.test_risk_parity",
        # Backtest engines (native, vectorbt, pynecore, lean) and their
        # contract/parity/sizing semantics.
        r"^tests\.test_engine",
        r"^tests\.test_native_",
        r"^tests\.test_vectorbt",
        r"^tests\.test_pynecore",
        r"^tests\.test_lean_adapter",
        r"^tests\.test_trade_engine",
        r"^tests\.test_cross_engine",
        r"^tests\.test_direction_strictness",
        r"^tests\.test_adapter_curve",
        r"^tests\.test_strategy_meta_adapters",
        r"^tests\.test_strategy_ir",
        # Sizing, fills, fees, slippage, exits — the money math itself.
        r"^tests\.test_sizing",
        r"^tests\.test_lot_grid",
        r"^tests\.test_realistic_fills",
        r"^tests\.test_slippage",
        r"^tests\.test_fees",
        r"^tests\.test_execution_timing",
        r"^tests\.test_fill_dedupe",
        r"^tests\.test_trailing_modes",
        r"^tests\.test_replay_supertrend",
        r"^tests\.test_signal_replay_adapter",
        r"^tests\.test_stockkit",
        # Backtest runners/reporting and portfolio-level money metrics.
        r"^tests\.test_backtest",
        r"^tests\.test_walkforward",
        r"^tests\.test_portfolio",
        r"^tests\.test_mtm_equity",
        r"^tests\.test_daily_equity",
        r"^tests\.test_missing_data_failures",
        r"^tests\.test_differential_reference",
        r"^tests\.test_optimization",
        r"^tests\.test_optimizer",
    )
)

#: Skip reasons a money-path test may still have in CI. These are gates that
#: can never open in CI — closing them is #289's job, not this report's —
#: everything else on the money path must run.
ALLOWED_SKIP_REASON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # The dataset gate: real-market-data tests skip when no local OHLCV
        # dataset is exported. CI has none by design (#289).
        r"TP_TEST_OHLCV_DIR",
        # The downloaded differential real-data slice (scripts/
        # fetch_differential_slice.py); absent on a Dukascopy outage.
        r"differential real-data slice",
        r"DIFFERENTIAL_REAL_SLICE_CSV",
        # MT5 parity artifacts are generated locally by
        # scripts/regenerate_mt5_parity_artifacts.py and deliberately not
        # committed, so no CI checkout can ever contain them.
        r"not present in this checkout",
        r"generated locally, not committed",
        # Production-corpus drift checks need a copy of the live database,
        # which is not in the repository and must never be opened by CI.
        r"TRADING_PRODUCTION_DB",
        # vectorbt cannot enforce protective exits without intrabar data —
        # a documented engine limitation (test_differential_reference_
        # strategies.py), not an environment gap CI could close.
        r"vectorbt has no protective exits",
    )
)


@dataclass(frozen=True)
class SkippedTest:
    """One ``<testcase>`` that carries a ``<skipped>`` child."""

    classname: str
    name: str
    reason: str
    source: str

    @property
    def label(self) -> str:
        return f"{self.classname}::{self.name}"


def is_money_path(classname: str) -> bool:
    return any(pattern.search(classname) for pattern in MUST_NOT_SKIP_PATTERNS)


def is_allowed_skip(reason: str) -> bool:
    return any(pattern.search(reason) for pattern in ALLOWED_SKIP_REASON_PATTERNS)


def normalize_reason(reason: str) -> str:
    """Collapse per-run volatile text so one logical reason forms one group."""
    return _SESSION_DIR_PATTERN.sub("tp-pytest-session-…", reason).strip()


def _skipped_reason(skipped: ET.Element) -> str:
    message = (skipped.get("message") or "").strip()
    if message:
        return message
    text = (skipped.text or "").strip()
    return re.sub(r"^Skipped:\s*", "", text)


def parse_junit(path: Path) -> list[SkippedTest]:
    """Every skipped testcase in one pytest JUnit XML file."""
    root = ET.parse(path).getroot()
    skipped: list[SkippedTest] = []
    for case in root.iter("testcase"):
        for child in case:
            if child.tag != "skipped":
                continue
            skipped.append(
                SkippedTest(
                    classname=case.get("classname") or "",
                    name=case.get("name") or "",
                    reason=_skipped_reason(child),
                    source=str(path),
                )
            )
            break
    return skipped


def collect_skips(paths: list[Path]) -> tuple[list[SkippedTest], list[str]]:
    """Parse every existing XML file; report the ones that are absent."""
    skipped: list[SkippedTest] = []
    missing: list[str] = []
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        skipped.extend(parse_junit(path))
    return skipped, missing


def violations(skipped: list[SkippedTest]) -> list[SkippedTest]:
    """Money-path skips whose reason is not on the allowed list."""
    return [
        test
        for test in skipped
        if is_money_path(test.classname) and not is_allowed_skip(test.reason)
    ]


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_report(skipped: list[SkippedTest], sources: list[str]) -> str:
    """The markdown table appended to the step summary and stdout."""
    lines = ["## Pytest skip report", ""]
    lines.append(f"JUnit input: {', '.join(sources)}")
    lines.append(f"Skipped tests: {len(skipped)}")
    if not skipped:
        lines.append("")
        lines.append("No skips.")
        return "\n".join(lines) + "\n"

    groups: dict[str, list[SkippedTest]] = defaultdict(list)
    for test in skipped:
        groups[normalize_reason(test.reason)].append(test)

    bad = violations(skipped)
    lines.append("")
    if bad:
        lines.append(
            f"**{len(bad)} money-path test(s) skipped for a disallowed reason** — "
            "CI is not exercising them:"
        )
        lines.append("")
        lines.extend(f"- `{t.label}` — {_truncate(t.reason)}" for t in bad)
        lines.append("")

    lines.append("| Skip reason | Count | Examples |")
    lines.append("|---|---:|---|")
    for reason in sorted(groups, key=lambda r: (-len(groups[r]), r)):
        members = groups[reason]
        money = any(is_money_path(t.classname) for t in members)
        if money and is_allowed_skip(reason):
            marker = " · env-gated"
        elif money:
            marker = " ⚠ money-path"
        else:
            marker = ""
        examples = "<br>".join(
            f"`{_truncate(t.label, 80)}`" for t in members[:3]
        )
        if len(members) > 3:
            examples += f"<br>…and {len(members) - 3} more"
        lines.append(
            f"| {_truncate(reason)}{marker} | {len(members)} | {examples} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "junit_xml",
        nargs="+",
        type=Path,
        help="pytest --junitxml output file(s) to read",
    )
    args = parser.parse_args(argv)

    skipped, missing = collect_skips(args.junit_xml)
    for path in missing:
        print(f"warning: JUnit XML not found, skipping it: {path}", file=sys.stderr)

    existing = [str(p) for p in args.junit_xml if p.is_file()]
    report = build_report(skipped, existing)
    print(report)

    summary = os.environ.get(GITHUB_STEP_SUMMARY_ENV, "").strip()
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")

    bad = violations(skipped)
    if bad:
        print(
            f"error: {len(bad)} money-path test(s) skipped for a disallowed reason",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

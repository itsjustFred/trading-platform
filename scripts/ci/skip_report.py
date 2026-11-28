"""Report pytest skips from JUnit XML and fail on money-path skips.

CI runs pytest with ``--junitxml``; this script reads the resulting files and
prints a markdown table of skipped tests grouped by skip reason, so a quietly
narrowed suite is visible in the job summary instead of hidden inside a green
build ("a skip is not a pass", AGENTS.md).

The report is appended to ``$GITHUB_STEP_SUMMARY`` when that variable is set.

Exit status is non-zero when a test on the money path was skipped for a reason
that is not explicitly allowed. The money-path set is ``MUST_NOT_SKIP_PATTERNS``
below; the allowed skips are ``ALLOWED_SKIPS`` — ``(module, reason)`` pairs for
gates that can never open in CI, each scoped to the module that emits it.
XFail outcomes (``<skipped type="pytest.xfail">``) are reported separately and
never count as violations: an xfail is an expected outcome, not a hidden skip.
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
        # Live signal-level resolution shared by the broker adapters
        # (risk.signal_levels / risk.live).
        r"^tests\.test_signal_levels",
        # risk/ engine and its parity/validation coverage.
        r"^tests\.test_risk",
        r"^tests\.validation\.test_.*risk",
        r"^tests\.test_risk_parity",
        # Backtest engines (native, vectorbt, pynecore, lean) and their
        # contract/parity/sizing semantics.
        r"^tests\.test_engine",
        r"^tests\.test_native_",
        # vectorbt engine coverage goes with #318; until then these tests must
        # keep running (their documented-limitation skips are allowed below).
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
        # test_ohlcv_store verifies the store→consumer contract (sizing,
        # slippage resolve the manifest's asset_class), so it imports the
        # money path and must never skip either.
        r"^tests\.test_ohlcv_store",
        r"^tests\.test_sizing",
        r"^tests\.test_lot_grid",
        r"^tests\.test_realistic_fills",
        r"^tests\.test_bid_ask_fills",
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
        # Engine parity against the reference library, engine residual
        # verification, commission math in the live adapters, risk-engine
        # consumers and the runner CLI surface.
        r"^tests\.parity\.",
        r"^tests\.test_verified_residuals",
        r"^tests\.test_commission",
        r"^tests\.test_signal_bots_risk",
        r"^tests\.test_runner",
    )
)

#: ``(module pattern, reason pattern)`` pairs: the only skips a money-path
#: module may have in CI. Each pair names the module that emits the reason, so
#: a lookalike message from a different module still fails. Everything else on
#: the money path must run.
ALLOWED_SKIPS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    # tests/test_trade_engine.py real-data smoke needs the local OHLCV opt-in;
    # the suite's DATA_DIR is a throwaway dir, so CI has no dataset (#289).
    (
        re.compile(r"^tests\.test_trade_engine"),
        re.compile(r"TP_TEST_OHLCV_DIR", re.IGNORECASE),
    ),
    # tests/test_differential_reference_strategies.py: the downloaded real-data
    # slice is absent on a Dukascopy outage (PRs warn-and-skip, #289) ...
    (
        re.compile(r"^tests\.test_differential_reference_strategies"),
        re.compile(
            r"differential real-data slice|DIFFERENTIAL_REAL_SLICE_CSV",
            re.IGNORECASE,
        ),
    ),
    # ... and vectorbt cannot enforce protective exits without intrabar data —
    # a documented engine limitation, not an environment gap. This allowance
    # and the ``^tests\.test_vectorbt`` pattern go with #318.
    (
        re.compile(r"^tests\.test_differential_reference_strategies"),
        re.compile(r"vectorbt has no protective exits", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class SkippedTest:
    """One ``<testcase>`` that carries a ``<skipped>`` child.

    ``module`` is the dotted module path used for money-path matching. For a
    normal testcase it is the JUnit ``classname``; for a module skipped at
    collection (``pytest.importorskip`` / ``allow_module_level``) pytest emits
    an empty ``classname`` and puts the module path in ``name`` instead, so the
    two are folded together here — otherwise a whole-module skip would bypass
    the gate.
    """

    module: str
    name: str
    reason: str
    kind: str  # "skip" or "xfail" (pytest.xfail)
    source: str

    @property
    def label(self) -> str:
        if self.name == self.module:
            return self.module
        return f"{self.module}::{self.name}"


def is_money_path(module: str) -> bool:
    return any(pattern.search(module) for pattern in MUST_NOT_SKIP_PATTERNS)


def is_allowed_skip(module: str, reason: str) -> bool:
    return any(
        module_pattern.search(module) and reason_pattern.search(reason)
        for module_pattern, reason_pattern in ALLOWED_SKIPS
    )


def normalize_reason(reason: str) -> str:
    """Collapse per-run volatile text so one logical reason forms one group."""
    return _SESSION_DIR_PATTERN.sub("tp-pytest-session-…", reason).strip()


_COLLECTION_SKIP_MESSAGE = "collection skipped"


def _reason_from_skipped_text(text: str) -> str:
    """Pull the ``Skipped: …`` reason out of a collection-skip payload.

    pytest writes ``('path', line, "Skipped: reason")`` — a repr tuple, with
    either quote style — into the element text when ``message`` only says
    "collection skipped".
    """
    match = re.search(r"Skipped:\s*(.*)", text, re.DOTALL)
    if match is None:
        return text.strip()
    reason = match.group(1).strip()
    # Drop the tuple's closing quote and paren: '…")' or "…')".
    return re.sub(r"['\"]\)?\s*$", "", reason).strip()


def _skipped_reason(skipped: ET.Element) -> str:
    message = (skipped.get("message") or "").strip()
    if message and message != _COLLECTION_SKIP_MESSAGE:
        return message
    return _reason_from_skipped_text((skipped.text or "").strip())


def _module_from_case(classname: str, name: str) -> str:
    """Best-effort dotted module path for a skipped testcase.

    Normal testcases carry the dotted module in ``classname``; a module
    skipped at collection carries it in ``name`` instead. pytest's
    ``mangle_test_address`` already emits dotted paths, but normalise
    separators and a trailing ``.py`` so a path-shaped value from another
    pytest version still classifies as money-path rather than slipping
    through as an unmatched module.
    """
    module = (classname or name).replace("/", ".").replace("\\", ".")
    if module.endswith(".py"):
        module = module[: -len(".py")]
    return module


def parse_junit(path: Path) -> list[SkippedTest]:
    """Every skipped testcase in one pytest JUnit XML file."""
    root = ET.parse(path).getroot()
    skipped: list[SkippedTest] = []
    for case in root.iter("testcase"):
        for child in case:
            if child.tag != "skipped":
                continue
            classname = (case.get("classname") or "").strip()
            name = (case.get("name") or "").strip()
            skipped.append(
                SkippedTest(
                    module=_module_from_case(classname, name),
                    name=name,
                    reason=_skipped_reason(child),
                    kind=(child.get("type") or "").strip() or "pytest.skip",
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
    """Money-path skips whose (module, reason) pair is not on the allowed list.

    XFail outcomes are excluded: ``pytest.xfail`` is an expected result the
    suite already reports, not a hidden skip.
    """
    return [
        test
        for test in skipped
        if test.kind != "pytest.xfail"
        and is_money_path(test.module)
        and not is_allowed_skip(test.module, test.reason)
    ]


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _skip_groups(tests: list[SkippedTest]) -> dict[str, list[SkippedTest]]:
    groups: dict[str, list[SkippedTest]] = defaultdict(list)
    for test in tests:
        groups[normalize_reason(test.reason)].append(test)
    return groups


def _reason_table(
    groups: dict[str, list[SkippedTest]], *, money_markers: bool = True
) -> list[str]:
    lines = ["| Reason | Count | Examples |", "|---|---:|---|"]
    for reason in sorted(groups, key=lambda r: (-len(groups[r]), r)):
        members = groups[reason]
        money = [t for t in members if is_money_path(t.module)]
        bad = [
            t
            for t in money
            if t.kind != "pytest.xfail"
            and not is_allowed_skip(t.module, t.reason)
        ]
        if not money_markers:
            marker = ""
        elif bad:
            marker = " ⚠ money-path"
        elif money:
            marker = " · env-gated"
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
    return lines


def build_report(skipped: list[SkippedTest], sources: list[str]) -> str:
    """The markdown report appended to the step summary and stdout."""
    skips = [t for t in skipped if t.kind != "pytest.xfail"]
    xfails = [t for t in skipped if t.kind == "pytest.xfail"]

    lines = ["## Pytest skip report", ""]
    lines.append(f"JUnit input: {', '.join(sources)}")
    lines.append(f"Skipped tests: {len(skips)}")
    if xfails:
        lines.append(f"XFailed tests: {len(xfails)} (expected outcomes, not counted)")
    if not skipped:
        lines.append("")
        lines.append("No skips.")
        return "\n".join(lines) + "\n"

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

    lines.append("### Skips by reason")
    lines.append("")
    if skips:
        lines.extend(_reason_table(_skip_groups(skips)))
    else:
        lines.append("None.")

    if xfails:
        lines.append("")
        lines.append("### XFail by reason")
        lines.append("")
        lines.extend(_reason_table(_skip_groups(xfails), money_markers=False))
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
        # A missing input means the skip report cannot vouch for the run —
        # fail loudly rather than let money-path skips pass unreported.
        print(f"error: JUnit XML not found: {path}", file=sys.stderr)

    existing = [str(p) for p in args.junit_xml if p.is_file()]
    report = build_report(skipped, existing)
    print(report)

    summary = os.environ.get(GITHUB_STEP_SUMMARY_ENV, "").strip()
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")

    if missing:
        return 2
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

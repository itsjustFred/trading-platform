"""Tests for scripts/ci/skip_report.py — the CI skip-visibility gate."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from scripts.ci.skip_report import (
    build_report,
    collect_skips,
    is_allowed_skip,
    is_money_path,
    main,
    normalize_reason,
    parse_junit,
    violations,
)
from tests.conftest import sandboxed_env


def _junit(cases: list[tuple[str, str, str | None]]) -> str:
    """Minimal pytest JUnit XML. ``cases`` are (classname, name, reason|None)."""
    body = ""
    for classname, name, reason in cases:
        skipped = ""
        if reason is not None:
            skipped = f'<skipped type="pytest.skip" message="{reason}">detail</skipped>'
        body += (
            f'<testcase classname="{classname}" name="{name}" time="0.1">'
            f"{skipped}</testcase>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" tests="{len(cases)}">{body}</testsuite>'
        "</testsuites>"
    )


def _write(tmp_path: Path, name: str, cases: list[tuple[str, str, str | None]]) -> Path:
    path = tmp_path / name
    path.write_text(_junit(cases), encoding="utf-8")
    return path


def test_parse_junit_collects_only_skipped_cases(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [
            ("tests.test_a", "test_runs", None),
            ("tests.test_a", "test_skips", "no dataset"),
            ("tests.live_runner.test_x", "test_money", "boom"),
        ],
    )
    skipped = parse_junit(path)
    assert [t.label for t in skipped] == [
        "tests.test_a::test_skips",
        "tests.live_runner.test_x::test_money",
    ]
    assert skipped[0].reason == "no dataset"


def test_parse_junit_falls_back_to_element_text(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuite><testcase classname="tests.test_a" name="t">'
        '<skipped type="pytest.skip">Skipped: bare reason</skipped>'
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    (test,) = parse_junit(path)
    assert test.reason == "bare reason"


def test_parse_junit_collection_skip_uses_name_as_module(tmp_path: Path) -> None:
    """A module skipped at collection has empty classname and the module path
    in ``name``; without the fallback the whole-module skip bypasses the gate.
    """
    # Build a real pytest JUnit file: a package tests/live_runner whose module
    # importorskips at module level, exactly like test_ctrader_live_fix.py.
    # The project dir is clean so pytest derives the module name
    # "tests.live_runner.test_ctrader_live_fix" the way the repo does.
    proj = tmp_path / "proj"
    pkg = proj / "tests" / "live_runner"
    pkg.mkdir(parents=True)
    # An ini file pins rootdir to proj so nodeids — and therefore the JUnit
    # ``name`` attribute — are "tests.live_runner.test_..." exactly as in the
    # real repo, whose rootdir is the checkout root.
    (proj / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (proj / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "test_ctrader_live_fix.py").write_text(
        "import pytest\n"
        "pytest.importorskip('definitely_missing_extra_xyz')\n"
        "def test_never_runs():\n"
        "    pass\n",
        encoding="utf-8",
    )
    # A passing sibling keeps pytest's exit code at 0 — an all-skipped run
    # exits 5 (NO_TESTS_COLLECTED), which is not what CI produces.
    (proj / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8"
    )
    junit = proj / "junit.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            "tests",
        ],
        capture_output=True,
        text=True,
        cwd=proj,
        env=sandboxed_env(),
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert junit.is_file()

    (test,) = parse_junit(junit)
    assert test.module == "tests.live_runner.test_ctrader_live_fix"
    assert test.label == test.module  # no "::test" suffix for a module skip
    assert "could not import 'definitely_missing_extra_xyz'" in test.reason
    assert is_money_path(test.module)
    assert violations([test]) == [test]
    assert main([str(junit)]) == 1


def test_parse_junit_collection_skip_with_path_shaped_name(tmp_path: Path) -> None:
    """If a pytest version emits the module as a path instead of a dotted
    name, separators and ``.py`` are normalised so the money-path match
    still fires."""
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuite><testcase classname="" name="tests/live_runner/test_x.py">'
        '<skipped message="collection skipped">('
        "'tests/live_runner/test_x.py', 1, \"Skipped: could not import 'nope'\""
        ")</skipped></testcase></testsuite>",
        encoding="utf-8",
    )
    (test,) = parse_junit(path)
    assert test.module == "tests.live_runner.test_x"
    assert is_money_path(test.module)
    assert violations([test]) == [test]


def test_parse_junit_xfail_is_not_a_violation(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuite><testcase classname="tests.test_risk_engine" name="t">'
        '<skipped type="pytest.xfail" message="known engine gap"/>'
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    (test,) = parse_junit(path)
    assert test.kind == "pytest.xfail"
    assert violations([test]) == []
    report = build_report([test], [str(path)])
    assert "XFailed tests: 1" in report
    assert "### XFail by reason" in report


def test_collect_skips_warns_on_missing_files(tmp_path: Path) -> None:
    present = _write(tmp_path, "a.xml", [("tests.test_a", "t", "why")])
    skipped, missing = collect_skips([present, tmp_path / "absent.xml"])
    assert len(skipped) == 1
    assert missing == [str(tmp_path / "absent.xml")]


def test_is_money_path_covers_live_runner_and_engine_tests() -> None:
    assert is_money_path("tests.live_runner.test_loss_breaker")
    assert is_money_path("tests.test_risk_engine.TestGrouping")
    assert is_money_path("tests.test_vectorbt_sizing_semantics")
    assert is_money_path("tests.test_live_daemon_sizing")
    assert not is_money_path("tests.test_safe_path")
    assert not is_money_path("tests.strategies.test_channel_breakout")


def test_is_allowed_skip_is_scoped_to_the_emitting_module() -> None:
    trade_engine = "tests.test_trade_engine"
    diff_ref = "tests.test_differential_reference_strategies"
    # Real messages, verbatim from the tests that emit them.
    assert is_allowed_skip(
        trade_engine,
        "No local 1h OHLCV parquet data under /tmp/tp-pytest-session-x/data/ohlcv: "
        "set TP_TEST_OHLCV_DIR=/path/to/ohlcv to run this real-data smoke test "
        "(the suite's DATA_DIR is a throwaway temp directory)",
    )
    assert is_allowed_skip(
        diff_ref,
        "the differential real-data slice is missing at /x "
        "(point DIFFERENTIAL_REAL_SLICE_CSV at an existing slice)",
    )
    assert is_allowed_skip(
        diff_ref,
        "documented: vectorbt has no protective exits without intrabar data",
    )
    # The same reasons on any other module — including other money-path
    # modules — are not allowed.
    assert not is_allowed_skip(
        "tests.live_runner.test_x",
        "set TP_TEST_OHLCV_DIR=/data to run this real-data smoke test",
    )
    assert not is_allowed_skip(
        "tests.test_safe_path",
        "set TP_TEST_OHLCV_DIR=/data",
    )
    # Lookalike messages that no money-path module emits stay disallowed even
    # on the modules that do have allowances.
    assert not is_allowed_skip(trade_engine, "pynecore is not installed")
    assert not is_allowed_skip(diff_ref, "needs bash")
    assert not is_allowed_skip(
        "tests.test_mt5_parity_artifacts_fresh",
        "intraday_volatility_breakout_eurusd_h1.json not present in this checkout",
    )
    assert not is_allowed_skip(
        "tests.test_backfill_sharpe_column",
        "set TRADING_PRODUCTION_DB to a production copy to check the corpus",
    )


def test_normalize_reason_collapses_session_tmp_dirs() -> None:
    a = "missing data under /tmp/tp-pytest-session-aaa/data/ohlcv"
    b = "missing data under /tmp/tp-pytest-session-bbb/data/ohlcv"
    assert normalize_reason(a) == normalize_reason(b)


def test_violations_only_flags_money_path_disallowed_skips(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [
            ("tests.live_runner.test_x", "test_skips", "unexpected env"),
            # An allowed reason on the wrong module is still a violation.
            ("tests.live_runner.test_x", "test_dataset", "set TP_TEST_OHLCV_DIR=x"),
            # The same reason on its emitting module is allowed.
            ("tests.test_trade_engine", "test_smoke", "set TP_TEST_OHLCV_DIR=x"),
            ("tests.test_safe_path", "test_skips", "unexpected env"),
        ],
    )
    bad = violations(parse_junit(path))
    assert [t.name for t in bad] == ["test_skips", "test_dataset"]


def test_build_report_groups_by_reason_and_marks_money_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [
            ("tests.test_a", "t1", "same reason"),
            ("tests.test_b", "t2", "same reason"),
            ("tests.live_runner.test_x", "t3", "money reason"),
        ],
    )
    skipped = parse_junit(path)
    report = build_report(skipped, [str(path)])
    assert "Skipped tests: 3" in report
    assert "| same reason | 2 |" in report
    assert "money reason ⚠ money-path" in report
    assert "1 money-path test(s) skipped" in report
    assert "`tests.live_runner.test_x::t3`" in report


def test_build_report_marks_allowed_money_path_groups_env_gated(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [
            (
                "tests.test_trade_engine",
                "test_smoke",
                "No local 1h OHLCV parquet data under /x: set TP_TEST_OHLCV_DIR=/d",
            ),
        ],
    )
    report = build_report(parse_junit(path), [str(path)])
    assert "env-gated" in report
    assert "⚠ money-path" not in report


def test_main_writes_github_step_summary(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, "junit.xml", [("tests.test_a", "t", "why not")])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main([str(path)]) == 0
    assert "## Pytest skip report" in summary.read_text(encoding="utf-8")


def test_main_exits_nonzero_on_money_path_skip(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [("tests.test_risk_engine", "test_x", "not a dataset gate")],
    )
    assert main([str(path)]) == 1


def test_main_exits_zero_when_only_dataset_gated_money_path_skips(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "junit.xml",
        [("tests.test_trade_engine", "test_x", "set TP_TEST_OHLCV_DIR=/data")],
    )
    assert main([str(path)]) == 0


def test_main_fails_loudly_when_a_junit_input_is_missing(tmp_path: Path) -> None:
    """A missing JUnit file must not pass silently: no report, no verdict."""
    assert main([str(tmp_path / "absent.xml")]) == 2


# ── money-path classification completeness ──────────────────────────────────
# A new test module that imports the money path (live_runner, risk, the
# backtest engines) but matches no MUST_NOT_SKIP_PATTERNS entry would let its
# skips pass unreported. This test forces every such module to be classified:
# either it matches a pattern, or it is named below with the reason it is not
# a money-path test. Update the lists together — that is the point.

# The money-path modules a test can import, enumerated explicitly: the live
# order path, the risk engine, and the runner modules that carry the trade
# math — engine(s), fees, slippage, and the backtest runners/optimizers that
# MUST_NOT_SKIP_PATTERNS classifies. Both ``runner.x`` and ``from runner
# import x`` forms are matched. Deliberately out of scope, matching the
# MUST_NOT_SKIP_PATTERNS boundary: runner.stats/strategy_ir/converters/grid/
# cli/agent_*/llm_* (reporting, codegen, plumbing), shared.* (data models —
# the math lives in runner/risk), data_tools, validation.*, signal_bots,
# scheduler.
_MONEY_PATH_RUNNER = (
    r"(?:engines|engine|fees|slippage|daily_equity|walkforward|optimizer"
    r"|portfolio_optimizer)\b"
)
_MONEY_PATH_IMPORT = re.compile(
    r"^\s*(?:"
    r"(?:from|import)\s+(?:live_runner|risk)\b"
    r"|(?:from|import)\s+runner\." + _MONEY_PATH_RUNNER +
    r"|from\s+runner\s+import\s+" + _MONEY_PATH_RUNNER +
    r")",
    re.M,
)

_NON_MONEY_PATH_MODULES = {
    "tests.builders.test_builders": (
        "fixture-builder contract tests; engine/model imports only "
        "type-check builder output"
    ),
    "tests.test_alerts": (
        "ops alert sink plumbing; touches live_runner.daemon only for "
        "control-plane wiring"
    ),
    "tests.test_discord_signal_parsing": (
        "Discord message text parsing; feeds ingestion, not order/risk math"
    ),
    "tests.test_models": (
        "shared model shapes; the runner.daily_equity import only exercises "
        "result attachment, not order/risk math"
    ),
    "tests.test_packaging": "packaging and wheel surface checks",
    "tests.test_resample": (
        "OHLCV resample utility; imports execution_timing for a candle-close edge"
    ),
    "tests.test_service_discovery": "service/account discovery, not order math",
    "tests.test_stats_ruin_consumers": (
        "stats consumers; live_runner import covers promotion-score plumbing"
    ),
    "tests.test_strategy_registry_static": (
        "static strategy-registry contract checks; the engine imports only "
        "read alias/key tables, no trading math runs"
    ),
    "tests.test_suite_isolation": "suite hermeticity self-guard",
    "tests.test_symbol_class": "symbol classification / market-data plumbing",
    "tests.test_universe_provenance": (
        "universe provenance tracking around optimizer artifacts; not "
        "order/risk math"
    ),
    "tests.test_venue_calendar": "venue calendar helpers",
}

# The dataset-gated strategy suite (#289): every skip it emits is the data
# gate, which the report already counts — classifying each file would add
# noise, not coverage. This exemption goes away with #289, when the gate is
# closed and the suite either runs or is removed.
_NON_MONEY_PATH_PREFIXES = ("tests.strategies.",)


def test_every_money_path_importing_module_is_classified() -> None:
    # An exemption must never cover a money-path module: that would let its
    # skips pass unreported while looking reviewed.
    misclassified = [
        module
        for module in (*_NON_MONEY_PATH_MODULES, *_NON_MONEY_PATH_PREFIXES)
        if is_money_path(module)
    ]
    assert not misclassified, (
        "money-path modules cannot be exempted in _NON_MONEY_PATH_MODULES / "
        "_NON_MONEY_PATH_PREFIXES: " + ", ".join(misclassified)
    )

    tests_root = Path(__file__).resolve().parents[1] / "tests"
    unclassified: list[str] = []
    for path in sorted(tests_root.rglob("test_*.py")):
        classname = "tests." + ".".join(
            path.relative_to(tests_root).with_suffix("").parts
        )
        if classname in _NON_MONEY_PATH_MODULES or classname.startswith(
            _NON_MONEY_PATH_PREFIXES
        ):
            continue
        if not _MONEY_PATH_IMPORT.search(path.read_text(encoding="utf-8")):
            continue
        if not is_money_path(classname):
            unclassified.append(classname)
    assert not unclassified, (
        "these test modules import the money path but are neither matched by "
        "MUST_NOT_SKIP_PATTERNS nor exempted in _NON_MONEY_PATH_MODULES — "
        "classify them so their skips cannot pass silently: "
        + ", ".join(unclassified)
    )

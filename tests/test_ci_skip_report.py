"""Tests for scripts/ci/skip_report.py — the CI skip-visibility gate."""

from __future__ import annotations

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


def test_is_allowed_skip_only_for_gates_ci_can_never_open() -> None:
    assert is_allowed_skip(
        "missing OHLCV dataset for EURUSD/15m covering 2025-01-01..2025-03-31 "
        "under /tmp/tp-pytest-session-x/data/ohlcv (point TP_TEST_OHLCV_DIR at "
        "a local dataset root to use one)"
    )
    assert is_allowed_skip(
        "the differential real-data slice is missing at /x "
        "(DIFFERENTIAL_REAL_SLICE_CSV)"
    )
    # Real messages, verbatim from the tests that emit them.
    assert is_allowed_skip(
        "intraday_volatility_breakout_eurusd_h1.json not present in this checkout"
    )
    assert is_allowed_skip(
        "intraday_volatility_breakout_sp500_m1.json not present (artifacts are "
        "generated locally, not committed); run "
        "scripts/regenerate_mt5_parity_artifacts.py"
    )
    assert is_allowed_skip(
        "set TRADING_PRODUCTION_DB to a production copy to check the corpus"
    )
    assert is_allowed_skip(
        "set TRADING_PRODUCTION_DB to a copy of production to re-verify its defaults"
    )
    assert is_allowed_skip(
        "documented: vectorbt has no protective exits without intrabar data"
    )
    assert is_allowed_skip(
        "No local 1h OHLCV parquet data under /tmp/tp-pytest-session-x/data/ohlcv: "
        "set TP_TEST_OHLCV_DIR=/path/to/ohlcv to run this real-data smoke test "
        "(the suite's DATA_DIR is a throwaway temp directory)"
    )
    assert not is_allowed_skip("pynecore is not installed in this environment")
    assert not is_allowed_skip("needs bash")


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
            ("tests.live_runner.test_x", "test_dataset", "set TP_TEST_OHLCV_DIR=x"),
            ("tests.test_safe_path", "test_skips", "unexpected env"),
        ],
    )
    bad = violations(parse_junit(path))
    assert [t.name for t in bad] == ["test_skips"]


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

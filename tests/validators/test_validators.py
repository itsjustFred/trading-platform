"""Regression tests for the AI-quality validators (Cleanup 17-B).

Covers the denylist-bypass gap in ``AICodeValidator`` and the
string-"NaN" bypass in ``BacktestResultValidator`` — plus the missing
negative control: until now the fixture gate asserted only that fixtures
PASS, so a validator regressing to accepting garbage would go unnoticed
(ox-alpha C5/M18, M12).

Containment note (code review finding, Cleanup 17-B self-review):
``AICodeValidator`` historically EXECUTED the submitted code in a
subprocess to check the interface. Module-level statements therefore ran
with full user privileges (``python -I`` isolates sys.path/env, not the
OS) — validating a strategy containing ``shutil.rmtree(...,
ignore_errors=True)`` executed it. The validator is now static: it
parses and AST-checks the code and verifies the interface by
inspection, never by running it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.validators.backtest_validator import BacktestExpectation, BacktestResultValidator
from tests.validators.code_validator import AICodeValidator

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "ai_generated_strategies"
INVALID_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "invalid_strategies"


def _result_for(code: str, name: str = "checked_strategy"):
    return AICodeValidator().validate(code=code, strategy_name=name)


def _valid_strategy_code() -> str:
    return (
        "from __future__ import annotations\n"
        "import pandas as pd\n\n"
        "def get_name() -> str:\n"
        "    return 'Valid'\n\n"
        "def get_parameters() -> dict:\n"
        "    return {'fast': 10}\n\n"
        "def generate_signals(df, params):\n"
        "    close = df['close'].astype(float)\n"
        "    fast = close.ewm(span=10, adjust=False).mean()\n"
        "    slow = close.ewm(span=30, adjust=False).mean()\n"
        "    return fast > slow, fast < slow\n"
    )


# ─── Positive controls (fixture gate) ────────────────────────────────────────


def test_positive_fixture_gate_is_not_empty():
    """M18: a lost fixtures directory must fail loudly, not pass vacuously."""
    fixtures = sorted(FIXTURES_DIR.glob("*.py"))
    assert fixtures, f"No positive AI fixtures found in {FIXTURES_DIR}"


@pytest.mark.parametrize("fixture_path", sorted((FIXTURES_DIR).glob("*.py")), ids=lambda p: p.stem)
def test_positive_fixtures_pass(fixture_path: Path):
    result = _result_for(fixture_path.read_text(encoding="utf-8"), fixture_path.stem)
    assert result.passed, f"{fixture_path.name} failed validation: {result.errors}"


def test_intentionally_broken_fixture_is_not_actually_valid_by_name_only():
    """C5: the 'intentionally broken' fixture must not silently double as a
    negative control — it is semantically broken but syntactically valid,
    so the suite needs real negative fixtures (see invalid_strategies)."""
    broken = FIXTURES_DIR / "intentionally_broken_strategy.py"
    if not broken.exists():
        pytest.skip("intentionally_broken_strategy fixture not present")
    result = _result_for(broken.read_text(encoding="utf-8"), broken.stem)
    assert result.passed, (
        "This fixture is a POSITIVE control: it documents that semantic "
        f"'brokenness' is not caught by the validator. Errors: {result.errors}"
    )


# ─── Negative controls (real rejection path) ────────────────────────────────


def test_negative_fixture_gate_is_not_empty():
    negatives = sorted(INVALID_FIXTURES_DIR.glob("*.py"))
    assert negatives, f"No negative-control fixtures found in {INVALID_FIXTURES_DIR}"


@pytest.mark.parametrize(
    "fixture_path", sorted((INVALID_FIXTURES_DIR).glob("*.py")), ids=lambda p: p.stem
)
def test_negative_fixtures_fail(fixture_path: Path):
    result = _result_for(fixture_path.read_text(encoding="utf-8"), fixture_path.stem)
    assert not result.passed, (
        f"Negative control {fixture_path.name} unexpectedly PASSED — "
        "the validator lost its rejection path"
    )


def test_validator_never_executes_submitted_code(tmp_path):
    """Containment: module-level statements must not run during validation.

    Historically the validator wrote the code to a file and ran
    ``spec.loader.exec_module`` in a subprocess; module-level destructive
    code executed with full user privileges. The validator must decide
    from the AST only — proven here by a module-level payload that writes
    a sentinel file IF it is ever executed.
    """
    sentinel = tmp_path / "executed.sentinel"
    assert not sentinel.exists()

    payload = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n\n"
        + _valid_strategy_code()
    )
    result = _result_for(payload)

    assert not sentinel.exists(), (
        "The validator executed the submitted code: module-level statements "
        "must never run during validation"
    )
    # The dangerous top-level side effect must itself be flagged.
    assert not result.passed, f"Validator accepted module-level side effects: {result.errors}"


# ─── AICodeValidator: basic detection ────────────────────────────────────────


def test_valid_strategy_passes():
    result = _result_for(_valid_strategy_code())
    assert result.passed, result.errors


def test_missing_required_method_fails():
    result = _result_for("def get_name():\n    return 'x'\n")
    assert not result.passed
    assert any("generate_signals" in error for error in result.errors)


# ─── AICodeValidator: denylist (bypass regressions) ──────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "eval('1+1')",
        "exec('x=1')",
        "import os\nos.system('id')",
        "import subprocess\nsubprocess.run(['ls'])",
        "getattr(__builtins__, 'eval')",
        "__import__('os').system('id')",
        "import shutil\nshutil.rmtree('x', ignore_errors=True)",
        "import socket\nsocket.socket()",
    ],
)
def test_dangerous_code_fails(code: str):
    result = _result_for(code + "\n" + _valid_strategy_code())
    assert not result.passed, f"Validator accepted dangerous code:\n{code}"


@pytest.mark.parametrize("module", ["os", "socket", "shutil", "subprocess"])
def test_direct_import_of_risky_module_is_an_error(module: str):
    """Risky imports must be hard errors decided by AST, not by crashing."""
    result = _result_for(f"import {module}\n" + _valid_strategy_code())
    assert not result.passed


def test_denylist_cannot_be_bypassed_by_attribute_aliasing():
    """eval/exec reached through an aliased module object must be caught."""
    code = "import os\n_system = os.system\n_system('id')\n" + _valid_strategy_code()
    result = _result_for(code)
    assert not result.passed


def test_string_and_dunder_tricks_do_not_restore_execution():
    """No path through __import__ or getattr should pass the gate."""
    payloads = [
        "x = getattr(__builtins__, 'eval')('1+1')",
        "imp = __builtins__['__import__']\nimp('os')",
        "__g = __builtins__.__dict__\n",
    ]
    for payload in payloads:
        result = _result_for(payload + "\n" + _valid_strategy_code())
        assert not result.passed, f"Validator accepted {payload!r}"


@pytest.mark.parametrize(
    "payload",
    [
        # Direct reflective-namespace lookup with a literal key.
        "vars()['eval']('1+1')",
        "globals()['__import__']('os')",
        "locals()['exec']('x=1')",
        # Computed key: the same bypass with an indirection the denylist
        # cannot name.
        "name = 'eval'\nvars()[name]('1+1')",
    ],
)
def test_reflective_namespace_lookups_are_rejected(payload: str):
    """vars()/globals()/locals()[...] resolve callables by computed name.

    A denylist of call names cannot enumerate what is reachable through the
    live namespace, so any subscript (literal or computed) on it is rejected
    as a policy, regardless of the payload.
    """
    result = _result_for(payload + "\n" + _valid_strategy_code())
    assert not result.passed, f"Validator accepted reflective lookup {payload!r}"
    assert any("reflective" in error for error in result.errors), result.errors


def test_harmless_subscripts_still_pass_after_reflective_policy():
    """The reflective-namespace rejection must not criminalize normal code."""
    code = "mapping = {'close': 1}\nvalue = mapping['close']\n" + _valid_strategy_code()
    result = _result_for(code)
    assert result.passed, result.errors


# ─── BacktestResultValidator: numeric hygiene ────────────────────────────────


def _backtest_payload(**stats):
    base = {
        "total_pnl": 100.0,
        "sharpe_ratio": 1.0,
        "cagr": 0.1,
        "max_drawdown_pct": 0.1,
        "win_rate": 0.5,
        "total_trades": 3,
    }
    base.update(stats)
    return {
        "stats": base,
        "trades": [
            {
                "entry_time": "2024-01-02T14:00:00+00:00",
                "exit_time": "2024-01-03T15:00:00+00:00",
            }
        ],
    }


def test_clean_backtest_passes():
    validation = BacktestResultValidator().validate(
        _backtest_payload(), BacktestExpectation(min_trades=1)
    )
    assert validation["passed"], validation["errors"]


def test_nan_stat_is_an_error():
    validation = BacktestResultValidator().validate(
        _backtest_payload(total_pnl=float("nan")), BacktestExpectation(min_trades=1)
    )
    assert not validation["passed"]


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", "inf"])
def test_string_nan_stats_cannot_bypass_validation(value: str):
    """String-typed NaN/Inf values previously skipped every numeric check.

    With a valid sharpe_ratio next to them they scored 1.0 / passed=True,
    because the stats loop only inspected float instances.
    """
    validation = BacktestResultValidator().validate(
        _backtest_payload(total_pnl=value, sharpe_ratio=1.0), BacktestExpectation(min_trades=1)
    )
    assert not validation["passed"], (
        f"String stat value {value!r} bypassed validation: {validation}"
    )


def test_missing_trades_list_fails():
    payload = _backtest_payload()
    payload.pop("trades")
    validation = BacktestResultValidator().validate(payload, BacktestExpectation(min_trades=1))
    assert not validation["passed"]


def test_high_sharpe_is_a_warning_not_an_error():
    """Sharpe > 10 stays a warning (documented decision, ox-alpha M12 note)."""
    validation = BacktestResultValidator().validate(
        _backtest_payload(sharpe_ratio=25.0), BacktestExpectation(min_trades=1)
    )
    assert validation["passed"]
    assert validation["warnings"], "Suspicious Sharpe must still be surfaced"

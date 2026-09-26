from __future__ import annotations

import ast
import re

import pandas as pd
import pytest

from runner.strategy_ir.codegen_vectorbt import generate_vectorbt_strategy_code
from runner.strategy_ir.models import (
    Condition,
    ConditionWhen,
    EngineHint,
    ExitSignal,
    ExitSignalKind,
    IndicatorRef,
    ParameterSpec,
    ParameterValueType,
    StrategyIR,
)


def _sample_ir() -> StrategyIR:
    return StrategyIR(
        name="Deterministic Demo",
        parameters=[
            ParameterSpec(name="fast", value_type=ParameterValueType.INT, default=10),
            ParameterSpec(name="slow", value_type=ParameterValueType.INT, default=30),
        ],
        indicators=[
            IndicatorRef(name="Fast EMA", params={"fast": 10}),
            IndicatorRef(name="Slow EMA", params={"slow": 30}),
        ],
        entry_conditions=[
            Condition(
                expression="Fast EMA crossover Slow EMA",
                when=ConditionWhen.ENTRY,
            )
        ],
        exit_signals=[
            ExitSignal(
                name="exit_1",
                condition="Fast EMA crossunder Slow EMA",
                kind=ExitSignalKind.CLOSE,
            )
        ],
        confidence=1.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {"type": ["momentum"]}},
    )


def test_generate_vectorbt_strategy_code_is_deterministic():
    strategy_ir = _sample_ir()

    code_a = generate_vectorbt_strategy_code(strategy_ir)
    code_b = generate_vectorbt_strategy_code(strategy_ir)

    assert code_a == code_b


def test_generate_vectorbt_strategy_code_emits_valid_contract():
    strategy_ir = _sample_ir()
    code = generate_vectorbt_strategy_code(strategy_ir)

    namespace: dict[str, object] = {}
    exec(code, namespace)

    assert "STRATEGY_META" in namespace
    assert namespace["STRATEGY_META"]["name"] == "Deterministic Demo"
    assert "generate_signals" in namespace

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC"),
            "close": [100 + idx * 0.2 for idx in range(60)],
        }
    )

    output = namespace["generate_signals"](df, {"fast": 5, "slow": 12})
    assert isinstance(output, tuple)
    # The platform contract is exactly 4 elements; a len-3 output is silently
    # repaired by both engine adapters, so it must never be generated.
    assert len(output) == 4

    entries, exits, indicators, drawings = output
    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert isinstance(indicators, dict)
    assert set(indicators) >= {"Fast EMA", "Slow EMA"}
    assert isinstance(drawings, list)


def test_generate_vectorbt_strategy_code_handles_ir_without_indicators():
    strategy_ir = StrategyIR(
        name="No Indicator Demo",
        parameters=[
            ParameterSpec(name="window", value_type=ParameterValueType.INT, default=15),
        ],
        indicators=[],
        entry_conditions=[],
        exit_signals=[],
        confidence=0.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {}},
    )

    code = generate_vectorbt_strategy_code(strategy_ir)
    namespace: dict[str, object] = {}
    exec(code, namespace)

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC"),
            "close": [100 + idx * 0.1 for idx in range(48)],
        }
    )

    output = namespace["generate_signals"](df, {"window": 10})
    entries, exits, indicators = output[:3]

    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert "baseline" in indicators
    assert len(pd.Series(indicators["baseline"])) == len(df)


def test_generate_vectorbt_strategy_code_sorts_meta_parameters_for_stable_output():
    strategy_ir = StrategyIR(
        name="Stable Meta Demo",
        parameters=[
            ParameterSpec(name="slow", value_type=ParameterValueType.INT, default=30),
            ParameterSpec(name="fast", value_type=ParameterValueType.INT, default=10),
        ],
        indicators=[],
        entry_conditions=[],
        exit_signals=[],
        confidence=0.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {}},
    )

    code = generate_vectorbt_strategy_code(strategy_ir)
    namespace: dict[str, object] = {}
    exec(code, namespace)

    meta = namespace["STRATEGY_META"]
    assert list(meta["parameters"].keys()) == ["fast", "slow"]


# ─── Injection / containment regression coverage (Cleanup 17-B) ───────────────


# Constructs that must never appear as executable code in generated output.
# Identifiers derived from adversarial IR names are inert slugs; the check
# therefore targets call syntax (parenthesized calls, dotted attribute
# access on dangerous modules, dunder access) rather than raw substrings.
_FORBIDDEN_CALL_PATTERNS = (
    "eval(",
    "exec(",
    "__import__(",
    "os.system",
    "os.popen",
    "subprocess.",
    "importlib.",
    "open(",
    "__globals__",
    "__builtins__",
    "__subclasses__",
    "getattr(",
)


def _injection_ir(name: str, indicator_label: str, expression: str, tag_value: str) -> StrategyIR:
    return StrategyIR(
        name=name,
        parameters=[ParameterSpec(name="fast", value_type=ParameterValueType.INT, default=10)],
        indicators=[IndicatorRef(name=indicator_label, params={"fast": 10})],
        entry_conditions=[Condition(expression=expression, when=ConditionWhen.ENTRY)],
        exit_signals=[],
        confidence=1.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {"type": [tag_value]}},
    )


@pytest.mark.parametrize(
    "payload",
    [
        ").__import__('os').system('id') #",
        "\" + __import__('os').getcwd() + \"",
        "__import__('subprocess').run(['ls'])",
    ],
)
def test_generated_code_contains_no_executable_injection_vector(payload: str):
    """Adversarial IR fields must end up as data, never as executable code."""
    strategy_ir = _injection_ir(
        name=f"Injected {payload}",
        indicator_label=f"EMA {payload}",
        expression=f"{payload} crossover Fast EMA",
        tag_value=payload,
    )

    code = generate_vectorbt_strategy_code(strategy_ir)

    assert _executable_code_is_clean(code), (
        f"Generated code contains forbidden executable syntax when IR carried "
        f"payload {payload!r}\n\n{code}"
    )

    # Every identifier derived from an adversarial IR name must be a plain
    # slug: letters/digits/underscores only, no quote or dunder boundary.
    for slug in _generated_identifiers(code):
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", slug), (
            f"Generated identifier {slug!r} is not a sanitized slug\n\n{code}"
        )
        assert "__" not in slug, (
            f"Generated identifier {slug!r} embeds a dunder sequence\n\n{code}"
        )


def _generated_identifiers(code: str) -> list[str]:
    """Identifiers the generator derived from IR-provided names."""
    return re.findall(r"\b(?:window_|indicator_)([A-Za-z0-9_]+)", code)


def _executable_code_is_clean(code: str) -> bool:
    """True when no executable node carries a forbidden construct.

    String literals (labels, metadata carried from the IR verbatim) are
    data, so checking AST nodes instead of raw text avoids false positives
    on adversarial payloads that only appear inside quotes.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    forbidden = tuple(pattern.lower() for pattern in _FORBIDDEN_CALL_PATTERNS)
    violations: list[str] = []

    # Only executable nodes matter; every quoted string in them is data and
    # is masked before the pattern check.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.Assign, ast.BinOp, ast.Subscript, ast.Attribute)):
            continue
        segment = ast.get_source_segment(code, node)
        if not segment:
            continue
        executable = _code_without_string_literals(segment)
        lowered = executable.lower()
        for pattern in forbidden:
            if pattern in lowered:
                violations.append(f"{type(node).__name__}: {pattern!r} in {executable!r}")

    return not violations


def _code_without_string_literals(segment: str) -> str:
    """Replace string-literal payloads with empty quotes of the same kind.

    Using the token stream (via ``ast.unparse`` of a stripped tree) keeps
    the quoting structure intact, so escaped quotes inside adversarial
    payloads cannot leave orphan quote characters behind that would make
    data look like executable syntax.
    """
    try:
        tree = ast.parse(segment)
    except SyntaxError:
        try:
            tree = ast.parse(segment, mode="eval")
        except SyntaxError:
            return segment

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            node.value = b""
    try:
        return ast.unparse(tree)
    except Exception:
        return segment


@pytest.mark.parametrize(
    "payload",
    [
        "__import__('os').system('id')",
        "'); import os; os.system('id'); '",
        "eval('1+1')",
    ],
)
def test_generated_module_survives_adversarial_names(payload: str):
    """The generated module must still exec and run on a plain frame."""
    strategy_ir = _injection_ir(
        name=f"Strategy {payload}",
        indicator_label=f"RSI {payload}",
        expression=f"RSI {payload} oversold",
        tag_value=payload,
    )

    namespace: dict[str, object] = {}
    exec(generate_vectorbt_strategy_code(strategy_ir), namespace)

    assert namespace["STRATEGY_META"]["name"] == f"Strategy {payload}"

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC"),
            "close": [100 + idx * 0.2 for idx in range(60)],
        }
    )
    output = namespace["generate_signals"](df, {"fast": 5})
    entries, exits, indicators = output[:3]
    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert isinstance(indicators, dict)


def test_generated_code_is_self_contained_no_risky_imports(tmp_path):
    """Containment: the generated module only needs pandas, nothing risky.

    Simulates an execution environment where os/subprocess/socket/importlib
    cannot be imported — generated code must not reach for them (ox-alpha
    F25/F26: helper namespace exec() is not contained).

    Inspected on the AST (not by substring) so every import form is covered,
    including ``from os import system``, which a plain ``"import os" not in
    code`` check would miss.
    """
    strategy_ir = _sample_ir()
    code = generate_vectorbt_strategy_code(strategy_ir)

    tree = ast.parse(code)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots <= {"__future__", "pandas"}, (
        f"Generated code imports non-pandas modules "
        f"{sorted(imported_roots - {'__future__', 'pandas'})}; only pandas "
        "(plus the inert `from __future__ import annotations`) may be imported\n\n"
        f"{code}"
    )


def test_generated_code_metadata_round_trips_quotes_and_backslashes():
    """Metadata with quotes/backslashes stays data (no string breakout)."""
    tricky_name = "Broken \" strategy 'with' \\ quotes"
    strategy_ir = _injection_ir(
        name=tricky_name,
        indicator_label="Fast EMA",
        expression="Fast EMA crossover Slow EMA",
        tag_value=tricky_name,
    )

    code = generate_vectorbt_strategy_code(strategy_ir)
    namespace: dict[str, object] = {}
    exec(code, namespace)

    assert namespace["STRATEGY_META"]["name"] == tricky_name


# ─── Condition-expression keyword contract (pre-mortem 17-B #1) ────────────────
#
# `_rule_from_text` classifies free-text IR expressions by SUBSTRING probing.
# That is a contract between the IR producer (builder/prompts) and this
# generator that no type enforces: if the producer's wording changes, the
# chain silently falls through to `default_rule` and the strategy trades a
# different rule without any error. These tests pin the accepted vocabulary
# so a wording change shows up as a test failure, not as drifted backtests.


@pytest.mark.parametrize(
    ("expression", "expected_fragment"),
    [
        # explicit compound keywords
        ("Fast EMA crossunder Slow EMA", "_cross_under("),
        ("Fast EMA crossover Slow EMA", "_cross_over("),
        # split keyword family
        ("Fast EMA cross below Slow EMA", "_cross_under("),
        ("Fast EMA cross above Slow EMA", "_cross_over("),
        # F4 (16-B): RSI-threshold keyword guessing is REMOVED — "overbought"/
        # "take profit"/"oversold"/"risk-off" prose no longer fabricates
        # (primary > 70) / (primary < 30) rules. Explicit expressions are the
        # contract; see tests/test_codegen_loud_failures.py for the fail-loud
        # pin of the removed keyword families.
    ],
)
def test_rule_keyword_families_are_pinned(expression: str, expected_fragment: str):
    strategy_ir = _injection_ir(
        name="Contract Demo",
        indicator_label="RSI Fast",
        expression=expression,
        tag_value="contract",
    )
    strategy_ir.exit_signals = []

    code = generate_vectorbt_strategy_code(strategy_ir)

    assert expected_fragment in code, (
        f"Expression {expression!r} no longer compiles to the pinned rule "
        f"(expected {expected_fragment!r}). If the IR wording changed on "
        "purpose, update _rule_from_text AND this contract test together."
    )


@pytest.mark.parametrize(
    ("expression", "expected_fragment"),
    [
        # plain comparison families (two indicators: primary vs secondary)
        ("Fast EMA greater than Slow EMA", "(indicator_fast_ema > indicator_slow_ema)"),
        ("Fast EMA less than Slow EMA", "(indicator_fast_ema < indicator_slow_ema)"),
        # threshold wording with a single indicator falls back to the
        # documented (primary > secondary) rule
        ("Fast EMA greater than Slow EMA", "(indicator_fast_ema > close.shift(1))"),
    ],
)
def test_comparison_families_are_pinned(expression: str, expected_fragment: str):
    strategy_ir = _injection_ir(
        name="Contract Demo",
        indicator_label="Fast EMA",
        expression=expression,
        tag_value="contract",
    )
    strategy_ir.exit_signals = []
    if expected_fragment.startswith("(indicator_fast_ema > indicator_slow_ema)") or (
        expected_fragment.startswith("(indicator_fast_ema < indicator_slow_ema)")
    ):
        strategy_ir.indicators.append(IndicatorRef(name="Slow EMA", params={"slow": 30}))

    code = generate_vectorbt_strategy_code(strategy_ir)

    assert expected_fragment in code, (
        f"Expression {expression!r} no longer compiles to the pinned rule "
        f"(expected {expected_fragment!r}). If the IR wording changed on "
        "purpose, update _rule_from_text AND this contract test together."
    )


def test_unrecognized_expression_fails_loudly():
    """F4 (16-B): unknown wording must fail loudly, never fabricate a rule."""
    strategy_ir = _injection_ir(
        name="Fallback Demo",
        indicator_label="Fast EMA",
        expression="the moon is in retrograde",
        tag_value="fallback",
    )
    strategy_ir.exit_signals = []

    with pytest.raises(ValueError, match="Cannot deterministically translate"):
        generate_vectorbt_strategy_code(strategy_ir)

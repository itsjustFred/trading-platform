"""Every strategy-IR text field must reject a blank value, in every model that has one.

`_validate_text` is implemented three times in `runner/strategy_ir/models.py` -
in `ExitSignal`, `StateTransition` and `RawExpression` - with the same rule
(strip, then reject empty) and three different error messages. Only `ExitSignal`'s
was tested.

Measured: removing the guard from `StateTransition` or from `RawExpression`
passes all 61 strategy-IR and codegen tests. So two of the three identical
validation rules had no coverage at all, and the shape is the one this branch
keeps finding - a rule implemented in more than one place, guarded in one of them.
The same pattern turned up twice already today: the optimizer embargo forwarded at
four call sites with one guard, and RamJet's BUY-only guard untested beside
Nitro's.

Why it matters here: these values are interpolated into generated strategy code.
A `RawExpression` whose expression is a single space, or a `StateTransition`
whose state name is blank, is not a cosmetic hole - it reaches the code generator
as an instruction with no content.

So the test discovers the validators rather than listing them, which is what makes
a fourth model covered for free.

**And then it turned out the discovery was itself name-keyed, which is the same
defect one level over.** `_validators()` asks for a method *named* `_validate_text`.
There are nine field validators wired across this module under three names -
`_validate_text` x3, `_validate_name` x4, `_validate_expression`, `_validate_states` -
so six of them were invisible to a test whose stated purpose is to discover them. Four
identical `_validate_name` copies and a `_validate_expression`, each with the same
strip-and-reject rule as the covered three, and none of them tested.

The fix below discovers from `__pydantic_decorators__.field_validators`, which is
pydantic's own record of what it actually wired. That is name-independent *and* it
describes the wiring rather than a list of intentions, so a validator added under any
name is covered without this file being edited.

Worth recording how the coverage was measured: reading the source said
`_validate_states` did not reject a blank, because its body does not read like the
others. Calling it says it does. Source inspection lost to execution, which is the
usual outcome and worth checking rather than assuming.
"""

from __future__ import annotations

import typing

import pytest

from runner.strategy_ir import models as ir_models


def _validators() -> list[tuple[str, type]]:
    found = []
    for name in dir(ir_models):
        candidate = getattr(ir_models, name)
        if isinstance(candidate, type) and hasattr(candidate, "_validate_text"):
            found.append((name, candidate))
    return sorted(found)


def test_the_module_still_has_the_validators_this_test_is_about():
    """Precondition. A rename would otherwise leave this file passing vacuously."""
    names = [name for name, _ in _validators()]
    assert len(names) >= 3, (
        f"expected at least three models carrying a text validator, found {names}. "
        "Either the models were consolidated - in which case this test should "
        "follow them - or one lost its validator, which is itself the bug."
    )


@pytest.mark.parametrize(
    "blank", ["", " ", "\t", "\n", "   \t\n "], ids=["empty", "space", "tab", "newline", "mixed"]
)
@pytest.mark.parametrize("model_name", [n for n, _ in _validators()])
def test_every_text_validator_rejects_a_blank_value(model_name, blank):
    _, model = next(c for c in _validators() if c[0] == model_name)
    validator = getattr(model, "_validate_text")

    with pytest.raises(ValueError):
        validator(blank)


@pytest.mark.parametrize("model_name", [n for n, _ in _validators()])
def test_every_text_validator_strips_its_input(model_name):
    """Stripping is the other half: a field of "  buy  " must not keep the padding.

    Without the strip, a whitespace-padded expression or state name reaches the
    generated code with the padding intact, and two spellings of the same intent
    become two different code paths.
    """
    _, model = next(c for c in _validators() if c[0] == model_name)
    validator = getattr(model, "_validate_text")

    assert validator("  entry  ") == "entry"


# ─── The whole wired family, discovered by pydantic rather than by name ──


def _wired_validators() -> list[tuple[str, str, object, object]]:
    """Every field validator pydantic actually wired, found without naming any of them.

    Returns ``(model_name, field_names, bound_validator, value_annotation)``.

    `__pydantic_decorators__.field_validators` is pydantic's own record of the
    validators it attached, so this is the wiring itself rather than a list of
    intentions, and it is completely independent of what the methods are called. The
    previous discovery in this file asked for a method named `_validate_text`, so the
    four `_validate_name` copies, `_validate_expression` and `_validate_states` were
    all outside it.

    The annotation is read from the *unbound* function: the bound classmethod hides the
    `value` parameter, and `from __future__ import annotations` means the source
    annotation is a string until `get_type_hints` resolves it.

    It is returned because the family is not uniform and pretending otherwise is how
    a test ends up asserting something false. `StateMachine._validate_states` validates
    a *collection* of state names; it still refuses a blank, but `"  entry  "` iterates
    it character by character and comes back a list, so the strip contract below applies
    to the eight `str`-typed validators and not to that one.
    """
    found: list[tuple[str, str, object, object]] = []
    for name in dir(ir_models):
        candidate = getattr(ir_models, name)
        if not isinstance(candidate, type):
            continue
        decorators = getattr(candidate, "__pydantic_decorators__", None)
        if decorators is None:
            continue
        for entry in (decorators.field_validators or {}).values():
            raw = candidate.__dict__.get(entry.func.__name__)
            function = getattr(raw, "__func__", None)
            annotation: object = object
            if function is not None:
                annotation = typing.get_type_hints(function).get("value", object)
            found.append((name, ",".join(entry.info.fields), entry.func, annotation))
    return sorted(found)

def test_pydantic_wires_more_validators_than_the_name_keyed_discovery_finds():
    """Precondition for the discovery below, and the reason it exists.

    A discovery that silently shrinks is the failure mode: the file would keep
    passing while covering less. This asserts the gap explicitly, so consolidating
    the validators has to be a deliberate change here rather than a silent loss of
    coverage.
    """
    wired = _wired_validators()
    wired_models = {name for name, _, _, _ in wired}
    by_name_models = {name for name, _ in _validators()}

    assert len(wired) >= 9, (
        f"expected at least nine wired field validators, found {len(wired)}: "
        f"{[(n, f) for n, f, _, _ in wired]}"
    )
    assert len(by_name_models) == 3, (
        f"expected the three _validate_text models to still be there, found {by_name_models}"
    )
    uncovered = sorted(wired_models - by_name_models)
    assert len(uncovered) >= 6, (
        "the pydantic-wired set is no longer larger than the name-keyed one, so the "
        "gap this file describes has closed - which is a good outcome, but it has to "
        "be a deliberate one and this test should be rewritten to say so. Currently "
        f"uncovered: {uncovered}"
    )


@pytest.mark.parametrize(
    "blank", ["", " ", "\t", "\n", "   \t\n "], ids=["empty", "space", "tab", "newline", "mixed"]
)
def test_every_wired_field_validator_rejects_a_blank_value(blank):
    """The rule, for all nine wired validators rather than the three named ones."""
    for model_name, fields, validator, _annotation in _wired_validators():
        with pytest.raises(ValueError):
            validator(blank)


def test_every_wired_field_validator_strips_its_input():
    """The strip half, for the eight `str`-typed validators.

    Excludes the one collection-typed validator rather than asserting a contract it
    cannot satisfy - see `_wired_validators`. Its count is asserted so the exclusion
    stays visible instead of quietly becoming three.
    """
    string_typed = [row for row in _wired_validators() if row[3] is str]
    assert len(string_typed) == 8, (
        f"expected eight str-typed field validators, found {len(string_typed)}: "
        f"{[(n, f, a) for n, f, _, a in _wired_validators()]}"
    )
    for model_name, fields, validator, _annotation in string_typed:
        assert validator("  entry  ") == "entry", f"{model_name}.{fields} kept the padding"


def test_every_blank_rejection_names_the_field_it_refused():
    """An error that does not name the field is an error nobody can act on.

    Measured while writing this: the messages differ per model - "Parameter name",
    "Indicator name", "State transition" - and each identifies its own model. A
    validator that raised a bare "must not be empty" would be indistinguishable from
    seven others in a stack trace pointing at generated code.
    """
    for model_name, fields, validator, _annotation in _wired_validators():
        try:
            validator("")
        except ValueError as exc:
            message = str(exc)
            assert message.strip(), (
                f"{model_name}.{fields} raised a ValueError with no message"
            )
            assert len(message) > len("must not be empty"), (
                f"{model_name}.{fields} raised a message that identifies nothing: {message!r}"
            )
        else:
            raise AssertionError(f"{model_name}.{fields} accepted a blank value")

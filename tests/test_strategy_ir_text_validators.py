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
"""

from __future__ import annotations

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

"""Guards against assertions that can never fail.

An `assert` whose condition is a tuple is always true, because a non-empty tuple is
truthy. The usual way to write one by accident is to wrap the failure message in the
same parentheses as the comparison:

    assert (
        some_call(...) == "",
        "why this matters",
    )

which parses as `assert (False, "why this matters")` and **passes unconditionally**.
Python does emit `SyntaxWarning: assertion is always true, perhaps remove
parentheses?`, but the warning is easy to miss and the test looks correct on the page.

This is not hypothetical: one such assertion shipped in this sweep. It made a mutation
survive a test that appeared to cover the mutation exactly, and the only reason it was
found is that the harness kept disagreeing with a hand-applied mutation of the same code.
The mutation verdict was the thing that noticed, not the test.

The fix for the assertion itself is to keep the message as a plain second argument:

    resolved = some_call(...)
    assert resolved == "", f"message about {resolved!r}"

Both forms are allowed below; what is banned is a tuple as the asserted expression.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _test_files() -> list[Path]:
    return sorted(TESTS_DIR.rglob("test_*.py"))


def test_the_test_directory_is_not_empty() -> None:
    """A guard that silently passes because it found nothing is worse than no guard."""
    files = _test_files()
    assert len(files) > 20, (
        f"only {len(files)} test files found under {TESTS_DIR}; this guard would be "
        "vacuous if the path were wrong"
    )


def test_no_assertion_is_a_tuple() -> None:
    """The guard itself. Reports every offender with its file and line."""
    offenders: list[str] = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple):
                elts = ", ".join(ast.unparse(e)[:70] for e in node.test.elts)
                offenders.append(f"{path.name}:{node.lineno}  assert ({elts}...)")

    assert not offenders, (
        "these assertions can never fail, because a non-empty tuple is truthy:\n  "
        + "\n  ".join(offenders)
        + "\n\nKeep the failure message as a plain second argument instead:\n"
        "    value = call(...)\n"
        '    assert value == "", f"reason: {value!r}"'
    )


def test_the_banned_form_is_actually_detected() -> None:
    """The guard has to be able to fail, so prove it catches the shape it bans.

    Parsed here rather than executed: running a deliberately-failing assert in this suite
    would make the run itself fail, which is the opposite of useful.
    """
    banned = ast.parse("assert (value == '', 'a message')").body[0]
    assert isinstance(banned, ast.Assert)
    assert isinstance(banned.test, ast.Tuple), "the banned shape was not recognised"

    allowed = ast.parse("assert value == '', 'a message'").body[0]
    assert isinstance(allowed, ast.Assert)
    assert not isinstance(allowed.test, ast.Tuple), "the allowed shape was misread"

    # And the truth value that makes the bug dangerous, spelled out.
    assert bool((False, "a message")) is True, (
        "a one-element-ish tuple is truthy even when its comparison failed; if this ever "
        "changes the premise of the guard is wrong"
    )

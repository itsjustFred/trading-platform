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


# ---------------------------------------------------------------------------
# Second class: a test with no way to fail at all.
#
# The tuple guard above catches an assertion that is always true. This catches a
# *function* that can never fail, which is a stronger version of the same disease: six
# `test_*` functions in this suite had a docstring and nothing else, so they passed
# unconditionally and appeared in the test count as coverage. Each was a finding written
# down rather than verified - a careful argument in a docstring looks exactly like a pin
# until you ask what would fail.
#
# An empty test is worse than an admitted gap. A gap is visible; an empty test is
# invisible, because it shows up in the count, passes in the report, and reads as coverage
# in review.
#
# What counts as "can fail", and why the list is this shape:
#   * `ast.Assert`                      - the ordinary case.
#   * `pytest.raises` / `warns` / `fail` - a test whose subject is the raise.
#   * `assert_called_once` and friends  - mock-based assertion helpers.
#   * a call to a name starting `assert`/`check_`/`expect_` - the pandas testing helpers
#     (`assert_series_equal`, `assert_frame_equal`) and this suite's own conventions.
#   * an explicit `raise AssertionError` (or any `*Error` raise) in the test, or in a
#     same-module helper it calls.
#
# The first version of this audit recognised only the first two and reported 17 remaining
# offenders - which I then described as "no-raise style or cross-module fixture" without
# checking. Both halves were wrong: most of the 17 assert through pandas or through an
# explicit `raise AssertionError` in a helper, neither of which the first version saw. The
# real number was 7. A guard built on the weaker classifier would have enshrined the wrong
# number, so the classifier is spelled out here instead of being implicit.
#
# What is deliberately NOT banned: a no-raise test whose refusal is pinned by a sibling in
# the same file. `test_a_lowercase_signal_symbol_is_accepted` is the pass half of a
# pass/fail pair, and mutating the gate it exercises is killed by the fail half next to it
# (verified with the harness, not assumed). Banning no-raise tests outright would push
# authors to assert on a boolean twin that nothing else uses, which is a worse trade than
# the pattern it replaces.
# ---------------------------------------------------------------------------

#: Names that make a call an assertion regardless of where they are defined.
FAILING_CALL_NAMES = frozenset({
    "raises", "warns", "fail", "xfail", "skip",
    "assert_called_once", "assert_called_with", "assert_called", "assert_not_called",
    "assert_any_call", "assert_has_calls", "assert_awaited", "assert_awaited_once",
})

#: Prefixes that mark a call as an assertion by convention.
ASSERT_NAME_PREFIXES = ("assert", "check_", "expect_", "verify_")


def _looks_like_assertion(name: str) -> bool:
    # Leading underscores are stripped so a private helper - `_check_thing`,
    # `_assert_shape` - is recognised the same as its public spelling. Without this the
    # guard missed the module's own convention, which is the case most likely to appear.
    bare = name.lstrip("_")
    return bare in FAILING_CALL_NAMES or bare.startswith(ASSERT_NAME_PREFIXES)


def _can_fail(node: ast.AST, helpers: dict[str, ast.AST], seen: set[str] | None = None) -> bool:
    """Whether `node` contains anything that can fail the test.

    Follows same-module helpers, because a test that delegates its whole body to a local
    `def _check_...` is asserting as much as one with a bare `assert`. It deliberately does
    NOT follow helpers in other modules: a cross-module call may or may not assert, and
    guessing is how the first version of this audit got the number wrong. Those cases are
    left to the mutation harness, which is the authority anyway.
    """
    seen = set() if seen is None else seen
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Raise):
            exc = child.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name and ("assert" in name.lower() or name.lower().endswith("error")):
                return True
        if isinstance(child, ast.Call):
            func = child.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if _looks_like_assertion(name):
                return True
            if name in helpers and name not in seen:
                seen.add(name)
                if _can_fail(helpers[name], helpers, seen):
                    return True
    return False


#: A no-raise test may carry `# no-raise: <sibling_name>` in its body or docstring to
#: declare that the refusal is asserted by that sibling. The name is resolved against the
#: same file's functions and must exist, so the exemption cannot rot into a free pass.
NO_RAISE_MARKER = "no-raise:"


def _called_names(node: ast.AST, helpers: dict[str, ast.AST], seen: set[str] | None = None) -> set[str]:
    """Every name called from `node`, following same-module helpers.

    Used to check that a `no-raise:` marker names a test of the *same* code rather than any
    test that happens to exist. Existence alone was the first version's condition and it was
    too weak: it accepted a marker pointing at a test of a different function, so a
    completely unjustified exemption passed. One of the seven markers added under the
    existence-only rule named a config-file test that shares no call with the test claiming
    it - caught by this check, not by reading.
    """
    seen = set() if seen is None else seen
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name:
                out.add(name)
            if name in helpers and name not in seen:
                seen.add(name)
                out |= _called_names(helpers[name], helpers, seen)
    return out


def _no_raise_marker(path: Path, node: ast.AST) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    # Start AFTER the def line. The first version started on it, and the `def` matched the
    # loop's own stop condition immediately - so the scan returned before reading the
    # docstring, and every marker was reported as absent. The guard was not rejecting the
    # exemptions, it was never seeing them.
    for line in lines[node.lineno:]:
        stripped = line.strip()
        if stripped.startswith(("def ", "@", "class ")):
            break
        if NO_RAISE_MARKER in stripped:
            return stripped.split(NO_RAISE_MARKER, 1)[1].strip().split()[0].strip(",;()")
    return None


def _module_helpers(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_no_test_function_is_empty() -> None:
    """A `test_*` with no body but a docstring can never fail, whatever the docstring says.

    This is the class the tuple guard cannot see, and it is the one that actually cost
    coverage: six of these existed, all of them findings recorded in prose. Reported with
    file and line so the offender is findable.
    """
    offenders: list[str] = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            body = [
                stmt for stmt in node.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            ]
            if not body:
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{node.lineno} {node.name}")
    assert not offenders, (
        "these test functions have no body, so they cannot fail and only inflate the count:\n"
        + "\n".join(f"  {line}" for line in offenders)
    )


def test_every_test_function_has_a_way_to_fail() -> None:
    """Every `test_*` must contain something that can fail it.

    Weaker than the tuple guard in one direction and stronger in another: it cannot tell a
    *wrong* assertion from a right one, but it does catch an absent one. Paired no-raise
    tests are fine as long as a sibling pins the refusal - see the module docstring.
    """
    offenders: list[str] = []
    checked = 0
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        helpers = _module_helpers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            checked += 1
            if _can_fail(node, helpers):
                continue
            # A paired no-raise test may claim its refusal is pinned by a sibling, and the
            # claim is CHECKED rather than trusted: the named symbol must exist in the same
            # file. An unresolvable marker is still an offender, so this cannot be used as
            # a blanket exemption - it has to name something real, and something that
            # disappears when the sibling is renamed fails the next run.
            marker = _no_raise_marker(path, node)
            if marker and marker in helpers:
                sibling = helpers[marker]
                shared = _called_names(node, helpers) & _called_names(sibling, helpers)
                if not shared:
                    offenders.append(
                        f"{path.relative_to(TESTS_DIR)}:{node.lineno} {node.name}\n"
                        f"      claims sibling {marker!r}, which exists but shares no call "
                        f"with it - so it is not a test of the same code"
                    )
                    continue
                if not _can_fail(sibling, helpers):
                    offenders.append(
                        f"{path.relative_to(TESTS_DIR)}:{node.lineno} {node.name}\n"
                        f"      claims sibling {marker!r}, which cannot fail either - so "
                        f"nothing pins the refusal"
                    )
                continue
            suffix = f"  [claims sibling {marker!r}, which is not in this file]" if marker else ""
            offenders.append(
                f"{path.relative_to(TESTS_DIR)}:{node.lineno} {node.name}{suffix}"
            )
    assert checked > 500, (
        f"only {checked} test functions scanned; the path or the walker is wrong and this "
        "guard would be vacuous"
    )
    assert not offenders, (
        "these test functions contain nothing that can fail them. If the refusal is pinned "
        "by a sibling test in the same file, say so in a comment - otherwise add the "
        "assertion:\n" + "\n".join(f"  {line}" for line in offenders)
    )


def test_the_new_guards_can_actually_detect_what_they_ban() -> None:
    """A guard that cannot fail is the disease it is meant to cure.

    Proved on source strings rather than by running deliberately-failing tests, which
    would make this suite's own run fail.
    """
    empty = ast.parse('def test_x():\n    """doc"""\n').body[0]
    assert not [
        stmt for stmt in empty.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ], "the empty-body detection is wrong"

    for source, why in (
        ("def test_x():\n    assert f() == 1\n", "plain assert"),
        ("def test_x():\n    import pytest\n    with pytest.raises(ValueError):\n        f()\n", "raises"),
        ("def test_x():\n    import pandas.testing as pt\n    pt.assert_frame_equal(a, b)\n", "pandas"),
        ("def test_x():\n    _check_thing()\n", "same-module helper by name"),
        ("def test_x():\n    raise AssertionError('no')\n", "explicit raise"),
    ):
        node = ast.parse(source).body[0]
        assert _can_fail(node, _module_helpers(ast.parse(source))), f"missed: {why}"

    bare = ast.parse("def test_x():\n    do_something()\n").body[0]
    assert not _can_fail(bare, {}), "a call that cannot fail was treated as an assertion"

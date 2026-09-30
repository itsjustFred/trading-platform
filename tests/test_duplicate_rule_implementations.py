"""Rules that must be written exactly once, checked across every copy.

A static pin that reads the source and asserts an identifier is *mentioned* fails
one way: it passes while the behaviour goes wrong. This one fails the other way,
which is the one that matters for duplicated code. It asks whether the copies of a
rule still agree with each other, so editing one of three is caught even though
every copy is individually well-formed and the names never moved.

The failure this guards is the recurring one across this branch: a safety or
data-correctness rule implemented in two places. Six instances have been live
bugs. The rule itself was never wrong in any of them - the copies were.

Why identical bodies and not "the module exports it": the defect *is* the
duplication, so the only way to see it coming is to look for it. And the copies
are discovered by scanning production code for the name, not by listing the files
today - otherwise a fourth copy in a new downloader would simply be outside the
comparison and would pass, which is the whole failure mode.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

# Same derivation the rest of the suite uses (`parents[1]`, 19 other sites).
REPO_ROOT = Path(__file__).resolve().parents[1]

# Production roots. `tests/` is excluded on purpose: these are rules about the
# shipped code, and a test helper named the same thing is not a second copy.
PRODUCTION_ROOTS = (
    "data_tools",
    "frontend",
    "live_runner",
    "risk",
    "runner",
    "scripts",
    "shared",
    "signal_bots",
    "validation",
    "viewer",
)

# Each entry is one logical rule. `copies` is the number of implementations that
# must agree today; raising it without keeping them identical fails, which is the
# point - adding a copy has to come with an argument that it is the same rule.
DUPLICATED_RULES: tuple[tuple[str, int], ...] = (
    # Timestamp normalisation for a downloaded row. Three downloaders each need
    # it, and `errors='raise'` is the load-bearing part: coercing would turn an
    # unreadable timestamp into NaT and silently drop the row, which is how a
    # dataset becomes quietly empty.
    ("_to_utc_timestamp", 3),
    # Trading-session and symbol classification, used to pick a download source.
    # A drift means two callers disagree about whether a symbol is intraday US,
    # so one fetches a session and the other fetches the calendar.
    ("_is_us_intraday_stock", 2),
    ("_auto_download_source", 2),
    # Catalog date identity: decides which catalog entries exist.
    ("_catalog_timestamp_to_epoch", 2),
    # MT5 import: exit reason feeds trade statistics and per-trade reporting.
    # Two readers, one answer.
    ("_infer_exit_reason", 2),
)

_SKIP_DIR_PARTS = {"__pycache__", ".git", "node_modules", "dist", "build"}

# One duplicated pair this test structurally cannot see, recorded rather than
# quietly dropped: `viewer/main.py::_merge_timeframe_entry_dates` and
# `data_tools/catalog_builder.py::_merge_catalog_dates` have byte-identical
# bodies under *different* names, so grouping by name puts them in different
# buckets and grouping by body never reaches a test. Finding them needed the
# opposite scan - hashing every function body and looking for collisions - which
# is a different tool with different blind spots. A name-keyed test and a
# body-keyed test miss opposite things; neither is the whole job.


def _production_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for root in PRODUCTION_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _SKIP_DIR_PARTS.intersection(path.parts):
                continue
            out.append(path)
    return out


def _functions_named(path: Path, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - would fail the suite elsewhere
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _normalised_body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Structure and names, with the docstring dropped.

    Comments and docstrings are excluded because they are the part that stays
    identical when two copies have already diverged in behaviour - which is
    exactly what must not decide the verdict here.
    """
    body = [
        stmt
        for stmt in fn.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    return ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)


def _copies(repo_root: Path, name: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in _production_files(repo_root):
        for fn in _functions_named(path, name):
            rel = path.relative_to(repo_root).as_posix()
            found.append((rel, _normalised_body(fn)))
    return found


@pytest.mark.parametrize(
    ("name", "expected_copies"), DUPLICATED_RULES, ids=[n for n, _ in DUPLICATED_RULES]
)
def test_every_copy_of_a_duplicated_rule_agrees(
    name: str, expected_copies: int
) -> None:
    """All implementations of one rule must have identical bodies.

    This is a negative pin, in the sense that it fails when something is
    *removed* as well as added: dropping a copy changes the count, and changing
    one copy changes its body.
    """
    copies = _copies(REPO_ROOT, name)
    locations = [rel for rel, _ in copies]

    assert len(copies) == expected_copies, (
        f"{name}: expected {expected_copies} identical implementation(s), found "
        f"{len(copies)} at {locations}. If a copy was removed, the rule changed; "
        f"if one was added, the new copy has to be justified as the same rule and "
        f"this count raised deliberately."
    )

    first_rel, first_body = copies[0]
    divergent = [rel for rel, body in copies[1:] if body != first_body]
    assert not divergent, (
        f"{name}: implementations have diverged. {first_rel} and {divergent} no "
        f"long longer agree. Two copies of one rule is the defect this whole branch "
        f"exists to find - one of them is now wrong, or both are. Names being "
        f"present is not the check; agreement is."
    )


# --------------------------------------------------------------------------
# Independently-written copies: pinned by behaviour, not by text
# --------------------------------------------------------------------------
#
# The table above only catches a rule written *verbatim* twice. Two copies of one
# rule that were written independently - different variable names, different
# annotations, same intent - never share a body and so never collide, even though
# they are exactly as duplicated. For those the instrument has to be behaviour:
# call every copy with the same inputs and require the same verdict.
#
# `risk/` has three finite-coercion helpers. Two refused a bool because
# `float(True)` is `1.0`; `risk/models.py` did not, so `TradeSignal(
# entry_price=True)` became an entry at 1.0 - on the live signal object, which is
# the copy where the difference could reach a trade. Grouping by body had missed
# this entirely: it reported the two *refusing* copies as a clean duplicate pair
# and never compared the odd one out, because the odd one out does not match
# anything.


def _finite_coercers() -> list[tuple[str, Callable[[object, str], float]]]:
    from risk.config import RiskConfig
    from risk.engine import RiskEngine
    from risk.models import _finite_float as models_finite_float

    return [
        ("risk/models.py::_finite_float", models_finite_float),
        ("risk/config.py::_finite_float", RiskConfig._finite_float),
        ("risk/engine.py::_finite_number", RiskEngine._finite_number),
    ]


@pytest.mark.parametrize(
    ("value", "should_raise"),
    [
        # A bool is not a number, whatever `float()` says about it.
        (True, True),
        (False, True),
        # Genuinely non-numeric.
        ("not-a-number", True),
        (None, True),
        (object(), True),
        # Numeric but not finite: the guard this helper exists for.
        (float("nan"), True),
        (float("inf"), True),
        (float("-inf"), True),
        # Real numbers must still be accepted, or the fix would be a new outage.
        (1.5, False),
        (0, False),
        ("2.5", False),
    ],
)
def test_every_risk_finite_coercer_agrees_on_bool_and_finiteness(
    value: object, should_raise: bool
) -> None:
    """All three finite-coercion helpers must reach the same verdict.

    Asserted per copy rather than as a comparison, so a helper that accepts a
    bool fails on its own account instead of only when another one disagrees.
    """
    for label, coercer in _finite_coercers():
        if should_raise:
            with pytest.raises(ValueError):
                coercer(value, "field")
        else:
            assert isinstance(coercer(value, "field"), float)

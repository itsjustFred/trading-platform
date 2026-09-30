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
    # Message-spelling timeframe normalisation, in the two processors that each
    # carry their own copy. Nitro is not listed because it subclasses RamJet and
    # therefore has no copy of its own - which is asserted separately below,
    # precisely because "no copy" is the thing that could change silently.
    ("_resolve_timeframe", 2),
    # OHLCV cleaning on the live signal path. Coerce-then-dropna looks like the
    # opposite policy to the downloaders' `errors="raise"`, but it fails closed:
    # an all-unreadable frame raises rather than silently sizing a position.
    ("_prepare_market_data", 2),
    # Provider timeframe normalisation. These three differ by *design* in one
    # respect - yfinance serves `1w`/`4h`, alpaca and huggingface do not - so the
    # per-provider map is data, not duplication, and is deliberately not compared.
    # The rule around it is one rule, and two of the three copies carried a second
    # lookup the third did not. See the alias-coverage pin at the end of this file
    # for why removing it is safe.
    ("_normalize_supported_timeframe", 3),
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


# --------------------------------------------------------------------------
# The coincidence that made the divergence above harmless
# --------------------------------------------------------------------------
#
# `_normalize_supported_timeframe` in alpaca and huggingface did a second lookup,
# `TIMEFRAME_MAP.get(normalize_timeframe(x))`, that yfinance did not. It is
# unreachable today, and for a reason that is worth stating precisely because
# nothing about it looks like a guarantee:
#
# `normalize_timeframe` translates only `TIMEFRAME_ALIASES`, which is
# `{d1: 1d, h1: 1h, m1: 1m}`. Those three are *also* direct keys of every
# downloader's `TIMEFRAME_MAP`, so the first lookup always wins and the second
# can never fire. Verified two ways: 245,410 fuzzed inputs rescued by the second
# lookup (0), and the set-inclusion assertion below.
#
# So the two copies disagreed on a line whose harmlessness depended on one table
# agreeing with another for no stated reason. Removing the fallback is safe
# **only** as long as that inclusion holds, and the inclusion is the thing that
# can change - a fourth alias in `shared/constants.py` would make alpaca and
# huggingface silently accept it while yfinance rejected it, which is the
# same drift shape as the timeframe-set bug this branch already closed once.
#
# Hence the pin. If someone adds an alias, this fails and the three copies are
# reconsidered together, rather than one of them quietly changing behaviour.

_ALIAS_BEARING_DOWNLOADERS = (
    "data_tools.downloaders.alpaca_dl",
    "data_tools.downloaders.huggingface_dl",
    "data_tools.downloaders.yfinance_dl",
)


@pytest.mark.parametrize("module_name", _ALIAS_BEARING_DOWNLOADERS)
def test_every_timeframe_alias_is_a_direct_key_in_each_downloader_map(module_name: str) -> None:
    """Every alias must be directly accepted, with no help from a second lookup.

    `m1` resolves to `1m` because `m1` is a key in the map, not because a
    fallback normalised it. If that stops being true, an accepted spelling starts
    depending on which downloader handles it.
    """
    import importlib

    from shared.constants import TIMEFRAME_ALIASES

    module = importlib.import_module(module_name)
    keys = set(module.TIMEFRAME_MAP)
    missing = sorted(alias for alias in TIMEFRAME_ALIASES if alias not in keys)
    assert not missing, (
        f"{module_name}: aliases {missing} are not direct keys of its TIMEFRAME_MAP. "
        f"They currently resolve only via normalize_timeframe, and no downloader "
        f"has that second lookup any more - so these spellings now raise where "
        f"they used to work. Either add them as keys, or drop them from "
        f"TIMEFRAME_ALIASES deliberately."
    )
    # And the behaviour that is easy to break by accident: upper case and padding.
    for alias in TIMEFRAME_ALIASES:
        for variant in (alias.upper(), f"  {alias}  "):
            assert module._normalize_supported_timeframe(variant) == TIMEFRAME_ALIASES[alias]


# --------------------------------------------------------------------------
# The three bots share a parser, but they do not share the table the same way
# --------------------------------------------------------------------------
#
# Nitro subclasses RamJet, so it *inherits* `_resolve_timeframe` and RamJet's
# `_TIMEFRAME_ALIASES`. RocketFuel carries its own copy of both. The copies are
# equal today.
#
# The asymmetry is the hazard, not the duplication. A fix to RamJet's table
# silently changes Nitro - which nobody would read as a Nitro change - while
# RocketFuel keeps the old values. On the path where this repo has already had a
# parser bug open a short position on a message that said buy, "which bots are
# affected by this edit" should not be answerable by accident.
#
# Deriving all three from one owner is the better fix and `GRID_TIMEFRAME_ALIASES`
# in `shared/constants.py` is the precedent for it. Not done here: the tables
# agree, so this is a hazard rather than a bug, and the change would edit the live
# signal path of all three bots on the strength of nothing observable. Pinned
# instead, so the drift is caught rather than prevented, and the decision stays
# with the human.


def _signal_timeframe_aliases() -> list[tuple[str, dict[str, str]]]:
    from signal_bots.ramjet.processor import _TIMEFRAME_ALIASES as ramjet
    from signal_bots.rocketfuel.processor import _TIMEFRAME_ALIASES as rocketfuel

    return [("signal_bots/ramjet/processor.py", ramjet),
            ("signal_bots/rocketfuel/processor.py", rocketfuel)]


def test_every_signal_bot_resolves_timeframe_spellings_identically() -> None:
    """All three bots must map a message's timeframe spelling to the same thing."""
    from signal_bots.nitro.processor import NitroSignalProcessor
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    tables = _signal_timeframe_aliases()
    ramjet_table = dict(tables[0][1])
    for label, table in tables[1:]:
        assert table == ramjet_table, (
            f"{label} timeframe spellings differ from {tables[0][0]}: "
            f"{ {k: v for k, v in table.items() if ramjet_table.get(k) != v} }"
        )

    # Nitro has no copy of the table because it subclasses RamJet and resolves a
    # timeframe by delegating to RamJet's `process_signal` via `super()`. Asserted
    # structurally, because "Nitro reuses RamJet's" is exactly the property that
    # silently stops being true if someone rewrites that override.
    assert NitroSignalProcessor.__mro__[1] is RamJetSignalProcessor, (
        "NitroSignalProcessor no longer subclasses RamJetSignalProcessor."
    )
    import pathlib as _pathlib

    module_tree = ast.parse(
        (_pathlib.Path(NitroSignalProcessor.__module__.replace(".", "/") + ".py"))
        .read_text(encoding="utf-8")
    )
    nitro_process = next(
        fn
        for cls in module_tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "NitroSignalProcessor"
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "process_signal"
    )
    delegates = [
        node
        for node in ast.walk(nitro_process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "process_signal"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    ]
    assert delegates, (
        "NitroSignalProcessor.process_signal no longer calls "
        "super().process_signal(...). It overrides RamJet's entry point, so it now "
        "decides its own timeframe handling - and this test must compare three bots."
    )

    # Behaviour, not just data: the same spellings through both resolvers.
    from signal_bots.ramjet import processor as ramjet_module
    from signal_bots.rocketfuel import processor as rocketfuel_module

    resolvers = {
        "ramjet (and nitro, via super)": ramjet_module._resolve_timeframe,
        "rocketfuel": rocketfuel_module._resolve_timeframe,
    }
    for spelling in ["", "5min", "5MIN", " 5min ", "1min", "60min", "1h", "5m", "nonsense"]:
        resolved = {label: fn(spelling) for label, fn in resolvers.items()}
        assert len(set(resolved.values())) == 1, (
            f"timeframe spelling {spelling!r} resolves differently per bot: {resolved}"
        )

    # And the OHLCV cleaning both bots share is the same object, not two copies.
    assert NitroSignalProcessor._prepare_market_data is RamJetSignalProcessor._prepare_market_data, (
        "NitroSignalProcessor._prepare_market_data is no longer the inherited RamJet one."
    )

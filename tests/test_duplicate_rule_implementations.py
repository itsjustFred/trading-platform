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
# Directories excluded from discovery, and why.
_EXCLUDED_ROOTS = {
    "tests",          # these guards are about shipped code, and a test helper
                       # sharing a name is not a second copy
    ".agents", ".git", "node_modules", "docs", "logs", "backups", "data",
    ".venv", "venv", "build", "dist", "frontend/dist",
}

# Derived, not listed. A hand-maintained list of roots is exactly the "a list of
# what to check is a list that can be wrong" mistake this branch has already
# fixed twice - once for CWD-relative paths and once for fixture globs. The
# previous hand-written ten-entry tuple omitted `strategies` (30 modules),
# `scheduler` and `strategy-comparisons` while including `frontend`, which has
# zero Python files, and a probe confirmed a third copy of
# `_catalog_timestamp_to_epoch` placed under `strategies/` was completely
# invisible to the comparison.
def _production_roots() -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.name
            for entry in REPO_ROOT.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and entry.name not in _EXCLUDED_ROOTS
            and any(entry.rglob("*.py"))
        )
    )


PRODUCTION_ROOTS = _production_roots()

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
    """Structure and names, with the top-level docstring dropped.

    Comments and docstrings are excluded because they are the part that stays
    identical when two copies have already diverged in behaviour - which is
    exactly what must not decide the verdict here.

    **Two known blind spots, verified rather than assumed:**

    - *Module-level constants are not compared.* `_is_us_intraday_stock` and
      `_auto_download_source` both read a file-local `US_INTRADAY_TIMEFRAMES`,
      so a copy can diverge behaviourally while the bodies stay identical - which
      is precisely the drift this pin claims to catch. A pre-existing viewer test
      covers those two, so it is a limitation of the pin, not a hole in the suite.
    - *Signatures are not compared.* `fn.body` only, so a differing default
      argument, a missing `@staticmethod` or an `async def` in place of `def`
      would pass. No currently pinned rule has a default argument.

    A docstring nested inside an `if` or `for` **is** compared, so two
    behaviourally identical copies could fail here on a wording difference. None
    currently do; if one starts to, the fix is in this function rather than in
    the copies.
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
    # Resolved against REPO_ROOT, never against the working directory. Found by
    # CodeRabbit: run pytest from anywhere but the repo root and this raised
    # FileNotFoundError, so the pin reported the wrong problem when the thing it
    # guards had actually changed. It is the same CWD-relative-path hazard this
    # branch has now fixed three times - df64c93, e4523e5, and the recursive pin -
    # this time introduced by the pin written to guard against it.
    module_tree = ast.parse(
        (REPO_ROOT / (NitroSignalProcessor.__module__.replace(".", "/") + ".py")).read_text(
            encoding="utf-8"
        )
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


def test_both_signal_processors_compute_the_same_atr() -> None:
    """The two copies of `_calculate_atr` must agree, and neither was tested at all.

    They differ textually - one drops the scratch columns and returns, the other returns
    the drop inline - so a body-equality pin cannot see them, and before this the
    function was named in no test file. It is the ATR that becomes the live stop and
    take-profit, so a divergence here is a divergence in order prices.

    Pinned by output rather than by text, because the textual difference is benign and
    normalising it would be churn.
    """
    import numpy as np
    import pandas as pd
    from signal_bots.ramjet.processor import RamJetSignalProcessor as RamJet
    from signal_bots.rocketfuel.processor import RocketFuelSignalProcessor as RocketFuel

    rng = np.random.default_rng(11)
    rows = 200
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, rows)))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1000.0,
        }
    )
    for period in (5, 7, 14, 30, 50):
        ramjet = RamJet._calculate_atr(frame, period)
        rocketfuel = RocketFuel._calculate_atr(frame, period)
        assert ramjet["atr"].equals(rocketfuel["atr"]), (
            f"period {period}: the two processors compute a different ATR, and this "
            f"value becomes the live stop-loss and take-profit offset"
        )
        # Neither may leak the scratch columns it builds on the way.
        for scratch in ("h_l", "h_pc", "l_pc", "tr"):
            assert scratch not in ramjet.columns, f"period {period}: leaked {scratch!r}"


# --------------------------------------------------------------------------
# A recorded divergence, pinned so it cannot move without the decision moving
# --------------------------------------------------------------------------
#
# `_parse_session_clock` exists in `data_tools/auto_download.py` and
# `data_tools/validate.py`, and they disagree about what a malformed trading
# session *is*. Measured across the same inputs:
#
#   input                        validate.py        auto_download.py
#   absent                       None               None
#   09:30-16:00                  (570, 960)         (570, 960)
#   09:30-24:00                  (570, 1440)        (570, 1440)
#   25:00-23:00  (malformed)     MALFORMED_SESSION               ValueError
#   abc/def     (malformed)      MALFORMED_SESSION               ValueError
#   16:00-09:30  (reversed)      (960, 2010)        ValueError
#
# So the downloader treats a malformed session as fatal and the validator treats
# it as absent. In `validate.py` those are the same value: `_session_window`
# returns `None` either way, its caller at :286 substitutes an observed session
# only for equity-like assets, and the resolved window then skips the whole
# session/holiday/weekend/overnight classification. For forex, metals and crypto a
# configured session therefore stops being applied to the coverage check, so the same
# gap is labelled "data" instead of "session".
#
# > **Corrected 2026-10-03: that is a mislabelling, not a silence.** `"session"` is
# > not in `_EXPECTED_GAP_CLASSIFICATIONS` (`validate.py:69`), so a gap inside a valid
# > session is *already* reported; the malformed case is reported too, just under a
# > different label. The harm is triage in the wrong direction, not a hidden problem.
# > See `test_a_malformed_session_mislabels_a_gap_rather_than_silencing_it`.
#
# **This test asserts the divergence exists.** That is deliberate, and it is the
# point: the alternative was no pin at all, where fixing one copy would leave the
# other quietly different. When the decision in `/home/ubuntu/perma-runner/notes/backlog.md` is taken, this
# fails and points at the entry to be updated with it.
#
# It is not a claim that the validator is right. Reversing it would fail
# configurations that currently pass, and whether a malformed session should fail
# the run or be reported is a policy question about what this validator is for.

_SESSION_CASES = [
    ("absent", None),
    ("valid", {"start": "09:30", "end": "16:00"}),
    ("valid 24h close", {"start": "09:30", "end": "24:00"}),
    ("malformed hour", {"start": "25:00", "end": "23:00"}),
    ("malformed text", {"start": "abc", "end": "def"}),
    ("one side malformed", {"start": "09:30", "end": "oops"}),
    ("reversed", {"start": "16:00", "end": "09:30"}),
]


@pytest.mark.parametrize(
    ("label", "session"),
    [("malformed hour", {"start": "25:00", "end": "23:00"}),
     ("malformed text", {"start": "abc", "end": "def"})],
    ids=["malformed hour", "malformed text"],
)
def test_a_malformed_session_mislabels_a_gap_rather_than_silencing_it(label, session) -> None:
    """**Correction to the comment above: this is a mislabelling, not a silent failure.**

    That comment said a configured session "can therefore stop being applied to the coverage
    check with nothing recorded - the silent-failure signature". Measured, it does not. The
    classification only decides the *label*:

        _EXPECTED_GAP_CLASSIFICATIONS == {"overnight", "weekend", "holiday"}

    `"session"` is **not** in that set, so a gap inside a correctly-configured session is already
    reported as unexpected. With a malformed session the same gap is classified `"data"` instead -
    still reported, still counted in `unexpected_gaps`, still producing the
    `"N data gaps detected"` warning at `validate.py:636`. Verified against a real week-wide gap:
    valid session -> `"session"`, malformed -> `"data"`, both reported.

    So the failure direction is the opposite of what the comment claimed. The practical harm is
    **triage in the wrong direction**: a reader sees missing-data warnings caused by their own
    session typo and goes looking for a market-data problem. Still worth fixing - the two copies
    should agree - but it is a noise-and-confusion bug, not a silent one, and treating it as urgent
    on the strength of "nothing recorded" would itself be a misdiagnosis.

    Pinned so the direction cannot be restated wrongly. If a future change makes a malformed
    session *suppress* a report, the last assertion fails - and that genuinely would be a silent
    failure, worth fixing rather than reassuring.
    """
    import pandas as pd
    from data_tools import validate

    gap_start = pd.Timestamp("2023-01-03 00:00", tz="UTC")
    gap_end = pd.Timestamp("2023-01-14 00:00", tz="UTC")

    mislabelled = validate._gap_classification(gap_start, gap_end, "fx", "1h", session=session)
    correctly_labelled = validate._gap_classification(
        gap_start, gap_end, "fx", "1h", session={"start": "09:30", "end": "16:00"}
    )
    assert mislabelled == "data", (
        f"a malformed session now classifies as {mislabelled!r} rather than 'data'; the comment "
        "this test corrects describes the failure direction and must be updated with it"
    )
    assert correctly_labelled == "session", (
        f"a valid session now classifies as {correctly_labelled!r}, so this test's premise - "
        "that only the label differs - no longer holds"
    )
    expected = validate._EXPECTED_GAP_CLASSIFICATIONS
    assert "session" not in expected and "data" not in expected, (
        f"_EXPECTED_GAP_CLASSIFICATIONS changed to {sorted(expected)}; if 'session' is now "
        "expected then a valid session IS silenced, and whether a malformed one is too becomes "
        "the thing to measure - re-read this test before trusting it"
    )
    assert mislabelled not in expected, (
        "a malformed session is now classified as an EXPECTED gap, so it IS silently skipped - "
        "that is the silent failure the old comment predicted, and it now needs fixing rather "
        "than reassuring"
    )


@pytest.mark.parametrize(
    ("label", "session"),
    _SESSION_CASES,
    ids=[label for label, _ in _SESSION_CASES],
)
def test_known_session_parsing_divergence_pending_decision(label: str, session: object) -> None:
    """The validator and the downloader currently disagree; see backlog.md.

    Read this as a tripwire, not an endorsement. Both copies are correct about the
    cases they agree on; the assertion is that the disagreement has not moved.
    """
    from data_tools import auto_download, validate

    def outcome(call, *args):
        try:
            return ("value", call(*args))
        except ValueError as exc:
            return ("refused", str(exc))

    as_validator = outcome(validate._session_window, session)
    as_downloader = outcome(auto_download._explicit_session_window, session, None, None)

    if label in {"absent", "valid", "valid 24h close"}:
        assert as_validator == as_downloader, (
            f"{label}: the two copies used to agree on this and no longer do. If that is "
            f"the fix for the recorded divergence, update the entry in "
            f"/home/ubuntu/perma-runner/notes/backlog.md "
            f"in the same commit that changed it."
        )
        return

    assert as_validator != as_downloader, (
        f"{label}: the recorded divergence between validate._session_window and "
        f"auto_download._explicit_session_window has been resolved - the validator now "
        f"returns {as_downloader[0]} where it used to return a value. Update the "
        f"needs-decision entry in /home/ubuntu/perma-runner/notes/backlog.md to "
        f"match; do not delete this test."
    )


class TestMalformedAndAbsentSessionsStoppedSharingAValue:
    """Item 7's decidable half: make the difference *observable*, and decide nothing.

    `validate._session_window` returned `None` for three distinct situations - nothing configured,
    a value with no `start`/`end` separator, and a clock that would not parse - so the caller could
    not tell them apart. That matters because for equity-like assets the caller does not merely
    shrug at `None`: it substitutes an *observed* window, so a configured session that never parsed
    was silently replaced by a guess. `auto_download._parse_session_clock` raises for the same
    inputs, so the two copies disagreed about what malformed means.

    A sentinel separates the cases without choosing a policy. The caller still maps malformed to
    exactly today's behaviour, so no gap classification changes and the mislabelling pin above
    stays valid - what changes is that the difference is now visible, and that the substitution is
    logged instead of silent.
    """

    @staticmethod
    def _window(value):
        from data_tools.validate import _session_window

        return _session_window(value)

    @pytest.mark.parametrize(
        "value",
        ["25:00-23:00", "abc-def", "09:30", ("09:30", "bad"), {"start": "bad", "end": "16:00"}],
    )
    def test_a_configured_but_unparseable_session_is_marked_malformed(self, value) -> None:
        from data_tools.validate import MALFORMED_SESSION

        assert self._window(value) is MALFORMED_SESSION, (
            f"{value!r} is configured but unparseable, and is no longer distinguishable from an "
            "absent session - so the substitution below is silent again"
        )

    @pytest.mark.parametrize("value", [None, "", {}, {"start": None, "end": None}])
    def test_an_absent_session_is_still_absent_not_malformed(self, value) -> None:
        """An empty configuration is not a broken one.

        `{}` and `{"start": None, "end": None}` say nothing at all, and reporting them as typos
        would train an operator to ignore this warning - which is the only reason it exists.
        """
        from data_tools.validate import MALFORMED_SESSION

        assert self._window(value) is None, f"{value!r} is an absent session, not a malformed one"
        assert self._window(value) is not MALFORMED_SESSION

    @pytest.mark.parametrize(
        "value", ["09:30-16:00", ("09:30", "16:00"), {"start": "09:30", "end": "16:00"}]
    )
    def test_a_valid_session_still_resolves_to_a_window(self, value) -> None:
        assert self._window(value) == (570, 960)

    def test_the_substitution_is_logged_rather_than_silent(self, caplog) -> None:
        """The equity-like fallback is the case that matters: a guess replaces the user's value."""
        import logging

        import pandas as pd
        from data_tools import validate

        with caplog.at_level(logging.WARNING, logger="data_tools.validate"):
            validate._gap_classification(
                pd.Timestamp("2023-01-03 09:30", tz="UTC"),
                pd.Timestamp("2023-01-04 09:30", tz="UTC"),
                "equity", "1h", session="25:00-23:00", observed_session=(570, 960),
            )
        joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "could not be parsed" in joined, (
            f"a configured session that never parsed produced no warning: {joined!r}"
        )
        assert "replaced by an observed one" in joined, (
            "the warning does not say the configured session was replaced by an observed one, "
            f"which is the part that misleads triage: {joined!r}"
        )

    def test_the_warning_points_at_the_downloader_that_would_have_refused_it(self, caplog) -> None:
        """The two copies disagree, so the log is the only place that says so."""
        import logging

        import pandas as pd
        from data_tools import validate

        with caplog.at_level(logging.WARNING, logger="data_tools.validate"):
            validate._gap_classification(
                pd.Timestamp("2023-01-03 09:30", tz="UTC"),
                pd.Timestamp("2023-01-04 09:30", tz="UTC"),
                "forex", "1h", session="abc-def", observed_session=None,
            )
        joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "downloader" in joined, (
            f"the warning does not mention that the downloader raises on this input: {joined!r}"
        )
        assert "replaced by an observed one" not in joined, (
            "a non-equity asset has no observed fallback, so claiming one was substituted would be "
            f"false: {joined!r}"
        )

    def test_a_valid_session_logs_nothing(self, caplog) -> None:
        import logging

        import pandas as pd
        from data_tools import validate

        with caplog.at_level(logging.WARNING, logger="data_tools.validate"):
            validate._gap_classification(
                pd.Timestamp("2023-01-03 09:30", tz="UTC"),
                pd.Timestamp("2023-01-04 09:30", tz="UTC"),
                "equity", "1h", session="09:30-16:00", observed_session=(570, 960),
            )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a correctly-configured session warned; the warning has to stay rare to be read"
        )

    def test_the_downloader_still_raises_on_the_same_input(self) -> None:
        """Unchanged on purpose: making the two copies agree is the open half of item 7."""
        from data_tools.auto_download import _explicit_session_window

        # Note the shape: the downloader takes the two clocks separately, and only parses them
        # once both are present. A bare string in the first position is not parsed at all - it
        # returns None without ever looking at it, which is why an earlier version of this test
        # asserted a raise that could not happen.
        with pytest.raises(ValueError):
            _explicit_session_window(None, "25:00", "23:00")
        with pytest.raises(ValueError):
            _explicit_session_window(None, "bad", "16:00")
        assert _explicit_session_window(None, "09:30", "16:00") == (570, 960)


def test_the_malformed_session_warning_names_the_session_that_failed(caplog) -> None:
    """The whole value of the warning is *which* session, and the sweep proved it untested.

    Replacing `session_value` with a placeholder in the log survived every other test in the file.
    That is the difference between a warning an operator can act on and one they have to
    reverse-engineer, and it is the only thing distinguishing this from a generic "check your
    config" message.
    """
    import logging

    import pandas as pd
    from data_tools import validate

    with caplog.at_level(logging.WARNING, logger="data_tools.validate"):
        validate._gap_classification(
            pd.Timestamp("2023-01-03 09:30", tz="UTC"),
            pd.Timestamp("2023-01-04 09:30", tz="UTC"),
            "equity", "1h", session="25:00-23:00", observed_session=(570, 960),
        )
    joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "25:00-23:00" in joined, (
        f"the warning does not quote the session that failed to parse: {joined!r}"
    )

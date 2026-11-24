"""Guard against new private indicator copies (issue #317, phase P4).

``shared/indicators`` is the single home for indicator math: every ATR,
RSI, EMA, Supertrend and friends must come from ``core``/``presets`` (or a
``@register_indicator`` custom indicator), never a fresh ``.ewm()``/
``.rolling()`` chain or hand-rolled recursion. The P0-P3 migrations removed
the copies that existed when the module landed; this test keeps them from
coming back.

The scan enumerates **tracked** files only (``git ls-files`` — untracked
local clutter must not fail the suite; git being unavailable fails loudly)
and is AST-based over first-party ``*.py`` (tests, docs, frontend,
``shared/indicators`` itself and generated/vendored trees excluded) and
records two kinds of signal per file:

- ``ewm@<func>`` / ``rolling@<func>`` — each ``.ewm(...)``/``.rolling(...)``
  call, attributed to its enclosing function (``<module>`` at top level).
- ``def:<name>`` — a function whose name carries an indicator keyword
  (``atr``, ``rsi``, ``supertrend``, ...) *and* whose body contains a
  compute primitive (a loop, a ``np.*`` attribute, or pandas window/reduction
  calls). Name-only matches like ``_supertrend_states`` or ``stop_atr``
  survive only when the body actually computes something.

``_ALLOWED`` pins the exact per-file signal counts of the copies that still
exist on purpose: the ``strategies/`` entries are pre-P3 private
implementations awaiting their one-strategy-per-PR parity migration (the
``pine_*`` ones were removed by the P3 migration in PR #375), the
``ref_*``/agent strategies and parity/audit scripts are self-contained
reference oracles,
and the two non-strategy entries are reviewed non-computations kept for
name/shape reasons. Files under ``strategies/`` carrying generated-code
provenance (a ``.generated.json`` marker or a
``STRATEGY_META.provenance.generated`` declaration) are exempt from this
roster: the codegen contract requires
self-contained pandas, so its indicator chains are emitted, not copied.
Any *new* signal — a new file, a new helper, an extra
``.ewm()`` in an existing function — fails here; any *removed* signal fails
too, so the roster cannot silently rot (same contract as
``scripts/gate-tests.sh``).

Third-party indicator libraries (``pandas_ta``, ``ta``) are refused
outright: the maintainer decision on #317 removed ``pandas_ta`` and no
supported environment may grow a dependency on it again.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories never scanned: tests carry frozen reference copies on
#: purpose, docs/frontend hold no first-party Python, and the module itself
#: is where indicator math is allowed to live.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "data",
    "logs",
    "docs",
    "frontend",
    "tests",
    ".agents",
    ".claude",
    ".codex",
    ".t3",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

#: Function names that mark a private indicator implementation when the
#: body also shows compute primitives. The keyword must be ``_``-delimited
#: (or the whole name) so ``normalize``/``persist`` do not match; the
#: optional leading token covers the repo's ``_calc_*``/``_wilder_*``/
#: ``_mt5_*`` naming.
_INDICATOR_NAME_RE = re.compile(
    r"(?:^|_)(?:calc|compute|wilder|mt5|pine|ta|get|make|build|find|series"
    r"|values|true|stop)?"
    r"(?:atr|rsi|ema|sma|rma|macd|dema|tema|supertrend|wavetrend|adx|stoch"
    r"|cci|vwap|donchian|keltner|bbands|bollinger|obv|mfi|willr|dpo|roc|mom"
    r"|hull|alma|kama|true_range|trange)(?:_|$)",
    re.IGNORECASE,
)

#: Attribute calls that make a function body "compute" something.
_PRIMITIVE_ATTRS = {
    "ewm",
    "rolling",
    "diff",
    "cumsum",
    "shift",
    "pct_change",
    "rank",
    "expanding",
    "apply",
}


def _has_compute_primitive(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.For, ast.While, ast.AsyncFor)):
            return True
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr in _PRIMITIVE_ATTRS:
                return True
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id in {"np", "numpy"}
        ):
            return True
    return False


class _SignalTracker(ast.NodeVisitor):
    """Collect ``ewm@func``/``rolling@func`` call-site tags per file."""

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.tags: Counter[str] = Counter()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"ewm", "rolling"}:
            owner = self._stack[-1] if self._stack else "<module>"
            self.tags[f"{func.attr}@{owner}"] += 1
        self.generic_visit(node)


def _scan_file(path: Path) -> tuple[Counter[str], list[str]]:
    """Return (private-copy signal counts, forbidden library imports)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tags: Counter[str] = Counter()

    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in {"pandas_ta", "ta"}:
                    forbidden.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in {"pandas_ta", "ta"}:
                forbidden.append(f"from {module} import ...")

    tracker = _SignalTracker()
    tracker.visit(tree)
    tags.update(tracker.tags)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _INDICATOR_NAME_RE.search(node.name) and _has_compute_primitive(node):
                tags[f"def:{node.name}"] += 1

    return tags, forbidden


def _iter_source_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Tracked ``*.py`` files only, via ``git ls-files``.

    Scanning the working tree would flag unrelated local clutter (a scratch
    ``.venv-alt``, an editor folder, an untracked strategy draft) and fail the
    suite for reasons unrelated to the change. Only files git tracks are
    first-party source; if git cannot answer, fail loudly rather than fall
    back to an untracked-file scan.
    """
    # Inherit the user environment minus GIT_*: a parent GIT_DIR/GIT_WORK_TREE
    # must not redirect the query, while HOME (and with it a user's
    # safe.directory setting) stays so container/devcontainer checkouts owned
    # by another uid do not die on "dubious ownership".
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            env=env,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "private-copy guard requires `git ls-files` to enumerate tracked "
            f"sources; git is unavailable or failed: {exc}"
        ) from exc

    files: list[Path] = []
    for raw in proc.stdout.decode("utf-8").split("\0"):
        if not raw:
            continue
        rel = Path(raw)
        # Same exclusions the directory walk applied: skipped directory
        # names at any depth, plus the indicators package itself (the one
        # place indicator math is allowed to live).
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if rel.parts[:2] == ("shared", "indicators"):
            continue
        path = repo_root / rel
        # `ls-files` still lists a tracked file deleted in the working tree;
        # scanning it would crash on FileNotFoundError. Skip it — the roster
        # comparison then reports the removed signal, which is the intended
        # message for a deleted allowlisted file.
        if not path.is_file():
            continue
        files.append(path)
    return files


def _is_generated_strategy(path: Path, repo_root: Path = REPO_ROOT) -> bool:
    """True only for ``strategies/`` files carrying generated-code provenance.

    Codegen emits self-contained pandas by contract (pinned by
    ``test_generated_code_is_self_contained_no_risky_imports``), so a
    committed generated strategy will necessarily contain ``.rolling()``/
    ``.ewm()`` chains this guard would otherwise flag. The exemption is
    deliberately narrow: the file must live under ``strategies/`` AND carry
    generated provenance — the ``.generated.json`` marker written by
    ``mark_generated_strategy`` or a ``STRATEGY_META.provenance.generated``
    declaration (both defined by ``runner.approval_gate``). Hand-written code
    cannot qualify by accident — any file *can* declare the provenance, but
    ``require_generated_strategy_approval`` then demands an approval ledger
    entry before the strategy may run, which is the real deterrent.
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    if not rel.parts or rel.parts[0] != "strategies":
        return False
    from runner.approval_gate import is_generated_strategy

    return is_generated_strategy(path)


#: Expected signal counts per repository-relative path. See the module
#: docstring for what each group is and why it is still here.
_ALLOWED: dict[str, dict[str, int]] = {
    # Presentation reshaping of already-computed series for the viewer
    # contract — not an indicator computation.
    "runner/engines/_indicator_series.py": {"def:_build_supertrend_series": 1},
    # MT5 parity analysis CLI handler (``stop_atr`` subcommand), not an
    # indicator implementation.
    "scripts/analyze_mt5_parity.py": {"def:stop_atr": 1},
    # Parity reference oracles: deliberately self-contained reference
    # implementations the differential tests compare engines against.
    "scripts/reference_parity/reference_strategies.py": {
        "def:true_range": 1,
        "rolling@init": 2,
    },
    # Pre-P3 private implementations awaiting their one-strategy-per-PR
    # parity migration (#317). PR #375 (P3) migrated
    # custom_swings_supertrend, ema_crossoiver_conversion_test,
    # pine_superosc_vumanchu and pine_supertrend_dema onto
    # ``shared.indicators`` presets, so their entries are gone;
    # ``scripts/bench_indicators_numba.py`` benchmarks the shared kernels
    # in ``shared.indicators.core`` and carries no private copy.
    "strategies/channel_breakout/channel_breakout.py": {
        "ewm@generate_signals": 1,
        "rolling@generate_signals": 3,
    },
    "strategies/cross_asset_momentum/cross_asset_momentum.py": {
        "rolling@generate_signals": 2,
    },
    "strategies/demo_agent/demo_agent.py": {"rolling@generate_signals": 2},
    "strategies/example_sma/example_sma.py": {"ewm@generate_signals": 2},
    "strategies/gold_momentum/gold_momentum.py": {"rolling@generate_signals": 2},
    "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py": {
        "def:_mt5_atr": 1,
        "def:_wilder_atr": 1,
    },
    "strategies/lab_generated_strategy/lab_generated_strategy.py": {
        "ewm@generate_signals": 2,
    },
    "strategies/long_agent/long_agent.py": {"rolling@generate_signals": 2},
    "strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py": {
        "rolling@generate_signals": 1,
    },
    "strategies/post_vix_dip_buy/post_vix_dip_buy.py": {
        "def:_wilder_rsi": 1,
        "ewm@_wilder_rsi": 2,
        "rolling@generate_signals": 1,
    },
    "strategies/pynecore_sma_crossover/pynecore_sma_crossover_context.py": {
        "rolling@build_trade_context": 3,
    },
    "strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py": {
        "def:_calc_adx": 1,
        "def:_wilder_rma": 1,
        "ewm@_wilder_rma": 1,
    },
    "strategies/ref_bracket_long/ref_bracket_long.py": {
        "def:_true_range": 1,
        "rolling@generate_signals": 3,
    },
    "strategies/ref_bracket_short/ref_bracket_short.py": {
        "def:_true_range": 1,
        "rolling@generate_signals": 3,
    },
    "strategies/ref_sma_cross_all_in/ref_sma_cross_all_in.py": {
        "rolling@generate_signals": 2,
    },
    "strategies/retry_agent/retry_agent.py": {"rolling@generate_signals": 2},
    "strategies/risk_parity/risk_parity.py": {"rolling@generate_signals": 4},
    "strategies/sector_rotation/sector_rotation.py": {
        "rolling@generate_signals": 1,
    },
    "strategies/stat_arb_pairs/stat_arb_pairs.py": {
        "rolling@generate_signals": 2,
    },
    "strategies/volatility_breakout/volatility_breakout.py": {
        "def:_wilder_rma": 1,
        "ewm@_wilder_rma": 1,
    },
    # Audit script that monkeypatches a strategy's ATR for MT5 parity
    # scoring — it patches, it does not implement an indicator.
    "strategy-comparisons/intraday_volatility_breakout/audit/run_native_window.py": {
        "def:_inject_atr": 1,
    },
}


def test_no_third_party_indicator_libraries() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {hit}"
        for path in _iter_source_files()
        for hit in _scan_file(path)[1]
    ]
    assert not offenders, (
        "third-party indicator libraries are banned on the live path "
        "(issue #317 maintainer decision); use shared.indicators instead:\n"
        + "\n".join(offenders)
    )


def _roster_problems(
    paths: Iterable[Path],
    *,
    expected: dict[str, dict[str, int]] | None = None,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Compare scanned private-copy signals against the ``_ALLOWED`` roster.

    Generated strategies are exempt (see ``_is_generated_strategy``):
    codegen emits self-contained pandas by contract, so its ``.rolling()``/
    ``.ewm()`` chains are required, not copied indicators. Parameterised over
    the file set and root so the exemption wiring itself can be tested on a
    scratch tree — with no tracked generated file in the repo, the skip in
    ``test_private_indicator_copies_match_roster`` would otherwise be
    untested.
    """
    roster = _ALLOWED if expected is None else expected
    actual: dict[str, dict[str, int]] = {}
    for path in paths:
        if _is_generated_strategy(path, repo_root=repo_root):
            continue
        tags, _forbidden = _scan_file(path)
        if tags:
            actual[path.relative_to(repo_root).as_posix()] = dict(sorted(tags.items()))

    expected_map = {name: dict(sorted(counts.items())) for name, counts in roster.items()}

    stale_entries = sorted(
        name for name in expected_map if not (repo_root / name).exists()
    )
    missing_signals = sorted(
        name
        for name in expected_map
        if (repo_root / name).exists() and name not in actual
    )
    unexpected = {
        name: counts for name, counts in actual.items() if name not in expected_map
    }
    mismatched = {
        name: {"expected": expected_map[name], "actual": actual[name]}
        for name in expected_map.keys() & actual.keys()
        if expected_map[name] != actual[name]
    }

    problems: list[str] = [
        f"{name}: allowlisted file no longer exists — drop the entry"
        for name in stale_entries
    ]
    problems.extend(
        f"{name}: allowlisted signals no longer found — shrink or drop the entry"
        for name in missing_signals
    )
    problems.extend(
        f"{name}: new private indicator copy {counts} — "
        "use shared.indicators instead of adding to _ALLOWED"
        for name, counts in unexpected.items()
    )
    problems.extend(
        f"{name}: signal counts changed {diff}"
        for name, diff in mismatched.items()
    )

    return problems


def test_private_indicator_copies_match_roster() -> None:
    problems = _roster_problems(_iter_source_files())
    assert not problems, "\n".join(problems)


def _write_generated_fixture(root: Path, rel: str, *, marker: bool, declared: bool) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    provenance = ', "provenance": {"generated": True}' if declared else ""
    path.write_text(
        "STRATEGY_META = {\n"
        f'    "name": "fixture"{provenance}\n'
        "}\n\n"
        "def generate_signals(df, params):\n"
        "    return df['close'].rolling(3).mean(), None, {}, []\n",
        encoding="utf-8",
    )
    if marker:
        path.with_name(path.name + ".generated.json").write_text("{}", encoding="utf-8")
    return path


def test_generated_strategy_exemption_is_narrow(tmp_path: Path) -> None:
    """Only strategies/-tree files with generated provenance are exempt."""
    marked = _write_generated_fixture(
        tmp_path, "strategies/gen_x/gen_x.py", marker=True, declared=False
    )
    declared = _write_generated_fixture(
        tmp_path, "strategies/gen_y/gen_y.py", marker=False, declared=True
    )
    hand_written = _write_generated_fixture(
        tmp_path, "strategies/hand/hand.py", marker=False, declared=False
    )
    outside_tree = _write_generated_fixture(
        tmp_path, "scripts/gen_z.py", marker=True, declared=False
    )

    assert _is_generated_strategy(marked, repo_root=tmp_path)
    assert _is_generated_strategy(declared, repo_root=tmp_path)
    assert not _is_generated_strategy(hand_written, repo_root=tmp_path)
    assert not _is_generated_strategy(outside_tree, repo_root=tmp_path)


def test_generated_exempt_file_still_flagged_without_provenance(tmp_path: Path) -> None:
    """The same indicator-copying source fails the roster when not generated."""
    path = _write_generated_fixture(
        tmp_path, "strategies/plain/plain.py", marker=False, declared=False
    )
    tags, _ = _scan_file(path)
    assert tags, "fixture must produce a private-copy signal to be meaningful"


def test_roster_comparison_applies_the_generated_exemption(tmp_path: Path) -> None:
    """The exemption must be wired into the roster check itself.

    No tracked file in the repo is generated today, so the ``continue`` in
    ``_roster_problems`` was previously unpinned: deleting it changed
    nothing. On a scratch tree the marked file must produce no problem
    while the identical unmarked file is flagged as a new private copy.
    """
    marked = _write_generated_fixture(
        tmp_path, "strategies/gen_x/gen_x.py", marker=True, declared=False
    )
    unmarked = _write_generated_fixture(
        tmp_path, "strategies/plain_y/plain_y.py", marker=False, declared=False
    )

    assert _roster_problems([marked], expected={}, repo_root=tmp_path) == []

    problems = _roster_problems([marked, unmarked], expected={}, repo_root=tmp_path)
    assert len(problems) == 1
    assert "strategies/plain_y/plain_y.py" in problems[0]
    assert "new private indicator copy" in problems[0]


def test_iter_source_files_tolerates_tracked_but_deleted(tmp_path: Path) -> None:
    """A tracked file deleted in the working tree is skipped, not a crash.

    ``git ls-files`` keeps listing a deleted-but-unstaged file; ``_scan_file``
    would raise FileNotFoundError on it. Build a scratch repo, commit a
    file, delete it from the worktree, and the guard must return the
    remaining files without raising.
    """
    import subprocess as sp

    def git(*args: str) -> None:
        sp.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
        )

    git("init", "-q")
    kept = tmp_path / "kept.py"
    kept.write_text("x = 1\n", encoding="utf-8")
    deleted = tmp_path / "deleted.py"
    deleted.write_text("y = 2\n", encoding="utf-8")
    git("add", "kept.py", "deleted.py")
    git(
        "-c", "user.email=test@example.com", "-c", "user.name=test",
        "commit", "-qm", "init",
    )
    deleted.unlink()

    files = _iter_source_files(repo_root=tmp_path)

    assert files == [kept]

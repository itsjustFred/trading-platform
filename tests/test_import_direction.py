"""Import-direction guard for the top-level packages.

Stable code must never import volatile code: ``shared`` models and helpers are
imported by every layer, so a ``shared -> live_runner`` edge would let the most
stable package depend on the most changeable one (and can create import cycles,
as happened with ``shared.trailing -> signal_bots -> live_runner.base_adapter``).

This test statically scans every ``import``/``from ... import`` statement in the
top-level packages — including ``TYPE_CHECKING`` blocks and function-level
imports, which still create coupling — and fails when a package imports a
higher layer. It never imports the scanned modules, so it is fast and has no
side effects.
"""

from __future__ import annotations

import ast
import functools
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Top-level packages ordered from the most stable/dependency-free layer to the
# most volatile application layer. A package may import its own layer or any
# lower one; importing a higher layer is forbidden.
#
#   shared       models/constants/database/logging — the leaf everything uses
#   strategies   user strategy modules; may only use shared helpers, so they
#                sit directly above shared and below every platform layer
#   risk         risk engine and models, built on shared
#   data_tools   market-data download/catalog/resample, built on shared
#   signal_bots  signal ingestion producing risk.models.TradeSignal; consumed
#                by runner (signal replay), scheduler (health checks) and
#                live_runner (daemon registry). Scheduled for removal.
#   runner       backtest engines and execution on risk + data_tools;
#                live_runner reuses the engine for internal strategies
#   validation   post-hoc comparison/validation statistics on runner.stats;
#                only scripts consume it, so it sits directly above runner
#   scheduler    background jobs (data refresh, bot health) orchestrating the
#                layers below; embedded by the live daemon
#   live_runner  live execution on runner engines + risk + shared
#   viewer       FastAPI app composing the whole stack
#   scripts      operator CLIs
LAYERS: dict[str, int] = {
    "shared": 0,
    "strategies": 1,
    "risk": 2,
    "data_tools": 3,
    "signal_bots": 4,
    "runner": 5,
    "validation": 6,
    "scheduler": 7,
    "live_runner": 8,
    "viewer": 9,
    "scripts": 10,
}

# Known upward edges that already exist on main, as (importer module,
# imported module) pairs. Each entry is a real violation kept only so this test
# can land green; the list must only shrink — when an edge is removed from the
# code, remove it here too (test_allowlist_edges_still_exist enforces that).
# Edges inside ``if TYPE_CHECKING:`` and function-level imports count: they
# still couple the packages. A pair covers every import line between those two
# modules, so a *new* importer->importee module pair is always caught.
ALLOWED_UPWARD_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        # shared/venue_calendar.py resolves Dukascopy symbol shapes through a
        # lazy, failure-tolerant import of the downloader's SYMBOL_CONFIG.
        ("shared.venue_calendar", "data_tools.dukascopy.native"),
        # shared/database.py lazy-imports live-runner helpers inside functions
        # to avoid import cycles at module load time.
        ("shared.database", "live_runner.base_adapter"),
        ("shared.database", "live_runner.broker_reconciliation"),
        ("shared.database", "live_runner.credential_env"),
        ("shared.database", "live_runner.exit_signals"),
        ("shared.database", "live_runner.loss_breaker"),
        ("shared.database", "live_runner.pre_submit_caps"),
        ("shared.database", "live_runner.service_discovery"),
        ("shared.database", "live_runner.stockkit_routing"),
        ("shared.database", "risk.live"),
        # shared/strategy_registry.py lazy-imports the approval gate so listing
        # strategies never executes the gate.
        ("shared.strategy_registry", "runner.approval_gate"),
        # shared/trailing.py lazy-imports the bot registry; the processors
        # import shared.trailing, so a module-level import would be a cycle.
        ("shared.trailing", "signal_bots.registry"),
        # data_tools/downloaders/alpaca_dl.py reads the paper-trading flag via
        # a lazy import of the credential-env parser.
        ("data_tools.downloaders.alpaca_dl", "live_runner.credential_env"),
        # runner/promotion_cli.py lazy-imports the promotion flow.
        ("runner.promotion_cli", "live_runner.promotion"),
        # signal_bots is scheduled for removal; rocketfuel's processor still
        # imports the adapter's unbounded-TP policy constant.
        ("signal_bots.rocketfuel.processor", "live_runner.base_adapter"),
    }
)

# Top-level directories that contain .py files but are deliberately not part
# of the layered package graph: the test suite itself and one-off strategy
# comparison/audit scripts (not importable packages, not platform code).
NON_PACKAGE_DIRS = frozenset({"tests", "strategy-comparisons"})

_PRUNE_DIRS = frozenset({"node_modules", "__pycache__", ".venv"})


def _top_level_dirs_with_python() -> set[str]:
    found = set()
    for entry in REPO_ROOT.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        for _dirpath, dirnames, filenames in os.walk(entry):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
            if any(name.endswith(".py") for name in filenames):
                found.add(entry.name)
                break
    return found


def _repo_modules() -> dict[str, Path]:
    """Map dotted module name -> file for every .py under the layered packages."""
    modules: dict[str, Path] = {}
    for package in LAYERS:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            module = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            modules[module] = path
    return modules


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    """Longest known module that ``name`` refers to (``shared.models.Foo`` ->
    ``shared.models`` when ``Foo`` is not a module)."""
    while name:
        if name in modules:
            return name
        name = name.rpartition(".")[0]
    return None


def _import_targets(
    node: ast.Import | ast.ImportFrom, module: str, is_package_init: bool
) -> list[str]:
    """Candidate dotted names an import statement may refer to."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        base = module.split(".")
        if not is_package_init:
            base = base[:-1]
        base = base[: len(base) - (node.level - 1)]
        mod = ".".join(base + ([node.module] if node.module else []))
    else:
        mod = node.module or ""
    return [mod] + [f"{mod}.{alias.name}" for alias in node.names]


def _dynamic_import_target(node: ast.AST) -> str | None:
    """Module-name literal in ``importlib.import_module("x")`` /
    ``__import__("x")`` calls, else ``None``. Only absolute string literals
    can be resolved statically; relative or computed names are skipped."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_dynamic = (
        isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}
    ) or (
        isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}
    )
    if not is_dynamic:
        return None
    if node.args:
        arg = node.args[0]
    else:
        arg = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


@functools.lru_cache(maxsize=1)
def _cross_package_edges() -> tuple[tuple[str, str, Path, int], ...]:
    """All (importer module, imported module, file, line) edges between
    different top-level packages. Static ``import`` statements and dynamic
    ``import_module``/``__import__`` calls with a literal module name both
    count; a file that does not parse fails the guard rather than being
    silently skipped. Cached: the AST scan costs several seconds and both
    guard tests need the same result."""
    modules = _repo_modules()
    edges: set[tuple[str, str, Path, int]] = set()
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        is_init = path.name == "__init__.py"
        src_package = module.split(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = _import_targets(node, module, is_init)
            elif isinstance(node, ast.Call):
                target = _dynamic_import_target(node)
                targets = [target] if target is not None else []
            else:
                continue
            resolved_targets = {
                resolved
                for target in targets
                if (resolved := _resolve(target, modules)) is not None
                and resolved != module
            }
            # ``from pkg import mod`` yields both ``pkg`` and ``pkg.mod``;
            # keep only the longest resolution so each edge names the real
            # target module rather than its parent package as well.
            resolved_targets = {
                resolved
                for resolved in resolved_targets
                if not any(
                    other != resolved and other.startswith(f"{resolved}.")
                    for other in resolved_targets
                )
            }
            for resolved in resolved_targets:
                dst_package = resolved.split(".")[0]
                if dst_package == src_package or dst_package not in LAYERS:
                    continue
                edges.add((module, resolved, path, node.lineno))
    return tuple(sorted(edges))


def _is_upward(edge: tuple[str, str, Path, int]) -> bool:
    src, dst, _, _ = edge
    return LAYERS[src.split(".")[0]] < LAYERS[dst.split(".")[0]]


def test_no_new_upward_import_edges() -> None:
    violations = [
        edge
        for edge in _cross_package_edges()
        if _is_upward(edge) and (edge[0], edge[1]) not in ALLOWED_UPWARD_EDGES
    ]
    if violations:
        lines = [
            f"  {src} -> {dst}  ({path.relative_to(REPO_ROOT)}:{lineno})"
            for src, dst, path, lineno in violations
        ]
        pytest.fail(
            "New import edge(s) go against the package layer order "
            f"({', '.join(f'{name}={layer}' for name, layer in LAYERS.items())}). "
            "Lower layers must not import higher layers; move the shared code "
            "down or invert the dependency instead:\n" + "\n".join(lines)
        )


def test_layers_cover_all_top_level_packages() -> None:
    """LAYERS is hand-maintained: every top-level directory containing .py
    files must be classified, so a new package cannot bypass the guard."""
    found = _top_level_dirs_with_python()
    unclassified = found - set(LAYERS) - NON_PACKAGE_DIRS
    assert not unclassified, (
        "Top-level directories with .py files are not classified: "
        f"{sorted(unclassified)}. Add each to LAYERS with a justified layer "
        "position, or to NON_PACKAGE_DIRS if it is not platform code."
    )
    top_level_dirs = {
        entry.name for entry in REPO_ROOT.iterdir() if entry.is_dir()
    }
    missing = set(LAYERS) - top_level_dirs
    assert not missing, f"LAYERS entries without a top-level directory: {sorted(missing)}"


def test_allowlist_edges_still_exist() -> None:
    edges = _cross_package_edges()
    edge_pairs = {(src, dst) for src, dst, _, _ in edges}
    stale = []
    for src, dst in sorted(ALLOWED_UPWARD_EDGES):
        if (src, dst) not in edge_pairs:
            stale.append(f"{src} -> {dst}: edge no longer exists; remove it")
        elif not _is_upward((src, dst, REPO_ROOT, 0)):
            stale.append(f"{src} -> {dst}: no longer upward; remove it")
    assert not stale, "Allowlist entries are stale:\n" + "\n".join(stale)

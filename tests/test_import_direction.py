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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Top-level packages ordered from the most stable/dependency-free layer to the
# most volatile application layer. A package may import its own layer or any
# lower one; importing a higher layer is forbidden.
#
#   shared       models/constants/database/logging — the leaf everything uses
#   risk         risk engine and models, built on shared
#   data_tools   market-data download/catalog/resample, built on shared
#   signal_bots  signal ingestion producing risk.models.TradeSignal; consumed
#                by runner (signal replay), scheduler (health checks) and
#                live_runner (daemon registry). Scheduled for removal.
#   runner       backtest engines and execution on risk + data_tools;
#                live_runner reuses the engine for internal strategies
#   scheduler    background jobs (data refresh, bot health) orchestrating the
#                layers below; embedded by the live daemon
#   live_runner  live execution on runner engines + risk + shared
#   validation   post-hoc comparison/validation statistics on runner.stats
#   strategies   user strategy modules; may only use shared helpers
#   viewer       FastAPI app composing the whole stack
#   scripts      operator CLIs
LAYERS: dict[str, int] = {
    "shared": 0,
    "risk": 1,
    "data_tools": 2,
    "signal_bots": 3,
    "runner": 4,
    "scheduler": 5,
    "live_runner": 6,
    "validation": 7,
    "strategies": 8,
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
        ("shared.venue_calendar", "data_tools.downloaders.dukascopy_native"),
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


def _cross_package_edges() -> list[tuple[str, str, Path, int]]:
    """All (importer module, imported module, file, line) edges between
    different top-level packages."""
    modules = _repo_modules()
    edges: set[tuple[str, str, Path, int]] = set()
    for module, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        is_init = path.name == "__init__.py"
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            src_package = module.split(".")[0]
            for target in _import_targets(node, module, is_init):
                resolved = _resolve(target, modules)
                if resolved is None or resolved == module:
                    continue
                dst_package = resolved.split(".")[0]
                if dst_package == src_package or dst_package not in LAYERS:
                    continue
                edges.add((module, resolved, path, node.lineno))
    return sorted(edges)


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

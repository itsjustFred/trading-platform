#!/usr/bin/env python3
"""Report private functions that no test reaches, directly or transitively.

Why this exists
---------------
This is the third measurement of the same question, and the first two were both wrong in
ways that mattered. Recording the sequence because each failure mode is distinct and each
one produced a confidently false statement.

**Measure 1 - does the name appear anywhere in `tests/`?**
Over-reports. `_preflight_alpaca_account` sat on the "unmeasured" list for several batches
while `test_preflight.py` drove it through `_preflight_account` on every live-mode case. The
name is simply absent from the test source. It also under-reports, which is worse: a test
can mention a function in a comment and assert nothing, and the function then reads as
covered while no mutation of it could ever fail.

**Measure 2 - is the function the target of a `Call` node in the test corpus?**
Fixes the under-report, inherits the over-report. 55 "unreached", of which most are reached
by two or three hops: `_preflight_alpaca_account` is called by `_preflight_account`, which
tests do call. Calling a test a miss because it does not name the function it exercises
produces a list that cannot be worked off.

**Measure 3 - this one.** Build a call graph over `live_runner`, take the functions tests
call directly as roots, and propagate. A function is covered when a test can reach it by
executing the code under test, which is what "covered" has to mean for the claim to be
worth anything.

Approximations, all of which over-estimate coverage so the output under-reports rather than
pads the list:
  * An attribute call (`self._foo()`, `obj._bar()`) resolves to *any* function of that name
    in the module, not necessarily the right class. Over-approximates reach.
  * Calls through a variable, `getattr`, or a callable stored in a dict are invisible, so a
    dynamically dispatched function reads as uncovered.
  * A method invoked only via the class (`Cls.method`) resolves by name like any other.

Deliberately excluded: Discord, by choice. `--include-discord` disables the exclusion; do
not use it to start work on that code.

This is a static estimate. The authority on whether a function is genuinely pinned is the
mutation harness: a function can be reachable and still have no assertion that can fail.
A function listed here is definitely unreached; a function absent from this list is not
necessarily pinned.

Usage
-----
    python3 call_reach.py [--root .] [--include-discord] [--explain NAME]
"""
from __future__ import annotations

import argparse
import ast
import collections
import pathlib
import sys

EXCLUDED_MODULES = {"discord_source.py"}
SOURCE_ROOT = "live_runner"


def parse(path: pathlib.Path):
    return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))



def top_level_functions(tree: ast.Module):
    """(name, node) for functions and methods only - NOT nested closures.

    A `def` inside another `def` is a closure: it has no independent entry point and is
    only ever reached by executing its enclosing function. Counting it as its own
    uncovered unit produces phantom gaps - `_fetch` in `alpaca_adapter.py` and `_replace`
    in `config_parser.py` are both closures at column offsets 8 and 4, and both showed up
    here as unreached until this filter was added. Module-level functions and class methods
    are the units that can be called by name from a test, so they are the units that count.
    """
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent = getattr(node, "_reach_parent", None)
        if isinstance(parent, (ast.Module, ast.ClassDef)):
            out.append((node.name, node))
    return out


def tag_parents(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._reach_parent = node


def _call_targets(node: ast.AST) -> set[str]:
    """Names used as the callee of a call, from anywhere inside `node`."""
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def source_modules(root: pathlib.Path, include_discord: bool):
    """(module_name, ast.Module) for the package under test."""
    out = []
    for path in sorted((root / SOURCE_ROOT).rglob("*.py")):
        if path.name in EXCLUDED_MODULES and not include_discord:
            continue
        try:
            out.append((str(path.relative_to(root)), parse(path)))
        except SyntaxError as exc:
            print(f"  !! could not parse {path}: {exc}", file=sys.stderr)
    return out


def test_modules(root: pathlib.Path):
    out = []
    for path in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" in path.parts or "fixtures" in path.parts:
            # tests/fixtures holds deliberately-broken strategies; they are not valid
            # Python and are excluded rather than reported as parse failures.
            continue
        try:
            out.append(path)
        except Exception:  # pragma: no cover - defensive
            continue
    return out


def alias_map(modules) -> dict[str, str]:
    """Module-level `alias = <function name>` assignments, resolved to their target.

    A fourth kind of indirection, found the hard way: `daemon.py` ends with

        _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO = daemon._deployment_account_is_demo

    a self-alias through the module's own name, so that the gate cannot be swapped by
    monkeypatching the name the rest of the module calls. Tests therefore call
    `_REAL_DEPLOYMENT_ACCOUNT_IS_DEMO`, and without resolving aliases the tool reports a
    function that five killed mutations show is thoroughly pinned as unreached. That is the
    worst kind of false report: it looks like a gap and invites duplicate work on a
    safety-critical function that is in fact well covered.

    Only module-level assignments of a bare name or a dotted attribute are resolved, and
    only to a name that is a known function elsewhere - so this cannot invent reach out of
    nothing.
    """
    aliases: dict[str, str] = {}
    for _name, tree in modules:
        for node in tree.body:
            targets = None
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            if targets is None or value is None or len(targets) != 1:
                continue
            target = targets[0]
            if not isinstance(target, ast.Name):
                continue
            if isinstance(value, ast.Name):
                aliases[target.id] = value.id
            elif isinstance(value, ast.Attribute):
                aliases[target.id] = value.attr
    return aliases


def build_graph(modules):
    """name -> set of names it can call, over the whole package (name-based, so a name
    defined in two modules merges their edges - over-approximating reach)."""
    edges: dict[str, set[str]] = collections.defaultdict(set)
    for _name, tree in modules:
        tag_parents(tree)
        for name, node in top_level_functions(tree):
            edges[name] |= _call_targets(node)
    return edges


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", type=pathlib.Path)
    ap.add_argument("--include-discord", action="store_true")
    ap.add_argument("--explain", metavar="NAME",
                    help="show which test-reachable functions can reach NAME")
    args = ap.parse_args()

    root = args.root.resolve()
    modules = source_modules(root, args.include_discord)
    if not modules:
        print(f"  no source modules under {root / SOURCE_ROOT}", file=sys.stderr)
        return 2

    roots: set[str] = set()
    for path in test_modules(root):
        try:
            tree = parse(path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                roots |= _call_targets(node)
            elif isinstance(node, ast.Call):
                roots |= _call_targets(ast.Module(body=[node], type_ignores=[]))

    edges = build_graph(modules)
    # The alias map must include the TEST modules, not just the source. My first fix
    # scanned source only and the false report survived it, because the alias that hides
    # `_deployment_account_is_demo` lives in the test file itself:
    #
    #     tests/test_live_daemon.py
    #     _REAL_DEPLOYMENT_ACCOUNT_IS_DEMO = daemon._deployment_account_is_demo
    #
    # which exists precisely so the gate cannot be swapped by monkeypatching the name the
    # rest of the module calls. Collecting aliases from one side and roots from the other
    # is how the report stayed wrong after a fix that looked right.
    test_trees = []
    for path in test_modules(root):
        try:
            test_trees.append((str(path), parse(path)))
        except SyntaxError:
            continue
    aliases = alias_map(modules + test_trees)
    known = set(edges) | set(aliases.values())

    # Resolve the roots and the edges through the alias map, so a self-alias like
    # `_REAL_X = module._X` is transparent. Iterated to a fixed point because aliases may
    # chain, and a chain longer than one hop is not worth a cleverer algorithm than this.
    def resolve(name: str) -> str:
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    roots = {resolve(n) for n in roots if resolve(n) in known}
    edges = {
        resolve(name): {resolve(n) for n in outs if resolve(n) in known}
        for name, outs in edges.items()
    }
    edges = {k: v for k, v in edges.items() if v}

    # Transitive closure from the test roots.
    covered: set[str] = set()
    queue = collections.deque(roots)
    while queue:
        name = queue.popleft()
        if name in covered:
            continue
        covered.add(name)
        for nxt in edges.get(name, ()):
            if nxt not in covered:
                queue.append(nxt)

    defs: list[tuple[str, str]] = []
    for name, tree in modules:
        tag_parents(tree)
        for fname, _node in top_level_functions(tree):
            if fname.startswith("_") and not fname.startswith("__"):
                defs.append((name, fname))

    unreached = sorted({(m, n) for m, n in defs if n not in covered})
    direct = {(m, n) for m, n in defs if n in roots}

    if args.explain:
        target = args.explain
        parents = sorted(n for n, outs in edges.items() if target in outs and n in covered)
        print(f"  {target} is {'reached' if target in covered else 'NOT reached'}")
        print(f"  reached directly by a test : {target in roots}")
        print(f"  covered functions that call it: {len(parents)}")
        for p in parents[:20]:
            print(f"    {p}")
        return 0

    print(f"  source modules parsed       : {len(modules)}")
    print(f"  private functions defined   : {len(set(n for _m, n in defs))}")
    print(f"  called directly by a test   : {len({n for _m, n in direct})}")
    print(f"  reached transitively        : "
          f"{len({n for _m, n in defs if n in covered})}")
    print(f"  NOT reached at all          : {len({n for _m, n in unreached})}")
    for module, name in unreached:
        print(f"    UNREACHED  {module} :: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Guard for the live daemon's ``os._exit`` contract: no teardown hooks.

``live_runner.daemon.main`` leaves through ``os._exit`` on every path - clean
shutdown, timed-out phase and crash alike - because interpreter teardown joins
every thread and executor worker the daemon left alive, with no deadline, past
the unit's ``TimeoutStopSec``. ``os._exit`` runs none of that teardown, which is
the point: nothing the daemon needs on the way out may live there.

That makes every interpreter-teardown hook a silent break of the contract: the
hook simply never runs, and whatever it was supposed to flush, close or report
is lost without an error anywhere. A maintainer decision on the graceful
shutdown work accepted the ``os._exit`` contract on the condition that a guard
test fails if ``atexit.register`` - or a similar exit hook - appears in daemon
code, so the contract cannot be broken silently.

This file parses every ``live_runner`` module and fails on:

* ``atexit`` imports and registrations (``atexit.register``, ``__import__`` or
  ``importlib`` loading of the module);
* ``weakref.finalize`` and ``*.addfinalizer(...)`` callbacks;
* ``__del__`` methods, which interpreter teardown would otherwise run;
* signal-handler installation outside ``live_runner/shutdown.py`` - the one
  place the ``ShutdownCoordinator`` deliberately installs SIGINT/SIGTERM via
  ``signal.signal`` - including ``loop.add_signal_handler`` and self-signals
  (``raise_signal``/``pthread_kill``) anywhere;
* ``sys.exit``/``exit()``/``quit()`` and ``os._exit`` outside the two files
  that own the bounded exit path (``daemon.py``'s ``_leave_the_process`` and
  ``shutdown.py``'s second-signal ``force_exit``);
* ``multiprocessing`` and ``ProcessPoolExecutor``, whose pools register their
  own interpreter-teardown joins.

The checks are AST-based so the contract's own docstrings - which name
``atexit`` and ``__del__`` to explain why they never run - do not trip them.
"""

from __future__ import annotations

import ast
from pathlib import Path

LIVE_RUNNER = Path(__file__).resolve().parents[1] / "live_runner"

# ``os._exit`` is the contract itself; it may only be reached from the bounded
# exit path and the second-signal force exit.
OS_EXIT_ALLOWED = {"daemon.py", "shutdown.py"}

# ``signal.signal`` is the shutdown coordinator's deliberate choice (it works on
# Windows and while the loop is busy); no other daemon module may install a
# handler, and ``add_signal_handler`` is banned everywhere because the
# coordinator's docstring records why it was rejected. Self-signals are held to
# the same rule: they exist to drive the handler path, which only the
# coordinator owns.
SIGNAL_INSTALL_ALLOWED = {"shutdown.py"}
_SIGNAL_FUNCTIONS = {"signal", "raise_signal", "pthread_kill"}

_BANNED_MODULES = {"atexit", "multiprocessing"}


def _dotted(node: ast.expr, module_aliases: dict[str, str]) -> str | None:
    """The dotted path an expression refers to, or None if not module-based."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = module_aliases.get(node.id)
    if base is None:
        return None
    parts.append(base)
    return ".".join(reversed(parts))


def _scan_source(rel_path: str, source: str) -> list[str]:
    """Every teardown-hook violation in one ``live_runner`` module's source."""
    tree = ast.parse(source, filename=rel_path)
    filename = Path(rel_path).name

    # `import a.b [as c]`: c (or top package a) -> dotted module path.
    module_aliases: dict[str, str] = {}
    # `from a.b import c [as d]`: d -> (dotted module, original name).
    from_names: dict[str, tuple[str, str]] = {}
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BANNED_MODULES:
                    violations.append(
                        f"{rel_path}:{node.lineno} imports {alias.name}"
                    )
                module_aliases[alias.asname or top] = (
                    alias.name if alias.asname else top
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            for alias in node.names:
                local = alias.asname or alias.name
                from_names[local] = (module, alias.name)
                if top in _BANNED_MODULES:
                    violations.append(
                        f"{rel_path}:{node.lineno} imports from {module}"
                    )
                elif module == "weakref" and alias.name == "finalize":
                    violations.append(
                        f"{rel_path}:{node.lineno} imports weakref.finalize"
                    )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__del__":
                violations.append(f"{rel_path}:{node.lineno} defines __del__")

        if isinstance(node, ast.Attribute):
            # Any load of these attributes is the hook itself (or its handle);
            # a call adds nothing the load did not already flag.
            if _dotted(node.value, module_aliases) == "weakref" and (
                node.attr == "finalize"
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} uses weakref.finalize"
                )
            if _dotted(node.value, module_aliases) == "atexit" and (
                node.attr == "register"
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} uses atexit.register"
                )

        if not isinstance(node, ast.Call):
            continue

        func = node.func
        name: str | None = None
        module: str | None = None
        func_is_attribute = isinstance(func, ast.Attribute)
        if func_is_attribute:
            assert isinstance(func, ast.Attribute)
            name = func.attr
            module = _dotted(func.value, module_aliases)
            # `x.y.addfinalizer(...)` / `loop.add_signal_handler(...)`:
            # receiver is not a module we track, but the method name is the
            # hook regardless of what it is bound to.
            if module is None and name in {"addfinalizer", "add_signal_handler"}:
                violations.append(
                    f"{rel_path}:{node.lineno} calls .{name}()"
                )
            if name == "ProcessPoolExecutor":
                violations.append(
                    f"{rel_path}:{node.lineno} creates a ProcessPoolExecutor"
                )
        elif isinstance(func, ast.Name):
            if func.id in {"exit", "quit"}:
                violations.append(f"{rel_path}:{node.lineno} calls {func.id}()")
            resolved = from_names.get(func.id)
            if resolved is not None:
                module, name = resolved

        if module is None or name is None:
            # Dynamic module loads smuggling a banned module past the
            # import checks: __import__("atexit"), importlib.import_module(...).
            if (
                func_is_attribute
                and isinstance(func, ast.Attribute)
                and func.attr in {"__import__", "import_module"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _BANNED_MODULES
            ):
                violations.append(
                    f"{rel_path}:{node.lineno} loads "
                    f"{node.args[0].value} dynamically"
                )
            continue

        if func_is_attribute and module in {"atexit", "weakref"}:
            # `atexit.register(...)` / `weakref.finalize(...)`: the attribute
            # load above already flagged the hook; a from-imported bare name
            # reaches here with func_is_attribute False and is flagged below.
            continue
        if module in _BANNED_MODULES:
            violations.append(f"{rel_path}:{node.lineno} calls {module}.{name}()")
        elif module == "weakref" and name == "finalize":
            violations.append(f"{rel_path}:{node.lineno} calls weakref.finalize()")
        elif name == "addfinalizer":
            violations.append(f"{rel_path}:{node.lineno} calls .addfinalizer()")
        elif name == "add_signal_handler":
            violations.append(
                f"{rel_path}:{node.lineno} calls add_signal_handler()"
            )
        elif (
            module == "signal"
            and name in _SIGNAL_FUNCTIONS
            and filename not in SIGNAL_INSTALL_ALLOWED
        ):
            violations.append(
                f"{rel_path}:{node.lineno} installs a signal handler"
            )
        elif module == "os" and name == "_exit":
            if filename not in OS_EXIT_ALLOWED:
                violations.append(f"{rel_path}:{node.lineno} calls os._exit()")
        elif module == "sys" and name == "exit":
            violations.append(f"{rel_path}:{node.lineno} calls sys.exit()")
        elif module == "concurrent.futures" and name == "ProcessPoolExecutor":
            violations.append(
                f"{rel_path}:{node.lineno} creates a ProcessPoolExecutor"
            )

    return violations


def test_live_runner_modules_have_no_interpreter_teardown_hooks() -> None:
    """No ``live_runner`` module may rely on interpreter teardown running."""
    modules = sorted(LIVE_RUNNER.rglob("*.py"))
    assert modules, "live_runner/ has no modules to scan"
    violations: list[str] = []
    for path in modules:
        rel = str(path.relative_to(LIVE_RUNNER.parent))
        violations.extend(_scan_source(rel, path.read_text(encoding="utf-8")))
    assert violations == [], (
        "the daemon leaves through os._exit, so interpreter-teardown hooks never "
        "run; these would break silently:\n" + "\n".join(violations)
    )


def test_the_guard_catches_a_planted_exit_hook() -> None:
    """The detector itself is pinned: a planted hook must be reported."""
    planted = """
import atexit
import weakref
import signal
import os
import sys
from concurrent.futures import ProcessPoolExecutor


def cleanup():
    pass


class Thing:
    def __del__(self):
        pass


atexit.register(cleanup)
weakref.finalize(obj, cleanup)
obj.addfinalizer(cleanup)
signal.signal(signal.SIGTERM, handler)
loop.add_signal_handler(signal.SIGTERM, handler)
os._exit(0)
sys.exit(1)
exit()
ProcessPoolExecutor()
"""
    # Scanned as a module that owns no exit path, so even os._exit is banned.
    found = _scan_source("live_runner/config_parser.py", planted)
    joined = "\n".join(found)
    for marker in (
        "imports atexit",
        "defines __del__",
        "weakref.finalize",
        "addfinalizer",
        "installs a signal handler",
        "add_signal_handler",
        "os._exit",
        "sys.exit",
        "exit()",
        "ProcessPoolExecutor",
    ):
        assert marker in joined, f"guard missed {marker!r}:\n{joined}"


def test_the_coordinator_is_the_one_file_allowed_to_install_handlers() -> None:
    """``shutdown.py`` owns ``signal.signal``/``os._exit``; it must stay clean."""
    coordinator = (LIVE_RUNNER / "shutdown.py").read_text(encoding="utf-8")
    assert _scan_source("live_runner/shutdown.py", coordinator) == []
    daemon = (LIVE_RUNNER / "daemon.py").read_text(encoding="utf-8")
    assert _scan_source("live_runner/daemon.py", daemon) == []
